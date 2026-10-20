"""``atri_x.request`` 的单元测试。

对应 Go：``internal/request/manager_test.go`` + ``fairness_test.go``。
"""

from __future__ import annotations

import asyncio
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import atri_x
from atri_x.agent.core import _request_settings_from_ops
from atri_x.config.app_sections import OpsConfig
from atri_x.errors import (
    ConcurrencyLimitError,
    QueueCancelledError,
    QueueFullError,
    QueueTimeoutError,
)
from atri_x.request import (
    KindHook,
    KindLLM,
    KindTool,
    KindTurn,
    Manager,
    QueueConfig,
    StartRequest,
    new_manager_with_limits_and_queue,
)
from atri_x.safecontext import CancelToken, CancelledError, with_cancel


async def _wait_queued(manager: Manager, kind: str) -> None:
    for _ in range(500):
        if manager.snapshot().queued_by_kind.get(kind):
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"request never entered the {kind} queue")


class TestManagerBasics(unittest.IsolatedAsyncioTestCase):
    async def test_start_list_and_done(self) -> None:
        manager = Manager(60.0)
        request, _ctx, done = await manager.start(
            None, StartRequest(session_id="s1", kind=KindLLM, label="chat")
        )
        requests = manager.list()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].id, request.id)
        self.assertIsNotNone(requests[0].deadline)
        done()
        done()  # 幂等
        self.assertEqual(manager.list(), [])

    async def test_kind_and_stage_defaults(self) -> None:
        manager = Manager(60.0)
        request, _ctx, done = await manager.start(None, StartRequest())
        self.assertEqual(request.kind, KindLLM)
        self.assertEqual(request.stage, KindLLM)
        done()

    async def test_start_keeps_mapping_ctx_values(self) -> None:
        """平台边界常把 ctx 传成 Mapping；start() 后新 token 仍要能读到键值。

        否则 ``MessageContext``（输出落点）、安全策略、入站段等键值在 turn 内全部
        丢失，典型症状是回复退到 headless service 平台并报 "cannot send chat
        output without a target platform"。
        """
        from atri_x.delivery import ctx_value

        manager = Manager(60.0)
        _request, ctx, done = await manager.start(
            {"k": "v"}, StartRequest(session_id="s1", kind=KindLLM)
        )
        try:
            self.assertIsInstance(ctx, CancelToken)
            self.assertEqual(ctx_value(ctx, "k"), "v")
        finally:
            done()

    async def test_cancel(self) -> None:
        manager = Manager(60.0)
        request, ctx, _done = await manager.start(
            None, StartRequest(session_id="s1", kind=KindLLM)
        )
        found, ok = manager.get(request.id)
        self.assertTrue(ok)
        self.assertEqual(found.id, request.id)
        self.assertTrue(manager.cancel(request.id))
        self.assertTrue(ctx.is_cancelled())
        self.assertEqual(manager.list(), [])
        self.assertFalse(manager.cancel(request.id))

    async def test_cancel_session_keeps_other_sessions(self) -> None:
        manager = Manager(60.0)
        parent, ctx1, _d1 = await manager.start(
            None, StartRequest(session_id="s1", kind=KindTurn)
        )
        child, ctx2, _d2 = await manager.start(
            ctx1, StartRequest(parent_id=parent.id, session_id="s1", kind=KindTool)
        )
        _third, ctx3, _d3 = await manager.start(
            None, StartRequest(session_id="s2", kind=KindLLM)
        )
        self.assertEqual(child.parent_id, parent.id)
        self.assertEqual(manager.cancel_session("s1"), 2)
        self.assertTrue(ctx1.is_cancelled())
        self.assertTrue(ctx2.is_cancelled())
        self.assertFalse(ctx3.is_cancelled())
        self.assertEqual(len(manager.list_by_session("s2")), 1)

    async def test_cancel_all(self) -> None:
        manager = Manager(60.0)
        _a, ctx1, _d1 = await manager.start(None, StartRequest(session_id="s1"))
        _b, ctx2, _d2 = await manager.start(
            None, StartRequest(session_id="s2", kind=KindTool)
        )
        self.assertEqual(manager.cancel_all(), 2)
        self.assertTrue(ctx1.is_cancelled())
        self.assertTrue(ctx2.is_cancelled())
        self.assertEqual(manager.list(), [])

    async def test_session_and_scope_and_fair_key_ids(self) -> None:
        manager = Manager(60.0)
        request, _ctx, done = await manager.start(
            None,
            StartRequest(
                session_id="s1", kind=KindTurn, fair_key="A", scope_key="group:1"
            ),
        )
        self.assertEqual(manager.session_ids("s1"), [request.id])
        self.assertEqual(manager.scope_ids("group:1"), [request.id])
        self.assertEqual(manager.fair_key_ids("A"), [request.id])
        self.assertEqual(manager.session_ids("  "), [])
        done()

    async def test_timeout_cleans_request_and_counts(self) -> None:
        manager = Manager(0.01)
        _request, ctx, _done = await manager.start(None, StartRequest(kind=KindLLM))
        await ctx.wait(1.0)
        for _ in range(500):
            if not manager.list():
                break
            await asyncio.sleep(0.002)
        self.assertEqual(manager.list(), [])
        self.assertEqual(manager.snapshot().timeouts, 1)

    async def test_concurrency_limit(self) -> None:
        manager = Manager(60.0, {KindTool: 1})
        _request, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        with self.assertRaises(ConcurrencyLimitError):
            await manager.start(None, StartRequest(kind=KindTool))
        done()
        _again, _ctx2, done2 = await manager.start(None, StartRequest(kind=KindTool))
        done2()
        self.assertEqual(manager.snapshot().queue_busy_rejected, 1)

    async def test_snapshot_and_stage(self) -> None:
        manager = Manager(60.0)
        turn, _ctx, _done = await manager.start(
            None,
            StartRequest(session_id="s1", kind=KindTurn, label="chat", stage="preparing"),
        )
        await manager.start(
            None, StartRequest(session_id="s1", kind=KindTool, label="shell", stage="running")
        )
        self.assertTrue(manager.set_stage(turn.id, "llm"))
        self.assertTrue(manager.touch(turn.id))
        self.assertFalse(manager.set_stage(turn.id, "   "))
        self.assertFalse(manager.set_stage("missing", "llm"))
        self.assertFalse(manager.touch("missing"))
        snapshot = manager.snapshot()
        self.assertEqual(len(snapshot.active), 2)
        self.assertEqual(snapshot.count_by_kind[KindTurn], 1)
        self.assertEqual(snapshot.count_by_kind[KindTool], 1)
        self.assertIsNotNone(snapshot.oldest_started_at)
        self.assertGreaterEqual(snapshot.oldest_age, 0)
        stages = {item.id: item.stage for item in snapshot.active}
        self.assertEqual(stages[turn.id], "llm")

    async def test_set_fair_key(self) -> None:
        manager = Manager(60.0)
        request, _ctx, done = await manager.start(None, StartRequest(kind=KindTurn))
        self.assertTrue(manager.set_fair_key(request.id, " late "))
        self.assertEqual(manager.fair_key_ids("late"), [request.id])
        self.assertFalse(manager.set_fair_key("missing", "late"))
        done()


class TestManagerQueue(unittest.IsolatedAsyncioTestCase):
    def queue_config(self, **kwargs) -> QueueConfig:
        kwargs.setdefault("wait_kinds", [KindTool, KindTurn])
        return QueueConfig(**kwargs)

    async def test_queue_wait_and_grant(self) -> None:
        manager = Manager(
            60.0,
            {KindTool: 1},
            self.queue_config(max_queue=1, wait_timeout=5.0),
        )
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        started: list[str] = []

        async def second() -> None:
            request, _ctx2, done2 = await manager.start(
                None, StartRequest(kind=KindTool, label="second")
            )
            started.append(request.label)
            done2()

        task = asyncio.ensure_future(second())
        await _wait_queued(manager, KindTool)
        done()
        await asyncio.wait_for(task, 2)
        self.assertEqual(started, ["second"])
        self.assertEqual(manager.snapshot().queue_admitted, 1)

    async def test_queue_full(self) -> None:
        manager = Manager(
            60.0, {KindTool: 1}, self.queue_config(max_queue=1, wait_timeout=5.0)
        )
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        task = asyncio.ensure_future(
            manager.start(None, StartRequest(kind=KindTool))
        )
        await _wait_queued(manager, KindTool)
        with self.assertRaises(QueueFullError):
            await manager.start(None, StartRequest(kind=KindTool))
        self.assertGreaterEqual(manager.snapshot().queue_full_rejected, 1)
        done()
        _second, _ctx2, done2 = await asyncio.wait_for(task, 2)
        done2()

    async def test_queue_timeout(self) -> None:
        manager = Manager(
            60.0, {KindTool: 1}, self.queue_config(max_queue=1, wait_timeout=0.01)
        )
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        with self.assertRaises(QueueTimeoutError):
            await manager.start(None, StartRequest(kind=KindTool))
        self.assertEqual(manager.snapshot().queue_timeout_rejected, 1)
        done()

    async def test_queue_context_cancel(self) -> None:
        manager = Manager(
            60.0, {KindTool: 1}, self.queue_config(max_queue=1, wait_timeout=60.0)
        )
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        ctx = with_cancel(None)
        ctx.cancel("test")
        with self.assertRaises(CancelledError):
            await manager.start(ctx, StartRequest(kind=KindTool))
        self.assertEqual(manager.snapshot().queue_cancel_rejected, 1)
        done()

    async def test_queue_disabled_for_kind(self) -> None:
        manager = Manager(60.0, {KindTool: 1}, QueueConfig(max_queue=8))
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTool))
        with self.assertRaises(ConcurrencyLimitError):
            await manager.start(None, StartRequest(kind=KindTool))
        done()

    async def test_cancel_scope_removes_active_and_queued(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            self.queue_config(max_queue=8, wait_timeout=5.0),
        )
        _active, active_ctx, done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
        )
        waiter = asyncio.ensure_future(
            manager.start(
                None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
            )
        )
        await _wait_queued(manager, KindTurn)
        self.assertEqual(manager.cancel_scope("group:1"), 2)
        self.assertTrue(active_ctx.is_cancelled())
        with self.assertRaises(QueueCancelledError):
            await asyncio.wait_for(waiter, 2)
        self.assertEqual(manager.cancel_scope("  "), 0)
        done()


class TestManagerFairness(unittest.IsolatedAsyncioTestCase):
    async def test_fair_queue_prefers_idle_fair_key(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            QueueConfig(
                max_queue=10, wait_timeout=5.0, wait_kinds=[KindTurn]
            ),
        )
        _a1, _ctx, a1_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A")
        )
        granted: list[str] = []
        hold = asyncio.Event()

        async def queued(key: str, gate: asyncio.Event | None = None) -> None:
            _request, _ctx2, done = await manager.start(
                None, StartRequest(kind=KindTurn, fair_key=key)
            )
            granted.append(key)
            if gate is not None:
                await gate.wait()
            done()

        a2 = asyncio.ensure_future(queued("A"))
        await _wait_queued(manager, KindTurn)
        b1 = asyncio.ensure_future(queued("B", hold))
        for _ in range(500):
            if manager.snapshot().queued_by_kind.get(KindTurn) == 2:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(manager.snapshot().queued_by_kind.get(KindTurn), 2)

        a1_done()
        for _ in range(500):
            if granted:
                break
            await asyncio.sleep(0.002)
        # 空闲 key（B）先上队；A 仍有活跃请求，所以 A2 继续等。
        self.assertEqual(granted, ["B"])
        self.assertEqual(manager.snapshot().queued_by_kind.get(KindTurn), 1)

        hold.set()
        await asyncio.wait_for(a2, 2)
        self.assertEqual(granted, ["B", "A"])
        await asyncio.wait_for(b1, 2)

    async def test_cancel_fair_key_cancels_only_matching_bucket(self) -> None:
        manager = Manager(60.0)
        _a, a_ctx, a_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A")
        )
        _b, b_ctx, b_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="B")
        )
        self.assertEqual(manager.cancel_fair_key("A"), 1)
        self.assertTrue(a_ctx.is_cancelled())
        self.assertFalse(b_ctx.is_cancelled())
        a_done()
        b_done()

    async def test_cancel_fair_key_removes_queued_waiters(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            QueueConfig(max_queue=10, wait_timeout=5.0, wait_kinds=[KindTurn]),
        )
        _a1, a1_ctx, a1_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A")
        )
        waiter = asyncio.ensure_future(
            manager.start(None, StartRequest(kind=KindTurn, fair_key="A"))
        )
        await _wait_queued(manager, KindTurn)
        self.assertEqual(manager.cancel_fair_key("A"), 2)
        self.assertTrue(a1_ctx.is_cancelled())
        with self.assertRaises(QueueCancelledError):
            await asyncio.wait_for(waiter, 2)
        a1_done()

    async def test_queue_per_fair_key_cap(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            QueueConfig(
                max_queue=10,
                max_queue_per_fair_key=1,
                wait_timeout=5.0,
                wait_kinds=[KindTurn],
            ),
        )
        _a1, _ctx, a1_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
        )
        waiter = asyncio.ensure_future(
            manager.start(
                None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
            )
        )
        await _wait_queued(manager, KindTurn)
        with self.assertRaises(QueueFullError):
            await manager.start(
                None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
            )
        a1_done()
        _second, _ctx2, done2 = await asyncio.wait_for(waiter, 2)
        done2()

    async def test_queue_per_scope_cap_across_fair_keys(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            QueueConfig(
                max_queue=10,
                max_queue_per_scope=1,
                wait_timeout=5.0,
                wait_kinds=[KindTurn],
            ),
        )
        _a, _ctx, a_done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A", scope_key="group:1")
        )
        waiter = asyncio.ensure_future(
            manager.start(
                None, StartRequest(kind=KindTurn, fair_key="B", scope_key="group:1")
            )
        )
        await _wait_queued(manager, KindTurn)
        with self.assertRaises(QueueFullError):
            await manager.start(
                None, StartRequest(kind=KindTurn, fair_key="C", scope_key="group:1")
            )
        a_done()
        _second, _ctx2, done2 = await asyncio.wait_for(waiter, 2)
        done2()

    async def test_snapshot_reports_queue_metrics(self) -> None:
        manager = Manager(
            60.0,
            {KindTurn: 1},
            QueueConfig(
                max_queue=10,
                max_queue_per_fair_key=1,
                wait_timeout=5.0,
                wait_kinds=[KindTurn],
            ),
        )
        _active, _ctx, done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="A")
        )
        waiter = asyncio.ensure_future(
            manager.start(None, StartRequest(kind=KindTurn, fair_key="A"))
        )
        await _wait_queued(manager, KindTurn)
        with self.assertRaises(QueueFullError):
            await manager.start(None, StartRequest(kind=KindTurn, fair_key="A"))
        snapshot = manager.snapshot()
        self.assertGreaterEqual(snapshot.queue_full_rejected, 1)
        self.assertEqual(snapshot.queued_by_kind[KindTurn], 1)
        self.assertGreater(snapshot.oldest_queued_wait, 0)
        done()
        _second, _ctx2, done2 = await asyncio.wait_for(waiter, 2)
        done2()
        self.assertGreaterEqual(manager.snapshot().queue_admitted, 1)

    async def test_cancel_scope_cancels_only_matching_scope(self) -> None:
        manager = Manager(60.0)
        _a, a_ctx, a_done = await manager.start(
            None, StartRequest(kind=KindTurn, scope_key="qqonebot:group:9")
        )
        _b, b_ctx, b_done = await manager.start(
            None, StartRequest(kind=KindTurn, scope_key="qqonebot:group:10")
        )
        self.assertEqual(manager.cancel_scope("qqonebot:group:9"), 1)
        self.assertTrue(a_ctx.is_cancelled())
        self.assertFalse(b_ctx.is_cancelled())
        a_done()
        b_done()

    async def test_parent_cancellation_propagates_to_child_request(self) -> None:
        manager = Manager(60.0)
        parent = CancelToken()
        _p, parent_ctx, _done = await manager.start(
            parent, StartRequest(kind=KindTurn)
        )
        child, child_ctx, _done2 = await manager.start(
            parent_ctx, StartRequest(kind=KindTool)
        )
        parent.cancel("test")
        self.assertTrue(child_ctx.is_cancelled())
        for _ in range(500):
            if not manager.list():
                break
            await asyncio.sleep(0.002)
        self.assertEqual(manager.list(), [])
        self.assertTrue(child.id)


# ``[ops]`` → request manager 的接线（core.py:247）

#: 出厂配置资产：用它断言"默认配置下并发上限真的生效"。
_DEFAULT_APP_TOML = Path(atri_x.__file__).resolve().parent / "config" / "assets_data" / "app.toml"


def _ops(**overrides: Any) -> SimpleNamespace:
    """一份与默认资产同形的 ``[ops]`` 段（可逐字段覆盖）。"""
    fields: dict[str, Any] = {
        "max_concurrent_turns": 10, "max_concurrent_tools": 10,
        "max_concurrent_hooks": 10, "queue_max_size": 20, "queue_max_per_user": 0,
        "queue_max_per_scope": 0, "queue_wait_timeout_seconds": 30,
        "queue_wait_kinds": [KindTurn, KindTool, KindHook, "compress"],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _map(**overrides: Any) -> tuple[dict[str, int], QueueConfig | None]:
    """``_request_settings_from_ops`` 套在 :func:`_ops` 上，省掉重复包装。"""
    return _request_settings_from_ops(_ops(**overrides))


class TestRequestSettingsFromOps(unittest.TestCase):
    """``[ops]`` → limits + queue 的纯映射。"""

    def test_default_asset_yields_three_limits_and_a_bounded_queue(self) -> None:
        raw = tomllib.loads(_DEFAULT_APP_TOML.read_text(encoding="utf-8"))
        limits, queue = _request_settings_from_ops(
            OpsConfig.from_dict(raw["ops"], source=str(_DEFAULT_APP_TOML))
        )
        self.assertEqual(limits, {KindTurn: 10, KindTool: 10, KindHook: 10})
        self.assertNotIn("compress", limits, "compress 没有 [ops] 上限字段")
        self.assertEqual(
            (
                queue.max_queue,
                queue.max_queue_per_fair_key,
                queue.max_queue_per_scope,
                queue.wait_timeout,
            ),
            (20, 0, 0, 30.0),
        )
        self.assertEqual(queue.normalized_wait_kinds(), frozenset({"turn", "tool", "hook", "compress"}))

    def test_unconfigured_ops_stays_unlimited_and_queue_free(self) -> None:
        self.assertEqual(_request_settings_from_ops(None), ({}, None))
        self.assertEqual(_request_settings_from_ops(SimpleNamespace()), ({}, None))
        # 全 0（含只配 wait_kinds）都不算"有队列"：Manager 还要 max_queue > 0 才入队。
        self.assertEqual(
            _map(
                max_concurrent_turns=0, max_concurrent_tools=0,
                max_concurrent_hooks=0, queue_max_size=0,
                queue_wait_timeout_seconds=0,
            ),
            ({}, None),
        )

    def test_only_positive_limits_and_any_capacity_knob(self) -> None:
        self.assertEqual(
            _map(
                max_concurrent_turns=3, max_concurrent_tools=0,
                max_concurrent_hooks=-5, queue_max_size=0,
                queue_wait_timeout_seconds=0,
            ),
            ({KindTurn: 3}, None),
        )
        for overrides in (
            {"queue_max_size": 1}, {"queue_max_per_user": 2},
            {"queue_max_per_scope": 3}, {"queue_wait_timeout_seconds": 4},
        ):
            with self.subTest(**overrides):
                self.assertIsNotNone(_map(**overrides)[1])

    def test_wait_kinds_forms_and_garbage_values(self) -> None:
        self.assertEqual(
            _map(queue_wait_kinds=KindTurn)[1].normalized_wait_kinds(),
            frozenset({KindTurn}),
        )
        self.assertEqual(
            _map(queue_wait_kinds={KindTurn: True, KindTool: False})[1]
            .normalized_wait_kinds(),
            frozenset({KindTurn}),
        )
        self.assertEqual(
            _map(
                max_concurrent_turns="many", max_concurrent_tools=None,
                max_concurrent_hooks=[], queue_max_size="lots",
                queue_wait_timeout_seconds=0,
            ),
            ({}, None),
        )


class TestOpsWiringInManager(unittest.IsolatedAsyncioTestCase):
    """接线后的行为：并发满 → 排队；没有配置 → 不限并发（保持旧行为）。"""

    async def test_turn_over_the_limit_waits_in_the_queue(self) -> None:
        limits, queue = _map(
            max_concurrent_turns=1, queue_max_size=2, queue_wait_timeout_seconds=5
        )
        manager = new_manager_with_limits_and_queue(0.0, limits, queue)
        _first, _ctx, done = await manager.start(
            None, StartRequest(kind=KindTurn, fair_key="user:1", scope_key="cli:local")
        )
        waiter = asyncio.ensure_future(
            manager.start(None, StartRequest(kind=KindTurn, fair_key="user:2"))
        )
        await _wait_queued(manager, KindTurn)
        self.assertFalse(waiter.done(), "第 N+1 个必须先排队，不能直接登记")
        done()
        _second, _ctx2, done2 = await asyncio.wait_for(waiter, 5)
        done2()
        self.assertEqual(manager.snapshot().queue_admitted, 1)

    async def test_queue_with_zero_capacity_rejects_instead_of_queueing(self) -> None:
        limits, queue = _map(
            max_concurrent_turns=1, queue_max_size=0, queue_wait_timeout_seconds=30
        )
        self.assertIsNotNone(queue, "wait_timeout > 0 也算有队列配置")
        manager = new_manager_with_limits_and_queue(0.0, limits, queue)
        _first, _ctx, done = await manager.start(None, StartRequest(kind=KindTurn))
        with self.assertRaises(ConcurrencyLimitError):
            await manager.start(None, StartRequest(kind=KindTurn))
        self.assertEqual(manager.snapshot().queue_busy_rejected, 1)
        done()

    async def test_ops_none_keeps_unlimited_concurrency(self) -> None:
        manager = new_manager_with_limits_and_queue(0.0, *_request_settings_from_ops(None))
        for _ in range(25):  # 远超默认上限 10
            await manager.start(None, StartRequest(kind=KindTurn))
        self.assertEqual(len(manager.list()), 25)
        manager.cancel_all()
