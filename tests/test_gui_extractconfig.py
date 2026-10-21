"""「模型 → 抓取服务」页的数据层：``[web_extract]`` 的读写、默认链、校验与密钥状态。

这些函数是"在界面上点保存"实际落盘的那一层，所以逐条断言：默认链、自定义通道、
删除后的小节清理、顺序落盘、非法名字不落盘、密钥状态只算不写。
"""

from __future__ import annotations

import os
import shutil
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from atri_x.gui import configedit, extractconfig

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_extractconfig"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")


class ExtractConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)
        for name in ASSET_FILES:
            shutil.copyfile(ASSETS / name, self.config_dir / name)
        self.paths = configedit.ConfigPaths.from_config(str(self.config_dir / "app.toml"))
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for name in ("JINA_API_KEY", "DEEPSEEK_SEARCH_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def parse(self) -> dict[str, Any]:
        text = (self.config_dir / "services.toml").read_text(encoding="utf-8")
        return tomllib.loads(text)


class LoadDefaultsCase(ExtractConfigCase):
    def test_absent_section_returns_default_chain(self) -> None:
        engines = extractconfig.load_engines(self.paths)
        self.assertEqual([view.name for view in engines], ["jina", "direct"])
        self.assertEqual(engines[0].protocol, "jina")
        self.assertEqual(engines[0].api_key_env, "JINA_API_KEY")

    def test_existing_section_is_loaded(self) -> None:
        configedit.patch_section(
            self.paths.services_path,
            "web_extract",
            {"engine_order": ["deepseek", "direct"]},
        )
        configedit.patch_section(
            self.paths.services_path,
            "web_extract.engines.deepseek",
            {"protocol": "deepseek", "api_key_env": "MY_KEY", "base_url": "https://relay"},
        )
        engines = extractconfig.load_engines(self.paths)
        self.assertEqual([view.name for view in engines], ["deepseek", "direct"])
        self.assertEqual(engines[0].protocol, "deepseek")
        self.assertEqual(engines[0].api_key_env, "MY_KEY")
        self.assertEqual(engines[0].base_url, "https://relay")
        self.assertEqual(engines[1].protocol, "direct")

    def test_builtin_name_without_protocol_infers_it(self) -> None:
        configedit.patch_section(
            self.paths.services_path, "web_extract", {"engine_order": ["direct"]}
        )
        engines = extractconfig.load_engines(self.paths)
        self.assertEqual(engines[0].protocol, "direct")
        self.assertEqual(engines[0].api_key_env, "")


class SaveCase(ExtractConfigCase):
    def test_saves_order_and_engine_sections(self) -> None:
        engines = [
            extractconfig.ExtractEngineView(name="deepseek", protocol="deepseek"),
            extractconfig.ExtractEngineView(name="direct", protocol="direct"),
        ]
        extractconfig.save_engines(self.paths, engines)
        data = self.parse()
        section = data["web_extract"]
        self.assertEqual(section["engine_order"], ["deepseek", "direct"])
        self.assertEqual(section["engines"]["deepseek"]["protocol"], "deepseek")
        self.assertEqual(
            section["engines"]["deepseek"]["api_key_env"], "DEEPSEEK_SEARCH_KEY"
        )
        self.assertNotIn("base_url", section["engines"]["deepseek"])
        self.assertNotIn("api_key_env", section["engines"]["direct"])

    def test_removed_engine_section_is_deleted(self) -> None:
        extractconfig.save_engines(
            self.paths,
            [
                extractconfig.ExtractEngineView(name="direct", protocol="direct"),
                extractconfig.ExtractEngineView(name="gone", protocol="jina"),
            ],
        )
        self.assertIn("gone", self.parse()["web_extract"]["engines"])
        extractconfig.save_engines(
            self.paths, [extractconfig.ExtractEngineView(name="direct", protocol="direct")]
        )
        data = self.parse()["web_extract"]
        self.assertEqual(data["engine_order"], ["direct"])
        self.assertNotIn("gone", data["engines"])

    def test_duplicate_names_are_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            extractconfig.save_engines(
                self.paths,
                [
                    extractconfig.ExtractEngineView(name="dup", protocol="direct"),
                    extractconfig.ExtractEngineView(name="dup", protocol="jina"),
                ],
            )

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            extractconfig.save_engines(
                self.paths, [extractconfig.ExtractEngineView(name="x", protocol="bing")]
            )

    def test_dotted_name_is_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            extractconfig.validate_engine_name("a.b")
        with self.assertRaises(configedit.ConfigEditError):
            extractconfig.validate_engine_name("")


class KeyStateCase(ExtractConfigCase):
    def test_reports_dotenv_value_with_mask(self) -> None:
        configedit.set_env_value(self.paths.env_path, "JINA_API_KEY", "jina-secret-123456")
        view = extractconfig.ExtractEngineView(
            name="jina", protocol="jina", api_key_env="JINA_API_KEY"
        )
        state = extractconfig.engine_key_state(self.paths, view)
        self.assertTrue(state.configured)
        self.assertTrue(state.in_dotenv)
        self.assertNotIn("jina-secret-123456", state.describe())

    def test_deepseek_falls_back_to_provider_key(self) -> None:
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_API_KEY", "sk-provider-123456")
        view = extractconfig.ExtractEngineView(
            name="deepseek", protocol="deepseek", api_key_env="DEEPSEEK_SEARCH_KEY"
        )
        state = extractconfig.engine_key_state(self.paths, view)
        self.assertTrue(state.configured)
        self.assertEqual(state.name, "DEEPSEEK_API_KEY")

    def test_direct_needs_no_key(self) -> None:
        view = extractconfig.ExtractEngineView(name="direct", protocol="direct")
        state = extractconfig.engine_key_state(self.paths, view)
        self.assertFalse(state.configured)

    def test_deepseek_protocol_is_offered_in_gui(self) -> None:
        self.assertIn("deepseek", extractconfig.PROTOCOL_LABELS)
        self.assertIn("DeepSeek", extractconfig.PROTOCOL_LABELS["deepseek"])
        self.assertIn(
            "tools/fetch",
            extractconfig.ExtractEngineView(name="deepseek", protocol="deepseek").base_placeholder(),
        )


if __name__ == "__main__":
    unittest.main()
