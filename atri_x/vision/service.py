"""视觉服务：用配置好的视觉 LLM 把图片转成文本。

对应 Go：``internal/vision/service.go``。

它是内置 ``image_to_prompt`` 工具与聊天视觉兜底**共用**的唯一描述引擎。缓存键
带版本、包含完整配置指纹，所以改模型、改 prompt、改预处理一定 miss，而改日志
级别不会。
"""

from __future__ import annotations

import base64
import time
from typing import Any

from atri_x.llm.errors import as_api_error
from atri_x.llm.llm import (
    ChatRequest,
    LLMMessage,
    MessageSegment,
    Role,
    SegmentImage,
    SegmentText,
    text_segments,
)
from atri_x.media import prepare_vision_image_context
from atri_x.media.mimetype import detect_content_type
from atri_x.ops import concurrency
from atri_x.ops.executors import run_cpu
from atri_x.safecontext import (
    CancelToken,
    CancelledError,
    DeadlineExceededError,
    with_timeout,
)
from atri_x.vision.cache import (
    DefaultSharedTimeout,
    Inflight,
    NegativeCache,
    SuccessCache,
    is_cancellation,
)
from atri_x.vision.fingerprint import (
    CacheIdentity,
    CacheSchemaVersion,
    RequestFingerprint,
    hash_text,
)
from atri_x.vision.metrics import Metrics, NoopMetrics, Snapshotter
from atri_x.vision.prompt import (
    ChatDescriptionTemplateVersion,
    ImageAnalysisTemplateVersion,
    ImagePromptTemplateVersion,
    PreprocessVersion,
    chat_description,
    clean_text,
    image_analysis_prompt,
    image_prompt,
    normalize_image_prompt_language,
    normalize_image_prompt_target,
)
from atri_x.vision.service_types import Options, Prompt, Request, Result, Stats

__all__ = [
    "ChatDescriptionTemplateVersion",
    "DefaultMaxConcurrentJobs",
    "DefaultMaxJobQueue",
    "DefaultMaxJobWaiters",
    "DefaultMaxOutputBytes",
    "DefaultTimeout",
    "ImageAnalysisTemplateVersion",
    "ImagePromptTemplateVersion",
    "NoopMetrics",
    "Options",
    "PreprocessVersion",
    "Prompt",
    "Request",
    "Result",
    "Service",
    "Stats",
    "chat_description",
    "classify_error",
    "image_analysis_prompt",
    "image_prompt",
    "new",
    "normalize_image_prompt_language",
    "normalize_image_prompt_target",
    "truncated",
]

#: 预处理 + 请求 + 流式读取的总时限。
DefaultTimeout = 90.0
#: 累计流式答案的上限。
DefaultMaxOutputBytes = 64 * 1024
#: 同时在跑的不同上游视觉任务上限。一阵图片（或并行多图兜底）不能打开无限条
#: 供应商连接。
DefaultMaxConcurrentJobs = 10
#: 等待 runner 槽位的任务上限，超过的新调用方以「队列已满」被拒。
DefaultMaxJobQueue = 16
#: 合并到同一个在途任务的调用者上限；再来的调用方快速失败，而不是在慢供应商
#: 后面排一个无界队列。
DefaultMaxJobWaiters = 16

#: 上游任务拿到 runner 槽位前失败的包装前缀（Go 用 ``fmt.Errorf("vision: job queue: %w")``）。
_JOB_QUEUE_PREFIX = "vision: job queue: "


class Service:
    """用一个共享缓存 + 合并层来描述图片。对应 Go ``Service``。"""

    def __init__(self, opts: Options | None = None) -> None:
        opts = opts or Options()
        if opts.timeout <= 0:
            opts.timeout = DefaultTimeout
        if opts.max_output_bytes <= 0:
            opts.max_output_bytes = DefaultMaxOutputBytes
        if opts.metrics is None:
            opts.metrics = NoopMetrics()
        if opts.now is None:
            opts.now = time.monotonic
        # 0 表示「用默认」，负数表示「不限」：Default* 刻意取得足够小，
        # 以保护供应商不被一阵图片打垮。
        max_concurrent = DefaultMaxConcurrentJobs
        if opts.max_concurrent_jobs < 0:
            max_concurrent = 0
        elif opts.max_concurrent_jobs > 0:
            max_concurrent = opts.max_concurrent_jobs
        max_queue = DefaultMaxJobQueue
        if opts.max_job_queue < 0:
            max_queue = 0
        elif opts.max_job_queue > 0:
            max_queue = opts.max_job_queue
        max_waiters = DefaultMaxJobWaiters
        if opts.max_job_waiters < 0:
            max_waiters = 0
        elif opts.max_job_waiters > 0:
            max_waiters = opts.max_job_waiters

        self.opts = opts
        self.success = SuccessCache(
            opts.cache_max_entries, opts.cache_ttl, opts.max_cache_value_size, opts.now
        )
        self.negative = NegativeCache(opts.negative_max, opts.negative_ttl, opts.now)
        self.inflight = Inflight(opts.base_context)
        self.runner = concurrency.new(
            concurrency.Config(max=max_concurrent, queue_size=max_queue)
        )
        self.max_concurrent_jobs = max_concurrent
        self.max_waiters = max_waiters

    # -- 观测 ---------------------------------------------------------------

    def _metrics(self) -> Metrics:
        return self.opts.metrics or NoopMetrics()

    # -- 主路径 -------------------------------------------------------------

    async def describe(self, ctx: CancelToken | None, req: Request) -> Result:
        """描述一张图片。对应 Go ``Service.Describe``。

        依次尝试成功缓存、负缓存、在途合并，最后才打上游。失败抛异常。
        """
        if self.opts.client is None or (self.opts.model or "").strip() == "":
            raise ValueError("vision: model is not configured")
        if (req.media_id or "").strip() == "":
            raise ValueError("vision: media id is required")

        timeout_ctx = ctx
        deadline: float | None = None
        own_timeout: CancelToken | None = None
        if self.opts.timeout > 0:
            timeout_ctx = with_timeout(ctx, self.opts.timeout)
            own_timeout = timeout_ctx
            # Go 从 ctx.Deadline() 读截止时间；CancelToken 不暴露它，所以在
            # 这里把本次请求的绝对截止时间显式交给 inflight（Python 侧偏差）。
            deadline = time.monotonic() + self.opts.timeout
        try:
            return await self._describe_locked(timeout_ctx, req, deadline)
        finally:
            # Go 用 ``defer timeoutCancel()``；不取消的话每个请求都会在事件
            # 循环上留下一个直到截止时间才触发的定时器。
            if own_timeout is not None:
                own_timeout.cancel("describe finished")

    async def _describe_locked(
        self, ctx: CancelToken | None, req: Request, deadline: float | None
    ) -> Result:
        key = self._cache_key(req)
        cached, ok = await self.success.get(key)
        if ok:
            self._metrics().cache_lookup("hit")
            return cached
        negative = await self.negative.get(key)
        if negative is not None:
            self._metrics().cache_lookup("negative_hit")
            raise negative
        self._metrics().cache_lookup("miss")

        started = time.monotonic()

        async def _job(shared_ctx: Any) -> Result:
            # 成为 leader 之后再查一次成功缓存：错过了缓存而排在上一波后面的
            # 调用者，不能在那一波刚结束时又起一次上游。
            again, hit = await self.success.get(key)
            if hit:
                return again
            # 给上游任务封顶。这里等待受共享任务上下文（调用者截止时间 /
            # 共享超时）约束，所以被打满的供应商不会让调用者无限等下去。
            try:
                release = await self.runner.acquire(shared_ctx)
            except BaseException as exc:
                raise RuntimeError(_JOB_QUEUE_PREFIX + str(exc)) from exc
            try:
                self._metrics().job_started()
                try:
                    value = await self._run(shared_ctx, req)
                except BaseException as exc:
                    # 只有确定性（明确的模型/参数）失败才被记住；瞬时失败永不。
                    await self.negative.put(key, exc)
                    self._metrics().error(classify_error(exc))
                    raise
                await self.success.put(key, value)
                return value
            finally:
                release()

        result, coalesced = await self.inflight.do(
            ctx,
            key,
            self.opts.shared_timeout,
            self.max_waiters,
            _job,
            deadline=deadline,
        )
        if coalesced:
            self._metrics().coalesced()
        self._metrics().duration("total", time.monotonic() - started)
        return result

    def _cache_key(self, req: Request) -> str:
        request_hash = RequestFingerprint(
            max_tokens=self.opts.max_tokens,
            temperature=self.opts.temperature,
            max_edge=self.opts.max_edge,
            max_image_bytes=self.opts.max_image_bytes,
            output_mime_type="image/jpeg",
        ).hash()
        identity = CacheIdentity(
            schema_version=CacheSchemaVersion,
            media_id=(req.media_id or "").strip(),
            provider=(self.opts.provider or "").strip(),
            endpoint=(self.opts.endpoint or "").strip(),
            model=(self.opts.model or "").strip(),
            prompt_version=(req.prompt.version or "").strip(),
            prompt_hash=hash_text(req.prompt.system + "\x00" + req.prompt.user),
            preprocess_version=PreprocessVersion,
            request_hash=request_hash,
            credential_epoch=self.opts.credential_epoch,
        )
        return identity.key()

    async def _run(self, ctx: Any, req: Request) -> Result:
        """缓存未命中时执行预处理 + 上游调用。对应 Go ``Service.run``。

        预处理是**纯 CPU**（解码 + 重编码；实测 3.1 MPix 图要 15 秒上下），
        必须放到工作线程：直接在事件循环里调用会把整个进程的定时器、
        心跳和其它请求一起冻住。它走 :func:`atri_x.ops.executors.run_cpu` 的专用
        ``atrix-cpu`` 池，既不冻事件循环，也不与阻塞 IO 抢同一个池。
        ``prepare_vision_image_context`` 不碰
        asyncio 对象，只读 ``ctx`` 的取消状态，因此线程里调用是安全的。
        """
        preprocess_start = time.monotonic()
        data, mime_type = await run_cpu(
            prepare_vision_image_context,
            ctx,
            req.data,
            req.mime_type,
            self.opts.max_edge,
            self.opts.max_image_bytes,
        )
        self._metrics().duration("preprocess", time.monotonic() - preprocess_start)
        if (mime_type or "").strip() == "":
            mime_type = detect_content_type(data)
        data_url = (
            "data:" + mime_type + ";base64," + base64.b64encode(data).decode("ascii")
        )

        upstream_start = time.monotonic()
        # 一个专用的可取消子上下文让我们在放弃时（输出超限、错误块、调用者
        # 丢掉共享任务）立刻停止读取并关闭传输，而不是让生产者卡在没人读的
        # 通道上直到外层截止时间。
        stream_ctx = CancelToken(ctx)
        try:
            stream = self.opts.client.chat_stream(
                stream_ctx,
                ChatRequest(
                    model=self.opts.model,
                    messages=[
                        LLMMessage(
                            role=Role.SYSTEM, segments=text_segments(req.prompt.system)
                        ),
                        LLMMessage(
                            role=Role.USER,
                            segments=[
                                MessageSegment(type=SegmentText, text=req.prompt.user),
                                MessageSegment(
                                    type=SegmentImage, url=data_url, mime_type=mime_type
                                ),
                            ],
                        ),
                    ],
                    temperature=self.opts.temperature,
                    max_tokens=self.opts.max_tokens,
                ),
            )
            out: list[str] = []
            used = 0
            finish_reason = ""
            async for chunk in stream:
                if chunk.error is not None:
                    raise chunk.error
                if chunk.finish_reason:
                    # 终止事件可能只带 finish_reason，正文是空的。
                    finish_reason = chunk.finish_reason
                delta = chunk.delta_content or ""
                if delta:
                    out.append(delta)
                    used += len(delta.encode("utf-8"))
                if self.opts.max_output_bytes > 0 and used > self.opts.max_output_bytes:
                    raise ValueError(
                        "vision: model output exceeded "
                        f"{self.opts.max_output_bytes >> 10} KiB before finishing"
                    )
        finally:
            stream_ctx.cancel("stream finished")
        self._metrics().duration("upstream", time.monotonic() - upstream_start)

        # 「通道关闭」不等于成功：没有终止符就关闭的流由适配器以 Error 块
        # 抛出，所以成功只能显式推断。
        current = _ctx_error(ctx)
        if current is not None:
            raise RuntimeError(
                f"vision: request stopped before finishing: {current}"
            ) from current
        if truncated(finish_reason):
            raise ValueError(
                f"vision: model output was truncated (finish_reason={finish_reason})"
            )
        value = clean_text("".join(out))
        if value == "":
            raise ValueError("vision: model returned an empty result")
        return Result(text=value, provider=self.opts.provider, model=self.opts.model)

    # -- 运维视图 -----------------------------------------------------------

    def stats(self) -> Stats:
        """返回当前观测数据。对应 Go ``Service.Stats``。

        可并发调用；``metrics`` 实现 :class:`Snapshotter` 时填计数，限流器占用
        来自 ``runner.stats()``。
        """
        stats = Stats()
        metrics = self.opts.metrics
        if metrics is not None and isinstance(metrics, Snapshotter):
            snapshot = metrics.metrics_snapshot()
            cache = getattr(snapshot, "cache", None)
            errors = getattr(snapshot, "errors", None)
            durations = getattr(snapshot, "durations", None)
            stats.cache = dict(cache) if cache else {}
            stats.coalesced = int(getattr(snapshot, "coalesced", 0) or 0)
            stats.jobs = int(getattr(snapshot, "jobs", 0) or 0)
            stats.errors = dict(errors) if errors else {}
            if durations:
                stats.durations_ms = {
                    stage: int(elapsed * 1000) for stage, elapsed in durations.items()
                }
        if self.runner is not None:
            stats.active_jobs, stats.queued_jobs = self.runner.stats()
        stats.max_concurrent_jobs = self.max_concurrent_jobs
        stats.max_waiters = self.max_waiters
        return stats


def new(opts: Options | None = None) -> Service:
    """构造一个 Service，并给未设置的缓存/超时字段填默认值。对应 Go ``New``。"""
    return Service(opts)


def truncated(finish_reason: str) -> bool:
    """``finish_reason`` 是否表示被截断。对应 Go ``truncated``。"""
    return (finish_reason or "").strip().lower() in (
        "length",
        "max_tokens",
        "max_output_tokens",
    )


def classify_error(err: BaseException | None) -> str:
    """把错误折叠成 metrics 用的**安全类别串**。对应 Go ``classifyError``。

    注意这不是 :class:`atri_x.llm.errors.ErrorCategory`：它只是观测标签，
    取值只有 ``none`` / ``timeout`` / ``canceled`` / ``http_<code>`` / ``other``。
    """
    if err is None:
        return "none"
    if is_cancellation(err):
        seen = 0
        current: Any = err
        while current is not None and seen < 32:
            if isinstance(current, (TimeoutError, DeadlineExceededError)):
                return "timeout"
            current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
            seen += 1
        return "canceled"
    api_err = as_api_error(err)
    if api_err is not None and api_err.status_code > 0:
        return f"http_{api_err.status_code}"
    return "other"


def _ctx_error(ctx: Any) -> BaseException | None:
    """``ctx.Err()``：未取消返回 ``None``。"""
    if ctx is None:
        return None
    error = ctx.error()
    if error is not None:
        return error
    if ctx.is_cancelled():
        return CancelledError("context canceled")
    return None
