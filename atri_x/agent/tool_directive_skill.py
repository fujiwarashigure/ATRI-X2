"""``@skill:`` 指令：Skill 详情注入与包装工具预载。

对应 Go：``internal/agent/tool_directive.go`` 的 ``@skill:`` 部分
（``skillDirectiveResult`` / ``applySkillDirectives`` / ``preloadSkillWrapper`` /
``skillDetailBlock`` / ``notifySkillDirectiveResult`` 与三个准入判定）。

共享的准入助手、注册表访问与记账在 :mod:`atri_x.agent.tool_directive`；本模块由
:class:`SkillDirectiveMixin` 组合进 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供类型注解（`from __future__ import annotations` 下不求值）
    from atri_x.tool import Tool

from atri_x.agent.model import AgentService, host_of, install_methods
from atri_x.agent.tool_directive import (
    ToolDirectiveResult,
    _audit,
    _contains_any,
    _discovered,
    _is_detail_provider,
    _outcome,
    _registry,
    _remember_preloaded_discovery,
    _security_policy,
    _sorted_unique,
    preload_skip_result,
)
from atri_x.directive import (
    SKILL_FULL_PREFIX,
    SKILL_PREFIX,
    SKILL_SHORT_FULL,
    SKILL_SHORT_PREFIX,
    skill_matches,
    strip_tool_matches,
)
from atri_x.logging import ResultSkipped
from atri_x.security import WithActor
from atri_x.storage.models import SessionModeWork
from atri_x.tool import (
    DetailBlock,
    DiscoveryResult,
    LazyDetailProvider,
    StructuredDetailProvider,
    ToolReasonHidden,
    ToolReasonNoSchema,
    ToolReasonNotFound,
    new_rule_card_formats_from_context,
    render_detail_blocks_with_context,
)
from atri_x.tool.availability import tool_access_reason

__all__ = [
    "SKILL_METHODS",
    "SkillDirectiveMixin",
    "SkillDirectiveResult",
    "SkillDirectiveService",
    "apply_skill_directives",
    "apply_skill_directives_async",
    "notify_skill_directive_result",
    "preload_skill_root_reason",
    "preload_skill_wrapper_reason",
]


#: 该名字是 Skill 本体（详情提供者），不是可注入的 top-level 工具 schema。
TOOL_REASON_SKILL_HAS_NO_SCHEMA = "skill_has_no_schema"

#: ``@skill:`` 指向的工具不是详情提供者（是普通工具）。
TOOL_REASON_NOT_SKILL_LIKE = "not_a_skill"


@dataclass
class SkillDirectiveResult:
    """``@skill:`` 的处理结果。对应 Go ``skillDirectiveResult``。"""

    text: str = ""
    skills: list[str] = field(default_factory=list)
    injected_wrappers: list[str] = field(default_factory=list)
    existing_wrappers: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    mode_blocked: bool = False
    applied: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    err: BaseException | None = None


def preload_skill_root_reason(actor: Any, policy: Any, candidate: Tool) -> str:
    """``@skill:`` 根 Skill 被拒绝的原因。对应 Go ``preloadSkillRootReason``。"""
    info = candidate.info()
    reason = tool_access_reason(actor, policy, info)
    if reason:
        return reason
    if info.hidden:
        return ToolReasonHidden
    if not _is_detail_provider(candidate):
        return TOOL_REASON_NOT_SKILL_LIKE
    return ""


def preload_skill_wrapper_reason(actor: Any, policy: Any, candidate: Tool) -> str:
    """Skill 包装工具被拒绝的原因。对应 Go ``preloadSkillWrapperReason``。

    刻意**不**按 ``hidden`` 拒绝：隐藏正是包装工具的常态，它们由 Skill 的 activate
    列表显式声明后才注入。
    """
    info = candidate.info()
    reason = tool_access_reason(actor, policy, info)
    if reason:
        return reason
    if _is_detail_provider(candidate):
        return TOOL_REASON_SKILL_HAS_NO_SCHEMA
    return ""


# ---------------------------------------------------------------------------
# @skill:
# ---------------------------------------------------------------------------
def apply_skill_directives(agent: Any, ctx: Any, session: Any, text: str) -> Any:
    """处理 ``@skill:`` 指令（同步版）。对应 Go ``applySkillDirectives``。"""
    host = host_of(agent)
    result = SkillDirectiveResult(text=text)
    if session is None or _registry(host) is None:
        return _outcome(**vars(result))
    if not _contains_any(
        text or "",
        (SKILL_PREFIX, SKILL_FULL_PREFIX, SKILL_SHORT_PREFIX, SKILL_SHORT_FULL),
    ):
        return _outcome(**vars(result))
    matches = skill_matches(text or "")
    if not matches:
        return _outcome(**vars(result))
    if getattr(session, "mode", "") != SessionModeWork:
        result.mode_blocked = True
        remove = [True] * len(matches)
        result.invalid = [str(match.name).strip() for match in matches]
        result.text = strip_tool_matches(text or "", matches, remove)
        return _outcome(**vars(result))

    policy = _security_policy(host)
    actor = host.actor(ctx)
    registry = _registry(host)
    remove = [False] * len(matches)
    seen_skills: dict[str, bool] = {}
    seen_injected: dict[str, bool] = {}
    for index, match in enumerate(matches):
        name = str(match.name).strip()
        candidate = registry.get(name)
        if candidate is None or preload_skill_root_reason(actor, policy, candidate) != "":
            result.invalid.append(name)
            continue
        if not seen_skills.get(name):
            seen_skills[name] = True
            result.skills.append(name)
        for wrapper in list(getattr(candidate, "activate_tools", lambda: [])() or []):
            injected, existing = _preload_skill_wrapper_sync(
                host, ctx, session, str(wrapper), actor, policy, seen_injected
            )
            result.injected_wrappers.extend(injected)
            result.existing_wrappers.extend(existing)
        remove[index] = True
    if not result.skills:
        return _outcome(**vars(result))
    result.text = strip_tool_matches(text or "", matches, remove)
    return _outcome(**vars(result))


def _preload_skill_wrapper_sync(
    agent: Any,
    ctx: Any,
    session: Any,
    name: str,
    actor: Any,
    policy: Any,
    seen: dict[str, bool],
) -> tuple[list[str], list[str]]:
    """预载一个 Skill 包装工具（同步版）。对应 Go ``preloadSkillWrapper``。"""
    name = (name or "").strip()
    registry = _registry(agent)
    if not name or name == "discover_tool" or registry is None:
        return [], []
    candidate = registry.get(name)
    if candidate is None:
        _audit(
            agent,
            "skill_wrapper_preload_skipped",
            session_id=getattr(session, "id", ""),
            tool=name,
            reason=ToolReasonNotFound,
            result=ResultSkipped,
        )
        return [], []
    reason = preload_skill_wrapper_reason(actor, policy, candidate)
    if reason:
        _audit(
            agent,
            "skill_wrapper_preload_skipped",
            session_id=getattr(session, "id", ""),
            tool=name,
            reason=reason,
            result=preload_skip_result(reason),
        )
        return [], []
    schema = candidate.schema()
    if schema is None or not getattr(schema.function, "name", ""):
        _audit(
            agent,
            "skill_wrapper_preload_skipped",
            session_id=getattr(session, "id", ""),
            tool=name,
            reason=ToolReasonNoSchema,
            result=ResultSkipped,
        )
        return [], []
    info = candidate.info()
    discovery = DiscoveryResult(tools=[_discovered(info, schema, ctx)])
    return _remember_preloaded_discovery(agent, ctx, session, discovery, seen)


async def apply_skill_directives_async(
    agent: Any, ctx: Any, session: Any, text: str
) -> Any:
    """处理 ``@skill:`` 指令（完整保真版）：额外注入 Skill 的详情块。"""
    host = host_of(agent)
    result = apply_skill_directives(host, ctx, session, text)
    if not getattr(result, "skills", None):
        return result
    registry = _registry(host)
    policy = _security_policy(host)
    actor = host.actor(ctx)
    blocks: list[Any] = []
    for name in result.skills:
        candidate = registry.get(name) if registry is not None else None
        if candidate is None:
            continue
        block = await _skill_detail_block(WithActor(ctx, actor), candidate)
        if block is not None:
            blocks.append(block)
    if not blocks:
        return result
    detail_text = render_detail_blocks_with_context(ctx, blocks)
    if detail_text:
        body = (result.text or "").strip()
        result.text = detail_text if not body else body + "\n\n" + detail_text
    try:
        from atri_x.agent.tool_cache import persist_shown_rule_card_formats
        from atri_x.tool import new_rule_card_formats_from_context

        formats = new_rule_card_formats_from_context(ctx)
        if formats:
            await persist_shown_rule_card_formats(host, ctx, session, formats)
    except Exception:  # noqa: BLE001 - 记录失败不影响注入
        pass
    return result


async def _skill_detail_block(ctx: Any, candidate: Any) -> DetailBlock | None:
    """取 Skill 的详情块。对应 Go ``skillDetailBlock``。"""
    loader = getattr(candidate, "load_detail", None)
    if callable(loader) and isinstance(candidate, LazyDetailProvider):
        return await loader(ctx)
    if isinstance(candidate, StructuredDetailProvider):
        return candidate.detail_block()
    detail = getattr(candidate, "detail", None)
    if callable(detail):
        return DetailBlock(content=str(detail() or ""))
    return None


async def notify_skill_directive_result(agent: Any, ctx: Any, result: Any) -> None:
    """把 ``@skill:`` 的处理结果回给用户。对应 Go ``notifySkillDirectiveResult``。"""
    host = host_of(agent)
    parts: list[str] = []
    skills = list(getattr(result, "skills", None) or [])
    injected = list(getattr(result, "injected_wrappers", None) or [])
    existing = list(getattr(result, "existing_wrappers", None) or [])
    invalid = list(getattr(result, "invalid", None) or [])
    if skills:
        parts.append("已注入 Skill：" + ", ".join(_sorted_unique(skills)))
    if injected:
        parts.append("已注入 Skill 工具：" + ", ".join(_sorted_unique(injected)))
    if existing:
        parts.append("已存在 Skill 工具：" + ", ".join(_sorted_unique(existing)))
    if invalid and not getattr(result, "mode_blocked", False):
        parts.append("未找到或不可用的 Skill：" + ", ".join(_sorted_unique(invalid)))
    if getattr(result, "mode_blocked", False):
        parts.append(
            "Skill 预载仅在 work 模式可用，当前是 chat 模式。发送 "
            + host.command_prefix()
            + "work 切换后再试。"
        )
    if not parts:
        return
    await host.send_chat(ctx, "\n".join(parts))


class SkillDirectiveMixin:
    """``@skill:`` 指令的 Agent 方法组。"""

    def apply_skill_directives(self, ctx: Any, session: Any, text: str) -> Any:
        """对应 Go ``Agent.applySkillDirectives``（同步契约）。"""
        return apply_skill_directives(host_of(self), ctx, session, text)

    async def apply_skill_directives_async(
        self, ctx: Any, session: Any, text: str
    ) -> Any:
        """完整保真版（调用点需 ``await``）。"""
        return await apply_skill_directives_async(host_of(self), ctx, session, text)

    async def notify_skill_directive_result(self, ctx: Any, result: Any) -> None:
        """对应 Go ``Agent.notifySkillDirectiveResult``。"""
        await notify_skill_directive_result(host_of(self), ctx, result)


#: ``@skill:`` 侧的方法。
SKILL_METHODS: tuple[str, ...] = (
    "apply_skill_directives",
    "apply_skill_directives_async",
    "notify_skill_directive_result",
)


class SkillDirectiveService(AgentService):
    """``@skill:`` 服务门面（``agent.services['skill_directive']``）。"""
