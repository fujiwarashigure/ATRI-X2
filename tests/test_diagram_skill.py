"""draw.io 动作适配器（``drawio_pack/skills/drawio-skill/atrix_cli.py``）的离线测试。

全部通过**子进程**调用适配器（与 ``CommandTool`` 的真实调用方式一致：cwd = 技能根、
stdout 只认 JSON 信封），断言：

* 生成/校验/检索/文档这些**不需要 draw.io 渲染器**的动作可用且产物落在技能目录内；
* 图片/文件段路径是技能目录内相对路径（宿主媒体导入的前提）；
* 渲染器缺失时导出动作给可读错误而不是崩栈。

需要 draw.io 的用例只在 ``ATRIX_DRAWIO`` 指向真实可执行文件时跑（否则 skip 并写原因）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from atri_x.diagram import bootstrap

_REPO = Path(__file__).resolve().parent.parent

#: 最小 XML 图（两节点一边；不依赖上游示例文件）。
_DIAGRAM_XML = (
    '<mxfile host="app"><diagram name="P" id="p1"><mxGraphModel><root>'
    '<mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="2" value="API" style="rounded=1;fillColor=#dae8fc;" vertex="1" parent="1">'
    '<mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell>'
    '<mxCell id="3" value="DB" style="shape=cylinder3;fillColor=#d5e8d4;" vertex="1" parent="1">'
    '<mxGeometry x="40" y="200" width="120" height="60" as="geometry"/></mxCell>'
    '<mxCell id="4" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="2" target="3">'
    '<mxGeometry relative="1" as="geometry"/></mxCell>'
    '</root></mxGraphModel></diagram></mxfile>'
)

_SQL = (
    "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);\n"
    "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT REFERENCES users(id), total NUMERIC);\n"
)

_IR = {
    "schema": "drawio-skill/diagram-ir/v1",
    "metadata": {"title": "Checkout"},
    "nodes": [
        {"id": "client", "label": "Client", "kind": "actor"},
        {"id": "api", "label": "API", "kind": "service"},
        {"id": "db", "label": "DB", "kind": "database"},
    ],
    "edges": [
        {"from": "client", "to": "api", "label": "HTTPS"},
        {"from": "api", "to": "db", "label": "SQL"},
    ],
}


class _Workspace:
    """装一份技能到临时配置目录（冻结形态），供子进程调用。"""

    def __init__(self) -> None:
        # Windows 上杀软/索引器偶尔短暂占用刚生成的技能文件，清不掉时不要让清理
        # 报错掩盖真正的断言结果（目录在系统 temp 下，泄漏一次无害）。
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        result = bootstrap.ensure_installed(self.temp.name, frozen=True, allow_source=True)
        assert result.state == "installed", result.message
        self.skill = Path(result.skill_dir)
        self.adapter = self.skill / "atrix_cli.py"
        self.env = dict(os.environ)
        self.env["PYTHONUTF8"] = "1"
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 默认不暴露宿主机渲染器，保证"不需要 draw.io 的用例"在任何机器上行为一致。
        self.env.pop("ATRIX_DRAWIO", None)
        self.env["ATRIX_DRAWIO"] = str(self.skill / "no-such-drawio.exe")

    def close(self) -> None:
        self.temp.cleanup()

    def run(self, args: list[str], timeout: int = 240) -> dict:
        proc = subprocess.run(
            [sys.executable, str(self.adapter), *args],
            cwd=str(self.skill),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        self.last_stderr = proc.stderr.decode("utf-8", errors="replace")
        self.last_code = proc.returncode
        payload = json.loads(text) if text else {}
        assert isinstance(payload, dict), text[:500]
        return payload

    def media_path(self, payload: dict, kind: str = "image") -> Path:
        segment = next(
            (item for item in payload.get("segments") or [] if item.get("type") == kind),
            None,
        )
        assert segment is not None, payload
        rel = str(segment.get("path") or "")
        assert rel and not os.path.isabs(rel) and ".." not in rel, rel
        path = self.skill / rel
        assert path.is_file(), path
        return path


class DrawioAdapterOfflineTest(unittest.TestCase):
    """不需要 draw.io 渲染器的动作。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = _Workspace()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.close()

    def test_doctor_reports_skill_and_missing_renderer(self) -> None:
        payload = self.ws.run(["--action", "doctor"])
        content = str(payload.get("content") or "")
        self.assertIn("draw.io 技能自检", content)
        report = json.loads(content[content.index("{") : content.rindex("}") + 1])
        self.assertEqual(report.get("drawio_version"), "")
        self.assertFalse(report.get("render_available"))
        self.assertIn("build", report.get("actions") or [])

    def test_create_from_xml_without_renderer(self) -> None:
        payload = self.ws.run(
            ["--action", "create", "--content", _DIAGRAM_XML, "--output", "t-xml", "--format", "drawio"]
        )
        content = str(payload.get("content") or "")
        self.assertIn("已生成 .drawio", content)
        target = self.ws.media_path(payload, "file")
        self.assertTrue(target.read_text(encoding="utf-8").lstrip().startswith("<mxfile"))

    def test_create_from_ir_json(self) -> None:
        payload = self.ws.run(
            [
                "--action",
                "create",
                "--content",
                json.dumps(_IR),
                "--output",
                "t-ir",
                "--format",
                "drawio",
            ]
        )
        target = self.ws.media_path(payload, "file")
        text = target.read_text(encoding="utf-8")
        self.assertIn("<mxfile", text)
        self.assertIn("Client", text)

    def test_validate_reports_zero_errors(self) -> None:
        created = self.ws.run(
            ["--action", "create", "--content", _DIAGRAM_XML, "--output", "t-val", "--format", "drawio"]
        )
        target = self.ws.media_path(created, "file")
        payload = self.ws.run(["--action", "validate", "--source", str(target)])
        self.assertIn("0 error", str(payload.get("content") or ""))

    def test_build_from_inline_sql(self) -> None:
        payload = self.ws.run(
            [
                "--action",
                "build",
                "--content",
                _SQL,
                "--source-type",
                "sql",
                "--output",
                "t-sql",
                "--format",
                "drawio",
            ]
        )
        content = str(payload.get("content") or "")
        self.assertIn("已从 sql 源生成", content)
        target = self.ws.media_path(payload, "file")
        self.assertIn("<mxfile", target.read_text(encoding="utf-8"))

    def test_build_from_python_directory(self) -> None:
        codebase = _REPO / "drawio_pack" / "examples" / "architecture-studio" / "codebase"
        payload = self.ws.run(
            [
                "--action",
                "build",
                "--source",
                str(codebase),
                "--source-type",
                "python",
                "--group",
                "true",
                "--output",
                "t-code",
                "--format",
                "drawio",
            ]
        )
        content = str(payload.get("content") or "")
        self.assertIn("节点", content)
        target = self.ws.media_path(payload, "file")
        self.assertIn("<mxfile", target.read_text(encoding="utf-8"))

    def test_shapes_and_docs(self) -> None:
        shapes = self.ws.run(["--action", "shapes", "--keyword", "aws lambda"])
        self.assertIn("shape 检索", str(shapes.get("content") or ""))
        listing = self.ws.run(["--action", "docs"])
        self.assertIn("diagram-types.md", str(listing.get("content") or ""))
        doc = self.ws.run(["--action", "docs", "--doc", "diagram-types.md"])
        self.assertIn("# Diagram Type Presets", str(doc.get("content") or ""))

    def test_export_without_renderer_is_readable(self) -> None:
        payload = self.ws.run(
            ["--action", "export", "--source", str(self.ws.skill / "no-such.drawio")]
        )
        self.assertIn("需要 source", str(payload.get("content") or ""))
        created = self.ws.run(
            ["--action", "create", "--content", _DIAGRAM_XML, "--output", "t-exp", "--format", "drawio"]
        )
        target = self.ws.media_path(created, "file")
        payload = self.ws.run(["--action", "export", "--source", str(target), "--format", "png"])
        content = str(payload.get("content") or "")
        self.assertIn("未找到 draw.io 渲染器", content)
        # 渲染失败也仍然交付 .drawio 源文件
        self.ws.media_path(payload, "file")


class DrawioAdapterRenderTest(unittest.TestCase):
    """真实无头渲染（仅当本机/内置目录有 draw.io 时跑）。"""

    def test_render_png(self) -> None:
        env_drawio = os.environ.get("ATRIX_DRAWIO", "").strip()
        if not env_drawio or not Path(env_drawio).is_file():
            raise unittest.SkipTest("未设置 ATRIX_DRAWIO（本机无 draw.io 渲染器），跳过真实渲染")
        ws = _Workspace()
        try:
            ws.env["ATRIX_DRAWIO"] = env_drawio
            payload = ws.run(
                [
                    "--action",
                    "create",
                    "--content",
                    _DIAGRAM_XML,
                    "--output",
                    "t-png",
                    "--format",
                    "png",
                    "--scale",
                    "2",
                ],
                timeout=300,
            )
            self.assertIn("已导出", str(payload.get("content") or ""))
            image = ws.media_path(payload, "image")
            self.assertGreater(image.stat().st_size, 1024)
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
