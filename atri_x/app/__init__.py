"""``atri_x.app`` —— 装配层。

对应 Go：``internal/app``（4,010 行生产代码）。

它是把 config / storage / session / llm / tool / toolrun / hook / agent / platform
装配成可运行应用的唯一入口，并提供生命周期（启动、健康、关闭）与运行态服务。

## 公开面（外部只需要这些）

| 用途 | 名字 |
|---|---|
| 启动应用 | :func:`run` / :func:`Run` / :func:`run_sync` / :class:`Options` / :class:`RunMode` |
| 自定义依赖注入 | :class:`Dependencies` / :attr:`DefaultDependencies` / :class:`Runner` |
| ``config check`` | :func:`check_config` |
| ``doctor`` | :func:`run_doctor` / :class:`DoctorOptions` / :class:`DoctorReport` |
| CLI 瘦客户端 | :func:`run_cli_client` / :func:`try_run_cli_client` / :class:`CLIClientOptions` |

## 并行移植期的导入纪律（重要）

``atri_x/platform/cli``、``atri_x/platform/qq_onebot``、``atri_x/platform/qqofficial``、
``atri_x/platform/builtin``、``atri_x/launcher``、``atri_x/__main__`` 由同僚并行编写，
本包**绝不在模块顶层 import 它们**：

* :func:`atri_x.app.platforms.DefaultPlatformFactory` 在 ``build`` 里延迟导入
  ``atri_x.platform.builtin``；
* :func:`atri_x.app.cli_client.run_cli_client` 与
  :func:`atri_x.app.doctor_checks.doctor_cli_round_trip` 延迟导入
  ``atri_x.platform.cli``；
* :func:`DefaultDependencies` 用 :func:`importlib` 组装默认工厂，缺件时置 ``None``，
  由 :class:`Runner` 给出 ``app: missing <name>`` 的可读错误。

因此 ``import atri_x.app`` 永远不会因为同僚尚未交付而失败。

## 与 Go 的命名约定

Go 的导出名在这里同时提供两种拼写：**snake_case 主名**（PORTING §1.4）与
**Go 原名别名**（便于交叉检索、也便于同僚按 Go 代码直译调用）。
两条路径指向同一个对象，不会出现"两份实现"。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.app import (
    RUN_MODE_AUTO,
    RUN_MODE_CLI_ONLY,
    RUN_MODE_FULL,
    RUN_MODE_SERVICE,
    Options,
    Run,
    RunMode,
    RunModeAuto,
    RunModeCLIOnly,
    RunModeFull,
    RunModeService,
    new_runner,
    run,
    run_sync,
)
from atri_x.app.breaker_llm import (
    BreakerLLM,
    breakerLLM,
    with_fallback_on_error,
    with_fallback_total_timeout,
    wrap_breaker_llm,
)
from atri_x.app.cli_client import (
    CLIClientOptions,
    is_local_cli_url,
    new_cli_client,
    run_cli_client,
    try_run_cli_client,
)
from atri_x.app.config_check import check_config, check_config_warnings
from atri_x.app.cron import (
    enabled_cron_platforms,
    platform_config_enabled,
    setup_cron,
    start_cron_async,
)
from atri_x.app.dependencies import (
    ChatHistoryStoreProtocol,
    Dependencies,
    DefaultDependencies,
    EnvironmentProtocol,
    FoundationComponents,
    FoundationRequest,
    IntegrationRequest,
    LifecycleProtocol,
    LogManagerProtocol,
    ModelClients,
    ModelRequest,
    PlatformComponents,
    PlatformRequest,
    PlatformRunRequest,
    RuntimeComponents,
    RuntimeRequest,
    StartupProfilerProtocol,
)
from atri_x.app.doctor import (
    DEFAULT_DOCTOR_TIMEOUT,
    DoctorCheck,
    DoctorOptions,
    DoctorReport,
    DoctorService,
    format_doctor_report,
    run_doctor,
)
from atri_x.app.environment import (
    DefaultEnvironment,
    StartupProfiler,
    new_startup_profiler,
    platform_mode,
    resolve_run_mode,
    should_start_cron,
    startup_profile_enabled,
)
from atri_x.app.foundation import (
    DefaultFoundationFactory,
    FoundationLifecycle,
    log_startup_configuration,
    validate_work_model,
)
from atri_x.app.health import (
    HEALTH_ADDR_ENV,
    HEALTH_ALLOW_UNAUTHENTICATED_ENV,
    HEALTH_LIVE_STALE_ENV,
    HEALTH_OPS_TOKEN_ENV,
    HEALTH_RESTART_REASON_ENV,
    HEALTH_RESTART_REASON_VALUE_ENV,
    apply_restart_reason,
    close_health_server,
    is_loopback_health_addr,
    read_health_config,
    start_health_server,
)
from atri_x.app.health_handler import HealthHandler
from atri_x.app.health_llm import HealthLLM, wrap_health_llm
from atri_x.app.comic import (
    LLMComicPlanner,
    PlatformAvatarResolver,
    build_comic_planner,
    comic_plan_selection,
)
from atri_x.app.image_rewriter import (
    ImagePromptRewriter,
    build_image_prompt_rewriter,
    image_rewrite_selection,
    image_rewrite_system_for,
)
from atri_x.app.image_to_prompt import build_image_prompt_service
from atri_x.app.integrations import (
    DefaultIntegrationFactory,
    resolve_atrinis_tokens,
    runtime_names,
)
from atri_x.app.models import (
    DefaultModelFactory,
    ProtocolRouter,
    app_llm_request_options,
    model_extra_payloads,
    new_provider_llm,
    set_client_logger,
)
from atri_x.app.naming import NamingLogger
from atri_x.app.ops_health import (
    ImageLimitMetrics,
    LazyHTTPHandler,
    LazyJSONProvider,
    OpsDiagnostics,
    OpsMetrics,
    RateLimitMetrics,
    VisionMetrics,
    VisionMetricsState,
    VisionRunnerStats,
    collect_ops_diagnostics,
    collect_ops_metrics,
    marshal,
    write_json_response,
)
from atri_x.app.platform_caller import (
    HookPlatformCallerResolver,
    PlatformCallerResolver,
    resolve_platform_caller,
)
from atri_x.app.platforms import (
    AtrinisRuntimeAdapter,
    DefaultPlatformExecutor,
    DefaultPlatformFactory,
    deliver_hook_notices,
    hook_notice_target,
    notify_platform_failure,
    register_command_catalogs,
    register_completion_platforms,
    register_cron_platform_hook,
    register_platform_hooks,
    run_platforms,
)
from atri_x.app.runner import CleanupStep, NewRunner, Runner, maybe_await
from atri_x.app.runtime import (
    DefaultRuntimeFactory,
    HookRuntimeLifecycle,
    audit_func,
    build_agent,
    build_cron_service,
    build_hook_service,
    max_int64,
    register_turn_alias,
    resolve_file_delivery_credentials,
)
from atri_x.app.service_marker import (
    ServiceMarker,
    ServiceMarkerError,
    claim_service_marker,
    read_service_marker,
    service_marker_path,
    service_marker_running,
)
from atri_x.app.service_types import (
    AudioTranscriptionRequest,
    AudioTranscriptionResult,
    ImagePromptRewriteRequest,
)
from atri_x.app.tool_runtime import (
    ToolRuntime,
    ToolRuntimeOptions,
    build_tool_runtime,
)
from atri_x.app.vision import (
    VisionChatDescriber,
    build_vision_describer,
    vision_capability_error,
)

#: 延迟导入的"尚未交付模块"清单 —— Lead 做最终对接时按这个表核对。
#: 名字按 PORTING.md §2 书写（函数 snake_case、类型 PascalCase）。
DEFERRED_IMPORTS: dict[str, tuple[str, ...]] = {
    "atri_x.platform.builtin": ("new", "Options", "Bundle"),
    "atri_x.platform.cli": (
        "new_config_from_platform_config",
        "new_remote_client",
        "RemoteClientOptions",
    ),
}

__all__ = [
    # app.py
    "RunMode",
    "RunModeAuto",
    "RunModeFull",
    "RunModeCLIOnly",
    "RunModeService",
    "RUN_MODE_AUTO",
    "RUN_MODE_FULL",
    "RUN_MODE_CLI_ONLY",
    "RUN_MODE_SERVICE",
    "Options",
    "run",
    "Run",
    "run_sync",
    "new_runner",
    # dependencies.py / runner.py
    "Dependencies",
    "DefaultDependencies",
    "Runner",
    "NewRunner",
    "CleanupStep",
    "maybe_await",
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
    # environment.py / service_marker.py
    "DefaultEnvironment",
    "StartupProfiler",
    "new_startup_profiler",
    "resolve_run_mode",
    "platform_mode",
    "should_start_cron",
    "startup_profile_enabled",
    "ServiceMarker",
    "ServiceMarkerError",
    "service_marker_path",
    "read_service_marker",
    "service_marker_running",
    "claim_service_marker",
    # foundation.py / cron.py
    "DefaultFoundationFactory",
    "FoundationLifecycle",
    "log_startup_configuration",
    "validate_work_model",
    "start_cron_async",
    "setup_cron",
    "enabled_cron_platforms",
    "platform_config_enabled",
    # health*.py / ops_health.py
    "start_health_server",
    "close_health_server",
    "apply_restart_reason",
    "is_loopback_health_addr",
    "read_health_config",
    "HEALTH_ADDR_ENV",
    "HEALTH_LIVE_STALE_ENV",
    "HEALTH_OPS_TOKEN_ENV",
    "HEALTH_ALLOW_UNAUTHENTICATED_ENV",
    "HEALTH_RESTART_REASON_ENV",
    "HEALTH_RESTART_REASON_VALUE_ENV",
    "HealthHandler",
    "HealthLLM",
    "wrap_health_llm",
    "LazyJSONProvider",
    "LazyHTTPHandler",
    "RateLimitMetrics",
    "ImageLimitMetrics",
    "VisionRunnerStats",
    "VisionMetrics",
    "VisionMetricsState",
    "OpsDiagnostics",
    "OpsMetrics",
    "collect_ops_metrics",
    "collect_ops_diagnostics",
    "write_json_response",
    "marshal",
    # models.py / breaker_llm.py
    "DefaultModelFactory",
    "ProtocolRouter",
    "new_provider_llm",
    "app_llm_request_options",
    "model_extra_payloads",
    "set_client_logger",
    "BreakerLLM",
    "wrap_breaker_llm",
    "with_fallback_on_error",
    "with_fallback_total_timeout",
    # services
    "ToolRuntime",
    "ToolRuntimeOptions",
    "build_tool_runtime",
    "VisionChatDescriber",
    "build_vision_describer",
    "vision_capability_error",
    "build_image_prompt_service",
    "ImagePromptRewriter",
    "build_image_prompt_rewriter",
    "image_rewrite_selection",
    "image_rewrite_system_for",
    "LLMComicPlanner",
    "PlatformAvatarResolver",
    "build_comic_planner",
    "comic_plan_selection",
    "NamingLogger",
    "PlatformCallerResolver",
    "HookPlatformCallerResolver",
    "resolve_platform_caller",
    "AudioTranscriptionRequest",
    "AudioTranscriptionResult",
    "ImagePromptRewriteRequest",
    # platforms.py / integrations.py
    "DefaultPlatformFactory",
    "DefaultPlatformExecutor",
    "AtrinisRuntimeAdapter",
    "run_platforms",
    "register_completion_platforms",
    "register_command_catalogs",
    "register_platform_hooks",
    "register_cron_platform_hook",
    "notify_platform_failure",
    "hook_notice_target",
    "deliver_hook_notices",
    "DefaultIntegrationFactory",
    "resolve_atrinis_tokens",
    "runtime_names",
    # runtime.py
    "DefaultRuntimeFactory",
    "HookRuntimeLifecycle",
    "audit_func",
    "build_cron_service",
    "build_hook_service",
    "build_agent",
    "max_int64",
    "register_turn_alias",
    "resolve_file_delivery_credentials",
    # config_check.py / doctor*.py / cli_client.py
    "check_config",
    "check_config_warnings",
    "DoctorService",
    "DoctorOptions",
    "DoctorCheck",
    "DoctorReport",
    "run_doctor",
    "format_doctor_report",
    "DEFAULT_DOCTOR_TIMEOUT",
    "CLIClientOptions",
    "run_cli_client",
    "try_run_cli_client",
    "new_cli_client",
    "is_local_cli_url",
    # 元信息
    "DEFERRED_IMPORTS",
    "app_package_info",
]


def app_package_info() -> dict[str, Any]:
    """给 ``doctor`` / ``config check`` 之类的诊断输出的包自述。"""
    return {
        "package": "atri_x.app",
        "go_source": "internal/app",
        "deferred_imports": {
            module: list(names) for module, names in DEFERRED_IMPORTS.items()
        },
    }


# Go 命名别名（PascalCase 入口）—— 与 snake_case 主名指向同一对象。
DefaultDependenciesFn = DefaultDependencies
