"""``web_search``：基于 Tavily 的网页搜索。

对应 Go：``internal/tool/builtin/web_search.go``。

与 Go 的差异（仅形式）：

* ``net/http`` → :class:`atri_x.net.httpclient.HTTPClient`（统一重试/超时/流式），
  因此 ``call`` 是 ``async def``，超时以 ``HTTPClient`` 的 ``timeout`` 表达（15 秒）。
* 请求体字段名逐字照抄 Go 的 struct tag，``omitempty`` 逐字段复刻。
* Go 用 ``ctx.Err()`` 判取消；Python 用 :func:`atri_x.tool.capability.ctx_raise_if_cancelled`。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.net.httpclient import HTTPClient, HTTPError
from atri_x.processenv import Environment
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import optional_builtin_env
from atri_x.tool.capability import ctx_raise_if_cancelled
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
DEFAULT_SEARCH_RESULTS = 5
DEFAULT_SEARCH_DEPTH = "basic"
DEFAULT_SEARCH_ENDPOINT = "https://api.tavily.com/search"
SEARCH_TIMEOUT_SECONDS = 15.0
_MAX_HTTP_BODY = 2048


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
    """对应 Go ``WebSearchTool``。"""

    def __init__(
        self,
        environment: Environment | None = None,
        *,
        client: HTTPClient | None = None,
        endpoint: str = "",
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self.process_env = environment

    def name(self) -> str:
        return "web_search"

    def info(self) -> Info:
        return web_search_builder().build_info()

    def schema(self) -> ToolSchema:
        return web_search_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``WebSearchTool.Call``。"""
        ctx_raise_if_cancelled(ctx)
        args = parse_web_search_args(req.arguments)
        query = (args.query or "").strip()
        if query == "":
            raise InvalidError("query is required")
        api_key = _lookup_env(self.process_env, ctx, TAVILY_API_KEY_ENV)
        if api_key == "":
            raise InvalidError(
                f"{TAVILY_API_KEY_ENV} is required; set it in environment or config .env"
            )
        payload = normalize_tavily_request(args, query)
        client = self.http_client()
        headers = {"Authorization": "Bearer " + api_key}
        try:
            response = await client.request(
                "POST",
                self.search_endpoint(),
                headers=headers,
                json=payload.to_payload(),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except HTTPError as exc:
            raise InvalidError(f"Tavily request: {exc}") from exc
        if not response.ok():
            raise InvalidError(
                f"Tavily HTTP {response.status}: {truncate_http_body(response.body)}"
            )
        try:
            data = parse_tavily_response(response.json())
        except ValueError as exc:
            raise InvalidError(f"parse Tavily response: {exc}") from exc
        return Result(content=format_tavily_content(data))

    def http_client(self) -> HTTPClient:
        """对应 Go ``WebSearchTool.httpClient``。"""
        if self.client is not None:
            return self.client
        return HTTPClient(timeout=SEARCH_TIMEOUT_SECONDS)

    def search_endpoint(self) -> str:
        """对应 Go ``WebSearchTool.searchEndpoint``。"""
        endpoint = (self.endpoint or "").strip()
        return endpoint if endpoint != "" else DEFAULT_SEARCH_ENDPOINT


def new_web_search_tool(*environment: Environment) -> WebSearchTool:
    """对应 Go ``NewWebSearchTool``。"""
    return WebSearchTool(environment[0] if environment else None)


def web_search_builder() -> Builder:
    """对应 Go ``webSearchBuilder``。"""
    return (
        Builder("web_search")
        .description(
            "执行网页搜索，返回简洁答案、来源链接和摘要；需要完整网页内容时继续调用 "
            "web_extract。未配置 TAVILY_API_KEY 时不可用。"
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


def trim_string_list(values: list[str] | None) -> list[str]:
    """对应 Go ``trimStringList``。"""
    out: list[str] = []
    for value in values or []:
        value = (value or "").strip()
        if value != "":
            out.append(value)
    return out


def truncate_http_body(body: bytes | str) -> str:
    """对应 Go ``truncateHTTPBody``。"""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    if len(text) <= _MAX_HTTP_BODY:
        return text
    return text[:_MAX_HTTP_BODY] + "..."


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
