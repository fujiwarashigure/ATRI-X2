"""Skill 子进程的媒体交接。

对应 Go：``internal/tool/skill/media.go``。

两条入口互不重叠：

* :func:`prepare_go_media` 只改 stdin 副本里的显式 ``media_inputs`` 字段，
  为每个媒体补上相对路径、元数据、小文件 base64 与 ``media_workspace``；
* :func:`prepare_command_media` 按清单 schema 里的自定义 ``type: "media"``
  递归把媒体 ID 换成 skill 目录内的相对路径。

媒体运行时（Go ``tool.MediaRuntime`` / ``MediaCall``）在 Python 侧由另一位
实现者交付 ``atri_x.tool.media_runtime``，本模块**鸭子类型**依赖它：只要求
对象提供 ``export`` / ``workspace`` / ``retain`` 与 ``import_``（或
``import_media``）、``new_call()``。方法名 ``import`` 是 Python 关键字，故
按 PEP 8 取 ``import_``。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from atri_x.llm.llm import (
    MessageSegment,
    SegmentFile,
    SegmentImage,
    SegmentText,
    text_segments,
)
from atri_x.media import Input as MediaInput
from atri_x.tool.skill.process import truncate_output
from atri_x.tool.tool import Result

__all__ = [
    "MAX_INLINE_MEDIA_BYTES",
    "prepare_command_media",
    "prepare_go_media",
    "prepare_media_value",
    "result_from_stdout",
    "result_from_stdout_with_media",
    "schema_contains_media",
    "truncate_output",
]

#: 内联进 stdin 的媒体上限（Go 的 ``1024 * 1024``）。
MAX_INLINE_MEDIA_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# stdin 准备
# ---------------------------------------------------------------------------


def prepare_go_media(ctx: Any, raw: str, call: Any, base: str) -> str:
    """改写 Go skill 的 stdin 副本。对应 Go ``prepareGoMedia``。

    没有 ``media_inputs`` 字段时原样返回 ``raw``（一个字节都不动）。
    """
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "media_inputs" not in payload:
        return raw
    inputs = payload.get("media_inputs")
    if inputs is None:
        inputs = []
    if not isinstance(inputs, list):
        raise ValueError("media_inputs: must be an array")
    prepared: list[dict[str, Any]] = []
    for item in inputs:
        media_id = _media_id_of(item)
        path, metadata = _export(call, ctx, media_id, base)
        size = _meta_attr(metadata, "size", 0)
        entry: dict[str, Any] = {
            "media": media_id,
            "path": _to_slash(path),
            "name": _meta_attr(metadata, "name", ""),
            "mime_type": _meta_attr(metadata, "mime_type", ""),
            "size": size,
        }
        if size <= MAX_INLINE_MEDIA_BYTES:
            with open(os.path.join(base, path), "rb") as handle:
                entry["base64"] = base64.b64encode(handle.read()).decode("ascii")
        prepared.append(entry)
    workspace = _workspace(call, base)
    payload["media_inputs"] = prepared
    payload["media_workspace"] = _to_slash(workspace)
    return _dump(payload)


def prepare_command_media(
    ctx: Any, raw: str, manifest: Any, call: Any, base: str
) -> str:
    """按清单 schema 把媒体 ID 换成相对路径。对应 Go ``prepareCommandMedia``。

    schema 里没有 ``media`` 或参数为空时原样返回。
    """
    parameters = getattr(manifest, "parameters", None) or {}
    if not schema_contains_media(parameters) or (raw or "").strip() == "":
        return raw
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        properties = {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        raise
    if not isinstance(values, dict):
        raise ValueError("skill tool arguments must be a JSON object")
    for name, property_ in properties.items():
        if name not in values or not schema_contains_media(property_):
            continue
        values[name] = prepare_media_value(
            ctx, values[name], property_, call, base, name
        )
    return _dump(values)


def schema_contains_media(value: Any) -> bool:
    """schema 里是否含自定义 ``type: "media"``。对应 Go ``schemaContainsMedia``。"""
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if kind == "media":
        return True
    if kind == "array":
        return schema_contains_media(value.get("items"))
    if kind == "object":
        properties = value.get("properties")
        if isinstance(properties, dict):
            return any(schema_contains_media(item) for item in properties.values())
    return False


def prepare_media_value(
    ctx: Any, raw: Any, schema_value: Any, call: Any, base: str, name: str
) -> Any:
    """递归替换一个参数里的媒体值。对应 Go ``prepareMediaValue``。"""
    if raw is None:
        raise ValueError(f"media argument {name} must not be null")
    schema = schema_value if isinstance(schema_value, dict) else {}
    kind = schema.get("type")
    if kind == "media":
        if not isinstance(raw, str):
            raise ValueError(f"media argument {name} must be a media ID")
        path, _ = _export(call, ctx, raw, base)
        return _to_slash(path)
    if kind == "array":
        if not isinstance(raw, list):
            raise ValueError(f"media argument {name} must be an array")
        return [
            prepare_media_value(
                ctx, item, schema.get("items"), call, base, f"{name}[{index}]"
            )
            for index, item in enumerate(raw)
        ]
    if kind == "object":
        if not isinstance(raw, dict):
            raise ValueError(f"media argument {name} must be an object")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        out = dict(raw)
        for field_name, property_ in properties.items():
            if field_name not in raw or not schema_contains_media(property_):
                continue
            out[field_name] = prepare_media_value(
                ctx, raw[field_name], property_, call, base, f"{name}.{field_name}"
            )
        return out
    return raw


# ---------------------------------------------------------------------------
# stdout 解析
# ---------------------------------------------------------------------------


def result_from_stdout(out: str) -> Result:
    """不带媒体运行时的 stdout 解析。对应 Go ``resultFromStdout``。"""
    return result_from_stdout_with_media(None, out, None, "")


def result_from_stdout_with_media(
    ctx: Any, out: str, call: Any, base: str
) -> Result:
    """解析 skill 的 stdout。

    对应 Go ``resultFromStdoutWithMedia``。stdout 要么是
    ``{"content": ..., "segments": [...]}``，要么被当成纯文本内容；空输出
    返回空结果。``image`` / ``file`` 段必须**恰好**给出 ``media`` 或 ``path``
    之一，``path`` 由运行时的 ``import`` 换成稳定 media ID。
    """
    trimmed = (out or "").strip()
    structured: dict[str, Any] | None = None
    try:
        decoded = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        decoded = None
    if isinstance(decoded, dict):
        segments = decoded.get("segments")
        content = decoded.get("content")
        if isinstance(content, str) or isinstance(segments, list):
            structured = {
                "content": content if isinstance(content, str) else "",
                "segments": segments if isinstance(segments, list) else [],
            }
    if structured is None or (structured["content"] == "" and not structured["segments"]):
        if trimmed == "":
            return Result()
        return Result(content=truncate_output(out))
    result = Result(content=truncate_output(structured["content"]))
    if not structured["segments"]:
        return result
    result.segments = text_segments(result.content)
    for raw_segment in structured["segments"]:
        if not isinstance(raw_segment, dict):
            raise ValueError("skill segment must be an object")
        kind = str(raw_segment.get("type") or "")
        media_id = str(raw_segment.get("media") or "")
        path = str(raw_segment.get("path") or "")
        if kind == SegmentText:
            if media_id != "" or path != "":
                raise ValueError("text segment cannot contain media")
            result.segments.append(
                MessageSegment(type=SegmentText, text=str(raw_segment.get("text") or ""))
            )
            continue
        if kind not in (SegmentImage, SegmentFile):
            raise ValueError(f"unsupported skill segment type {kind!r}")
        if (media_id == "") == (path == ""):
            raise ValueError("media segment requires exactly one of media or path")
        if call is None:
            raise ValueError("skill media runtime is not configured")
        name = str(raw_segment.get("name") or "")
        mime_type = str(raw_segment.get("mime_type") or "")
        resolved = media_id
        if path != "":
            metadata = _import_media(call, ctx, base, path, name, mime_type)
            resolved = _meta_attr(metadata, "id", "")
        metadata = _retain(call, ctx, resolved)
        if name == "":
            name = _meta_attr(metadata, "name", "")
        if mime_type == "":
            mime_type = _meta_attr(metadata, "mime_type", "")
        result.segments.append(
            MessageSegment(
                type=kind,
                text=str(raw_segment.get("text") or ""),
                media=resolved,
                mime_type=mime_type,
                name=name,
            )
        )
    return result


# ---------------------------------------------------------------------------
# 媒体运行时适配（鸭子类型）
# ---------------------------------------------------------------------------


def _media_id_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("media") or item.get("media_id") or "")
    return ""


def _export(call: Any, ctx: Any, media_id: str, base: str) -> tuple[str, Any]:
    export = getattr(call, "export", None)
    if export is None:
        raise ValueError("skill media runtime is not configured")
    return export(ctx, media_id, base)


def _workspace(call: Any, base: str) -> str:
    workspace = getattr(call, "workspace", None)
    if workspace is None:
        raise ValueError("skill media runtime is not configured")
    return workspace(base)


def _import_media(
    call: Any, ctx: Any, base: str, path: str, name: str, mime_type: str
) -> Any:
    """调用运行时的 ``Import``（Python 关键字，故为 ``import_``）。"""
    importer = getattr(call, "import_", None) or getattr(call, "import_media", None)
    if importer is None:
        raise ValueError("skill media runtime does not support import")
    return importer(ctx, base, path, MediaInput(name=name, mime_type=mime_type))


def _retain(call: Any, ctx: Any, media_id: str) -> Any:
    retain = getattr(call, "retain", None)
    if retain is None:
        raise ValueError("skill media runtime is not configured")
    return retain(ctx, media_id)


def _meta_attr(metadata: Any, name: str, default: Any) -> Any:
    """读媒体元数据字段（Go ``storage.Media`` 的 snake_case 字段）。"""
    if metadata is None:
        return default
    value = getattr(metadata, name, None)
    return default if value is None else value


def _to_slash(path: str) -> str:
    """``filepath.ToSlash``。"""
    return (path or "").replace(os.sep, "/")


def _dump(value: Any) -> str:
    """紧凑 JSON；键排序对齐 Go 的 map 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
