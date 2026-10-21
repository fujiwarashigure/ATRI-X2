"""``web_search``：网页搜索（可配置的通道链）。

对应 Go：``internal/tool/builtin/web_search.go``。

与 Go 的差异（仅形式）：

* ``net/http`` → :class:`atri_x.net.httpclient.HTTPClient`（统一重试/超时/流式），
  因此 ``call`` 是 ``async def``，超时以 ``HTTPClient`` 的 ``timeout`` 表达（15 秒）。
* 请求体字段名逐字照抄 Go 的 struct tag，``omitempty`` 逐字段复刻。
* Go 用 ``ctx.Err()`` 判取消；Python 用 :func:`atri_x.tool.capability.ctx_raise_if_cancelled`。

与 Go 的差异（行为）：

* Go 版只有 Tavily 一条通道，没有 ``TAVILY_API_KEY`` 时工具直接不可用。Python 版按
  ``[web_search]`` 配置（桌面端「模型 → 搜索服务」页写的就是它）逐条尝试通道，失败自动
  换下一条；配置段缺席时回落到「DeepSeek（有 key 才参与）→ Tavily → Firecrawl 免注册 →
  Keenable 免注册 → Exa」的环境变量默认链，其中 DeepSeek / Tavily / Exa 需要各自的 key
  才参与。
* 各通道的请求与解析见 :mod:`atri_x.tool.builtin.web_search_engines`（HTTP 通道）与
  :mod:`atri_x.tool.builtin.web_search_cli`（``agy`` / ``grok`` 外部 CLI 通道）。
* 所有通道都归一成 :class:`~atri_x.tool.builtin.web_search_engines.SearchOutcome` 再渲染，
  因此「Tavily / DeepSeek 有摘要、其他通道只有条目」这一点只影响文本内容，不影响工具契约。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import optional_builtin_env
from atri_x.tool.builtin.web_search_cli import (
    CLI_SEARCH_TIMEOUT_SECONDS,
    search_antigravity,
    search_grok,
)
from atri_x.tool.builtin.web_search_engines import (
    BUILTIN_PROTOCOLS,
    DEFAULT_KEY_ENV,
    KEYED_PROTOCOLS,
    PROTOCOL_ANTIGRAVITY,
    PROTOCOL_DEEPSEEK,
    PROTOCOL_EXA,
    PROTOCOL_FIRECRAWL,
    PROTOCOL_GROK,
    PROTOCOL_KEENABLE,
    PROTOCOL_SEARXNG,
    PROTOCOL_TAVILY,
    EngineFailure,
    SearchEngineSpec,
    SearchHit,
    SearchOutcome,
    SearchQuery,
    default_specs,
    search_deepseek,
    search_exa,
    search_firecrawl,
    search_keenable,
    search_searxng,
    truncate_http_body,
)
from atri_x.tool.capability import ctx_raise_if_cancelled
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
#: 通道顺序覆盖（逗号分隔，名字与 ``[web_search.engines.<name>]`` 一致）。
#: 配置段存在时仍然生效，便于临时只跑某一条通道。
WEB_SEARCH_ENGINES_ENV = "WEB_SEARCH_ENGINES"
DEFAULT_SEARCH_RESULTS = 5
DEFAULT_SEARCH_DEPTH = "basic"
DEFAULT_SEARCH_ENDPOINT = "https://api.tavily.com/search"
SEARCH_TIMEOUT_SECONDS = 15.0


@dataclass
class WebSearchArgs:
    """对应 Go ``webSearchArgs``。"""

    query: str = ""
    search_depth: str = ""
    max_results: int = 0
    topic: str = ""
    time_range: str = ""
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    country: str = ""
    exact_match: bool = False


@dataclass
class TavilyRequest:
    """对应 Go ``tavilyRequest``（``omitempty`` 字段在 :meth:`to_payload` 里复刻）。"""

    query: str = ""
    search_depth: str = ""
    max_results: int = 0
    topic: str = ""
    time_range: str = ""
    include_answer: bool = False
    include_raw_content: bool = False
    include_images: bool = False
    include_image_descriptions: bool = False
    include_favicon: bool = False
    include_usage: bool = False
    auto_parameters: bool = False
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    country: str = ""
    exact_match: bool = False

    def to_payload(self) -> dict[str, Any]:
        """按 Go 的 tag + omitempty 生成 JSON 请求体。"""
        payload = asdict(self)
        for key in (
            "topic",
            "time_range",
            "include_domains",
            "exclude_domains",
            "country",
        ):
            if not payload[key]:
                del payload[key]
        if not payload["exact_match"]:
            del payload["exact_match"]
        return payload


@dataclass
class TavilyResult:
    """对应 Go ``tavilyResult``。"""

    title: str = ""
    url: str = ""
    content: str = ""
    score: float = 0.0


@dataclass
class TavilyResponse:
    """对应 Go ``tavilyResponse``。"""

    query: str = ""
    answer: str = ""
    results: list[TavilyResult] = field(default_factory=list)
    response_time: float = 0.0
    request_id: str = ""


class WebSearchTool:
    """对应 Go ``WebSearchTool``；通道链可配置，单条失败自动换下一条。"""

    def __init__(
        self,
        environment: Environment | None = None,
        *,
        client: HTTPClient | None = None,
        endpoint: str = "",
        firecrawl_endpoint: str = "",
        keenable_endpoint: str = "",
        engines: str = "",
        specs: Sequence[SearchEngineSpec] | None = None,
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self.firecrawl_endpoint = firecrawl_endpoint
        self.keenable_endpoint = keenable_endpoint
        self.engines = engines
        #: 来自 ``[web_search]`` 配置；为空表示走环境变量默认链。
        self.specs = list(specs) if specs else []
        self.process_env = environment

    def name(self) -> str:
        return "web_search"

    def info(self) -> Info:
        return web_search_builder().build_info()

    def schema(self) -> ToolSchema:
        return web_search_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``WebSearchTool.Call``；通道失败时按顺序降级。"""
        ctx_raise_if_cancelled(ctx)
        args = parse_web_search_args(req.arguments)
        query = (args.query or "").strip()
        if query == "":
            raise InvalidError("query is required")
        client = self.http_client()
        failures: list[str] = []
        for spec in self.search_chain(ctx):
            ctx_raise_if_cancelled(ctx)
            try:
                outcome = await self.run_engine(ctx, client, spec, args, query)
            except EngineFailure as exc:
                failures.append(f"{spec.name}：{exc}")
                continue
            return Result(content=format_search_content(outcome))
        raise InvalidError("web_search 所有通道均失败：" + "；".join(failures))

    async def run_engine(
        self,
        ctx: Any,
        client: HTTPClient,
        spec: SearchEngineSpec,
        args: WebSearchArgs,
        query: str,
    ) -> SearchOutcome:
        """按协议跑一条通道；通道级失败抛 :class:`EngineFailure`。"""
        search_query = self.search_query(args, query)
        api_key = self.engine_api_key(ctx, spec)
        if spec.protocol == PROTOCOL_DEEPSEEK:
            return await search_deepseek(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url,
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        if spec.protocol == PROTOCOL_TAVILY:
            return await self.search_tavily(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url or self.endpoint,
            )
        if spec.protocol == PROTOCOL_FIRECRAWL:
            return await search_firecrawl(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url or self.firecrawl_endpoint,
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        if spec.protocol == PROTOCOL_KEENABLE:
            return await search_keenable(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url or self.keenable_endpoint,
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        if spec.protocol == PROTOCOL_EXA:
            return await search_exa(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url,
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        if spec.protocol == PROTOCOL_SEARXNG:
            return await search_searxng(
                client,
                search_query,
                api_key=api_key,
                endpoint=spec.base_url,
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        if spec.protocol == PROTOCOL_ANTIGRAVITY:
            return await search_antigravity(
                search_query, binary=spec.binary, timeout=CLI_SEARCH_TIMEOUT_SECONDS
            )
        if spec.protocol == PROTOCOL_GROK:
            return await search_grok(
                search_query, binary=spec.binary, timeout=CLI_SEARCH_TIMEOUT_SECONDS
            )
        raise InvalidError(
            f"未知搜索协议：{spec.protocol}（可选 {', '.join(BUILTIN_PROTOCOLS)}）"
        )

    async def search_tavily(
        self,
        client: HTTPClient,
        query: SearchQuery,
        *,
        api_key: str,
        endpoint: str = "",
    ) -> SearchOutcome:
        """Tavily 通道：请求体与 Go 版逐字段一致，多出来的答案摘要并入 ``answer``。"""
        if api_key == "":
            raise EngineFailure(
                f"Tavily 通道缺少密钥（{DEFAULT_KEY_ENV[PROTOCOL_TAVILY]} 未配置）"
            )
        args = WebSearchArgs(
            query=query.query,
            search_depth=query.search_depth,
            max_results=query.max_results,
            topic=query.topic,
            time_range=query.time_range,
            include_domains=list(query.include_domains),
            exclude_domains=list(query.exclude_domains),
            country=query.country,
            exact_match=query.exact_match,
        )
        payload = normalize_tavily_request(args, query.query)
        headers = {"Authorization": "Bearer " + api_key}
        try:
            response = await client.request(
                "POST",
                (endpoint or "").strip() or DEFAULT_SEARCH_ENDPOINT,
                headers=headers,
                json=payload.to_payload(),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except HTTPError as exc:
            raise EngineFailure(f"Tavily request: {exc}") from exc
        if not response.ok:
            raise EngineFailure(
                f"Tavily HTTP {response.status}: {truncate_http_body(response.body)}"
            )
        try:
            data = parse_tavily_response(response.json())
        except ValueError as exc:
            raise EngineFailure(f"parse Tavily response: {exc}") from exc
        return SearchOutcome(
            engine=PROTOCOL_TAVILY,
            answer=data.answer,
            hits=[
                SearchHit(title=item.title, url=item.url, snippet=item.content)
                for item in data.results
            ],
        )

    def search_query(self, args: WebSearchArgs, query: str) -> SearchQuery:
        """把工具参数收敛成各通道共有的查询字段。"""
        max_results = args.max_results
        if max_results <= 0:
            max_results = DEFAULT_SEARCH_RESULTS
        if max_results > 20:
            max_results = 20
        return SearchQuery(
            query=query,
            max_results=max_results,
            search_depth=(args.search_depth or "").strip() or DEFAULT_SEARCH_DEPTH,
            topic=(args.topic or "").strip(),
            time_range=(args.time_range or "").strip(),
            include_domains=tuple(trim_string_list(args.include_domains)),
            exclude_domains=tuple(trim_string_list(args.exclude_domains)),
            country=(args.country or "").strip(),
            exact_match=args.exact_match,
        )

    def search_chain(self, ctx: Any) -> list[SearchEngineSpec]:
        """候选通道：``[web_search]`` 配置优先，否则环境变量默认链。"""
        candidates = self.specs or default_specs()
        override = self.engine_override(ctx)
        if override:
            by_name = {spec.name.lower(): spec for spec in candidates}
            unknown = [name for name in override if name not in by_name]
            if unknown:
                raise InvalidError(
                    f"{WEB_SEARCH_ENGINES_ENV} 含未知通道：{', '.join(unknown)}；"
                    f"可选 {', '.join(sorted(by_name))}"
                )
            candidates = [by_name[name] for name in override]
        chain = [spec for spec in candidates if self.spec_available(ctx, spec)]
        if chain:
            return chain
        missing = "、".join(spec.name for spec in candidates)
        raise InvalidError(
            "web_search 没有可用通道：候选 "
            + missing
            + " 都缺少必要配置（Tavily / Exa / DeepSeek 需要各自的 API Key，例如 "
            + f"{DEFAULT_KEY_ENV[PROTOCOL_TAVILY]}、{DEFAULT_KEY_ENV[PROTOCOL_DEEPSEEK]}；"
            + "SearXNG 需要服务地址）。"
            + "到「模型 → 搜索服务」页补上，或加入 firecrawl / keenable 免注册通道；"
            + f"也可以留空 {WEB_SEARCH_ENGINES_ENV} 使用默认链。"
        )

    def engine_override(self, ctx: Any) -> list[str]:
        """``WEB_SEARCH_ENGINES`` 或构造参数给的通道顺序（空表示用配置顺序）。"""
        override = (self.engines or "").strip()
        if override == "":
            override = _lookup_env(self.process_env, ctx, WEB_SEARCH_ENGINES_ENV)
        return [name.strip().lower() for name in override.split(",") if name.strip() != ""]

    def spec_available(self, ctx: Any, spec: SearchEngineSpec) -> bool:
        """需要密钥/服务地址的协议缺配置时不进候选链。"""
        if spec.protocol in KEYED_PROTOCOLS:
            return self.engine_api_key(ctx, spec) != ""
        if spec.protocol == PROTOCOL_SEARXNG:
            return spec.base_url != ""
        return True

    def engine_api_key(self, ctx: Any, spec: SearchEngineSpec) -> str:
        """取一条通道的密钥：按 ``key_envs()`` 顺序找第一个非空环境变量。

        DeepSeek 的口径是「``DEEPSEEK_SEARCH_KEY`` 优先，回落 ``DEEPSEEK_API_KEY``」；
        其它协议通常只有一个环境变量名。
        """
        for name in spec.key_envs():
            value = _lookup_env(self.process_env, ctx, name)
            if value != "":
                return value
        return ""

    def http_client(self) -> HTTPClient:
        """对应 Go ``WebSearchTool.httpClient``。"""
        if self.client is not None:
            return self.client
        return HTTPClient(timeout=SEARCH_TIMEOUT_SECONDS)

    def search_endpoint(self) -> str:
        """对应 Go ``WebSearchTool.searchEndpoint``。"""
        endpoint = (self.endpoint or "").strip()
        return endpoint if endpoint != "" else DEFAULT_SEARCH_ENDPOINT


def new_web_search_tool(
    *environment: Environment, specs: Sequence[SearchEngineSpec] | None = None
) -> WebSearchTool:
    """对应 Go ``NewWebSearchTool``；``specs`` 来自 ``[web_search]`` 配置。"""
    return WebSearchTool(environment[0] if environment else None, specs=specs)


def web_search_builder() -> Builder:
    """对应 Go ``webSearchBuilder``。"""
    return (
        Builder("web_search")
        .description(
            "执行网页搜索，返回简洁答案、来源链接和摘要；需要完整网页内容时继续调用 "
            "web_extract。搜索通道在「模型 → 搜索服务」页配置（DeepSeek / Tavily / "
            "Firecrawl / Keenable / Exa / SearXNG / agy / grok / 自定义），按顺序自动降级；"
            "未配置任何通道时用 DeepSeek（有 key 才参与）→ Tavily → Firecrawl → Keenable "
            "的默认链。"
        )
        .risk(RiskLow)
        .tags("web")
        .depends_on("web_extract")
        .string("query", "搜索查询。", required())
        .integer("max_results", "最大结果数，默认 5，范围 1 到 20。")
        .string(
            "search_depth",
            "搜索深度，默认 basic；可选 basic、fast、ultra-fast、advanced。"
            "advanced 更贵但更精确。",
        )
        .string("topic", "搜索类别，默认 general；可选 general、news、finance。")
        .string("time_range", "时间范围；可选 day、week、month、year、d、w、m、y。")
        .string_array("include_domains", "只包含这些域名的结果。")
        .string_array("exclude_domains", "排除这些域名的结果。")
        .string("country", "结果区域偏好，仅 topic=general 时有效，例如 china、united states。")
        .boolean("exact_match", "是否只返回包含查询中引号短语的精确匹配结果。")
    )


def parse_web_search_args(raw: str) -> WebSearchArgs:
    """对应 Go ``json.Unmarshal`` 到 ``webSearchArgs``。"""
    if not raw:
        return WebSearchArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse web_search arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse web_search arguments: expected a JSON object")
    args = WebSearchArgs()
    args.query = _as_str(data.get("query"))
    args.search_depth = _as_str(data.get("search_depth"))
    args.max_results = _as_int(data.get("max_results"))
    args.topic = _as_str(data.get("topic"))
    args.time_range = _as_str(data.get("time_range"))
    args.include_domains = _as_str_list(data.get("include_domains"))
    args.exclude_domains = _as_str_list(data.get("exclude_domains"))
    args.country = _as_str(data.get("country"))
    args.exact_match = bool(data.get("exact_match"))
    return args


def normalize_tavily_request(args: WebSearchArgs, query: str) -> TavilyRequest:
    """对应 Go ``normalizeTavilyRequest``。"""
    max_results = args.max_results
    if max_results <= 0:
        max_results = DEFAULT_SEARCH_RESULTS
    if max_results > 20:
        max_results = 20
    depth = (args.search_depth or "").strip() or DEFAULT_SEARCH_DEPTH
    return TavilyRequest(
        query=query,
        search_depth=depth,
        max_results=max_results,
        topic=(args.topic or "").strip(),
        time_range=(args.time_range or "").strip(),
        include_answer=True,
        include_raw_content=False,
        include_images=False,
        include_image_descriptions=False,
        include_favicon=False,
        include_usage=False,
        auto_parameters=False,
        include_domains=trim_string_list(args.include_domains),
        exclude_domains=trim_string_list(args.exclude_domains),
        country=(args.country or "").strip(),
        exact_match=args.exact_match,
    )


def parse_tavily_response(payload: Any) -> TavilyResponse:
    """把响应 JSON 映射到 :class:`TavilyResponse`。"""
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    results: list[TavilyResult] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            TavilyResult(
                title=_as_str(item.get("title")),
                url=_as_str(item.get("url")),
                content=_as_str(item.get("content")),
                score=float(item.get("score") or 0.0),
            )
        )
    return TavilyResponse(
        query=_as_str(payload.get("query")),
        answer=_as_str(payload.get("answer")),
        results=results,
        response_time=float(payload.get("response_time") or 0.0),
        request_id=_as_str(payload.get("request_id")),
    )


def format_tavily_content(data: TavilyResponse) -> str:
    """对应 Go ``formatTavilyContent``。"""
    out: list[str] = []
    if data.answer != "":
        out.append("Answer: ")
        out.append(data.answer)
        out.append("\n\n")
    for position, result in enumerate(data.results):
        if position > 0:
            out.append("\n")
        out.append(f"{position + 1}. {result.title}\n")
        out.append(result.url)
        if result.content != "":
            out.append("\n")
            out.append(result.content)
        out.append("\n")
    return "".join(out).strip()


def format_search_content(outcome: SearchOutcome) -> str:
    """把免注册通道的结果排成与 Tavily 通道一致的文本。"""
    out: list[str] = []
    if outcome.answer != "":
        out.append("Answer: ")
        out.append(outcome.answer)
        out.append("\n\n")
    for position, hit in enumerate(outcome.hits):
        if position > 0:
            out.append("\n")
        out.append(f"{position + 1}. {hit.title}\n")
        out.append(hit.url)
        if hit.snippet != "":
            out.append("\n")
            out.append(hit.snippet)
        out.append("\n")
    text = "".join(out).strip()
    return text if text != "" else "没有搜索到结果。"


def trim_string_list(values: list[str] | None) -> list[str]:
    """对应 Go ``trimStringList``。"""
    out: list[str] = []
    for value in values or []:
        value = (value or "").strip()
        if value != "":
            out.append(value)
    return out


def _lookup_env(environment: Environment | None, ctx: Any, key: str) -> str:
    """按 Go 的 ``processenv.Environment.Lookup`` 优先，再回落配置目录 ``.env``。

    未配置的 ``Environment`` 本身就读 ``os.environ``（与 Go 一致）；额外的
    ``optional_builtin_env`` 回落让 ``config/.env`` 里的 key 也能生效。
    """
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


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_as_str(item) for item in value]


__all__ = [name for name in dir() if not name.startswith("_")]
