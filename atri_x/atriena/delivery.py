"""Atriena 段/目标的投递层换算。

对应 Go：``internal/atriena/delivery.go``。

本模块只做"协议类型 → 交付层类型"的纯换算：目标落点（``TargetScopeID``）、
按段构造输出（``SegmentsOutputs`` / ``BuildDirectOutputs``）、以及 direct 模式没有
段时用的纯文本正文（``DirectText``）。

两个换算规则值得单独记一笔：

* ``TargetScopeID`` 是**历史/映射的稳定标识**，不是平台 id 本身：私聊前缀
  ``private:``（QQ 官方因 C2C 语义用 ``c2c:``），群 ``group:``；type 为空
  （即"发给超级管理员"）时没有 scope，返回空串。
* 段里的 ``url`` 既可能是 ``media:<sha256>`` 稳定引用，也可能是沙盒相对路径。
  后者由调用方通过 ``paths``（键为 :func:`SegmentKey`）提供物化后的本地路径，
  没有就回落到 ``seg.url`` 本身。

``Target.ToDeliveryTarget`` 在 Go 里定义于本文件，但它是 ``Target`` 的方法；
Python 侧为了保持类型定义唯一出处，把它放在 :mod:`atri_x.atriena.types`。
"""

from __future__ import annotations

from collections.abc import Sequence

from atri_x.atriena.types import (
    Request,
    Segment,
    SegmentKindFile,
    SegmentKindImage,
    SegmentKindText,
    Target,
)
from atri_x.delivery import Output, Source, file_path, image_path, text
from atri_x.media import ValidID

__all__ = [
    "BuildDirectOutputs",
    "DirectText",
    "PathForSegment",
    "SegmentKey",
    "SegmentsOutputs",
    "TargetScopeID",
    "build_direct_outputs",
    "direct_text",
    "path_for_segment",
    "segment_key",
    "segments_outputs",
    "target_scope_id",
]


def TargetScopeID(target: Target | None) -> str:  # noqa: N802 - 对齐 Go 导出名
    """目标的稳定 scope 标识。对应 Go ``TargetScopeID``。

    ``private`` → ``private:<id>``（``qqofficial`` 平台 → ``c2c:<id>``）；
    ``group`` → ``group:<id>``；其余（含缺 id）→ 空串。
    """
    if target is None:
        return ""
    identifier = (target.id or "").strip()
    kind = (target.type or "").strip()
    if kind == "private":
        if not identifier:
            return ""
        if (target.platform or "").strip() == "qqofficial":
            return "c2c:" + identifier
        return "private:" + identifier
    if kind == "group":
        if not identifier:
            return ""
        return "group:" + identifier
    return ""


target_scope_id = TargetScopeID


def SegmentKey(i: int, seg: Segment) -> str:  # noqa: N802 - 对齐 Go 导出名
    """段的位置键。对应 Go ``SegmentKey``（``"<i>:<kind>"``）。"""
    return f"{i}:{seg.kind}"


segment_key = SegmentKey


def PathForSegment(i: int, seg: Segment, paths: dict[str, str] | None) -> str:  # noqa: N802
    """取段的本地路径。对应 Go ``PathForSegment``（``paths`` 命中非空即用，否则回落 url）。"""
    if paths:
        found = paths.get(SegmentKey(i, seg))
        if found:
            return found
    return seg.url


path_for_segment = PathForSegment


def SegmentsOutputs(  # noqa: N802 - 对齐 Go 导出名
    segments: Sequence[Segment] | None, paths: dict[str, str] | None
) -> list[Output]:
    """把协议段转成交付输出。对应 Go ``SegmentsOutputs``。

    文本段直出；图片/文件段先用 :func:`PathForSegment` 取路径，命中
    ``media:<sha256>`` 时改用稳定媒体 ID（并带上 MIME 类型）。
    """
    out: list[Output] = []
    for index, seg in enumerate(segments or []):
        if seg.kind == SegmentKindText:
            out.append(text(seg.text))
            continue
        if seg.kind == SegmentKindImage:
            item = image_path(PathForSegment(index, seg, paths))
        elif seg.kind == SegmentKindFile:
            item = file_path(PathForSegment(index, seg, paths))
        else:
            continue
        if ValidID(seg.url):
            item.source = Source(media_id=seg.url, mime_type=seg.mime_type)
        item.name = seg.name
        out.append(item)
    return out


segments_outputs = SegmentsOutputs


def BuildDirectOutputs(  # noqa: N802 - 对齐 Go 导出名
    req: Request, paths: dict[str, str] | None
) -> list[Output]:
    """direct 模式的输出列表。对应 Go ``BuildDirectOutputs``。

    没有段时退化成一条纯文本（:func:`DirectText`），与 Go 一致。
    """
    if not req.segments:
        return [text(DirectText(req))]
    return SegmentsOutputs(req.segments, paths)


build_direct_outputs = BuildDirectOutputs


def DirectText(req: Request) -> str:  # noqa: N802 - 对齐 Go 导出名
    """direct 模式的正文：标题（非空时）与内容各占一行。对应 Go ``DirectText``。"""
    parts: list[str] = []
    title = (req.title or "").strip()
    if title:
        parts.append(title)
    parts.append((req.content or "").strip())
    return "\n".join(parts)


direct_text = DirectText
