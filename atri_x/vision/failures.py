"""识图失败的安全类别。

对应 Go：无（Go 只有 ``vision.Stats.Errors`` 里的错误串）。Python 侧把「为什么
这张图没描述出来」折叠成**有界、低基数**的类别串，供 :meth:`Service.stats`
与运维接口消费：

``download_failed`` / ``unsupported_format`` / ``too_large`` / ``timeout`` /
``cancelled`` / ``provider_error`` / ``privacy_blocked`` / ``empty_result`` /
``parse_failed`` / ``identity_disabled`` / ``identity_quota`` / ``identity_error``

这些类别**不含** media ID、图像字节、base64、URL 与凭据，因此可以安全地对外
暴露；分类只按异常类型（与 :mod:`atri_x.llm.errors` 的 ``category``）判定，
绝不按人类可读的 message 猜。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "FAILURE_CANCELLED",
    "FAILURE_DOWNLOAD_FAILED",
    "FAILURE_EMPTY_RESULT",
    "FAILURE_IDENTITY_DISABLED",
    "FAILURE_IDENTITY_ERROR",
    "FAILURE_IDENTITY_QUOTA",
    "FAILURE_KINDS",
    "FAILURE_PARSE_FAILED",
    "FAILURE_PRIVACY_BLOCKED",
    "FAILURE_PROVIDER_ERROR",
    "FAILURE_TIMEOUT",
    "FAILURE_TOO_LARGE",
    "FAILURE_UNSUPPORTED_FORMAT",
    "failure_kind",
]

FAILURE_DOWNLOAD_FAILED = "download_failed"
FAILURE_UNSUPPORTED_FORMAT = "unsupported_format"
FAILURE_TOO_LARGE = "too_large"
FAILURE_TIMEOUT = "timeout"
FAILURE_CANCELLED = "cancelled"
FAILURE_PROVIDER_ERROR = "provider_error"
FAILURE_PRIVACY_BLOCKED = "privacy_blocked"
FAILURE_EMPTY_RESULT = "empty_result"
FAILURE_PARSE_FAILED = "parse_failed"
FAILURE_IDENTITY_DISABLED = "identity_disabled"
FAILURE_IDENTITY_QUOTA = "identity_quota"
FAILURE_IDENTITY_ERROR = "identity_error"

#: 全部合法类别（顺序即运维视图里的输出顺序）。
FAILURE_KINDS = (
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_UNSUPPORTED_FORMAT,
    FAILURE_TOO_LARGE,
    FAILURE_TIMEOUT,
    FAILURE_CANCELLED,
    FAILURE_PROVIDER_ERROR,
    FAILURE_PRIVACY_BLOCKED,
    FAILURE_EMPTY_RESULT,
    FAILURE_PARSE_FAILED,
    FAILURE_IDENTITY_DISABLED,
    FAILURE_IDENTITY_QUOTA,
    FAILURE_IDENTITY_ERROR,
)


def failure_kind(err: BaseException | None) -> str:
    """把一个识图失败折叠成 :data:`FAILURE_KINDS` 里的一个类别；无错返回空串。"""
    if err is None:
        return ""
    kind = _cancellation_kind(err)
    if kind:
        return kind
    if _privacy_blocked(err):
        return FAILURE_PRIVACY_BLOCKED
    if _too_large(err):
        return FAILURE_TOO_LARGE
    if _unsupported_format(err):
        return FAILURE_UNSUPPORTED_FORMAT
    if _api_error(err):
        return FAILURE_PROVIDER_ERROR
    if _download_failure(err):
        return FAILURE_DOWNLOAD_FAILED
    if _empty_result(err):
        return FAILURE_EMPTY_RESULT
    return FAILURE_PROVIDER_ERROR


# ---------------------------------------------------------------------------
# 内部判定
# ---------------------------------------------------------------------------
def _is_cancellation(err: BaseException) -> bool:
    from atri_x.vision.cache import is_cancellation

    return bool(is_cancellation(err))


def _cancellation_kind(err: BaseException) -> str:
    """取消链里出现截止时间就记 ``timeout``，否则记 ``cancelled``。"""
    if not _is_cancellation(err):
        return ""
    from atri_x.safecontext import DeadlineExceededError

    current: Any = err
    seen = 0
    while current is not None and seen < 32:
        if isinstance(current, (TimeoutError, DeadlineExceededError)):
            return FAILURE_TIMEOUT
        current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
        seen += 1
    return FAILURE_CANCELLED


def _privacy_blocked(err: BaseException) -> bool:
    from atri_x.vision.service_types import PrivacyBlockedError

    return isinstance(err, PrivacyBlockedError)


def _too_large(err: BaseException) -> bool:
    from atri_x.errors import MediaTooLargeError

    if isinstance(err, MediaTooLargeError):
        return True
    try:
        from atri_x.media.imagecodec.image import ImageTooLargeError

        return isinstance(err, ImageTooLargeError)
    except Exception:  # noqa: BLE001 - 可选后端缺失时只按 MediaTooLargeError 判定
        return False


def _unsupported_format(err: BaseException) -> bool:
    try:
        from atri_x.media.imagecodec.image import ImageCodecError

        return isinstance(err, ImageCodecError)
    except Exception:  # noqa: BLE001
        return False


def _api_error(err: BaseException) -> bool:
    from atri_x.llm.errors import as_api_error

    return as_api_error(err) is not None


def _download_failure(err: BaseException) -> bool:
    """取字节失败：媒体不存在/过期/读超时/传输错误都归到这里。"""
    from atri_x.errors import NotFoundError
    from atri_x.media import MediaError

    return isinstance(err, (NotFoundError, MediaError, OSError, ConnectionError))


def _empty_result(err: BaseException) -> bool:
    if not isinstance(err, ValueError):
        return False
    return "empty" in str(err).lower()
