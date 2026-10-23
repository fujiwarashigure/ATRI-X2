"""共享只读服务配置：``[providers.*]`` / ``[model_metadata]`` / ``[model_profiles]`` /
``[image_generation]`` / ``[image_to_prompt]`` / ``[vision]`` / ``[asr]`` /
``[web_search]`` / ``[web_extract]``。

对应 Go：``internal/config/config.go`` 里的 ``ProviderConfig`` / ``ModelConfig`` /
``ModelMetadataConfig`` / ``ModelProfileConfig`` / ``ServicesConfig`` /
``ImageGenerationConfig(ProfileConfig)`` / ``ImageToPromptConfig`` / ``VisionConfig`` /
``ASRConfig``，以及 ``ParseAPIProtocol`` / ``VisionSupport`` / ``AudioSupport``。
``[web_extract]`` 是 Python 版新增的段（Go 的 ``web_extract`` 没有通道配置）。

分工（与 :mod:`atri_x.config.app` 的分界）：本模块只管**多个进程共享的只读服务配置**，
它们来自 ``[config_files].services``（新布局 ``services.toml``）或旧的 ``providers.toml``；
``state.toml`` 运行态见 :mod:`atri_x.config.state`，app.toml 静态行为段见
:mod:`atri_x.config.app_sections`。

旧部署兼容：``providers.toml`` 用 :func:`atri_x.config.loader.load_providers_config` 读，
只取 ``providers`` 与 ``model_metadata`` 两段（与 Go ``mergeProviders`` 一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from atri_x.config.app_sections import (
    ArtistLibraryConfig,
    ConfigDecodeError,
    StrictConfig,
    load_toml,
)

__all__ = [
    "APIProtocol",
    "ASRConfig",
    "AudioSupport",
    "DEFAULT_CONTEXT_WINDOW",
    "ImageGenerationConfig",
    "ImageGenerationProfileConfig",
    "ImageToPromptConfig",
    "ModelConfig",
    "ModelMetadataConfig",
    "ModelProfileConfig",
    "ProviderConfig",
    "REASONING_EFFORT_VALUES",
    "RESERVED_HEADER_NAMES",
    "ServicesConfig",
    "VisionConfig",
    "VisionSupport",
    "WebSearchConfig",
    "WebSearchEngineConfig",
    "WebExtractConfig",
    "WebExtractEngineConfig",
    "audio_support_from_bool",
    "load_services_config",
    "normalize_reasoning_effort",
    "parse_api_protocol",
    "validate_provider_extra_headers",
    "vision_support_from_bool",
]

#: ``[model_metadata].default_context_window`` 的内置默认值。对应 Go ``DefaultContextWindow``。
DEFAULT_CONTEXT_WINDOW = 256000


class VisionSupport(IntEnum):
    """图像输入能力的三态声明。对应 Go ``VisionSupport``。"""

    UNKNOWN = 0
    SUPPORTED = 1
    UNSUPPORTED = 2


class AudioSupport(IntEnum):
    """语音转写能力的三态声明。对应 Go ``AudioSupport``。"""

    UNKNOWN = 0
    SUPPORTED = 1
    UNSUPPORTED = 2


class APIProtocol(IntEnum):
    """OpenAI 兼容上游的协议。对应 Go ``APIProtocol``。"""

    CHAT = 0
    RESPONSES = 1


def vision_support_from_bool(supported: bool) -> VisionSupport:
    return VisionSupport.SUPPORTED if supported else VisionSupport.UNSUPPORTED


def audio_support_from_bool(supported: bool) -> AudioSupport:
    return AudioSupport.SUPPORTED if supported else AudioSupport.UNSUPPORTED


def parse_api_protocol(raw: str) -> tuple[APIProtocol, bool]:
    """解析 ``api_mode``。返回 ``(协议, 是否被识别)``；未识别时不静默改写配置。

    规范值是 ``chat`` / ``response``；``chat_completions`` / ``responses_api`` 等
    拼写按 Go ``ParseAPIProtocol`` 一并接受。
    """
    value = (raw or "").strip().lower()
    if value in ("", "chat", "chat_completions", "chat-completions"):
        return APIProtocol.CHAT, True
    if value in ("response", "responses", "responses_api", "responses-api"):
        return APIProtocol.RESPONSES, True
    return APIProtocol.CHAT, False


#: ``reasoning_effort`` 允许的取值（OpenAI 兼容上游的通用思考强度档位）。
#:
#: 语义是"把值原样放进请求 JSON 的 ``reasoning_effort`` 字段"，因此只收各家都认的
#: 通用档位；个别网关的私有取值请走 ``extra_payload``（那里不做白名单）。
REASONING_EFFORT_VALUES: tuple[str, ...] = ("none", "minimal", "low", "medium", "high")


def normalize_reasoning_effort(raw: str) -> tuple[str, bool]:
    """解析 ``reasoning_effort``，返回 ``(归一化值, 是否被识别)``。

    空串（含仅空白）表示**未配置**：请求里不发送该字段，``(空串, True)``。
    非空值大小写不敏感，必须属于 :data:`REASONING_EFFORT_VALUES`；不认识的写法返回
    ``(原值, False)``——启动期校验与桌面端保存前校验都用这一个判定，避免出现
    「界面存得进去、后台服务起不来」或反过来的配置。
    """
    value = (raw or "").strip().lower()
    if not value:
        return "", True
    if value in REASONING_EFFORT_VALUES:
        return value, True
    return value, False


@dataclass
class ModelConfig(StrictConfig):
    """``[providers.<name>.model_configs.<model>]``。"""

    context_window: int = 0
    extra_payload: dict[str, Any] = field(default_factory=dict)
    vision: bool | None = None
    audio: bool | None = None
    api_mode: str = ""
    #: 推理思考强度（``none`` / ``minimal`` / ``low`` / ``medium`` / ``high``）。
    #: 空串 = 不发送 ``reasoning_effort``；模型级覆盖 Provider 级。
    reasoning_effort: str = ""


@dataclass
class ProviderConfig(StrictConfig):
    """``[providers.<name>]``。

    ``vision`` / ``audio`` 是**指针语义**（``None`` = 未声明，与显式 ``false`` 不同）。
    密钥解析优先级见 :func:`atri_x.config.loader.config_env`：系统环境变量 > 配置目录
    ``.env``；已显式写出 ``api_key`` 时不再解析 ``api_key_env``。
    """

    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    proxy: str = ""
    models: list[str] = field(default_factory=list)
    model_configs: dict[str, ModelConfig] = field(default_factory=dict)
    extra_payload: dict[str, Any] = field(default_factory=dict)
    #: Provider 级自定义请求头。用于中转/网关要求的专有头（如 OpenCode Go 的
    #: ``x-opencode-session``）；``Authorization`` / ``Content-Type`` 等保留头
    #: 始终由适配器独占，写在这里会被忽略。
    extra_headers: dict[str, str] = field(default_factory=dict)
    fallback_provider: str = ""
    fallback_model: str = ""
    vision: bool | None = None
    audio: bool | None = None
    fallback_mode: str = ""
    fallback_on_error: bool = False
    fallback_timeout_seconds: int = 0
    api_mode: str = ""
    #: Provider 级推理思考强度（写进请求 JSON 的 ``reasoning_effort``）。
    #: 空串 = 不发送；``[providers.<name>.model_configs.<model>].reasoning_effort``
    #: 可按模型覆盖。桌面端「模型」页直接编辑 Provider 级这一项。
    reasoning_effort: str = ""

    def uses_fallback_on_error(self) -> bool:
        """首个流前错误即切备用 Provider（而不是等熔断打开）。对应 Go ``UsesFallbackOnError``。"""
        if self.fallback_on_error:
            return True
        return self.fallback_mode.strip().lower() in (
            "on_error",
            "first_error",
            "immediate",
            "error",
        )

    def vision_support(self, model: str) -> VisionSupport:
        """模型级声明覆盖 Provider 级默认。对应 Go ``VisionSupport``。"""
        model_config = self.model_configs.get(model)
        if model_config is not None and model_config.vision is not None:
            return vision_support_from_bool(model_config.vision)
        if self.vision is not None:
            return vision_support_from_bool(self.vision)
        return VisionSupport.UNKNOWN

    def audio_support(self, model: str) -> AudioSupport:
        model_config = self.model_configs.get(model)
        if model_config is not None and model_config.audio is not None:
            return audio_support_from_bool(model_config.audio)
        if self.audio is not None:
            return audio_support_from_bool(self.audio)
        return AudioSupport.UNKNOWN

    def api_protocol_for(self, model: str) -> APIProtocol:
        """模型级 ``api_mode`` 覆盖 Provider 级；两者都默认 ``chat``。对应 Go ``APIProtocolFor``。"""
        model_config = self.model_configs.get(model)
        if model_config is not None and model_config.api_mode.strip():
            protocol, ok = parse_api_protocol(model_config.api_mode)
            if ok:
                return protocol
        protocol, ok = parse_api_protocol(self.api_mode)
        return protocol if ok else APIProtocol.CHAT

    def uses_responses_api(self) -> bool:
        """该 Provider 是否需要 Responses 客户端。对应 Go ``UsesResponsesAPI``。"""
        protocol, ok = parse_api_protocol(self.api_mode)
        if ok and protocol is APIProtocol.RESPONSES:
            return True
        for model_config in self.model_configs.values():
            if not model_config.api_mode.strip():
                continue
            protocol, ok = parse_api_protocol(model_config.api_mode)
            if ok and protocol is APIProtocol.RESPONSES:
                return True
        return False

    def reasoning_effort_for(self, model: str) -> str:
        """该模型生效的推理强度：模型级覆盖 Provider 级，空串 = 不发送。

        非法值（启动期校验会先拒绝）在这里按"未配置"处理，避免把错值发到上游。
        """
        model_config = self.model_configs.get(model)
        if model_config is not None:
            value, ok = normalize_reasoning_effort(model_config.reasoning_effort)
            if ok and value:
                return value
        value, ok = normalize_reasoning_effort(self.reasoning_effort)
        return value if ok else ""


#: Provider 自定义请求头里**不允许**覆盖的保留头：它们由适配器按协议自己构造，
#: 交给配置覆盖会让 ``Authorization`` / ``Content-Type`` 这类语义失效。
RESERVED_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "content-type",
        "content-length",
        "host",
        "connection",
        "transfer-encoding",
        "accept-encoding",
    }
)


def validate_provider_extra_headers(name: str, extra_headers: Any) -> None:
    """校验一组 Provider 自定义请求头；非法时抛 :class:`ConfigDecodeError`。

    启动期（:meth:`atri_x.config.app.AppConfig.validate_provider_headers`）与桌面端
    「模型」页保存 provider 前用的是**同一套规则**，避免"界面存得进去、启动却报错"。
    """
    for raw_name, raw_value in (extra_headers or {}).items():
        header = str(raw_name or "").strip()
        value = str(raw_value or "")
        if not header or any(char.isspace() for char in header) or ":" in header:
            raise ConfigDecodeError(
                f"[providers.{name}].extra_headers has an invalid header name {header!r}"
            )
        if header.lower() in RESERVED_HEADER_NAMES:
            raise ConfigDecodeError(
                f"[providers.{name}].extra_headers cannot override the reserved header "
                f"{header!r}"
            )
        if "\r" in value or "\n" in value:
            raise ConfigDecodeError(
                f"[providers.{name}].extra_headers[{header!r}] contains a line break"
            )


@dataclass
class ModelMetadataConfig(StrictConfig):
    default_context_window: int = DEFAULT_CONTEXT_WINDOW


@dataclass
class ModelProfileConfig(StrictConfig):
    """``[model_profiles.<name>]``：``@model:<别名>`` 用的命名模型档。"""

    provider: str = ""
    model: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class ImageGenerationProfileConfig(StrictConfig):
    """``[image_generation.profiles.<name>]``：空串 / ``None`` 表示继承基础配置。"""

    enabled: bool | None = None
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
    preset_prompt: str | None = None
    negative_prompt: str | None = None
    superadmin_only: bool | None = None
    save_to_character: bool | None = None
    send_by_default: bool | None = None
    supports_reference: bool | None = None
    reference_field: str = ""
    model_family: str = ""
    prompt_dialect: str = ""
    negative_prompt_field: str = ""
    negative_prompt_mode: str = ""
    artist_default_preset: str = ""
    extra_payload: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class ImageGenerationConfig(StrictConfig):
    enabled: bool = False
    default_profile: str = ""
    profiles: dict[str, ImageGenerationProfileConfig] = field(default_factory=dict)
    base_url: str = ""
    endpoint: str = ""
    api_key: str = ""
    api_key_env: str = "IMAGE_API_KEY"
    model: str = "gpt-image-2.5"
    size: str = "1024x1024"
    quality: str = "high"
    output_format: str = "png"
    response_format: str = ""
    timeout_seconds: int = 180
    preset_prompt: str = ""
    negative_prompt: str = ""
    max_prompt_runes: int = 4000
    optimize: str = "rules"
    optimize_term_mode: str = "phrase"
    optimize_max_anchors: int = 4
    optimize_max_negatives: int = 10
    optimize_max_added_runes: int = 400
    optimize_max_tags: int = 12
    optimize_rewrite: str = "auto"
    optimize_rewrite_model: str = "naming"
    optimize_rewrite_min_runes: int = 40
    auto_character: bool | None = None
    auto_context: bool | None = None
    context_default_limit: int = 6
    superadmin_only: bool | None = None
    save_to_character: bool | None = None
    send_by_default: bool = False
    supports_reference: bool = False
    reference_field: str = "image"
    model_family: str = ""
    prompt_dialect: str = ""
    negative_prompt_field: str = ""
    negative_prompt_mode: str = ""
    artist_default_preset: str = ""
    artist_library: ArtistLibraryConfig = field(default_factory=ArtistLibraryConfig)
    metadata_store: str = ""
    extra_payload: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: str = ""
    max_concurrent: int = 0
    queue_size: int = 0
    queue_timeout_seconds: int = 0

    def is_superadmin_only(self) -> bool:
        return self.superadmin_only is None or self.superadmin_only

    def is_auto_character(self) -> bool:
        return self.auto_character is None or self.auto_character

    def is_auto_context(self) -> bool:
        return self.auto_context is None or self.auto_context

    def is_save_to_character(self) -> bool:
        return self.save_to_character is None or self.save_to_character

    def default_profile_name(self) -> str:
        return self.default_profile.strip()

    def resolve_profile(self, name: str) -> ImageGenerationConfig | None:
        """按名字解析生效配置；``name`` 为空返回基础配置，名字不存在返回 ``None``。

        对应 Go ``ResolveProfile``（Go 返回 ``(零值, false)``，这里用 ``None`` 表达未命中）。
        返回值的 ``profiles`` 已被清空，避免拿到递归结构。
        """
        wanted = (name or "").strip()
        if wanted == "":
            return self.copy()
        profile = self.profiles.get(wanted)
        if profile is None:
            return None
        out = self.copy()
        out.profiles = {}
        if profile.enabled is not None:
            out.enabled = profile.enabled
        for name_attr in (
            "base_url",
            "endpoint",
            "api_key",
            "api_key_env",
            "model",
            "size",
            "quality",
            "output_format",
            "response_format",
            "reference_field",
            "model_family",
            "prompt_dialect",
            "negative_prompt_field",
            "negative_prompt_mode",
            "artist_default_preset",
            "proxy",
        ):
            value = getattr(profile, name_attr)
            if value != "":
                setattr(out, name_attr, value)
        if profile.timeout_seconds > 0:
            out.timeout_seconds = profile.timeout_seconds
        if profile.preset_prompt is not None:
            out.preset_prompt = profile.preset_prompt
        if profile.negative_prompt is not None:
            out.negative_prompt = profile.negative_prompt
        if profile.superadmin_only is not None:
            out.superadmin_only = profile.superadmin_only
        if profile.save_to_character is not None:
            out.save_to_character = profile.save_to_character
        if profile.send_by_default is not None:
            out.send_by_default = profile.send_by_default
        if profile.supports_reference is not None:
            out.supports_reference = profile.supports_reference
        if profile.extra_payload:
            out.extra_payload = profile.extra_payload
        if profile.extra_headers:
            out.extra_headers = profile.extra_headers
        return out


@dataclass
class ImageToPromptConfig(StrictConfig):
    """``[image_to_prompt]``：复用已有 ``[providers.*]`` 的视觉模型。"""

    enabled: bool | None = None
    provider: str = ""
    model: str = ""
    max_tokens: int = 400
    temperature: float | None = None
    max_edge: int = 1536
    max_image_bytes: int = 12 * 1024 * 1024
    timeout_seconds: int = 90

    def temperature_value(self) -> float:
        """未设置时 0.2；显式 0 会被保留（适配器据此不覆盖上游温度）。"""
        return 0.2 if self.temperature is None else self.temperature

    def is_enabled(self) -> bool:
        """显式 ``enabled`` 优先；否则配好 provider+model 即视为启用。"""
        if self.enabled is not None:
            return self.enabled
        return self.provider.strip() != "" and self.model.strip() != ""


@dataclass
class VisionConfig(StrictConfig):
    """``[vision]``：文本模型拒绝图片时的自动视觉兜底（默认关闭，避免隐性费用）。"""

    enabled: bool | None = None
    provider: str = ""
    model: str = ""
    max_tokens: int = 400
    temperature: float | None = None
    max_edge: int = 1536
    max_image_bytes: int = 12 * 1024 * 1024
    timeout_seconds: int = 90
    language: str = ""
    cache_ttl_seconds: int = 1800
    negative_cache_ttl_seconds: int = 30

    def is_enabled(self) -> bool:
        return self.enabled is not None and self.enabled

    def temperature_value(self) -> float:
        return 0.2 if self.temperature is None else self.temperature

    def language_value(self) -> str:
        """归一化为 ``zh`` / ``en``。对应 Go ``LanguageValue``。"""
        return "en" if self.language.strip().lower() == "en" else "zh"


@dataclass
class ASRConfig(StrictConfig):
    """``[asr]``：语音转写，复用已有 ``[providers.*]``（默认关闭）。"""

    enabled: bool | None = None
    provider: str = ""
    model: str = ""
    language: str = ""
    prompt: str = ""
    timeout_seconds: int = 120
    max_audio_bytes: int = 20 * 1024 * 1024
    max_concurrent: int = 10
    queue_size: int = 8
    max_segments: int = 4
    cache_ttl_seconds: int = 1800
    cache_max_entries: int = 128
    negative_cache_ttl_seconds: int = 30
    negative_cache_max_entries: int = 128
    max_retries: int = 2
    retry_initial_delay_seconds: int = 1

    def is_enabled(self) -> bool:
        return self.enabled is not None and self.enabled

    def normalized(self) -> ASRConfig:
        """补齐文档化默认值并去掉首尾空白。对应 Go ``Normalized``。"""
        out = self.copy()
        out.provider = out.provider.strip()
        out.model = out.model.strip()
        out.language = out.language.strip()
        out.prompt = out.prompt.strip()
        for name, fallback in (
            ("timeout_seconds", 120),
            ("max_audio_bytes", 20 * 1024 * 1024),
            ("max_concurrent", 10),
            ("queue_size", 8),
            ("max_segments", 4),
            ("cache_ttl_seconds", 1800),
            ("cache_max_entries", 128),
            ("negative_cache_ttl_seconds", 30),
            ("negative_cache_max_entries", 128),
            ("max_retries", 2),
            ("retry_initial_delay_seconds", 1),
        ):
            if getattr(out, name) <= 0:
                setattr(out, name, fallback)
        return out


@dataclass
class WebSearchEngineConfig(StrictConfig):
    """``[web_search.engines.<name>]``：一条搜索通道。

    ``protocol`` 决定用哪个实现（``deepseek`` / ``tavily`` / ``firecrawl`` /
    ``keenable`` / ``exa`` / ``searxng`` / ``antigravity`` / ``grok``）；内置名留空时
    等于名字，自定义名字必须显式写 ``protocol``。``bin`` 只给 ``antigravity`` / ``grok``
    这类外部 CLI 通道覆盖可执行文件名。
    """

    protocol: str = ""
    api_key_env: str = ""
    base_url: str = ""
    bin: str = ""


@dataclass
class WebSearchConfig(StrictConfig):
    """``[web_search]``：``web_search`` 工具的搜索通道与尝试顺序。

    ``engine_order`` 里的名字按顺序逐条尝试；``engines`` 里没有列出的内置通道不参与。
    整段缺席时回落到「DeepSeek（有 key 才参与）→ Tavily → Firecrawl → Keenable → Exa
    （有 key 才参与）」的环境变量默认链，保持旧部署行为不变。
    """

    engine_order: list[str] = field(default_factory=list)
    engines: dict[str, WebSearchEngineConfig] = field(default_factory=dict)


@dataclass
class WebExtractEngineConfig(StrictConfig):
    """``[web_extract.engines.<name>]``：一条抓取通道。

    ``protocol`` 决定用哪个实现（``deepseek`` / ``jina`` / ``direct``）；内置名留空时
    等于名字，自定义名字必须显式写 ``protocol``。``base_url`` 是完整接口地址
    （DeepSeek 的 ``/v1/tools/fetch`` 或 Jina Reader 的根地址），留空用官方端点。
    """

    protocol: str = ""
    api_key_env: str = ""
    base_url: str = ""


@dataclass
class WebExtractConfig(StrictConfig):
    """``[web_extract]``：``web_extract`` 工具的抓取通道与尝试顺序。

    ``engine_order`` 里的名字按顺序逐条尝试，单条失败自动换下一条。整段缺席时回落到
    「Jina（有 ``JINA_API_KEY`` 才参与）→ 直接抓取」的默认链，与 0.9.9 的既有行为一致；
    ``deepseek`` 通道按次计费，不在默认链里，要显式写进 ``engine_order`` 才启用。
    """

    engine_order: list[str] = field(default_factory=list)
    engines: dict[str, WebExtractEngineConfig] = field(default_factory=dict)


@dataclass
class ServicesConfig(StrictConfig):
    """整个 ``services.toml``。

    刻意不含运行态：``state.toml`` 由 ATRI-X 运行期回写，必须独立成文件。
    ``None`` 表示该段在文件里缺席，保留从 app.toml / 旧 providers.toml 读到的值
    （对应 Go ``mergeServices`` 的逐段覆盖）。
    """

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    model_metadata: ModelMetadataConfig | None = None
    model_profiles: dict[str, ModelProfileConfig] = field(default_factory=dict)
    image_generation: ImageGenerationConfig | None = None
    image_to_prompt: ImageToPromptConfig | None = None
    vision: VisionConfig | None = None
    asr: ASRConfig | None = None
    web_search: WebSearchConfig | None = None
    web_extract: WebExtractConfig | None = None


def load_services_config(path: str, *, strict: bool = True) -> ServicesConfig:
    """严格读取 ``services.toml``。"""
    config = load_toml(path, ServicesConfig, strict=strict)
    assert isinstance(config, ServicesConfig)
    return config
