"""QQ 官方出站发送：文本分页、Markdown 降级、媒体上传、键盘、Ark。

对应 Go：``internal/platform/qqofficial/send.go``。

要点（上游语义）：

* 默认先发 Markdown；**只有当一条都还没发出去**时才降级成纯文本 —— 已经可见的
  Markdown 分页再发纯文本会重复投递，那种情况改为报告部分失败。
* 每个分页单独一次 API 调用，失败时回执保留已成功的页（异常里带 receipt）。
* ``msg_id`` / ``event_id`` 二选一：有 ``msg_id`` 就带 ``msg_seq``（同一被回复
  消息的序号自增），否则带 ``event_id``。

形式差异：Go 的 ``(*Adapter)`` 方法 → 模块级协程函数（首参是适配器），由
:class:`atri_x.platform.qqofficial.adapter.Adapter` 的同名方法委托；``(Receipt,
error)`` 双返回 → 抛 :class:`atri_x.delivery.delivery.DeliveryError`（带 receipt）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from atri_x.delivery.delivery import (
    DeliveryError,
    Kind,
    Output,
    Receipt,
    SentMessage,
    ctx_value,
    fallback_text,
)
from atri_x.errors import InvalidError
from atri_x.platform.config import primary_command_prefix
from atri_x.platform.platform import message_context_from
from atri_x.platform.qqofficial.client import prepare_source
from atri_x.platform.qqofficial.config import PLATFORM_NAME
from atri_x.platform.qqofficial.keyboard import risk_keyboard, should_attach_risk_keyboard
from atri_x.platform.qqofficial.message import META_EVENT_ID, META_MSG_ID
from atri_x.platform.qqofficial.target import (
    TARGET_C2C,
    TARGET_GROUP,
    TARGET_KEY,
    SendTarget,
    target_scope,
)
from atri_x.platform.qqofficial.types import (
    MessageArk,
    MessageMarkdown,
    MessageMedia,
    MessageToCreate,
)

#: 消息类型。对应 Go 常量。
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_ARK = 3
MSG_TYPE_MEDIA = 7

#: 媒体文件类型。对应 Go 常量。
FILE_TYPE_IMAGE = 1
FILE_TYPE_FILE = 4

#: 单页文本的 rune 上限。对应 Go ``qqOfficialTextPageRunes``。
QQ_OFFICIAL_TEXT_PAGE_RUNES = 1500


def receipt_with_message_id(message_id: str) -> Receipt:
    """单个平台消息 ID 的回执。对应 Go ``receiptWithMessageID``。"""
    message_id = (message_id or "").strip()
    if message_id == "":
        return Receipt()
    return Receipt(platform_message_ids=[message_id])


def qq_official_text_pages(text: str) -> list[str]:
    """按 rune 分页并附 ``……（n/N）`` 续页标记。对应 Go ``qqOfficialTextPages``。"""
    if len(text) <= QQ_OFFICIAL_TEXT_PAGE_RUNES:
        return [text]
    page_size = QQ_OFFICIAL_TEXT_PAGE_RUNES - 16
    if page_size <= 0:
        page_size = QQ_OFFICIAL_TEXT_PAGE_RUNES
    total = (len(text) + page_size - 1) // page_size
    pages: list[str] = []
    for start in range(0, len(text), page_size):
        chunk = text[start : start + page_size]
        pages.append(f"{chunk}……（{len(pages) + 1}/{total}）")
    return pages


def qq_official_media_receipt(
    receipt: Receipt, target: SendTarget, out: Output, output_index: int
) -> Receipt:
    """给图片/文件输出补一条结构化 ``SentMessage``。对应 Go ``qqOfficialMediaReceipt``。"""
    if len(receipt.platform_message_ids) != 1 or out.kind not in (Kind.IMAGE, Kind.FILE):
        return receipt
    out_receipt = Receipt(
        platform_message_ids=list(receipt.platform_message_ids),
        sent_messages=list(receipt.sent_messages),
        failed=receipt.failed,
        failure=receipt.failure,
    )
    out_receipt.sent_messages.append(
        SentMessage(
            platform_message_id=receipt.platform_message_ids[0],
            platform=PLATFORM_NAME,
            scope_id=target_scope(target),
            output_indexes=[output_index],
        )
    )
    return out_receipt


def meta_string(meta: Mapping[str, Any] | None, key: str) -> str:
    """取 ``MessageContext.meta`` 里的字符串值。对应 Go ``metaString``。"""
    if not meta:
        return ""
    value = meta.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


def base_message(adapter: Any, target: SendTarget) -> MessageToCreate:
    """带 ``msg_id``/``msg_seq`` 或 ``event_id`` 的空消息。对应 Go ``baseMessage``。"""
    msg = MessageToCreate()
    if (target.msg_id or "").strip() != "":
        msg.msg_id = target.msg_id.strip()
        msg.msg_seq = adapter.next_msg_seq(msg.msg_id)
    elif (target.event_id or "").strip() != "":
        msg.event_id = target.event_id.strip()
    return msg


async def context_target(adapter: Any, ctx: Any) -> SendTarget:
    """从 ctx 里解析发送落点。对应 Go ``(*Adapter).contextTarget``。"""
    value = ctx_value(ctx, TARGET_KEY)
    if isinstance(value, SendTarget) and value.kind != "" and (value.openid or "").strip() != "":
        return value
    message_ctx, ok = message_context_from(ctx)
    if ok:
        scope = (message_ctx.scope_id or "").strip()
        if scope.startswith("group:"):
            return SendTarget(
                kind=TARGET_GROUP,
                openid=scope.removeprefix("group:"),
                msg_id=meta_string(message_ctx.meta, META_MSG_ID),
                event_id=meta_string(message_ctx.meta, META_EVENT_ID),
            )
        if scope.startswith("c2c:"):
            return SendTarget(
                kind=TARGET_C2C,
                openid=scope.removeprefix("c2c:"),
                msg_id=meta_string(message_ctx.meta, META_MSG_ID),
                event_id=meta_string(message_ctx.meta, META_EVENT_ID),
            )
        openid = (message_ctx.platform_user_id or "").strip()
        if openid.startswith(PLATFORM_NAME + ":"):
            openid = openid[len(PLATFORM_NAME) + 1 :]
        if openid != "":
            return SendTarget(
                kind=TARGET_C2C,
                openid=openid,
                msg_id=meta_string(message_ctx.meta, META_MSG_ID),
                event_id=meta_string(message_ctx.meta, META_EVENT_ID),
            )
    raise InvalidError("qqofficial send target missing")


async def send_context_output(
    adapter: Any, ctx: Any, outputs: Sequence[Output]
) -> Receipt:
    """按 ctx 里的落点逐条发送。对应 Go ``(*Adapter).sendContextOutput``。"""
    target = await context_target(adapter, ctx)
    receipt = Receipt()
    for index, out in enumerate(outputs):
        try:
            sent = await send_output(adapter, ctx, target, out)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 保留已成功项
            partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
            merged = receipt.merge(partial)
            raise DeliveryError(str(exc), merged.mark_partial_failure(exc)) from exc
        sent = qq_official_media_receipt(sent, target, out, index)
        receipt = receipt.merge(sent)
    return receipt


async def send_output(adapter: Any, ctx: Any, target: SendTarget, out: Output) -> Receipt:
    """单条输出。对应 Go ``(*Adapter).sendOutput``。"""
    if out.kind == Kind.TEXT:
        return await send_text(adapter, ctx, target, out.text)
    if out.kind == Kind.REPLY:
        return await send_text(adapter, ctx, target, fallback_text(out))
    if out.kind == Kind.EMOTICON:
        return await send_text(adapter, ctx, target, fallback_text(out))
    if out.kind == Kind.IMAGE:
        return await send_media(adapter, ctx, target, out, FILE_TYPE_IMAGE)
    if out.kind == Kind.FILE:
        return await send_media(adapter, ctx, target, out, FILE_TYPE_FILE)
    return await send_text(adapter, ctx, target, fallback_text(out))


async def send_text(adapter: Any, ctx: Any, target: SendTarget, text: str) -> Receipt:
    """发文本（Markdown 优先、可降级）。对应 Go ``(*Adapter).sendText``。"""
    text = (text or "").strip()
    if text == "":
        return Receipt()
    cfg = adapter.cfg
    if target.proactive and not cfg.allow_proactive_messages():
        raise InvalidError("qqofficial proactive messages are disabled")
    prefix = primary_command_prefix(cfg.command_prefixes)
    if cfg.markdown_by_default_enabled():
        try:
            return await send_text_pages(adapter, ctx, target, text, MSG_TYPE_MARKDOWN, prefix)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 已投递分页时不再降级
            partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
            if partial.platform_message_ids:
                raise DeliveryError(str(exc), partial.mark_partial_failure(exc)) from exc
            adapter.log_warn(
                ctx, "qqofficial markdown send failed, fallback to text", error=exc
            )
    return await send_text_pages(adapter, ctx, target, text, MSG_TYPE_TEXT, prefix)


async def send_text_pages(
    adapter: Any, ctx: Any, target: SendTarget, text: str, msg_type: int, prefix: str
) -> Receipt:
    """分页发送。对应 Go ``(*Adapter).sendTextPages``。"""
    cfg = adapter.cfg
    pages = qq_official_text_pages(text)
    keyboard = cfg.keyboard_enabled() and should_attach_risk_keyboard(text, prefix)
    receipt = Receipt()
    for index, page in enumerate(pages):
        msg = base_message(adapter, target)
        if msg_type == MSG_TYPE_MARKDOWN:
            msg.msg_type = MSG_TYPE_MARKDOWN
            msg.markdown = MessageMarkdown(content=page)
        else:
            msg.msg_type = MSG_TYPE_TEXT
            msg.content = page
        if index == 0 and keyboard:
            msg.keyboard = risk_keyboard(cfg.app_id, prefix)
        try:
            resp = await adapter.client.send_message(ctx, target, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 保留已成功页
            wrapped = DeliveryError(f"page {index + 1}/{len(pages)}: {exc}")
            raise DeliveryError(str(wrapped), receipt.mark_partial_failure(wrapped)) from exc
        if (resp.id or "").strip() != "":
            receipt.platform_message_ids.append(resp.id.strip())
    return receipt


async def send_media(
    adapter: Any, ctx: Any, target: SendTarget, out: Output, file_type: int
) -> Receipt:
    """上传媒体再发一条媒体消息。对应 Go ``(*Adapter).sendMedia``。"""
    if target.proactive and not adapter.cfg.allow_proactive_messages():
        raise InvalidError("qqofficial proactive messages are disabled")
    source = prepare_source(out.source.url, out.source.path, out.source.data)
    uploaded = await adapter.client.upload_file(ctx, target, file_type, source)
    msg = base_message(adapter, target)
    msg.msg_type = MSG_TYPE_MEDIA
    msg.media = MessageMedia(file_info=uploaded.file_info)
    resp = await adapter.client.send_message(ctx, target, msg)
    return receipt_with_message_id(resp.id)


async def send_ark(adapter: Any, ctx: Any, target: SendTarget, ark: MessageArk) -> Receipt:
    """发 Ark 卡片（需显式开启）。对应 Go ``(*Adapter).sendArk``。"""
    if not adapter.cfg.enable_ark:
        raise InvalidError("qqofficial ark is disabled")
    msg = base_message(adapter, target)
    msg.msg_type = MSG_TYPE_ARK
    msg.ark = ark
    resp = await adapter.client.send_message(ctx, target, msg)
    return receipt_with_message_id(resp.id)


__all__ = [name for name in dir() if not name.startswith("_")]
