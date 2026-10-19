"""``view_image``：把一张图片作为真实图片段交给模型。

对应 Go：``internal/tool/builtin/view_image.go``。

``source`` 与 ``message_id`` 二选一：``source`` 传媒体 ID、HTTP(S) URL 或本地路径；
``message_id`` 传当前聊天的消息 ID 数组（可带 ``#``），按 ``media_index`` 选图。
``VisionRequired`` 让不支持图片输入的模型看不到它。

与 Go 的差异：

* ``Call`` / ``AssessRisk`` / ``PreflightConfirmation`` 是 async；``RiskAssessment``
  的字段名 snake_case（``level`` / ``reasons``）。
* ``sourcePath`` 也必须是 async（``resolveWorkspacePath`` 在 Python 侧是 async），
  并沿用 Go 的零值语义：非本地来源返回 ``None``。
* ``center.Metadata`` / ``ImportURL`` / ``ImportFile`` / ``ReadLimited`` 在 Python 侧
  是 async。
* Go 的 ``if ctx.Err() != nil { return ctx.Err() }`` → 捕获 ``BaseException`` 的位置
  用 :func:`_raise_if_cancelled` 放行 :class:`asyncio.CancelledError`。
* ``media.VisionImageInputLimit`` → :data:`atri_x.media.VisionImageInputLimit`。
* ``llm.MessageSegment{Type: SegmentText, Text: ...}`` → :class:`atri_x.llm.llm.MessageSegment`
  直接构造（``atri_x.llm`` 没有 Go 的 ``llm.Text`` / ``llm.Image`` 构造器）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.delivery import is_http_media_source
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText, ToolSchema
from atri_x.media import (
    IDPrefix,
    ID_PREFIX,
    Manager,
    VisionImageInputLimit,
    history_segments,
    prepare_vision_image_context,
    valid_id,
)
from atri_x.ops.executors import run_cpu
from atri_x.platform import SegmentImage as PlatformSegmentImage
from atri_x.platform import message_context_from
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.tool.builder import Builder
from atri_x.tool.builtin.chat_history import (
    current_chat_history_context,
    parse_chat_history_message_id,
)
from atri_x.tool.builtin.file_tools import is_sensitive_read_file
from atri_x.tool.builtin.get_media import GET_MEDIA_DOWNLOAD_LIMIT
from atri_x.tool.builtin.send_file import local_source_path
from atri_x.tool.capability import vision_available
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskAssessment,
    RiskHigh,
    RiskLow,
)
from atri_x.tool.workspace import PathResolveOptions, resolve_workspace_path

#: 工具名。对应 Go ``ViewImageName``。
VIEW_IMAGE_NAME = "view_image"

_PREFIX = IDPrefix or ID_PREFIX

_DESCRIPTION = (
    "查看图片，把图片本身交给模型，而不是只给地址。source 与 message_id 二选一："
    "source 传媒体 ID、HTTP(S) URL 或本地路径；message_id 传当前聊天的消息 ID 数组"
    "（可带 #），按 media_index 选择该消息里的第几张图片。已经看过的图片不要重复查看。"
)


@dataclass
class ViewImageArgs:
    """对应 Go ``viewImageArgs``。"""

    source: str = ""
    message_id: list[str] = field(default_factory=list)
    media_index: list[list[int]] | None = None


def new_view_image_tool(history: Any, center: Manager | None = None) -> "ViewImageTool":
    """对应 Go ``NewViewImageTool``。"""
    return ViewImageTool(history, center)


def view_image_builder() -> Builder:
    """对应 Go ``viewImageBuilder``。"""
    return (
        Builder(VIEW_IMAGE_NAME)
        .description(_DESCRIPTION)
        .vision_required()
        .risk(RiskLow)
        .tags("chat", "media")
        .string(
            "source",
            "媒体 ID（media:<sha256>）、HTTP(S) URL 或本地路径；本地路径仅超级管理员可用。",
        )
        .string_array("message_id", "当前聊天的平台消息 ID 数组，可带 #。")
    )


class ViewImageTool:
    """对应 Go ``ViewImageTool``。"""

    def __init__(self, history: Any = None, center: Manager | None = None) -> None:
        self.history = history
        self.center = center

    def name(self) -> str:
        return VIEW_IMAGE_NAME

    def info(self) -> Info:
        return view_image_builder().build_info()

    def schema(self) -> ToolSchema:
        """对应 Go ``ViewImageTool.Schema``：手工补 ``minItems`` 与 ``media_index``。"""
        schema = view_image_builder().build_schema()
        properties = schema.function.parameters.get("properties")
        if isinstance(properties, dict):
            message_id = properties.get("message_id")
            if isinstance(message_id, dict):
                message_id["minItems"] = 1
            properties["media_index"] = {
                "type": "array",
                "description": (
                    "与 message_id 逐项对应的图片序号数组，从 1 开始，只能选图片；"
                    "省略时每条消息取首张图片。"
                ),
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 1},
                },
            }
        return schema

    def parse_args(self, req: CallRequest) -> ViewImageArgs:
        """对应 Go ``parseViewImageArgs``。"""
        raw = req.arguments or ""
        args = ViewImageArgs()
        if raw:
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise InvalidError(f"parse view_image arguments: {exc}") from exc
            if not isinstance(data, dict):
                raise InvalidError("parse view_image arguments: expected a JSON object")
            if isinstance(data.get("source"), str):
                args.source = data["source"]
            ids = data.get("message_id")
            if isinstance(ids, list):
                args.message_id = [item for item in ids if isinstance(item, str)]
            media_index = data.get("media_index")
            if isinstance(media_index, list):
                args.media_index = [
                    [
                        item
                        for item in entry
                        if isinstance(item, int) and not isinstance(item, bool)
                    ]
                    for entry in media_index
                    if isinstance(entry, list)
                ]
        args.source = (args.source or "").strip()
        if (args.source != "") == (len(args.message_id) > 0):
            raise InvalidError("source 与 message_id 必须二选一")
        if args.source != "":
            if args.media_index is not None or args.message_id:
                raise InvalidError("source 不能与消息参数同时使用")
            return args
        if args.media_index is not None and len(args.media_index) != len(args.message_id):
            raise InvalidError("media_index 必须与 message_id 逐项对应")
        for position, id in enumerate(args.message_id):
            args.message_id[position] = parse_chat_history_message_id(id)
            if args.message_id[position] == "":
                raise InvalidError("message_id 不能为空")
            if args.media_index is not None:
                if not args.media_index[position]:
                    raise InvalidError("media_index 每项不能为空")
                for index in args.media_index[position]:
                    if index < 1:
                        raise InvalidError("媒体序号从 1 开始")
        return args

    async def source_path(self, ctx: Any, source: str) -> Any:
        """解析本地来源并做权限检查。对应 Go ``ViewImageTool.sourcePath``。

        Go 返回 ``(tool.ResolvedPath, error)``，零值表示"不是本地来源"；Python 侧
        用 ``None`` 表示同一个意思。``resolve_workspace_path`` 是 async，所以本方法
        也只能是 async。
        """
        if source == "" or is_http_media_source(source):
            return None
        if source.startswith(_PREFIX):
            if not valid_id(source):
                raise InvalidError("invalid media ID")
            return None
        actor, ok = actor_from_context(ctx)
        if not ok or getattr(actor, "role", "") != RoleSuperadmin:
            raise InvalidError("查看本地图片需要超级管理员权限")
        path = local_source_path(source)
        return await resolve_workspace_path(ctx, path, PathResolveOptions())

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """对应 Go ``ViewImageTool.AssessRisk``。"""
        args = self.parse_args(req)
        path = await self.source_path(ctx, args.source)
        if path is not None and path.path and is_sensitive_read_file(path.path):
            return RiskAssessment(
                level=RiskHigh, reasons=["读取可能包含凭据的敏感文件，需要用户确认"]
            )
        return RiskAssessment(level=RiskLow)

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """对应 Go ``ViewImageTool.PreflightConfirmation``。"""
        await self.assess_risk(ctx, req)

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = self.parse_args(req)
        if not vision_available(ctx):
            raise InvalidError(f"当前模型不支持图片输入，无法使用 {VIEW_IMAGE_NAME}")
        if self.center is None:
            raise InvalidError("media center is not configured")
        if args.source == "":
            return await self._view_messages(ctx, args)
        path = await self.source_path(ctx, args.source)
        try:
            if args.source.startswith(_PREFIX):
                item = await self.center.metadata(ctx, args.source)
            elif is_http_media_source(args.source):
                item = await self.center.import_url(ctx, args.source, _media_input())
            else:
                item = await self.center.import_file(
                    ctx, (path.path if path else ""), _media_input()
                )
        except BaseException as exc:  # noqa: BLE001 - 统一转成可回喂文案
            _raise_if_cancelled(exc)
            # Go 在 ctx 已取消时返回 ctx.Err()，这里靠 CancelledError 天然传播。
            raise InvalidError("图片获取失败：来源不可用或超限") from exc
        if item is None:
            raise InvalidError("图片获取失败：来源不可用或超限")
        await self._validate_image(ctx, item)
        return Result(
            segments=[view_image_segment(item)],
            warnings=list(path.warnings) if path is not None else [],
        )

    async def _validate_image(self, ctx: Any, item: Any) -> None:
        """确认存下来的对象确实可解码。对应 Go ``validateImage``。"""
        if item is None:
            raise InvalidError("图片获取失败：来源不可用或超限")
        if not _is_image_mime(getattr(item, "mime_type", "")):
            raise InvalidError("媒体不是有效或支持的图片")
        try:
            data, meta = await self.center.read_limited(
                ctx, item.id, VisionImageInputLimit
            )
        except BaseException as exc:  # noqa: BLE001 - 统一转成可回喂文案
            _raise_if_cancelled(exc)
            raise InvalidError("图片读取失败：媒体不存在、过大或已过期") from exc
        mime_type = getattr(item, "mime_type", "")
        if meta is not None and getattr(meta, "mime_type", ""):
            mime_type = meta.mime_type
        try:
            # 解码校验是纯 CPU（大图可达十几秒）：放进 atrix-cpu 工作线程，
            # 别冻住事件循环，也别和阻塞 IO 抢同一个池。
            await run_cpu(
                prepare_vision_image_context, ctx, data, mime_type, 0, 0
            )
        except BaseException as exc:  # noqa: BLE001
            _raise_if_cancelled(exc)
            raise InvalidError("媒体不是有效或支持的图片") from exc

    async def _view_messages(self, ctx: Any, args: ViewImageArgs) -> Result:
        """按消息 ID 取图。对应 Go ``ViewImageTool.viewMessages``。"""
        chat, err_text = current_chat_history_context(ctx)
        if err_text != "":
            return Result(content=err_text)
        if self.history is None:
            raise InvalidError("chat history storage is not configured")
        msg, _ok = message_context_from(ctx)
        attempts = 0
        segments: list[MessageSegment] = []
        for position, id in enumerate(args.message_id):
            try:
                row = self.history.get_by_platform_message(
                    ctx, chat.platform, chat.scope_id, id
                )
            except NotFoundError:
                segments.append(_text_segment(f"[#{id}] 当前聊天没有该消息"))
                continue
            all_segments = history_segments(row)
            ids = await self.center.history_ids(ctx, row)
            indexes = None if args.media_index is None else args.media_index[position]
            if indexes is None:
                first = first_image_index(all_segments)
                indexes = [first] if first > 0 else None
            if not indexes:
                segments.append(_text_segment(f"[#{id}] 没有图片"))
                continue
            for index in indexes:
                if index > len(all_segments) or all_segments[index - 1].type != PlatformSegmentImage:
                    available = image_indexes(all_segments)
                    if not available:
                        segments.append(_text_segment(f"[#{id}] 没有图片"))
                    else:
                        segments.append(
                            _text_segment(
                                f"[#{id}] 序号 {index} 不是图片；可选图片序号：{available}"
                            )
                        )
                    continue
                label = f"[#{id}] {index}."
                if ids.get(index, "") == "" and attempts >= GET_MEDIA_DOWNLOAD_LIMIT:
                    segments.append(
                        _text_segment(
                            label
                            + f" 达到本次 {GET_MEDIA_DOWNLOAD_LIMIT} 个下载尝试上限"
                        )
                    )
                    continue
                if ids.get(index, "") == "":
                    attempts += 1
                try:
                    item = await self.center.get_history_media(
                        ctx, row, index, getattr(msg, "media_resolver", None)
                    )
                except BaseException as exc:  # noqa: BLE001 - 传输错误可能带凭据 URL
                    _raise_if_cancelled(exc)
                    segments.append(_text_segment(label + " 图片获取失败"))
                    continue
                try:
                    await self._validate_image(ctx, item)
                except InvalidError:
                    segments.append(
                        _text_segment(label + " 媒体不是有效或支持的图片")
                    )
                    continue
                segments.append(_text_segment(label))
                segments.append(view_image_segment(item))
        return Result(segments=segments)


def first_image_index(segments: list[Any]) -> int:
    """对应 Go ``firstImageIndex``。"""
    for position, segment in enumerate(segments):
        if segment.type == PlatformSegmentImage:
            return position + 1
    return 0


def image_indexes(segments: list[Any]) -> list[int]:
    """对应 Go ``imageIndexes``。"""
    return [
        position + 1
        for position, segment in enumerate(segments)
        if segment.type == PlatformSegmentImage
    ]


def view_image_segment(item: Any) -> MessageSegment:
    """对应 Go ``viewImageSegment``。"""
    return MessageSegment(
        type=SegmentImage,
        media=getattr(item, "id", ""),
        name=getattr(item, "name", ""),
        mime_type=getattr(item, "mime_type", ""),
    )


def _text_segment(text: str) -> MessageSegment:
    return MessageSegment(type=SegmentText, text=text)


def _is_image_mime(mime_type: str) -> bool:
    return (mime_type or "").strip().lower().startswith("image/")


def _raise_if_cancelled(exc: BaseException) -> None:
    """对应 Go 的 ``if ctx.Err() != nil { return ctx.Err() }``。

    Python 的取消是 :class:`asyncio.CancelledError`，它继承自 ``BaseException``；
    宽口径 ``except BaseException`` 会把它吞掉，所以这里显式放行。
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc


def _media_input() -> Any:
    from atri_x.media import Input

    return Input()


__all__ = [name for name in dir() if not name.startswith("_")]
