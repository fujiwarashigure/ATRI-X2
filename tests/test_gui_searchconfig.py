"""「模型 → 搜索服务」页的数据层：``[web_search]`` 的读写、默认链、校验与密钥状态。

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

from atri_x.gui import configedit, searchconfig

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_searchconfig"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")


class SearchConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)
        for name in ASSET_FILES:
            shutil.copyfile(ASSETS / name, self.config_dir / name)
        self.paths = configedit.ConfigPaths.from_config(str(self.config_dir / "app.toml"))
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for name in (
            "TAVILY_API_KEY",
            "FIRECRAWL_API_KEY",
            "KEENABLE_API_KEY",
            "EXA_API_KEY",
            "DEEPSEEK_SEARCH_KEY",
            "DEEPSEEK_API_KEY",
        ):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def parse(self) -> dict[str, Any]:
        text = (self.config_dir / "services.toml").read_text(encoding="utf-8")
        return tomllib.loads(text)


class LoadDefaultsCase(SearchConfigCase):
    def test_absent_section_returns_default_chain(self) -> None:
        engines = searchconfig.load_engines(self.paths)
        self.assertEqual(
            [view.name for view in engines],
            ["deepseek", "tavily", "firecrawl", "keenable", "exa"],
        )
        self.assertEqual(engines[0].protocol, "deepseek")
        self.assertEqual(engines[0].api_key_env, "DEEPSEEK_SEARCH_KEY")
        self.assertEqual(engines[1].protocol, "tavily")
        self.assertEqual(engines[1].api_key_env, "TAVILY_API_KEY")

    def test_existing_section_is_loaded(self) -> None:
        configedit.patch_section(
            self.paths.services_path,
            "web_search",
            {"engine_order": ["keenable", "mine"]},
        )
        configedit.patch_section(
            self.paths.services_path,
            "web_search.engines.mine",
            {"protocol": "searxng", "base_url": "https://s.example.com"},
        )
        engines = searchconfig.load_engines(self.paths)
        self.assertEqual([view.name for view in engines], ["keenable", "mine"])
        self.assertEqual(engines[0].protocol, "keenable")
        self.assertEqual(engines[1].protocol, "searxng")
        self.assertEqual(engines[1].base_url, "https://s.example.com")

    def test_builtin_name_without_protocol_infers_it(self) -> None:
        configedit.patch_section(
            self.paths.services_path, "web_search", {"engine_order": ["firecrawl"]}
        )
        engines = searchconfig.load_engines(self.paths)
        self.assertEqual(engines[0].protocol, "firecrawl")
        self.assertEqual(engines[0].api_key_env, "FIRECRAWL_API_KEY")


class SaveCase(SearchConfigCase):
    def test_saves_order_and_engine_sections(self) -> None:
        engines = [
            searchconfig.SearchEngineView(name="tavily", protocol="tavily"),
            searchconfig.SearchEngineView(
                name="mine",
                protocol="searxng",
                base_url="https://s.example.com",
                api_key_env="MY_SEARCH_KEY",
            ),
        ]
        searchconfig.save_engines(self.paths, engines)
        data = self.parse()
        section = data["web_search"]
        self.assertEqual(section["engine_order"], ["tavily", "mine"])
        self.assertEqual(section["engines"]["tavily"]["protocol"], "tavily")
        self.assertEqual(section["engines"]["tavily"]["api_key_env"], "TAVILY_API_KEY")
        self.assertEqual(section["engines"]["mine"]["base_url"], "https://s.example.com")
        self.assertEqual(section["engines"]["mine"]["api_key_env"], "MY_SEARCH_KEY")

    def test_empty_optional_fields_are_omitted(self) -> None:
        searchconfig.save_engines(
            self.paths, [searchconfig.SearchEngineView(name="firecrawl", protocol="firecrawl")]
        )
        entry = self.parse()["web_search"]["engines"]["firecrawl"]
        self.assertNotIn("base_url", entry)
        self.assertNotIn("bin", entry)

    def test_removed_engine_section_is_deleted(self) -> None:
        searchconfig.save_engines(
            self.paths,
            [
                searchconfig.SearchEngineView(name="firecrawl", protocol="firecrawl"),
                searchconfig.SearchEngineView(name="gone", protocol="searxng", base_url="https://x"),
            ],
        )
        self.assertIn("gone", self.parse()["web_search"]["engines"])
        searchconfig.save_engines(
            self.paths, [searchconfig.SearchEngineView(name="firecrawl", protocol="firecrawl")]
        )
        data = self.parse()["web_search"]
        self.assertEqual(data["engine_order"], ["firecrawl"])
        self.assertNotIn("gone", data["engines"])

    def test_duplicate_names_are_rejected(self) -> None:
        engines = [
            searchconfig.SearchEngineView(name="dup", protocol="firecrawl"),
            searchconfig.SearchEngineView(name="dup", protocol="keenable"),
        ]
        with self.assertRaises(configedit.ConfigEditError):
            searchconfig.save_engines(self.paths, engines)

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            searchconfig.save_engines(
                self.paths, [searchconfig.SearchEngineView(name="x", protocol="bing")]
            )

    def test_dotted_name_is_rejected(self) -> None:
        with self.assertRaises(configedit.ConfigEditError):
            searchconfig.validate_engine_name("a.b")
        with self.assertRaises(configedit.ConfigEditError):
            searchconfig.validate_engine_name("")


class KeyStateCase(SearchConfigCase):
    def test_reports_dotenv_value_with_mask(self) -> None:
        configedit.set_env_value(self.paths.env_path, "TAVILY_API_KEY", "tvly-secret-123456")
        view = searchconfig.SearchEngineView(name="tavily", protocol="tavily", api_key_env="TAVILY_API_KEY")
        state = searchconfig.engine_key_state(self.paths, view)
        self.assertTrue(state.configured)
        self.assertTrue(state.in_dotenv)
        self.assertNotIn("tvly-secret-123456", state.describe())

    def test_missing_key_is_not_configured(self) -> None:
        view = searchconfig.SearchEngineView(name="exa", protocol="exa", api_key_env="EXA_API_KEY")
        state = searchconfig.engine_key_state(self.paths, view)
        self.assertFalse(state.configured)

    def test_deepseek_falls_back_to_provider_key(self) -> None:
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_API_KEY", "sk-provider-123456")
        view = searchconfig.SearchEngineView(
            name="deepseek", protocol="deepseek", api_key_env="DEEPSEEK_SEARCH_KEY"
        )
        state = searchconfig.engine_key_state(self.paths, view)
        self.assertTrue(state.configured)
        self.assertEqual(state.name, "DEEPSEEK_API_KEY")

    def test_deepseek_search_key_wins(self) -> None:
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_API_KEY", "sk-provider-123456")
        configedit.set_env_value(self.paths.env_path, "DEEPSEEK_SEARCH_KEY", "sk-search-654321")
        view = searchconfig.SearchEngineView(
            name="deepseek", protocol="deepseek", api_key_env="DEEPSEEK_SEARCH_KEY"
        )
        state = searchconfig.engine_key_state(self.paths, view)
        self.assertTrue(state.configured)
        self.assertEqual(state.name, "DEEPSEEK_SEARCH_KEY")

    def test_deepseek_protocol_is_offered_in_gui(self) -> None:
        self.assertIn("deepseek", searchconfig.PROTOCOL_LABELS)
        self.assertIn("DeepSeek", searchconfig.PROTOCOL_LABELS["deepseek"])
        self.assertIn("tools/search", searchconfig.SearchEngineView(
            name="deepseek", protocol="deepseek"
        ).base_placeholder())


if __name__ == "__main__":
    unittest.main()
