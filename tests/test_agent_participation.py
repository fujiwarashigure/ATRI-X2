"""群聊参与状态机（评估 §D）的行为契约测试。

覆盖：

* ``[participation]`` 配置段：默认关闭、参数夹紧；
* 状态机：冷却、连续无回应、直接互动重置、快照与清空；
* 唤醒接线：开启后冷却期内普通闲聊不唤醒，但点名 / 回复仍然唤醒；
  默认关闭时行为与之前完全一致。
"""

import time
import unittest
from types import SimpleNamespace

from atri_x.agent import participation
from atri_x.agent.wakeup import message_wakeup
from atri_x.config.app_sections import ParticipationConfig
from atri_x.platform import (
    ConversationGroup,
    Identity,
    Mention,
    MessageContext,
    with_message_context,
)
from atri_x.security import Actor, RoleSuperadmin, RoleUser, actor_from_context


class _Policy:
    def __init__(self, mode: str = "") -> None:
        self.mode = mode

    def response_mode_value(self) -> str:
        return self.mode

    def wake_keywords_value(self) -> list[str]:
        return []


class _Commands:
    def is_command(self, text: str) -> bool:
        return False


class _Scope:
    def __init__(self, platform: str = "qq", scope_id: str = "group:1") -> None:
        self.platform = platform
        self.platform_scope_id = scope_id


class _Agent:
    """``message_wakeup`` 与参与状态机用到的全部方法。"""

    def __init__(self, guard: bool = False, mode: str = "all") -> None:
        self.participation_config = SimpleNamespace(
            guard=guard, cooldown_seconds=120, no_reply_streak_max=3
        )
        self.commands = _Commands()
        self.policy = _Policy(mode)
        self.store = None
        self.role = RoleUser

    def scope(self, ctx):
        return _Scope()

    def group_policy_for_scope(self, scope):
        return self.policy

    def group_quiet_hours_active(self, ctx):
        return False

    def actor(self, ctx):
        return Actor(id="qq:1", platform="qq", platform_user_id="1", role=self.role)


def _group_ctx(text: str = "hi", mention_bot: bool = False):
    msg = MessageContext(
        platform="qq",
        scope_id="group:1",
        conversation_kind=ConversationGroup,
        raw_text=text,
        bot=Identity(user_id="bot1", username="atri"),
        mentions=[Mention(user_id="bot1")] if mention_bot else [],
    )
    return with_message_context({}, msg)


class ParticipationConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = ParticipationConfig()
        self.assertFalse(config.guard)
        self.assertEqual(config.cooldown_seconds, 120)
        self.assertEqual(config.no_reply_streak_max, 3)

    def test_normalized_clamps(self) -> None:
        config = ParticipationConfig(cooldown_seconds=-5, no_reply_streak_max=-1).normalized()
        self.assertEqual(config.cooldown_seconds, 0)
        self.assertEqual(config.no_reply_streak_max, 0)

    def test_app_config_has_section(self) -> None:
        from atri_x.config import default as default_config

        self.assertFalse(default_config().participation.guard)


class ParticipationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = _Agent(guard=True)

    def test_disabled_is_noop(self) -> None:
        agent = _Agent(guard=False)
        participation.note_batch_finished(agent, _group_ctx(), replied=True, now=100.0)
        self.assertFalse(participation.cooldown_active(agent, _group_ctx(), now=101.0))
        self.assertFalse(
            participation.suppress_ambient_wakeup(agent, _group_ctx(), direct=False)
        )

    def test_reply_starts_cooldown(self) -> None:
        ctx = _group_ctx()
        participation.note_batch_finished(self.agent, ctx, replied=True, now=100.0)
        self.assertTrue(participation.cooldown_active(self.agent, ctx, now=101.0))
        self.assertTrue(
            participation.suppress_ambient_wakeup(self.agent, ctx, False, now=101.0)
        )
        self.assertFalse(
            participation.suppress_ambient_wakeup(self.agent, ctx, True, now=101.0)
        )
        self.assertFalse(participation.cooldown_active(self.agent, ctx, now=300.0))

    def test_no_reply_streak_enters_cooldown(self) -> None:
        ctx = _group_ctx()
        for step in range(3):
            participation.note_batch_finished(
                self.agent, ctx, replied=False, now=100.0 + step
            )
        state = participation.state_for(self.agent, ctx, now=103.0)
        self.assertEqual(state.no_reply_streak, 3)
        self.assertTrue(participation.cooldown_active(self.agent, ctx, now=103.0))

    def test_direct_interaction_resets(self) -> None:
        ctx = _group_ctx()
        participation.note_batch_finished(self.agent, ctx, replied=False, now=100.0)
        participation.note_batch_finished(self.agent, ctx, replied=False, now=101.0)
        participation.note_inbound(self.agent, ctx, direct=True, now=102.0)
        state = participation.state_for(self.agent, ctx, now=102.0)
        self.assertEqual(state.no_reply_streak, 0)
        self.assertFalse(participation.cooldown_active(self.agent, ctx, now=102.0))

    def test_snapshot_and_reset(self) -> None:
        ctx = _group_ctx()
        participation.note_wakeup(self.agent, ctx, now=100.0)
        payload = participation.state_snapshot(self.agent, ctx, now=100.0)
        self.assertIn("no_reply_streak", payload)
        self.assertEqual(participation.reset(self.agent, ctx), 1)
        self.assertEqual(participation.reset(self.agent), 0)

    def test_scope_key_requires_conversation(self) -> None:
        agent = _Agent(guard=True)
        agent.scope = lambda ctx: SimpleNamespace(platform="qq", platform_scope_id="")
        participation.note_batch_finished(agent, _group_ctx(), replied=True, now=100.0)
        self.assertFalse(participation.cooldown_active(agent, _group_ctx(), now=101.0))


class WakeupIntegrationTest(unittest.TestCase):
    def test_guard_off_keeps_previous_behavior(self) -> None:
        agent = _Agent(guard=False)
        ctx = _group_ctx()
        participation.note_batch_finished(agent, ctx, replied=True, now=100.0)
        self.assertTrue(message_wakeup(agent, ctx, "hi"))

    def test_cooldown_suppresses_ambient_but_not_mention(self) -> None:
        agent = _Agent(guard=True, mode="all")
        ctx = _group_ctx()
        participation.note_batch_finished(agent, ctx, replied=True)
        self.assertFalse(message_wakeup(agent, ctx, "普通闲聊"))
        self.assertTrue(message_wakeup(agent, _group_ctx("atri 在吗", mention_bot=True), "atri 在吗"))

    def test_direct_mention_resets_streak(self) -> None:
        agent = _Agent(guard=True, mode="mention")
        ctx = _group_ctx()
        participation.note_batch_finished(agent, ctx, replied=False, now=100.0)
        participation.note_batch_finished(agent, ctx, replied=False, now=101.0)
        self.assertTrue(message_wakeup(agent, _group_ctx(mention_bot=True), "atri"))
        self.assertEqual(
            participation.state_for(agent, ctx, now=102.0).no_reply_streak, 0
        )


if __name__ == "__main__":
    unittest.main()
