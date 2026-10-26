"""``atri_x.timesync.https_time`` 的单元测试：JSON 契约、新鲜度、大小、TLS 不降级。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import unittest
from unittest import mock

from atri_x.config.time_sections import TimeSourceConfig
from atri_x.timesync import https_time
from atri_x.timesync.model import (
    STATUS_HTTP_ERROR,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_OVERSIZE,
    STATUS_TIMEOUT,
    STATUS_TLS_ERROR,
)

_NOW = dt.datetime(2026, 3, 1, 1, 12, 33, tzinfo=dt.timezone.utc)
_URL = "https://time.example/v1/now"

_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
}


def _source(**kwargs) -> TimeSourceConfig:
    return TimeSourceConfig(
        id="api", protocol="https", url=_URL, enabled=True, **kwargs
    )


def _payload(**overrides) -> bytes:
    data = {"unixtime": int(_NOW.timestamp()), "iso8601": _NOW.isoformat(), "accuracy_ms": 50}
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


class PayloadParseTest(unittest.TestCase):
    def test_parse_unixtime(self) -> None:
        parsed = https_time.parse_time_payload(_payload())
        self.assertEqual(parsed.remote_time, _NOW)
        self.assertEqual(parsed.accuracy_ms, 50.0)

    def test_parse_iso8601_only(self) -> None:
        body = json.dumps({"iso8601": "2026-03-01T01:12:33Z"}).encode()
        parsed = https_time.parse_time_payload(body)
        self.assertEqual(parsed.remote_time, _NOW)
        self.assertIsNone(parsed.accuracy_ms)

    def test_rejects_bad_payloads(self) -> None:
        for body in (
            b"not json",
            b"[]",
            b"{}",
            b'{"unixtime": true}',
            b'{"iso8601": "2026-03-01T01:12:33"}',
        ):
            with self.assertRaises(ValueError, msg=body):
                https_time.parse_time_payload(body)


class QueryTest(unittest.IsolatedAsyncioTestCase):
    async def _query(self, response, *, source=None, timeout=1.0):
        async def fetch(url: str, timeout_s: float, max_bytes: int):
            if isinstance(response, Exception):
                raise response
            status, headers, body = response
            return status, headers, body

        return await https_time.query_source(
            source or _source(),
            now_utc=lambda: _NOW,
            timeout=timeout,
            fetch=fetch,
        )

    async def test_success(self) -> None:
        observation = await self._query((200, dict(_HEADERS), _payload()))
        self.assertEqual(observation.status, STATUS_OK)
        self.assertTrue(observation.authenticated)  # 端点身份经 TLS 认证
        self.assertIsNotNone(observation.estimated_offset_ms)
        self.assertLess(abs(observation.estimated_offset_ms or 0), 1.0)

    async def test_non_200(self) -> None:
        observation = await self._query((503, dict(_HEADERS), b""))
        self.assertEqual(observation.status, STATUS_HTTP_ERROR)

    async def test_timeout(self) -> None:
        observation = await self._query(asyncio.TimeoutError())
        self.assertEqual(observation.status, STATUS_TIMEOUT)

    async def test_tls_error_reported_not_bypassed(self) -> None:
        observation = await self._query(ssl.SSLError("certificate verify failed"))
        self.assertEqual(observation.status, STATUS_TLS_ERROR)
        self.assertEqual(observation.error_code, "tls_error")
        self.assertFalse(observation.ok)

    async def test_requires_no_store(self) -> None:
        headers = {"Content-Type": "application/json"}  # 缺少 no-store
        observation = await self._query((200, headers, _payload()), source=_source())
        self.assertEqual(observation.status, STATUS_INVALID)
        self.assertEqual(observation.error_code, "missing_no_store")

    async def test_no_store_can_be_disabled(self) -> None:
        headers = {"Content-Type": "application/json"}
        source = _source(require_no_store=False)
        observation = await self._query((200, headers, _payload()), source=source)
        self.assertEqual(observation.status, STATUS_OK)

    async def test_content_type_required(self) -> None:
        headers = dict(_HEADERS)
        headers["Content-Type"] = "text/html"
        observation = await self._query((200, headers, _payload()))
        self.assertEqual(observation.status, STATUS_INVALID)
        self.assertEqual(observation.error_code, "bad_content_type")

    async def test_oversize_rejected(self) -> None:
        source = _source(max_body_bytes=256)
        body = _payload() + b" " * 300
        observation = await self._query((200, dict(_HEADERS), body), source=source)
        self.assertEqual(observation.status, STATUS_OVERSIZE)

    async def test_stale_age_rejected(self) -> None:
        headers = dict(_HEADERS)
        headers["Age"] = str(https_time.MAX_ACCEPTABLE_AGE_SECONDS + 1)
        observation = await self._query((200, headers, _payload()))
        self.assertEqual(observation.status, STATUS_INVALID)
        self.assertEqual(observation.error_code, "stale_response")

    async def test_implausible_skew_rejected(self) -> None:
        far = _NOW + dt.timedelta(days=30)
        body = json.dumps({"unixtime": int(far.timestamp())}).encode()
        observation = await self._query((200, dict(_HEADERS), body))
        self.assertEqual(observation.status, STATUS_INVALID)
        self.assertEqual(observation.error_code, "implausible_skew")


class DefaultFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_fetch_never_disables_tls(self) -> None:
        """默认传输层必须用 ``verify_tls=True``；这里钉死"从不传 False"。"""
        captured: dict = {}

        class _FakeResponse:
            status = 200
            headers = dict(_HEADERS)
            body = _payload()

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

            async def get(self, url: str, **kwargs):
                captured["url"] = url
                return _FakeResponse()

        with mock.patch("atri_x.net.httpclient.HTTPClient", _FakeClient):
            status, headers, body = await https_time._default_fetch(_URL, 1.0, 4096)
        self.assertEqual(status, 200)
        self.assertTrue(captured.get("verify_tls"))
        self.assertEqual(captured.get("url"), _URL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
