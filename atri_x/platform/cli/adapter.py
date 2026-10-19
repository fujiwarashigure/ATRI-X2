"""CLI 平台适配器主体：stdin 读取循环、TUI 交互与出站发送。

对应 Go：``internal/platform/cli/cli.go``。

硬边界（上游）：适配器**只**做平台输入输出 —— 把 stdin 的一行变成
``handler.handle_message(ctx, text)``，把
:class:`atri_x.delivery.delivery.Output` 变成 TUI 消息或一行纯文本；它不驱动 LLM。

与 Go 的差异（仅形式）：

* ``chan tea.Msg`` → :class:`asyncio.Queue`（容量 256，与 Go ``make(chan tea.Msg, 256)``
  一致）；``select { case output <- msg: default: }`` → ``put_nowait`` + ``QueueFull`` 兜底。
* ``sync.Mutex`` → :class:`asyncio.Lock`（只保护跨 await 的读写；纯同步的
  :meth:`Adapter.send_tui_message` 跑在事件循环线程上，不需要加锁）。
* ``bufio.Scanner`` 的阻塞读 → :func:`atri_x.ops.executors.run_io`（``atrix-io`` 池）
  里的 ``readline``，并支持注入
  输入/输出流（Go 直接绑 ``os.Stdin`` / ``os.Stdout``）。
* ``isatty.IsTerminal(os.Stdin.Fd())`` → :meth:`Adapter.is_interactive`（拿不到
  ``fileno`` 时按"非终端"处理，与 Go 在非终端上的行为一致）。
* ``(Receipt, error)`` → 返回 :class:`atri_x.delivery.delivery.Receipt` / 抛异常。
* Go 的 ``SendToolNotice`` 是同步方法（内部用 ``context.Background()``）；Python 侧
  因为没有同步的 ``send_notice``，改成协程 :meth:`Adapter.send_tool_notice`。
* TUI 相关类型与 :func:`run_tui` 由 :mod:`atri_x.platform.cli.tui` 提供；本模块
  在**函数内**延迟导入它，避免 ``adapter`` ↔ ``tui`` 的模块级循环导入。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from atri_x.delivery.delivery import (
    Kind,
    MessageStream,
    Notice,
    Output,
    Receipt,
    ctx_error,
    fallback_output,
    fallback_text,
    text as text_output,
)
from atri_x.errors import InvalidError
from atri_x.ops.executors import run_io
from atri_x.runtime.status import Snapshot, format_compact

#: 平台名。对应 Go ``(*Adapter).Name`` 的返回值。
PLATFORM_NAME = "cli"

#: TUI 消息队列容量。对应 Go ``make(chan tea.Msg, 256)``。
OUTPUT_QUEUE_SIZE = 256

#: 未显式指定时使用的用户名 / 助手名。对应 Go ``New`` 里的字面量。
DEFAULT_USER_NAME = "user"
DEFAULT_ASSISTANT_NAME = "assistant"

__all__ = [
    "DEFAULT_ASSISTANT_NAME",
    "DEFAULT_USER_NAME",
    "OUTPUT_QUEUE_SIZE",
    "PLATFORM_NAME",
    "Adapter",
    "CliMessageStream",
    "new_adapter",
    "outputs_text",
    "print_line",
]


def new_adapter(**kwargs: Any) -> "Adapter":
    """对应 Go ``New``：创建一个 CLI 适配器。"""
    return Adapter(**kwargs)


class Adapter:
    """本地交互式 CLI 适配器。对应 Go ``Adapter``。

    ``input_stream`` / ``output_stream`` / ``error_stream`` 是 Python 侧新增的测试接缝
    （Go 直接绑标准流）；缺省即标准流。``interactive`` 显式覆盖终端判定，
    ``None`` 表示按 :meth:`is_interactive` 探测。
    """

    def __init__(
        self,
        *,
        input_stream: Any = None,
        output_stream: Any = None,
        error_stream: Any = None,
        interactive: bool | None = None,
        user_name: str = DEFAULT_USER_NAME,
        assistant_name: str = DEFAULT_ASSISTANT_NAME,
    ) -> None:
        self._lock = asyncio.Lock()
        self.output: asyncio.Queue[Any] = asyncio.Queue(maxsize=OUTPUT_QUEUE_SIZE)
        self.program: Any = None
        self.user_name = user_name or DEFAULT_USER_NAME
        self.assistant_name = assistant_name or DEFAULT_ASSISTANT_NAME
        self.connect_notify: Callable[[Any, str], Any] | None = None
        self.completion: Any = None
        self.stdin = input_stream if input_stream is not None else sys.stdin
        self.stdout = output_stream if output_stream is not None else sys.stdout
        self.stderr = error_stream if error_stream is not None else sys.stderr
        self._interactive = interactive

    # -- 生命周期 -----------------------------------------------------------
    def name(self) -> str:
        """对应 Go ``(*Adapter).Name``。"""
        return PLATFORM_NAME

    def set_completer(self, service: Any) -> None:
        """对应 Go ``(*Adapter).SetCompleter``。"""
        self.completion = service

    def set_connect_notifier(self, notify: Callable[[Any, str], Any] | None) -> None:
        """对应 Go ``(*Adapter).SetConnectNotifier``。"""
        self.connect_notify = notify

    def stop_app_on_exit(self) -> bool:
        """对应 Go ``(*Adapter).StopAppOnExit``：交互式 CLI 独占前台进程。"""
        return True

    def is_interactive(self) -> bool:
        """stdin 是否是终端。对应 Go ``isatty.IsTerminal(os.Stdin.Fd())``。"""
        if self._interactive is not None:
            return bool(self._interactive)
        fileno = getattr(self.stdin, "fileno", None)
        if not callable(fileno):
            return False
        try:
            return bool(os.isatty(fileno()))
        except (OSError, ValueError, AttributeError):
            return False

    async def run(self, ctx: Any, handler: Any) -> None:
        """启动 stdin 读取循环。对应 Go ``(*Adapter).Run``。

        ``/exit`` 在本地处理，其余输入转交 ``handler.handle_message``。
        非交互终端走行扫描循环，交互终端走 TUI（:func:`run_tui`）。
        """
        await self.notify_connected(ctx)
        if not self.is_interactive():
            await self.run_scanner(ctx, handler)
            return
        await self.run_interactive(ctx, handler)

    async def notify_connected(self, ctx: Any) -> None:
        """对应 Go ``(*Adapter).notifyConnected``。"""
        notify = self.connect_notify
        if notify is None:
            return
        result = notify(ctx, self.name())
        if asyncio.iscoroutine(result):
            await result

    # -- 非交互行模式 --------------------------------------------------------
    async def run_scanner(self, ctx: Any, handler: Any) -> None:
        """逐行读取 stdin 直到 EOF 或 ``/exit``。对应 Go ``runScanner``。"""
        self.write(self.stdout, "> ")
        while True:
            if ctx_error(ctx) is not None:
                raise_ctx_error(ctx)
            line = await run_io(self._read_line)
            if line is None:
                return
            if line == "/exit":
                return
            await self._dispatch(ctx, handler, line)
            self.write(self.stdout, "> ")

    async def _dispatch(self, ctx: Any, handler: Any, line: str) -> None:
        """把一行交给 handler；失败时只打印到 stderr（与 Go 一致）。"""
        try:
            await handler.handle_message(ctx, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Go 只 fprintf 到 stderr
            self.write(self.stderr, f"error: {exc}\n")

    def _read_line(self) -> str | None:
        """读一行并剥掉行尾（``bufio.Scanner`` 的 ScanLines 语义）。EOF 返回 ``None``。"""
        reader = getattr(self.stdin, "readline", None)
        if not callable(reader):
            return None
        try:
            raw = reader()
        except (OSError, ValueError):  # pragma: no cover - 流已关闭
            return None
        if not raw:
            return None
        if raw.endswith("\n"):
            raw = raw[:-1]
        if raw.endswith("\r"):
            raw = raw[:-1]
        return raw

    # -- 交互模式 -----------------------------------------------------------
    async def run_interactive(self, ctx: Any, handler: Any) -> None:
        """进入 TUI。对应 Go ``runInteractive``。"""
        from atri_x.platform.cli.tui import run_tui  # 延迟导入：见模块 docstring

        await run_tui(
            ctx,
            handler,
            self.completion,
            self.output,
            self.set_program,
            self.user_name,
            self.assistant_name,
        )

    def set_program(self, program: Any) -> None:
        """对应 Go ``(*Adapter).setProgram``（``None`` 表示 TUI 已退出）。"""
        self.program = program

    # -- 流式输出 -----------------------------------------------------------
    def start_stream(self, ctx: Any) -> MessageStream:
        """开启流式助手消息。对应 Go ``(*Adapter).StartStream``。

        同步工厂（Go 也是同步的 ``(MessageStream, error)``，这里按契约抛异常）：
        调用方 :func:`atri_x.agent.turn_output.start_message_stream` 是同步的，
        失败时应退回整条发送。
        """
        if not self.is_interactive():
            raise InvalidError("cli streaming output requires interactive TUI")
        if self.program is None:
            raise InvalidError("cli TUI is not running")
        return CliMessageStream(self)

    def set_runtime_status(self, ctx: Any, snapshot: Snapshot) -> None:
        """对应 Go ``(*Adapter).SetRuntimeStatus``。

        同步方法：唯一的调用方 :func:`atri_x.agent.status.publish_runtime_status`
        按 Go 的形状（``_ = sender.SetRuntimeStatus(...)``）同步调用，本方法内部
        只做一次 TUI 消息投递，没有任何需要 await 的动作。
        """
        from atri_x.platform.cli import tui  # 延迟导入

        self.send_tui_message(
            tui.TuiStatusMsg(snapshot),
            format_compact(snapshot, datetime.now(timezone.utc), 0),
        )

    async def send_reasoning(self, ctx: Any, value: str) -> None:
        """对应 Go ``(*Adapter).SendReasoning``。"""
        if not value:
            return
        from atri_x.platform.cli import tui  # 延迟导入

        self.send_tui_message(tui.TuiReasoningMsg(value), value)

    # -- 出站 ---------------------------------------------------------------
    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``(*Adapter).SendChat``。"""
        from atri_x.platform.cli import tui  # 延迟导入

        value = outputs_text(outputs)
        if value:
            self.send_tui_message(tui.TuiOutputMsg(value), value)
        return Receipt()

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """对应 Go ``(*Adapter).SendNotice``：空 Target 表示发回当前对话。"""
        from atri_x.platform.cli import tui  # 延迟导入

        platform_name = (notice.target.platform or "").strip()
        if platform_name and platform_name != self.name():
            raise InvalidError(f'cli cannot send to platform "{platform_name}"')
        value = outputs_text(notice.outputs)
        if value:
            self.send_tui_message(
                tui.TuiNoticeMsg(value, notice.level),
                "[notice] " + value,
            )
        return Receipt()

    async def send_tool_notice(self, value: str) -> None:
        """发一条工具进度通知。对应 Go ``(*Adapter).SendToolNotice``。"""
        value = (value or "").strip()
        if not value:
            return
        await self.send_notice(
            {},
            Notice(outputs=[text_output("[tool] " + value)], level=logging.DEBUG),
        )

    def send_tui_message(self, msg: Any, fallback: str) -> None:
        """把一条 TUI 消息投给 program / 队列，两者都没有时退化成一行文本。

        对应 Go ``(*Adapter).sendTUIMessage``。本方法是**同步**的（Go 侧持锁），
        在 Python 里它只做非阻塞入队，因此不需要 ``await``。
        """
        if self.output is None or not self.is_interactive():
            print_line(self.stdout, fallback)
            return
        program = self.program
        if program is not None:
            send = getattr(program, "send", None)
            if callable(send):
                send(msg)
                return
        try:
            self.output.put_nowait(msg)
        except asyncio.QueueFull:
            print_line(self.stdout, fallback)

    # -- 输出 ---------------------------------------------------------------
    def write(self, stream: Any, value: str) -> None:
        """写一段文本，遇到控制台无法编码的字符时退到 ASCII 转义（PORTING §7.4）。"""
        if not value:
            return
        try:
            stream.write(value)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            escaped = value.encode(encoding, "backslashreplace").decode(encoding, "replace")
            stream.write(escaped)
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()


class CliMessageStream:
    """本地 TUI 上的一条流式助手消息。对应 Go ``cliMessageStream``。"""

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter

    async def append(self, ctx: Any, value: str) -> None:
        """对应 Go ``cliMessageStream.Append``。"""
        if not value:
            return
        from atri_x.platform.cli import tui  # 延迟导入

        self.adapter.send_tui_message(tui.TuiOutputMsg(value), value)

    async def replace(self, ctx: Any, value: str) -> Receipt:
        """对应 Go ``cliMessageStream.Replace``。"""
        from atri_x.platform.cli import tui  # 延迟导入

        self.adapter.send_tui_message(tui.TuiReplaceAssistantMsg(value), value)
        return Receipt()

    async def finish(self, ctx: Any) -> Receipt:
        """对应 Go ``cliMessageStream.Finish``。"""
        from atri_x.platform.cli import tui  # 延迟导入

        self.adapter.send_tui_message(tui.TuiFinishAssistantMsg(), "\n")
        return Receipt()


def outputs_text(outputs: Sequence[Output] | None) -> str:
    """把一批输出压成一条纯文本。对应 Go ``outputsText``。"""
    return fallback_output(outputs).text


def output_message_text(out: Output) -> str:
    """单条输出的纯文本表示（对应 Go ``outputMessage`` 的取文本分支）。"""
    return out.text if out.kind == Kind.TEXT else fallback_text(out)


def print_line(stream: Any, value: str) -> None:
    """打印一行（末尾没有换行时补一个）。对应 Go ``printLine``。"""
    if not value:
        return
    if not value.endswith("\n"):
        value += "\n"
    try:
        stream.write(value)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(value.encode(encoding, "backslashreplace").decode(encoding, "replace"))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def raise_ctx_error(ctx: Any) -> None:
    """抛出 ctx 的取消原因。对应 Go 的 ``return ctx.Err()``。"""
    err = ctx_error(ctx)
    if err is not None:
        raise err
    raise asyncio.CancelledError("cli adapter context cancelled")
