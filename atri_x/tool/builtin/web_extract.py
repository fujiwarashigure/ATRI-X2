"""``web_extract``：网页正文提取（Jina Reader 优先，失败/禁用时直接抓取）。

对应 Go：``internal/tool/builtin/web_extract.go``。

与 Go 的差异（仅形式）：

* ``net/http`` → :class:`atri_x.net.httpclient.HTTPClient`；代理通过带
  ``proxy`` 的 ``HTTPClient`` 表达（Go 是自建 ``http.Transport``）。
* ``sync.Mutex`` → :class:`threading.Lock`（缓存是同步内存结构）。
* HTML 清洗是纯标准库实现，逐函数对齐 Go：``extractSimpleHTMLText`` /
  ``extractTagContent`` / ``removeTagBlocks`` / ``removeHTMLComments`` / ``stripTags``。
* 请求体字段名逐字照抄 Go struct tag，``omitempty`` 逐字段复刻。
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import optional_builtin_env
from atri_x.tool.builtin.web_search import truncate_http_body
from atri_x.tool.capability import ctx_raise_if_cancelled
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

JINA_API_KEY_ENV = "JINA_API_KEY"
WEB_EXTRACT_PROXY_ENV = "WEB_EXTRACT_PROXY"
DEFAULT_EXTRACT_ENDPOINT = "https://r.jina.ai/"
DEFAULT_REMOVE_SELECTOR = "header, .class, #id"
DEFAULT_EXTRACT_CHARS = 8000
MAX_EXTRACT_CHARS = 16000
DEFAULT_EXTRACT_TIMEOUT = 15.0
MAX_EXTRACT_CACHE_ENTRIES = 8


@dataclass
class WebExtractArgs:
    """对应 Go ``webExtractArgs``。"""

    url: str = ""
    offset: int = 0
    max_chars: int = 0
    remove_selector: str = ""
    timeout_ms: int = 0
    proxy: str = ""
    jina: bool | None = None
    force_refresh: bool = False


@dataclass
class JinaResponseData:
    """对应 Go ``jinaResponseData``。"""

    url: str = ""
    title: str = ""
    content: str = ""
    timestamp: str = ""


@dataclass
class JinaResponse:
    """对应 Go ``jinaResponse``。"""

    url: str = ""
    title: str = ""
    content: str = ""
    timestamp: str = ""
    data: JinaResponseData | None = None


@dataclass
class WebExtractData:
    """对应 Go ``webExtractData``（``omitempty`` 字段为 None 时不输出）。"""

    url: str = ""
    title: str = ""
    source: str = ""
    total_chars: int = 0
    offset: int = 0
    returned_chars: int = 0
    next_offset: int | None = None
    truncated: bool = False
    cached: bool = False
    content: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": self.url}
        if self.title:
            payload["title"] = self.title
        payload.update(
            {
                "source": self.source,
                "total_chars": self.total_chars,
                "offset": self.offset,
                "returned_chars": self.returned_chars,
                "truncated": self.truncated,
                "cached": self.cached,
                "content": self.content,
            }
        )
        if self.next_offset is not None:
            payload["next_offset"] = self.next_offset
        return payload


@dataclass
class CachedExtract:
    """对应 Go ``cachedExtract``。"""

    url: str = ""
    title: str = ""
    source: str = ""
    content: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ExtractCache:
    """FIFO 上限 8 条的提取缓存。对应 Go ``extractCache``。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._order: list[str] = []
        self._values: dict[str, CachedExtract] = {}

    def get(self, key: str) -> tuple[CachedExtract, bool]:
        """对应 Go ``extractCache.get``。"""
        with self._mu:
            entry = self._values.get(key)
            return (entry, True) if entry is not None else (CachedExtract(), False)

    def set(self, key: str, entry: CachedExtract) -> None:
        """对应 Go ``extractCache.set``。"""
        with self._mu:
            if key in self._values:
                self._values[key] = entry
                return
            if len(self._order) >= MAX_EXTRACT_CACHE_ENTRIES:
                oldest = self._order.pop(0)
                self._values.pop(oldest, None)
            self._order.append(key)
            self._values[key] = entry


@dataclass
class ExtractProxyConfig:
    """对应 Go ``extractProxyConfig``。"""

    proxy_url: str = ""
    disabled: bool = False
    cache_key: str = ""


class WebExtractTool:
    """对应 Go ``WebExtractTool``。"""

    def __init__(
        self,
        environment: Environment | None = None,
        *,
        client: HTTPClient | None = None,
        endpoint: str = "",
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self.process_env = environment if environment is not None else Environment()
        self.cache = ExtractCache()

    def name(self) -> str:
        return "web_extract"

    def info(self) -> Info:
        return web_extract_builder().build_info()

    def schema(self) -> ToolSchema:
        return web_extract_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``WebExtractTool.Call``。"""
        ctx_raise_if_cancelled(ctx)
        args = parse_web_extract_args(req.arguments)
        target_url = (args.url or "").strip()
        if target_url == "":
            raise InvalidError("url is required")
        selector = (args.remove_selector or "").strip() or DEFAULT_REMOVE_SELECTOR
        proxy = resolve_extract_proxy(self.process_env, args.proxy, ctx)
        cache = self.cache
        use_jina = True if args.jina is None else bool(args.jina)
        api_key = ""
        has_jina_key = False
        if use_jina:
            api_key = _lookup_env(self.process_env, ctx, JINA_API_KEY_ENV)
            has_jina_key = api_key != ""
        source = "jina" if (use_jina and has_jina_key) else "direct"
        key = extract_cache_key(source, target_url, selector, proxy)
        entry = CachedExtract()
        cached = False
        if not args.force_refresh:
            entry, cached = cache.get(key)
        if not cached:
            if use_jina and has_jina_key:
                entry = await self.fetch_jina(
                    ctx, api_key, target_url, selector, args.timeout_ms, proxy
                )
            else:
                entry = await self.fetch_direct(
                    ctx, target_url, args.timeout_ms, proxy
                )
            cache.set(key, entry)
        data = slice_extract(entry, args.offset, args.max_chars, cached)
        return Result(content=format_extract_content(data))

    async def fetch_jina(
        self,
        ctx: Any,
        api_key: str,
        target_url: str,
        selector: str,
        timeout_ms: int,
        proxy: ExtractProxyConfig,
    ) -> CachedExtract:
        """对应 Go ``fetchJina``。"""
        reader_url = jina_reader_url(self.extract_endpoint(), target_url)
        headers = {
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "X-Respond-With": "json",
            "X-Remove-Selector": selector,
            "X-Robots-Txt": "JinaReader",
            "X-With-Generated-Alt": "true",
            "X-With-Images-Summary": "true",
            "X-With-Links-Summary": "true",
        }
        try:
            response = await self.http_client(proxy).request(
                "GET",
                reader_url,
                headers=headers,
                timeout=_timeout_seconds(timeout_ms),
            )
        except HTTPError as exc:
            raise InvalidError(f"Jina request: {exc}") from exc
        if not response.ok:
            raise InvalidError(
                f"Jina HTTP {response.status}: {truncate_http_body(response.body)}"
            )
        try:
            data = _parse_jina_response(response.json())
        except ValueError as exc:
            raise InvalidError(f"parse Jina response: {exc}") from exc
        return normalize_jina_response(data, target_url)

    async def fetch_direct(
        self, ctx: Any, target_url: str, timeout_ms: int, proxy: ExtractProxyConfig
    ) -> CachedExtract:
        """对应 Go ``fetchDirect``。"""
        try:
            response = await self.http_client(proxy).request(
                "GET",
                target_url,
                headers={"User-Agent": "ATRI-X-WebExtract/1.0"},
                timeout=_timeout_seconds(timeout_ms),
            )
        except HTTPError as exc:
            raise InvalidError(f"direct extract request: {exc}") from exc
        if not response.ok:
            raise InvalidError(
                f"direct extract HTTP {response.status}: "
                f"{truncate_http_body(response.body)}"
            )
        title, content = extract_simple_html_text(response.text())
        return CachedExtract(
            url=target_url, title=title, source="direct", content=content
        )

    def http_client(self, proxy: ExtractProxyConfig) -> HTTPClient:
        """对应 Go ``WebExtractTool.httpClient``。"""
        if self.client is not None:
            return self.client
        if proxy.disabled:
            # 显式清空 proxy：HTTPClient 的构造器会回落到 HTTPS_PROXY 环境变量，
            # 直接覆盖属性才能表达 Go 的 transport.Proxy = nil。
            plain = HTTPClient(timeout=DEFAULT_EXTRACT_TIMEOUT)
            plain.proxy = ""
            return plain
        if proxy.proxy_url:
            return HTTPClient(
                timeout=DEFAULT_EXTRACT_TIMEOUT, proxy=proxy.proxy_url
            )
        return HTTPClient(timeout=DEFAULT_EXTRACT_TIMEOUT)

    def extract_endpoint(self) -> str:
        """对应 Go ``WebExtractTool.extractEndpoint``。"""
        endpoint = (self.endpoint or "").strip()
        return endpoint if endpoint != "" else DEFAULT_EXTRACT_ENDPOINT


def new_web_extract_tool(*environment: Environment) -> WebExtractTool:
    """对应 Go ``NewWebExtractTool``。"""
    return WebExtractTool(environment[0] if environment else None)


def web_extract_builder() -> Builder:
    """对应 Go ``webExtractBuilder``。"""
    return (
        Builder("web_extract")
        .description(
            "提取网页正文；默认返回前 8000 字符，可用同一 URL 加 offset 继续读取后续内容。"
        )
        .risk(RiskLow)
        .tags("web")
        .depends_on("web_search")
        .string("url", "要提取的网页 URL。", required())
        .integer("offset", "从第几个字符开始返回，默认 0；用于继续读取缓存中的后续内容。")
        .integer("max_chars", "本次最多返回字符数，默认 8000，硬上限 16000。")
        .string("remove_selector", "可选，覆盖默认移除 CSS 选择器：header, .class, #id。")
        .integer("timeout_ms", "可选，请求超时时间，默认 15000。")
        .string(
            "proxy",
            "代理设置：不填则使用 WEB_EXTRACT_PROXY 或系统代理；填 disabled 禁用代理；"
            "填 URL 使用指定代理。",
        )
        .boolean(
            "jina",
            "是否使用 Jina Reader 提取网页，默认 true；Jina 失败或效果不佳时可传 false "
            "改用直接爬取。",
        )
        .boolean(
            "force_refresh",
            "是否跳过缓存并重新获取网页，默认 false；刷新成功后会覆盖对应缓存。",
        )
    )


def parse_web_extract_args(raw: str) -> WebExtractArgs:
    """对应 Go ``json.Unmarshal`` 到 ``webExtractArgs``。"""
    if not raw:
        return WebExtractArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse web_extract arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse web_extract arguments: expected a JSON object")
    args = WebExtractArgs()
    args.url = _as_str(data.get("url"))
    args.offset = _as_int(data.get("offset"))
    args.max_chars = _as_int(data.get("max_chars"))
    args.remove_selector = _as_str(data.get("remove_selector"))
    args.timeout_ms = _as_int(data.get("timeout_ms"))
    args.proxy = _as_str(data.get("proxy"))
    args.jina = None if data.get("jina") is None else bool(data.get("jina"))
    args.force_refresh = bool(data.get("force_refresh"))
    return args


def normalize_jina_response(data: JinaResponse, fallback_url: str) -> CachedExtract:
    """对应 Go ``normalizeJinaResponse``。"""
    if data.data is not None:
        data.url = data.data.url
        data.title = data.data.title
        data.content = data.data.content
        data.timestamp = data.data.timestamp
    if data.url == "":
        data.url = fallback_url
    return CachedExtract(
        url=data.url, title=data.title, source="jina", content=data.content
    )


def jina_reader_url(endpoint: str, target_url: str) -> str:
    """对应 Go ``jinaReaderURL``：``<endpoint>/http://host/path?query``。"""
    parsed = urlsplit((target_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise InvalidError(f"invalid url: {target_url}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return endpoint.rstrip("/") + "/http://" + parsed.netloc + path


def slice_extract(
    entry: CachedExtract, offset: int, max_chars: int, cached: bool
) -> WebExtractData:
    """对应 Go ``sliceExtract``：按**字符**（Go 的 rune）切片。"""
    if offset < 0:
        offset = 0
    if max_chars <= 0:
        max_chars = DEFAULT_EXTRACT_CHARS
    if max_chars > MAX_EXTRACT_CHARS:
        max_chars = MAX_EXTRACT_CHARS
    total = len(entry.content)
    if offset > total:
        offset = total
    end = min(offset + max_chars, total)
    content = entry.content[offset:end]
    next_offset = end if end < total else None
    return WebExtractData(
        url=entry.url,
        title=entry.title,
        source=entry.source,
        total_chars=total,
        offset=offset,
        returned_chars=end - offset,
        next_offset=next_offset,
        truncated=next_offset is not None,
        cached=cached,
        content=content,
    )


def format_extract_content(data: WebExtractData) -> str:
    """对应 Go ``formatExtractContent``。"""
    out = ["URL: ", data.url]
    if data.source != "":
        out.append("\nSource: ")
        out.append(data.source)
    if data.title != "":
        out.append("\nTitle: ")
        out.append(data.title)
    out.append(
        f"\nRange: {data.offset}-{data.offset + data.returned_chars} / {data.total_chars}"
    )
    if data.next_offset is not None:
        out.append(f"\nNext offset: {data.next_offset}")
    out.append(f"\nCached: {'true' if data.cached else 'false'}\n\n")
    out.append(data.content)
    return "".join(out).strip()


def resolve_extract_proxy(
    environment: Environment | None, value: str, ctx: Any = None
) -> ExtractProxyConfig:
    """对应 Go ``resolveExtractProxy``。"""
    proxy = (value or "").strip()
    if proxy == "":
        proxy = _lookup_env(environment, ctx, WEB_EXTRACT_PROXY_ENV)
    if proxy == "":
        return ExtractProxyConfig(cache_key="env")
    if proxy == "disabled":
        return ExtractProxyConfig(disabled=True, cache_key="disabled")
    parsed = urlsplit(proxy)
    if not parsed.scheme or not parsed.netloc:
        raise InvalidError(f"invalid web_extract proxy url: {proxy}")
    return ExtractProxyConfig(proxy_url=proxy, cache_key=proxy)


def extract_simple_html_text(raw: str) -> tuple[str, str]:
    """对应 Go ``extractSimpleHTMLText``。"""
    title = html.unescape(strip_tags(extract_tag_content(raw, "title"))).strip()
    text = raw
    for tag in ("script", "style", "noscript"):
        text = remove_tag_blocks(text, tag)
    text = remove_html_comments(text)
    text = html.unescape(strip_tags(text))
    return title, " ".join(text.split())


def extract_tag_content(raw: str, tag: str) -> str:
    """对应 Go ``extractTagContent``。"""
    lower = raw.lower()
    open_tag = "<" + tag
    start = lower.find(open_tag)
    if start < 0:
        return ""
    open_end = lower.find(">", start)
    if open_end < 0:
        return ""
    content_start = open_end + 1
    close = lower.find("</" + tag + ">", content_start)
    if close < 0:
        return ""
    return raw[content_start:close]


def remove_tag_blocks(raw: str, tag: str) -> str:
    """对应 Go ``removeTagBlocks``。"""
    while True:
        lower = raw.lower()
        open_index = lower.find("<" + tag)
        if open_index < 0:
            return raw
        close_start = lower.find("</" + tag + ">", open_index)
        if close_start < 0:
            return raw[:open_index]
        end = close_start + len(tag) + 3
        raw = raw[:open_index] + " " + raw[end:]


def remove_html_comments(raw: str) -> str:
    """对应 Go ``removeHTMLComments``。"""
    while True:
        start = raw.find("<!--")
        if start < 0:
            return raw
        end = raw.find("-->", start + 4)
        if end < 0:
            return raw[:start]
        raw = raw[:start] + " " + raw[end + 3 :]


def strip_tags(raw: str) -> str:
    """对应 Go ``stripTags``。"""
    out: list[str] = []
    in_tag = False
    for ch in raw:
        if ch == "<":
            in_tag = True
            out.append(" ")
        elif ch == ">":
            in_tag = False
            out.append(" ")
        elif not in_tag:
            out.append(ch)
    return "".join(out)


def extract_cache_key(
    source: str, target_url: str, selector: str, proxy: ExtractProxyConfig
) -> str:
    """对应 Go ``extractCacheKey``（``\\x00`` 分隔）。"""
    return "\x00".join(
        [
            (source or "").strip(),
            (target_url or "").strip(),
            (selector or "").strip(),
            proxy.cache_key,
        ]
    )


def _parse_jina_response(payload: Any) -> JinaResponse:
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    data = payload.get("data")
    parsed_data = None
    if isinstance(data, dict):
        parsed_data = JinaResponseData(
            url=_as_str(data.get("url")),
            title=_as_str(data.get("title")),
            content=_as_str(data.get("content")),
            timestamp=_as_str(data.get("timestamp")),
        )
    return JinaResponse(
        url=_as_str(payload.get("url")),
        title=_as_str(payload.get("title")),
        content=_as_str(payload.get("content")),
        timestamp=_as_str(payload.get("timestamp")),
        data=parsed_data,
    )


def _timeout_seconds(timeout_ms: int) -> float:
    return timeout_ms / 1000 if timeout_ms > 0 else DEFAULT_EXTRACT_TIMEOUT


def _lookup_env(environment: Environment | None, ctx: Any, key: str) -> str:
    environment = environment if environment is not None else Environment()
    value, ok = environment.lookup(key)
    if ok and (value or "").strip() != "":
        return value.strip()
    fallback, found = optional_builtin_env(ctx, key)
    return fallback if found else ""


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [name for name in dir() if not name.startswith("_")]
