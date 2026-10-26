"""装配运行器：按固定顺序装配各阶段并保证逆序清理。

对应 Go：``internal/app/runner.go``（``Runner.Run``）与 ``dependencies.go``
里的 ``Runner`` / ``NewRunner``。

与 Go 的差异：

* Go 用 ``defer`` 收集 ``cleanupStep`` 并在函数返回时逆序执行；Python 侧用
  ``try/except/finally`` + 显式 ``cleanups`` 列表，语义相同（主体异常与清理异常
  用 :class:`ExceptionGroup` 一起抛出，对齐 Go 的 ``errors.Join``）。
* Go 的 ``context.WithTimeout`` 关停预算用 :func:`asyncio.wait_for` 复刻。
* Go 末尾 ``runPlatforms`` 返回 ``ctx.Err()``；Python 侧被取消时抛
  :class:`asyncio.CancelledError`。
* ``/plugins/memory``、``/plugins/learning`` 两个 ops 端点用
  :class:`atri_x.app.ops_health.LazyHTTPHandler` 延迟接线，请求上下文取
  ``request.ctx``（没有则传 ``None``）。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from atri_x.app.app import Options
from atri_x.app.dependencies import (
    Dependencies,
    FoundationComponents,
    FoundationRequest,
    IntegrationRequest,
    ModelRequest,
    PlatformRequest,
    PlatformRunRequest,
    RuntimeComponents,
    RuntimeRequest,
)
from atri_x.app.environment import should_start_cron
from atri_x.app.health import apply_restart_reason, close_health_server, start_health_server
from atri_x.app.health_handler import HealthHandler
from atri_x.app.ops_health import (
    LazyHTTPHandler,
    LazyJSONProvider,
    collect_ops_diagnostics,
    collect_ops_metrics,
    write_json_response,
)
from atri_x.app.platforms import (
    deliver_hook_notices,
    hook_notice_target,
    notify_platform_failure,
)
from atri_x.app.support import maybe_await
from atri_x.errors import InvalidError

__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "CleanupStep",
    "Runner",
    "NewRunner",
    "maybe_await",
]

#: 对应 Go ``defaultShutdownTimeout``。
DEFAULT_SHUTDOWN_TIMEOUT = 30.0


@dataclass
class CleanupStep:
    """对应 Go ``cleanupStep``。"""

    name: str
    close: Callable[[Any], Any]


class Runner:
    """装配运行器。对应 Go ``Runner``。"""

    def __init__(
        self, deps: Dependencies, shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT
    ) -> None:
        checks = (
            ("environment", "environment", deps.environment),
            ("foundation factory", "foundation", deps.foundation),
            ("model factory", "models", deps.models),
            ("platform factory", "platforms", deps.platforms),
            ("runtime factory", "runtime", deps.runtime),
            ("integration factory", "integrations", deps.integrations),
            ("platform executor", "executor", deps.executor),
        )
        for name, slot, value in checks:
            if value is None:
                raise InvalidError(f"app: missing {name}{_degraded_hint(deps, slot)}")
        self.deps = deps
        self.shutdown_timeout = shutdown_timeout

    # -- 主流程 ---------------------------------------------------------------
    async def run(self, ctx: Any = None, opts: Options | None = None) -> None:
        """对应 Go ``Runner.Run``。

        ``ctx`` 为 ``None`` 时只靠协程取消停止；传入任何带 ``is_set()`` 的对象时，
        它被置位即视为外部停止信号。
        """
        options = opts if opts is not None else Options()
        cleanups: list[CleanupStep] = []
        body_error: BaseException | None = None
        try:
            await self._build_and_run(_RunContext(ctx), options, cleanups)
        except BaseException as exc:  # noqa: BLE001 - 与清理异常合并后重抛
            body_error = exc
        cleanup_error = await self._shutdown(cleanups)
        if body_error is not None and cleanup_error is not None:
            raise ExceptionGroup("app: run and shutdown errors", [body_error, cleanup_error])
        if body_error is not None:
            raise body_error
        if cleanup_error is not None:
            raise cleanup_error

    async def _build_and_run(
        self, ctx: "_RunContext", opts: Options, cleanups: list[CleanupStep]
    ) -> None:
        if ctx.stopped:
            raise asyncio.CancelledError()

        mode = await maybe_await(self.deps.environment.resolve_mode(opts.mode))
        marker = await maybe_await(self.deps.environment.claim_service_marker(mode))
        if marker is not None:
            cleanups.append(CleanupStep("service marker", lambda _ctx: marker.close()))

        profiler = self.deps.environment.new_startup_profiler(opts.started_at)
        if profiler is None:
            raise InvalidError("app: environment returned nil startup profiler")

        foundation = await maybe_await(
            self.deps.foundation.build(
                FoundationRequest(options=opts, mode=mode, profiler=profiler)
            )
        )
        _require_foundation(foundation)
        cleanups.append(CleanupStep("foundation", _foundation_close(foundation)))
        # Go 的顺序：先登记清理、再查 Logger（`runner.go:60-66`）。反过来的话
        # ``logger is None`` 这条错误路径不会关掉已建好的 foundation。
        if foundation.logger is None:
            raise InvalidError("app: foundation factory returned incomplete components")

        tasks_provider = LazyJSONProvider()
        metrics_provider = LazyJSONProvider()
        diagnostics_provider = LazyJSONProvider()
        memory_admin_provider = LazyHTTPHandler()
        learning_admin_provider = LazyHTTPHandler()
        health_state, health_server = await maybe_await(
            start_health_server(
                foundation.config,
                foundation.logger,
                opts.version,
                {
                    "/tasks": tasks_provider,
                    "/metrics": metrics_provider,
                    "/diagnostics": diagnostics_provider,
                    "/plugins/memory": memory_admin_provider,
                    "/plugins/learning": learning_admin_provider,
                },
            )
        )
        if health_server is not None:
            cleanups.append(
                CleanupStep("health server", lambda c: close_health_server(c, health_server))
            )
        apply_restart_reason(health_state)
        if health_state is not None:
            cleanups.append(CleanupStep("health state", _health_state_close(health_state)))

        models = await maybe_await(
            self.deps.models.build(
                ModelRequest(foundation=foundation, profiler=profiler, health=health_state)
            )
        )
        platforms = await maybe_await(
            self.deps.platforms.build(
                PlatformRequest(foundation=foundation, mode=mode, profiler=profiler)
            )
        )
        runtime = await maybe_await(
            self.deps.runtime.build(
                ctx,
                RuntimeRequest(
                    foundation=foundation,
                    models=models,
                    platforms=platforms,
                    profiler=profiler,
                ),
            )
        )
        _require_runtime(runtime)
        cleanups.append(CleanupStep("runtime", _runtime_close(runtime)))
        # Go 的顺序：先登记清理、再查 Handler（`runner.go:112-118`）。
        if runtime.handler is None:
            raise InvalidError("app: runtime factory returned incomplete components")

        platforms = await maybe_await(
            self.deps.integrations.attach(
                ctx,
                IntegrationRequest(
                    foundation=foundation,
                    runtime=runtime,
                    platforms=platforms,
                    mode=mode,
                    profiler=profiler,
                    health=health_state,
                )
            )
        )
        # Hook 加载告警在 Agent 构造期只能先攒着：真实平台发送器要等 Attach 里的
        # registerPlatformHooks 才注册，所以在这里补投递（没有可用平台时退回日志）。
        await maybe_await(
            deliver_hook_notices(
                ctx,
                foundation.logger,
                runtime.agent,
                hook_notice_target(platforms.primary, platforms.runtimes),
                runtime.hook_notices,
            )
        )

        agent = runtime.agent
        tasks_provider.set(lambda: _call_or_empty(agent, "active_requests"))
        metrics_provider.set(
            lambda: collect_ops_metrics(
                foundation.config,
                health_state,
                agent,
                runtime.image_limiter,
                runtime.vision_stats,
            )
        )
        diagnostics_provider.set(
            lambda: collect_ops_diagnostics(
                health_state, agent, foundation.time_service, foundation.time_verifier
            )
        )
        memory_admin_provider.set(_memory_admin_handler(agent))
        learning_admin_provider.set(_learning_admin_handler(agent))
        if health_state is not None:
            runtime.handler = HealthHandler(inner=runtime.handler, state=health_state)
            health_state.set_ready(True)
        startup_duration = profiler.flush()
        _log_info(
            foundation.logger,
            "atri-x startup completed",
            startup_duration=_format_seconds(startup_duration),
        )

        async def start_time_verifier(cctx: Any) -> None:
            """启动网络时间校验（未启用时 ``start()`` 立即返回，不发起任何请求）。"""
            verifier = getattr(foundation, "time_verifier", None)
            if verifier is None:
                return
            await maybe_await(verifier.start())

        async def after_start(cctx: Any) -> None:
            if should_start_cron(mode) and foundation.start_cron is not None:
                await maybe_await(foundation.start_cron(cctx, runtime.cron_service))
            # 校验循环独立于 cron：cron 关闭时网络时间校验仍可工作。
            await start_time_verifier(cctx)

        heartbeat = health_state.beat if health_state is not None else None

        async def platform_failure(
            failure_ctx: Any,
            platform_name: str,
            still_running: list[str],
            failure_err: BaseException,
        ) -> None:
            if health_state is not None:
                health_state.mark_platform_disconnected(platform_name, failure_err)
            await maybe_await(
                notify_platform_failure(
                    failure_ctx,
                    foundation.config,
                    agent,
                    platform_name,
                    still_running,
                    failure_err,
                )
            )

        await maybe_await(
            self.deps.executor.run(
                ctx,
                PlatformRunRequest(
                    handler=runtime.handler,
                    logger=foundation.logger,
                    runtimes=platforms.runtimes,
                    after_start=after_start,
                    heartbeat=heartbeat,
                    mode=mode,
                    platform_failure=platform_failure,
                ),
            )
        )

    # -- 关停 ---------------------------------------------------------------
    async def _shutdown(self, cleanups: list[CleanupStep]) -> BaseException | None:
        """逆序执行清理，单步失败不阻断后续（对应 Go 的 ``errors.Join`` 收集）。"""
        timeout = self.shutdown_timeout
        if timeout <= 0:
            timeout = DEFAULT_SHUTDOWN_TIMEOUT
        errors: list[BaseException] = []
        ctx = ShutdownContext()
        for step in reversed(cleanups):
            try:
                await asyncio.wait_for(maybe_await(step.close(ctx)), timeout)
            except asyncio.TimeoutError:
                errors.append(TimeoutError(f"close {step.name}: timed out"))
            except asyncio.CancelledError:
                errors.append(TimeoutError(f"close {step.name}: cancelled"))
            except BaseException as exc:  # noqa: BLE001 - 收集后统一重抛
                errors.append(exc)
        if not errors:
            return None
        if len(errors) == 1 and isinstance(errors[0], Exception):
            return errors[0]
        if all(isinstance(item, Exception) for item in errors):
            return ExceptionGroup("app: shutdown errors", errors)  # type: ignore[arg-type]
        # 含 BaseException（KeyboardInterrupt / CancelledError）时逐个重抛第一个。
        return errors[0]


def NewRunner(
    deps: Dependencies, shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT
) -> Runner:
    """对应 Go ``NewRunner``。"""
    return Runner(deps, shutdown_timeout)


# ---------------------------------------------------------------------------
# 内部助手
# ---------------------------------------------------------------------------


class _RunContext:
    """Go ``context.Context`` 的最小替身：只提供"外部是否要求停止"。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    @property
    def stopped(self) -> bool:
        check = getattr(self._ctx, "is_set", None)
        if callable(check):
            try:
                return bool(check())
            except Exception:  # noqa: BLE001
                return False
        return False

    async def wait_stopped(self) -> None:
        waiter = getattr(self._ctx, "wait", None)
        if callable(waiter):
            await maybe_await(waiter())


class ShutdownContext:
    """关停上下文：Go 侧是带超时的 ``context.Background()``。

    Python 侧超时由 :func:`asyncio.wait_for` 施加；保留这个对象是为了让
    ``close(ctx)`` 的签名可被测试断言，且与 Go 的调用形态一致。
    """

    def __init__(self) -> None:
        self.cancelled = False

    def is_set(self) -> bool:
        return self.cancelled


def _call_or_empty(obj: Any, name: str) -> Any:
    method = getattr(obj, name, None)
    if not callable(method):
        return {}
    return method()


def _degraded_hint(deps: Dependencies, slot: str) -> str:
    """把 ``DefaultDependencies`` 吞掉的构造失败补进「缺件」报错里。

    对应 Go 没有东西可对：Go 的工厂构造失败会直接返回 ``error``，Python 侧并行
    移植期必须把缺件降级成 ``None``（见 :func:`atri_x.app.dependencies._maybe`）。
    没有这一行，用户只能看到 ``app: missing integration factory``，真正原因
    （模块 import 失败 / 工厂构造抛异常）就丢了。
    """
    reason_for = getattr(deps, "degraded_reason", None)
    if not callable(reason_for):
        return ""
    reason = reason_for(slot)
    return f": {reason}" if reason else ""


def _log_info(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "info", None)
    if callable(emit):
        emit(message, **fields)


def _format_seconds(seconds: float) -> str:
    return f"{float(seconds):.6f}".rstrip("0").rstrip(".") + "s"


def _require_foundation(foundation: FoundationComponents | None) -> None:
    """Go ``runner.go:57-62`` 的两道校验（``logger`` 由调用方在登记清理后再查）。"""
    if foundation is None or foundation.lifecycle is None:
        raise InvalidError("app: foundation factory returned incomplete components")


def _require_runtime(runtime: RuntimeComponents | None) -> None:
    """Go ``runner.go:109-114`` 的两道校验（``handler`` 由调用方在登记清理后再查）。"""
    if runtime is None or runtime.lifecycle is None:
        raise InvalidError("app: runtime factory returned incomplete components")


def _foundation_close(foundation: FoundationComponents) -> Callable[[Any], Any]:
    lifecycle = foundation.lifecycle

    async def close(ctx: Any) -> Any:
        return await maybe_await(lifecycle.close(ctx))

    return close


def _runtime_close(runtime: RuntimeComponents) -> Callable[[Any], Any]:
    lifecycle = runtime.lifecycle

    async def close(ctx: Any) -> Any:
        return await maybe_await(lifecycle.close(ctx))

    return close


def _health_state_close(state: Any) -> Callable[[Any], Any]:
    def close(_ctx: Any) -> None:
        state.set_shutting_down(True)
        state.set_ready(False)
        return None

    return close


def _admin_callable(agent: Any, name: str) -> Callable[..., Any] | None:
    """解析插件管理端点要调的入口。

    对应 Go 的 ``agent.AngelMemoryCount`` / ``agent.SelfLearningStats``。Python 侧这
    两个能力落在 :mod:`atri_x.agent.memory_learning_admin` 的**模块函数**上
    （签名 ``f(agent, ctx, platform, scope_id)``），而不是 ``Agent`` 的方法。这里先找
    Agent 上的同名方法，找不到再回落到模块函数并把 ``agent`` 作为首参绑定，两者都
    没有才返回 ``None``（调用方回 501）。
    """
    method = getattr(agent, name, None)
    if callable(method):
        return method
    try:
        from atri_x.agent import memory_learning_admin as admin
    except Exception:  # noqa: BLE001 - 缺件时保持 501，不要让整个端点崩
        return None
    function = getattr(admin, name, None)
    if not callable(function):
        return None

    def call(ctx: Any, platform: str, scope_id: str) -> Any:
        return function(agent, ctx, platform, scope_id)

    return call


def _memory_admin_handler(agent: Any) -> Callable[[Any, Any], Any]:
    """``GET /plugins/memory?platform=&scope_id=`` → ``{"count": n}``。"""

    async def handle(handler_ctx: Any, request: Any) -> None:
        if getattr(request, "method", "GET") != "GET":
            request.send_header("Allow", "GET")
            write_json_response(handler_ctx, 405, {"error": "method not allowed"})
            return
        params = getattr(request, "query", None) or {}
        platform = params.get("platform", "") if hasattr(params, "get") else ""
        scope_id = params.get("scope_id", "") if hasattr(params, "get") else ""
        method = _admin_callable(agent, "angel_memory_count")
        if not callable(method):
            write_json_response(
                handler_ctx, 501, {"error": "angel memory admin is not available"}
            )
            return
        try:
            count = await maybe_await(method(_request_ctx(request), platform, scope_id))
        except Exception as exc:  # noqa: BLE001 - 端点必须回 JSON 而不是抛
            write_json_response(handler_ctx, 500, {"error": str(exc)})
            return
        write_json_response(
            handler_ctx, 200, {"platform": platform, "scope_id": scope_id, "count": count}
        )

    return handle


def _learning_admin_handler(agent: Any) -> Callable[[Any, Any], Any]:
    """``GET /plugins/learning?platform=&scope_id=`` → ``{"pending":n,"approved":n}``。"""

    async def handle(handler_ctx: Any, request: Any) -> None:
        if getattr(request, "method", "GET") != "GET":
            request.send_header("Allow", "GET")
            write_json_response(handler_ctx, 405, {"error": "method not allowed"})
            return
        params = getattr(request, "query", None) or {}
        platform = params.get("platform", "") if hasattr(params, "get") else ""
        scope_id = params.get("scope_id", "") if hasattr(params, "get") else ""
        method = _admin_callable(agent, "self_learning_stats")
        if not callable(method):
            write_json_response(
                handler_ctx, 501, {"error": "self learning admin is not available"}
            )
            return
        try:
            pending, approved = await maybe_await(
                method(_request_ctx(request), platform, scope_id)
            )
        except Exception as exc:  # noqa: BLE001 - 端点必须回 JSON 而不是抛
            write_json_response(handler_ctx, 500, {"error": str(exc)})
            return
        write_json_response(
            handler_ctx,
            200,
            {
                "platform": platform,
                "scope_id": scope_id,
                "pending": pending,
                "approved": approved,
            },
        )

    return handle


def _request_ctx(request: Any) -> Any:
    """请求上的上下文；没有就用 ``None``。"""
    return getattr(request, "ctx", None)
