"""「内容安全」页的用例：数据层（无 Tk）与选项卡（有 Tk 才跑）。

覆盖三层：

* **策略文件**：默认模板能创建/解码；表单保存走行级补丁、不破坏其它小节；
  非法值被拒绝且不写盘；
* **词表/正则/试扫**：目录清单、条目解析、离线试扫命中与脱敏；
* **复核与审计**：入队→列出→通过/过期；审计只读、坏行跳过、尾部截断；
* **选项卡**：六个子页都能构建、能保存、能刷新（无 Tk 环境整组跳过）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import configedit, safetyconfig, safetyrecords
from atri_x.gui.tabs.safety import SafetyTab
from atri_x.safety.audit import AuditLog
from atri_x.safety.review_queue import ReviewQueue

TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_safety"
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"
ASSET_FILES = ("app.toml", "services.toml", "state.toml")


def _prepare_config() -> configedit.ConfigPaths:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    app_path = config_dir / "app.toml"
    configedit.patch_section(
        str(app_path),
        "storage",
        {
            "sessions_sqlite_path": str(TEMP_ROOT / "data" / "atrix_sessions.db"),
            "chat_history_sqlite_path": str(TEMP_ROOT / "data" / "atrix_chat_history.db"),
        },
    )
    return configedit.ConfigPaths.from_config(str(app_path))


class SafetyDataCase(unittest.TestCase):
    """一个用例一套临时配置目录（数据层，不需要 Tk）。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_create_list_rejects_escape_and_existing_without_overwrite(self) -> None:
        from atri_x.gui.configedit import ConfigEditError
        directory = str(TEMP_ROOT / "lists")
        for name in ("../outside.txt", "..\\outside.txt", "C:\\outside.txt", "/outside.txt", "bad:name.txt"):
            with self.subTest(name=name), self.assertRaises(ConfigEditError):
                safetyconfig.create_list_file(directory, name, "header")
        path = safetyconfig.create_list_file(directory, "safe.txt", "sentinel\n")
        with self.assertRaises(ConfigEditError):
            safetyconfig.create_list_file(directory, "safe.txt", "replacement")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "sentinel\n")

    # -- 策略文件 -----------------------------------------------------------
    def test_default_template_creates_valid_audit_policy(self) -> None:
        path = safetyconfig.create_default_safety_file(self.paths)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(safetyconfig.has_safety_file(self.paths))
        settings, errors = safetyconfig.load_settings(self.paths)
        self.assertEqual(errors, [])
        self.assertTrue(settings.local.enabled)
        self.assertEqual(settings.local.mode, "audit")
        self.assertFalse(settings.remote.enabled)
        self.assertIn("safety/wordlists", settings.local.wordlist_dir)

    def test_second_create_is_refused(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        with self.assertRaises(configedit.ConfigEditError):
            safetyconfig.create_default_safety_file(self.paths)

    def test_save_sections_patches_only_given_keys(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        safetyconfig.save_safety_sections(
            self.paths,
            {
                "local": {"mode": "enforce", "enabled": False},
                "tiers": {"inbound_review": "hold"},
            },
        )
        data = configedit.read_toml(safetyconfig.safety_path(self.paths))
        self.assertEqual(data["local"]["mode"], "enforce")
        self.assertFalse(data["local"]["enabled"])
        self.assertEqual(data["tiers"]["inbound_review"], "hold")
        # 没被本页负责的小节/注释都还在（行级补丁不能重写整份文件）。
        self.assertIn("remote", data)
        self.assertIn("cost", data)
        text = safetyconfig.safety_text(self.paths)
        self.assertIn("# ATRI-X 内容安全策略", text)

    def test_invalid_type_is_rejected_without_writing(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        before = safetyconfig.safety_text(self.paths)
        with self.assertRaises(configedit.ConfigEditError):
            safetyconfig.save_safety_sections(self.paths, {"media": {"images": 123}})
        self.assertEqual(safetyconfig.safety_text(self.paths), before)

    def test_raw_text_save_rejects_broken_toml(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        before = safetyconfig.safety_text(self.paths)
        with self.assertRaises(configedit.ConfigEditError):
            safetyconfig.save_safety_text(self.paths, "[[[ 坏掉的 toml")
        self.assertEqual(safetyconfig.safety_text(self.paths), before)

    def test_template_roundtrip_through_raw_editor(self) -> None:
        safetyconfig.save_safety_text(self.paths, safetyconfig.DEFAULT_TEMPLATE)
        settings, errors = safetyconfig.load_settings(self.paths)
        self.assertEqual(errors, [])
        self.assertEqual(settings.tiers.remote_block, "block")
        self.assertEqual(settings.media.images, "sync")

    # -- 词表 ---------------------------------------------------------------
    def test_wordlist_files_and_terms(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        word_dir = safetyconfig.wordlists_dir(self.paths, settings)
        sensitive = safetyconfig.sensitive_dir(self.paths, settings)
        safetyconfig.write_list_file(
            os.path.join(word_dir, "mytest.txt"),
            "# 注释行\n测试违禁词\n第二个词\n测试违禁词\n",
        )
        safetyconfig.write_list_file(
            os.path.join(sensitive, "工号.regex"),
            "# 注释\n工号 = /EMP\\d{4}/\n",
        )
        files = {item.name: item for item in safetyconfig.list_wordlist_files(self.paths, settings)}
        self.assertIn("mytest.txt", files)
        self.assertEqual(files["mytest.txt"].kind, "wordlist")
        self.assertEqual(files["mytest.txt"].entries, 2)
        self.assertEqual(files["工号.regex"].kind, "regex")
        self.assertEqual(files["工号.regex"].entries, 1)
        terms = safetyconfig.file_terms(files["mytest.txt"].path)
        self.assertEqual(terms, ["测试违禁词", "第二个词"])

    def test_local_scan_hits_user_wordlist(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        word_dir = safetyconfig.wordlists_dir(self.paths, settings)
        safetyconfig.write_list_file(os.path.join(word_dir, "mytest.txt"), "测试违禁词\n")
        result = safetyconfig.local_scan(self.paths, settings, "这是测试违禁词，应该被拦")
        self.assertTrue(result.enabled)
        categories = {hit["category"] for hit in result.hits}
        self.assertIn("mytest", categories)
        self.assertTrue(result.blocked)
        clean = safetyconfig.local_scan(self.paths, settings, "今天天气不错")
        self.assertEqual(clean.hits, ())

    def test_local_scan_redacts_custom_regex(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        sensitive = safetyconfig.sensitive_dir(self.paths, settings)
        safetyconfig.write_list_file(
            os.path.join(sensitive, "工号.regex"), "工号 = /EMP\\d{4}/\n"
        )
        result = safetyconfig.local_scan(self.paths, settings, "我的工号是 EMP1234")
        self.assertIn("工号", {hit["category"] for hit in result.hits})
        self.assertIn("[已脱敏:工号]", result.text)

    # -- 复核 / 审计 --------------------------------------------------------
    def test_review_queue_lifecycle(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        path = safetyconfig.review_db_path(self.paths, settings)
        queue = ReviewQueue(path)
        item_id = queue.add(
            stage="send",
            tier="review",
            labels=["porn"],
            provider="tencent",
            preview="[已过滤] 预览",
            platform="qqonebot",
            scope_id="g1",
        )
        self.assertTrue(item_id)
        items = safetyrecords.list_review_items(self.paths, settings)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, item_id)
        self.assertEqual(items[0].labels, ("porn",))
        self.assertEqual(safetyrecords.review_stats(self.paths, settings)["pending"], 1)
        self.assertTrue(
            safetyrecords.resolve_review_item(
                self.paths, settings, item_id, approved=True, note="没问题"
            )
        )
        stats = safetyrecords.review_stats(self.paths, settings)
        self.assertEqual(stats.get("approved"), 1)
        self.assertEqual(stats.get("pending", 0), 0)
        self.assertEqual(safetyrecords.list_review_items(self.paths, settings), [])
        # 二次处置同一 id 必须失败（已经不是 pending）。
        self.assertFalse(
            safetyrecords.resolve_review_item(self.paths, settings, item_id, approved=False)
        )

    def test_expire_marks_old_pending(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        path = safetyconfig.review_db_path(self.paths, settings)
        queue = ReviewQueue(path)
        queue.add(stage="inbound", tier="review", labels=[])
        # 把创建时间改到很久以前（过期判定是 created_at < cutoff）。
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE review_items SET created_at=?", ("2020-01-01T00:00:00+00:00",)
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            safetyrecords.expire_review_items(self.paths, settings, older_than_seconds=0), 1
        )

    def test_missing_review_db_is_read_only(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        self.assertEqual(safetyrecords.list_review_items(self.paths, settings), [])
        self.assertEqual(safetyrecords.review_stats(self.paths, settings)["total"], 0)
        self.assertFalse(
            safetyrecords.resolve_review_item(self.paths, settings, "nope", approved=True)
        )
        self.assertFalse(os.path.isfile(safetyconfig.review_db_path(self.paths, settings)))

    def test_audit_read_tail_and_skip_bad_lines(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        path = safetyconfig.audit_path(self.paths, settings)
        log = AuditLog(path)
        log.append({"stage": "inbound", "action": "block", "labels": ["porn"], "preview": "a"})
        log.append({"stage": "send", "action": "redact", "labels": ["phone"], "preview": "b"})
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("{ 坏行\n")
        records = safetyrecords.read_audit(self.paths, settings)
        self.assertEqual([item["action"] for item in records], ["block", "redact"])
        tail = safetyrecords.read_audit(self.paths, settings, limit=1)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["action"], "redact")

    def test_overview_reports_files_and_counts(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        word_dir = safetyconfig.wordlists_dir(self.paths, settings)
        safetyconfig.write_list_file(os.path.join(word_dir, "mytest.txt"), "甲\n乙\n")
        AuditLog(safetyconfig.audit_path(self.paths, settings)).append(
            {"stage": "inbound", "action": "block", "ts": "2026-01-01T00:00:00+00:00"}
        )
        data = safetyconfig.overview(self.paths)
        self.assertTrue(data.file_exists)
        self.assertEqual(data.local_mode, "audit")
        self.assertFalse(data.remote_enabled)
        self.assertEqual(data.audit_records, 1)
        self.assertEqual(data.last_audit_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(data.wordlist_terms, 2)

    def test_overview_without_file(self) -> None:
        data = safetyconfig.overview(self.paths)
        self.assertFalse(data.file_exists)
        self.assertTrue(data.local_enabled)  # 全默认
        self.assertFalse(data.remote_enabled)
        self.assertEqual(data.review_pending, 0)


class SafetyTabCase(unittest.TestCase):
    """Tk 侧：六个子页都能构建、保存与刷新；无显示环境整组跳过。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()
        self._dialogs = [
            mock.patch("tkinter.messagebox.showerror", return_value=None),
            mock.patch("tkinter.messagebox.showinfo", return_value=None),
            mock.patch("tkinter.messagebox.showwarning", return_value=None),
            mock.patch("tkinter.messagebox.askyesno", return_value=False),
        ]
        for item in self._dialogs:
            item.start()

    def tearDown(self) -> None:
        for item in self._dialogs:
            item.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def _build(self) -> SafetyTab:
        frame = ttk.Frame(self.root)
        frame.pack()
        tab = SafetyTab(frame, self.paths)
        self.root.update()
        return tab

    @staticmethod
    def _arranged(widget) -> int:
        return len([child for child in widget.winfo_children() if child.winfo_manager()])

    def test_all_sub_pages_build_and_are_arranged(self) -> None:
        tab = self._build()
        self.assertEqual(len(tab.notebook.tabs()), 6)
        for tab_id in tab.notebook.tabs():
            tab.notebook.select(tab_id)
            self.root.update()
            page = tab.notebook.nametowidget(tab_id)
            self.assertGreater(self._arranged(page), 0, tab.notebook.tab(tab_id, "text"))

    def test_overview_create_button_writes_default_file(self) -> None:
        tab = self._build()
        panel = tab._panels["总览"]
        panel._create()
        self.root.update()
        self.assertTrue(safetyconfig.has_safety_file(self.paths))
        self.assertIn("audit", panel._vars["local"].get())

    def test_policy_panel_saves_mode(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        tab = self._build()
        panel = tab._panels["策略"]
        panel.reload()
        panel._vars[("local", "mode")].set("enforce  enforce（拦截）")
        panel._save()
        self.root.update()
        data = configedit.read_toml(safetyconfig.safety_path(self.paths))
        self.assertEqual(data["local"]["mode"], "enforce")

    def test_policy_panel_invalid_number_keeps_file(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        tab = self._build()
        panel = tab._panels["策略"]
        panel.reload()
        panel._vars[("local", "max_scan_chars")].set("abc")
        before = safetyconfig.safety_text(self.paths)
        panel._save()
        self.root.update()
        self.assertEqual(safetyconfig.safety_text(self.paths), before)

    def test_wordlist_panel_lists_and_saves(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        word_dir = safetyconfig.wordlists_dir(self.paths, settings)
        safetyconfig.write_list_file(os.path.join(word_dir, "mytest.txt"), "初始词\n")
        tab = self._build()
        panel = tab._panels["词表"]
        panel.reload()
        self.root.update()
        self.assertEqual(len(panel.tree.get_children()), 1)
        panel._current = panel.tree.get_children()[0]
        panel._load_editor(panel._current)
        panel.editor.delete("1.0", tk.END)
        panel.editor.insert("1.0", "改后的词\n")
        panel._save_file()
        self.root.update()
        self.assertIn("改后的词", safetyconfig.read_text_file(panel._current))

    def test_review_panel_resolves_item(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        queue = ReviewQueue(safetyconfig.review_db_path(self.paths, settings))
        item_id = queue.add(stage="send", tier="review", labels=["porn"])
        tab = self._build()
        panel = tab._panels["复核"]
        panel.reload()
        self.root.update()
        self.assertEqual(len(panel.tree.get_children()), 1)
        panel.tree.selection_set(item_id)
        panel._on_select()
        with mock.patch("atri_x.gui.safetyviews.confirm", return_value=True):
            panel._resolve(True)
        self.root.update()
        self.assertEqual(safetyrecords.review_stats(self.paths, settings).get("approved"), 1)

    def test_audit_panel_shows_records(self) -> None:
        safetyconfig.create_default_safety_file(self.paths)
        settings, _ = safetyconfig.load_settings(self.paths)
        log = AuditLog(safetyconfig.audit_path(self.paths, settings))
        log.append({"stage": "inbound", "action": "block", "labels": ["porn"], "preview": "x"})
        log.append({"stage": "send", "action": "redact", "labels": ["phone"], "preview": "y"})
        tab = self._build()
        panel = tab._panels["审计"]
        panel.reload()
        self.root.update()
        self.assertEqual(len(panel.tree.get_children()), 2)

    def test_raw_panel_saves_and_rejects_broken_toml(self) -> None:
        tab = self._build()
        panel = tab._panels["原始 TOML"]
        panel.text.delete("1.0", tk.END)
        panel.text.insert("1.0", safetyconfig.DEFAULT_TEMPLATE)
        panel._save()
        self.root.update()
        self.assertTrue(safetyconfig.has_safety_file(self.paths))
        before = safetyconfig.safety_text(self.paths)
        panel.text.delete("1.0", tk.END)
        panel.text.insert("1.0", "[[[ 坏")
        panel._save()
        self.root.update()
        self.assertEqual(safetyconfig.safety_text(self.paths), before)


if __name__ == "__main__":  # pragma: no cover - 手动运行
    unittest.main()
