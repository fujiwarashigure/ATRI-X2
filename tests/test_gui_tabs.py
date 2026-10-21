"""桌面端选项卡冒烟用例：真的建 Tk 控件，把新增/改造的交互各跑一遍。

覆盖的是"点了按钮会不会真的落盘/显示"这一层：

- 「模型」：模型目录下拉、加入列表、备注名、刷新模型列表、备注标签→真模型名的还原；
- 「日志」：结构化通道 + 纯文本通道、级别/关键字筛选、通道切换、导出当前视图；
- 「群友」：从聊天历史发现群友、保存档案、头像缓存落到档案；
- 「知识库」：全局设置保存、state.toml 条目加载/筛选/详情；
- 「进阶」：生图模型目录、备注名、模型名落 services.toml、本地生图挂载（端口映射 /
  SSL 链接）、探测结果记入模型目录、维护任务摘要；
- 二级窗口：自学习设置 / HOOK 插件配置 / 定时维护任务（含非法输入不落盘）；
- 「平台」：密钥输入框默认掩码 + 显示/隐藏，保存进 ``.env``；
- 「关于」：项目简介/仓库地址/推荐语/POWERED BY DAFEIYU 文案常量。

没有 Tk（无显示环境）时整组跳过，不影响其它用例。
"""

from __future__ import annotations

import datetime
import os
import shutil
import sqlite3
import tkinter as tk
import tomllib
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import app as gui_app
from atri_x.gui import (
    about,
    avatars,
    configedit,
    extractconfig,
    featureview,
    imagemount,
    journal,
    memberdata,
    modelcatalog,
    searchconfig,
    toolcatalog,
    widgets,
)
from atri_x.gui.dialogs import HooksDialog, MaintenanceDialog, SelfLearningDialog
from atri_x.gui.tabs.advanced import AdvancedTab
from atri_x.gui.tabs.features import FeaturesTab
from atri_x.gui.tabs.knowledge import KnowledgeTab
from atri_x.gui.tabs.logs import LogsTab, channel_level_tag
from atri_x.gui.tabs.members import MembersTab
from atri_x.gui.tabs.model import ModelTab
from atri_x.gui.tabs.platform import PlatformTab
from atri_x.gui.tabs.toolperm import (
    DISCOVER_TOOL,
    PRESET_COMMON,
    ToolPermTab,
    load_scope_policies,
    load_tool_choices,
    save_scope_policy,
)
from atri_x.media.imagecodec import Image, encode_png
from atri_x.storage.sqlite.chat_history_repository import new_chat_history

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_tabs"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

#: 会被复制进临时配置目录的资产。
ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")

#: 采集用的群消息：(platform, scope, type, message_id, sender, name, text)。
MESSAGES = (
    ("qqonebot", "g1", "group", "m1", "1001", "小明", "在吗"),
    ("qqonebot", "g1", "group", "m2", "1002", "小红", "在的"),
    ("qqonebot", "g1", "group", "m3", "1001", "小明", "晚上好"),
)


def _prepare_config() -> configedit.ConfigPaths:
    """复制发行资产到临时目录，并把两个 SQLite 库也指到临时目录里。"""
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


def _make_chat_db(path: str, rows: tuple = MESSAGES) -> None:
    """建真的 ``chat_messages`` 表并插入消息（复用服务端同一套建表语句）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(target) + suffix).unlink()
        except FileNotFoundError:
            pass
    store = new_chat_history(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        for index, (platform, scope, scope_type, message_id, sender, name, text) in enumerate(rows):
            connection.execute(
                "INSERT INTO chat_messages (id, platform, platform_scope_id, scope_type,"
                " platform_message_id, sender_id, sender_name, text, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"msg-{index}",
                    platform,
                    scope,
                    scope_type,
                    message_id,
                    sender,
                    name,
                    text,
                    f"2026-01-0{index + 1}T12:00:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()


class TkCase(unittest.TestCase):
    """每个用例一套独立的临时配置目录 + 一个隐藏的 Tk 根窗口；没有 Tk 就跳过整组。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()
        # 失败路径会弹 messagebox，模态框在无人值守的测试里会一直等点击。
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

    def build(self, tab_class, **kwargs):
        frame = ttk.Frame(self.root)
        frame.pack()
        return tab_class(frame, self.paths, **kwargs)

    def read_toml(self, name: str) -> dict:
        with open(os.path.join(self.paths.config_dir, name), "rb") as handle:
            return tomllib.load(handle)


class LayoutCase(unittest.TestCase):
    """不建窗口：选项卡清单、「功能」页跳转键、关于文案。"""

    def test_tab_list_and_keys(self) -> None:
        self.assertEqual(len(gui_app._TABS), 11)
        self.assertEqual(len(set(gui_app._TABS)), 11)
        self.assertIn("在线", gui_app._TABS)
        self.assertIn("工具权限", gui_app._TABS)
        self.assertLess(gui_app._TABS.index("平台"), gui_app._TABS.index("在线"))
        for key in (
            "model",
            "character",
            "platform",
            "advanced",
            "members",
            "knowledge",
            "logs",
            "toolperm",
        ):
            self.assertIn(key, gui_app._TAB_KEYS)
            self.assertIn(gui_app._TAB_KEYS[key], gui_app._TABS)

    def test_feature_rows_link_new_tabs(self) -> None:
        paths = _prepare_config()
        try:
            rows = {row.key: row for row in featureview.collect_features(paths)}
            self.assertEqual(rows["members"].tab, "members")
            self.assertEqual(rows["logs"].tab, "logs")
            self.assertEqual(rows["knowledge"].tab, "knowledge")
            self.assertEqual(rows["members"].status, "未建档")
        finally:
            shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_about_text_has_description_repo_recommend_and_powered_by(self) -> None:
        text = about.about_text("9.9.9")
        self.assertIn("9.9.9", text)
        self.assertIn(about.REPO_URL, text)
        self.assertEqual(about.REPO_URL, "https://github.com/fujiwarashigure/ATRI-X")
        self.assertIn(about.RECOMMEND, text)
        self.assertEqual(about.POWERED_BY, "POWERED BY DAFEIYU")
        self.assertIn("POWERED BY DAFEIYU", text)
        self.assertTrue(about.DESCRIPTION.strip())


class ToolCatalogCase(unittest.TestCase):
    """「工具权限」页的工具目录：覆盖全部内置工具，分组/风险/兜底都对。"""

    def test_catalog_covers_registered_tools_exactly(self) -> None:
        names = toolcatalog.catalog_names()
        self.assertEqual(set(names), set(load_tool_choices()))
        self.assertEqual(len(names), len(set(names)))

    def test_every_entry_has_title_summary_category_and_risk(self) -> None:
        self.assertEqual(toolcatalog.CATEGORY_ORDER[-1], toolcatalog.OTHER_CATEGORY)
        self.assertEqual(
            len(toolcatalog.CATEGORY_ORDER), len(set(toolcatalog.CATEGORY_ORDER))
        )
        for meta in toolcatalog.TOOL_CATALOG:
            self.assertTrue(meta.title.strip(), meta.name)
            self.assertTrue(meta.summary.strip(), meta.name)
            self.assertIn(meta.category, toolcatalog.CATEGORY_ORDER, meta.name)
            self.assertIn(meta.risk, toolcatalog.RISK_LABELS, meta.name)

    def test_ordered_names_puts_unknown_tools_last(self) -> None:
        names = toolcatalog.ordered_names(["my_skill", "web_search", "read_file"])
        self.assertEqual(names, ["web_search", "read_file", "my_skill"])
        fallback = toolcatalog.tool_meta("my_skill")
        self.assertEqual(fallback.category, toolcatalog.OTHER_CATEGORY)
        self.assertTrue(fallback.summary.strip())

    def test_catalog_by_category_keeps_menu_order(self) -> None:
        groups = toolcatalog.catalog_by_category(["read_file", "web_search"])
        self.assertEqual([category for category, _ in groups], ["联网", "文件与执行"])

class ModelTabCase(TkCase):
    """模型页：模型目录、备注名、刷新、标签还原。"""

    def setUp(self) -> None:
        super().setUp()
        catalog = modelcatalog.load(self.paths.config_dir)
        catalog.record("providers.deepseek", ["deepseek-chat", "deepseek-reasoner"])
        catalog.set_alias("providers.deepseek", "deepseek-chat", "主力")
        catalog.save(self.paths.config_dir)
        self.tab = self.build(ModelTab)

    def test_provider_list_loaded(self) -> None:
        self.assertIn("deepseek", self.tab._providers)
        self.assertIn("deepseek", self.tab.tree.get_children(""))

    def test_picker_lists_catalog_labels(self) -> None:
        values = list(self.tab.picker_box.cget("values"))
        self.assertIn("主力（deepseek-chat）", values)

    def test_append_selected_model(self) -> None:
        self.tab.models_var.set("")
        self.tab.picker_var.set("主力（deepseek-chat）")
        self.tab._append_selected_model()
        self.assertEqual(self.tab.models_var.get(), "deepseek-chat")

    def test_alias_roundtrip(self) -> None:
        self.tab.alias_model_var.set("deepseek-chat")
        self.tab._alias_model_picked()
        self.assertEqual(self.tab.alias_value_var.get(), "主力")
        self.tab.alias_value_var.set("主力二号")
        self.tab._save_alias()
        catalog = modelcatalog.load(self.paths.config_dir)
        self.assertEqual(catalog.alias_of("providers.deepseek", "deepseek-chat"), "主力二号")

    def test_refresh_slot_list_reloads_from_disk(self) -> None:
        self.tab._refresh_slots_from_disk()
        values = list(self.tab.slot_model_boxes["work"].cget("values"))
        self.assertIn("主力（deepseek-chat）", values)
        self.tab._refresh_slot_providers()
        self.assertIn("deepseek", list(self.tab.slot_provider_boxes[0].cget("values")))

    def test_save_slots_restores_model_name_from_label(self) -> None:
        provider_var, model_var = self.tab.slot_vars["work"]
        provider_var.set("deepseek")
        model_var.set("主力（deepseek-chat）")
        self.tab._save_slots()
        data = self.read_toml("state.toml")
        self.assertEqual(data["mode_models"]["work"]["provider"], "deepseek")
        self.assertEqual(data["mode_models"]["work"]["model"], "deepseek-chat")

    def test_extra_headers_roundtrip(self) -> None:
        self.tab.headers_text.delete("1.0", tk.END)
        self.tab.headers_text.insert("1.0", "x-opencode-session: atri-x-local\n")
        self.tab._save_provider()
        data = self.read_toml("services.toml")
        self.assertEqual(
            data["providers"]["deepseek"]["extra_headers"],
            {"x-opencode-session": "atri-x-local"},
        )
        self.assertEqual(
            self.tab.headers_text.get("1.0", tk.END).strip(),
            "x-opencode-session: atri-x-local",
        )

    def test_extra_headers_removed_when_cleared(self) -> None:
        self.tab.headers_text.insert("1.0", "x-trace: on")
        self.tab._save_provider()
        self.assertIn("extra_headers", self.read_toml("services.toml")["providers"]["deepseek"])
        self.tab.headers_text.delete("1.0", tk.END)
        self.tab._save_provider()
        self.assertNotIn("extra_headers", self.read_toml("services.toml")["providers"]["deepseek"])

    def test_extra_headers_reject_reserved_name(self) -> None:
        self.tab.headers_text.insert("1.0", "Authorization: Bearer forge\n")
        self.tab._save_provider()
        self.assertIn("reserved header", self.tab._status.get())
        provider = self.read_toml("services.toml")["providers"]["deepseek"]
        self.assertNotIn("extra_headers", provider)

    def test_extra_headers_reject_bad_line(self) -> None:
        self.tab.headers_text.insert("1.0", "missing-colon\n")
        self.tab._save_provider()
        self.assertIn("格式不对", self.tab._status.get())

    def test_secret_entry_toggles_and_writes_dotenv(self) -> None:
        self.assertEqual(self.tab.key_entry.entry.cget("show"), "*")
        self.tab.key_entry.set("sk-secret-123")
        self.tab.key_entry.toggle_visibility()
        self.assertEqual(self.tab.key_entry.entry.cget("show"), "")
        self.tab._save_key_only()
        env_text = Path(self.paths.env_path).read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_API_KEY=sk-secret-123", env_text)
        self.assertEqual(self.tab.key_entry.get(), "")
        self.assertEqual(self.tab.key_entry.entry.cget("show"), "*")

    # -- 搜索服务子页 -------------------------------------------------------
    def test_search_page_exists_with_default_chain(self) -> None:
        panel = self.tab.search_panel
        self.assertEqual(self.tab.notebook.index("end"), 3)
        self.assertEqual(
            [view.name for view in panel.engines],
            ["deepseek", "tavily", "firecrawl", "keenable", "exa"],
        )
        self.assertEqual(len(panel.tree.get_children("")), 5)
        self.assertEqual(panel.engines[0].protocol, "deepseek")

    def test_search_state_column_marks_keyless_and_missing_keys(self) -> None:
        panel = self.tab.search_panel
        rows = {
            name: panel.tree.item(name, "values")[2] for name in panel.tree.get_children("")
        }
        self.assertEqual(rows["firecrawl"], "免注册")
        self.assertEqual(rows["keenable"], "免注册")
        self.assertEqual(rows["tavily"], "缺密钥")

    def test_search_saves_custom_engine(self) -> None:
        panel = self.tab.search_panel
        panel._new_engine()
        panel.name_var.set("my-searx")
        panel.protocol_var.set(searchconfig.PROTOCOL_LABELS["searxng"])
        panel.base_url_var.set("https://searx.example.com")
        panel._save_engine()
        data = self.read_toml("services.toml")
        self.assertEqual(data["web_search"]["engine_order"][-1], "my-searx")
        entry = data["web_search"]["engines"]["my-searx"]
        self.assertEqual(entry["protocol"], "searxng")
        self.assertEqual(entry["base_url"], "https://searx.example.com")
        self.assertIn("my-searx", panel.tree.get_children(""))

    def test_search_writes_key_to_dotenv(self) -> None:
        panel = self.tab.search_panel
        panel._select_row("tavily")
        self.assertIn("TAVILY_API_KEY", panel.key_env_var.get())
        panel.key_entry.set("tvly-123")
        panel._save_key()
        env_text = Path(self.paths.env_path).read_text(encoding="utf-8")
        self.assertIn("TAVILY_API_KEY=tvly-123", env_text)
        self.assertEqual(panel.key_entry.get(), "")

    def test_search_move_changes_attempt_order(self) -> None:
        panel = self.tab.search_panel
        panel._select_row("keenable")
        panel._move_engine(-1)
        data = self.read_toml("services.toml")
        self.assertEqual(
            data["web_search"]["engine_order"],
            ["deepseek", "tavily", "keenable", "firecrawl", "exa"],
        )
        self.assertEqual(panel.engines[2].name, "keenable")

    def test_search_delete_requires_confirmation(self) -> None:
        panel = self.tab.search_panel
        panel._select_row("exa")
        panel._delete_engine()  # askyesno 在测试里被 mock 成 False
        self.assertEqual(len(panel.engines), 5)
        self.assertNotIn("web_search", self.read_toml("services.toml"))

    def test_search_reset_restores_default_chain(self) -> None:
        panel = self.tab.search_panel
        panel._new_engine()
        panel._save_engine()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            panel._reset_engines()
        data = self.read_toml("services.toml")
        self.assertEqual(
            data["web_search"]["engine_order"],
            ["deepseek", "tavily", "firecrawl", "keenable", "exa"],
        )

    # -- 抓取服务子页 -------------------------------------------------------
    def test_extract_page_exists_with_default_chain(self) -> None:
        panel = self.tab.extract_panel
        self.assertEqual([view.name for view in panel.engines], ["jina", "direct"])
        self.assertEqual(len(panel.tree.get_children("")), 2)
        self.assertEqual(panel.engines[0].protocol, "jina")
        rows = {
            name: panel.tree.item(name, "values")[2] for name in panel.tree.get_children("")
        }
        self.assertEqual(rows["jina"], "缺密钥")
        self.assertEqual(rows["direct"], "无需密钥")

    def test_extract_saves_deepseek_channel(self) -> None:
        panel = self.tab.extract_panel
        panel._new_engine()
        panel.name_var.set("deepseek")
        panel.protocol_var.set(extractconfig.PROTOCOL_LABELS["deepseek"])
        panel._save_engine()
        data = self.read_toml("services.toml")
        self.assertEqual(data["web_extract"]["engine_order"][-1], "deepseek")
        entry = data["web_extract"]["engines"]["deepseek"]
        self.assertEqual(entry["protocol"], "deepseek")
        self.assertEqual(entry["api_key_env"], "DEEPSEEK_SEARCH_KEY")
        self.assertIn("deepseek", panel.tree.get_children(""))

    def test_extract_writes_key_to_dotenv(self) -> None:
        panel = self.tab.extract_panel
        panel._select_row("jina")
        self.assertIn("JINA_API_KEY", panel.key_env_var.get())
        panel.key_entry.set("jina-123")
        panel._save_key()
        env_text = Path(self.paths.env_path).read_text(encoding="utf-8")
        self.assertIn("JINA_API_KEY=jina-123", env_text)
        self.assertEqual(panel.key_entry.get(), "")

    def test_extract_reset_restores_default_chain(self) -> None:
        panel = self.tab.extract_panel
        panel._new_engine()
        panel._save_engine()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            panel._reset_engines()
        data = self.read_toml("services.toml")
        self.assertEqual(data["web_extract"]["engine_order"], ["jina", "direct"])


class LogsTabCase(TkCase):
    """日志页：两种通道 + 筛选。"""

    def setUp(self) -> None:
        super().setUp()
        self.today = datetime.datetime.now().strftime("%Y-%m-%d")
        Path(self.paths.log_dir).mkdir(parents=True, exist_ok=True)
        self.log_file = Path(self.paths.log_dir) / f"atri-x-{self.today}.log"
        self.log_file.write_text(
            'time="2026-01-01 12:00:00" level=INFO msg=hello\n'
            'time="2026-01-01 12:00:01" level=ERROR msg=boom key=value\n'
            'time="2026-01-01 12:00:02" level=WARN msg=careful\n',
            encoding="utf-8",
        )
        self.tab = self.build(LogsTab)

    def test_day_detected_and_rendered(self) -> None:
        self.assertEqual(self.tab.day_var.get(), self.today)
        text = self.tab.text.get("1.0", "end-1c")
        self.assertIn("boom", text)
        self.assertIn("hello", text)

    def test_level_filter(self) -> None:
        self.tab.level_var.set("error")
        self.tab.render()
        text = self.tab.text.get("1.0", "end-1c")
        self.assertIn("boom", text)
        self.assertNotIn("hello", text)

    def test_keyword_filter(self) -> None:
        self.tab.keyword_var.set("hello")
        self.tab.render()
        text = self.tab.text.get("1.0", "end-1c")
        self.assertIn("hello", text)
        self.assertNotIn("boom", text)

    def test_switch_channel_to_plain_text(self) -> None:
        self.tab.channel_var.set("后台服务输出（service.out.log）")
        self.tab._channel_changed()
        self.assertEqual(str(self.tab.day_box.cget("state")), "disabled")
        self.assertIn("service.out.log", self.tab.info.cget("text"))

    def test_gui_journal_channel(self) -> None:
        journal.record(self.paths.config_dir, "自检写入一条界面操作")
        self.tab.channel_var.set("界面操作（gui.log）")
        self.tab._channel_changed()
        self.assertIn("自检写入一条界面操作", self.tab.text.get("1.0", "end-1c"))

    def test_export_writes_filtered_view_with_header(self) -> None:
        self.tab.level_var.set("error")
        self.tab.render()
        target = TEMP_ROOT / "export" / "runtime.log"
        self.tab._export_to(str(target))
        text = target.read_text(encoding="utf-8")
        self.assertIn("# ATRI-X 日志导出", text)
        self.assertIn("通道：运行日志", text)
        self.assertIn("级别：error", text)
        self.assertIn("boom", text)
        self.assertNotIn("hello", text)
        self.assertEqual(self.tab._default_export_name(), f"atri-x-runtime-{self.today}.log")

    def test_auto_refresh_toggle_and_level_tags(self) -> None:
        self.tab.auto_var.set(True)
        self.tab._toggle_auto()
        self.assertIsNotNone(self.tab._auto_timer)
        self.tab.auto_var.set(False)
        self.tab._toggle_auto()
        self.assertIsNone(self.tab._auto_timer)
        self.assertEqual(channel_level_tag('level=ERROR msg=x'), "error")
        self.assertEqual(channel_level_tag('level=WARN msg=x'), "warn")
        self.assertEqual(channel_level_tag('level=DEBUG msg=x'), "debug")
        self.assertEqual(channel_level_tag("plain line"), "")


class MembersTabCase(TkCase):
    """群友页：发现群友、保存档案、头像写入档案。"""

    def setUp(self) -> None:
        super().setUp()
        _make_chat_db(self.paths.chat_history_db_path)
        self.tab = self.build(MembersTab)

    def test_discovers_scope_and_members(self) -> None:
        self.assertEqual(len(self.tab._scopes), 1)
        self.assertEqual(len(self.tab.tree.get_children("")), 2)
        self.assertIn("qqonebot", self.tab.source_label.cget("text"))
        self.assertIsNotNone(self.tab._current)

    def test_editor_fills_from_selection(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        self.assertEqual(self.tab.name_var.get(), "小明")
        self.assertIn("用户 id：", self.tab.id_label.cget("text"))

    def test_save_profile_writes_json_and_journal(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        self.tab.alias_var.set("小明明")
        self.tab.tags_var.set("群友, 爱画图")
        self.tab.profile_text.delete("1.0", tk.END)
        self.tab.profile_text.insert("1.0", "性格活泼，喜欢猫。")
        self.tab._save()
        profiles = memberdata.load_profiles(self.paths.config_dir)
        self.assertEqual(len(profiles), 1)
        profile = next(iter(profiles.values()))
        self.assertEqual(profile.alias, "小明明")
        self.assertEqual(profile.tags, ["群友", "爱画图"])
        self.assertEqual(profile.profile, "性格活泼，喜欢猫。")
        log_text = Path(journal.journal_path(self.paths.config_dir)).read_text(encoding="utf-8")
        self.assertIn("保存群友档案", log_text)

    def test_avatar_bytes_cache_and_preview(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        png = encode_png(Image(16, 16, "RGB", bytearray([30, 120, 200] * 256)))
        self.tab._apply_avatar_bytes(png)
        self.assertTrue(self.tab._current.avatar)
        self.assertTrue(
            Path(avatars.avatar_file(self.paths.config_dir, self.tab._current.avatar)).is_file()
        )
        self.assertNotEqual(str(self.tab.avatar_label.cget("image")), "")
        self.tab._save()
        saved = memberdata.load_profiles(self.paths.config_dir)[self.tab._current.key]
        self.assertTrue(saved.avatar)

    def test_remove_avatar(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        png = encode_png(Image(8, 8, "RGB", bytearray([1, 2, 3] * 64)))
        self.tab._apply_avatar_bytes(png)
        relative = self.tab._current.avatar
        self.tab._remove_avatar()
        self.assertEqual(self.tab._current.avatar, "")
        self.assertFalse(Path(avatars.avatar_file(self.paths.config_dir, relative)).exists())


class AdvancedTabCase(TkCase):
    """进阶页：生图密钥可见 + 生图模型目录/备注名。"""

    def setUp(self) -> None:
        super().setUp()
        self.tab = self.build(AdvancedTab)

    def test_image_secret_entry_toggle(self) -> None:
        self.assertEqual(self.tab.image_key_entry.entry.cget("show"), "*")
        self.tab.image_key_entry.set("sk-image")
        self.tab.image_key_entry.toggle_visibility()
        self.assertEqual(self.tab.image_key_entry.entry.cget("show"), "")

    def test_image_model_catalog_and_alias(self) -> None:
        self.tab._image_models_fetched(["img-a", "img-b"], None)
        catalog = modelcatalog.load(self.paths.config_dir)
        self.assertEqual(catalog.models(modelcatalog.IMAGE_KEY), ["img-a", "img-b"])
        self.tab.image_model.set("img-a")
        self.tab.image_alias.set("主图")
        self.tab._save_image_alias()
        catalog = modelcatalog.load(self.paths.config_dir)
        self.assertEqual(catalog.alias_of(modelcatalog.IMAGE_KEY, "img-a"), "主图")
        self.assertIn("主图（img-a）", list(self.tab.image_model_box.cget("values")))
        self.tab._image_model_picked()
        self.assertEqual(self.tab.image_alias.get(), "主图")

    def test_save_writes_raw_model_name(self) -> None:
        self.tab._image_models_fetched(["img-a"], None)
        self.tab.image_model.set("主图（img-a）")
        self.tab._save()
        services = self.read_toml("services.toml")
        self.assertEqual(services["image_generation"]["model"], "img-a")

    # -- 本地生图服务挂载 ---------------------------------------------------
    def test_port_mapping_fills_base_url_and_saves(self) -> None:
        self.tab.image_access.set("local")
        self.tab._mount_changed()
        self.tab.image_mount.set(imagemount.MOUNT_PORT)
        self.tab.image_mount_host.set("127.0.0.1")
        self.tab.image_mount_port.set("8199")
        self.tab._apply_mount()
        self.assertEqual(self.tab.image_base_url.get(), "http://127.0.0.1:8199/v1")
        self.tab._save()
        services = self.read_toml("services.toml")
        self.assertEqual(
            services["image_generation"]["base_url"], "http://127.0.0.1:8199/v1"
        )

    def test_ssl_mount_fills_base_url(self) -> None:
        self.tab.image_access.set("local")
        self.tab._mount_changed()
        self.tab.image_mount.set(imagemount.MOUNT_SSL)
        self.tab._mount_changed()
        self.tab.image_mount_url.set("https://abc-8188.gpuhub.com")
        self.tab._apply_mount()
        self.assertEqual(
            self.tab.image_base_url.get(), "https://abc-8188.gpuhub.com/v1"
        )

    def test_mount_changed_toggles_local_rows(self) -> None:
        self.tab.image_access.set("external")
        self.tab._mount_changed()
        self.assertEqual(str(self.tab._local_frame.winfo_manager()), "")
        self.tab.image_access.set("local")
        self.tab._mount_changed()
        self.assertNotEqual(str(self.tab._local_frame.winfo_manager()), "")
        self.tab.image_mount.set(imagemount.MOUNT_SSL)
        self.tab._mount_changed()
        self.assertEqual(str(self.tab._host_row.winfo_manager()), "")
        self.assertNotEqual(str(self.tab._ssl_row.winfo_manager()), "")

    def test_probe_result_records_catalog(self) -> None:
        result = imagemount.ProbeResult(
            ok=True,
            kind=imagemount.KIND_OPENAI,
            detail="OpenAI 兼容生图服务（1 个模型）",
            models=("img-local",),
            base_url="http://127.0.0.1:8188/v1",
        )
        self.tab._mount_probed(result, None)
        catalog = modelcatalog.load(self.paths.config_dir)
        self.assertIn("img-local", catalog.models(modelcatalog.IMAGE_KEY))
        self.assertIn("img-local", list(self.tab.image_model_box.cget("values")))

    def test_maintenance_summary_counts_enabled(self) -> None:
        self.tab._refresh_maintenance_summary(
            {
                "maintenance": {
                    "log_cleanup": {"enabled": True},
                    "session_cleanup": {"enabled": True},
                    "sandbox_cleanup": {"enabled": False},
                }
            }
        )
        text = self.tab.maintenance_summary.cget("text")
        self.assertIn("2 个任务启用", text)
        self.assertIn("日志清理", text)

    def _fill_window(self, geometry: str) -> None:
        """把选项卡铺满整个测试窗口（TkCase 默认只按自然尺寸摆放）。"""
        self.root.geometry(geometry)
        self.root.deiconify()
        self.tab.frame.master.pack_configure(fill=tk.BOTH, expand=True)
        self.tab.frame.pack(fill=tk.BOTH, expand=True)
        self.root.update()

    def test_lower_boxes_stay_visible_after_local_mount_expands(self) -> None:
        """展开本地生图挂载不能把「记忆 / 维护任务」两块推出可视区。"""
        self._fill_window("1060x740+3000+3000")
        scroller = self.tab.scroller
        self.assertIsNotNone(scroller)
        before = scroller.inner.winfo_reqheight()
        self.tab.image_access.set("local")
        self.tab._mount_changed()
        self.root.update()
        # 两列布局把增高的生图区收在左列，整页仍装得下、不产生滚动条。
        self.assertLessEqual(scroller.inner.winfo_reqheight(), before + 40)
        self.assertLessEqual(scroller.inner.winfo_reqheight(), scroller.canvas.winfo_height())
        self.assertFalse(scroller.scrollbar.winfo_ismapped())
        canvas_bottom = scroller.canvas.winfo_rooty() + scroller.canvas.winfo_height()
        for box in (self.tab._toggle_box, self.tab._maintenance_box):
            self.assertLessEqual(box.winfo_rooty() + box.winfo_height(), canvas_bottom)

    def test_small_window_can_scroll_to_lower_boxes(self) -> None:
        """窗口被压小时靠滚动条兜底，仍然能滚到最下面那块。"""
        self._fill_window("900x520+3000+3000")
        scroller = self.tab.scroller
        self.assertIsNotNone(scroller)
        self.assertTrue(scroller.scrollbar.winfo_ismapped())
        self.assertLess(scroller.yview()[1], 1.0)
        scroller.canvas.yview_moveto(1.0)
        self.root.update()
        canvas_bottom = scroller.canvas.winfo_rooty() + scroller.canvas.winfo_height()
        box = self.tab._maintenance_box
        self.assertLessEqual(box.winfo_rooty() + box.winfo_height(), canvas_bottom)


class KnowledgeTabCase(TkCase):
    """知识库页：全局设置落盘 + state.toml 条目只读展示/筛选。"""

    def setUp(self) -> None:
        super().setUp()
        state = self.paths.state_path
        Path(state).write_text(
            '[session]\ndefault_mode = "work"\n\n'
            '[[group_knowledge."qqonebot:g1"]]\n'
            'id = "k1"\nquestion = "怎么用"\nanswer = "看文档"\n'
            'aliases = ["使用说明"]\n\n'
            '[[group_knowledge."qqonebot:g1"]]\n'
            'id = "k2"\nquestion = "营业时间"\nanswer = "10:00-22:00"\n'
            'match = "contains"\nkeywords = ["几点"]\nenabled = false\n',
            encoding="utf-8",
        )
        self.tab = self.build(KnowledgeTab)

    def test_entries_loaded_and_displayed(self) -> None:
        self.assertEqual(len(self.tab.tree.get_children("")), 2)
        self.assertIn("2 条", self.tab.info_label.cget("text"))
        self.assertIn("1 个群组", self.tab.info_label.cget("text"))
        values = self.tab.tree.item("entry-0", "values")
        self.assertEqual(values[0], "qqonebot:g1")
        self.assertEqual(values[4], "启用")
        self.assertEqual(self.tab.tree.item("entry-1", "values")[4], "停用")

    def test_filter_narrows_entries(self) -> None:
        self.tab.filter_var.set("营业")
        self.tab._refresh_entries()
        self.assertEqual(len(self.tab.tree.get_children("")), 1)
        self.assertIn("（已筛选）", self.tab.info_label.cget("text"))

    def test_selection_shows_detail(self) -> None:
        self.tab.tree.selection_set("entry-0")
        self.tab._show_selected()
        detail = self.tab.detail.get("1.0", "end-1c")
        self.assertIn("看文档", detail)
        self.assertIn("使用说明", detail)

    def test_save_global_settings(self) -> None:
        self.tab.enabled_var.set(False)
        self.tab.limit_vars["max_entries_per_scope"].set("50")
        self.tab.limit_vars["max_aliases"].set("")
        self.tab._save()
        app = self.read_toml("app.toml")
        self.assertFalse(app["group_knowledge"]["enabled"])
        self.assertEqual(app["group_knowledge"]["max_entries_per_scope"], 50)
        self.assertNotIn("max_aliases", app["group_knowledge"])


class SelfLearningDialogCase(TkCase):
    """自学习二级窗口：保存写 [self_learning]。"""

    def test_save_writes_section(self) -> None:
        dialog = SelfLearningDialog(self.root, self.paths)
        try:
            dialog.enabled_var.set(False)
            dialog.value_vars["min_count"].set("5")
            dialog.value_vars["min_users"].set("")
            dialog.value_vars["retention_days"].set("120")
            dialog._save()
            app = self.read_toml("app.toml")
            section = app["self_learning"]
            self.assertFalse(section["enabled"])
            self.assertEqual(section["min_count"], 5)
            self.assertEqual(section["retention_days"], 120)
            self.assertNotIn("min_users", section)
        finally:
            dialog.close()

    def test_invalid_number_is_rejected(self) -> None:
        dialog = SelfLearningDialog(self.root, self.paths)
        try:
            dialog.value_vars["min_count"].set("abc")
            dialog._save()
            self.assertIn("失败", dialog._status.get())
        finally:
            dialog.close()


class HooksDialogCase(TkCase):
    """HOOK 插件窗口：合法 TOML 落盘，非法内容拒绝。"""

    def test_save_valid_toml(self) -> None:
        dialog = HooksDialog(self.root, self.paths)
        try:
            dialog.text.delete("1.0", tk.END)
            dialog.text.insert(
                "1.0",
                '[[rules]]\nname = "demo"\non = "message.received"\nalways = true\n'
                'actions = [{ type = "send", kind = "text", text = "hi" }]\n',
            )
            dialog._save()
            text = Path(self.paths.hooks_path).read_text(encoding="utf-8")
            self.assertIn('name = "demo"', text)
            self.assertIn("规则 1 条", dialog.count_label.cget("text"))
        finally:
            dialog.close()

    def test_invalid_toml_is_not_written(self) -> None:
        dialog = HooksDialog(self.root, self.paths)
        try:
            dialog.text.delete("1.0", tk.END)
            dialog.text.insert("1.0", "[[rules]\nbroken = \n")
            dialog._save()
            self.assertIn("失败", dialog._status.get())
            self.assertFalse(Path(self.paths.hooks_path).exists())
        finally:
            dialog.close()


class ScrollFrameCase(TkCase):
    """滚动容器：内容超高时出现滚动条并且能滚，不超高时收起。"""

    def _build(self, rows: int):
        self.root.geometry("620x320+3000+3000")
        self.root.deiconify()
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)
        scroller = widgets.ScrollFrame(frame, height=100)
        scroller.pack(fill=tk.BOTH, expand=True)
        for index in range(rows):
            ttk.Label(scroller.inner, text=f"第 {index} 行").pack(anchor="w")
        self.root.update()
        return scroller

    def test_scrollbar_appears_and_scrolls(self) -> None:
        scroller = self._build(60)
        self.assertTrue(scroller.scrollbar.winfo_ismapped())
        start = scroller.yview()
        scroller.canvas.yview_moveto(1.0)
        self.root.update()
        self.assertGreater(scroller.yview()[0], start[0])

    def test_scrollbar_hidden_when_content_fits(self) -> None:
        scroller = self._build(1)
        self.assertFalse(scroller.scrollbar.winfo_ismapped())


class PlatformTabCase(TkCase):
    """平台页：三处密钥输入框都默认掩码，保存写 .env。"""

    def test_secret_entries_have_toggle_and_save(self) -> None:
        tab = self.build(PlatformTab)
        for entry in (tab.qqonebot.token_entry, tab.qqofficial.secret_entry, tab.telegram.token_entry):
            self.assertEqual(entry.entry.cget("show"), "*")
            entry.set("secret-value")
            entry.toggle_visibility()
            self.assertEqual(entry.entry.cget("show"), "")
            entry.toggle_visibility()
            self.assertEqual(entry.entry.cget("show"), "*")
        tab.qqonebot.token_env.set("QQONEBOT_TOKEN")
        tab._save()
        env_text = Path(self.paths.env_path).read_text(encoding="utf-8")
        self.assertIn("QQONEBOT_TOKEN=secret-value", env_text)
        self.assertEqual(tab.qqonebot.token_entry.get(), "")


class ToolPermTabCase(TkCase):
    """工具权限页：群白名单读写 + 群聊工具进度开关。"""

    SCOPE = "qqonebot:group:1126294273"

    def setUp(self) -> None:
        super().setUp()
        self.tab = self.build(ToolPermTab)

    def test_save_whitelist_writes_state_and_auto_includes_discover(self) -> None:
        self.tab.scope_var.set(self.SCOPE)
        self.tab._locate_scope()
        self.tab.mode_var.set("whitelist")
        self.tab._set_all(False)
        self.tab._tool_vars["web_search"].set(True)
        self.tab._save()
        policies = load_scope_policies(self.paths.state_path)
        mode, names = policies[self.SCOPE]
        self.assertEqual(mode, "whitelist")
        self.assertIn("web_search", names)
        self.assertIn(DISCOVER_TOOL, names)

    def test_save_none_and_inherit_roundtrip(self) -> None:
        save_scope_policy(self.paths.state_path, self.SCOPE, "none", [])
        self.assertEqual(load_scope_policies(self.paths.state_path)[self.SCOPE][0], "none")
        save_scope_policy(self.paths.state_path, self.SCOPE, "inherit", [])
        self.assertNotIn(self.SCOPE, load_scope_policies(self.paths.state_path))

    def test_inherit_keeps_other_group_policy_fields(self) -> None:
        save_scope_policy(self.paths.state_path, self.SCOPE, "none", [])
        configedit.patch_section(
            self.paths.state_path,
            f'group_policy."{self.SCOPE}"',
            {"wake_keywords": ["atri"]},
        )
        save_scope_policy(self.paths.state_path, self.SCOPE, "inherit", [])
        state = self.read_toml("state.toml")
        self.assertEqual(state["group_policy"][self.SCOPE]["wake_keywords"], ["atri"])
        self.assertIsNone(state["group_policy"][self.SCOPE].get("tool_allowlist"))

    def test_preset_fills_common_tools(self) -> None:
        self.tab.scope_var.set(self.SCOPE)
        self.tab._locate_scope()
        self.tab._apply_preset(PRESET_COMMON)
        self.assertEqual(self.tab.mode_var.get(), "whitelist")
        for name in PRESET_COMMON:
            self.assertTrue(self.tab._tool_vars[name].get(), name)

    def test_reload_lists_existing_scopes(self) -> None:
        save_scope_policy(self.paths.state_path, self.SCOPE, "whitelist", ["web_search"])
        self.tab.reload()
        self.assertEqual(len(self.tab.scope_tree.get_children("")), 1)
        values = self.tab.scope_tree.item(self.SCOPE, "values")
        self.assertEqual(values[0], self.SCOPE)
        self.assertIn("白名单", values[1])
        self.assertTrue(self.tab._tool_vars["web_search"].get())
        self.assertEqual(self.tab.mode_var.get(), "whitelist")

    def test_preview_switch_saved_into_app_toml(self) -> None:
        self.tab.preview_var.set(True)
        self.tab._save_preview_switch()
        app = self.read_toml("app.toml")
        self.assertTrue(
            app["platform"]["qqonebot"]["show_tool_preview_in_group"]
        )

    # -- 纵向滚动工具菜单（激活按钮 + 状态 + 功能解释）-----------------------
    def _open_whitelist(self) -> None:
        self.tab.scope_var.set(self.SCOPE)
        self.tab._locate_scope()
        self.tab.mode_var.set("whitelist")
        self.tab._on_mode_changed()

    def test_menu_renders_every_tool_with_state_and_description(self) -> None:
        self._open_whitelist()
        self.assertEqual(len(self.tab._rows), len(self.tab._tool_vars))
        row = self.tab._rows["web_search"]
        self.assertEqual(row.meta, toolcatalog.tool_meta("web_search"))
        self.assertEqual(row.badge.cget("text"), "未激活")
        self.assertEqual(row.button.cget("text"), "激活")
        self.assertIn("搜索", row.summary.cget("text"))
        self.assertEqual(row.summary.cget("wraplength") > 0, True)
        # 点激活按钮：状态翻转、徽标/按钮跟着变，discover_tool 自动补上。
        row.button.invoke()
        self.assertTrue(self.tab._tool_vars["web_search"].get())
        self.assertTrue(self.tab._tool_vars[DISCOVER_TOOL].get())
        row = self.tab._rows["web_search"]
        self.assertEqual(row.badge.cget("text"), "已激活")
        self.assertEqual(row.button.cget("text"), "停用")
        self.assertIn("有未保存改动", self.tab.count_label.cget("text"))
        # 再点一次（停用）。
        row.button.invoke()
        self.assertFalse(self.tab._tool_vars["web_search"].get())
        self.assertEqual(self.tab._rows["web_search"].badge.cget("text"), "未激活")

    def test_menu_locks_in_inherit_and_none_modes(self) -> None:
        self.tab.scope_var.set(self.SCOPE)
        self.tab._locate_scope()
        self.tab.mode_var.set("inherit")
        self.tab._on_mode_changed()
        row = self.tab._rows["web_search"]
        self.assertEqual(row.badge.cget("text"), "继承全局")
        self.assertEqual(str(row.button.cget("state")), str(tk.DISABLED))
        self.assertIn("全部工具可用", self.tab.count_label.cget("text"))
        # 锁定模式点行不改状态（并给出提示，不弹窗）。
        self.tab._toggle_tool("web_search")
        self.assertFalse(self.tab._tool_vars["web_search"].get())
        self.tab.mode_var.set("none")
        self.tab._on_mode_changed()
        self.assertEqual(self.tab._rows["web_search"].badge.cget("text"), "禁止使用")
        self.assertIn("全部工具已禁止", self.tab.count_label.cget("text"))

    def test_menu_search_and_only_active_filter(self) -> None:
        self._open_whitelist()
        self.tab.search_var.set("记忆")
        self.assertTrue(self.tab._rows)
        for name in self.tab._rows:
            self.assertEqual(toolcatalog.tool_meta(name).category, "记忆", name)
        self.assertIn("当前显示", self.tab.count_label.cget("text"))
        self.tab.search_var.set("没有这个工具xyz")
        self.assertEqual(self.tab._rows, {})
        self.tab._clear_search()
        self.assertEqual(len(self.tab._rows), len(self.tab._tool_vars))
        # 只看已激活：列表 == 当前勾选的工具。
        self.tab._apply_preset(PRESET_COMMON)
        self.tab.only_active_var.set(True)
        self.tab._render_rows()
        active = {
            name for name, variable in self.tab._tool_vars.items() if variable.get()
        }
        self.assertEqual(set(self.tab._rows), active)

    def test_menu_activation_save_roundtrip(self) -> None:
        self._open_whitelist()
        self.tab._rows["web_search"].button.invoke()
        self.tab._save()
        mode, names = load_scope_policies(self.paths.state_path)[self.SCOPE]
        self.assertEqual(mode, "whitelist")
        self.assertEqual(set(names), {"web_search", DISCOVER_TOOL})
        self.assertNotIn("有未保存改动", self.tab.count_label.cget("text"))
        # 保存后重新载入：菜单里的状态与落盘一致。
        self.tab.reload()
        self.assertTrue(self.tab._tool_vars["web_search"].get())
        self.assertTrue(self.tab._tool_vars[DISCOVER_TOOL].get())
        self.assertFalse(self.tab._tool_vars["web_extract"].get())


class FeaturesTabCase(TkCase):
    """功能页：双击/按钮跳转到新页。"""

    def test_view_log_jumps_to_logs_tab(self) -> None:
        goto: list[str] = []
        tab = self.build(FeaturesTab, on_goto=goto.append)
        tab._show_log_window()
        self.assertEqual(goto, ["logs"])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
