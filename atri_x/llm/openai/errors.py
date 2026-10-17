"""上游失败响应 → ``APIError`` 的错误分类。

对应 Go：``internal/llm/openai/openai.go`` 的 error handling 一节
（``parseError`` / ``responseSummary`` / ``looksLikeHTML`` / ``looksLikeSSE``）。
从 :mod:`atri_x.llm.openai.openai` 拆出（单文件 ≤ 600 行的移植约定），
Chat Completions 与 Responses 两个适配器共用。

分类表本身在 :func:`atri_x.llm.errors.classify`（对应 Go ``classifyAPIError``），
本模块只负责「把上游响应读成可分类的字段」。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from atri_x.llm.errors import APIError, ErrorCategory, classify, stringify_error_field
from atri_x.net.httpclient import HTTPError

from atri_x.llm.openai.wire import STREAM_PREFIX_BYTES, header_of, media_type_of

__all__ = [
    "HTML_RESPONSE_HINT",
    "APIError",
    "ErrorCategory",
    "classify",
    "looks_like_html",
    "looks_like_sse",
    "media_type_of",
    "parse_error",
    "read_error",
    "response_summary",
    "stringify_error_field",
]

#: 上游返回 HTML（网关页/代理页）时给用户看的提示，与 Go 逐字一致。
HTML_RESPONSE_HINT = "上游返回 HTML 而不是 API 响应，请检查 API Base URL、代理或网关"

_SUMMARY_MAX_BYTES = 256
_REDACT_MARKERS = (";base64,", "base64://", "authorization:", "api_key=", "api-key=")
_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body")


def looks_like_html(prefix: str | bytes) -> bool:
    """对应 Go ``looksLikeHTML``。"""
    text = prefix.decode("utf-8", errors="replace") if isinstance(prefix, bytes) else str(prefix)
    return text.lstrip("\ufeff").strip().lower().startswith(_HTML_MARKERS)


def looks_like_sse(prefix: str | bytes) -> bool:
    """对应 Go ``looksLikeSSE``：只看第一条非空行。"""
    text = prefix.decode("utf-8", errors="replace") if isinstance(prefix, bytes) else str(prefix)
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        return line.startswith(("data:", "event:", "id:", "retry:", ":"))
    return False


def response_summary(prefix: bytes | str) -> str:
    """对应 Go ``responseSummary``：有界、脱敏的响应摘要。"""
    text = prefix.decode("utf-8", errors="replace") if isinstance(prefix, bytes) else str(prefix)
    text = " ".join(text.split())
    lowered = text.lower()
    for marker in _REDACT_MARKERS:
        found = lowered.find(marker)
        if found >= 0:
            text = text[: found + len(marker)] + "[redacted]"
            lowered = text.lower()
    if len(text) > _SUMMARY_MAX_BYTES:
        text = text[: _SUMMARY_MAX_BYTES] + "..."
    return text or "响应内容为空或无法识别"


def parse_error(
    status_code: int, body: bytes | str = b"", headers: Mapping[str, str] | None = None
) -> APIError:
    """把上游失败响应转成 ``APIError``（对应 Go ``parseError``）。

    ``message`` 永远是安全摘要：原始 body 不会被整体携带，因为它可能回显请求
    内容或凭据。支持 ``{"error":{message,type,param,code}}``、纯文本、HTML 网关
    页与空体，四条路径都会把 ``category`` 填好。Go 侧用 ``io.LimitReader(body,
    streamPrefixBytes+1)`` 读，这里对入参做同样的截断。
    """
    raw = body.encode("utf-8", errors="replace") if isinstance(body, str) else bytes(body or b"")
    raw = raw[: STREAM_PREFIX_BYTES + 1]
    text = raw.decode("utf-8", errors="replace")
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None

    if isinstance(parsed, dict):
        envelope = parsed.get("error")
        if isinstance(envelope, dict) and str(envelope.get("message") or "") != "":
            message = str(envelope.get("message"))
            code = stringify_error_field(envelope.get("code"))
            type_name = str(envelope.get("type") or "").strip()
            param = stringify_error_field(envelope.get("param"))
            return APIError(
                response_summary(message),
                status_code=status_code,
                code=code,
                type=type_name,
                param=param,
                category=classify(status_code, code, type_name, param, message),
            )

    if header_of(headers, "content-type").lower().startswith("text/html") or looks_like_html(text):
        return APIError(
            HTML_RESPONSE_HINT,
            status_code=status_code,
            category=classify(status_code, "", "", "", ""),
        )
    summary = response_summary(text)
    # 没有 JSON 信封的纯文本网关，方言仍写在正文里，所以摘要也进分类表。
    return APIError(
        summary, status_code=status_code, category=classify(status_code, "", "", "", summary)
    )


def read_error(cause: BaseException) -> HTTPError:
    """对应 Go ``fmt.Errorf("read stream: %w", err)``。"""
    return HTTPError(f"read stream: {cause}", cause=cause)
