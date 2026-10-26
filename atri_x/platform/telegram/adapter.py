"""Telegram 适配器主体：long polling 入站循环与出站发送。

对应 Go：``internal/platform/telegram/adapter.go``。

硬边界（上游）：适配器**只**做平台输入输出 —— 把入站消息变成平台无关事件交给
``handler``，把 :class:`atri_x.delivery.delivery.Output` 变成平台调用；它不驱动 LLM。
**Telegram 不做群聊工具进度预览跳过**：``should_drop_group_tool_preview`` 只被
OneBot / QQ 官方使用，这里刻意不调用。

形式差异：

* ``(T, error)`` → 抛异常；发送失败但已投递部分内容时抛
  :class:`atri_x.delivery.delivery.DeliveryError`（``receipt`` 描述已成功项）。
* ``go a.handleUpdate(...)`` → ``asyncio.create_task``，任务登记在
  :meth:`Adapter.pending_tasks`（Python 侧测试接缝；Go 不保留句柄）。
* ``context.Context`` → 鸭子类型 ``ctx``；``sleepContext`` → :func:`sleep_context`。
* ``Config`` 字段 / 方法重命名见 :mod:`atri_x.platform.telegram.config`。
* ``target`` 类型与 ctx 键拆到 :mod:`atri_x.platform.telegram.target`；
  ``normalizeMessage`` 等入站 helper 在 :mod:`atri_x.platform.telegram.message`，
  群目录能力在 :mod:`atri_x.platform.telegram.capabilities`，均为 Go 同文件职责。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import unicodedata
from typing import Any, Callable, Sequence

from atri_x.command.types import Info as CommandInfo
from atri_x.delivery.delivery import (
    DeliveryError,
    Kind,
    Output,
    Receipt,
    SentMessage,
    Source,
    Target as DeliveryTarget,
    ctx_error,
    fallback_text,
)
from atri_x.errors import InvalidError
from atri_x.platform import refcontext
from atri_x.platform.backoff import new_backoff
from atri_x.platform.config import primary_command_prefix
from atri_x.platform.platform import (
    Identity,
    MessageContext,
    MessageSegment,
    ReplyContext,
    with_message_context,
)
from atri_x.platform.telegram import capabilities as telegram_capabilities
from atri_x.platform.telegram import message as telegram_message
from atri_x.platform.telegram import stream as telegram_stream
from atri_x.platform.telegram.client import ApiClient, MediaSource, new_api_client
from atri_x.platform.telegram.config import (
    PLATFORM_NAME,
    TELEGRAM_RICH_TEXT_RUNES,
    TELEGRAM_TEXT_PAGE_RUNES,
    Config,
    apply_defaults,
)
from atri_x.platform.telegram.format import (
    plain_text_from_markdown,
    telegram_html_from_markdown,
)
from atri_x.platform.telegram.keyboard import risk_keyboard, should_attach_risk_keyboard
from atri_x.platform.telegram.target import Target, target_from_context, with_target
from atri_x.platform.telegram.types import (
    BotCommand,
    InputRichMessage,
    Message,
    ReplyParameters,
    SendMessageRequest,
    SendRichMessageRequest,
    Update,
)
from atri_x.security import (
    ActorID as security_actor_id,
)
from atri_x.security import (
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
)

#: 私聊/群聊/超级群才进处理流程；频道等只记录历史。对应 Go 里的字面量判断。
SUPPORTED_CHAT_TYPES = ("private", "group", "supergroup")

_INT64_PATTERN = re.compile(r"^[+-]?\d+$")


def new_adapter(cfg: Config, store: Any, chat_history: Any, logger: Any) -> "Adapter":
    """对应 Go ``New``。"""
    return Adapter(cfg, store, chat_history, logger)


class Adapter:
    """Telegram 平台适配器。对应 Go ``Adapter``。"""

    def __init__(self, cfg: Config, store: Any, chat_history: Any, logger: Any) -> None:
        apply_defaults(cfg)
        self.cfg = cfg
        self.store = store
        self.chat_history = chat_history
        self.logger = logger
        self.client: ApiClient = new_api_client(cfg)
        self.notify: Callable[[Any, str], Any] | None = None
        self.bot_id = 0
        self.bot_username = ""
        self.command_catalog: list[CommandInfo] = []
        self._tasks: set[asyncio.Task] = set()

    # -- 生命周期 -----------------------------------------------------------
    def name(self) -> str:
        """对应 Go ``Name``。"""
        return PLATFORM_NAME

    def enabled(self) -> bool:
        """对应 Go ``Enabled``。"""
        return bool(self.cfg.enabled)

    def set_connect_notifier(self, notify: Callable[[Any, str], Any] | None) -> None:
        """对应 Go ``SetConnectNotifier``。"""
        self.notify = notify

    def set_command_catalog(self, infos: Sequence[CommandInfo]) -> None:
        """对应 Go ``SetCommandCatalog``。"""
        self.command_catalog = list(infos)

    async def _notify_connected(self, ctx: Any) -> None:
        if self.notify is not None:
            result = self.notify(ctx, self.name())
            if asyncio.iscoroutine(result):
                await result

    async def run(self, ctx: Any, handler: Any) -> None:
        """long polling 主循环：``getMe`` → ``getUpdates`` → 失败退避重连。

        对应 Go ``Run``。409 Conflict（同 token 多实例）与网络错误走同一条路径：
        记 WARN、跳出内层循环、按 :class:`atri_x.platform.backoff.Backoff` 退避后重来。
        """
        if not self.cfg.enabled:
            return
        backoff = new_backoff(self.cfg.reconnect_interval(), 10.0)
        offset = 0
        while True:
            if ctx_error(ctx) is not None:
                raise_context_error(ctx)
            try:
                me = await self.client.get_me(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接失败按退避重试
                if backoff.should_warn():
                    self._log_warn("telegram getMe failed", error=exc)
                if not await sleep_context(ctx, backoff.delay()):
                    raise_context_error(ctx)
                continue
            backoff.reset()
            self.bot_id = me.id
            self.bot_username = (me.username or "").strip()
            try:
                await self._sync_bot_commands(ctx)
            except Exception as exc:  # noqa: BLE001 - 菜单同步失败不影响收消息
                self._log_warn("sync telegram bot commands failed", error=exc)
            self._log_info("telegram connected", bot_id=me.id, bot_username=me.username)
            self._spawn(self._notify_connected(ctx))
            while True:
                try:
                    updates = await self.client.get_updates(ctx, offset)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if ctx_error(ctx) is not None:
                        raise_context_error(ctx)
                    self._log_warn("telegram getUpdates failed", error=exc)
                    break
                for update in updates:
                    if update.update_id >= offset:
                        offset = update.update_id + 1
                    self._spawn(self.handle_update(ctx, handler, update))
            if not await sleep_context(ctx, backoff.delay()):
                raise_context_error(ctx)

    def pending_tasks(self) -> set[asyncio.Task]:
        """尚未结束的后台任务（Python 侧测试接缝；对应 Go 的 ``go`` 句柄缺失）。"""
        return set(self._tasks)

    def _spawn(self, coro: Any) -> asyncio.Task:
        """对应 Go 的 ``go`` 语句。"""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log_warn("telegram background task failed", error=exc)

    # -- 入站 ---------------------------------------------------------------
    async def handle_update(self, ctx: Any, handler: Any, update: Update) -> None:
        """对应 Go ``handleUpdate``（``edited_message`` 与上游一致地不处理）。"""
        if update.callback_query is not None:
            await self.handle_callback_query(ctx, handler, update.callback_query)
            return
        if update.message is None:
            return
        await self.handle_message(ctx, handler, update.message)

    async def _message_group_role(self, ctx: Any, msg: Message) -> str:
        """群身份查询；失败按未知处理。对应 Go ``messageGroupRole``。"""
        if msg.from_ is None or msg.chat.type not in ("group", "supergroup"):
            return GroupRoleUnknown
        try:
            member = await self.client.get_chat_member(ctx, msg.chat.id, msg.from_.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 查询失败按未知角色
            self._log_warn("get telegram chat member failed", error=exc)
            return GroupRoleUnknown
        status = (member.status or "").strip()
        if status == "creator":
            return GroupRoleOwner
        if status == "administrator":
            return GroupRoleAdmin
        if status in ("member", "restricted"):
            return GroupRoleMember
        return GroupRoleUnknown

    async def handle_callback_query(self, ctx: Any, handler: Any, query: Any) -> None:
        """内联键盘回调 → 一条普通入站消息。对应 Go ``handleCallbackQuery``。"""
        if (query.id or "").strip() != "":
            try:
                await self.client.answer_callback_query(ctx, query.id, "已收到")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log_warn("answer telegram callback failed", error=exc)
        if query.message is None or (query.data or "").strip() == "":
            return
        msg = dataclasses.replace(query.message, from_=query.from_, text=(query.data or "").strip())
        await self.handle_message(ctx, handler, msg)

    async def handle_message(self, ctx: Any, handler: Any, msg: Message) -> None:
        """归一化入站消息并交给 handler。对应 Go ``handleMessage``。"""
        normalized = telegram_message.normalize_message(msg)
        if msg.chat.type not in SUPPORTED_CHAT_TYPES:
            self._record_chat_message(ctx, msg, normalized, ReplyContext())
            return
        text = normalized.text
        platform_user_id = telegram_message.user_id_string(msg.from_)
        group_role = await self._message_group_role(ctx, msg)
        message_ctx = MessageContext(
            platform=self.name(),
            actor_id=security_actor_id(self.name(), platform_user_id),
            platform_user_id=platform_user_id,
            nickname=telegram_message.display_name_ptr(msg.from_, ""),
            display_name=telegram_message.display_name_ptr(msg.from_, ""),
            group_role=group_role,
            scope_id=telegram_message.scope_id(msg.chat),
            conversation_kind=telegram_message.telegram_conversation_kind(msg.chat),
            platform_message_id=telegram_message.format_message_id(msg.message_id),
            reply_to_message_id=normalized.reply_id,
            reply_to_sender_id=telegram_message.user_id_string(
                telegram_message.reply_sender(normalized.reply_message)
            ),
            sender=self,
            media_resolver=self,
            segments=telegram_message.final_message_segments(text, normalized.segments, None),
            raw_text=normalized.text,
            bot=Identity(
                user_id=telegram_message.format_message_id(self.bot_id),
                username=self.bot_username,
            ),
            mentions=list(normalized.mentions),
            trigger_keywords=list(self.cfg.trigger_keywords),
            meta={
                "telegram.message_id": telegram_message.format_message_id(msg.message_id),
                "telegram.chat_id": str(msg.chat.id),
            },
        )
        tg_target = Target(chat_id=msg.chat.id, scope_id=telegram_message.scope_id(msg.chat))
        msg_ctx = with_target(with_message_context(ctx, message_ctx), tg_target)

        if normalized.reply_id != "":
            ref = refcontext.apply(
                refcontext.Options(
                    store=self.store,
                    chat_history=self.chat_history,
                    platform=self.name(),
                    scope_id=message_ctx.scope_id,
                    actor_id=message_ctx.actor_id,
                    is_superadmin=telegram_message.is_configured_superadmin(
                        self.cfg.superadmins, platform_user_id
                    ),
                    reply_id=normalized.reply_id,
                    text=text,
                    command_prefixes=self.cfg.command_prefixes,
                    fetch=telegram_message.reference_fetcher(msg, normalized),
                )
            )
            message_ctx.fork_from_message_id = ref.fork_from_message_id
            message_ctx.resume_session_id = ref.resume_session_id
            message_ctx.context_text = ref.text
            message_ctx.reply = ref.reply
            if (ref.text or "").strip() != "" or ref.reference_segments:
                message_ctx.context_segments = telegram_message.final_message_segments(
                    ref.text, normalized.segments, ref.reference_segments
                )
        self._record_chat_message(ctx, msg, normalized, message_ctx.reply)
        if (
            text.strip() == ""
            and (message_ctx.context_text or "").strip() == ""
            and not message_ctx.context_segments
            and not normalized.segments
        ):
            return
        message_ctx.segments = telegram_message.final_message_segments(text, normalized.segments, None)
        msg_ctx = with_target(with_message_context(ctx, message_ctx), tg_target)
        # Python 版新增（1.0.10）：平台侧发送时间随 ctx 进 Agent（相对时间基准）。
        if msg.date > 0:
            from datetime import datetime, timezone

            from atri_x.agent.time_context import with_platform_sent_at

            msg_ctx = with_platform_sent_at(
                msg_ctx, datetime.fromtimestamp(msg.date, timezone.utc)
            )
        try:
            await handler.handle_message(msg_ctx, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 处理失败只记日志
            self._log_warn(
                "handle telegram message failed", error=exc, message_id=msg.message_id
            )

    def _record_chat_message(
        self, ctx: Any, msg: Message, normalized: Any, reply: ReplyContext
    ) -> None:
        telegram_message.record_chat_message(
            self.chat_history, self.name(), self._log_warn, msg, normalized, reply
        )

    # -- 出站 ---------------------------------------------------------------
    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``SendChat``（落点来自 ctx 里的 target）。"""
        return await self._send_context_output(ctx, outputs)

    async def call_platform_api(self, ctx: Any, api: str, params: dict[str, Any] | None) -> str:
        """直接调用 Bot API。对应 Go ``CallPlatformAPI``。"""
        if self.client is None:
            raise InvalidError("telegram api client is not configured")
        return await self.client.call_raw(ctx, (api or "").strip(), params)

    async def send_notice(self, ctx: Any, notice: Any) -> Receipt:
        """对应 Go ``SendNotice``：空 Target 表示发回当前会话。"""
        out_target = notice.target
        outputs = notice.outputs
        if out_target.empty():
            return await self.send_chat(ctx, outputs)
        return await self._send_target(ctx, out_target, outputs)

    async def _send_context_output(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        t, ok = target_from_context(ctx)
        if not ok or t.chat_id == 0:
            raise InvalidError("telegram send target missing")
        return await self._send_outputs(ctx, t, outputs)

    async def _send_target(
        self, ctx: Any, out_target: DeliveryTarget, outputs: Sequence[Output]
    ) -> Receipt:
        if out_target.superadmins:
            if not self.cfg.superadmins:
                raise InvalidError("telegram superadmins are not configured")
            receipt = Receipt()
            for value in self.cfg.superadmins:
                superadmin = (value or "").strip()
                if superadmin == "":
                    continue
                copy_target = dataclasses.replace(
                    out_target,
                    superadmins=False,
                    private_user_id=superadmin,
                    group_id="",
                    scope_id="",
                )
                try:
                    sent = await self._send_target(ctx, copy_target, outputs)
                except Exception as exc:  # noqa: BLE001 - 保留已成功目标的回执
                    partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
                    merged = receipt.merge(partial)
                    raise DeliveryError(str(exc), merged.mark_partial_failure(exc)) from exc
                receipt = receipt.merge(sent)
            return receipt
        t = target_from_delivery(out_target)
        return await self._send_outputs(ctx, t, outputs)

    async def _send_outputs(self, ctx: Any, t: Target, outputs: Sequence[Output]) -> Receipt:
        receipt = Receipt()
        for index, out in enumerate(outputs):
            try:
                sent = await self._send_to_target(ctx, t, out)
            except Exception as exc:  # noqa: BLE001 - 失败也要记下已成功项
                partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
                sent = telegram_media_receipt(partial, t, out, index)
                merged = receipt.merge(sent)
                raise DeliveryError(str(exc), merged.mark_partial_failure(exc)) from exc
            sent = telegram_media_receipt(sent, t, out, index)
            receipt = receipt.merge(sent)
        return receipt

    async def _send_to_target(self, ctx: Any, t: Target, out: Output) -> Receipt:
        keyboard = should_attach_risk_keyboard(out.text, self.command_prefix())
        if out.kind == Kind.TEXT:
            return await self.send_text(ctx, t, out.text, 0, keyboard)
        if out.kind == Kind.REPLY:
            reply_to = parse_int64(out.reply_to_platform_message_id)
            return await self.send_text(
                ctx, t, out.text, reply_to if reply_to is not None else 0, keyboard
            )
        if out.kind == Kind.EMOTICON:
            return await self.send_sticker(ctx, t, out, 0)
        if out.kind == Kind.IMAGE:
            return await self.send_photo(ctx, t, out, 0)
        if out.kind == Kind.FILE:
            return await self.send_document(ctx, t, out, 0)
        return await self.send_text(ctx, t, fallback_text(out), 0, False)

    async def send_text(
        self, ctx: Any, t: Target, text: str, reply_to: int, keyboard: bool
    ) -> Receipt:
        """按 ``format`` 发送文本；``rich`` → ``html`` → ``plain`` 逐级降级。"""
        text = (text or "").strip()
        if text == "":
            return Receipt()
        fmt = self.cfg.message_format()
        if fmt == "rich":
            try:
                return await self.send_rich_text(ctx, t, text, reply_to, keyboard)
            except Exception as exc:  # noqa: BLE001 - 与 Go 一致：任何错误都先看是否已投递部分
                partial = _partial_receipt(exc)
                if partial.platform_message_ids:
                    # Rich Message 已可见，HTML 兜底会重复投递。
                    raise DeliveryError(str(exc), partial.mark_partial_failure(exc)) from exc
                self._log_warn("telegram rich message failed, fallback to html", error=exc)
                return await self.send_html_text(ctx, t, text, reply_to, keyboard)
        if fmt == "plain":
            return await self.send_plain_text(ctx, t, text, reply_to, keyboard)
        return await self.send_html_text(ctx, t, text, reply_to, keyboard)

    async def send_rich_text(
        self, ctx: Any, t: Target, text: str, reply_to: int, keyboard: bool
    ) -> Receipt:
        """Rich Message 发送（分页）。对应 Go ``sendRichText``。"""
        pages = telegram_rich_text_pages(text)
        receipt = Receipt()
        for index, page in enumerate(pages):
            request = SendRichMessageRequest(
                chat_id=t.chat_id, rich_message=InputRichMessage(markdown=page)
            )
            if index == 0:
                if reply_to != 0:
                    request.reply_parameters = ReplyParameters(message_id=reply_to)
                if keyboard:
                    request.reply_markup = risk_keyboard(self.command_prefix())
            try:
                msg = await self.client.send_rich_message(ctx, request)
            except Exception as exc:  # noqa: BLE001 - 保留已成功页
                raise DeliveryError(str(exc), receipt.mark_partial_failure(exc)) from exc
            if msg.message_id != 0:
                receipt.platform_message_ids.append(
                    telegram_message.format_message_id(msg.message_id)
                )
        return receipt

    async def send_html_text(
        self, ctx: Any, t: Target, text: str, reply_to: int, keyboard: bool
    ) -> Receipt:
        """HTML 发送，失败降级纯文本。对应 Go ``sendHTMLText``。"""
        try:
            return await self._send_formatted_text(
                ctx, t, telegram_html_from_markdown(text), "HTML", reply_to, keyboard
            )
        except Exception as exc:  # noqa: BLE001
            partial = _partial_receipt(exc)
            if partial.platform_message_ids:
                # 部分分页已可见，纯文本兜底会重复投递。
                raise DeliveryError(str(exc), partial.mark_partial_failure(exc)) from exc
            self._log_warn("telegram html message failed, fallback to plain", error=exc)
            return await self.send_plain_text(ctx, t, text, reply_to, keyboard)

    async def send_plain_text(
        self, ctx: Any, t: Target, text: str, reply_to: int, keyboard: bool
    ) -> Receipt:
        """对应 Go ``sendPlainText``。"""
        return await self._send_formatted_text(
            ctx, t, plain_text_from_markdown(text), "", reply_to, keyboard
        )

    async def _send_formatted_text(
        self,
        ctx: Any,
        t: Target,
        text: str,
        parse_mode: str,
        reply_to: int,
        keyboard: bool,
    ) -> Receipt:
        """按 4096 rune 分页发送。对应 Go ``sendFormattedText``。"""
        pages = telegram_text_pages(text)
        receipt = Receipt()
        for index, page in enumerate(pages):
            request = SendMessageRequest(chat_id=t.chat_id, text=page, parse_mode=parse_mode)
            if index == 0:
                request.reply_to_message_id = reply_to
                if keyboard:
                    request.reply_markup = risk_keyboard(self.command_prefix())
            try:
                msg = await self.client.send_message(ctx, request)
            except Exception as exc:  # noqa: BLE001 - 保留已成功页
                raise DeliveryError(str(exc), receipt.mark_partial_failure(exc)) from exc
            if msg.message_id != 0:
                receipt.platform_message_ids.append(
                    telegram_message.format_message_id(msg.message_id)
                )
        return receipt

    async def send_photo(self, ctx: Any, t: Target, out: Output, reply_to: int) -> Receipt:
        """对应 Go ``sendPhoto``。"""
        msg = await self.client.send_photo(ctx, t.chat_id, source_from_output(out), reply_to)
        return receipt_with_message_id(msg.message_id)

    async def send_document(self, ctx: Any, t: Target, out: Output, reply_to: int) -> Receipt:
        """对应 Go ``sendDocument``。"""
        msg = await self.client.send_document(ctx, t.chat_id, source_from_output(out), reply_to)
        return receipt_with_message_id(msg.message_id)

    async def send_sticker(self, ctx: Any, t: Target, out: Output, reply_to: int) -> Receipt:
        """对应 Go ``sendSticker``。"""
        msg = await self.client.send_sticker(ctx, t.chat_id, out.emoticon_id, reply_to)
        return receipt_with_message_id(msg.message_id)

    # -- 可选能力 -----------------------------------------------------------
    def stream_preview_payload(self, text: str) -> tuple[str, str]:
        """预览正文与 parse_mode。对应 Go ``(*Adapter).streamPreviewPayload``。"""
        return telegram_stream.stream_preview_payload(self, text)

    def start_stream(self, ctx: Any) -> telegram_stream.MessageStream:
        """开启流式消息。对应 Go ``(*Adapter).StartStream``。"""
        return telegram_stream.start_stream(self, ctx)

    async def resolve_media(self, ctx: Any, segment: MessageSegment, max_bytes: int) -> Source:
        """对应 Go ``(*Adapter).ResolveMedia``。"""
        return await telegram_message.resolve_media(self.client, ctx, segment, max_bytes)

    async def get_group_info(self, ctx: Any, group_id: str) -> Any:
        """对应 Go ``(*Adapter).GetGroupInfo``。"""
        return await telegram_capabilities.get_group_info(self, ctx, group_id)

    async def get_group_member_list(self, ctx: Any, group_id: str) -> list[Any]:
        """对应 Go ``(*Adapter).GetGroupMemberList``（只有管理员）。"""
        return await telegram_capabilities.get_group_member_list(self, ctx, group_id)

    # -- 命令菜单 -----------------------------------------------------------
    def command_prefix(self) -> str:
        return primary_command_prefix(self.cfg.command_prefixes)

    async def _sync_bot_commands(self, ctx: Any) -> None:
        """同步 bot 命令菜单。对应 Go ``syncBotCommands``。

        Telegram 的命令菜单只支持 ``/`` 前缀；前缀不是 ``/`` 时不同步，
        避免菜单点了不生效。
        """
        prefix = self.command_prefix()
        if prefix != "/":
            self._log_warn(
                "telegram bot command menu skipped: command prefix is not /", prefix=prefix
            )
            return
        commands = telegram_bot_commands(self.command_catalog)
        if not commands:
            return
        await self.client.set_my_commands(ctx, commands)

    # -- 日志 ---------------------------------------------------------------
    def _log_info(self, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.info(message, **fields)

    def _log_warn(self, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.warn(message, **fields)


# ---------------------------------------------------------------------------
# 模块级 helper（对应 Go adapter.go 里的自由函数）
# ---------------------------------------------------------------------------

def target_from_delivery(out_target: DeliveryTarget) -> Target:
    """把 ``delivery.Target`` 解析成 chat id + scope。对应 Go ``targetFromDelivery``。"""
    out_target = dataclasses.replace(out_target)
    if not (out_target.private_user_id or "").strip() and not (out_target.group_id or "").strip():
        scope = (out_target.scope_id or "").strip()
        for prefix in ("private:", "group:", "supergroup:"):
            if scope.startswith(prefix):
                if prefix == "private:":
                    out_target.private_user_id = scope[len(prefix) :]
                else:
                    out_target.group_id = scope[len(prefix) :]
                break
    id_text = telegram_message.first_non_empty(
        out_target.private_user_id, out_target.group_id
    )
    if id_text.strip() == "":
        raise InvalidError("telegram target missing private_user_id, group_id or scope_id")
    chat_id = parse_int64(id_text)
    if chat_id is None:
        raise InvalidError(f"parse telegram chat id: {id_text!r} is not a valid int64")
    scope = (out_target.scope_id or "").strip()
    if scope == "":
        if (out_target.private_user_id or "").strip() != "":
            scope = "private:" + str(chat_id)
        else:
            scope = "group:" + str(chat_id)
    return Target(chat_id=chat_id, scope_id=scope)


def source_from_output(out: Output) -> MediaSource:
    """对应 Go ``sourceFromOutput``。"""
    return MediaSource(
        url=out.source.url,
        path=out.source.path,
        data=out.source.data,
        name=telegram_message.media_source_name(out),
    )


def telegram_media_receipt(
    receipt: Receipt, target: Target, out: Output, output_index: int
) -> Receipt:
    """给媒体输出补一条结构化 ``SentMessage``。对应 Go ``telegramMediaReceipt``。

    只对**恰好一条**平台消息 ID 的图片/文件输出生效；文本降级不关联媒体。
    """
    if len(receipt.platform_message_ids) != 1 or out.kind not in (Kind.IMAGE, Kind.FILE):
        return receipt
    scope = (target.scope_id or "").strip()
    if scope == "":
        scope = "private:" + str(target.chat_id)
    out_receipt = dataclasses.replace(
        receipt,
        platform_message_ids=list(receipt.platform_message_ids),
        sent_messages=list(receipt.sent_messages),
    )
    out_receipt.sent_messages.append(
        SentMessage(
            platform_message_id=receipt.platform_message_ids[0],
            platform=PLATFORM_NAME,
            scope_id=scope,
            output_indexes=[output_index],
        )
    )
    return out_receipt


def telegram_bot_commands(infos: Sequence[CommandInfo]) -> list[BotCommand]:
    """把命令目录转成 bot 菜单项。对应 Go ``telegramBotCommands``。"""
    out: list[BotCommand] = []
    seen: set[str] = set()
    for info in infos:
        name = (info.name or "").removeprefix("/").strip()
        if not valid_telegram_command_name(name) or name in seen:
            continue
        description = (info.description or "").strip()
        if description == "":
            description = (info.usage or "").strip()
        if description == "":
            continue
        out.append(BotCommand(command=name, description=truncate_runes(description, 256)))
        seen.add(name)
    return out


def valid_telegram_command_name(name: str) -> bool:
    """命令名是否合法（``[a-z0-9_]``，≤32）。对应 Go ``validTelegramCommandName``。"""
    if name == "" or len(name) > 32:
        return False
    for ch in name:
        if ch == "_" or unicodedata.category(ch) == "Nd" or ("a" <= ch <= "z"):
            continue
        return False
    return True


def truncate_runes(text: str, limit: int) -> str:
    """按码点截断。对应 Go ``truncateRunes``。"""
    if len(text) <= limit:
        return text
    return text[:limit]


def receipt_with_message_id(message_id: int) -> Receipt:
    """对应 Go ``receiptWithMessageID``。"""
    if message_id == 0:
        return Receipt()
    return Receipt(platform_message_ids=[telegram_message.format_message_id(message_id)])


def telegram_rich_text_pages(text: str) -> list[str]:
    """Rich Message 分页（32768 rune）。对应 Go ``telegramRichTextPages``。"""
    return telegram_text_pages_by_limit(text, TELEGRAM_RICH_TEXT_RUNES)


def telegram_text_pages(text: str) -> list[str]:
    """普通文本分页（4096 rune）。对应 Go ``telegramTextPages``。"""
    return telegram_text_pages_by_limit(text, TELEGRAM_TEXT_PAGE_RUNES)


def telegram_text_pages_by_limit(text: str, limit: int) -> list[str]:
    """按 rune 上限切块并附 ``（n/N）`` 页码。对应 Go ``telegramTextPagesByLimit``。

    切块发生在码点边界上：不会切开 UTF-16 代理对（Python ``str`` 按码点索引，
    Go ``[]rune`` 亦然），也不会在实体中间断开——上游按裸 rune 切，超长文本里的
    实体在分页处会各自落在不同消息里，这是上游既有行为，本次保真保留。
    """
    if len(text) <= limit:
        return [text]
    page_size = limit - 16
    if page_size <= 0:
        page_size = limit
    total = (len(text) + page_size - 1) // page_size
    pages: list[str] = []
    for start in range(0, len(text), page_size):
        chunk = text[start : start + page_size]
        pages.append(f"{chunk}\n（{len(pages) + 1}/{total}）")
    return pages


async def sleep_context(ctx: Any, delay: float) -> bool:
    """可被 ctx 取消的 sleep。对应 Go ``sleepContext``。

    返回 False 表示等待期间 ctx 被取消。
    """
    if ctx_error(ctx) is not None:
        return False
    if delay <= 0:
        await asyncio.sleep(0)
        return ctx_error(ctx) is None
    event = asyncio.Event()
    remove: Callable[[], None] | None = None
    adder = getattr(ctx, "add_callback", None)
    if callable(adder):
        remove = adder(event.set)
    try:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(event.wait(), delay)
    finally:
        if remove is not None:
            remove()
    return ctx_error(ctx) is None


def raise_context_error(ctx: Any) -> None:
    """抛出 ctx 的取消原因。对应 Go 的 ``return ctx.Err()``。"""
    err = ctx_error(ctx)
    if err is not None:
        raise err
    raise asyncio.CancelledError("telegram adapter context cancelled")


def parse_int64(text: str) -> int | None:
    """对应 Go ``strconv.ParseInt(s, 10, 64)``；失败/越界返回 ``None``。"""
    value = (text or "").strip()
    if not _INT64_PATTERN.match(value):
        return None
    number = int(value)
    if number < -(2**63) or number > 2**63 - 1:
        return None
    return number


def _partial_receipt(exc: BaseException) -> Receipt:
    """取异常携带的部分成功回执（非 DeliveryError 视为空）。"""
    return exc.receipt if isinstance(exc, DeliveryError) else Receipt()


__all__ = [name for name in dir() if not name.startswith("_")]
