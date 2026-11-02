"""沙箱与工具层接线测试：shell 走沙箱、文件路径钉进租户空间、``/panic`` 停环境。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.sandbox import runtime as sandbox_runtime
from atri_x.sandbox.errors import SandboxUnavailableError
from atri_x.sandbox.manager import ShellOutcome
from atri_x.tool import CallRequest
from atri_x.tool.builtin.shell import ShellTool
from atri_x.tool.builtin.workspace import WorkspaceTool
from atri_x.tool.tool import RiskLow
from atri_x.tool.workspace import (
    PathResolveOptions,
    current_workspace_dir,
    resolve_workspace_path,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_sandbox_tools_test")


class FakeSandbox:
    """最小沙箱服务替身（工具层只依赖这几个方法）。"""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.workspace = os.path.join(TMP_ROOT, "workspace")
        os.makedirs(self.workspace, exist_ok=True)
        self.host_called = False

    def tenant_workspace(self, ctx: object = None) -> str:
        return self.workspace

    def resolve_path(self, ctx: object, raw: str, **kwargs: object) -> str:
        self.calls.append(("resolve", raw))
        return os.path.join(self.workspace, raw.strip("/\\"))

    async def run_shell(self, ctx: object, cmd: str, **kwargs: object) -> ShellOutcome:
        self.calls.append(("run", cmd, kwargs))
        return ShellOutcome(stdout="sandboxed\n", stderr="", exit_code=0, tenant="local")

    async def import_media(self, ctx: object, source: str, **kwargs: object) -> str:
        self.calls.append(("import", source))
        return "/workspace/imports/media.bin"

    async def reset(self, ctx: object = None, *, mode: str = "rebuild") -> dict:
        self.calls.append(("reset", mode))
        return {"message": "已重建", "mode": mode}

    async def stop_all(self, reason: str = "panic") -> int:
        self.calls.append(("stop_all", reason))
        return 2


def _call(name: str, arguments: dict) -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


class TestShellRouting(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.sandbox = FakeSandbox()
        sandbox_runtime.set_service(self.sandbox)

    def tearDown(self) -> None:
        sandbox_runtime.clear_service()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_risk_is_low_inside_sandbox(self) -> None:
        async def scenario() -> None:
            tool = ShellTool()
            assessment = await tool.assess_risk({}, _call("shell", {"cmd": "rm -rf /tmp/x"}))
            self.assertEqual(assessment.level, RiskLow)

        asyncio.run(scenario())

    def test_call_routes_to_sandbox_not_host(self) -> None:
        async def scenario() -> None:
            tool = ShellTool()
            result = await tool.call({}, _call("shell", {"cmd": "echo hi"}))
            self.assertIn("sandboxed", result.content)
            runs = [item for item in self.sandbox.calls if item[0] == "run"]
            self.assertEqual(len(runs), 1)
            self.assertFalse(self.sandbox.host_called)

        asyncio.run(scenario())

    def test_preflight_allows_cd_in_sandbox(self) -> None:
        async def scenario() -> None:
            tool = ShellTool()
            await tool.preflight_confirmation({}, _call("shell", {"cmd": "cd sub && ls"}))
            self.assertEqual(self.sandbox.calls, [])

        asyncio.run(scenario())

    def test_media_inputs_are_imported_first(self) -> None:
        async def scenario() -> None:
            tool = ShellTool()
            media_id = "media:" + "a" * 64

            class FakeMedia:
                def new_call(self) -> "FakeMedia":
                    return self

                async def cached_export(self, media_id_in: str) -> str:
                    return os.path.join(TMP_ROOT, "src.bin")

                async def close(self) -> None:
                    return None

            tool.media = FakeMedia()  # type: ignore[assignment]
            result = await tool.call(
                {}, _call("shell", {"cmd": "cat $ATRIX_MEDIA_1", "media_inputs": [{"media": media_id}]})
            )
            self.assertTrue(result.content)
            self.assertTrue(any(item[0] == "import" for item in self.sandbox.calls))
            runs = [item for item in self.sandbox.calls if item[0] == "run"]
            self.assertEqual(runs[0][2]["env"]["ATRIX_MEDIA_1"], "/workspace/imports/media.bin")

        asyncio.run(scenario())


class TestWorkspaceRouting(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.sandbox = FakeSandbox()
        sandbox_runtime.set_service(self.sandbox)

    def tearDown(self) -> None:
        sandbox_runtime.clear_service()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_current_workspace_is_tenant_space(self) -> None:
        async def scenario() -> None:
            directory = await current_workspace_dir({})
            self.assertEqual(directory, self.sandbox.workspace)

        asyncio.run(scenario())

    def test_resolve_path_uses_sandbox(self) -> None:
        async def scenario() -> None:
            resolved = await resolve_workspace_path({}, "a/b.txt", PathResolveOptions())
            self.assertEqual(
                resolved.path, os.path.normpath(os.path.join(self.sandbox.workspace, "a/b.txt"))
            )
            self.assertEqual(resolved.base_dir, self.sandbox.workspace)
            self.assertEqual(resolved.warnings, [])

        asyncio.run(scenario())

    def test_workspace_tool_query_and_reset(self) -> None:
        async def scenario() -> None:
            tool = WorkspaceTool()
            result = await tool.call({}, _call("workspace", {}))
            self.assertIn("/workspace", result.content)
            reset = await tool.call({}, _call("workspace", {"reset": True}))
            self.assertIn("已重建", reset.content)
            self.assertIn(("reset", "rebuild"), self.sandbox.calls)

        asyncio.run(scenario())

    def test_workspace_tool_rejects_host_path(self) -> None:
        from atri_x.errors import InvalidError

        async def scenario() -> None:
            tool = WorkspaceTool()
            with self.assertRaises(InvalidError):
                await tool.call({}, _call("workspace", {"path": "C:\\Windows"}))

        asyncio.run(scenario())


class TestUnavailableService(unittest.TestCase):
    def test_broken_service_fails_closed(self) -> None:
        service = sandbox_runtime.UnavailableService("测试原因")
        sandbox_runtime.set_service(service)
        try:
            with self.assertRaises(SandboxUnavailableError):
                asyncio.run(service.run_shell({}, "echo hi"))
        finally:
            sandbox_runtime.clear_service()

    def test_service_or_none(self) -> None:
        self.assertIsNone(sandbox_runtime.service_or_none())
        fake = FakeSandbox()
        sandbox_runtime.set_service(fake)
        try:
            self.assertIs(sandbox_runtime.service_or_none(), fake)
        finally:
            sandbox_runtime.clear_service()


if __name__ == "__main__":
    unittest.main()
