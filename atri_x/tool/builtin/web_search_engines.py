"""``web_search`` 的引擎通道。

对应 Go：``internal/tool/builtin/web_search.go``。上游文件只有 Tavily 一条通道；
Python 版按职责把「一条通道怎么发请求、怎么解析」放在本模块，通道列表、降级顺序
和密钥解析留在 :mod:`atri_x.tool.builtin.web_search`。

通道来自 modsearch 的备选引擎（Tavily 的实现仍留在 ``web_search.py``，以保持与 Go
同名字段的对照）：

* ``firecrawl``：``POST https://api.firecrawl.dev/v2/search``。不带 ``Authorization``
  就走免注册额度（按出口 IP 计，每月 1000 credits）；带 ``FIRECRAWL_API_KEY`` 时换成
  自己的额度。
* ``keenable``：``POST https://api.keenable.ai/v1/search/public``。免注册按出口 IP
  每小时 1000 次，请求头 ``X-Keenable-Title`` 用来归属匿名流量；带
  ``KEENABLE_API_KEY`` 时改走 ``/v1/search``，换成组织自己的额度。
* ``exa``：``POST https://api.exa.ai/search``，需要 ``EXA_API_KEY``（每月 $10 免费额度）。
* ``searxng``：``GET {base_url}/search?format=json``，自建实例；``base_url`` 必填。
* ``antigravity`` / ``grok``：外部 CLI 通道，见 :mod:`atri_x.tool.builtin.web_search_cli`。

除 Tavily 外都只返回「标题 + URL + 摘要」，没有模型写的答案，所以 ``answer`` 为空，
``warnings`` 里说明摘要由通道机械生成。本模块不读环境变量：密钥、端点、超时都由
调用方传入，便于测试注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.net.httpclient import HTTPError

FIRECRAWL_API_KEY_ENV = "FIRECRAWL_API_KEY"
KEENABLE_API_KEY_ENV = "KEENABLE_API_KEY"
FIRECRAWL_DEFAULT_ENDPOINT = "https://api.firecrawl.dev/v2/search"
KEENABLE_KEYLESS_ENDPOINT = "https://api.keenable.ai/v1/search/public"
KEENABLE_KEYED_ENDPOINT = "https://api.keenable.ai/v1/search"
EXA_DEFAULT_ENDPOINT = "https://api.exa.ai/search"

# -- 协议名（``[web_search.engines.<name>].protocol`` 的取值） -----------------
PROTOCOL_TAVILY = "tavily"
PROTOCOL_FIRECRAWL = "firecrawl"
PROTOCOL_KEENABLE = "keenable"
PROTOCOL_EXA = "exa"
PROTOCOL_SEARXNG = "searxng"
PROTOCOL_ANTIGRAVITY = "antigravity"
PROTOCOL_GROK = "grok"
#: 全部可选协议（``web_search`` 工具与桌面端下拉共用这份清单）。
BUILTIN_PROTOCOLS = (
    PROTOCOL_TAVILY,
    PROTOCOL_FIRECRAWL,
    PROTOCOL_KEENABLE,
    PROTOCOL_EXA,
    PROTOCOL_SEARXNG,
    PROTOCOL_ANTIGRAVITY,
    PROTOCOL_GROK,
)
#: 必须配 API key 才能用的协议。
KEYED_PROTOCOLS = (PROTOCOL_TAVILY, PROTOCOL_EXA)
#: 免注册协议：没有 key 也能搜。
KEYLESS_PROTOCOLS = (PROTOCOL_FIRECRAWL, PROTOCOL_KEENABLE)
#: 外部 CLI 协议：需要本机装好并登录对应命令行。
CLI_PROTOCOLS = (PROTOCOL_ANTIGRAVITY, PROTOCOL_GROK)
#: 配置里 ``api_key_env`` 留空时按协议回落到这个环境变量名。
DEFAULT_KEY_ENV = {
    PROTOCOL_TAVILY: "TAVILY_API_KEY",
    PROTOCOL_FIRECRAWL: "FIRECRAWL_API_KEY",
    PROTOCOL_KEENABLE: "KEENABLE_API_KEY",
    PROTOCOL_EXA: "EXA_API_KEY",
}
#: 必须显式给服务地址的协议（自建 SearXNG 没有官方默认端点）。
ENDPOINT_REQUIRED_PROTOCOLS = (PROTOCOL_SEARXNG,)
#: 免注册调用时用来归属匿名流量的应用名（Keenable 要求非空）。
KEYLESS_APP_TITLE = "atri-x"
#: Keenable 只接受 1 到 50 的结果数。
KEENABLE_MAX_RESULTS = 50
#: Firecrawl 的 ``timeout`` 字段取值范围（毫秒）。
FIRECRAWL_TIMEOUT_FLOOR_MS = 1_000
FIRECRAWL_TIMEOUT_CEILING_MS = 300_000
_MAX_HTTP_BODY = 2048


class EngineFailure(Exception):
    """单条通道失败。

    只表示「这条通道这次没成」，调用方据此换下一条通道；所有通道都失败时才升级成
    :class:`atri_x.errors.InvalidError`。
    """


@dataclass(frozen=True)
class SearchQuery:
    """一条通道能理解的查询。只保留各通道共有的字段。"""

    query: str
    max_results: int = 5
    search_depth: str = ""
    topic: str = ""
    time_range: str = ""
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    country: str = ""
    exact_match: bool = False


@dataclass
class SearchHit:
    """一条搜索结果。``snippet`` 是通道给的摘要，可能为空。"""

    title: str = ""
    url: str = ""
    snippet: str = ""


@dataclass
class SearchOutcome:
    """一条通道的完整结果。"""

    engine: str = ""
    answer: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchEngineSpec:
    """配置好的一条搜索通道（``[web_search.engines.<name>]`` 的解析结果）。"""

    name: str
    protocol: str
    api_key_env: str = ""
    base_url: str = ""
    binary: str = ""

    def key_env(self) -> str:
        """密钥环境变量名：显式配置优先，否则用协议的内置名（CLI/自建通道为空）。"""
        return self.api_key_env.strip() or DEFAULT_KEY_ENV.get(self.protocol, "")


def specs_from_config(config: Any) -> list[SearchEngineSpec]:
    """把 ``[web_search]`` 配置段转成通道列表；段缺席或没有顺序时返回空列表。

    内置名（``tavily`` 等）留空 ``protocol`` 时等于名字；自定义名字必须显式写
    ``protocol``，否则该条被忽略（配置错误留给工具层的报错信息说明）。
    """
    order = list(getattr(config, "engine_order", []) or [])
    engines = getattr(config, "engines", {}) or {}
    if not order:
        return []
    specs: list[SearchEngineSpec] = []
    for raw_name in order:
        name = str(raw_name or "").strip()
        if not name:
            continue
        entry = engines.get(name) if isinstance(engines, dict) else None
        protocol = str(getattr(entry, "protocol", "") or "").strip().lower()
        if not protocol:
            protocol = name.lower() if name.lower() in BUILTIN_PROTOCOLS else ""
        if not protocol:
            continue
        specs.append(
            SearchEngineSpec(
                name=name,
                protocol=protocol,
                api_key_env=str(getattr(entry, "api_key_env", "") or "").strip(),
                base_url=str(getattr(entry, "base_url", "") or "").strip(),
                binary=str(getattr(entry, "bin", "") or "").strip(),
            )
        )
    return specs


def default_specs() -> list[SearchEngineSpec]:
    """``[web_search]`` 缺席时的默认通道链（与 0.9.6 的行为一致，外加 Exa 有 key 才参与）。"""
    return [
        SearchEngineSpec(name=name, protocol=name, api_key_env=DEFAULT_KEY_ENV.get(name, ""))
        for name in (PROTOCOL_TAVILY, PROTOCOL_FIRECRAWL, PROTOCOL_KEENABLE, PROTOCOL_EXA)
    ]


async def search_firecrawl(
    client: Any,
    query: SearchQuery,
    *,
    api_key: str = "",
    endpoint: str = "",
    timeout: float = 15.0,
) -> SearchOutcome:
    """Firecrawl 网页搜索。无 key 时走免注册通道。"""
    payload = {
        "query": query.query,
        "limit": _clamp_max_results(query.max_results),
        "sources": ["web"],
        "timeout": int(_clamp_timeout_ms(timeout * 1000)),
    }
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = await _post_json(
        client,
        endpoint or FIRECRAWL_DEFAULT_ENDPOINT,
        payload,
        headers,
        timeout,
        "Firecrawl",
    )
    web = ((data.get("data") or {}) if isinstance(data.get("data"), dict) else {}).get("web")
    hits: list[SearchHit] = []
    for item in web or []:
        if not isinstance(item, dict):
            continue
        hits.append(
            SearchHit(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                snippet=_as_str(item.get("description")),
            )
        )
    return SearchOutcome(
        engine="firecrawl",
        hits=hits,
        warnings=[
            "Firecrawl 只返回排序后的结果、不返回模型摘要，摘要由通道机械生成，"
            "需要证据时直接读链接。"
        ],
    )


async def search_keenable(
    client: Any,
    query: SearchQuery,
    *,
    api_key: str = "",
    endpoint: str = "",
    timeout: float = 15.0,
) -> SearchOutcome:
    """Keenable 网页搜索。无 key 时走免注册通道。"""
    max_results = query.max_results
    if max_results <= 0:
        max_results = 5
    if max_results > KEENABLE_MAX_RESULTS:
        max_results = KEENABLE_MAX_RESULTS
    payload = {"query": query.query, "max_results": max_results}
    if api_key:
        headers = {"X-API-Key": api_key}
        url = endpoint or KEENABLE_KEYED_ENDPOINT
    else:
        headers = {"X-Keenable-Title": KEYLESS_APP_TITLE}
        url = endpoint or KEENABLE_KEYLESS_ENDPOINT
    data = await _post_json(client, url, payload, headers, timeout, "Keenable")
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        snippet = _as_str(item.get("snippet")) or _as_str(item.get("description"))
        hits.append(
            SearchHit(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                snippet=snippet,
            )
        )
    return SearchOutcome(
        engine="keenable",
        hits=hits,
        warnings=[
            "Keenable 只返回排序后的结果、不返回模型摘要，摘要由通道机械生成，"
            "需要证据时直接读链接。"
        ],
    )


async def search_exa(
    client: Any,
    query: SearchQuery,
    *,
    api_key: str = "",
    endpoint: str = "",
    timeout: float = 15.0,
) -> SearchOutcome:
    """Exa 网页搜索（需要 API key；返回排序结果 + 高亮片段，没有模型摘要）。"""
    if not api_key:
        raise EngineFailure("Exa 通道需要 API key（配置 api_key_env 指向的环境变量）")
    payload = {
        "query": query.query,
        "numResults": _clamp_max_results(query.max_results),
        "contents": {"highlights": True},
    }
    headers = {"x-api-key": api_key}
    data = await _post_json(
        client, endpoint or EXA_DEFAULT_ENDPOINT, payload, headers, timeout, "Exa"
    )
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        highlights = item.get("highlights")
        snippet = ""
        if isinstance(highlights, list) and highlights:
            snippet = _as_str(highlights[0])
        hits.append(
            SearchHit(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                snippet=snippet,
            )
        )
    return SearchOutcome(
        engine=PROTOCOL_EXA,
        hits=hits,
        warnings=[
            "Exa 只返回排序后的结果、不返回模型摘要，摘要由通道机械生成，需要证据时直接读链接。"
        ],
    )


async def search_searxng(
    client: Any,
    query: SearchQuery,
    *,
    api_key: str = "",
    endpoint: str = "",
    timeout: float = 15.0,
) -> SearchOutcome:
    """自建 SearXNG（``GET {base_url}/search?format=json``）。

    SearXNG 默认允许匿名 JSON 查询；实例开了 ``search.formats`` 限制时需要 ``api_key``，
    走 ``X-API-Key`` 头（部分反向代理用 ``Authorization: Bearer``，可自行换用兼容端点）。
    """
    base = endpoint.strip()
    if base == "":
        raise EngineFailure("SearXNG 通道需要 base_url（例如 https://searx.example.com）")
    url = base.rstrip("/") + "/search"
    headers = {"X-API-Key": api_key} if api_key else {}
    params = {
        "q": query.query,
        "format": "json",
        "categories": "general",
        "safesearch": "1",
    }
    try:
        response = await client.request(
            "GET", url, headers=headers, params=params, timeout=timeout
        )
    except HTTPError as exc:
        raise EngineFailure(f"SearXNG 请求失败：{exc}") from exc
    if not response.ok:
        raise EngineFailure(
            f"SearXNG HTTP {response.status}：{truncate_http_body(response.body)}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise EngineFailure(f"解析 SearXNG 响应失败：{exc}") from exc
    if not isinstance(data, dict):
        raise EngineFailure("解析 SearXNG 响应失败：不是一个 JSON 对象")
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        hits.append(
            SearchHit(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                snippet=_as_str(item.get("content")),
            )
        )
    return SearchOutcome(
        engine=PROTOCOL_SEARXNG,
        hits=hits,
        warnings=[
            "SearXNG 只返回排序后的结果、不返回模型摘要，摘要由通道机械生成，需要证据时直接读链接。"
        ],
    )


async def _post_json(
    client: Any,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    label: str,
) -> dict[str, Any]:
    """POST 一个 JSON 请求并解析 JSON 对象；失败统一转成 :class:`EngineFailure`。"""
    try:
        response = await client.request(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except HTTPError as exc:
        raise EngineFailure(f"{label} 请求失败：{exc}") from exc
    if not response.ok:
        raise EngineFailure(f"{label} HTTP {response.status}：{truncate_http_body(response.body)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise EngineFailure(f"解析 {label} 响应失败：{exc}") from exc
    if not isinstance(data, dict):
        raise EngineFailure(f"解析 {label} 响应失败：不是一个 JSON 对象")
    return data


def truncate_http_body(body: bytes | str) -> str:
    """对应 Go ``truncateHTTPBody``：错误信息里的响应体只留前 2 KiB。"""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    if len(text) <= _MAX_HTTP_BODY:
        return text
    return text[:_MAX_HTTP_BODY] + "..."


def _clamp_max_results(max_results: int) -> int:
    if max_results <= 0:
        return 5
    if max_results > 20:
        return 20
    return max_results


def _clamp_timeout_ms(value: float) -> int:
    return int(min(max(value, FIRECRAWL_TIMEOUT_FLOOR_MS), FIRECRAWL_TIMEOUT_CEILING_MS))


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


__all__ = [name for name in dir() if not name.startswith("_")]
