"""面向用户的错误呈现与"已通知用户"标记。

对应 Go：``internal/agent/errors.go``（``maxUserErrorRunes`` /
``userErrorDetails`` / ``newUserErrorDetails`` / ``safeError``）以及
``internal/agent/message.go`` 里的 ``userNotifiedError`` / ``markUserNotified`` /
``shouldNotifyUserError``。

Go 的 ``(T, error)`` 在 Python 里改为抛异常，因此这里没有哨兵错误类型：
``internal/agent/errors.go`` 本身也没有 ``ErrXxx``，只有"把底层错误脱敏、截断、
配一个可关联审计日志的 error_id"这两件事。agent 包用到的全部哨兵异常统一来自
:mod:`atri_x.errors`（见 :mod:`atri_x.agent` 的再导出）。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.redact import error as redact_error
from atri_x.redact import new_error_id, summarize

__all__ = [
    "MAX_USER_ERROR_RUNES",
    "UserErrorDetails",
    "UserNotifiedError",
    "is_user_notified",
    "mark_user_notified",
    "new_user_error_details",
    "safe_error",
    "should_notify_user_error",
    "unwrap_user_notified",
]

#: 用户可见错误正文的最大 rune 数。对应 Go ``maxUserErrorRunes``。
MAX_USER_ERROR_RUNES = 400


@dataclass
class UserErrorDetails:
    """一次用户可见错误的脱敏结果。对应 Go ``userErrorDetails``。"""

    id: str = ""
    safe: str = ""
    text: str = ""


def new_user_error_details(prefix: str, err: BaseException | None) -> UserErrorDetails:
    """构造脱敏、限长的用户可见错误，并保留可关联审计日志的 ``id``。

    对应 Go ``newUserErrorDetails``：``prefix`` 非空时输出
    ``<prefix>：<safe>（error_id=<id>）``，否则 ``<safe>（error_id=<id>）``。
    """
    ident = new_error_id()
    safe = summarize(redact_error(err), MAX_USER_ERROR_RUNES)
    if prefix:
        text = f"{prefix}：{safe}（error_id={ident}）"
    else:
        text = f"{safe}（error_id={ident}）"
    return UserErrorDetails(id=ident, safe=safe, text=text)


def safe_error(err: BaseException | None) -> BaseException | None:
    """把错误正文脱敏后重新包一层。对应 Go ``safeError``。"""
    if err is None:
        return None
    return Exception(summarize(str(err), MAX_USER_ERROR_RUNES))


class UserNotifiedError(Exception):
    """已经给用户发过提示的错误，外层不要再重复提示。

    对应 Go ``userNotifiedError``：Go 用 ``errors.As`` 判定，Python 用
    :func:`is_user_notified` / ``except UserNotifiedError``。
    """

    def __init__(self, err: BaseException) -> None:
        super().__init__(str(err))
        self.err = err

    def unwrap(self) -> BaseException:
        """对应 Go ``userNotifiedError.Unwrap``。"""
        return self.err


def mark_user_notified(err: BaseException | None) -> BaseException | None:
    """标记"这条错误已经通知过用户"。对应 Go ``markUserNotified``。"""
    if err is None:
        return None
    if isinstance(err, UserNotifiedError):
        return err
    return UserNotifiedError(err)


def is_user_notified(err: BaseException | None) -> bool:
    """递归判定错误链上是否带"已通知"标记。"""
    seen = 0
    current: BaseException | None = err
    while current is not None and seen < 16:
        if isinstance(current, UserNotifiedError):
            return True
        current = unwrap_user_notified(current)
        seen += 1
    return False


def unwrap_user_notified(err: BaseException) -> BaseException | None:
    """取一层被包裹的原始错误（``UserNotifiedError`` / ``__cause__``）。"""
    if isinstance(err, UserNotifiedError):
        return err.err
    cause = getattr(err, "__cause__", None)
    if isinstance(cause, BaseException):
        return cause
    return None


def should_notify_user_error(err: BaseException | None) -> bool:
    """是否应该把这条错误直接告诉用户。

    对应 Go ``shouldNotifyUserError``：已通知过的不再提示；``context`` 取消与
    超时属于用户自己打断的，也不提示。
    """
    if err is None:
        return False
    if is_user_notified(err):
        return False
    if is_cancellation(err):
        return False
    return True


def is_cancellation(err: BaseException | None) -> bool:
    """``context.Canceled`` / ``context.DeadlineExceeded`` 的 Python 等价判定。"""
    from atri_x.safecontext import CancelledError, DeadlineExceededError

    seen = 0
    current: BaseException | None = err
    while current is not None and seen < 16:
        if isinstance(current, (CancelledError, DeadlineExceededError, TimeoutError)):
            return True
        if isinstance(current, ExceptionGroup):  # pragma: no cover - 组合错误
            for inner in current.exceptions:
                if is_cancellation(inner):
                    return True
        current = current.__cause__
        seen += 1
    return False
