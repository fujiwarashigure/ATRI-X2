"""名称与来源规范化。

对应 Go：``internal/media/manager.go``（``sanitizeInput`` /
``sanitizeMediaMetadata`` / ``SanitizeName`` / ``sanitizeMediaName`` /
``sanitizeSourceURL`` / ``sanitizeSourceFileID``）。

语义（``devdocs/architecture.md`` §媒体中心）：

* 名称只保留**跨平台 basename**；
* 平台文件 ID 只保留**不透明 ID**；
* 来源 URL 不保留 userinfo / query / fragment / Telegram token 路径。

规范化只作用于**落库前**与**对外返回的副本**：旧记录按需清洗返回副本，
不批量回写、不重算媒体 ID。实际下载仍用清洗前的调用参数
（见 :meth:`atri_x.media.manager.Manager.import_url` 对 ``spec`` 的清洗时机）。

与 Go 的一处刻意差异：``sanitize_source_url`` 在校验通过后返回清洗后的原文，
而不是 Go 的 ``parsed.String()``。Go 的 ``String()`` 会按 ``RawPath`` 重新拼装，
Python 的 ``urlunparse`` 在这类输入上会做**不同**的再规范化；校验（无凭据、
无 query/fragment）才是这里真正要保证的不变量。
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any
from urllib.parse import unquote, urlparse

from atri_x.media.types import Input, Source

__all__ = [
    "sanitize_input",
    "sanitize_media_metadata",
    "sanitize_media_name",
    "sanitize_name",
    "sanitize_source_file_id",
    "sanitize_source_url",
]

_FALLBACK_NAME = "file"
#: Go 的 url.PathUnescape 对非法 % 转义返回错误；Python 的 unquote 不会，
#: 所以这里显式检测后再决定是否走解码分支。
_BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def path_base(value: str) -> str:
    """对应 Go ``path.Base``（与 ``posixpath.basename`` 在结尾斜杠上不同）。"""
    if value == "":
        return "."
    value = value.rstrip("/")
    if "/" in value:
        value = value[value.rindex("/") + 1:]
    return value or "/"


def sanitize_name(value: str) -> str:
    """返回可持久化、可对模型展示的媒体名（basename）。对应 Go ``SanitizeName``。"""
    return sanitize_media_name(value)


def sanitize_media_name(value: Any) -> str:
    """对应 Go ``sanitizeMediaName``。"""
    text = (value or "").strip() if isinstance(value, str) else ""
    lowered = text.lower()
    if not text or lowered.startswith("data:") or lowered.startswith("base64:"):
        return _FALLBACK_NAME
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme.lower() in ("http", "https", "file"):
        text = parsed.path
    text = path_base(text.replace("\\", "/"))
    if not _BAD_ESCAPE_RE.search(text):
        text = path_base(unquote(text).replace("\\", "/"))
    text = text.strip()
    if text in ("", ".", "..", "/"):
        return _FALLBACK_NAME
    return text


def sanitize_source_url(value: Any) -> str:
    """对应 Go ``sanitizeSourceURL``：只保留无凭据风险的绝对 HTTP(S) URL。"""
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    if "?" in text.partition("#")[0] or parsed.fragment != "":
        return ""
    for component in parsed.path.split("/"):
        lowered = component.lower()
        if lowered.startswith("bot") and ":" in lowered:
            return ""
    return parsed.geturl()


def sanitize_source_file_id(value: Any) -> str:
    """对应 Go ``sanitizeSourceFileID``：只保留不透明 ID，拒绝 URL / 路径 / 盘符。"""
    text = (value or "").strip() if isinstance(value, str) else ""
    lowered = text.lower()
    if (
        not text
        or lowered.startswith("data:")
        or lowered.startswith("base64:")
        or "://" in text
        or "/" in text
        or "\\" in text
    ):
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() in ("http", "https", "file"):
        return ""
    if len(text) >= 2 and text[1] == ":" and text[0].isascii() and text[0].isalpha():
        return ""
    return text


def sanitize_input(spec: Input) -> Input:
    """对应 Go ``sanitizeInput``：清洗一份导入描述（返回新对象，不改入参）。"""
    return Input(
        name=sanitize_media_name(spec.name),
        mime_type=spec.mime_type,
        source=Source(
            platform=spec.source.platform,
            url=sanitize_source_url(spec.source.url),
            file_id=sanitize_source_file_id(spec.source.file_id),
        ),
    )


def sanitize_media_metadata(item: Any) -> Any:
    """对应 Go ``sanitizeMediaMetadata``：返回清洗后的**副本**（``None`` 透传）。

    使用 :func:`dataclasses.replace`，因此不支持的模型形态会抛 ``TypeError``——
    与 Go 的 ``*storage.Media`` 值拷贝同样是"只对媒体记录成立"的操作。
    """
    if item is None:
        return None
    return dataclasses.replace(
        item,
        name=sanitize_media_name(item.name),
        source_url=sanitize_source_url(item.source_url),
        source_file_id=sanitize_source_file_id(item.source_file_id),
    )
