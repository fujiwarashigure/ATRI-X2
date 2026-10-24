"""费用归因（原因标签 / 旁路用量 / ``/usage --by trigger`` / 未知价格提示）的测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from atri_x.agent import call_reason
from atri_x.agent.chat_observe import audit_side_usage, audit_usage, price_known_for
from atri_x.agent.commands.usage import (
    UsageGroupKey,
    format_usage_summary,
    parse_usage_query,
    unpriced_usage_notice,
    usage_group_key_for,
)
from atri_x.llm import Usage
from atri_x.llm.usage_sink import (
    current_usage_sink,
    record_side_usage,
    set_usage_sink,
)
from atri_x.logging import LogEntry, LogQuery
from atri_x.tool.sandbox import SandboxContext, with_sandbox_context


class ReasonTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(call_reason.normalize_reason(" CRON "), call_reason.CRON)
        self.assertEqual(call_reason.normalize_reason("nope"), call_reason.UNKNOWN)
        self.assertEqual(call_reason.normalize_reason(""), call_reason.UNKNOWN)
        self.assertEqual(
            call_reason.normalize_reason("side:compaction"), "side:compaction"
        )
        self.assertEqual(call_reason.normalize_reason("side:"), call_reason.UNKNOWN)

    def test_side_reason(self) -> None:
        self.assertEqual(call_reason.side_reason("image rewrite"), "side:image_rewrite")
        self.assertEqual(call_reason.side_reason(""), "side:unknown")
        self.assertEqual(call_reason.side_reason("  "), "side:unknown")

    def test_classify(self) -> None:
        self.assertEqual(
            call_reason.classify_call_reason(None), call_reason.DIRECT_MESSAGE
        )
        self.assertEqual(
            call_reason.classify_call_reason(object()), call_reason.DIRECT_MESSAGE
        )
        ctx = with_sandbox_context(None, SandboxContext(background=True))
        self.assertEqual(call_reason.classify_call_reason(ctx), call_reason.CRON)
        ctx = with_sandbox_context(None, SandboxContext(background_kind="cron"))
        self.assertEqual(call_reason.classify_call_reason(ctx), call_reason.CRON)


class _FakeAgent:
    def __init__(self) -> None:
        self.audits: list[tuple] = []

    def audit(self, event: str, *attrs) -> None:  # noqa: ANN002
        self.audits.append((event, attrs))


class SinkTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_usage_sink(None)

    def test_no_sink_is_noop(self) -> None:
        set_usage_sink(None)
        self.assertIsNone(current_usage_sink())
        record_side_usage(reason="side:vision", usage=Usage(total_tokens=3))

    def test_records_payload(self) -> None:
        seen: list[dict] = []
        set_usage_sink(seen.append)
        self.assertEqual(current_usage_sink(), seen.append)
        usage = Usage(prompt_tokens=2, completion_tokens=1, total_tokens=3)
        record_side_usage(
            reason="side:vision", provider="p", model="m", usage=usage, elapsed_ms=9
        )
        record_side_usage(reason="side:title")  # 没有用量 → 不上报
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["reason"], "side:vision")
        self.assertEqual(seen[0]["provider"], "p")
        self.assertEqual(seen[0]["model"], "m")
        self.assertEqual(seen[0]["usage"], usage)
        self.assertEqual(seen[0]["elapsed_ms"], 9)


class AuditTests(unittest.TestCase):
    def test_audit_usage_adds_reason_and_price_known(self) -> None:
        agent = _FakeAgent()
        selection = type("S", (), {"provider": "p", "model": "m"})()
        usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        audit_usage(
            agent,
            "s1",
            selection,
            usage,
            12,
            reason=call_reason.DIRECT_MESSAGE,
            price_known=False,
        )
        event, attrs = agent.audits[0]
        self.assertEqual(event, "llm_usage")
        pairs = dict(zip(attrs[::2], attrs[1::2]))
        self.assertEqual(pairs["reason"], call_reason.DIRECT_MESSAGE)
        self.assertEqual(pairs["price_known"], False)
        self.assertEqual(pairs["total_tokens"], 15)
        self.assertEqual(pairs["session_id"], "s1")

    def test_audit_usage_without_reason_keeps_old_shape(self) -> None:
        agent = _FakeAgent()
        selection = type("S", (), {"provider": "p", "model": "m"})()
        audit_usage(agent, "s1", selection, None, 3)
        _event, attrs = agent.audits[0]
        pairs = dict(zip(attrs[::2], attrs[1::2]))
        self.assertNotIn("reason", pairs)
        self.assertNotIn("price_known", pairs)
        self.assertNotIn("total_tokens", pairs)

    def test_audit_side_usage(self) -> None:
        agent = _FakeAgent()
        usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        audit_side_usage(
            agent,
            {
                "reason": "side:compaction",
                "provider": "p",
                "model": "m",
                "usage": usage,
                "elapsed_ms": 7,
            },
        )
        event, attrs = agent.audits[0]
        self.assertEqual(event, "llm_usage")
        pairs = dict(zip(attrs[::2], attrs[1::2]))
        self.assertEqual(pairs["usage_source"], "side")
        self.assertEqual(pairs["reason"], "side:compaction")
        self.assertEqual(pairs["total_tokens"], 3)
        self.assertNotIn("session_id", pairs)

    def test_audit_side_usage_ignores_missing_usage(self) -> None:
        agent = _FakeAgent()
        audit_side_usage(agent, {"reason": "side:title"})
        self.assertEqual(agent.audits, [])

    def test_price_known_for(self) -> None:
        class Priced:
            def _price_for(self, model, at):  # noqa: ANN001, ARG002
                return object() if model == "priced" else None

        agent = Priced()
        self.assertTrue(price_known_for(agent, "priced"))
        self.assertFalse(price_known_for(agent, "free"))
        self.assertFalse(price_known_for(object(), "priced"))


class UsageCommandTests(unittest.TestCase):
    def _entry(self, **fields: str) -> LogEntry:
        return LogEntry(
            time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            level="info",
            message="audit event",
            fields=dict(fields),
        )

    def test_parse_by_trigger(self) -> None:
        query, group_by = parse_usage_query("--by trigger")
        self.assertEqual(group_by, UsageGroupKey.TRIGGER)
        self.assertEqual(query.fields["event"], "llm_usage")
        with self.assertRaises(ValueError):
            parse_usage_query("--by nope")

    def test_group_key_for_trigger(self) -> None:
        entry = self._entry(reason="direct_message")
        self.assertEqual(
            usage_group_key_for(entry, UsageGroupKey.TRIGGER), "direct_message"
        )
        self.assertEqual(
            usage_group_key_for(self._entry(), UsageGroupKey.TRIGGER),
            "unknown-trigger",
        )

    def test_summary_groups_by_trigger(self) -> None:
        entries = [
            self._entry(reason="direct_message", total_tokens="10"),
            self._entry(reason="cron", total_tokens="5"),
            self._entry(total_tokens="1"),
        ]
        text = format_usage_summary(
            entries, LogQuery(prefix="audit", limit=10, days=1), UsageGroupKey.TRIGGER
        )
        self.assertIn("by trigger", text)
        self.assertIn("direct_message", text)
        self.assertIn("cron", text)
        self.assertIn("unknown-trigger", text)
        self.assertIn("16", text)  # total

    def test_unpriced_notice(self) -> None:
        self.assertEqual(unpriced_usage_notice([]), "")
        self.assertIn(
            "价格未知",
            unpriced_usage_notice([self._entry(price_known="false")]),
        )
        self.assertIn(
            "旁路调用",
            unpriced_usage_notice([self._entry(usage_source="side")]),
        )
        combined = unpriced_usage_notice(
            [
                self._entry(price_known="false"),
                self._entry(usage_source="side"),
            ]
        )
        self.assertIn("价格未知", combined)
        self.assertIn("旁路调用", combined)
        self.assertIn("不等于 0 费用", combined)

    def test_summary_includes_unpriced_notice(self) -> None:
        entries = [self._entry(price_known="0", total_tokens="4")]
        text = format_usage_summary(
            entries, LogQuery(prefix="audit", limit=10, days=1), UsageGroupKey.MODEL
        )
        self.assertIn("费用未计入", text)


if __name__ == "__main__":
    unittest.main()
