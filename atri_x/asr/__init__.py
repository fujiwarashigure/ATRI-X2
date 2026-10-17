"""OpenAI 兼容语音转写（自包含子系统）。

对应 Go：``internal/asr``（``asr.go`` / ``cache.go`` / ``error.go``；本包只依赖
``internal/ops/concurrency``，Python 侧对应 :mod:`atri_x.ops.concurrency`）。

本包**不** import ``atri_x.config``：上游同一个包也不 import ``internal/config``。
调用方（``internal/app/asr.go`` → ``atri_x/app``）把 ``[providers.*]`` 的
``base_url`` / ``api_key`` / ``proxy`` 与 ``[asr]`` 段的模型、语言、提示词、
各项上限映射成 :class:`Options` 后交给 :class:`Service`；``internal/agent`` →
``atri_x/agent`` 负责段落选择、权限、把语音段替换成
:func:`asr_transcription_text` 的文本，以及四级每日额度的预占（见
:class:`BudgetLedger`）。
"""

from __future__ import annotations

from atri_x.asr.asr import (
    DEFAULT_MAX_AUDIO_BYTES,
    DEFAULT_MAX_CONCURRENT_JOBS,
    DEFAULT_MAX_JOB_QUEUE,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_INITIAL_DELAY,
    DEFAULT_TIMEOUT,
    KIND,
    MAX_RETRY_AFTER,
    BudgetLedger,
    Options,
    Request,
    ReservationRequest,
    Result,
    Service,
    asr_transcription_text,
    clean_text,
    extension_for_mime,
    reservation_call_id,
    reservation_digest_input,
    safe_filename,
)
from atri_x.asr.cache import (
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_CACHE_VALUE_SIZE,
    DEFAULT_NEGATIVE_CACHE_MAX_ENTRIES,
    DEFAULT_NEGATIVE_CACHE_TTL,
    NegativeCache,
    SuccessCache,
    new_negative_cache,
    new_success_cache,
)
from atri_x.asr.error import (
    AsrError,
    HTTPError,
    bounded_message,
    deterministic_error,
    new_http_error,
    provider_message,
    retryable,
)

#: Go 导出名的交叉检索别名（PascalCase）。
New = Service
Transcribe = Service.transcribe
CleanText = clean_text
SafeFilename = safe_filename
ExtensionForMIME = extension_for_mime
Retryable = retryable
DeterministicError = deterministic_error
NewHTTPError = new_http_error
ProviderMessage = provider_message
BoundedMessage = bounded_message
ASRTranscriptionText = asr_transcription_text

__all__ = [
    "ASRTranscriptionText",
    "AsrError",
    "BoundedMessage",
    "BudgetLedger",
    "CleanText",
    "DEFAULT_CACHE_MAX_ENTRIES",
    "DEFAULT_CACHE_TTL",
    "DEFAULT_MAX_AUDIO_BYTES",
    "DEFAULT_MAX_CACHE_VALUE_SIZE",
    "DEFAULT_MAX_CONCURRENT_JOBS",
    "DEFAULT_MAX_JOB_QUEUE",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_NEGATIVE_CACHE_MAX_ENTRIES",
    "DEFAULT_NEGATIVE_CACHE_TTL",
    "DEFAULT_RETRY_INITIAL_DELAY",
    "DEFAULT_TIMEOUT",
    "DeterministicError",
    "ExtensionForMIME",
    "HTTPError",
    "KIND",
    "MAX_RETRY_AFTER",
    "NegativeCache",
    "New",
    "NewHTTPError",
    "Options",
    "ProviderMessage",
    "Request",
    "ReservationRequest",
    "Result",
    "Retryable",
    "SafeFilename",
    "Service",
    "SuccessCache",
    "Transcribe",
    "asr_transcription_text",
    "bounded_message",
    "clean_text",
    "deterministic_error",
    "extension_for_mime",
    "new_http_error",
    "new_negative_cache",
    "new_success_cache",
    "provider_message",
    "reservation_call_id",
    "reservation_digest_input",
    "retryable",
    "safe_filename",
]
