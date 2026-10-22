"""``@tool:`` / ``@skill:`` 指令：预载工具与 Skill。

对应 Go：``internal/agent/tool_directive.go``。

**同步/异步的取舍**（Python 存储层的既定形状造成的，必须在集成时知道）：

* ``input.py`` 按 ``AgentSeams`` 的同步契约调用 ``apply_tool_directives`` /
  ``apply_skill_directives``（不 ``await``），而工具发现（``DiscoverDetails`` /
  ``LoadDiscoveryContent``）与 metadata 落库（``remember_cached_tools`` /
  ``persist_tool_tags``）在 Python 侧都是 ``async``。
* 因此本模块提供两套：同步版（:func:`apply_tool_directives`）完成匹配、剥除指令、
  注册表内的 schema 注入与策略裁决，并把异步的落库/文案加载**排进事件循环**；异步版
  （:func:`apply_tool_directives_async`）是完整保真实现，供调用点改成 ``await`` 后使用。

指令被剥除后，``@tool:`` 的发现文案会拼到正文后面；当前会话模式不支持工具时
整条指令按"不可用"处理。本仓 ``work`` / ``chat`` 两种模式都支持工具，所以正常情况
不会再触发"模式阻止"，该分支只作为未知/后台模式的兜底。
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from atri_x.agent.model import AgentService, host_of, install_methods
from atri_x.directive import (
    TOOL_FULL_PREFIX,
    TOOL_PREFIX,
    TOOL_SHORT_FULL,
    TOOL_SHORT_PREFIX,
    strip_tool_matches,
    tool_matches,
)
from atri_x.logging import ResultRejected, ResultSkipped
from atri_x.storage.models import session_mode_supports_tools
from atri_x.tool import (
    TOOL_REASON_RISK_DENIED,
    TOOL_REASON_SUPERADMIN_ONLY,
    DiscoveredTool,
    DiscoveryResult,
    LazyDetailProvider,
    PublicInfo,
    Tool,
    ToolReasonHidden,
    ToolReasonNoSchema,
    ToolReasonNotFound,
    can_access_tool,
    default_policy,
    info_available_in_context,
    with_workspace_store,
)
from atri_x.tool.availability import tool_access_reason

__all__ = [
    "TOOL_DIRECTIVE_METHODS",
    "ToolDirectiveMixin",
    "ToolDirectiveResult",
    "ToolDirectiveService",
    "apply_tool_directives",
    "apply_tool_directives_async",
    "install_tool_directive_services",
    "notify_tool_directive_result",
    "preload_root_reason",
    "preload_skip_result",
    "preload_tool_names",
]

@dataclass
class ToolDirectiveResult:
    """``@tool:`` 的处理结果。对应 Go ``toolDirectiveResult``。

    字段是 ``atri_x.agent.seams.DirectiveOutcome`` 的超集，两处可以互换消费。
    """

    text: str = ""
    injected: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    mode_blocked: bool = False
    err: BaseException | None = None
    applied: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    injected_wrappers: list[str] = field(default_factory=list)
    existing_wrappers: list[str] = field(default_factory=list)


def _outcome(**fields: Any) -> Any:
    """优先用 ``seams.DirectiveOutcome``（唯一出处），缺失时用本地结果类型。"""
    try:
        from atri_x.agent.seams import DirectiveOutcome
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return ToolDirectiveResult(**fields)
    known = {key: value for key, value in fields.items() if key in DirectiveOutcome.__dataclass_fields__}
    return DirectiveOutcome(**known)


# ---------------------------------------------------------------------------
# 预载准入
# ---------------------------------------------------------------------------
def preload_root_reason(actor: Any, policy: Any, candidate: Tool) -> str:
    """``@tool:`` 根工具被拒绝的原因（可用时返回空串）。对应 Go ``preloadRootReason``。"""
    info = candidate.info()
    if info.name == "discover_tool":
        return ToolReasonHidden
    reason = tool_access_reason(actor, policy, info)
    if reason:
        return reason
    if info.hidden:
        return ToolReasonHidden
    if isinstance(candidate, LazyDetailProvider) or _is_detail_provider(candidate):
        return ToolReasonHidden
    return ""


def _is_detail_provider(candidate: Any) -> bool:
    detail = getattr(candidate, "detail", None)
    activate = getattr(candidate, "activate_tools", None)
    return callable(detail) or callable(activate)


def preload_skip_result(reason: str) -> str:
    """跳过预载的审计 ``result``。对应 Go ``preloadSkipResult``（``cron.go``）。"""
    if reason in (TOOL_REASON_RISK_DENIED, TOOL_REASON_SUPERADMIN_ONLY):
        return ResultRejected
    return ResultSkipped


# ---------------------------------------------------------------------------
# 上下文辅助
# ---------------------------------------------------------------------------
def _session_metadata_module() -> Any:
    try:
        return importlib.import_module("atri_x.agent.session_metadata")
    except Exception:  # noqa: BLE001
        return None


def _security_policy(agent: Any) -> Any:
    return getattr(agent, "security_policy", None) or default_policy()


def _registry(agent: Any) -> Any:
    runtime = getattr(agent, "tool_runtime", None)
    return getattr(runtime, "registry", None) if runtime is not None else None


def _is_background_session(session: Any) -> bool:
    """是否为后台会话。对应 Go ``isBackgroundSession``（``chat_tools.go``）。"""
    if session is None:
        return False
    import importlib as _il

    for module_name, name in (
        ("atri_x.agent.tool_transcript", "is_background_session"),
        ("atri_x.agent.chat_tools", "is_background_session"),
    ):
        try:
            module = _il.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        fn = getattr(module, name, None)
        if callable(fn):
            return bool(fn(session))
    try:
        from atri_x.session.promotion import is_background

        return bool(is_background(session))
    except Exception:  # noqa: BLE001
        metadata = _session_metadata_module()
        if metadata is None:
            return False
        return bool(
            metadata.decode_session_metadata(getattr(session, "metadata", "") or "").background_kind
        )


def _contains_any(text: str, values: Iterable[str]) -> bool:
    return any(value in (text or "") for value in values)


def _schedule(awaitable: Any) -> None:
    """把副作用排进事件循环（没有循环时丢弃协程，避免 "never awaited"）。"""
    if awaitable is None or not hasattr(awaitable, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            awaitable.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return
    loop.create_task(awaitable)


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
async def _discovery_for_tool_names_async(
    agent: Any, ctx: Any, names: list[str], actor: Any, policy: Any
) -> DiscoveryResult | None:
    """``registry.DiscoverDetails`` 的异步完整版。对应 Go ``discoveryForToolNames``。"""
    registry = _registry(agent)
    if registry is None:
        return None
    from atri_x.tool.detail import discover_details

    def allowed(candidate: Tool) -> bool:
        info = candidate.info()
        return info_available_in_context(ctx, info) and can_access_tool(actor, policy, info)

    details, _ = await discover_details(registry, ctx, names, allowed)
    out = DiscoveryResult()
    for discovered in details:
        if (
            discovered.schema is None
            or not discovered.info.name
            or getattr(discovered, "detail", "")
        ):
            continue
        out.tools.append(discovered)
    return out if out.tools else None


def _discovery_for_tool_names_sync(
    agent: Any, ctx: Any, names: list[str], actor: Any, policy: Any
) -> DiscoveryResult | None:
    """同步精简版发现：只看注册表里的直接工具（不展开详情提供者）。"""
    registry = _registry(agent)
    if registry is None:
        return None
    out = DiscoveryResult()
    for name in names:
        candidate = registry.get(name)
        if candidate is None:
            continue
        info = candidate.info()
        if not info_available_in_context(ctx, info) or not can_access_tool(actor, policy, info):
            continue
        schema = candidate.schema()
        if schema is None or not getattr(schema.function, "name", ""):
            continue
        out.tools.append(
            _discovered(info, schema, ctx)
        )
    return out if out.tools else None


def _discovered(info: Any, schema: Any, ctx: Any) -> Any:
    """把 ``Info`` + schema 包成 ``DiscoveredTool``（对应 Go 的字面量构造）。"""
    return DiscoveredTool(
        info=PublicInfo(
            name=info.name,
            description=info.description,
            source=str(getattr(info, "source", "")),
            foreground_only=bool(getattr(info, "foreground_only", False)),
        ),
        schema=schema,
    )


def _discovery_for_tool_directive(
    agent: Any, ctx: Any, value: str, actor: Any, policy: Any
) -> tuple[DiscoveryResult | None, str, bool]:
    """``@tool:<名字或标签>`` 的发现。对应 Go ``discoveryForToolDirective``。"""
    from atri_x.agent.tool_tag_config import names_by_tool_tag_sync, normalize_tool_tag

    value = (value or "").strip()
    registry = _registry(agent)
    if not value or registry is None:
        return None, "", False
    root = registry.get(value)
    if root is not None:
        if preload_root_reason(actor, policy, root) != "":
            return None, "", False
        discovery = _discovery_for_tool_names_sync(agent, ctx, [value], actor, policy)
        return discovery, "", discovery is not None
    tag_name = normalize_tool_tag(value)
    names = names_by_tool_tag_sync(agent, ctx, tag_name, lambda candidate: preload_root_reason(actor, policy, candidate) == "")
    if not names:
        return None, "", False
    discovery = _discovery_for_tool_names_sync(agent, ctx, names, actor, policy)
    return discovery, tag_name, discovery is not None


# ---------------------------------------------------------------------------
# 预载记账
# ---------------------------------------------------------------------------
def _cached_name_set(agent: Any, session: Any) -> set[str]:
    """同步读「会话已缓存工具名」（Go ``cachedToolNameSet`` 的同步投影）。"""
    try:
        from atri_x.agent.tool_cache import tool_cache_state

        state = tool_cache_state(agent)
    except Exception:  # noqa: BLE001 - 同僚文件缺失时视为空缓存
        return set()
    bucket = {}
    with state.mutex():
        bucket = dict(state.discovered_tools.get(getattr(session, "id", ""), {}) or {})
    return {str(name) for name in bucket}


def _remember_preloaded_discovery(
    agent: Any, ctx: Any, session: Any, discovery: DiscoveryResult | None, seen: dict[str, bool]
) -> tuple[list[str], list[str]]:
    """记录预载工具并返回 ``(新注入, 已存在)``。对应 Go ``rememberPreloadedDiscovery``。"""
    if discovery is None:
        return [], []
    cached_before = _cached_name_set(agent, session)
    try:
        from atri_x.agent.tool_cache import remember_cached_tools
        from atri_x.toolrun.toolrun import native_cached_tools_from_discovery

        _schedule(remember_cached_tools(agent, ctx, session, native_cached_tools_from_discovery(discovery)))
    except Exception:  # noqa: BLE001 - 缓存写入失败不影响本轮注入
        pass
    injected: list[str] = []
    existing: list[str] = []
    for discovered in discovery.tools:
        name = getattr(discovered.info, "name", "")
        if discovered.schema is None or not name or seen.get(name):
            continue
        seen[name] = True
        if name in cached_before:
            existing.append(name)
        else:
            injected.append(name)
    return injected, existing


def _persist_tool_tags(agent: Any, ctx: Any, session: Any, tags: list[str]) -> None:
    """把标签记进会话 metadata（异步，排队执行）。"""
    try:
        from atri_x.agent.tool_cache import persist_tool_tags

        _schedule(persist_tool_tags(agent, ctx, session, list(tags)))
    except Exception:  # noqa: BLE001
        pass


def _audit(agent: Any, event: str, **attrs: Any) -> None:
    try:
        from atri_x.agent.logging import audit as module_audit

        pairs: list[Any] = []
        for key, value in attrs.items():
            pairs.extend([key, value])
        module_audit(agent, event, *pairs)
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


# ---------------------------------------------------------------------------
# @tool:
# ---------------------------------------------------------------------------
def apply_tool_directives(agent: Any, ctx: Any, session: Any, text: str) -> Any:
    """处理 ``@tool:`` 指令（同步版）。对应 Go ``applyToolDirectives``。"""
    host = host_of(agent)
    result = ToolDirectiveResult(text=text)
    if session is None or _registry(host) is None:
        return _outcome(**vars(result))
    if not _contains_any(
        text or "",
        (TOOL_PREFIX, TOOL_FULL_PREFIX, TOOL_SHORT_PREFIX, TOOL_SHORT_FULL),
    ):
        return _outcome(**vars(result))
    matches = tool_matches(text or "")
    if not matches:
        return _outcome(**vars(result))
    if not session_mode_supports_tools(getattr(session, "mode", "")):
        result.mode_blocked = True
        remove = [True] * len(matches)
        result.invalid = [str(match.name) for match in matches]
        result.text = strip_tool_matches(text or "", matches, remove)
        return _outcome(**vars(result))

    if not _is_background_session(session):
        # 前台会话注入工作区存取：预载进来的工具要能解析工作目录（后台会话不需要）。
        from atri_x.agent.workspace import SessionWorkspaceStore
        from atri_x.tool import with_workspace_store

        ctx = with_workspace_store(ctx, SessionWorkspaceStore(host, session))
    policy = _security_policy(host)
    actor = host.actor(ctx)
    remove = [False] * len(matches)
    seen_injected: dict[str, bool] = {}
    for index, match in enumerate(matches):
        discovery, tag_name, ok = _discovery_for_tool_directive(
            host, ctx, str(match.name), actor, policy
        )
        if not ok or discovery is None or not discovery.tools:
            result.invalid.append(str(match.name))
            continue
        injected, existing = _remember_preloaded_discovery(
            host, ctx, session, discovery, seen_injected
        )
        result.injected.extend(injected)
        result.existing.extend(existing)
        if tag_name:
            _persist_tool_tags(host, ctx, session, [tag_name])
        remove[index] = True
    if not result.injected and not result.existing:
        return _outcome(**vars(result))
    result.text = strip_tool_matches(text or "", matches, remove)
    return _outcome(**vars(result))


async def apply_tool_directives_async(
    agent: Any, ctx: Any, session: Any, text: str
) -> Any:
    """处理 ``@tool:`` 指令（完整保真版，需 ``await``）。

    与同步版的差别：用真正的 ``DiscoverDetails`` 展开工具、``LoadDiscoveryContent``
    拉取自定义发现文案（拼到正文后面），并 ``await`` 落库。
    """
    host = host_of(agent)
    result = ToolDirectiveResult(text=text)
    if session is None or _registry(host) is None:
        return _outcome(**vars(result))
    if not _contains_any(
        text or "",
        (TOOL_PREFIX, TOOL_FULL_PREFIX, TOOL_SHORT_PREFIX, TOOL_SHORT_FULL),
    ):
        return _outcome(**vars(result))
    matches = tool_matches(text or "")
    if not matches:
        return _outcome(**vars(result))
    if not session_mode_supports_tools(getattr(session, "mode", "")):
        result.mode_blocked = True
        remove = [True] * len(matches)
        result.invalid = [str(match.name) for match in matches]
        result.text = strip_tool_matches(text or "", matches, remove)
        return _outcome(**vars(result))

    if not _is_background_session(session):
        # 前台会话注入工作区存取：预载进来的工具要能解析工作目录（后台会话不需要）。
        from atri_x.agent.workspace import SessionWorkspaceStore
        from atri_x.tool import with_workspace_store

        ctx = with_workspace_store(ctx, SessionWorkspaceStore(host, session))
    policy = _security_policy(host)
    actor = host.actor(ctx)
    from atri_x.agent.tool_tag_config import names_by_tool_tag, normalize_tool_tag

    remove = [False] * len(matches)
    seen_injected: dict[str, bool] = {}
    seen_content: dict[str, bool] = {}
    contents: list[str] = []
    for index, match in enumerate(matches):
        name = str(match.name)
        registry = _registry(host)
        root = registry.get(name)
        tag_name = ""
        if root is not None:
            if preload_root_reason(actor, policy, root) != "":
                result.invalid.append(name)
                continue
            discovery = await _discovery_for_tool_names_async(
                host, ctx, [name], actor, policy
            )
        else:
            tag_name = normalize_tool_tag(name)
            names = await names_by_tool_tag(
                host,
                ctx,
                tag_name,
                lambda candidate: preload_root_reason(actor, policy, candidate) == "",
            )
            discovery = (
                await _discovery_for_tool_names_async(host, ctx, names, actor, policy)
                if names
                else None
            )
        if discovery is None or not discovery.tools:
            result.invalid.append(name)
            continue
        content = await _preloaded_context_discovery_content(host, ctx, discovery, seen_content)
        if content.strip():
            contents.append(content.strip())
        injected, existing = _remember_preloaded_discovery(
            host, ctx, session, discovery, seen_injected
        )
        result.injected.extend(injected)
        result.existing.extend(existing)
        if tag_name:
            _persist_tool_tags(host, ctx, session, [tag_name])
        remove[index] = True
    if not result.injected and not result.existing:
        return _outcome(**vars(result))
    result.text = strip_tool_matches(text or "", matches, remove)
    if contents:
        body = " ".join(contents).strip()
        if not (result.text or "").strip():
            result.text = body
        else:
            result.text = (result.text or "").strip() + "\n\n" + body
    return _outcome(**vars(result))


async def _preloaded_context_discovery_content(
    agent: Any, ctx: Any, discovery: DiscoveryResult, seen: dict[str, bool]
) -> str:
    """拉取带上下文的自定义发现文案。对应 Go ``preloadedContextDiscoveryContent``。"""
    registry = _registry(agent)
    if registry is None:
        return ""
    from atri_x.tool import ContextDiscoveryContentProvider, load_discovery_content

    parts: list[str] = []
    for discovered in discovery.tools:
        name = str(getattr(discovered.info, "name", "") or "").strip()
        if discovered.schema is None or not name or seen.get(name):
            continue
        target = registry.get(name)
        if target is None or not isinstance(target, ContextDiscoveryContentProvider):
            continue
        seen[name] = True
        result = await load_discovery_content(ctx, target)
        content = result[0] if isinstance(result, tuple) else result
        if content and str(content).strip():
            parts.append(str(content).strip())
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
async def notify_tool_directive_result(agent: Any, ctx: Any, result: Any) -> None:
    """把 ``@tool:`` 的处理结果回给用户。对应 Go ``notifyToolDirectiveResult``。"""
    host = host_of(agent)
    parts: list[str] = []
    injected = list(getattr(result, "injected", None) or [])
    existing = list(getattr(result, "existing", None) or [])
    invalid = list(getattr(result, "invalid", None) or [])
    if injected:
        parts.append("已注入工具：" + ", ".join(_sorted_unique(injected)))
    if existing:
        parts.append("已存在工具：" + ", ".join(_sorted_unique(existing)))
    if invalid and not getattr(result, "mode_blocked", False):
        parts.append("未找到或不可用的工具：" + ", ".join(_sorted_unique(invalid)))
    if getattr(result, "mode_blocked", False):
        parts.append(
            "当前会话模式不支持工具预载（本仓 work / chat 都支持；"
            "请确认会话模式是否正确）。"
        )
    if not parts:
        return
    await host.send_chat(ctx, "\n".join(parts))


def _sorted_unique(values: Iterable[str]) -> list[str]:
    from atri_x.agent.tool_tag_config import sorted_unique

    return sorted_unique(list(values))


async def preload_tool_names(
    agent: Any, ctx: Any, session: Any, names: list[str]
) -> list[str]:
    """按名字批量预载工具。对应 Go ``preloadToolNames``。"""
    host = host_of(agent)
    if session is None or not session_mode_supports_tools(
        getattr(session, "mode", "")
    ):
        return []
    registry = _registry(host)
    if registry is None:
        return []
    if not _is_background_session(session):
        # 前台会话注入工作区存取：预载进来的工具要能解析工作目录（后台会话不需要）。
        from atri_x.agent.workspace import SessionWorkspaceStore
        from atri_x.tool import with_workspace_store

        ctx = with_workspace_store(ctx, SessionWorkspaceStore(host, session))
    policy = _security_policy(host)
    actor = host.actor(ctx)
    seen: dict[str, bool] = {}
    injected: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        root = registry.get(name)
        if root is None:
            _audit(
                host,
                "tool_preload_skipped",
                session_id=getattr(session, "id", ""),
                tool=name,
                reason=ToolReasonNotFound,
                result=ResultSkipped,
            )
            continue
        reason = preload_root_reason(actor, policy, root)
        if reason:
            _audit(
                host,
                "tool_preload_skipped",
                session_id=getattr(session, "id", ""),
                tool=name,
                reason=reason,
                result=preload_skip_result(reason),
            )
            continue
        discovery = _discovery_for_tool_names_sync(host, ctx, [name], actor, policy)
        if discovery is None or not discovery.tools:
            _audit(
                host,
                "tool_preload_skipped",
                session_id=getattr(session, "id", ""),
                tool=name,
                reason=ToolReasonNoSchema,
                result=ResultSkipped,
            )
            continue
        newly, _ = _remember_preloaded_discovery(host, ctx, session, discovery, seen)
        injected.extend(newly)
    return injected


# ---------------------------------------------------------------------------
# mixin / 服务 / 装配
# ---------------------------------------------------------------------------
class ToolDirectiveMixin:
    """``@tool:`` 指令的 Agent 方法组。"""

    def apply_tool_directives(self, ctx: Any, session: Any, text: str) -> Any:
        """对应 Go ``Agent.applyToolDirectives``（同步契约）。"""
        return apply_tool_directives(host_of(self), ctx, session, text)

    async def apply_tool_directives_async(
        self, ctx: Any, session: Any, text: str
    ) -> Any:
        """完整保真版（调用点需 ``await``）。"""
        return await apply_tool_directives_async(host_of(self), ctx, session, text)

    async def notify_tool_directive_result(self, ctx: Any, result: Any) -> None:
        """对应 Go ``Agent.notifyToolDirectiveResult``。"""
        await notify_tool_directive_result(host_of(self), ctx, result)

    async def preload_tool_names(
        self, ctx: Any, session: Any, names: list[str]
    ) -> list[str]:
        """对应 Go ``Agent.preloadToolNames``。"""
        return await preload_tool_names(host_of(self), ctx, session, names)


#: 可以装到 Agent 实例上的方法。
TOOL_DIRECTIVE_METHODS: tuple[str, ...] = (
    "apply_tool_directives",
    "apply_tool_directives_async",
    "notify_tool_directive_result",
    "preload_tool_names",
)

class ToolDirectiveService(AgentService):
    """``@tool:`` 服务门面（``agent.services['tool_directive']``）。"""


def install_tool_directive_services(agent: Any) -> tuple[Any, Any]:
    """装配指令服务：把方法装到 Agent 上并注册两个门面（幂等）。"""
    install_methods(agent, ToolDirectiveMixin, TOOL_DIRECTIVE_METHODS)
    from atri_x.agent.tool_directive_skill import SKILL_METHODS, SkillDirectiveMixin

    install_methods(agent, SkillDirectiveMixin, SKILL_METHODS)
    from atri_x.agent.tool_directive_skill import SkillDirectiveService

    services = getattr(agent, "services", None)
    existing = services.get("tool_directive") if isinstance(services, dict) else None
    skill_existing = services.get("skill_directive") if isinstance(services, dict) else None
    if isinstance(existing, ToolDirectiveService) and isinstance(
        skill_existing, SkillDirectiveService
    ):
        return existing, skill_existing
    tool_service = (
        existing
        if isinstance(existing, ToolDirectiveService)
        else ToolDirectiveService(agent)
    )
    skill_service = (
        skill_existing
        if isinstance(skill_existing, SkillDirectiveService)
        else SkillDirectiveService(agent)
    )
    if isinstance(services, dict):
        services["tool_directive"] = tool_service
        services["skill_directive"] = skill_service
    return tool_service, skill_service
