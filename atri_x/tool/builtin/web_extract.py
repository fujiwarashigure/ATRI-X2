"""``web_extract``：网页正文提取（可配置的抓取通道链）。

对应 Go：``internal/tool/builtin/web_extract.go``。

与 Go 的差异（仅形式）：

* ``net/http`` → :class:`atri_x.net.httpclient.HTTPClient`；代理通过带
  ``proxy`` 的 ``HTTPClient`` 表达（Go 是自建 ``http.Transport``）。
* ``sync.Mutex`` → :class:`threading.Lock`（缓存是同步内存结构）。
* HTML 清洗与各通道的请求/解析在 :mod:`atri_x.tool.builtin.web_extract_engines`，
  本模块只负责通道链、缓存与分段输出。

与 Go 的差异（行为）：

* Go 版固定「Jina（有 key）→ 直接抓取」两级；本仓按 ``[web_extract]`` 配置
  （桌面端「模型 → 抓取服务」页写的就是它）逐条尝试通道，单条失败自动换下一条。
  配置段缺席时仍是「Jina → 直接抓取」，旧部署行为不变。
* 可选通道 ``deepseek`` 用 DeepSeek 官方抓取接口（返回 Markdown 正文），按成功请求
  计费，所以不在默认链里；要显式写进 ``engine_order`` 才启用。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import urlsplit

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import optional_builtin_env
from atri_x.tool.builtin.web_extract_engines import (
    BUILTIN_PROTOCOLS,
    DEFAULT_KEY_ENV,
    JINA_DEFAULT_ENDPOINT,
    KEYED_PROTOCOLS,
    PROTOCOL_DEEPSEEK,
    PROTOCOL_DIRECT,
    PROTOCOL_JINA,
    ExtractEngineFailure,
    ExtractEngineSpec,
    ExtractPage,
    JinaData as JinaResponseData,
    JinaResponse,
    default_specs,
    extract_deepseek,
    extract_direct,
    extract_jina,
    extract_simple_html_text,
    extract_tag_content,
    jina_reader_url,
    normalize_jina_response,
    remove_html_comments,
    remove_tag_blocks,
    strip_tags,
)
from atri_x.tool.capability import ctx_raise_if_cancelled
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

JINA_API_KEY_ENV = DEFAULT_KEY_ENV[PROTOCOL_JINA]
WEB_EXTRACT_PROXY_ENV = "WEB_EXTRACT_PROXY"
DEFAULT_EXTRACT_ENDPOINT = JINA_DEFAULT_ENDPOINT
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
    """对应 Go ``WebExtractTool``；抓取通道可配置，单条失败自动换下一条。"""

    def __init__(
        self,
        environment: Environment | None = None,
        *,
        client: HTTPClient | None = None,
        endpoint: str = "",
        specs: Sequence[ExtractEngineSpec] | None = None,
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        #: 来自 ``[web_extract]`` 配置；为空表示走 Jina → 直抓的默认链。
        self.specs = list(specs) if specs else []
        self.process_env = environment if environment is not None else Environment()
        self.cache = ExtractCache()

    def name(self) -> str:
        return "web_extract"

    def info(self) -> Info:
        return web_extract_builder().build_info()

    def schema(self) -> ToolSchema:
        return web_extract_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """按通道链抓取网页；单条失败换下一条，全部失败才报错。"""
        ctx_raise_if_cancelled(ctx)
        args = parse_web_extract_args(req.arguments)
        target_url = (args.url or "").strip()
        if target_url == "":
            raise InvalidError("url is required")
        selector = (args.remove_selector or "").strip() or DEFAULT_REMOVE_SELECTOR
        proxy = resolve_extract_proxy(self.process_env, args.proxy, ctx)
        client = self.http_client(proxy)
        use_jina = True if args.jina is None else bool(args.jina)
        failures: list[str] = []
        for spec in self.extract_chain(ctx, use_jina):
            ctx_raise_if_cancelled(ctx)
            key = extract_cache_key(spec.protocol, target_url, selector, proxy)
            if not args.force_refresh:
                entry, cached = self.cache.get(key)
                if cached:
                    data = slice_extract(entry, args.offset, args.max_chars, True)
                    return Result(content=format_extract_content(data))
            try:
                page = await self.run_engine(ctx, client, spec, target_url, selector, args.timeout_ms)
            except ExtractEngineFailure as exc:
                failures.append(f"{spec.name}：{exc}")
                continue
            entry = CachedExtract(
                url=page.url, title=page.title, source=page.source, content=page.content
            )
            self.cache.set(key, entry)
            data = slice_extract(entry, args.offset, args.max_chars, False)
            return Result(content=format_extract_content(data))
        raise InvalidError("web_extract 所有抓取通道均失败：" + "；".join(failures))

    def extract_chain(self, ctx: Any, use_jina: bool) -> list[ExtractEngineSpec]:
        """候选通道：``[web_extract]`` 配置优先，否则 Jina → 直抓的默认链。"""
        candidates = self.specs or default_specs()
        chain: list[ExtractEngineSpec] = []
        for spec in candidates:
            if spec.protocol == PROTOCOL_JINA and not use_jina:
                continue
            if spec.protocol in KEYED_PROTOCOLS and self.engine_api_key(ctx, spec) == "":
                continue
            chain.append(spec)
        if chain:
            return chain
        raise InvalidError(
            "web_extract 没有可用抓取通道：候选 "
            + "、".join(spec.name for spec in candidates)
            + " 都缺少必要配置（Jina / DeepSeek 需要各自的 API Key；也可以加入 direct "
            + "直抓通道）。到「模型 → 抓取服务」页补上，或删掉 [web_extract] 段用默认链。"
        )

    async def run_engine(
        self,
        ctx: Any,
        client: HTTPClient,
        spec: ExtractEngineSpec,
        target_url: str,
        selector: str,
        timeout_ms: int,
    ) -> ExtractPage:
        """按协议跑一条通道；通道级失败抛 :class:`ExtractEngineFailure`。"""
        api_key = self.engine_api_key(ctx, spec)
        timeout = _timeout_seconds(timeout_ms)
        if spec.protocol == PROTOCOL_DEEPSEEK:
            return await extract_deepseek(
                client,
                api_key,
                target_url,
                timeout=timeout,
                endpoint=spec.base_url,
            )
        if spec.protocol == PROTOCOL_JINA:
            return await extract_jina(
                client,
                api_key,
                target_url,
                selector,
                timeout=timeout,
                endpoint=spec.base_url or self.extract_endpoint(),
            )
        if spec.protocol == PROTOCOL_DIRECT:
            return await extract_direct(client, target_url, timeout=timeout)
        raise InvalidError(
            f"未知抓取协议：{spec.protocol}（可选 {', '.join(BUILTIN_PROTOCOLS)}）"
        )

    def engine_api_key(self, ctx: Any, spec: ExtractEngineSpec) -> str:
        """取一条通道的密钥：按 ``key_envs()`` 顺序找第一个非空环境变量。"""
        for name in spec.key_envs():
            value = _lookup_env(self.process_env, ctx, name)
            if value != "":
                return value
        return ""

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
            return HTTPClient(timeout=DEFAULT_EXTRACT_TIMEOUT, proxy=proxy.proxy_url)
        return HTTPClient(timeout=DEFAULT_EXTRACT_TIMEOUT)

    def extract_endpoint(self) -> str:
        """对应 Go ``WebExtractTool.extractEndpoint``。"""
        endpoint = (self.endpoint or "").strip()
        return endpoint if endpoint != "" else DEFAULT_EXTRACT_ENDPOINT


def new_web_extract_tool(
    *environment: Environment, specs: Sequence[ExtractEngineSpec] | None = None
) -> WebExtractTool:
    """对应 Go ``NewWebExtractTool``；``specs`` 来自 ``[web_extract]`` 配置。"""
    return WebExtractTool(environment[0] if environment else None, specs=specs)


def web_extract_builder() -> Builder:
    """对应 Go ``webExtractBuilder``。"""
    return (
        Builder("web_extract")
        .description(
            "提取网页正文；默认返回前 8000 字符，可用同一 URL 加 offset 继续读取后续内容。"
            "抓取通道在「模型 → 抓取服务」页配置（DeepSeek / Jina / 直接抓取），按顺序自动"
            "降级；未配置时用 Jina（有 key 才参与）→ 直接抓取的默认链。"
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
            "是否允许使用 Jina Reader 通道，默认 true；传 false 时跳过 Jina，直接用其它通道。",
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
