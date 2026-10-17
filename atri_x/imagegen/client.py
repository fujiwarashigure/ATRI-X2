"""OpenAI 兼容生图客户端。

对应 Go：``internal/imagegen/client.go``。

端点形如 ``{base_url}/images/generations``（``endpoint`` 显式配置时以它为准，
且不再追加路径），支持参考图（``data:<mime>;base64,...``，单张为字符串、多张
为数组）、``data[].b64_json`` 与 ``data[].url`` 两种回包。

``Config.normalize()`` 会剔除 ``extra_payload`` / ``extra_headers`` 里试图覆盖
ATRI-X 自建字段的键，结果记在 ``dropped_extra_payload`` /
``dropped_extra_headers``：静默忽略一条 ``extra_payload`` 与「后端不理提示词」
在现象上完全一样，所以拒绝必须在启动时能被记一次日志。

负向提示词按 ``negative_prompt_mode`` 分三种：``field``（默认，独立 JSON 字段）、
``inline``（调用方折进正向提示词）、``off``（丢弃）。折进 / 丢弃由持有正向
提示词的调用方决定，本客户端只负责 ``field`` 这一支。
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from atri_x.errors import AtriXError
from atri_x.net.httpclient import HTTPClient, HTTPError

__all__ = [
    "DEFAULT_MAX_PROMPT",
    "DEFAULT_MODEL",
    "DEFAULT_NEGATIVE_PROMPT_FIELD",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_QUALITY",
    "DEFAULT_SIZE",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_IMAGE_BYTES",
    "MAX_RESPONSE_BYTES",
    "Client",
    "Config",
    "ImageGenError",
    "NegativePromptMode",
    "NegativePromptModeField",
    "NegativePromptModeInline",
    "NegativePromptModeOff",
    "Reference",
    "Request",
    "Result",
    "filter_reserved_headers",
    "filter_reserved_payload",
    "first_name",
    "first_non_empty",
    "normalize_negative_prompt_mode",
    "read_error_body",
    "reserved_payload_fields",
]

DEFAULT_MODEL = "gpt-image-2.5"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "high"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_PROMPT = 4000
#: 响应体上限（Go ``maxResponseBytes``）。
MAX_RESPONSE_BYTES = 20 << 20
#: 单张图片上限（Go ``maxImageBytes``）。
MAX_IMAGE_BYTES = 20 << 20
#: 错误响应体最多读多少字节（Go ``readErrorBody`` 的 4096）。
ERROR_BODY_LIMIT = 4096
#: 非 2xx 响应体转文本时最多保留多少字符。
ERROR_TEXT_LIMIT = 500

#: 负向提示词默认走独立 JSON 字段。
DEFAULT_NEGATIVE_PROMPT_FIELD = "negative_prompt"
#: 未声明方言时的默认值：OpenAI 兼容端点就是自然语言目标。
DEFAULT_PROMPT_DIALECT = "natural"
#: 未声明模型族时的默认值。
DEFAULT_MODEL_FAMILY = "general"


class ImageGenError(AtriXError):
    """生图客户端错误。对应 Go 里 ``fmt.Errorf`` 返回的各类生图错误。"""


class NegativePromptMode(StrEnum):
    """负向提示词如何抵达后端。对应 Go ``NegativePromptMode``。"""

    #: 放在自己的 JSON 字段里（默认）。折进正向提示词会让带权重的 tag 模型
    #: 把负面词当成「要画的东西」。
    FIELD = "field"
    #: 折进正向提示词，作为 ``avoid:`` 从句；纯自然语言目标只认得这种。
    INLINE = "inline"
    #: 直接丢弃，并报告丢弃了几条。
    OFF = "off"


#: Go 常量名的交叉检索别名。
NegativePromptModeField = NegativePromptMode.FIELD
NegativePromptModeInline = NegativePromptMode.INLINE
NegativePromptModeOff = NegativePromptMode.OFF


def normalize_negative_prompt_mode(value: Any) -> NegativePromptMode:
    """把任意值收敛到模式枚举。对应 Go ``NormalizeNegativePromptMode``。"""
    text = str(value or "").strip().lower()
    if text == NegativePromptModeInline.value:
        return NegativePromptModeInline
    if text == NegativePromptModeOff.value:
        return NegativePromptModeOff
    return NegativePromptModeField


#: 便于交叉检索的 Go 名别名。
NormalizeNegativePromptMode = normalize_negative_prompt_mode


@dataclass(slots=True)
class Config:
    """归一化后的生图配置。对应 Go ``Config``。

    字段顺序与 Go 结构体一致，便于交叉检索；``dropped_extra_payload`` /
    ``dropped_extra_headers`` 由 :meth:`normalize` 填充。
    """

    enabled: bool = False
    base_url: str = ""
    endpoint: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    size: str = ""
    quality: str = ""
    output_format: str = ""
    response_format: str = ""
    timeout_seconds: int = 0
    preset_prompt: str = ""
    negative_prompt: str = ""
    max_prompt_runes: int = 0
    optimize: str = ""
    optimize_term_mode: str = ""
    optimize_max_anchors: int = 0
    optimize_max_negatives: int = 0
    optimize_max_added_runes: int = 0
    optimize_max_tags: int = 0
    optimize_rewrite: str = ""
    optimize_rewrite_model: str = ""
    optimize_rewrite_min_runes: int = 0
    auto_character: bool = False
    auto_context: bool = False
    context_default_limit: int = 0
    superadmin_only: bool = False
    save_to_character: bool = False
    send_by_default: bool = False
    supports_reference: bool = False
    reference_field: str = ""
    negative_prompt_field: str = ""
    negative_prompt_mode: NegativePromptMode | str = ""
    prompt_dialect: str = ""
    model_family: str = ""
    artist_default_preset: str = ""
    extra_payload: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: str = ""
    #: 因覆盖 ATRI-X 自建字段而被拒绝的 ``extra_payload`` 键（已排序）。
    dropped_extra_payload: list[str] = field(default_factory=list)
    #: 同上，``extra_headers`` 的。
    dropped_extra_headers: list[str] = field(default_factory=list)

    def normalize(self) -> "Config":
        """补齐默认值并剔除被保留字段，返回**新的** Config。

        对应 Go ``Config.Normalize``（值接收者，调用方的结构体不被改动）。
        Go 的 map 字段是引用，Python 的浅拷贝同样是引用，语义一致。
        """
        out = replace(self)
        if not _trim(out.model):
            out.model = DEFAULT_MODEL
        if not _trim(out.size):
            out.size = DEFAULT_SIZE
        if not _trim(out.quality):
            out.quality = DEFAULT_QUALITY
        if not _trim(out.output_format):
            out.output_format = DEFAULT_OUTPUT_FORMAT
        if out.timeout_seconds <= 0:
            out.timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if out.max_prompt_runes <= 0:
            out.max_prompt_runes = DEFAULT_MAX_PROMPT
        if not _trim(out.reference_field):
            out.reference_field = "image"
        if not _trim(out.negative_prompt_field):
            out.negative_prompt_field = DEFAULT_NEGATIVE_PROMPT_FIELD
        if not _trim(out.negative_prompt_mode):
            out.negative_prompt_mode = NegativePromptModeField
        else:
            out.negative_prompt_mode = normalize_negative_prompt_mode(out.negative_prompt_mode)
        if not _trim(out.prompt_dialect):
            out.prompt_dialect = DEFAULT_PROMPT_DIALECT
        if not _trim(out.model_family):
            out.model_family = DEFAULT_MODEL_FAMILY
        if not _trim(out.api_key_env):
            out.api_key_env = "IMAGE_API_KEY"
        if not _trim(out.optimize):
            out.optimize = "rules"
        if out.optimize_max_anchors <= 0:
            out.optimize_max_anchors = 4
        if out.optimize_max_negatives <= 0:
            out.optimize_max_negatives = 10
        if out.optimize_max_added_runes <= 0:
            out.optimize_max_added_runes = 400
        if not _trim(out.optimize_term_mode):
            out.optimize_term_mode = "phrase"
        if out.optimize_max_tags <= 0:
            out.optimize_max_tags = 12
        if not _trim(out.optimize_rewrite):
            out.optimize_rewrite = "auto"
        if not _trim(out.optimize_rewrite_model):
            out.optimize_rewrite_model = "naming"
        if out.optimize_rewrite_min_runes <= 0:
            out.optimize_rewrite_min_runes = 40
        if out.context_default_limit <= 0:
            out.context_default_limit = 6
        extra_payload, dropped_payload = filter_reserved_payload(
            out.extra_payload, out.reference_field, out.negative_prompt_field
        )
        out.extra_payload = extra_payload
        out.dropped_extra_payload = dropped_payload or []
        extra_headers, dropped_headers = filter_reserved_headers(out.extra_headers)
        out.extra_headers = extra_headers
        out.dropped_extra_headers = dropped_headers or []
        return out


def reserved_payload_fields(reference_field: str, negative_field: str) -> dict[str, None]:
    """ATRI-X 自建的 payload 键。对应 Go ``reservedPayloadFields``。"""
    reserved: dict[str, None] = {
        "model": None,
        "prompt": None,
        "n": None,
        "size": None,
        "quality": None,
        "output_format": None,
        "response_format": None,
    }
    for field_name in (reference_field, negative_field):
        text = str(field_name or "").strip().lower()
        if text:
            reserved[text] = None
    return reserved


def filter_reserved_payload(
    payload: Mapping[str, Any] | None, reference_field: str, negative_field: str
) -> tuple[dict[str, Any], list[str] | None]:
    """剔除试图替换自建字段的 ``extra_payload`` 键。

    对应 Go ``filterReservedPayload``：没有命中时原样返回入参、``dropped`` 为
    ``None``（对应 Go 的 nil slice）。
    """
    if not payload:
        return dict(payload or {}), None
    reserved = reserved_payload_fields(reference_field, negative_field)
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in payload.items():
        if str(key).strip().lower() in reserved:
            dropped.append(key)
            continue
        kept[key] = value
    if not dropped:
        return dict(payload), None
    dropped.sort()
    return kept, dropped


def filter_reserved_headers(
    headers: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[str] | None]:
    """剔除试图替换 ``Authorization`` / ``Content-Type`` 的 ``extra_headers``。

    对应 Go ``filterReservedHeaders``：一条替换 ``Authorization`` 的配置会把可用
    凭据变成 401，且没有任何其它症状。
    """
    if not headers:
        return dict(headers or {}), None
    reserved = {"authorization", "content-type"}
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in headers.items():
        if str(key).strip().lower() in reserved:
            dropped.append(key)
            continue
        kept[key] = value
    if not dropped:
        return dict(headers), None
    dropped.sort()
    return kept, dropped


@dataclass(slots=True)
class Reference:
    """一张参考图。对应 Go ``Reference``。"""

    data: bytes = b""
    mime_type: str = ""


@dataclass(slots=True)
class Request:
    """一次生图请求。对应 Go ``Request``。"""

    prompt: str = ""
    negative_prompt: str = ""
    size: str = ""
    quality: str = ""
    n: int = 0
    references: list[Reference] = field(default_factory=list)
    reference_data: bytes = b""
    reference_mime: str = ""


@dataclass(slots=True)
class Result:
    """一张生成结果。对应 Go ``Result``。"""

    data: bytes = b""
    mime_type: str = ""
    revised_prompt: str = ""
    model: str = ""
    size: str = ""


class Client:
    """并发安全的生图客户端。对应 Go ``Client``。

    ``lookup`` 用于解析 ``api_key_env`` 指定的环境变量（与 Go 一样可注入，
    例如 ``os.environ.get`` 的包装或配置层的进程环境查询）。
    """

    def __init__(
        self,
        config: Config | None = None,
        lookup: Callable[[str], tuple[str, bool]] | None = None,
    ) -> None:
        self._cfg = (config or Config()).normalize()
        self._lookup = lookup
        self._http = HTTPClient(
            "",
            timeout=float(self._cfg.timeout_seconds),
            proxy=str(self._cfg.proxy or "").strip(),
        )

    # -- 状态 ---------------------------------------------------------------

    @property
    def config(self) -> Config:
        """归一化后的配置。对应 Go ``Client.Config``。"""
        return self._cfg

    def enabled(self) -> bool:
        """生图服务是否可用。对应 Go ``Client.Enabled``。"""
        return bool(self._cfg.enabled)

    # -- 请求 ---------------------------------------------------------------

    async def generate(self, ctx: Any, request: Request) -> Result:
        """调用端点并返回第一张图片。对应 Go ``Client.Generate``。"""
        if not self._cfg.enabled:
            raise ImageGenError("生图服务未启用")
        prompt = str(request.prompt or "").strip()
        if not prompt:
            raise ImageGenError("prompt 不能为空")
        endpoint = self._endpoint()
        api_key = self._api_key()
        if not api_key:
            raise ImageGenError(
                f"生图服务缺少 API Key：请设置环境变量 {self._cfg.api_key_env} "
                "或 image_generation.api_key"
            )

        payload: dict[str, Any] = {"model": self._cfg.model, "prompt": prompt}
        # 负面提示词只有目标端有对应字段时才发过去；另外两种模式由持有正向
        # 提示词的调用方决定（也就由它负责 "avoid:" 折叠）。
        negative = str(request.negative_prompt or "").strip()
        if negative and self._cfg.negative_prompt_mode == NegativePromptModeField:
            field_name = str(self._cfg.negative_prompt_field or "").strip()
            if field_name:
                payload[field_name] = negative
        payload["n"] = request.n if request.n > 0 else 1
        size = first_non_empty(request.size, self._cfg.size)
        if size:
            payload["size"] = size
        quality = first_non_empty(request.quality, self._cfg.quality)
        if quality:
            payload["quality"] = quality
        output_format = str(self._cfg.output_format or "").strip()
        if output_format:
            payload["output_format"] = output_format
        response_format = str(self._cfg.response_format or "").strip()
        if response_format:
            payload["response_format"] = response_format

        references = list(request.references or [])
        if not references and request.reference_data:
            references = [Reference(data=request.reference_data, mime_type=request.reference_mime)]
        if self._cfg.supports_reference and references:
            values: list[str] = []
            for reference in references:
                if not reference.data:
                    continue
                mime = first_non_empty(reference.mime_type, "image/png")
                values.append(
                    "data:" + mime + ";base64," + base64.b64encode(reference.data).decode("ascii")
                )
            if len(values) == 1:
                payload[self._cfg.reference_field] = values[0]
            elif len(values) > 1:
                payload[self._cfg.reference_field] = values
        for key, value in self._cfg.extra_payload.items():
            payload[key] = value

        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        }
        for key, value in self._cfg.extra_headers.items():
            headers[key] = value
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            response = await self._http.request(
                "POST", endpoint, headers=headers, body=body, cancel=ctx
            )
        except HTTPError as exc:
            raise ImageGenError(f"生图请求失败：{exc}") from exc
        if not response.ok:
            raise ImageGenError(
                f"生图服务返回 {response.status}：{read_error_body(response.body)}"
            )

        parsed = self._parse_body(response.body)
        items = parsed.get("data") or []
        if not items:
            error = parsed.get("error")
            if isinstance(error, Mapping) and str(error.get("message") or "").strip():
                raise ImageGenError("生图服务错误：" + str(error["message"]).strip())
            raise ImageGenError("生图服务没有返回图片")
        item = items[0] if isinstance(items[0], Mapping) else {}
        data, mime_type = await self._decode_image(ctx, item)
        return Result(
            data=data,
            mime_type=mime_type,
            revised_prompt=str(item.get("revised_prompt") or "").strip(),
            model=self._cfg.model,
            size=first_name(payload.get("size")),
        )

    # -- 内部 ---------------------------------------------------------------

    def _endpoint(self) -> str:
        """对应 Go ``Client.endpoint``。"""
        raw = str(self._cfg.endpoint or "").strip()
        if raw:
            return raw
        base = str(self._cfg.base_url or "").strip().rstrip("/")
        if not base:
            raise ImageGenError("image_generation.base_url 未配置")
        if base.endswith("/images/generations"):
            return base
        return base + "/images/generations"

    def _api_key(self) -> str:
        """对应 Go ``Client.apiKey``。"""
        key = str(self._cfg.api_key or "").strip()
        if key:
            return key
        if self._lookup is not None:
            try:
                value, ok = self._lookup(self._cfg.api_key_env)
            except Exception:  # noqa: BLE001 - 环境查询失败等价于没有该变量
                return ""
            if ok:
                return str(value or "").strip()
        return ""

    def _parse_body(self, body: bytes) -> dict[str, Any]:
        if len(body) > MAX_RESPONSE_BYTES:
            raise ImageGenError(
                f"解析生图响应失败：响应超过 {MAX_RESPONSE_BYTES} bytes 上限"
            )
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ImageGenError(f"解析生图响应失败：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ImageGenError("解析生图响应失败：响应不是 JSON 对象")
        return parsed

    async def _decode_image(self, ctx: Any, item: Mapping[str, Any]) -> tuple[bytes, str]:
        """对应 Go ``Client.decodeImage``。"""
        raw = str(item.get("b64_json") or "").strip()
        if raw:
            mime_type = ""
            if raw.startswith("data:"):
                header, separator, encoded = raw.partition(",")
                if separator:
                    raw = encoded
                    trimmed = header[: -len(";base64")] if header.endswith(";base64") else header
                    mime_type = trimmed[len("data:") :]
            # Go 的 base64.StdEncoding 允许 \r\n，其它非字母表字符一律报错；
            # Python 需要 validate=True 才有同样的严格度。
            try:
                data = base64.b64decode(raw.replace("\r", "").replace("\n", ""), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageGenError(f"解码生图结果失败：{exc}") from exc
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageGenError(f"生图结果超过 {MAX_IMAGE_BYTES} bytes 上限")
            return data, first_non_empty(mime_type, self._mime_type_for_output())
        raw_url = str(item.get("url") or "").strip()
        if raw_url:
            return await self._download(ctx, raw_url)
        raise ImageGenError("生图服务返回了空图片")

    async def _download(self, ctx: Any, raw_url: str) -> tuple[bytes, str]:
        """对应 Go ``Client.download``。"""
        try:
            response = await self._http.request("GET", raw_url, cancel=ctx)
        except HTTPError as exc:
            raise ImageGenError(f"下载生图结果失败：{exc}") from exc
        if not response.ok:
            raise ImageGenError(
                "下载生图结果失败：" + _status_text(response.status)
            )
        data = response.body
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageGenError(f"生图结果超过 {MAX_IMAGE_BYTES} bytes 上限")
        mime_type = str(response.header("Content-Type") or "").strip()
        if ";" in mime_type:
            mime_type = mime_type.split(";", 1)[0].strip()
        return data, first_non_empty(mime_type, self._mime_type_for_output())

    def _mime_type_for_output(self) -> str:
        """对应 Go ``Client.mimeTypeForOutput``。"""
        value = str(self._cfg.output_format or "").strip().lower()
        if value in ("jpeg", "jpg"):
            return "image/jpeg"
        if value == "webp":
            return "image/webp"
        return "image/png"


def read_error_body(body: bytes) -> str:
    """把错误响应体读成一句可读文本。对应 Go ``readErrorBody``。"""
    data = body[:ERROR_BODY_LIMIT]
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return "空响应"
    try:
        parsed = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        error = parsed.get("error")
        if isinstance(error, Mapping) and str(error.get("message") or "").strip():
            return str(error["message"]).strip()
        if str(parsed.get("message") or "").strip():
            return str(parsed["message"]).strip()
    if len(text) > ERROR_TEXT_LIMIT:
        text = text[:ERROR_TEXT_LIMIT] + "..."
    return text


def first_non_empty(*values: Any) -> str:
    """返回第一个非空白值（已去首尾空白）。对应 Go ``firstNonEmpty``。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def first_name(value: Any) -> str:
    """把 payload 里的值渲染成字符串。对应 Go ``firstName``。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Go 用 strconv.FormatFloat(v, 'f', -1, 64)；Python 的 repr 在极大或极
        # 小时会转科学计数法，这里退回定点写法。
        text = repr(value)
        if "e" in text or "E" in text:
            return format(value, "f")
        return text[:-2] if text.endswith(".0") else text
    return ""


def _trim(value: Any) -> str:
    return str(value or "").strip()


def _status_text(status: int) -> str:
    """Go 的 ``http.Response.Status`` 形态：``"404 Not Found"``。"""
    phrase = http.client.responses.get(status, "")
    return f"{status} {phrase}".strip()
