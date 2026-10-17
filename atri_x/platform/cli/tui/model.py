"""TUI 主模型：状态、Update 主流程、输入提交、历史与补全。

对应 Go：``internal/platform/cli/tui.go``（``tuiModel`` 的字段、
``Init`` / ``Update`` / ``Update`` 里各消息分支、``completeInput`` / ``complete`` /
``applyCompletionItem`` / ``prevHistory`` / ``nextHistory`` / ``submitInput`` /
``clearInput``，以及 ``newTUIInput`` / ``textareaCursorRunePosition`` /
``setTextareaValueAndCursor`` 这几个自由函数）。

本模块只保留"Update 主流程与状态"，两个体量较大的相邻职责按 mixin 拆出：

* :mod:`atri_x.platform.cli.tui.content` —— 内容与通知的追加/重绘；
* :mod:`atri_x.platform.cli.tui.view` —— 整帧渲染与布局（含 Go ``(*tuiModel).View``
  对应的 :meth:`TuiModel.view`，以及 ``layoutWidths`` / ``resizeViewports`` / 各
  ``*View``）；
* :mod:`atri_x.platform.cli.tui.copy` / :mod:`atri_x.platform.cli.tui.mouse` ——
  copy mode 与鼠标。

Elm 架构的落法：``update(msg)`` 就地改自身状态并返回**动作列表**
（:class:`Action`），由 :mod:`atri_x.platform.cli.tui.program` 的循环解释执行。
Go 用 ``tea.Cmd`` 表达同一批后继动作（等待输出队列、状态栏时钟、读剪贴板、
把输入交给 handler）；Python 里没有可返回的闭包协议，因此显式化成常量 + 载荷。

两个**必须**异步的动作（Go 里分别靠 goroutine 与同步接口绕开）：

* ``ACTION_DISPATCH`` —— ``go func(){ handler.HandleMessage(...) }()``；
* ``ACTION_COMPLETE`` —— Go 的 ``completion.Service.Complete`` 是同步接口，Python 侧
  :meth:`atri_x.completion.completion.Service.complete` 是协程，因此补全解析改为
  "update 发出动作 → 循环 await → 回灌 :meth:`TuiModel.apply_completion_result`"。
  补全**候选内容与顺序**与 Go 完全一致，只有"谁在等"变了。

与 Go 的差异（仅形式）：

* ``tea.Msg`` → dataclass + ``isinstance`` 分派。
* ``lipgloss`` / ``viewport`` / ``textarea`` → 本包内的等价件（见各模块说明）。
* ``runtime.GOOS == "windows"`` → :data:`IS_WINDOWS`（可用 ``paste_burst_enabled``
  覆盖，便于在任何平台上测试粘贴突发逻辑）。
"""

from __future__ import annotations

import inspect
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from atri_x.completion.completion import Item, Request as CompletionRequest
from atri_x.errors import AtriXError
from atri_x.platform.cli.local_file import LocalFileResolver, new_local_file_resolver
from atri_x.platform.cli.tui.content import (  # noqa: F401 - 再导出，见模块 docstring
    EMPTY_CONTENT_HINT,
    EMPTY_NOTICES_HINT,
    MAX_NOTICES,
    ContentMixin,
    is_separator_line,
    level_style,
    render_speaker_line,
)
from atri_x.platform.cli.tui.copy import (
    REGION_CHAT,
    REGION_NOTICE,
    CopyModeMixin,
    CopyModeState,
    clamp_int,
)
from atri_x.platform.cli.tui.input import DEFAULT_CHAR_LIMIT, TextArea
from atri_x.platform.cli.tui.keys import (
    KEY_BACKTAB,
    KEY_CTRL_C,
    KEY_CTRL_D,
    KEY_CTRL_I,
    KEY_CTRL_J,
    KEY_CTRL_K,
    KEY_CTRL_U,
    KEY_CTRL_V,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_PGDOWN,
    KEY_PGUP,
    KEY_SHIFT_TAB,
    KEY_TAB,
    KEY_UP,
    KeyMsg,
    MouseMsg,
)
from atri_x.platform.cli.tui.messages import (
    CompletionState,
    TuiClipboardMsg,
    TuiFinishAssistantMsg,
    TuiNotice,
    TuiNoticeMsg,
    TuiOutputMsg,
    TuiPasteBurst,
    TuiReasoningMsg,
    TuiReplaceAssistantMsg,
    TuiStatusMsg,
    TuiStatusTickMsg,
    normalize_tui_input_paste,
    utc_now,
)
from atri_x.platform.cli.tui.mouse import MouseMixin
from atri_x.platform.cli.tui.view import (  # noqa: F401 - 再导出，见模块 docstring
    MAX_COMPLETION_POPUP_ITEMS,
    MAX_TUI_INPUT_HEIGHT,
    MIN_NOTICE_LAYOUT_WIDTH,
    MIN_TUI_INPUT_HEIGHT,
    NOTICE_PANEL_WIDTH,
    TUI_INPUT_GUTTER_WIDTH,
    ViewMixin,
    completion_display_text,
)
from atri_x.platform.cli.tui.viewport import Viewport
from atri_x.runtime.status import Snapshot

#: 输入框字符上限。对应 Go ``tuiInputCharLimit``。
TUI_INPUT_CHAR_LIMIT = DEFAULT_CHAR_LIMIT

#: 是否本机是 Windows。对应 Go 里的 ``runtime.GOOS == "windows"``。
IS_WINDOWS = sys.platform.startswith("win")

#: 动作类型常量。对应 Go 返回的 ``tea.Cmd`` 的几种含义。
ACTION_QUIT = "quit"
ACTION_WAIT_OUTPUT = "wait_output"
ACTION_TICK_STATUS = "tick_status"
ACTION_READ_CLIPBOARD = "read_clipboard"
ACTION_DISPATCH = "dispatch"
ACTION_COMPLETE = "complete"

__all__ = [
    "ACTION_COMPLETE",
    "ACTION_DISPATCH",
    "ACTION_QUIT",
    "ACTION_READ_CLIPBOARD",
    "ACTION_TICK_STATUS",
    "ACTION_WAIT_OUTPUT",
    "EMPTY_CONTENT_HINT",
    "EMPTY_NOTICES_HINT",
    "IS_WINDOWS",
    "MAX_COMPLETION_POPUP_ITEMS",
    "MAX_NOTICES",
    "MAX_TUI_INPUT_HEIGHT",
    "MIN_NOTICE_LAYOUT_WIDTH",
    "MIN_TUI_INPUT_HEIGHT",
    "NOTICE_PANEL_WIDTH",
    "TUI_INPUT_CHAR_LIMIT",
    "TUI_INPUT_GUTTER_WIDTH",
    "Action",
    "CompleteRequest",
    "TuiModel",
    "WindowSizeMsg",
    "completion_display_text",
    "is_separator_line",
    "level_style",
    "new_tui_input",
    "new_tui_model",
    "render_speaker_line",
    "set_textarea_value_and_cursor",
    "textarea_cursor_rune_position",
]


@dataclass
class Action:
    """模型要求循环执行的一个后继动作。对应 Go 返回的 ``tea.Cmd``。"""

    kind: str = ""
    payload: Any = None


@dataclass
class WindowSizeMsg:
    """终端尺寸变化。对应 ``tea.WindowSizeMsg``。"""

    width: int = 0
    height: int = 0


@dataclass
class CompleteRequest:
    """一次待解析的补全请求（``ACTION_COMPLETE`` 的载荷）。"""

    delta: int = 1
    text: str = ""
    cursor: int = 0


def new_tui_input(char_limit: int = TUI_INPUT_CHAR_LIMIT) -> TextArea:
    """构造输入框（对应 Go ``newTUIInput``）。"""
    area = TextArea(char_limit=char_limit)
    area.focus()
    area.set_height(1)
    return area


def textarea_cursor_rune_position(area: TextArea) -> int:
    """光标在整段文本里的码点偏移。对应 Go ``textareaCursorRunePosition``。

    Go 用 ``LineInfo().StartColumn + ColumnOffset``（bubbles 的 ``StartColumn`` 就是
    光标在行内的列号）；本实现以 ``line_info().column`` 承担同一角色。
    """
    rows = area.value().split("\n")
    row = min(max(0, area.line()), len(rows) - 1)
    position = sum(len(rows[index]) + 1 for index in range(row))
    info = area.line_info()
    return position + info.column + info.column_offset


def set_textarea_value_and_cursor(area: TextArea, value: str, rune_position: int) -> None:
    """整体替换并定位光标。对应 Go ``setTextareaValueAndCursor``。"""
    rune_position = clamp_int(rune_position, 0, len(value))
    row = 0
    column = 0
    for char in value[:rune_position]:
        if char == "\n":
            row += 1
            column = 0
        else:
            column += 1
    area.set_value(value)
    while area.line() > row:
        area.cursor_up()
    area.set_cursor(column)


def new_tui_model(**kwargs: Any) -> "TuiModel":
    """对应 Go ``runTUI`` 里的 ``tuiModel{...}`` 字面量构造。"""
    return TuiModel(**kwargs)


class TuiModel(CopyModeMixin, MouseMixin, ContentMixin, ViewMixin):
    """TUI 的完整状态。对应 Go ``tuiModel``。"""

    def __init__(
        self,
        *,
        ctx: Any = None,
        handler: Any = None,
        output: Any = None,
        completion: Any = None,
        clipboard: Any = None,
        local_files: LocalFileResolver | None = None,
        user_name: str = "user",
        assistant_name: str = "assistant",
        input_now: Callable[[], datetime] | None = None,
        paste_burst_enabled: bool | None = None,
        char_limit: int = TUI_INPUT_CHAR_LIMIT,
    ) -> None:
        self.ctx = ctx
        self.handler = handler
        self.output = output
        self.completion = completion
        self.clipboard = clipboard
        self.local_files = local_files if local_files is not None else new_local_file_resolver("")
        self.user_name = user_name or "user"
        self.assistant_name = assistant_name or "assistant"
        self.input_now = input_now
        self.paste_burst_enabled = IS_WINDOWS if paste_burst_enabled is None else paste_burst_enabled

        self.copy_state = CopyModeState()
        self.completion_state = CompletionState()
        self.content = ""
        self.notices: list[TuiNotice] = []
        self.inline_notice_levels: list[int] = []
        self.history: list[str] = []
        self.hist_pos = 0
        self.assistant_open = False
        self.assistant_start = 0
        self.reasoning_open = False
        self.runtime_status = Snapshot()
        self.status_now: datetime | None = None
        self.viewport = Viewport()
        self.notice_viewport = Viewport()
        self.input = new_tui_input(char_limit)
        self.paste_burst = TuiPasteBurst()
        self.width = 0
        self.height = 0
        self.notice_width = 0
        self.resizing_notice = False

    # -- 时间 ---------------------------------------------------------------
    def now(self) -> datetime:
        """当前时间（可注入）。对应 Go ``(*tuiModel).now``。"""
        if self.input_now is not None:
            return self.input_now()
        return utc_now()

    # -- Elm 架构 -----------------------------------------------------------
    def init(self) -> list[Action]:
        """对应 Go ``(*tuiModel).Init``。"""
        return [Action(ACTION_WAIT_OUTPUT), Action(ACTION_TICK_STATUS)]

    def update(self, msg: Any) -> list[Action]:
        """处理一条消息。对应 Go ``(*tuiModel).Update``。"""
        if isinstance(msg, KeyMsg) and msg.paste:
            msg = KeyMsg(
                type=msg.type,
                runes=normalize_tui_input_paste(msg.runes),
                alt=msg.alt,
                paste=True,
            )
        if isinstance(msg, KeyMsg) and self.paste_burst_enabled:
            now = self.now()
            if not msg.paste and not msg.alt and msg.runes:
                self.paste_burst.observe_text(now, len(msg.runes))
            elif msg.type != KEY_ENTER:
                self.paste_burst.reset()

        if isinstance(msg, WindowSizeMsg):
            self.width = msg.width
            self.height = msg.height
            self.resize_viewports()
            self.refresh_content()
            self.refresh_notices()
            return []
        if isinstance(msg, TuiOutputMsg):
            self.append_assistant_content(msg.text)
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiReplaceAssistantMsg):
            self.replace_assistant_content(msg.text)
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiFinishAssistantMsg):
            self.finish_assistant_content()
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiReasoningMsg):
            self.append_reasoning_content(msg.text)
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiStatusMsg):
            self.runtime_status = msg.snapshot
            if self.status_now is None:
                self.status_now = self.now()
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiStatusTickMsg):
            self.status_now = msg.now or self.now()
            return [Action(ACTION_TICK_STATUS)]
        if isinstance(msg, TuiNoticeMsg):
            self.append_notice_level(msg.text, msg.level)
            return [Action(ACTION_WAIT_OUTPUT)]
        if isinstance(msg, TuiClipboardMsg):
            return self._handle_clipboard(msg)
        if isinstance(msg, MouseMsg):
            self.handle_mouse(msg)
            return []
        if isinstance(msg, KeyMsg):
            actions = self._handle_key(msg)
            if actions is not None:
                return actions
        if self.copy_state.active():
            return []
        return self._update_input(msg)

    def _handle_clipboard(self, msg: TuiClipboardMsg) -> list[Action]:
        """剪贴板读结果 → 插入输入框。对应 Go ``Update`` 的 ``tuiClipboardMsg`` 分支。"""
        if msg.err is not None:
            self.append_notice_level("paste: " + str(msg.err), logging.ERROR)
            return []
        old_height = self.input_content_height()
        self.input.insert_string(normalize_tui_input_paste(msg.text))
        self.clear_completion()
        if self.input_content_height() != old_height:
            self.resize_viewports()
            self.refresh_content()
            self.refresh_notices()
        return []

    def _handle_key(self, msg: KeyMsg) -> list[Action] | None:
        """按键分派；返回 ``None`` 表示"交回输入框处理"（Go 的 switch 穿透）。"""
        if self.copy_state.active():
            self.update_copy_mode(msg)
            return []
        name = msg.string()
        if name == "alt+h":
            self.enter_copy_mode(REGION_CHAT)
            return []
        if name == "alt+l":
            if self.layout_notice_visible():
                self.enter_copy_mode(REGION_NOTICE)
            else:
                self.copy_state.status = "notices hidden"
            return []
        if name == KEY_ESC:
            if self.completion_state.visible():
                self.clear_completion()
                return []
            if self.input.value() != "":
                self.clear_input()
            return []
        if name == KEY_CTRL_C:
            if self.completion_state.visible():
                self.clear_completion()
                return []
            if self.input.value() != "":
                self.clear_input()
                return []
            return [Action(ACTION_QUIT)]
        if name == KEY_CTRL_V:
            return [Action(ACTION_READ_CLIPBOARD)]
        if name == KEY_PGUP:
            self.viewport.scroll_up(max(1, self.viewport.height - 1))
            return []
        if name == KEY_PGDOWN:
            self.viewport.scroll_down(max(1, self.viewport.height - 1))
            return []
        if name == KEY_CTRL_K:
            self.viewport.scroll_up(max(1, self.viewport.height // 4))
            return []
        if name == KEY_CTRL_J:
            self.viewport.scroll_down(max(1, self.viewport.height // 4))
            return []
        if name == KEY_CTRL_U:
            self.notice_viewport.scroll_up(max(1, self.notice_viewport.height // 2))
            return []
        if name == KEY_CTRL_D:
            self.notice_viewport.scroll_down(max(1, self.notice_viewport.height // 2))
            return []
        if name in (KEY_TAB, KEY_CTRL_I):
            return self.complete_input(1)
        if name in (KEY_SHIFT_TAB, KEY_BACKTAB):
            return self.complete_input(-1)
        if name == KEY_UP:
            if self.completion_state.visible():
                self.select_completion(-1)
                return []
            if self.input.line() == 0:
                self.clear_completion()
                return self.prev_history()
            return None
        if name == KEY_DOWN:
            if self.completion_state.visible():
                self.select_completion(1)
                return []
            if self.input.line() == self.input.line_count() - 1:
                self.clear_completion()
                return self.next_history()
            return None
        if name == KEY_ENTER:
            if self.paste_burst_enabled and self.paste_burst.should_insert_enter(self.now()):
                return None
            if self.input.line() == self.input.line_count() - 1:
                return self.submit_input()
            return None
        return None

    def _update_input(self, msg: Any) -> list[Action]:
        """把消息交给输入框。对应 Go ``Update`` 末尾的 textarea 分支。"""
        old_input = self.input.value()
        old_height = self.input_content_height()
        if isinstance(msg, KeyMsg):
            self.input.update(msg)
        if self.input.value() != old_input:
            self.clear_completion()
            if self.input_content_height() != old_height:
                self.resize_viewports()
                self.refresh_content()
                self.refresh_notices()
        return []

    # -- 补全 ---------------------------------------------------------------
    def complete_input(self, delta: int) -> list[Action]:
        """Tab / Shift+Tab 触发的补全。对应 Go ``completeInput``。"""
        if self.completion_state.visible():
            self.select_completion(delta)
            return []
        return [
            Action(
                ACTION_COMPLETE,
                CompleteRequest(
                    delta=delta,
                    text=self.input.value(),
                    cursor=textarea_cursor_rune_position(self.input),
                ),
            )
        ]

    async def resolve_completion(self, request: CompleteRequest) -> list[Item]:
        """解析补全候选。对应 Go ``(*tuiModel).complete``。

        顺序与 Go 一致：本地文件引用优先 → 补全服务 → handler 的老式
        ``complete(text) []string`` 接口。
        """
        completion_request = CompletionRequest(text=request.text, cursor=request.cursor)
        resolver = self.local_file_resolver()
        if resolver is not None:
            items = resolver.complete_sync(self.ctx, completion_request)
            if items:
                return items
        if self.completion is not None:
            result = self.completion.complete(self.ctx, completion_request)
            if inspect.isawaitable(result):
                result = await result
            return list(result) if result else []
        return await self._legacy_completion(request.text)

    async def _legacy_completion(self, value: str) -> list[Item]:
        """兼容 Go 里 handler 自己实现 ``Complete(text string) []string`` 的老接口。"""
        legacy = getattr(self.handler, "complete", None)
        if not callable(legacy):
            return []
        texts = legacy(value)
        if inspect.isawaitable(texts):
            texts = await texts
        return [Item(text=text) for text in (texts or [])]

    def apply_completion_result(self, delta: int, items: Sequence[Item]) -> None:
        """回灌补全结果。对应 Go ``completeInput`` 里 switch len(items) 之后的分支。"""
        items = list(items)
        if not items:
            self.clear_completion()
            return
        if len(items) == 1:
            self.clear_completion()
            self.apply_completion_item(items[0], self.input.value())
            return
        index = 0 if delta >= 0 else len(items) - 1
        self.completion_state = CompletionState(
            base=self.input.value(), items=list(items), index=index
        )
        self.apply_current_completion()
        self.resize_viewports()

    def local_file_resolver(self) -> LocalFileResolver | None:
        """对应 Go ``(*tuiModel).localFileResolver``。"""
        if self.local_files is not None:
            return self.local_files
        return new_local_file_resolver("")

    def expand_local_file_references(self, value: str) -> str:
        """对应 Go ``(*tuiModel).expandLocalFileReferences``。"""
        resolver = self.local_file_resolver()
        if resolver is None:
            return value
        return resolver.expand_references(value)

    def clear_completion(self) -> None:
        """对应 Go ``(*tuiModel).clearCompletion``。"""
        self.completion_state = CompletionState()
        self.resize_viewports()

    def select_completion(self, delta: int) -> None:
        """对应 Go ``(*tuiModel).selectCompletion``。"""
        if not self.completion_state.visible():
            return
        count = len(self.completion_state.items)
        self.completion_state.index = (self.completion_state.index + delta + count) % count
        self.apply_current_completion()

    def apply_current_completion(self) -> None:
        """对应 Go ``(*tuiModel).applyCurrentCompletion``。"""
        state = self.completion_state
        if not state.items or state.index < 0 or state.index >= len(state.items):
            return
        self.apply_completion_item(state.items[state.index], state.base)

    def apply_completion_item(self, item: Item, value: str) -> None:
        """把候选写进输入框。对应 Go ``(*tuiModel).applyCompletionItem``。"""
        text = item.text
        if text == "":
            return
        if (
            item.replace_start >= 0
            and item.replace_end >= item.replace_start
            and item.replace_end <= len(value)
            and (item.replace_start != 0 or item.replace_end != 0)
        ):
            updated = value[: item.replace_start] + text + value[item.replace_end :]
            set_textarea_value_and_cursor(self.input, updated, item.replace_start + len(text))
            return
        self.input.set_value(text)

    # -- 历史 ---------------------------------------------------------------
    def prev_history(self) -> list[Action]:
        """对应 Go ``(*tuiModel).prevHistory``。"""
        if not self.history:
            return []
        self.hist_pos = max(0, self.hist_pos - 1)
        self.input.set_value(self.history[self.hist_pos])
        self.resize_viewports()
        self.refresh_content()
        self.refresh_notices()
        return []

    def next_history(self) -> list[Action]:
        """对应 Go ``(*tuiModel).nextHistory``。"""
        if not self.history:
            return []
        if self.hist_pos >= len(self.history) - 1:
            self.hist_pos = len(self.history)
            self.clear_input()
        else:
            self.hist_pos += 1
            self.input.set_value(self.history[self.hist_pos])
            self.resize_viewports()
            self.refresh_content()
            self.refresh_notices()
        return []

    # -- 提交 ---------------------------------------------------------------
    def submit_input(self) -> list[Action]:
        """提交输入。对应 Go ``(*tuiModel).submitInput``。"""
        self.clear_completion()
        text = self.input.value().strip()
        if text == "":
            return []
        if text == "/exit":
            return [Action(ACTION_QUIT)]
        try:
            send_text = self.expand_local_file_references(text)
        except AtriXError as exc:
            self.append_notice_level("local file reference: " + str(exc), logging.ERROR)
            return []
        self.history.append(text)
        self.hist_pos = len(self.history)
        self.clear_input()
        self.append_user_content(text)
        return [Action(ACTION_DISPATCH, send_text)]

    def clear_input(self) -> None:
        """对应 Go ``(*tuiModel).clearInput``。"""
        self.input.reset()
        self.resize_viewports()
        self.refresh_content()
        self.refresh_notices()
