"""受限 SNTP 客户端（只做单播查询，不做服务端/转发/NTS）。

对应 Go：Go 侧没有网络时间校验。协议实现遵循 RFC 4330 的客户端最小集：

* 发送 48 字节请求（LI=0, VN=4, Mode=3），``originate`` 用本次采样的本地时间；
* 只接受 **48 字节**响应，且 ``LI != 3``、``VN ∈ {3,4}``、``Mode == 4``、
  ``stratum ∈ [1,15]``（``0`` 是 kiss-o'-death：未同步/拒绝服务）、``originate`` 回显一致、
  ``receive`` / ``transmit`` 非零；
* 偏移 ``offset = T3 - (T1 + T4)/2``，RTT ``= T4 - T1``；RTT 过大或为负直接丢弃样本；
* ``authenticated`` **恒为 False**——普通 SNTP 没有密码学认证，任何文案都不得说"已认证"。

**只做观测**：本模块不写系统时钟、不写全局时间、不监听端口、不转发任何报文。
传输层可注入（``transport``），全部测试离线完成。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from atri_x.errors import InvalidError
from atri_x.timesync.model import (
    STATUS_INVALID,
    STATUS_OK,
    STATUS_REFUSED,
    STATUS_TIMEOUT,
    STATUS_UNSYNCHRONIZED,
    TimeObservation,
)

__all__ = [
    "MAX_ROUND_TRIP_MS",
    "NTP_PACKET_SIZE",
    "NTP_UNIX_EPOCH_DELTA",
    "NtpResponse",
    "SntpError",
    "build_request",
    "decode_timestamp",
    "encode_timestamp",
    "parse_response",
    "query_source",
]

#: 标准 NTP 报文长度（RFC 4330 §4）。只接受恰好这么长。
NTP_PACKET_SIZE = 48
#: NTP 纪元（1900-01-01）到 UNIX 纪元（1970-01-01）的秒数。
NTP_UNIX_EPOCH_DELTA = 2208988800
#: RTT 超过它就丢弃该样本（局域网授时通常 < 10ms，公网 < 200ms）。
MAX_ROUND_TRIP_MS = 1000.0

_NTP_EPOCH = dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)
#: NTP 时间戳"纪元回绕"周期（2^32 秒 ≈ 136 年）。
_ERA_SECONDS = 1 << 32

#: 传输层契约：``async (host, port, payload, timeout) -> bytes``。
Transport = Callable[[str, int, bytes, float], Awaitable[bytes]]


class SntpError(Exception):
    """SNTP 协议层失败（带观测状态与错误码）。"""

    def __init__(self, status: str, reason: str, *, error_code: str = "") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code or status


@dataclass(frozen=True)
class NtpResponse:
    """解析后的 SNTP 响应。"""

    leap_indicator: int
    version: int
    mode: int
    stratum: int
    originate: bytes
    receive_time: dt.datetime | None
    transmit_time: dt.datetime | None


def encode_timestamp(moment: dt.datetime) -> bytes:
    """把 aware datetime 编码成 64 位 NTP 时间戳（秒 + 小数）。"""
    if moment.tzinfo is None:
        raise InvalidError("SNTP timestamp requires a timezone-aware datetime")
    utc = moment.astimezone(dt.timezone.utc)
    delta = utc - _NTP_EPOCH
    seconds = int(delta.total_seconds())
    fraction = int((delta - dt.timedelta(seconds=seconds)).total_seconds() * (1 << 32))
    if fraction < 0:
        fraction = 0
    if fraction >= (1 << 32):
        fraction = (1 << 32) - 1
    return seconds.to_bytes(4, "big") + fraction.to_bytes(4, "big")


def decode_timestamp(raw: bytes, *, reference: dt.datetime | None = None) -> dt.datetime | None:
    """把 64 位 NTP 时间戳解码成 UTC datetime；全零返回 ``None``。

    ``reference`` 用于消解 2036 年的纪元回绕：取离参考时刻最近的 era。
    """
    if len(raw) != 8:
        raise InvalidError("NTP timestamp must be 8 bytes")
    seconds = int.from_bytes(raw[0:4], "big")
    fraction = int.from_bytes(raw[4:8], "big")
    if seconds == 0 and fraction == 0:
        return None
    unix_seconds = seconds - NTP_UNIX_EPOCH_DELTA + fraction / float(1 << 32)
    anchor = (reference or dt.datetime.now(dt.timezone.utc)).timestamp()
    # 纪元回绕：取与参考时刻最近的 era（±68 年）。
    while unix_seconds < anchor - _ERA_SECONDS / 2:
        unix_seconds += _ERA_SECONDS
    while unix_seconds > anchor + _ERA_SECONDS / 2:
        unix_seconds -= _ERA_SECONDS
    try:
        return dt.datetime.fromtimestamp(unix_seconds, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def build_request(originate: bytes) -> bytes:
    """构造 48 字节 SNTP 请求（LI=0, VN=4, Mode=3）。"""
    if len(originate) != 8:
        raise InvalidError("originate timestamp must be 8 bytes")
    header = bytearray(NTP_PACKET_SIZE)
    header[0] = (0 << 6) | (4 << 3) | 3  # LI=0, VN=4, Mode=3 (client)
    header[40:48] = originate
    return bytes(header)


def parse_response(
    payload: bytes,
    originate: bytes,
    *,
    reference: dt.datetime | None = None,
) -> NtpResponse:
    """解析并校验 SNTP 响应；不合法抛 :class:`SntpError`。"""
    if len(payload) != NTP_PACKET_SIZE:
        raise SntpError(
            STATUS_INVALID,
            "SNTP response must be %d bytes, got %d" % (NTP_PACKET_SIZE, len(payload)),
            error_code="bad_length",
        )
    first = payload[0]
    leap = (first >> 6) & 0x03
    version = (first >> 3) & 0x07
    mode = first & 0x07
    stratum = payload[1]
    if leap == 3:
        raise SntpError(
            STATUS_UNSYNCHRONIZED,
            "server clock is not synchronized (LI=3)",
            error_code="leap_alarm",
        )
    if version not in (3, 4):
        raise SntpError(
            STATUS_INVALID, "unsupported SNTP version %d" % version, error_code="bad_version"
        )
    if mode != 4:
        raise SntpError(
            STATUS_INVALID, "unexpected SNTP mode %d (want 4=server)" % mode, error_code="bad_mode"
        )
    if stratum == 0:
        raise SntpError(
            STATUS_UNSYNCHRONIZED,
            "server rejected the request (stratum=0, kiss-o'-death)",
            error_code="kiss_o_death",
        )
    if stratum > 15:
        raise SntpError(
            STATUS_INVALID, "invalid stratum %d" % stratum, error_code="bad_stratum"
        )
    echoed = payload[24:32]
    if echoed != originate:
        raise SntpError(
            STATUS_INVALID, "originate timestamp was not echoed", error_code="originate_mismatch"
        )
    receive = decode_timestamp(payload[32:40], reference=reference)
    transmit = decode_timestamp(payload[40:48], reference=reference)
    if transmit is None or receive is None:
        raise SntpError(
            STATUS_INVALID, "server returned a zero timestamp", error_code="zero_timestamp"
        )
    return NtpResponse(
        leap_indicator=leap,
        version=version,
        mode=mode,
        stratum=stratum,
        originate=echoed,
        receive_time=receive,
        transmit_time=transmit,
    )


async def query_source(
    source: Any,
    *,
    now_utc: Callable[[], dt.datetime],
    timeout: float,
    transport: Transport | None = None,
    max_round_trip_ms: float = MAX_ROUND_TRIP_MS,
) -> TimeObservation:
    """查询一个 SNTP 来源，返回观测（失败也返回观测，不抛异常）。"""
    source_id = str(getattr(source, "id", "") or "?")
    group = str(getattr(source, "group", "") or "")
    host = str(getattr(source, "host", "") or "").strip()
    port = int(getattr(source, "port", 0) or 0)
    sender = transport or _udp_transport
    started = now_utc()
    originate = encode_timestamp(started)
    try:
        raw = await sender(host, port, build_request(originate), timeout)
    except asyncio.TimeoutError:
        return _failure(source_id, group, started, STATUS_TIMEOUT, "timed out")
    except (SntpError, InvalidError) as exc:
        status = getattr(exc, "status", STATUS_INVALID)
        return _failure(source_id, group, started, status, str(exc))
    except OSError as exc:
        return _failure(source_id, group, started, STATUS_REFUSED, f"transport error: {exc}")
    except Exception as exc:  # noqa: BLE001 - 观测层不抛异常
        return _failure(
            source_id, group, started, STATUS_INVALID, f"{type(exc).__name__}: {exc}"
        )
    finished = now_utc()
    try:
        response = parse_response(raw, originate, reference=started)
    except SntpError as exc:
        return _failure(source_id, group, finished, exc.status, exc.reason, exc.error_code)
    assert response.transmit_time is not None
    round_trip = (finished - started).total_seconds() * 1000.0
    if round_trip < 0:
        return _failure(
            source_id, group, finished, STATUS_INVALID, "negative round trip", "clock_jump"
        )
    if round_trip > max_round_trip_ms:
        return _failure(
            source_id,
            group,
            finished,
            STATUS_INVALID,
            "round trip %.1fms exceeds %.1fms" % (round_trip, max_round_trip_ms),
            "rtt_too_large",
        )
    midpoint = started + (finished - started) / 2
    offset_ms = (response.transmit_time - midpoint).total_seconds() * 1000.0
    uncertainty_ms = max(round_trip / 2.0, 1.0)
    return TimeObservation(
        provider_id=source_id,
        protocol="sntp",
        remote_time=response.transmit_time,
        sampled_at=finished,
        round_trip_ms=round_trip,
        estimated_offset_ms=offset_ms,
        uncertainty_ms=uncertainty_ms,
        authenticated=False,
        status=STATUS_OK,
        group=group,
        stratum=response.stratum,
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
        protocol="sntp",
        remote_time=None,
        sampled_at=sampled_at,
        status=status,
        error_code=error_code or reason[:120],
        group=group,
    )


# ---------------------------------------------------------------------------
# 默认传输层（单播 UDP）
# ---------------------------------------------------------------------------


class _UdpClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.future: asyncio.Future[bytes] = loop.create_future()

    def datagram_received(self, data: bytes, addr: Any) -> None:  # noqa: ARG002
        if not self.future.done():
            self.future.set_result(bytes(data))

    def error_received(self, exc: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(exc)


async def _udp_transport(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    """默认传输层：解析主机名后发一个单播 UDP 请求并等一个响应。

    只听自己发出的那个 socket 的回包（``create_datagram_endpoint`` 的 connected
    socket 语义），不监听任何固定端口。
    """
    if not host:
        raise SntpError(STATUS_REFUSED, "empty SNTP host", error_code="empty_host")
    if not 1 <= int(port) <= 65535:
        raise SntpError(STATUS_REFUSED, "bad SNTP port", error_code="bad_port")
    loop = asyncio.get_running_loop()
    family = socket.AF_INET
    address: Any = (host, int(port))
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        address = (host, int(port))
    else:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, int(port), type=socket.SOCK_DGRAM),
            timeout=timeout,
        )
        chosen = None
        for info in infos or []:
            chosen = info
            break
        if chosen is None:
            raise SntpError(STATUS_REFUSED, f"cannot resolve {host!r}", error_code="dns")
        family, _, _, _, address = chosen
    protocol = _UdpClientProtocol(loop)
    try:
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(
                lambda: protocol, remote_addr=address, family=family
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise
    except OSError as exc:
        raise SntpError(STATUS_REFUSED, f"udp connect failed: {exc}", error_code="udp") from exc
    try:
        transport.sendto(payload)
        return await asyncio.wait_for(protocol.future, timeout=timeout)
    finally:
        transport.close()
