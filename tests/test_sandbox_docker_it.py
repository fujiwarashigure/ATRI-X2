"""真 Docker 的沙箱集成测试（默认跳过）。

运行方式::

    ATRIX_SANDBOX_IT=1 python -m unittest tests.test_sandbox_docker_it -v

用独立的临时沙箱根（``tmp_sandbox_docker_it/``），跑完 ``wipe`` 清理容器；镜像若不存在
会按内置 Dockerfile 构建（数分钟）。CI 没有 Docker 时保持跳过，不把「没验证」当成
「通过」。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest

from atri_x.config.sandbox import SandboxConfig
from atri_x.sandbox.manager import SandboxService
from atri_x.sandbox.settings import from_config
from atri_x.sandbox.tenant import tenant_from_ids

TMP_ROOT = os.path.join(os.getcwd(), "tmp_sandbox_docker_it")
ENABLED = os.environ.get("ATRIX_SANDBOX_IT") == "1"


@unittest.skipUnless(ENABLED, "设置 ATRIX_SANDBOX_IT=1 才跑真 Docker 集成测试")
class TestDockerSandbox(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        section = SandboxConfig(
            root=os.path.join(TMP_ROOT, "root"),
            network="offline",
            apt_mirror=os.environ.get("ATRIX_SANDBOX_APT_MIRROR", ""),
        )
        self.service = SandboxService(from_config(section))
        self.tenant = tenant_from_ids("it", "bot", "scope", "user")
        cleanup = asyncio.run(self.service.stop_all(reason="it_setup"))
        self.assertGreaterEqual(cleanup, 0)

    def tearDown(self) -> None:
        try:
            asyncio.run(self.service.reset_tenant(self.tenant, mode="wipe"))
        finally:
            asyncio.run(self.service.aclose())
            shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_isolation_probe(self) -> None:
        async def scenario() -> None:
            report = await self.service.probe(refresh=True)
            self.assertTrue(report.ok, [item.dump() for item in report.failed])
            names = {item.name for item in report.checks}
            self.assertIn("non_root", names)
            self.assertIn("capabilities_dropped", names)
            self.assertIn("network_isolated", names)
            self.assertIn("no_docker_socket", names)

        asyncio.run(scenario())

    def test_offline_execution_and_process_tree_kill(self) -> None:
        async def scenario() -> None:
            result = await self.service.run_command(
                self.tenant,
                "id -u && echo hello > /workspace/x.txt && cat /workspace/x.txt",
                timeout=120,
            )
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertIn("1000", result.stdout)
            self.assertIn("hello", result.stdout)
            # 超时必须把容器内进程全部杀掉（环境随后会被重建）。
            from atri_x.sandbox.errors import SandboxTimeoutError

            with self.assertRaises(SandboxTimeoutError):
                await self.service.run_command(
                    self.tenant, "sleep 600", timeout=2
                )
            again = await self.service.run_command(self.tenant, "echo alive", timeout=120)
            self.assertEqual(again.exit_code, 0, again.stderr)

        asyncio.run(scenario())

    def test_default_public_network_and_denies(self) -> None:
        if os.environ.get("ATRIX_SANDBOX_NET_IT") != "1":
            self.skipTest("设置 ATRIX_SANDBOX_NET_IT=1 才跑真实公网/内网隔离断言")
        section = SandboxConfig(
            root=os.path.join(TMP_ROOT, "net-root"), network="public"
        )
        service = SandboxService(from_config(section))
        tenant = tenant_from_ids("it", "bot", "net", "user")

        async def scenario() -> None:
            try:
                ok = await service.run_command(
                    tenant,
                    "curl -sS -m 25 -o /dev/null -w '%{http_code}' https://example.com",
                    timeout=120,
                )
                self.assertEqual(ok.exit_code, 0, ok.stderr)
                self.assertIn("200", ok.stdout)
                denied = await service.run_command(
                    tenant,
                    "curl -sS -m 10 -o /dev/null -w '%{http_code}' http://169.254.169.254/",
                    timeout=60,
                )
                self.assertNotEqual(denied.exit_code, 0)
                self.assertIn("403", denied.stdout + denied.stderr)
                direct = await service.run_command(
                    tenant,
                    "python3 -c \"import socket;"
                    "s=socket.socket();s.settimeout(3);"
                    "print(s.connect_ex(('1.1.1.1',443)))\"",
                    timeout=60,
                )
                self.assertIn("101", direct.stdout + direct.stderr)
            finally:
                await service.reset_tenant(tenant, mode="wipe")
                await service.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
