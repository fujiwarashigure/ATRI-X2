"""唤醒判定：群里「@机器人」与「消息以唤醒词开头」都能叫醒模型。

对应 ``atri_x/agent/wakeup.py``（Go ``internal/agent/wakeup.go``）。桌面端「在线」页与
「平台」页都能改 ``[platform.qqonebot].trigger_keywords``，这里守住「改完真的会唤醒」：

- 默认响应模式（``mention``）下，``atri 你好`` 这种**前缀唤醒词**消息要唤醒，
  普通群聊（既没 @ 也没唤醒词）不能唤醒；
- 唤醒词只能前缀命中（``你好 atri`` 不算），剥离后模型拿到的是剩下的正文；
- @机器人、私聊、命令按平台/命令规则放行。
"""

from __future__ import annotations

import unittest

from atri_x.agent.wakeup import (
    message_wakeup,
    strip_wakeup_prefix_from_text,
)
from atri_x.platform import (
    ConversationGroup,
    ConversationPrivate,
    Identity,
    Mention,
    MessageContext,
    with_message_context,
)


class _Policy:
    """群策略的测试替身：响应模式、群级唤醒词。"""

    def __init__(self, mode: str = "", wake_keywords: list[str] | None = None) -> None:
        self.mode = mode
        self.wake_keywords = list(wake_keywords or [])

    def response_mode_value(self) -> str:
        return self.mode

    def wake_keywords_value(self) -> list[str]:
        return list(self.wake_keywords)


class _Commands:
    def __init__(self, command: bool = False) -> None:
        self.command = command

    def is_command(self, text: str) -> bool:
        return self.command


class _Agent:
    """``message_wakeup`` 只用到这几个方法；其余保持最简。"""

    def __init__(
        self,
        *,
        policy: _Policy | None = None,
        quiet: bool = False,
        command: bool = False,
        superadmin: bool = False,
    ) -> None:
        self.commands = _Commands(command)
        self.policy = policy or _Policy()
        self.quiet = quiet
        self.superadmin = superadmin
        self.store = None

    def scope(self, ctx: object) -> str:
        return "group:1"

    def group_policy_for_scope(self, scope: str) -> _Policy:
        return self.policy

    def group_quiet_hours_active(self, ctx: object) -> bool:
        return self.quiet

    def actor(self, ctx: object) -> object:
        role = "superadmin" if self.superadmin else "member"
        return type("_Actor", (), {"role": role})()


def _group_context(
    text: str = "atri 你好",
    *,
    keywords: list[str] | None = None,
    kind: str = ConversationGroup,
    mentions: list[Mention] | None = None,
) -> object:
    msg = MessageContext(
        platform="qqonebot",
        scope_id="group:1",
        conversation_kind=kind,
        platform_message_id="m1",
        raw_text=text,
        match_text=text,
        match_text_set=True,
        trigger_keywords=list(keywords if keywords is not None else ["atri"]),
        mentions=list(mentions or []),
        bot=Identity(user_id="42", username="ATRI"),
    )
    return with_message_context({}, msg)


class WakeupKeywordCase(unittest.TestCase):
    def test_prefix_keyword_wakes_group_message(self) -> None:
        agent = _Agent()
        self.assertTrue(message_wakeup(agent, _group_context("atri 你好"), "atri 你好"))

    def test_plain_group_message_does_not_wake(self) -> None:
        agent = _Agent()
        self.assertFalse(message_wakeup(agent, _group_context("今天天气不错"), "今天天气不错"))

    def test_keyword_must_be_prefix(self) -> None:
        agent = _Agent()
        self.assertFalse(message_wakeup(agent, _group_context("你好 atri"), "你好 atri"))

    def test_mention_still_wakes(self) -> None:
        agent = _Agent()
        ctx = _group_context(
            "你好", keywords=[], mentions=[Mention(user_id="42", username="ATRI")]
        )
        self.assertTrue(message_wakeup(agent, ctx, "你好"))

    def test_multiple_keywords(self) -> None:
        agent = _Agent()
        ctx = _group_context("小助手 在吗", keywords=["atri", "小助手"])
        self.assertTrue(message_wakeup(agent, ctx, "小助手 在吗"))

    def test_keyword_mode_only_accepts_keyword(self) -> None:
        agent = _Agent(policy=_Policy(mode="keyword"))
        ctx = _group_context(
            "你好", keywords=[], mentions=[Mention(user_id="42", username="ATRI")]
        )
        self.assertFalse(message_wakeup(agent, ctx, "你好"))

    def test_private_conversation_always_wakes(self) -> None:
        agent = _Agent()
        ctx = _group_context("随便说说", keywords=[], kind=ConversationPrivate)
        self.assertTrue(message_wakeup(agent, ctx, "随便说说"))

    def test_quiet_hours_suppress_keyword_wake(self) -> None:
        agent = _Agent(quiet=True)
        self.assertFalse(message_wakeup(agent, _group_context(), "atri 你好"))

    def test_quiet_hours_do_not_suppress_superadmin(self) -> None:
        agent = _Agent(quiet=True, superadmin=True)
        self.assertTrue(message_wakeup(agent, _group_context(), "atri 你好"))

    def test_command_bypasses_quiet_hours(self) -> None:
        agent = _Agent(quiet=True, command=True)
        self.assertTrue(message_wakeup(agent, _group_context("/help", keywords=[]), "/help"))

    def test_keyword_is_stripped_from_model_text(self) -> None:
        ctx = _group_context("atri 你好")
        from atri_x.platform import message_context_from

        msg, ok = message_context_from(ctx)
        self.assertTrue(ok)
        self.assertEqual(strip_wakeup_prefix_from_text("atri 你好", msg, []), "你好")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
