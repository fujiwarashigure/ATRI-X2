"""上下文块渲染与取消令牌。

对应 Go：``internal/safecontext/safecontext.go``。

上游 ``safecontext`` 只做一件事：把不可信文本安全地放进带分隔符的
prompt-context 块里（``EscapeBoundary`` / ``NormalizeInline`` / ``Inline`` /
``TruncateRunes``）。

本模块**额外**提供 :class:`CancelToken`：Python 标准库没有 Go 的
``context.Context``，而本仓的 request / signal 等异步路径需要等价的
「取消 + 截止时间 + 值传递 + 子上下文」语义（PORTING.md §1.6）。它没有 Go
对应文件，属于 Python 侧的必需新增件；命名与 Go 的 ``context`` 包一一对应
（``WithCancel`` → :func:`with_cancel`，``WithTimeout`` → :func:`with_timeout`，
``WithValue`` → :meth:`CancelToken.with_value`）。
"""

from __future__ import annotations

import asyncio
import atexit
import sys
import threading
import traceback
import unicodedata
import weakref
from collections.abc import Mapping
from typing import Any, Callable

from atri_x.errors import AtriXError
from atri_x.redact import secrets as redact_secrets

__all__ = [
    "CancelToken",
    "CancelledError",
    "DeadlineExceededError",
    "ErrCancelled",
    "ErrDeadlineExceeded",
    "EscapeBoundary",
    "Inline",
    "NormalizeInline",
    "TruncateRunes",
    "escape_boundary",
    "inline",
    "normalize_inline",
    "token_from_ctx",
    "truncate_runes",
    "with_cancel",
    "with_timeout",
]


def _print_redacted_exception() -> None:
    """把当前异常的回溯**脱敏后**写到 stderr。

    取消回调是用户/插件代码，异常正文可能带密钥；这里不再直接
    ``traceback.print_exc``（它会原样落 stderr，绕过日志侧的脱敏）。
    """
    try:
        text = traceback.format_exc()
    except Exception:  # pragma: no cover - 取回溯本身失败不应再抛
        return
    try:
        sys.stderr.write(redact_secrets(text))
    except Exception:  # pragma: no cover - stderr 不可写时静默
        pass


# ---------------------------------------------------------------------------
# 文本边界（对应 Go safecontext.go）
# ---------------------------------------------------------------------------

def escape_boundary(value: str) -> str:
    """中和可能伪造或闭合 XML 风格边界标签的字符。

    与号先替换，所以 ``&lt;/angel_memory&gt;`` 这种输入不会作为实体存活下来、
    又被模型解码回闭合标签。
    """
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Go 的 unicode.IsSpace 是 White_Space 属性；Python 的 str.isspace() 覆盖面
# 更宽（含 U+001C..U+001F 等），这里显式列出以保证逐字一致。
_WHITE_SPACE = frozenset(
    "\t\n\v\f\r \x85\xa0\u1680"
    + "".join(chr(cp) for cp in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000"
)

_INVISIBLE_EXPLICIT = frozenset("\u200b\u200c\u200d\u2060\ufeff")


def _is_invisible(ch: str) -> bool:
    category = unicodedata.category(ch)
    if category == "Cc":
        return True
    if ch in _INVISIBLE_EXPLICIT:
        return True
    # Go: unicode.In(r, unicode.Cf, unicode.Co, unicode.Cs)
    return category in ("Cf", "Co", "Cs")


def normalize_inline(value: str) -> str:
    """把不可信文本折叠成一行可见文本。

    所有 Unicode 空白串折叠成一个 ASCII 空格，控制/格式字符被丢弃，
    零宽与双向不可见字符被移除。
    """
    out: list[str] = []
    pending_space = False
    for ch in value:
        if ch in _WHITE_SPACE:
            pending_space = True
            continue
        if _is_invisible(ch):
            continue
        if pending_space and out:
            out.append(" ")
        pending_space = False
        out.append(ch)
    return "".join(out)


def inline(value: str) -> str:
    """归一化为一行并转义边界字符。"""
    return escape_boundary(normalize_inline(value))


def truncate_runes(value: str, max_runes: int) -> str:
    """截断到最多 ``max_runes`` 个 rune，发生截断时补省略号。

    ``max_runes <= 0`` 返回空串（与 Go 一致）。
    """
    if max_runes <= 0:
        return ""
    if len(value) <= max_runes:
        return value
    if max_runes == 1:
        return "…"
    return value[: max_runes - 1] + "…"


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
EscapeBoundary = escape_boundary
NormalizeInline = normalize_inline
Inline = inline
TruncateRunes = truncate_runes


# ---------------------------------------------------------------------------
# 取消令牌（Python 侧新增，对应 Go 的 context.Context）
# ---------------------------------------------------------------------------

class CancelledError(AtriXError):
    """对应 Go ``context.Canceled``。"""


class DeadlineExceededError(AtriXError):
    """对应 Go ``context.DeadlineExceeded``。"""


ErrCancelled = CancelledError("context canceled")
ErrDeadlineExceeded = DeadlineExceededError("context deadline exceeded")

# 活跃的根 token：仅用于 atexit 兜底取消，弱引用避免阻止回收。
_LIVE_TOKENS: "weakref.WeakSet[CancelToken]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False
_ATEXIT_LOCK = threading.Lock()


def _register_atexit(token: "CancelToken") -> None:
    global _ATEXIT_REGISTERED
    _LIVE_TOKENS.add(token)
    if _ATEXIT_REGISTERED:
        return
    with _ATEXIT_LOCK:
        if _ATEXIT_REGISTERED:
            return
        atexit.register(_cancel_all_at_exit)
        _ATEXIT_REGISTERED = True


def _cancel_all_at_exit() -> None:
    for token in list(_LIVE_TOKENS):
        try:
            token.cancel("interpreter exit")
        except Exception:  # pragma: no cover - 退出期不应再抛
            pass


class CancelToken:
    """``context.Context`` 的取消语义在 Python 的对应物。

    支持 Go 的四件事：取消（:meth:`cancel`）、截止时间
    (:func:`with_timeout`)、值传递（:meth:`with_value`）、子上下文
    (:func:`with_cancel`)。父 token 被取消时，所有子 token 一并取消。

    用法::

        token = with_cancel(parent)     # context.WithCancel
        try:
            token.raise_if_cancelled()
        finally:
            token.cancel()

    也可作为上下文管理器使用，``__exit__`` 时自动取消::

        with with_timeout(None, 5.0) as token:
            ...

    ``atexit`` 语义：根 token（无父）会登记进一个弱集合，解释器退出时被
    统一取消，避免子进程/后台任务在解释器收尾后继续存活。
    """

    __slots__ = (
        "__weakref__",
        "_atexit",
        "_callback_errors",
        "_callbacks",
        "_cancelled",
        "_children",
        "_error",
        "_parent",
        "_reason",
        "_timer",
        "_values",
    )

    def __init__(self, parent: "CancelToken | None" = None) -> None:
        self._parent = parent
        self._cancelled = False
        self._reason: str | None = None
        self._error: BaseException | None = None
        self._callbacks: list[Callable[[], None]] = []
        self._children: list[weakref.ref[CancelToken]] = []
        self._values: dict[Any, Any] = {}
        self._timer: Any = None
        self._callback_errors: list[BaseException] = []
        self._atexit = False
        if parent is None:
            self._atexit = True
            _register_atexit(self)
        else:
            parent._register_child(self)

    # -- 构造 ---------------------------------------------------------------

    @staticmethod
    def with_cancel(parent: "CancelToken | None" = None) -> "CancelToken":
        """对应 Go ``context.WithCancel``。"""
        return with_cancel(parent)

    @staticmethod
    def with_timeout(parent: "CancelToken | None", timeout: float | None) -> "CancelToken":
        """对应 Go ``context.WithTimeout``。"""
        return with_timeout(parent, timeout)

    def _register_child(self, child: "CancelToken") -> None:
        self._children = [ref for ref in self._children if ref() is not None]
        self._children.append(weakref.ref(child))
        if self._cancelled:
            child.cancel(self._reason, self._error)

    # -- 状态 ---------------------------------------------------------------

    def is_cancelled(self) -> bool:
        return self._cancelled

    # Go 的接口方法名叫 Done()/Err()，这里保留 is_done/error 便于阅读。
    def is_done(self) -> bool:
        return self._cancelled

    def cancel_reason(self) -> str | None:
        """返回取消原因字符串（未取消时为 ``None``）。"""
        return self._reason

    def error(self) -> BaseException | None:
        """对应 Go ``ctx.Err()``。"""
        return self._error

    def error_or(self, fallback: BaseException) -> BaseException:
        return self._error if self._error is not None else fallback

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise self._error if self._error is not None else ErrCancelled

    def callback_errors(self) -> list[BaseException]:
        """回调里被吞掉的异常（正常运行时为空）。"""
        return list(self._callback_errors)

    # -- 取消 ---------------------------------------------------------------

    def cancel(self, reason: str | None = None, error: BaseException | None = None) -> None:
        """取消本 token 及其所有子 token。幂等：第一次调用生效。"""
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        self._error = error if error is not None else CancelledError(reason or "context canceled")
        self._stop_timer()
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:  # 回调异常不能中断取消传播
                self._callback_errors.append(exc)
                _print_redacted_exception()
        children, self._children = self._children, []
        for ref in children:
            child = ref()
            if child is not None:
                child.cancel(self._reason, self._error)

    def _expire(self) -> None:
        self.cancel("deadline exceeded", DeadlineExceededError("context deadline exceeded"))

    def _stop_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # pragma: no cover - 定时器取消不应失败
                pass

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册取消回调，返回注销函数（对应 Go ``context.AfterFunc``）。"""
        if self._cancelled:
            try:
                callback()
            except Exception as exc:
                self._callback_errors.append(exc)
                _print_redacted_exception()
            return lambda: None
        self._callbacks.append(callback)

        def _remove() -> None:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

        return _remove

    # -- 值传递 -------------------------------------------------------------

    def with_value(self, key: Any, value: Any) -> "CancelToken":
        """对应 Go ``context.WithValue``，返回携带该值的子 token。"""
        child = with_cancel(self)
        child._values = {key: value}
        return child

    def value(self, key: Any, default: Any = None) -> Any:
        token: CancelToken | None = self
        while token is not None:
            if key in token._values:
                return token._values[key]
            token = token._parent
        return default

    # -- 等待 ---------------------------------------------------------------

    async def wait(self, timeout: float | None = None) -> None:
        """asyncio 友好：等待被取消。

        已取消时立即返回；``timeout`` 秒内未被取消则抛 ``TimeoutError``。
        """
        if self._cancelled:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _resolve() -> None:
            if not future.done():
                future.set_result(None)

        def _on_cancel() -> None:
            try:
                loop.call_soon_threadsafe(_resolve)
            except RuntimeError:  # pragma: no cover - loop 已关闭
                _resolve()

        remove = self.add_callback(_on_cancel)
        try:
            if timeout is None:
                await future
            else:
                await asyncio.wait_for(future, timeout)
        finally:
            remove()

    def wait_sync(self, timeout: float | None = None) -> bool:
        """线程路径的等待：被取消返回 ``True``，超时返回 ``False``。"""
        if self._cancelled:
            return True
        event = threading.Event()
        remove = self.add_callback(event.set)
        try:
            return event.wait(timeout)
        finally:
            remove()

    # -- 上下文管理器 -------------------------------------------------------

    def __enter__(self) -> "CancelToken":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.cancel("context closed")
        return False

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = f"cancelled({self._reason})" if self._cancelled else "active"
        return f"<CancelToken {state}>"


def with_cancel(parent: CancelToken | None = None) -> CancelToken:
    """对应 Go ``context.WithCancel``。"""
    return CancelToken(parent)


def with_timeout(parent: CancelToken | None, timeout: float | None) -> CancelToken:
    """对应 Go ``context.WithTimeout``。

    ``timeout`` 为 ``None`` 或非正数时立即到期（Go 的 ``WithTimeout`` 对
    非正超时的行为就是立刻 Done）。
    """
    token = CancelToken(parent)
    if timeout is None:
        return token
    if timeout <= 0:
        token.cancel("deadline exceeded", DeadlineExceededError("context deadline exceeded"))
        return token
    token._timer = _arm_timer(timeout, token._expire)
    return token


def token_from_ctx(ctx: Any) -> CancelToken | None:
    """把任意形式的 ctx 归一成可挂父级的 :class:`CancelToken`。

    本仓的 ctx 有两种表示并存：

    * :class:`CancelToken` —— 带取消/截止时间/值的完整实现；
    * 普通 ``Mapping`` —— 早期路径（以及 ``_RunContext`` 之类不带 ``with_value``
      的对象经过 :func:`atri_x.delivery.ctx_with_value` 之后）产生的"值包"，
      没有取消语义。

    Go 里只有一个 ``context.Context``。当上层拿到的是 Mapping 又要
    ``context.WithCancel/WithTimeout`` 时，必须先把它携带的键值搬进 token，
    否则新 token 的 ``value()`` 链路是空的 —— 典型症状是聊天的输出落点
    （``MessageContext.sender``）在 ``requests.start`` 之后丢失，最终退到 headless
    service 平台并报 "service platform cannot send chat output without a target
    platform"。

    返回 ``None`` 表示没有可继承的父级（调用方按无父 token 处理）。
    """
    if ctx is None or isinstance(ctx, CancelToken):
        return ctx
    if isinstance(ctx, Mapping):
        token = with_cancel(None)
        token._values = dict(ctx)
        return token
    return None


def _arm_timer(timeout: float, callback: Callable[[], None]) -> Any:
    """在事件循环线程用 ``call_later``，否则退化为守护线程定时器。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        return loop.call_later(timeout, callback)
    timer = threading.Timer(timeout, callback)
    timer.daemon = True
    timer.start()
    return timer
