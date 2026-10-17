"""后台任务的工具 / Skill 预载与 Session 元数据（Agent 侧）。

对应 Go：``internal/agent/cron.go`` 的 ``preloadBackgroundResources`` /
``preloadBackgroundTool`` / ``discoveryForBackgroundToolNames`` /
``rememberPreloadedDiscovery`` / ``preloadSkipResult`` / ``backgroundToolListNames`` /
``backgroundPromptWithSkills`` / ``normalizeBackgroundSessionMode`` /
``backgroundScopeID`` / ``backgroundTitle`` / ``toolBackgroundKind`` /
``backgroundSessionMetadata`` / ``mergeBackgroundSessionMetadata`` /
``backgroundMetadataMap`` / ``backgroundCachedTools`` / ``cronSessionMetadata``，
以及 ``internal/agent/tool_directive.go`` 的 ``skillDetailBlock``。

拆分来源：``cron.py`` 同时承载"跑一次后台任务"与"预载这次任务的工具/Skill、拼装它的
Session 元数据"两件事，超过 ``PORTING.md`` §2 的单文件目标（≤600 行），因此把这两块
无状态辅助拆到这里。``cron.py`` 顶部 import 本模块的名字，因此
``from atri_x.agent.cron import preload_background_resources`` 之类的既有路径不变。

预载是**后台任务的权限边界**：后台 Session 只拿到这里注入的工具，所以每一步拒绝都要
写审计（``background_preload_skipped``），并且"明确裁决"（策略/角色拒绝）与"没轮到执行"
（工具不存在、当前上下文不可用、隐藏、没有 schema）用不同的 ``result`` 区分 —— 混用
会让拒绝率统计失真。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.background import KindAtrinis, KindCron, RunRequest
from atri_x.security import DefaultPolicy
from atri_x.storage import SessionModeChat, SessionModeWork
from atri_x.tool import (
    BackgroundKindAtrinis,
    BackgroundKindCron,
    DetailBlock,
    DetailProvider,
    DiscoveredTool,
    DiscoveryResult,
    PublicInfo,
    ToolReasonHidden,
    ToolReasonNoSchema,
    ToolReasonNotFound,
    ToolReasonRiskDenied,
    ToolReasonSuperadminOnly,
    can_access_tool,
    info_available_in_context,
    render_detail_blocks,
    tool_availability_reason,
)
from atri_x.toolrun import CachedTool, background_tool_allowed as _bg_allowed

__all__ = [
    "BackgroundPreloadResult",
    "audit_event",
    "background_cached_tools",
    "background_metadata_map",
    "background_prompt_with_skills",
    "background_scope_id",
    "background_session_metadata",
    "background_title",
    "background_tool_list_names",
    "cron_session_metadata",
    "discovery_for_background_tool_names",
    "merge_background_session_metadata",
    "normalize_background_session_mode",
    "preload_background_resources",
    "preload_background_tool",
    "preload_skip_result",
    "remember_preloaded_discovery",
    "tool_background_kind",
]


def audit_event(agent: Any, event: str, **fields: Any) -> None:
    """把 ``key/value`` 摊平后调宿主的 ``audit``。对应 Go 的 ``a.audit(event, kv...)``。

    宿主没有 ``audit`` 时静默跳过（Go 里 ``a.audit`` 总是存在，Python 侧的替身与
    部分装配路径可能没有）。
    """
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in fields.items():
        args.append(key)
        args.append(value)
    audit(*args)


# ---------------------------------------------------------------------------
# 预载
# ---------------------------------------------------------------------------
@dataclass
class BackgroundPreloadResult:
    """预载结果。对应 Go ``backgroundPreloadResult``。"""

    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skill_prompt: str = ""


async def preload_background_resources(
    agent: Any, ctx: Any, session: Any, names: list[str]
) -> BackgroundPreloadResult:
    """预载后台任务需要的工具与 Skill。对应 Go ``preloadBackgroundResources``。"""
    result = BackgroundPreloadResult()
    if session is None or session.mode != SessionModeWork:
        return result
    from atri_x.agent.tools import tool_runtime_state

    state = tool_runtime_state(agent)
    if state.registry is None:
        return result
    with_caps = getattr(agent, "with_tool_capabilities", None)
    if with_caps is not None:
        ctx = with_caps(ctx, session)
    policy = getattr(agent, "security_policy", None) or DefaultPolicy()
    actor = _actor(agent, ctx)
    seen_injected: dict[str, bool] = {}
    seen_skills: dict[str, bool] = {}
    skill_sections: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        candidate, ok = state.registry.get(name)
        if not ok or candidate is None:
            audit_event(
                agent,
                "background_preload_skipped",
                session_id=session.id,
                name=name,
                reason=ToolReasonNotFound,
                result="skipped",
            )
            continue
        reason = tool_availability_reason(ctx, actor, policy, candidate)
        if reason:
            audit_event(
                agent,
                "background_preload_skipped",
                session_id=session.id,
                name=name,
                reason=reason,
                result=preload_skip_result(reason),
            )
            continue
        if _is_skill_like(candidate):
            # Go 先看 hidden 再取详情（隐藏的 Skill 不该触发详情加载）。
            if candidate.info().hidden:
                audit_event(
                    agent,
                    "background_preload_skipped",
                    session_id=session.id,
                    name=name,
                    reason=ToolReasonHidden,
                    result="skipped",
                )
                continue
            detail = await _skill_detail(ctx, actor, candidate)
            if not detail:
                audit_event(
                    agent,
                    "background_preload_skipped",
                    session_id=session.id,
                    name=name,
                    reason="empty_skill_detail",
                    result="skipped",
                )
                continue
            if not seen_skills.get(name):
                seen_skills[name] = True
                result.skills.append(name)
                skill_sections.append("## Skill: %s\n\n%s" % (name, detail))
            for wrapper in _activate_tools(candidate):
                result.tools.extend(
                    await preload_background_tool(
                        agent, ctx, session, wrapper, actor, policy, seen_injected, True
                    )
                )
            continue
        result.tools.extend(
            await preload_background_tool(
                agent, ctx, session, name, actor, policy, seen_injected, False
            )
        )
    if skill_sections:
        result.skill_prompt = "\n\n---\n\n".join(skill_sections)
    return result


async def preload_background_tool(
    agent: Any,
    ctx: Any,
    session: Any,
    name: str,
    actor: Any,
    policy: Any,
    seen: dict[str, bool],
    allow_hidden: bool,
) -> list[str]:
    """预载一个具体工具。对应 Go ``preloadBackgroundTool``。"""
    name = str(name or "").strip()
    if not name or name == "discover_tool":
        return []
    from atri_x.agent.tools import tool_runtime_state

    state = tool_runtime_state(agent)
    candidate, ok = state.registry.get(name)
    if not ok or candidate is None:
        audit_event(
            agent,
            "background_preload_skipped",
            session_id=session.id,
            name=name,
            reason=ToolReasonNotFound,
            result="skipped",
        )
        return []
    reason = tool_availability_reason(ctx, actor, policy, candidate)
    if reason:
        audit_event(
            agent,
            "background_preload_skipped",
            session_id=session.id,
            name=name,
            reason=reason,
            result=preload_skip_result(reason),
        )
        return []
    info = candidate.info()
    if info.hidden and not allow_hidden:
        audit_event(
            agent,
            "background_preload_skipped",
            session_id=session.id,
            name=name,
            reason=ToolReasonHidden,
            result="skipped",
        )
        return []
    if _is_skill_like(candidate):
        audit_event(
            agent,
            "background_preload_skipped",
            session_id=session.id,
            name=name,
            reason="skill_has_no_schema",
            result="skipped",
        )
        return []
    discovery = None
    discovered = False
    if allow_hidden and info.hidden:
        discovery = DiscoveryResult(
            tools=[
                DiscoveredTool(
                    info=PublicInfo(
                        name=info.name,
                        description=info.description,
                        source=str(info.source),
                        foreground_only=bool(getattr(info, "foreground_only", False)),
                    ),
                    schema=candidate.schema(),
                )
            ]
        )
        discovered = True
    else:
        discovery, discovered = await discovery_for_background_tool_names(
            agent, ctx, [name], actor, policy
        )
    if not discovered or discovery is None or not discovery.tools:
        audit_event(
            agent,
            "background_preload_skipped",
            session_id=session.id,
            name=name,
            reason=ToolReasonNoSchema,
            result="skipped",
        )
        return []
    new_tools, _ = await remember_preloaded_discovery(agent, ctx, session, discovery, seen)
    return new_tools


def preload_skip_result(reason: str) -> str:
    """不可用原因 → 操作结果。对应 Go ``preloadSkipResult``。"""
    if reason in (ToolReasonRiskDenied, ToolReasonSuperadminOnly):
        return "rejected"
    return "skipped"


async def discovery_for_background_tool_names(
    agent: Any, ctx: Any, names: list[str], actor: Any, policy: Any
) -> tuple[DiscoveryResult | None, bool]:
    """按名字做一次发现。对应 Go ``discoveryForBackgroundToolNames``。"""
    from atri_x.agent.tools import tool_runtime_state
    from atri_x.security import WithActor

    registry = tool_runtime_state(agent).registry
    if registry is None:
        return None, False

    def _allowed(candidate: Any) -> bool:
        # 与 Go 一样用 ``CanAccessTool``：它含 ``superadmin_only`` 门槛与风险归一化，
        # 而 ``policy.can_use_tool`` 只做风险比对（超管工具会被漏放进来）。
        info = candidate.info()
        return info_available_in_context(ctx, info) and can_access_tool(actor, policy, info)

    details, _errors = await registry.discover_details(
        WithActor(ctx, actor), names, _allowed
    )
    if not details:
        return None, False
    out = DiscoveryResult()
    for discovered in details:
        if discovered.schema is None or not discovered.info.name or discovered.detail:
            continue
        out.tools.append(discovered)
    return out, bool(out.tools)


async def remember_preloaded_discovery(
    agent: Any, ctx: Any, session: Any, discovery: DiscoveryResult, seen: dict[str, bool]
) -> tuple[list[str], list[str]]:
    """把预载发现结果记进缓存，返回 ``(新注入, 已存在)``。

    对应 Go ``rememberPreloadedDiscovery``：先在**写缓存之前**取一次名字集合，
    再把发现结果落进缓存；落进 ``seen`` 的只有本次真正要考虑的名字，已经在缓存里的
    归入"已存在"。审计事件 ``background_tool_preloaded`` 用的是第一个返回值 ——
    把"已存在"也算成"这次预载出来的工具"会让审计虚报。
    """
    from atri_x.agent.tool_cache import cached_tool_name_set, remember_cached_tools
    from atri_x.toolrun import native_cached_tools_from_discovery

    cached_before = await cached_tool_name_set(agent, ctx, session)
    await remember_cached_tools(
        agent, ctx, session, native_cached_tools_from_discovery(discovery)
    )
    injected: list[str] = []
    existing: list[str] = []
    for discovered in discovery.tools:
        name = str(getattr(discovered.info, "name", "") or "").strip()
        if discovered.schema is None or not name or seen.get(name):
            continue
        seen[name] = True
        if cached_before.get(name):
            existing.append(name)
        else:
            injected.append(name)
    return injected, existing


# ---------------------------------------------------------------------------
# 提示词与名字表
# ---------------------------------------------------------------------------
def background_prompt_with_skills(prompt: str, skill_prompt: str) -> str:
    """把预载的 Skill 说明拼到提示词前。对应 Go ``backgroundPromptWithSkills``。"""
    skill_prompt = str(skill_prompt or "").strip()
    if not skill_prompt:
        return prompt
    return (
        "[系统预加载 Skill]\n\n以下 Skill 说明已由系统预载。Skill 本体不是 top-level "
        "tool schema；可调用能力只以本次请求注入的 top-level tool schema 为准。\n\n"
        + skill_prompt
        + "\n\n[后台任务]\n\n"
        + str(prompt or "").strip()
    )


def background_tool_list_names(names: list[str] | None) -> list[str]:
    """去空去重、丢掉 ``discover_tool``。对应 Go ``backgroundToolListNames``。"""
    seen: dict[str, bool] = {}
    out: list[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if not name or name == "discover_tool" or seen.get(name):
            continue
        seen[name] = True
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is None:
        from atri_x.security import Actor

        return Actor()
    return helper(ctx)


def _is_skill_like(candidate: Any) -> bool:
    """是否 Skill 类工具。对应 Go 的 ``candidate.(tool.DetailProvider)`` 类型断言。

    ``DetailProvider`` 的方法集是 ``detail()`` **加** ``activate_tools()``；两处判定
    （详情分支与"Skill 没有 top-level schema"分支）必须用同一个谓词，否则会出现
    "被当成 Skill 却没有 activate 列表"这类半分类状态。
    """
    return isinstance(candidate, DetailProvider)


async def _skill_detail(ctx: Any, actor: Any, candidate: Any) -> str:
    """Skill 详情文本（空串表示没有详情）。

    对应 Go ``skillDetailBlock`` + ``RenderDetailBlocks``：详情加载失败与"详情为空"
    在 Go 里走同一条路（调用方拿到零值块，渲染出空串），所以这里也只返回空串。
    """
    if not _is_skill_like(candidate):
        return ""
    from atri_x.security import WithActor

    try:
        block = await _skill_detail_block(WithActor(ctx, actor), candidate)
    except BaseException:  # noqa: BLE001 - 与 Go 相同：详情失败按"没有详情"处理
        block = DetailBlock()
    return str(render_detail_blocks([block]) or "").strip()


async def _skill_detail_block(ctx: Any, candidate: Any) -> Any:
    """取 Skill 的详情块。对应 Go ``skillDetailBlock``（分支顺序也一致）。

    Skill 的详情加载在 Python 侧是**协程**（``LazyDetailProvider.load_detail``），
    而 Go 的 ``LoadDetail(ctx)`` 是同步阻塞调用；因此这里必须 ``await`` —— 早期版本
    在同步函数里发现返回值是协程就抛错，导致后台预载把每个走懒加载的 Skill 都记成
    ``empty_skill_detail``：**详情与 activate 出来的包装工具都不会注入**。
    """
    loader = getattr(candidate, "load_detail", None)
    if callable(loader):
        outcome = loader(ctx)
        if hasattr(outcome, "__await__"):
            return await outcome
        return outcome
    provider = getattr(candidate, "detail_block", None)
    if callable(provider):
        return provider()
    provider = getattr(candidate, "detail", None)
    if callable(provider):
        return DetailBlock(content=str(provider() or ""))
    return DetailBlock()


def _activate_tools(candidate: Any) -> list[str]:
    helper = getattr(candidate, "activate_tools", None)
    if not callable(helper):
        return []
    return [str(name) for name in (helper() or []) if str(name or "").strip()]


# ---------------------------------------------------------------------------
# 后台 Session 的元数据 / 作用域 / 标题
# ---------------------------------------------------------------------------
def normalize_background_session_mode(mode: str) -> str:
    """后台 Session 模式：只认 chat，其余一律 work。对应 Go 同名函数。"""
    if str(mode or "").strip() == SessionModeChat:
        return SessionModeChat
    return SessionModeWork


def background_scope_id(kind: str, name: str) -> str:
    """后台作用域 ID。对应 Go ``backgroundScopeID``。"""
    kind_text = str(kind or "").strip() or "background"
    name = str(name or "").strip()
    if not name:
        return kind_text + ":default"
    return kind_text + ":" + name


def background_title(kind: str, name: str) -> str:
    """后台 Session 标题。对应 Go ``backgroundTitle``。"""
    kind_text = str(kind or "").strip() or "Background"
    name = str(name or "").strip()
    head = kind_text[:1].upper() + kind_text[1:]
    if not name:
        return head
    return head + ": " + name


def tool_background_kind(kind: str) -> str:
    """后台种类 → ``tool.BackgroundKind``。对应 Go ``toolBackgroundKind``。

    Go 的 ``tool.BackgroundKind`` 是字符串类型，未知种类直接做类型转换；Python 侧
    :class:`atri_x.tool.sandbox.BackgroundKind` 只是常量容器（不可调用），因此
    这里返回常量字符串本身，与 ``BackgroundKindCron`` / ``BackgroundKindAtrinis``
    和 ``send_file`` 的 ``background_kind == BackgroundKindCron`` 判定完全一致。
    """
    if kind == KindCron:
        return BackgroundKindCron
    if kind == KindAtrinis:
        return BackgroundKindAtrinis
    return str(kind or "").strip()


def background_session_metadata(req: RunRequest) -> str:
    """新建后台 Session 的 metadata。对应 Go ``backgroundSessionMetadata``。"""
    return _encode_metadata(background_metadata_map(req))


def merge_background_session_metadata(raw: str, req: RunRequest) -> str:
    """合并后台 metadata（保留活动/工作区/命名字段）。对应 Go 同名函数。"""
    data: dict[str, Any] = {}
    if str(raw or "").strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            data = parsed
    data.update(background_metadata_map(req))
    return _encode_metadata(data)


def background_metadata_map(req: RunRequest) -> dict[str, Any]:
    """后台 metadata 的字段集。对应 Go ``backgroundMetadataMap``。"""
    data: dict[str, Any] = {
        "title_renamed": True,
        "title_source": req.kind,
        "background_kind": req.kind,
        "background_name": req.name,
    }
    for key, value in (req.metadata or {}).items():
        if str(key or "").strip():
            data[key] = value
    if req.cached_tools:
        cached = background_cached_tools(req.cached_tools)
        if cached:
            data["tool_cache"] = [item.to_json() for item in cached]
    return data


def background_cached_tools(cached: list[CachedTool] | None) -> list[CachedTool]:
    """丢掉后台任务绝不能跑的声明。对应 Go ``backgroundCachedTools``。"""
    out: list[CachedTool] = []
    for item in cached or ():
        if not _bg_allowed(item.name):
            continue
        out.append(item)
    return out


def cron_session_metadata(
    job_name: str, source_session_id: str, copied: bool
) -> str:
    """cron 源会话 metadata。对应 Go ``cronSessionMetadata``。"""
    return _encode_metadata(
        {
            "title_renamed": True,
            "title_source": KindCron,
            "background_kind": KindCron,
            "background_name": job_name,
            "cron_job_name": job_name,
            "cron_source_session_id": source_session_id,
            "cron_broadcast_copy": copied,
        }
    )


def _encode_metadata(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
