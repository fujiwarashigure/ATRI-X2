"""沙箱服务单元测试：fail-closed、执行链路、配额、重置与容器启动参数。

用假后端（:class:`FakeBackend`）覆盖控制面逻辑，不需要真 Docker；
真容器行为由 ``tests/test_sandbox_docker_it.py``（默认跳过）覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from typing import Any

from atri_x.config.sandbox import SandboxConfig
from atri_x.sandbox.backend import (
    BackendInfo,
    CapabilityCheck,
    CapabilityReport,
    EnvInfo,
    EnvStats,
    ExecResult,
    SandboxBackend,
)
from atri_x.sandbox.container_cli import (
    EngineCLI,
    parse_size_bytes,
    parse_stats,
)
from atri_x.sandbox.envs import build_env_request
from atri_x.sandbox.errors import (
    SandboxQuotaError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from atri_x.sandbox.ledger import UsageLedger
from atri_x.sandbox.manager import SandboxService
from atri_x.sandbox.settings import from_config
from atri_x.sandbox.tenant import LOCAL_TENANT, tenant_from_ids

TMP_ROOT = os.path.join(os.getcwd(), "tmp_sandbox_manager_test")


class FakeBackend(SandboxBackend):
    """内存后端：记录调用、返回预设结果。"""

    name = "fake"

    def __init__(self, *, available: bool = True, probe_ok: bool = True) -> None:
        self.available = available
        self.probe_ok = probe_ok
        self.created: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.executed: list[str] = []
        self.online: list[bool] = []
        self.exec_result = ExecResult(exit_code=0, stdout="ok\n")
        self.exec_delay = 0.0
        self.exec_gate: asyncio.Event | None = None
        self.build_envs: list[Any] = []

    async def backend_info(self) -> BackendInfo:
        return BackendInfo(
            backend="fake", available=self.available, error="" if self.available else "no engine"
        )

    async def probe(self) -> CapabilityReport:
        report = CapabilityReport(
            backend="fake",
            ok=self.probe_ok,
            checks=[CapabilityCheck("fake", self.probe_ok, "ok" if self.probe_ok else "bad")],
        )
        return report

    async def ensure_image(self) -> str:
        return "fake-image"

    async def create_env(self, request: Any) -> EnvInfo:
        self.build_envs.append(request)
        self.created.append(request.tenant.container_name)
        return EnvInfo(name=request.tenant.container_name, running=True)

    async def start_env(self, name: str) -> None:
        self.started.append(name)

    async def stop_env(self, name: str, *, grace_seconds: float = 5.0) -> None:
        self.stopped.append(name)

    async def remove_env(self, name: str, *, proxy_name: str = "") -> None:
        self.removed.append(name)

    async def exec_in_env(
        self,
        name: str,
        command: str,
        *,
        timeout: float,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 256 * 1024,
    ) -> ExecResult:
        self.executed.append(command)
        if self.exec_gate is not None:
            await self.exec_gate.wait()
        if self.exec_delay:
            await asyncio.sleep(self.exec_delay)
        return self.exec_result

    async def env_stats(self, name: str) -> EnvStats:
        return EnvStats(cpu_percent=1.0, memory_bytes=1024)

    async def list_envs(self) -> list[EnvInfo]:
        return []

    async def set_online(self, name: str, online: bool, network: Any) -> None:
        self.online.append(online)

    async def cleanup_orphans(self, known_envs: set[str], known_proxies: set[str]) -> int:
        return 0


def _settings(**overrides: Any):
    root = os.path.join(TMP_ROOT, "root")
    section = SandboxConfig(root=root, network="offline", require_probe=True)
    for key, value in overrides.items():
        setattr(section, key, value)
    return from_config(section)


def _service(backend: FakeBackend | None = None, **overrides: Any) -> SandboxService:
    settings = _settings(**overrides)
    service = SandboxService(
        settings,
        backend=backend or FakeBackend(),
        ledger=UsageLedger(settings.ledger_path),
    )
    return service


class TestFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_backend_unavailable_refuses_without_host_fallback(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend(available=False)
            service = _service(backend)
            with self.assertRaises(SandboxUnavailableError) as ctx:
                await service.run_command(LOCAL_TENANT, "echo hi")
            self.assertIn("宿主", str(ctx.exception))
            self.assertEqual(backend.executed, [])
            await service.aclose()

        asyncio.run(scenario())

    def test_probe_failure_refuses(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend(probe_ok=False)
            service = _service(backend)
            with self.assertRaises(SandboxUnavailableError):
                await service.run_command(LOCAL_TENANT, "echo hi")
            self.assertEqual(backend.executed, [])
            await service.aclose()

        asyncio.run(scenario())

    def test_require_probe_false_skips_probe(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend(probe_ok=False)
            service = _service(backend, require_probe=False)
            result = await service.run_command(LOCAL_TENANT, "echo hi")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(backend.executed, ["echo hi"])
            await service.aclose()

        asyncio.run(scenario())


class TestExecution(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_exec_reuses_environment(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(backend)
            await service.run_command(LOCAL_TENANT, "echo one")
            await service.run_command(LOCAL_TENANT, "echo two")
            self.assertEqual(len(backend.created), 1)
            self.assertEqual(backend.executed, ["echo one", "echo two"])
            self.assertEqual(service.ledger.record(LOCAL_TENANT.slug).execs, 2)
            await service.aclose()

        asyncio.run(scenario())

    def test_failure_classification_goes_to_ledger(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            backend.exec_result = ExecResult(exit_code=137, error_kind="oom")
            service = _service(backend)
            await service.run_command(LOCAL_TENANT, "stress")
            record = service.ledger.record(LOCAL_TENANT.slug)
            self.assertEqual(record.failures.get("oom"), 1)
            await service.aclose()

        asyncio.run(scenario())

    def test_timeout_propagates(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()

            async def boom(*args: Any, **kwargs: Any) -> ExecResult:
                raise SandboxTimeoutError("timeout")

            backend.exec_in_env = boom  # type: ignore[assignment]
            service = _service(backend)
            with self.assertRaises(SandboxTimeoutError):
                await service.run_command(LOCAL_TENANT, "sleep 999")
            await service.aclose()

        asyncio.run(scenario())

    def test_admission_waits_then_evicts_idle_environment(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            backend.exec_delay = 0.15
            service = _service(backend, max_environments=1)
            first_tenant = tenant_from_ids("qq", "bot", "scope", "a")
            second_tenant = tenant_from_ids("qq", "bot", "scope", "b")
            first = asyncio.ensure_future(service.run_command(first_tenant, "echo a"))
            await asyncio.sleep(0.05)
            second = asyncio.ensure_future(service.run_command(second_tenant, "echo b"))
            results = await asyncio.gather(first, second)
            self.assertEqual([item.exit_code for item in results], [0, 0])
            self.assertEqual(len(backend.created), 2)
            await service.aclose()

        asyncio.run(scenario())

    def test_media_import_maps_to_container_path(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(backend)
            source = os.path.join(TMP_ROOT, "media.png")
            with open(source, "wb") as handle:
                handle.write(b"png")
            path = await service.import_media({}, source)
            self.assertTrue(path.startswith("/workspace/imports/"))
            self.assertTrue(os.path.isdir(os.path.join(TMP_ROOT, "root", "tenants")))
            await service.aclose()

        asyncio.run(scenario())


class TestQuotaAndReset(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_traffic_quota_blocks_and_disconnects(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(
                backend, network="public", daily_traffic_mb=1
            )
            # 先跑一次让环境存在，再把今日额度用完：第二次必须被拒并强制断网。
            await service.run_command(LOCAL_TENANT, "echo warm")
            service.ledger.add_traffic(LOCAL_TENANT.slug, 1024 * 1024, 1)
            with self.assertRaises(SandboxQuotaError):
                await service.run_command(LOCAL_TENANT, "curl https://example.com")
            self.assertEqual(backend.executed, ["echo warm"])
            self.assertEqual(backend.online, [False])
            self.assertEqual(
                service.ledger.record(LOCAL_TENANT.slug).denials, 1
            )
            await service.aclose()

        asyncio.run(scenario())

    def test_reset_modes_keep_ledger(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(backend, network="offline")
            await service.run_command(LOCAL_TENANT, "echo hi")
            service.ledger.add_traffic(LOCAL_TENANT.slug, 10, 20)
            restart = await service.reset_tenant(LOCAL_TENANT, mode="restart")
            self.assertEqual(restart["mode"], "restart")
            self.assertTrue(restart["ledger_kept"])
            self.assertTrue(backend.stopped)
            service.ledger.add_traffic(LOCAL_TENANT.slug, 1, 1)
            rebuild = await service.reset_tenant(LOCAL_TENANT, mode="rebuild")
            self.assertEqual(rebuild["mode"], "rebuild")
            self.assertTrue(backend.removed)
            self.assertGreaterEqual(service.ledger.record(LOCAL_TENANT.slug).bytes_total, 32)
            wipe = await service.reset_tenant(LOCAL_TENANT, mode="wipe")
            self.assertEqual(wipe["mode"], "wipe")
            self.assertGreaterEqual(service.ledger.record(LOCAL_TENANT.slug).bytes_total, 32)
            await service.aclose()

        asyncio.run(scenario())

    def test_stop_all_cancels_queue_and_stops_envs(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(backend, network="offline")
            await service.run_command(LOCAL_TENANT, "echo hi")
            stopped = await service.stop_all(reason="test")
            self.assertEqual(stopped, 1)
            self.assertTrue(backend.stopped)
            await service.aclose()

        asyncio.run(scenario())

    def test_status_snapshot(self) -> None:
        async def scenario() -> None:
            backend = FakeBackend()
            service = _service(backend, network="offline")
            await service.run_command(LOCAL_TENANT, "echo hi")
            status = await service.status(refresh=True)
            self.assertTrue(status["backend"]["available"])
            self.assertEqual(status["settings"]["mode"], "container")
            self.assertIn("queue", status)
            self.assertIn("ledger", status)
            await service.aclose()

        asyncio.run(scenario())


class TestContainerArgs(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_create_args_are_hardened(self) -> None:
        from atri_x.sandbox.backend_container import ContainerBackend

        settings = _settings(network="public")
        backend = ContainerBackend(settings, cli=EngineCLI("docker", "docker"))
        request = build_env_request(
            settings, LOCAL_TENANT,
            __import__("atri_x.sandbox.tenant", fromlist=["TenantEnvPaths"]).TenantEnvPaths.for_root(
                settings.root, LOCAL_TENANT
            ),
            remaining_quota_bytes=1024,
        )
        args = backend._create_args(request, "atri-x-sandbox:1.2.0", request.egress.network_name)
        text = " ".join(args)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop", args)
        self.assertIn("ALL", args)
        self.assertIn("--security-opt", args)
        self.assertIn("no-new-privileges", args)
        self.assertIn("--pids-limit", args)
        self.assertIn("--memory-swap", args)
        self.assertEqual(args[args.index("--memory") + 1], args[args.index("--memory-swap") + 1])
        self.assertIn("--cpus", args)
        self.assertIn("--user", args)
        self.assertIn("1000:1000", args)
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertNotIn("docker.sock", text)
        # 只挂载租户自己的三个可写面，不挂宿主根 / 用户 home。
        mounts = [args[index + 1] for index, item in enumerate(args) if item == "-v"]
        self.assertEqual(len(mounts), 3)
        for mount in mounts:
            self.assertIn(os.path.join("tenants", LOCAL_TENANT.slug), mount)
        # 联网档：容器只接内部网络，代理通过环境变量告知（限制在网络层/代理里）。
        self.assertIn(request.egress.network_name, args)
        self.assertIn("HTTP_PROXY", text)
        self.assertIn("HTTPS_PROXY", text)

    def test_offline_uses_no_network(self) -> None:
        from atri_x.sandbox.backend_container import ContainerBackend

        settings = _settings(network="offline")
        backend = ContainerBackend(settings, cli=EngineCLI("docker", "docker"))
        paths = __import__("atri_x.sandbox.tenant", fromlist=["TenantEnvPaths"]).TenantEnvPaths.for_root(
            settings.root, LOCAL_TENANT
        )
        request = build_env_request(settings, LOCAL_TENANT, paths, remaining_quota_bytes=0)
        args = backend._create_args(request, "img", "")
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertNotIn("HTTP_PROXY", " ".join(args))


class TestCliParsing(unittest.TestCase):
    def test_sizes(self) -> None:
        self.assertEqual(parse_size_bytes("1KiB"), 1024)
        self.assertEqual(parse_size_bytes("12.5MiB"), int(12.5 * 1024 * 1024))
        self.assertEqual(parse_size_bytes("0B"), 0)
        self.assertEqual(parse_size_bytes("garbage"), 0)

    def test_stats(self) -> None:
        payload = json.dumps(
            {
                "Name": "x",
                "CPUPerc": "12.5%",
                "MemUsage": "1.5MiB / 1GiB",
                "NetIO": "2kB / 3kB",
                "PIDs": "7",
            }
        )
        stats = parse_stats(payload)
        self.assertEqual(stats["cpu_percent"], 12.5)
        self.assertEqual(stats["memory_bytes"], int(1.5 * 1024 * 1024))
        self.assertEqual(stats["pids"], 7)
        self.assertEqual(stats["net_rx_bytes"], 2000)


if __name__ == "__main__":
    unittest.main()
