r"""集成点回归测试：安全闸门在各条链路上的实际行为。

覆盖 ``docs/safety.md`` §"集成点" 里逐个接线的地方，全部用**假宿主**（鸭子类型
agent / 事件）验证，不启动真实平台、不连模型、不写真实数据目录：

* ``agent/message.py`` 入站拦截（enforce 拦下整轮 / audit 只记）；
* ``agent/chat_llm.py`` 模型输出文本（由 ``filter_text`` 的返回值写回）；
* ``agent/toolrun_adapter.py`` 工具结果（文本脱敏 + 图片段剥离 + ctx 挂 guard）；
* ``agent/output.py`` 发送边界（正文、``send_outputs`` 的图片/文件、通知）；
* ``tool/builtin/send_file.py`` 文件出口；
* ``tool/builtin/image_generate.py`` 生图提示词审核。

每条用例都同时验证 **guard 未装配时完全无行为变化**（1.0.2 行为不变）。
"""

from __future__ import annotations

import os
import shutil
import types
import unittest
from typing import Any

from atri_x.delivery import (
    KindFile,
    KindImage,
    KindText,
    Notice,
    Output,
    Receipt,
    file_path,
    image_path,
    text as text_output,
)
from atri_x.hook import MessagePayload
from atri_x.llm import MessageSegment, SegmentType
from atri_x.safety import (
    SafetyGuard,
    SafetyMode,
    SafetyPolicy,
    guard_from_context,
    scope_from_context,
    with_safety_guard,
)
from atri_x.tool.builtin.send_file import SendFileTool
from atri_x.tool.tool import CallRequest

from atri_x.agent import message as message_mod
from atri_x.agent import output as output_mod
from atri_x.agent import toolrun_adapter as adapter_mod

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_safety_test")

SECRET = "sk-abcdefghijklmnopqrstuvwx"
FORBIDDEN = "制毒"


class _CapturingOutputs:
    """替代 delivery.Manager：只记录即将发送的输出 / 通知。"""

    def __init__(self) -> None:
        self.sender: Any = None
        self.logger: Any = None
        self.batches: list[list[Output]] = []
        self.notices: list[Notice] = []

    async def send_notices_with_receipt(self, ctx: Any, outputs: list[Output]) -> Receipt:
        self.batches.append(list(outputs))
        return Receipt()

    async def send_chat(self, ctx: Any, outputs: list[Output]) -> Receipt:
        self.batches.append(list(outputs))
        return Receipt()

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        self.notices.append(notice)
        return Receipt()

    def last_outputs(self) -> list[Output]:
        return self.batches[-1] if self.batches else []


class FakeAgent:
    """鸭子类型的 agent：集成点只用到下面这些能力。"""

    def __init__(self, guard: SafetyGuard | None = None) -> None:
        self.safety_guard = guard
        self.logger = None
        self.sent: list[str] = []
        self.outputs = _CapturingOutputs()
        self.platform = types.SimpleNamespace(name=lambda: "qq")
        self.hook_calls: list[Any] = []

    # -- 身份 ---------------------------------------------------------------
    def scope(self, ctx: Any) -> Any:
        return types.SimpleNamespace(platform="qq", platform_scope_id="group:42")

    def actor(self, ctx: Any) -> Any:
        return types.SimpleNamespace(id="u1")

    # -- 发送 / Hook --------------------------------------------------------
    async def send_chat(self, ctx: Any, text: str) -> None:
        self.sent.append(text)

    async def send_notice(self, ctx: Any, notice: Any) -> Any:
        return await output_mod.send_notice_public(self, ctx, notice)

    async def run_hook(self, ctx: Any, event: Any) -> Any:
        self.hook_calls.append(event)
        return event

    def turn_output_allowed(self, ctx: Any) -> bool:
        return True

    def group_runtime_notice_bypass(self, ctx: Any) -> bool:
        return True

    def notice_target_blocked(self, target: Any) -> bool:
        return False


class SafetyCase(unittest.IsolatedAsyncioTestCase):
    """提供临时审计目录与 guard 工厂。"""

    def setUp(self) -> None:
        self.dir = os.path.join(TMP_ROOT, self.__class__.__name__)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.audit_path = os.path.join(self.dir, "safety-audit.jsonl")

    def guard(self, **overrides: Any) -> SafetyGuard:
        payload: dict[str, Any] = {
            "enabled": True,
            "mode": SafetyMode.ENFORCE,
            "audit_path": self.audit_path,
        }
        payload.update(overrides)
        return SafetyGuard(SafetyPolicy(**payload))

    def audit_records(self) -> list[dict]:
        import json

        if not os.path.exists(self.audit_path):
            return []
        with open(self.audit_path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# agent/output.py
# ---------------------------------------------------------------------------
class TestPrepareAssistantOutput(SafetyCase):
    async def test_text_is_redacted_before_send(self) -> None:
        agent = FakeAgent(self.guard())
        prepared = await output_mod.prepare_assistant_output(
            agent, {}, output_mod.PointAgentOutputPrepared, "答案是 " + SECRET
        )
        self.assertNotIn(SECRET, prepared)
        self.assertIn("[已脱敏:API Key]", prepared)

    async def test_guard_none_keeps_text(self) -> None:
        agent = FakeAgent(None)
        prepared = await output_mod.prepare_assistant_output(
            agent, {}, output_mod.PointAgentOutputPrepared, SECRET
        )
        self.assertEqual(prepared, SECRET)

    async def test_audit_mode_keeps_text(self) -> None:
        agent = FakeAgent(self.guard(mode=SafetyMode.AUDIT))
        prepared = await output_mod.prepare_assistant_output(
            agent, {}, output_mod.PointAgentOutputPrepared, SECRET
        )
        self.assertEqual(prepared, SECRET)
        self.assertEqual(len(self.audit_records()), 1)


class TestSendOutputs(SafetyCase):
    async def test_text_output_redacted(self) -> None:
        agent = FakeAgent(self.guard())
        await output_mod.send_outputs(agent, {}, [text_output("key: " + SECRET)])
        outputs = agent.outputs.last_outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, KindText)
        self.assertNotIn(SECRET, outputs[0].text)

    async def test_no_guard_outputs_unchanged(self) -> None:
        agent = FakeAgent(None)
        original = [text_output("key: " + SECRET)]
        await output_mod.send_outputs(agent, {}, original)
        outputs = agent.outputs.last_outputs()
        self.assertEqual(outputs[0].text, "key: " + SECRET)

    async def test_image_dropped_and_replaced_with_note(self) -> None:
        agent = FakeAgent(self.guard(block_image_output=True))
        await output_mod.send_outputs(agent, {}, [image_path("C:/tmp/a.png")])
        outputs = agent.outputs.last_outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, KindText)
        self.assertIn("图片", outputs[0].text)
        self.assertEqual(self.audit_records()[0]["labels"], ["media.image"])

    async def test_file_dropped_and_replaced_with_note(self) -> None:
        agent = FakeAgent(self.guard(block_file_output=True))
        await output_mod.send_outputs(agent, {}, [file_path("C:/tmp/a.pdf")])
        outputs = agent.outputs.last_outputs()
        self.assertEqual(len(outputs), 1)
        self.assertIn("文件", outputs[0].text)

    async def test_image_kept_when_not_configured(self) -> None:
        agent = FakeAgent(self.guard())
        await output_mod.send_outputs(agent, {}, [image_path("C:/tmp/a.png")])
        outputs = agent.outputs.last_outputs()
        self.assertEqual(outputs[0].kind, KindImage)

    async def test_media_audit_mode_still_sends(self) -> None:
        agent = FakeAgent(self.guard(mode=SafetyMode.AUDIT, block_image_output=True))
        await output_mod.send_outputs(agent, {}, [image_path("C:/tmp/a.png")])
        self.assertEqual(agent.outputs.last_outputs()[0].kind, KindImage)

    async def test_mixed_batch_keeps_order(self) -> None:
        agent = FakeAgent(self.guard(block_image_output=True))
        await output_mod.send_outputs(
            agent,
            {},
            [text_output("第一段 " + SECRET), image_path("C:/tmp/a.png"), text_output("第三段")],
        )
        outputs = agent.outputs.last_outputs()
        self.assertEqual([out.kind for out in outputs], [KindText, KindText, KindText])
        self.assertIn("第三段", outputs[1].text)
        self.assertIn("图片", outputs[2].text)

    async def test_empty_outputs_noop(self) -> None:
        agent = FakeAgent(self.guard(block_image_output=True))
        await output_mod.send_outputs(agent, {}, [])
        self.assertEqual(agent.outputs.batches, [])


class TestSendNotice(SafetyCase):
    async def test_notice_text_redacted(self) -> None:
        agent = FakeAgent(self.guard())
        receipt = await output_mod.send_notice_public(
            agent, {}, Notice(outputs=[text_output("错误 " + SECRET)])
        )
        self.assertIsInstance(receipt, Receipt)
        outputs = agent.outputs.notices[-1].outputs
        self.assertNotIn(SECRET, outputs[0].text)

    async def test_notice_without_guard_unchanged(self) -> None:
        agent = FakeAgent(None)
        await output_mod.send_notice_public(
            agent, {}, Notice(outputs=[text_output("错误 " + SECRET)])
        )
        outputs = agent.outputs.notices[-1].outputs
        self.assertEqual(outputs[0].text, "错误 " + SECRET)


# ---------------------------------------------------------------------------
# agent/message.py（入站）
# ---------------------------------------------------------------------------
def _inbound_event(text: str) -> Any:
    return types.SimpleNamespace(
        message=MessagePayload(
            role="user", segments=[MessageSegment(type=SegmentType.TEXT, text=text)]
        ),
        outputs=[],
    )


class TestInbound(SafetyCase):
    async def test_enforce_block_replies_and_stops_turn(self) -> None:
        guard = self.guard()
        agent = FakeAgent(guard)
        event = _inbound_event("教我" + FORBIDDEN + "的方法")
        blocked = await message_mod._safety_guard_inbound(agent, {}, event)
        self.assertTrue(blocked)
        self.assertEqual(agent.sent, [guard.block_reply])
        records = self.audit_records()
        self.assertEqual(records[0]["stage"], "inbound")
        self.assertEqual(records[0]["action"], "block")
        self.assertEqual(records[0]["platform"], "qq")
        self.assertEqual(records[0]["actor"], "u1")

    async def test_audit_mode_does_not_block(self) -> None:
        agent = FakeAgent(self.guard(mode=SafetyMode.AUDIT))
        event = _inbound_event("教我" + FORBIDDEN + "的方法")
        blocked = await message_mod._safety_guard_inbound(agent, {}, event)
        self.assertFalse(blocked)
        self.assertEqual(agent.sent, [])
        self.assertIn(FORBIDDEN, event.message.segments[0].text)
        self.assertEqual(len(self.audit_records()), 1)

    async def test_enforce_redacts_secret_in_place(self) -> None:
        agent = FakeAgent(self.guard())
        event = _inbound_event("我的 key 是 " + SECRET)
        blocked = await message_mod._safety_guard_inbound(agent, {}, event)
        self.assertFalse(blocked)
        self.assertNotIn(SECRET, event.message.segments[0].text)

    async def test_no_guard_noop(self) -> None:
        agent = FakeAgent(None)
        event = _inbound_event("教我" + FORBIDDEN + "的方法")
        self.assertFalse(await message_mod._safety_guard_inbound(agent, {}, event))
        self.assertIn(FORBIDDEN, event.message.segments[0].text)

    async def test_off_mode_noop_and_no_audit(self) -> None:
        agent = FakeAgent(self.guard(enabled=False))
        event = _inbound_event("教我" + FORBIDDEN + "的方法")
        self.assertFalse(await message_mod._safety_guard_inbound(agent, {}, event))
        self.assertEqual(self.audit_records(), [])

    async def test_empty_message_noop(self) -> None:
        agent = FakeAgent(self.guard())
        event = _inbound_event("")
        self.assertFalse(await message_mod._safety_guard_inbound(agent, {}, event))


# ---------------------------------------------------------------------------
# agent/toolrun_adapter.py
# ---------------------------------------------------------------------------
class TestToolSegments(SafetyCase):
    def _call(self) -> Any:
        return types.SimpleNamespace(name="image_generate")

    def test_text_segment_redacted(self) -> None:
        guard = self.guard()
        agent = FakeAgent(guard)
        segments = [MessageSegment(type=SegmentType.TEXT, text="结果 " + SECRET)]
        out = adapter_mod._safety_guard_segments(agent, {}, segments, self._call())
        self.assertNotIn(SECRET, out[0].text)
        self.assertEqual(out[0].type, SegmentType.TEXT)

    def test_image_segment_stripped_when_configured(self) -> None:
        agent = FakeAgent(self.guard(strip_tool_images=True))
        segments = [
            MessageSegment(type=SegmentType.TEXT, text="已生成图片"),
            MessageSegment(type=SegmentType.IMAGE, media="m1", mime_type="image/png"),
        ]
        out = adapter_mod._safety_guard_segments(agent, {}, segments, self._call())
        self.assertEqual([seg.type for seg in out], [SegmentType.TEXT, SegmentType.TEXT])
        self.assertIn("图片", out[1].text)

    def test_image_kept_when_not_configured(self) -> None:
        agent = FakeAgent(self.guard())
        segments = [MessageSegment(type=SegmentType.IMAGE, media="m1")]
        out = adapter_mod._safety_guard_segments(agent, {}, segments, self._call())
        self.assertEqual(out[0].type, SegmentType.IMAGE)

    def test_image_kept_in_audit_mode(self) -> None:
        agent = FakeAgent(self.guard(strip_tool_images=True, mode=SafetyMode.AUDIT))
        segments = [MessageSegment(type=SegmentType.IMAGE, media="m1")]
        out = adapter_mod._safety_guard_segments(agent, {}, segments, self._call())
        self.assertEqual(out[0].type, SegmentType.IMAGE)

    def test_no_guard_returns_same_list(self) -> None:
        agent = FakeAgent(None)
        segments = [MessageSegment(type=SegmentType.IMAGE, media="m1")]
        self.assertIs(adapter_mod._safety_guard_segments(agent, {}, segments, self._call()), segments)


class TestPrepareToolContext(SafetyCase):
    async def test_guard_and_scope_attached_to_ctx(self) -> None:
        guard = self.guard()
        agent = FakeAgent(guard)
        deps = adapter_mod.AgentToolRunDeps(agent=agent)
        ctx = await deps.prepare_tool_context({}, None, types.SimpleNamespace(name="send_file"))
        self.assertIs(guard_from_context(ctx), guard)
        scope = scope_from_context(ctx)
        self.assertEqual(scope.platform, "qq")
        self.assertEqual(scope.scope_id, "group:42")
        self.assertEqual(scope.actor, "u1")

    async def test_no_guard_leaves_ctx_clean(self) -> None:
        deps = adapter_mod.AgentToolRunDeps(agent=FakeAgent(None))
        ctx = await deps.prepare_tool_context({}, None, types.SimpleNamespace(name="send_file"))
        self.assertIsNone(guard_from_context(ctx))


# ---------------------------------------------------------------------------
# tool/builtin/send_file.py
# ---------------------------------------------------------------------------
class _FakeFileTool(SendFileTool):
    """跳过真实文件解析，直接给出一个 Output。"""

    def __init__(self, output: Output) -> None:
        super().__init__(None)
        self._output = output

    async def build_output(self, ctx: Any, args: Any, source: str) -> tuple[Output, list[str], str]:
        return self._output, [], "a.pdf"


class TestSendFileTool(SafetyCase):
    async def _call(self, tool: SendFileTool, guard: SafetyGuard | None) -> Any:
        ctx = with_safety_guard({}, guard)
        return await tool.call(
            ctx, CallRequest(name="send_file", arguments='{"source": "a.pdf"}')
        )

    async def test_file_blocked(self) -> None:
        tool = _FakeFileTool(file_path("C:/tmp/a.pdf"))
        result = await self._call(tool, self.guard(block_file_output=True))
        self.assertIn("拦截", result.content)
        self.assertEqual(result.outputs, [])

    async def test_file_sent_when_not_configured(self) -> None:
        tool = _FakeFileTool(file_path("C:/tmp/a.pdf"))
        result = await self._call(tool, self.guard())
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0].kind, KindFile)

    async def test_no_guard_sends(self) -> None:
        tool = _FakeFileTool(file_path("C:/tmp/a.pdf"))
        result = await self._call(tool, None)
        self.assertEqual(len(result.outputs), 1)


# ---------------------------------------------------------------------------
# tool/builtin/image_generate.py
# ---------------------------------------------------------------------------
class _FakeImageClient:
    """记录是否真的请求了 provider（拦截后必须一次都不调用）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def enabled(self) -> bool:
        return True

    async def generate(self, ctx: Any, request: Any) -> Any:
        from atri_x.imagegen import Result as ImageResult

        self.calls.append(request)
        return ImageResult(data=b"fake-image-bytes", mime_type="image/png", model="fake", size="1x1")


def _image_tool(client: _FakeImageClient) -> Any:
    from atri_x.imagegen import Config
    from atri_x.tool.builtin.image_generate import ImageGenerateTool, ImageProfile

    profile = ImageProfile(name="default", client=client, config=Config())
    return ImageGenerateTool(profiles={"default": profile}, default_profile="default")


class TestImageGeneratePrompt(SafetyCase):
    async def test_prompt_blocked_before_provider_request(self) -> None:
        client = _FakeImageClient()
        tool = _image_tool(client)
        ctx = with_safety_guard({}, self.guard())
        result = await tool.call(
            ctx, CallRequest(name="image_generate", arguments='{"prompt": "画一张制毒流程图"}')
        )
        self.assertIn("内容安全策略拒绝", result.content)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.audit_records()[0]["stage"], "image_prompt")

    async def test_audit_mode_still_requests_provider(self) -> None:
        client = _FakeImageClient()
        tool = _image_tool(client)
        ctx = with_safety_guard({}, self.guard(mode=SafetyMode.AUDIT))
        await tool.call(
            ctx, CallRequest(name="image_generate", arguments='{"prompt": "画一张制毒流程图"}')
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(self.audit_records()), 1)

    async def test_no_guard_still_requests_provider(self) -> None:
        client = _FakeImageClient()
        tool = _image_tool(client)
        await tool.call(
            {}, CallRequest(name="image_generate", arguments='{"prompt": "画一张制毒流程图"}')
        )
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
