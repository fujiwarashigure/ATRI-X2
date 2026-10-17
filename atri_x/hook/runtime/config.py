"""有状态 Hook 进程的配置、状态与运行期选项。

对应 Go：``internal/hook/runtime/config.go``。

包的位置是刻意的（与 Go 一致）：**持久 Hook 仍然是 Hook**，不是另一条插件
分发路径，所以它挂在 Hook 子系统下而不是另起一个顶层包。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from atri_x.errors import AtriXError
from atri_x.hook.match import BlockPolicy
from atri_x.hook.process import ProcessEnvironment

__all__ = [
    "RuntimeError_",
    "RuntimeConfigError",
    "Status",
    "Mode",
    "RestartConfig",
    "ToolsConfig",
    "Config",
    "Info",
    "Options",
    "RuntimeLogger",
    "log_warning",
    "valid_id",
    "resolve_cwd",
]


class RuntimeConfigError(AtriXError):
    """``[plugin.runtime]`` 配置非法。对应 Go ``Config.Validate`` 的错误返回。"""


#: ``RuntimeError`` 是内建名，导出时用这个别名避免遮蔽。
RuntimeError_ = RuntimeConfigError


class Status:
    """持久 Hook 进程的状态。对应 Go ``Status``。"""

    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class Mode:
    """运行模式。对应 Go ``Mode``。"""

    ONCE = "once"
    PERSISTENT = "persistent"
    TRANSIENT = "transient"


StatusStarting = Status.STARTING
StatusReady = Status.READY
StatusRunning = Status.RUNNING
StatusDegraded = Status.DEGRADED
StatusStopping = Status.STOPPING
StatusStopped = Status.STOPPED
StatusFailed = Status.FAILED

ModeOnce = Mode.ONCE
ModePersistent = Mode.PERSISTENT
ModeTransient = Mode.TRANSIENT


@dataclass
class RestartConfig:
    """``[plugin.runtime.restart]``。对应 Go ``RestartConfig``。"""

    strategy: str = ""
    initial_delay_seconds: int = 0
    max_delay_seconds: int = 0


@dataclass
class ToolsConfig:
    """``[plugin.runtime.tools]``。对应 Go ``ToolsConfig``。"""

    allow: list[str] = field(default_factory=list)
    background_allow: list[str] = field(default_factory=list)


@dataclass
class Config:
    """从 ``[plugin.runtime]`` 解码出来的运行配置。对应 Go ``Config``。

    省略 ``mode`` 等价于 ``once``；只有 ``persistent`` 与 ``transient``
    会创建 hook.v2 worker。``id`` / ``description`` / ``dir`` / ``config_path`` /
    ``block`` / ``process_env`` 带 ``toml:"-"``，由配置装载方填充。
    """

    mode: str = ""
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    startup_timeout_seconds: int = 0
    shutdown_timeout_seconds: int = 0
    event_timeout_seconds: int = 0
    max_wait_seconds: int = 0
    restart: RestartConfig = field(default_factory=RestartConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)

    id: str = ""
    description: str = ""
    dir: str = ""
    config_path: str = ""
    block: BlockPolicy = field(default_factory=BlockPolicy)
    process_env: ProcessEnvironment = field(default_factory=ProcessEnvironment)

    def mode_or_once(self) -> str:
        """对应 Go ``Config.ModeOrOnce``。"""
        return self.mode or Mode.ONCE

    def is_worker(self) -> bool:
        """对应 Go ``Config.IsWorker``。"""
        return self.mode_or_once() != Mode.ONCE

    def validate(self) -> None:
        """对应 Go ``Config.Validate``；不合法抛 :class:`RuntimeConfigError`。"""
        mode = self.mode_or_once()
        if mode == Mode.ONCE:
            return
        if mode not in (Mode.PERSISTENT, Mode.TRANSIENT):
            raise RuntimeConfigError("runtime mode must be once, persistent or transient")
        if not (self.id or "").strip():
            raise RuntimeConfigError("hook id is required")
        if not valid_id(self.id):
            raise RuntimeConfigError(
                f"hook id {self.id!r} must contain only lowercase letters, digits, '-' or '_'"
            )
        if not self.command or not (self.command[0] or "").strip():
            raise RuntimeConfigError("runtime command is required")
        if not (self.cwd or "").strip():
            raise RuntimeConfigError("runtime cwd is required")
        if (
            self.startup_timeout_seconds <= 0
            or self.shutdown_timeout_seconds <= 0
            or self.event_timeout_seconds <= 0
            or self.max_wait_seconds <= 0
        ):
            raise RuntimeConfigError(
                "runtime startup_timeout_seconds, shutdown_timeout_seconds, "
                "event_timeout_seconds and max_wait_seconds must be positive"
            )
        strategy = (self.restart.strategy or "").strip()
        if strategy not in ("never", "on_failure", "always"):
            raise RuntimeConfigError(
                "runtime restart.strategy must be never, on_failure or always"
            )
        if (
            self.restart.initial_delay_seconds <= 0
            or self.restart.max_delay_seconds <= 0
            or self.restart.initial_delay_seconds > self.restart.max_delay_seconds
        ):
            raise RuntimeConfigError(
                "runtime restart delays must be positive and initial_delay_seconds "
                "cannot exceed max_delay_seconds"
            )
        if not (self.dir or "").strip():
            raise RuntimeConfigError("runtime plugin directory is required")


def valid_id(value: str) -> bool:
    """id 只允许小写字母、数字、``-`` 与 ``_``。对应 Go ``validID``。"""
    if not value:
        return False
    for char in value:
        if not ("a" <= char <= "z") and not ("0" <= char <= "9") and char not in "-_":
            return False
    return True


@dataclass
class Info:
    """``/hooks`` 展示的一条有状态 Hook 信息。对应 Go ``Info``。"""

    id: str = ""
    description: str = ""
    mode: str = Mode.ONCE
    status: str = ""
    detail: str = ""
    active: int = 0
    waiting: int = 0


class RuntimeLogger(Protocol):
    """运行期日志接口（``atri_x.logging.Logger`` 满足它）。

    上游用 ``*slog.Logger``；这里只要求 ``debug`` / ``info`` / ``error`` 与
    ``warn`` 或 ``warning`` 之一存在。
    """

    def debug(self, message: str, **fields: Any) -> None: ...

    def info(self, message: str, **fields: Any) -> None: ...

    def error(self, message: str, **fields: Any) -> None: ...


def log_warning(logger: Any, message: str, **fields: Any) -> None:
    """兼容 ``warn`` / ``warning`` 两种命名的告警输出。"""
    if logger is None:
        return
    for name in ("warning", "warn"):
        emit = getattr(logger, name, None)
        if callable(emit):
            emit(message, **fields)
            return
    emit = getattr(logger, "info", None)
    if callable(emit):
        emit(message, **fields)


@dataclass
class Options:
    """运行期依赖。对应 Go ``Options``。

    ``send`` 是 async 可调用：``await send(ctx, target, outputs) -> Receipt``。
    """

    media: Any = None
    registry: Any = None
    logger: Any = None
    audit: Callable[..., None] | None = None
    send: Callable[..., Any] | None = None
    shared_dir: str = ""
    process_env: ProcessEnvironment = field(default_factory=ProcessEnvironment)


def resolve_cwd(dir: str, cwd: str) -> str:
    """把插件目录内的相对 cwd 解析成绝对路径。对应 Go ``resolveCwd``。

    ``cwd`` 必须是相对路径且不能逃出插件目录，否则抛
    :class:`RuntimeConfigError`。
    """
    if os.path.isabs(cwd) or os.path.splitdrive(cwd)[0]:
        raise RuntimeConfigError("runtime cwd must be relative to plugin directory")
    base = os.path.normpath(dir or ".")
    path = os.path.normpath(os.path.join(base, cwd or "."))
    rel = os.path.relpath(path, base)
    if rel == ".." or rel.startswith(".." + os.sep) or rel.startswith("../"):
        raise RuntimeConfigError("runtime cwd escapes plugin directory")
    return path
