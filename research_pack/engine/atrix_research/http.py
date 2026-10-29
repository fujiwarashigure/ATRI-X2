"""带磁盘缓存与礼貌限速的 HTTP 客户端。

为什么不用 ``requests``：本引擎的核心部分必须"装了 Python 就能跑"，
PIP 依赖只用在导出（docx/pdf）这种增强能力上。检索是最常用的路径，
不能因为 venv 没装好就整条链路不可用。

缓存策略：

* 键是 URL 的 sha256，正文与元信息分两个文件存（二进制安全）；
* 命中条件：缓存文件存在、``fetched_at`` 未超过 TTL、状态码 2xx；
* 写缓存前先写临时文件再 ``os.replace``，并发读不会读到半截；
* ``refresh=True`` 跳过读缓存（仍写入新结果）。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import ResearchSettings, paths
from .errors import SOURCE_UNAVAILABLE, ResearchError
from .store import atomic_write_json, now_iso

__all__ = ["USER_AGENT", "HttpClient", "http"]

USER_AGENT = "ATRI-X-Research/0.1 (+academic literature retrieval; contact: {mailto})"

#: 认为可重试的 HTTP 状态码。
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(ResearchError):
    """带状态码的 HTTP 错误，便于调用方区分 429 与 404。"""

    def __init__(self, message: str, *, status: int = 0, url: str = "", hint: str = "") -> None:
        super().__init__(SOURCE_UNAVAILABLE, message, hint=hint, details={"status": status, "url": url})
        self.status = status
        self.url = url


class HttpClient:
    """进程内单例式的取数器；缓存目录跨运行共享。"""

    def __init__(self, settings: ResearchSettings | None = None) -> None:
        from .config import load_settings

        self.settings = settings or load_settings()
        http_cfg = self.settings.section("http")
        self.timeout = float(http_cfg.get("timeout_seconds", 20) or 20)
        self.min_interval = float(http_cfg.get("min_interval_seconds", 0.35) or 0.0)
        self.ttl_seconds = float(http_cfg.get("cache_ttl_days", 14) or 0) * 86400.0
        self.retries = int(http_cfg.get("retries", 2) or 0)
        self.cache_dir = paths().http_cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self._last_request: dict[str, float] = {}
        #: 按主机覆盖的最小请求间隔（数据源各自的礼貌要求）
        self._host_interval: dict[str, float] = {}
        #: 统计，便于 ``doctor`` 与结果里报告真实网络开销
        self.stats = {"hit": 0, "miss": 0, "fetch": 0, "retry": 0, "error": 0}

    # -- 公共接口 ----------------------------------------------------------

    def set_host_interval(self, host: str, seconds: float) -> None:
        """为单个主机设置更保守的最小间隔（arXiv 要求 3 秒一次）。"""
        if host and seconds > 0:
            self._host_interval[host] = float(seconds)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        accept: str = "application/json",
        refresh: bool = False,
    ) -> bytes:
        """取 URL 正文；失败抛 :class:`HttpError`。"""
        cached = None if refresh else self._read_cache(url)
        if cached is not None:
            self.stats["hit"] += 1
            return cached
        self.stats["miss"] += 1
        body = self._fetch(url, headers=headers, accept=accept)
        self._write_cache(url, body)
        return body

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        refresh: bool = False,
    ) -> Any:
        raw = self.get(url, headers=headers, accept="application/json", refresh=refresh)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(
                f"响应不是合法 JSON: {url}: {exc}",
                url=url,
                hint="稍后重试；若持续失败说明该数据源改了接口或返回了 HTML 错误页。",
            ) from None

    def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        accept: str = "application/xml, text/xml, text/plain, */*",
        refresh: bool = False,
    ) -> str:
        raw = self.get(url, headers=headers, accept=accept, refresh=refresh)
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # -- 缓存 --------------------------------------------------------------

    def _cache_paths(self, url: str) -> tuple[str, str]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return (
            os.path.join(self.cache_dir, f"{key}.json"),
            os.path.join(self.cache_dir, f"{key}.bin"),
        )

    def _read_cache(self, url: str) -> bytes | None:
        meta_path, body_path = self._cache_paths(url)
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            if self.ttl_seconds > 0:
                age = time.time() - float(meta.get("fetched_ts", 0))
                if age > self.ttl_seconds:
                    return None
            with open(body_path, "rb") as handle:
                return handle.read()
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, url: str, body: bytes) -> None:
        meta_path, body_path = self._cache_paths(url)
        try:
            tmp = f"{body_path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(body)
            os.replace(tmp, body_path)
            atomic_write_json(
                meta_path,
                {"url": url, "fetched_at": now_iso(), "fetched_ts": time.time(), "bytes": len(body)},
            )
        except OSError:
            # 缓存写失败不影响本次结果；下次重新取即可
            pass

    # -- 取数 --------------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        if not host:
            return
        interval = self._host_interval.get(host, self.min_interval)
        if interval <= 0:
            return
        last = self._last_request.get(host, 0.0)
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(min(wait, 5.0))
        self._last_request[host] = time.time()

    def _fetch(self, url: str, *, headers: dict[str, str] | None, accept: str) -> bytes:
        request_headers = {
            "User-Agent": USER_AGENT.format(mailto=self.settings.mailto or "noreply@example.invalid"),
            "Accept": accept,
            "Accept-Encoding": "identity",
        }
        if headers:
            request_headers.update(headers)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            request = urllib.request.Request(url, headers=request_headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.stats["fetch"] += 1
                    return response.read()
            except urllib.error.HTTPError as exc:
                self.stats["error"] += 1
                last_error = exc
                if exc.code not in _RETRY_STATUS or attempt >= self.retries:
                    hint = ""
                    if exc.code == 429:
                        hint = "数据源限速；等几十秒再试，或减少 --limit 与 --sources。"
                    elif exc.code in (401, 403):
                        hint = "该数据源需要凭据；换用 openalex/crossref/arxiv 这些免密钥源。"
                    elif exc.code == 404:
                        hint = "该标识在数据源不存在；检查 DOI/PMID 是否正确。"
                    raise HttpError(
                        f"HTTP {exc.code}: {url}",
                        status=exc.code,
                        url=url,
                        hint=hint,
                    ) from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                self.stats["error"] += 1
                last_error = exc
                if attempt >= self.retries:
                    raise HttpError(
                        f"网络不可达: {url}: {exc}",
                        url=url,
                        hint="检查本机网络/代理；离线环境下只能使用已有缓存与手工导入的库。",
                    ) from None
            self.stats["retry"] += 1
            time.sleep(min(0.8 * (2**attempt), 6.0))
        raise HttpError(
            f"请求失败: {url}: {last_error}", url=url, hint="稍后重试。"
        )  # pragma: no cover - 循环内已返回/抛出


_HTTP: HttpClient | None = None


def http(settings: ResearchSettings | None = None) -> HttpClient:
    """取进程内共享的 HTTP 客户端。"""
    global _HTTP
    if _HTTP is None or settings is not None:
        _HTTP = HttpClient(settings)
    return _HTTP
