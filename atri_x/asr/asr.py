"""OpenAI 兼容的语音转写（``/audio/transcriptions``）。

对应 Go：``internal/asr/asr.go``（缓存见 :mod:`atri_x.asr.cache`，错误分类见
:mod:`atri_x.asr.error`）。

设计立场与上游一致：本模块**不**依赖聊天模型，运维把 ``[asr]`` 指向一个已有的
``[providers.*]``，这里负责 multipart 上传、有界响应解析、有界并发 / 队列、
成功缓存 + 负缓存与重试。**段落选择、权限与额度记账由调用方负责**。

额度（四级每日额度：全局、全局单用户、本群、本群单用户）的账本本体在
``internal/agent/budget.go``（Python 侧 ``atri_x/agent/budget*.py``），不在本包。
本包只提供记账所需的两样东西，见文末「额度预占接口」一节：预占键的构造规则
（``reservation_call_id`` / ``reservation_digest_input``）与消费方协议
（``BudgetLedger``）。**转写没有独立的"结算"步骤**：Go 在调用 provider 之前就
按条预占，失败只记审计、不回滚额度（见 ``internal/agent/asr.go``），因此这里
也不提供 release 接口。

成功后的文本替换标记（``[语音 N 自动转写（可能有误）：...]``）在 Go 里位于
``internal/agent/asr.go`` 的 ``asrTranscriptionText``；为了让替换方只有一个
出处，这里同样导出 :func:`asr_transcription_text` 供 ``atri_x/agent`` 消费。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from atri_x.asr.cache import NegativeCache, SuccessCache
from atri_x.asr.error import AsrError, HTTPError, bounded_message, new_http_error, retryable
from atri_x.net.httpclient import HTTPClient, HTTPError as TransportError
from atri_x.ops.concurrency import Config as LimiterConfig
from atri_x.ops.concurrency import Limiter

__all__ = [
    "DEFAULT_MAX_AUDIO_BYTES",
    "DEFAULT_MAX_CONCURRENT_JOBS",
    "DEFAULT_MAX_JOB_QUEUE",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_INITIAL_DELAY",
    "DEFAULT_TIMEOUT",
    "KIND",
    "MAX_RETRY_AFTER",
    "BudgetLedger",
    "Options",
    "Request",
    "ReservationRequest",
    "Result",
    "Service",
    "asr_transcription_text",
    "clean_text",
    "extension_for_mime",
    "reservation_call_id",
    "reservation_digest_input",
    "safe_filename",
]

#: 单次请求超时（秒）。对应 Go ``DefaultTimeout``。
DEFAULT_TIMEOUT = 120.0
#: 单条录音上限（字节）。对应 Go ``DefaultMaxAudioBytes``。
DEFAULT_MAX_AUDIO_BYTES = 20 * 1024 * 1024
#: 响应体上限（字节）。对应 Go ``DefaultMaxResponseBytes``。
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
#: 并发上限。对应 Go ``DefaultMaxConcurrentJobs``。
DEFAULT_MAX_CONCURRENT_JOBS = 10
#: 等待队列上限。对应 Go ``DefaultMaxJobQueue``。
DEFAULT_MAX_JOB_QUEUE = 8
#: 重试初值（秒）。对应 Go ``DefaultRetryInitialDelay``。
DEFAULT_RETRY_INITIAL_DELAY = 1.0
#: 重试次数。对应 Go ``DefaultMaxRetries``。
DEFAULT_MAX_RETRIES = 2
#: ``Retry-After`` 采纳上限（秒）。对应 Go ``maxRetryAfter``。
MAX_RETRY_AFTER = 30.0
#: 额度账本的 kind（对应 Go 里 ``reserveBudgetBatchRequests(ctx, "asr", ...)``）。
KIND = "asr"
#: ``asr:audio`` 文件名兜底。
_DEFAULT_FILENAME = "audio"


@dataclass(slots=True)
class Options:
    """``Service`` 的配置。对应 Go ``Options``（新增时补默认值）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider: str = ""
    language: str = ""
    prompt: str = ""

    timeout: float = 0.0
    max_audio_bytes: int = 0
    max_response_bytes: int = 0
    max_retries: int = 0
    retry_initial_delay: float = 0.0
    proxy: str = ""

    max_concurrent_jobs: int = 0
    max_job_queue: int = 0
    job_wait_timeout: float = 0.0

    cache_ttl: float = 0.0
    cache_max_entries: int = 0
    max_cache_value_size: int = 0
    negative_ttl: float = 0.0
    negative_max: int = 0

    #: 可选的父 ctx（鸭子类型：带 ``is_cancelled()`` 的对象）。``Service.close()``
    #: 只会取消本服务，不会取消父 ctx（对应 Go 的 ``context.WithCancel``）。
    base_context: Any = None
    #: 凭据版本；参与缓存键，换 key 后旧缓存自动失效。
    credential_epoch: int = 0

    #: 可选的 logger（``atri_x.logging.Logger`` 形态：``warn(msg, **fields)``）。
    logger: Any = None
    #: 可注入的时钟（秒），用于缓存 TTL；默认 ``time.monotonic``。
    now: Callable[[], float] | None = None


@dataclass(slots=True)
class Request:
    """一段待转写的录音。对应 Go ``Request``。"""

    media_id: str = ""
    data: bytes = b""
    name: str = ""
    mime_type: str = ""
    language: str = ""


@dataclass(slots=True)
class Result:
    """一次成功转写。对应 Go ``Result``。"""

    text: str = ""
    language: str = ""
    #: 音频时长（秒，对应 Go ``time.Duration`` 的换算口径）。
    duration: float = 0.0
    provider: str = ""
    model: str = ""
    #: 是否来自成功缓存。
    cached: bool = False


class _CancelProxy:
    """把「服务已关闭」与调用方 ctx 合成一个取消判定。

    对应 Go ``context.WithCancel(baseCtx)`` + 每请求 ``context.WithTimeout``：
    :mod:`atri_x.net.httpclient` 只认 ``is_cancelled()``，所以这里做一层合并。
    """

    __slots__ = ("_ctx", "_closed")

    def __init__(self, ctx: Any, closed: threading.Event) -> None:
        self._ctx = ctx
        self._closed = closed

    def is_cancelled(self) -> bool:
        if self._closed.is_set():
            return True
        return _ctx_cancelled(self._ctx)


class Service:
    """按一个 provider 转写音频，带并发上限与进程内小缓存。对应 Go ``Service``。"""

    def __init__(self, opts: Options | None = None) -> None:
        # Go 的 New 按值接收 Options，调用方那份不受影响；这里同样先复制一份。
        options = replace(opts) if opts is not None else Options()
        if options.timeout <= 0:
            options.timeout = DEFAULT_TIMEOUT
        if options.max_audio_bytes <= 0:
            options.max_audio_bytes = DEFAULT_MAX_AUDIO_BYTES
        if options.max_response_bytes <= 0:
            options.max_response_bytes = DEFAULT_MAX_RESPONSE_BYTES
        if options.max_retries < 0:
            options.max_retries = 0
        elif options.max_retries == 0:
            options.max_retries = DEFAULT_MAX_RETRIES
        if options.retry_initial_delay <= 0:
            options.retry_initial_delay = DEFAULT_RETRY_INITIAL_DELAY
        if options.job_wait_timeout < 0:
            options.job_wait_timeout = 0.0
        if options.now is None:
            options.now = time.monotonic
        max_concurrent = DEFAULT_MAX_CONCURRENT_JOBS
        if options.max_concurrent_jobs < 0:
            max_concurrent = 0
        elif options.max_concurrent_jobs > 0:
            max_concurrent = options.max_concurrent_jobs
        max_queue = DEFAULT_MAX_JOB_QUEUE
        if options.max_job_queue < 0:
            max_queue = 0
        elif options.max_job_queue > 0:
            max_queue = options.max_job_queue

        self.opts = options
        self._closed = threading.Event()
        self._http = HTTPClient(
            str(options.base_url or "").strip(),
            timeout=float(options.timeout),
            proxy=str(options.proxy or "").strip(),
        )
        self.cache = SuccessCache(
            options.cache_max_entries, options.cache_ttl, options.max_cache_value_size, options.now
        )
        self.negative = NegativeCache(options.negative_max, options.negative_ttl, options.now)
        self.limiter = Limiter(
            LimiterConfig(
                max=max_concurrent, queue_size=max_queue, wait_timeout=options.job_wait_timeout
            )
        )

    # -- 生命周期 -----------------------------------------------------------

    def close(self) -> None:
        """取消在途任务并释放资源。对应 Go ``Service.Close``。"""
        self._closed.set()

    # -- 主流程 -------------------------------------------------------------

    async def transcribe(self, ctx: Any, req: Request) -> Result:
        """转写一段录音。对应 Go ``Service.Transcribe``。"""
        if not str(self.opts.base_url or "").strip():
            raise AsrError("asr: base URL is not configured")
        if not str(self.opts.model or "").strip():
            raise AsrError("asr: model is not configured")
        if not str(req.media_id or "").strip():
            raise AsrError("asr: media id is required")
        if not req.data:
            raise AsrError("asr: audio is empty")
        if len(req.data) > self.opts.max_audio_bytes:
            raise AsrError(
                f"asr: audio is {len(req.data)} bytes, over the {self.opts.max_audio_bytes} byte limit"
            )

        key = self.cache_key(req)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        denied = self.negative.get(key)
        if denied is not None:
            raise denied

        timeout = float(self.opts.timeout or 0.0)
        if timeout > 0:
            # 对应 Go 的 ``context.WithTimeout(ctx, timeout)``：整个操作（含排队
            # 与重试）共享一个总预算，超时等价于 Go 的 DeadlineExceeded。
            try:
                async with asyncio.timeout(timeout):
                    return await self._run(ctx, req, key)
            except TimeoutError as exc:
                raise AsrError(f"asr: request timed out after {timeout}s") from exc
        return await self._run(ctx, req, key)

    async def _run(self, ctx: Any, req: Request, key: str) -> Result:
        try:
            release = await self.limiter.acquire(_as_cancel_token(ctx))
        except BaseException as exc:  # noqa: BLE001 - 队满 / 超时 / 取消
            raise AsrError(f"asr: job queue: {exc}") from exc
        try:
            result = await self._transcribe_with_retries(ctx, req)
        except BaseException as exc:  # noqa: BLE001 - 记负缓存后原样抛出
            self.negative.put(key, exc)
            raise
        finally:
            release()
        result.provider = self.opts.provider
        result.model = self.opts.model
        self.cache.put(key, result)
        return result

    async def _transcribe_with_retries(self, ctx: Any, req: Request) -> Result:
        """对应 Go ``transcribeWithRetries``。"""
        attempts = self.opts.max_retries + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            _raise_if_cancelled(ctx, self._closed)
            try:
                return await self._transcribe_once(ctx, req)
            except HTTPError as exc:
                last_error = exc
            except TransportError as exc:
                last_error = HTTPError(status_code=0, message=bounded_message(str(exc)))
            if attempt == attempts - 1 or not retryable(last_error):
                break
            delay = self._retry_delay(attempt, last_error)
            _log_warning(
                self.opts.logger,
                "asr request retry",
                attempt=attempt + 1,
                delay=f"{delay}s",
                error=str(last_error),
            )
            await _sleep_cancellable(delay, ctx, self._closed)
        if last_error is None:  # pragma: no cover - 循环至少跑一次
            raise AsrError("asr: request failed")
        raise last_error

    def _retry_delay(self, attempt: int, err: BaseException | None) -> float:
        """对应 Go ``retryDelay``。"""
        if isinstance(err, HTTPError) and err.retry_after > 0:
            return min(err.retry_after, MAX_RETRY_AFTER)
        delay = self.opts.retry_initial_delay * (2**attempt)
        if delay <= 0:
            delay = self.opts.retry_initial_delay
        return delay

    async def _transcribe_once(self, ctx: Any, req: Request) -> Result:
        """一次上传 + 解析。对应 Go ``transcribeOnce``。"""
        fields, files = self._multipart(req)
        headers = {"Accept": "application/json"}
        key = str(self.opts.api_key or "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        cancel = _CancelProxy(ctx, self._closed)
        try:
            response = await self._http.request_multipart(
                "POST", "/audio/transcriptions", fields=fields, files=files, headers=headers, cancel=cancel
            )
        except TransportError as exc:
            raise HTTPError(status_code=0, message=bounded_message(str(exc))) from exc
        if not response.ok:
            body = response.body[: 64 * 1024]
            raise new_http_error(response.status, response.headers, body)
        return self._parse_response(response.body)

    def _multipart(self, req: Request) -> tuple[dict[str, str], list[tuple[str, str, bytes, str]]]:
        """构造 multipart 字段。对应 Go ``multipartBody``。"""
        filename = safe_filename(req.name, req.mime_type)
        mime_type = str(req.mime_type or "").strip() or "application/octet-stream"
        fields: dict[str, str] = {"model": self.opts.model}
        language = self._language_for(req)
        if language:
            fields["language"] = language
        prompt = str(self.opts.prompt or "").strip()
        if prompt:
            fields["prompt"] = prompt
        fields["response_format"] = "json"
        return fields, [("file", filename, req.data, mime_type)]

    def _parse_response(self, body: bytes) -> Result:
        """对应 Go ``parseResponse``。"""
        if len(body) > self.opts.max_response_bytes:
            raise AsrError(f"asr: response exceeded {self.opts.max_response_bytes} bytes")
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise AsrError(f"asr: decode response: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise AsrError("asr: decode response: 顶层不是 JSON 对象")
        text = _first_non_empty(payload.get("text"), payload.get("result"), payload.get("transcription"))
        text = clean_text(text)
        if not text:
            raise AsrError("asr: provider returned an empty transcript")
        result = Result(text=text, language=str(payload.get("language") or "").strip())
        duration = payload.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            result.duration = float(duration)
        if not result.language:
            result.language = str(self.opts.language or "").strip()
        return result

    def _language_for(self, req: Request) -> str:
        """对应 Go ``languageFor``。"""
        language = str(req.language or "").strip()
        if language:
            return language
        return str(self.opts.language or "").strip()

    def cache_key(self, req: Request) -> str:
        """缓存键：媒体 ID + 端点 + 模型 + 语言 + 提示词 + 凭据版本。

        对应 Go ``Service.cacheKey``（Go 用媒体 ID，而不是音频内容哈希：同一条
        媒体被重复读取时按键命中，内容变化由调用方换媒体 ID 表达）。返回
        sha256 的十六进制串。
        """
        digest = hashlib.sha256()
        for part in (
            "asr\x00",
            str(req.media_id or "").strip(),
            "\x00",
            str(self.opts.base_url or "").strip(),
            "\x00",
            str(self.opts.model or "").strip(),
            "\x00",
            self._language_for(req),
            "\x00",
            str(self.opts.prompt or "").strip(),
            f"\x00{int(self.opts.credential_epoch)}",
        ):
            digest.update(part.encode("utf-8"))
        return digest.hexdigest()


# ---------------------------------------------------------------------------
# 文本与文件名（Go asr.go 的包级函数）
# ---------------------------------------------------------------------------

def clean_text(value: str) -> str:
    """把转写结果压成一行并去掉控制字符。对应 Go ``CleanText``。

    永不返回含 NUL 的值；控制字符折叠成一个空格（行首不补空格）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    out: list[str] = []
    last_space = False
    for ch in text:
        code = ord(ch)
        if code == 0 or code < 0x20 or code == 0x7F:
            if not last_space and out:
                out.append(" ")
                last_space = True
            continue
        out.append(ch)
        last_space = ch == " " or ch == "\u3000"
    return "".join(out).strip()


def safe_filename(name: str, mime_type: str = "") -> str:
    """把录音文件名收敛成安全的基名。对应 Go ``safeFilename``。

    与 Go 的一处有意差异：Go 的 ``name[:120]`` 按**字节**截断，可能把多字节
    字符切成无效 UTF-8；这里同样按字节截断，但用 ``errors="ignore"`` 丢掉被切
    坏的那半个字符（multipart 头只能是合法 UTF-8）。
    """
    text = str(name or "").strip().replace("\\", "/")
    text = text.rstrip("/")
    text = text.split("/")[-1] if text else ""
    if text in (".", "/", ""):
        text = _DEFAULT_FILENAME
    mapped: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or ch in "\"';/\\":
            mapped.append("_")
            continue
        mapped.append(ch)
    text = "".join(mapped)
    encoded = text.encode("utf-8")
    if len(encoded) > 120:
        text = encoded[:120].decode("utf-8", errors="ignore")
    if not os.path.splitext(text)[1]:
        text += extension_for_mime(mime_type)
    return text


def extension_for_mime(mime_type: str) -> str:
    """音频 MIME → 扩展名。对应 Go ``extensionForMIME``。"""
    value = str(mime_type or "").strip().lower()
    table = {
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/wave": ".wav",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/amr": ".amr",
        "audio/webm": ".webm",
    }
    if value in table:
        return table[value]
    return ".bin"


# ---------------------------------------------------------------------------
# 结果文本替换（Go 在 internal/agent/asr.go，Python 侧归口到本模块）
# ---------------------------------------------------------------------------

def asr_transcription_text(index: int, text: str) -> str:
    """给转写结果打上「自动转写（可能有误）」标记。

    对应 Go ``internal/agent/asr.go`` 的 ``asrTranscriptionText``；消费方是
    ``atri_x/agent``（把入站语音段替换成这个文本段）。标记以 ``[`` 开头，因此
    误听的语音不会意外触发 slash 命令。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    return f"[语音 {index} 自动转写（可能有误）：{value}]"


# ---------------------------------------------------------------------------
# 额度预占接口（账本本体不在这里）
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ReservationRequest:
    """一条额度预占。对应 Go ``agent.budgetReservationRequest``。"""

    call_id: str = ""
    #: 幂等摘要：同一 ``call_id`` 重复预占时摘要必须一致（Go 用它识别重复）。
    digest: str = ""


@runtime_checkable
class BudgetLedger(Protocol):
    """额度账本的消费方协议。**消费方**：``atri_x/agent`` 的 budget 模块。

    Go 对应 ``internal/agent/budget.go`` 的 ``reserveBudgetBatchRequests``：
    调用前按 ``kind="asr"`` 取四级每日额度（全局 / 全局单用户 / 本群 / 本群单
    用户），全部通过才允许真正打 provider；同一 ``(turn, media)`` 重复预占按
    ``digest`` 视为幂等。账本本体（``state.toml [budget]``、落盘失败回滚、保守
    占用）不归本模块实现。
    """

    def reserve_batch(
        self, ctx: Any, kind: str, requests: Sequence[ReservationRequest]
    ) -> tuple[bool, str]:
        """预占一批额度，返回 ``(是否放行, 拒绝原因)``。"""
        ...


def reservation_call_id(turn_id: str, media_id: str, index: int) -> str:
    """预占键。对应 Go ``fmt.Sprintf("%s:%s:asr:%d", turnID, mediaID, index)``。

    ``turn_id`` 为空时 Go 回落到 ``shortHash(mediaID)``，那一步属于 agent 侧
    （``shortHash`` 不在本包），由调用方传入已算好的值。
    """
    return f"{turn_id}:{media_id}:{KIND}:{index}"


def reservation_digest_input(provider: str, model: str, media_id: str) -> str:
    """幂等摘要的原文：``provider\\x00model\\x00mediaID``。

    对应 Go ``shortHash(provider + "\\x00" + model + "\\x00" + mediaID)`` 的
    **输入**；摘要算法属于 agent 侧（``shortHash``），由账本自行计算。
    """
    return f"{provider}\x00{model}\x00{media_id}"


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------

def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _ctx_cancelled(ctx: Any) -> bool:
    """ctx 是否已取消（鸭子类型）。"""
    if ctx is None:
        return False
    checker = getattr(ctx, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - 判定失败按未取消处理
            return False
    if getattr(ctx, "cancelled", False) is True:
        return True
    is_set = getattr(ctx, "is_set", None)
    if callable(is_set) and hasattr(ctx, "set"):  # asyncio.Event / threading.Event
        try:
            return bool(is_set())
        except Exception:  # noqa: BLE001
            return False
    return False


def _as_cancel_token(ctx: Any) -> Any:
    """把 ctx 交给 :class:`atri_x.ops.concurrency.Limiter`（它只要有回调即可）。"""
    if ctx is None or hasattr(ctx, "add_callback"):
        return ctx
    return None


def _raise_if_cancelled(ctx: Any, closed: threading.Event) -> None:
    if closed.is_set():
        raise AsrError("asr: service is closed")
    if _ctx_cancelled(ctx):
        error = None
        getter = getattr(ctx, "error", None)
        if callable(getter):
            error = getter()
        raise error if error is not None else AsrError("asr: request cancelled")


async def _sleep_cancellable(delay: float, ctx: Any, closed: threading.Event) -> None:
    """可被取消 / 关闭打断的退避等待。"""
    remaining = max(0.0, float(delay))
    while remaining > 0:
        _raise_if_cancelled(ctx, closed)
        step = min(0.05, remaining)
        await asyncio.sleep(step)
        remaining -= step
    _raise_if_cancelled(ctx, closed)


def _log_warning(logger: Any, message: str, **fields: Any) -> None:
    if logger is None:
        return
    for name in ("warn", "warning"):
        writer = getattr(logger, name, None)
        if callable(writer):
            try:
                writer(message, **fields)
            except Exception:  # noqa: BLE001 - 日志失败不影响请求
                pass
            return
