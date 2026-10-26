"""网络时间来源的出站校验：协议白名单、私网例外、大小/超时上限、日志脱敏。

对应 Go：Go 侧没有这层（也没有网络时间校验）。本模块是 Python 侧安全边界：

* **只允许管理员在 ``[[time.sources]]`` 里声明的来源**，不内置任何公共端点；
* HTTPS 一律走 ``verify_tls=True``（永不关闭证书/主机名校验），URL 里的凭据直接拒绝；
* 私网 / 回环 / 链路本地地址必须 ``allow_private = true`` 才放行——这是内网授时
  的显式例外，也让"时间来源"不能变成普通用户的内网探测通道；
* DNS 解析失败、地址类型不可判定都算拒绝（失败关闭，不猜）。

**已知限制**：DNS 预检与真正的连接之间存在 TOCTOU 窗口（解析结果可能变），
首版不实现连接级 pinning；来源只能由管理员配置，因此把这个窗口当作可接受残余风险，
并在 ``docs/configuration.md`` 里写明。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from atri_x.errors import InvalidError, TimeSourceRejectedError

__all__ = [
    "ADDRESS_PRIVATE",
    "ADDRESS_PUBLIC",
    "EndpointCheck",
    "OutboundPolicy",
    "classify_address",
    "redact_url",
    "validate_source",
]

ADDRESS_PUBLIC = "public"
ADDRESS_PRIVATE = "private"

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)
_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
_LINK_LOCAL_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)
_CARRIER_GRADE_NAT = (ipaddress.ip_network("100.64.0.0/10"),)


def classify_address(value: str) -> str:
    """把 IP 文本归类成 ``public`` / ``private`` / ``loopback`` / ``link_local`` / ``other``。"""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        raise TimeSourceRejectedError(f"not an IP address: {value!r}") from None
    if any(address in network for network in _LOOPBACK_NETWORKS):
        return "loopback"
    if any(address in network for network in _LINK_LOCAL_NETWORKS):
        return "link_local"
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return "other"
    if any(address in network for network in _PRIVATE_NETWORKS):
        return ADDRESS_PRIVATE
    if any(address in network for network in _CARRIER_GRADE_NAT):
        return ADDRESS_PRIVATE
    return ADDRESS_PUBLIC


def redact_url(url: str) -> str:
    """去掉 URL 里的用户信息与查询串（写日志/诊断用；绝不带凭据）。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or ""
    return f"{parts.scheme}://{host}{port}{path}"


def validate_source(source: Any) -> None:
    """来源配置的**结构性**校验（不触网、不解析 DNS）。

    配置解码阶段已经校验过一次；这里再查一遍，因为装配层可能被测试/插件直接调用。
    """
    protocol = str(getattr(source, "protocol", "") or "").strip().lower()
    source_id = str(getattr(source, "id", "") or "")
    if protocol == "sntp":
        host = str(getattr(source, "host", "") or "").strip()
        if not host:
            raise InvalidError(f"time source {source_id!r}: sntp requires host")
        port = int(getattr(source, "port", 0) or 0)
        if not 1 <= port <= 65535:
            raise InvalidError(f"time source {source_id!r}: sntp port out of range")
        return
    if protocol == "https":
        url = str(getattr(source, "url", "") or "").strip()
        if not url:
            raise InvalidError(f"time source {source_id!r}: https requires url")
        _split_https_url(url, source_id)
        return
    raise InvalidError(f"time source {source_id!r}: unsupported protocol {protocol!r}")


def _split_https_url(url: str, source_id: str) -> Any:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidError(f"time source {source_id!r}: invalid url ({exc})") from None
    if parts.scheme.lower() != "https":
        raise InvalidError(
            f"time source {source_id!r}: url must use https:// "
            f"(TLS verification is never disabled)"
        )
    if not parts.hostname:
        raise InvalidError(f"time source {source_id!r}: url has no host")
    if parts.username or parts.password:
        raise InvalidError(f"time source {source_id!r}: credentials in url are not allowed")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise InvalidError(f"time source {source_id!r}: url port out of range")
    return parts


@dataclass(frozen=True)
class EndpointCheck:
    """一次出站端点的检查结果。"""

    source_id: str
    host: str
    addresses: tuple[str, ...]
    classification: str
    allow_private: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "host": self.host,
            "addresses": list(self.addresses),
            "classification": self.classification,
            "allow_private": self.allow_private,
        }


class OutboundPolicy:
    """时间来源出站策略（DNS 预检 + 地址分类）。"""

    def __init__(
        self,
        *,
        dns_timeout_seconds: float = 3.0,
        resolver: Any = None,
    ) -> None:
        self.dns_timeout_seconds = max(0.2, float(dns_timeout_seconds))
        #: 可注入的解析器：``async (host, port) -> list[str]``（测试用）。
        self._resolver = resolver

    async def check_source(self, source: Any) -> EndpointCheck:
        """校验一个来源当前是否允许出站；不允许就抛 :class:`TimeSourceRejectedError`。"""
        validate_source(source)
        source_id = str(getattr(source, "id", "") or "?")
        allow_private = bool(getattr(source, "allow_private", False))
        if str(getattr(source, "protocol", "")).lower() == "https":
            url = str(getattr(source, "url", "") or "")
            parts = _split_https_url(url, source_id)
            host = str(parts.hostname or "")
            port = parts.port or 443
        else:
            host = str(getattr(source, "host", "") or "").strip()
            port = int(getattr(source, "port", 0) or 0)
        addresses = await self._resolve(host, port)
        if not addresses:
            raise TimeSourceRejectedError(
                f"time source {source_id!r}: cannot resolve {host!r}"
            )
        classifications = {classify_address(address) for address in addresses}
        public_only = classifications == {ADDRESS_PUBLIC}
        if not public_only and not allow_private:
            raise TimeSourceRejectedError(
                "time source %r resolves to non-public address(es) %s; set "
                "allow_private = true only for a trusted internal time source"
                % (source_id, ", ".join(sorted(addresses)))
            )
        classification = ADDRESS_PUBLIC
        if not public_only:
            classification = sorted(classifications)[0]
        return EndpointCheck(
            source_id=source_id,
            host=host,
            addresses=tuple(sorted(addresses)),
            classification=classification,
            allow_private=allow_private,
        )

    async def _resolve(self, host: str, port: int) -> list[str]:
        """解析主机名（或直接返回 IP 字面量）。"""
        text = (host or "").strip()
        if not text:
            return []
        try:
            literal = ipaddress.ip_address(text)
        except ValueError:
            literal = None
        if literal is not None:
            return [str(literal)]
        if self._resolver is not None:
            result = await self._resolver(text, port)
            return [str(item) for item in (result or [])]
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(text, port, type=socket.SOCK_STREAM),
                timeout=self.dns_timeout_seconds,
            )
        except (asyncio.TimeoutError, OSError):
            return []
        out: list[str] = []
        for info in infos or []:
            address = info[4][0]
            if address and address not in out:
                out.append(str(address))
        return out
