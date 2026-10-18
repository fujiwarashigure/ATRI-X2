"""``atri_x.agent.dsh``（DSH ACP 客户端）的离线测试。

不依赖 Node / dsh 二进制：用 ``tests/_dsh_fake_acp_server.py`` 在子进程里模拟
ACP v1 服务端，覆盖握手、会话生命周期、流式通知、token 用量、权限默认拒绝、
未知服务端请求、超时取消、服务端崩溃与冒烟入口。
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
import unittest
from pathlib import Path

from atri_x.agent.dsh import AcpClient
from atri_x.agent.dsh.smoke import main as smoke_main
from atri_x.agent.dsh.types import (
    ACP_PROTOCOL_VERSION,
    AcpError,
    AcpProcessError,
    AcpProtocolError,
    AcpTimeoutError,
)

FAKE_SERVER = Path(__file__).resolve().parent / "_dsh_fake_acp_server.py"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _posix(path: Path | str) -> str:
    """命令行里用正斜杠，避免 shlex 把 Windows 反斜杠当转义符。"""
    return str(path).replace("\\", "/")


class _ClientTestCase(unittest.TestCase):
    """统一登记与回收客户端，避免子进程泄漏。"""

    def setUp(self) -> None:
        self.clients: list[AcpClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - 清理阶段不掩盖断言失败
                pass

    def make_client(self, *, env: dict[str, str] | None = None, **kwargs: object) -> AcpClient:
        """起一个客户端并完成握手。"""
        client = AcpClient([sys.executable, "-u", str(FAKE_SERVER)], env=env, **kwargs)  # type: ignore[arg-type]
        self.clients.append(client)
        client.start()
        return client


class AcpHandshakeTest(_ClientTestCase):
    """握手与协议版本。"""

    def test_initialize_reports_agent_and_protocol(self) -> None:
        client = self.make_client()
        info = client.server_info
        assert info is not None
        self.assertEqual(info.protocol_version, ACP_PROTOCOL_VERSION)
        self.assertEqual(info.agent_name, "fake-dsh-acp")
        self.assertEqual(info.agent_version, "0.0.1")
        self.assertEqual(info.auth_methods, ())
        self.assertTrue(client.alive)

    def test_auth_method_is_used_when_advertised(self) -> None:
        client = self.make_client(env={"FAKE_ACP_AUTH": "1"})
        info = client.server_info
        assert info is not None
        self.assertEqual(info.auth_methods, ("none",))
        session = client.new_session(str(REPO_ROOT))
        self.assertEqual(session.session_id, "sess-1")

    def test_describe_snapshot(self) -> None:
        client = self.make_client()
        client.new_session(str(REPO_ROOT))
        snapshot = client.describe()
        self.assertTrue(snapshot["alive"])
        self.assertEqual(snapshot["protocolVersion"], ACP_PROTOCOL_VERSION)
        self.assertEqual(len(snapshot["sessions"]), 1)


class AcpSessionTest(_ClientTestCase):
    """会话建立、配置项与关闭。"""

    def test_new_session_exposes_config_options(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        self.assertEqual(session.session_id, "sess-1")
        self.assertEqual(session.cwd, str(REPO_ROOT))
        self.assertEqual(session.current_value("model"), "fake-model")
        self.assertIsNotNone(session.option("reasoning_effort"))
        self.assertIsNone(session.option("unknown"))

    def test_set_config_option_updates_value(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        options = client.set_config_option(session.session_id, "model", "another-model")
        values = [item.get("currentValue") for item in options if item.get("id") == "model"]
        self.assertEqual(values, ["another-model"])

    def test_close_session_then_stop(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        client.close_session(session.session_id)
        self.assertEqual(client.describe()["sessions"], [])
        client.stop()
        self.assertFalse(client.alive)
        self.assertIsNotNone(client.exit_code)

    def test_list_sessions_returns_mapping(self) -> None:
        client = self.make_client()
        client.new_session(str(REPO_ROOT))
        listing = client.list_sessions(cwd=str(REPO_ROOT))
        self.assertIn("sessions", listing)
        self.assertEqual(len(listing["sessions"]), 1)


class AcpPromptTest(_ClientTestCase):
    """一轮 prompt 的事件流、token 用量与停止原因。"""

    def test_prompt_streams_text_thought_and_usage(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        result = client.prompt(session.session_id, "打个招呼")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.text, "你好，世界")
        self.assertEqual(result.thought, "想一想")
        self.assertEqual(result.usage.total_tokens, 123)
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(result.usage.output_tokens, 23)
        self.assertEqual(result.usage.cached_read_tokens, 50)
        self.assertEqual(result.usage.cached_write_tokens, 10)
        self.assertEqual(result.used_tokens, 123)
        self.assertEqual(result.context_size, 1000)
        kinds = [event.kind for event in result.events]
        self.assertIn("agent_message_chunk", kinds)
        self.assertIn("agent_thought_chunk", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("usage_update", kinds)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)

    def test_prompt_events_reach_on_event_callback(self) -> None:
        seen: list[str] = []
        client = self.make_client(on_event=lambda event: seen.append(event.kind))
        session = client.new_session(str(REPO_ROOT))
        client.prompt(session.session_id, "打个招呼")
        self.assertIn("agent_message_chunk", seen)
        self.assertIn("usage_update", seen)

    def test_second_prompt_on_busy_session_is_rejected(self) -> None:
        client = self.make_client(prompt_timeout=10.0)
        session = client.new_session(str(REPO_ROOT))
        results: list[object] = []
        outcome: list[BaseException] = []

        def _first() -> None:
            try:
                results.append(client.prompt(session.session_id, "__HANG__ 卡住", timeout=10.0))
            except BaseException as exc:  # noqa: BLE001 - 线程里收集所有异常
                outcome.append(exc)

        worker = threading.Thread(target=_first, name="dsh-acp-busy", daemon=True)
        worker.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not client.has_prompt_in_flight(session.session_id):
            time.sleep(0.01)
        self.assertTrue(client.has_prompt_in_flight(session.session_id))
        with self.assertRaises(AcpError):
            client.prompt(session.session_id, "并发再来一次")
        client.cancel(session.session_id)
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(getattr(results[0], "stop_reason", ""), "cancelled")


class AcpPermissionTest(_ClientTestCase):
    """``session/request_permission`` 的默认拒绝与回调覆盖。"""

    def test_default_policy_rejects_permission(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        result = client.prompt(session.session_id, "__PERMISSION__ 帮我写文件")
        self.assertIn('"optionId": "reject"', result.text)

    def test_callback_can_allow_permission(self) -> None:
        client = self.make_client(
            on_permission=lambda params: {"outcome": {"outcome": "selected", "optionId": "allow"}},
        )
        session = client.new_session(str(REPO_ROOT))
        result = client.prompt(session.session_id, "__PERMISSION__ 帮我写文件")
        self.assertIn('"optionId": "allow"', result.text)


class AcpFaultTest(_ClientTestCase):
    """未知服务端请求、超时取消与服务端崩溃。"""

    def test_unknown_server_request_gets_method_not_found(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        result = client.prompt(session.session_id, "__UNKNOWN__ 看看文件")
        self.assertIn("unknown:", result.text)
        self.assertIn("-32601", result.text)

    def test_timeout_cancels_and_client_stays_usable(self) -> None:
        client = self.make_client(prompt_timeout=1.5)
        session = client.new_session(str(REPO_ROOT))
        with self.assertRaises(AcpTimeoutError):
            client.prompt(session.session_id, "__HANG__ 卡住", timeout=0.5)
        self.assertTrue(client.alive)
        result = client.prompt(session.session_id, "恢复后继续")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.text, "你好，世界")

    def test_server_crash_surfaces_process_error(self) -> None:
        client = self.make_client()
        session = client.new_session(str(REPO_ROOT))
        with self.assertRaises(AcpProcessError):
            client.prompt(session.session_id, "__CRASH__ 直接退出")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and client.alive:
            time.sleep(0.02)
        self.assertFalse(client.alive)

    def test_start_with_missing_binary_raises(self) -> None:
        client = AcpClient([_posix(REPO_ROOT / "no-such-dsh-binary")])
        self.clients.append(client)
        with self.assertRaises(AcpProcessError):
            client.start()

    def test_protocol_version_mismatch_is_rejected(self) -> None:
        client = AcpClient([sys.executable, "-u", str(FAKE_SERVER)], env={"FAKE_ACP_PROTOCOL": "2"})
        self.clients.append(client)
        with self.assertRaises(AcpProtocolError):
            client.start()
        self.assertFalse(client.alive)


class AcpSmokeTest(unittest.TestCase):
    """``python -m atri_x.agent.dsh.smoke`` 的最小闭环。"""

    def test_smoke_main_reports_tokens(self) -> None:
        command = f"{_posix(sys.executable)} -u {_posix(FAKE_SERVER)}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = smoke_main(
                [
                    "--command",
                    command,
                    "--cwd",
                    _posix(REPO_ROOT),
                    "--prompt",
                    "打个招呼",
                    "--json",
                ]
            )
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("tokens: input=100 output=23 total=123", text)
        self.assertIn("answer: 你好，世界", text)
        self.assertIn('"ok": true', text)
        self.assertIn("[dsh-acp] ok", text)

    def test_smoke_main_fails_without_command(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = smoke_main(["--command", _posix(REPO_ROOT / "no-such-dsh-binary"), "--cwd", _posix(REPO_ROOT)])
        self.assertEqual(code, 1)
        self.assertIn("error:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
