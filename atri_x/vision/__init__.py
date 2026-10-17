"""视觉（图片转文本）引擎。

对应 Go：``internal/vision/``（1122 行）。它是内置 ``image_to_prompt`` 工具与
聊天视觉兜底共用的唯一描述引擎：

* :mod:`atri_x.vision.prompt` —— 四套模板（绘图提示词、结构化分析、聊天描述）
  与文本清洗；模板文本是逐字照抄 Go 的契约；
* :mod:`atri_x.vision.analysis` —— 结构化分析结果的解析与限幅；
* :mod:`atri_x.vision.cache` —— 成功缓存、确定性失败负缓存、在途任务合并；
* :mod:`atri_x.vision.fingerprint` —— 带版本的缓存身份与请求指纹；
* :mod:`atri_x.vision.metrics` / :mod:`atri_x.vision.counters` —— 观测与运维计数；
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
    MAX_ANALYSIS_PROMPT_RUNES,
    MAX_ANALYSIS_TAGS,
    MAX_ANALYSIS_TAG_RUNES,
    MAX_ANALYSIS_WARNINGS,
    MAX_ANALYSIS_WARNING_RUNES,
    ImageAnalysis,
    clean_analysis_list,
    extract_json_object,
    parse_image_analysis,
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
from .fingerprint import (
    CacheIdentity,
    CacheSchemaVersion,
    RequestFingerprint,
    hash_text,
)
from .metrics import Metrics, NoopMetrics, Snapshotter
from .prompt import (
    ChatDescriptionTemplateVersion,
    ImageAnalysisTemplateVersion,
    ImagePromptTemplateVersion,
    PreprocessVersion,
    chat_description,
    clean_text,
    image_analysis_prompt,
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
from .service_types import Options, Prompt, Request, Result, Stats

#: Go 的 ``NewCounters``。
new_counters = Counters

__all__ = [
    "CacheIdentity",
    "CacheSchemaVersion",
    "ChatDescriptionTemplateVersion",
    "Counters",
    "DefaultCacheMaxEntries",
    "DefaultCacheTTL",
    "DefaultMaxCacheValueSize",
    "DefaultMaxConcurrentJobs",
    "DefaultMaxJobQueue",
    "DefaultMaxJobWaiters",
    "DefaultMaxOutputBytes",
    "DefaultNegativeCacheTTL",
    "DefaultNegativeMax",
    "DefaultSharedTimeout",
    "DefaultTimeout",
    "DetachedContext",
    "ImageAnalysis",
    "ImageAnalysisTemplateVersion",
    "ImagePromptTemplateVersion",
    "Inflight",
    "MAX_ANALYSIS_PROMPT_RUNES",
    "MAX_ANALYSIS_TAGS",
    "MAX_ANALYSIS_TAG_RUNES",
    "MAX_ANALYSIS_WARNINGS",
    "MAX_ANALYSIS_WARNING_RUNES",
    "Metrics",
    "NegativeCache",
    "NoopMetrics",
    "Options",
    "PreprocessVersion",
    "Prompt",
    "Request",
    "RequestFingerprint",
    "Result",
    "Service",
    "Snapshot",
    "Snapshotter",
    "Stats",
    "SuccessCache",
    "chat_description",
    "clean_analysis_list",
    "clean_text",
    "classify_error",
    "extract_json_object",
    "hash_text",
    "image_analysis_prompt",
    "image_prompt",
    "image_prompt_instruction",
    "is_cancellation",
    "new",
    "new_counters",
    "normalize_image_prompt_language",
    "normalize_image_prompt_target",
    "parse_image_analysis",
    "truncated",
]
