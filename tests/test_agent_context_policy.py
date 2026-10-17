"""``atri_x.agent.context_policy`` / ``context_usage`` 的测试。

对应 Go：``internal/agent/context_policy_test.go``（长消息保护的判定与处理）与
``context_usage.go`` 的状态文本。
"""

from __future__ import annotations

import unittest

from atri_x.agent.context_policy import (
    ContextOverflowState,
    ContextPolicyMixin,
    PromptOverflowInfo,
    TRUNCATION_MARKER,
    context_overflow_key,
    ensure_context_overflow_state,
    normalize_overflow_target,
    valid_overflow_mode,
)
from atri_x.agent.context_runtime import ContextRuntimeState
from atri_x.agent.context_usage import render_context_status
from atri_x.config import (
    ContextConfig,
    ContextOverflowConfig,
    ModelMetadataConfig,
    ModelSelection,
    ProviderConfig,
)
from atri_x.contextmgr import new_prompt_budget
from atri_x.errors import PromptOverflowError
from atri_x.llm.llm import LLMMessage, Role, text_segments
from atri_x.session.types import Scope
from atri_x.storage.models import Session, SessionModeChat, SessionModeWork


class _Out:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def send_notice(self, ctx, level: str, text: str) -> None:
        self.notices.append((level, text))


class _Scope:
    platform = "qq"
    platform_scope_id = "group:1"


def _runtime(window: int = 100, *, overflow_mode: str = "reject") -> ContextRuntimeState:
    runtime = ContextRuntimeState()
    cfg = ContextConfig(overflow_mode=overflow_mode)
    runtime.configure(
        cfg,
        ModelMetadataConfig(default_context_window=window),
        {"p": ProviderConfig(models=["m"])},
        ModelSelection(),
        None,
    )
    return runtime


class _Host(ContextPolicyMixin):
    def __init__(self, runtime: ContextRuntimeState | None = None, *, mode: str = "work") -> None:
        self.context_runtime = runtime or _runtime()
        self.session_mode = mode
        self.saved = 0
        self.save_error: BaseException | None = None
        self.runtime_context: object = None

    # -- 宿主契约 -----------------------------------------------------------
    def scope(self, ctx) -> _Scope:
        return _Scope()

    def command_prefix(self) -> str:
        return "/"

    def current_mode(self, ctx) -> str:
        return self.session_mode

    def compact_selection_for_session(self, session):
        return ModelSelection("p", "compact")

    def save_runtime_state(self) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved += 1

    async def load_session(self, ctx, session_id: str):
        return Session(id=session_id, mode=self.session_mode)


class TestPureHelpers(unittest.TestCase):
    def test_context_overflow_key(self) -> None:
        self.assertEqual(context_overflow_key(_Scope()), "qq:group:1")

        class _Empty:
            platform = ""
            platform_scope_id = ""

        self.assertEqual(context_overflow_key(_Empty()), "unknown")

    def test_valid_overflow_mode(self) -> None:
        self.assertTrue(valid_overflow_mode(" Truncate "))
        self.assertTrue(valid_overflow_mode("summarize"))
        self.assertFalse(valid_overflow_mode("nope"))
        self.assertFalse(valid_overflow_mode(""))

    def test_normalize_overflow_target(self) -> None:
        self.assertEqual(normalize_overflow_target("Chat"), "chat")
        self.assertEqual(normalize_overflow_target("work"), "work")
        self.assertEqual(normalize_overflow_target("all"), "all")
        self.assertEqual(normalize_overflow_target(""), "all")

    def test_overflow_info_flags(self) -> None:
        info = PromptOverflowInfo(
            budget=new_prompt_budget(100, 0.8, 0.5, 0),
            estimated_tokens=0,
            current_tokens=0,
        )
        self.assertFalse(info.exceeded())
        info.current_tokens = info.budget.single_message_limit + 1
        self.assertTrue(info.single_message_exceeded())
        self.assertTrue(info.exceeded())
        self.assertEqual(info.reason, "")
        info2 = PromptOverflowInfo(budget=info.budget, estimated_tokens=10_000)
        self.assertTrue(info2.total_exceeded())

    def test_overflow_state_store(self) -> None:
        state = ContextOverflowState({"qq:g1": ContextOverflowConfig(chat="truncate")})
        self.assertEqual(state.get("qq:g1").chat, "truncate")
        snapshot = state.snapshot()
        snapshot["qq:g1"].chat = "changed"
        self.assertEqual(state.get("qq:g1").chat, "truncate")
        # 两个模式都空 -> 删除键
        state.set("qq:g2", ContextOverflowConfig(work="  "))
        self.assertIsNone(state.get("qq:g2"))
        state.set("qq:g2", ContextOverflowConfig(work="summarize"))
        self.assertEqual(state.get("qq:g2").work, "summarize")
        state.replace({})
        self.assertIsNone(state.get("qq:g1"))

    def test_render_context_status_unknown_and_known(self) -> None:
        text = render_context_status(None, 100, compact_enabled=True, compact_trigger_ratio=0.8)
        self.assertIn("context usage: unknown", text)
        self.assertIn("compact threshold: 80%", text)
        self.assertIn("compact status: unknown", text)

        from atri_x.llm.llm import Usage

        text = render_context_status(
            Usage(total_tokens=85, cache_hit_tokens=5), 100,
            compact_enabled=True, compact_trigger_ratio=0.8,
        )
        self.assertIn("context usage: 85.0%", text)
        self.assertIn("compact status: will compact before next request", text)
        self.assertIn("tokens：85（命中：5）", text)

        text = render_context_status(
            Usage(total_tokens=75), 100, compact_enabled=True, compact_trigger_ratio=0.8
        )
        self.assertIn("compact status: near threshold", text)
        text = render_context_status(
            Usage(total_tokens=10), 100, compact_enabled=True, compact_trigger_ratio=0.8
        )
        self.assertIn("compact status: ok", text)
        text = render_context_status(
            Usage(total_tokens=10), 100, compact_enabled=False, compact_trigger_ratio=0.8
        )
        self.assertIn("compact status: disabled", text)


class TestScopeOverrides(unittest.TestCase):
    def test_snapshot_and_scope_helpers(self) -> None:
        host = _Host()
        self.assertEqual(host.context_overflow_snapshot(), {})
        host.set_context_overflow_for_scope(_Scope(), ContextOverflowConfig(chat="summarize"))
        value, ok = host.context_overflow_for_scope(_Scope())
        self.assertTrue(ok)
        self.assertEqual(value.chat, "summarize")
        self.assertEqual(host.context_overflow_snapshot(), {"qq:group:1": value})
        host.set_context_overflow_snapshot({})
        self.assertFalse(host.context_overflow_for_scope(_Scope())[1])
        self.assertIsInstance(ensure_context_overflow_state(host), ContextOverflowState)

    def test_mode_resolution_prefers_scope_then_global(self) -> None:
        host = _Host(_runtime(overflow_mode="summarize"))
        self.assertEqual(host.context_overflow_mode(None, None), "summarize")
        host.set_context_overflow_for_scope(
            _Scope(), ContextOverflowConfig(work="truncate", chat="reject")
        )
        self.assertEqual(
            host.context_overflow_mode(None, Session(id="s", mode=SessionModeWork)), "truncate"
        )
        self.assertEqual(
            host.context_overflow_mode(None, Session(id="s", mode=SessionModeChat)), "reject"
        )
        # 覆盖里没有该模式 -> 回落全局
        host.set_context_overflow_for_scope(_Scope(), ContextOverflowConfig(work="truncate"))
        self.assertEqual(
            host.context_overflow_mode(None, Session(id="s", mode=SessionModeChat)), "summarize"
        )
        # 全局非法 -> reject
        bad = _Host(_runtime(overflow_mode="bogus"))
        self.assertEqual(bad.context_overflow_mode(None, None), "reject")

    def test_set_and_reset_policy_persist(self) -> None:
        import asyncio

        host = _Host()

        async def run() -> None:
            text = await host.set_context_policy(None, "chat", "Summarize")
            self.assertEqual(text, "长消息保护策略已更新：chat = summarize")
            self.assertEqual(host.saved, 1)
            with self.assertRaises(Exception):
                await host.set_context_policy(None, "chat", "bogus")
            # 落盘失败要回滚内存
            host.save_error = RuntimeError("disk")
            with self.assertRaises(RuntimeError):
                await host.set_context_policy(None, "work", "truncate")
            override, _ = host.context_overflow_for_scope(_Scope())
            self.assertEqual(override.work, "")
            self.assertEqual(override.chat, "summarize")
            host.save_error = None
            self.assertEqual(
                await host.reset_context_policy(None, "all"),
                "长消息保护策略已重置：all 使用全局默认",
            )
            self.assertFalse(host.context_overflow_for_scope(_Scope())[1])

        asyncio.run(run())

    def test_status_text(self) -> None:
        import asyncio

        host = _Host(_runtime(overflow_mode="truncate"))

        async def run() -> None:
            host.set_context_overflow_for_scope(_Scope(), ContextOverflowConfig(chat="reject"))
            text = await host.context_policy_status(None)
            self.assertIn("长消息保护策略", text)
            self.assertIn("作用域：qq:group:1", text)
            self.assertIn("当前模式：work", text)
            self.assertIn("chat：reject（当前群覆盖）", text)
            self.assertIn("work：truncate（全局默认）", text)
            self.assertIn("全局默认：truncate", text)
            self.assertIn("/overflow reset", text)

        asyncio.run(run())


class TestOverflowHandling(unittest.IsolatedAsyncioTestCase):
    def _long_message(self, chars: int) -> LLMMessage:
        return LLMMessage(role=Role.USER, segments=text_segments("字" * chars))

    async def test_detect_single_message(self) -> None:
        host = _Host(_runtime(100))
        messages = [LLMMessage(role=Role.USER, segments=text_segments("字" * 80))]
        info, exceeded = await host.detect_prompt_overflow(None, None, ModelSelection("p", "m"), messages, [])
        self.assertTrue(exceeded)
        self.assertEqual(info.reason, "single_message")
        self.assertEqual(info.current_tokens, 80 + 4)

    async def test_detect_total_only(self) -> None:
        host = _Host(_runtime(100))
        messages = [
            LLMMessage(role=Role.SYSTEM, segments=text_segments("字" * 100)),
            LLMMessage(role=Role.USER, segments=text_segments("hi")),
        ]
        info, exceeded = await host.detect_prompt_overflow(None, None, ModelSelection("p", "m"), messages, [])
        self.assertTrue(exceeded)
        self.assertEqual(info.reason, "total_prompt")

    async def test_no_window_means_no_overflow(self) -> None:
        runtime = _runtime(0)
        runtime.window_resolver = None
        runtime.model_metadata = ModelMetadataConfig(default_context_window=0)
        host = _Host(runtime)
        info, exceeded = await host.detect_prompt_overflow(
            None, None, ModelSelection("p", "m"), [self._long_message(1000)], []
        )
        self.assertFalse(exceeded)
        self.assertEqual(info.budget.window, 256000)

    async def test_truncate_rewrites_last_message(self) -> None:
        host = _Host(_runtime(100, overflow_mode="truncate"))
        messages = [self._long_message(80)]
        out = _Out()
        ok, modified = await host.handle_initial_prompt_overflow(
            None, Session(id="s", mode=SessionModeWork), ModelSelection("p", "m"), messages, [], out
        )
        self.assertTrue(ok)
        self.assertTrue(modified)
        text = messages[0].segments[0].text
        self.assertTrue(text.endswith(TRUNCATION_MARKER))
        self.assertEqual(len(out.notices), 1)
        self.assertEqual(out.notices[0][0], "warning")
        self.assertIn("已自动截断后继续处理", out.notices[0][1])

    async def test_total_overflow_is_rejected_even_with_truncate(self) -> None:
        host = _Host(_runtime(100, overflow_mode="truncate"))
        messages = [
            LLMMessage(role=Role.SYSTEM, segments=text_segments("字" * 100)),
            LLMMessage(role=Role.USER, segments=text_segments("hi")),
        ]
        out = _Out()
        ok, modified = await host.handle_initial_prompt_overflow(
            None, None, ModelSelection("p", "m"), messages, [], out
        )
        self.assertFalse(ok)
        self.assertFalse(modified)
        self.assertEqual(messages[1].segments[0].text, "hi")
        self.assertIn("未发送给模型", out.notices[0][1])

    async def test_no_overflow_returns_ok(self) -> None:
        host = _Host(_runtime(100))
        messages = [LLMMessage(role=Role.USER, segments=text_segments("hi"))]
        out = _Out()
        self.assertEqual(
            await host.handle_initial_prompt_overflow(
                None, None, ModelSelection("p", "m"), messages, [], out
            ),
            (True, False),
        )
        self.assertEqual(out.notices, [])

    async def test_empty_messages_is_ok(self) -> None:
        host = _Host(_runtime(100))
        self.assertEqual(
            await host.handle_initial_prompt_overflow(
                None, None, ModelSelection("p", "m"), [], [], _Out()
            ),
            (True, False),
        )

    async def test_summarize_success_replaces_message(self) -> None:
        runtime = _runtime(100, overflow_mode="summarize")
        self.summarized = None

        class _Compactor:
            def name(self) -> str:
                return "fake"

            async def summarize_text(self, ctx, req):
                from atri_x.contextmgr import SummarizeResult

                outer.summarized = req
                return SummarizeResult(summary="SUMMARY")

        outer = self
        runtime.compactor = _Compactor()
        host = _Host(runtime)
        messages = [self._long_message(80)]
        out = _Out()
        ok, modified = await host.handle_initial_prompt_overflow(
            None, Session(id="s", mode=SessionModeWork), ModelSelection("p", "m"), messages, [], out
        )
        self.assertTrue(ok)
        self.assertTrue(modified)
        self.assertEqual(messages[0].segments[0].text, "SUMMARY")
        self.assertIsNotNone(self.summarized)
        self.assertEqual(self.summarized.provider, "p")
        self.assertIn("已自动摘要后继续处理", out.notices[0][1])

    async def test_summarize_failure_rejects(self) -> None:
        runtime = _runtime(100, overflow_mode="summarize")

        class _Compactor:
            def name(self) -> str:
                return "fake"

            async def summarize_text(self, ctx, req):
                raise RuntimeError("boom")

        runtime.compactor = _Compactor()
        host = _Host(runtime)
        messages = [self._long_message(80)]
        out = _Out()
        ok, modified = await host.handle_initial_prompt_overflow(
            None, None, ModelSelection("p", "m"), messages, [], out
        )
        self.assertFalse(ok)
        self.assertFalse(modified)
        self.assertIn("自动摘要失败", out.notices[0][1])

    async def test_guard_raises_prompt_overflow(self) -> None:
        host = _Host(_runtime(100))
        out = _Out()
        with self.assertRaises(PromptOverflowError):
            await host.guard_prompt_before_call(
                None, "s1", ModelSelection("p", "m"), [self._long_message(80)], [], out
            )
        self.assertIn("工具阶段上下文超限", out.notices[0][1])

    async def test_guard_passes_when_within_budget(self) -> None:
        host = _Host(_runtime(100))
        out = _Out()
        await host.guard_prompt_before_call(
            None, "s1", ModelSelection("p", "m"), [LLMMessage(role=Role.USER, segments=text_segments("hi"))], [], out
        )
        self.assertEqual(out.notices, [])

    def test_alert_text_contains_policy_lines(self) -> None:
        host = _Host(_runtime(100))
        info = PromptOverflowInfo(
            budget=new_prompt_budget(100, 0.8, 0.5, 0),
            estimated_tokens=120,
            current_tokens=80,
        )
        text = host.overflow_alert_text(
            Session(id="s", mode=SessionModeChat), ModelSelection("p", "m"), info, "reject", "已拒绝"
        )
        self.assertIn("⚠️ 长消息保护已触发", text)
        self.assertIn("模式：chat / 策略：reject", text)
        self.assertIn("模型：p/m", text)
        self.assertIn("模型窗口：100 tokens；可用输入：80 tokens", text)
        self.assertIn("估算输入：120 tokens；本条消息：80 tokens", text)
        self.assertIn("单条消息上限：50 tokens", text)
        self.assertIn("处理：已拒绝", text)
        self.assertIn("/overflow --chat summarize", text)

    async def test_load_session_returns_none_without_store(self) -> None:
        class _Bare(ContextPolicyMixin):
            pass

        self.assertIsNone(await _Bare().load_session(None, "x"))
        # 有 store 时按 ID 取；取不到不算错误
        host = _Host(_runtime(100))
        self.assertEqual((await host.load_session(None, "s1")).id, "s1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
