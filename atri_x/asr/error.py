"""ASR 结构化错误分类。

对应 Go：``internal/asr/error.go``。

``HTTPError.status_code == 0`` 表示请求从未拿到 HTTP 响应（连接失败 / 超时）。
分类只影响两件事：是否重试（``retryable``）与是否写入负缓存
（``deterministic_error``：只有确定性的 4xx 才记，避免坏配置每条录音都打一次
provider）。
"""

from __future__ import annotations

import datetime
import email.utils
import json
from collections.abc import Mapping
from typing import Any

from atri_x.errors import AtriXError

__all__ = [
    "HTTPError",
    "AsrError",
    "bounded_message",
    "deterministic_error",
    "new_http_error",
    "provider_message",
    "retryable",
]

#: 单条错误信息的上限（Go ``boundedMessage`` 的 512）。
MESSAGE_LIMIT = 512
#: ``Retry-After`` 的最大采纳值（秒，Go ``maxRetryAfter = 30 * time.Second``）。
MAX_RETRY_AFTER = 30.0


class AsrError(AtriXError):
    """ASR 的非 HTTP 错误（未配置、录音为空、响应越界等）。

    对应 Go 里 ``errors.New`` / ``fmt.Errorf`` 返回的那些错误。
    """


class HTTPError(AsrError):
    """provider 或传输层失败。对应 Go ``*HTTPError``。

    ``status_code == 0`` 表示请求没有产生 HTTP 响应。
    """

    def __init__(
        self, status_code: int = 0, message: str = "", retry_after: float = 0.0
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        super().__init__(self.render())

    def render(self) -> str:
        """对应 Go ``HTTPError.Error()``。"""
        if self.status_code == 0:
            return "asr: request failed: " + self.message
        if not self.message:
            return f"asr: provider returned HTTP {self.status_code}"
        return f"asr: provider returned HTTP {self.status_code}: {self.message}"

    def __str__(self) -> str:  # pragma: no cover - 与 render 相同，便于日志
        return self.render()


def retryable(err: BaseException | None) -> bool:
    """该错误是否值得重试。对应 Go ``retryable``。"""
    if not isinstance(err, HTTPError):
        return False
    if err.status_code == 0:
        return True
    return err.status_code == 429 or err.status_code >= 500


def deterministic_error(err: BaseException | None) -> bool:
    """该错误是否是确定性失败。对应 Go ``deterministicError``。"""
    if not isinstance(err, HTTPError):
        return False
    if err.status_code in (0, 429):
        return False
    return 400 <= err.status_code < 500


def bounded_message(value: Any) -> str:
    """截断到 512 个字符。对应 Go ``boundedMessage``。"""
    text = str(value or "").strip()
    if len(text) > MESSAGE_LIMIT:
        return text[:MESSAGE_LIMIT]
    return text


def new_http_error(
    status_code: int, headers: Mapping[str, str] | None = None, body: bytes = b""
) -> HTTPError:
    """按响应构造结构化错误。对应 Go ``newHTTPError``。"""
    http_error = HTTPError(status_code=status_code, message=provider_message(body))
    retry_after = _header(headers, "Retry-After")
    if retry_after:
        try:
            seconds = int(retry_after)
        except ValueError:
            seconds = 0
        if seconds > 0:
            http_error.retry_after = float(seconds)
        else:
            when = _parse_http_time(retry_after)
            if when is not None:
                delay = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                if delay > 0:
                    http_error.retry_after = delay
    if not http_error.message:
        http_error.message = bounded_message(body.decode("utf-8", errors="replace"))
    http_error.args = (http_error.render(),)
    return http_error


def provider_message(body: bytes) -> str:
    """从错误响应体里取出 provider 的报错文本。对应 Go ``providerMessage``。"""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    error = error if isinstance(error, Mapping) else {}
    message = _first_non_empty(error.get("message"), payload.get("message"))
    if not message:
        return ""
    code = str(error.get("code") or "").strip()
    if code:
        return code + ": " + bounded_message(message)
    return bounded_message(message)


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _parse_http_time(value: str) -> datetime.datetime | None:
    """解析 HTTP 日期（对应 Go ``http.ParseTime``）。"""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
