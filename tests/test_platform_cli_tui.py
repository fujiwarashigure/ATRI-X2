"""``atri_x.platform.cli.tui`` 的模型、copy mode、鼠标与主循环测试。

对应 Go 测试：``internal/platform/cli/tui_test.go``、``tui_copy_test.go``、
``tui_mouse_test.go``（抓顶层可测行为，不逐行照抄）。

断言尽量针对**去掉 ANSI 后的纯文本**，避免把配色写死进测试。
"""

from __future__ import annotations

import asyncio
import io
import logging
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.completion.completion import Item, Request as CompletionRequest
from atri_x.errors import InvalidError, AtriXError
from atri_x.platform.cli.local_file import new_local_file_resolver
from atri_x.platform.cli.tui.copy import (
    MODE_COPY_NORMAL,
    MODE_COPY_VISUAL_CHAR,
    MODE_COPY_VISUAL_LINE,
    REGION_CHAT,
    REGION_INPUT,
    REGION_NONE,
    REGION_NOTICE,
    CopyCursor,
    clamp_int,
    col_from_display_width,
    next_word_end,
    next_word_start,
    plural,
    previous_word_start,
    region_name,
    search_match_ranges,
    selection_cols_for_line,
    split_lines,
)
from atri_x.platform.cli.tui.input import TextArea
from atri_x.platform.cli.tui.keys import (
    KEY_CTRL_C,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_TAB,
    KEY_UP,
    KeyMsg,
    MouseMsg,
    MOUSE_ACTION_PRESS,
    MOUSE_ACTION_RELEASE,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_WHEEL_DOWN,
    MOUSE_BUTTON_WHEEL_UP,
)
from atri_x.platform.cli.tui.model import (
    ACTION_COMPLETE,
    ACTION_DISPATCH,
    ACTION_QUIT,
    ACTION_READ_CLIPBOARD,
    NOTICE_PANEL_WIDTH,
    CompleteRequest,
    TuiModel,
    WindowSizeMsg,
    new_tui_model,
    textarea_cursor_rune_position,
)
from atri_x.platform.cli.tui.messages import (
    TuiClipboardMsg,
    TuiFinishAssistantMsg,
    TuiNoticeMsg,
    TuiOutputMsg,
    TuiPasteBurst,
    TuiReasoningMsg,
    TuiReplaceAssistantMsg,
    TuiStatusMsg,
    TuiStatusTickMsg,
)
from atri_x.platform.cli.tui.program import (
    CallbackKeySource,
    StdinKeySource,
    TuiProgram,
    run_tui,
)
from atri_x.platform.cli.tui.style import (
    Style,
    join_horizontal,
    place_vertical,
    strip_ansi,
    truncate_left,
    visible_width,
    wrap_display_width,
)
from atri_x.platform.cli.tui.viewport import Viewport
from atri_x.runtime.status import Snapshot
from atri_x.safecontext import CancelToken


class FakeClipboard:
    """记录写进剪贴板的文本。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.written: list[str] = []
        self.error = error

    def write_all(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.written.append(text)


class FakeCompleter:
    """按文本返回固定候选的补全服务。"""

    def __init__(self, items: list[Item]) -> None:
        self.items = items
        self.calls: list[CompletionRequest] = []

    async def complete(self, ctx, req: CompletionRequest) -> list[Item]:
        self.calls.append(req)
        return list(self.items)


class QueueKeySource:
    """阻塞式按键源：测试可以随时投消息，读到 ``None`` 时表示 EOF。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    async def read(self):
        return await self.queue.get()

    def push(self, msg) -> None:
        self.queue.put_nowait(msg)


class RecordingHandler:
    def __init__(self) -> None:
        self.received: list[str] = []

    async def handle_message(self, ctx, text: str) -> None:
        self.received.append(text)


def plain(view: str) -> str:
    return strip_ansi(view)


def make_model(**kwargs) -> TuiModel:
    options = {
        "user_name": "user",
        "assistant_name": "assistant",
        "clipboard": FakeClipboard(),
        "local_files": new_local_file_resolver("__no_such_dir__"),
        "paste_burst_enabled": False,
    }
    options.update(kwargs)
    return new_tui_model(**options)


# ---------------------------------------------------------------------------
# 基础件
# ---------------------------------------------------------------------------

class TestTuiPrimitives(unittest.TestCase):
    def test_style_render_and_width(self) -> None:
        style = Style(fg=15, bg=62, bold=True)
        rendered = style.render("hi")
        self.assertEqual(strip_ansi(rendered), "hi")
        self.assertEqual(visible_width(rendered), 2)
        self.assertTrue(rendered.startswith("\x1b[1;38;5;15;48;5;62m"))

    def test_style_width_pads(self) -> None:
        self.assertEqual(Style(width=5).render("ab"), "ab   ")

    def test_style_border_left_and_rounded(self) -> None:
        self.assertIn("│", Style(border="left", padding_left=1).render("x"))
        box = Style(border="rounded", width=6).render("ab")
        self.assertTrue(box.startswith("╭"))
        self.assertIn("│ab", box)

    def test_join_and_place(self) -> None:
        self.assertEqual(join_horizontal("top", "a\nb", "1\n2"), "a1\nb2")
        self.assertEqual(place_vertical(4, "center", "x"), "\nx\n\n")

    def test_truncate_left(self) -> None:
        self.assertEqual(truncate_left("abcdef", 3), "…ef")
        self.assertEqual(truncate_left("ab", 5), "ab")
        self.assertEqual(truncate_left("ab", 0), "")

    def test_wrap_display_width(self) -> None:
        self.assertEqual(wrap_display_width("abcd", 2), "ab\ncd")
        self.assertEqual(wrap_display_width("a\nb", 4), "a\nb")

    def test_viewport_scrolling(self) -> None:
        viewport = Viewport(width=10, height=2)
        viewport.set_content("1\n2\n3\n4")
        self.assertEqual(viewport.view(), "1\n2")
        viewport.scroll_down(1)
        self.assertEqual(viewport.view(), "2\n3")
        viewport.goto_bottom()
        self.assertEqual(viewport.view(), "3\n4")
        viewport.scroll_up(5)
        self.assertEqual(viewport.y_offset, 0)

    def test_text_area_editing(self) -> None:
        area = TextArea()
        area.update(KeyMsg.runes_key("abc"))
        self.assertEqual(area.value(), "abc")
        area.update(KeyMsg(type=KEY_ENTER))
        area.update(KeyMsg.runes_key("de"))
        self.assertEqual(area.value(), "abc\nde")
        self.assertEqual(area.line(), 1)
        self.assertEqual(area.line_count(), 2)
        area.update(KeyMsg(type="backspace"))
        self.assertEqual(area.value(), "abc\nd")
        area.update(KeyMsg(type=KEY_UP))
        self.assertEqual(area.line(), 0)
        area.set_cursor(1)
        self.assertEqual(area.line_info().column, 1)

    def test_text_area_char_limit_truncates(self) -> None:
        area = TextArea(char_limit=4)
        area.update(KeyMsg.runes_key("abc"))
        area.update(KeyMsg(type=KEY_ENTER))
        area.update(KeyMsg.runes_key("de"))
        # 上限是码点数：到顶后的插入被丢弃（Go 的 textarea 同样拒绝）。
        self.assertEqual(area.value(), "abc\n")
        self.assertEqual(len(area.value()), 4)

    def test_text_area_cursor_moves_and_delete(self) -> None:
        area = TextArea()
        area.set_value("ab\ncd")
        area.update(KeyMsg(type=KEY_LEFT))
        area.update(KeyMsg(type="delete"))
        self.assertEqual(area.value(), "ab\nc")
        area.update(KeyMsg(type=KEY_END))
        self.assertEqual(area.line_info().column, 1)
        area.update(KeyMsg(type="home"))
        self.assertEqual(area.line_info().column, 0)

    def test_text_area_cursor_helpers(self) -> None:
        area = TextArea()
        area.set_value("ab\ncd")
        area.set_cursor(1)
        self.assertEqual(textarea_cursor_rune_position(area), 4)


# ---------------------------------------------------------------------------
# copy mode 纯函数
# ---------------------------------------------------------------------------

class TestCopyHelpers(unittest.TestCase):
    def test_clamp_and_plural(self) -> None:
        self.assertEqual(clamp_int(5, 0, 3), 3)
        self.assertEqual(clamp_int(5, 4, 3), 4)
        self.assertEqual(plural(1, "line"), "1 line")
        self.assertEqual(plural(2, "line"), "2 lines")

    def test_region_names(self) -> None:
        self.assertEqual(region_name(REGION_CHAT), "chat")
        self.assertEqual(region_name(REGION_NOTICE), "notices")
        self.assertEqual(region_name(REGION_NONE), "none")

    def test_split_lines(self) -> None:
        self.assertEqual(split_lines("a\nb\n\n"), ["a", "b"])
        self.assertEqual(split_lines(""), [])

    def test_word_motions(self) -> None:
        lines = ["hello world", "  foo_bar"]
        cursor, ok = next_word_start(lines, CopyCursor(line=0, col=0))
        self.assertTrue(ok)
        self.assertEqual((cursor.line, cursor.col), (0, 6))
        cursor, ok = next_word_end(lines, CopyCursor(line=0, col=0))
        self.assertTrue(ok)
        self.assertEqual((cursor.line, cursor.col), (0, 4))
        cursor, ok = previous_word_start(lines, CopyCursor(line=1, col=5))
        self.assertTrue(ok)
        self.assertEqual((cursor.line, cursor.col), (1, 2))

    def test_search_match_ranges(self) -> None:
        ranges = search_match_ranges("abcABC", "abc")
        self.assertEqual([(r.start, r.end) for r in ranges], [(0, 2), (3, 5)])
        self.assertEqual(search_match_ranges("abc", ""), [])

    def test_selection_cols_for_line(self) -> None:
        # 中间行整行选中（Go 用 1 << 30 表示"行尾"）。
        self.assertEqual(
            selection_cols_for_line(1, CopyCursor(line=0, col=2), CopyCursor(line=2, col=1)),
            (0, 1 << 30),
        )
        self.assertEqual(
            selection_cols_for_line(0, CopyCursor(line=0, col=2), CopyCursor(line=2, col=1)),
            (2, 1 << 30),
        )
        self.assertEqual(
            selection_cols_for_line(2, CopyCursor(line=0, col=2), CopyCursor(line=2, col=1)),
            (0, 1),
        )
        self.assertEqual(
            selection_cols_for_line(0, CopyCursor(line=0, col=1), CopyCursor(line=0, col=4)),
            (1, 4),
        )

    def test_col_from_display_width(self) -> None:
        self.assertEqual(col_from_display_width("abc", 0), 0)
        self.assertEqual(col_from_display_width("abc", 1), 1)
        self.assertEqual(col_from_display_width("abc", 99), 2)


# ---------------------------------------------------------------------------
# 模型：内容与通知
# ---------------------------------------------------------------------------

class TestTuiModelContent(unittest.TestCase):
    def test_assistant_and_user_content(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.append_user_content("hi")
        model.append_assistant_content("hello")
        model.append_assistant_content(" there")
        content = plain(model.content)
        self.assertIn("user: hi", content)
        self.assertIn("assistant: hello there", content)
        self.assertTrue(model.assistant_open)

    def test_replace_and_finish_assistant(self) -> None:
        model = make_model()
        model.append_assistant_content("partial")
        model.replace_assistant_content("final")
        self.assertIn("assistant: final", plain(model.content))
        model.finish_assistant_content()
        self.assertFalse(model.assistant_open)
        # finish 之后再追加会另起一行。
        model.append_assistant_content("second")
        self.assertIn("assistant: final", plain(model.content))
        self.assertIn("assistant: second", plain(model.content))

    def test_replace_without_open_appends(self) -> None:
        model = make_model()
        model.replace_assistant_content("only")
        self.assertIn("assistant: only", plain(model.content))

    def test_reasoning_markers_are_stripped(self) -> None:
        model = make_model()
        model.append_reasoning_content("[thinking] step one")
        model.append_reasoning_content("[/thinking]\n\nstep two")
        self.assertIn("thinking: step one", plain(model.content))
        self.assertNotIn("[thinking]", plain(model.content))
        self.assertNotIn("[/thinking]", plain(model.content))

    def test_reasoning_then_assistant_closes_block(self) -> None:
        model = make_model()
        model.append_reasoning_content("hmm")
        model.append_assistant_content("answer")
        self.assertIn("\n\nassistant: answer", plain(model.content))
        self.assertFalse(model.reasoning_open)

    def test_empty_content_renders_hint(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        self.assertIn("还没有消息", plain(model.view()))

    def test_notices_go_to_panel_when_wide(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=40))
        model.append_notice("first")
        self.assertEqual([notice.text for notice in model.notices], ["first"])
        self.assertNotIn("first", plain(model.content))
        self.assertIn("first", plain(model.notice_viewport.content()))

    def test_notices_are_inlined_when_narrow(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.append_notice_level("boom", logging.ERROR)
        self.assertEqual(model.notices, [])
        self.assertIn("[notice] boom", plain(model.content))
        self.assertEqual(model.inline_notice_levels, [logging.ERROR])

    def test_notices_are_capped_at_fifty(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=40))
        for index in range(60):
            model.append_notice(f"n{index}")
        self.assertEqual(len(model.notices), 50)
        self.assertEqual(model.notices[0].text, "n10")

    def test_empty_notice_text_is_ignored(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=40))
        model.append_notice("   ")
        self.assertEqual(model.notices, [])

    def test_notice_panel_empty_hint(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=40))
        model.refresh_notices()
        self.assertIn("暂无通知", plain(model.notice_viewport.content()))

    def test_status_line(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=40))
        model.update(
            TuiStatusMsg(
                Snapshot(
                    session_id="s",
                    phase="llm",
                    provider="openai",
                    model="gpt",
                    stage_started_at=datetime.now(timezone.utc),
                )
            )
        )
        self.assertIn("openai/gpt", plain(model.view()))

    def test_status_tick_updates_clock(self) -> None:
        model = make_model()
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        actions = model.update(TuiStatusTickMsg(now=moment))
        self.assertEqual(model.status_now, moment)
        self.assertEqual([action.kind for action in actions], ["tick_status"])


# ---------------------------------------------------------------------------
# 模型：布局
# ---------------------------------------------------------------------------

class TestTuiModelLayout(unittest.TestCase):
    def test_narrow_layout_hides_notice_panel(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=99, height=30))
        chat, notice = model.layout_widths()
        self.assertEqual(notice, 0)
        self.assertEqual(chat, 99)
        self.assertFalse(model.layout_notice_visible())

    def test_wide_layout_splits(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=30))
        chat, notice = model.layout_widths()
        self.assertEqual(notice, NOTICE_PANEL_WIDTH)
        self.assertEqual(chat, 120 - NOTICE_PANEL_WIDTH - 2)

    def test_notice_width_is_clamped(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=100, height=30))
        model.notice_width = 5
        _, notice = model.layout_widths()
        self.assertEqual(notice, 20)
        model.notice_width = 999
        _, notice = model.layout_widths()
        self.assertEqual(notice, 100 - 2 - 40)

    def test_shortcut_text_depends_on_layout(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        self.assertEqual(model.input_shortcut_text(), "Alt+h copy · Esc clear · Ctrl+C exit")
        model.update(WindowSizeMsg(width=120, height=24))
        self.assertIn("Alt+h/l", model.input_shortcut_text())

    def test_header_mentions_chat_when_split(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=24))
        self.assertIn("chat", plain(model.header_view()))
        model.update(WindowSizeMsg(width=60, height=24))
        self.assertNotIn("chat", plain(model.header_view()))


# ---------------------------------------------------------------------------
# 模型：输入与提交
# ---------------------------------------------------------------------------

class TestTuiModelInput(unittest.TestCase):
    def test_typing_and_submit(self) -> None:
        model = make_model(handler=RecordingHandler())
        model.update(WindowSizeMsg(width=80, height=24))
        for char in "hello":
            model.update(KeyMsg.runes_key(char))
        actions = model.update(KeyMsg(type=KEY_ENTER))
        self.assertEqual([action.kind for action in actions], [ACTION_DISPATCH])
        self.assertEqual(actions[0].payload, "hello")
        self.assertEqual(model.input.value(), "")
        self.assertIn("user: hello", plain(model.content))
        self.assertEqual(model.history, ["hello"])

    def test_empty_submit_does_nothing(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        self.assertEqual(model.update(KeyMsg(type=KEY_ENTER)), [])
        self.assertEqual(model.history, [])

    def test_exit_command_quits(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        for char in "/exit":
            model.update(KeyMsg.runes_key(char))
        actions = model.update(KeyMsg(type=KEY_ENTER))
        self.assertEqual([action.kind for action in actions], [ACTION_QUIT])

    def test_escape_clears_input_then_completion(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.update(KeyMsg.runes_key("x"))
        model.update(KeyMsg(type=KEY_ESC))
        self.assertEqual(model.input.value(), "")

    def test_ctrl_c_quits_only_on_empty_input(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.update(KeyMsg.runes_key("x"))
        self.assertEqual(model.update(KeyMsg(type=KEY_CTRL_C)), [])
        self.assertEqual(model.input.value(), "")
        self.assertEqual(
            [action.kind for action in model.update(KeyMsg(type=KEY_CTRL_C))], [ACTION_QUIT]
        )

    def test_ctrl_v_asks_for_clipboard(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        actions = model.update(KeyMsg(type="ctrl+v"))
        self.assertEqual([action.kind for action in actions], [ACTION_READ_CLIPBOARD])

    def test_clipboard_paste_inserts_text(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.update(TuiClipboardMsg(text="a\r\nb"))
        self.assertEqual(model.input.value(), "a\nb")

    def test_clipboard_error_becomes_notice(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.update(TuiClipboardMsg(err=RuntimeError("no clipboard")))
        self.assertIn("[notice] paste: no clipboard", plain(model.content))

    def test_history_navigation(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.history = ["one", "two"]
        model.hist_pos = 2
        model.update(KeyMsg(type=KEY_UP))
        self.assertEqual(model.input.value(), "two")
        model.update(KeyMsg(type=KEY_UP))
        self.assertEqual(model.input.value(), "one")
        model.update(KeyMsg(type=KEY_DOWN))
        self.assertEqual(model.input.value(), "two")
        model.update(KeyMsg(type=KEY_DOWN))
        self.assertEqual(model.input.value(), "")

    def test_local_file_reference_error_is_reported(self) -> None:
        model = make_model(local_files=new_local_file_resolver("__no_such_dir__"))
        model.update(WindowSizeMsg(width=80, height=24))
        for char in "#../escape.txt":
            model.update(KeyMsg.runes_key(char))
        model.update(KeyMsg(type=KEY_ENTER))
        self.assertIn("local file reference:", plain(model.content))
        self.assertEqual(model.history, [])

    def test_viewport_scroll_keys(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=10))
        model.content = "\n".join(f"line{i}" for i in range(50))
        model.refresh_content()
        bottom = model.viewport.y_offset
        model.update(KeyMsg(type="pgup"))
        self.assertLess(model.viewport.y_offset, bottom)
        model.update(KeyMsg(type="pgdown"))
        self.assertEqual(model.viewport.y_offset, bottom)

    def test_notice_scroll_keys(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=10))
        for index in range(20):
            model.append_notice(f"n{index}")
        offset = model.notice_viewport.y_offset
        model.update(KeyMsg(type="ctrl+u"))
        self.assertLessEqual(model.notice_viewport.y_offset, offset)


# ---------------------------------------------------------------------------
# 模型：补全
# ---------------------------------------------------------------------------

class TestTuiModelCompletion(unittest.IsolatedAsyncioTestCase):
    def test_no_candidates_clears_state(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        actions = model.update(KeyMsg(type=KEY_TAB))
        self.assertEqual([action.kind for action in actions], [ACTION_COMPLETE])
        model.apply_completion_result(1, [])
        self.assertFalse(model.completion_state.visible())

    def test_single_candidate_is_applied_directly(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.apply_completion_result(1, [Item(text="/help")])
        self.assertEqual(model.input.value(), "/help")

    def test_multiple_candidates_enter_popup_and_rotate(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.apply_completion_result(1, [Item(text="a"), Item(text="b"), Item(text="c")])
        self.assertTrue(model.completion_state.visible())
        self.assertEqual(model.completion_state.index, 0)
        self.assertEqual(model.input.value(), "a")
        model.update(KeyMsg(type=KEY_TAB))
        self.assertEqual(model.completion_state.index, 1)
        self.assertEqual(model.input.value(), "b")
        model.update(KeyMsg(type="shift+tab"))
        self.assertEqual(model.completion_state.index, 0)
        # 反向进入时从末项开始。
        model.clear_completion()
        model.apply_completion_result(-1, [Item(text="a"), Item(text="b")])
        self.assertEqual(model.completion_state.index, 1)
        self.assertEqual(model.input.value(), "b")
        self.assertIn("2/2", plain(model.completion_view()))

    def test_selection_keys_drive_completion(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.apply_completion_result(1, [Item(text="a"), Item(text="b")])
        model.update(KeyMsg(type=KEY_DOWN))
        self.assertEqual(model.completion_state.index, 1)
        model.update(KeyMsg(type=KEY_UP))
        self.assertEqual(model.completion_state.index, 0)

    def test_escape_closes_popup_before_clearing_input(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.apply_completion_result(1, [Item(text="a"), Item(text="b")])
        model.update(KeyMsg(type=KEY_ESC))
        self.assertFalse(model.completion_state.visible())
        self.assertEqual(model.input.value(), "a")

    def test_replace_range_is_honoured(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=80, height=24))
        model.input.set_value("#ma tail")
        model.apply_completion_result(
            1, [Item(text="#main.py", replace_start=0, replace_end=3)]
        )
        self.assertEqual(model.input.value(), "#main.py tail")

    async def test_resolve_completion_uses_service(self) -> None:
        completer = FakeCompleter([Item(text="/help")])
        model = make_model(completion=completer)
        items = await model.resolve_completion(CompleteRequest(delta=1, text="/he", cursor=3))
        self.assertEqual([item.text for item in items], ["/help"])
        self.assertEqual(completer.calls[0].cursor, 3)

    async def test_resolve_completion_legacy_handler(self) -> None:
        class LegacyHandler:
            def complete(self, value: str) -> list[str]:
                return ["/legacy"]

        model = make_model(handler=LegacyHandler())
        items = await model.resolve_completion(CompleteRequest(delta=1, text="/l", cursor=2))
        self.assertEqual([item.text for item in items], ["/legacy"])

    async def test_resolve_completion_without_sources(self) -> None:
        model = make_model()
        self.assertEqual(
            await model.resolve_completion(CompleteRequest(delta=1, text="x", cursor=1)), []
        )


# ---------------------------------------------------------------------------
# 模型：copy mode 与鼠标
# ---------------------------------------------------------------------------

class TestTuiModelCopyMode(unittest.TestCase):
    def setUp(self) -> None:
        self.model = make_model()
        self.model.update(WindowSizeMsg(width=80, height=12))
        self.model.content = "user: alpha beta\nassistant: gamma\nline four"
        self.model.refresh_content()

    def enter(self, region: str = REGION_CHAT) -> None:
        self.model.enter_copy_mode(region)

    def test_enter_and_exit(self) -> None:
        self.enter()
        self.assertTrue(self.model.copy_state.active())
        self.assertEqual(self.model.copy_state.region, REGION_CHAT)
        self.assertIn("COPY", plain(self.model.status_view()))
        self.model.update(KeyMsg.runes_key("i"))
        self.assertFalse(self.model.copy_state.active())

    def test_enter_with_nothing_to_copy(self) -> None:
        model = make_model()
        model.enter_copy_mode(REGION_CHAT)
        self.assertFalse(model.copy_state.active())
        self.assertEqual(model.copy_state.status, "nothing to copy")

    def test_movement_and_selection(self) -> None:
        self.enter()
        self.model.update(KeyMsg.runes_key("k"))
        self.assertEqual(self.model.copy_state.cursor.line, 1)
        self.model.update(KeyMsg.runes_key("v"))
        self.assertEqual(self.model.copy_state.mode, MODE_COPY_VISUAL_CHAR)
        # 同行的字符选区。
        self.model.copy_state.anchor = CopyCursor(line=0, col=0)
        self.model.copy_state.cursor = CopyCursor(line=0, col=3)
        self.assertEqual(self.model.selected_copy_text(), "user")
        # 跨行选区：首行从锚点切到行尾，末行切到光标处。
        self.model.copy_state.anchor = CopyCursor(line=0, col=4)
        self.model.copy_state.cursor = CopyCursor(line=1, col=0)
        self.assertEqual(self.model.selected_copy_text(), ": alpha beta\na")
        self.model.update(KeyMsg(type=KEY_ESC))
        self.assertEqual(self.model.copy_state.mode, MODE_COPY_NORMAL)

    def test_visual_line_selection_and_yank(self) -> None:
        self.enter()
        self.model.copy_state.cursor = CopyCursor(line=1, col=0)
        self.model.update(KeyMsg.runes_key("V"))
        self.assertEqual(self.model.copy_state.mode, MODE_COPY_VISUAL_LINE)
        self.model.update(KeyMsg.runes_key("y"))
        self.assertEqual(
            self.model.clipboard.written, ["assistant: gamma"]
        )
        self.assertIn("copied 1 line", self.model.copy_state.status)

    def test_yank_failure_is_reported(self) -> None:
        model = make_model(clipboard=FakeClipboard(error=RuntimeError("denied")))
        model.update(WindowSizeMsg(width=80, height=12))
        model.content = "hello"
        model.refresh_content()
        model.enter_copy_mode(REGION_CHAT)
        model.update(KeyMsg.runes_key("y"))
        self.assertIn("copy failed: denied", model.copy_state.status)

    def test_yank_with_empty_selection(self) -> None:
        self.enter()
        self.model.copy_state.cursor = CopyCursor(line=999, col=0)
        self.model.copy_state.status = ""
        self.model.selected_copy_text = lambda: ""  # type: ignore[method-assign]
        self.model.yank_copy_selection()
        self.assertEqual(self.model.copy_state.status, "nothing copied")

    def test_word_motion_keys(self) -> None:
        self.enter()
        self.model.copy_state.cursor = CopyCursor(line=0, col=0)
        self.model.update(KeyMsg.runes_key("w"))
        self.assertEqual(self.model.copy_state.cursor.col, 6)
        self.model.update(KeyMsg.runes_key("e"))
        self.assertEqual(self.model.copy_state.cursor.col, 10)
        self.model.update(KeyMsg.runes_key("b"))
        self.assertEqual(self.model.copy_state.cursor.col, 6)

    def test_page_and_home_end(self) -> None:
        self.enter()
        self.model.update(KeyMsg(type="pgup"))
        self.assertEqual(self.model.copy_state.cursor.line, 0)
        self.model.update(KeyMsg(type="pgdown"))
        self.model.update(KeyMsg.runes_key("G"))
        self.assertEqual(self.model.copy_state.cursor.line, 2)
        self.model.update(KeyMsg.runes_key("g"))
        self.assertEqual(self.model.copy_state.cursor.line, 0)

    def test_search_flow(self) -> None:
        self.enter()
        self.model.update(KeyMsg.runes_key("/"))
        self.assertEqual(self.model.copy_state.mode, "copy_search")
        for char in "gamma":
            self.model.update(KeyMsg.runes_key(char))
        self.assertIn("SEARCH", plain(self.model.status_view()))
        self.model.update(KeyMsg(type=KEY_ENTER))
        self.assertEqual(self.model.copy_state.mode, MODE_COPY_NORMAL)
        self.assertEqual(self.model.copy_state.cursor.line, 1)
        self.assertIn("found: gamma", self.model.copy_state.status)
        # 只有一个命中时会在原地绕回（Go 的取模步进）。
        self.assertTrue(self.model.jump_copy_search(1))
        self.assertEqual(self.model.copy_state.cursor.line, 1)

    def test_search_not_found_and_empty(self) -> None:
        self.enter()
        self.model.begin_copy_search()
        self.model.update(KeyMsg(type=KEY_ENTER))
        self.assertEqual(self.model.copy_state.status, "empty search")
        self.model.copy_state.search_query = "zzz"
        self.assertFalse(self.model.jump_copy_search(1))
        self.model.update(KeyMsg.runes_key("n"))
        self.assertEqual(self.model.copy_state.status, "not found")

    def test_search_backspace(self) -> None:
        self.enter()
        self.model.begin_copy_search()
        self.model.update(KeyMsg.runes_key("ab"))
        self.model.update(KeyMsg(type="backspace"))
        self.assertEqual(self.model.copy_state.search_input, "a")
        self.model.update(KeyMsg(type=KEY_ESC))
        self.assertEqual(self.model.copy_state.mode, MODE_COPY_NORMAL)

    def test_alt_l_toggles_notice_region(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=12))
        model.content = "hello"
        model.refresh_content()
        model.enter_copy_mode(REGION_CHAT)
        # 通知面板为空时 ``enterCopyMode`` 会拒绝进入（Go 里先判 lines 非空）。
        model.update(KeyMsg.special("l", alt=True))
        self.assertEqual(model.copy_state.region, REGION_CHAT)
        self.assertEqual(model.copy_state.status, "nothing to copy")
        model.append_notice("notice one")
        model.update(KeyMsg.special("l", alt=True))
        self.assertEqual(model.copy_state.region, REGION_NOTICE)
        # 窄屏下没有通知面板。
        narrow = make_model()
        narrow.update(WindowSizeMsg(width=60, height=12))
        narrow.content = "hello"
        narrow.refresh_content()
        narrow.enter_copy_mode(REGION_CHAT)
        narrow.update(KeyMsg.special("l", alt=True))
        self.assertEqual(narrow.copy_state.status, "notices hidden")

    def test_copy_lines_for_notice_region(self) -> None:
        model = make_model()
        model.update(WindowSizeMsg(width=120, height=12))
        model.append_notice("notice one")
        model.append_notice("notice two")
        lines = model.notice_copy_lines()
        self.assertIn("notice one", lines)
        self.assertIn("", lines)

    def test_input_is_blurred_in_copy_mode(self) -> None:
        self.enter()
        self.assertFalse(self.model.input.focused)
        self.model.exit_copy_mode()
        self.assertTrue(self.model.input.focused)

    def test_rendered_view_contains_cursor_style(self) -> None:
        self.enter()
        rendered = self.model.viewport.content()
        self.assertIn("\x1b[", rendered)


class TestTuiModelMouse(unittest.TestCase):
    def setUp(self) -> None:
        self.model = make_model()
        self.model.update(WindowSizeMsg(width=120, height=20))
        self.model.content = "\n".join(f"line{i}" for i in range(30))
        self.model.refresh_content()

    def test_mouse_regions(self) -> None:
        chat_width, _ = self.model.layout_widths()
        self.assertEqual(self.model.mouse_region(0, 0), REGION_NONE)
        self.assertEqual(self.model.mouse_region(1, 1), REGION_CHAT)
        self.assertEqual(self.model.mouse_region(chat_width + 1, 1), REGION_NOTICE)
        self.assertEqual(self.model.mouse_region(1, self.model.height - 1), REGION_INPUT)

    def test_wheel_scrolls_chat(self) -> None:
        self.model.viewport.goto_bottom()
        bottom = self.model.viewport.y_offset
        self.model.handle_mouse(
            MouseMsg(x=1, y=5, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_WHEEL_UP)
        )
        self.assertLess(self.model.viewport.y_offset, bottom)
        self.model.handle_mouse(
            MouseMsg(x=1, y=5, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_WHEEL_DOWN)
        )
        self.assertEqual(self.model.viewport.y_offset, bottom)

    def test_wheel_outside_body_still_scrolls_chat(self) -> None:
        # Go 的滚轮分支只区分 regionNotice 与 default，表头（regionNone）也走 chat。
        self.model.viewport.goto_bottom()
        bottom = self.model.viewport.y_offset
        self.model.handle_mouse(
            MouseMsg(x=1, y=0, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_WHEEL_UP)
        )
        self.assertLess(self.model.viewport.y_offset, bottom)

    def test_click_enters_copy_mode(self) -> None:
        self.model.handle_mouse(
            MouseMsg(x=3, y=2, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_LEFT)
        )
        self.assertTrue(self.model.copy_state.active())
        self.assertEqual(self.model.copy_state.region, REGION_CHAT)

    def test_divider_drag_resizes_notice(self) -> None:
        chat_width, notice_width = self.model.layout_widths()
        self.assertTrue(self.model.is_notice_divider(chat_width, 2))
        self.model.handle_mouse(
            MouseMsg(x=chat_width, y=2, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_LEFT)
        )
        self.assertTrue(self.model.resizing_notice)
        self.model.handle_mouse(
            MouseMsg(x=chat_width - 10, y=2, action=MOUSE_ACTION_RELEASE, button=MOUSE_BUTTON_LEFT)
        )
        self.assertFalse(self.model.resizing_notice)
        _, resized = self.model.layout_widths()
        self.assertGreater(resized, notice_width)

    def test_divider_drag_without_press_then_release(self) -> None:
        self.model.handle_mouse(
            MouseMsg(x=5, y=2, action=MOUSE_ACTION_RELEASE, button=MOUSE_BUTTON_LEFT)
        )
        self.assertFalse(self.model.resizing_notice)

    def test_mouse_cursor_after_click(self) -> None:
        self.model.handle_mouse(
            MouseMsg(x=2, y=2, action=MOUSE_ACTION_PRESS, button=MOUSE_BUTTON_LEFT)
        )
        self.assertEqual(self.model.copy_state.cursor.line, self.model.viewport.y_offset + 1)


# ---------------------------------------------------------------------------
# 粘贴突发
# ---------------------------------------------------------------------------

class TestPasteBurst(unittest.TestCase):
    def test_burst_detection(self) -> None:
        burst = TuiPasteBurst()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        burst.observe_text(start, 1)
        self.assertFalse(burst.active)
        burst.observe_text(start + timedelta(milliseconds=5), 2)
        self.assertTrue(burst.active)
        self.assertTrue(burst.should_insert_enter(start + timedelta(milliseconds=6)))

    def test_should_insert_enter_resets_when_idle(self) -> None:
        burst = TuiPasteBurst()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(burst.should_insert_enter(start))
        self.assertIsNone(burst.last_input)
        # reset 之后重新计数（Go 同样会清空累积）。
        burst.observe_text(start, 1)
        self.assertFalse(burst.active)

    def test_idle_timeout_resets(self) -> None:
        burst = TuiPasteBurst()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        burst.observe_text(start, 5)
        self.assertTrue(burst.active)
        later = start + timedelta(seconds=1)
        self.assertFalse(burst.should_insert_enter(later))
        self.assertFalse(burst.active)

    def test_slow_typing_is_not_a_burst(self) -> None:
        burst = TuiPasteBurst()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        burst.observe_text(start, 1)
        burst.observe_text(start + timedelta(milliseconds=200), 1)
        self.assertFalse(burst.active)

    def test_zero_count_is_ignored(self) -> None:
        burst = TuiPasteBurst()
        burst.observe_text(datetime(2026, 1, 1, tzinfo=timezone.utc), 0)
        self.assertIsNone(burst.last_input)

    def test_model_inserts_newline_during_burst(self) -> None:
        clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        model = make_model(input_now=lambda: clock["now"], paste_burst_enabled=True)
        model.update(WindowSizeMsg(width=80, height=24))
        model.update(KeyMsg.runes_key("abc"))
        clock["now"] += timedelta(milliseconds=5)
        actions = model.update(KeyMsg(type=KEY_ENTER))
        self.assertEqual(actions, [])
        self.assertEqual(model.input.value(), "abc\n")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

class TestRunTui(unittest.IsolatedAsyncioTestCase):
    async def test_submit_dispatches_to_handler(self) -> None:
        handler = RecordingHandler()
        output: asyncio.Queue = asyncio.Queue()
        stream = io.StringIO()
        source = CallbackKeySource(
            [
                WindowSizeMsg(width=80, height=24),
                *[KeyMsg.runes_key(char) for char in "hi"],
                KeyMsg(type=KEY_ENTER),
                KeyMsg(type=KEY_CTRL_C),
            ]
        )
        program_handles: list = []
        await run_tui(
            None,
            handler,
            None,
            output,
            program_handles.append,
            "user",
            "assistant",
            key_source=source,
            output_stream=stream,
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        await asyncio.sleep(0.05)
        self.assertEqual(handler.received, ["hi"])
        self.assertIsNone(program_handles[-1])
        self.assertIsNotNone(program_handles[0])
        self.assertIn("user: hi", strip_ansi(stream.getvalue()))

    async def test_exit_command_stops_loop(self) -> None:
        handler = RecordingHandler()
        output: asyncio.Queue = asyncio.Queue()
        source = CallbackKeySource(
            [
                WindowSizeMsg(width=80, height=24),
                *[KeyMsg.runes_key(char) for char in "/exit"],
                KeyMsg(type=KEY_ENTER),
            ]
        )
        await run_tui(
            None,
            handler,
            None,
            output,
            None,
            "user",
            "assistant",
            key_source=source,
            output_stream=io.StringIO(),
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        self.assertEqual(handler.received, [])

    async def test_eof_stops_loop(self) -> None:
        output: asyncio.Queue = asyncio.Queue()
        await run_tui(
            None,
            RecordingHandler(),
            None,
            output,
            None,
            "user",
            "assistant",
            key_source=CallbackKeySource([]),
            output_stream=io.StringIO(),
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )

    async def test_output_queue_messages_reach_view(self) -> None:
        output: asyncio.Queue = asyncio.Queue()
        stream = io.StringIO()
        source = QueueKeySource()
        source.push(WindowSizeMsg(width=80, height=24))
        output.put_nowait(TuiOutputMsg("streamed answer"))

        async def stop_soon() -> None:
            await asyncio.sleep(0.1)
            source.push(KeyMsg(type=KEY_CTRL_C))

        stopper = asyncio.ensure_future(stop_soon())
        await run_tui(
            None,
            RecordingHandler(),
            None,
            output,
            None,
            "user",
            "assistant",
            key_source=source,
            output_stream=stream,
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        await stopper
        self.assertIn("streamed answer", strip_ansi(stream.getvalue()))

    async def test_program_send_reaches_model(self) -> None:
        output: asyncio.Queue = asyncio.Queue()
        stream = io.StringIO()
        source = QueueKeySource()
        source.push(WindowSizeMsg(width=80, height=24))
        holder: list = []

        def set_program(program) -> None:
            holder.append(program)

        async def push() -> None:
            await asyncio.sleep(0.1)
            program = holder[0]
            if program is not None:
                program.send(TuiOutputMsg("pushed"))
                program.send(KeyMsg(type=KEY_CTRL_C))

        pusher = asyncio.ensure_future(push())
        await run_tui(
            None,
            RecordingHandler(),
            None,
            output,
            set_program,
            "user",
            "assistant",
            key_source=source,
            output_stream=stream,
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        await pusher
        self.assertIn("pushed", strip_ansi(stream.getvalue()))

    async def test_ctx_cancel_raises(self) -> None:
        ctx = CancelToken()
        output: asyncio.Queue = asyncio.Queue()
        source = QueueKeySource()
        source.push(WindowSizeMsg(width=80, height=24))

        async def cancel_soon() -> None:
            await asyncio.sleep(0.1)
            ctx.cancel("done")

        canceller = asyncio.ensure_future(cancel_soon())
        with self.assertRaises(AtriXError):
            await run_tui(
                ctx,
                RecordingHandler(),
                None,
                output,
                None,
                "user",
                "assistant",
                key_source=source,
                output_stream=io.StringIO(),
                clipboard=FakeClipboard(),
                paste_burst_enabled=False,
            )
        await canceller

    async def test_handler_error_becomes_notice(self) -> None:
        class FailingHandler:
            async def handle_message(self, ctx, text: str) -> None:
                raise RuntimeError("kaboom")

        output: asyncio.Queue = asyncio.Queue()
        stream = io.StringIO()
        source = QueueKeySource()
        source.push(WindowSizeMsg(width=80, height=24))
        for char in "x":
            source.push(KeyMsg.runes_key(char))
        source.push(KeyMsg(type=KEY_ENTER))

        async def stop_soon() -> None:
            await asyncio.sleep(0.2)
            source.push(KeyMsg(type=KEY_CTRL_C))

        stopper = asyncio.ensure_future(stop_soon())
        await run_tui(
            None,
            FailingHandler(),
            None,
            output,
            None,
            "user",
            "assistant",
            key_source=source,
            output_stream=stream,
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        await stopper
        await asyncio.sleep(0.05)
        # 与 Go 一致：goroutine 里的错误作为通知投进适配器的输出队列，TUI 再读回来渲染。
        self.assertIn("kaboom", strip_ansi(stream.getvalue()))

    async def test_completion_action_runs_service(self) -> None:
        completer = FakeCompleter([Item(text="/help"), Item(text="/health")])
        output: asyncio.Queue = asyncio.Queue()
        stream = io.StringIO()
        source = CallbackKeySource(
            [
                WindowSizeMsg(width=80, height=24),
                KeyMsg.runes_key("/"),
                KeyMsg(type=KEY_TAB),
                KeyMsg(type=KEY_CTRL_C),
            ]
        )
        await run_tui(
            None,
            RecordingHandler(),
            completer,
            output,
            None,
            "user",
            "assistant",
            key_source=source,
            output_stream=stream,
            clipboard=FakeClipboard(),
            paste_burst_enabled=False,
        )
        self.assertEqual(len(completer.calls), 1)
        self.assertIn("/help", strip_ansi(stream.getvalue()))


class TestKeySources(unittest.TestCase):
    def test_callback_source_exhaustion(self) -> None:
        source = CallbackKeySource([KeyMsg.runes_key("a")])

        async def run() -> list:
            out = []
            while True:
                msg = await source.read()
                if msg is None:
                    return out
                out.append(msg)

        self.assertEqual(len(asyncio.run(run())), 1)

    def test_stdin_line_mode(self) -> None:
        source = StdinKeySource(io.StringIO("hello\n"), interactive=False)

        async def run() -> list:
            out = []
            while True:
                msg = await source.read()
                if msg is None:
                    return out
                out.append(msg)

        messages = asyncio.run(run())
        self.assertEqual(messages[0].runes, "hello")
        self.assertEqual(messages[1].type, KEY_ENTER)

    def test_stdin_line_mode_blank_line(self) -> None:
        source = StdinKeySource(io.StringIO("\n"), interactive=False)

        async def run() -> list:
            out = []
            while True:
                msg = await source.read()
                if msg is None:
                    return out
                out.append(msg)

        messages = asyncio.run(run())
        self.assertEqual([msg.type for msg in messages], [KEY_ENTER])

    def test_program_drops_when_queue_full(self) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        program = TuiProgram(queue)
        program.send(TuiOutputMsg("a"))
        program.send(TuiOutputMsg("b"))
        self.assertEqual(queue.qsize(), 1)
        program.close()
        program.send(TuiOutputMsg("c"))
        self.assertEqual(queue.qsize(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
