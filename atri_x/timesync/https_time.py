"""HTTPS 时间 API 适配器（JSON 契约 + TLS + 新鲜度/大小校验）。

对应 Go：Go 侧没有网络时间校验。本模块只支持**管理员配置的 HTTPS JSON 时间接口**，
响应契约固定为::

    {"unixtime": 1772327553, "iso8601": "2026-03-01T01:12:33Z", "accuracy_ms": 50}

硬性边界：

* **必须 HTTPS**，证书与主机名校验**永不关闭**（复用 :class:`atri_x.net.httpclient`,
  它默认 ``verify_tls=True``；本模块从不传 ``verify_tls=False``）；
* 必须 ``Content-Type: application/json``；响应体超过 ``max_body_bytes`` 拒绝；
* 必须能确认新鲜度：``Cache-Control: no-store``（可由配置放宽），``Age`` 过大拒绝；
* 与本地相差超过 ``max_plausible_skew_seconds`` 的响应视为可疑，拒绝并记审计；
* 普通网页的 ``Date`` 响应头**不作为**权威授时；
* 不发送任何业务数据（无 cookie / token / 会话凭据），日志只写脱敏后的 URL。

传输层可注入（``fetch``），全部测试离线完成。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
from typing import Any, Awaitable, Callable

from atri_x.timesync.model import (
    STATUS_HTTP_ERROR,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_OVERSIZE,
    STATUS_REFUSED,
    STATUS_TIMEOUT,
    STATUS_TLS_ERROR,
    TimeObservation,
)
from atri_x.timesync.policy import redact_url, validate_source

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "MAX_ACCEPTABLE_AGE_SECONDS",
    "FetchResult",
    "FetchTransport",
    "parse_time_payload",
    "query_source",
]

#: 默认响应体上限（时间接口的响应只有几十字节）。
DEFAULT_MAX_BODY_BYTES = 4096
#: ``Age`` 超过它就认为响应可能来自缓存，拒绝。
MAX_ACCEPTABLE_AGE_SECONDS = 300

#: ``async (url, timeout, max_bytes) -> (status, headers, body)``。
FetchTransport = Callable[[str, float, int], Awaitable[tuple[int, dict[str, str], bytes]]]


class FetchResult:
    """一次 HTTPS 取数的结果（值对象）。"""

    __slots__ = ("body", "headers", "status")

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = int(status)
        self.headers = dict(headers or {})
        self.body = body or b""

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return str(value)
        return default


class TimePayload:
    """解析后的时间响应。"""

    __slots__ = ("accuracy_ms", "remote_time")

    def __init__(self, remote_time: dt.datetime, accuracy_ms: float | None) -> None:
        self.remote_time = remote_time
        self.accuracy_ms = accuracy_ms


def parse_time_payload(body: bytes) -> TimePayload:
    """解析并校验 JSON 时间载荷；不合法抛 :class:`ValueError`。"""
    try:
        data = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("time payload must be a JSON object")
    unixtime = data.get("unixtime")
    iso8601 = data.get("iso8601")
    remote_time: dt.datetime | None = None
    if isinstance(unixtime, bool):
        raise ValueError("unixtime must be a number, not a bool")
    if isinstance(unixtime, (int, float)):
        try:
            remote_time = dt.datetime.fromtimestamp(float(unixtime), dt.timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"unixtime out of range: {exc}") from exc
    elif isinstance(iso8601, str) and iso8601.strip():
        text = iso8601.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid iso8601: {exc}") from exc
        if parsed.tzinfo is None:
            raise ValueError("iso8601 must include a timezone offset")
        remote_time = parsed.astimezone(dt.timezone.utc)
    if remote_time is None:
        raise ValueError("time payload requires unixtime or iso8601")
    accuracy = data.get("accuracy_ms")
    accuracy_ms: float | None = None
    if isinstance(accuracy, bool):
        raise ValueError("accuracy_ms must be a number, not a bool")
    if isinstance(accuracy, (int, float)):
        accuracy_ms = max(0.0, float(accuracy))
    return TimePayload(remote_time=remote_time, accuracy_ms=accuracy_ms)


async def query_source(
    source: Any,
    *,
    now_utc: Callable[[], dt.datetime],
    timeout: float,
    fetch: FetchTransport | None = None,
    max_plausible_skew_seconds: int = 604800,
) -> TimeObservation:
    """查询一个 HTTPS 时间来源，返回观测（失败也返回观测，不抛异常）。"""
    source_id = str(getattr(source, "id", "") or "?")
    group = str(getattr(source, "group", "") or "")
    url = str(getattr(source, "url", "") or "").strip()
    started = now_utc()
    try:
        validate_source(source)
    except Exception as exc:  # noqa: BLE001 - 配置问题也要变成观测
        return _failure(source_id, group, started, STATUS_REFUSED, str(exc), "invalid_source")
    max_body = int(getattr(source, "max_body_bytes", 0) or DEFAULT_MAX_BODY_BYTES)
    sender = fetch or _default_fetch
    try:
        status, headers, body = await sender(url, timeout, max_body)
    except asyncio.TimeoutError:
        return _failure(source_id, group, started, STATUS_TIMEOUT, "timed out")
    except ssl.SSLError as exc:
        return _failure(
            source_id, group, started, STATUS_TLS_ERROR, f"TLS error: {exc}", "tls_error"
        )
    except OSError as exc:
        return _failure(source_id, group, started, STATUS_REFUSED, f"network error: {exc}")
    except Exception as exc:  # noqa: BLE001 - 传输层错误一律变成观测
        name = type(exc).__name__
        if "ssl" in name.lower() or "certificate" in str(exc).lower():
            return _failure(source_id, group, started, STATUS_TLS_ERROR, str(exc), "tls_error")
        return _failure(source_id, group, started, STATUS_REFUSED, f"{name}: {exc}")
    finished = now_utc()
    if int(status) != 200:
        return _failure(
            source_id,
            group,
            finished,
            STATUS_HTTP_ERROR,
            f"HTTP {status} from {redact_url(url)}",
            f"http_{int(status)}",
        )
    if len(body or b"") > max_body:
        return _failure(
            source_id,
            group,
            finished,
            STATUS_OVERSIZE,
            "response body %d bytes exceeds %d" % (len(body or b""), max_body),
            "oversize",
        )
    result = FetchResult(int(status), dict(headers or {}), body or b"")
    content_type = result.header("content-type", "").lower()
    if "application/json" not in content_type:
        return _failure(
            source_id,
            group,
            finished,
            STATUS_INVALID,
            f"unexpected content-type {content_type!r}",
            "bad_content_type",
        )
    if bool(getattr(source, "require_no_store", True)):
        cache_control = result.header("cache-control", "").lower()
        if "no-store" not in cache_control:
            return _failure(
                source_id,
                group,
                finished,
                STATUS_INVALID,
                "response is cacheable (missing Cache-Control: no-store)",
                "missing_no_store",
            )
    age_text = result.header("age", "").strip()
    if age_text:
        try:
            age = int(age_text)
        except ValueError:
            age = 0
        if age > MAX_ACCEPTABLE_AGE_SECONDS:
            return _failure(
                source_id,
                group,
                finished,
                STATUS_INVALID,
                f"response is stale (Age={age}s)",
                "stale_response",
            )
    try:
        payload = parse_time_payload(result.body)
    except ValueError as exc:
        return _failure(source_id, group, finished, STATUS_INVALID, str(exc), "bad_payload")
    midpoint = started + (finished - started) / 2
    offset_ms = (payload.remote_time - midpoint).total_seconds() * 1000.0
    if abs(offset_ms) > float(max_plausible_skew_seconds) * 1000.0:
        return _failure(
            source_id,
            group,
            finished,
            STATUS_INVALID,
            "implausible skew %.0fms (limit %ds)"
            % (offset_ms, int(max_plausible_skew_seconds)),
            "implausible_skew",
        )
    round_trip = max(0.0, (finished - started).total_seconds() * 1000.0)
    uncertainty = payload.accuracy_ms
    if uncertainty is None:
        uncertainty = max(round_trip / 2.0, 1.0)
    else:
        uncertainty = max(0.0, uncertainty)
    return TimeObservation(
        provider_id=source_id,
        protocol="https",
        remote_time=payload.remote_time,
        sampled_at=finished,
        round_trip_ms=round_trip,
        estimated_offset_ms=offset_ms,
        uncertainty_ms=uncertainty,
        # TLS 认证只描述**端点身份**，不代表时间正确。
        authenticated=True,
        status=STATUS_OK,
        group=group,
    )


def _failure(
    source_id: str,
    group: str,
    sampled_at: dt.datetime,
    status: str,
    reason: str,
    error_code: str = "",
) -> TimeObservation:
    return TimeObservation(
        provider_id=source_id,
        protocol="https",
        remote_time=None,
        sampled_at=sampled_at,
        status=status,
        error_code=(error_code or reason)[:120],
        group=group,
    )


async def _default_fetch(url: str, timeout: float, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    """默认传输层：走项目统一的 HTTP 客户端（``verify_tls=True`` 是它的默认行为）。"""
    from atri_x.net.httpclient import HTTPClient

    client = HTTPClient(timeout=timeout, verify_tls=True)
    response = await client.get(
        url,
        timeout=timeout,
        follow_redirects=False,
        headers={"Accept": "application/json", "Cache-Control": "no-store"},
    )
    return int(response.status), dict(response.headers or {}), bytes(response.body or b"")
