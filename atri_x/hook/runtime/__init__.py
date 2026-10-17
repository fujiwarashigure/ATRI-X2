"""有状态 Hook 运行时（hook.v2 进程）。

对应 Go：``internal/hook/runtime``。

- :mod:`atri_x.hook.runtime.config` —— ``[plugin.runtime]`` 配置、状态与选项
- :mod:`atri_x.hook.runtime.protocol` —— ``system.init`` / ``event.handle`` 负载
- :mod:`atri_x.hook.runtime.manager` —— worker 集合与事件派发
- :mod:`atri_x.hook.runtime.worker` —— 单个子进程的生命周期与协议 IO
- :mod:`atri_x.hook.runtime.shared` —— 共享 JSON 协调存储
- :mod:`atri_x.hook.runtime.media` —— ``media.*`` RPC 桥
- :mod:`atri_x.hook.runtime.tool_bridge` —— ``tool.call`` / ``hooks.reload``
"""

from __future__ import annotations

from atri_x.hook.runtime.config import (
    Config,
    Info,
    Mode,
    ModeOnce,
    ModePersistent,
    ModeTransient,
    Options,
    RestartConfig,
    RuntimeConfigError,
    Status,
    StatusDegraded,
    StatusFailed,
    StatusReady,
    StatusRunning,
    StatusStarting,
    StatusStopped,
    StatusStopping,
    ToolsConfig,
    log_warning,
    resolve_cwd,
    valid_id,
)
from atri_x.hook.runtime.manager import Manager, ManagerError, new_manager
from atri_x.hook.runtime.media import (
    MediaBridge,
    MediaBridgeError,
    MediaResult,
    new_media_bridge,
)
from atri_x.hook.runtime.routes import (
    DISPATCHED_METADATA_KEY,
    MAX_TOOL_CALLS_PER_INVOCATION,
    SKIP_HOOK_ID_METADATA_KEY,
    Invocation,
    Lease,
    RouteError,
    RouteKey,
    ToolContext,
    random_id,
    route_key_for,
)
from atri_x.hook.runtime.shared import SharedState, SharedStateError
from atri_x.hook.runtime.tool_bridge import ToolBridgeError
from atri_x.hook.runtime.worker import (
    Worker,
    WorkerError,
    WorkerProcessError,
    WorkerTimeoutError,
)

__all__ = [
    "Config",
    "Info",
    "Mode",
    "ModeOnce",
    "ModePersistent",
    "ModeTransient",
    "Options",
    "RestartConfig",
    "RuntimeConfigError",
    "Status",
    "StatusDegraded",
    "StatusFailed",
    "StatusReady",
    "StatusRunning",
    "StatusStarting",
    "StatusStopped",
    "StatusStopping",
    "ToolsConfig",
    "log_warning",
    "resolve_cwd",
    "valid_id",
    "Manager",
    "ManagerError",
    "new_manager",
    "MediaBridge",
    "MediaBridgeError",
    "MediaResult",
    "new_media_bridge",
    "DISPATCHED_METADATA_KEY",
    "SKIP_HOOK_ID_METADATA_KEY",
    "MAX_TOOL_CALLS_PER_INVOCATION",
    "Invocation",
    "Lease",
    "RouteError",
    "RouteKey",
    "ToolContext",
    "random_id",
    "route_key_for",
    "SharedState",
    "SharedStateError",
    "ToolBridgeError",
    "Worker",
    "WorkerError",
    "WorkerProcessError",
    "WorkerTimeoutError",
]
