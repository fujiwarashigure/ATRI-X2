"""TUI 的整帧渲染与布局。

对应 Go：``internal/platform/cli/tui.go`` 里 ``tuiModel`` 的渲染/布局部分
（``View`` / ``layoutWidths`` / ``resizeViewports`` / ``inputView`` /
``inputSeparatorView`` / ``completionView`` / ``headerView`` / ``bodyView`` /
``statusView`` / ``runtimeStatusText``，以及 ``completionDisplayText``）。

拆成 :class:`ViewMixin` 由 :class:`atri_x.platform.cli.tui.model.TuiModel` 继承
（同 :mod:`atri_x.platform.cli.tui.copy` / ``mouse`` / ``content`` 的手法）。

布局常量（原本与 Go ``tui.go`` 的 const 块并列）放在本模块，因为它们是**布局**参数；
``model`` 会再导出它们，保持 ``tui.model`` 的这一组公开名不变。

依赖宿主字段：``width``、``height``、``notice_width``、``viewport``、
``notice_viewport``、``input``、``completion_state``、``copy_state``、
``runtime_status``、``status_now``。
依赖宿主方法：``now()``、``copy_status_text()``、``layout_notice_visible()``。
"""

from __future__ import annotations

from atri_x.completion.completion import Item
from atri_x.platform.cli.tui import theme
from atri_x.platform.cli.tui.copy import clamp_int
from atri_x.platform.cli.tui.mouse import (
    MIN_CHAT_PANEL_WIDTH,
    MIN_NOTICE_PANEL_WIDTH,
    NOTICE_PANEL_FRAME_WIDTH,
)
from atri_x.platform.cli.tui.style import (
    Style,
    join_horizontal,
    place_vertical,
    style_width,
    truncate_left,
    wrap_display_width,
)
from atri_x.runtime.status import format_compact
from atri_x.utils.runes import string_width

#: 通知面板默认宽度。对应 Go ``noticePanelWidth``。
NOTICE_PANEL_WIDTH = 40

#: 启用通知面板所需的最小终端宽度。对应 Go ``minNoticeLayoutWidth``。
MIN_NOTICE_LAYOUT_WIDTH = 100

#: 补全弹窗最多显示条数。对应 Go ``maxCompletionPopupItems``。
MAX_COMPLETION_POPUP_ITEMS = 8

#: 输入框左侧提示符占的列数。对应 Go ``tuiInputGutterWidth``。
TUI_INPUT_GUTTER_WIDTH = 2

#: 输入框高度范围。对应 Go ``minTUIInputHeight`` / ``maxTUIInputHeight``。
MIN_TUI_INPUT_HEIGHT = 3
MAX_TUI_INPUT_HEIGHT = 6

__all__ = [
    "MAX_COMPLETION_POPUP_ITEMS",
    "MAX_TUI_INPUT_HEIGHT",
    "MIN_NOTICE_LAYOUT_WIDTH",
    "MIN_TUI_INPUT_HEIGHT",
    "NOTICE_PANEL_WIDTH",
    "TUI_INPUT_GUTTER_WIDTH",
    "ViewMixin",
    "completion_display_text",
]


def completion_display_text(item: Item) -> str:
    """补全项的显示文本。对应 Go ``completionDisplayText``。"""
    label = item.label or item.text
    if not item.description:
        return label
    return label + "  " + item.description


class ViewMixin:
    """把 ``tui.go`` 的渲染/布局方法挂到 :class:`TuiModel` 上。"""

    def view(self) -> str:
        """整帧渲染。对应 Go ``(*tuiModel).View``。"""
        return (
            self.header_view()
            + "\n"
            + self.body_view()
            + self.completion_view()
            + "\n"
            + self.status_view()
            + "\n"
            + self.input_separator_view()
            + "\n"
            + self.input_view()
        )

    # -- 布局 ---------------------------------------------------------------
    def layout_widths(self) -> tuple[int, int]:
        """``(chat 宽, notice 宽)``。对应 Go ``layoutWidths``。"""
        if self.width < MIN_NOTICE_LAYOUT_WIDTH:
            return max(1, self.width), 0
        notice_width = self.notice_width if self.notice_width > 0 else NOTICE_PANEL_WIDTH
        max_notice_width = self.width - NOTICE_PANEL_FRAME_WIDTH - MIN_CHAT_PANEL_WIDTH
        notice_width = clamp_int(notice_width, MIN_NOTICE_PANEL_WIDTH, max_notice_width)
        return self.width - notice_width - NOTICE_PANEL_FRAME_WIDTH, notice_width

    def resize_viewports(self) -> None:
        """按当前尺寸重算视口与输入框。对应 Go ``resizeViewports``。"""
        chat_width, notice_width = self.layout_widths()
        self.input.set_width(max(1, self.width - TUI_INPUT_GUTTER_WIDTH))
        input_height = self.input_view_height()
        body_height = max(1, self.height - 4 - self.completion_view_height() - input_height)
        self.viewport.width = chat_width
        self.viewport.height = body_height
        self.notice_viewport.width = notice_width
        self.notice_viewport.height = body_height
        self.input.set_height(min(self.input_content_height(), MAX_TUI_INPUT_HEIGHT))

    def input_view_height(self) -> int:
        """输入区高度。对应 Go ``inputViewHeight``。"""
        return min(max(MIN_TUI_INPUT_HEIGHT, self.input_content_height()), MAX_TUI_INPUT_HEIGHT)

    def input_content_height(self) -> int:
        """输入内容折行后的行数。对应 Go ``inputContentHeight``。"""
        width = max(1, self.input.width)
        height = 0
        for line in self.input.value().split("\n"):
            height += max(1, wrap_display_width(line, width).count("\n") + 1)
        return max(1, height)

    # -- 各区块 -------------------------------------------------------------
    def input_view(self) -> str:
        """对应 Go ``inputView``。"""
        height = self.input_view_height()
        prompt = place_vertical(height, "center", theme.TUI_INPUT_PROMPT_STYLE.render("❯ "))
        content = place_vertical(height, "center", self.input.value())
        return join_horizontal("top", prompt, content)

    def input_separator_view(self) -> str:
        """输入区上方的分隔线与快捷键提示。对应 Go ``inputSeparatorView``。"""
        if self.width <= 0:
            return ""
        padding = max(1, self.width // 20)
        min_separator_width = min(self.width, max(8, self.width // 3))
        keys = self.input_shortcut_text()
        max_keys_width = max(0, self.width - padding * 2 - min_separator_width)
        keys = truncate_left(keys, max_keys_width)
        keys_width = string_width(keys)
        if self.width <= 80:
            keys_width = style_width(keys)
        separator_width = max(0, self.width - padding * 2 - keys_width)
        return (
            " " * padding
            + theme.TUI_INPUT_SEPARATOR_STYLE.render("─" * separator_width)
            + " " * padding
            + theme.TUI_STATUS_STYLE.render(keys)
        )

    def input_shortcut_text(self) -> str:
        """对应 Go ``inputShortcutText``。"""
        if self.layout_notice_visible():
            return "Alt+h/l copy · Esc clear · Ctrl+C exit"
        return "Alt+h copy · Esc clear · Ctrl+C exit"

    def completion_view_height(self) -> int:
        """补全弹窗占的行数。对应 Go ``completionViewHeight``。"""
        if not self.completion_state.visible():
            return 0
        return min(len(self.completion_state.items), MAX_COMPLETION_POPUP_ITEMS) + 3

    def completion_popup_width(self) -> int:
        """对应 Go ``completionPopupWidth``。"""
        chat_width, _ = self.layout_widths()
        return min(max(24, chat_width // 2), max(24, chat_width))

    def completion_view(self) -> str:
        """对应 Go ``completionView``。"""
        if not self.completion_state.visible():
            return ""
        items = self.completion_state.items
        start = 0
        if self.completion_state.index >= MAX_COMPLETION_POPUP_ITEMS:
            start = self.completion_state.index - MAX_COMPLETION_POPUP_ITEMS + 1
        end = min(len(items), start + MAX_COMPLETION_POPUP_ITEMS)
        lines: list[str] = []
        for index in range(start, end):
            text = completion_display_text(items[index])
            if index == self.completion_state.index:
                text = theme.TUI_COMPLETION_SELECTED_STYLE.with_(
                    width=max(1, self.completion_popup_width() - 4)
                ).render(text)
            lines.append(text)
        lines.append(
            theme.TUI_TITLE_MUTED_STYLE.render(
                f"{self.completion_state.index + 1}/{len(items)} · Tab/↑/↓"
            )
        )
        return "\n" + theme.TUI_COMPLETION_STYLE.with_(
            width=self.completion_popup_width()
        ).render("\n".join(lines))

    def header_view(self) -> str:
        """对应 Go ``headerView``。"""
        chat_width, notice_width = self.layout_widths()
        if self.width <= 0:
            return theme.TUI_TITLE_STYLE.render("ATRI-X CLI")
        chat_title = theme.TUI_TITLE_STYLE.render("ATRI-X CLI")
        if notice_width > 0:
            chat_title += " " + theme.TUI_TITLE_MUTED_STYLE.render("chat")
        return Style(width=chat_width).render(chat_title)

    def status_view(self) -> str:
        """对应 Go ``statusView``。"""
        if self.copy_state.active():
            return theme.TUI_STATUS_STYLE.render(self.copy_status_text())
        return theme.TUI_STATUS_STYLE.render(self.runtime_status_text())

    def body_view(self) -> str:
        """对应 Go ``bodyView``。"""
        chat_width, notice_width = self.layout_widths()
        if notice_width <= 0:
            return self.viewport.view()
        chat = Style(width=chat_width).render(self.viewport.view())
        notice = theme.TUI_PANEL_STYLE.with_(width=notice_width).render(
            self.notice_viewport.view()
        )
        return join_horizontal("top", chat, notice)

    def runtime_status_text(self) -> str:
        """状态栏里的运行状态。对应 Go ``runtimeStatusText``。"""
        snapshot = self.runtime_status
        if not (
            snapshot.session_id or snapshot.provider or snapshot.model or snapshot.phase
        ):
            return ""
        now = self.status_now or self.now()
        width = self.width
        if width > 24:
            width -= 24
        return format_compact(snapshot, now, width)
