"""「提示词」选项卡与标签库的 GUI 用例（Tk 不可用时整组跳过）。"""

from __future__ import annotations

import json
import os
import shutil
import tkinter as tk
import tomllib
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import configedit
from atri_x.gui import imageprofileedit, promptedit, tooltagedit  # noqa: F401 - 覆盖登记
from atri_x.gui.tabs.promptlib import PromptLibTab
from atri_x.promptlib import TagCatalog, TagEntry

TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_promptlib"
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"
ASSET_FILES = (
    "app.toml",
    "services.toml",
    "state.toml",
    "atrinis.toml",
    "SOUL.md",
    "tool_tags.toml",
)


def _prepare_config() -> configedit.ConfigPaths:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    return configedit.ConfigPaths.from_config(str(config_dir / "app.toml"))


class TkCase(unittest.TestCase):
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
            mock.patch("tkinter.messagebox.askyesno", return_value=True),
        ]
        for item in self._dialogs:
            item.start()

    def tearDown(self) -> None:
        for item in self._dialogs:
            item.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def build_tab(self) -> PromptLibTab:
        frame = ttk.Frame(self.root)
        frame.pack()
        return PromptLibTab(frame, self.paths)

    def read_toml(self, name: str) -> dict:
        with open(os.path.join(self.paths.config_dir, name), "rb") as handle:
            return tomllib.load(handle)


class PathsCase(TkCase):
    def test_tool_tags_path_is_derived(self) -> None:
        self.assertTrue(self.paths.tool_tags_path.endswith("tool_tags.toml"))


class PromptLibTabCase(TkCase):
    def test_tab_builds_four_panels(self) -> None:
        tab = self.build_tab()
        self.assertEqual(len(tab._panels), 4)
        for name in ("提示词模板", "生图档案", "工具标签", "标签库"):
            self.assertIn(name, tab._panels)

    def test_template_override_written_to_prompts_json(self) -> None:
        tab = self.build_tab()
        panel = tab._panels["提示词模板"]
        panel._select("image.rewrite.natural")
        panel.text.delete("1.0", tk.END)
        panel.text.insert("1.0", "自定义生图教义")
        panel.save()
        path = Path(self.paths.config_dir) / "gui" / "prompts.json"
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["prompts"]["image.rewrite.natural"]["content"], "自定义生图教义"
        )
        tab._panels["提示词模板"].reload()
        panel._select("image.rewrite.natural")
        self.assertEqual(panel.text.get("1.0", "end-1c"), "自定义生图教义")

    def test_custom_entry_inject_roundtrip(self) -> None:
        tab = self.build_tab()
        panel = tab._panels["提示词模板"]
        panel._new_entry()
        panel.title_var.set("群聊口吻")
        panel.text.delete("1.0", tk.END)
        panel.text.insert("1.0", "回复尽量简短。")
        panel.inject_var.set(True)
        panel.save()
        path = Path(self.paths.config_dir) / "gui" / "prompts.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        custom = [item for item in payload["prompts"].values() if item["id"].startswith("custom.")]
        self.assertEqual(len(custom), 1)
        self.assertTrue(custom[0]["inject"])


class ImageProfilesCase(TkCase):
    def test_add_profile_writes_services_toml(self) -> None:
        with mock.patch("tkinter.simpledialog.askstring", return_value="fast"):
            tab = self.build_tab()
            panel = tab._panels["生图档案"]
            panel._new_profile()
        panel._entries["model"].set("gpt-image-2.5")
        panel._entries["quality"].set("medium")
        panel._entries["aliases"].set("快速, fast")
        panel._texts["preset_prompt"].insert("1.0", "赛博朋克，霓虹")
        panel.save()
        data = self.read_toml("services.toml")
        profile = data["image_generation"]["profiles"]["fast"]
        self.assertEqual(profile["model"], "gpt-image-2.5")
        self.assertEqual(profile["quality"], "medium")
        self.assertEqual(profile["aliases"], ["快速", "fast"])
        self.assertEqual(profile["preset_prompt"], "赛博朋克，霓虹")

    def test_delete_profile_removes_section(self) -> None:
        with mock.patch("tkinter.simpledialog.askstring", return_value="temp"):
            tab = self.build_tab()
            panel = tab._panels["生图档案"]
            panel._new_profile()
        panel.save()
        self.assertIn("temp", self.read_toml("services.toml")["image_generation"]["profiles"])
        panel._select("temp")
        panel._delete_profile()
        panel.save()
        profiles = self.read_toml("services.toml")["image_generation"].get("profiles", {})
        self.assertNotIn("temp", profiles)


class ToolTagsCase(TkCase):
    def test_add_tag_writes_toml(self) -> None:
        with mock.patch("tkinter.simpledialog.askstring", return_value="coder"):
            tab = self.build_tab()
            panel = tab._panels["工具标签"]
            panel._new_tag()
        panel.tools_var.set("read_file, shell")
        panel.text.insert("1.0", "先读上下文再改代码。")
        panel.save()
        data = self.read_toml("tool_tags.toml")
        self.assertEqual(data["tags"]["coder"]["tools"], ["read_file", "shell"])
        self.assertEqual(data["tags"]["coder"]["prompt"], "先读上下文再改代码。")

    def test_existing_file_reloads(self) -> None:
        tab = self.build_tab()
        panel = tab._panels["工具标签"]
        # 默认资产里有一个 agent 标签。
        self.assertIn("agent", panel._tags)


class TagLibraryCase(TkCase):
    def test_add_tag_writes_tags_json(self) -> None:
        with mock.patch("tkinter.simpledialog.askstring", return_value="傲娇"):
            tab = self.build_tab()
            panel = tab._panels["标签库"]
            panel._new_tag()
        panel.group_var.set("性格")
        panel.aliases_var.set("tsundere")
        panel.text.insert("1.0", "嘴硬心软")
        panel.save()
        path = Path(self.paths.config_dir) / "gui" / "tags.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["tags"][0]["name"], "傲娇")
        self.assertEqual(payload["tags"][0]["description"], "嘴硬心软")
        self.assertEqual(payload["tags"][0]["aliases"], ["tsundere"])

    def test_choose_tags_without_config_dir_returns_none(self) -> None:
        from atri_x.gui.taglibedit import choose_tags

        self.assertIsNone(choose_tags(self.root, "", []))


class MemberTagPickerCase(TkCase):
    def test_members_tab_merges_picked_tags(self) -> None:
        from atri_x.gui.tabs.members import MembersTab
        from atri_x.gui.taglibedit import apply_tag_picker

        TagCatalog(str(self.paths.config_dir)).save([TagEntry(name="傲娇")])
        frame = ttk.Frame(self.root)
        frame.pack()
        tab = MembersTab(frame, self.paths)
        tab.tags_var.set("老标签")
        with mock.patch(
            "atri_x.gui.taglibedit.choose_tags", return_value=["傲娇", "新标签"]
        ):
            apply_tag_picker(tab, tab.tags_var, "从标签库选择群友标签")
        self.assertEqual(tab.tags_var.get(), "老标签, 傲娇, 新标签")

    def test_members_preview_shows_tag_description(self) -> None:
        from atri_x.gui.memberdata import MemberProfile
        from atri_x.gui.tabs.members import MembersTab

        TagCatalog(str(self.paths.config_dir)).save(
            [TagEntry(name="傲娇", description="嘴硬心软")]
        )
        frame = ttk.Frame(self.root)
        frame.pack()
        tab = MembersTab(frame, self.paths)
        tab._current = MemberProfile(
            platform="qqonebot", scope_id="group:1", user_id="2", alias="小梅"
        )
        tab.tags_var.set("傲娇")
        tab._update_preview()
        preview = tab.preview_text.get("1.0", "end-1c")
        self.assertIn("标签：傲娇（嘴硬心软）", preview)

    def test_character_tab_merges_picked_tags(self) -> None:
        from atri_x.gui.tabs.character import CharacterTab
        from atri_x.gui.taglibedit import apply_tag_picker

        frame = ttk.Frame(self.root)
        frame.pack()
        tab = CharacterTab(frame, self.paths)
        tab.tags_var.set("")
        with mock.patch(
            "atri_x.gui.taglibedit.choose_tags", return_value=["元气"]
        ):
            apply_tag_picker(tab, tab.tags_var, "从标签库选择人设卡标签")
        self.assertEqual(tab.tags_var.get(), "元气")


if __name__ == "__main__":
    unittest.main()
