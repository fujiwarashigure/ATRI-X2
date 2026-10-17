"""工具运行时状态装配。对应 Go：``internal/agent/tools.go``。

职责：``toolRuntimeState``（provider / manager / registry / skills / config /
tool tags）、``SetToolProvider`` / ``SetToolRuntime`` / ``SetToolConfig`` /
``SetToolTagConfig`` / ``List`` / ``Unregister`` / ``Remove`` / ``Reload``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``*Agent`` 方法 → :class:`ToolRuntimeMixin` 的方法。``Agent`` 继承本 mixin
  即得到同名接口；不继承时也可以 ``ToolRuntimeMixin.set_tool_runtime(agent, ...)``
  这种"显式 self"的方式调用（鸭子类型），因此并行开发期不依赖 ``core.py`` 存在。
* 运行时状态挂在一个惰性创建的对象上（``tool_runtime_state(agent)``），
  mixin **不**定义 ``__init__``（``Agent`` 的构造归 ``core.py``）。
* ``atri_x.agent.system_prompt`` / ``tool_tag_config`` / ``resident_memory`` 属于
  其他组的文件：这里一律函数内延迟导入，缺失时降级为"不挂该来源"，不抛异常。

本模块还收拢了几个跨文件复用的小工具函数（各自注明 Go 出处），这样
``atri_x/agent/`` 内部的工具链路模块只依赖本文件，不依赖尚未落地的同僚文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from atri_x.config import default as default_config
from atri_x.tool.tool import Registry
from atri_x.toolrun import Manager

__all__ = [
    "AgentService",
    "SkillLifecycle",
    "ToolRuntimeMixin",
    "ToolRuntimeState",
    "compact_arguments",
    "first_non_empty",
    "is_background_session",
    "is_group_scope_id",
    "min_int",
    "new_tool_runtime_state",
    "normalize_string_list",
    "preview_arguments",
    "preview_runes",
    "risk_reasons_text",
    "rune_len",
    "service_host",
    "short_hash",
    "sorted_unique",
    "tool_runtime_state",
    "truncate_runes",
]

#: 工具参数预览的最大 rune 数。对应 Go ``toolLogArgumentLimit``。
TOOL_LOG_ARGUMENT_LIMIT = 256


@runtime_checkable
class SkillLifecycle(Protocol):
    """Skill 生命周期（Go ``SkillLifecycle`` 接口）。"""

    async def reload(self, ctx: Any) -> None: ...

    async def remove(self, ctx: Any, name: str) -> None: ...


class _NoopToolSchemaProvider:
    """``noopToolSchemaProvider``：没有注册表时的默认 provider。"""

    async def schemas(self, ctx: Any, mode: str, session: Any, scope: Any) -> list[Any]:
        return []


@dataclass
class ToolRuntimeState:
    """对应 Go ``toolRuntimeState``。"""

    provider: Any = field(default_factory=_NoopToolSchemaProvider)
    manager: Manager | None = None
    registry: Registry | None = None
    skills: Any = None
    config: Any = None
    tool_tags: Any = None
    default_provider: bool = False


def new_tool_runtime_state() -> ToolRuntimeState:
    """对应 Go ``newToolRuntimeState``（``config`` 默认取 ``config.Default().Tools``）。"""
    return ToolRuntimeState(provider=_NoopToolSchemaProvider(), config=default_config().tools)


_STATE_ATTR = "_tool_runtime_state"


def service_host(obj: Any) -> Any:
    """服务对象 → 真实宿主 ``Agent``。

    本组模块既以 mixin 形式挂在 ``Agent`` 上，也以"持 agent 的服务对象"形式登记到
    ``agent.services``（见 ``atri_x.agent.seams.AgentSeams``）。两种情况用同一个
    函数归一：服务对象有 ``_agent`` 就用它，否则 ``obj`` 自己是宿主。
    """
    host = getattr(obj, "_agent", None)
    return host if host is not None else obj


class AgentService:
    """服务对象的公共基类：持有宿主并把未知属性转发给宿主。

    用于把本组的 mixin 登记成 ``agent.services["group_policy"]`` 这类条目 ——
    ``AgentSeams`` 只要求服务对象上存在对应方法，其余能力（``audit`` / ``scope``
    / ``store`` …）由宿主提供。
    """

    def __init__(self, agent: Any) -> None:
        object.__setattr__(self, "_agent", agent)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            agent = object.__getattribute__(self, "_agent")
        except AttributeError:  # pragma: no cover - 未初始化
            raise AttributeError(name) from None
        return getattr(agent, name)


class _ToolRuntimeAdapter:
    """把宿主自己的 ``tool_runtime`` 对象投影成 :class:`ToolRuntimeState`。

    ``Agent``（agent-core）自带 ``_ToolRuntime``（``__slots__`` 里没有 ``skills``），
    因此这里只在宿主对象上补一个 ``_tool_skills`` 槽位，其余字段直接读写宿主对象，
    避免出现两份互不相认的工具运行时状态。
    """

    def __init__(self, target: Any, owner: Any) -> None:
        self._target = target
        self._owner = owner

    @property
    def registry(self) -> Any:
        return getattr(self._target, "registry", None)

    @registry.setter
    def registry(self, value: Any) -> None:
        _safe_set(self._target, "registry", value)

    @property
    def manager(self) -> Any:
        return getattr(self._target, "manager", None)

    @manager.setter
    def manager(self, value: Any) -> None:
        _safe_set(self._target, "manager", value)

    @property
    def provider(self) -> Any:
        return getattr(self._target, "provider", None)

    @provider.setter
    def provider(self, value: Any) -> None:
        _safe_set(self._target, "provider", value)

    @property
    def default_provider(self) -> bool:
        return bool(getattr(self._target, "default_provider", False))

    @default_provider.setter
    def default_provider(self, value: bool) -> None:
        _safe_set(self._target, "default_provider", bool(value))

    @property
    def config(self) -> Any:
        return getattr(self._target, "config", None)

    @config.setter
    def config(self, value: Any) -> None:
        _safe_set(self._target, "config", value)

    @property
    def skills(self) -> Any:
        return getattr(self._target, "skills", None) or getattr(self._owner, "_tool_skills", None)

    @skills.setter
    def skills(self, value: Any) -> None:
        if not _safe_set(self._target, "skills", value):
            _safe_set(self._owner, "_tool_skills", value)

    @property
    def tool_tags(self) -> Any:
        return getattr(self._owner, "_tool_tags", None)

    @tool_tags.setter
    def tool_tags(self, value: Any) -> None:
        _safe_set(self._owner, "_tool_tags", value)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


def tool_runtime_state(agent: Any) -> Any:
    """取工具运行时状态。

    优先复用宿主自带的 ``tool_runtime``（``agent-core`` 的 ``_ToolRuntime``）；没有
    时在 agent 上惰性创建本模块的 :class:`ToolRuntimeState`，保证并行开发期两边都可用。
    """
    host = service_host(agent)
    existing = getattr(host, "tool_runtime", None)
    if existing is not None and hasattr(existing, "registry"):
        return _ToolRuntimeAdapter(existing, host)
    state = getattr(host, _STATE_ATTR, None)
    if state is None:
        state = new_tool_runtime_state()
        _safe_set(host, _STATE_ATTR, state)
    return state


class ToolRuntimeMixin:
    """Go ``*Agent`` 的工具运行时方法组。"""

    # -- provider / registry ------------------------------------------------
    def set_tool_provider(self, provider: Any) -> None:
        """对应 Go ``SetToolProvider``。"""
        host = service_host(self)
        state = tool_runtime_state(host)
        state.provider = _NoopToolSchemaProvider() if provider is None else provider
        state.default_provider = False
        _call(host, "rebuild_system_prompt")

    def set_tool_runtime(self, registry: Registry | None, skills: Any) -> None:
        """对应 Go ``SetToolRuntime``。"""
        host = service_host(self)
        state = tool_runtime_state(host)
        state.registry = registry
        state.skills = skills
        state.manager = Manager(registry, getattr(host, "security_policy", None))
        if registry is not None:
            from atri_x.agent.toolrun_prompt_provider import ToolRunPromptProvider

            state.provider = ToolRunPromptProvider(host)
            state.default_provider = True
        _call(host, "rebuild_system_prompt")

    def set_tool_config(self, cfg: Any) -> None:
        """对应 Go ``SetToolConfig``（``MaxRoundsPerTurn`` 缺省 2）。"""
        host = service_host(self)
        if cfg is not None and getattr(cfg, "max_rounds_per_turn", 0) <= 0:
            cfg.max_rounds_per_turn = 2
        tool_runtime_state(host).config = cfg
        if hasattr(host, "tools_config"):
            _safe_set(host, "tools_config", cfg)

    def set_tool_tag_config(self, path: str, cfg: Any) -> None:
        """对应 Go ``SetToolTagConfig``。``tool_tag_config`` 未落地时降级为 None。

        Go 的 ``cfg`` 是 ``config.ToolTagsConfig``；Python 侧 ``Options.tool_tags``
        声明为 ``Any``，因此这里用 :func:`tool_tag_source_of` 归一（dict 也接受）。
        """
        host = service_host(self)
        source = None
        try:  # 同僚模块可能尚未落地 —— 延迟导入
            from atri_x.agent.tool_tag_config import ToolTagConfigSource, tool_tag_source_of

            source = ToolTagConfigSource(path, tool_tag_source_of(cfg))
        except Exception:  # noqa: BLE001 - 只影响提示词来源，不影响工具执行
            source = None
        tool_runtime_state(host).tool_tags = source
        _call(host, "rebuild_system_prompt")

    # -- 查询 / 卸载 --------------------------------------------------------
    def list(self) -> list[Any]:
        """对应 Go ``List``。"""
        registry = tool_runtime_state(service_host(self)).registry
        if registry is None:
            return []
        return registry.list()

    def unregister(self, name: str) -> None:
        """对应 Go ``Unregister``。"""
        registry = tool_runtime_state(service_host(self)).registry
        if registry is None:
            return
        registry.unregister(name)

    async def remove(self, ctx: Any, name: str) -> None:
        """对应 Go ``Remove``。"""
        host = service_host(self)
        state = tool_runtime_state(host)
        if state.skills is None or state.registry is None:
            ToolRuntimeMixin.unregister(self, name)
            return
        await state.skills.remove(ctx, name)

    async def reload(self, ctx: Any) -> None:
        """对应 Go ``Reload``。"""
        state = tool_runtime_state(service_host(self))
        if state.skills is None or state.registry is None:
            return
        await state.skills.reload(ctx)

    # -- 系统提示词 ---------------------------------------------------------
    def rebuild_system_prompt(self) -> None:
        """对应 Go ``rebuildSystemPrompt``。

        ``SystemPromptManager`` 及其各来源属 agent-model 组；未落地时本方法
        只记录状态（``prompt_builder.system`` 保持原值），不影响工具执行。

        来源顺序与 Go 逐行一致（``tools.go:33-44``）：SOUL → 工具名 → 工具标签 →
        常驻记忆 → 对话元信息。注意 :class:`SystemPromptManager` 收的是**序列**，
        单个来源必须走 ``add_source``。
        """
        try:  # 同僚模块可能尚未落地 —— 延迟导入
            from atri_x.agent.system_prompt import SystemPromptManager
            from atri_x.agent.system_prompt_sources import (
                ConversationMetaSystemPromptSource,
                ResidentMemorySystemPromptSource,
                SoulSystemPromptSource,
                ToolNamesSystemPromptSource,
            )
        except Exception:  # noqa: BLE001
            return
        host = service_host(self)
        state = tool_runtime_state(host)
        manager = SystemPromptManager()
        manager.add_source(SoulSystemPromptSource(getattr(host, "soul", None)))
        if hasattr(state.provider, "tool_names"):
            manager.add_source(ToolNamesSystemPromptSource(state.provider))
        if state.tool_tags is not None:
            manager.add_source(state.tool_tags)
        manager.add_source(
            ResidentMemorySystemPromptSource(getattr(host, "resident_memory", None))
        )
        manager.add_source(ConversationMetaSystemPromptSource())
        builder = getattr(host, "prompt_builder", None)
        if builder is not None:
            builder.system = manager


def _call(host: Any, name: str, *args: Any) -> Any:
    """调用宿主上的可选方法（不存在即空操作）。"""
    helper = getattr(host, name, None)
    if helper is None:
        return None
    return helper(*args)


# ---------------------------------------------------------------------------
# 共享小工具（Go 里散落在 chat_tools.go / identity.go / session_metadata.go）
# ---------------------------------------------------------------------------
def first_non_empty(*values: Any) -> str:
    """第一个非空字符串。对应 Go ``firstNonEmpty``（identity.go）。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def short_hash(value: str) -> str:
    """FNV-1a 32 位十六进制摘要。对应 Go ``shortHash``（tool_auth.go）。"""
    digest = 0x811C9DC5
    for byte in str(value or "").encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return format(digest, "x")


def sorted_unique(values: Sequence[str] | None) -> list[str]:
    """去空去重后按字典序排序。对应 Go ``sortedUnique``（session_metadata.go）。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        text = value if isinstance(value, str) else str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    out.sort()
    return out


def normalize_string_list(values: Sequence[str] | None) -> list[str]:
    """去空去重、保持出现顺序。对应 Go ``normalizeStringList``（group_policy.go）。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def min_int(left: int, right: int) -> int:
    """对应 Go ``minInt``（group_knowledge.go）。"""
    return left if left < right else right


def rune_len(value: str) -> int:
    """rune 长度。对应 Go ``runeLen``（group_knowledge.go）。"""
    return len(str(value or ""))


def truncate_runes(value: str, max_runes: int) -> str:
    """按 rune 截断。对应 Go ``truncateRunes``（group_knowledge.go）。"""
    text = str(value or "")
    if max_runes <= 0:
        return text
    return text[:max_runes]


def preview_runes(text: str, max_runes: int) -> str:
    """按 rune 预览并追加省略号。对应 Go ``previewRunes``（chat_tools.go）。"""
    text = str(text or "").strip()
    if not text or max_runes <= 0:
        return ""
    if len(text) <= max_runes:
        return text
    return text[:max_runes] + "..."


def compact_arguments(args: str) -> str:
    """压缩 JSON 参数（失败时原样返回）。对应 Go ``compactArguments``。"""
    text = str(args or "").strip()
    if not text:
        return "{}"
    try:
        return json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        return text


def preview_arguments(args: str) -> str:
    """供用户与日志看的参数预览。对应 Go ``previewArguments``。"""
    return preview_runes(compact_arguments(args), TOOL_LOG_ARGUMENT_LIMIT)


def risk_reasons_text(reasons: Sequence[str] | None) -> str:
    """风险原因行。对应 Go ``riskReasonsText``（chat_tools.go）。"""
    if not reasons:
        return ""
    lines = ["\n风险原因："]
    for reason in reasons:
        text = str(reason or "").strip()
        if text:
            lines.append("\n- ")
            lines.append(text)
    return "".join(lines)


def is_background_session(session: Any) -> bool:
    """该 Session 是否是后台任务工作区。对应 Go ``isBackgroundSession``。"""
    if session is None:
        return False
    try:
        from atri_x.session import background_kind

        return bool(background_kind(getattr(session, "metadata", "") or ""))
    except Exception:  # noqa: BLE001 - 会话模块未落地时退回本地解析
        return bool(_local_background_kind(getattr(session, "metadata", "") or ""))


def _local_background_kind(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("background_kind") or "").strip()


def is_group_scope_id(scope_id: str) -> bool:
    """群作用域 ID 判定。对应 Go ``isGroupScopeID``（group_runtime.go）。"""
    text = str(scope_id or "").strip()
    return text.startswith("group:") or text.startswith("supergroup:")
