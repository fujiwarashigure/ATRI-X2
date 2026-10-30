"""随包 draw.io 技能（``drawio_pack``）与安装引导（``atri_x.diagram``）的离线测试。

覆盖三层：

1. **随包资源本身**：``pack.json`` 摘要与 ``tool.json`` 参数/开关自洽，渲染后的
   ``ATRIX_SKILL.toml`` 能通过 ``agent_manifest`` 的**真实**校验（8 字段、required
   必须有 args 映射、parameters 必须是 object schema）；
2. **安装引导**：装进临时配置目录、幂等、源码模式跳过、技能文件齐全；
3. **渲染器定位**：``ATRIX_DRAWIO`` 覆盖与候选顺序（不启动真实 draw.io）。

测试不碰真实 ``%APPDATA%``：所有安装都指向 ``tempfile`` 目录，源码模式默认 ``skipped``。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from atri_x.diagram import bootstrap, pack, runner
from atri_x.diagram.paths import ENV_HOME, drawio_home
from atri_x.tool.skill.agent_manifest import parse_agent_skill_manifest

_REPO = Path(__file__).resolve().parent.parent
_PACK = _REPO / "drawio_pack"


class DrawioPackResourceTest(unittest.TestCase):
    """随包资源：位置、元数据、工具清单。"""

    def test_pack_root_and_meta(self) -> None:
        root = pack.pack_root()
        self.assertIsNotNone(root, "源码树里必须能找到 drawio_pack/")
        assert root is not None
        self.assertEqual(root.resolve(), _PACK.resolve())
        meta = pack.load_pack_meta(root)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.get("schema"), pack.PACK_SCHEMA)
        self.assertEqual(meta.get("skill_version"), "3.4.2")
        self.assertEqual((meta.get("renderer") or {}).get("version"), "32.6.1")
        self.assertRegex(str(meta.get("digest")), r"^[0-9a-f]{64}$")
        files = meta.get("files") or []
        self.assertEqual(len(files), meta.get("file_count"))
        self.assertGreater(len(files), 50)

    def test_pack_files_are_real_and_hashed(self) -> None:
        """清单里的每个路径都必须真实存在，且格式是 ``path\\tsize\\tsha256``。"""
        meta = pack.load_pack_meta()
        assert meta is not None
        root = pack.pack_root()
        assert root is not None
        for line in meta.get("files") or []:
            parts = str(line).split("\t")
            self.assertEqual(len(parts), 3, line)
            rel, size, digest = parts
            target = root / rel
            self.assertTrue(target.is_file(), rel)
            self.assertEqual(target.stat().st_size, int(size), rel)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("tool.json", "\n".join(str(line) for line in meta["files"]))

    def test_tool_meta_is_consistent(self) -> None:
        tool = pack.load_tool_meta()
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.get("tool"), "drawio")
        self.assertEqual(tool.get("risk"), "low")
        self.assertIs(tool.get("superadmin_only"), True, "1.1.5 起 draw.io 只给超管")
        parameters = tool.get("parameters") or {}
        self.assertEqual(parameters.get("type"), "object")
        properties = parameters.get("properties") or {}
        self.assertIn("action", properties)
        args = tool.get("args") or {}
        for name in args:
            self.assertIn(name, properties, f"args.{name} 不在 parameters.properties 里")
        for name in parameters.get("required") or []:
            self.assertIn(name, args, f"required 参数 {name} 没有 args 映射")

    def test_rendered_manifest_passes_repo_validator(self) -> None:
        """``render_skill_manifest`` 生成的文件必须能被 AgentSkill 清单校验器接受。"""
        tool = pack.load_tool_meta()
        assert tool is not None
        text = bootstrap.render_skill_manifest(
            tool,
            ["atri-x.exe", "-m", "atri_x.diagram.runner", "--skill-dir", "C:/tmp/skill"],
        )
        manifest = parse_agent_skill_manifest(text)
        self.assertTrue(manifest.callable)
        self.assertEqual(manifest.risk, "low")
        self.assertTrue(manifest.superadmin_only)
        self.assertEqual(manifest.tags, ["diagram", "drawio", "architecture", "visualization"])
        self.assertEqual(manifest.timeout_seconds, 300)
        self.assertEqual(manifest.args.get("action"), "--action")
        self.assertGreaterEqual(len(manifest.command), 5)
        self.assertIn("-m", manifest.command)
        self.assertIn("atri_x.diagram.runner", manifest.command)
        self.assertIn("--skill-dir", manifest.command)


class DrawioBootstrapTest(unittest.TestCase):
    """安装引导：源目录 → 配置目录（冻结形态），幂等，源码模式跳过。"""

    def test_source_mode_is_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = bootstrap.ensure_installed(temp)
            self.assertEqual(result.state, "skipped")
            self.assertFalse(result.ok)
            self.assertFalse((Path(temp) / "skills").exists())

    def test_install_and_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = bootstrap.ensure_installed(temp, frozen=True, allow_source=True)
            self.assertEqual(first.state, "installed", first.message)
            skill_dir = Path(first.skill_dir)
            for name in ("SKILL.md", "atrix_cli.py", "ATRIX_SKILL.toml", "scripts/diagramctl.py"):
                self.assertTrue((skill_dir / name).is_file(), name)
            manifest = parse_agent_skill_manifest((skill_dir / "ATRIX_SKILL.toml").read_text(encoding="utf-8"))
            self.assertTrue(manifest.superadmin_only)
            self.assertIn("drawio", first.commands)
            self.assertIn("atri_x.diagram.runner", " ".join(first.commands["drawio"]))
            install = json.loads((Path(temp) / "drawio" / "install.json").read_text(encoding="utf-8"))
            self.assertEqual(install.get("mode"), "bundled")
            self.assertEqual(install.get("skill_version"), "3.4.2")

            second = bootstrap.ensure_installed(temp, frozen=True, allow_source=True)
            self.assertEqual(second.state, "up-to-date", second.message)

            forced = bootstrap.ensure_installed(temp, frozen=True, allow_source=True, force=True)
            self.assertEqual(forced.state, "installed")

    def test_status_reports_pack_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bootstrap.ensure_installed(temp, frozen=True, allow_source=True)
            status = bootstrap.pack_status(temp)
            self.assertTrue(status["pack_available"])
            self.assertTrue(status["installed"])
            self.assertTrue(status["up_to_date"])
            self.assertEqual(status["tool"], "drawio")
            self.assertIn("skill_dir", status)


class DrawioPathsAndRendererTest(unittest.TestCase):
    """数据目录与渲染器定位规则（不启动真实 draw.io）。"""

    def test_drawio_home_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original = os.environ.get(ENV_HOME)
            os.environ[ENV_HOME] = temp
            try:
                self.assertEqual(drawio_home("C:/ignored"), os.path.abspath(temp))
            finally:
                if original is None:
                    os.environ.pop(ENV_HOME, None)
                else:
                    os.environ[ENV_HOME] = original

    def test_renderer_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "draw.io.exe"
            fake.write_bytes(b"stub")
            original = os.environ.get("ATRIX_DRAWIO")
            os.environ["ATRIX_DRAWIO"] = temp
            try:
                self.assertEqual(Path(pack.renderer_exe()).resolve(), fake.resolve())
                self.assertEqual(Path(pack.renderer_root()).resolve(), Path(temp).resolve())
            finally:
                if original is None:
                    os.environ.pop("ATRIX_DRAWIO", None)
                else:
                    os.environ["ATRIX_DRAWIO"] = original

    def test_renderer_missing_returns_empty_or_real_path(self) -> None:
        original = os.environ.pop("ATRIX_DRAWIO", None)
        try:
            found = pack.renderer_exe()
            self.assertIsInstance(found, str)
            if found:
                self.assertTrue(Path(found).is_file())
        finally:
            if original is not None:
                os.environ["ATRIX_DRAWIO"] = original


class DrawioRunnerTest(unittest.TestCase):
    """冻结入口的参数解析（``-m atri_x.diagram.runner`` 的前置开关）。"""

    def test_parse_skill_dir(self) -> None:
        skill, script, rest = runner.parse_target(
            ["--skill-dir", "C:/cfg/skills/agent/drawio_skill", "--action", "doctor"]
        )
        self.assertEqual(skill, "C:/cfg/skills/agent/drawio_skill")
        self.assertEqual(script, "")
        self.assertEqual(rest, ["--action", "doctor"])

    def test_parse_script_and_equals_form(self) -> None:
        skill, script, rest = runner.parse_target(
            ["--skill-dir=C:/cfg/skill", "--script", "C:/x/diagramctl.py", "build", "src"]
        )
        self.assertEqual(skill, "C:/cfg/skill")
        self.assertEqual(script, "C:/x/diagramctl.py")
        self.assertEqual(rest, ["build", "src"])

    def test_parse_target_requires_flag(self) -> None:
        skill, script, rest = runner.parse_target(["--action", "doctor"])
        self.assertEqual((skill, script, rest), ("", "", ["--action", "doctor"]))


if __name__ == "__main__":
    unittest.main()
