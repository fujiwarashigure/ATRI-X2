"""视觉（图片转文本）引擎。

对应 Go：``internal/vision/``（1122 行）。它是内置 ``image_to_prompt`` 工具与
聊天视觉兜底共用的唯一描述引擎：

* :mod:`atri_x.vision.prompt` —— 四套模板（绘图提示词、结构化分析、聊天描述、
  画面属性描述）与文本清洗；前三段模板文本是逐字照抄 Go 的契约；
* :mod:`atri_x.vision.analysis` —— 结构化分析结果与画面属性的解析与限幅；
* :mod:`atri_x.vision.cache` —— 成功缓存、确定性失败负缓存、在途任务合并；
* :mod:`atri_x.vision.fingerprint` —— 带版本的缓存身份与请求指纹；
* :mod:`atri_x.vision.metrics` / :mod:`atri_x.vision.counters` —— 观测与运维计数；
* :mod:`atri_x.vision.failures` —— 失败类别（有界枚举，可安全对外）；
* :mod:`atri_x.vision.privacy` —— 图片要不要外发的两个开关；
* :mod:`atri_x.vision.identity` —— 以图搜图兜底（SauceNAO，默认关闭）；
* :mod:`atri_x.vision.service` —— 组装以上各层的 :class:`Service`。

用法::

    from atri_x.vision import Options, Request, Service, image_prompt

    service = Service(Options(client=llm, model="qwen-vl-max"))
    result = await service.describe(token, Request(media_id="media:...", data=raw,
                                                  mime_type="image/png",
                                                  prompt=image_prompt("general", "zh")))

错误分类只使用 :class:`atri_x.llm.errors.ErrorCategory` 已有的 7 个取值（外加
空串表示未分类），本包不发明新类别。
"""

from __future__ import annotations

from .analysis import (
    IDENTITY_CONFIDENCE_HIGH,
    IDENTITY_CONFIDENCE_LOW,
    IDENTITY_CONFIDENCE_MEDIUM,
    MAX_ANALYSIS_PROMPT_RUNES,
    MAX_ANALYSIS_TAGS,
    MAX_ANALYSIS_TAG_RUNES,
    MAX_ANALYSIS_WARNINGS,
    MAX_ANALYSIS_WARNING_RUNES,
    MAX_ATTRIBUTE_DESCRIPTION_RUNES,
    MAX_ATTRIBUTE_FIELD_RUNES,
    MAX_ATTRIBUTE_TEXT_RUNES,
    ImageAnalysis,
    ImageAttributes,
    analyze_attributes_text,
    attributes_summary,
    clean_analysis_list,
    extract_json_object,
    normalize_identity_confidence,
    parse_image_analysis,
    parse_image_attributes,
)
from .cache import (
    DefaultCacheMaxEntries,
    DefaultCacheTTL,
    DefaultMaxCacheValueSize,
    DefaultNegativeCacheTTL,
    DefaultNegativeMax,
    DefaultSharedTimeout,
    DetachedContext,
    Inflight,
    NegativeCache,
    SuccessCache,
    is_cancellation,
)
from .counters import Counters, Snapshot
from .failures import (
    FAILURE_CANCELLED,
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_EMPTY_RESULT,
    FAILURE_IDENTITY_DISABLED,
    FAILURE_IDENTITY_ERROR,
    FAILURE_IDENTITY_QUOTA,
    FAILURE_KINDS,
    FAILURE_PARSE_FAILED,
    FAILURE_PRIVACY_BLOCKED,
    FAILURE_PROVIDER_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_TOO_LARGE,
    FAILURE_UNSUPPORTED_FORMAT,
    failure_kind,
)
from .fingerprint import (
    CacheIdentity,
    CacheSchemaVersion,
    RequestFingerprint,
    hash_text,
)
from .identity import (
    DailyQuota,
    IdentityCandidate,
    IdentityConfig,
    SauceNaoClient,
    identity_reference,
    identity_requested,
    parse_saucenao_response,
    quota_for,
    reset_identity_state,
)
from .metrics import Metrics, NoopMetrics, Snapshotter
from .privacy import (
    IMAGES_NOT_SENT_TEXT,
    PrivacySettings,
    blocked_reason,
    configure_privacy,
    current_privacy,
    ctx_is_group_chat,
    reset_privacy,
    scope_is_group,
)
from .prompt import (
    AttributeDescriptionTemplateVersion,
    ChatDescriptionTemplateVersion,
    ImageAnalysisTemplateVersion,
    ImagePromptTemplateVersion,
    PreprocessVersion,
    chat_description,
    clean_text,
    image_analysis_prompt,
    image_attributes_prompt,
    image_prompt,
    image_prompt_instruction,
    normalize_image_prompt_language,
    normalize_image_prompt_target,
)
from .service import (
    DefaultMaxConcurrentJobs,
    DefaultMaxJobQueue,
    DefaultMaxJobWaiters,
    DefaultMaxOutputBytes,
    DefaultTimeout,
    Service,
    classify_error,
    new,
    truncated,
)
from .service_types import (
    DefaultDetail,
    DefaultIdentityApiKeyEnv,
    DefaultIdentityDailyLimit,
    DefaultIdentityMinSimilarity,
    DefaultIdentityTimeoutSeconds,
    DefaultProbeTimeoutSeconds,
    MaxProbeTimeoutSeconds,
    MinProbeTimeoutSeconds,
    Options,
    PrivacyBlockedError,
    Prompt,
    Request,
    Result,
    Stats,
    clamp_probe_timeout,
)

#: Go 的 ``NewCounters``。
new_counters = Counters

__all__ = [
    "AttributeDescriptionTemplateVersion",
    "CacheIdentity",
    "CacheSchemaVersion",
    "ChatDescriptionTemplateVersion",
    "Counters",
    "DailyQuota",
    "DefaultCacheMaxEntries",
    "DefaultCacheTTL",
    "DefaultDetail",
    "DefaultIdentityApiKeyEnv",
    "DefaultIdentityDailyLimit",
    "DefaultIdentityMinSimilarity",
    "DefaultIdentityTimeoutSeconds",
    "DefaultMaxCacheValueSize",
    "DefaultMaxConcurrentJobs",
    "DefaultMaxJobQueue",
    "DefaultMaxJobWaiters",
    "DefaultMaxOutputBytes",
    "DefaultNegativeCacheTTL",
    "DefaultNegativeMax",
    "DefaultProbeTimeoutSeconds",
    "DefaultSharedTimeout",
    "DefaultTimeout",
    "DetachedContext",
    "FAILURE_CANCELLED",
    "FAILURE_DOWNLOAD_FAILED",
    "FAILURE_EMPTY_RESULT",
    "FAILURE_IDENTITY_DISABLED",
    "FAILURE_IDENTITY_ERROR",
    "FAILURE_IDENTITY_QUOTA",
    "FAILURE_KINDS",
    "FAILURE_PARSE_FAILED",
    "FAILURE_PRIVACY_BLOCKED",
    "FAILURE_PROVIDER_ERROR",
    "FAILURE_TIMEOUT",
    "FAILURE_TOO_LARGE",
    "FAILURE_UNSUPPORTED_FORMAT",
    "IDENTITY_CONFIDENCE_HIGH",
    "IDENTITY_CONFIDENCE_LOW",
    "IDENTITY_CONFIDENCE_MEDIUM",
    "IMAGES_NOT_SENT_TEXT",
    "IdentityCandidate",
    "IdentityConfig",
    "ImageAnalysis",
    "ImageAnalysisTemplateVersion",
    "ImageAttributes",
    "ImagePromptTemplateVersion",
    "Inflight",
    "MAX_ANALYSIS_PROMPT_RUNES",
    "MAX_ANALYSIS_TAGS",
    "MAX_ANALYSIS_TAG_RUNES",
    "MAX_ANALYSIS_WARNINGS",
    "MAX_ANALYSIS_WARNING_RUNES",
    "MAX_ATTRIBUTE_DESCRIPTION_RUNES",
    "MAX_ATTRIBUTE_FIELD_RUNES",
    "MAX_ATTRIBUTE_TEXT_RUNES",
    "MaxProbeTimeoutSeconds",
    "Metrics",
    "MinProbeTimeoutSeconds",
    "NegativeCache",
    "NoopMetrics",
    "Options",
    "PreprocessVersion",
    "PrivacyBlockedError",
    "PrivacySettings",
    "Prompt",
    "Request",
    "RequestFingerprint",
    "Result",
    "SauceNaoClient",
    "Service",
    "Snapshot",
    "Snapshotter",
    "Stats",
    "SuccessCache",
    "analyze_attributes_text",
    "attributes_summary",
    "blocked_reason",
    "chat_description",
    "clean_analysis_list",
    "clean_text",
    "classify_error",
    "clamp_probe_timeout",
    "configure_privacy",
    "current_privacy",
    "ctx_is_group_chat",
    "extract_json_object",
    "failure_kind",
    "hash_text",
    "identity_reference",
    "identity_requested",
    "image_analysis_prompt",
    "image_attributes_prompt",
    "image_prompt",
    "image_prompt_instruction",
    "is_cancellation",
    "new",
    "new_counters",
    "normalize_identity_confidence",
    "normalize_image_prompt_language",
    "normalize_image_prompt_target",
    "parse_image_analysis",
    "parse_image_attributes",
    "parse_saucenao_response",
    "quota_for",
    "reset_identity_state",
    "reset_privacy",
    "scope_is_group",
    "truncated",
]
