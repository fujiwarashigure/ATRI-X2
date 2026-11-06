"""出口代理：沙箱唯一的跨边界网络通道（在代理容器内以脚本方式运行）。

部署形态由控制面决定：租户容器只接一个 ``internal`` Docker 网络（无默认路由、
无外部 DNS），同一网络里只有本代理同时连着 ``bridge``。因此：

* 程序按 ``HTTP_PROXY`` 走代理时，本模块按目标地址做 deny/allow、限速、限并发、
  计流量；
* 程序**不**走代理直连时，在路由层就失败（internal 网络出不去）。

策略要点（修改清单 §6）：

* 禁止回环、私网、链路本地（含云元数据 ``169.254.169.254``）、CGNAT、组播、
  保留段与 IPv6 ULA/链路本地；域名先解析再**按解析出的 IP 连接**，避免 DNS
  rebinding 在解析与连接之间换目标；
* 禁止容器引擎/编排/运维管理端口（见 :data:`DENIED_PORTS`）；
* ``restricted`` 档只允许授权主机（支持 ``*.example.com`` 通配）；
* 每日流量额度、上下行速率、并发连接数都有硬上限，超限返回明确状态码。

本文件**不 import 任何 atri_x 模块**：它会被单独复制进镜像（``/opt/atri/egress.py``）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import sys
import time

#: 即使授权出口也不放行的端口（与 :mod:`atri_x.sandbox.network` 保持一致）。
DENIED_PORTS = frozenset(
    {22, 25, 465, 587, 2375, 2376, 2379, 2380, 6443, 10250, 10255, 5000, 9090}
)

#: 云元数据 / 链路本地等固定禁止地址（IPv4 与 IPv6）。
DENIED_IPS = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure 元数据
        "169.254.170.2",  # ECS task metadata
        "100.100.100.200",  # 阿里云元数据
        "fd00:ec2::254",  # AWS IPv6 元数据
    }
)

_MAX_HEADER = 32 * 1024
_MAX_REQUEST_LINE = 8 * 1024
_BUFFER = 65536


class Denied(Exception):
    """目标被策略拒绝（映射成 403）。"""


class Throttle:
    """简单的令牌桶限速（字节/秒）；``rate<=0`` 表示不限。"""

    def __init__(self, rate_bps: int) -> None:
        self.rate = max(0, int(rate_bps))
        self._allowance = float(self.rate) if self.rate else 0.0
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, size: int) -> None:
        """消耗 ``size`` 字节额度；不足时 await 补足（串行化）。"""
        if not self.rate or size <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._allowance = min(
                float(self.rate), self._allowance + (now - self._updated) * self.rate
            )
            self._updated = now
            if self._allowance < size:
                wait = (size - self._allowance) / self.rate
                await asyncio.sleep(wait)
                self._updated = time.monotonic()
                self._allowance = 0.0
            else:
                self._allowance -= size


class Stats:
    """代理计数与状态文件（挂载在宿主 ``<tenant>/proxy/stats.json``）。"""

    def __init__(self, path: str, tenant: str) -> None:
        self.path = path
        self.tenant = tenant
        self.up = 0
        self.down = 0
        self.requests = 0
        self.denied = 0
        self.limited = 0
        self.last_error = ""
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._lock = asyncio.Lock()
        self._dirty = False
        self._task: asyncio.Task | None = None

    def add(self, *, up: int = 0, down: int = 0, denied: bool = False, limited: bool = False) -> None:
        """累计计数（无锁，事件循环内单线程调用安全）。"""
        if up:
            self.up += up
        if down:
            self.down += down
        if denied:
            self.denied += 1
        if limited:
            self.limited += 1
        self._dirty = True

    async def flush(self) -> None:
        """把状态写盘（原子替换）。"""
        async with self._lock:
            payload = {
                "tenant": self.tenant,
                "up_bytes": self.up,
                "down_bytes": self.down,
                "bytes_total": self.up + self.down,
                "requests": self.requests,
                "denied": self.denied,
                "limited": self.limited,
                "last_error": self.last_error,
                "started_at": self.started_at,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                directory = os.path.dirname(self.path) or "."
                os.makedirs(directory, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                os.replace(tmp, self.path)
                self._dirty = False
            except OSError as exc:  # 状态写不了不影响代理本身
                self.last_error = f"state: {exc}"

    async def flusher(self) -> None:
        """后台周期刷盘。"""
        while True:
            await asyncio.sleep(1.0)
            if self._dirty:
                await self.flush()

    def start(self) -> None:
        """启动周期刷盘任务。"""
        if self._task is None:
            self._task = asyncio.ensure_future(self.flusher())


def _is_public_ip(ip: str) -> tuple[bool, str]:
    """判断一个 IP 是否允许作为出口目标；返回 ``(ok, reason)``。"""
    text = str(ip or "").strip().strip("[]")
    if text in DENIED_IPS:
        return False, "云元数据 / 链路本地地址"
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False, "无法解析的地址"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return False, "回环地址"
    if address.is_link_local:
        return False, "链路本地地址"
    if address.is_private:
        return False, "私网地址"
    if address.is_multicast or address.is_reserved or address.is_unspecified or not address.is_global:
        return False, "非公网/保留/组播地址"
    return True, ""


def _host_allowed(host: str, port: int, allowed: tuple[str, ...]) -> bool:
    """``restricted`` 档主机匹配：``host`` / ``host:port`` / ``*.example.com``。"""
    if not allowed:
        return True
    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return False
    for entry in allowed:
        entry = entry.strip().lower()
        if not entry:
            continue
        entry_host, _, entry_port = entry.partition(":")
        if entry_port and entry_port != str(port):
            continue
        if entry_host.startswith("*."):
            suffix = entry_host[2:]
            if name == suffix or name.endswith("." + suffix):
                return True
        elif name == entry_host:
            return True
    return False


async def check_destination(
    host: str,
    port: int,
    allowed: tuple[str, ...],
    *,
    resolver=None,
) -> list[str]:
    """解析目标并校验：返回可连接的 IP 列表；不合法抛 :class:`Denied`。"""
    name = str(host or "").strip()
    if not name:
        raise Denied("缺少目标主机")
    if not (0 < int(port) < 65536):
        raise Denied("非法端口")
    if int(port) in DENIED_PORTS:
        raise Denied(f"端口 {port} 属于管理/滥用端口，沙箱出口禁止")
    if not _host_allowed(name, int(port), allowed):
        raise Denied(f"目标 {name}:{port} 不在授权出口清单内")
    literal = name.strip("[]")
    try:
        ipaddress.ip_address(literal)
        candidates = [literal]
    except ValueError:
        if literal in DENIED_IPS:
            raise Denied("云元数据 / 链路本地地址")
        loop = asyncio.get_running_loop()
        resolve = resolver or loop.getaddrinfo
        try:
            infos = await resolve(literal, int(port), type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as exc:
            raise Denied(f"DNS 解析失败：{exc}") from exc
        candidates = []
        for info in infos:
            address = info[4][0]
            if address not in candidates:
                candidates.append(address)
        if not candidates:
            raise Denied("DNS 解析结果为空")
    for address in candidates:
        ok, reason = _is_public_ip(address)
        if not ok:
            raise Denied(f"目标解析到禁止地址（{reason}）：{address}")
    return candidates


async def _connect(candidates: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """依次尝试解析出的 IP 建连（连的是 IP，不是域名）。"""
    last: Exception | None = None
    for address in candidates:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address, int(port)), timeout=15
            )
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc
    raise Denied(f"连接目标失败：{last}")


class ProxyServer:
    """最小 HTTP 代理（CONNECT + 绝对 URI 转发），只做转发与限流，不做内容缓存。"""

    def __init__(self, config: dict) -> None:
        self.port = int(config["port"])
        self.allowed = tuple(config.get("allowed_hosts") or ())
        self.daily_limit = int(config.get("daily_bytes") or 0)
        self.max_conns = max(1, int(config.get("max_connections") or 64))
        self.stats = Stats(str(config["state_path"]), str(config.get("tenant") or ""))
        self.down_throttle = Throttle(int(config.get("download_bps") or 0))
        self.up_throttle = Throttle(int(config.get("upload_bps") or 0))
        self._sem = asyncio.Semaphore(self.max_conns)

    async def run(self) -> None:
        """启动监听（常驻）。"""
        self.stats.start()
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        await self.stats.flush()
        async with server:
            await server.serve_forever()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理一个客户端连接。"""
        if self._sem.locked():
            await self._reject(writer, 429, "Too Many Connections")
            return
        async with self._sem:
            try:
                await self._serve(reader, writer)
            except Denied as exc:
                self.stats.add(denied=True)
                self.stats.last_error = str(exc)[:200]
                await self._reject(writer, 403, str(exc))
            except (OSError, asyncio.TimeoutError) as exc:
                self.stats.last_error = f"io: {exc}"[:200]
            except asyncio.CancelledError:
                raise
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except (OSError, asyncio.TimeoutError):
                    pass
                await self.stats.flush()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        if len(head) > _MAX_HEADER:
            raise Denied("请求头过大")
        lines = head.decode("latin-1").split("\r\n")
        request_line = lines[0] if lines else ""
        if len(request_line) > _MAX_REQUEST_LINE:
            raise Denied("请求行过长")
        parts = request_line.split(" ")
        if len(parts) < 2:
            raise Denied("无法解析的请求行")
        method, target = parts[0].upper(), parts[1]
        headers = [line for line in lines[1:] if line]
        if method == "CONNECT":
            await self._tunnel(target, headers, reader, writer)
        else:
            await self._forward(method, target, headers, reader, writer)

    async def _tunnel(
        self,
        target: str,
        headers: list[str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port = _split_host_port(target, 443)
        self._check_quota()
        candidates = await check_destination(host, port, self.allowed)
        remote_reader, remote_writer = await _connect(candidates, port)
        self.stats.requests += 1
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pump(reader, writer, remote_reader, remote_writer)
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

    async def _forward(
        self,
        method: str,
        target: str,
        headers: list[str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port, path = _split_absolute_url(target)
        self._check_quota()
        candidates = await check_destination(host, port, self.allowed)
        remote_reader, remote_writer = await _connect(candidates, port)
        self.stats.requests += 1
        try:
            filtered = [
                line
                for line in headers
                if not line.lower().startswith(("proxy-connection:", "proxy-authorization:"))
            ]
            payload = [f"{method} {path} HTTP/1.1", *filtered]
            remote_writer.write(("\r\n".join(payload) + "\r\n\r\n").encode("latin-1"))
            await remote_writer.drain()
            await self._pump(reader, writer, remote_reader, remote_writer, first=True)
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

    async def _pump(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
        *,
        first: bool = False,
    ) -> None:
        """双向转发并计流量、限速、查额度。"""

        async def upstream() -> None:
            while True:
                chunk = await client_reader.read(_BUFFER)
                if not chunk:
                    break
                self._check_quota()
                await self.up_throttle.consume(len(chunk))
                self.stats.add(up=len(chunk))
                remote_writer.write(chunk)
                await remote_writer.drain()

        async def downstream() -> None:
            while True:
                chunk = await remote_reader.read(_BUFFER)
                if not chunk:
                    break
                self._check_quota()
                await self.down_throttle.consume(len(chunk))
                self.stats.add(down=len(chunk))
                client_writer.write(chunk)
                await client_writer.drain()

        tasks = [asyncio.ensure_future(upstream()), asyncio.ensure_future(downstream())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (OSError, asyncio.CancelledError)):
                    raise exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _check_quota(self) -> None:
        if self.daily_limit <= 0:
            return
        if self.stats.up + self.stats.down >= self.daily_limit:
            self.stats.add(limited=True)
            raise Denied("今日联网流量额度已用尽（重置沙箱不会清零流量账本）")

    async def _reject(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        try:
            body = message.encode("utf-8", "replace")
            head = (
                f"HTTP/1.1 {status} Sandbox Denied\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(head + body)
            await writer.drain()
        except (OSError, asyncio.TimeoutError):
            pass


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    text = str(target or "").strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise Denied("IPv6 目标格式错误")
        host = text[1:end]
        rest = text[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if ":" in text:
        host, _, raw_port = text.rpartition(":")
        if not host:
            raise Denied("目标格式错误")
        try:
            return host, int(raw_port)
        except ValueError as exc:
            raise Denied("目标端口不是数字") from exc
    return text, default_port


def _split_absolute_url(target: str) -> tuple[str, int, str]:
    text = str(target or "").strip()
    scheme, sep, rest = text.partition("://")
    if not sep or not scheme:
        raise Denied("只代理绝对 URI 请求（普通 HTTP 代理语义）")
    if scheme.lower() != "http":
        raise Denied("明文代理只接受 http://；https 请用 CONNECT")
    slash = rest.find("/")
    authority = rest if slash < 0 else rest[:slash]
    path = "/" if slash < 0 else rest[slash:]
    host, port = _split_host_port(authority, 80)
    return host, port, path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or 0) or default
    except ValueError:
        return default


def config_from_env() -> dict:
    """从 ``ATRIX_EGRESS_*`` 环境变量读配置（容器启动时注入）。"""
    hosts = [
        item.strip()
        for item in (os.environ.get("ATRIX_EGRESS_ALLOW_HOSTS", "") or "").split(",")
        if item.strip()
    ]
    return {
        "port": _env_int("ATRIX_EGRESS_PORT", 3128),
        "state_path": os.environ.get("ATRIX_EGRESS_STATE", "/state/stats.json"),
        "tenant": os.environ.get("ATRIX_EGRESS_TENANT", ""),
        "daily_bytes": _env_int("ATRIX_EGRESS_DAILY_BYTES", 0),
        "download_bps": _env_int("ATRIX_EGRESS_DOWN_BPS", 0),
        "upload_bps": _env_int("ATRIX_EGRESS_UP_BPS", 0),
        "max_connections": _env_int("ATRIX_EGRESS_MAX_CONNS", 64),
        "allowed_hosts": hosts,
    }


def main(argv: list[str] | None = None) -> int:
    """容器入口：启动代理并常驻。"""
    del argv
    config = config_from_env()
    try:
        asyncio.run(ProxyServer(config).run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - 容器里只保留一行可读错误
        sys.stderr.write(f"atri-x egress failed: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
