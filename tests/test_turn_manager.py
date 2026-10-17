"""``atri_x.turn`` 与 ``atri_x.runtime`` 的单元测试。

对应 Go：``internal/turn/manager_test.go`` + ``internal/runtime/status_test.go``。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.llm.llm import MessageSegment, SegmentType, Usage, text_segments
from atri_x.runtime import (
    PhaseError,
    PhaseIdle,
    PhaseLLM,
    PhaseTool,
    Snapshot as RuntimeSnapshot,
    format_compact,
    format_duration,
    format_int,
    format_model,
    format_usage,
    truncate_runes,
)
from atri_x.turn import (
    Input,
    Manager,
    PhaseAwaitAppendConfirm,
    PhaseAwaitRiskConfirm,
    PhaseIdle as TurnPhaseIdle,
    PhaseLLM as TurnPhaseLLM,
    PhaseTool as TurnPhaseTool,
    RiskConfirmation,
    RiskConfirmationResponse,
    Speaker,
    is_cancel,
    is_confirm,
    merge_inputs,
    tools_string,
)


class TestTurnManager(unittest.IsolatedAsyncioTestCase):
    def test_drain_merged_input_preserves_media_and_platform_text(self) -> None:
        manager = Manager()
        self.assertTrue(
            manager.start_llm_input(
                "s1",
                Input(
                    text="start",
                    platform_text="raw start",
                    segments=text_segments("start"),
                ),
            )
        )
        self.assertTrue(manager.start_tool_phase("s1"))
        manager.append_pending_input(
            "s1",
            Input(
                text="first",
                platform_text="raw first",
                segments=[
                    MessageSegment(type=SegmentType.TEXT, text="first"),
                    MessageSegment(type=SegmentType.IMAGE, url="image-1"),
                ],
            ),
        )
        manager.append_pending_input(
            "s1",
            Input(
                text="second",
                platform_text="raw second",
                segments=[
                    MessageSegment(type=SegmentType.TEXT, text="second"),
                    MessageSegment(type=SegmentType.IMAGE, url="image-2"),
                ],
            ),
        )
        merged = manager.drain_merged_input("s1")
        self.assertEqual(merged.text, "补充信息：\n1. first\n2. second")
        self.assertEqual(merged.platform_text, "补充信息：\n1. raw first\n2. raw second")
        images = [s for s in merged.segments if s.type == SegmentType.IMAGE]
        self.assertEqual(len(images), 2)
        self.assertEqual(manager.snapshot("s1").pending_count, 0)

    def test_confirm_and_cancel_tokens(self) -> None:
        for token in ("$", "是", "y", "yes", " YES "):
            self.assertTrue(is_confirm(token), token)
        for token in ("取消", "否", "n", "no", " NO "):
            self.assertTrue(is_cancel(token), token)
        self.assertFalse(is_confirm("no"))
        self.assertFalse(is_cancel("yes"))

    def test_interrupt_and_confirm_merges_original_input(self) -> None:
        manager = Manager()
        self.assertTrue(manager.start_llm("s1", "zz"))
        self.assertTrue(manager.interrupt_llm("s1", "等等！"))
        self.assertTrue(manager.append_pending("s1", "再补一句"))
        snapshot = manager.snapshot("s1")
        self.assertEqual(snapshot.phase, PhaseAwaitAppendConfirm)
        self.assertEqual(snapshot.pending_count, 2)
        merged, ok = manager.confirm_append("s1")
        self.assertTrue(ok)
        self.assertEqual(merged, "补充信息：\n1. zz\n2. 等等！\n3. 再补一句")
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    def test_start_llm_rejects_second_turn(self) -> None:
        manager = Manager()
        self.assertTrue(manager.start_llm("s1", "a"))
        self.assertFalse(manager.start_llm("s1", "b"))
        self.assertTrue(manager.complete_llm("s1")[1])
        self.assertTrue(manager.start_llm("s1", "b"))

    def test_cancel_append_drops_turn(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        manager.interrupt_llm("s1", "等等！")
        self.assertTrue(manager.cancel_append("s1"))
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    def test_tool_pending_drain(self) -> None:
        manager = Manager()
        manager.add_tool_use("s1", "web_search")
        manager.add_tool_use("s1", "web_search")
        self.assertTrue(manager.append_pending("s1", "补充 A"))
        self.assertTrue(manager.append_pending("s1", "补充 B"))
        snapshot = manager.snapshot("s1")
        self.assertEqual(snapshot.phase, TurnPhaseTool)
        self.assertEqual(snapshot.pending_count, 2)
        self.assertEqual(tools_string(snapshot.tools), "web_search x2")
        merged = manager.drain_merged("s1")
        self.assertEqual(merged, "补充信息：\n1. 补充 A\n2. 补充 B")
        self.assertEqual(manager.snapshot("s1").pending_count, 0)

    def test_complete_llm_returns_merged_pending(self) -> None:
        manager = Manager()
        self.assertTrue(manager.start_llm("s1", "先查"))
        self.assertTrue(manager.start_tool_phase("s1"))
        manager.append_pending("s1", "补充 A")
        manager.append_pending("s1", "补充 B")
        pending, ok = manager.complete_llm("s1")
        self.assertTrue(ok)
        self.assertEqual(pending, "补充信息：\n1. 补充 A\n2. 补充 B")
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    def test_append_pending_rejected_outside_tool_or_append(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "a")
        self.assertFalse(manager.append_pending("s1", "b"))
        self.assertFalse(manager.append_pending("missing", "b"))
        self.assertEqual(manager.snapshot("s1").pending_count, 0)

    def test_compact_phase_round_trip(self) -> None:
        manager = Manager()
        self.assertTrue(manager.start_compact("s1"))
        self.assertFalse(manager.start_compact("s1"))
        self.assertFalse(manager.start_llm("s1", "a"))
        self.assertTrue(manager.complete_compact("s1"))
        self.assertFalse(manager.complete_compact("s1"))

    def test_stop_all_clears_turns(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "a")
        manager.start_llm("s2", "b")
        manager.stop_all()
        self.assertEqual(manager.snapshot_all(), [])
        manager.start_llm("s2", "b")
        manager.stop_session("s2")
        self.assertEqual(manager.snapshot("s2").phase, TurnPhaseIdle)

    def test_snapshot_all_is_sorted(self) -> None:
        manager = Manager()
        manager.start_llm("s2", "b")
        manager.start_llm("s1", "a")
        self.assertEqual([item.session_id for item in manager.snapshot_all()], ["s1", "s2"])

    def test_finish_request_clears_tool_pending(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        manager.start_tool_phase("s1")
        manager.append_pending("s1", "补充 A")
        manager.finish_request("s1")
        snapshot = manager.snapshot("s1")
        self.assertEqual(snapshot.phase, TurnPhaseIdle)
        self.assertEqual(snapshot.pending_count, 0)

    def test_finish_request_keeps_append_confirmation(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        manager.interrupt_llm("s1", "等等！")
        manager.finish_request("s1")
        snapshot = manager.snapshot("s1")
        self.assertEqual(snapshot.phase, PhaseAwaitAppendConfirm)
        self.assertEqual(snapshot.pending_count, 1)

    async def test_finish_request_stops_risk_confirmation(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        manager.start_tool_phase("s1")
        results: list[tuple[RiskConfirmationResponse, bool]] = []

        async def wait() -> None:
            results.append(
                await manager.await_risk_confirmation(
                    "s1", RiskConfirmation(id="c1", tool_name="shell")
                )
            )

        task = asyncio.ensure_future(wait())
        for _ in range(500):
            if manager.snapshot("s1").phase == PhaseAwaitRiskConfirm:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(manager.snapshot("s1").phase, PhaseAwaitRiskConfirm)
        manager.finish_request("s1")
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0].stopped)
        self.assertFalse(results[0][1])
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    async def test_resolve_and_refresh_risk_confirmation(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        manager.start_tool_phase("s1")
        results: list[tuple[RiskConfirmationResponse, bool]] = []

        async def wait() -> None:
            results.append(
                await manager.await_risk_confirmation(
                    "s1", RiskConfirmation(id="c1", tool_name="shell")
                )
            )

        task = asyncio.ensure_future(wait())
        for _ in range(500):
            if manager.snapshot("s1").phase == PhaseAwaitRiskConfirm:
                break
            await asyncio.sleep(0.002)
        pending, ok = manager.pending_risk_confirmation("s1")
        self.assertTrue(ok)
        self.assertEqual(pending.tool_name, "shell")
        self.assertTrue(manager.refresh_risk_confirmation("s1"))
        self.assertTrue(
            manager.resolve_risk_confirmation(
                "s1", RiskConfirmationResponse(confirmed=True)
            )
        )
        await asyncio.wait_for(task, 2)
        self.assertTrue(results[0][1])
        self.assertTrue(results[0][0].confirmed)
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseTool)
        self.assertFalse(manager.pending_risk_confirmation("s1")[1])
        self.assertFalse(
            manager.resolve_risk_confirmation("s1", RiskConfirmationResponse())
        )

    async def test_await_risk_confirmation_requires_tool_phase(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "zz")
        response, ok = await manager.await_risk_confirmation("s1", RiskConfirmation())
        self.assertTrue(response.stopped)
        self.assertFalse(ok)

    async def test_append_confirmation_expiration_refreshes_on_content(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "start")
        manager.interrupt_llm("s1", "more")
        expired: list[bool] = []

        async def wait() -> None:
            expired.append(await manager.await_append_expiration("s1", 0.08))

        task = asyncio.ensure_future(wait())
        await asyncio.sleep(0.05)
        self.assertTrue(manager.append_pending("s1", "latest"))
        await asyncio.sleep(0.05)
        self.assertEqual(manager.snapshot("s1").phase, PhaseAwaitAppendConfirm)
        await asyncio.wait_for(task, 2)
        self.assertEqual(expired, [True])
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    async def test_expired_append_wait_cannot_remove_new_turn(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "first")
        manager.interrupt_llm("s1", "more")
        old: list[bool] = []

        async def wait() -> None:
            old.append(await manager.await_append_expiration("s1", 0.1))

        task = asyncio.ensure_future(wait())
        await asyncio.sleep(0.01)
        self.assertTrue(manager.cancel_append("s1"))
        self.assertTrue(manager.start_llm("s1", "second"))
        self.assertTrue(manager.interrupt_llm("s1", "new more"))
        await asyncio.wait_for(task, 2)
        self.assertEqual(old, [False])
        await asyncio.sleep(0.11)
        self.assertEqual(manager.snapshot("s1").phase, PhaseAwaitAppendConfirm)

    async def test_risk_confirmation_expiration_refreshes_on_detail(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "start")
        manager.start_tool_phase("s1")
        results: list[RiskConfirmationResponse] = []

        async def wait() -> None:
            response, _ok = await manager.await_risk_confirmation_context(
                None, "s1", RiskConfirmation(id="c1", tool_name="shell"), 0.08
            )
            results.append(response)

        task = asyncio.ensure_future(wait())
        for _ in range(500):
            if manager.snapshot("s1").phase == PhaseAwaitRiskConfirm:
                break
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.05)
        self.assertTrue(manager.refresh_risk_confirmation("s1"))
        await asyncio.sleep(0.05)
        self.assertEqual(results, [])
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].stopped)
        self.assertTrue(results[0].expired)
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    async def test_append_expiration_disabled_for_non_positive_timeout(self) -> None:
        manager = Manager()
        manager.start_llm("s1", "a")
        manager.interrupt_llm("s1", "b")
        self.assertFalse(await manager.await_append_expiration("s1", 0))

    async def test_risk_confirmation_context_cancel_removes_turn(self) -> None:
        from atri_x.safecontext import with_cancel

        manager = Manager()
        manager.start_llm("s1", "a")
        manager.start_tool_phase("s1")
        ctx = with_cancel(None)
        results: list[tuple[RiskConfirmationResponse, bool]] = []

        async def wait() -> None:
            results.append(
                await manager.await_risk_confirmation_context(ctx, "s1", RiskConfirmation())
            )

        task = asyncio.ensure_future(wait())
        for _ in range(500):
            if manager.snapshot("s1").phase == PhaseAwaitRiskConfirm:
                break
            await asyncio.sleep(0.002)
        ctx.cancel("test")
        await asyncio.wait_for(task, 2)
        self.assertTrue(results[0][0].stopped)
        self.assertFalse(results[0][1])
        self.assertEqual(manager.snapshot("s1").phase, TurnPhaseIdle)

    def test_merge_inputs_preserves_speaker_attribution(self) -> None:
        merged = merge_inputs(
            [
                Input(
                    text="one",
                    segments=text_segments("one"),
                    speaker=Speaker(actor_id="a", user_id="1001", name="张三"),
                ),
                Input(
                    text="two",
                    segments=text_segments("two"),
                    speaker=Speaker(actor_id="a", user_id="1001", name="张三"),
                ),
            ]
        )
        self.assertEqual(len(merged.parts), 2)
        self.assertEqual(merged.speaker.actor_id, "a")
        self.assertEqual(merged.speaker.name, "张三")

        mixed = merge_inputs(
            [
                Input(
                    text="one",
                    segments=text_segments("one"),
                    speaker=Speaker(actor_id="a", user_id="1001", name="张三"),
                ),
                Input(
                    text="two",
                    segments=text_segments("two"),
                    speaker=Speaker(actor_id="b", user_id="1002", name="李四"),
                ),
            ]
        )
        self.assertEqual(len(mixed.parts), 2)
        self.assertEqual(mixed.parts[0].speaker.name, "张三")
        self.assertEqual(mixed.parts[1].speaker.name, "李四")
        self.assertEqual(mixed.speaker, Speaker())

    def test_merge_inputs_drops_empty_and_single(self) -> None:
        self.assertEqual(merge_inputs([]), Input())
        self.assertEqual(merge_inputs([Input(text="   ")]), Input())
        single = merge_inputs([Input(text="only", platform_text="raw")])
        self.assertEqual(single.text, "only")
        self.assertEqual(single.parts, [])

    def test_normalize_input_derives_text_from_segments(self) -> None:
        from atri_x.turn import normalize_input, text_input

        input_value = normalize_input(
            Input(
                text="  ",
                segments=[
                    MessageSegment(type=SegmentType.TEXT, text=" 来自图片 "),
                    MessageSegment(type=SegmentType.IMAGE, url="u"),
                ],
            )
        )
        self.assertEqual(input_value.text, "来自图片")
        self.assertEqual(text_input("  hi  ").text, "hi")
        self.assertEqual(text_input("  hi  ").platform_text, "hi")


class TestRuntimeStatus(unittest.TestCase):
    def test_elapsed_uses_finished_at(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = RuntimeSnapshot(
            turn_started_at=start,
            stage_started_at=start + timedelta(seconds=1),
            finished_at=start + timedelta(seconds=65),
        )
        self.assertEqual(format_duration(snapshot.elapsed(start + timedelta(minutes=2))), "01:05")

    def test_running_and_stage_elapsed(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = RuntimeSnapshot(
            phase=PhaseLLM, stage_started_at=start, turn_started_at=start
        )
        self.assertTrue(snapshot.running())
        self.assertEqual(snapshot.stage_elapsed(start + timedelta(seconds=30)), 30.0)
        self.assertEqual(snapshot.elapsed(start + timedelta(seconds=30)), 30.0)
        snapshot.finished_at = start + timedelta(seconds=5)
        self.assertFalse(snapshot.running())
        self.assertEqual(snapshot.stage_elapsed(start + timedelta(seconds=30)), 5.0)
        self.assertEqual(RuntimeSnapshot().elapsed(None), 0.0)
        self.assertEqual(RuntimeSnapshot().stage_elapsed(None), 0.0)

    def test_format_usage(self) -> None:
        self.assertEqual(
            format_usage(Usage(total_tokens=18240, cache_hit_tokens=12100)),
            "tokens 18,240 · cache 12,100",
        )
        self.assertEqual(format_usage(None), "")
        self.assertEqual(format_usage(Usage()), "")

    def test_format_int(self) -> None:
        self.assertEqual(format_int(0), "0")
        self.assertEqual(format_int(999), "999")
        self.assertEqual(format_int(1000), "1,000")
        self.assertEqual(format_int(1234567), "1,234,567")
        self.assertEqual(format_int(-1234), "-1,234")

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(0), "00:00")
        self.assertEqual(format_duration(8), "00:08")
        self.assertEqual(format_duration(65), "01:05")
        self.assertEqual(format_duration(3661), "1:01:01")
        self.assertEqual(format_duration(-5), "00:00")

    def test_format_model(self) -> None:
        self.assertEqual(format_model("deepseek", "reasoner"), "deepseek/reasoner")
        self.assertEqual(format_model("", "reasoner"), "reasoner")
        self.assertEqual(format_model("deepseek", ""), "deepseek")
        self.assertEqual(format_model(" ", " "), "")

    def test_format_compact(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = RuntimeSnapshot(
            phase=PhaseLLM,
            provider="deepseek",
            model="reasoner",
            turn_started_at=start,
            stage_started_at=start,
            usage=Usage(total_tokens=1234, cache_hit_tokens=100),
        )
        text = format_compact(snapshot, start + timedelta(seconds=8), 120)
        for expected in ("llm", "00:08", "deepseek/reasoner", "tokens 1,234", "cache 100"):
            self.assertIn(expected, text)

    def test_format_compact_truncates_and_shows_tool(self) -> None:
        snapshot = RuntimeSnapshot(phase=PhaseError, error="x" * 80)
        text = format_compact(snapshot, datetime.now(timezone.utc), 16)
        self.assertLessEqual(len(text), 16)
        tool = RuntimeSnapshot(phase=PhaseTool, tool_name="shell")
        self.assertIn("tool shell", format_compact(tool, None, 0))
        self.assertEqual(RuntimeSnapshot().phase, PhaseIdle)
        self.assertEqual(truncate_runes("abcdef", 0), "abcdef")
        self.assertEqual(truncate_runes("abcdef", 1), "a")
        self.assertEqual(truncate_runes("abcdef", 3), "ab…")
        self.assertEqual(truncate_runes("abc", 3), "abc")
