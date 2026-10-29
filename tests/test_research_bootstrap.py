"""随包研究引擎（``research_pack``）与首次启动引导（``atri_x.research``）的离线测试。

覆盖三层：

1. **随包资源本身**：``research_pack/pack.json``、15 个技能文档、12 个硬工具清单
   与引擎 schema 同源（清单里的参数/args 必须能通过 ``agent_manifest`` 校验）；
2. **安装引导**：装进临时配置目录、幂等、升级、尊重外部安装（``install.py`` 的 venv 版）、
   失败不抛异常；
3. **子进程入口**：``atri_x.research.runner`` 的 ``--home`` 解析与引擎分发
   （用假的 ``atrix_research`` 包，不依赖真实引擎导入）。

这些测试**不碰**真实 ``%APPDATA%``：所有安装都指向 ``tempfile`` 目录，且
``ensure_installed`` 的源码模式默认返回 ``skipped``。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from atri_x.research import bootstrap, pack
from atri_x.research.paths import research_home
from atri_x.tool.skill.agent_manifest import parse_agent_skill_manifest

_REPO = Path(__file__).resolve().parent.parent
_PACK = _REPO / "research_pack"
_TOOLS = 12
_SKILLS = 15


def _clear_engine_modules() -> None:
    """清掉测试里从临时目录导入的假引擎，避免污染其它用例。"""
    for name in list(sys.modules):
        if name == "atrix_research" or name.startswith("atrix_research."):
            del sys.modules[name]


class ResearchPackTest(unittest.TestCase):
    """随包资源：位置、元数据、技能文档、清单数据。"""

    def test_pack_root_and_meta(self) -> None:
        root = pack.pack_root()
        self.assertIsNotNone(root, "源码树里必须能找到 research_pack/")
        assert root is not None
        self.assertEqual(root.resolve(), _PACK.resolve())
        meta = pack.load_pack_meta(root)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.get("schema"), pack.PACK_SCHEMA)
        self.assertEqual(meta.get("engine_version"), "0.2.0")
        self.assertRegex(str(meta.get("digest")), r"^[0-9a-f]{64}$")
        self.assertEqual(len(meta.get("skills") or []), _SKILLS)

    def test_engine_entry_present(self) -> None:
        root = pack.pack_root()
        assert root is not None
        self.assertTrue((root / "engine" / "run.py").is_file())
        self.assertTrue((root / "engine" / "atrix_research" / "__init__.py").is_file())
        self.assertTrue((root / "pack.json").is_file())
        self.assertTrue((root / "settings.json").is_file())

    def test_skill_docs_and_manifests(self) -> None:
        assert _PACK.is_dir()
        skills = sorted(path.name for path in (_PACK / "skills").iterdir() if path.is_dir())
        self.assertEqual(len(skills), _SKILLS)
        manifests = 0
        for name in skills:
            doc = (_PACK / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("hardtool-contract:begin", doc, f"{name} 缺工具契约附录")
            manifest_path = _PACK / "skills" / name / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifests += 1
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("schema"), "atrix-research/manifest/1")
            self.assertEqual(manifest.get("skill"), name)
            self.assertEqual(manifest.get("risk"), "low")
            parameters = manifest.get("parameters") or {}
            self.assertEqual(parameters.get("type"), "object")
            properties = parameters.get("properties") or {}
            args = manifest.get("args") or {}
            self.assertTrue(args, f"{name} 的 args 不能为空")
            for key, flag in args.items():
                self.assertIn(key, properties, f"{name}.args.{key} 不在 parameters.properties 里")
                self.assertTrue(flag.startswith("--"), f"{name}.args.{key} 应为命令行开关")
        self.assertEqual(manifests, _TOOLS)

    def test_manifest_renders_valid_atrix_skill_toml(self) -> None:
        manifest_path = _PACK / "skills" / "research_search" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        command = ["atri-x.exe", "-m", "atri_x.research.runner", "--home", "C:/x", "search"]
        text = bootstrap.render_skill_manifest(manifest, command)
        parsed = parse_agent_skill_manifest(text.encode("utf-8"))
        self.assertTrue(parsed.callable)
        self.assertEqual(parsed.risk, "low")
        self.assertEqual(parsed.command, command)
        self.assertEqual(parsed.timeout_seconds, 240)
        self.assertEqual(parsed.args.get("queries"), "--queries")
        # 清单里的每个 required 参数都必须有 args 映射（agent_manifest 的硬要求）
        for name in (manifest["parameters"].get("required") or []):
            self.assertIn(name, parsed.args)


class BootstrapInstallTest(unittest.TestCase):
    """安装引导：装、幂等、升级、尊重外部安装、状态快照。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atrix-research-")
        self.config_dir = Path(self._tmp.name) / "ATRI-X"
        self.home = self.config_dir / "research"
        self.addCleanup(self._tmp.cleanup)

    def _install(self, **kwargs: object) -> bootstrap.BootstrapResult:
        return bootstrap.ensure_installed(
            str(self.config_dir), frozen=True, force=False, **kwargs
        )

    def test_install_creates_engine_skills_and_manifest(self) -> None:
        result = self._install()
        self.assertEqual(result.state, "installed", result.message)
        self.assertEqual(result.engine_version, "0.2.0")
        self.assertEqual(len(result.skills), _SKILLS)
        self.assertEqual(len(result.commands), _TOOLS)
        self.assertTrue((self.home / "engine" / "atrix_research" / "__init__.py").is_file())
        self.assertTrue((self.home / "settings.json").is_file())
        install = json.loads((self.home / "install.json").read_text(encoding="utf-8"))
        self.assertEqual(install["schema"], bootstrap.INSTALL_SCHEMA)
        self.assertEqual(install["mode"], bootstrap.MODE_BUNDLED)
        self.assertEqual(install["command_mode"], "frozen")
        self.assertEqual(len(install["skills"]), _SKILLS)

        skills_dir = self.config_dir / "skills" / "agent"
        docs = sorted(path.name for path in skills_dir.iterdir() if (path / "SKILL.md").is_file())
        self.assertEqual(len(docs), _SKILLS)
        toml_count = 0
        for name in docs:
            toml_path = skills_dir / name / "ATRIX_SKILL.toml"
            if not toml_path.is_file():
                continue
            toml_count += 1
            manifest = parse_agent_skill_manifest(toml_path.read_bytes())
            self.assertTrue(manifest.callable)
            self.assertEqual(manifest.risk, "low")
            self.assertEqual(manifest.command[1:3], ["-m", "atri_x.research.runner"])
            self.assertIn("--home", manifest.command)
            self.assertIn(str(self.home), manifest.command)
        self.assertEqual(toml_count, _TOOLS)

    def test_second_run_is_up_to_date(self) -> None:
        self.assertEqual(self._install().state, "installed")
        first = (self.home / "install.json").read_text(encoding="utf-8")
        second = self._install()
        self.assertEqual(second.state, "up-to-date", second.message)
        self.assertEqual((self.home / "install.json").read_text(encoding="utf-8"), first)

    def test_force_reinstalls(self) -> None:
        self.assertEqual(self._install().state, "installed")
        forced = bootstrap.ensure_installed(str(self.config_dir), frozen=True, force=True)
        self.assertEqual(forced.state, "installed")

    def test_external_install_is_respected(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        external = {
            "schema": "atrix-research/install/1",
            "engine_version": "0.1.0",
            "venv_python": str(self.home / "venv" / "Scripts" / "python.exe"),
        }
        (self.home / "install.json").write_text(
            json.dumps(external), encoding="utf-8"
        )
        result = self._install()
        self.assertEqual(result.state, "external")
        self.assertFalse((self.home / "engine").exists(), "外部安装时不应写引擎目录")
        self.assertFalse((self.config_dir / "skills" / "agent").exists())

    def test_digest_change_triggers_upgrade(self) -> None:
        self.assertEqual(self._install().state, "installed")
        manifest_path = self.home / "install.json"
        stale = json.loads(manifest_path.read_text(encoding="utf-8"))
        stale["pack_digest"] = "0" * 64
        manifest_path.write_text(json.dumps(stale), encoding="utf-8")
        upgraded = self._install()
        self.assertEqual(upgraded.state, "installed")
        fresh = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotEqual(fresh["pack_digest"], "0" * 64)

    def test_source_mode_is_skipped_by_default(self) -> None:
        result = bootstrap.ensure_installed(str(self.config_dir), frozen=False)
        self.assertEqual(result.state, "skipped")
        self.assertFalse(self.home.exists())

    def test_allow_source_uses_current_interpreter(self) -> None:
        result = bootstrap.ensure_installed(
            str(self.config_dir), frozen=False, allow_source=True
        )
        self.assertEqual(result.state, "installed")
        command = result.commands["search"]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("run.py"))
        self.assertIn("--home", command)

    def test_missing_pack_reports_unavailable(self) -> None:
        original = bootstrap.pack_root
        bootstrap.pack_root = lambda: None  # type: ignore[assignment]
        try:
            result = bootstrap.ensure_installed(str(self.config_dir), frozen=True)
        finally:
            bootstrap.pack_root = original  # type: ignore[assignment]
        self.assertEqual(result.state, "unavailable")
        self.assertFalse(self.home.exists())

    def test_pack_status_before_and_after(self) -> None:
        before = bootstrap.pack_status(str(self.config_dir))
        self.assertTrue(before["pack_available"])
        self.assertFalse(before["installed"])
        self.assertEqual(before["pack_engine_version"], "0.2.0")
        self._install()
        after = bootstrap.pack_status(str(self.config_dir))
        self.assertTrue(after["installed"])
        self.assertTrue(after["up_to_date"])
        self.assertTrue(after["engine_present"])


class RunnerTest(unittest.TestCase):
    """``atri_x.research.runner``：``--home`` 解析与引擎分发（用假引擎）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atrix-research-runner-")
        self.home = Path(self._tmp.name) / "home"
        self.engine = self.home / "engine" / "atrix_research"
        self.engine.mkdir(parents=True, exist_ok=True)
        (self.engine / "__init__.py").write_text('VERSION = "test"\n', encoding="utf-8")
        self.log = Path(self._tmp.name) / "argv.json"
        (self.engine / "cli.py").write_text(
            "import json, os\n"
            "def main(argv=None):\n"
            "    with open(os.environ['RUNNER_TEST_LOG'], 'w', encoding='utf-8') as fh:\n"
            "        json.dump(list(argv or []), fh)\n"
            "    return 0\n",
            encoding="utf-8",
        )
        self._old_path = list(sys.path)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sys.path[:] = self._old_path
        _clear_engine_modules()

    def test_parse_target_leading_flags(self) -> None:
        home, config_dir, rest = bootstrap_runner_parse(
            ["--home", "C:/x", "--config-dir", "C:/cfg", "plan", "--action", "status"]
        )
        self.assertEqual(home, os.path.abspath("C:/x"))
        self.assertEqual(config_dir, "C:/cfg")
        self.assertEqual(rest, ["plan", "--action", "status"])

    def test_parse_target_home_equals_form(self) -> None:
        home, _config, rest = bootstrap_runner_parse(["--home=C:/y", "doctor"])
        self.assertEqual(home, os.path.abspath("C:/y"))
        self.assertEqual(rest, ["doctor"])

    def test_runner_dispatches_to_engine(self) -> None:
        from atri_x.research import runner

        os.environ["RUNNER_TEST_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "RUNNER_TEST_LOG", None)
        code = runner.main(["--home", str(self.home), "plan", "--action", "status"])
        self.assertEqual(code, 0)
        argv = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(argv, ["--home", str(self.home), "plan", "--action", "status"])

    def test_runner_reports_missing_engine(self) -> None:
        from atri_x.research import runner

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = runner.main(["--home", str(Path(self._tmp.name) / "nope"), "version"])
        self.assertEqual(code, 3)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ENGINE_MISSING")


def bootstrap_runner_parse(argv: list[str]):
    """``runner.parse_target`` 的薄包装（避免在类体内再用相对导入）。"""
    from atri_x.research.runner import parse_target

    return parse_target(argv)


class PathsTest(unittest.TestCase):
    """研究目录解析：唯一出处（命令 / GUI / 子进程共用）。"""

    def test_explicit_config_dir(self) -> None:
        self.assertEqual(research_home("C:/ATRI-X"), os.path.abspath("C:/ATRI-X/research"))

    def test_env_override_wins(self) -> None:
        old = os.environ.get("ATRIX_RESEARCH_HOME")
        os.environ["ATRIX_RESEARCH_HOME"] = "C:/custom-research"
        self.addCleanup(self._restore_env, old)
        self.assertEqual(research_home("C:/ATRI-X"), os.path.abspath("C:/custom-research"))

    def test_env_override_empty_falls_back(self) -> None:
        old = os.environ.get("ATRIX_RESEARCH_HOME")
        os.environ["ATRIX_RESEARCH_HOME"] = "  "
        self.addCleanup(self._restore_env, old)
        self.assertTrue(research_home("C:/ATRI-X").endswith(os.path.join("ATRI-X", "research")))

    @staticmethod
    def _restore_env(old: str | None) -> None:
        if old is None:
            os.environ.pop("ATRIX_RESEARCH_HOME", None)
        else:
            os.environ["ATRIX_RESEARCH_HOME"] = old


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
