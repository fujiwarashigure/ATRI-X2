"""``web_extract`` 的抓取通道。

对应 Go：``internal/tool/builtin/web_extract.go`` 里的 ``fetchJina`` / ``fetchDirect``
（Go 只有「Jina → 直接抓取」两级，且没有配置段）。本模块把「一条抓取通道怎么发请求、
怎么解析」独立出来，通道列表与尝试顺序留在 :mod:`atri_x.tool.builtin.web_extract`。

协议（``[web_extract.engines.<name>].protocol`` 的取值）：

* ``deepseek``：``POST https://api.deepseek.com/v1/tools/fetch``，DeepSeek 官方网页抓取
  （Bearer 鉴权，返回页面标题 + Markdown 正文）。按成功请求计费（5 元 / 1000 次），
  所以它**不在**默认链里，要显式写进 ``engine_order`` 才启用。
* ``jina``：``GET {endpoint}/http://host/path``，Jina Reader（需要 ``JINA_API_KEY``）。
  这是 0.9.9 的默认首选通道。
* ``direct``：直接 ``GET`` 目标网页，再用纯标准库剥离 HTML 标签；不需要 key。

密钥口径与 ``web_search`` 一致：``deepseek`` 优先读 ``DEEPSEEK_SEARCH_KEY``，回落
``DEEPSEEK_API_KEY``（:func:`key_env_names`）。本模块不读环境变量：密钥、端点、超时都由
调用方传入，便于测试注入。
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from atri_x.errors import InvalidError
from atri_x.net.httpclient import HTTPError
from atri_x.tool.builtin.web_search import truncate_http_body

# -- 协议名（``[web_extract.engines.<name>].protocol`` 的取值） -----------------
PROTOCOL_DEEPSEEK = "deepseek"
PROTOCOL_JINA = "jina"
PROTOCOL_DIRECT = "direct"
#: 全部可选协议（``web_extract`` 工具与桌面端下拉共用这份清单）。
BUILTIN_PROTOCOLS = (PROTOCOL_DEEPSEEK, PROTOCOL_JINA, PROTOCOL_DIRECT)
#: 必须配 API key 才能用的协议；缺 key 的通道不参与尝试。
KEYED_PROTOCOLS = (PROTOCOL_DEEPSEEK, PROTOCOL_JINA)
#: 配置里 ``api_key_env`` 留空时按协议回落到这个环境变量名。
DEFAULT_KEY_ENV = {
    PROTOCOL_DEEPSEEK: "DEEPSEEK_SEARCH_KEY",
    PROTOCOL_JINA: "JINA_API_KEY",
}
#: 首选密钥环境变量都没配时，再回落这些。
FALLBACK_KEY_ENV = {
    PROTOCOL_DEEPSEEK: "DEEPSEEK_API_KEY",
}
DEEPSEEK_DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
JINA_DEFAULT_ENDPOINT = "https://r.jina.ai/"


def key_env_names(protocol: str, api_key_env: str = "") -> tuple[str, ...]:
    """一条通道按优先级读取的密钥环境变量名。

    显式配置了 ``api_key_env`` 就只用它；显式配置成协议的首选名（或留空）时，
    再追加该协议的兜底环境变量（``DEEPSEEK_SEARCH_KEY`` → ``DEEPSEEK_API_KEY``）。
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


class ExtractEngineFailure(Exception):
    """单条抓取通道失败；调用方据此换下一条通道。"""


@dataclass(frozen=True)
class ExtractEngineSpec:
    """配置好的一条抓取通道（``[web_extract.engines.<name>]`` 的解析结果）。"""

    name: str
    protocol: str
    api_key_env: str = ""
    base_url: str = ""

    def key_env(self) -> str:
        """首个密钥环境变量名（CLI / 直抓通道为空）。"""
        names = key_env_names(self.protocol, self.api_key_env)
        return names[0] if names else ""

    def key_envs(self) -> tuple[str, ...]:
        """这条通道按优先级读取的全部密钥环境变量名。"""
        return key_env_names(self.protocol, self.api_key_env)


@dataclass
class ExtractPage:
    """一条通道抓到的页面（编排层再加缓存 / 分段字段）。"""

    url: str = ""
    title: str = ""
    source: str = ""
    content: str = ""


@dataclass
class JinaData:
    """Jina Reader 响应里的 ``data`` 段。对应 Go ``jinaResponseData``。"""

    url: str = ""
    title: str = ""
    content: str = ""
    timestamp: str = ""


@dataclass
class JinaResponse:
    """Jina Reader 响应。对应 Go ``jinaResponse``。"""

    url: str = ""
    title: str = ""
    content: str = ""
    timestamp: str = ""
    data: JinaData | None = None


def specs_from_config(config: Any) -> list[ExtractEngineSpec]:
    """把 ``[web_extract]`` 配置段转成通道列表；段缺席或没有顺序时返回空列表。

    内置名（``deepseek`` / ``jina`` / ``direct``）留空 ``protocol`` 时等于名字；
    自定义名字必须显式写 ``protocol``，否则该条被忽略。
    """
    order = list(getattr(config, "engine_order", []) or [])
    engines = getattr(config, "engines", {}) or {}
    if not order:
        return []
    specs: list[ExtractEngineSpec] = []
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
            ExtractEngineSpec(
                name=name,
                protocol=protocol,
                api_key_env=str(getattr(entry, "api_key_env", "") or "").strip(),
                base_url=str(getattr(entry, "base_url", "") or "").strip(),
            )
        )
    return specs


def default_specs() -> list[ExtractEngineSpec]:
    """``[web_extract]`` 缺席时的默认通道链：Jina（有 key 才参与）→ 直接抓取。

    这就是 0.9.9 的既有行为；``deepseek`` 不进默认链（按次计费，要显式配置）。
    """
    return [
        ExtractEngineSpec(
            name=name, protocol=name, api_key_env=DEFAULT_KEY_ENV.get(name, "")
        )
        for name in (PROTOCOL_JINA, PROTOCOL_DIRECT)
    ]


async def extract_deepseek(
    client: Any,
    api_key: str,
    target_url: str,
    *,
    timeout: float,
    endpoint: str = "",
) -> ExtractPage:
    """DeepSeek 官方网页抓取：``POST {base_url}/v1/tools/fetch`` → ``{title, content}``。"""
    if not api_key:
        raise ExtractEngineFailure(
            "DeepSeek 抓取通道缺少密钥（DEEPSEEK_SEARCH_KEY / DEEPSEEK_API_KEY 未配置）"
        )
    url = (endpoint or "").strip() or DEEPSEEK_DEFAULT_ENDPOINT
    try:
        response = await client.request(
            "POST",
            url,
            headers={"Authorization": "Bearer " + api_key},
            json={"url": target_url},
            timeout=timeout,
        )
    except HTTPError as exc:
        raise ExtractEngineFailure(f"DeepSeek 抓取请求失败：{exc}") from exc
    if not response.ok:
        raise ExtractEngineFailure(
            f"DeepSeek 抓取 HTTP {response.status}：{truncate_http_body(response.body)}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ExtractEngineFailure(f"解析 DeepSeek 抓取响应失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ExtractEngineFailure("解析 DeepSeek 抓取响应失败：不是一个 JSON 对象")
    content = _as_str(data.get("content"))
    if content == "":
        raise ExtractEngineFailure("DeepSeek 抓取没有返回正文（404 / 无正文页面）")
    return ExtractPage(
        url=target_url,
        title=_as_str(data.get("title")),
        source=PROTOCOL_DEEPSEEK,
        content=content,
    )


async def extract_jina(
    client: Any,
    api_key: str,
    target_url: str,
    selector: str,
    *,
    timeout: float,
    endpoint: str = "",
) -> ExtractPage:
    """Jina Reader 抓取：``GET {endpoint}/http://host/path``，需要 ``JINA_API_KEY``。"""
    if not api_key:
        raise ExtractEngineFailure("Jina 抓取通道缺少密钥（JINA_API_KEY 未配置）")
    reader_url = jina_reader_url((endpoint or "").strip() or JINA_DEFAULT_ENDPOINT, target_url)
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
        response = await client.request(
            "GET", reader_url, headers=headers, timeout=timeout
        )
    except HTTPError as exc:
        raise ExtractEngineFailure(f"Jina request: {exc}") from exc
    if not response.ok:
        raise ExtractEngineFailure(
            f"Jina HTTP {response.status}: {truncate_http_body(response.body)}"
        )
    try:
        data = _parse_jina_response(response.json())
    except ValueError as exc:
        raise ExtractEngineFailure(f"parse Jina response: {exc}") from exc
    return normalize_jina_response(data, target_url)


async def extract_direct(client: Any, target_url: str, *, timeout: float) -> ExtractPage:
    """直接 GET 目标网页并用纯标准库剥离 HTML。"""
    try:
        response = await client.request(
            "GET",
            target_url,
            headers={"User-Agent": "ATRI-X-WebExtract/1.0"},
            timeout=timeout,
        )
    except HTTPError as exc:
        raise ExtractEngineFailure(f"direct extract request: {exc}") from exc
    if not response.ok:
        raise ExtractEngineFailure(
            f"direct extract HTTP {response.status}: {truncate_http_body(response.body)}"
        )
    title, content = extract_simple_html_text(response.text())
    return ExtractPage(url=target_url, title=title, source=PROTOCOL_DIRECT, content=content)


def normalize_jina_response(data: JinaResponse, fallback_url: str) -> ExtractPage:
    """对应 Go ``normalizeJinaResponse``。"""
    if data.data is not None:
        data.url = data.data.url
        data.title = data.data.title
        data.content = data.data.content
        data.timestamp = data.data.timestamp
    if data.url == "":
        data.url = fallback_url
    return ExtractPage(
        url=data.url, title=data.title, source=PROTOCOL_JINA, content=data.content
    )


def jina_reader_url(endpoint: str, target_url: str) -> str:
    """对应 Go ``jinaReaderURL``：``<endpoint>/http://host/path?query``。

    URL 不合法是调用方错误（不是通道失败），所以抛 :class:`InvalidError`，
    与 Go 版的语义一致；它也不会触发降级。
    """
    parsed = urlsplit((target_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise InvalidError(f"invalid url: {target_url}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return endpoint.rstrip("/") + "/http://" + parsed.netloc + path


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


def _parse_jina_response(payload: Any) -> JinaResponse:
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    data = payload.get("data")
    parsed_data = None
    if isinstance(data, dict):
        parsed_data = JinaData(
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


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


__all__ = [name for name in dir() if not name.startswith("_")]
