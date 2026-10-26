"""``atri_x.timesync.sntp`` / ``policy`` 的单元测试：报文校验、偏移计算、出站策略。

全部离线：SNTP 通过注入的假传输层完成，不监听端口、不联网。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import unittest

from atri_x.config.time_sections import TimeSourceConfig
from atri_x.errors import TimeSourceRejectedError
from atri_x.timesync import sntp
from atri_x.timesync.model import STATUS_INVALID, STATUS_OK, STATUS_TIMEOUT, STATUS_UNSYNCHRONIZED
from atri_x.timesync.policy import (
    ADDRESS_PRIVATE,
    ADDRESS_PUBLIC,
    classify_address,
    redact_url,
    validate_source,
)

_NOW = dt.datetime(2026, 3, 1, 1, 12, 33, tzinfo=dt.timezone.utc)


def _response(transmit: bytes, *, originate: bytes, leap: int = 0, mode: int = 4,
               version: int = 4, stratum: int = 1) -> bytes:
    packet = bytearray(48)
    packet[0] = (leap << 6) | (version << 3) | mode
    packet[1] = stratum
    packet[24:32] = originate
    packet[32:40] = sntp.encode_timestamp(_NOW - dt.timedelta(milliseconds=1))
    packet[40:48] = transmit
    return bytes(packet)


class TimestampTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        raw = sntp.encode_timestamp(_NOW)
        self.assertEqual(len(raw), 8)
        decoded = sntp.decode_timestamp(raw, reference=_NOW)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertLess(abs((decoded - _NOW).total_seconds()), 1e-5)

    def test_zero_timestamp_is_none(self) -> None:
        self.assertIsNone(sntp.decode_timestamp(b"\x00" * 8))

    def test_naive_rejected(self) -> None:
        with self.assertRaises(Exception):
            sntp.encode_timestamp(dt.datetime(2026, 1, 1))


class RequestTest(unittest.TestCase):
    def test_request_layout(self) -> None:
        originate = sntp.encode_timestamp(_NOW)
        packet = sntp.build_request(originate)
        self.assertEqual(len(packet), sntp.NTP_PACKET_SIZE)
        self.assertEqual(packet[0] & 0x07, 3)  # mode = client
        self.assertEqual((packet[0] >> 3) & 0x07, 4)  # version 4
        self.assertEqual(packet[40:48], originate)


class ParseResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.originate = sntp.encode_timestamp(_NOW)
        self.transmit = sntp.encode_timestamp(_NOW + dt.timedelta(milliseconds=5))

    def test_valid(self) -> None:
        parsed = sntp.parse_response(
            _response(self.transmit, originate=self.originate), self.originate
        )
        self.assertEqual(parsed.stratum, 1)
        self.assertIsNotNone(parsed.transmit_time)

    def test_wrong_length(self) -> None:
        with self.assertRaises(sntp.SntpError) as ctx:
            sntp.parse_response(b"\x00" * 47, self.originate)
        self.assertEqual(ctx.exception.status, STATUS_INVALID)

    def test_leap_alarm_is_unsynchronized(self) -> None:
        with self.assertRaises(sntp.SntpError) as ctx:
            sntp.parse_response(
                _response(self.transmit, originate=self.originate, leap=3), self.originate
            )
        self.assertEqual(ctx.exception.status, STATUS_UNSYNCHRONIZED)

    def test_bad_mode(self) -> None:
        with self.assertRaises(sntp.SntpError):
            sntp.parse_response(
                _response(self.transmit, originate=self.originate, mode=3), self.originate
            )

    def test_bad_version(self) -> None:
        with self.assertRaises(sntp.SntpError):
            sntp.parse_response(
                _response(self.transmit, originate=self.originate, version=1), self.originate
            )

    def test_kiss_o_death(self) -> None:
        with self.assertRaises(sntp.SntpError) as ctx:
            sntp.parse_response(
                _response(self.transmit, originate=self.originate, stratum=0), self.originate
            )
        self.assertEqual(ctx.exception.status, STATUS_UNSYNCHRONIZED)

    def test_stratum_out_of_range(self) -> None:
        with self.assertRaises(sntp.SntpError):
            sntp.parse_response(
                _response(self.transmit, originate=self.originate, stratum=16), self.originate
            )

    def test_originate_must_be_echoed(self) -> None:
        other = sntp.encode_timestamp(_NOW + dt.timedelta(seconds=1))
        with self.assertRaises(sntp.SntpError):
            sntp.parse_response(_response(self.transmit, originate=other), self.originate)

    def test_zero_transmit_rejected(self) -> None:
        with self.assertRaises(sntp.SntpError):
            sntp.parse_response(
                _response(b"\x00" * 8, originate=self.originate), self.originate
            )


class QuerySourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_offsets(self) -> None:
        source = TimeSourceConfig(id="local-ntp", protocol="sntp", host="127.0.0.1", enabled=True)
        remote = _NOW + dt.timedelta(milliseconds=250)  # 本地慢 250ms

        async def transport(host: str, port: int, payload: bytes, timeout: float) -> bytes:
            originate = payload[40:48]
            return _response(sntp.encode_timestamp(remote), originate=originate)

        observation = await sntp.query_source(
            source, now_utc=lambda: _NOW, timeout=1.0, transport=transport
        )
        self.assertEqual(observation.status, STATUS_OK)
        self.assertEqual(observation.provider_id, "local-ntp")
        self.assertFalse(observation.authenticated)
        assert observation.estimated_offset_ms is not None
        self.assertAlmostEqual(observation.estimated_offset_ms, 250.0, delta=1.0)
        self.assertIsNotNone(observation.uncertainty_ms)
        self.assertGreater(observation.uncertainty_ms or 0, 0)

    async def test_timeout_becomes_observation(self) -> None:
        source = TimeSourceConfig(id="t", protocol="sntp", host="127.0.0.1", enabled=True)

        async def transport(*args, **kwargs):
            raise asyncio.TimeoutError()

        observation = await sntp.query_source(
            source, now_utc=lambda: _NOW, timeout=0.1, transport=transport
        )
        self.assertEqual(observation.status, STATUS_TIMEOUT)
        self.assertFalse(observation.ok)

    async def test_negative_round_trip_rejected(self) -> None:
        source = TimeSourceConfig(id="t", protocol="sntp", host="127.0.0.1", enabled=True)
        calls = {"n": 0}

        def fake_now() -> dt.datetime:
            calls["n"] += 1
            return _NOW if calls["n"] == 1 else _NOW - dt.timedelta(seconds=1)

        async def transport(host, port, payload, timeout):
            return _response(sntp.encode_timestamp(_NOW), originate=payload[40:48])

        observation = await sntp.query_source(
            source, now_utc=fake_now, timeout=0.1, transport=transport
        )
        self.assertFalse(observation.ok)


class PolicyTest(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify_address("8.8.8.8"), ADDRESS_PUBLIC)
        self.assertEqual(classify_address("10.1.2.3"), ADDRESS_PRIVATE)
        self.assertEqual(classify_address("192.168.1.1"), ADDRESS_PRIVATE)
        self.assertEqual(classify_address("127.0.0.1"), "loopback")
        self.assertEqual(classify_address("169.254.1.1"), "link_local")

    def test_https_requires_tls_and_no_credentials(self) -> None:
        with self.assertRaises(Exception):
            validate_source(
                TimeSourceConfig(id="x", protocol="https", url="http://time.example/x")
            )
        with self.assertRaises(Exception):
            validate_source(
                TimeSourceConfig(id="x", protocol="https", url="https://u:p@time.example/x")
            )
        validate_source(TimeSourceConfig(id="x", protocol="https", url="https://time.example/x"))

    def test_redact_url_drops_credentials_and_query(self) -> None:
        redacted = redact_url("https://user:pw@time.example/v1/now?token=secret")
        self.assertNotIn("pw", redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn("time.example", redacted)

    def test_private_endpoint_needs_explicit_exception(self) -> None:
        async def run() -> tuple[bool, bool]:
            from atri_x.timesync.policy import OutboundPolicy

            async def resolver(host: str, port: int) -> list[str]:
                return ["192.168.1.10"]

            policy = OutboundPolicy(resolver=resolver)
            blocked_source = TimeSourceConfig(
                id="corp", protocol="sntp", host="ntp.corp", enabled=True
            )
            allowed_source = TimeSourceConfig(
                id="corp", protocol="sntp", host="ntp.corp", enabled=True, allow_private=True
            )
            blocked = False
            try:
                await policy.check_source(blocked_source)
            except TimeSourceRejectedError:
                blocked = True
            allowed = (await policy.check_source(allowed_source)).classification
            return blocked, allowed == ADDRESS_PRIVATE

        blocked, allowed = asyncio.run(run())
        self.assertTrue(blocked)
        self.assertTrue(allowed)

    def test_unresolvable_host_rejected(self) -> None:
        async def run() -> bool:
            from atri_x.timesync.policy import OutboundPolicy

            async def resolver(host: str, port: int) -> list[str]:
                return []

            policy = OutboundPolicy(resolver=resolver)
            try:
                await policy.check_source(
                    TimeSourceConfig(id="x", protocol="sntp", host="nope.invalid", enabled=True)
                )
            except TimeSourceRejectedError:
                return True
            return False

        self.assertTrue(asyncio.run(run()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
