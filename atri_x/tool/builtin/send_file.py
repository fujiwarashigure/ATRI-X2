"""``send_file``：把本地文件 / file URI / HTTP(S) URL / 媒体 ID 作为产物发出。

对应 Go：``internal/tool/builtin/send_file.go``。

与 Go 的差异：

* ``Call`` / ``AssessRisk`` 是 async；``Output`` / ``Source`` / ``Target`` 来自
  :mod:`atri_x.delivery`（PORTING.md §3 的唯一出处）。
* Go 的 ``mime.TypeByExtension`` → 标准库 :mod:`mimetypes`（本仓统一口径见
  :mod:`atri_x.tool.builtin.file_manager`）。
* ``localSourcePath`` 里 ``runtime.GOOS == "windows"`` 的分支在这里恒按当前平台判断
  （``os.name == "nt"``），与 Go 行为一致。
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from atri_x.delivery import (
    KindImage,
    Output,
    Target,
    file_path,
    image_path,
    is_http_media_source,
)
from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.media import IDPrefix, ID_PREFIX, valid_id
from atri_x.platform import message_context_from
# safety-guard: 文件出口（guard 由 agent 挂到工具 ctx 上）
from atri_x.safety import guard_from_context, scope_from_context
from atri_x.tool.builder import Builder
from atri_x.tool.builtin.common import (
    first_non_empty_string,
    safe_file_name,
)
from atri_x.tool.builtin.file_manager import FileManager
from atri_x.tool.sandbox import BackgroundKindCron, sandbox_context_from_context
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskAssessment,
    RiskHigh,
    RiskMedium,
)
from atri_x.tool.workspace import PathResolveOptions, resolve_workspace_path

SEND_FILE_TOOL_NAME = "send_file"

_PREFIX = IDPrefix or ID_PREFIX


@dataclass
class SendFileArgs:
    """对应 Go ``sendFileArgs``（JSON 键名照抄 struct tag）。"""

    source: str = ""
    name: str = ""
    mime_type: str = ""


def new_send_file_tool(files: FileManager | None) -> "SendFileTool":
    """对应 Go ``NewSendFileTool``。"""
    return SendFileTool(files)


def send_file_builder() -> Builder:
    """对应 Go ``sendFileBuilder``。"""
    return (
        Builder(SEND_FILE_TOOL_NAME)
        .description("发送文件。")
        .risk(RiskMedium)
        .superadmin_only()
        .string(
            "source",
            "要发送的文件来源。支持本地路径、file URI、HTTP(S) URL 或 Media Center 媒体 ID。",
        )
        .string("name", "可选，发送时展示的文件名。")
        .string("mime_type", "可选，文件 MIME 类型；不填时按扩展名推断。")
    )


class SendFileTool:
    """对应 Go ``SendFileTool``。"""

    def __init__(self, files: FileManager | None = None) -> None:
        self.files = files

    def name(self) -> str:
        return SEND_FILE_TOOL_NAME

    def info(self) -> Info:
        return send_file_builder().build_info()

    def schema(self) -> ToolSchema:
        return send_file_builder().build_schema()

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """对应 Go ``SendFileTool.AssessRisk``：外部文件需确认。"""
        args = parse_send_file_args(req)
        source = args_source(args)
        if source == "":
            raise InvalidError("source is required")
        if source.startswith(_PREFIX):
            if not valid_id(source):
                raise InvalidError("invalid media ID")
            return RiskAssessment(level=RiskMedium)
        if is_http_media_source(source):
            return RiskAssessment(level=RiskMedium)
        local_source = local_source_path(source)
        resolved = await resolve_workspace_path(ctx, local_source, PathResolveOptions())
        if resolved.was_abs:
            return RiskAssessment(
                level=RiskHigh, reasons=["发送外部文件需要确认"]
            )
        return RiskAssessment(level=RiskMedium)

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = parse_send_file_args(req)
        source = args_source(args)
        if source == "":
            raise InvalidError("source is required")
        sandbox, _ok = sandbox_context_from_context(ctx)
        out, warnings, label = await self.build_output(ctx, args, source)
        # safety-guard: 文件出口按策略拦截（不做文件内容识别）；
        # 图片类 mime 走 image 判定，其余走 file 判定。
        guard = guard_from_context(ctx)
        if guard is not None:
            kind = "image" if out.kind == KindImage else "file"
            decision = guard.allow_media_output(
                kind,
                stage="tool",
                scope=scope_from_context(ctx),
                meta={"tool": SEND_FILE_TOOL_NAME, "name": label},
            )
            if not decision.allowed:
                return Result(
                    content=(decision.text or "（这个文件因内容安全策略被拦截，未发送。）")
                )
        if sandbox is not None and sandbox.background_kind == BackgroundKindCron:
            msg, ok = message_context_from(ctx)
            if ok and (msg.platform or "").strip() != "":
                out.target = Target(platform=msg.platform, superadmins=True)
        return Result(
            content=f"已发送文件：{label}",
            warnings=warnings,
            outputs=[out],
        )

    async def build_output(
        self, ctx: Any, args: SendFileArgs, source: str
    ) -> tuple[Output, list[str], str]:
        """对应 Go ``SendFileTool.buildOutput``。"""
        if source.startswith(_PREFIX):
            if not valid_id(source):
                raise InvalidError("invalid media ID")
            if self.files is None or self.files.media is None:
                raise InvalidError("media center is not configured")
            item = await self.files.media.metadata(ctx, source)
            name = safe_file_name(
                first_non_empty_string([args.name, getattr(item, "name", "")])
            )
            out = output_for_media_type(getattr(item, "mime_type", ""), "")
            out.name = name
            out.source.media_id = item.id
            out.source.mime_type = getattr(item, "mime_type", "")
            return out, [], name
        if is_http_media_source(source):
            url_name = file_name_from_url(source)
            name = safe_file_name(first_non_empty_string([args.name, url_name]))
            mime_type = first_non_empty_string(
                [
                    args.mime_type,
                    mime_type_from_name(url_name),
                    mime_type_from_name(name),
                ]
            )
            out = output_for_media_type(mime_type, "")
            out.source.url = source
            out.source.mime_type = mime_type
            out.name = name
            return out, [], name
        local_source = local_source_path(source)
        resolved = await resolve_workspace_path(ctx, local_source, PathResolveOptions())
        if self.files is None:
            raise InvalidError("file manager is not configured")
        prepared = self.files.prepare(resolved.path, args.name, args.mime_type)
        out = output_for_media_type(prepared.mime_type, prepared.path)
        out.name = prepared.name
        out.source.mime_type = prepared.mime_type
        return out, list(resolved.warnings), prepared.name


def output_for_media_type(mime_type: str, path: str) -> Output:
    """对应 Go ``outputForMediaType``。"""
    if is_image_mime(mime_type):
        return image_path(path)
    return file_path(path)


def is_image_mime(mime_type: str) -> bool:
    """对应 Go ``isImageMIME``。"""
    return (mime_type or "").strip().lower().startswith("image/")


def mime_type_from_name(name: str) -> str:
    """对应 Go ``mimeTypeFromName``。"""
    value = (name or "").strip()
    guessed, _encoding = mimetypes.guess_type(value)
    return guessed or ""


def file_name_from_url(value: str) -> str:
    """对应 Go ``fileNameFromURL``：取 URL path 的 basename。"""
    try:
        parts = urlsplit((value or "").strip())
    except ValueError:
        return ""
    return posixpath.basename(parts.path)


def local_source_path(source: str) -> str:
    """``file://`` URI → 本地路径。对应 Go ``localSourcePath``。"""
    source = (source or "").strip()
    if not source.lower().startswith("file://"):
        return source
    parts = urlsplit(source)
    # Go 的 ``u.Host`` 不含 userinfo，这里按同样的口径取 host。
    host = parts.netloc.rsplit("@", 1)[-1]
    if (
        parts.scheme != "file"
        or host not in ("", "localhost")
        or parts.query != ""
        or parts.fragment != ""
    ):
        raise InvalidError("invalid file URI")
    path = parts.path
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if path == "":
        raise InvalidError("invalid file URI")
    return path.replace("/", os.sep)


def args_source(args: SendFileArgs) -> str:
    """对应 Go ``sendFileArgs.source``。"""
    return (args.source or "").strip()


def parse_send_file_args(req: CallRequest) -> SendFileArgs:
    """对应 Go ``parseSendFileArgs``。"""
    from atri_x.tool.builtin.common import decode_args

    return decode_args(req.arguments or "", SendFileArgs, SEND_FILE_TOOL_NAME)


__all__ = [name for name in dir() if not name.startswith("_")]
