"""沙箱错误类型。

沙箱是**拒绝执行**的安全边界：任何「后端不可用 / 隔离未验证 / 配额超限 / 排队超时」
都必须以明确异常结束，调用方（工具层）把异常回灌给模型，绝不静默回落宿主 Shell。

异常层级方便上层只按大类处理：

* :class:`SandboxUnavailableError` —— 后端缺失、镜像缺失、探测失败；
* :class:`SandboxDeniedError` —— 越界路径、被拒网络目标、被策略拒绝；
* :class:`SandboxQuotaError` —— 资源/流量/队列配额；
* :class:`SandboxTimeoutError` —— 命令时限、排队等待时限；
* :class:`SandboxBackendError` —— 引擎命令失败（容器崩溃、无法回收等）。

``kind`` 是稳定字符串，写进审计与工具错误文本，便于统计（区分普通失败 / OOM /
磁盘超限 / 命令超时 / 后端故障）。
"""

from __future__ import annotations

__all__ = [
    "SandboxBackendError",
    "SandboxDeniedError",
    "SandboxError",
    "SandboxQuotaError",
    "SandboxQueueTimeoutError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
]


class SandboxError(Exception):
    """所有沙箱错误的基类。"""

    #: 稳定错误类别（审计与错误文本用）。
    kind = "sandbox_error"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = str(message or "").strip()
        self.detail = str(detail or "").strip()

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}（{self.detail}）"
        return self.message


class SandboxUnavailableError(SandboxError):
    """沙箱后端不可用：没有引擎、镜像缺失、隔离探测没通过。

    这是**硬失败**：调用方不得回退宿主执行。
    """

    kind = "unavailable"


class SandboxDeniedError(SandboxError):
    """请求被沙箱策略拒绝（越界路径、被禁网络目标、权限不足）。"""

    kind = "denied"


class SandboxQuotaError(SandboxError):
    """配额超限（内存/磁盘/进程/流量/单租户排队数）。"""

    kind = "quota"


class SandboxTimeoutError(SandboxError):
    """命令超时或取消。"""

    kind = "timeout"


class SandboxQueueTimeoutError(SandboxTimeoutError):
    """排队等待超过上限。"""

    kind = "queue_timeout"


class SandboxBackendError(SandboxError):
    """容器引擎执行失败（创建/启动/回收失败，或容器异常退出无法解释）。"""

    kind = "backend"
