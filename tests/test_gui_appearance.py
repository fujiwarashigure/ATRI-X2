"""蓝白主题、侧栏导航与空对话欢迎页的真实 Tk 回归。"""

from __future__ import annotations

import tkinter as tk
import unittest

from atri_x.gui.app import GuiApp
from atri_x.gui.taborder import TABS


class AppearanceTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.app = GuiApp(self.root, tray_enabled=False)
        self.root.update()

    def tearDown(self) -> None:
        if hasattr(self, "root"):
            import gc
            for callback in self.root.tk.call("after", "info"):
                self.root.tk.call("after", "cancel", callback)
            self.root.destroy()
            self.app = None
            gc.collect()

    def test_stale_model_catalog_result_ignored_after_provider_change(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from atri_x.gui.tabs.model import ModelTab
        callback = Mock()
        view = SimpleNamespace(_catalog_key=lambda: "B", base_url_var=SimpleNamespace(get=lambda: "https://b.invalid"),
                               _models_fetched=callback)
        ModelTab._models_result(view, ("A", "https://a.invalid"), ["A-model"], None)
        callback.assert_not_called()
        ModelTab._models_result(view, ("B", "https://b.invalid"), ["B-model"], None)
        callback.assert_called_once_with(["B-model"], None)

    def test_stale_avatar_result_cannot_change_new_member(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from atri_x.gui.tabs.members import MembersTab
        callback = Mock()
        view = SimpleNamespace(_current=SimpleNamespace(key="B"))
        MembersTab._avatar_result(view, "A", callback, b"old-avatar", None)
        callback.assert_not_called()
        MembersTab._avatar_result(view, "B", callback, b"current-avatar", None)
        callback.assert_called_once_with(b"current-avatar", None)

    def test_empty_superadmin_field_is_explicit_revocation(self) -> None:
        from atri_x.gui.tabs.platform import _SuperadminBox
        box = _SuperadminBox(self.root)
        box.load({"qqonebot": ["old-admin"]})
        for variable in box.vars.values():
            variable.set("")
        self.assertTrue(box.values())
        self.assertTrue(all(value == [] for value in box.values().values()))
        self.assertEqual(set(box.values()), set(box.vars))
        box.frame.destroy()

    def test_privacy_controls_preserve_draft_and_require_delete_consent(self) -> None:
        from unittest.mock import Mock, patch
        app = self.app
        app.client = Mock(connected=True)
        app.client.send_input.return_value = True
        app.chat.set_input("private draft")
        self.assertTrue(app.chat._send_control("/forget list 100"))
        self.assertEqual(app.chat.get_input(), "private draft")
        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.assertFalse(app.chat._send_control("/forget abc", destructive=True))
        app.client.send_input.assert_called_once_with("/forget list 100")
        app.chat.sidebar._pending_switch = ("next", "old", 0)
        with patch("tkinter.messagebox.showwarning"):
            self.assertFalse(app.chat._send_control("/confirm"))
        app.client.send_input.assert_called_once_with("/forget list 100")

    def test_privacy_dialogs_construct_without_sending_commands(self) -> None:
        from unittest.mock import Mock
        self.app.client = Mock(connected=True)
        self.app.chat.open_memory_controls()
        self.app.chat.open_tool_controls()
        self.root.update()
        self.app.client.send_input.assert_not_called()

    def test_search_passes_query_but_switch_confirmation_is_unfiltered(self) -> None:
        from unittest.mock import Mock
        app = self.app
        app.client = Mock(connected=True)
        app.chat.sidebar.search_var.set("旅行")
        app.refresh_sessions()
        app.client.request_sessions.assert_called_with(100, "旅行")
        app.chat.sidebar._pending_switch = ("next", "old", 0)
        app.refresh_sessions()
        app.client.request_sessions.assert_called_with(100, "")

    def test_message_copy_edit_and_markdown_preserve_original(self) -> None:
        app = self.app
        text = "# 标题\n**重点**\n```python\nprint('hello')\n```\n> 引用"
        app.transcript.add_user("original question")
        app.transcript.add_chat(text)
        app.render_now()
        chat = app.chat
        self.assertTrue(chat.view.tag_ranges("code"))
        self.assertTrue(chat.view.tag_ranges("heading"))
        self.assertTrue(chat.view.tag_ranges("bold"))
        chat._selected_message = 1
        chat.copy_message()
        self.assertEqual(self.root.clipboard_get(), text)
        chat.copy_code()
        self.assertEqual(self.root.clipboard_get(), "print('hello')")
        chat._selected_message = 0
        chat.edit_message()
        self.assertEqual(chat.get_input(), "original question")
        self.assertEqual(len(app.transcript.lines), 2)

    def test_failed_send_preserves_draft_and_session(self) -> None:
        from unittest.mock import Mock
        client = Mock(connected=True)
        client.send_input.return_value = False
        self.app.client = client
        self.app.chat.set_input("/model example")
        self.app.send_current_input()
        self.assertEqual(self.app.chat.get_input(), "/model example")
        self.assertFalse(any(line.role == "user" for line in self.app.transcript.lines))
        self.app.transcript.add_user("old session")
        self.app.new_session()
        self.assertIn("old session", self.app.transcript.text())

    def test_switch_waits_for_server_and_ignores_stale_history(self) -> None:
        from unittest.mock import Mock
        from atri_x.gui.protocol import SessionItem
        from atri_x.platform.cli.protocol import RemoteMessage
        app = self.app
        app.client = Mock(connected=True)
        app.client.send_input.return_value = True
        app._active_session_id = "old"
        app.transcript.add_user("old history")
        app.open_session("next")
        self.assertEqual(app._active_session_id, "old")
        app.client.request_history.assert_not_called()
        app.chat.sidebar.confirm_switch(app, [SessionItem(id="next", current=True)])
        self.assertEqual(app._active_session_id, "next")
        app.client.request_history.assert_called_once_with("next", 200)
        app._handle_message(RemoteMessage(type="history_result", session_id="old"))
        self.assertIn("old history", app.transcript.text())

    def test_stop_preserves_draft(self) -> None:
        from unittest.mock import Mock
        client = Mock(connected=True)
        client.send_input.return_value = True
        self.app.chat.set_input("unfinished draft")
        self.assertTrue(self.app.chat.stop_request(client))
        client.send_input.assert_called_once_with("/stop")
        self.assertEqual(self.app.chat.get_input(), "unfinished draft")

    def test_stream_render_preserves_earlier_selection(self) -> None:
        app = self.app
        app.transcript.add_user("keep this")
        app.transcript.stream_append("first")
        app.render_now()
        app.chat.view.tag_add("sel", "1.0", "1.4")
        app.transcript.stream_append(" second")
        app.render_now()
        self.assertTrue(app.chat.view.tag_ranges("sel"))
        self.assertIn("first second", app.chat.view.get("1.0", "end"))
        app.transcript.stream_finish()
        app.render_now()
        app.transcript.add_user("next")
        app.render_now()
        self.assertEqual(app.chat.view.get("1.0", "end").count("first second"), 1)
        app.transcript.clear()
        app.render_now()
        self.assertEqual(app.chat.view.get("1.0", "end-1c"), "")

    def test_navigation_preserves_indices_and_lazy_loading(self) -> None:
        app = self.app
        self.assertEqual(len(app.notebook.tabs()), len(TABS))
        self.assertEqual(set(app._shell.buttons), set(range(len(TABS))))
        self.assertEqual(set(app._tab_instances), {0, 10})
        navigation = app._shell.navigation
        navigation.selection_set("page:1")
        self.root.update()
        self.assertEqual(app.notebook.index("current"), 1)
        self.assertEqual(app._shell.title.cget("text"), "模型与工具 / 模型")
        self.assertEqual(len(app._shell.group_buttons), 6)
        self.assertFalse(hasattr(app._shell, "section_choice"))
        app.select_tab(11)
        self.root.update()
        self.assertEqual(app._shell._section_pages, (1, 11, 17))
        app.select_tab(1)
        self.root.update()
        book = app.model_tab.notebook
        self.assertFalse(hasattr(book, "_compact_selector"))
        node = next(key for key, (candidate, tab) in app._shell._book_nodes.items()
                    if candidate is book and book.index(tab) == 1)
        navigation.selection_set(node)
        self.root.update()
        self.assertEqual(book.index("current"), 1)
        self.assertIn(1, app._tab_instances)
        app.select_tab(0)
        self.root.update()
        self.assertEqual(app._shell.title.cget("text"), "对话")

    def test_all_pages_have_shared_background_and_checkbox_tick(self) -> None:
        from tkinter import ttk
        shell = self.app._shell
        self.assertIsNotNone(shell._page_wallpaper)
        self.assertIsNotNone(shell._wallpaper)
        self.assertIn("Aero.Check.indicator", str(ttk.Style(self.root).layout("TCheckbutton")))
        flag = tk.BooleanVar(self.root, value=False)
        box = ttk.Checkbutton(self.root, variable=flag)
        box.invoke()
        self.assertTrue(flag.get())
        box.invoke()
        self.assertFalse(flag.get())
        box.destroy()
        for index in range(len(TABS)):
            self.app.select_tab(index)
            self.root.update()
            self.assertEqual(self.app.notebook.index("current"), index)
            self.assertEqual(shell._active_index, index)
            self.assertIn(index, self.app._tab_instances)
        self.assertEqual(len(shell._check_images), 2)

    def test_input_visible_at_minimum_window_after_heavy_pages(self) -> None:
        self.root.deiconify()
        self.root.geometry("1100x700+0+0")
        self.root.update()
        for index in (0, 11, 1, 0):
            self.app.select_tab(index)
            self.root.update()
        input_box = self.app.chat.input_box
        self.assertTrue(input_box.winfo_ismapped())
        self.assertGreaterEqual(input_box.winfo_height(), 50)
        bottom = input_box.winfo_rooty() + input_box.winfo_height()
        self.assertLessEqual(bottom, self.root.winfo_rooty() + self.root.winfo_height())
        self.assertTrue(self.app.chat.send_button.winfo_ismapped())

    def test_page_backgrounds_and_unchecked_icons_are_not_missing(self) -> None:
        from tkinter import ttk
        for index in range(len(TABS)):
            self.app.select_tab(index)
            self.root.update()
            frame = self.app._tab_instances[index].frame
            self.assertEqual(str(frame.cget("style")), "TFrame")
        shell = self.app._shell
        self.assertTrue(shell._background_frames)
        self.assertEqual(shell._check_images[0].width(), 24)
        # 1.2.1 第二次冻结起指示器换成了 Aero 主题的浅青玻璃边框。
        self.assertEqual(shell._check_images[0].get(1, 1), (143, 214, 238))
        self.assertIn("Aero.Check.indicator", str(ttk.Style(self.root).layout("TRadiobutton")))
        for book in shell._books:
            self.assertEqual(str(book.cget("style")), "Workspace.TNotebook")

    def test_welcome_is_not_part_of_transcript_or_input(self) -> None:
        app = self.app
        app.render_now()
        self.assertTrue(any("你好，我是 ATRI" in app.chat._welcome.itemcget(item, "text")
                            for item in app.chat._welcome.find_all()
                            if app.chat._welcome.type(item) == "text"))
        self.assertIsNotNone(app.chat._wallpaper)
        self.assertEqual(app.chat._welcome.winfo_manager(), "place")
        self.assertEqual(app.chat.get_input(), "")
        self.assertEqual(app.transcript.text(), "")
        app.transcript.add_user("测试消息")
        app.render_now()
        rendered = app.transcript_view.get("1.0", "end")
        self.assertIn("测试消息", rendered)
        self.assertNotIn("你好，我是 ATRI", rendered)
        app.transcript.clear()
        app.render_now()
        self.assertTrue(any("你好，我是 ATRI" in app.chat._welcome.itemcget(item, "text")
                            for item in app.chat._welcome.find_all()
                            if app.chat._welcome.type(item) == "text"))


if __name__ == "__main__":
    unittest.main()
