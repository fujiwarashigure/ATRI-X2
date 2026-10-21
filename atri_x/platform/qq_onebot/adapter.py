"""qq-onebot 适配器主体：正向 WebSocket 读循环与出站发送。

对应 Go：``internal/platform/qq-onebot/adapter.go``。

硬边界（上游）：适配器**只**做平台输入输出 —— 把入站消息变成平台无关事件交给
``handler``，把 :class:`atri_x.delivery.delivery.Output` 变成 OneBot 动作；它不驱动
LLM。群聊里**跳过**工具调用进度预览（规则在交付层
:func:`atri_x.delivery.delivery.should_drop_group_tool_preview`，这里只回答
"是不是群聊"）。

形式差异：

* ``(T, error)`` → 抛异常；发送失败但已投递部分内容时抛
  :class:`atri_x.delivery.delivery.DeliveryError`（``receipt`` 描述已成功项）。
* ``context.Context`` → 鸭子类型 ``ctx``；``sleepContext`` / ``raiseContextError``
  在 :mod:`atri_x.platform.qq_onebot.inbound`。
* ``go a.notifyConnected(ctx)`` → :meth:`Adapter.pending_tasks` 里登记的任务。
* ``prepareInbound`` / ``resolveAtSegments`` / ``resolveForwardSegments`` /
  ``recordChatMessage`` 的实现拆到 :mod:`atri_x.platform.qq_onebot.preprocess`
  （它们要发网络请求，且 ``adapter.py`` 已接近单文件行数上限）；本类保留同名方法
  做一层委托。
* 可选能力（群历史 / 群资料 / 群成员 / 头像）委托
  :mod:`atri_x.platform.qq_onebot.capabilities`，媒体解析委托
  :mod:`atri_x.platform.qq_onebot.attachment`。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any, Sequence

from atri_x.delivery.delivery import (
    DeliveryError,
    Notice,
    Output,
    Receipt,
    Source,
    Target as DeliveryTarget,
    ctx_error,
    should_drop_group_tool_preview,
    use_temporary_connection,
)
from atri_x.errors import AtriXError, InvalidError
from atri_x.net.websocket import CLOSE_NORMAL
from atri_x.platform import refcontext
from atri_x.platform.backoff import new_backoff
from atri_x.platform.platform import (
    GroupInfo,
    GroupMember,
    HistoryMessage,
    MessageContext,
    MessageSegment,
    PlatformEvent,
    PlatformEventHandler,
    ReplyContext,
)
from atri_x.platform.qq_onebot import attachment as qq_attachment
from atri_x.platform.qq_onebot import capabilities as qq_capabilities
from atri_x.platform.qq_onebot import preprocess as qq_preprocess
from atri_x.platform.qq_onebot.config import Config, PLATFORM_NAME, apply_defaults
from atri_x.platform.qq_onebot.forward import first_non_empty
from atri_x.platform.qq_onebot.inbound import (
    INBOUND_DEDUP_STATE_FAILED,
    EventDispatcher,
    InboundStats,
    OutboundEventJob,
    begin_inbound_job,
    finish_inbound_job,
    inbound_key,
    inbound_stats,
    new_inbound_runtime,
    raise_context_error,
    sleep_context,
)
from atri_x.platform.qq_onebot.output import (
    one_bot_media_receipt,
    output_segments,
    qq_forward_nodes,
    qq_text_pages,
    receipt_with_message_id,
    text_outputs,
)
from atri_x.platform.qq_onebot.preprocess import (
    first_non_zero,
    is_configured_superadmin,
    is_message_event,
)
from atri_x.platform.qq_onebot.target import (
    MESSAGE_TYPE_GROUP,
    MESSAGE_TYPE_PRIVATE,
    Target,
    event_scope_id,
    is_group_context,
    target_from_context,
    target_to_qq,
)
from atri_x.platform.qq_onebot.transport import Transport
from atri_x.platform.qq_onebot.types import (
    DEFAULT_FORWARD_FETCH_TIMEOUT,
    Event,
    ForwardLimits,
)
from atri_x.security import GroupRoleUnknown, ParseGroupRole
from atri_x.storage.times import now as storage_now


def new_adapter(cfg: Config, store: Any, chat_history: Any, logger: Any) -> "Adapter":
    """对应 Go ``New``。"""
    return Adapter(cfg, store, chat_history, logger)


class Adapter:
    """OneBot v11 平台适配器。对应 Go ``Adapter``。"""

    def __init__(self, cfg: Config, store: Any, chat_history: Any, logger: Any) -> None:
        apply_defaults(cfg)
        self.cfg = cfg
        self.store = store
        self.chat_history = chat_history
        self.logger = logger
        timeout = float(cfg.api_timeout_seconds or 0)
        if timeout <= 0:
            timeout = 15.0
        self.transport = Transport(
            url=cfg.url,
            access_token=cfg.access_token,
            timeout=timeout,
            read_limit_bytes=int(cfg.forward_max_result_bytes or 0),
            logger=logger,
        )
        self.notify: Any = None
        self.inbound = new_inbound_runtime(cfg)
        self._tasks: set[asyncio.Task] = set()

    # -- 生命周期 -----------------------------------------------------------
    def name(self) -> str:
        """对应 Go ``Name``。"""
        return PLATFORM_NAME

    def enabled(self) -> bool:
        """对应 Go ``Enabled``。"""
        return bool(self.cfg.enabled)

    def set_connect_notifier(self, notify: Any) -> None:
        """对应 Go ``SetConnectNotifier``。"""
        self.notify = notify

    async def notify_connected(self, ctx: Any) -> None:
        """对应 Go ``notifyConnected``。"""
        if self.notify is None:
            return
        result = self.notify(ctx, self.name())
        if asyncio.iscoroutine(result):
            await result

    def forward_limits(self) -> ForwardLimits:
        """当前转发展开上限。对应 Go ``(*Adapter).forwardLimits``。"""
        return ForwardLimits(
            max_nodes=self.cfg.forward_max_nodes,
            max_runes=self.cfg.forward_max_runes,
            max_depth=self.cfg.forward_max_depth,
            max_fetches=self.cfg.forward_max_fetches,
            max_result_bytes=self.cfg.forward_max_result_bytes,
            max_non_text=self.cfg.forward_max_non_text,
            fetch_timeout=float(self.cfg.forward_fetch_timeout_seconds or 0)
            or DEFAULT_FORWARD_FETCH_TIMEOUT,
        )

    def pending_tasks(self) -> set[asyncio.Task]:
        """尚未结束的后台任务（Python 侧测试接缝；对应 Go 的 ``go`` 句柄缺失）。"""
        return set(self._tasks)

    def _spawn(self, coro: Any) -> asyncio.Task:
        """对应 Go 的 ``go`` 语句。"""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(self, ctx: Any, handler: Any) -> None:
        """连接 → 读循环 → 断开后按退避重连。对应 Go ``Run``。"""
        if not self.cfg.enabled:
            return
        backoff = new_backoff(float(self.cfg.reconnect_interval_seconds or 0), 10.0)
        while True:
            err = ctx_error(ctx)
            if err is not None:
                raise err
            try:
                await self.transport.connect(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接失败按退避重试
                if backoff.should_warn():
                    self.log_warn("onebot connect failed", error=exc)
                if not await sleep_context(ctx, backoff.delay()):
                    raise_context_error(ctx)
                continue
            backoff.reset()
            self.log_info("onebot connected", url=self.cfg.url)
            self._spawn(self.notify_connected(ctx))
            error: BaseException | None = None
            try:
                await self.read_loop(ctx, handler)
            except asyncio.CancelledError:
                await self.transport.close(CLOSE_NORMAL, "reconnect")
                raise
            except Exception as exc:  # noqa: BLE001 - 断开原因只用于日志
                error = exc
            await self.transport.close(CLOSE_NORMAL, "reconnect")
            ctx_err = ctx_error(ctx)
            if ctx_err is not None:
                raise ctx_err
            if error is not None:
                self.log_warn("onebot disconnected", error=error)
            if not await sleep_context(ctx, backoff.delay()):
                raise_context_error(ctx)

    async def read_loop(self, ctx: Any, handler: Any) -> None:
        """读事件并按类型分发。对应 Go ``readLoop``。"""
        dispatcher = EventDispatcher(
            self,
            ctx,
            handler,
            self.cfg.preprocess_workers,
            self.cfg.preprocess_queue_size,
            self.cfg.high_priority_workers,
            self.cfg.high_priority_queue_size,
        )
        try:
            while True:
                event = await self.transport.read(ctx)
                if event is None or event.post_type == "":
                    continue
                if self.is_message_event(event):
                    self.dispatch_message_event(dispatcher, event)
                    continue
                if event.post_type in ("notice", "request", "meta_event"):
                    if not dispatcher.enqueue_high(OutboundEventJob(event=event)):
                        # 撤回、成员与管理事件不能被丢弃或排在普通聊天流量后面。
                        # 就地执行是有界的兜底：读循环等这一条事件，而不是开无界协程。
                        await self.handle_platform_event(ctx, handler, event)
        finally:
            await dispatcher.stop()

    def dispatch_message_event(self, dispatcher: EventDispatcher, event: Event) -> None:
        """消息事件入普通队列（带去重）。对应 Go ``dispatchMessageEvent``。"""
        if dispatcher is None:
            return
        key = inbound_key(self, event)
        if key != "":
            duplicate, state = self.begin_inbound_job(key)
            if duplicate:
                self.log_info(
                    "duplicate qq message ignored", message_id=event.message_id, state=state
                )
                return
        if dispatcher.enqueue_normal(OutboundEventJob(event=event, message_key=key)):
            return
        self.finish_inbound_job(key, INBOUND_DEDUP_STATE_FAILED, "preprocess_queue_full")
        if self.inbound is not None:
            self.inbound.record_reject(storage_now())
        self.log_warn(
            "qq inbound preprocess queue full, message dropped", message_id=event.message_id
        )

    async def handle_platform_event(
        self, ctx: Any, handler: Any, event: Event
    ) -> None:
        """notice / request / meta_event → 平台中立事件。对应 Go ``handlePlatformEvent``。"""
        if not isinstance(handler, PlatformEventHandler):
            return
        notice_type = (event.notice_type or "").strip().lower()
        user_id = first_non_zero(event.operator_id, event.user_id)
        if "decrease" in notice_type or "ban" in notice_type:
            user_id = first_non_zero(event.user_id, event.target_id)
        message_id = "" if event.message_id == 0 else str(event.message_id)
        platform_event = PlatformEvent(
            platform=self.name(),
            kind=event.post_type,
            type=first_non_empty(event.notice_type, event.request_type, event.post_type),
            scope_id=event_scope_id(event),
            user_id=str(user_id),
            message_id=message_id,
            meta={
                "qq_onebot.post_type": event.post_type,
                "qq_onebot.message_type": event.message_type,
                "qq_onebot.sub_type": event.sub_type,
                "qq_onebot.notice_type": event.notice_type,
                "qq_onebot.request_type": event.request_type,
                "qq_onebot.group_id": str(event.group_id),
                "qq_onebot.user_id": str(event.user_id),
                "qq_onebot.operator_id": str(event.operator_id),
                "qq_onebot.target_id": str(event.target_id),
                "qq_onebot.self_id": str(event.self_id),
            },
        )
        platform_event.raw = json.dumps(
            event.to_json(), ensure_ascii=False, separators=(",", ":")
        )
        try:
            await handler.handle_platform_event(ctx, platform_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 事件处理失败只记日志
            self.log_warn(
                "handle qq platform event failed",
                post_type=event.post_type,
                type=platform_event.type,
                error=exc,
            )

    async def handle_event(self, ctx: Any, handler: Any, event: Event) -> None:
        """跑一次完整的入站处理并交给 handler。对应 Go ``handleEvent``。"""
        _msg_ctx, msg_ctx, text, ok = await self.prepare_inbound(ctx, ctx, event)
        if not ok:
            return
        try:
            result = handler.handle_message(msg_ctx, text)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log_warn("handle qq message failed", error=exc, message_id=event.message_id)
            raise

    # -- 入站预处理（委托 preprocess） --------------------------------------
    async def prepare_inbound(
        self, prep_ctx: Any, base_ctx: Any, event: Event
    ) -> tuple[MessageContext, Any, str, bool]:
        """对应 Go ``(*Adapter).prepareInbound``。"""
        return await qq_preprocess.prepare_inbound(self, prep_ctx, base_ctx, event)

    async def resolve_forward_segments(
        self, ctx: Any, event: Event, msg: Any
    ) -> Any:
        """对应 Go ``(*Adapter).resolveForwardSegments``。"""
        return await qq_preprocess.resolve_forward_segments(self, ctx, event, msg)

    async def resolve_at_segments(self, ctx: Any, event: Event, msg: Any) -> Any:
        """对应 Go ``(*Adapter).resolveAtSegments``。"""
        return await qq_preprocess.resolve_at_segments(self, ctx, event, msg)

    async def reply_to_sender_id(self, ctx: Any, event: Event, reply_id: str) -> str:
        """对应 Go ``(*Adapter).replyToSenderID``。"""
        return await qq_preprocess.reply_to_sender_id(self, ctx, event, reply_id)

    async def reference_fetcher(
        self, ctx: Any, event: Event, reply_id: str
    ) -> tuple[refcontext.ReferencedMessage, bool]:
        """对应 Go ``(*Adapter).referenceFetcher``（返回已取到的引用）。"""
        return await qq_preprocess.fetch_referenced(self, ctx, event, reply_id)

    def record_chat_message(
        self, ctx: Any, event: Event, normalized: Any, reply: ReplyContext
    ) -> None:
        """对应 Go ``(*Adapter).recordChatMessage``。"""
        qq_preprocess.record_chat_message(self, ctx, event, normalized, reply)

    def group_role(self, event: Event) -> str:
        """入站消息的群身份。对应 Go ``oneBotGroupRole``。"""
        if event.message_type != MESSAGE_TYPE_GROUP:
            return GroupRoleUnknown
        return ParseGroupRole(event.sender.role)

    def is_message_event(self, event: Event) -> bool:
        """对应 Go ``(*Adapter).isMessageEvent``。"""
        return is_message_event(event)

    def inbound_stats(self) -> InboundStats:
        """对应 Go ``(*Adapter).InboundStats``。"""
        return inbound_stats(self)

    def begin_inbound_job(self, key: str) -> tuple[bool, str]:
        """对应 Go ``(*Adapter).beginInboundJob``。"""
        return begin_inbound_job(self, key)

    def finish_inbound_job(self, key: str, state: str, note: str) -> None:
        """对应 Go ``(*Adapter).finishInboundJob``。"""
        finish_inbound_job(self, key, state, note)

    # -- 出站 ---------------------------------------------------------------
    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``SendChat``（落点来自 ctx 里的 target）。"""
        text, ok = text_outputs(outputs)
        if ok:
            return await self.send_context_text(ctx, text)
        target, found = target_from_context(ctx)
        if not found:
            raise InvalidError("qq send target missing")
        segments = output_segments(self.cfg.send_file_mode, outputs)
        receipt = await self.send_segments(ctx, target, segments)
        return one_bot_media_receipt(receipt, target, outputs)

    async def call_platform_api(
        self, ctx: Any, api: str, params: dict[str, Any] | None
    ) -> str:
        """直接调用 OneBot 动作。对应 Go ``CallPlatformAPI``。

        返回 ``data`` 字段的 JSON 文本（与 Telegram 适配器同一口径）。
        """
        if self.transport is None:
            raise InvalidError("qqonebot transport is not configured")
        resp = await self.transport.call(ctx, (api or "").strip(), params)
        return json.dumps(resp.data, ensure_ascii=False, separators=(",", ":"))

    async def send_temporary_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """临时连接发送（``atrinis`` 的一次性投递）。对应 Go ``sendTemporaryNotice``。"""
        transport = Transport(
            url=self.cfg.url,
            access_token=self.cfg.access_token,
            timeout=float(self.cfg.api_timeout_seconds or 0),
            logger=self.logger,
        )
        await transport.connect(ctx)
        try:
            self._spawn(transport.read_responses(ctx))
            target = target_to_qq(notice.target)
            segments = output_segments(self.cfg.send_file_mode, notice.outputs)
            if target.message_type == MESSAGE_TYPE_PRIVATE:
                message_id = await transport.send_private_segments(
                    ctx, target.user_id, [segment.to_json() for segment in segments]
                )
            elif target.message_type == MESSAGE_TYPE_GROUP:
                message_id = await transport.send_group_segments(
                    ctx, target.group_id, [segment.to_json() for segment in segments]
                )
            else:
                raise InvalidError(f"unsupported message target {target.message_type!r}")
            receipt = receipt_with_message_id(message_id)
            return one_bot_media_receipt(receipt, target, notice.outputs)
        finally:
            await transport.close(CLOSE_NORMAL, "temporary atrinis delivery done")

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """对应 Go ``SendNotice``：空 Target 表示发回当前会话。"""
        if use_temporary_connection(ctx):
            return await self.send_temporary_notice(ctx, notice)
        out_target = notice.target
        outputs = notice.outputs
        if should_drop_group_tool_preview(
            out_target,
            outputs,
            is_group_context(ctx),
            getattr(self.cfg, "show_tool_preview_in_group", False),
        ):
            return Receipt()
        if out_target.empty():
            return await self.send_chat(ctx, outputs)
        if out_target.superadmins:
            if not self.cfg.superadmins:
                raise AtriXError("qqonebot superadmins are not configured")
            receipt = Receipt()
            for value in self.cfg.superadmins:
                identifier = (value or "").strip()
                if identifier == "":
                    continue
                copy_target = dataclasses.replace(
                    out_target,
                    superadmins=False,
                    private_user_id=identifier,
                    group_id="",
                    scope_id="",
                )
                try:
                    sent = await self.send_notice(
                        ctx, dataclasses.replace(notice, target=copy_target)
                    )
                except Exception as exc:  # noqa: BLE001 - 保留已成功目标的回执
                    partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
                    merged = receipt.merge(partial)
                    raise DeliveryError(str(exc), merged.mark_partial_failure(exc)) from exc
                receipt = receipt.merge(sent)
            return receipt
        target = target_to_qq(out_target)
        segments = output_segments(self.cfg.send_file_mode, outputs)
        receipt = await self.send_segments(ctx, target, segments)
        return one_bot_media_receipt(receipt, target, outputs)

    async def send_context_text(self, ctx: Any, text: str) -> Receipt:
        """按文本长度决定单条发送还是合并转发。对应 Go ``sendContextText``。"""
        if (text or "").strip() == "":
            return Receipt()
        target, found = target_from_context(ctx)
        if not found:
            raise InvalidError("qq send target missing")
        pages = qq_text_pages(text)
        if len(pages) > 1:
            return await self.send_forward_pages(ctx, target, pages)
        message_id = await self.send_qq_text(ctx, target, text)
        return receipt_with_message_id(message_id)

    async def send_forward_pages(
        self, ctx: Any, target: Target, pages: Sequence[str]
    ) -> Receipt:
        """一条合并转发装下所有分页。对应 Go ``sendForwardPages``。"""
        nodes = [segment.to_json() for segment in qq_forward_nodes(pages)]
        if target.message_type == MESSAGE_TYPE_PRIVATE:
            message_id = await self.transport.send_private_forward_message(
                ctx, target.user_id, nodes
            )
        elif target.message_type == MESSAGE_TYPE_GROUP:
            message_id = await self.transport.send_group_forward_message(
                ctx, target.group_id, nodes
            )
        else:
            raise InvalidError(f"unsupported message target {target.message_type!r}")
        return receipt_with_message_id(message_id)

    async def send_qq_text(self, ctx: Any, target: Target, text: str) -> str:
        """对应 Go ``(*Adapter).sendQQText``。"""
        if target.message_type == MESSAGE_TYPE_PRIVATE:
            return await self.transport.send_private_message(ctx, target.user_id, text)
        if target.message_type == MESSAGE_TYPE_GROUP:
            return await self.transport.send_group_message(ctx, target.group_id, text)
        raise InvalidError(f"unsupported message target {target.message_type!r}")

    async def send_segments(
        self, ctx: Any, target: Target, segments: Sequence[Any]
    ) -> Receipt:
        """对应 Go ``(*Adapter).sendSegments``。"""
        payload = [segment.to_json() for segment in segments]
        if target.message_type == MESSAGE_TYPE_PRIVATE:
            message_id = await self.transport.send_private_segments(
                ctx, target.user_id, payload
            )
            return receipt_with_message_id(message_id)
        if target.message_type == MESSAGE_TYPE_GROUP:
            message_id = await self.transport.send_group_segments(
                ctx, target.group_id, payload
            )
            return receipt_with_message_id(message_id)
        raise InvalidError(f"unsupported message target {target.message_type!r}")

    async def send_target(
        self, ctx: Any, out_target: DeliveryTarget, out: Output
    ) -> Receipt:
        """对应 Go ``(*Adapter).sendTarget``。"""
        return await self.send_notice(ctx, Notice(target=out_target, outputs=[out]))

    # -- 可选能力 -----------------------------------------------------------
    async def resolve_media(
        self, ctx: Any, segment: MessageSegment, max_bytes: int
    ) -> Source:
        """对应 Go ``(*Adapter).ResolveMedia``。"""
        return await qq_attachment.resolve_media(self, ctx, segment, max_bytes)

    async def fetch_group_history(
        self, ctx: Any, group_id: str, since: Any, until: Any, limit: int
    ) -> list[HistoryMessage]:
        """对应 Go ``(*Adapter).FetchGroupHistory``。"""
        return await qq_capabilities.fetch_group_history(
            self, ctx, group_id, since, until, limit
        )

    async def get_group_info(self, ctx: Any, group_id: str) -> GroupInfo:
        """对应 Go ``(*Adapter).GetGroupInfo``。"""
        return await qq_capabilities.get_group_info(self, ctx, group_id)

    async def get_group_member_list(self, ctx: Any, group_id: str) -> list[GroupMember]:
        """对应 Go ``(*Adapter).GetGroupMemberList``。"""
        return await qq_capabilities.get_group_member_list(self, ctx, group_id)

    async def get_user_avatar_url(self, ctx: Any, user_id: str, size: int) -> str:
        """对应 Go ``(*Adapter).GetUserAvatarURL``（不读 ctx，也不用适配器状态）。"""
        return qq_capabilities.get_user_avatar_url(user_id, size)

    # -- 日志 ---------------------------------------------------------------
    def log_info(self, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.info(message, **fields)

    def log_warn(self, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.warn(message, **fields)

    # -- 内部小工具（Go 的自由函数） ----------------------------------------
    def is_superadmin(self, identifier: str) -> bool:
        """对应 Go ``isConfiguredSuperadmin(a.cfg.Superadmins, id)`` 的调用点。"""
        return is_configured_superadmin(self.cfg.superadmins, identifier)


__all__ = [name for name in dir() if not name.startswith("_")]
