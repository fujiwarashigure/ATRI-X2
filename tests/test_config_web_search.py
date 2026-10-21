"""``services.toml`` 的 ``[web_search]`` / ``[web_extract]`` 段：解析、缺省与合并进 AppConfig。

临时目录用仓库根 ``tmp_cfg_test/``（本机沙箱下 ``tempfile.TemporaryDirectory()`` 的清理
会抛 PermissionError），``tearDown`` 里收干净，不在仓库根留下空目录。
"""

from __future__ import annotations

import pathlib
import shutil
import unittest

from atri_x.config.app import default_app_config
from atri_x.config.app_sections import ConfigDecodeError
from atri_x.config.services import load_services_config

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tmp_cfg_test"

WEB_SEARCH_TOML = """
[web_search]
engine_order = ["tavily", "firecrawl", "my-search"]

[web_search.engines.tavily]
api_key_env = "TAVILY_API_KEY"

[web_search.engines.firecrawl]
protocol = "firecrawl"
base_url = "https://relay.example.com/v2/search"

[web_search.engines.my-search]
protocol = "searxng"
base_url = "https://searx.example.com"
"""


class WebSearchServicesConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = ROOT / "web_search"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            ROOT.rmdir()
        except OSError:
            pass

    def _write(self, text: str) -> str:
        path = self.dir / "services.toml"
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)

    def test_absent_section_is_none(self) -> None:
        services = load_services_config(self._write(SERVICES_WITHOUT_WEB_SEARCH))
        self.assertIsNone(services.web_search)

    def test_parses_order_and_engines(self) -> None:
        services = load_services_config(self._write(WEB_SEARCH_TOML))
        config = services.web_search
        assert config is not None
        self.assertEqual(config.engine_order, ["tavily", "firecrawl", "my-search"])
        self.assertEqual(config.engines["tavily"].api_key_env, "TAVILY_API_KEY")
        self.assertEqual(config.engines["firecrawl"].base_url, "https://relay.example.com/v2/search")
        self.assertEqual(config.engines["my-search"].protocol, "searxng")
        self.assertEqual(config.engines["my-search"].base_url, "https://searx.example.com")

    def test_unknown_key_is_rejected(self) -> None:
        text = WEB_SEARCH_TOML + "\n[web_search.engines.extra]\nnot_a_real_key = 1\n"
        with self.assertRaises(ConfigDecodeError):
            load_services_config(self._write(text))

    def test_merges_into_app_config(self) -> None:
        services = load_services_config(self._write(WEB_SEARCH_TOML))
        cfg = default_app_config()
        cfg.merge_services(services)
        self.assertEqual(cfg.web_search.engine_order, ["tavily", "firecrawl", "my-search"])
        self.assertEqual(cfg.web_search.engines["tavily"].api_key_env, "TAVILY_API_KEY")

    def test_absent_section_keeps_default_chain(self) -> None:
        cfg = default_app_config()
        cfg.merge_services(load_services_config(self._write(SERVICES_WITHOUT_WEB_SEARCH)))
        self.assertEqual(cfg.web_search.engine_order, [])
        self.assertEqual(cfg.web_search.engines, {})


SERVICES_WITHOUT_WEB_SEARCH = """
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
"""

WEB_EXTRACT_TOML = """
[web_extract]
engine_order = ["deepseek", "jina", "direct"]

[web_extract.engines.deepseek]
protocol = "deepseek"
api_key_env = "DEEPSEEK_SEARCH_KEY"

[web_extract.engines.jina]
protocol = "jina"
api_key_env = "JINA_API_KEY"
base_url = "https://r.example.com/"

[web_extract.engines.direct]
protocol = "direct"
"""


class WebExtractServicesConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = ROOT / "web_extract"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            ROOT.rmdir()
        except OSError:
            pass

    def _write(self, text: str) -> str:
        path = self.dir / "services.toml"
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)

    def test_absent_section_is_none(self) -> None:
        services = load_services_config(self._write(SERVICES_WITHOUT_WEB_SEARCH))
        self.assertIsNone(services.web_extract)

    def test_parses_order_and_engines(self) -> None:
        services = load_services_config(self._write(WEB_EXTRACT_TOML))
        config = services.web_extract
        assert config is not None
        self.assertEqual(config.engine_order, ["deepseek", "jina", "direct"])
        self.assertEqual(config.engines["deepseek"].api_key_env, "DEEPSEEK_SEARCH_KEY")
        self.assertEqual(config.engines["jina"].base_url, "https://r.example.com/")
        self.assertEqual(config.engines["direct"].protocol, "direct")

    def test_unknown_key_is_rejected(self) -> None:
        text = WEB_EXTRACT_TOML + "\n[web_extract.engines.extra]\nnot_a_real_key = 1\n"
        with self.assertRaises(ConfigDecodeError):
            load_services_config(self._write(text))

    def test_merges_into_app_config(self) -> None:
        services = load_services_config(self._write(WEB_EXTRACT_TOML))
        cfg = default_app_config()
        cfg.merge_services(services)
        self.assertEqual(cfg.web_extract.engine_order, ["deepseek", "jina", "direct"])
        self.assertEqual(
            cfg.web_extract.engines["deepseek"].api_key_env, "DEEPSEEK_SEARCH_KEY"
        )

    def test_absent_section_keeps_default_chain(self) -> None:
        cfg = default_app_config()
        cfg.merge_services(load_services_config(self._write(SERVICES_WITHOUT_WEB_SEARCH)))
        self.assertEqual(cfg.web_extract.engine_order, [])
        self.assertEqual(cfg.web_extract.engines, {})


if __name__ == "__main__":
    unittest.main()
