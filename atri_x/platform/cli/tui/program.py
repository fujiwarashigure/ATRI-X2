"""TUI 主循环与键盘事件源：``run_tui`` 的落地。

对应 Go：``tui.go`` 的 ``runTUI``（``tea.NewProgram(...).Run()``）与
``tui.go`` 里的 ``waitTUIOutput`` / ``tickTUIStatus``。

Python 没有 bubbletea，本模块用 asyncio 复刻它的运行时：

* **消息源**：外部输出队列（``chan tea.Msg`` 的等价物）、键盘事件源、每秒一次的
  状态栏时钟，三条流汇入一个内部队列；``program.send(msg)`` 也直接投进该队列
  （对应 ``tea.Program.Send``，适配器的 ``send_tui_message`` 依赖它）。
* **动作解释**：:meth:`TuiModel.update` 返回的动作列表在这里执行（见 ``model`` 模块
  的说明）——退出、读剪贴板、把输入交给 handler、异步补全。
* **渲染**：终端上整帧重绘（清屏 + 输出 ``model.view()``）；**非终端**（管道、
  测试）只在整个循环结束时输出最终一帧，避免把逐帧刷屏写进日志。
* **ctx 取消**：与 :mod:`atri_x.platform.telegram.adapter` 同口径 —— 取消时抛
  ``ctx_error(ctx)``，并停掉全部后台任务。

键盘事件源可注入（``key_source``）。默认实现:

* Windows：``msvcrt.getwch()``，含 ``\\x00`` / ``\\xe0`` 前缀的功能键解码；
* POSIX：``termios`` + ``tty`` 原始模式逐字节读，含 CSI 转义序列解码；
* 非交互终端：行模式 —— 每次读一行，拆成字符 ``KeyMsg`` 后补一个 ``enter``，
  EOF 结束循环（对应"管道里喂完就退出"）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from atri_x.delivery.delivery import ctx_error
from atri_x.platform.cli.clipboard import read_tui_clipboard
from atri_x.platform.cli.tui.keys import (
    KEY_BACKSPACE,
    KEY_CTRL_C,
    KEY_CTRL_D,
    KEY_CTRL_H,
    KEY_CTRL_J,
    KEY_CTRL_K,
    KEY_CTRL_U,
    KEY_CTRL_V,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_ESC,
    KEY_HOME,
    KEY_LEFT,
    KEY_PGDOWN,
    KEY_PGUP,
    KEY_RIGHT,
    KEY_SHIFT_TAB,
    KEY_TAB,
    KEY_UP,
    KeyMsg,
    MouseMsg,
)
from atri_x.platform.cli.tui.model import (
    ACTION_COMPLETE,
    ACTION_DISPATCH,
    ACTION_QUIT,
    ACTION_READ_CLIPBOARD,
    CompleteRequest,
    TuiModel,
    new_tui_model,
)
from atri_x.platform.cli.tui.messages import TuiClipboardMsg, TuiNoticeMsg, TuiStatusTickMsg

#: 状态栏时钟间隔（秒）。对应 Go ``tea.Tick(time.Second, ...)``。
STATUS_TICK_INTERVAL = 1.0

#: ctx 取消的轮询间隔（秒）：没有 ``add_callback`` 的 ctx 走轮询。
CTX_POLL_INTERVAL = 0.1

#: 整帧重绘的清屏序列。
CLEAR_SCREEN = "\x1b[H\x1b[2J"

#: 键盘事件源 EOF 的占位标记（内部队列里出现它就退出主循环）。
EOF_MARKER = "@@tui-eof@@"

__all__ = [
    "CLEAR_SCREEN",
    "CTX_POLL_INTERVAL",
    "EOF_MARKER",
    "KEY_UNKNOWN_INSERT",
    "MESSAGE_TYPES",
    "STATUS_TICK_INTERVAL",
    "CallbackKeySource",
    "StdinKeySource",
    "TuiProgram",
    "iter_key_messages",
    "run_tui",
]


def _write(stream: Any, text: str) -> None:
    """写一段文本，编码不下时退到 ASCII 转义（PORTING §7.4）。"""
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding, "replace"))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


class TuiProgram:
    """暴露给适配器的句柄：``send(msg)`` 把消息投进 TUI 的消息队列。

    对应 ``tea.Program``（本 TUI 只用它的 ``Send``）。
    """

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self.queue = queue
        self._closed = False

    def send(self, msg: Any) -> None:
        """对应 ``tea.Program.Send``：队列满时丢弃（Go 侧同样是非阻塞投递）。"""
        if self._closed:
            return
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def close(self) -> None:
        """停止接收后续消息（循环退出时调用）。"""
        self._closed = True


# ---------------------------------------------------------------------------
# 键盘事件源
# ---------------------------------------------------------------------------

@dataclass
class CallbackKeySource:
    """测试用事件源：按顺序吐出一串消息，吐完即 EOF。"""

    messages: list[Any] = field(default_factory=list)
    delay: float = 0.0

    async def read(self) -> Any:
        """返回下一条消息；没有更多时返回 ``None``（EOF）。"""
        if not self.messages:
            return None
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.messages.pop(0)


class StdinKeySource:
    """从标准输入读按键。对应 bubbletea 的输入读取循环。

    ``interactive`` 为 ``None`` 时按 ``os.isatty(stdin.fileno())`` 探测。
    """

    def __init__(self, stream: Any = None, interactive: bool | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdin
        self._interactive = interactive
        self._restore: Callable[[], None] | None = None
        self._eof = False
        self._pending: list[KeyMsg] = []
        self._windows_prefix = False

    def is_interactive(self) -> bool:
        if self._interactive is not None:
            return bool(self._interactive)
        fileno = getattr(self.stream, "fileno", None)
        if not callable(fileno):
            return False
        try:
            return bool(os.isatty(fileno()))
        except (OSError, ValueError, AttributeError):
            return False

    async def read(self) -> KeyMsg | None:
        """读一个按键；EOF / 读到 ``ctrl+d`` 时返回 ``None``。"""
        if self._pending:
            return self._pending.pop(0)
        if self._eof:
            return None
        if not self.is_interactive():
            return await self._read_line_mode()
        try:
            return await asyncio.to_thread(self._read_raw_key)
        finally:
            self._restore_terminal()

    # -- 行模式 -------------------------------------------------------------
    async def _read_line_mode(self) -> KeyMsg | None:
        line = await asyncio.to_thread(self._readline)
        if line is None:
            self._eof = True
            return None
        runes = line
        if runes.endswith("\n"):
            runes = runes[:-1]
        if runes.endswith("\r"):
            runes = runes[:-1]
        if runes:
            self._pending.append(KeyMsg(type=KEY_ENTER))
            return KeyMsg.runes_key(runes)
        return KeyMsg(type=KEY_ENTER)

    def _readline(self) -> str | None:
        reader = getattr(self.stream, "readline", None)
        if not callable(reader):
            return None
        try:
            raw = reader()
        except (OSError, ValueError):  # pragma: no cover - 流已关闭
            return None
        return raw if raw else None

    # -- 原始模式 -----------------------------------------------------------
    def _read_raw_key(self) -> KeyMsg | None:
        """同步读一个键（在 to_thread 里跑）。"""
        if sys.platform.startswith("win"):
            return self._read_windows_key()
        return self._read_posix_key()

    def _read_windows_key(self) -> KeyMsg | None:
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            return _windows_special(second)
        return _decode_char(char)

    def _read_posix_key(self) -> KeyMsg | None:
        fileno = self.stream.fileno()
        self._enter_raw_mode(fileno)
        raw = os.read(fileno, 1)
        if not raw:
            self._eof = True
            return None
        char = raw.decode("utf-8", "replace")
        if char == "\x1b":
            return self._read_posix_escape(fileno)
        return _decode_char(char)

    def _read_posix_escape(self, fileno: int) -> KeyMsg:
        """读 ESC 之后的字节，识别 CSI / Alt 组合。"""
        import select

        def ready() -> bool:
            return bool(select.select([fileno], [], [], 0.02)[0])

        if not ready():
            return KeyMsg(type=KEY_ESC)
        second = os.read(fileno, 1).decode("utf-8", "replace")
        if second != "[":
            return KeyMsg(type=second, alt=True) if second else KeyMsg(type=KEY_ESC)
        sequence = ""
        while ready() and len(sequence) < 8:
            sequence += os.read(fileno, 1).decode("utf-8", "replace")
        return _decode_csi(sequence)

    def _enter_raw_mode(self, fileno: int) -> None:
        if self._restore is not None:
            return
        try:
            import termios
            import tty

            original = termios.tcgetattr(fileno)
            tty.setcbreak(fileno)
        except (ImportError, OSError, ValueError):  # pragma: no cover - 非 tty
            return
        self._restore = lambda: termios.tcsetattr(fileno, termios.TCSADRAIN, original)  # noqa: E731

    def _restore_terminal(self) -> None:
        restore, self._restore = self._restore, None
        if restore is not None:
            with contextlib.suppress(OSError, ValueError):
                restore()


def _decode_char(char: str) -> KeyMsg:
    """把单个控制字符/普通字符转成 :class:`KeyMsg`。"""
    mapping = {
        "\r": KEY_ENTER,
        "\n": KEY_CTRL_J,
        "\x7f": KEY_BACKSPACE,
        "\x08": KEY_CTRL_H,
        "\t": KEY_TAB,
        "\x03": KEY_CTRL_C,
        "\x04": KEY_CTRL_D,
        "\x0b": KEY_CTRL_K,
        "\x15": KEY_CTRL_U,
        "\x16": KEY_CTRL_V,
        "\x1b": KEY_ESC,
        "\x1a": KEY_SHIFT_TAB,
    }
    if char in mapping:
        return KeyMsg(type=mapping[char])
    if char == "":
        return KeyMsg(type=KEY_ESC)
    return KeyMsg.runes_key(char)


def _windows_special(second: str) -> KeyMsg:
    """Windows ``\\x00`` / ``\\xe0`` 前缀后的功能键。"""
    mapping = {
        "H": KEY_UP,
        "P": KEY_DOWN,
        "K": KEY_LEFT,
        "M": KEY_RIGHT,
        "I": KEY_PGUP,
        "Q": KEY_PGDOWN,
        "G": KEY_HOME,
        "O": KEY_END,
        "S": KEY_CTRL_D,  # Delete：TUI 不用，归还给输入框的删除语义
    }
    return KeyMsg(type=mapping.get(second, ""))


def _decode_csi(sequence: str) -> KeyMsg:
    """解码 CSI 序列（``A`` / ``5~`` 这类）。"""
    mapping = {
        "A": KEY_UP,
        "B": KEY_DOWN,
        "C": KEY_RIGHT,
        "D": KEY_LEFT,
        "H": KEY_HOME,
        "F": KEY_END,
        "1~": KEY_HOME,
        "2~": KEY_UNKNOWN_INSERT,
        "3~": KEY_CTRL_D,
        "4~": KEY_END,
        "5~": KEY_PGUP,
        "6~": KEY_PGDOWN,
        "Z": KEY_SHIFT_TAB,
    }
    return KeyMsg(type=mapping.get(sequence, ""))


#: 无法映射的 CSI 序列的占位类型（交给输入框时会忽略）。
KEY_UNKNOWN_INSERT = "insert"


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def run_tui(
    ctx: Any,
    handler: Any,
    completion: Any,
    output: asyncio.Queue[Any] | None,
    set_program: Callable[[Any], None] | None,
    user_name: str,
    assistant_name: str,
    *,
    key_source: Any = None,
    output_stream: Any = None,
    clipboard: Any = None,
    local_files: Any = None,
    paste_burst_enabled: bool | None = None,
    input_now: Callable[[], Any] | None = None,
) -> None:
    """跑 TUI 主循环直到退出或 ctx 取消。对应 Go ``runTUI``。

    正常退出（用户按 ``Ctrl+C`` 且输入为空、提交 ``/exit``、键盘事件源 EOF）返回
    ``None``；ctx 取消时抛 ``ctx_error(ctx)``。
    """
    from atri_x.platform.cli.clipboard import new_clipboard_writer  # 延迟导入避免环

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
    program = TuiProgram(queue)
    stream = output_stream if output_stream is not None else sys.stdout
    source = key_source if key_source is not None else StdinKeySource()
    interactive = output is not None and _stream_is_tty(stream)

    model = new_tui_model(
        ctx=ctx,
        handler=handler,
        output=output,
        completion=completion,
        clipboard=clipboard if clipboard is not None else new_clipboard_writer(),
        local_files=local_files,
        user_name=user_name,
        assistant_name=assistant_name,
        input_now=input_now,
        paste_burst_enabled=paste_burst_enabled,
    )

    if set_program is not None:
        set_program(program)
    tasks: list[asyncio.Task[Any]] = []
    try:
        if output is not None:
            tasks.append(asyncio.ensure_future(_pump(output, queue)))
        tasks.append(asyncio.ensure_future(_pump_keys(source, queue)))
        tasks.append(asyncio.ensure_future(_pump_tick(queue)))
        actions = model.init()
        _ = actions  # Init 的两个动作由常驻 pump 承担（见模块 docstring）
        _draw(model, stream, interactive, force=False)
        while True:
            _raise_if_cancelled(ctx)
            msg = await _next_message(ctx, queue)
            if msg is None:
                # ctx 取消（``_next_message`` 返回 None）时抛取消原因；
                # 只有键盘事件源 EOF 才会走到下面的 EOF_MARKER 分支。
                _raise_if_cancelled(ctx)
                return
            if msg == EOF_MARKER:
                return
            if isinstance(msg, TuiStatusTickMsg):
                model.update(msg)
                _draw(model, stream, interactive)
                continue
            actions = model.update(msg)
            _draw(model, stream, interactive)
            if await _run_actions(ctx, model, actions, queue, handler, stream, interactive):
                return
    finally:
        if set_program is not None:
            set_program(None)
        program.close()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _draw(model, stream, interactive, force=True)


def _stream_is_tty(stream: Any) -> bool:
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        return False
    try:
        return bool(os.isatty(fileno()))
    except (OSError, ValueError, AttributeError):
        return False


def _draw(model: TuiModel, stream: Any, interactive: bool, *, force: bool = False) -> None:
    """渲染一帧。非交互输出只在 ``force`` 时落盘（见模块 docstring）。"""
    if not interactive and not force:
        return
    frame = model.view()
    if interactive:
        _write(stream, CLEAR_SCREEN + frame + "\n")
    else:
        _write(stream, frame + "\n")


async def _run_actions(
    ctx: Any,
    model: TuiModel,
    actions: list[Any],
    queue: asyncio.Queue[Any],
    handler: Any,
    stream: Any,
    interactive: bool,
) -> bool:
    """执行动作列表；返回 ``True`` 表示应当退出主循环。"""
    for action in actions:
        kind = getattr(action, "kind", "")
        if kind == ACTION_QUIT:
            return True
        if kind == ACTION_READ_CLIPBOARD:
            await _read_clipboard_into(model, queue)
        elif kind == ACTION_DISPATCH:
            _spawn_dispatch(ctx, model, queue, handler, action.payload)
        elif kind == ACTION_COMPLETE:
            request = action.payload
            if not isinstance(request, CompleteRequest):  # pragma: no cover - 防御性
                continue
            items = await model.resolve_completion(request)
            model.apply_completion_result(request.delta, items)
            _draw(model, stream, interactive)
    return False


def _spawn_dispatch(
    ctx: Any, model: TuiModel, queue: asyncio.Queue[Any], handler: Any, text: str
) -> None:
    """把输入交给 handler（对应 Go 的 ``go func(){ HandleMessage }()``）。

    Go 里这个 goroutine 出错时把 ``tuiNoticeMsg`` 投回 ``m.output``（适配器的输出
    队列，不是 program 的消息通道），这里保持一致；没有外部队列时退回内部队列。
    """
    if handler is None:
        return
    target = model.output if model.output is not None else queue

    async def run() -> None:
        try:
            await handler.handle_message(ctx, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 与 Go 一致：错误变成一条通知
            try:
                target.put_nowait(TuiNoticeMsg("error: " + str(exc), logging.ERROR))
            except asyncio.QueueFull:  # pragma: no cover - 防御性
                pass

    asyncio.ensure_future(run())


async def _read_clipboard_into(model: TuiModel, queue: asyncio.Queue[Any]) -> None:
    """读剪贴板并把结果投回消息队列（对应 ``readTUIClipboard`` 这个 Cmd）。"""
    _ = model
    command = read_tui_clipboard()
    msg = await asyncio.to_thread(command)
    if isinstance(msg, TuiClipboardMsg):
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:  # pragma: no cover - 防御性
            pass


async def _pump(source: asyncio.Queue[Any], target: asyncio.Queue[Any]) -> None:
    """把外部输出队列的消息转投到内部队列。"""
    while True:
        msg = await source.get()
        await target.put(msg)


async def _pump_keys(source: Any, target: asyncio.Queue[Any]) -> None:
    """把键盘事件源的消息转投到内部队列；EOF 时投 :data:`EOF_MARKER`。"""
    while True:
        msg = await source.read()
        if msg is None:
            await target.put(EOF_MARKER)
            return
        await target.put(msg)


async def _pump_tick(target: asyncio.Queue[Any]) -> None:
    """每秒投一个状态栏时钟消息。对应 ``tickTUIStatus``。"""
    while True:
        await asyncio.sleep(STATUS_TICK_INTERVAL)
        await target.put(TuiStatusTickMsg())


async def _next_message(ctx: Any, queue: asyncio.Queue[Any]) -> Any:
    """等一条消息；ctx 取消时返回 ``None``（由调用方决定语义）。"""
    get_task = asyncio.ensure_future(queue.get())
    watch_task = asyncio.ensure_future(_watch_cancel(ctx))
    done, pending = await asyncio.wait(
        {get_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    if watch_task in done:
        return None
    if get_task.cancelled():  # pragma: no cover - 防御性
        return None
    return get_task.result()


async def _watch_cancel(ctx: Any) -> None:
    """等到 ctx 取消。对应 Go 的 ``<-ctx.Done()``。"""
    event = asyncio.Event()
    remover = None
    adder = getattr(ctx, "add_callback", None)
    if callable(adder):
        try:
            remover = adder(event.set)
        except Exception:  # noqa: BLE001 - 不支持回调的 ctx 走轮询
            remover = None
    try:
        while ctx_error(ctx) is None:
            try:
                await asyncio.wait_for(event.wait(), CTX_POLL_INTERVAL)
            except (asyncio.TimeoutError, TimeoutError):
                continue
    finally:
        if remover is not None:
            with contextlib.suppress(Exception):
                remover()


def _raise_if_cancelled(ctx: Any) -> None:
    err = ctx_error(ctx)
    if err is not None:
        raise err


async def iter_key_messages(source: Any) -> AsyncIterator[Any]:
    """把一个按键事件源适配成异步迭代器（对外的小工具，便于自定义循环）。"""
    while True:
        msg = await source.read()
        if msg is None:
            return
        yield msg


# 外部可以经 ``program.send(...)`` 投递的消息类型（本 TUI 实际消费的全部消息类）。
MESSAGE_TYPES = (KeyMsg, MouseMsg, TuiNoticeMsg, TuiClipboardMsg, TuiStatusTickMsg)
