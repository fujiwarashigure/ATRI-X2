"""后台运行（cron / Atrinis 共用的 ``RunBackground``）与会话预处理。

对应 Go：``internal/agent/cron.go``。

职责：``RunCronMessage`` / ``RunBackground`` / ``backgroundSession`` /
``ensureBackgroundSession`` / ``backgroundSessionTakenOver`` /
``backgroundSessionMustStop`` / ``latestAssistantMessage`` —— 即"跑一次后台任务"的
生命周期；无状态辅助见 :mod:`atri_x.agent.cron_background`。

Go ``cron.go`` 里的工具/Skill **预载**与后台 Session **元数据/作用域/标题**辅助
（``preloadBackgroundResources`` / ``backgroundMetadataMap`` / ``cronSessionMetadata`` 等）
已按 ``PORTING.md`` §2 的单文件目标拆到 :mod:`atri_x.agent.cron_background`；本模块顶部
import 它们并在末尾保留再导出清单，``from atri_x.agent.cron import X`` 的既有路径不变。

与 Go 的差异（形式，不是简化）：

* Go 的 ``*Agent`` 方法 → :class:`CronMixin` 的方法；同名的模块级函数也导出
  （首个参数 ``agent``），便于鸭子类型调用与单测。
* ``errBackgroundTakenOver`` → :class:`BackgroundTakenOverError`
  （从 :mod:`atri_x.errors` 延迟取哨兵；缺失时用本模块的类）。
* Go 的 ``context.WithValue`` → ``atri_x.tool.sandbox`` /
  ``atri_x.security`` / ``atri_x.platform`` 的 ``with_*`` 助手。
* ``discardSender`` → :class:`DiscardSender`（丢掉后台消息的发送器）。
* ``storage`` / ``session`` / ``tool`` 的仓储在 Python 侧是同步 SQLite 直连，
  因此读写调用点不传 ctx（与 Go 的 ``ctx`` 参数对应关系见各函数 docstring）。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from atri_x.background import (
    KindCron,
    OutcomeTakenOver,
    RunRequest,
    RunResult,
)
from atri_x.cron.service import RunCronMessageRequest, RunCronMessageResult
from atri_x.errors import BackgroundTakenOverError, NotFoundError
from atri_x.llm import SegmentFile, SegmentImage, SegmentText
from atri_x.platform import (
    MessageSegment,
    SegmentFile as PlatformSegmentFile,
    SegmentImage as PlatformSegmentImage,
    SegmentText as PlatformSegmentText,
)
from atri_x.security import RoleSuperadmin
from atri_x.session import Scope, was_promoted
from atri_x.storage import Session
from atri_x.storage.models import SessionStatusActive
from atri_x.storage.times import now as storage_now
from atri_x.tool import SandboxContext, with_sandbox_context

from atri_x.agent.cron_background import (
    BackgroundPreloadResult,
    audit_event,
    background_cached_tools,
    background_metadata_map,
    background_prompt_with_skills,
    background_scope_id,
    background_session_metadata,
    background_title,
    background_tool_list_names,
    cron_session_metadata,
    discovery_for_background_tool_names,
    merge_background_session_metadata,
    normalize_background_session_mode,
    preload_background_resources,
    preload_background_tool,
    preload_skip_result,
    remember_preloaded_discovery,
    tool_background_kind,
)
from atri_x.agent.tools import is_group_scope_id  # noqa: F401

#: 模块内历史名字（``run_background`` 的审计调用点用它）。实现见 ``cron_background``。
_audit = audit_event

__all__ = [
    "BackgroundPreloadResult",
    "BackgroundTakenOverError",
    "CronMixin",
    "DiscardSender",
    "assistant_raw_text_from_metadata",
    "audit_event",
    "background_cached_tools",
    "background_metadata_map",
    "background_prompt_segments",
    "background_prompt_with_skills",
    "background_scope_id",
    "background_session",
    "background_session_metadata",
    "background_session_must_stop",
    "background_session_taken_over",
    "background_title",
    "background_tool_list_names",
    "cron_session_metadata",
    "discovery_for_background_tool_names",
    "ensure_background_session",
    "latest_assistant_message",
    "merge_background_session_metadata",
    "normalize_background_session_mode",
    "preload_background_resources",
    "preload_background_tool",
    "preload_skip_result",
    "remember_preloaded_discovery",
    "report_takeover_check_failure",
    "run_background",
    "run_cron_message",
    "tool_background_kind",
]


class DiscardSender:
    """丢掉一切发送的后台发送器。对应 Go ``discardSender``。"""

    async def send_chat(self, ctx: Any, outputs: list[Any]) -> Any:
        from atri_x.delivery import Receipt

        return Receipt()

    async def send_notice(self, ctx: Any, notice: Any) -> Any:
        from atri_x.delivery import Receipt

        return Receipt()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def run_cron_message(
    agent: Any, ctx: Any, req: RunCronMessageRequest
) -> RunCronMessageResult:
    """cron 的 agent 入口。对应 Go ``RunCronMessage``。"""
    result = await run_background(
        agent,
        ctx,
        RunRequest(
            kind=KindCron,
            name=req.job_name,
            title=req.title,
            platform=req.platform,
            actor=req.actor,
            scope_id=req.scope_id,
            session_id=req.session_id,
            model_provider=req.model_provider,
            model=req.model,
            prompt=req.prompt,
            tool_list_names=list(req.tool_list_names or []),
            sandbox_subdir=KindCron,
            metadata={"cron_job_name": req.job_name},
        ),
    )
    return RunCronMessageResult(
        session_id=result.session_id, text=result.text, taken_over=result.taken_over
    )


async def run_background(agent: Any, ctx: Any, req: RunRequest) -> RunResult:
    """跑一次后台任务。对应 Go ``RunBackground``。"""
    actor = req.actor
    if not getattr(actor, "role", ""):
        # Go 的 ``actor := req.Actor`` 是**值拷贝**：这里的角色提升只作用于本次后台
        # 运行。直接在调用方那个 Actor 上改，会把它在别处（前台路径）也变成超管。
        actor = copy.copy(actor)
        actor.role = RoleSuperadmin
    platform_name = req.platform or getattr(actor, "platform", "") or ""
    if not platform_name:
        platform = getattr(agent, "platform", None)
        if platform is not None:
            platform_name = platform.name()
    scope_id = req.scope_id or background_scope_id(req.kind, req.name)
    ctx = _with_background_message_context(ctx, platform_name, actor, scope_id, req)
    ctx = _with_security(ctx, agent, actor)

    sandbox_root = getattr(agent, "sandbox_root", "") or os.path.join("data", "sandbox")
    sandbox_subdir = str(req.sandbox_subdir or "").strip() or str(req.kind or "").strip()
    if not sandbox_subdir:
        sandbox_subdir = "background"
    ctx = with_sandbox_context(
        ctx,
        SandboxContext(
            root=sandbox_root,
            dir=os.path.join(sandbox_root, *sandbox_subdir.replace("\\", "/").split("/")),
            background=True,
            background_kind=tool_background_kind(req.kind),
        ),
    )
    if req.model_provider or req.model:
        ctx = _with_cron_model_selection(ctx, req.model_provider, req.model)

    scope = Scope(
        actor_id=actor.id,
        platform=platform_name,
        platform_scope_id=scope_id,
        is_cli=platform_name == "cli",
    )
    try:
        bg_session = await background_session(agent, ctx, req, scope)
    except BackgroundTakenOverError:
        return _taken_over_result(req.session_id)
    if req.cached_tools:
        cached = background_cached_tools(req.cached_tools)
        if cached:
            from atri_x.agent.tool_cache import remember_cached_tools

            await remember_cached_tools(agent, ctx, bg_session, cached)
            _audit(
                agent,
                "background_external_tools_preloaded",
                session_id=bg_session.id,
                kind=req.kind,
                name=req.name,
                tools=[item.name for item in cached],
                result="succeeded",
            )
    preloaded = await preload_background_resources(
        agent, ctx, bg_session, background_tool_list_names(req.tool_list_names)
    )
    if preloaded.tools:
        _audit(
            agent,
            "background_tool_preloaded",
            session_id=bg_session.id,
            kind=req.kind,
            name=req.name,
            tools=preloaded.tools,
            result="succeeded",
        )
    if preloaded.skills:
        _audit(
            agent,
            "background_skill_preloaded",
            session_id=bg_session.id,
            kind=req.kind,
            name=req.name,
            skills=preloaded.skills,
            result="succeeded",
        )
    prompt = background_prompt_with_skills(req.prompt, preloaded.skill_prompt)
    # 预载期间前台可能刚接管这个 Session，进入模型前再确认一次。
    if background_session_must_stop(agent, ctx, bg_session.id):
        return _taken_over_result(bg_session.id)
    starter = getattr(agent, "start_background_chat", None)
    if starter is not None:
        try:
            await _maybe_await(starter(ctx, bg_session, prompt))
        except BackgroundTakenOverError:
            return _taken_over_result(bg_session.id)
    # turn 正常结束之后再确认一次：写助手消息之前提交的接管会被这一步接住。
    taken, err = background_session_taken_over(agent, ctx, bg_session.id)
    if err is not None or taken:
        if err is not None:
            report_takeover_check_failure(agent, ctx, bg_session.id, err)
        return _taken_over_result(bg_session.id)
    message = await latest_assistant_message(agent, ctx, bg_session.id)
    text = message.content
    raw_text = assistant_raw_text_from_metadata(message.metadata)
    if raw_text:
        text = raw_text
    return RunResult(session_id=bg_session.id, message_id=message.id, text=text)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
async def background_session(agent: Any, ctx: Any, req: RunRequest, scope: Scope) -> Session:
    """取或建后台 Session。对应 Go ``backgroundSession``。"""
    store = agent.store
    if req.session_id:
        bg_session = store.sessions().get(req.session_id)
        if was_promoted(bg_session):
            raise BackgroundTakenOverError(
                "background session was taken over by foreground"
            )
        return await ensure_background_session(agent, ctx, bg_session, req)
    title = str(req.title or "").strip() or background_title(req.kind, req.name)
    bg_session = Session(
        owner_id=scope.actor_id,
        platform=scope.platform,
        platform_scope_id=scope.platform_scope_id,
        mode=normalize_background_session_mode(req.session_mode),
        title=title,
        status=SessionStatusActive,
        metadata=background_session_metadata(req),
    )
    store.sessions().create(bg_session)
    return bg_session


async def ensure_background_session(
    agent: Any, ctx: Any, bg_session: Session | None, req: RunRequest
) -> Session:
    """把后台元数据合并进已存在的 Session。对应 Go ``ensureBackgroundSession``。"""
    if bg_session is None:
        raise NotFoundError
    store = agent.store

    def _update(current: Session) -> None:
        if was_promoted(current):
            raise BackgroundTakenOverError("background session was taken over by foreground")
        metadata = merge_background_session_metadata(current.metadata or "", req)
        mode = normalize_background_session_mode(req.session_mode)
        if current.mode == mode and (current.metadata or "") == metadata:
            return
        current.mode = mode
        current.metadata = metadata
        current.updated_at = storage_now()

    updated = store.sessions().mutate(bg_session.id, _update)
    if hasattr(updated, "__await__"):
        updated = await updated
    bg_session.mode = updated.mode
    bg_session.metadata = updated.metadata
    bg_session.updated_at = updated.updated_at
    return bg_session


def background_session_taken_over(
    agent: Any, ctx: Any, session_id: str
) -> tuple[bool, BaseException | None]:
    """目标 Session 是否已被前台接管。对应 Go ``backgroundSessionTakenOver``。

    接管标记由前台激活（``/resume``、``/unarchive``）在同一事务里写入，因此这里
    读到的一定是已提交状态。与 Go 一样带 error 出口：读不出状态时调用方要保守停下。
    """
    if agent is None or getattr(agent, "store", None) is None or not session_id:
        return False, None
    try:
        row = agent.store.sessions().get(session_id)
    except NotFoundError:
        return False, None
    except BaseException as exc:  # noqa: BLE001
        return False, exc
    return bool(was_promoted(row)), None


def background_session_must_stop(agent: Any, ctx: Any, session_id: str) -> bool:
    """后台 turn 在安全点是否必须停下。对应 Go ``backgroundSessionMustStop``。

    接管标记查不出来时保守返回 ``True``；上下文已结束时不走这条路径。
    """
    taken, err = background_session_taken_over(agent, ctx, session_id)
    if err is None:
        return taken
    if _ctx_done(ctx):
        return False
    report_takeover_check_failure(agent, ctx, session_id, err)
    return True


def report_takeover_check_failure(
    agent: Any, ctx: Any, session_id: str, err: BaseException
) -> None:
    """记录一次接管检查失败。对应 Go ``reportTakeoverCheckFailure``。"""
    warn = getattr(agent, "audit_warn", None)
    if warn is not None:
        warn(
            "background_takeover_check_failed",
            session_id=session_id,
            error=str(err),
            result="failed",
        )
    logger = getattr(agent, "logger", None)
    if logger is not None:
        log = getattr(logger, "warn", None)
        if log is not None:
            log(
                "background takeover check failed; stopping the background turn",
                session_id=session_id,
                error=str(err),
            )


async def latest_assistant_message(agent: Any, ctx: Any, session_id: str) -> Any:
    """该 Session 最后一条助手消息。对应 Go ``latestAssistantMessage``。"""
    messages = agent.store.messages().list_by_session(session_id)
    if hasattr(messages, "__await__"):
        messages = await messages
    for message in reversed(list(messages or [])):
        if message.role == "assistant":
            return message
    raise ValueError("background session %s has no assistant message" % session_id)


def assistant_raw_text_from_metadata(raw: str) -> str:
    """助手 metadata 里的 ``raw_text``。对应 Go ``assistantRawTextFromMetadata``。"""
    if not str(raw or "").strip():
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("raw_text") or "")


# ---------------------------------------------------------------------------
# 纯 helper
# ---------------------------------------------------------------------------
def background_prompt_segments(segments: list[Any]) -> list[MessageSegment]:
    """llm 段 → 平台段。对应 Go ``backgroundPromptSegments``。"""
    out: list[MessageSegment] = []
    for segment in segments or []:
        if segment.type == SegmentText:
            out.append(
                MessageSegment(
                    type=PlatformSegmentText,
                    text=segment.text,
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
        elif segment.type == SegmentImage:
            out.append(
                MessageSegment(
                    type=PlatformSegmentImage,
                    text=segment.text,
                    url=segment.url,
                    media_id=segment.media,
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
        elif segment.type == SegmentFile:
            out.append(
                MessageSegment(
                    type=PlatformSegmentFile,
                    text=segment.text,
                    url=segment.url,
                    media_id=segment.media,
                    mime_type=segment.mime_type,
                    name=segment.name,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------
class CronMixin:
    """Go ``*Agent`` 的后台运行方法组。"""

    async def run_cron_message(self, ctx: Any, req: RunCronMessageRequest) -> RunCronMessageResult:
        return await run_cron_message(self, ctx, req)

    async def run_background(self, ctx: Any, req: RunRequest) -> RunResult:
        return await run_background(self, ctx, req)

    def background_session_must_stop(self, ctx: Any, session_id: str) -> bool:
        return background_session_must_stop(self, ctx, session_id)

    def background_session_taken_over(
        self, ctx: Any, session_id: str
    ) -> tuple[bool, BaseException | None]:
        return background_session_taken_over(self, ctx, session_id)

    async def latest_assistant_message(self, ctx: Any, session_id: str) -> Any:
        return await latest_assistant_message(self, ctx, session_id)

    async def preload_background_resources(
        self, ctx: Any, session: Any, names: list[str]
    ) -> BackgroundPreloadResult:
        return await preload_background_resources(self, ctx, session, names)

    def report_takeover_check_failure(
        self, ctx: Any, session_id: str, err: BaseException
    ) -> None:
        report_takeover_check_failure(self, ctx, session_id, err)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _taken_over_result(session_id: str) -> RunResult:
    return RunResult(session_id=session_id, taken_over=True, outcome=OutcomeTakenOver)


def _ctx_done(ctx: Any) -> bool:
    done = getattr(ctx, "done", None)
    if callable(done):
        try:
            return bool(done())
        except BaseException:  # noqa: BLE001
            return False
    cancel = getattr(ctx, "cancelled", None)
    if callable(cancel):
        try:
            return bool(cancel())
        except BaseException:  # noqa: BLE001
            return False
    return False


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _with_background_message_context(
    ctx: Any, platform_name: str, actor: Any, scope_id: str, req: RunRequest
) -> Any:
    from atri_x.platform import MessageContext, with_message_context

    return with_message_context(
        ctx,
        MessageContext(
            platform=platform_name,
            actor_id=actor.id,
            platform_user_id=getattr(actor, "platform_user_id", ""),
            display_name=getattr(actor, "display_name", ""),
            scope_id=scope_id,
            sender=DiscardSender(),
            segments=background_prompt_segments(req.prompt_segments),
        ),
    )


def _with_security(ctx: Any, agent: Any, actor: Any) -> Any:
    from atri_x.security import WithActor, WithPolicy

    policy = getattr(agent, "security_policy", None)
    return WithPolicy(WithActor(ctx, actor), policy)


_CRON_MODEL_SELECTION_KEY = "atri_x.agent.cron_model_selection"


def _with_cron_model_selection(ctx: Any, provider: str, model: str) -> Any:
    from atri_x.delivery import ctx_with_value

    return ctx_with_value(
        ctx, _CRON_MODEL_SELECTION_KEY, {"provider": provider, "model": model}
    )


# ---------------------------------------------------------------------------
# 再导出
# ---------------------------------------------------------------------------
# 预载与后台 Session 元数据相关实现已按 ``PORTING.md`` §2 的单文件目标拆到
# :mod:`atri_x.agent.cron_background`（见那里的模块 docstring）。上面的 import 让既有
# 调用路径保持不变：
#
#     from atri_x.agent.cron import preload_background_resources, BackgroundPreloadResult
#
# ``_audit`` 是模块内历史名字，指向 ``cron_background.audit_event``。
_REEXPORTED_FROM_CRON_BACKGROUND = (
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
)
