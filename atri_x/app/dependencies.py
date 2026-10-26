"""装配层依赖注入点：各阶段请求/组件与 :class:`Dependencies`。

对应 Go：``internal/app/dependencies.go``。

Go 在这里定义了 **7 个接口**（Environment / FoundationFactory / ModelFactory /
PlatformFactory / RuntimeFactory / IntegrationFactory / PlatformExecutor）以及
配套的 ``XxxRequest`` / ``XxxComponents`` 结构体。Python 侧：

* 组件一律用 ``@dataclass``（字段名 snake_case，与 Go 字段一一对应）；
* 工厂**不用 ABC**，只做鸭子类型 —— 每个工厂对象只要有对应方法即可，
  方法可以是同步也可以是协程（:func:`atri_x.app.runner.maybe_await` 统一收口）。
  这样并行移植期同僚可以先给同步桩，再换成真协程，不需要改 app 层。
* Go 里返回 ``error`` 的地方一律改成抛异常。

``Runner`` 与 ``NewRunner`` 的**定义**放在 :mod:`atri_x.app.runner`（因为 Python
无法把一个类拆到两个文件），这里只做再导出，保证 ``app.dependencies.Runner``
这个交叉检索点存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from atri_x.app.app import Options, RunMode
from atri_x.app.environment import DefaultEnvironment

__all__ = [
    "StartupProfilerProtocol",
    "EnvironmentProtocol",
    "LifecycleProtocol",
    "LogManagerProtocol",
    "ChatHistoryStoreProtocol",
    "FoundationRequest",
    "FoundationComponents",
    "ModelRequest",
    "ModelClients",
    "PlatformRequest",
    "PlatformComponents",
    "RuntimeRequest",
    "RuntimeComponents",
    "IntegrationRequest",
    "PlatformRunRequest",
    "Dependencies",
    "DefaultDependencies",
    "Runner",
    "NewRunner",
]


@runtime_checkable
class StartupProfilerProtocol(Protocol):
    """对应 Go ``StartupProfiler``。"""

    def set_enabled(self, enabled: bool) -> None: ...

    def mark(self, name: str) -> None: ...

    def flush(self) -> float: ...


@runtime_checkable
class EnvironmentProtocol(Protocol):
    """对应 Go ``Environment``。"""

    async def resolve_mode(self, mode: RunMode | str) -> RunMode: ...

    async def claim_service_marker(self, mode: RunMode | str) -> Any: ...

    def new_startup_profiler(self, started_at: datetime | None) -> Any: ...


@runtime_checkable
class LifecycleProtocol(Protocol):
    """对应 Go ``Lifecycle``。"""

    async def close(self, ctx: Any = None) -> Any: ...


@runtime_checkable
class LogManagerProtocol(Protocol):
    """对应 Go ``LogManager``。"""

    def runtime(self) -> Any: ...

    def audit(self) -> Any: ...

    def atrinis(self) -> Any: ...

    def log_dir(self) -> str: ...

    def close(self) -> Any: ...


@runtime_checkable
class ChatHistoryStoreProtocol(Protocol):
    """对应 Go ``ChatHistoryStore``。"""

    def repository(self) -> Any: ...

    def outbound(self) -> Any: ...

    def close(self) -> Any: ...


@dataclass
class FoundationRequest:
    """对应 Go ``FoundationRequest``。"""

    options: Options = field(default_factory=Options)
    mode: RunMode = RunMode("")
    profiler: Any = None


@dataclass
class FoundationComponents:
    """对应 Go ``FoundationComponents``。"""

    maintenance: Any = None
    history_policy: Any = None
    config: Any = None
    logs: Any = None
    logger: Any = None
    store: Any = None
    chat_history_store: Any = None
    chat_history: Any = None
    outbound_messages: Any = None
    cron_manager: Any = None
    #: ``async def start_cron(ctx, service) -> None``（Go ``func(context.Context, *elcron.Service)``）。
    start_cron: Any = None
    lifecycle: Any = None
    #: Python 版新增：统一时间服务（:class:`atri_x.time_service.TimeService`）。
    #: 业务时间、cron 时区、提示词注入、``get_current_time`` 都从这里取同一个口径。
    time_service: Any = None
    #: Python 版新增：网络时间校验器（:class:`atri_x.timesync.verifier.Verifier`）。
    time_verifier: Any = None


@dataclass
class ModelRequest:
    """对应 Go ``ModelRequest``。"""

    foundation: FoundationComponents | None = None
    profiler: Any = None
    health: Any = None


@dataclass
class ModelClients:
    """对应 Go ``ModelClients``。"""

    by_provider: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformRequest:
    """对应 Go ``PlatformRequest``。"""

    foundation: FoundationComponents | None = None
    mode: RunMode = RunMode("")
    profiler: Any = None


@dataclass
class PlatformComponents:
    """对应 Go ``PlatformComponents``。"""

    primary: Any = None
    runtimes: list[Any] = field(default_factory=list)


@dataclass
class RuntimeRequest:
    """对应 Go ``RuntimeRequest``。"""

    foundation: FoundationComponents | None = None
    models: ModelClients = field(default_factory=ModelClients)
    platforms: PlatformComponents = field(default_factory=PlatformComponents)
    profiler: Any = None


@dataclass
class RuntimeComponents:
    """对应 Go ``RuntimeComponents``。"""

    media: Any = None
    agent: Any = None
    handler: Any = None
    cron_service: Any = None
    atriena_bus: Any = None
    image_limiter: Any = None
    #: Agent 构造期攒下的 Hook 加载告警，等 ``Integrations.Attach`` 之后补投递。
    hook_notices: list[str] = field(default_factory=list)
    #: ``() -> visionMetrics | None``，供 ``/metrics`` 用。
    vision_stats: Any = None
    lifecycle: Any = None
    #: Python 版新增：网络时间校验器（:class:`atri_x.timesync.verifier.Verifier`）。
    #: ``verification.enabled=false`` 时仍会构造，但 ``start()`` 立即返回 disabled。
    time_verifier: Any = None


@dataclass
class IntegrationRequest:
    """对应 Go ``IntegrationRequest``。"""

    foundation: FoundationComponents | None = None
    runtime: RuntimeComponents | None = None
    platforms: PlatformComponents = field(default_factory=PlatformComponents)
    mode: RunMode = RunMode("")
    profiler: Any = None
    health: Any = None


@dataclass
class PlatformRunRequest:
    """对应 Go ``PlatformRunRequest``。

    ``platform_failure`` 是 ``async`` 可调用：
    ``await platform_failure(ctx, platform, still_running, err)``。
    """

    handler: Any = None
    logger: Any = None
    runtimes: list[Any] = field(default_factory=list)
    after_start: Any = None
    heartbeat: Any = None
    mode: RunMode = RunMode("")
    platform_failure: Any = None


@dataclass
class Dependencies:
    """对应 Go ``Dependencies``。"""

    environment: Any = None
    foundation: Any = None
    models: Any = None
    platforms: Any = None
    runtime: Any = None
    integrations: Any = None
    executor: Any = None
    #: ``DefaultDependencies()`` 装配时被吞掉的失败：(槽位名, 原因)。
    #: 只反映"工厂没拿到"，不改变 :class:`atri_x.app.runner.Runner` 的必填校验语义
    #: —— 它的用途是把原因带进 ``app: missing <slot>`` 的报错里（见
    #: :meth:`degraded_reason`）。手写 ``Dependencies(...)`` 时一律为空。
    degraded: list[tuple[str, str]] = field(default_factory=list)

    def degraded_reason(self, slot: str) -> str:
        """某个槽位为什么没拿到；没有记录时返回空串。"""
        for name, reason in self.degraded:
            if name == slot:
                return reason
        return ""


def DefaultDependencies() -> Dependencies:
    """对应 Go ``DefaultDependencies``。

    ``platform`` / ``cli`` 等尚未交付的模块一律**函数内延迟导入**，且失败时降级成
    ``None``，让 :class:`atri_x.app.runner.Runner` 的必填校验给出可读错误 ——
    而不是在 import ``atri_x.app`` 时就把整个包炸掉。

    与 Go 的差异：Go 的 ``defaultDependencies`` 里这些全是直接构造，构造失败只能
    崩；Python 侧并行移植期必须容错。代价是"缺件"与"构造抛错"原本不可区分，
    因此这里把每次失败的原因收进 :attr:`Dependencies.degraded`，由 ``Runner`` 打进
    启动错误信息（缺件文案 + 真正原因）。
    """
    environment = DefaultEnvironment()
    degraded: list[tuple[str, str]] = []
    foundation = _maybe(
        "atri_x.app.foundation", "DefaultFoundationFactory", "foundation", degraded
    )
    models = _maybe("atri_x.app.models", "DefaultModelFactory", "models", degraded)
    platforms = _maybe(
        "atri_x.app.platforms", "DefaultPlatformFactory", "platforms", degraded
    )
    runtime = _maybe("atri_x.app.runtime", "DefaultRuntimeFactory", "runtime", degraded)
    integrations = _maybe(
        "atri_x.app.integrations", "DefaultIntegrationFactory", "integrations", degraded
    )
    executor = _maybe(
        "atri_x.app.platforms", "DefaultPlatformExecutor", "executor", degraded
    )
    return Dependencies(
        environment=environment,
        foundation=foundation,
        models=models,
        platforms=platforms,
        runtime=runtime,
        integrations=integrations,
        executor=executor,
        degraded=degraded,
    )


def _maybe(
    module_name: str,
    attr: str,
    slot: str = "",
    degraded: list[tuple[str, str]] | None = None,
) -> Any:
    """延迟构造默认工厂；模块缺失/构造失败时返回 ``None`` 并把原因记进 ``degraded``。"""

    def record(reason: str) -> None:
        if degraded is not None:
            degraded.append((slot or attr, reason))

    import importlib

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 并行移植期允许缺件
        record(f"import {module_name}: {type(exc).__name__}: {exc}")
        return None
    factory = getattr(module, attr, None)
    if factory is None:
        record(f"{module_name}.{attr} is missing")
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001
        record(f"{module_name}.{attr}(): {type(exc).__name__}: {exc}")
        return None


def __getattr__(name: str) -> Any:
    """``Runner`` / ``NewRunner`` 定义在 :mod:`atri_x.app.runner`（避免循环导入）。"""
    if name in {"Runner", "NewRunner"}:
        from atri_x.app import runner as _runner

        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
