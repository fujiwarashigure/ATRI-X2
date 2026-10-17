"""TUI 的鼠标处理：分区判定、分隔线拖拽、滚轮与鼠标定位光标。

对应 Go：``internal/platform/cli/tui_mouse.go``。

拆成 :class:`MouseMixin` 由 :class:`atri_x.platform.cli.tui.model.TuiModel` 继承
（同 :mod:`atri_x.platform.cli.tui.copy` 的处理方式）。

依赖宿主字段：``width``、``height``、``notice_width``、``resizing_notice``、
``viewport``、``notice_viewport``、``completion_state``、``copy_state``。
依赖宿主方法：``layout_widths()``、``resize_viewports()``、``refresh_content()``、
``refresh_notices()``、``completion_view_height()``、``enter_copy_mode(region)``、
``ensure_copy_cursor_visible()``、``copy_lines(region)``。

与 Go 的差异：Go 的 ``handleMouse`` 返回 ``(tea.Model, tea.Cmd)``；本实现就地改自身
状态并返回 ``None``（鼠标分支从不返回非 nil 的 Cmd）。
"""

from __future__ import annotations

from atri_x.platform.cli.tui.copy import (
    CopyCursor,
    REGION_CHAT,
    REGION_COMPLETION,
    REGION_INPUT,
    REGION_NONE,
    REGION_NOTICE,
    clamp_int,
    col_from_display_width,
)
from atri_x.platform.cli.tui.keys import (
    MOUSE_ACTION_MOTION,
    MOUSE_ACTION_PRESS,
    MOUSE_ACTION_RELEASE,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_WHEEL_DOWN,
    MOUSE_BUTTON_WHEEL_UP,
    MOUSE_WHEEL_DOWN,
    MOUSE_WHEEL_UP,
    MouseMsg,
)

#: 通知面板边框占的列数。对应 Go ``noticePanelFrameWidth``。
NOTICE_PANEL_FRAME_WIDTH = 2

#: 通知面板最小宽度。对应 Go ``minNoticePanelWidth``。
MIN_NOTICE_PANEL_WIDTH = 20

#: 聊天面板最小宽度。对应 Go ``minChatPanelWidth``。
MIN_CHAT_PANEL_WIDTH = 40

__all__ = [
    "MIN_CHAT_PANEL_WIDTH",
    "MIN_NOTICE_PANEL_WIDTH",
    "NOTICE_PANEL_FRAME_WIDTH",
    "MouseMixin",
    "mouse_wheel_delta",
]


def mouse_wheel_delta(delta: int) -> int:
    """滚轮一格的滚动行数（非正数退回 3）。对应 Go ``mouseWheelDelta``。"""
    return delta if delta > 0 else 3


class MouseMixin:
    """把 ``tui_mouse.go`` 的方法挂到 :class:`TuiModel` 上。"""

    def handle_mouse(self, msg: MouseMsg) -> None:
        """鼠标事件分派。对应 Go ``(*tuiModel).handleMouse``。"""
        event = msg.event()
        if self.resizing_notice:
            if event.action == MOUSE_ACTION_MOTION:
                self.resize_notice_from_divider(event.x)
                return
            if event.action == MOUSE_ACTION_RELEASE:
                self.resize_notice_from_divider(event.x)
                self.resizing_notice = False
                return
        if (
            event.action == MOUSE_ACTION_PRESS
            and event.button == MOUSE_BUTTON_LEFT
            and self.is_notice_divider(event.x, event.y)
        ):
            self.resizing_notice = True
            return

        region = self.mouse_region(event.x, event.y)
        if event.is_wheel() or event.type in (MOUSE_WHEEL_UP, MOUSE_WHEEL_DOWN):
            scroll_up = event.button == MOUSE_BUTTON_WHEEL_UP or event.type == MOUSE_WHEEL_UP
            scroll_down = event.button == MOUSE_BUTTON_WHEEL_DOWN or event.type == MOUSE_WHEEL_DOWN
            if region == REGION_NOTICE:
                delta = mouse_wheel_delta(self.notice_viewport.mouse_wheel_delta)
                if scroll_up:
                    self.notice_viewport.scroll_up(delta)
                elif scroll_down:
                    self.notice_viewport.scroll_down(delta)
            else:
                delta = mouse_wheel_delta(self.viewport.mouse_wheel_delta)
                if scroll_up:
                    self.viewport.scroll_up(delta)
                elif scroll_down:
                    self.viewport.scroll_down(delta)
            return
        if (
            event.action == MOUSE_ACTION_PRESS
            and event.button == MOUSE_BUTTON_LEFT
            and region in (REGION_CHAT, REGION_NOTICE)
        ):
            self.enter_copy_mode(region)
            self.copy_state.cursor = self.copy_cursor_from_mouse(region, event.x, event.y)
            self.ensure_copy_cursor_visible()

    def is_notice_divider(self, x: int, y: int) -> bool:
        """命中通知面板左侧分隔线。对应 Go ``isNoticeDivider``。"""
        chat_width, notice_width = self.layout_widths()
        if notice_width <= 0 or x != chat_width:
            return False
        body_top = 1
        body_bottom = body_top + max(1, self.viewport.height) - 1
        return body_top <= y <= body_bottom

    def resize_notice_from_divider(self, divider_x: int) -> None:
        """按分隔线位置调整通知面板宽度。对应 Go ``resizeNoticeFromDivider``。"""
        max_notice_width = self.width - NOTICE_PANEL_FRAME_WIDTH - MIN_CHAT_PANEL_WIDTH
        self.notice_width = clamp_int(
            self.width - divider_x - NOTICE_PANEL_FRAME_WIDTH,
            MIN_NOTICE_PANEL_WIDTH,
            max_notice_width,
        )
        self.resize_viewports()
        self.refresh_content()
        self.refresh_notices()

    def mouse_region(self, x: int, y: int) -> str:
        """鼠标所在分区。对应 Go ``mouseRegion``。"""
        if y == 0:
            return REGION_NONE
        body_top = 1
        body_bottom = body_top + max(1, self.viewport.height) - 1
        if body_top <= y <= body_bottom:
            chat_width, notice_width = self.layout_widths()
            if notice_width > 0 and x >= chat_width:
                return REGION_NOTICE
            return REGION_CHAT
        completion_top = body_bottom + 1
        completion_bottom = completion_top + self.completion_view_height()
        if self.completion_state.visible() and completion_top <= y <= completion_bottom:
            return REGION_COMPLETION
        if y >= self.height - 1:
            return REGION_INPUT
        return REGION_NONE

    def copy_cursor_from_mouse(self, region: str, x: int, y: int) -> CopyCursor:
        """把鼠标坐标换算成 copy mode 光标。对应 Go ``copyCursorFromMouse``。"""
        lines = self.copy_lines(region)
        if not lines:
            return CopyCursor()
        body_y = y - 1
        line = body_y
        col_x = x
        if region == REGION_CHAT:
            line += self.viewport.y_offset
        elif region == REGION_NOTICE:
            chat_width, _ = self.layout_widths()
            line += self.notice_viewport.y_offset
            col_x = max(0, x - chat_width - NOTICE_PANEL_FRAME_WIDTH)
        line = clamp_int(line, 0, len(lines) - 1)
        return CopyCursor(line=line, col=col_from_display_width(lines[line], col_x))
