"""出站输出 → OneBot 消息段，以及文本分页 / 合并转发节点 / 回执。

对应 Go：``internal/platform/qq-onebot/adapter.go`` 里的
``qqTextPages`` / ``qqForwardNodes`` / ``textOutputs`` / ``receiptWithMessageID`` /
``oneBotMediaReceipt`` / ``outputSegments`` / ``oneBotSourceFile`` / ``localPathFileURI``。

要点（上游语义）：

* 文本分页**没有页码标记**：多页回答以一条合并转发投递，到达时是一张可展开的卡片
  而不是满屏气泡（Telegram 才有 ``（n/N）`` 标记）。
* 媒体来源优先级：``data`` → ``url`` → 本地 ``path``；本地路径按 ``send_file_mode``
  转成 ``base64://`` 或 ``file://`` URI。
* ``at`` 段的目标 QQ 取 ``out.name``，为空时退回 ``out.text``。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Sequence
from urllib.parse import urlunsplit

from atri_x.delivery.delivery import Kind, Output, Receipt, SentMessage, Source
from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.qq_onebot.target import Target, one_bot_target_scope
from atri_x.platform.qq_onebot.types import Segment

#: 单页文本的 rune 上限。对应 Go ``qqTextPageRunes``。
QQ_TEXT_PAGE_RUNES = 3000

#: ``send_file_mode`` 的两个合法取值。对应 Go 常量。
SEND_FILE_MODE_BASE64 = "base64"
SEND_FILE_MODE_FILE_URI = "file_uri"


def qq_text_pages(text: str) -> list[str]:
    """把长文本切成页大小的块（无页码标记）。对应 Go ``qqTextPages``。"""
    if len(text) <= QQ_TEXT_PAGE_RUNES:
        return [text]
    pages: list[str] = []
    for start in range(0, len(text), QQ_TEXT_PAGE_RUNES):
        pages.append(text[start : start + QQ_TEXT_PAGE_RUNES])
    return pages


def qq_forward_nodes(pages: Sequence[str]) -> list[Segment]:
    """把每一页包成一个合并转发节点。对应 Go ``qqForwardNodes``。

    长回答因此以单张可展开卡片到达，而不是许多气泡。
    """
    return [
        Segment(
            type="node",
            data={"content": [Segment(type="text", data={"text": page}).to_json()]},
        )
        for page in pages
    ]


def text_outputs(outputs: Sequence[Output]) -> tuple[str, bool]:
    """全是文本输出时把它们拼成一条。对应 Go ``textOutputs``。"""
    parts: list[str] = []
    for out in outputs:
        if out.kind != Kind.TEXT:
            return "", False
        parts.append(out.text)
    return "".join(parts), True


def receipt_with_message_id(message_id: str) -> Receipt:
    """单个平台消息 ID 的回执。对应 Go ``receiptWithMessageID``。"""
    message_id = (message_id or "").strip()
    if message_id == "":
        return Receipt()
    return Receipt(platform_message_ids=[message_id])


def one_bot_media_receipt(receipt: Receipt, target: Target, outputs: Sequence[Output]) -> Receipt:
    """给媒体输出补一条结构化 ``SentMessage``。对应 Go ``oneBotMediaReceipt``。"""
    if len(receipt.platform_message_ids) != 1:
        return receipt
    indexes = [
        index
        for index, out in enumerate(outputs)
        if out.kind in (Kind.IMAGE, Kind.FILE, Kind.RECORD)
    ]
    if not indexes:
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
            platform="qqonebot",
            scope_id=one_bot_target_scope(target),
            output_indexes=indexes,
        )
    )
    return out_receipt


def output_segments(send_file_mode: str, outputs: Sequence[Output]) -> list[Segment]:
    """输出列表 → OneBot 段列表。对应 Go ``outputSegments``。"""
    segments: list[Segment] = []
    for out in outputs:
        if out.kind == Kind.TEXT:
            segments.append(Segment(type="text", data={"text": out.text}))
        elif out.kind == Kind.REPLY:
            reply_id = (out.reply_to_platform_message_id or "").strip()
            if reply_id == "":
                raise InvalidError("reply target message id is empty")
            segments.append(Segment(type="reply", data={"id": reply_id}))
            segments.append(Segment(type="text", data={"text": out.text}))
        elif out.kind == Kind.EMOTICON:
            emoticon_id = (out.emoticon_id or "").strip()
            if emoticon_id == "":
                raise InvalidError("emoticon id is empty")
            segments.append(Segment(type="face", data={"id": emoticon_id}))
        elif out.kind == Kind.IMAGE:
            file = one_bot_source_file(out.source, "image", send_file_mode)
            segments.append(Segment(type="image", data={"file": file}))
        elif out.kind == Kind.FILE:
            file = one_bot_source_file(out.source, "file", send_file_mode)
            data: dict[str, Any] = {"file": file}
            name = (out.name or "").strip()
            if name != "":
                data["name"] = name
            segments.append(Segment(type="file", data=data))
        elif out.kind == Kind.RECORD:
            file = one_bot_source_file(out.source, "record", send_file_mode)
            segments.append(Segment(type="record", data={"file": file}))
        elif out.kind == Kind.AT:
            qq = (out.name or "").strip()
            if qq == "":
                qq = (out.text or "").strip()
            if qq == "":
                raise InvalidError("at target is empty")
            segments.append(Segment(type="at", data={"qq": qq}))
        else:
            raise InvalidError(f"unsupported output kind {out.kind!r}")
    return segments


def one_bot_source_file(source: Source, label: str, send_file_mode: str) -> str:
    """媒体来源 → OneBot ``file`` 字段。对应 Go ``oneBotSourceFile``。"""
    if source.data:
        return "base64://" + base64.b64encode(source.data).decode("ascii")
    source_url = (source.url or "").strip()
    if source_url != "":
        return source_url
    path = (source.path or "").strip()
    if path == "":
        raise InvalidError(f"{label} path is empty")
    if send_file_mode == SEND_FILE_MODE_BASE64:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise AtriXError(f"read {label} path {path!r}: {exc}") from exc
        return "base64://" + base64.b64encode(data).decode("ascii")
    if send_file_mode == SEND_FILE_MODE_FILE_URI:
        return local_path_file_uri(path, label)
    raise InvalidError(f"unsupported qqonebot send_file_mode {send_file_mode!r}")


def local_path_file_uri(path: str, label: str) -> str:
    """本地路径 → ``file://`` URI。对应 Go ``localPathFileURI``。

    UNC 路径（``//host/share/...``）保留主机名；Windows 盘符路径补前导 ``/``
    （``file:///C:/...``），与 Go 的 ``url.URL`` 输出一致。
    """
    try:
        absolute = os.path.abspath(path)
    except (OSError, ValueError) as exc:
        raise AtriXError(f"resolve {label} path {path!r}: {exc}") from exc
    slash_path = absolute.replace("\\", "/")
    if slash_path.startswith("//"):
        trimmed = slash_path[2:]
        host, separator, rest = trimmed.partition("/")
        if separator:
            return urlunsplit(("file", host, "/" + rest, "", ""))
    drive, _tail = os.path.splitdrive(absolute)
    if drive != "" and not slash_path.startswith("/"):
        slash_path = "/" + slash_path
    return urlunsplit(("file", "", slash_path, "", ""))


__all__ = [name for name in dir() if not name.startswith("_")]
