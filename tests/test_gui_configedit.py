"""桌面端配置读写用例：行级 TOML 补丁、``.env``、provider、模型分配、功能汇总。

这些函数是"在界面上点保存"实际落盘的那一层，所以逐条断言：只改目标键、保留注释、
写前备份、非法内容不落盘、改完能重新读回来。
"""

from __future__ import annotations

import os
import shutil
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from atri_x.gui import configedit, configmodel, featureview

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_configedit"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

#: 会被复制进临时配置目录的资产。
ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")


class ConfigCase(unittest.TestCase):
    """准备一个"发行资产风格"的临时配置目录。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)
        for name in ASSET_FILES:
            shutil.copyfile(ASSETS / name, self.config_dir / name)
        self.paths = configedit.ConfigPaths.from_config(str(self.config_dir / "app.toml"))
        # 测试进程环境里可能有真实密钥，先清掉以免影响"密钥状态"断言。
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "IMAGE_API_KEY", "MY_KEY"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def read(self, name: str) -> str:
        return (self.config_dir / name).read_text(encoding="utf-8")

    def parse(self, name: str) -> dict[str, Any]:
        return tomllib.loads(self.read(name))


class PathsTestCase(ConfigCase):
    """``app.toml`` → 其它配置文件路径。"""

    def test_resolves_all_configured_files(self) -> None:
        self.assertEqual(self.paths.config_dir, str(self.config_dir))
        self.assertEqual(self.paths.services_path, str(self.config_dir / "services.toml"))
        self.assertEqual(self.paths.state_path, str(self.config_dir / "state.toml"))
        self.assertEqual(self.paths.atrinis_path, str(self.config_dir / "atrinis.toml"))
        self.assertEqual(self.paths.env_path, str(self.config_dir / ".env"))
        self.assertEqual(self.paths.soul_path, str(self.config_dir / "SOUL.md"))
        self.assertEqual(self.paths.character_root, str(self.config_dir / "characters"))
        self.assertEqual(self.paths.hooks_path, str(self.config_dir / "plugins" / "hooks.toml"))

    def test_missing_app_toml_falls_back_to_default_names(self) -> None:
        paths = configedit.ConfigPaths.from_config(str(TEMP_ROOT / "nope" / "app.toml"))
        self.assertEqual(paths.services_path, str(TEMP_ROOT / "nope" / "services.toml"))
        self.assertTrue(paths.character_root.endswith("characters"))


class PatchSectionTestCase(ConfigCase):
    """行级 TOML 补丁的语义。"""

    def test_replaces_only_target_key(self) -> None:
        before = self.read("services.toml")
        changed = configedit.patch_section(
            self.paths.services_path,
            "providers.deepseek",
            {"base_url": "https://relay.example.com/v1"},
        )
        self.assertTrue(changed)
        data = self.parse("services.toml")
        self.assertEqual(data["providers"]["deepseek"]["base_url"], "https://relay.example.com/v1")
        # 其他 key 与注释都还在
        self.assertEqual(data["providers"]["deepseek"]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(self.read("services.toml").count("#"), before.count("#"))

    def test_is_idempotent(self) -> None:
        configedit.patch_section(self.paths.services_path, "model_metadata", {"default_context_window": 128000})
        first = self.read("services.toml")
        changed = configedit.patch_section(
            self.paths.services_path, "model_metadata", {"default_context_window": 128000}
        )
        self.assertFalse(changed)
        self.assertEqual(self.read("services.toml"), first)

    def test_creates_missing_section(self) -> None:
        configedit.patch_section(
            self.paths.config_path,
            "platform.qqonebot",
            {"enabled": True, "ws_url": "ws://127.0.0.1:3001"},
            header_comment="桌面端添加",
        )
        data = self.parse("app.toml")
        self.assertIs(data["platform"]["qqonebot"]["enabled"], True)
        self.assertEqual(data["platform"]["qqonebot"]["ws_url"], "ws://127.0.0.1:3001")
        self.assertIn("# 桌面端添加", self.read("app.toml"))

    def test_none_removes_key(self) -> None:
        configedit.patch_section(self.paths.services_path, "providers.openai", {"models": None})
        data = self.parse("services.toml")
        self.assertNotIn("models", data["providers"]["openai"])

    def test_backup_written_once(self) -> None:
        before = self.read("services.toml")
        configedit.patch_section(self.paths.services_path, "model_metadata", {"default_context_window": 1})
        backup = self.config_dir / "services.toml.gui.bak"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), before)

    def test_bad_value_is_rejected_without_writing(self) -> None:
        before = self.read("app.toml")
        with self.assertRaises(configedit.ConfigEditError):
            configedit.patch_section(self.paths.config_path, "x", {"y": object()})
        self.assertEqual(self.read("app.toml"), before)

    def test_remove_section_removes_subtables(self) -> None:
        configedit.patch_section(
            self.paths.services_path,
            "providers.temp",
            {"base_url": "https://x/v1", "api_key_env": "MY_KEY"},
        )
        configedit.patch_section(
            self.paths.services_path, "providers.temp.model_configs.m1", {"context_window": 8000}
        )
        self.assertIn("providers.temp", self.read("services.toml"))
        self.assertTrue(configedit.remove_section(self.paths.services_path, "providers.temp"))
        text = self.read("services.toml")
        self.assertNotIn("providers.temp", text)
        self.assertNotIn("model_configs.m1", text)
        self.assertIn("providers.deepseek", text)

    def test_patch_top_level_keeps_sections(self) -> None:
        changed = configedit.patch_top_level(self.paths.atrinis_path, {"enabled": True})
        self.assertTrue(changed)
        data = self.parse("atrinis.toml")
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["http"]["addr"], "127.0.0.1:32170")


class EnvTestCase(ConfigCase):
    """``.env`` 读写与掩码。"""

    def test_upsert_and_pop(self) -> None:
        path = Path(self.paths.env_path)
        self.assertTrue(configedit.set_env_value(path, "DEEPSEEK_API_KEY", "sk-1234567890"))
        self.assertTrue(configedit.set_env_value(path, "OTHER", "1"))
        self.assertTrue(configedit.set_env_value(path, "DEEPSEEK_API_KEY", "sk-0987654321"))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("DEEPSEEK_API_KEY"), 1)
        self.assertIn("DEEPSEEK_API_KEY=sk-0987654321", text)
        self.assertTrue(configedit.pop_env_value(path, "OTHER"))
        self.assertFalse(configedit.pop_env_value(path, "MISSING"))
        self.assertNotIn("OTHER", path.read_text(encoding="utf-8"))

    def test_rejects_invalid_name(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            configedit.set_env_value(self.paths.env_path, "1BAD-NAME", "x")

    def test_mask_secret(self) -> None:
        self.assertEqual(configmodel.mask_secret(""), "")
        self.assertEqual(configmodel.mask_secret("short"), "****")
        self.assertEqual(configmodel.mask_secret("sk-1234567890abcdef"), "sk-1…cdef")


class ProviderTestCase(ConfigCase):
    """provider 的读、写、改名、删除与密钥状态。"""

    def test_load_from_services(self) -> None:
        providers = configmodel.load_providers(self.paths)
        self.assertEqual(sorted(providers), ["deepseek", "openai"])
        self.assertEqual(providers["deepseek"].api_key_env, "DEEPSEEK_API_KEY")

    def test_save_new_provider(self) -> None:
        view = configmodel.ProviderView(
            name="relay", base_url="https://relay/v1", api_key_env="MY_KEY", models=["m1", "m2"]
        )
        target = configmodel.save_provider(self.paths, view)
        self.assertEqual(target, self.paths.services_path)
        data = self.parse("services.toml")
        self.assertEqual(data["providers"]["relay"]["models"], ["m1", "m2"])
        self.assertEqual(configmodel.load_providers(self.paths)["relay"].models, ["m1", "m2"])

    def test_rename_moves_section(self) -> None:
        view = configmodel.ProviderView(name="deepseek2", base_url="https://x/v1")
        configmodel.save_provider(self.paths, view, original="deepseek")
        providers = configmodel.load_providers(self.paths)
        self.assertIn("deepseek2", providers)
        self.assertNotIn("deepseek", providers)

    def test_delete_provider(self) -> None:
        configmodel.delete_provider(self.paths, "openai")
        self.assertNotIn("openai", configmodel.load_providers(self.paths))
        with self.assertRaises(configedit.ConfigEditError):
            configmodel.delete_provider(self.paths, "openai")

    def test_invalid_name_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            configmodel.validate_provider_name("bad name")

    def test_reasoning_effort_roundtrip_and_clear(self) -> None:
        """推理思考强度：保存落进 services.toml，清空 = 删键（不是留空串）。"""
        view = configmodel.load_providers(self.paths)["deepseek"]
        self.assertEqual(view.reasoning_effort, "")
        view.reasoning_effort = "HIGH"
        configmodel.save_provider(self.paths, view, original="deepseek")
        data = self.parse("services.toml")
        self.assertEqual(data["providers"]["deepseek"]["reasoning_effort"], "high")
        self.assertEqual(
            configmodel.load_providers(self.paths)["deepseek"].reasoning_effort, "high"
        )
        view = configmodel.load_providers(self.paths)["deepseek"]
        view.reasoning_effort = ""
        configmodel.save_provider(self.paths, view, original="deepseek")
        self.assertNotIn(
            "reasoning_effort", self.parse("services.toml")["providers"]["deepseek"]
        )

    def test_reasoning_effort_unknown_value_rejected_before_write(self) -> None:
        view = configmodel.ProviderView(
            name="relay", base_url="https://relay/v1", reasoning_effort="ultra"
        )
        with self.assertRaises(configedit.ConfigEditError) as ctx:
            configmodel.save_provider(self.paths, view)
        self.assertIn("推理思考强度", str(ctx.exception))
        self.assertNotIn("relay", configmodel.load_providers(self.paths))

    def test_save_provider_verifies_file_roundtrip(self) -> None:
        """写盘后必须回读一致；不一致要报错，而不是留下"界面改了、文件没改"。"""
        view = configmodel.ProviderView(name="relay", base_url="https://relay/v1")
        real = configmodel.load_providers

        def corrupted(paths: Any) -> dict[str, configmodel.ProviderView]:
            saved = real(paths)
            saved["relay"].base_url = "https://wrong/v1"
            return saved

        with mock.patch.object(configmodel, "load_providers", side_effect=corrupted):
            with self.assertRaises(configedit.ConfigEditError) as ctx:
                configmodel.save_provider(self.paths, view)
        self.assertIn("回读与界面不一致", str(ctx.exception))
        self.assertIn("base_url", str(ctx.exception))

    def test_key_state_from_dotenv_and_process_env(self) -> None:
        providers = configmodel.load_providers(self.paths)
        self.assertFalse(configmodel.provider_key_state(self.paths, providers["deepseek"]).configured)
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_API_KEY", "sk-dotenv-123456")
        state = configmodel.provider_key_state(self.paths, providers["deepseek"])
        self.assertTrue(state.in_dotenv)
        self.assertTrue(state.configured)
        self.assertEqual(state.masked, "sk-d…3456")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-proc-abcdefgh"}):
            state = configmodel.provider_key_state(self.paths, providers["deepseek"])
        self.assertTrue(state.in_process_env)
        self.assertEqual(state.masked, "sk-p…efgh")


class ModelSelectionTestCase(ConfigCase):
    """``state.toml`` 的模型分配。"""

    def test_read_default_mode_and_work_model(self) -> None:
        default_mode, slots = configmodel.load_model_selection(self.paths)
        self.assertEqual(default_mode, "work")
        self.assertEqual(slots["work"], ("deepseek", "deepseek-v4-pro"))
        self.assertEqual(slots["chat"], ("deepseek", "deepseek-v4-flash"))
        self.assertEqual(slots["naming"], ("", ""))

    def test_save_roundtrip_keeps_other_sections(self) -> None:
        configmodel.save_model_selection(
            self.paths,
            "chat",
            {
                "work": ("deepseek", "deepseek-flash"),
                "chat": ("deepseek", "deepseek-v4-flash"),
                "naming": ("deepseek", "deepseek-flash"),
                "compact": ("deepseek", "deepseek-flash"),
            },
        )
        default_mode, slots = configmodel.load_model_selection(self.paths)
        self.assertEqual(default_mode, "chat")
        self.assertEqual(slots["work"], ("deepseek", "deepseek-flash"))
        self.assertEqual(slots["naming"], ("deepseek", "deepseek-flash"))
        data = self.parse("state.toml")
        self.assertIn("atriwisp1", data["mode_models"])
        self.assertEqual(data["session"]["default_mode"], "chat")

    def test_save_skips_empty_slots(self) -> None:
        configmodel.save_model_selection(self.paths, "", {"naming": ("", "")})
        self.assertNotIn("naming_model", self.parse("state.toml"))

    def test_clear_optional_slot_removes_section(self) -> None:
        """清空命名/压缩槽位必须真的从 state.toml 删段，而不是留着旧值。"""
        configmodel.save_model_selection(
            self.paths, "", {"naming": ("deepseek", "deepseek-chat")}
        )
        self.assertIn("naming_model", self.parse("state.toml"))
        configmodel.save_model_selection(self.paths, "", {"naming": ("", "")})
        self.assertNotIn("naming_model", self.parse("state.toml"))
        self.assertEqual(configmodel.load_model_selection(self.paths)[1]["naming"], ("", ""))

    def test_clear_required_slot_rejected_without_touching_file(self) -> None:
        """work/chat 清空会让服务启动失败，必须拒绝（且不能写半份配置）。"""
        before = self.read("state.toml")
        for slot in ("work", "chat"):
            with self.subTest(slot=slot):
                with self.assertRaises(configedit.ConfigEditError) as ctx:
                    configmodel.save_model_selection(self.paths, "", {slot: ("", "")})
                self.assertIn("不能清空", str(ctx.exception))
                self.assertEqual(self.read("state.toml"), before)

    def test_half_filled_slot_rejected_without_touching_file(self) -> None:
        before = self.read("state.toml")
        for values in ({"chat": ("deepseek", "")}, {"chat": ("", "deepseek-chat")}):
            with self.subTest(values=values):
                with self.assertRaises(configedit.ConfigEditError) as ctx:
                    configmodel.save_model_selection(self.paths, "", values)
                self.assertIn("必须同时填写", str(ctx.exception))
                self.assertEqual(self.read("state.toml"), before)

    def test_all_slots_validated_before_any_write(self) -> None:
        """前面槽位合法、后面槽位非法时，整份文件都不应被改。"""
        before = self.read("state.toml")
        with self.assertRaises(configedit.ConfigEditError):
            configmodel.save_model_selection(
                self.paths,
                "chat",
                {"naming": ("deepseek", "deepseek-chat"), "work": ("", "")},
            )
        self.assertEqual(self.read("state.toml"), before)

    def test_save_model_selection_verifies_file_roundtrip(self) -> None:
        real = configmodel.load_model_selection

        def corrupted(paths: Any) -> tuple[str, dict[str, tuple[str, str]]]:
            mode, slots = real(paths)
            slots["chat"] = ("deepseek", "wrong-model")
            return mode, slots

        with mock.patch.object(configmodel, "load_model_selection", side_effect=corrupted):
            with self.assertRaises(configedit.ConfigEditError) as ctx:
                configmodel.save_model_selection(
                    self.paths, "", {"chat": ("deepseek", "deepseek-v4-flash")}
                )
        self.assertIn("回读与界面不一致", str(ctx.exception))


class FeatureViewTestCase(ConfigCase):
    """「功能」页的状态汇总。"""

    def test_collects_expected_rows(self) -> None:
        rows = {row.key: row for row in featureview.collect_features(self.paths)}
        self.assertGreaterEqual(len(rows), 15)
        self.assertEqual(rows["llm"].tab, "model")
        self.assertEqual(rows["character_library"].tab, "character")
        self.assertEqual(rows["platforms"].tab, "platform")
        self.assertEqual(rows["image_generation"].tab, "advanced")
        self.assertEqual(rows["llm"].status, "缺密钥")

    def test_status_follows_config(self) -> None:
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_API_KEY", "sk-1234567890")
        configedit.patch_section(
            self.paths.services_path,
            "image_generation",
            {"enabled": True, "base_url": "https://relay/v1", "model": "img"},
        )
        configedit.patch_section(self.paths.config_path, "maintenance.daily_summary", {"enabled": False})
        rows = {row.key: row for row in featureview.collect_features(self.paths)}
        self.assertEqual(rows["llm"].status, "已配置")
        self.assertEqual(rows["image_generation"].status, "已启用")
        self.assertIn("relay", rows["image_generation"].detail)
        # 关掉 daily_summary 后：仍开启的其它任务列在 detail 里，数量变成 6。
        self.assertNotIn("daily_summary", rows["maintenance"].detail)
        self.assertIn("6 个已开启", rows["maintenance"].detail)

    def test_missing_files_degrade(self) -> None:
        rows = featureview.collect_features(
            configedit.ConfigPaths.from_config(str(TEMP_ROOT / "empty" / "app.toml"))
        )
        self.assertEqual({row.key for row in rows} >= {"llm", "character_library", "hooks"}, True)


class CharacterPathsTestCase(ConfigCase):
    """角色库 / SOUL 路径来自 app.toml。"""

    def test_custom_roots_are_resolved_relative(self) -> None:
        configedit.patch_section(
            self.paths.config_path, "character_library", {"root": "my_chars"}
        )
        configedit.patch_section(self.paths.config_path, "soul", {"path": "prompts/SOUL.md"})
        paths = configedit.ConfigPaths.from_config(str(self.config_dir / "app.toml"))
        self.assertEqual(paths.character_root, str(self.config_dir / "my_chars"))
        self.assertEqual(paths.soul_path, str(self.config_dir / "prompts" / "SOUL.md"))

    def test_write_text_file_has_backup(self) -> None:
        soul = self.paths.soul_path
        original = Path(soul).read_text(encoding="utf-8")
        self.assertTrue(configedit.write_text_file(soul, "新的系统人设"))
        self.assertEqual(Path(soul).read_text(encoding="utf-8"), "新的系统人设\n")
        self.assertEqual((self.config_dir / "SOUL.md.gui.bak").read_text(encoding="utf-8"), original)
        self.assertFalse(configedit.write_text_file(soul, "新的系统人设"))


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
