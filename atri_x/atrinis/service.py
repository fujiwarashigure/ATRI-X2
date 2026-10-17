"""Atrinis 服务：装配、鉴权入口、去重与三种模式（record / direct / llm）的分派。

对应 Go：``internal/atrinis/service.go``。

分派流程（:meth:`Service.handle_prepared_event`）：

1. 三道授权闸门（atriwisp 策略 → 内部工具白名单 → 外部工具声明），任一不过即
   审计 + 告警 + 返回失败；
2. 按 ``(atriwisp, source, id)`` 去重：命中就只记审计，返回 ``duplicate``；
3. 落库（``llm`` 模式直接落 ``queued``，其余落 ``accepted``）；
4. 按模式收尾：``record`` 立即完成；``direct`` 立即投递（失败写 failed 并返回失败
   响应）；``llm`` 交给 :meth:`Service.set_llm_enqueuer` 装上的入队回调。

**与 Go 的 ``(Response, error)`` 双返回**：本模块保留 Response 作为主要通道——
请求级问题（来源非法、预处理失败、去重、授权拒绝、未授权令牌）返回
``accepted=False`` 的 :class:`~atri_x.atriena.Response`，其 ``error`` 字段携带 Go
那条 error 的文案；只有**基础设施级**异常（存储/发送抛出的非预期错误）才向上抛。
理由：``Dispatcher`` 的调用方都是"必须写出一个响应体"的入口，抛异常会丢掉 Go 会
写出的响应体。HTTP 层因此可以只按 :meth:`atri_x.atrinis.http.response_status`
判定状态码，与 Go 的判定逐条等价。

与 Go 的结构差异（Python 侧必需）：Go 把这些方法分散在 ``service.go`` /
``auth.go`` / ``prepare.go`` / ``targets.go`` / ``media.go`` / ``direct.go`` /
``llm.go`` / ``outbox.go`` / ``store.go``，同一个 ``Service`` 结构体把它们都收在
一起。Python 没有"同一包内跨文件定义同一类型方法"的写法，改用**混入类**
（每个 Go 文件对应一个 Mixin），在这里组合成 :class:`Service`。调用点仍然是
``self.prepare_event(...)`` / ``self.deliver_report(...)``，与 Go 逐字对应。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from atri_x.atriena import (
    Event,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Origin,
    OriginHTTPToken,
    Request,
    Response,
    StatusAccepted,
    StatusCompleted,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
)
from atri_x.atrinis.auth import AuthMixin
from atri_x.atrinis.background import maybe_await, unique_sorted
from atri_x.atrinis.direct import DirectMixin
from atri_x.atrinis.llm import LLMMixin
from atri_x.atrinis.media import MediaMixin
from atri_x.atrinis.outbox import OutboxMixin
from atri_x.atrinis.prepare import PrepareMixin
from atri_x.atrinis.store import StoreMixin
from atri_x.atrinis.targets import TargetsMixin
from atri_x.config import AtrinisConfig, ModelSelection
from atri_x.errors import InvalidError, NotFoundError
from atri_x.media import ValidID
from atri_x.storage import CreateAtrinisEventRequest, ErrNotFound

__all__ = [
    "AuditFunc",
    "EnqueueLLMFunc",
    "ModelResolverFunc",
    "Options",
    "QueuedLLMEvent",
    "SenderFunc",
    "Service",
    "UNAUTHORIZED_MESSAGE",
    "new_service",
]

#: 未授权令牌的错误文案（与 Go 逐字一致，HTTP 层据此回 401）。
UNAUTHORIZED_MESSAGE = "unauthorized"

#: 发送回调签名：``send(ctx, target, outputs) -> delivery.Receipt``（可为同步或异步）。
SenderFunc = Callable[..., Any]
#: 审计回调签名：``audit(event, *attrs)``，``attrs`` 是扁平键值对。
AuditFunc = Callable[..., None]
#: 模型解析回调：``resolve(slot) -> config.ModelSelection``。
ModelResolverFunc = Callable[[str], ModelSelection]
#: LLM 入队回调：``enqueue(ctx, queued) -> None``（可为同步或异步）。
EnqueueLLMFunc = Callable[..., Any]

#: 三道闸门对应的告警文案（与 Go 的 slog 消息逐字一致）。
_DENY_MESSAGES = {
    "atrinis.permission_denied": "atrinis permission denied",
    "atrinis.tool_denied": "atrinis internal tool denied",
    "atrinis.external_tool_denied": "atrinis external tool denied",
}


@dataclass
class QueuedLLMEvent:
    """待入队的 LLM 事件。对应 Go ``QueuedLLMEvent``。"""

    event: Event = field(default_factory=Event)
    event_id: str = ""


@dataclass
class Options:
    """构造 :class:`Service` 的参数。对应 Go ``Options``。

    字段与 Go 一一对应，只是命名改为 ``snake_case``。``sandbox_root`` 为空时
    回落到 ``data/sandbox``（同 Go 的 ``filepath.Join("data", "sandbox")``）。
    """

    media: Any = None
    config: AtrinisConfig = field(default_factory=AtrinisConfig)
    sandbox_root: str = ""
    tokens: dict[str, str] = field(default_factory=dict)
    store: Any = None
    logger: Any = None
    audit: AuditFunc | None = None
    send: SenderFunc | None = None
    runner: Any = None
    resolve_model: ModelResolverFunc | None = None
    enabled_platforms: list[str] | None = None
    platform_callers: Any = None


class Service(
    PrepareMixin,
    TargetsMixin,
    AuthMixin,
    MediaMixin,
    DirectMixin,
    LLMMixin,
    OutboxMixin,
    StoreMixin,
):
    """Atrinis 服务。对应 Go ``Service``。

    实现 :class:`atri_x.atriena.Dispatcher`，因此可以直接装到
    :class:`atri_x.atriena.Bus` 上（``bus.set_dispatcher(service)``）。
    构造即校验（同 Go 的 ``NewService``）。
    """

    def __init__(self, opts: Options) -> None:
        if not opts.sandbox_root:
            opts.sandbox_root = os.path.join("data", "sandbox")
        if opts.store is None or opts.store.atrinis_events() is None:
            raise InvalidError("atrinis event store is not configured")
        if opts.config.enabled and not opts.tokens:
            raise InvalidError("atrinis enabled but no tokens are configured")
        self.media = opts.media
        self.cfg = opts.config
        self.sandbox_root = opts.sandbox_root
        self.tokens = dict(opts.tokens or {})
        self.store = opts.store
        self.logger = opts.logger
        self.audit = opts.audit
        self.send = opts.send
        self.runner = opts.runner
        self.resolve_model = opts.resolve_model
        self.enabled_platforms = unique_sorted(opts.enabled_platforms)
        self.platform_callers = opts.platform_callers
        self.enqueue_llm: EnqueueLLMFunc | None = None

    def set_llm_enqueuer(self, enqueue: EnqueueLLMFunc | None) -> None:
        """装上 LLM 入队回调。对应 Go ``SetLLMEnqueuer``。"""
        self.enqueue_llm = enqueue

    # -- 入口 ---------------------------------------------------------------

    async def handle(self, ctx: Any, token: str, req: Request) -> Response:
        """HTTP 令牌入口。对应 Go ``Handle``。"""
        token_name, ok = self.authenticate(token)
        if not ok:
            self.audit_event("atrinis.auth_failed")
            return Response(
                accepted=False, status=StatusFailed, error=UNAUTHORIZED_MESSAGE
            )
        return await self.dispatch_atriena(
            ctx, Origin(kind=OriginHTTPToken, name=token_name), req
        )

    async def dispatch_atriena(self, ctx: Any, origin: Origin, req: Request) -> Response:
        """Atriena 分派入口。对应 Go ``DispatchAtriena``。"""
        try:
            origin.validate()
        except InvalidError as exc:
            return Response(accepted=False, status=StatusFailed, error=str(exc))
        try:
            event = self.prepare_event(origin, req)
        except InvalidError as exc:
            self.audit_event("atrinis.rejected", "origin", origin.label(), "error", str(exc))
            return Response(accepted=False, status=StatusFailed, error=str(exc))
        return await self.handle_prepared_event(ctx, event)

    async def handle_prepared_event(self, ctx: Any, event: Event) -> Response:
        """已预处理事件的主流程。对应 Go ``handlePreparedEvent``。"""
        req = event.request
        attrs = self.event_attrs(event)
        for audit_name, gate in (
            ("atrinis.permission_denied", self.authorize_atriwisp),
            ("atrinis.tool_denied", self.authorize_internal_tools),
            ("atrinis.external_tool_denied", self.authorize_external_tools),
        ):
            try:
                gate(event)
            except InvalidError as exc:
                self.audit_event(audit_name, *attrs, "error", str(exc))
                self.log_warn(_DENY_MESSAGES[audit_name], *attrs, "error", str(exc))
                return Response(
                    accepted=False,
                    event_key=event.event_key,
                    mode=req.mode,
                    status=StatusFailed,
                    error=str(exc),
                )

        try:
            existing = self.store.atrinis_events().get_by_key(
                req.atriwisp.name, req.source, req.id
            )
        except BaseException as exc:
            if not _is_not_found(exc):
                raise
            existing = None
        if existing is not None:
            self.handle_duplicate(event, existing)
            return Response(
                accepted=True,
                duplicate=True,
                event_key=event.event_key,
                mode=req.mode,
                status=StatusDuplicate,
            )

        status = StatusQueued if req.mode == ModeLLM else StatusAccepted
        media_ids = [
            seg.url
            for seg in (req.segments or [])
            if req.mode != ModeRecord and ValidID(seg.url)
        ]
        record = self.store.atrinis_events().create(
            CreateAtrinisEventRequest(
                media_ids=media_ids,
                event_key=event.event_key,
                token_name=event.origin.label() if event.origin is not None else "",
                atriwisp_name=req.atriwisp.name,
                source=req.source,
                source_id=req.id,
                tags=event.tags_json,
                mode=req.mode,
                model_slot=req.model_slot,
                content_hash=event.content_hash,
                tool_declarations=event.tool_declarations,
                tool_hash=event.tool_hash,
                requested_targets=event.requested_targets,
                resolved_targets=event.resolved_targets,
                status=status,
                received_at=event.received_at,
                created_at=event.created_at,
            )
        )
        attrs = (*attrs, "event_id", record.id)
        self.audit_event("atrinis.accepted", *attrs)
        self.log_info("atrinis event accepted", *attrs)

        if req.mode == ModeRecord:
            self.complete_event(ctx, record.id, event.resolved_targets, StatusCompleted, "", "")
            self.audit_event("atrinis.recorded", *attrs)
            return Response(
                accepted=True, event_key=event.event_key, mode=req.mode, status=StatusCompleted
            )
        if req.mode == ModeDirect:
            return await self._dispatch_direct(ctx, event, record.id, attrs)
        if req.mode == ModeLLM:
            return await self._dispatch_llm(ctx, event, record.id, attrs)
        raise InvalidError(f"unsupported mode {req.mode!r}")

    async def _dispatch_direct(
        self, ctx: Any, event: Event, event_id: str, attrs: tuple[Any, ...]
    ) -> Response:
        """direct 分支。对应 Go ``handlePreparedEvent`` 的 ``case ModeDirect``。"""
        try:
            await self.run_direct(ctx, event, event_id)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.audit_event("atrinis.direct_failed", *attrs, "error", str(exc))
            self.log_warn("atrinis direct failed", *attrs, "error", str(exc))
            self.complete_event(
                ctx, event_id, event.resolved_targets, StatusFailed, "", str(exc)
            )
            return Response(
                accepted=True,
                event_key=event.event_key,
                mode=event.request.mode,
                status=StatusFailed,
                error=str(exc),
            )
        self.audit_event("atrinis.direct_completed", *attrs)
        return Response(
            accepted=True,
            event_key=event.event_key,
            mode=event.request.mode,
            status=StatusCompleted,
        )

    async def _dispatch_llm(
        self, ctx: Any, event: Event, event_id: str, attrs: tuple[Any, ...]
    ) -> Response:
        """llm 分支（入队）。对应 Go ``handlePreparedEvent`` 的 ``case ModeLLM``。"""
        self.audit_event("atrinis.llm_queued", *attrs)
        self.log_info("atrinis llm queued", *attrs)
        if self.enqueue_llm is not None:
            try:
                await maybe_await(
                    self.enqueue_llm(ctx, QueuedLLMEvent(event=event, event_id=event_id))
                )
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.complete_event(
                    ctx, event_id, event.resolved_targets, StatusFailed, "", str(exc)
                )
                self.log_warn("atrinis llm enqueue failed", *attrs, "error", str(exc))
                return Response(
                    accepted=True,
                    event_key=event.event_key,
                    mode=event.request.mode,
                    status=StatusFailed,
                    error=str(exc),
                )
        return Response(
            accepted=True,
            event_key=event.event_key,
            mode=event.request.mode,
            status=StatusQueued,
        )


def _is_not_found(exc: BaseException) -> bool:
    """``exc`` 是否是"没找到"（错误类或哨兵实例都算）。"""
    return exc is ErrNotFound or isinstance(exc, NotFoundError)


def new_service(opts: Options) -> Service:
    """构造并校验。对应 Go ``NewService``（失败时抛异常）。"""
    return Service(opts)
