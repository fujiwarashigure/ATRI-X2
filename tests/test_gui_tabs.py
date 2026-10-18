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
    featureview,
    imagemount,
    journal,
    memberdata,
    modelcatalog,
    searchconfig,
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
        self.assertEqual(len(gui_app._TABS), 9)
        self.assertEqual(len(set(gui_app._TABS)), 9)
        for key in ("model", "character", "platform", "advanced", "members", "knowledge", "logs"):
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
        self.assertEqual(self.tab.notebook.index("end"), 2)
        self.assertEqual(
            [view.name for view in panel.engines], ["tavily", "firecrawl", "keenable", "exa"]
        )
        self.assertEqual(len(panel.tree.get_children("")), 4)
        self.assertEqual(panel.engines[0].protocol, "tavily")

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
            data["web_search"]["engine_order"], ["tavily", "keenable", "firecrawl", "exa"]
        )
        self.assertEqual(panel.engines[1].name, "keenable")

    def test_search_delete_requires_confirmation(self) -> None:
        panel = self.tab.search_panel
        panel._select_row("exa")
        panel._delete_engine()  # askyesno 在测试里被 mock 成 False
        self.assertEqual(len(panel.engines), 4)
        self.assertNotIn("web_search", self.read_toml("services.toml"))

    def test_search_reset_restores_default_chain(self) -> None:
        panel = self.tab.search_panel
        panel._new_engine()
        panel._save_engine()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            panel._reset_engines()
        data = self.read_toml("services.toml")
        self.assertEqual(
            data["web_search"]["engine_order"], ["tavily", "firecrawl", "keenable", "exa"]
        )


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


class FeaturesTabCase(TkCase):
    """功能页：双击/按钮跳转到新页。"""

    def test_view_log_jumps_to_logs_tab(self) -> None:
        goto: list[str] = []
        tab = self.build(FeaturesTab, on_goto=goto.append)
        tab._show_log_window()
        self.assertEqual(goto, ["logs"])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
