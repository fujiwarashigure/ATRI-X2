"""计算静默（评估 §E）的行为契约测试。

覆盖：

* ``[quiet]`` 配置段：默认关闭、窗口格式校验、非法窗口回落默认值；
* :mod:`atri_x.agent.admission`：静默期允许 / 跳过矩阵（cron / 外部事件 /
  直聊 / 超管例外 / 显式放行开关）、群级静默与全局静默取"或"；
* ``cron.run_background``：静默期直接返回 ``outcome=quiet_skipped``，不建 Session、
  不跑模型，并写 ``cron_skipped_quiet`` 审计；
* ``cron.execution``：被静默跳过的结果不会被当成"解析失败"去重试。
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from atri_x.agent import admission
from atri_x.agent import cron as cron_module
from atri_x.agent.call_reason import CRON, DIRECT_MESSAGE, EXTERNAL_EVENT
from atri_x.background import KindCron, RunRequest
from atri_x.config.app_sections import QuietConfig
from atri_x.cron.execution import _quiet_skipped
from atri_x.security import Actor, RoleSuperadmin, RoleUser
from tests.agent_fakes import FakeAgent


def _quiet_agent(**overrides):
    config = SimpleNamespace(
        enabled=True,
        window="00:00-23:59",
        timezone="",
        allow_cron=False,
        allow_external=False,
        allow_direct=True,
        allow_superadmin=True,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return SimpleNamespace(quiet_config=config)


def _actor_ctx(role: str = RoleUser):
    from atri_x.security import with_actor

    return with_actor({}, Actor(id="qq:1", platform="qq", platform_user_id="1", role=role))


class QuietConfigTest(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        config = QuietConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.window, "23:00-07:00")
        self.assertTrue(config.allow_direct)
        self.assertFalse(config.allow_cron)

    def test_normalized_falls_back_on_invalid_window(self) -> None:
        config = QuietConfig(window="not-a-window").normalized()
        self.assertEqual(config.window, "23:00-07:00")
        self.assertEqual(QuietConfig(window=" 08:00-09:30 ").normalized().window, "08:00-09:30")

    def test_app_config_has_quiet_section(self) -> None:
        from atri_x.config import default as default_config

        cfg = default_config()
        self.assertFalse(cfg.quiet.enabled)
        self.assertEqual(cfg.quiet.window, "23:00-07:00")


class AdmissionTest(unittest.TestCase):
    def test_disabled_allows_everything(self) -> None:
        agent = SimpleNamespace(quiet_config=SimpleNamespace(enabled=False))
        for reason in (CRON, EXTERNAL_EVENT, DIRECT_MESSAGE):
            self.assertTrue(admission.admit(agent, None, reason).allowed)

    def test_quiet_skips_cron_and_external(self) -> None:
        agent = _quiet_agent()
        self.assertTrue(admission.admit(agent, None, CRON).skipped)
        self.assertTrue(admission.admit(agent, None, EXTERNAL_EVENT).skipped)
        self.assertIn("补跑", admission.admit(agent, None, CRON).detail)

    def test_quiet_allows_direct_message(self) -> None:
        agent = _quiet_agent()
        decision = admission.admit(agent, None, DIRECT_MESSAGE)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.quiet)

    def test_quiet_allows_superadmin(self) -> None:
        agent = _quiet_agent()
        decision = admission.admit(agent, _actor_ctx(RoleSuperadmin), CRON)
        self.assertTrue(decision.allowed)

    def test_superadmin_can_be_disabled(self) -> None:
        agent = _quiet_agent(allow_superadmin=False)
        self.assertTrue(admission.admit(agent, _actor_ctx(RoleSuperadmin), CRON).skipped)

    def test_explicit_allow_switches(self) -> None:
        agent = _quiet_agent(allow_cron=True, allow_external=True)
        self.assertTrue(admission.admit(agent, None, CRON).allowed)
        self.assertTrue(admission.admit(agent, None, EXTERNAL_EVENT).allowed)

    def test_window_outside_hours_allows(self) -> None:
        agent = _quiet_agent(window="03:00-03:01")
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(admission.global_quiet_active(agent, noon))
        self.assertTrue(admission.admit(agent, None, CRON, noon).allowed)

    def test_window_inside_hours_skips(self) -> None:
        agent = _quiet_agent(window="03:00-03:01")
        three = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(admission.global_quiet_active(agent, three))
        self.assertTrue(admission.admit(agent, None, CRON, three).skipped)

    def test_cross_midnight_window(self) -> None:
        agent = _quiet_agent(window="23:00-07:00")
        late = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
        early = datetime(2026, 1, 1, 6, 59, tzinfo=timezone.utc)
        midday = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(admission.global_quiet_active(agent, late))
        self.assertTrue(admission.global_quiet_active(agent, early))
        self.assertFalse(admission.global_quiet_active(agent, midday))

    def test_group_quiet_ors_global(self) -> None:
        agent = SimpleNamespace(
            quiet_config=SimpleNamespace(enabled=False),
            group_quiet_hours_active=lambda ctx: True,
        )
        self.assertTrue(admission.quiet_window_active(agent, ctx=object()))
        self.assertTrue(admission.admit(agent, object(), CRON).skipped)

    def test_group_checker_failure_is_not_quiet(self) -> None:
        def boom(ctx):
            raise RuntimeError("no policy")

        agent = SimpleNamespace(quiet_config=SimpleNamespace(enabled=False), group_quiet_hours_active=boom)
        self.assertFalse(admission.quiet_window_active(agent, ctx=object()))

    def test_background_reason_and_audit_event(self) -> None:
        self.assertEqual(admission.background_reason(KindCron), CRON)
        self.assertEqual(admission.background_reason("atrinis"), EXTERNAL_EVENT)
        self.assertEqual(admission.background_reason("hook"), "")
        self.assertEqual(admission.skip_audit_event(CRON), "cron_skipped_quiet")
        self.assertEqual(admission.skip_audit_event(EXTERNAL_EVENT), "external_skipped_quiet")


class CronQuietGateTest(unittest.TestCase):
    def _agent(self, **overrides):
        agent = FakeAgent()
        config = SimpleNamespace(
            enabled=True,
            window="00:00-23:59",
            timezone="",
            allow_cron=False,
            allow_external=False,
            allow_direct=True,
            allow_superadmin=True,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        agent.quiet_config = config
        return agent

    def test_cron_skipped_during_quiet(self) -> None:
        agent = self._agent()
        started: list[str] = []
        agent.start_background_chat = lambda ctx, session, prompt: started.append(prompt)
        req = RunRequest(
            kind=KindCron,
            name="daily",
            prompt="p",
            actor=Actor(id="qq:1", platform="qq", platform_user_id="1", role=RoleUser),
        )
        result = asyncio.run(cron_module.run_background(agent, None, req))
        self.assertEqual(result.outcome, "quiet_skipped")
        self.assertEqual(result.text, "")
        self.assertEqual(started, [])
        events = [call[0] for call in agent.audit_calls]
        self.assertIn("cron_skipped_quiet", events)

    def test_cron_allowed_when_configured(self) -> None:
        from atri_x.storage import Message

        agent = self._agent(allow_cron=True)
        started: list[str] = []

        async def _start(ctx, session, prompt):
            started.append(prompt)
            agent.store.messages().append(
                Message(session_id=session.id, role="assistant", content="report")
            )

        agent.start_background_chat = _start
        req = RunRequest(
            kind=KindCron,
            name="daily",
            prompt="p",
            actor=Actor(id="qq:1", platform="qq", platform_user_id="1", role=RoleUser),
        )
        result = asyncio.run(cron_module.run_background(agent, None, req))
        self.assertEqual(result.text, "report")
        self.assertEqual(started, ["p"])

    def test_outcome_marker_is_recognized(self) -> None:
        from atri_x.background import RunResult

        self.assertTrue(_quiet_skipped(RunResult(outcome="quiet_skipped")))
        self.assertFalse(_quiet_skipped(RunResult(text="x")))


if __name__ == "__main__":
    unittest.main()
