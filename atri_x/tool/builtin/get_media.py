"""``get_media``：按当前聊天的消息 ID 与媒体序号取媒体 ID（不返回内容）。

对应 Go：``internal/tool/builtin/get_media.go``。

与 Go 的差异：

* ``ChatHistoryRepository.GetByPlatformMessage`` → Python 的
  ``get_by_platform_message``；Go 的 ``err == storage.ErrNotFound`` → 捕获
  :class:`atri_x.errors.NotFoundError`（``ErrNotFound`` 是实例，不能拿来 except）。
* ``media.HistorySegments`` / ``Manager.HistoryIDs`` / ``Manager.GetHistoryMedia``
  → :func:`atri_x.media.history_segments` / :meth:`Manager.history_ids` /
  :meth:`Manager.get_history_media`，均为 async 或需 ctx。
* Go 的 ``[]tool.MediaInput`` 无关；这里保持"只返回 media ID"的语义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import ToolSchema
from atri_x.media import IDPrefix, ID_PREFIX, Manager, history_segments
from atri_x.platform import SegmentImage, message_context_from
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.chat_history import (
    current_chat_history_context,
    parse_chat_history_message_id,
)
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

GET_MEDIA_TOOL_NAME = "get_media"

#: 单次调用最多尝试下载的媒体数（失败也计入）。对应 Go ``getMediaDownloadLimit``。
GET_MEDIA_DOWNLOAD_LIMIT = 5

_PREFIX = IDPrefix or ID_PREFIX

_DESCRIPTION = (
    "仅在需要使用聊天记录中的媒体时调用。按当前聊天的平台消息 ID 和媒体序号获取媒体；"
    "已入库直接复用，未入库按需下载。仅返回媒体 ID，不返回媒体内容。"
    "省略 media_index 时获取每条消息第一个媒体；单次最多尝试下载 5 个媒体，失败也计入上限。"
)


@dataclass
class GetMediaArgs:
    """对应 Go ``getMediaArgs``。"""

    message_id: list[str] = field(default_factory=list)
    media_index: list[list[int]] | None = None


def new_get_media_tool(
    history: Any, center: Manager | None = None
) -> "GetMediaTool":
    """对应 Go ``NewGetMediaTool``。"""
    return GetMediaTool(history, center)


def get_media_builder() -> Builder:
    """对应 Go ``getMediaBuilder``。"""
    return (
        Builder(GET_MEDIA_TOOL_NAME)
        .description(_DESCRIPTION)
        .risk(RiskLow)
        .hidden()
        .tags("chat")
        .depends_on(
            "search_chat_history",
            "get_chat_history_around",
            "reply_to_chat_history_message",
        )
        .string_array(
            "message_id",
            "当前聊天的平台消息 ID 数组，接受 #ID 或纯 ID。",
            required(),
        )
    )


class GetMediaTool:
    """对应 Go ``GetMediaTool``。"""

    def __init__(self, history: Any = None, center: Manager | None = None) -> None:
        self.history = history
        self.center = center

    def name(self) -> str:
        return GET_MEDIA_TOOL_NAME

    def info(self) -> Info:
        return get_media_builder().build_info()

    def schema(self) -> ToolSchema:
        """对应 Go ``GetMediaTool.Schema``：手工补 ``media_index``。"""
        schema = get_media_builder().build_schema()
        properties = schema.function.parameters.get("properties")
        if isinstance(properties, dict):
            properties["media_index"] = {
                "type": "array",
                "description": (
                    "与 message_id 一一对应的媒体序号数组。序号从1开始，"
                    "只计算图片/文件等媒体，不计文字；每个内层数组非空。"
                    "省略时每条消息默认 [1]。"
                ),
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 1},
                },
            }
        return schema

    def parse_args(self, req: CallRequest) -> GetMediaArgs:
        """解析并校验入参。对应 Go ``GetMediaTool.Call`` 的前半段。"""
        raw = req.arguments or ""
        args = GetMediaArgs()
        if raw:
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise InvalidError(f"parse get_media arguments: {exc}") from exc
            if not isinstance(data, dict):
                raise InvalidError("parse get_media arguments: expected a JSON object")
            ids = data.get("message_id")
            if isinstance(ids, list):
                args.message_id = [item for item in ids if isinstance(item, str)]
            media_index = data.get("media_index")
            if isinstance(media_index, list):
                args.media_index = [
                    [item for item in entry if isinstance(item, int) and not isinstance(item, bool)]
                    for entry in media_index
                    if isinstance(entry, list)
                ]
        if not args.message_id:
            raise InvalidError("message_id must be a nonempty array")
        if args.media_index is not None and len(args.media_index) != len(args.message_id):
            raise InvalidError("media_index must match message_id length")
        for position, id in enumerate(args.message_id):
            args.message_id[position] = parse_chat_history_message_id(id)
            if args.message_id[position] == "":
                raise InvalidError("message_id must not contain empty IDs")
            if args.media_index is not None:
                if not args.media_index[position]:
                    raise InvalidError("media_index entries must not be empty")
                for index in args.media_index[position]:
                    if index < 1:
                        raise InvalidError("media_index must contain positive integers")
        return args

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = self.parse_args(req)
        chat, err_text = current_chat_history_context(ctx)
        if err_text != "":
            return Result(content=err_text)
        if self.history is None or self.center is None:
            raise InvalidError("history media is not configured")
        msg, _ok = message_context_from(ctx)
        attempts = 0
        results: dict[tuple[str, int], str] = {}
        lines: list[str] = []
        for position, id in enumerate(args.message_id):
            indexes = [1] if args.media_index is None else args.media_index[position]
            try:
                row = self.history.get_by_platform_message(
                    chat.platform, chat.scope_id, id
                )
            except NotFoundError:
                lines.append(f"[#{id}] 当前聊天没有该消息。")
                continue
            segments = history_segments(row)
            ids = await self.center.history_ids(ctx, row)
            for index in indexes:
                key = (id, index)
                if key in results:
                    lines.append(results[key])
                    continue
                prefix = f"[#{id}] {index}. "
                line = ""
                if index > len(segments):
                    line = prefix + "[媒体序号越界]"
                elif ids.get(index, "") == "" and attempts >= GET_MEDIA_DOWNLOAD_LIMIT:
                    line = prefix + (
                        f"[媒体未下载：达到本次 {GET_MEDIA_DOWNLOAD_LIMIT} 个下载尝试上限]"
                    )
                else:
                    if ids.get(index, "") == "":
                        attempts += 1
                    try:
                        item = await self.center.get_history_media(
                            ctx, row, index, getattr(msg, "media_resolver", None)
                        )
                    except BaseException:  # noqa: BLE001 - 传输错误可能带凭据 URL
                        # Transport errors may contain credential URLs or local
                        # paths; never echo them to the LLM.
                        line = prefix + "[媒体获取失败：来源不可用、超限或关联保存失败]"
                    else:
                        line = prefix + get_media_history_label(
                            segments[index - 1].type, item.id
                        )
                        ids[index] = item.id
                results[key] = line
                lines.append(line)
        return Result(content="\n".join(lines))


def get_media_history_label(kind: str, media_id: str) -> str:
    """媒体标签。对应 Go ``historyMediaLabel``。

    注意：与 ``chat_history.history_media_label``（Go ``historyMediaLabel`` in
    ``chat_history.go``）**同名不同义**，因此本包 ``__init__`` 只再导出工具类，
    不导出本函数，避免两个名字在包级撞车。
    """
    label = "文件"
    if kind == SegmentImage:
        label = "图片"
    if media_id == "":
        media_id = "未下载"
    elif _PREFIX and media_id.startswith(_PREFIX):
        media_id = media_id[len(_PREFIX) :]
    return "[" + label + " media:" + media_id + "]"


__all__ = [name for name in dir() if not name.startswith("_")]
