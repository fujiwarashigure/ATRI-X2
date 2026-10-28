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
from atri_x.gui.tabs.character import CharacterTab
from atri_x.gui.tabs.features import FeaturesTab
from atri_x.gui.tabs.knowledge import KnowledgeTab
from atri_x.gui.tabs.logs import LogsTab, channel_level_tag
from atri_x.gui.tabs.members import MembersTab
from atri_x.gui.tabs.model import ModelTab
from atri_x.gui.tabs.platform import PlatformTab
from atri_x.gui.tabs.toolperm import (
    DISCOVER_TOOL,
    MODE_ORDER,
    PARAMS_TAB_TITLE,
    PRESET_COMMON,
    ToolPermTab,
    load_scope_mode_policies,
    load_tool_choices,
    save_scope_mode_policy,
)
from atri_x.gui.toolsettings import GROUPS, groups_for_tool
from atri_x.gui.toolsettings_base import TRILABELS
from atri_x.gui.toolparams import SettingPanel
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


class LazyTabBuildTest(TkCase):
    """12 个选项卡**按需构建**：启动只建对话 + 功能，重页首次访问才建。

    为什么值得立成测试：Tk 每个控件都是一个 Win32 子窗口，窗口移动成本随子窗口数
    线性上升（实测 2434 个控件时每次移动约 6ms，拖窗口明显发滞；裸 Tk 约 1ms）。
    全量预建 12 页是 2400+ 控件，按需构建后启动只剩 50 个左右。
    """

    def _build_app(self):
        from atri_x.gui import app as gui_app_mod
        from atri_x.gui.service import prepare_settings

        settings = prepare_settings(str(self.paths.config_path))
        return gui_app_mod.GuiApp(
            self.root,
            settings,
            tray_enabled=False,
            client=None,
            service_owned=False,
        )

    @staticmethod
    def _count_widgets(widget) -> int:
        total = 1
        for child in widget.winfo_children():
            total += LazyTabBuildTest._count_widgets(child)
        return total

    def test_startup_builds_only_light_tabs(self) -> None:
        app = self._build_app()
        self.root.update()
        # 启动只建「对话」(0) 与「功能」(9)；其余是占位 Frame。
        self.assertEqual(len(app._tab_instances), 2)
        self.assertEqual(len(app._tab_placeholders), len(gui_app._TABS) - 2)
        # 性能属性：启动控件数应为百级，而不是全量预建的 2400+。
        self.assertLess(self._count_widgets(self.root), 150)
        # 选项卡顺序/标题与清单一致（懒加载不能打乱 notebook）。
        titles = [app.notebook.tab(tab_id, "text").strip() for tab_id in app.notebook.tabs()]
        self.assertEqual(titles, list(gui_app._TABS))

    def test_heavy_tab_built_on_first_visit(self) -> None:
        app = self._build_app()
        self.root.update()
        self.assertNotIn(11, app._tab_instances)  # 「工具权限」1811 个控件，不预建
        app.select_tab(11)
        self.root.update()
        self.assertIn(11, app._tab_instances)
        toolperm = app.toolperm_tab
        # 懒加载页建在占位 Frame 里（占位页挂在 notebook 上），建完占位记录移除。
        self.assertIs(toolperm.frame.master.master, app.notebook)
        self.assertGreater(len(toolperm.frame.winfo_children()), 0)
        # 反复访问不重复构建
        self.assertIs(app.toolperm_tab, toolperm)

    # -- 布局回归 -----------------------------------------------------------
    def test_feature_jump_to_reaction_sub_tab(self) -> None:
        """「功能」页 → 表情图库：跳转不报错，且真的选中那张子页。"""
        app = self._build_app()
        self.root.update()
        app.show_tab_key("character_reactions")
        self.root.update()
        character = app.character_tab
        self.assertEqual(str(character.reaction_page), character.notebook.select())
        self.assertEqual("pack", character.reaction_panel.frame.winfo_manager())

    def test_feature_jump_to_image_to_prompt_params(self) -> None:
        """「功能」页 → 图转提示词：落到「工具参数（全局）」并展开对应面板。

        回归背景：功能行原来指向 ``advanced``，而「进阶」页没有 ``image_to_prompt``
        的字段，双击过去等于"导流到一个不能配置的地方"。
        """
        app = self._build_app()
        self.root.update()
        app.show_tab_key("toolperm_params:image_to_prompt")
        self.root.update()
        toolperm = app.toolperm_tab
        self.assertEqual(
            str(toolperm.params_editor.frame), toolperm.notebook.select()
        )
        panel = toolperm.params_editor.panels["image_to_prompt"]
        self.assertTrue(panel.expanded)

    def test_every_tab_and_sub_tab_page_is_laid_out(self) -> None:
        """所有页（含子选项卡）都必须有已布局的子控件：防止再次出现“整页空白”。

        只查 ``winfo_manager()``：TkCase 的根窗口是 withdraw 的，``winfo_ismapped()``
        恒为 0，不能用来判断可见性；而“有没有被 pack/grid 过”与窗口是否显示无关。
        """
        app = self._build_app()
        self.root.update()
        for index, name in enumerate(gui_app._TABS):
            app.select_tab(index)
            self.root.update()
            tab = app._tab_instances.get(index)
            self.assertIsNotNone(tab, f"选项卡未构建：{name}")
            self._check_page(tab.frame, name)

    def _check_page(self, page, label: str) -> None:
        arranged = [child for child in page.winfo_children() if child.winfo_manager()]
        self.assertTrue(arranged, f"页面没有已布局的子控件（整页空白）：{label}")
        for child in self._descendants(page):
            if isinstance(child, ttk.Notebook):
                for tab_id in child.tabs():
                    child.select(tab_id)
                    self.root.update()
                    sub = child.nametowidget(tab_id)
                    self._check_page(sub, f"{label} → {child.tab(tab_id, 'text').strip()}")

    @staticmethod
    def _descendants(widget):
        out = []
        for child in widget.winfo_children():
            out.append(child)
            out.extend(LazyTabBuildTest._descendants(child))
        return out


class LayoutCase(unittest.TestCase):
    """不建窗口：选项卡清单、「功能」页跳转键、关于文案。"""

    def test_tab_list_and_keys(self) -> None:
        self.assertEqual(len(gui_app._TABS), 12)
        self.assertEqual(len(set(gui_app._TABS)), 12)
        self.assertIn("在线", gui_app._TABS)
        self.assertIn("时间", gui_app._TABS)
        self.assertIn("工具权限", gui_app._TABS)
        self.assertLess(gui_app._TABS.index("平台"), gui_app._TABS.index("在线"))
        self.assertLess(gui_app._TABS.index("进阶"), gui_app._TABS.index("时间"))
        for key in (
            "model",
            "character",
            "platform",
            "advanced",
            "time",
            "members",
            "knowledge",
            "logs",
            "toolperm",
            "toolperm_params",
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

    def test_reasoning_effort_setting_roundtrip(self) -> None:
        """「模型」页新增的推理思考强度：下拉选中 → 落进 services.toml → 重新载入还原。"""
        self.tab.reasoning_var.set("high")
        self.tab._save_provider()
        provider = self.read_toml("services.toml")["providers"]["deepseek"]
        self.assertEqual(provider["reasoning_effort"], "high")
        self.assertIn("services.toml", self.tab._status.get())
        self.tab.reload()
        self.assertEqual(self.tab.reasoning_var.get(), "high")
        # 选「（不设置）」保存 = 删除该键，不留下空串配置。
        self.tab.reasoning_var.set("（不设置）")
        self.tab._save_provider()
        self.assertNotIn(
            "reasoning_effort", self.read_toml("services.toml")["providers"]["deepseek"]
        )

    def test_clearing_optional_model_slot_persists(self) -> None:
        """清空命名模型槽位必须落到 state.toml（旧版会"界面清空、文件留着旧值"）。"""
        provider_var, model_var = self.tab.slot_vars["naming"]
        provider_var.set("deepseek")
        model_var.set("deepseek-chat")
        self.tab._save_slots()
        self.assertIn("naming_model", self.read_toml("state.toml"))
        provider_var.set("")
        model_var.set("")
        self.tab._save_slots()
        self.assertNotIn("naming_model", self.read_toml("state.toml"))
        self.assertIn("已保存并回读校验", self.tab._status.get())

    def test_clearing_required_model_slot_is_refused(self) -> None:
        """工作/聊天槽位不能清空：界面要给出明确错误，且文件保持原样。"""
        before = Path(self.paths.state_path).read_text(encoding="utf-8")
        provider_var, model_var = self.tab.slot_vars["work"]
        provider_var.set("")
        model_var.set("")
        self.tab._save_slots()
        self.assertIn("不能清空", self.tab._status.get())
        self.assertEqual(Path(self.paths.state_path).read_text(encoding="utf-8"), before)

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
        # 默认参与对话：已录档案升级后直接生效。
        self.assertTrue(profile.enabled)
        log_text = Path(journal.journal_path(self.paths.config_dir)).read_text(encoding="utf-8")
        self.assertIn("保存群友档案", log_text)

    def test_enabled_switch_is_saved_and_shown(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        self.tab.enabled_var.set(False)
        self.tab._update_preview()
        self.assertIn("不参与对话", self.tab.preview_text.get("1.0", "end"))
        self.tab._save()
        profile = next(iter(memberdata.load_profiles(self.paths.config_dir).values()))
        self.assertFalse(profile.enabled)
        self.assertEqual(self.tab.tree.set(keys[0], "talk"), "—")

    def test_preview_renders_prompt_for_current_member(self) -> None:
        keys = list(self.tab.tree.get_children(""))
        self.tab._select(keys[0])
        self.tab.alias_var.set("小明明")
        self.tab._update_preview()
        preview = self.tab.preview_text.get("1.0", "end")
        self.assertIn("当前发言人的角色档案", preview)
        self.assertIn("称呼：小明明", preview)
        self.assertIn("不改变你自身的身份", preview)

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

    def test_backend_defaults_to_napcat_for_new_install(self) -> None:
        """段原本不存在 = 新装：下拉预选 NapCat（随包内置），保存落显式字符串。"""
        tab = self.build(PlatformTab)
        self.assertEqual(tab.qqonebot.backend.get(), "NapCat")
        self.assertEqual(tab.qqonebot.selected_backend(), "napcat")
        tab._save()
        app = self.read_toml("app.toml")
        self.assertEqual(app["platform"]["qqonebot"]["protocol_backend"], "napcat")

    def test_existing_section_shows_napcat_and_can_switch(self) -> None:
        """老配置（段存在但无该键）显示生效值 napcat；改成 SnowLuma 要二次确认。"""
        configedit.patch_section(
            str(self.paths.config_path),
            "platform.qqonebot",
            {"enabled": True, "ws_url": "ws://127.0.0.1:3456/"},
        )
        tab = self.build(PlatformTab)
        self.assertEqual(tab.qqonebot.backend.get(), "NapCat")
        self.assertEqual(tab.qqonebot.selected_backend(), "napcat")
        tab.qqonebot.backend.set("SnowLuma")
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            tab._save()
        section = self.read_toml("app.toml")["platform"]["qqonebot"]
        self.assertEqual(section["protocol_backend"], "snowluma")
        # 切协议端不动地址（"不覆盖原 WS 地址"这条硬约束的回归断言）。
        self.assertEqual(section["ws_url"], "ws://127.0.0.1:3456/")
        self.assertTrue(section["enabled"])

    def test_backend_switch_can_be_cancelled(self) -> None:
        configedit.patch_section(
            str(self.paths.config_path),
            "platform.qqonebot",
            {"enabled": True},
        )
        before = Path(self.paths.config_path).read_text(encoding="utf-8")
        tab = self.build(PlatformTab)
        tab.qqonebot.backend.set("SnowLuma")
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            tab._save()
        self.assertEqual(Path(self.paths.config_path).read_text(encoding="utf-8"), before)


class ToolPermTabCase(TkCase):
    """工具权限页：chat / work 分模式白名单读写 + 群聊工具进度开关。"""

    SCOPE = "qqonebot:group:123456789"
    LEGACY = "qqonebot\u00b7group:123456789"

    def setUp(self) -> None:
        super().setUp()
        self.tab = self.build(ToolPermTab)

    # -- 工具 ---------------------------------------------------------------
    def _open(self) -> None:
        self.tab.scope_var.set(self.SCOPE)
        self.tab._locate_scope()

    def _editor(self, mode_key: str):
        return self.tab.editors[mode_key]

    def _save_mode(self, mode_key: str, mode: str, names: list[str]) -> None:
        save_scope_mode_policy(self.paths.state_path, self.SCOPE, mode_key, mode, names)

    def _policies(self):
        return load_scope_mode_policies(self.paths.state_path).get(self.SCOPE, {})

    # -- 分模式保存 ---------------------------------------------------------
    def test_save_chat_and_work_whitelists_separately(self) -> None:
        self._open()
        chat = self._editor("chat")
        work = self._editor("work")
        chat.mode_var.set("whitelist")
        chat.set_all(False)
        chat.tool_vars["web_search"].set(True)
        work.mode_var.set("whitelist")
        work.set_all(False)
        work.tool_vars["read_file"].set(True)
        self.tab._save()

        policies = self._policies()
        self.assertEqual(policies["chat"][0], "whitelist")
        self.assertEqual(set(policies["chat"][1]), {"web_search", DISCOVER_TOOL})
        self.assertEqual(policies["work"][0], "whitelist")
        self.assertEqual(set(policies["work"][1]), {"read_file", DISCOVER_TOOL})
        # 通用（/tools allow 写入的那份）不被界面改动。
        self.assertEqual(policies["generic"][0], "inherit")

    def test_chat_none_does_not_affect_work(self) -> None:
        self._save_mode("work", "whitelist", ["web_search"])
        self._save_mode("chat", "none", [])
        policies = self._policies()
        self.assertEqual(policies["chat"], ("none", []))
        self.assertEqual(policies["work"][0], "whitelist")

    def test_inherit_removes_only_that_mode(self) -> None:
        self._save_mode("work", "whitelist", ["web_search"])
        self._save_mode("chat", "none", [])
        self._save_mode("chat", "inherit", [])
        policies = self._policies()
        self.assertEqual(policies["chat"][0], "inherit")
        self.assertEqual(policies["work"][0], "whitelist")
        state = self.read_toml("state.toml")
        self.assertNotIn(
            "tool_allowlist_chat", state["group_policy"][self.SCOPE]
        )
        self.assertIn("tool_allowlist_work", state["group_policy"][self.SCOPE])

    def test_generic_allowlist_survives_and_is_shown_as_fallback(self) -> None:
        # 群内 /tools allow 写的通用白名单：界面不改它，但要在页面上显示为兜底。
        configedit.patch_section(
            self.paths.state_path,
            f'group_policy."{self.SCOPE}"',
            {"tool_allowlist": ["web_extract"]},
        )
        self.tab.reload()
        self._open()
        text = self.tab.generic_label.cget("text")
        self.assertIn("web_extract", text)
        self._save_mode("chat", "whitelist", ["web_search"])
        state = self.read_toml("state.toml")
        self.assertEqual(
            state["group_policy"][self.SCOPE]["tool_allowlist"], ["web_extract"]
        )

    # -- 结算 / 载入 --------------------------------------------------------
    def test_reload_lists_existing_scopes(self) -> None:
        self._save_mode("chat", "whitelist", ["web_search"])
        self.tab.reload()
        self.assertEqual(len(self.tab.scope_tree.get_children("")), 1)
        values = self.tab.scope_tree.item(self.SCOPE, "values")
        self.assertEqual(values[0], self.SCOPE)
        self.assertIn("chat:白名单", values[1])
        self.assertTrue(self._editor("chat").tool_vars["web_search"].get())
        self.assertEqual(self._editor("chat").mode_var.get(), "whitelist")
        self.assertEqual(self._editor("work").mode_var.get(), "inherit")

    def test_scope_separator_normalized_on_save(self) -> None:
        # 从「群友」页复制来的 `平台 · group:id`（间隔号 + 空格）必须落成半角冒号键。
        self.tab.scope_var.set("qqonebot · group:123456789")
        self.tab._locate_scope()
        self.assertEqual(self.tab._current_scope, self.SCOPE)
        editor = self._editor("work")
        editor.mode_var.set("whitelist")
        editor.set_all(False)
        editor.tool_vars["web_search"].set(True)
        self.tab._save()
        state = self.read_toml("state.toml")
        self.assertIn(self.SCOPE, state["group_policy"])
        self.assertNotIn("qqonebot · group:123456789", state["group_policy"])

    def test_legacy_scope_key_migrated_on_save(self) -> None:
        configedit.patch_section(
            self.paths.state_path,
            f'group_policy."{self.LEGACY}"',
            {"wake_keywords": ["atri"], "tool_allowlist_work": ["web_search"]},
        )
        self._save_mode("work", "whitelist", ["web_extract"])
        state = self.read_toml("state.toml")
        self.assertNotIn(self.LEGACY, state["group_policy"])
        self.assertEqual(state["group_policy"][self.SCOPE]["wake_keywords"], ["atri"])
        names = state["group_policy"][self.SCOPE]["tool_allowlist_work"]
        self.assertIn("web_extract", names)
        self.assertIn(DISCOVER_TOOL, names)

    def test_invalid_scope_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            save_scope_mode_policy(self.paths.state_path, "qqonebot", "chat", "none", [])
        self.assertNotIn("qqonebot", self.read_toml("state.toml").get("group_policy", {}))

    def test_discover_group_scopes_from_sessions_db(self) -> None:
        from atri_x.gui import toolpolicy

        db = self.paths.sessions_db_path
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "CREATE TABLE sessions (platform TEXT, platform_scope_id TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions VALUES ('qqonebot', 'group:42')"
            )
            connection.execute(
                "INSERT INTO sessions VALUES ('qqonebot', 'private:7')"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(["qqonebot:group:42"], toolpolicy.discover_group_scopes(db, ""))

    def test_preview_switch_saved_into_app_toml(self) -> None:
        self.tab.preview_var.set(True)
        self.tab._save_preview_switch()
        app = self.read_toml("app.toml")
        self.assertTrue(
            app["platform"]["qqonebot"]["show_tool_preview_in_group"]
        )

    # -- 工具进度预览子选项卡 ---------------------------------------------
    def test_preview_sub_tab_present(self) -> None:
        titles = [
            self.tab.notebook.tab(index, "text")
            for index in range(len(self.tab.notebook.tabs()))
        ]
        self.assertIn(" 工具进度预览 ", titles)
        self.assertIsNotNone(self.tab.preview_panel)
        self.tab.show_preview_tab()
        self.assertEqual(
            str(self.tab.notebook.select()), str(self.tab.preview_panel.frame)
        )

    def test_preview_panel_saves_texts_and_switch(self) -> None:
        panel = self.tab.preview_panel
        panel.style_var.set("system")
        panel.field_vars["search_text"].set("检索中，请稍候。")
        panel.field_vars["default_text"].set("正在调用 {tool}，稍等。")
        panel.switch_vars["platform.qqonebot"].set(True)
        panel.save()

        app = self.read_toml("app.toml")
        self.assertEqual("system", app["tool_preview"]["style"])
        self.assertEqual("检索中，请稍候。", app["tool_preview"]["search_text"])
        self.assertEqual("正在调用 {tool}，稍等。", app["tool_preview"]["default_text"])
        self.assertTrue(app["platform"]["qqonebot"]["show_tool_preview_in_group"])
        # qqofficial 段不存在时不得被 patch_section 创建出来。
        self.assertNotIn("qqofficial", app["platform"])

        # 回读：界面上看到的就是落盘值（双向联动）。
        panel.style_var.set("persona")
        panel.load()
        self.assertEqual("system", panel.style_var.get())
        self.assertEqual("检索中，请稍候。", panel.field_vars["search_text"].get())
        self.assertTrue(panel.switch_vars["platform.qqonebot"].get())

    def test_preview_panel_writes_qqofficial_only_when_section_exists(self) -> None:
        configedit.patch_section(
            self.paths.config_path, "platform.qqofficial", {"enabled": True}
        )
        panel = self.tab.preview_panel
        panel.switch_vars["platform.qqonebot"].set(True)
        panel.switch_vars["platform.qqofficial"].set(False)
        panel.save()
        app = self.read_toml("app.toml")
        self.assertFalse(
            app["platform"]["qqofficial"]["show_tool_preview_in_group"]
        )
        self.assertTrue(app["platform"]["qqonebot"]["show_tool_preview_in_group"])

    # -- 纵向滚动工具菜单（激活按钮 + 状态 + 功能解释）-----------------------
    def _open_whitelist(self, mode_key: str = "work"):
        self._open()
        editor = self._editor(mode_key)
        editor.mode_var.set("whitelist")
        editor.refresh_rows()
        return editor

    def test_menu_renders_every_tool_with_state_and_description(self) -> None:
        editor = self._open_whitelist()
        self.assertEqual(len(editor._rows), len(editor.tool_vars))
        row = editor._rows["web_search"]
        self.assertEqual(row.meta, toolcatalog.tool_meta("web_search"))
        self.assertEqual(row.badge.cget("text"), "未激活")
        self.assertEqual(row.button.cget("text"), "激活")
        self.assertIn("搜索", row.summary.cget("text"))
        self.assertEqual(row.summary.cget("wraplength") > 0, True)
        # 点激活按钮：状态翻转、徽标/按钮跟着变，discover_tool 自动补上。
        row.button.invoke()
        self.assertTrue(editor.tool_vars["web_search"].get())
        self.assertTrue(editor.tool_vars[DISCOVER_TOOL].get())
        row = editor._rows["web_search"]
        self.assertEqual(row.badge.cget("text"), "已激活")
        self.assertEqual(row.button.cget("text"), "停用")
        self.assertIn("有未保存改动", editor.count_label.cget("text"))
        # 再点一次（停用）。
        row.button.invoke()
        self.assertFalse(editor.tool_vars["web_search"].get())
        self.assertEqual(editor._rows["web_search"].badge.cget("text"), "未激活")

    def test_menu_locks_in_inherit_and_none_modes(self) -> None:
        editor = self._open_whitelist()
        editor.mode_var.set("inherit")
        editor.refresh_rows()
        row = editor._rows["web_search"]
        self.assertEqual(row.badge.cget("text"), "继承全局")
        self.assertEqual(str(row.button.cget("state")), str(tk.DISABLED))
        self.assertIn("跟随通用策略", editor.count_label.cget("text"))
        # 锁定模式点行不改状态（并给出提示，不弹窗）。
        editor._toggle_tool("web_search")
        self.assertFalse(editor.tool_vars["web_search"].get())
        editor.mode_var.set("none")
        editor.refresh_rows()
        self.assertEqual(editor._rows["web_search"].badge.cget("text"), "禁止使用")
        self.assertIn("全部工具已禁止", editor.count_label.cget("text"))

    def test_menu_search_and_only_active_filter(self) -> None:
        editor = self._open_whitelist()
        editor.search_var.set("记忆")
        self.assertTrue(editor._rows)
        for name in editor._rows:
            self.assertEqual(toolcatalog.tool_meta(name).category, "记忆", name)
        self.assertIn("当前显示", editor.count_label.cget("text"))
        editor.search_var.set("没有这个工具xyz")
        self.assertEqual(editor._rows, {})
        editor._clear_search()
        self.assertEqual(len(editor._rows), len(editor.tool_vars))
        # 只看已激活：列表 == 当前勾选的工具。
        editor.apply_preset(PRESET_COMMON)
        editor.only_active_var.set(True)
        editor.render_rows()
        active = {name for name, variable in editor.tool_vars.items() if variable.get()}
        self.assertEqual(set(editor._rows), active)

    def test_menu_activation_save_roundtrip(self) -> None:
        editor = self._open_whitelist("chat")
        editor._rows["web_search"].button.invoke()
        self.tab._save()
        policies = self._policies()
        mode, names = policies["chat"]
        self.assertEqual(mode, "whitelist")
        self.assertEqual(set(names), {"web_search", DISCOVER_TOOL})
        # 另一个模式不受影响，仍是继承通用。
        self.assertEqual(policies["work"][0], "inherit")
        self.assertNotIn("有未保存改动", editor.count_label.cget("text"))
        # 保存后重新载入：菜单里的状态与落盘一致。
        self.tab.reload()
        self._open()
        editor = self._editor("chat")
        self.assertTrue(editor.tool_vars["web_search"].get())
        self.assertTrue(editor.tool_vars[DISCOVER_TOOL].get())
        self.assertFalse(editor.tool_vars["web_extract"].get())
        self.assertEqual(self._editor("work").mode_var.get(), "inherit")


class ToolParamsTabCase(TkCase):
    """工具参数子选项卡：面板渲染 / 只写改动字段 / 校验 / 从工具行跳转。"""

    def setUp(self) -> None:
        super().setUp()
        self.tab = self.build(ToolPermTab)

    # -- 工具 ---------------------------------------------------------------
    def panel(self, key: str):
        return self.tab.params_editor.panels[key]

    def open_whitelist(self, mode_key: str = "work"):
        self.tab.scope_var.set("qqonebot:group:123456789")
        self.tab._locate_scope()
        editor = self.tab.editors[mode_key]
        editor.mode_var.set("whitelist")
        editor.refresh_rows()
        return editor

    def set_tristate(self, panel, key: str, value: str) -> None:
        label = {item: text for item, text in TRILABELS}[value]
        panel._vars[key].set(label)

    def set_choice(self, panel, key: str, value: str) -> None:
        field = next(item for item in panel.group.fields if item.key == key)
        panel._vars[key].set(field.choice_label(value))

    def set_text(self, panel, key: str, value: str) -> None:
        widget = panel._texts[key]
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)

    # -- 结构 ---------------------------------------------------------------
    def test_third_sub_tab_and_panels(self) -> None:
        titles = [
            self.tab.notebook.tab(index, "text")
            for index in range(len(self.tab.notebook.tabs()))
        ]
        self.assertEqual(len(titles), 4)
        self.assertIn("chat 模式", titles[0])
        self.assertIn("work 模式", titles[1])
        self.assertEqual(titles[2], PARAMS_TAB_TITLE)
        self.assertEqual(titles[3], " 工具进度预览 ")
        self.assertEqual(set(self.tab.params_editor.panels), {group.key for group in GROUPS})
        # 全局面板默认展开，其余默认收起（页面不会一进来就长得离谱）。
        self.assertTrue(self.panel("global").expanded)
        self.assertFalse(self.panel("image_prompt").expanded)

    def test_panels_render_a_widget_per_field(self) -> None:
        for key, panel in self.tab.params_editor.panels.items():
            rendered = set(panel._vars) | set(panel._texts)
            expected = {field.key for field in panel.group.fields}
            self.assertEqual(rendered, expected, f"面板 {key} 的控件数量不对")
            self.assertTrue(panel.status_label.cget("text"), f"面板 {key} 没有状态文字")
            self.assertEqual(
                str(panel.toggle_button.cget("text")),
                "▾" if panel.expanded else "▸",
            )

    def test_safety_override_panels_render_after_global_before_categories(self) -> None:
        """内容安全的 chat / work 覆盖面板：紧跟全局面板、各 7 个控件、默认收起。"""
        self.assertEqual(
            list(self.tab.params_editor.panels)[:3],
            ["global", "safety_chat", "safety_work"],
        )
        for key, section in (
            ("safety_chat", "safety.chat"),
            ("safety_work", "safety.work"),
        ):
            panel = self.panel(key)
            self.assertEqual({field.section for field in panel.group.fields}, {section})
            self.assertEqual(len(panel.group.fields), 7)
            rendered = set(panel._vars) | set(panel._texts)
            self.assertEqual(rendered, {field.key for field in panel.group.fields})
            self.assertEqual(len(rendered), 7)
            self.assertFalse(panel.expanded)
            self.assertIn("未单独配置（默认值）", panel.status_label.cget("text"))

    def test_mode_sub_tabs_embed_safety_override_panels(self) -> None:
        """chat / work 子选项卡内容区的最上面各有一个同字段的覆盖面板。"""
        self.assertEqual(set(self.tab.mode_safety_panels), {"chat", "work"})
        for mode_key, key in (("chat", "safety_chat"), ("work", "safety_work")):
            panel = self.tab.mode_safety_panels[mode_key]
            editor = self.tab.editors[mode_key]
            self.assertIsInstance(panel, SettingPanel)
            self.assertEqual(panel.group.key, key)
            # 父容器是该模式的编辑器，且排在内容区最上面（pack 顺序第一）。
            self.assertIs(panel.frame.master, editor.frame)
            self.assertIs(editor.frame.pack_slaves()[0], panel.frame)
            # 控件数与参数页那一份完全相同（7 个字段：6 下拉/输入框 + 1 多行文本）。
            rendered = set(panel._vars) | set(panel._texts)
            self.assertEqual(rendered, {field.key for field in panel.group.fields})
            self.assertEqual(len(rendered), 7)
            # 默认收起：一进来不该把工具菜单挤出可视区。
            self.assertFalse(panel.expanded)

    def test_mode_safety_panel_saves_and_syncs_params_panel(self) -> None:
        panel = self.tab.mode_safety_panels["chat"]
        self.set_choice(panel, "mode", "enforce")
        self.assertTrue(panel.save())
        # 只写改动过的键：覆盖段里只有 mode。
        self.assertEqual(self.read_toml("app.toml")["safety"]["chat"], {"mode": "enforce"})
        self.assertIn("已保存", self.tab._status.get())
        self.assertIn("重启后台服务后生效", self.tab._status.get())
        self.assertFalse(panel.dirty())
        # 参数页里的同名面板同步成落盘值，不用手动「重新载入」。
        params_panel = self.panel("safety_chat")
        self.assertEqual(params_panel._vars["mode"].get(), "enforce（真正拦截/脱敏）")
        self.assertIn("已配置 1 / 7 项", params_panel.status_label.cget("text"))

    def test_mode_safety_panel_reload_picks_up_external_change(self) -> None:
        configedit.patch_section(
            self.paths.config_path, "safety.work", {"mode": "audit"}
        )
        self.tab.reload()
        panel = self.tab.mode_safety_panels["work"]
        self.assertEqual(panel._vars["mode"].get(), "audit（只记审计，不改输出）")
        # 参数页里的同一组也跟着重新载入。
        params_panel = self.panel("safety_work")
        self.assertEqual(
            params_panel._vars["mode"].get(), "audit（只记审计，不改输出）"
        )

    def test_mode_safety_panel_without_changes_does_not_write(self) -> None:
        path = Path(self.paths.config_path)
        before = path.read_bytes()
        self.assertTrue(self.tab.mode_safety_panels["work"].save())
        self.assertIn("没有改动", self.tab._status.get())
        self.assertEqual(path.read_bytes(), before)

    def test_expand_collapse_all(self) -> None:
        editor = self.tab.params_editor
        editor.expand_all()
        for panel in editor.panels.values():
            self.assertTrue(panel.expanded)
            self.assertTrue(panel.body.winfo_manager())
            self.assertEqual(str(panel.toggle_button.cget("text")), "▾")
        editor.collapse_all()
        for panel in editor.panels.values():
            self.assertFalse(panel.expanded)
            self.assertEqual(str(panel.toggle_button.cget("text")), "▸")

    def test_params_tab_hides_scope_list_and_restores(self) -> None:
        # 参数是全局的、跟群无关：切到参数页时隐藏左侧群列表，把宽度让给参数面板。
        self.tab.notebook.select(len(self.tab.notebook.tabs()) - 1)
        self.root.update()
        self.assertFalse(self.tab._left_frame.winfo_manager())
        self.tab.notebook.select(0)
        self.root.update()
        self.assertTrue(self.tab._left_frame.winfo_manager())

    def test_footer_lists_tools_without_params(self) -> None:
        text = self.tab.params_editor.footer_label.cget("text")
        self.assertIn("没有可调参数", text)
        self.assertIn(DISCOVER_TOOL, text)

    # -- 保存 ---------------------------------------------------------------
    def test_save_only_touched_field_keeps_other_keys_and_comments(self) -> None:
        panel = self.panel("image_prompt")
        before = self.read_toml("services.toml")
        self.set_text(panel, "preset_prompt", "masterpiece, best quality")
        self.assertTrue(panel.save())
        services = self.read_toml("services.toml")
        self.assertEqual(
            services["image_generation"]["preset_prompt"], "masterpiece, best quality"
        )
        # 同小节其它键一个都没被重写（行内注释还在）。
        self.assertEqual(
            services["image_generation"]["optimize_max_anchors"],
            before["image_generation"]["optimize_max_anchors"],
        )
        text = Path(self.paths.services_path).read_text(encoding="utf-8")
        self.assertIn("# low / medium / high", text)
        self.assertIn("已保存", self.tab._status.get())
        # 保存后不再是脏面板，状态回到"已配置 N / M 项"。
        self.assertFalse(panel.dirty())

    def test_global_panel_writes_app_toml(self) -> None:
        panel = self.panel("global")
        panel._vars["max_rounds_per_turn"].set("4")
        self.assertTrue(panel.save())
        app = self.read_toml("app.toml")
        self.assertEqual(app["tools"]["max_rounds_per_turn"], 4)
        self.assertEqual(panel._vars["max_rounds_per_turn"].get(), "4")

    def test_tristate_save_and_delete(self) -> None:
        panel = self.panel("image_core")
        self.set_tristate(panel, "superadmin_only", "off")
        self.assertTrue(panel.save())
        self.assertIs(
            self.read_toml("services.toml")["image_generation"]["superadmin_only"], False
        )
        self.set_tristate(panel, "superadmin_only", "default")
        self.assertTrue(panel.save())
        self.assertNotIn(
            "superadmin_only", self.read_toml("services.toml")["image_generation"]
        )
        self.assertFalse(panel.dirty())

    def test_choice_save(self) -> None:
        panel = self.panel("image_core")
        self.set_choice(panel, "quality", "medium")
        self.assertTrue(panel.save())
        self.assertEqual(
            self.read_toml("services.toml")["image_generation"]["quality"], "medium"
        )

    def test_path_field_and_bool_field(self) -> None:
        panel = self.panel("artist")
        panel._vars["metadata_store"].set("image_metadata.json")
        self.assertTrue(panel.save())
        self.assertEqual(
            self.read_toml("services.toml")["image_generation"]["metadata_store"],
            "image_metadata.json",
        )
        core = self.panel("image_core")
        core._vars["send_by_default"].set(True)
        self.assertTrue(core.save())
        self.assertIs(
            self.read_toml("services.toml")["image_generation"]["send_by_default"], True
        )

    def test_invalid_int_shows_error_and_does_not_write(self) -> None:
        path = Path(self.paths.config_path)
        before = path.read_text(encoding="utf-8")
        panel = self.panel("global")
        panel._vars["max_rounds_per_turn"].set("很多次")
        self.assertFalse(panel.save())
        self.assertIn("失败", self.tab._status.get())
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertTrue(panel.dirty())

    def test_save_all_without_changes(self) -> None:
        self.tab.params_editor.save_all()
        self.assertIn("没有未保存的改动", self.tab._status.get())

    def test_save_all_saves_every_dirty_panel(self) -> None:
        global_panel = self.panel("global")
        global_panel._vars["max_rounds_per_turn"].set("5")
        prompt_panel = self.panel("image_prompt")
        self.set_text(prompt_panel, "preset_prompt", "cinematic lighting")
        self.assertEqual(len(self.tab.params_editor.dirty_panels()), 2)
        self.tab.params_editor.save_all()
        app = self.read_toml("app.toml")
        self.assertEqual(app["tools"]["max_rounds_per_turn"], 5)
        services = self.read_toml("services.toml")
        self.assertEqual(
            services["image_generation"]["preset_prompt"], "cinematic lighting"
        )
        self.assertEqual(self.tab.params_editor.dirty_panels(), [])
        self.assertIn("已保存 2 组", self.tab._status.get())

    def test_reload_picks_up_external_change(self) -> None:
        configedit.patch_section(
            self.paths.services_path,
            "image_generation",
            {"preset_prompt": "外部改的值"},
        )
        self.tab.reload()
        text = self.panel("image_prompt")._texts["preset_prompt"].get("1.0", tk.END)
        self.assertIn("外部改的值", text)

    # -- 从工具行跳转 -------------------------------------------------------
    def test_tool_row_params_button_reveals_panel(self) -> None:
        editor = self.open_whitelist("work")
        row = editor._rows["web_search"]
        self.assertIsNotNone(row.params_button)
        row.params_button.invoke()
        selected = self.tab.notebook.select()
        self.assertEqual(selected, str(self.tab.params_editor.frame))
        panels = [group.key for group in groups_for_tool("web_search")]
        for key in panels:
            self.assertTrue(self.panel(key).expanded)
        self.assertIn("web_search", self.tab.params_editor.panels["web_search"].group.tools)

    def test_tool_rows_without_params_have_no_button(self) -> None:
        editor = self.open_whitelist("work")
        self.assertIsNone(editor._rows[DISCOVER_TOOL].params_button)
        self.assertIsNotNone(editor._rows["image_generate"].params_button)

    def test_show_tool_params_for_image_generate_expands_both_panels(self) -> None:
        self.tab.show_tool_params("image_generate")
        self.assertTrue(self.panel("image_core").expanded)
        self.assertTrue(self.panel("image_prompt").expanded)
        self.assertEqual(
            self.tab.notebook.select(), str(self.tab.params_editor.frame)
        )

    def test_show_global_params_selects_params_sub_tab(self) -> None:
        # 「功能」页「内容安全」行 → 工具权限 → 工具参数（全局）→ 展开全局面板。
        self.tab.show_global_params()
        self.assertEqual(
            self.tab.notebook.select(), str(self.tab.params_editor.frame)
        )
        self.assertTrue(self.panel("global").expanded)


class CharacterReactionTabCase(TkCase):
    """人设卡页的「表情图库」子选项卡：策略落盘 + 图库条目落盘。"""

    def setUp(self) -> None:
        super().setUp()
        (Path(self.paths.config_dir) / "characters").mkdir(parents=True, exist_ok=True)
        self.tab = self.build(CharacterTab)

    def _titles(self) -> list[str]:
        return [
            self.tab.notebook.tab(index, "text")
            for index in range(len(self.tab.notebook.tabs()))
        ]

    def test_reaction_sub_tab_present(self) -> None:
        self.assertIn(" 表情图库 ", self._titles())
        self.assertIsNotNone(self.tab.reaction_panel)

    def test_policy_saved_and_reloaded(self) -> None:
        panel = self.tab.reaction_panel
        panel.enabled_var.set(True)
        panel.group_var.set(False)
        panel.private_var.set(True)
        panel.number_vars["cooldown_text_replies"].set("3")
        panel.number_vars["probability"].set("0.5")
        panel.emotions_var.set("无语、得意")
        panel.keywords_var.set("报错，失败")
        panel.save_policy()

        raw = self.read_toml("app.toml")["reactions"]
        self.assertTrue(raw["enabled"])
        self.assertFalse(raw["show_in_group"])
        self.assertEqual(3, raw["cooldown_text_replies"])
        self.assertEqual(0.5, raw["probability"])
        self.assertEqual(["无语", "得意"], raw["emotions"])
        self.assertEqual(["报错", "失败"], raw["suppress_keywords"])
        self.assertIn("reactions", raw["gallery_dir"])
        self.assertTrue(os.path.isabs(raw["gallery_dir"]))

        # 界面改回内存值后再 load：必须回显落盘值（双向联动）。
        panel.enabled_var.set(False)
        panel.number_vars["cooldown_text_replies"].set("0")
        panel.load()
        self.assertTrue(panel.enabled_var.get())
        self.assertEqual("3", panel.number_vars["cooldown_text_replies"].get())

    def test_gallery_add_and_save_catalog(self) -> None:
        import json

        panel = self.tab.reaction_panel
        gallery = panel.gallery_label_var.get()
        self.assertTrue(gallery)
        source = os.path.join(str(TEMP_ROOT), "src.png")
        with open(source, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        panel.item_emotion_var.set("无语")
        panel.item_name_var.set("亚尔缇娜·无语")
        panel.item_weight_var.set("2")
        with mock.patch("tkinter.filedialog.askopenfilename", return_value=source):
            panel._add_image()
        self.assertEqual(1, len(panel.items))
        panel.save_catalog()

        with open(os.path.join(gallery, "catalog.json"), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(1, len(raw["items"]))
        self.assertTrue(raw["items"][0]["path"].startswith("assets/"))
        self.assertEqual("无语", raw["items"][0]["emotion"])
        self.assertEqual(2.0, raw["items"][0]["weight"])
        self.assertTrue(os.path.isfile(os.path.join(gallery, raw["items"][0]["path"])))

        # 重新载入：列表里能看到这条。
        panel.load()
        self.assertEqual(1, len(panel.tree.get_children("")))

    def test_gallery_toggle_and_remove(self) -> None:
        panel = self.tab.reaction_panel
        source = os.path.join(str(TEMP_ROOT), "src.png")
        with open(source, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        panel.item_emotion_var.set("开心")
        with mock.patch("tkinter.filedialog.askopenfilename", return_value=source):
            panel._add_image()
        self.assertEqual(1, len(panel.items))
        panel.tree.selection_set("0")
        panel._toggle_item()
        self.assertFalse(panel.items[0]["enabled"])
        panel.tree.selection_set("0")
        panel._remove_item()
        self.assertEqual([], panel.items)


    # -- 布局回归 -----------------------------------------------------------
    def test_reaction_sub_tab_is_laid_out(self) -> None:
        """面板必须被 pack 进子选项卡页框，否则整页空白（1.0.4–1.0.7 的缺陷）。"""
        panel = self.tab.reaction_panel
        page = self.tab.reaction_page
        self.assertIsNotNone(page)
        self.assertEqual(str(page), self.tab.notebook.tabs()[3])
        # 未布局时 manager 为空字符串，这正是“一片空白”的直接原因。
        self.assertEqual("pack", panel.frame.winfo_manager())
        self.assertEqual([panel.frame], page.winfo_children())
        self.assertGreaterEqual(len(panel.frame.winfo_children()), 2)
        self.assertEqual("pack", panel.tree.winfo_manager())

    def test_show_reactions_tab_selects_its_page(self) -> None:
        """「功能」页跳转要选中 notebook 页框，不能拿面板内部 frame 去 select（会 TclError）。"""
        self.tab.notebook.select(0)
        self.root.update()
        self.tab.show_reactions_tab()
        self.assertEqual(str(self.tab.reaction_page), self.tab.notebook.select())


class FeaturesTabCase(TkCase):
    """功能页：双击/按钮跳转到新页。"""

    def test_view_log_jumps_to_logs_tab(self) -> None:
        goto: list[str] = []
        tab = self.build(FeaturesTab, on_goto=goto.append)
        tab._show_log_window()
        self.assertEqual(goto, ["logs"])


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
