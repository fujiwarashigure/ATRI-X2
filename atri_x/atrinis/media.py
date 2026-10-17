"""Atrinis 媒体：入站段物化、报告段导入，以及宿主侧 workspace 导入/导出 API。

对应 Go：``internal/atrinis/media.go``。

三条媒体路径：

* :meth:`MediaMixin.materialize_segments` —— 把入站段（``data:`` 内联、http(s)
  URL、``media:`` 引用）统一物化成稳定媒体 ID，并登记 ``atrinis_event`` 引用；
  物化失败即整体失败（不让后台任务拿着不可用的段往下跑）。
* :meth:`MediaMixin.import_report_segments` —— 报告段导入。优先级与 Go 一致：
  已有媒体 ID → 校验存在；否则 http(s) → 下载导入；否则按 **workspace 相对路径**
  导入。导入后清空 ``url``，只留稳定 ID。
* :meth:`MediaMixin.import_workspace_media` / :meth:`MediaMixin.export_workspace_media`
  —— 宿主 API（不是 Atriena RPC），供运维/工具在某个 atriwisp 的沙盒目录里读写文件。

与 Go 的差异（Python 侧的等价物，不是简化）：

* Go 走 ``tool.MediaRuntime``（用 ``os.OpenRoot`` 做防符号链接逃逸）。
  ``atri_x.tool`` 尚未提供该类，因此本模块用 :func:`_safe_join` 复刻同样的约束：
  相对路径、不含 ``:``、不以 ``/`` 开头、不含 ``..`` 片段，且 ``realpath``
  必须仍落在 base 之内。
* Go 的段列表是值语义（``out[i] = seg`` 不回写调用方）；Python 侧显式
  :meth:`atri_x.llm.MessageSegment.clone`，否则会改到调用方的段对象。
* ``mime.ExtensionsByType`` → :func:`mimetypes.guess_all_extensions`（取第一个）：
  两个实现的候选表不完全相同，内联 ``data:`` 段补出的扩展名可能不同（只影响
  展示用文件名，媒体 ID 由内容哈希决定，不受影响）。
"""

from __future__ import annotations

import mimetypes
import os

from atri_x.atriena import Event, Segment, SegmentKindText, direct_text
from atri_x.atrinis.background import atrinis_sandbox_subdir
from atri_x.atrinis.segments import segments_llm
from atri_x.delivery import (
    Kind as DeliveryKind,
    Output,
    Source,
    is_http_media_source,
    text as delivery_text,
)
from atri_x.errors import InvalidError
from atri_x.llm import MessageSegment, SegmentType
from atri_x.media import IDPrefix, ImportLimits, Input, MediaError, ValidID
from atri_x.storage import MediaReference, new_id

__all__ = [
    "OWNER_TYPE_ATRINIS_EVENT",
    "MediaMixin",
    "media_filename",
]

#: 报告/入站媒体的引用 owner 类型（与 Go 逐字一致）。
OWNER_TYPE_ATRINIS_EVENT = "atrinis_event"


def media_filename(metadata: object) -> str:
    """由媒体行推一个安全文件名。对应 Go ``tool.mediaFilename``。

    ``media:<sha256>`` 的哈希部分 + 扩展名；扩展名只保留纯 ASCII 字母数字，
    否则整个丢掉（防止 ``name`` 里的怪字符跑到文件名里）。
    """
    name = str(getattr(metadata, "name", "") or "")
    identifier = str(getattr(metadata, "id", "") or "")
    extension = os.path.splitext(name)[1]
    if extension:
        body = extension[1:]
        if not body or any(not (ch.isascii() and ch.isalnum()) for ch in body):
            extension = ""
    return identifier.removeprefix(IDPrefix) + extension


def _is_abs_like_go(path: str) -> bool:
    """``path`` 是否像 Go ``filepath.IsAbs`` 那样绝对（Windows 口径）。

    关键差异：Go 在 Windows 上认为 ``/x`` **不是**绝对路径（没有卷名），
    Python 的 :func:`os.path.isabs` 认为是。Atrinis 的 Go 代码正是靠这一点
    把"以 ``/`` 开头"单独列为一条错误分支。
    """
    if len(path) >= 2 and path[0] in "/\\" and path[1] in "/\\":
        return True
    drive, rest = os.path.splitdrive(path)
    if not drive:
        return False
    return bool(rest) and rest[0] in "/\\"


def _is_local(path: str) -> bool:
    """``filepath.IsLocal`` 的近似实现。对应 Go ``filepath.IsLocal``。

    未复刻的两点（Go 在 Windows 上的额外规则）：保留设备名（``CON`` / ``NUL``
    等）与 UNC 细节。Atrinis 可到达的路径都由本模块自己拼出来，不受影响。
    """
    if not path:
        return False
    if _is_abs_like_go(path):
        return False
    normalized = path.replace("\\", "/")
    if normalized == ".." or normalized.startswith("../"):
        return False
    return True


def _validate_under_workspace(path: str, what: str) -> str:
    """校验"必须是 workspace 内相对路径"（report / import 侧口径）。

    与 Go ``tool.MediaCall.Import`` 的判定顺序一致：``IsLocal`` + ``:`` +
    以 ``/`` 开头 → 一条错误；``..`` 片段 → 另一条错误。
    """
    slash = (path or "").replace("\\", "/")
    if not _is_local(path) or ":" in (path or "") or slash.startswith("/"):
        raise InvalidError(f"{what} path must be relative")
    if any(part == ".." for part in slash.split("/")):
        raise InvalidError(f"{what} path must not contain parent traversal")
    return slash


def _safe_join(base: str, relative: str) -> str:
    """把相对路径接到 base 上，并确保 ``realpath`` 不逃出 base。

    对应 Go ``os.OpenRoot(base)`` + ``root.Open(path)`` 的符号链接安全语义。
    """
    base_real = os.path.realpath(base)
    joined = os.path.join(base_real, relative.replace("\\", os.sep).replace("/", os.sep))
    target_real = os.path.realpath(joined)
    if target_real != base_real and not target_real.startswith(base_real + os.sep):
        raise InvalidError(f"path escapes workspace: {relative!r}")
    return target_real


def _extension_for_mime(mime_type: str) -> str:
    """按 MIME 类型猜扩展名。对应 Go ``mime.ExtensionsByType``（取第一个）。"""
    if not mime_type:
        return ""
    extensions = mimetypes.guess_all_extensions(mime_type, strict=False)
    return extensions[0] if extensions else ""


class MediaMixin:
    """媒体物化与 workspace 读写。对应 Go ``media.go`` 里的 ``Service`` 方法。

    混入方需提供 ``media`` / ``cfg`` / ``sandbox_root``（见
    :class:`atri_x.atrinis.service.Service`）。
    """

    # -- 宿主 API（不是 Atriena RPC） ---------------------------------------

    async def import_workspace_media(
        self, ctx: object, atriwisp_name: str, path: str, spec: Input
    ) -> object:
        """把 atriwisp workspace 里的文件导入媒体中心。对应 Go ``ImportWorkspaceMedia``。"""
        from atri_x.atrinis.prepare import ATRIWISP_NAME_PATTERN

        if ATRIWISP_NAME_PATTERN.fullmatch(atriwisp_name or "") is None:
            raise InvalidError("invalid atriwisp name")
        if self.media is None:
            raise InvalidError("media center is not configured")
        base = self.workspace_dir(atriwisp_name)
        relative = _validate_under_workspace(path, "skill media")
        target = _safe_join(base, relative)
        if not os.path.isfile(target):
            raise MediaError("skill media must be a regular file")
        if not spec.name:
            spec.name = os.path.basename(relative)
        return await self.media.import_file(ctx, target, spec)

    async def export_workspace_media(
        self, ctx: object, atriwisp_name: str, id: str, path: str
    ) -> None:
        """把媒体导出成该 atriwisp workspace 内的**新**文件。对应 Go ``ExportWorkspaceMedia``。

        用 ``O_EXCL`` 创建：目标已存在即失败，避免静默覆盖 agent 的产物。
        """
        from atri_x.atrinis.prepare import ATRIWISP_NAME_PATTERN

        if self.media is None:
            raise InvalidError("media center is not configured")
        slash = (path or "").replace("\\", "/")
        if (
            ATRIWISP_NAME_PATTERN.fullmatch(atriwisp_name or "") is None
            or not _is_local(path)
            or ":" in (path or "")
        ):
            raise InvalidError("invalid workspace path")
        if any(part == ".." for part in slash.split("/")):
            raise InvalidError("workspace path contains parent traversal")
        if slash.startswith("/"):
            raise InvalidError("workspace path must be relative")
        workspace = self.workspace_dir(atriwisp_name)
        os.makedirs(workspace, mode=0o700, exist_ok=True)
        target = _safe_join(workspace, slash)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        reader, _ = await self.media.open(ctx, id)
        try:
            handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            _close_quietly(reader)
            raise
        try:
            with os.fdopen(handle, "wb") as writer:
                while True:
                    chunk = reader.read(65536)
                    if not chunk:
                        break
                    writer.write(chunk)
        except BaseException:
            _close_quietly(reader)
            _remove_quietly(target)
            raise
        _close_quietly(reader)
        return None

    # -- 事件媒体 -----------------------------------------------------------

    async def materialize_segments(
        self, ctx: object, segments: list[Segment] | None, event_id: str
    ) -> list[MessageSegment]:
        """物化入站媒体段并登记引用。对应 Go ``materializeSegments``。

        文本段原样保留；媒体段经媒体中心换成稳定 ID，并登记
        ``atrinis_event/<event_id>`` / ``input`` 引用。

        ``out[index]`` 的下标直接取自**原始段列表**的循环变量（与 Go 一致）。
        这只对"段种类全部已知"的输入成立，而 ``prepare_event`` 已经保证了这一点；
        未知 kind 会自然抛 ``IndexError``，与 Go 的越界 panic 同义。
        """
        out = segments_llm(segments)
        if not out:
            return out
        for index, seg in enumerate(segments or []):
            if seg.kind == SegmentKindText:
                continue
            if self.media is None:
                raise InvalidError("atrinis media center is not configured")
            if ValidID(seg.url):
                out[index].media = seg.url
                out[index].url = ""
            if not out[index].name and (seg.url or "").startswith("data:"):
                out[index].name = new_id()
            if (seg.url or "").startswith("data:") and not os.path.splitext(out[index].name)[1]:
                header, _, _ = (seg.url or "").partition(",")
                extension = _extension_for_mime(
                    header.removeprefix("data:").removesuffix(";base64")
                )
                if extension:
                    out[index].name += extension
            resolved = await self.media.materialize_with_limits(
                ctx,
                [out[index]],
                ImportLimits(
                    max_import_bytes=self.cfg.segment.max_file_bytes,
                    download_timeout=float(self.cfg.segment.download_timeout_secs),
                ),
            )
            if len(resolved) != 1 or not resolved[0].media:
                raise InvalidError(f"segment {index} media unavailable")
            out[index] = resolved[0]
            self.media.add_reference(
                MediaReference(
                    media_id=out[index].media,
                    owner_type=OWNER_TYPE_ATRINIS_EVENT,
                    owner_id=event_id,
                    purpose="input",
                )
            )
        return out

    async def direct_media_outputs(
        self, ctx: object, event: Event, event_id: str
    ) -> list[Output]:
        """direct 模式的输出（含媒体）。对应 Go ``directMediaOutputs``。

        没有段时退化成一条纯文本（与 :func:`atri_x.atriena.direct_text` 一致）。
        """
        segments = await self.materialize_segments(ctx, event.request.segments, event_id)
        if not segments:
            return [delivery_text(direct_text(event.request))]
        outputs: list[Output] = []
        for seg in segments:
            if seg.type == SegmentType.TEXT:
                outputs.append(delivery_text(seg.text))
                continue
            kind = DeliveryKind.FILE
            if seg.type == SegmentType.IMAGE:
                kind = DeliveryKind.IMAGE
            outputs.append(
                Output(
                    kind=kind,
                    name=seg.name,
                    source=Source(media_id=seg.media, mime_type=seg.mime_type),
                )
            )
        return outputs

    async def import_report_segments(
        self,
        ctx: object,
        segments: list[MessageSegment] | None,
        event_id: str,
        workspace: str,
    ) -> list[MessageSegment]:
        """导入报告段，只留稳定媒体 ID。对应 Go ``importReportSegments``。

        与 Go 一致：``media`` 与 ``url`` **同时**非空是错误（来源二选一）；
        导入成功后清空 ``url``。返回新列表，不改动调用方的段对象。
        """
        out = [segment.clone() for segment in (segments or [])]
        for index, seg in enumerate(out):
            if seg.type not in (SegmentType.IMAGE, SegmentType.FILE):
                raise InvalidError(f"unsupported report segment type {seg.type!r}")
            if seg.media and seg.url:
                raise InvalidError("report media sources are mutually exclusive")
            if ValidID(seg.url):
                seg.media = seg.url
                seg.url = ""
            if seg.media:
                await self.media.metadata(ctx, seg.media)
            elif is_http_media_source(seg.url):
                # 保留既有 HTTP 报告兼容性，但只落稳定 ID。
                item = await self.media.import_url(
                    ctx, seg.url, Input(name=seg.name, mime_type=seg.mime_type)
                )
                seg.media = str(getattr(item, "id", "") or "")
            else:
                item = await self._import_workspace_file(
                    ctx, workspace, seg.url, Input(name=seg.name, mime_type=seg.mime_type)
                )
                seg.media = str(getattr(item, "id", "") or "")
            self.media.add_reference(
                MediaReference(
                    media_id=seg.media,
                    owner_type=OWNER_TYPE_ATRINIS_EVENT,
                    owner_id=event_id,
                    purpose="report",
                )
            )
            seg.url = ""
            out[index] = seg
        return out

    async def _import_workspace_file(
        self, ctx: object, workspace: str, path: str, spec: Input
    ) -> object:
        """按 workspace 相对路径导入。对应 Go ``tool.MediaCall.Import`` 的被调片段。"""
        if self.media is None:
            raise InvalidError("media center is not configured")
        relative = _validate_under_workspace(path, "skill media")
        target = _safe_join(workspace, relative)
        if not os.path.isfile(target):
            raise MediaError("skill media must be a regular file")
        if not spec.name:
            spec.name = os.path.basename(relative)
        return await self.media.import_file(ctx, target, spec)

    def workspace_dir(self, atriwisp_name: str) -> str:
        """该 atriwisp 的沙盒目录（绝对路径）。"""
        parts = atrinis_sandbox_subdir(atriwisp_name).split("/")
        return os.path.join(self.sandbox_root, *parts)


def _close_quietly(handle: object) -> None:
    close = getattr(handle, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
