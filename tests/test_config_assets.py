"""内置配置资产的保真度测试。

对应上游 ``internal/config/assets.go`` 与 ``internal/config/config_test.go``
的资产相关断言。

**为什么需要这些测试**：``atri_x/config/assets_data/`` 下的文件由
``scripts/extract_assets.py`` 从 Go 源码机械抽取。抽象最容易出错的两种情形是

1. Go 的**常量拼接**（``defaultServicesTOML`` 就是 5 段原始字符串用 ``+`` 连接）
   被截断成只取第一段；
2. 抽到的正文其实不是合法 TOML。

这两点都无法从"脚本退出码为 0"看出来，因此在这里显式断言。
"""

from __future__ import annotations

import pathlib
import tomllib
import unittest

from atri_x.config import assets as A


class TestAssetInventory(unittest.TestCase):
    def test_asset_count_matches_upstream(self) -> None:
        # 上游 defaultConfigAssets 共 14 条；Python 版新增 1 条
        # ``reactions/catalog.json``（本地表情图库清单模板），共 15 条。
        self.assertEqual(len(A.DEFAULT_CONFIG_ASSETS), 15)

    def test_upstream_paths_present(self) -> None:
        paths = {a.path for a in A.DEFAULT_CONFIG_ASSETS}
        for expected in (
            "app.toml",
            "services.toml",
            "state.toml",
            "SOUL.md",
            "memories.toml",
            "atrinis.toml",
            "tool_tags.toml",
            "plugins/hooks.toml",
            "plugins/.env",
            "skills/agent/agent_skill_creator/SKILL.md",
            "skills/agent/agent_skill_creator/ATRIX_SKILL.toml",
            "skills/agent/write_atrix_hook/SKILL.md",
            "skills/agent/write_atrix_hook/ATRIX_SKILL.toml",
            ".env.example",
            "reactions/catalog.json",
        ):
            self.assertIn(expected, paths, f"缺少默认资产 {expected}")

    def test_default_config_dirs_match_upstream(self) -> None:
        self.assertEqual(
            set(A.DEFAULT_CONFIG_DIRS),
            {
                "skills",
                "skills/agent",
                "skills/go",
                "plugins",
                "long_memory",
                "reactions",
                "reactions/assets",
            },
        )

    def test_every_asset_has_non_empty_content(self) -> None:
        for asset in A.DEFAULT_CONFIG_ASSETS:
            with self.subTest(asset=asset.path):
                self.assertTrue(asset.content().strip(), f"{asset.path} 内容为空")


class TestTomlAssetsParse(unittest.TestCase):
    """所有 .toml 资产必须能被标准库 tomllib 解析。

    这是最强的低成本自检：常量拼接被截断或转义有误时通常直接解析失败。
    """

    def test_all_toml_assets_parse(self) -> None:
        checked = 0
        for asset in A.DEFAULT_CONFIG_ASSETS:
            if not asset.path.endswith(".toml"):
                continue
            with self.subTest(asset=asset.path):
                parsed = tomllib.loads(asset.content())
                self.assertIsInstance(parsed, dict)
            checked += 1
        self.assertGreaterEqual(checked, 6)


class TestServicesTomlConcatenation(unittest.TestCase):
    """``services.toml`` 由 5 段 Go 常量拼接而成 —— 必须完整。"""

    def setUp(self) -> None:
        self.text = A.DEFAULT_CONFIG_ASSETS[1].content() if False else A.default_config_asset("services.toml").content()  # type: ignore[union-attr]
        self.parsed = tomllib.loads(self.text)

    def test_size_in_expected_range(self) -> None:
        # 只取第一段原始字符串时约 1.x KB；完整拼接约 7.5 KB。
        # 用 4 KB 作为下界即可拦住"截断"这一类回归。
        self.assertGreater(len(self.text.encode("utf-8")), 4096)
        self.assertLess(len(self.text.encode("utf-8")), 20000)

    def test_contains_all_concatenated_sections(self) -> None:
        # 5 段常量各自的标题行必须都在
        for marker in (
            "Shared read-only service config",
            "Provider/model config. Do not commit real API keys.",
            "Optional image generation tool",
            "Optional image_to_prompt tool",
            "Optional automatic vision fallback",
            "Optional voice-message transcription",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_known_top_level_tables(self) -> None:
        self.assertIn("providers", self.parsed)
        self.assertIn("model_metadata", self.parsed)
        self.assertIn("image_generation", self.parsed)

    def test_has_at_least_two_providers(self) -> None:
        self.assertGreaterEqual(len(self.parsed["providers"]), 2)


class TestAppTomlDefaults(unittest.TestCase):
    """默认 app.toml 里的关键默认值。

    注意：这些值取自**实际抽取到的资产正文**（``assets.go`` 的工作树版本），
    而不是《ATRI-X Python/Go 研究报告》§1.2 引用的数值 ——
    那份报告写的是 ``release: v0.9.0`` 提交上的 ``assets.go:363,373,377``
    （``max_concurrent_turns = 4`` / ``queue_max_size = 0``），而当前工作树的
    ``assets.go`` 已被修改（``git status`` 显示为 M）。移植以工作树为准。
    """

    def setUp(self) -> None:
        self.text = A.default_config_asset("app.toml").content()  # type: ignore[union-attr]
        self.parsed = tomllib.loads(self.text)

    def test_parses_and_has_expected_tables(self) -> None:
        for table in (
            "config_files",
            "storage",
            "runtime",
            "maintenance",
            "llm_request",
            "context",
            "ops",
            "tools",
            "budget_limits",
            "security",
            "session",
            "platform",
        ):
            with self.subTest(table=table):
                self.assertIn(table, self.parsed)

    def test_timeouts(self) -> None:
        llm = self.parsed["llm_request"]
        self.assertEqual(llm["first_chunk_timeout_seconds"], 180)
        self.assertEqual(llm["stream_idle_timeout_seconds"], 60)
        # turn 端到端无限时
        self.assertEqual(llm["response_timeout_seconds"], 0)
        self.assertEqual(llm["max_retries"], 3)

    def test_tool_rounds(self) -> None:
        self.assertEqual(self.parsed["tools"]["max_rounds_per_turn"], 10)

    def test_ops_concurrency(self) -> None:
        ops = self.parsed["ops"]
        self.assertEqual(ops["max_concurrent_turns"], 10)
        self.assertEqual(ops["queue_max_size"], 20)
        self.assertIn("turn", ops["queue_wait_kinds"])
        self.assertEqual(ops["tool_timeout_seconds"], 600)
        self.assertEqual(ops["hook_timeout_seconds"], 60)
        self.assertEqual(ops["compress_timeout_seconds"], 300)

    def test_context_overflow_default_is_reject(self) -> None:
        self.assertEqual(self.parsed["context"]["overflow_mode"], "reject")

    def test_default_config_files_point_at_siblings(self) -> None:
        cfg = self.parsed["config_files"]
        self.assertEqual(cfg["services"], "services.toml")
        self.assertEqual(cfg["state"], "state.toml")
        self.assertEqual(cfg["atrinis"], "atrinis.toml")
        self.assertEqual(cfg["tool_tags"], "tool_tags.toml")

    def test_cli_platform_default(self) -> None:
        cli = self.parsed["platform"]["cli"]
        self.assertTrue(cli["enabled"])
        self.assertEqual(cli["default_client"], "local")


class TestSkillAssets(unittest.TestCase):
    def test_agent_skill_creator_md(self) -> None:
        md = A.default_config_asset("skills/agent/agent_skill_creator/SKILL.md").content()  # type: ignore[union-attr]
        self.assertTrue(md.startswith("---"), "SKILL.md 应以 YAML front matter 开头")
        self.assertGreater(len(md), 500)

    def test_skill_toml_assertions_from_upstream_test(self) -> None:
        # 直接来自 upstream internal/config/config_test.go 的断言
        creator = A.default_config_asset("skills/agent/agent_skill_creator/ATRIX_SKILL.toml").content()  # type: ignore[union-attr]
        self.assertIn('risk = "low"', creator)
        self.assertIn("superadmin_only = true", creator)

        hook_md = A.default_config_asset("skills/agent/write_atrix_hook/SKILL.md").content()  # type: ignore[union-attr]
        self.assertTrue(hook_md)

        hook_toml = A.default_config_asset("skills/agent/write_atrix_hook/ATRIX_SKILL.toml").content()  # type: ignore[union-attr]
        self.assertIn('risk = "low"', hook_toml)
        self.assertIn("superadmin_only = true", hook_toml)

    def test_skill_toml_parses(self) -> None:
        for path in (
            "skills/agent/agent_skill_creator/ATRIX_SKILL.toml",
            "skills/agent/write_atrix_hook/ATRIX_SKILL.toml",
        ):
            with self.subTest(path=path):
                asset = A.default_config_asset(path)
                self.assertIsNotNone(asset)
                tomllib.loads(asset.content())  # type: ignore[union-attr]


class TestEncodingHygiene(unittest.TestCase):
    def test_no_bom_and_lf_only(self) -> None:
        data_dir = A.DATA_DIR
        for path in sorted(data_dir.rglob("*")):
            if not path.is_file():
                continue
            with self.subTest(path=str(path.relative_to(data_dir))):
                raw = path.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "资产不得带 UTF-8 BOM")
                self.assertNotIn(b"\r\n", raw, "资产行尾必须是 LF")
                raw.decode("utf-8")

    def test_every_declared_asset_file_exists(self) -> None:
        for asset in A.DEFAULT_CONFIG_ASSETS:
            with self.subTest(asset=asset.path):
                self.assertTrue((A.DATA_DIR / asset.path).is_file())


class TestEnsurePlatformDefaults(unittest.TestCase):
    def test_writes_missing_assets_and_keeps_existing(self) -> None:
        import shutil

        # 不用 tempfile.TemporaryDirectory：本机沙箱 tmp 目录的 ACL 会让它的
        # 清理钩子抛 PermissionError（WinError 5）。改用仓库内的临时目录。
        root = pathlib.Path(__file__).resolve().parent / "_tmp_ensure_defaults"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            # 预先写一个 app.toml，必须**不被覆盖**
            (root / "app.toml").write_text("sentinel = true\n", encoding="utf-8")
            A.ensure_platform_defaults(root)
            self.assertEqual((root / "app.toml").read_text(encoding="utf-8"), "sentinel = true\n")
            for rel in ("services.toml", "state.toml", "SOUL.md", "plugins/hooks.toml"):
                self.assertTrue((root / rel).is_file(), f"{rel} 未被创建")
            for rel in A.DEFAULT_CONFIG_DIRS:
                self.assertTrue((root / rel).is_dir(), f"{rel} 目录未被创建")
            # 幂等：第二次调用不改变已有内容
            before = (root / "state.toml").read_text(encoding="utf-8")
            A.ensure_platform_defaults(root)
            self.assertEqual((root / "state.toml").read_text(encoding="utf-8"), before)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
