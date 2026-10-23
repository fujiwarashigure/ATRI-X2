"""「工具参数」子选项卡的数据层用例：注册表自检 + 读写 + 值转换（不建 Tk 窗口）。

覆盖三件事：

- **注册表自检**：面板 key 唯一、字段 key 唯一、引用的工具都在工具目录里、
  全局面板排第一、分类顺序与工具目录一致；内容安全的 chat / work 覆盖面板同样是
  「无工具」面板（各 7 个字段、只写 ``[safety.chat]`` / ``[safety.work]``）；
- **读**：从发行资产（真实的 ``app.toml`` / ``services.toml``）读到正确的值，
  未配置的键读成 ``None``；
- **写**：字符串 / 整数 / 浮点 / 布尔 / 三态 / 下拉 / 列表 / 路径字段的落盘语义，
  非法输入一个字节都不写，注释保留、备份生成。
"""

from __future__ import annotations

import shutil
import tomllib
import unittest
from pathlib import Path

from atri_x.gui import toolcatalog, toolsettings
from atri_x.gui.configedit import ConfigEditError, ConfigPaths, patch_section

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_toolsettings"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

#: 会被复制进临时配置目录的资产。
ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")


def _prepare_config() -> ConfigPaths:
    """复制发行资产到临时目录，返回配置路径表。"""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    return ConfigPaths.from_config(str(config_dir / "app.toml"))


class RegistryCase(unittest.TestCase):
    """注册表本身的自检（不需要配置文件）。"""

    def test_groups_have_fields_and_valid_kinds(self) -> None:
        self.assertTrue(toolsettings.GROUPS)
        for group in toolsettings.GROUPS:
            self.assertTrue(group.fields, f"面板 {group.key} 没有字段")
            for field in group.fields:
                self.assertIn(field.kind, toolsettings.KINDS)
                self.assertTrue(field.section.strip(), f"{group.key}.{field.key} 缺小节")
                self.assertIn(field.file, ("app", "services"))
                if field.kind == "choice":
                    values = [value for value, _label in field.choices]
                    self.assertTrue(values, f"{group.key}.{field.key} 没有候选值")
                    self.assertIn("", values, f"{group.key}.{field.key} 缺少「默认」候选")

    def test_field_keys_unique_inside_group(self) -> None:
        for group in toolsettings.GROUPS:
            keys = [field.key for field in group.fields]
            self.assertEqual(len(keys), len(set(keys)), group.key)

    def test_group_keys_unique(self) -> None:
        keys = [group.key for group in toolsettings.GROUPS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_first_group_is_global_and_other_scopeless_groups_allowed(self) -> None:
        """全局面板必须排第一；其余「无工具」面板（内容安全覆盖）允许存在。"""
        self.assertTrue(toolsettings.GROUPS[0].global_scope)
        self.assertEqual(toolsettings.GROUPS[0].key, "global")
        globals_ = toolsettings.global_groups()
        self.assertGreaterEqual(len(globals_), 3)
        self.assertIs(globals_[0], toolsettings.GROUPS[0])
        self.assertEqual(
            [group.key for group in globals_[:3]], ["global", "safety_chat", "safety_work"]
        )

    def test_safety_override_groups_are_scopeless_and_fully_in_their_section(self) -> None:
        """两个覆盖面板：各 7 个字段、小节全在 safety.chat / safety.work。"""
        for key, section in (
            ("safety_chat", "safety.chat"),
            ("safety_work", "safety.work"),
        ):
            group = toolsettings.group_by_key(key)
            self.assertIsNotNone(group, f"缺少面板 {key}")
            assert group is not None
            self.assertEqual(group.tools, ())
            self.assertTrue(group.global_scope)
            self.assertEqual(
                [field.key for field in group.fields],
                [
                    "enabled",
                    "mode",
                    "block_reply",
                    "max_scan_chars",
                    "strip_tool_images",
                    "block_image_output",
                    "block_file_output",
                ],
            )
            self.assertEqual(len(group.fields), 7)
            self.assertEqual({field.section for field in group.fields}, {section})
            self.assertEqual({field.file for field in group.fields}, {"app"})
            # 审计文件全局唯一，类别 / 白名单只认全局：覆盖面板里都不能出现。
            keys = {field.key for field in group.fields}
            self.assertNotIn("audit_path", keys)
            for forbidden in (
                "categories",
                "sensitive",
                "sensitive_actions",
                "allowlist",
            ):
                self.assertNotIn(forbidden, keys)
            kinds = {field.key: field.kind for field in group.fields}
            self.assertEqual(kinds["enabled"], "tristate")
            self.assertEqual(kinds["mode"], "choice")
            self.assertEqual(kinds["block_reply"], "text")
            self.assertEqual(kinds["max_scan_chars"], "int")
            for name in (
                "strip_tool_images",
                "block_image_output",
                "block_file_output",
            ):
                self.assertEqual(kinds[name], "tristate")
            mode_field = next(field for field in group.fields if field.key == "mode")
            self.assertEqual(mode_field.choices[0], ("", "（继承全局）"))
            self.assertEqual(
                [value for value, _label in mode_field.choices],
                ["", "off", "audit", "enforce"],
            )
            self.assertIn("重启后台服务", group.note)
            self.assertIn(f"[{section}]", group.note)
            self.assertIn("app.toml", group.note)

    def test_safety_override_panels_do_not_join_category_groups(self) -> None:
        """无工具面板不进「按工具分类」的分组（由参数页单独排在全局面板之后）。"""
        keys = [
            group.key
            for _category, groups in toolsettings.groups_by_category()
            for group in groups
        ]
        self.assertNotIn("safety_chat", keys)
        self.assertNotIn("safety_work", keys)
        self.assertNotIn("global", keys)

    def test_group_tools_exist_in_catalog(self) -> None:
        names = set(toolcatalog.catalog_names())
        for group in toolsettings.GROUPS:
            for tool in group.tools:
                self.assertIn(tool, names, f"面板 {group.key} 引用了不存在的工具 {tool}")

    def test_covered_and_uncovered_partition_catalog(self) -> None:
        covered = {tool for group in toolsettings.GROUPS for tool in group.tools}
        uncovered = set(toolsettings.uncovered_tools())
        self.assertEqual(covered & uncovered, set())
        self.assertEqual(covered | uncovered, set(toolcatalog.catalog_names()))

    def test_has_settings_and_groups_for_tool(self) -> None:
        self.assertTrue(toolsettings.has_settings("image_generate"))
        keys = [group.key for group in toolsettings.groups_for_tool("image_generate")]
        self.assertIn("image_core", keys)
        self.assertIn("image_prompt", keys)
        self.assertFalse(toolsettings.has_settings("discover_tool"))
        self.assertEqual(toolsettings.groups_for_tool("discover_tool"), ())

    def test_groups_by_category_is_ordered_and_complete(self) -> None:
        grouped = toolsettings.groups_by_category()
        keys = [group.key for _category, groups in grouped for group in groups]
        expected = [group.key for group in toolsettings.GROUPS if not group.global_scope]
        self.assertEqual(sorted(keys), sorted(expected))
        categories = [category for category, _groups in grouped]
        for category in categories:
            self.assertIn(category, toolcatalog.CATEGORY_ORDER)
        # 分类顺序跟着工具目录走（联网在文件与执行之前，图片与画师在记忆之后）。
        self.assertLess(categories.index("联网"), categories.index("文件与执行"))
        self.assertLess(categories.index("记忆"), categories.index("图片与画师"))
        # 同一个工具的两个面板挨在一起。
        image = next(groups for category, groups in grouped if category == "图片与画师")
        image_keys = [group.key for group in image]
        self.assertEqual(
            image_keys.index("image_prompt") - image_keys.index("image_core"), 1
        )


class ReadWriteCase(unittest.TestCase):
    """读写：用真实的发行资产起一份临时配置目录。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()

    def read(self, name: str) -> dict:
        with open(Path(self.paths.config_dir) / name, "rb") as handle:
            return tomllib.load(handle)

    def values(self, key: str) -> dict:
        group = toolsettings.group_by_key(key)
        assert group is not None
        return dict(toolsettings.load_group(self.paths, group))

    def save(self, key: str, **changes) -> list[str]:
        group = toolsettings.group_by_key(key)
        assert group is not None
        values = self.values(key)
        values.update(changes)
        return toolsettings.save_group(self.paths, group, values)

    def save_changes(self, key: str, **changes) -> list[str]:
        """只提交改动过的键——与界面（``SettingPanel.save``）的实际入参一致。

        :meth:`save` 会把整组值（含 ``None``）一起交给 ``save_group``，对已经有完整小节
        的面板等价；覆盖面板的小节一开始不存在，用它才测得出"只写改动键"。
        """
        group = toolsettings.group_by_key(key)
        assert group is not None
        return toolsettings.save_group(self.paths, group, changes)

    # -- 读 -----------------------------------------------------------------
    def test_load_all_reads_assets(self) -> None:
        data = toolsettings.load_all(self.paths)
        self.assertEqual(set(data), {group.key for group in toolsettings.GROUPS})
        # app.toml 里显式写了 max_rounds_per_turn = 10。
        self.assertEqual(data["global"]["max_rounds_per_turn"], 10)
        self.assertEqual(data["global"]["user_max_tool_risk"], "low")
        # services.toml 里 enabled = false / 默认尺寸。
        self.assertIs(data["image_core"]["enabled"], False)
        self.assertEqual(data["image_core"]["size"], "1024x1024")
        # 没配置的键读成 None（界面上留空 = 用默认值）；资产里显式写了空串的保持空串。
        self.assertEqual(data["image_prompt"]["preset_prompt"], "")
        self.assertIsNone(data["artist"]["metadata_store"])
        self.assertIsNone(data["web_search"]["engine_order"])

    def test_status_text_counts_configured_fields(self) -> None:
        group = toolsettings.group_by_key("global")
        assert group is not None
        text = toolsettings.status_text(group, self.values("global"))
        self.assertIn("已配置", text)
        group = toolsettings.group_by_key("artist")
        assert group is not None
        self.assertEqual(
            toolsettings.status_text(group, self.values("artist")),
            "未单独配置（默认值）",
        )

    # -- 写 -----------------------------------------------------------------
    def test_save_text_field_round_trip(self) -> None:
        changed = self.save("image_prompt", preset_prompt="masterpiece, best quality")
        self.assertEqual(changed, ["services.toml"])
        data = self.read("services.toml")
        self.assertEqual(
            data["image_generation"]["preset_prompt"], "masterpiece, best quality"
        )
        self.assertEqual(self.values("image_prompt")["preset_prompt"], "masterpiece, best quality")

    def test_save_int_and_float(self) -> None:
        self.save("image_prompt", optimize_max_anchors="6", passthrough=None)
        self.save("image_to_prompt", temperature="0.35")
        services = self.read("services.toml")
        self.assertEqual(services["image_generation"]["optimize_max_anchors"], 6)
        self.assertEqual(services["image_to_prompt"]["temperature"], 0.35)

    def test_save_bool_field(self) -> None:
        self.save("image_core", send_by_default=True, supports_reference=True)
        services = self.read("services.toml")
        self.assertIs(services["image_generation"]["send_by_default"], True)
        self.assertIs(services["image_generation"]["supports_reference"], True)

    def test_tristate_on_off_and_default(self) -> None:
        self.save("image_core", superadmin_only="off")
        services = self.read("services.toml")
        self.assertIs(services["image_generation"]["superadmin_only"], False)
        self.save("image_core", superadmin_only="on")
        self.assertIs(self.read("services.toml")["image_generation"]["superadmin_only"], True)
        # 选「默认」= 把这个键删掉（回到配置层默认值）。
        self.save("image_core", superadmin_only="default")
        self.assertNotIn("superadmin_only", self.read("services.toml")["image_generation"])
        self.assertIsNone(self.values("image_core")["superadmin_only"])

    def test_choice_field(self) -> None:
        self.save("image_core", quality="medium", output_format="webp")
        services = self.read("services.toml")
        self.assertEqual(services["image_generation"]["quality"], "medium")
        self.assertEqual(services["image_generation"]["output_format"], "webp")
        # 选「（默认）」= 删掉键。
        self.save("image_core", quality="", output_format="")
        section = self.read("services.toml")["image_generation"]
        self.assertNotIn("quality", section)
        self.assertNotIn("output_format", section)

    def test_list_field_and_empty_semantics(self) -> None:
        self.save("web_search", engine_order="deepseek, tavily， firecrawl")
        section = self.read("services.toml")["web_search"]
        self.assertEqual(section["engine_order"], ["deepseek", "tavily", "firecrawl"])
        # 留空 = 删掉键（回落到内置默认链）；删掉唯一键后只剩一个空小节，语义等价。
        self.save("web_search", engine_order="")
        self.assertEqual(self.read("services.toml")["web_search"], {})
        # 显式 none = 空数组（后台 shell 全部拒绝的语义）。
        self.save(
            "file_exec", root="", background_shell_allow="none", background_shell_allow_all=False
        )
        security = self.read("app.toml")["security"]
        self.assertEqual(security["background_shell_allow"], [])

    def test_path_field_empty_removes_key(self) -> None:
        self.save("artist", metadata_store="image_metadata.json")
        self.assertEqual(
            self.read("services.toml")["image_generation"]["metadata_store"],
            "image_metadata.json",
        )
        self.save("artist", metadata_store="")
        self.assertNotIn("metadata_store", self.read("services.toml")["image_generation"])

    def test_nested_section_created_on_demand(self) -> None:
        # 资产里 [image_generation.artist_library] 整段是注释掉的：保存一次就该建出来。
        self.save("artist", root="artist_library.json", enabled="on")
        services = self.read("services.toml")
        library = services["image_generation"]["artist_library"]
        self.assertEqual(library["root"], "artist_library.json")
        self.assertIs(library["enabled"], True)

    def test_missing_top_level_section_created_on_demand(self) -> None:
        # services.toml 资产里 [vision] 整段是注释掉的：保存一次就该建出来。
        self.save("media_vision", enabled="on")
        services = self.read("services.toml")
        self.assertIs(services["vision"]["enabled"], True)
        # 已存在的小节（app.toml 的 [sandbox]）只改键、不重复建小节。
        self.save("file_exec", root="sandbox")
        self.assertEqual(self.read("app.toml")["sandbox"]["root"], "sandbox")

    # -- 内容安全的按模式覆盖（[safety.chat] / [safety.work]）----------------
    def test_safety_chat_override_writes_only_changed_keys(self) -> None:
        changed = self.save_changes("safety_chat", enabled="on", mode="enforce")
        self.assertEqual(changed, ["app.toml"])
        safety = self.read("app.toml")["safety"]
        # 只写改动过的两个键：覆盖段里没有别的键（尤其不带 audit_path / 类别 / 白名单）。
        self.assertEqual(set(safety["chat"]), {"enabled", "mode"})
        self.assertIs(safety["chat"]["enabled"], True)
        self.assertEqual(safety["chat"]["mode"], "enforce")
        # 全局 [safety] 一个键都没被这次写入改掉（只多了一个子表）。
        self.assertIs(safety["enabled"], False)
        self.assertEqual(safety["mode"], "audit")
        self.assertEqual(safety["max_scan_chars"], 8000)
        # 回读：覆盖面板读到的是覆盖段自己的值，另一个模式仍是"没配"（= 继承）。
        self.assertEqual(self.values("safety_chat")["mode"], "enforce")
        self.assertIs(self.values("safety_chat")["enabled"], True)
        self.assertIsNone(self.values("safety_work")["mode"])
        self.assertIsNone(self.values("safety_work")["enabled"])

    def test_safety_work_override_ints_and_inherit_deletes_key(self) -> None:
        changed = self.save_changes(
            "safety_work", block_file_output="on", max_scan_chars="4000"
        )
        self.assertEqual(changed, ["app.toml"])
        work = self.read("app.toml")["safety"]["work"]
        self.assertIs(work["block_file_output"], True)
        self.assertEqual(work["max_scan_chars"], 4000)
        # 拨回「（默认）」/ 清空数字 = 删掉该键（= 继承全局）。
        self.save_changes("safety_work", block_file_output="default", max_scan_chars="")
        self.assertEqual(self.read("app.toml")["safety"]["work"], {})
        self.assertIsNone(self.values("safety_work")["block_file_output"])

    def test_safety_override_no_change_does_not_write(self) -> None:
        before = Path(self.paths.config_path).read_bytes()
        self.assertEqual(self.save_changes("safety_chat"), [])
        self.assertEqual(self.save_changes("safety_work"), [])
        self.assertEqual(Path(self.paths.config_path).read_bytes(), before)

    def test_safety_override_invalid_int_does_not_write(self) -> None:
        before = Path(self.paths.config_path).read_bytes()
        with self.assertRaises(ConfigEditError):
            self.save_changes("safety_chat", max_scan_chars="八千")
        self.assertEqual(Path(self.paths.config_path).read_bytes(), before)

    def test_no_change_returns_empty_list(self) -> None:
        self.assertEqual(self.save("global"), [])
        group = toolsettings.group_by_key("image_prompt")
        assert group is not None
        values = self.values("image_prompt")
        values["preset_prompt"] = "x"
        self.assertEqual(
            toolsettings.save_group(self.paths, group, values), ["services.toml"]
        )
        # 同样的值再存一次：没有改动，不写盘。
        self.assertEqual(toolsettings.save_group(self.paths, group, values), [])

    def test_invalid_int_does_not_write(self) -> None:
        before = self.read("app.toml")
        group = toolsettings.group_by_key("global")
        assert group is not None
        values = self.values("global")
        values["max_rounds_per_turn"] = "很多次"
        with self.assertRaises(ConfigEditError):
            toolsettings.save_group(self.paths, group, values)
        self.assertEqual(self.read("app.toml"), before)

    def test_invalid_choice_does_not_write(self) -> None:
        before = self.read("services.toml")
        group = toolsettings.group_by_key("image_core")
        assert group is not None
        values = self.values("image_core")
        values["quality"] = "banana"
        with self.assertRaises(ConfigEditError):
            toolsettings.save_group(self.paths, group, values)
        self.assertEqual(self.read("services.toml"), before)

    def test_comments_preserved_and_backup_written(self) -> None:
        path = Path(self.paths.services_path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 预设提示词", text)
        self.save("image_prompt", preset_prompt="keep comments")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 预设提示词", text)
        backup = path.with_name(path.name + ".gui.bak")
        self.assertTrue(backup.exists())
        self.assertIn("# 预设提示词", backup.read_text(encoding="utf-8"))

    def test_unknown_value_already_in_file_is_loaded_verbatim(self) -> None:
        patch_section(
            self.paths.services_path,
            "image_generation",
            {"quality": "auto"},
        )
        self.assertEqual(self.values("image_core")["quality"], "auto")

    # -- 跨字段校验 ---------------------------------------------------------
    def test_enabling_image_to_prompt_without_backend_is_rejected(self) -> None:
        """只把「启用反推」拨到「开」不能写盘。

        真机事故：这样写进去的 ``enabled = true`` 会让下次重启后台服务直接失败
        （``[image_to_prompt] is enabled but incomplete``），整个 bot 起不来。
        """
        before = Path(self.paths.services_path).read_bytes()
        group = toolsettings.group_by_key("image_to_prompt")
        assert group is not None
        values = self.values("image_to_prompt")
        values["enabled"] = "on"
        with self.assertRaises(ConfigEditError) as caught:
            toolsettings.save_group(self.paths, group, values)
        message = str(caught.exception)
        self.assertIn("provider", message)
        self.assertIn("model", message)
        # 一个字节都不写（连 .gui.bak 都不该在这次失败里产生新内容）。
        self.assertEqual(Path(self.paths.services_path).read_bytes(), before)

    def test_enabling_image_to_prompt_with_backend_is_allowed(self) -> None:
        self.save("image_to_prompt", provider="central", model="vision-model")
        group = toolsettings.group_by_key("image_to_prompt")
        assert group is not None
        values = self.values("image_to_prompt")
        values["enabled"] = "on"
        self.assertEqual(
            toolsettings.save_group(self.paths, group, values), ["services.toml"]
        )
        section = self.read("services.toml")["image_to_prompt"]
        self.assertIs(section["enabled"], True)
        self.assertEqual(section["provider"], "central")
        self.assertEqual(section["model"], "vision-model")

    def test_enabling_image_to_prompt_reuses_existing_backend(self) -> None:
        """文件里已经有 provider/model 时，只改 enabled 应当通过（校验用合成值）。"""
        patch_section(
            self.paths.services_path,
            "image_to_prompt",
            {"provider": "central", "model": "vision-model"},
        )
        group = toolsettings.group_by_key("image_to_prompt")
        assert group is not None
        values = self.values("image_to_prompt")
        values["enabled"] = "on"
        toolsettings.save_group(self.paths, group, values)
        self.assertIs(self.read("services.toml")["image_to_prompt"]["enabled"], True)


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    unittest.main()
