"""通用类型化信号。

对应 Go：``internal/signal/``。

Go 侧 ``signal`` **不依赖任何** ``atri-x/internal/*`` 包，是上游唯一可整包
移植的东西；Python 侧同样只依赖标准库与 :mod:`atri_x.safecontext` /
:mod:`atri_x.errors`（后者是 Go 各包哨兵错误的共享出处）。

模块布局对应 Go 文件：``executor.py``（Options）、``shutdown.py``
（ShutdownPolicy/Task）、``queue.py``（Queue）、``result.py``
（Result/Emission）、``connection.py``（Connection）、``diagnostics.py``、
``errors.py``、``signal.py``（Signal）。
"""

from __future__ import annotations

from atri_x.signal.connection import Connection
from atri_x.signal.diagnostics import (
    diagnostics_written,
    dropped_diagnostics,
    flush_diagnostics,
    report_failure,
    reset_diagnostics,
)
from atri_x.signal.errors import (
    ErrClosed,
    ErrNoReceiver,
    ErrQueueFull,
    ErrRecursive,
    JoinedError,
    SignalError,
    filter_errors,
    is_error,
    join_errors,
)
from atri_x.signal.executor import ConnectOptions, Empty, Options
from atri_x.signal.queue import Queue, QueueOptions, new_queue
from atri_x.signal.result import Emission, Result, Resolved, map_results
from atri_x.signal.shutdown import (
    SHUTDOWN_POLICIES,
    CancelPending,
    Drain,
    ShutdownPolicy,
    Task,
)
from atri_x.signal.signal import Signal, new

__all__ = [name for name in dir() if not name.startswith("_")]
