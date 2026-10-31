"""断线自愈：状态机时机/退避/上限、只对本地服务启用、恢复动作的依赖编排。

真实事故：本地跑全量测试的人先把用户正在用的后台服务杀掉腾端口，测试跑完忘了重启；
GUI 只会每 2 秒重连一次，永远停在「已断开（WinError 1225）」。这里的用例钉住自愈的
每一条安全边界——尤其「连远端服务器时绝不替对方重启进程」和「用户显式停止后不抢跑」。
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from atri_x.gui import supervisor
from atri_x.gui.supervisor import (
    MAX_ATTEMPTS,
    ServiceSupervisor,
    is_loopback_url,
    service_recovery,
)


class FakeClock:
    """可控时钟（自愈用 ``time.monotonic``，测试里不能真的等 8 秒）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def make_supervisor(
    outcomes: list[tuple[bool, str]] | None = None, **kwargs
):
    """造一个用队列结果驱动 recover 的看护对象。"""
    calls: list[int] = []
    results = list(outcomes or [])

    def recover() -> tuple[bool, str]:
        calls.append(1)
        return results.pop(0) if results else (False, "still down")

    clock = FakeClock()
    sup = ServiceSupervisor(recover, clock=clock, **kwargs)
    return sup, clock, calls


def drain(sup: ServiceSupervisor) -> list:
    """等后台恢复线程结束并取回结果。"""
    sup.join(5.0)
    return sup.take_outcomes()


def settings(url: str = "ws://127.0.0.1:32172/cli/v1/ws") -> SimpleNamespace:
    return SimpleNamespace(url=url, client_id="local", token="secret")


class ServiceSupervisorTest(unittest.TestCase):
    """主线程 tick 的判定逻辑。"""

    def test_waits_for_grace_before_recovering(self) -> None:
        sup, clock, calls = make_supervisor(grace=8.0)
        sup.note_state("disconnected")
        clock.advance(7.9)
        self.assertFalse(sup.tick())
        self.assertEqual(calls, [])
        clock.advance(0.2)
        self.assertTrue(sup.tick())
        drain(sup)
        self.assertEqual(len(calls), 1)

    def test_tick_without_disconnect_does_nothing(self) -> None:
        sup, clock, calls = make_supervisor()
        clock.advance(60)
        self.assertFalse(sup.tick())
        self.assertEqual(calls, [])

    def test_connected_state_resets_the_timer(self) -> None:
        sup, clock, calls = make_supervisor()
        sup.note_state("disconnected")
        clock.advance(60)
        sup.note_state("connected")
        self.assertFalse(sup.tick())
        sup.note_state("disconnected")
        clock.advance(60)
        self.assertTrue(sup.tick())
        drain(sup)
        self.assertEqual(len(calls), 1)

    def test_in_flight_blocks_second_attempt(self) -> None:
        release = threading.Event()
        calls: list[int] = []

        def recover() -> tuple[bool, str]:
            calls.append(1)
            release.wait(5.0)
            return True, ""

        sup = ServiceSupervisor(recover, grace=0.0, clock=FakeClock())
        sup.note_state("disconnected")
        self.assertTrue(sup.tick())
        # 恢复线程还在跑：不能又起一个（否则会同时拉起两个服务）。
        self.assertTrue(sup.in_flight)
        self.assertFalse(sup.tick())
        release.set()
        outcomes = drain(sup)
        self.assertEqual(len(calls), 1)
        self.assertTrue(outcomes[0].ok)

    def test_backoff_delays_the_next_attempt(self) -> None:
        sup, clock, calls = make_supervisor(grace=0.0, backoff=(15.0,))
        sup.note_state("disconnected")
        self.assertTrue(sup.tick())
        drain(sup)  # 第一次失败（默认 recover 返回 False）
        self.assertEqual(sup.attempts, 1)
        clock.advance(14.0)
        self.assertFalse(sup.tick())
        clock.advance(1.5)
        self.assertTrue(sup.tick())
        drain(sup)
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_max_attempts(self) -> None:
        sup, clock, calls = make_supervisor(grace=0.0, backoff=(0.0,), max_attempts=2)
        sup.note_state("disconnected")
        for _ in range(10):
            sup.tick()
            drain(sup)
            clock.advance(60)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sup.attempts, 2)
        self.assertTrue(sup.exhausted)

    def test_suspend_stops_and_resume_restarts(self) -> None:
        sup, clock, calls = make_supervisor(grace=0.0)
        sup.note_state("disconnected")
        sup.suspend()
        clock.advance(60)
        self.assertFalse(sup.tick())
        self.assertEqual(calls, [])
        # resume 后要等下一次 disconnected 事件重新计时（不拿旧时间点算账）。
        sup.resume()
        clock.advance(60)
        self.assertFalse(sup.tick())
        sup.note_state("disconnected")
        clock.advance(9.0)
        self.assertTrue(sup.tick())
        drain(sup)
        self.assertEqual(len(calls), 1)

    def test_recover_exception_becomes_failed_outcome(self) -> None:
        def recover() -> tuple[bool, str]:
            raise RuntimeError("boom")

        sup = ServiceSupervisor(recover, grace=0.0, clock=FakeClock())
        sup.note_state("disconnected")
        self.assertTrue(sup.tick())
        outcomes = drain(sup)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].ok)
        self.assertIn("boom", outcomes[0].detail)
        self.assertFalse(sup.in_flight)


class RecoveringGuiAppTest(unittest.TestCase):
    """入口层的启用条件：只给「本地回环 + 允许管理服务」的桌面端装看护对象。"""

    def make_app(self, url: str, *, allow: bool):
        from atri_x.gui import recovery

        def fake_init(self, *args, **kwargs):
            self.settings = SimpleNamespace(url=url, config_dir="", client_id="local")
            self.service_owned = False

        with mock.patch.object(recovery.GuiApp, "__init__", fake_init):
            return recovery.RecoveringGuiApp(allow_auto_recover=allow)

    def test_loopback_app_gets_a_supervisor(self) -> None:
        app = self.make_app("ws://127.0.0.1:32172/cli/v1/ws", allow=True)
        self.assertIsNotNone(app._supervisor)

    def test_no_service_mode_gets_no_supervisor(self) -> None:
        # --no-service：只连已有服务，不能自动重启（可能是别人的进程）。
        app = self.make_app("ws://127.0.0.1:32172/cli/v1/ws", allow=False)
        self.assertIsNone(app._supervisor)

    def test_remote_url_gets_no_supervisor(self) -> None:
        app = self.make_app("ws://10.0.0.2:32172/cli/v1/ws", allow=True)
        self.assertIsNone(app._supervisor)


class LoopbackUrlTest(unittest.TestCase):
    """只有本机回环地址才允许自动重启后台服务。"""

    def test_loopback_urls(self) -> None:
        for url in (
            "ws://127.0.0.1:32172/cli/v1/ws",
            "ws://127.0.0.2:32172/cli/v1/ws",
            "ws://localhost:32172/cli/v1/ws",
            "ws://[::1]:32172/cli/v1/ws",
            "wss://127.0.0.1:32172/cli/v1/ws",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_loopback_url(url))

    def test_remote_or_invalid_urls(self) -> None:
        for url in (
            "ws://10.0.0.2:32172/cli/v1/ws",
            "ws://192.168.1.10:32172/cli/v1/ws",
            "ws://example.test/cli/v1/ws",
            "ws://0.0.0.0:32172/cli/v1/ws",  # 0.0.0.0 视为本地占位
            "http://127.0.0.1:32172/cli/v1/ws",
            "",
            "not a url",
        ):
            with self.subTest(url=url):
                expected = url.startswith("ws://0.0.0.0")
                self.assertEqual(is_loopback_url(url), expected)


class ServiceRecoveryTest(unittest.TestCase):
    """恢复动作的编排：探测 → 处理挂死进程 → 拉起 → 等就绪。"""

    def make_recovery(self, **deps):
        defaults = {
            "probe": mock.Mock(return_value=(False, "refused")),
            "spawn": mock.Mock(return_value=None),
            "wait_ready": mock.Mock(return_value=(True, "")),
            "read_pid": mock.Mock(return_value=0),
            "alive": mock.Mock(return_value=False),
            "stop": mock.Mock(return_value=(True, "")),
        }
        defaults.update(deps)
        recover = service_recovery(settings(), **defaults)
        return recover, defaults

    def test_probe_ok_returns_without_touching_processes(self) -> None:
        recover, deps = self.make_recovery(probe=mock.Mock(return_value=(True, "")))
        ok, detail = recover()
        self.assertTrue(ok)
        self.assertIn("已在运行", detail)
        deps["spawn"].assert_not_called()
        deps["stop"].assert_not_called()

    def test_spawns_when_nothing_records_a_live_pid(self) -> None:
        recover, deps = self.make_recovery()
        ok, detail = recover()
        self.assertTrue(ok, detail)
        deps["spawn"].assert_called_once()
        deps["stop"].assert_not_called()

    def test_stops_hung_pid_before_spawning(self) -> None:
        recover, deps = self.make_recovery(
            read_pid=mock.Mock(return_value=4242), alive=mock.Mock(return_value=True)
        )
        ok, _detail = recover()
        self.assertTrue(ok)
        deps["stop"].assert_called_once()
        deps["spawn"].assert_called_once()

    def test_probe_uses_handshake_timeout(self) -> None:
        recover, deps = self.make_recovery(probe=mock.Mock(return_value=(True, "")))
        recover()
        _args, kwargs = deps["probe"].call_args
        self.assertEqual(kwargs.get("timeout"), supervisor.RECOVERY_PROBE_TIMEOUT)

    def test_spawn_failure_is_reported(self) -> None:
        recover, _deps = self.make_recovery(spawn=mock.Mock(side_effect=OSError("no exe")))
        ok, detail = recover()
        self.assertFalse(ok)
        self.assertIn("启动后台服务失败", detail)
        self.assertIn("no exe", detail)

    def test_not_ready_is_reported(self) -> None:
        recover, _deps = self.make_recovery(
            wait_ready=mock.Mock(return_value=(False, "等待 hello_ok 超时"))
        )
        ok, detail = recover()
        self.assertFalse(ok)
        self.assertIn("hello_ok", detail)

    def test_default_max_attempts_is_finite(self) -> None:
        self.assertGreaterEqual(MAX_ATTEMPTS, 2)
        self.assertLessEqual(MAX_ATTEMPTS, 10)


if __name__ == "__main__":
    unittest.main()
