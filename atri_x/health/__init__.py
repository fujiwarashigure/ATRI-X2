"""健康状态、readiness 检查与独立健康 HTTP 服务端。

对应 Go：``internal/health``。

语义边界见 :mod:`atri_x.health.state` 的模块 docstring（``/live``、``/ready``、
``/healthz`` 三者的判定差异是契约）。
"""

from __future__ import annotations

from atri_x.health.checker import Checker, CheckerFunc, DirWritable
from atri_x.health.server import (
    DEFAULT_READ_HEADER_TIMEOUT_SECONDS,
    GO_ZERO_TIME,
    HTTPResponse,
    Server,
    ServerOptions,
    marshal_model_status,
    marshal_platform_status,
    marshal_snapshot,
    new_server,
)
from atri_x.health.state import (
    DEFAULT_LIVE_STALE_SECONDS,
    ModelStatus,
    Options,
    PlatformStatus,
    RedactSecrets,
    Snapshot,
    State,
    new_state,
    redact_secrets_health,
)

__all__ = [
    "DEFAULT_LIVE_STALE_SECONDS",
    "DEFAULT_READ_HEADER_TIMEOUT_SECONDS",
    "GO_ZERO_TIME",
    "Checker",
    "CheckerFunc",
    "DirWritable",
    "HTTPResponse",
    "ModelStatus",
    "Options",
    "PlatformStatus",
    "RedactSecrets",
    "Server",
    "ServerOptions",
    "Snapshot",
    "State",
    "marshal_model_status",
    "marshal_platform_status",
    "marshal_snapshot",
    "new_server",
    "new_state",
    "redact_secrets_health",
]
