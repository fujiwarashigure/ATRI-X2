"""模型运行时状态、模式模型选择、客户端查找与重试通知。

对应 Go：``internal/agent/model.go``（状态结构、``Current*`` / ``Select*`` /
``applyModeModelSelection`` / ``llmOriginFor`` / ``checkModelSwitch`` /
``attachLLMRetryNotifier``）。模型列表缓存与聚合在 :mod:`atri_x.agent.model_list`，
``state.toml`` 热加载在 :mod:`atri_x.agent.model_state`。

**组合方式**：本模块提供 :class:`ModelMixin`，由 ``atri_x.agent`` 的 ``Agent``
与 :class:`atri_x.agent.model_list.ModelListMixin`、
:class:`atri_x.agent.model_state.RuntimeStateMixin` 一起组合。它按鸭子类型使用宿主
（``Agent``）上的这些成员：

* ``self.model_runtime``（:class:`ModelRuntimeState`，缺失时 :func:`ensure_model_runtime` 会补建）
* ``self.sessions``（``session.Service``：``current(scope)`` / ``default_mode()``）
* ``self.title_gen``（可选：``set_primary(client, model)`` / ``set_naming(client, provider, model)``）
* ``self.scope(ctx)`` / ``self.command_prefix()`` / ``self.audit(event, **attrs)``
* ``self.record_provider_retry(ctx, provider)``（:class:`atri_x.agent.budget.BudgetMixin`）
* ``self.send_notice(ctx, notice)``（可选；缺失时只记日志）
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import threading
import types
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from atri_x.config import ModelSelection, ProviderConfig
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import RetryEvent
from atri_x.llm.origin import Origin, can_switch
from atri_x.llm.protocol import protocol_capabilities_of
from atri_x.storage.models import SessionModeChat, SessionModeWork

#: 用户可见错误的 rune 上限。对应 Go ``maxUserErrorRunes``（errors.go）。
MAX_USER_ERROR_RUNES = 400

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 命令层可见的模型视图（对应 Go ``internal/agent/commands`` 的同名类型）
#
# **唯一出处**是 :mod:`atri_x.agent.commands.model_types`：命令层与模型层必须共用同一份
# 类对象，否则 ``==`` / ``isinstance`` 跨模块静默失效（PORTING.md §3.1）。
# ``commands/__init__.py`` 只是把这些名字再导出一次。此处刻意只 import 叶子模块
# ``commands.model_types``（它对 ``atri_x.agent.model`` 零 import），因此不构成导入环。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于同僚的 commands 包是否已落地
    from atri_x.agent.commands.model_types import (  # type: ignore  # noqa: F401
        ModelListOptions,
        ModelListResult,
        ModelOption,
        ModelProfile,
        ModelProviderError,
        ModelSnapshot,
        ModelSnapshotSlot,
    )
except Exception:  # noqa: BLE001 - commands 包并行移植中：缺失时用字段等价的本地定义
    @dataclass
    class ModelOption:
        """一个可选模型。对应 Go ``agentcommands.ModelOption``。"""

        index: int = 0
        provider: str = ""
        model: str = ""
        current: bool = False
        chat_current: bool = False
        work_current: bool = False
        mode_marks: list[str] = field(default_factory=list)
        compact: bool = False
        naming: bool = False

    @dataclass
    class ModelProviderError:
        """某个 provider 列举模型时的失败。对应 Go ``ModelProviderError``。"""

        provider: str = ""
        err: BaseException | None = None

    @dataclass
    class ModelListOptions:
        """``/model --refresh`` 的开关。对应 Go ``ModelListOptions``。"""

        fresh: bool = False

    @dataclass
    class ModelListResult:
        """一次模型列举的结果。对应 Go ``ModelListResult``。"""

        options: list[ModelOption] = field(default_factory=list)
        errors: list[ModelProviderError] = field(default_factory=list)

    @dataclass
    class ModelProfile:
        """一个命名模型档（``services.toml`` 的 model_profiles / 别名）。"""

        name: str = ""
        provider: str = ""
        model: str = ""
        available: bool = False

    @dataclass
    class ModelSnapshotSlot:
        """命名快照里的一个槽位（模式名或 compact / naming）。"""

        label: str = ""
        provider: str = ""
        model: str = ""

    @dataclass
    class ModelSnapshot:
        """一组命名保存的模型选择（``/model --save``）。"""

        name: str = ""
        slots: list[ModelSnapshotSlot] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 共享助手（唯一出处分别在 core.go / identity.go：函数内延迟导入，缺失时本地兜底）
# ---------------------------------------------------------------------------
def _shared(module: str, name: str) -> Any:
    """取同僚模块里的共享助手；模块不存在或没这个名字时返回 ``None``。"""
    try:
        mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


def clone_mode_models(
    models: Mapping[str, ModelSelection] | None,
) -> dict[str, ModelSelection]:
    """浅拷贝「模式 → 模型选择」表。对应 Go ``cloneModeModels``（定义在 ``core.go``）。"""
    if not models:
        return {}
    shared = _shared("atri_x.agent.core", "clone_mode_models")
    if shared is not None and shared is not clone_mode_models:
        result = shared(models)
        if isinstance(result, dict):
            return result
    return {mode: _copy_selection(selection) for mode, selection in models.items()}


def _copy_selection(selection: ModelSelection | None) -> ModelSelection:
    if isinstance(selection, ModelSelection):
        return selection.copy()
    return ModelSelection(
        provider=str(getattr(selection, "provider", "") or ""),
        model=str(getattr(selection, "model", "") or ""),
    )


def model_selection_key(provider: str, model: str) -> str:
    """模式标记的键。对应 Go ``modelSelectionKey``（``\\x00`` 分隔，避免歧义）。"""
    return f"{provider}\x00{model}"


def join_model_matches(matches: Sequence[ModelOption]) -> str:
    """把候选模型拼成 ``[1] provider/model, ...``。对应 Go ``joinModelMatches``。"""
    return ", ".join(f"[{m.index}] {m.provider}/{m.model}" for m in matches)


def format_delay(seconds: float) -> str:
    """把秒数渲染成 Go ``time.Duration.Round(time.Millisecond).String()`` 的样子。"""
    if seconds <= 0:
        return "0s"
    milliseconds = int(round(seconds * 1000))
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    whole, remainder = divmod(milliseconds, 1000)
    if remainder == 0:
        return f"{whole}s"
    text = f"{whole}.{remainder:03d}".rstrip("0")
    return f"{text}s"


# ---------------------------------------------------------------------------
# 运行态
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 与宿主（Agent）之间的装配辅助
# ---------------------------------------------------------------------------
def host_of(obj: Any) -> Any:
    """取「真正的宿主」。

    :class:`AgentService` 门面把 Agent 包在里面（``_agent``）；mixin 方法里的
    ``self`` 既可能是 Agent 本身，也可能是门面。凡是**可能与同名方法撞名的宿主字段**
    （``model_profiles`` / ``model_aliases`` / …）都必须经这里取，否则会读到方法而不是
    字段。非门面对象原样返回。
    """
    agent = getattr(obj, "_agent", None)
    return agent if agent is not None else obj


class AgentService:
    """把 :class:`atri_x.agent.core.Agent` 包成可注册进 ``agent.services`` 的门面。

    存在的理由：``Agent`` 自己也持有 ``model_profiles`` / ``model_aliases`` 这类**字典
    字段**，与同名方法冲突；门面把方法留在类上、把字段读写转发给 Agent，两边就都成立。

    读写规则：

    * ``self._x``（下划线开头）留在门面上；
    * 其它属性**读**优先命中门面类上的方法，其次转发给 Agent；
    * 其它属性**写**一律落到 Agent 上，保证所有服务看到同一份状态。
    """

    def __init__(self, agent: Any) -> None:
        object.__setattr__(self, "_agent", agent)

    @property
    def agent(self) -> Any:
        """被包装的宿主。"""
        return object.__getattribute__(self, "_agent")

    def __getattr__(self, name: str) -> Any:
        if name == "_agent":
            raise AttributeError(name)
        agent = object.__getattribute__(self, "_agent")
        return getattr(agent, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, "_agent"), name, value)


def install_methods(
    agent: Any, owner: type, names: Iterable[str], *, overwrite: bool = False
) -> list[str]:
    """把 ``owner`` 上的方法绑到 ``agent`` 实例上（缺什么装什么）。

    这是给**鸭子类型调用点**用的：``state_watch`` 取 ``agent.refresh_runtime_state``、
    ``tool_auth`` 取 ``agent.reserve_budget_request``、``group_policy`` 取
    ``agent.save_runtime_state``。方法体里的 ``self`` 就是 Agent，因此与 mixin 组合
    等价。返回实际安装的方法名。
    """
    installed: list[str] = []
    for name in names:
        if not overwrite and getattr(agent, name, None) is not None:
            continue
        fn = getattr(owner, name, None)
        if fn is None:
            continue
        setattr(agent, name, types.MethodType(fn, agent))
        installed.append(name)
    return installed


class ModelRuntimeState:
    """进程内的模型运行态。对应 Go ``modelRuntimeState``。

    锁的粒度与 Go 一致：选择相关字段（``mode_models`` / ``provider`` / ``model`` /
    ``llm_client``）由 ``selection_mu`` 保护，客户端表由 ``clients_mu`` 保护。

    字段名以 Go 为准（``llm_client`` / ``provider_name`` / ``provider``）；为兼容
    ``core.py`` 的 ``_FallbackModelRuntime`` 命名，额外提供 ``client`` /
    ``provider_config`` 两个只读别名。
    """

    def __init__(
        self,
        client: Any = None,
        model: str = "",
        provider_name: str = "",
        provider: ProviderConfig | None = None,
        providers: Mapping[str, ProviderConfig] | None = None,
        mode_models: Mapping[str, ModelSelection] | None = None,
        clients: Mapping[str, Any] | None = None,
    ) -> None:
        self.llm_client = client
        self.model = model
        self.provider_name = provider_name
        self.provider = provider
        self.providers: dict[str, ProviderConfig] = dict(providers or {})
        self.mode_models: dict[str, ModelSelection] = clone_mode_models(mode_models)
        self.clients: dict[str, Any] = dict(clients or {})
        self.all_models: list[str] = []
        #: 模型列表缓存与合并（对应 Go 的 modelListCache / modelListInflight / modelListMu）。
        from atri_x.agent.model_list import ModelListCache

        self.model_list = ModelListCache()
        self.selection_mu = threading.RLock()
        self.clients_mu = threading.RLock()

    # -- 模式模型 -----------------------------------------------------------
    def provider_for(self, provider_name: str) -> ProviderConfig | None:
        """按名字取 provider 配置。对应 Go ``a.modelRuntime.providers[name]`` 的存在性判定。"""
        return self.providers.get(provider_name.strip())

    def model_for_mode(self, mode: str) -> ModelSelection:
        """某个模式槽位的选择；未配置时退回 work 槽位。对应 Go ``modelForMode``。"""
        with self.selection_mu:
            selected = self.mode_models.get(mode)
            if selected is None or not selected.provider or not selected.model:
                return _copy_selection(self.mode_models.get(SessionModeWork))
            return _copy_selection(selected)

    def mode_models_snapshot(self) -> dict[str, ModelSelection]:
        """内存副本，供落盘。对应 Go ``modeModelsSnapshot``。"""
        with self.selection_mu:
            return clone_mode_models(self.mode_models)

    def set_mode_models(self, models: Mapping[str, ModelSelection] | None) -> None:
        """用文件里的内容替换模式选择表。对应 Go ``applyRuntimeState`` 的赋值。"""
        with self.selection_mu:
            self.mode_models = clone_mode_models(models)

    def apply_mode_model_selection(
        self, mode: str, provider_name: str, model: str
    ) -> ProviderConfig:
        """切某个模式槽位，并把该 provider/model 记为进程主选择。

        对应 Go ``applyModeModelSelection``。provider 不存在时抛
        :data:`atri_x.errors.ErrNotFound`。
        """
        provider = self.providers.get(provider_name)
        if provider is None:
            raise NotFoundError(f'provider "{provider_name}" not found')
        client = self.client_for_provider(provider_name)
        with self.selection_mu:
            self.mode_models[mode] = ModelSelection(provider=provider_name, model=model)
            self.provider_name = provider_name
            self.provider = provider
            self.model = model
            self.llm_client = client
        return provider

    def client_for_provider(self, provider_name: str) -> Any:
        """按 provider 取客户端。对应 Go ``clientForProvider``。"""
        with self.clients_mu:
            return self.clients.get(provider_name)

    def selection(self) -> tuple[str, str, Any]:
        """当前主选择 ``(provider_name, model, client)``（读锁快照）。"""
        with self.selection_mu:
            return self.provider_name, self.model, self.llm_client

    # -- 兼容别名（core.py 的 _FallbackModelRuntime 用这组名字） ---------------
    @property
    def client(self) -> Any:
        """``llm_client`` 的别名。"""
        return self.llm_client

    @client.setter
    def client(self, value: Any) -> None:
        with self.selection_mu:
            self.llm_client = value

    @property
    def provider_config(self) -> ProviderConfig | None:
        """``provider`` 的别名。"""
        return self.provider

    @provider_config.setter
    def provider_config(self, value: ProviderConfig | None) -> None:
        with self.selection_mu:
            self.provider = value


def new_model_runtime_state(
    client: Any = None,
    model: str = "",
    provider_name: str = "",
    provider: ProviderConfig | None = None,
    providers: Mapping[str, ProviderConfig] | None = None,
    mode_models: Mapping[str, ModelSelection] | None = None,
    clients: Mapping[str, Any] | None = None,
) -> ModelRuntimeState:
    """构造运行态（参数顺序与 Go ``newModelRuntimeState`` 一致）。

    ``atri_x.agent.core._new_model_runtime`` 按这个顺序位置传参，是它与本模块的接缝。
    """
    return ModelRuntimeState(
        client, model, provider_name, provider, providers, mode_models, clients
    )


def ensure_model_runtime(host: Any) -> ModelRuntimeState:
    """取宿主的模型运行态；缺失时补建（便于 mixin 单独使用）。"""
    state = getattr(host, "model_runtime", None)
    if isinstance(state, ModelRuntimeState):
        return state
    state = ModelRuntimeState()
    host.model_runtime = state
    return state


def install_methods_from(
    agent: Any, module_name: str, class_name: str, names: Iterable[str]
) -> list[str]:
    """:func:`install_methods` 的延迟导入版本（避免兄弟模块的导入环）。"""
    module = importlib.import_module(module_name)
    owner = getattr(module, class_name)
    return install_methods(agent, owner, names)


def audit_event(host: Any, event: str, **attrs: Any) -> None:
    """写一条审计。

    ``Agent`` 自己没有 ``audit`` 方法（Go 的方法在 Python 侧是模块级函数
    :func:`atri_x.agent.logging.audit`，参数是 ``key, value`` 位置对）。宿主提供
    ``audit`` 方法时优先用它（kwargs 形式），否则走模块函数。
    """
    agent = host_of(host)
    fn = getattr(agent, "audit", None)
    if callable(fn):
        try:
            fn(event, **attrs)
            return
        except TypeError:  # pragma: no cover - 宿主 audit 只吃位置对
            pass
    try:
        from atri_x.agent.logging import audit as module_audit
    except Exception:  # noqa: BLE001 - 审计不可用时静默跳过
        return
    pairs: list[Any] = []
    for key, value in attrs.items():
        pairs.extend([key, value])
    try:
        module_audit(agent, event, *pairs)
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


__all__ = [
    "INSTALLABLE_MODEL_METHODS",
    "MAX_USER_ERROR_RUNES",
    "AgentService",
    "ModelListOptions",
    "ModelListResult",
    "ModelMixin",
    "ModelOption",
    "ModelProfile",
    "ModelProviderError",
    "ModelRuntimeState",
    "ModelService",
    "ModelSnapshot",
    "ModelSnapshotSlot",
    "clone_mode_models",
    "ensure_model_runtime",
    "format_delay",
    "host_of",
    "audit_event",
    "install_methods",
    "install_methods_from",
    "install_model_services",
    "join_model_matches",
    "model_selection_key",
    "new_model_runtime_state",
]


#: 与 :mod:`atri_x.agent.model_service` 互为环的四个名字。
#:
#: ``model_service`` import 本模块（``AgentService`` / ``ModelProfile`` …），所以本模块
#: 不能在文件顶部 import 它。但这四个名字是历史导入路径的一部分
#: （``from atri_x.agent.model import ModelMixin`` 之类），因此用模块级 ``__getattr__``
#: 在**首次访问时**转发，既保住 ``__all__`` 名不虚传，也不制造导入环。
_LAZY_FROM_MODEL_SERVICE: dict[str, str] = {
    "INSTALLABLE_MODEL_METHODS": "INSTALLABLE_MODEL_METHODS",
    "ModelMixin": "ModelMixin",
    "ModelService": "ModelService",
    "install_model_services": "install_model_services",
}


def __getattr__(name: str) -> Any:
    """按需从 :mod:`atri_x.agent.model_service` 取回上面那四个名字。"""
    attribute = _LAZY_FROM_MODEL_SERVICE.get(name)
    if attribute is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from atri_x.agent import model_service

    value = getattr(model_service, attribute)
    globals()[name] = value
    return value
