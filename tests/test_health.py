"""``atri_x.health`` 的单元测试。对应上游 ``internal/health``。

重点覆盖三件事：

1. ``/live`` / ``/ready`` / ``/healthz`` 的**语义差异**（契约，不能合并）；
2. ``/healthz`` 与 extra handler 的 **token 安全默认**；
3. JSON 形状与 Go 的 struct tag 一致，含 ``time.Time`` 零值不会被 ``omitempty``
   省略这个容易漏掉的细节。
"""

from __future__ import annotations

import http.client
import json
import shutil
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atri_x.health import checker as hc
from atri_x.health import server as hs
from atri_x.health import state as hst

TOKEN = "s3cret-ops-token"


class _Clock:
    """可推进的时钟，用来验证心跳过期而不必真的等 90 秒。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_state(clock: _Clock | None = None, **kw: object) -> hst.State:
    clock = clock or _Clock()
    return hst.new_state(hst.Options(version="0.9.0", now=clock, **kw))  # type: ignore[arg-type]


def make_server(state: hst.State, **kw: object) -> hs.Server:
    kw.setdefault("addr", "127.0.0.1:0")
    kw.setdefault("state", state)
    return hs.new_server(hs.ServerOptions(**kw))  # type: ignore[arg-type]


class TestLivenessSemantics(unittest.TestCase):
    def test_process_live_ignores_heartbeat(self) -> None:
        state = make_state()
        # 从未 Beat 过，但进程就是活的
        self.assertTrue(state.is_process_live())
        self.assertFalse(state.is_live())
        self.assertFalse(state.scheduler_known())

    def test_shutting_down_makes_process_not_live(self) -> None:
        state = make_state()
        state.set_shutting_down(True)
        self.assertFalse(state.is_process_live())
        self.assertTrue(state.shutting_down())

    def test_beat_then_stale(self) -> None:
        clock = _Clock()
        state = make_state(clock)
        state.beat()
        self.assertTrue(state.is_live())
        self.assertTrue(state.scheduler_known())
        clock.advance(89)
        self.assertTrue(state.is_live())
        clock.advance(2)  # 共 91 > 90
        self.assertFalse(state.is_live())
        # 已知但过期
        self.assertTrue(state.scheduler_known())
        self.assertFalse(state.scheduler_live())

    def test_default_live_stale_is_90s(self) -> None:
        self.assertEqual(90.0, hst.DEFAULT_LIVE_STALE_SECONDS)
        state = make_state()
        self.assertEqual(timedelta(seconds=90), state.live_stale)


class TestReadySemantics(unittest.TestCase):
    def test_platform_and_model_failures_do_not_break_ready(self) -> None:
        """``/ready`` 不受平台/模型影响 —— 这是文档承诺的语义。"""
        clock = _Clock()
        state = make_state(clock)
        state.set_ready(True)
        state.beat()
        state.expect_platform("telegram")
        state.mark_platform_disconnected("telegram", RuntimeError("boom"))
        state.record_model_error("deepseek", RuntimeError("503"))
        server = make_server(state)
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(200, status)
        self.assertTrue(payload["ready"])
        self.assertEqual("ready", payload["checks"]["process"])
        # 但聚合状态确实 degraded
        self.assertEqual("degraded", state.snapshot().status)

    def test_not_ready_when_flag_unset(self) -> None:
        state = make_state()
        state.beat()
        server = make_server(state)
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(503, status)
        self.assertFalse(payload["ready"])
        self.assertEqual("not ready", payload["checks"]["process"])

    def test_shutting_down_is_not_ready(self) -> None:
        state = make_state()
        state.set_ready(True)
        state.beat()
        state.set_shutting_down(True)
        server = make_server(state)
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(503, status)
        self.assertEqual("shutting down", payload["checks"]["process"])

    def test_stale_heartbeat_makes_not_ready(self) -> None:
        clock = _Clock()
        state = make_state(clock)
        state.set_ready(True)
        state.beat()
        clock.advance(120)
        server = make_server(state)
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(503, status)
        self.assertEqual("heartbeat stale", payload["checks"]["scheduler"])

    def test_scheduler_not_started_still_ready(self) -> None:
        state = make_state()
        state.set_ready(True)
        server = make_server(state)
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(200, status)
        self.assertEqual("not started", payload["checks"]["scheduler"])


class TestLiveEndpoint(unittest.TestCase):
    def test_live_ok(self) -> None:
        state = make_state()
        server = make_server(state)
        status, payload, headers = _get(server, "/live")
        self.assertEqual(200, status)
        self.assertEqual("live", payload["status"])
        self.assertEqual("0.9.0", payload["version"])
        self.assertIn("application/json; charset=utf-8", headers["Content-Type"])
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_live_not_live_when_shutting_down(self) -> None:
        state = make_state()
        state.set_shutting_down(True)
        status, payload, _ = _get(make_server(state), "/live")
        self.assertEqual(503, status)
        self.assertEqual("not_live", payload["status"])


class TestHealthzTokenPolicy(unittest.TestCase):
    def test_unregistered_without_token(self) -> None:
        """安全默认：没有 token 时 ``/healthz`` 不注册（404），而不是裸奔。"""
        server = make_server(make_state())
        self.assertNotIn("/healthz", server.registered_paths())
        status, payload, _ = _get(server, "/healthz")
        self.assertEqual(404, status)
        self.assertEqual("not found", payload["error"])

    def test_registered_with_token(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        self.assertIn("/healthz", server.registered_paths())

    def test_registered_when_explicitly_allowed(self) -> None:
        server = make_server(make_state(), allow_unauthenticated_extra_handlers=True)
        self.assertIn("/healthz", server.registered_paths())
        # 只断言"没有因为缺 token 被拒"：状态码本身还取决于 readiness
        status, _, _ = _get(server, "/healthz")
        self.assertNotEqual(401, status)

    def test_requires_token(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        status, payload, _ = _get(server, "/healthz")
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"])

    def test_accepts_bearer(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        status, _, _ = _get(server, "/healthz", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertNotEqual(401, status)

    def test_bearer_prefix_is_case_insensitive(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        status, _, _ = _get(server, "/healthz", headers={"Authorization": f"bEaReR {TOKEN}"})
        self.assertNotEqual(401, status)

    def test_accepts_ops_token_header(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        status, _, _ = _get(server, "/healthz", headers={"X-Atrix-Ops-Token": TOKEN})
        self.assertNotEqual(401, status)

    def test_rejects_wrong_token(self) -> None:
        server = make_server(make_state(), extra_handler_token=TOKEN)
        status, _, _ = _get(server, "/healthz", headers={"Authorization": "Bearer nope"})
        self.assertEqual(401, status)

    def test_healthz_status_codes(self) -> None:
        state = make_state()
        state.set_ready(True)
        state.beat()
        server = make_server(state, extra_handler_token=TOKEN)
        auth = {"Authorization": f"Bearer {TOKEN}"}
        status, payload, _ = _get(server, "/healthz", headers=auth)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

        state.set_shutting_down(True)
        status, payload, _ = _get(server, "/healthz", headers=auth)
        self.assertEqual(503, status)
        self.assertEqual("not_live", payload["status"])


class TestExtraHandlers(unittest.TestCase):
    def test_path_must_start_with_slash(self) -> None:
        with self.assertRaises(ValueError):
            make_server(make_state(), extra_handlers={"metrics": lambda ctx: {}})

    def test_none_handler_ignored(self) -> None:
        server = make_server(make_state(), extra_handlers={"/metrics": None})
        self.assertNotIn("/metrics", server.registered_paths())

    def test_disabled_without_token(self) -> None:
        server = make_server(make_state(), extra_handlers={"/metrics": lambda ctx: {"a": 1}})
        self.assertNotIn("/metrics", server.registered_paths())
        status, _, _ = _get(server, "/metrics")
        self.assertEqual(404, status)

    def test_enabled_with_token_and_guarded(self) -> None:
        server = make_server(
            make_state(),
            extra_handler_token=TOKEN,
            extra_handlers={"/metrics": lambda ctx: {"tasks": 3}},
        )
        self.assertIn("/metrics", server.registered_paths())
        status, _, _ = _get(server, "/metrics")
        self.assertEqual(401, status)
        status, payload, _ = _get(server, "/metrics", headers={"X-Atrix-Ops-Token": TOKEN})
        self.assertEqual(200, status)
        self.assertEqual({"tasks": 3}, payload)

    def test_tuple_result_controls_status(self) -> None:
        server = make_server(
            make_state(),
            extra_handler_token=TOKEN,
            extra_handlers={"/boom": lambda ctx: (500, {"error": "x"})},
        )
        status, payload, _ = _get(server, "/boom", headers={"X-Atrix-Ops-Token": TOKEN})
        self.assertEqual(500, status)
        self.assertEqual({"error": "x"}, payload)


class TestMethodAndValidation(unittest.TestCase):
    def test_post_rejected_with_allow_header(self) -> None:
        server = make_server(make_state())
        response = server.dispatch("POST", "/live")
        self.assertEqual(405, response.status)
        self.assertEqual("GET", response.headers["Allow"])

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(404, make_server(make_state()).dispatch("GET", "/nope").status)

    def test_unknown_path_is_404_for_any_method(self) -> None:
        """Go 的 ``ServeMux`` 先按路径路由：``POST /nope`` 是 404，不是 405。"""
        self.assertEqual(404, make_server(make_state()).dispatch("POST", "/nope").status)

    def test_token_check_precedes_method_check(self) -> None:
        """``requireExtraHandlerToken`` 在方法判定之前：无 token 先 401。"""
        server = make_server(make_state(), extra_handler_token=TOKEN)
        self.assertEqual(401, server.dispatch("POST", "/healthz").status)
        guarded = server.dispatch(
            "POST", "/healthz", {"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(405, guarded.status)
        self.assertEqual("GET", guarded.headers["Allow"])

    def test_empty_addr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            hs.new_server(hs.ServerOptions(addr="  ", state=make_state()))

    def test_nil_state_rejected(self) -> None:
        with self.assertRaises(ValueError):
            hs.new_server(hs.ServerOptions(addr="127.0.0.1:0", state=None))


class TestSnapshotAndJsonShape(unittest.TestCase):
    def test_status_transitions(self) -> None:
        clock = _Clock()
        state = make_state(clock)
        # 还没 ready
        self.assertEqual("not_ready", state.snapshot().status)
        state.set_ready(True)
        state.beat()
        self.assertEqual("ok", state.snapshot().status)
        state.record_model_error("p", RuntimeError("x"))
        self.assertEqual("degraded", state.snapshot().status)
        state.record_model_success("p")
        self.assertEqual("ok", state.snapshot().status)
        state.set_shutting_down(True)
        self.assertEqual("not_live", state.snapshot().status)

    def test_zero_time_heartbeat_is_emitted(self) -> None:
        """Go 的 ``time.Time`` 是结构体，``omitempty`` 无效 —— 零值必须输出。"""
        state = make_state()
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertIn("last_heartbeat", payload)
        # 这个 state 从未 beat() 过，所以是 Go 的零值时间
        self.assertEqual(hs.GO_ZERO_TIME, payload["last_heartbeat"])

    def test_optional_fields_omitted_when_empty(self) -> None:
        # make_state() 默认会带上 version=0.9.0，这里显式构造一个没有 version 的
        state = hst.new_state(hst.Options(now=_Clock()))
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertNotIn("version", payload)
        self.assertNotIn("checks_empty_marker", payload)
        self.assertNotIn("models", payload)
        self.assertNotIn("platforms", payload)
        self.assertNotIn("last_message_at", payload)
        self.assertNotIn("last_restart_reason", payload)

    def test_required_fields_always_present(self) -> None:
        payload = hs.marshal_snapshot(make_state().snapshot())
        for key in (
            "status",
            "live",
            "ready",
            "degraded",
            "scheduler_live",
            "scheduler_known",
            "started_at",
            "uptime_seconds",
            "messages_ok",
            "messages_failed",
        ):
            self.assertIn(key, payload)

    def test_checks_present_and_includes_platform_rollup(self) -> None:
        state = make_state()
        state.set_ready(True)
        state.beat()
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertEqual("none configured", payload["checks"]["platforms"])

    def test_models_sorted_and_shape(self) -> None:
        state = make_state()
        state.record_model_error("zeta", ValueError("bad token sk-abcdefghijklmnop"))
        state.record_model_success("alpha")
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertEqual(["alpha", "zeta"], [m["provider"] for m in payload["models"]])
        zeta = payload["models"][1]
        self.assertEqual("degraded", zeta["status"])
        self.assertIn("last_failure_at", zeta)
        self.assertNotIn("circuit", zeta)
        # 错误文本里的凭据必须已脱敏
        self.assertNotIn("sk-abcdefghijklmnop", zeta["last_error"])
        self.assertIn("[REDACTED]", zeta["last_error"])

    def test_platforms_sorted_and_shape(self) -> None:
        state = make_state()
        state.mark_platform_connected("telegram")
        state.expect_platform("qq-onebot")
        state.mark_platform_disconnected("qq-onebot", RuntimeError("x"))
        payload = hs.marshal_snapshot(state.snapshot())
        names = [p["name"] for p in payload["platforms"]]
        self.assertEqual(["qq-onebot", "telegram"], names)
        telegram = payload["platforms"][1]
        self.assertTrue(telegram["connected"])
        self.assertEqual(1, telegram["connect_count"])
        self.assertIn("last_connected_at", telegram)
        self.assertNotIn("last_error", telegram)

    def test_reconnect_clears_error_and_resets_disconnect(self) -> None:
        state = make_state()
        state.mark_platform_connected("p")
        state.mark_platform_disconnected("p", RuntimeError("x"))
        state.mark_platform_connected("p")
        payload = hs.marshal_snapshot(state.snapshot())
        entry = payload["platforms"][0]
        self.assertTrue(entry["connected"])
        self.assertEqual(2, entry["connect_count"])
        self.assertNotIn("last_error", entry)
        self.assertNotIn("last_disconnected_at", entry)

    def test_circuit_recorded(self) -> None:
        state = make_state()
        state.set_model_circuit("p", "open")
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertEqual("open", payload["models"][0]["circuit"])

    def test_platform_login_state_is_separate_from_connected(self) -> None:
        """``connected`` 只说明传输层通了；``login_ok`` 才是"QQ 已登录"。"""
        state = make_state()
        state.mark_platform_connected("qqonebot")
        state.mark_platform_login(
            "qqonebot",
            backend="snowluma",
            login_ok=True,
            self_id="10001",
            nickname="bot",
            app_name="SnowLuma",
        )
        entry = hs.marshal_snapshot(state.snapshot())["platforms"][0]
        self.assertTrue(entry["connected"])
        self.assertTrue(entry["login_ok"])
        self.assertEqual(entry["backend"], "snowluma")
        self.assertEqual(entry["self_id"], "10001")
        self.assertEqual(entry["nickname"], "bot")
        self.assertEqual(entry["app_name"], "SnowLuma")
        self.assertIn("login_checked_at", entry)

    def test_disconnect_clears_login_state(self) -> None:
        state = make_state()
        state.mark_platform_connected("qqonebot")
        state.mark_platform_login("qqonebot", backend="nan", login_ok=True, self_id="1")
        state.mark_platform_disconnected("qqonebot", RuntimeError("boom"))
        entry = hs.marshal_snapshot(state.snapshot())["platforms"][0]
        self.assertFalse(entry["connected"])
        self.assertFalse(entry["login_ok"])

    def test_platform_without_login_check_omits_login_fields(self) -> None:
        state = make_state()
        state.mark_platform_connected("telegram")
        entry = hs.marshal_snapshot(state.snapshot())["platforms"][0]
        self.assertNotIn("login_ok", entry)
        self.assertNotIn("login_checked_at", entry)
        self.assertNotIn("backend", entry)

    def test_login_nickname_is_redacted(self) -> None:
        state = make_state()
        state.mark_platform_login(
            "qqonebot", login_ok=True, self_id="1", nickname="token=sk-abcdefghijklmnop"
        )
        entry = hs.marshal_snapshot(state.snapshot())["platforms"][0]
        self.assertNotIn("sk-abcdefghijklmnop", entry["nickname"])

    def test_restart_reason_redacted(self) -> None:
        state = make_state()
        state.set_last_restart_reason("crashed api_key=abcdef123456")
        self.assertNotIn("abcdef123456", state.last_restart_reason())
        self.assertIn("[REDACTED]", state.last_restart_reason())

    def test_message_counters(self) -> None:
        state = make_state()
        state.record_message_handled()
        state.record_message_handled()
        state.record_message_error()
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertEqual(2, payload["messages_ok"])
        self.assertEqual(1, payload["messages_failed"])
        self.assertIn("last_message_at", payload)

    def test_iso_format_matches_go(self) -> None:
        stamp = datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual("2026-10-13T12:00:00.123456000Z", hs._iso(stamp))
        self.assertEqual(hs.GO_ZERO_TIME, hs._iso(None))

    def test_unknown_provider_name_normalized(self) -> None:
        state = make_state()
        state.record_model_error("   ", RuntimeError("x"))
        payload = hs.marshal_snapshot(state.snapshot())
        self.assertEqual("unknown", payload["models"][0]["provider"])


class TestCheckers(unittest.TestCase):
    def test_checker_func_default_name(self) -> None:
        self.assertEqual("check", hc.CheckerFunc(fn=lambda ctx: None).name())
        self.assertEqual("custom", hc.CheckerFunc(check_name=" custom ").name())

    def test_failing_checker_makes_not_ready(self) -> None:
        def boom(ctx: object) -> None:
            raise RuntimeError("db down")

        state = make_state()
        state.set_ready(True)
        state.beat()
        server = make_server(state, checkers=[hc.CheckerFunc(check_name="storage", fn=boom)])
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(503, status)
        self.assertEqual("db down", payload["checks"]["storage"])

    def test_passing_checker_is_ok(self) -> None:
        state = make_state()
        state.set_ready(True)
        server = make_server(state, checkers=[hc.CheckerFunc(check_name="storage")])
        status, payload, _ = _get(server, "/ready")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["checks"]["storage"])

    def test_none_checker_skipped(self) -> None:
        state = make_state()
        state.set_ready(True)
        server = make_server(state, checkers=[None])
        self.assertEqual(200, _get(server, "/ready")[0])

    def test_dir_writable_ok_and_cleanup(self) -> None:
        root = Path(__file__).resolve().parent / "_tmp_health_dir"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            hc.DirWritable(dir=str(root), check_name="data").check()
            self.assertEqual([], list(root.iterdir()))
            self.assertEqual("data", hc.DirWritable(dir=str(root), check_name="data").name())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dir_writable_empty_dir(self) -> None:
        with self.assertRaises(ValueError):
            hc.DirWritable(dir="   ").check()

    def test_dir_writable_missing_dir_raises(self) -> None:
        missing = Path(__file__).resolve().parent / "_tmp_health_missing" / "deep"
        shutil.rmtree(missing.parent, ignore_errors=True)
        with self.assertRaises(OSError):
            hc.DirWritable(dir=str(missing)).check()

    def test_checker_protocol_conformance(self) -> None:
        self.assertIsInstance(hc.CheckerFunc(fn=lambda ctx: None), hc.Checker)
        self.assertIsInstance(hc.DirWritable(dir="."), hc.Checker)


class TestRealHttpServer(unittest.TestCase):
    """真起一个 socket 走一遍 —— 证明 dispatch 之外的 HTTP 层也是通的。"""

    def setUp(self) -> None:
        self.state = make_state()
        self.state.set_ready(True)
        self.state.beat()
        self.server = make_server(self.state, extra_handler_token=TOKEN)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    def tearDown(self) -> None:
        self.server.close()

    def _fetch(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_live_ready_healthz(self) -> None:
        status, payload = self._fetch("/live")
        self.assertEqual(200, status)
        self.assertEqual("live", payload["status"])

        status, payload = self._fetch("/ready")
        self.assertEqual(200, status)
        self.assertTrue(payload["ready"])

        status, payload = self._fetch("/healthz")
        self.assertEqual(401, status)

        status, payload = self._fetch("/healthz", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        # setUp 里 beat() 过，所以这里是真实时间而不是 Go 的零值
        self.assertNotEqual(hs.GO_ZERO_TIME, payload["last_heartbeat"])
        self.assertTrue(
            str(payload["last_heartbeat"]).startswith("2026-10-13T12:00:00"),
            payload["last_heartbeat"],
        )

    def test_post_is_405(self) -> None:
        request = urllib.request.Request(self.base + "/live", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            self.assertEqual("GET", exc.headers.get("Allow"))
        self.assertEqual(405, status)

    def test_head_is_405_json_without_body(self) -> None:
        """``HEAD`` 不是 GET → 405 + ``Allow: GET``，且不写实体（Go 同）。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=5)
        try:
            connection.request("OPTIONS", "/live")
            reference = connection.getresponse()
            reference_body = reference.read()
            connection.request("HEAD", "/live")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(405, response.status)
        self.assertEqual("GET", response.getheader("Allow"))
        self.assertEqual(b"", body)
        # Content-Length 仍按不带实体的同一个 405 形状给出（与 Go 的 HEAD 语义一致）
        self.assertEqual(
            len(reference_body), int(response.getheader("Content-Length"))
        )

    def test_options_is_405_json(self) -> None:
        """Python 默认对 OPTIONS 回 501 + HTML；Go 回 405 JSON。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=5)
        try:
            connection.request("OPTIONS", "/live")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(405, response.status)
        self.assertEqual("GET", response.getheader("Allow"))
        self.assertEqual({"error": "method not allowed"}, json.loads(body.decode("utf-8")))

    def test_post_unknown_path_is_404(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=5)
        try:
            connection.request("POST", "/nope")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(404, response.status)
        self.assertEqual({"error": "not found"}, json.loads(body.decode("utf-8")))

    def test_server_header_has_no_python_version(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=5)
        try:
            connection.request("GET", "/live")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        self.assertEqual("atri-x-health", response.getheader("Server"))

    def test_bound_port_allocated(self) -> None:
        self.assertIsInstance(self.server.bound_port, int)
        self.assertGreater(self.server.bound_port, 0)


def _get(
    server: hs.Server, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict, dict]:
    response = server.dispatch("GET", path, headers)
    return response.status, response.json(), response.headers


if __name__ == "__main__":
    unittest.main()
