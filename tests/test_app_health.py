"""健康与运维端点测试。

覆盖：``atri_x/app/health.py``（环境变量解析、回环判定、重启原因、真起一次
健康服务器）、``atri_x/app/health_handler.py``、``atri_x/app/ops_health.py``
（``omitempty`` 序列化、延迟接线组件、``/metrics`` 与 ``/diagnostics`` 载荷）。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from atri_x.app.health import (
    HEALTH_ADDR_ENV,
    HEALTH_ALLOW_UNAUTHENTICATED_ENV,
    HEALTH_LIVE_STALE_ENV,
    HEALTH_OPS_TOKEN_ENV,
    HEALTH_RESTART_REASON_ENV,
    HEALTH_RESTART_REASON_VALUE_ENV,
    apply_restart_reason,
    close_health_server,
    is_loopback_health_addr,
    read_health_config,
    start_health_server,
)
from atri_x.app.health_handler import HealthHandler
from atri_x.app.ops_health import (
    ImageLimitMetrics,
    LazyHTTPHandler,
    LazyJSONProvider,
    OpsDiagnostics,
    OpsMetrics,
    RateLimitMetrics,
    VisionMetrics,
    VisionMetricsState,
    VisionRunnerStats,
    collect_ops_diagnostics,
    collect_ops_metrics,
    marshal,
    write_json_response,
)
from atri_x.errors import InvalidError

REPO = Path(__file__).resolve().parent.parent
TMP = REPO / "tmp_app_health_test"

_MANAGED_ENV = (
    HEALTH_ADDR_ENV,
    HEALTH_LIVE_STALE_ENV,
    HEALTH_OPS_TOKEN_ENV,
    HEALTH_ALLOW_UNAUTHENTICATED_ENV,
    HEALTH_RESTART_REASON_ENV,
    HEALTH_RESTART_REASON_VALUE_ENV,
)


class TestHealthEnvParsing(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)
        self._saved = {name: os.environ.pop(name, None) for name in _MANAGED_ENV}
        self.cfg = SimpleNamespace(config_path=str(TMP / "app.toml"))

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(TMP, ignore_errors=True)

    def test_disabled_without_addr(self):
        parsed = read_health_config(self.cfg)
        self.assertFalse(parsed["enabled"])
        self.assertEqual(parsed["addr"], "")
        self.assertEqual(parsed["live_stale"], timedelta(seconds=90))

    def test_addr_enables_and_live_stale_parsed(self):
        os.environ[HEALTH_ADDR_ENV] = "127.0.0.1:9099"
        os.environ[HEALTH_LIVE_STALE_ENV] = "30"
        parsed = read_health_config(self.cfg)
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["addr"], "127.0.0.1:9099")
        self.assertEqual(parsed["live_stale"], timedelta(seconds=30))

    def test_live_stale_rejects_non_positive(self):
        os.environ[HEALTH_LIVE_STALE_ENV] = "0"
        with self.assertRaises(InvalidError):
            read_health_config(self.cfg)

    def test_live_stale_rejects_garbage(self):
        os.environ[HEALTH_LIVE_STALE_ENV] = "abc"
        with self.assertRaises(InvalidError):
            read_health_config(self.cfg)

    def test_allow_unauthenticated_parsed(self):
        os.environ[HEALTH_ALLOW_UNAUTHENTICATED_ENV] = "true"
        self.assertTrue(read_health_config(self.cfg)["allow_unauthenticated"])

    def test_allow_unauthenticated_rejects_garbage(self):
        os.environ[HEALTH_ALLOW_UNAUTHENTICATED_ENV] = "maybe"
        with self.assertRaises(InvalidError):
            read_health_config(self.cfg)


class TestLoopbackDetection(unittest.TestCase):
    def test_loopback_addresses(self):
        for addr in ("127.0.0.1:8080", "localhost:1", "[::1]:80", "[::1]:0"):
            with self.subTest(addr=addr):
                self.assertTrue(is_loopback_health_addr(addr))

    def test_non_loopback_addresses(self):
        for addr in ("0.0.0.0:8080", "10.0.0.5:80", ":8080", "127.0.0.1", "nonsense"):
            with self.subTest(addr=addr):
                self.assertFalse(is_loopback_health_addr(addr))


class TestApplyRestartReason(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)
        self._saved = {name: os.environ.pop(name, None) for name in _MANAGED_ENV}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(TMP, ignore_errors=True)

    def _state(self):
        return SimpleNamespace(
            value=None,
            set_last_restart_reason=lambda v: setattr(self._holder, "value", v),
        )

    def test_value_env_wins(self):
        holder = SimpleNamespace(value=None)
        state = SimpleNamespace(
            set_last_restart_reason=lambda v: setattr(holder, "value", v)
        )
        os.environ[HEALTH_RESTART_REASON_VALUE_ENV] = "watchdog"
        apply_restart_reason(state)
        self.assertEqual(holder.value, "watchdog")

    def test_file_env_read(self):
        holder = SimpleNamespace(value=None)
        state = SimpleNamespace(
            set_last_restart_reason=lambda v: setattr(holder, "value", v)
        )
        path = TMP / "reason.txt"
        path.write_text("  oom-killer\n", encoding="utf-8")
        os.environ[HEALTH_RESTART_REASON_ENV] = str(path)
        apply_restart_reason(state)
        self.assertEqual(holder.value, "oom-killer")

    def test_missing_file_is_silent(self):
        holder = SimpleNamespace(value=None)
        state = SimpleNamespace(
            set_last_restart_reason=lambda v: setattr(holder, "value", v)
        )
        os.environ[HEALTH_RESTART_REASON_ENV] = str(TMP / "nope.txt")
        apply_restart_reason(state)
        self.assertIsNone(holder.value)

    def test_none_state_is_noop(self):
        apply_restart_reason(None)


class TestHealthHandler(unittest.TestCase):
    class State:
        def __init__(self):
            self.ok = 0
            self.failed = 0

        def record_message_handled(self):
            self.ok += 1

        def record_message_error(self):
            self.failed += 1

    def test_success_counts(self):
        state = self.State()

        class Inner:
            async def handle_message(self, ctx, text):
                return "done"

        handler = HealthHandler(Inner(), state)
        import asyncio

        self.assertEqual(asyncio.run(handler.handle_message(None, "hi")), "done")
        self.assertEqual((state.ok, state.failed), (1, 0))

    def test_failure_counts_and_reraises(self):
        state = self.State()

        class Inner:
            async def handle_message(self, ctx, text):
                raise InvalidError("nope")

        handler = HealthHandler(Inner(), state)
        import asyncio

        with self.assertRaises(InvalidError):
            asyncio.run(handler.handle_message(None, "hi"))
        self.assertEqual((state.ok, state.failed), (0, 1))

    def test_nil_inner_returns_none(self):
        import asyncio

        self.assertIsNone(asyncio.run(HealthHandler(None, self.State()).handle_message(None, "x")))

    def test_unknown_attribute_forwards_to_inner(self):
        inner = SimpleNamespace(marker="inner")
        self.assertEqual(HealthHandler(inner, None).marker, "inner")


class TestMarshalOmitempty(unittest.TestCase):
    def test_zero_values_dropped(self):
        payload = marshal(RateLimitMetrics(allowed=3))
        self.assertEqual(payload, {"allowed": 3})

    def test_all_zero_is_empty_object(self):
        self.assertEqual(marshal(RateLimitMetrics()), {})

    def test_map_keys_are_never_dropped(self):
        """Go 的 ``map`` **没有** omitempty：零值键照常输出。

        真实事故（第三轮验证）：``/plugins/memory`` 的载荷是 ``map[string]any``，
        这里以前按 dataclass 的规则把 ``count: 0`` 整键吞掉，与 Go 形状不符。
        """
        self.assertEqual(
            marshal({"platform": "cli", "scope_id": "", "count": 0}),
            {"platform": "cli", "scope_id": "", "count": 0},
        )

    def test_datetime_rendered_rfc3339(self):
        moment = datetime(2026, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
        self.assertEqual(
            marshal({"collected_at": moment}),
            {"collected_at": "2026-05-04T03:02:01Z"},
        )

    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(
            marshal({"t": datetime(2026, 1, 1, 0, 0, 0)}), {"t": "2026-01-01T00:00:00Z"}
        )

    def test_nested_structures(self):
        payload = marshal(
            OpsMetrics(
                version="1.0",
                uptime_seconds=0,
                rate_limit=RateLimitMetrics(keys=2),
                image_limit=ImageLimitMetrics(active=1),
                vision=VisionMetrics(jobs_started=5),
            )
        )
        self.assertEqual(payload["version"], "1.0")
        self.assertNotIn("uptime_seconds", payload)
        self.assertEqual(payload["rate_limit"], {"keys": 2})
        self.assertEqual(payload["image_limit"], {"active": 1})
        self.assertEqual(payload["vision"], {"jobs_started": 5})


class TestWriteJSONResponse(unittest.TestCase):
    class HandlerCtx:
        def __init__(self):
            self.status = None
            self.headers = []
            self.wfile = _Buffer()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            pass

    def test_headers_and_body(self):
        ctx = self.HandlerCtx()
        write_json_response(ctx, 200, RateLimitMetrics(allowed=1))
        self.assertEqual(ctx.status, 200)
        headers = dict(ctx.headers)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(ctx.wfile.data.decode("utf-8")), {"allowed": 1})


class _Buffer:
    def __init__(self):
        self.data = b""

    def write(self, payload: bytes):
        self.data += payload

    def flush(self):
        pass


class _Request:
    def __init__(self, method="GET", path="/", query=None):
        self.method = method
        self.path = path
        self.query = query or {}
        self.headers: list[tuple[str, str]] = []
        self.ctx = None

    def send_header(self, key, value):
        self.headers.append((key, value))


class TestLazyJSONProvider(unittest.TestCase):
    def test_not_ready_before_set(self):
        ctx = TestWriteJSONResponse.HandlerCtx()
        LazyJSONProvider().handle(ctx, _Request())
        self.assertEqual(ctx.status, 503)
        self.assertEqual(json.loads(ctx.wfile.data.decode())["status"], "not_ready")

    def test_returns_payload_after_set(self):
        provider = LazyJSONProvider()
        provider.set(lambda: {"active": 2})
        ctx = TestWriteJSONResponse.HandlerCtx()
        provider.handle(ctx, _Request())
        self.assertEqual(ctx.status, 200)
        self.assertEqual(json.loads(ctx.wfile.data.decode()), {"active": 2})

    def test_non_get_is_405(self):
        provider = LazyJSONProvider()
        provider.set(lambda: {})
        ctx = TestWriteJSONResponse.HandlerCtx()
        provider.handle(ctx, _Request(method="POST"))
        self.assertEqual(ctx.status, 405)

    def test_provider_error_becomes_500(self):
        provider = LazyJSONProvider()

        def boom():
            raise RuntimeError("kaboom")

        provider.set(boom)
        ctx = TestWriteJSONResponse.HandlerCtx()
        provider.handle(ctx, _Request())
        self.assertEqual(ctx.status, 500)


class TestLazyHTTPHandler(unittest.TestCase):
    def test_not_ready_before_set(self):
        ctx = TestWriteJSONResponse.HandlerCtx()
        LazyHTTPHandler().handle(ctx, _Request())
        self.assertEqual(ctx.status, 503)

    def test_delegates_after_set(self):
        handler = LazyHTTPHandler()
        seen = []

        def inner(handler_ctx, request):
            seen.append(request.path)
            write_json_response(handler_ctx, 200, {"ok": True})

        handler.set(inner)
        ctx = TestWriteJSONResponse.HandlerCtx()
        handler.handle(ctx, _Request(path="/plugins/memory"))
        self.assertEqual(seen, ["/plugins/memory"])
        self.assertEqual(ctx.status, 200)


class TestVisionMetricsState(unittest.TestCase):
    class Service:
        def __init__(self, stats):
            self._stats = stats

        def stats(self):
            return self._stats

    def test_snapshot_is_none_without_roles(self):
        self.assertIsNone(VisionMetricsState().snapshot())

    def test_snapshot_reports_runner_occupancy(self):
        state = VisionMetricsState()
        state.set("image_to_prompt", self.Service(VisionRunnerStats(active_jobs=1)))
        snapshot = state.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.image_to_prompt.active_jobs, 1)
        self.assertIsNone(snapshot.fallback)

    def test_counters_created_on_demand(self):
        state = VisionMetricsState()
        self.assertIsNotNone(state.counters_value())
        self.assertIsNotNone(state.counters)


class TestCollectOps(unittest.TestCase):
    class State:
        def snapshot(self):
            return SimpleNamespace(
                version="2.0",
                status="ok",
                started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                models=[{"id": "m"}],
                platforms=[{"name": "cli"}],
                last_restart_reason="deploy",
            )

    class Agent:
        def active_requests(self):
            return {"active": 1}

        def rate_limit_status(self):
            return SimpleNamespace(allowed=7, rejected=1, keys=2, last_reason="burst")

    class Limiter:
        def stats(self):
            return (3, 4)

    def test_diagnostics_payload(self):
        report = collect_ops_diagnostics(self.State(), self.Agent())
        self.assertIsInstance(report, OpsDiagnostics)
        self.assertEqual(report.rate_limit.allowed, 7)
        self.assertEqual(report.rate_limit.last_reason, "burst")
        self.assertEqual(report.last_restart_reason, "deploy")
        self.assertEqual(report.models, [{"id": "m"}])
        self.assertEqual(report.platforms, [{"name": "cli"}])

    def test_diagnostics_without_agent(self):
        report = collect_ops_diagnostics(self.State(), None)
        self.assertEqual(report.tasks, {})
        self.assertEqual(report.rate_limit.allowed, 0)

    def test_metrics_payload(self):
        cfg = SimpleNamespace(
            sandbox=SimpleNamespace(root=""),
            storage=SimpleNamespace(sessions_sqlite_path=str(TMP / "s.db")),
        )
        metrics = collect_ops_metrics(
            cfg,
            self.State(),
            self.Agent(),
            self.Limiter(),
            lambda: VisionMetrics(jobs_started=9),
        )
        self.assertIsInstance(metrics, OpsMetrics)
        self.assertEqual(metrics.version, "2.0")
        self.assertGreaterEqual(metrics.uptime_seconds, 4)
        self.assertEqual((metrics.image_limit.active, metrics.image_limit.waiting), (3, 4))
        self.assertEqual(metrics.vision.jobs_started, 9)
        self.assertIsNotNone(metrics.resources)

    def test_metrics_tolerates_broken_agent(self):
        class Broken:
            def active_requests(self):
                raise RuntimeError("boom")

            def rate_limit_status(self):
                raise RuntimeError("boom")

        metrics = collect_ops_metrics(None, None, Broken())
        self.assertEqual(metrics.tasks, {})
        self.assertEqual(metrics.rate_limit.allowed, 0)


class TestCollectOpsCapabilities(unittest.TestCase):
    """/diagnostics 新增的能力段：装配报告、脱敏自检、限流"已配置/未配置"。

    这三段回答的是"能力装上了吗"，与 ``health``（"当前是否健康"）是不同问题；
    旧载荷把"没有限流"渲染成"0 次拒绝"，这里把两种状态钉开。
    """

    class LiveAgent:
        rate_limit_user = object()

        def rate_limit_status(self):
            return SimpleNamespace(
                allowed=1,
                rejected=0,
                keys=1,
                user_messages_per_minute=12,
                user_burst=3,
                group_messages_per_minute=60,
                group_burst=10,
            )

        agent_services_report = {
            "installed": [["models", "ModelService"]],
            "skipped": [],
            "failed": [],
            "warnings": [],
            "ok": True,
        }

    class UnconfiguredAgent:
        def rate_limit_status(self):
            return SimpleNamespace(allowed=0, rejected=0, keys=0)

    def test_agent_services_passthrough(self):
        report = collect_ops_diagnostics(None, self.LiveAgent())
        self.assertEqual(report.agent_services["ok"], True)
        self.assertEqual(report.agent_services["installed"], [["models", "ModelService"]])
        rendered = marshal(report)
        self.assertEqual(rendered["agent_services"]["ok"], True)
        # 空列表也要出现（不能被 omitempty 吞掉），"跳过 0 项"本身是信息。
        self.assertEqual(rendered["agent_services"]["skipped"], [])

    def test_agent_services_absent_without_agent(self):
        report = collect_ops_diagnostics(None, None)
        self.assertIsNone(report.agent_services)
        self.assertNotIn("agent_services", marshal(report))

    def test_redaction_status_is_exposed(self):
        report = collect_ops_diagnostics(None, None)
        self.assertIsInstance(report.redaction, dict)
        self.assertIn("available", report.redaction)
        self.assertIn("redaction", marshal(report))

    def test_configured_rate_limit_is_distinguishable(self):
        report = collect_ops_diagnostics(None, self.LiveAgent())
        self.assertTrue(report.rate_limit.configured)
        self.assertEqual(report.rate_limit.disabled_reason, "")
        self.assertEqual(report.rate_limit.user_messages_per_minute, 12)
        self.assertEqual(report.rate_limit.group_messages_per_minute, 60)
        rendered = marshal(report)["rate_limit"]
        self.assertIs(rendered["configured"], True)
        self.assertNotIn("disabled_reason", rendered)

    def test_unconfigured_rate_limit_is_not_reported_as_zero_rejections(self):
        report = collect_ops_diagnostics(None, self.UnconfiguredAgent())
        self.assertFalse(report.rate_limit.configured)
        self.assertIn("not configured", report.rate_limit.disabled_reason)
        rendered = marshal(report)["rate_limit"]
        # configured=False 被 omitempty 省略；用非空 disabled_reason 明确表达"未配置"。
        self.assertNotIn("configured", rendered)
        self.assertIn("not configured", rendered["disabled_reason"])

    def test_metrics_also_marks_rate_limit_configuration(self):
        metrics = collect_ops_metrics(None, None, self.LiveAgent())
        self.assertTrue(metrics.rate_limit.configured)
        self.assertEqual(metrics.rate_limit.user_messages_per_minute, 12)
        metrics = collect_ops_metrics(None, None, self.UnconfiguredAgent())
        self.assertFalse(metrics.rate_limit.configured)
        self.assertTrue(metrics.rate_limit.disabled_reason)


class TestStartHealthServer(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)
        self._saved = {name: os.environ.pop(name, None) for name in _MANAGED_ENV}
        self.cfg = SimpleNamespace(
            config_path=str(TMP / "app.toml"),
            storage=SimpleNamespace(
                sessions_sqlite_path=str(TMP / "s.db"),
                chat_history_sqlite_path=str(TMP / "h.db"),
            ),
            sandbox=SimpleNamespace(root=str(TMP)),
        )

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(TMP, ignore_errors=True)

    def test_no_addr_returns_none_pair(self):
        import asyncio

        state, server = asyncio.run(start_health_server(self.cfg, None, "1.0", {}))
        self.assertIsNone(state)
        self.assertIsNone(server)

    def test_none_config_returns_none_pair(self):
        import asyncio

        self.assertEqual(asyncio.run(start_health_server(None, None, "1.0", {})), (None, None))

    def test_real_server_serves_live_and_ready(self):
        import asyncio

        os.environ[HEALTH_ADDR_ENV] = "127.0.0.1:0"
        os.environ[HEALTH_OPS_TOKEN_ENV] = "secret-token"

        async def run():
            state, server = await start_health_server(self.cfg, None, "1.0", {})
            self.assertIsNotNone(server)
            port = server.bound_port
            server.state.set_ready(True)

            def fetch(path):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Authorization": "Bearer secret-token"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, response.read()

            loop = asyncio.get_running_loop()
            status, body = await loop.run_in_executor(None, fetch, "/live")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "live")
            status, body = await loop.run_in_executor(None, fetch, "/ready")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ready")
            # ``Server.close`` 是同步的；app 层的包装允许同步或协程。
            await close_health_server(None, server)

        asyncio.run(run())

    def test_start_error_wrapped(self):
        import asyncio

        os.environ[HEALTH_ADDR_ENV] = "definitely-not-an-address"
        with self.assertRaises(InvalidError):
            asyncio.run(start_health_server(self.cfg, None, "1.0", {}))


if __name__ == "__main__":
    unittest.main()
