"""TUI 的聊天区内容与通知面板维护。

对应 Go：``internal/platform/cli/tui.go`` 里 ``tuiModel`` 的内容/通知部分
（``appendUserContent`` / ``appendAssistantContent`` / ``appendReasoningContent`` /
``appendNotice*`` / ``refreshContent`` / ``refreshNotices`` / ``renderContent`` /
``wrappedContent``，以及 ``separatorLine`` / ``renderSpeakerLine`` /
``isSeparatorLine`` / ``tuiNoticeLevelStyle`` 这些自由函数）。

拆成 :class:`ContentMixin` 由 :class:`atri_x.platform.cli.tui.model.TuiModel` 继承，
与 :mod:`atri_x.platform.cli.tui.copy` / :mod:`atri_x.platform.cli.tui.mouse` 同一手法：
让 ``model.py``（Update 主流程）保持在文件长度上限内，同时不引入跨模块循环导入。

依赖宿主字段（与 Go 的 ``tuiModel`` 同名）：``content``、``notices``、
``inline_notice_levels``、``assistant_open``、``assistant_start``、``reasoning_open``、
``user_name``、``assistant_name``、``width``、``viewport``、``notice_viewport``、
``copy_state``。
依赖宿主方法：``layout_widths()``、``render_copy_lines()``、``notice_copy_lines()``。
"""

from __future__ import annotations

import logging

from atri_x.platform.cli.tui import theme
from atri_x.platform.cli.tui.copy import (
    REGION_CHAT,
    REGION_NOTICE,
    split_lines,
)
from atri_x.platform.cli.tui.messages import TuiNotice
from atri_x.platform.cli.tui.style import Style, wrap_display_width

#: 通知面板最多保留条数。对应 Go ``appendNoticeLevel`` 里的字面量 50。
MAX_NOTICES = 50

#: 空内容时的提示语。对应 Go ``renderContent`` 的字面量。
EMPTY_CONTENT_HINT = "还没有消息。输入内容后按 Enter 发送。"

#: 通知面板为空时的提示语。对应 Go ``refreshNotices`` 的字面量。
EMPTY_NOTICES_HINT = "暂无通知"

#: 思维链标记（写入前剥掉）。对应 Go ``appendReasoningContent`` 的四次 ReplaceAll。
REASONING_MARKERS = ("[thinking] ", "[thinking]", "[/thinking]\n\n", "[/thinking]")


__all__ = [
    "EMPTY_CONTENT_HINT",
    "EMPTY_NOTICES_HINT",
    "MAX_NOTICES",
    "REASONING_MARKERS",
    "ContentMixin",
    "is_separator_line",
    "level_style",
    "render_speaker_line",
]


def level_style(level: int) -> Style:
    """按级别取通知样式。对应 Go ``tuiNoticeLevelStyle``。"""
    if level >= logging.ERROR:
        return theme.TUI_ERROR_STYLE
    if level >= logging.WARNING:
        return theme.TUI_WARN_STYLE
    if level >= logging.INFO:
        return theme.TUI_INFO_STYLE
    return theme.TUI_DEBUG_STYLE


def render_speaker_line(line: str, name: str, style: Style) -> str:
    """给 ``名字:`` 前缀上色。对应 Go ``renderSpeakerLine``。"""
    prefix = name + ":"
    return style.render(prefix) + line[len(prefix) :]


def is_separator_line(line: str) -> bool:
    """整行都是 ``─``。对应 Go ``isSeparatorLine``。"""
    line = line.strip()
    return line != "" and line.strip("─") == ""


class ContentMixin:
    """把 ``tui.go`` 的内容/通知方法挂到 :class:`TuiModel` 上。"""

    # -- 内容 ---------------------------------------------------------------
    def append_user_content(self, text: str) -> None:
        """对应 Go ``(*tuiModel).appendUserContent``。"""
        if self.content.strip() != "":
            self.content += "\n" + self.separator_line() + "\n"
        self.content += self.user_name + ": " + text + "\n"
        self.assistant_open = False
        self.reasoning_open = False
        self.refresh_content()

    def separator_line(self) -> str:
        """对应 Go ``(*tuiModel).separatorLine``。"""
        width = self.width
        if width <= 0:
            width = 32
        if width > 80:
            width = 80
        return "─" * max(8, width)

    def append_reasoning_content(self, text: str) -> None:
        """对应 Go ``(*tuiModel).appendReasoningContent``。"""
        for marker in REASONING_MARKERS:
            text = text.replace(marker, "")
        if text == "":
            return
        if not self.reasoning_open:
            if self.content.strip() != "":
                self.content += "\n"
            self.content += "thinking: "
            self.reasoning_open = True
        self.content += text
        self.refresh_content()

    def append_assistant_content(self, text: str) -> None:
        """对应 Go ``(*tuiModel).appendAssistantContent``。"""
        if text == "":
            return
        if not self.assistant_open:
            if self.reasoning_open:
                self.content += "\n\n"
                self.reasoning_open = False
            elif self.content.strip() != "":
                self.content += "\n"
            self.assistant_start = len(self.content)
            self.content += self.assistant_name + ": "
            self.assistant_open = True
        self.content += text
        self.refresh_content()

    def replace_assistant_content(self, text: str) -> None:
        """对应 Go ``(*tuiModel).replaceAssistantContent``。"""
        if not self.assistant_open:
            self.append_assistant_content(text)
            return
        if self.assistant_start < 0 or self.assistant_start > len(self.content):
            self.assistant_start = len(self.content)
        self.content = self.content[: self.assistant_start] + self.assistant_name + ": " + text
        self.refresh_content()

    def finish_assistant_content(self) -> None:
        """对应 Go ``(*tuiModel).finishAssistantContent``。"""
        self.assistant_open = False
        self.assistant_start = 0
        self.reasoning_open = False
        self.refresh_content()

    def append_content(self, text: str) -> None:
        """对应 Go ``(*tuiModel).appendContent``。"""
        if text == "":
            return
        if self.reasoning_open:
            if not self.content.endswith("\n"):
                self.content += "\n"
            self.reasoning_open = False
        self.content += text
        self.refresh_content()

    # -- 通知 ---------------------------------------------------------------
    def append_notice(self, text: str) -> None:
        """对应 Go ``(*tuiModel).appendNotice``（默认 INFO 级）。"""
        self.append_notice_level(text, logging.INFO)

    def append_notice_level(self, text: str, level: int) -> None:
        """对应 Go ``(*tuiModel).appendNoticeLevel``。"""
        text = (text or "").strip()
        if text == "":
            return
        if self.layout_notice_visible():
            self.notices.append(TuiNotice(text=text, level=level))
            if len(self.notices) > MAX_NOTICES:
                self.notices = self.notices[len(self.notices) - MAX_NOTICES :]
            self.refresh_notices()
            return
        self.append_notice_content(text, level)

    def append_notice_content(self, text: str, level: int) -> None:
        """对应 Go ``(*tuiModel).appendNoticeContent``。"""
        if self.content.strip() != "":
            self.content += "\n" + self.separator_line() + "\n"
        self.content += "[notice] " + text
        self.inline_notice_levels.append(level)
        self.assistant_open = False
        self.reasoning_open = False
        self.refresh_content()

    def layout_notice_visible(self) -> bool:
        """通知面板是否可见。对应 Go ``(*tuiModel).layoutNoticeVisible``。"""
        _, notice_width = self.layout_widths()
        return notice_width > 0

    # -- 重绘 ---------------------------------------------------------------
    def refresh_notices(self) -> None:
        """重绘通知面板。对应 Go ``(*tuiModel).refreshNotices``。"""
        if not self.layout_notice_visible():
            return
        if self.copy_state.active() and self.copy_state.region == REGION_NOTICE:
            self.notice_viewport.set_content(
                self.render_copy_lines(REGION_NOTICE, self.notice_copy_lines())
            )
            return
        content_width = max(1, self.notice_viewport.width - 4)
        parts = [theme.TUI_NOTICE_STYLE.bold_(True).render("Notices")]
        if not self.notices:
            parts.append("\n" + theme.TUI_TITLE_MUTED_STYLE.render(EMPTY_NOTICES_HINT))
            self.notice_viewport.set_content("".join(parts))
            return
        separator = theme.TUI_SEPARATOR_STYLE.render("─" * max(8, content_width))
        for notice in self.notices:
            style = level_style(notice.level)
            parts.append("\n" + separator + "\n")
            parts.append(style.render("• "))
            parts.append(style.render(wrap_display_width(notice.text, content_width)))
        self.notice_viewport.set_content("".join(parts))
        self.notice_viewport.goto_bottom()

    def refresh_content(self) -> None:
        """重绘聊天区。对应 Go ``(*tuiModel).refreshContent``。"""
        content = self.wrapped_content()
        if self.copy_state.active() and self.copy_state.region == REGION_CHAT:
            self.viewport.set_content(self.render_copy_lines(REGION_CHAT, split_lines(content)))
            return
        self.viewport.set_content(self.render_content(content))
        self.viewport.goto_bottom()

    def refresh_copy_region(self) -> None:
        """重绘 copy mode 所在区域。对应 Go ``(*tuiModel).refreshCopyRegion``。"""
        if self.copy_state.region == REGION_CHAT:
            self.refresh_content()
        elif self.copy_state.region == REGION_NOTICE:
            self.refresh_notices()

    def render_content(self, content: str) -> str:
        """给内容行上色。对应 Go ``(*tuiModel).renderContent``。"""
        if content == "":
            return theme.TUI_TITLE_MUTED_STYLE.render(EMPTY_CONTENT_HINT)
        lines = content.split("\n")
        in_reasoning = False
        in_notice = False
        notice_index = 0
        notice_style = theme.TUI_INFO_STYLE
        for index, line in enumerate(lines):
            if line.startswith(self.user_name + ": "):
                in_reasoning = False
                in_notice = False
                lines[index] = render_speaker_line(line, self.user_name, theme.TUI_USER_STYLE)
            elif line.startswith(self.assistant_name + ": "):
                in_reasoning = False
                in_notice = False
                lines[index] = render_speaker_line(
                    line, self.assistant_name, theme.TUI_ASSISTANT_STYLE
                )
            elif line.startswith("thinking: "):
                in_reasoning = True
                in_notice = False
                lines[index] = theme.TUI_REASONING_STYLE.render(line)
            elif is_separator_line(line):
                in_reasoning = False
                in_notice = False
                lines[index] = theme.TUI_SEPARATOR_STYLE.render(line)
            elif line.startswith("[notice] "):
                level = (
                    self.inline_notice_levels[notice_index]
                    if notice_index < len(self.inline_notice_levels)
                    else logging.INFO
                )
                notice_index += 1
                in_reasoning = False
                in_notice = True
                notice_style = level_style(level)
                lines[index] = notice_style.render(line)
            elif in_reasoning:
                lines[index] = theme.TUI_REASONING_STYLE.render(line)
            elif in_notice:
                lines[index] = notice_style.render(line)
        return "\n".join(lines)

    def wrapped_content(self) -> str:
        """按视口宽度折行后的内容。对应 Go ``(*tuiModel).wrappedContent``。"""
        if self.viewport.width <= 1:
            return self.content
        return wrap_display_width(self.content, self.viewport.width)
