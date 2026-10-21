"""``web_search`` 的引擎通道。

对应 Go：``internal/tool/builtin/web_search.go``。上游文件只有 Tavily 一条通道；
Python 版按职责把「一条通道怎么发请求、怎么解析」放在本模块，通道列表、降级顺序
和密钥解析留在 :mod:`atri_x.tool.builtin.web_search`。

通道来自 modsearch 的备选引擎（Tavily 的实现仍留在 ``web_search.py``，以保持与 Go
同名字段的对照）：

* ``deepseek``：``POST https://api.deepseek.com/v1/tools/search``，DeepSeek 官方自研
  搜索引擎（Bearer 鉴权，与聊天 API 同一把 key，但建议用 ``DEEPSEEK_SEARCH_KEY``
  单独配置，回落 ``DEEPSEEK_API_KEY``）。结果带 rerank 分数、长摘要、发布日期与
  权威度；按成功请求计费（5 元 / 1000 次）。
* ``firecrawl``：``POST https://api.firecrawl.dev/v2/search``。不带 ``Authorization``
  就走免注册额度（按出口 IP 计，每月 1000 credits）；带 ``FIRECRAWL_API_KEY`` 时换成
  自己的额度。
* ``keenable``：``POST https://api.keenable.ai/v1/search/public``。免注册按出口 IP
  每小时 1000 次，请求头 ``X-Keenable-Title`` 用来归属匿名流量；带
  ``KEENABLE_API_KEY`` 时改走 ``/v1/search``，换成组织自己的额度。
* ``exa``：``POST https://api.exa.ai/search``，需要 ``EXA_API_KEY``（每月 $10 免费额度）。
* ``searxng``：``GET {base_url}/search?format=json``，自建实例；``base_url`` 必填。
* ``antigravity`` / ``grok``：外部 CLI 通道，见 :mod:`atri_x.tool.builtin.web_search_cli`。

除 Tavily / DeepSeek 外都只返回「标题 + URL + 摘要」，没有模型写的答案，所以
``answer`` 为空；DeepSeek 的 ``summary``（600~800 字）填进 ``snippet`` 供模型阅读。
``warnings`` 里说明摘要生成方式与计费口径。本模块不读环境变量：密钥、端点、超时都由
调用方传入，便于测试注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.net.httpclient import HTTPError

FIRECRAWL_API_KEY_ENV = "FIRECRAWL_API_KEY"
KEENABLE_API_KEY_ENV = "KEENABLE_API_KEY"
DEEPSEEK_KEY_ENVS = ("DEEPSEEK_SEARCH_KEY", "DEEPSEEK_API_KEY")
FIRECRAWL_DEFAULT_ENDPOINT = "https://api.firecrawl.dev/v2/search"
KEENABLE_KEYLESS_ENDPOINT = "https://api.keenable.ai/v1/search/public"
KEENABLE_KEYED_ENDPOINT = "https://api.keenable.ai/v1/search"
EXA_DEFAULT_ENDPOINT = "https://api.exa.ai/search"
DEEPSEEK_DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/search"

# -- 协议名（``[web_search.engines.<name>].protocol`` 的取值） -----------------
PROTOCOL_DEEPSEEK = "deepseek"
PROTOCOL_TAVILY = "tavily"
PROTOCOL_FIRECRAWL = "firecrawl"
PROTOCOL_KEENABLE = "keenable"
PROTOCOL_EXA = "exa"
PROTOCOL_SEARXNG = "searxng"
PROTOCOL_ANTIGRAVITY = "antigravity"
PROTOCOL_GROK = "grok"
#: 全部可选协议（``web_search`` 工具与桌面端下拉共用这份清单）。
BUILTIN_PROTOCOLS = (
    PROTOCOL_DEEPSEEK,
    PROTOCOL_TAVILY,
    PROTOCOL_FIRECRAWL,
    PROTOCOL_KEENABLE,
    PROTOCOL_EXA,
    PROTOCOL_SEARXNG,
    PROTOCOL_ANTIGRAVITY,
    PROTOCOL_GROK,
)
#: 必须配 API key 才能用的协议。
KEYED_PROTOCOLS = (PROTOCOL_DEEPSEEK, PROTOCOL_TAVILY, PROTOCOL_EXA)
#: 免注册协议：没有 key 也能搜。
KEYLESS_PROTOCOLS = (PROTOCOL_FIRECRAWL, PROTOCOL_KEENABLE)
#: 外部 CLI 协议：需要本机装好并登录对应命令行。
CLI_PROTOCOLS = (PROTOCOL_ANTIGRAVITY, PROTOCOL_GROK)
#: 配置里 ``api_key_env`` 留空时按协议回落到这个环境变量名。
DEFAULT_KEY_ENV = {
    PROTOCOL_DEEPSEEK: DEEPSEEK_KEY_ENVS[0],
    PROTOCOL_TAVILY: "TAVILY_API_KEY",
    PROTOCOL_FIRECRAWL: "FIRECRAWL_API_KEY",
    PROTOCOL_KEENABLE: "KEENABLE_API_KEY",
    PROTOCOL_EXA: "EXA_API_KEY",
}
#: 首选密钥环境变量都没配时，再回落这些（只用于「同一提供商另有通用 key」的场景）。
FALLBACK_KEY_ENV = {
    PROTOCOL_DEEPSEEK: DEEPSEEK_KEY_ENVS[1],
}
#: 必须显式给服务地址的协议（自建 SearXNG 没有官方默认端点）。
ENDPOINT_REQUIRED_PROTOCOLS = (PROTOCOL_SEARXNG,)
#: 免注册调用时用来归属匿名流量的应用名（Keenable 要求非空）。
KEYLESS_APP_TITLE = "atri-x"
#: Keenable 只接受 1 到 50 的结果数。
KEENABLE_MAX_RESULTS = 50
#: DeepSeek 搜索的 ``count`` 取值范围。
DEEPSEEK_MAX_RESULTS = 50
#: Firecrawl 的 ``timeout`` 字段取值范围（毫秒）。
FIRECRAWL_TIMEOUT_FLOOR_MS = 1_000
FIRECRAWL_TIMEOUT_CEILING_MS = 300_000
_MAX_HTTP_BODY = 2048


def key_env_names(protocol: str, api_key_env: str = "") -> tuple[str, ...]:
    """一条通道按优先级读取的密钥环境变量名。

    显式配置了 ``api_key_env`` 就只用它；显式配置成协议的首选名（或留空）时，
    再追加该协议的兜底环境变量（目前只有 DeepSeek：``DEEPSEEK_SEARCH_KEY``
    → ``DEEPSEEK_API_KEY``）。桌面端与工具层共用这一处口径，避免「界面显示已设置、
    实际取不到 key」。
    """
    explicit = (api_key_env or "").strip()
    default = DEFAULT_KEY_ENV.get(protocol, "")
    fallback = FALLBACK_KEY_ENV.get(protocol, "")
    if explicit != "":
        names = [explicit]
        if fallback != "" and explicit == default:
            names.append(fallback)
        return tuple(names)
    names = [default] if default != "" else []
    if fallback != "":
        names.append(fallback)
    return tuple(names)


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
        """首个密钥环境变量名（显式配置优先，否则用协议内置名；CLI/自建通道为空）。"""
        names = key_env_names(self.protocol, self.api_key_env)
        return names[0] if names else ""

    def key_envs(self) -> tuple[str, ...]:
        """这条通道按优先级读取的全部密钥环境变量名。"""
        return key_env_names(self.protocol, self.api_key_env)


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
    """``[web_search]`` 缺席时的默认通道链。

    DeepSeek 官方搜索排在首位：只有 ``DEEPSEEK_SEARCH_KEY`` / ``DEEPSEEK_API_KEY``
    配了才参与（``KEYED_PROTOCOLS`` 的跳过规则），没配时行为与 0.9.9 一致。
    """
    return [
        SearchEngineSpec(
            name=name,
            protocol=name,
            api_key_env=DEFAULT_KEY_ENV.get(name, ""),
        )
        for name in (
            PROTOCOL_DEEPSEEK,
            PROTOCOL_TAVILY,
            PROTOCOL_FIRECRAWL,
            PROTOCOL_KEENABLE,
            PROTOCOL_EXA,
        )
    ]


async def search_deepseek(
    client: Any,
    query: SearchQuery,
    *,
    api_key: str = "",
    endpoint: str = "",
    timeout: float = 15.0,
) -> SearchOutcome:
    """DeepSeek 官方网页搜索（``POST {base_url}/v1/tools/search``）。

    请求体只发工具能给出的字段：``query`` / ``count``；``need_content`` 固定 ``false``
    （正文交给 ``web_extract``，避免搜索把整页内容灌进上下文）。响应里的 ``summary``
    （600~800 字，给模型阅读）填进 ``SearchHit.snippet``，``date`` 有值时前置成
    「（发布：YYYY-MM-DD）」，没有模型写的答案，所以 ``answer`` 为空。
    """
    if not api_key:
        raise EngineFailure(
            "DeepSeek 搜索通道缺少密钥（DEEPSEEK_SEARCH_KEY / DEEPSEEK_API_KEY 未配置）"
        )
    payload = {
        "query": query.query,
        "count": _clamp_deepseek_count(query.max_results),
        "need_content": False,
    }
    data = await _post_json(
        client,
        (endpoint or "").strip() or DEEPSEEK_DEFAULT_ENDPOINT,
        payload,
        {"Authorization": "Bearer " + api_key},
        timeout,
        "DeepSeek 搜索",
    )
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        snippet = _as_str(item.get("summary")) or _as_str(item.get("snippet"))
        date = _as_str(item.get("date"))
        if date:
            published = f"（发布：{date[:10]}）"
            snippet = published + ("\n" + snippet if snippet else "")
        hits.append(
            SearchHit(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                snippet=snippet,
            )
        )
    return SearchOutcome(
        engine=PROTOCOL_DEEPSEEK,
        hits=hits,
        warnings=[
            "DeepSeek 官方搜索引擎（自研索引）：结果带相关性/权威度/发布日期排序，"
            "摘要由通道生成；该通道按成功请求计费（5 元 / 1000 次）。"
        ],
    )


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


def _clamp_deepseek_count(max_results: int) -> int:
    """DeepSeek 的 ``count`` 取值 1~50；工具层已经把它收敛到 1~20。"""
    if max_results <= 0:
        return 5
    if max_results > DEEPSEEK_MAX_RESULTS:
        return DEEPSEEK_MAX_RESULTS
    return max_results


def _clamp_timeout_ms(value: float) -> int:
    return int(min(max(value, FIRECRAWL_TIMEOUT_FLOOR_MS), FIRECRAWL_TIMEOUT_CEILING_MS))


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


__all__ = [name for name in dir() if not name.startswith("_")]
