"""沙箱核心单元测试：配置归一化、资源档、租户键、文件空间与用量账本。"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from types import SimpleNamespace

from atri_x.config.sandbox import SandboxConfig
from atri_x.sandbox.egress import _host_allowed, _is_public_ip, _split_host_port
from atri_x.sandbox.errors import SandboxDeniedError, SandboxQuotaError
from atri_x.sandbox.files import (
    check_regular_file,
    export_file,
    import_file,
    prune_tree,
    resolve_workspace_path,
)
from atri_x.sandbox.ledger import UsageLedger
from atri_x.sandbox.profiles import NETWORK_PROFILES, PROFILES, network_profile, resource_profile
from atri_x.sandbox.settings import clamp, from_config
from atri_x.sandbox.tenant import (
    LOCAL_TENANT,
    TenantEnvPaths,
    TenantKey,
    tenant_from_context,
    tenant_from_ids,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_sandbox_core_test")


def _settings(**overrides: object):
    root = os.path.join(TMP_ROOT, "root")
    section = SandboxConfig(root=root, network="offline")
    for key, value in overrides.items():
        setattr(section, key, value)
    return from_config(section)


class TestContainerNetworkSwitch(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_uses_actual_tenant_network_and_checks_result(self) -> None:
        from unittest.mock import AsyncMock
        from atri_x.sandbox.backend_container import ContainerBackend
        from atri_x.sandbox.errors import SandboxBackendError
        backend = ContainerBackend(_settings(), cli=SimpleNamespace(kind="docker"))
        backend._run = AsyncMock(return_value=SimpleNamespace(ok=True))
        await backend.set_online(LOCAL_TENANT.container_name, False, NETWORK_PROFILES["public"])
        backend._run.assert_awaited_once_with(
            ["network", "disconnect", "-f", LOCAL_TENANT.network_name, LOCAL_TENANT.container_name], timeout=60)
        backend._run.return_value = SimpleNamespace(ok=False, error_text=lambda: "disconnect failed")
        with self.assertRaises(SandboxBackendError):
            await backend.set_online(LOCAL_TENANT.container_name, False, NETWORK_PROFILES["public"])


class TestProfilesAndSettings(unittest.TestCase):
    def test_profiles_are_explicit(self) -> None:
        self.assertIn("light", PROFILES)
        self.assertIn("standard", PROFILES)
        self.assertIn("dsh", PROFILES)
        # CPU 是上限 + 权重两件事，不能把权重误报成保底核数。
        dump = PROFILES["standard"].dump()
        self.assertIn("cpu_cores_note", dump)
        self.assertIn("cpu_weight_note", dump)

    def test_unknown_profile_rejected(self) -> None:
        with self.assertRaises(SandboxDeniedError):
            resource_profile("turbo")
        with self.assertRaises(SandboxDeniedError):
            network_profile("everything")

    def test_config_defaults_and_derived_paths(self) -> None:
        settings = _settings()
        self.assertEqual(settings.mode, "container")
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.profile, "standard")
        self.assertEqual(settings.resources.memory_mb, 1024)
        self.assertEqual(settings.resources.command_timeout_s, 120)
        self.assertTrue(settings.ledger_path.endswith("usage-ledger.json"))
        self.assertTrue(settings.audit_path.endswith(os.path.join("audit", "sandbox.jsonl")))
        self.assertGreaterEqual(settings.max_environments, 1)

    def test_overrides_and_clamps(self) -> None:
        settings = _settings(memory_mb=100000, pids_limit=1, command_timeout_seconds=1)
        self.assertEqual(settings.resources.memory_mb, 65536)
        self.assertEqual(settings.resources.pids, 16)
        self.assertEqual(settings.resources.command_timeout_s, 5)
        self.assertEqual(clamp(0, 1, 10, 4), 4)
        self.assertEqual(clamp("xx", 1, 10, 4), 4)

    def test_mode_switch(self) -> None:
        off = _settings(mode="off")
        self.assertFalse(off.enabled)
        with self.assertRaises(SandboxDeniedError):
            _settings(mode="host")
        with self.assertRaises(SandboxDeniedError):
            _settings(backend="vmware")

    def test_network_tiers(self) -> None:
        self.assertFalse(NETWORK_PROFILES["offline"].online)
        self.assertTrue(NETWORK_PROFILES["public"].online)
        self.assertGreater(NETWORK_PROFILES["public"].daily_bytes, 0)
        restricted = _settings(network="restricted", restricted_hosts=["api.example.com"])
        self.assertEqual(restricted.network_policy.restricted_hosts, ("api.example.com",))

    def test_settings_dump_has_no_secrets(self) -> None:
        payload = json.dumps(_settings().dump())
        self.assertNotIn("token", payload.lower())


class TestTenantKeys(unittest.TestCase):
    def test_ids_normalized(self) -> None:
        tenant = tenant_from_ids("qq", "bot42", "group:1", "u7")
        self.assertEqual(tenant.platform, "qq")
        self.assertEqual(tenant.user, "u7")
        default = tenant_from_ids("", "", "", "")
        self.assertEqual(default, LOCAL_TENANT)

    def test_slug_and_names_are_stable_and_safe(self) -> None:
        tenant = tenant_from_ids("qq", "bot42", "group:1", "u7")
        self.assertEqual(tenant.slug, tenant_from_ids("qq", "bot42", "group:1", "u7").slug)
        self.assertRegex(tenant.slug, r"^[0-9A-Za-z._-]+$")
        self.assertNotEqual(tenant.slug, tenant_from_ids("qq", "bot42", "group:2", "u7").slug)
        self.assertTrue(tenant.container_name.startswith("atrix-sb-"))
        self.assertEqual(tenant.container_name, TenantKey(**{
            "platform": "qq", "bot": "bot42", "scope": "group:1", "user": "u7",
        }).container_name)

    def test_context_derivation(self) -> None:
        from atri_x.platform import Identity, MessageContext, with_message_context
        from atri_x.security import Actor, with_actor

        ctx = with_message_context(
            {},
            MessageContext(
                platform="qq",
                scope_id="group:99",
                platform_user_id="10001",
                bot=Identity(user_id="bot-1", username="bot"),
            ),
        )
        ctx = with_actor(ctx, Actor(id="a-1", platform_user_id="10001"))
        tenant = tenant_from_context(ctx)
        self.assertEqual(tenant.platform, "qq")
        self.assertEqual(tenant.bot, "bot-1")
        self.assertEqual(tenant.scope, "group:99")
        self.assertEqual(tenant.user, "10001")

    def test_paths_layout(self) -> None:
        paths = TenantEnvPaths.for_root(os.path.join(TMP_ROOT, "r"), LOCAL_TENANT)
        self.assertTrue(paths.workspace.endswith("workspace"))
        self.assertTrue(paths.root.endswith(LOCAL_TENANT.slug))


class TestFileSpace(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        self.workspace = os.path.join(TMP_ROOT, "ws")
        os.makedirs(self.workspace, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_relative_paths_resolve_inside(self) -> None:
        with open(os.path.join(self.workspace, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        path = resolve_workspace_path(self.workspace, "a.txt")
        self.assertTrue(path.endswith("a.txt"))
        created = resolve_workspace_path(self.workspace, "sub/new.txt", allow_create=True)
        self.assertTrue(created.endswith("new.txt"))

    def test_absolute_and_traversal_rejected(self) -> None:
        for raw in ("/etc/passwd", "C:\\Windows\\win.ini", "~/x", "../x", "a/../../x", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(SandboxDeniedError):
                    resolve_workspace_path(self.workspace, raw, allow_create=True)

    def test_host_files_require_explicit_capability(self) -> None:
        target = os.path.join(TMP_ROOT, "host.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("host")
        with self.assertRaises(SandboxDeniedError):
            resolve_workspace_path(self.workspace, target)
        allowed = resolve_workspace_path(self.workspace, target, allow_host_files=True)
        self.assertEqual(allowed, os.path.abspath(target))

    def test_special_files_rejected(self) -> None:
        target = os.path.join(self.workspace, "dir")
        os.makedirs(target, exist_ok=True)
        with self.assertRaises(SandboxDeniedError):
            check_regular_file(target)
        with self.assertRaises(SandboxDeniedError):
            resolve_workspace_path(self.workspace, "dir")

    def test_import_export_limits(self) -> None:
        source = os.path.join(TMP_ROOT, "src.bin")
        with open(source, "wb") as handle:
            handle.write(b"0123456789")
        dest = os.path.join(self.workspace, "in.bin")
        copied = import_file(source, dest, max_bytes=100, workspace=self.workspace)
        self.assertEqual(copied, 10)
        with self.assertRaises(SandboxDeniedError):
            import_file(source, os.path.join(self.workspace, "big.bin"), max_bytes=5)
        self.assertEqual(export_file(dest, max_bytes=100), os.path.abspath(dest))
        with self.assertRaises(SandboxDeniedError):
            export_file(dest, max_bytes=1)

    def test_prune_tree(self) -> None:
        nested = os.path.join(self.workspace, "a", "b")
        os.makedirs(nested)
        with open(os.path.join(nested, "f"), "w", encoding="utf-8") as handle:
            handle.write("x")
        prune_tree(self.workspace)
        self.assertFalse(os.path.exists(self.workspace))


class TestUsageLedger(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.now = 1000.0
        self.path = os.path.join(TMP_ROOT, "ledger.json")
        self.ledger = UsageLedger(self.path, clock=lambda: self.now)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_traffic_accumulates_and_persists(self) -> None:
        self.ledger.add_traffic("t1", 100, 200)
        self.assertEqual(self.ledger.daily_bytes("t1"), 300)
        reloaded = UsageLedger(self.path, clock=lambda: self.now)
        self.assertEqual(reloaded.daily_bytes("t1"), 300)

    def test_quota_and_reset_semantics(self) -> None:
        self.ledger.add_traffic("t1", 600, 500)
        with self.assertRaises(SandboxQuotaError):
            self.ledger.check_quota("t1", 1000)
        self.assertEqual(self.ledger.remaining_bytes("t1", 2000), 900)
        self.ledger.mark_reset("t1")
        # reset 不清零流量账本。
        self.assertEqual(self.ledger.daily_bytes("t1"), 1100)

    def test_day_rollover(self) -> None:
        self.ledger.add_traffic("t1", 100, 0)
        self.now += 24 * 3600
        self.assertEqual(self.ledger.daily_bytes("t1"), 0)
        self.ledger.add_traffic("t1", 50, 0)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["tenants"]["t1"]["bytes_total"], 150)

    def test_failure_counters(self) -> None:
        self.ledger.record_exec("t1", failure_kind="oom")
        self.ledger.record_exec("t1", failure_kind="oom")
        self.ledger.record_exec("t1", denied=True)
        record = self.ledger.record("t1")
        self.assertEqual(record.failures["oom"], 2)
        self.assertEqual(record.denials, 1)
        self.assertEqual(record.execs, 3)


class TestEgressPolicy(unittest.TestCase):
    def test_private_and_metadata_denied(self) -> None:
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
                   "169.254.169.254", "100.100.100.200", "fe80::1", "fc00::1", "::1"):
            ok, _reason = _is_public_ip(ip)
            self.assertFalse(ok, ip)
        for ip in ("1.1.1.1", "8.8.8.8", "2606:4700::1111"):
            ok, _reason = _is_public_ip(ip)
            self.assertTrue(ok, ip)

    def test_allowed_hosts_wildcard_and_port(self) -> None:
        allowed = ("api.example.com", "*.cdn.example.com", "files.example.net:8443")
        self.assertTrue(_host_allowed("api.example.com", 443, allowed))
        self.assertTrue(_host_allowed("x.cdn.example.com", 80, allowed))
        self.assertTrue(_host_allowed("files.example.net", 8443, allowed))
        self.assertFalse(_host_allowed("files.example.net", 443, allowed))
        self.assertFalse(_host_allowed("evil.example.com", 443, allowed))
        self.assertTrue(_host_allowed("anything.example", 80, ()))

    def test_target_split(self) -> None:
        self.assertEqual(_split_host_port("example.com:8080", 443), ("example.com", 8080))
        self.assertEqual(_split_host_port("example.com", 443), ("example.com", 443))
        self.assertEqual(_split_host_port("[::1]:9000", 443), ("::1", 9000))


class TestConfigModel(unittest.TestCase):
    def test_default_config_includes_sandbox_fields(self) -> None:
        from atri_x.config import default as default_config

        cfg = default_config()
        self.assertTrue(hasattr(cfg.sandbox, "mode"))
        self.assertTrue(cfg.sandbox.root)
        self.assertEqual(cfg.sandbox.mode, "container")


if __name__ == "__main__":
    unittest.main()
