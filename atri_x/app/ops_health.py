"""``/metrics`` 与 ``/diagnostics`` 的载荷收集，以及两个延迟接线的 HTTP 组件。

对应 Go：``internal/app/ops_health.go``。

JSON 键名**逐字照抄** Go struct tag（``collected_at`` / ``uptime_seconds`` /
``image_to_prompt`` …），``omitempty`` 逐字段复刻：空字符串、0、``None``、
空列表/字典一律不出现在输出里，与 Go 的 ``encoding/json`` 行为一致
（注意 Go 的 ``omitempty`` 对 ``int64`` 的 0 也生效，这里照抄）。

两个 Go 类型在 Python 里换成了 ``http.server`` 风格的 handler：

* :class:`LazyJSONProvider` —— Go ``lazyJSONProvider``，``GET`` 返回 ``fn()``；
* :class:`LazyHTTPHandler` —— Go ``lazyHTTPHandler``，``GET`` 交给 ``fn``。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from atri_x.media.imagecodec.backend_probe import image_backend_info as _image_backend_info
from atri_x.sysinfo import collect as _collect_sysinfo

__all__ = [
    "OMITEMPTY",
    "RateLimitMetrics",
    "ImageLimitMetrics",
    "VisionRunnerStats",
    "VisionMetrics",
    "VisionMetricsState",
    "OpsDiagnostics",
    "OpsMetrics",
    "LazyJSONProvider",
    "LazyHTTPHandler",
    "lazyJSONProvider",
    "lazyHTTPHandler",
    "writeJSONResponse",
    "write_json_response",
    "collectOpsDiagnostics",
    "collect_ops_diagnostics",
    "collectOpsMetrics",
    "collect_ops_metrics",
    "marshal",
]

#: 与 ``atri_x.config.OMITEMPTY`` 同义；这里单独给一份常量，避免 app 层
#: 为了一个哨兵值去 import config 的私有细节。
OMITEMPTY = None


# ---------------------------------------------------------------------------
# JSON 落盘：逐字段复刻 Go 的 omitempty
# ---------------------------------------------------------------------------


def _is_empty(value: Any) -> bool:
    """Go ``encoding/json`` 的 ``omitempty`` 判定。"""
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def marshal(value: Any) -> Any:
    """把带 ``omitempty`` 语义的 dataclass 递归渲染成 JSON-ready 结构。

    约定：字段值等于 :data:`OMITEMPTY` 以外的"零值"时按 Go 规则省略。
    ``datetime`` 一律渲染成 RFC3339（UTC，``Z`` 结尾）。
    """
    if isinstance(value, datetime):
        return format_time(value)
    if isinstance(value, dict):
        # Go 的 ``map`` **没有** omitempty：零值键照常输出（``"count": 0``）。
        # 历史事故（第三轮验证）：这里按 dataclass 的 omitempty 规则过滤了字典键，
        # ``GET /plugins/memory`` 的 ``count: 0``、``GET /plugins/learning`` 的
        # ``pending`` / ``approved: 0`` 被整键吞掉，与 Go 的 ``map[string]any`` 形状不符。
        return {key: marshal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [marshal(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        out: dict[str, Any] = {}
        for name in value.__dataclass_fields__:
            item = getattr(value, name)
            if item is None:
                continue
            rendered = marshal(item)
            if _is_empty(rendered):
                continue
            out[name] = rendered
        return out
    return value


def format_time(value: datetime) -> str:
    """RFC3339（秒级，UTC），与 Go ``time.RFC3339`` 兼容。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return text


# ---------------------------------------------------------------------------
# 载荷类型
# ---------------------------------------------------------------------------


@dataclass
class RateLimitMetrics:
    """``/metrics`` / ``/diagnostics`` 里的限速统计。对应 Go ``rateLimitMetrics``。

    ``configured`` 区分「未配置」（``False``：``rejected=0`` 只代表没有限流）与
    「已配置但零拒绝」。``configured=False`` 按 ``omitempty`` 不入 JSON，所以未配置时
    另带一条非空 ``disabled_reason``。
    """

    allowed: int = 0
    rejected: int = 0
    user_rejected: int = 0
    group_rejected: int = 0
    keys: int = 0
    user_messages_per_minute: int = 0
    user_burst: int = 0
    group_messages_per_minute: int = 0
    group_burst: int = 0
    last_reason: str = ""
    last_rejected_at: datetime | None = None
    #: 入站限流是否真的接线（见类 docstring）。
    configured: bool = False
    #: 未接线时的原因（``configured=False`` 时非空）。
    disabled_reason: str = ""


@dataclass
class ImageLimitMetrics:
    """``/metrics`` 里的生图并发统计。对应 Go ``imageLimitMetrics``。"""

    active: int = 0
    waiting: int = 0


@dataclass
class VisionRunnerStats:
    """单个视觉角色的运行器占用。对应 Go ``visionRunnerStats``。"""

    active_jobs: int = 0
    queued_jobs: int = 0
    max_concurrent_jobs: int = 0
    max_waiters: int = 0


@dataclass
class VisionMetrics:
    """``/metrics`` 里的图片描述引擎统计。对应 Go ``visionMetrics``。

    只包含计数、上限与占用，不含图片内容、媒体 ID、缓存键或凭据。
    """

    cache: dict[str, int] = field(default_factory=dict)
    coalesced: int = 0
    jobs_started: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    image_to_prompt: VisionRunnerStats | None = None
    fallback: VisionRunnerStats | None = None


@dataclass
class OpsDiagnostics:
    """``/diagnostics`` 载荷。对应 Go ``opsDiagnostics``。

    Python 版新增三段"能力装上了吗"（Go 没有）：``agent_services`` 是启动期
    :class:`atri_x.agent.bootstrap.AgentServicesReport` 的机器可读快照，
    ``redaction`` 是日志脱敏接线的自检，``image_backend`` 是实际生效的图像后端。
    """

    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    health: Any = None
    tasks: Any = None
    rate_limit: RateLimitMetrics = field(default_factory=RateLimitMetrics)
    models: list[Any] = field(default_factory=list)
    platforms: list[Any] = field(default_factory=list)
    last_restart_reason: str = ""
    #: 启动期装配报告（``None`` = 没有可用的报告，例如未装配的测试宿主）。
    agent_services: dict[str, Any] | None = None
    #: 日志脱敏自检快照（见 :func:`atri_x.logging.logging.redact_status`）。
    redaction: dict[str, Any] | None = None
    image_backend: dict[str, Any] | None = None  # backend_probe 探测：pillow/stdlib + 版本号
    #: Python 版新增（1.0.10）：时间段（时区 / tzdata / 网络校验状态）。
    #: **只进 /diagnostics，绝不进 /ready**：网络抖动不得触发容器重启。
    time: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpsMetrics:
    """``/metrics`` 载荷。对应 Go ``opsMetrics``。"""

    version: str = ""
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    uptime_seconds: int = 0
    health: Any = None
    tasks: Any = None
    resources: Any = None
    platforms: list[Any] = field(default_factory=list)
    models: list[Any] = field(default_factory=list)
    rate_limit: RateLimitMetrics = field(default_factory=RateLimitMetrics)
    image_limit: ImageLimitMetrics = field(default_factory=ImageLimitMetrics)
    vision: VisionMetrics | None = None


# ---------------------------------------------------------------------------
# 视觉统计状态
# ---------------------------------------------------------------------------


class VisionMetricsState:
    """共享计数器 + 每个角色的运行器占用。对应 Go ``visionMetricsState``。

    启动期写入一次，之后只被 metrics handler 读。
    """

    def __init__(self, counters: Any = None) -> None:
        self.counters = counters
        self.roles: dict[str, Any] = {}

    def counters_value(self) -> Any:
        """共享计数器；按需创建，保证调用方永远拿不到 ``None``。"""
        if self.counters is None:
            from atri_x.vision import new_counters

            self.counters = new_counters()
        return self.counters

    def set(self, role: str, service: Any) -> None:
        """对应 Go ``visionMetricsState.set``。"""
        if service is None:
            return
        self.roles[role] = service

    def runner_stats(self, role: str) -> VisionRunnerStats | None:
        service = self.roles.get(role)
        if service is None:
            return None
        stats = service.stats()
        if stats is None:
            return None
        return VisionRunnerStats(
            active_jobs=int(getattr(stats, "active_jobs", 0) or 0),
            queued_jobs=int(getattr(stats, "queued_jobs", 0) or 0),
            max_concurrent_jobs=int(getattr(stats, "max_concurrent_jobs", 0) or 0),
            max_waiters=int(getattr(stats, "max_waiters", 0) or 0),
        )

    def snapshot(self) -> VisionMetrics | None:
        """渲染 ``/metrics`` 里的 ``vision`` 段；两个角色都没配时返回 ``None``。"""
        image_to_prompt = self.runner_stats("image_to_prompt")
        fallback = self.runner_stats("fallback")
        if image_to_prompt is None and fallback is None:
            return None
        metrics = VisionMetrics(image_to_prompt=image_to_prompt, fallback=fallback)
        if self.counters is not None:
            counters = self.counters.snapshot()
            metrics.cache = dict(getattr(counters, "cache", None) or {})
            metrics.coalesced = int(getattr(counters, "coalesced", 0) or 0)
            metrics.jobs_started = int(getattr(counters, "jobs", 0) or 0)
            metrics.errors = dict(getattr(counters, "errors", None) or {})
            durations = getattr(counters, "durations", None) or {}
            if durations:
                metrics.durations_ms = {
                    stage: int(_millis(elapsed)) for stage, elapsed in durations.items()
                }
        return metrics

    # Go 命名别名。
    countersValue = counters_value
    runnerStats = runner_stats


def _millis(elapsed: Any) -> float:
    """``timedelta`` 或秒数 → 毫秒。"""
    total = getattr(elapsed, "total_seconds", None)
    if callable(total):
        return total() * 1000
    return float(elapsed) * 1000


def new_vision_metrics_state(counters: Any = None) -> VisionMetricsState:
    """对应 Go ``newVisionMetricsState``。"""
    return VisionMetricsState(counters)


# ---------------------------------------------------------------------------
# 收集函数
# ---------------------------------------------------------------------------


def _safe_call(obj: Any, name: str, default: Any) -> Any:
    method = getattr(obj, name, None)
    if not callable(method):
        return default
    try:
        return method()
    except Exception:  # noqa: BLE001 - 运维端点不得因单点失败整体 500
        return default


def _rate_limit_metrics(
    status: Any, *, configured: bool = False, disabled_reason: str = ""
) -> RateLimitMetrics:
    """把 ``RateLimitStatus`` 转成载荷。

    ``configured`` / ``disabled_reason`` 由调用方按"宿主上有没有限流器"给出：
    没有限流器时 ``rejected=0`` 是**没有限流**，不是"没有拒绝"。
    """
    if status is None:
        return RateLimitMetrics(
            configured=configured,
            disabled_reason="" if configured else disabled_reason,
        )
    return RateLimitMetrics(
        allowed=int(getattr(status, "allowed", 0) or 0),
        rejected=int(getattr(status, "rejected", 0) or 0),
        user_rejected=int(getattr(status, "user_rejected", 0) or 0),
        group_rejected=int(getattr(status, "group_rejected", 0) or 0),
        keys=int(getattr(status, "keys", 0) or 0),
        # 这里曾读不存在的 ``user_messages_per_min``，阈值因此永远显示 0。
        user_messages_per_minute=int(getattr(status, "user_messages_per_minute", 0) or 0),
        user_burst=int(getattr(status, "user_burst", 0) or 0),
        group_messages_per_minute=int(getattr(status, "group_messages_per_minute", 0) or 0),
        group_burst=int(getattr(status, "group_burst", 0) or 0),
        last_reason=str(getattr(status, "last_reason", "") or ""),
        last_rejected_at=getattr(status, "last_rejected_at", None),
        configured=configured,
        disabled_reason="" if configured else disabled_reason,
    )


#: 未配置入站限流时的说明（``[ops]`` 里两个 per-minute 阈值都为 0）。
_RATE_LIMIT_DISABLED_REASON = (
    "inbound rate limiting is not configured: "
    "ops.user_messages_per_minute / ops.group_messages_per_minute are 0"
)


def _inbound_rate_limit_state(agent: Any) -> tuple[bool, str]:
    """宿主上是否真的有限流器（返回 ``(configured, disabled_reason)``）。"""
    if agent is not None:
        for attribute in ("rate_limit_user", "rate_limit_group"):
            if getattr(agent, attribute, None) is not None:
                return True, ""
    return False, _RATE_LIMIT_DISABLED_REASON


def _redaction_status() -> dict[str, Any] | None:
    """日志脱敏自检快照；导入/求值失败返回 ``None``（诊断端点不因它 500）。"""
    try:
        from atri_x.logging.logging import redact_status

        return dict(redact_status())
    except Exception:  # noqa: BLE001
        return None


def _agent_services_payload(agent: Any) -> dict[str, Any] | None:
    """取启动期装配报告快照（读取实现见 :func:`atri_x.agent.bootstrap.services_snapshot`）。"""
    try:
        from atri_x.agent.bootstrap import services_snapshot

        return services_snapshot(agent) if agent is not None else None
    except Exception:  # noqa: BLE001 - 诊断端点不得因单点失败整体 500
        return None


def collect_ops_diagnostics(
    state: Any,
    agent: Any,
    time_service: Any = None,
    time_verifier: Any = None,
) -> OpsDiagnostics:
    """对应 Go ``collectOpsDiagnostics``（Python 版多几段能力自检）。"""
    now = datetime.now(timezone.utc)
    health_snapshot = _safe_call(state, "snapshot", None)
    tasks: Any = {}
    configured, disabled_reason = _inbound_rate_limit_state(agent)
    rate_limit = RateLimitMetrics(configured=configured, disabled_reason=disabled_reason)
    if agent is not None:
        tasks = _safe_call(agent, "active_requests", {})
        rate_limit = _rate_limit_metrics(
            _safe_call(agent, "rate_limit_status", None),
            configured=configured,
            disabled_reason=disabled_reason,
        )
    return OpsDiagnostics(
        collected_at=now,
        health=health_snapshot,
        tasks=tasks,
        rate_limit=rate_limit,
        models=list(getattr(health_snapshot, "models", None) or []),
        platforms=list(getattr(health_snapshot, "platforms", None) or []),
        last_restart_reason=str(getattr(health_snapshot, "last_restart_reason", "") or ""),
        agent_services=_agent_services_payload(agent),
        redaction=_redaction_status(),
        image_backend=dict(_image_backend_info()),
        time=_time_diagnostics(time_service, time_verifier),
    )


def _time_diagnostics(time_service: Any, time_verifier: Any) -> dict[str, Any]:
    """时间段（任何异常都降级成空段，诊断端点不得因此 500）。"""
    try:
        from atri_x.timesync.diagnostics import time_diagnostics

        return time_diagnostics(time_service, time_verifier)
    except Exception:  # noqa: BLE001 - 诊断自身失败不影响其余段
        return {}


def collect_ops_metrics(
    cfg: Any,
    state: Any,
    agent: Any,
    image_limiter: Any = None,
    vision_stats: Callable[[], VisionMetrics | None] | None = None,
) -> OpsMetrics:
    """对应 Go ``collectOpsMetrics``。"""
    import os

    now = datetime.now(timezone.utc)
    root = ""
    if cfg is not None:
        root = str(getattr(getattr(cfg, "sandbox", None), "root", "") or "")
        if not root:
            storage = getattr(cfg, "storage", None)
            root = os.path.dirname(str(getattr(storage, "sessions_sqlite_path", "") or ""))
    try:
        resources = _collect_sysinfo(root)
    except Exception:  # noqa: BLE001 - 资源采集失败不该让 /metrics 500
        resources = None
    tasks: Any = {}
    if agent is not None:
        tasks = _safe_call(agent, "active_requests", {})
    health_snapshot = _safe_call(state, "snapshot", None)
    metrics = OpsMetrics(
        collected_at=now,
        health=health_snapshot,
        tasks=tasks,
        resources=resources,
    )
    if agent is not None:
        configured, disabled_reason = _inbound_rate_limit_state(agent)
        metrics.rate_limit = _rate_limit_metrics(
            _safe_call(agent, "rate_limit_status", None),
            configured=configured,
            disabled_reason=disabled_reason,
        )
    if image_limiter is not None:
        stats = _safe_call(image_limiter, "stats", None)
        if stats is not None:
            active, waiting = _unpack_stats(stats)
            metrics.image_limit = ImageLimitMetrics(active=active, waiting=waiting)
    if vision_stats is not None:
        try:
            metrics.vision = vision_stats()
        except Exception:  # noqa: BLE001
            metrics.vision = None
    started_at = getattr(health_snapshot, "started_at", None)
    if isinstance(started_at, datetime) and started_at != _GO_ZERO:
        metrics.uptime_seconds = int((now - started_at).total_seconds())
    metrics.version = str(getattr(health_snapshot, "version", "") or "")
    metrics.platforms = list(getattr(health_snapshot, "platforms", None) or [])
    metrics.models = list(getattr(health_snapshot, "models", None) or [])
    return metrics


def _unpack_stats(stats: Any) -> tuple[int, int]:
    if isinstance(stats, (tuple, list)) and len(stats) >= 2:
        return int(stats[0]), int(stats[1])
    return int(getattr(stats, "active", 0) or 0), int(getattr(stats, "waiting", 0) or 0)


#: Go 的零值 ``time.Time``；Python 侧用哨兵日期表示，避免 1970 误算 uptime。
_GO_ZERO = datetime(1, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 延迟接线的 HTTP 组件
# ---------------------------------------------------------------------------


def write_json_response(handler_ctx: Any, status: int, value: Any) -> None:
    """写 JSON 响应。对应 Go ``writeJSONResponse``。

    ``handler_ctx`` 需要是 :class:`http.server.BaseHTTPRequestHandler` 那样的对象：
    有 ``send_response`` / ``send_header`` / ``end_headers`` / ``wfile``。
    """
    payload = json.dumps(
        marshal(value), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    handler_ctx.send_response(status)
    handler_ctx.send_header("Content-Type", "application/json; charset=utf-8")
    handler_ctx.send_header("Cache-Control", "no-store")
    handler_ctx.send_header("Content-Length", str(len(payload)))
    handler_ctx.end_headers()
    writer = getattr(handler_ctx, "wfile", None)
    if writer is not None:
        writer.write(payload)
        flush = getattr(writer, "flush", None)
        if callable(flush):
            flush()


class _HandlerMixin:
    """两个 lazy 组件共用的线程安全槽位。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._fn: Any = None
        self._use_lock = False

    def set(self, fn: Any = None, **kwargs: Any) -> None:
        """接线实现。对应 Go ``Set``。"""
        if "fn" in kwargs:
            fn = kwargs["fn"]
        if fn is None:
            return
        with self._lock:
            self._fn = fn

    def _get_fn(self) -> Any:
        with self._lock:
            return self._fn

    # Go 命名别名。
    Set = set

    def _query(self, path: str) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit

        data = parse_qs(urlsplit(path or "").query, keep_blank_values=True)
        return {k: v[0] if v else "" for k, v in data.items()}


class LazyJSONProvider(_HandlerMixin):
    """对应 Go ``lazyJSONProvider``：``GET`` 返回 ``fn()`` 的 JSON。"""

    def __call__(self, handler_ctx: Any, request: Any) -> None:
        self.handle(handler_ctx, request)

    def handle(self, handler_ctx: Any, request: Any) -> None:
        if str(getattr(request, "method", "GET")).upper() != "GET":
            _send_allow(handler_ctx, "GET")
            write_json_response(handler_ctx, 405, {"error": "method not allowed"})
            return
        fn = self._get_fn()
        if fn is None:
            write_json_response(
                handler_ctx,
                503,
                {"status": "not_ready", "error": "provider is not initialized"},
            )
            return
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - 端点必须回 JSON
            write_json_response(handler_ctx, 500, {"error": str(exc)})
            return
        write_json_response(handler_ctx, 200, value)

    ServeHTTP = handle


class LazyHTTPHandler(_HandlerMixin):
    """对应 Go ``lazyHTTPHandler``：``GET`` 交给延迟接线的 ``fn``。"""

    def __call__(self, handler_ctx: Any, request: Any) -> None:
        self.handle(handler_ctx, request)

    def handle(self, handler_ctx: Any, request: Any) -> None:
        fn = self._get_fn()
        if fn is None:
            write_json_response(
                handler_ctx,
                503,
                {"status": "not_ready", "error": "handler is not initialized"},
            )
            return
        result = fn(handler_ctx, request)
        if hasattr(result, "__await__"):
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:  # pragma: no cover - 服务器线程里通常没有运行中的 loop
                raise RuntimeError("lazy HTTP handler cannot await inside a running loop")

    ServeHTTP = handle


def _send_allow(handler_ctx: Any, allow: str) -> None:
    sender = getattr(handler_ctx, "send_header", None)
    if callable(sender):
        sender("Allow", allow)


# Go 命名别名。
lazyJSONProvider = LazyJSONProvider
lazyHTTPHandler = LazyHTTPHandler
writeJSONResponse = write_json_response
collectOpsDiagnostics = collect_ops_diagnostics
collectOpsMetrics = collect_ops_metrics
