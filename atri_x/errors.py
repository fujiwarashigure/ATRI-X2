"""全仓哨兵异常。

对应 Go：各包里的 ``var ErrXxx = errors.New(...)``。
Go 的 ``(T, error)`` 返回在 Python 中改为抛异常；需要按身份判定时用
``except`` 捕获具体类，或用 ``is`` 比较模块级单例（哨兵实例）。

命名规则：Go 的 ``ErrXxx`` → Python 类 ``XxxError``，同名的模块级单例
``Xxx = XxxError()`` 便于 `raise ErrNotFound` 与 `err is ErrNotFound` 两种写法。
"""

from __future__ import annotations


class AtriXError(Exception):
    """全仓异常基类。"""


class NotFoundError(AtriXError):
    pass


class ConflictError(AtriXError):
    pass


class ForbiddenError(AtriXError):
    pass


class InvalidError(AtriXError):
    pass


class DisabledError(AtriXError):
    pass


class LimitExceededError(AtriXError):
    pass


class InvalidIDError(AtriXError):
    pass


class AmbiguousIDError(AtriXError):
    pass


class CandidateNotFoundError(AtriXError):
    pass


class ContentTooLongError(AtriXError):
    pass


class FileChangedError(AtriXError):
    """编辑/回滚时文件自编辑后已被改动。对应 Go ``errFileChanged``。"""


class QueueFullError(AtriXError):
    """有界队列已满（背压，不是故障）。"""


class QueueTimeoutError(AtriXError):
    pass


class QueueCancelledError(AtriXError):
    pass


class ClosedError(AtriXError):
    pass


class ConcurrencyLimitError(AtriXError):
    pass


class MediaTooLargeError(AtriXError):
    pass


class NoReceiverError(AtriXError):
    pass


class RecursiveError(AtriXError):
    pass


class PromptOverflowError(AtriXError):
    """对应 Go ``errPromptOverflow``。"""


class BackgroundTakenOverError(AtriXError):
    """后台 turn 被前台接管。对应 Go ``errBackgroundTakenOver``。"""


class SessionNotIdleError(AtriXError):
    pass


class ForegroundSessionError(AtriXError):
    pass


class ChatModeRequiresEmptySessionError(AtriXError):
    pass


class TitleRenamedError(AtriXError):
    """异步命名迟到、不覆盖手动改名。对应 Go ``errTitleRenamed``。"""


class ModelSnapshotNameEmptyError(AtriXError):
    pass


class FallbackTotalTimeoutError(AtriXError):
    """对应 Go ``errFallbackTotalTimeout``。"""


class OpenError(AtriXError):
    """熔断器打开。对应 Go ``ErrOpen``。"""


class ImageAnalysisEmptyError(AtriXError):
    pass


class TooManyWaitersError(AtriXError):
    pass


class CLIClientFallbackError(AtriXError):
    pass


class HealthzDisabledError(AtriXError):
    pass


class RollbackConflictError(AtriXError):
    pass


class RollbackNotFoundError(AtriXError):
    pass


class LearningDisabledError(AtriXError):
    pass


class ObservationRateLimitedError(AtriXError):
    pass


class ObservationLimitError(AtriXError):
    pass


class ObservationTooLongError(AtriXError):
    pass


class ScopeFullError(AtriXError):
    pass


class WriteRateLimitedError(AtriXError):
    """写入被限流（对应 Go ``angelmemory.errWriteRateLimited``）。

    Go 里这个哨兵是**未导出**的（``errWriteRateLimited``），只被
    ``internal/angelmemory`` 内部用。Python 侧没有包内私有哨兵的等价物，
    而多个调用点（记忆写入、自我学习写入）都要判它，所以收进这里统一持有，
    由 ``atri_x.angelmemory`` 再导出。
    """


class InvalidTimezoneError(InvalidError):
    """时区名/固定偏移无法解析。

    这是**硬失败**：不允许静默回落到进程本地时区或 UTC——回落会让
    ``[quiet]`` / cron / 提醒的判定口径在用户不知情的情况下改变。
    """


class TimezoneDataUnavailableError(InvalidError):
    """IANA 时区数据（``tzdata``）缺失或不可读。

    与 :class:`InvalidTimezoneError` 分开，因为修复动作不同：前者是**写错了时区名**，
    后者是**装漏了时区数据库**（Windows 缺 ``tzdata`` 包 / 冻结版没打进 ``_internal``）。
    """


class TimeVerificationError(AtriXError):
    """网络时间校验的配置/使用错误（来源不允许、模式未实现等）。"""


class TimeSourceRejectedError(TimeVerificationError):
    """某个时间来源被出站策略拒绝（非公网地址未显式放行、URL 非法等）。"""


class RangeUnsupportedError(AtriXError):
    """被包装的仓库不支持"按时间窗批量读取"这一可选能力。

    对应 Go ``internal/historygate`` 的包内私有哨兵 ``errRangeUnsupported``
    （``&unsupportedError{}``，消息 ``history range access is not supported by the
    wrapped repository``）。Go 里它未导出、只被 ``historygate`` 用；Python 侧没有
    包内私有哨兵的等价物，且调用方（群分析等）需要按身份判定"是不是不支持"而不是
    去比对消息文本，因此按 PORTING §1.5 收进这里统一持有，由
    ``atri_x.historygate`` 再导出。
    """


# 注意：**不要**在这里定义 `class ErrQueueFull`。
# 下面的哨兵块里 `ErrQueueFull = QueueFullError("queue full")` 会把同名类覆盖成一个
# 实例，于是 `isinstance(x, ErrQueueFull)` 抛 TypeError（`arg 2 must be a type`）、
# `except ErrQueueFull` 也在运行时才炸。曾真实存在，已删。
# 校验由 `scripts/check_integration.py` 的 sentinel/class 同名检查守着。


# ---------------------------------------------------------------------------
# 哨兵单例：Go 的 `err is ErrXxx` 判定在 Python 里写成 `err is ErrXxx`。
# ---------------------------------------------------------------------------
ErrNotFound = NotFoundError("not found")
ErrConflict = ConflictError("conflict")
ErrForbidden = ForbiddenError("forbidden")
ErrInvalid = InvalidError("invalid")
ErrDisabled = DisabledError("disabled")
ErrLimitExceeded = LimitExceededError("limit exceeded")
ErrInvalidID = InvalidIDError("invalid id")
ErrAmbiguousID = AmbiguousIDError("ambiguous id")
ErrCandidateNotFound = CandidateNotFoundError("candidate not found")
ErrContentTooLong = ContentTooLongError("content too long")
ErrFileChanged = FileChangedError("file changed")
ErrQueueFull = QueueFullError("queue full")
ErrQueueTimeout = QueueTimeoutError("queue timeout")
ErrQueueCancelled = QueueCancelledError("queue cancelled")
ErrClosed = ClosedError("closed")
ErrConcurrencyLimit = ConcurrencyLimitError("concurrency limit")
ErrMediaTooLarge = MediaTooLargeError("media too large")
ErrNoReceiver = NoReceiverError("no receiver")
ErrRecursive = RecursiveError("recursive")
ErrPromptOverflow = PromptOverflowError("prompt overflow")
ErrBackgroundTakenOver = BackgroundTakenOverError("background taken over")
ErrSessionNotIdle = SessionNotIdleError("session not idle")
ErrForegroundSession = ForegroundSessionError("foreground session")
ErrChatModeRequiresEmptySession = ChatModeRequiresEmptySessionError("chat mode requires empty session")
ErrTitleRenamed = TitleRenamedError("title renamed")
ErrModelSnapshotNameEmpty = ModelSnapshotNameEmptyError("model snapshot name empty")
ErrFallbackTotalTimeout = FallbackTotalTimeoutError("fallback total timeout")
ErrOpen = OpenError("breaker open")
ErrImageAnalysisEmpty = ImageAnalysisEmptyError("image analysis empty")
ErrTooManyWaiters = TooManyWaitersError("too many waiters")
ErrCLIClientFallback = CLIClientFallbackError("cli client fallback")
ErrHealthzDisabled = HealthzDisabledError("healthz disabled")
ErrRollbackConflict = RollbackConflictError("rollback conflict")
ErrRollbackNotFound = RollbackNotFoundError("rollback not found")
ErrLearningDisabled = LearningDisabledError("learning disabled")
ErrObservationRateLimited = ObservationRateLimitedError("observation rate limited")
ErrObservationLimit = ObservationLimitError("observation limit")
ErrObservationTooLong = ObservationTooLongError("observation too long")
ErrScopeFull = ScopeFullError("scope full")
ErrWriteRateLimited = WriteRateLimitedError("write rate limited")
ErrRangeUnsupported = RangeUnsupportedError(
    "history range access is not supported by the wrapped repository"
)
ErrInvalidTimezone = InvalidTimezoneError("invalid timezone")
ErrTimezoneDataUnavailable = TimezoneDataUnavailableError("timezone data unavailable")
ErrTimeVerification = TimeVerificationError("time verification error")
ErrTimeSourceRejected = TimeSourceRejectedError("time source rejected")


__all__ = [name for name in dir() if not name.startswith("_")]
