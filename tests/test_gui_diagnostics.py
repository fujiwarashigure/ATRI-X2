"""桌面端「诊断可见性」用例：托盘创建失败要看得见、启动失败要留固定日志。

背景（2026-10-20 本机实测）：把发行包放在带 **Low 强制完整性标签** 的目录里启动时，
进程以 Low 完整性运行——写 ``%APPDATA%\\ATRI-X`` 被拒（WinError 5）、
``Shell_NotifyIconW(NIM_ADD)`` 返回 ACCESS_DENIED。修复前这两种失败分别是
「一个看不懂的弹窗」和「静默没有托盘图标」；这里把两处可见性行为钉住：

* 托盘失败：聊天区通知 + ``<配置目录>/gui/gui.log`` + 一次性弹窗，``--minimized`` 时强制显示窗口；
* 启动失败：写 ``<配置目录>/gui/gui-startup.log``，配置目录不可写时退回 ``%TEMP%/ATRI-X/``。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atri_x import __version__
from atri_x.gui import actions, journal, process
from atri_x.gui import __main__ as gui_main
from atri_x.gui import app as gui_app
from atri_x.gui.protocol import Transcript

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_diagnostics"


class FakeRoot:
    """够 :meth:`GuiApp._start_tray` 用的 Tk 根窗口替身。"""

    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []
        self.deiconified = 0

    def after(self, delay: int, callback: object) -> None:
        self.after_calls.append((delay, callback))

    def deiconify(self) -> None:
        self.deiconified += 1


def make_tray_icon(start_ok: bool, error: str = "") -> type:
    """造一个 ``TrayIcon`` 替身类（构造参数原样忽略）。"""

    class _FakeTrayIcon:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.error = error

        def start(self) -> bool:
            return start_ok

    return _FakeTrayIcon


def make_app(
    *,
    start_ok: bool,
    error: str = "",
    minimized: bool = False,
    config_dir: str = "",
    tray_enabled: bool = True,
) -> gui_app.GuiApp:
    """裸造一个 ``GuiApp``（不走 Tk 构造器），只挂测托盘失败路径需要的属性。"""
    instance = object.__new__(gui_app.GuiApp)
    instance.root = FakeRoot()
    instance.settings = SimpleNamespace(config_dir=config_dir) if config_dir else None
    instance.transcript = Transcript()
    instance.client = None
    instance._tray = None
    instance._tray_enabled = tray_enabled
    instance._tray_error = ""
    instance._tray_warned = False
    instance._minimized = minimized
    instance._dirty = False
    return instance


class GuiDiagnosticsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


class TrayFailureVisibilityTest(GuiDiagnosticsTestCase):
    """托盘失败：可见提示 + 日志 + 一次性弹窗 + minimized 兜底。"""

    def test_failure_is_journalled_and_notified(self) -> None:
        config_dir = str(TEMP_ROOT / "config")
        app = make_app(start_ok=False, error="OSError: Shell_NotifyIconW(NIM_ADD) 失败", config_dir=config_dir)
        with mock.patch.object(gui_app, "TrayIcon", make_tray_icon(False, error="OSError: boom")), \
                mock.patch.object(journal, "record", return_value=True) as record:
            app._start_tray()
        self.assertIsNone(app._tray)
        self.assertEqual(app._tray_error, "OSError: boom")
        record.assert_called_once()
        called_dir, called_message = record.call_args.args
        self.assertEqual(called_dir, config_dir)
        self.assertIn("托盘图标不可用", called_message)
        self.assertIn("OSError: boom", called_message)
        rendered = app.transcript.text()
        self.assertIn("托盘图标不可用", rendered)
        self.assertEqual(len(app.root.after_calls), 1)
        self.assertEqual(app.root.after_calls[0][0], 600)

    def test_warning_dialog_is_shown_once(self) -> None:
        app = make_app(start_ok=False, error="boom")
        with mock.patch.object(gui_app, "TrayIcon", make_tray_icon(False, error="boom")):
            app._start_tray()
        callback = app.root.after_calls[0][1]
        with mock.patch.object(gui_app.messagebox, "showwarning") as warn:
            callback()
            callback()  # 第二次不能再弹
        self.assertEqual(warn.call_count, 1)
        self.assertIn("boom", warn.call_args.args[1])

    def test_minimized_without_tray_restores_window(self) -> None:
        app = make_app(start_ok=False, error="boom", minimized=True)
        with mock.patch.object(gui_app, "TrayIcon", make_tray_icon(False, error="boom")):
            app._start_tray()
        self.assertFalse(app._minimized)
        self.assertEqual(app.root.deiconified, 1)

    def test_success_has_no_warning(self) -> None:
        app = make_app(start_ok=True)
        with mock.patch.object(gui_app, "TrayIcon", make_tray_icon(True)), \
                mock.patch.object(journal, "record") as record:
            app._start_tray()
        self.assertIsNotNone(app._tray)
        record.assert_not_called()
        self.assertEqual(app.root.after_calls, [])
        self.assertNotIn("托盘图标不可用", app.transcript.text())

    def test_disabled_tray_does_nothing(self) -> None:
        app = make_app(start_ok=False, tray_enabled=False)
        with mock.patch.object(gui_app, "TrayIcon") as factory:
            app._start_tray()
        factory.assert_not_called()
        self.assertEqual(app.root.after_calls, [])


class TrayHelpTextTest(GuiDiagnosticsTestCase):
    """「托盘/后台说明」对话框必须反映真实状态，而不是永远说"正常"。"""

    def _help_text(self, app: SimpleNamespace) -> str:
        with mock.patch.object(actions.messagebox, "showinfo") as info:
            actions.show_tray_help(app)
        return str(info.call_args.args[1])

    def test_reports_ok(self) -> None:
        text = self._help_text(SimpleNamespace(root=None, _tray=object(), _tray_error=""))
        self.assertIn("正常", text)

    def test_reports_unavailable_with_reason(self) -> None:
        text = self._help_text(SimpleNamespace(root=None, _tray=None, _tray_error="Shell_NotifyIconW(NIM_ADD) 失败"))
        self.assertIn("不可用", text)
        self.assertIn("NIM_ADD", text)

    def test_reports_disabled(self) -> None:
        text = self._help_text(SimpleNamespace(root=None, _tray=None, _tray_error=""))
        self.assertIn("未启用", text)


class StartupFailureLogTest(GuiDiagnosticsTestCase):
    """启动失败固定日志：优先配置目录，配置目录写不进时退回 TEMP。"""

    def test_candidates_prefer_config_dir(self) -> None:
        appdata = str(TEMP_ROOT / "appdata")
        env = {"APPDATA": appdata, "AppData": appdata}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(process, "config_dir_hint", return_value=os.path.join(appdata, "ATRI-X")):
            candidates = process.startup_log_candidates()
        self.assertTrue(candidates)
        self.assertEqual(
            candidates[0],
            os.path.join(appdata, "ATRI-X", "gui", process.STARTUP_LOG_NAME),
        )
        self.assertTrue(all(path.endswith(process.STARTUP_LOG_NAME) for path in candidates))

    def test_fallback_when_config_dir_unwritable(self) -> None:
        blocker = TEMP_ROOT / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        unwritable = str(blocker / "gui" / process.STARTUP_LOG_NAME)
        writable = str(TEMP_ROOT / "logs" / process.STARTUP_LOG_NAME)
        path = process.record_startup_failure(
            "ATRI-X 配置有问题",
            "无法创建默认配置：[WinError 5] 拒绝访问。",
            candidates=[unwritable, writable],
        )
        self.assertEqual(path, writable)
        content = Path(writable).read_text(encoding="utf-8")
        self.assertIn("ATRI-X 配置有问题", content)
        self.assertIn("WinError 5", content)
        self.assertIn(__version__, content)
        self.assertIn("frozen=", content)

    def test_returns_empty_when_all_candidates_fail(self) -> None:
        blocker = TEMP_ROOT / "blocked2"
        blocker.write_text("x", encoding="utf-8")
        path = process.record_startup_failure(
            "t", "d", candidates=[str(blocker / "a.log")]
        )
        self.assertEqual(path, "")

    def test_appends_not_truncates(self) -> None:
        target = str(TEMP_ROOT / "logs" / process.STARTUP_LOG_NAME)
        process.record_startup_failure("first", "one", candidates=[target])
        process.record_startup_failure("second", "two", candidates=[target])
        content = Path(target).read_text(encoding="utf-8")
        self.assertIn("first", content)
        self.assertIn("second", content)

    def test_show_message_logs_and_survives_without_tk(self) -> None:
        with mock.patch.object(gui_main, "record_startup_failure", return_value="C:/logs/gui-startup.log") as rec, \
                mock.patch.dict(sys.modules, {"tkinter": None, "tkinter.messagebox": None}), \
                mock.patch.object(gui_main, "_print") as printed:
            gui_main._show_message("ATRI-X 配置有问题", "无法创建默认配置")
        rec.assert_called_once_with("ATRI-X 配置有问题", "无法创建默认配置")
        printed.assert_called_once()
        self.assertIn("gui-startup.log", printed.call_args.args[0])


if __name__ == "__main__":  # pragma: no cover - 单模块运行入口
    unittest.main()
