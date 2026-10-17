"""本地 CLI 的终端界面（Tarui）——对应 Go 包 ``internal/platform/cli`` 的 TUI 部分。

PORTING.md §4.1 把 ``atri_x/platform/cli/tui/`` 单列为一个"Python 必须新增"的模块：
Go 侧的 TUI 由 ``bubbletea``（Elm 架构）+ ``lipgloss``（样式）+ ``bubbles``
（``viewport`` / ``textarea``）组成，标准库没有对应能力。本包用**纯标准库**重写同一套
交互语义（命令、状态栏、输入行为），渲染简化为 ANSI 行式渲染。

模块 ↔ Go 文件对照：

=========================  ============================================================
Python 模块                 Go 文件 / 职责
=========================  ============================================================
``keys.py``                ``tea.KeyMsg`` / ``tea.MouseMsg`` 的等价物与键名规范化
``style.py``               ``lipgloss`` 的最小实现（ANSI 样式、宽度、拼接、边框）
``theme.py``               ``tui.go`` / ``tui_copy.go`` 里的 ``lipgloss`` 样式常量
``viewport.py``            ``bubbles/viewport`` 的行缓冲等价物
``input.py``               ``bubbles/textarea`` 的等价物
``messages.py``            ``tui.go`` 的 ``tui*Msg`` / ``completionState`` / 粘贴突发
``copy.py``                ``tui_copy.go``（copy mode：移动、选区、搜索、复制）
``mouse.py``               ``tui_mouse.go``（分区、分隔线拖拽、滚轮）
``content.py``             ``tui.go`` 的内容/通知维护（append*/refresh*，Python 拆出）
``view.py``                ``tui.go`` 的布局与整帧渲染（Python 拆出）
``model.py``               ``tui.go`` 的 ``tuiModel``（Update/补全/历史/提交）
``program.py``             ``tui.go`` 的 ``runTUI``（主循环、键盘事件源、渲染）
=========================  ============================================================

导入顺序刻意按依赖排列：``model`` 依赖 ``copy`` / ``mouse`` / ``content`` / ``view``，
``program`` 依赖 ``model``，放在最后。
"""

from __future__ import annotations

from atri_x.platform.cli.tui import keys as keys
from atri_x.platform.cli.tui import style as style
from atri_x.platform.cli.tui import theme as theme
from atri_x.platform.cli.tui import viewport as viewport
from atri_x.platform.cli.tui import messages as messages
from atri_x.platform.cli.tui import input as input  # noqa: A004 - 对齐 Go 的输入框职责
from atri_x.platform.cli.tui import copy as copy
from atri_x.platform.cli.tui import mouse as mouse
from atri_x.platform.cli.tui import content as content
from atri_x.platform.cli.tui import view as view
from atri_x.platform.cli.tui import model as model
from atri_x.platform.cli.tui import program as program

from atri_x.platform.cli.tui.content import (  # noqa: F401
    ContentMixin,
    is_separator_line,
    level_style,
    render_speaker_line,
)
from atri_x.platform.cli.tui.keys import (  # noqa: F401
    KeyMsg,
    MouseMsg,
)
from atri_x.platform.cli.tui.messages import (  # noqa: F401
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
)
from atri_x.platform.cli.tui.model import (  # noqa: F401
    Action,
    CompleteRequest,
    TuiModel,
    WindowSizeMsg,
    new_tui_model,
)
from atri_x.platform.cli.tui.program import (  # noqa: F401
    CallbackKeySource,
    StdinKeySource,
    TuiProgram,
    run_tui,
)
from atri_x.platform.cli.tui.style import (  # noqa: F401
    Style,
    strip_ansi,
    visible_width,
)
from atri_x.platform.cli.tui.view import (  # noqa: F401
    ViewMixin,
    completion_display_text,
)

__all__ = [name for name in dir() if not name.startswith("_")]
