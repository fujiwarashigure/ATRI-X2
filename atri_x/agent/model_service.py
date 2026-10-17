"""模型服务的门面与装配（从 ``model.py`` 尾部拆出）。

对应 Go：``internal/agent/model.go`` 的 ``Agent`` 模型方法在 Python 侧除了 mixin 还有
两处装配入口 —— 命令层的 ``Deps.models``（:class:`ModelService`）与鸭子类型调用点
（``state_watch`` / ``tool_auth`` 之类用 ``getattr(agent, ...)`` 取方法）。

``model_profiles`` 必须留在门面上：Agent 自己的 ``model_profiles`` 字段是字典，实例字段
会盖住同名方法。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.model import (
    AgentService,
    ModelListOptions,
    ModelListResult,
    ModelOption,
    ModelProfile,
    ModelSnapshot,
    install_methods,
    install_methods_from,
)
from atri_x.agent.model_agent import ModelMixin

__all__ = [
    "INSTALLABLE_MODEL_METHODS",
    "ModelService",
    "install_model_services",
]


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 服务门面与装配
# ---------------------------------------------------------------------------
#: 可以装到 Agent 实例上的方法（**不含**与 Agent 字段撞名的 ``model_profiles``）。
INSTALLABLE_MODEL_METHODS: tuple[str, ...] = (
    "current_model",
    "current_provider",
    "current_mode_model",
    "current_model_for_mode",
    "current_compact_model",
    "current_naming_model",
    "current_mode",
    "select_model",
    "select_model_for_mode",
    "select_compact_model",
    "select_naming_model",
    "select_model_option",
    "apply_mode_model_selection",
    "model_for_mode",
    "model_mode_marks",
    "llm_origin_for",
    "check_model_switch",
    "attach_llm_retry_notifier",
    "_on_llm_retry",
    "_current_session",
    "configured_naming_model",
    "set_naming_model",
    "models",
    "model_list",
    "model_options",
    "cached_provider_models",
    "model_snapshots",
    "save_model_snapshot",
    "apply_model_snapshot",
    "delete_model_snapshot",
    "authorize_execution_model_selection",
    "merge_external_runtime_state",
    "save_runtime_state",
    "refresh_runtime_state",
    "load_runtime_state_at_startup",
    "apply_runtime_state",
    "publish_runtime_state",
    "runtime_state_digest",
    "mode_models_snapshot",
)


class ModelService(AgentService):
    """模型主题的服务门面。

    :meth:`model_profiles` 必须留在门面上，因为 Agent 自己的 ``model_profiles`` 字段是
    字典，实例字段会盖住同名方法。命令层真正会调到的其余方法（:meth:`models` /
    :meth:`model_list` / :meth:`model_snapshots` / :meth:`select_model` …）在这里**显式
    声明一遍**再转发给宿主：

    * ``typing.Protocol`` 的 ``runtime_checkable`` 判定只看 ``type(obj)`` 上的属性，
      纯 ``__getattr__`` 转发会被判成 ``False``（"装了个壳"就看不出来了）；
    * 顺便把命令层实际用到的协议面固定下来，便于交叉检索。
    """

    def model_profiles(self) -> list[ModelProfile]:
        """列出命名模型档。对应 Go ``Agent.ModelProfiles``。"""
        from atri_x.agent.model_list import model_profiles_of

        return model_profiles_of(self.agent)

    # -- Deps.ModelService 的其余方法（命令层 / 补全真正调用的那几个） ---------
    def current_model(self) -> str:
        return self.agent.current_model()

    def current_provider(self) -> str:
        return self.agent.current_provider()

    def current_mode_model(self) -> ModelOption:
        return self.agent.current_mode_model()

    def current_model_for_mode(self, mode: str) -> ModelOption:
        return self.agent.current_model_for_mode(mode)

    def current_compact_model(self) -> ModelOption:
        return self.agent.current_compact_model()

    def current_naming_model(self) -> ModelOption:
        return self.agent.current_naming_model()

    async def select_model(self, ctx: Any, arg: str) -> ModelOption:
        return await self.agent.select_model(ctx, arg)

    async def select_model_for_mode(
        self, ctx: Any, mode: str, arg: str
    ) -> ModelOption:
        return await self.agent.select_model_for_mode(ctx, mode, arg)

    async def select_compact_model(self, arg: str) -> ModelOption:
        return await self.agent.select_compact_model(arg)

    async def select_naming_model(self, arg: str) -> ModelOption:
        return await self.agent.select_naming_model(arg)

    def models(self, query: str) -> list[ModelOption]:
        return self.agent.models(query)

    def model_list(self, query: str, opts: ModelListOptions) -> ModelListResult:
        return self.agent.model_list(query, opts)

    def model_snapshots(self) -> list[ModelSnapshot]:
        return self.agent.model_snapshots()

    def save_model_snapshot(self, name: str) -> None:
        self.agent.save_model_snapshot(name)

    async def apply_model_snapshot(self, ctx: Any, name: str) -> ModelSnapshot:
        return await self.agent.apply_model_snapshot(ctx, name)

    def delete_model_snapshot(self, name: str) -> None:
        self.agent.delete_model_snapshot(name)


def install_model_services(agent: Any) -> ModelService:
    """装配模型服务。

    1. 把模型方法装到 ``agent`` 实例上（缺什么装什么，已被同僚实现的照旧）；
    2. 注册门面到 ``agent.services['models']``（命令层 ``Deps.models`` 用它）。

    **幂等**：重复调用不会换掉已经注册的门面 —— 已经别处拿到的门面引用必须继续有效。
    """
    from atri_x.agent.model_list import MODEL_LIST_METHODS
    from atri_x.agent.model_snapshot import SNAPSHOT_METHODS
    from atri_x.agent.model_state import RUNTIME_STATE_METHODS

    install_methods(agent, ModelMixin, INSTALLABLE_MODEL_METHODS)
    install_methods_from(
        agent, "atri_x.agent.model_list", "ModelListMixin", MODEL_LIST_METHODS
    )
    install_methods_from(
        agent, "atri_x.agent.model_snapshot", "ModelSnapshotMixin", SNAPSHOT_METHODS
    )
    install_methods_from(
        agent,
        "atri_x.agent.model_policy",
        "ModelPolicyMixin",
        ("authorize_execution_model_selection",),
    )
    install_methods_from(
        agent, "atri_x.agent.model_state", "RuntimeStateMixin", RUNTIME_STATE_METHODS
    )
    services = getattr(agent, "services", None)
    existing = services.get("models") if isinstance(services, dict) else None
    if isinstance(existing, ModelService):
        return existing
    service = ModelService(agent)
    if isinstance(services, dict):
        services["models"] = service
    return service
