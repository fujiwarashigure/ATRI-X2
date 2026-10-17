"""流式助手消息：增量预览 + 最终替换。

对应 Go：``internal/platform/telegram/stream.go``。

**已知上游缺陷（本次保真保留，不改成增量编辑）**：
:meth:`MessageStream.append` 在**节流早退之前**就对**全文**做 ``[]rune`` 截断
（:func:`stream_preview_text`）并跑 Markdown→HTML 的 6 条正则
（:func:`atri_x.platform.telegram.format.telegram_html_from_markdown`），
因此每个分片都付全量代价，累计 **O(n²)**
（《ATRI-X Python/Go 研究报告》记录：
``internal/platform/telegram/stream.go:44,45`` 早于 ``:50,:69`` 的节流检查，
``:147-166`` 是预览与格式化）。本次移植顺序逐行照搬，行为（节流间隔、编辑触发
条件、4096 rune 预览截断、失败降级、结束时 flush）与上游一致。

其他形式差异：

* ``sync.Mutex`` → ``asyncio.Lock``（跨 await 串行化）。
* ``time.Since(lastEdit)`` → :func:`time.monotonic`；``lastEdit`` 为 0 表示
  "还没编辑过"（等价于 Go 的 ``time.Time{}`` 零值，``time.Since`` 必然超过节流间隔）。
* Go 的 ``(Receipt, error)`` → 抛 :class:`atri_x.delivery.delivery.DeliveryError`
  并携带部分成功的 ``receipt``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from atri_x.delivery.delivery import DeliveryError, Receipt
from atri_x.errors import InvalidError
from atri_x.platform.telegram.config import TELEGRAM_TEXT_PAGE_RUNES
from atri_x.platform.telegram.format import (
    plain_text_from_markdown,
    telegram_html_from_markdown,
)
from atri_x.platform.telegram.message import format_message_id
from atri_x.platform.telegram.target import Target, target_from_context
from atri_x.platform.telegram.types import (
    EditMessageTextRequest,
    InputRichMessage,
    SendMessageRequest,
    SendRichMessageDraftRequest,
)

#: 预览被截断时追加的提示（与 Go 的字面量逐字一致）。
STREAM_TRUNCATED_SUFFIX = "\n……（生成中，完整内容稍后发送）"


class MessageStream:
    """对应 Go ``messageStream``。"""

    def __init__(
        self,
        adapter: Any,
        target: Target,
        *,
        draft_id: int = 0,
        use_draft: bool = False,
    ) -> None:
        self.adapter = adapter
        self.target = target
        self.message = 0
        self.draft_id = draft_id
        self.use_draft = use_draft
        self.text = ""
        self.last_edit = 0.0
        self.finished = False
        self._lock = asyncio.Lock()

    async def append(self, ctx: Any, text: str) -> None:
        """追加分片并按节流刷新预览。对应 Go ``(*messageStream).Append``。"""
        if text == "":
            return
        async with self._lock:
            if self.finished:
                return
            self.text += text
            # 注意：这两步在节流判断之前，是上游 O(n²) 的来源，保真保留。
            preview = stream_preview_text(self.text)
            preview_text, preview_parse_mode = self.adapter.stream_preview_payload(preview)
            if preview_text.strip() == "":
                return
            if self.use_draft:
                if self._throttled():
                    return
                try:
                    await self.adapter.client.send_rich_message_draft(
                        ctx,
                        SendRichMessageDraftRequest(
                            chat_id=self.target.chat_id,
                            draft_id=self.draft_id,
                            rich_message=InputRichMessage(markdown=preview),
                        ),
                    )
                except Exception:  # noqa: BLE001 - 草稿失败就退回普通消息
                    self.use_draft = False
                else:
                    self._mark_edit()
                    return
            if self.message == 0:
                sent = await self.adapter.client.send_message(
                    ctx,
                    SendMessageRequest(
                        chat_id=self.target.chat_id,
                        text=preview_text,
                        parse_mode=preview_parse_mode,
                    ),
                )
                self.message = sent.message_id
                self._mark_edit()
                return
            if self._throttled():
                return
            await self.adapter.client.edit_message_text(
                ctx,
                EditMessageTextRequest(
                    chat_id=self.target.chat_id,
                    message_id=self.message,
                    text=preview_text,
                    parse_mode=preview_parse_mode,
                ),
            )
            self._mark_edit()

    async def replace(self, ctx: Any, text: str) -> Receipt:
        """用最终全文替换预览（必要时分页）。对应 Go ``(*messageStream).Replace``。"""
        async with self._lock:
            if self.finished:
                return Receipt()
            text = (text or "").strip()
            if text == "":
                text = self.text.strip()
            if text == "":
                return Receipt()
            if self.adapter.cfg.rich_message_enabled():
                if self.message != 0:
                    # 尽力而为：失败不影响后续发送。
                    try:
                        await self.adapter.client.edit_message_text(
                            ctx,
                            EditMessageTextRequest(
                                chat_id=self.target.chat_id,
                                message_id=self.message,
                                text="回复已生成：",
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    receipt = await self.adapter.send_rich_text(ctx, self.target, text, 0, False)
                except Exception as exc:  # noqa: BLE001 - 与 Go 一致：任何错误都先看是否已投递部分
                    partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
                    if partial.platform_message_ids:
                        # 已经投递了部分页，退回 sendMessage 会产生重复内容。
                        raise DeliveryError(str(exc), partial.mark_partial_failure(exc)) from exc
                    self.adapter.log_warn(
                        "telegram rich stream final failed, fallback to sendMessage", error=exc
                    )
                else:
                    self.finished = True
                    return receipt
            pages = _text_pages(text)
            if self.message == 0:
                page_text, page_parse_mode = self.adapter.stream_preview_payload(pages[0])
                sent = await self.adapter.client.send_message(
                    ctx,
                    SendMessageRequest(
                        chat_id=self.target.chat_id,
                        text=page_text,
                        parse_mode=page_parse_mode,
                    ),
                )
                self.message = sent.message_id
            else:
                page_text, page_parse_mode = self.adapter.stream_preview_payload(pages[0])
                await self.adapter.client.edit_message_text(
                    ctx,
                    EditMessageTextRequest(
                        chat_id=self.target.chat_id,
                        message_id=self.message,
                        text=page_text,
                        parse_mode=page_parse_mode,
                    ),
                )
            receipt = Receipt(platform_message_ids=[format_message_id(self.message)])
            for page in pages[1:]:
                page_text, page_parse_mode = self.adapter.stream_preview_payload(page)
                try:
                    sent = await self.adapter.client.send_message(
                        ctx,
                        SendMessageRequest(
                            chat_id=self.target.chat_id,
                            text=page_text,
                            parse_mode=page_parse_mode,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - 保留已成功页的回执
                    raise DeliveryError(str(exc), receipt.mark_partial_failure(exc)) from exc
                if sent.message_id != 0:
                    receipt.platform_message_ids.append(format_message_id(sent.message_id))
            self.finished = True
            return receipt

    async def finish(self, ctx: Any) -> Receipt:
        """结束流式（不额外发消息，只回执已发出的预览）。对应 Go ``Finish``。"""
        async with self._lock:
            self.finished = True
            if self.message == 0:
                return Receipt()
            return Receipt(platform_message_ids=[format_message_id(self.message)])

    # -- 内部 ---------------------------------------------------------------
    def _throttled(self) -> bool:
        """节流判定。对应 Go 的 ``time.Since(s.lastEdit) < interval``。"""
        if self.last_edit == 0.0:
            return False
        return (time.monotonic() - self.last_edit) < self.adapter.cfg.stream_edit_interval()

    def _mark_edit(self) -> None:
        self.last_edit = time.monotonic()


def start_stream(adapter: Any, ctx: Any) -> MessageStream:
    """从 ctx 取落点并开启流式。对应 Go ``(*Adapter).StartStream``。

    私聊 + ``format="rich"`` 时使用 Rich Message 草稿（``sendRichMessageDraft``）。
    """
    target, ok = target_from_context(ctx)
    if not ok or target.chat_id == 0:
        raise InvalidError("telegram stream target missing")
    return MessageStream(
        adapter,
        target,
        draft_id=time.time_ns(),
        use_draft=target.scope_id.startswith("private:") and adapter.cfg.rich_message_enabled(),
    )


def stream_preview_text(text: str) -> str:
    """预览文本：超过 4096 rune 时截断并附"生成中"提示。对应 Go ``streamPreviewText``。"""
    text = (text or "").strip()
    if text == "":
        return ""
    if len(text) <= TELEGRAM_TEXT_PAGE_RUNES:
        return text
    return text[: TELEGRAM_TEXT_PAGE_RUNES - 24] + STREAM_TRUNCATED_SUFFIX


def stream_preview_payload(adapter: Any, text: str) -> tuple[str, str]:
    """预览正文与 parse_mode；纯文本格式不带 parse_mode。对应 Go ``streamPreviewPayload``。"""
    if adapter.cfg.message_format() in ("plain", "rich"):
        return plain_text_from_markdown(text), ""
    return telegram_html_from_markdown(text), "HTML"


def _text_pages(text: str) -> list[str]:
    """按 4096 rune 分页（``adapter.telegramTextPages`` 的等价物）。

    这里**延迟导入**：``adapter`` 在模块级导入本模块（``adapter.py:57``），本模块再
    在模块级反向导入就成了循环。真实事故：这里原先直接写 ``telegram_text_pages(text)``
    却没有导入它，Telegram 流式 ``replace`` 一跑到这行就 ``NameError``；单测用假分页
    函数时看不见。
    """
    from atri_x.platform.telegram.adapter import telegram_text_pages

    return telegram_text_pages(text)


__all__ = [name for name in dir() if not name.startswith("_")]
