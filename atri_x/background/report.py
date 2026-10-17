"""后台报告的投递输出构造。对应 Go：``internal/background/report.go``。

把 LLM 报告正文与 ``report_segments`` 转成平台无关的 :class:`~atri_x.delivery.Output`：
文本直出，图片/文件段按"媒体 ID → HTTP(S) URL → 沙盒相对路径"三级解析，路径必须落在
任务工作目录内且是真实文件（目录、缺文件都在这里失败）。

本模块不监听任何状态：解析失败的语义由调用方决定（正常触发直接失败，补发降级为文字）。
"""

from __future__ import annotations

import os
import re

from atri_x.delivery.delivery import Kind, Output, Source, is_http_media_source
from atri_x.llm.llm import MessageSegment, SegmentType
from atri_x.tool.sandbox import (
    SandboxContext,
    background_path_instruction,
    resolve_sandbox_relative_path,
)

__all__ = [
    "BuildReportOutputs",
    "BuildReportSegmentOutput",
    "PathInstruction",
    "build_report_outputs",
    "build_report_segment_output",
    "path_instruction",
]

#: 媒体 ID 判定的**唯一出处**是 :func:`atri_x.media.ValidID`（对应 Go
#: ``internal/media.ValidID``）。曾经的本地正则兜底已删除：两份实现会在
#: ``media`` 包调整 ID 规则时静默分叉。
from atri_x.media import ValidID as valid_media_id  # noqa: E402


def PathInstruction() -> str:  # noqa: N802 - 对齐 Go 导出名，便于交叉检索
    """后台任务路径约定说明。对应 Go ``PathInstruction``。"""
    return background_path_instruction()


path_instruction = PathInstruction


def BuildReportOutputs(  # noqa: N802 - 对齐 Go 导出名
    report: str, segments: list[MessageSegment] | None, sandbox: SandboxContext
) -> list[Output]:
    """把报告正文与报告段拼成输出列表。对应 Go ``BuildReportOutputs``。

    正文为空（或只有空白）时不产生文本输出；任一段构造失败即抛异常，消息前缀为
    ``report segment <i>: ``（与 Go 的 ``fmt.Errorf("report segment %d: %w", i, err)`` 一致）。
    """
    outputs: list[Output] = []
    text = (report or "").strip()
    if text:
        outputs.append(Output(kind=Kind.TEXT, text=text))
    for index, segment in enumerate(segments or []):
        try:
            out = BuildReportSegmentOutput(segment, sandbox)
        except Exception as exc:  # noqa: BLE001 - 统一加段序号前缀后重抛（对应 Go 的 %w）
            raise ValueError(f"report segment {index}: {exc}") from exc
        outputs.append(out)
    return outputs


build_report_outputs = BuildReportOutputs


def BuildReportSegmentOutput(  # noqa: N802 - 对齐 Go 导出名
    segment: MessageSegment, sandbox: SandboxContext
) -> Output:
    """构造单段报告输出。对应 Go ``BuildReportSegmentOutput``。

    只接受 ``image`` / ``file`` 段；``media_id`` 与 ``url`` 同时存在、或媒体 ID 形态不合法
    都视为混合/非法来源。
    """
    if segment.type == SegmentType.IMAGE:
        kind = Kind.IMAGE
    elif segment.type == SegmentType.FILE:
        kind = Kind.FILE
    else:
        raise ValueError(f'unsupported type "{segment.type}"')

    out = Output(kind=kind, name=segment.name, source=Source(mime_type=segment.mime_type))
    media_id = (segment.media or "").strip()
    if media_id:
        if (segment.url or "").strip() or not valid_media_id(media_id):
            raise ValueError("invalid or mixed report media source")
        out.source.media_id = media_id
        return out
    if is_http_media_source(segment.url):
        out.source.url = segment.url.strip()
        return out
    path = resolve_sandbox_relative_path(sandbox, segment.url)
    try:
        os.stat(path)
    except OSError as exc:
        raise ValueError(f"stat: {exc}") from exc
    if os.path.isdir(path):
        raise ValueError("path is a directory")
    out.source.path = path
    return out


build_report_segment_output = BuildReportSegmentOutput
