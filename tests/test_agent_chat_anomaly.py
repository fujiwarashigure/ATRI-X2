"""``atri_x.agent.chat_anomaly``：把文本形态的工具调用接回工具轮。

覆盖四种行为：
* 能还原 → 补上 ``tool_calls`` + 清空正文 + 补 id；
* 已结构化调用 → 只清洗正文，不重复执行；
* 还原失败 / chat 会话 → 正文换成提示；
* 普通回答 → 原样返回、不写审计。
"""

import json
import unittest
import uuid

from atri_x.agent.chat_anomaly import apply_text_tool_recovery
from atri_x.agent.chat_llm import LLMCallResult
from atri_x.llm import ToolCallRequest
from atri_x.llm.tooltext import TEXT_TOOL_CALL_NOTICE

LEAK = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="web_search">\n'
    '<｜｜DSML｜｜ parameter name="query" string="true">最新游戏显卡 2026 型号</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


class _FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, tuple]] = []

    def warn(self, message: str, *args) -> None:
        self.records.append(("warn", message, args))


class _FakeAgent:
    def __init__(self) -> None:
        self.logger = _FakeLogger()
        self.audits: list[tuple[str, tuple]] = []

    def audit_warn(self, event: str, *attrs) -> None:
        self.audits.append((event, attrs))


class _RecordingLogger:
    def __init__(self) -> None:
        self.warns: list[tuple[str, tuple]] = []

    def warn(self, message: str, *args) -> None:
        self.warns.append((message, args))


class _FakeLogs:
    """``AgentOptions.logs`` 在真实装配里就是一个 Manager：``runtime()`` / ``audit()``。"""

    def __init__(self) -> None:
        self.runtime_logger = _RecordingLogger()
        self.audit_logger = _RecordingLogger()

    def runtime(self) -> _RecordingLogger:
        return self.runtime_logger

    def audit(self) -> _RecordingLogger:
        return self.audit_logger


class _UnwiredAgent:
    """生产装配的真实形态：``logs`` 有值，但 ``logger`` / ``audit_logger`` 没挂上。"""

    def __init__(self, logs: _FakeLogs | None) -> None:
        self.logger = None
        self.audit_logger = None
        self.logs = logs
        self.events: list[tuple[str, tuple]] = []

    def audit_warn(self, event: str, *attrs) -> None:
        # 与 ``atri_x.agent.logging.audit_log`` 同语义：没有通道就静默丢弃。
        if self.audit_logger is None:
            return
        self.audit_logger.warn("audit event", "event", event, "module", "agent", *attrs)
        self.events.append((event, attrs))


class TestApplyTextToolRecovery(unittest.TestCase):
    def _result(self, text: str) -> LLMCallResult:
        return LLMCallResult(text=text, raw_text=text)

    def test_recovered_calls_continue_tool_loop(self) -> None:
        agent = _FakeAgent()
        result = apply_text_tool_recovery(agent, "s1", self._result(LEAK), tools_enabled=True)
        self.assertEqual([call.name for call in result.tool_calls], ["web_search"])
        self.assertEqual(json.loads(result.tool_calls[0].arguments), {"query": "最新游戏显卡 2026 型号"})
        self.assertEqual(result.text, "")
        self.assertEqual(result.raw_text, "")
        # 文本形态没有上游 id：接线层必须补一个唯一 id，工具转录才闭合得起来。
        self.assertTrue(result.tool_calls[0].id)
        uuid.UUID(result.tool_calls[0].id)
        events = [event for event, _attrs in agent.audits]
        self.assertEqual(events, ["llm_tool_calls_in_text"])
        attrs = agent.audits[0][1]
        self.assertIn("recovered", attrs)
        self.assertTrue(attrs[attrs.index("recovered") + 1])
        self.assertFalse(attrs[attrs.index("duplicated") + 1])
        self.assertEqual(agent.logger.records[0][1], "llm returned tool calls as text")

    def test_chat_session_only_cleans_text(self) -> None:
        agent = _FakeAgent()
        result = apply_text_tool_recovery(agent, "s2", self._result(LEAK), tools_enabled=False)
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.text, TEXT_TOOL_CALL_NOTICE)
        self.assertEqual(result.raw_text, TEXT_TOOL_CALL_NOTICE)

    def test_existing_structured_calls_win(self) -> None:
        agent = _FakeAgent()
        original = ToolCallRequest(id="call_1", name="web_search", arguments='{"query": "x"}')
        result = self._result("好的。" + LEAK)
        result.tool_calls = [original]
        apply_text_tool_recovery(agent, "s3", result, tools_enabled=True)
        self.assertEqual([call.id for call in result.tool_calls], ["call_1"])
        self.assertEqual(result.text, "好的。")
        attrs = agent.audits[0][1]
        self.assertTrue(attrs[attrs.index("duplicated") + 1])

    def test_plain_answer_untouched(self) -> None:
        agent = _FakeAgent()
        plain = self._result("RTX 5090 依旧是消费级旗舰。")
        same = apply_text_tool_recovery(agent, "s4", plain, tools_enabled=True)
        self.assertIs(same, plain)
        self.assertEqual(same.text, "RTX 5090 依旧是消费级旗舰。")
        self.assertEqual(same.tool_calls, [])
        self.assertEqual(agent.audits, [])
        self.assertEqual(agent.logger.records, [])

    def test_unparsable_leak_never_reaches_user(self) -> None:
        agent = _FakeAgent()
        # 只有协议标记、没有任何可识别调用：正文必须被替换，不能原样发出去。
        broken = "<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>"
        result = apply_text_tool_recovery(agent, "s5", self._result(broken), tools_enabled=True)
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.text, TEXT_TOOL_CALL_NOTICE)
        self.assertNotIn("｜DSML｜", result.text)


class TestLoggingWhenChannelsUnwired(unittest.TestCase):
    """回归：生产装配只给了 ``agent.logs``，通道没挂时 WARN / 审计也不能静默丢。"""

    def _result(self, text: str) -> LLMCallResult:
        return LLMCallResult(text=text, raw_text=text)

    def test_channels_are_mounted_from_agent_logs(self) -> None:
        logs = _FakeLogs()
        agent = _UnwiredAgent(logs)
        self.assertIsNone(agent.logger)
        self.assertIsNone(agent.audit_logger)
        apply_text_tool_recovery(agent, "s6", self._result(LEAK), tools_enabled=True)
        # 通道被按需补挂，且真的写了 WARN + 审计（只记工具名与计数）。
        self.assertIs(agent.logger, logs.runtime_logger)
        self.assertIs(agent.audit_logger, logs.audit_logger)
        self.assertEqual(logs.runtime_logger.warns[0][0], "llm returned tool calls as text")
        attrs = logs.runtime_logger.warns[0][1]
        self.assertEqual(attrs[attrs.index("tools") + 1], "web_search")
        self.assertEqual([event for event, _ in agent.events], ["llm_tool_calls_in_text"])
        audit_attrs = agent.events[0][1]
        self.assertEqual(audit_attrs[audit_attrs.index("tools") + 1], "web_search")
        audit_args = logs.audit_logger.warns[0][1]
        self.assertEqual(audit_args[audit_args.index("event") + 1], "llm_tool_calls_in_text")
        self.assertEqual(audit_args[audit_args.index("module") + 1], "agent")

    def test_no_logs_channel_does_not_crash(self) -> None:
        agent = _UnwiredAgent(None)
        result = apply_text_tool_recovery(agent, "s7", self._result(LEAK), tools_enabled=True)
        self.assertEqual([call.name for call in result.tool_calls], ["web_search"])
        self.assertIsNone(agent.logger)
        self.assertEqual(agent.events, [])


if __name__ == "__main__":
    unittest.main()
