"""``atri_x.hook.match`` 的单元测试。对应上游 ``internal/hook/match.go``。"""

from __future__ import annotations

import unittest

from atri_x.hook import match as m
from atri_x.hook.event import (
    ActorContext,
    Event,
    MessagePayload,
    MessageReplyPayload,
    PlatformContext,
    Point,
    SessionContext,
    ToolPayload,
)
from atri_x.llm.llm import MessageSegment, SegmentType


def sample_event() -> Event:
    return Event(
        point=Point.PLATFORM_MESSAGE_RECEIVED,
        platform=PlatformContext(
            name="telegram",
            scope_id="group:-100123",
            user_id="42",
            conversation_id="c1",
            message_id="m1",
            reply_to_message_id="r1",
        ),
        actor=ActorContext(
            id="telegram:42",
            role="user",
            group_role="member",
            user_id="42",
            nickname="昵称",
            group_card="群名片",
            display_name="显示名",
        ),
        session=SessionContext(id="s1", mode="chat", title="标题", status="active"),
        message=MessagePayload(
            id="m1",
            role="user",
            platform_text="原文",
            intent_text="意图",
            reply=MessageReplyPayload(
                message_id="r1", sender_id="7", text="被引用文本", display_text="被引用显示"
            ),
            segments=[MessageSegment(type=SegmentType.TEXT, text="文本")],
        ),
        tool=ToolPayload(name="read_file", arguments="{}", result="ok", risk="low"),
    )


class TestFieldValue(unittest.TestCase):
    def test_known_fields_all_resolve(self) -> None:
        event = sample_event()
        for name in m.KNOWN_FIELDS:
            with self.subTest(field=name):
                self.assertIsInstance(m.field_value_of(event, name), str)

    def test_specific_values(self) -> None:
        event = sample_event()
        self.assertEqual("telegram", m.field_value_of(event, "platform.name"))
        self.assertEqual("group:-100123", m.field_value_of(event, "platform.scope_id"))
        self.assertEqual("文本", m.field_value_of(event, "message.text"))
        self.assertEqual("意图", m.field_value_of(event, "message.intent_text"))
        self.assertEqual("r1", m.field_value_of(event, "message.reply.message_id"))
        self.assertEqual("7", m.field_value_of(event, "message.reply.sender_id"))
        self.assertEqual("被引用文本", m.field_value_of(event, "message.reply.text"))
        self.assertEqual("read_file", m.field_value_of(event, "tool.name"))
        self.assertEqual("low", m.field_value_of(event, "tool.risk"))
        self.assertEqual("标题", m.field_value_of(event, "session.title"))
        self.assertEqual("", m.field_value_of(event, "unknown.field"))

    def test_padding_applied(self) -> None:
        # Go 用 strings.TrimSpace(c.Field) 做 dispatch，这里也要一致
        self.assertEqual("telegram", m.field_value_of(sample_event(), "  platform.name  "))

    def test_reply_absent_is_empty(self) -> None:
        event = sample_event()
        event.message.reply = None
        self.assertEqual("", m.field_value_of(event, "message.reply.message_id"))
        self.assertEqual("", m.field_value_of(event, "message.reply.text"))

    def test_error_message_field(self) -> None:
        event = sample_event()
        event.error = RuntimeError("炸了")
        self.assertEqual("炸了", m.field_value_of(event, "error.message"))


class TestKnownFieldAsymmetry(unittest.TestCase):
    """上游不对称：这两个字段能取值但不在 KnownField 里。"""

    def test_values_exist(self) -> None:
        event = sample_event()
        self.assertEqual("昵称", m.field_value_of(event, "actor.nickname"))
        self.assertEqual("群名片", m.field_value_of(event, "actor.group_card"))

    def test_but_validation_rejects_them(self) -> None:
        self.assertFalse(m.known_field("actor.nickname"))
        self.assertFalse(m.known_field("actor.group_card"))
        match = m.Match(conditions=[m.Condition(field="actor.nickname", op=m.MATCH_EXISTS)])
        with self.assertRaises(ValueError):
            match.validate()


class TestConditionValidation(unittest.TestCase):
    def test_empty_match_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match().validate()

    def test_always_ok(self) -> None:
        m.always().validate()

    def test_always_with_field_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match(conditions=[m.Condition(op=m.MATCH_ALWAYS, field="x")]).validate()

    def test_missing_op_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match(conditions=[m.Condition(field="platform.name")]).validate()

    def test_unknown_op_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match(conditions=[m.Condition(field="platform.name", op="nope")]).validate()

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match(conditions=[m.Condition(field="nope", op=m.MATCH_EXISTS)]).validate()

    def test_value_required_rejected(self) -> None:
        for op in m.OPS_NEEDING_VALUE:
            with self.subTest(op=op):
                with self.assertRaises(ValueError):
                    m.Match(conditions=[m.Condition(field="platform.name", op=op)]).validate()

    def test_exists_needs_no_value(self) -> None:
        m.Match(conditions=[m.Condition(field="platform.name", op=m.MATCH_EXISTS)]).validate()

    def test_invalid_regex_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.Match(conditions=[m.Condition(field="llm.text", op=m.MATCH_REGEX, value="(")]).validate()

    def test_error_message_mentions_condition_index(self) -> None:
        match = m.Match(
            conditions=[
                m.Condition(field="platform.name", op=m.MATCH_EXISTS),
                m.Condition(field="nope", op=m.MATCH_EXISTS),
            ]
        )
        with self.assertRaises(ValueError) as ctx:
            match.validate()
        self.assertIn("condition 2", str(ctx.exception))


class TestMatching(unittest.TestCase):
    def setUp(self) -> None:
        self.event = sample_event()

    def test_always(self) -> None:
        self.assertTrue(m.always().matches(self.event))

    def test_exists(self) -> None:
        self.assertTrue(m.exists("platform.name").matches(self.event))
        self.assertFalse(m.exists("platform.user_id").matches(_no_user()))

    def test_contains_full_prefix_suffix(self) -> None:
        self.assertTrue(m.contains("message.text", "文").matches(self.event))
        self.assertTrue(m.full_match("message.text", "文本").matches(self.event))
        self.assertFalse(m.full_match("message.text", "文").matches(self.event))
        self.assertTrue(m.starts_with("message.text", "文").matches(self.event))
        self.assertTrue(m.ends_with("message.text", "本").matches(self.event))

    def test_and_semantics(self) -> None:
        both = m.Match(
            conditions=[
                m.Condition(field="platform.name", op=m.MATCH_FULL, value="telegram"),
                m.Condition(field="session.mode", op=m.MATCH_FULL, value="chat"),
            ]
        )
        self.assertTrue(both.matches(self.event))
        one_wrong = m.Match(
            conditions=[
                m.Condition(field="platform.name", op=m.MATCH_FULL, value="telegram"),
                m.Condition(field="session.mode", op=m.MATCH_FULL, value="work"),
            ]
        )
        self.assertFalse(one_wrong.matches(self.event))

    def test_regex_captures(self) -> None:
        match = m.regex("message.text", r"(?P<first>文)(本)?")
        result = match.match_event(self.event)
        self.assertTrue(result.ok)
        self.assertEqual(1, len(result.context.regex))
        cap = result.context.regex[0]
        self.assertEqual("文本", cap.text)
        self.assertEqual(["文本", "文", "本"], cap.groups)
        self.assertEqual({"first": "文"}, cap.named)
        self.assertEqual(0, cap.start)
        self.assertEqual(2, cap.end)
        self.assertEqual("message.text", cap.field)
        self.assertEqual(r"(?P<first>文)(本)?", cap.value)

    def test_regex_unmatched_optional_group_is_empty(self) -> None:
        match = m.regex("message.text", r"(文)(zzz)?")
        cap = match.match_event(self.event).context.regex[0]
        self.assertEqual(["文", "文", ""], cap.groups)

    def test_regex_no_named_groups_gives_none(self) -> None:
        cap = m.regex("message.text", "文").match_event(self.event).context.regex[0]
        self.assertIsNone(cap.named)

    def test_regex_not_matching(self) -> None:
        self.assertFalse(m.regex("message.text", "不存在").matches(self.event))

    def test_failed_match_returns_empty_context(self) -> None:
        match = m.Match(
            conditions=[
                m.Condition(field="llm.text", op=m.MATCH_REGEX, value="."),
                m.Condition(field="platform.name", op=m.MATCH_FULL, value="nope"),
            ]
        )
        result = match.match_event(self.event)
        self.assertFalse(result.ok)
        self.assertEqual([], result.context.regex)

    def test_invalid_regex_at_runtime_is_false_not_raise(self) -> None:
        # 绕过 validate 直接 match，模拟 Go 的「编译失败 → 不匹配」
        cond = m.Condition(field="message.text", op=m.MATCH_REGEX, value="(")
        self.assertFalse(cond.matches(self.event))


def _no_user() -> Event:
    event = sample_event()
    event.platform.user_id = ""
    return event


class TestTemplateValues(unittest.TestCase):
    def test_all_template_fields_present(self) -> None:
        values = m.template_values(sample_event())
        for name in m.TEMPLATE_FIELDS:
            self.assertIn("{{" + name + "}}", values)
        self.assertEqual("telegram", values["{{platform.name}}"])
        self.assertEqual("read_file", values["{{tool.name}}"])

    def test_template_fields_are_known_fields(self) -> None:
        for name in m.TEMPLATE_FIELDS:
            with self.subTest(field=name):
                self.assertTrue(m.known_field(name))


class TestBlockPolicy(unittest.TestCase):
    def test_blocks_platform(self) -> None:
        policy = m.new_block_policy(["telegram"])
        self.assertTrue(policy.blocks(sample_event()))

    def test_does_not_block_other_platform(self) -> None:
        policy = m.new_block_policy(["qq-onebot"])
        self.assertFalse(policy.blocks(sample_event()))

    def test_empty_platform_never_blocks(self) -> None:
        event = sample_event()
        event.platform.name = ""
        self.assertFalse(m.new_block_policy(["telegram"]).blocks(event))

    def test_blocks_group(self) -> None:
        policy = m.new_block_policy(blocked_groups=["telegram:-100123"])
        self.assertTrue(policy.blocks(sample_event()))

    def test_scope_without_group_kind_not_blocked_as_group(self) -> None:
        event = sample_event()
        event.platform.scope_id = "private:-100123"
        policy = m.new_block_policy(blocked_groups=["telegram:-100123"])
        self.assertFalse(policy.blocks(event))

    def test_blocks_user_by_user_id(self) -> None:
        policy = m.new_block_policy(blocked_users=["telegram:42"])
        self.assertTrue(policy.blocks(sample_event()))

    def test_blocks_user_falls_back_to_actor_id(self) -> None:
        event = sample_event()
        event.actor.user_id = ""
        event.actor.id = "telegram:42"
        self.assertTrue(m.new_block_policy(blocked_users=["telegram:42"]).blocks(event))

    def test_empty_user_never_blocks(self) -> None:
        event = sample_event()
        event.actor.user_id = ""
        event.actor.id = ""
        self.assertFalse(m.new_block_policy(blocked_users=["telegram:42"]).blocks(event))

    def test_empty_platform_entry_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.new_block_policy(["  "])

    def test_bad_id_entries_rejected(self) -> None:
        for bad in ["no-colon", ":42", "telegram:", "  :  "]:
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    m.new_block_policy(blocked_users=[bad])

    def test_split_uses_first_colon(self) -> None:
        policy = m.new_block_policy(blocked_users=["telegram:a:b"])
        event = sample_event()
        event.actor.user_id = "a:b"
        self.assertTrue(policy.blocks(event))


class TestEventMatchContext(unittest.TestCase):
    def test_reads_from_metadata(self) -> None:
        event = sample_event()
        ctx = m.MatchContext(regex=[m.RegexMatch(field="x", text="y")])
        event.metadata["match"] = ctx
        self.assertIs(ctx, m.event_match_context(event))

    def test_missing_or_wrong_type(self) -> None:
        event = sample_event()
        self.assertEqual([], m.event_match_context(event).regex)
        event.metadata["match"] = "not-a-context"
        self.assertEqual([], m.event_match_context(event).regex)

    def test_none_metadata(self) -> None:
        event = sample_event()
        event.metadata = None  # type: ignore[assignment]
        self.assertEqual([], m.event_match_context(event).regex)


if __name__ == "__main__":
    unittest.main()
