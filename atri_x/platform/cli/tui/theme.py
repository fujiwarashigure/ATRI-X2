"""TUI 主题：Go 里 ``lipgloss.NewStyle()...`` 常量块的等价物。

对应 Go：``tui.go`` 的 ``tui*Style`` 变量块与 ``tui_copy.go`` 末尾的
``tuiCopy*Style`` 变量块。

颜色编号**逐字**沿用 Go 的 ``lipgloss.Color("NN")``（ANSI 256 色号）。
``AdaptiveColor``（仅 ``tuiInputSeparatorStyle`` 用）取 Dark 值 ``#666666``，
换算到 256 色是 ``241``；浅色终端下观感会略有差别，这是刻意的取舍。

命名差异：Go 的 ``tuiTitleStyle`` → :data:`TUI_TITLE_STYLE`（PORTING §1.4 的
「变量 snake_case」）。边框颜色与前景色共用 ``Style.fg``（见
:mod:`atri_x.platform.cli.tui.style` 的说明）。
"""

from __future__ import annotations

from atri_x.platform.cli.tui.style import Style

#: 标题底色 / 前景。对应 ``tuiTitleStyle``（Bold + fg 15 + bg 62 + Padding(0,1)）。
TUI_TITLE_STYLE = Style(bold=True, fg=15, bg=62, padding_left=1)

#: 弱化标题。对应 ``tuiTitleMutedStyle``。
TUI_TITLE_MUTED_STYLE = Style(fg=8)

#: 状态栏。对应 ``tuiStatusStyle``。
TUI_STATUS_STYLE = Style(fg=8)

#: 用户发言。对应 ``tuiUserStyle``。
TUI_USER_STYLE = Style(bold=True, fg=81)

#: 助手发言。对应 ``tuiAssistantStyle``。
TUI_ASSISTANT_STYLE = Style(bold=True, fg=217)

#: 思维链。对应 ``tuiReasoningStyle``。
TUI_REASONING_STYLE = Style(fg=244)

#: 通知（面板标题）。对应 ``tuiNoticeStyle``。
TUI_NOTICE_STYLE = Style(fg=11)

#: debug 级通知。对应 ``tuiDebugStyle``。
TUI_DEBUG_STYLE = Style(fg=15)

#: info 级通知。对应 ``tuiInfoStyle``。
TUI_INFO_STYLE = Style(fg=10)

#: warn 级通知。对应 ``tuiWarnStyle``。
TUI_WARN_STYLE = Style(fg=11)

#: error 级通知。对应 ``tuiErrorStyle``。
TUI_ERROR_STYLE = Style(fg=9)

#: 分隔线。对应 ``tuiSeparatorStyle``。
TUI_SEPARATOR_STYLE = Style(fg=8)

#: 通知面板。对应 ``tuiPanelStyle``（左边框 + PaddingLeft(1)）。
TUI_PANEL_STYLE = Style(border="left", fg=8, padding_left=1)

#: 补全弹窗。对应 ``tuiCompletionStyle``（圆角边框 + Padding(0,1)）。
TUI_COMPLETION_STYLE = Style(border="rounded", fg=62)

#: 补全选中项。对应 ``tuiCompletionSelectedStyle``。
TUI_COMPLETION_SELECTED_STYLE = Style(fg=15, bg=62)

#: 输入提示符。对应 ``tuiInputPromptStyle``。
TUI_INPUT_PROMPT_STYLE = Style(bold=True, fg=81)

#: 输入分隔线。对应 ``tuiInputSeparatorStyle``（AdaptiveColor 的 Dark 值）。
TUI_INPUT_SEPARATOR_STYLE = Style(fg=241)

#: copy mode 光标。对应 ``tuiCopyCursorStyle``（Reverse）。
TUI_COPY_CURSOR_STYLE = Style(reverse=True)

#: copy mode 选区。对应 ``tuiCopySelectionStyle``。
TUI_COPY_SELECTION_STYLE = Style(fg=15, bg=62)

#: copy mode 搜索命中。对应 ``tuiCopySearchStyle``。
TUI_COPY_SEARCH_STYLE = Style(fg=11, bg=236)

#: copy mode 当前搜索命中。对应 ``tuiCopySearchCurrentStyle``。
TUI_COPY_SEARCH_CURRENT_STYLE = Style(fg=15, bg=99)

__all__ = [name for name in dir() if name.endswith("_STYLE")]
