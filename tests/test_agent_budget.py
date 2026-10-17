"""``atri_x.agent.budget``（每日计量账本 / 预算）的测试。

对应 Go：``internal/agent/budget_test.go`` 的核心路径：
原子批量预留、幂等重放、写盘失败回滚、聊天软/硬预算、执行幂等。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from atri_x.agent.budget import (
    BUDGET_ID_REUSED,
    BudgetLedger,
    BudgetLimitSpec,
    BudgetMixin,
    BudgetReservationRequest,
    ChatBudgetLimitSpec,
    budget_key,
    budget_key_parts,
    budget_prefix,
    clone_budget_digests,
    clone_budget_reservations,
    count_budget_reservation_matches,
    count_budget_usage_matches,
    ensure_budget,
    first_non_empty,
    install_budget_services,
    llm_usage_cost_micros,
    llm_usage_total_tokens,
    prune_budget_digests,
    prune_budget_reservations,
    short_hash,
)
from atri_x.config import (
    BudgetLimitsConfig,
    GroupPolicyConfig,
    ModelPriceConfig,
    ModelSelection,
    StateBudgetConfig,
    StateConfig,
)
from atri_x.errors import InvalidError
from atri_x.llm.llm import Usage

NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
YESTERDAY = NOW - timedelta(days=1)
OLD = NOW - timedelta(days=3)


class TestKeysAndPruning(unittest.TestCase):
    def test_budget_key_shape(self) -> None:
        key = budget_key(NOW, " image ", " qq:g1 ", " u1 ", " c1 ")
        self.assertEqual(key, "2026-05-20|image|qq:g1|u1|c1")
        self.assertEqual(budget_prefix(NOW, "image", "qq:g1"), "2026-05-20|image|qq:g1|")
        self.assertEqual(
            budget_key_parts(key), ("2026-05-20", "image", "qq:g1", "u1", True)
        )
        self.assertEqual(budget_key_parts("bad"), ("", "", "", "", False))

    def test_budget_key_defaults_scope(self) -> None:
        self.assertEqual(
            budget_key(NOW, "image", "", "", "c"), "2026-05-20|image|unknown||c"
        )

    def test_clone_filters_empty_and_nonpositive(self) -> None:
        self.assertEqual(
            clone_budget_reservations({"a": 1, " ": 1, "b": 0, "c": -1}), {"a": 1}
        )
        self.assertEqual(clone_budget_reservations(None), {})
        self.assertEqual(clone_budget_digests({"a": " d ", "b": " "}), {"a": "d"})

    def test_prune_keeps_today_and_yesterday(self) -> None:
        values = {
            budget_key(NOW, "image", "s", "u", "a"): 1,
            budget_key(YESTERDAY, "image", "s", "u", "b"): 1,
            budget_key(OLD, "image", "s", "u", "c"): 1,
            "garbage": 1,
        }
        pruned = prune_budget_reservations(values, NOW)
        self.assertEqual(len(pruned), 2)
        self.assertNotIn(budget_key(OLD, "image", "s", "u", "c"), pruned)
        self.assertEqual(len(prune_budget_digests({"garbage": "x", "2026-05-19|a|b|c|d": "v"}, NOW)), 1)

    def test_count_matches_scope_and_actor(self) -> None:
        values = {
            budget_key(NOW, "image", "qq:g1", "u1", "a"): 1,
            budget_key(NOW, "image", "qq:g1", "u2", "b"): 1,
            budget_key(NOW, "image", "qq:g2", "u1", "c"): 1,
            budget_key(YESTERDAY, "image", "qq:g1", "u1", "d"): 1,
        }
        self.assertEqual(
            count_budget_reservation_matches(values, "image", "qq:g1", "u1", True, True, NOW), 1
        )
        self.assertEqual(
            count_budget_reservation_matches(values, "image", "qq:g1", "u1", True, False, NOW), 2
        )
        self.assertEqual(
            count_budget_reservation_matches(values, "image", "", "", False, False, NOW), 3
        )
        self.assertEqual(
            count_budget_usage_matches(values, "image", "", "", False, False, NOW), 3
        )

    def test_shared_helpers(self) -> None:
        self.assertEqual(first_non_empty("", "  x  ", "y"), "x")
        self.assertEqual(len(short_hash("abc")), 8)

    def test_usage_cost_helpers(self) -> None:
        usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=0)
        self.assertEqual(llm_usage_total_tokens(usage), 15)
        self.assertEqual(llm_usage_total_tokens(None), 0)
        price = ModelPriceConfig(input_per_million=1.0, output_per_million=2.0)
        self.assertEqual(llm_usage_cost_micros(price, Usage(prompt_tokens=1_000_000, completion_tokens=0)), 1_000_000)


class TestBudgetLedgerReserve(unittest.TestCase):
    def _ledger(self, **kwargs) -> BudgetLedger:
        return BudgetLedger(**kwargs)

    def test_batch_is_atomic(self) -> None:
        ledger = self._ledger()
        specs = [BudgetLimitSpec(label="全局生图日", limit=1)]
        ok, reason = ledger.reserve_batch(
            "image",
            [BudgetReservationRequest("c1"), BudgetReservationRequest("c2")],
            specs=specs,
            scope_key="qq:g1",
            actor_id="u1",
            now=NOW,
        )
        self.assertFalse(ok)
        self.assertIn("已用完", reason)
        self.assertEqual(ledger.reservations, {})

    def test_reserve_then_replay_is_idempotent(self) -> None:
        ledger = self._ledger()
        specs = [BudgetLimitSpec(label="全局生图日", limit=2)]
        request = BudgetReservationRequest(call_id="c1", digest="d1")
        ok, _ = ledger.reserve_batch(
            "image", [request], specs=specs, scope_key="s", actor_id="u", now=NOW
        )
        self.assertTrue(ok)
        ok, _ = ledger.reserve_batch(
            "image", [request], specs=specs, scope_key="s", actor_id="u", now=NOW
        )
        self.assertTrue(ok)
        self.assertEqual(len(ledger.reservations), 1)

    def test_reused_call_id_with_other_digest_is_rejected(self) -> None:
        ledger = self._ledger()
        specs = [BudgetLimitSpec(label="全局生图日", limit=5)]
        ledger.reserve_batch(
            "image",
            [BudgetReservationRequest("c1", "d1")],
            specs=specs,
            scope_key="s",
            actor_id="u",
            now=NOW,
        )
        ok, reason = ledger.reserve_batch(
            "image",
            [BudgetReservationRequest("c1", "d2")],
            specs=specs,
            scope_key="s",
            actor_id="u",
            now=NOW,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, BUDGET_ID_REUSED)

    def test_empty_call_ids_are_skipped(self) -> None:
        ledger = self._ledger()
        ok, _ = ledger.reserve_batch(
            "image",
            [BudgetReservationRequest("")],
            specs=[BudgetLimitSpec(label="l", limit=1)],
            scope_key="s",
            actor_id="u",
            now=NOW,
        )
        self.assertTrue(ok)
        self.assertEqual(ledger.reservations, {})

    def test_save_failure_rolls_back_and_marks_unwritable(self) -> None:
        def boom() -> None:
            raise RuntimeError("disk full")

        ledger = self._ledger(save_state=boom)
        ok, reason = ledger.reserve_batch(
            "image",
            [BudgetReservationRequest("c1")],
            specs=[BudgetLimitSpec(label="全局生图日", limit=1)],
            scope_key="s",
            actor_id="u",
            now=NOW,
        )
        self.assertFalse(ok)
        self.assertIn("额度账本写入失败", reason)
        self.assertEqual(ledger.reservations, {})
        self.assertFalse(ledger.writable())

    def test_actor_scoped_limit(self) -> None:
        ledger = self._ledger()
        specs = [BudgetLimitSpec(label="全局单用户生图日", limit=1, actor_match=True)]
        ledger.reserve_batch(
            "image", [BudgetReservationRequest("c1")], specs=specs,
            scope_key="s", actor_id="u1", now=NOW,
        )
        ok, _ = ledger.reserve_batch(
            "image", [BudgetReservationRequest("c2")], specs=specs,
            scope_key="s", actor_id="u1", now=NOW,
        )
        self.assertFalse(ok)
        ok, _ = ledger.reserve_batch(
            "image", [BudgetReservationRequest("c3")], specs=specs,
            scope_key="s", actor_id="u2", now=NOW,
        )
        self.assertTrue(ok)


class TestChatBudget(unittest.TestCase):
    def test_soft_check_raises_when_exhausted(self) -> None:
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="全局日聊天 token", limit=10)]
        ledger.tokens[budget_key(NOW, "chat", "s", "u", "a")] = 10
        with self.assertRaises(InvalidError):
            ledger.check_chat_budget(specs, scope_key="s", actor_id="u", now=NOW)
        ledger.tokens = {budget_key(NOW, "chat", "s", "u", "a"): 9}
        ledger.check_chat_budget(specs, scope_key="s", actor_id="u", now=NOW)

    def test_soft_check_requires_writable_ledger(self) -> None:
        ledger = BudgetLedger()
        ledger.mark_write_failed(True)
        with self.assertRaises(InvalidError):
            ledger.check_chat_budget(
                [ChatBudgetLimitSpec(label="l", limit=1)], scope_key="s", actor_id="u", now=NOW
            )

    def test_cost_metric_uses_micros_and_unit(self) -> None:
        ledger = BudgetLedger()
        ledger.costs[budget_key(NOW, "chat", "s", "u", "a")] = 5
        with self.assertRaises(InvalidError) as caught:
            ledger.check_chat_budget(
                [ChatBudgetLimitSpec(label="全局日聊天费用", limit=5, metric="cost")],
                scope_key="s",
                actor_id="u",
                now=NOW,
            )
        self.assertIn("微单位", str(caught.exception))

    def test_hard_limit_reserve_settle_with_usage(self) -> None:
        price = ModelPriceConfig(input_per_million=1.0, output_per_million=1.0)
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="全局日聊天 token", limit=1000, metric="tokens")]
        reservation = ledger.begin_chat_budget(
            specs=specs,
            reserved_tokens=100,
            reserved_cost=0,
            scope_key="s",
            actor_id="u",
            model="m",
            call_id="call-1",
            now=NOW,
        )
        self.assertEqual(ledger.tokens[reservation.key], 100)
        self.assertIn(reservation.key, ledger.uncertain)
        ledger.settle_chat_budget(
            reservation,
            Usage(prompt_tokens=10, completion_tokens=5),
            price_for=lambda model, at: price,
            now=NOW,
        )
        self.assertEqual(ledger.tokens[reservation.key], 15)
        self.assertNotIn(reservation.key, ledger.uncertain)

    def test_hard_limit_missing_usage_keeps_reservation_uncertain(self) -> None:
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="全局日聊天费用", limit=10_000_000, metric="cost")]
        reservation = ledger.begin_chat_budget(
            specs=specs, reserved_tokens=100, reserved_cost=500,
            scope_key="s", actor_id="u", model="m", call_id="c", now=NOW,
        )
        ledger.settle_chat_budget(reservation, None, now=NOW)
        self.assertEqual(ledger.tokens[reservation.key], 100)
        self.assertEqual(ledger.costs[reservation.key], 500)
        self.assertIn(reservation.key, ledger.uncertain)

    def test_hard_limit_release_clears_everything(self) -> None:
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="l", limit=1000)]
        reservation = ledger.begin_chat_budget(
            specs=specs, reserved_tokens=10, reserved_cost=0,
            scope_key="s", actor_id="u", model="m", call_id="c", now=NOW,
        )
        ledger.settle_chat_budget(reservation, None, True, now=NOW)
        self.assertNotIn(reservation.key, ledger.tokens)
        self.assertNotIn(reservation.key, ledger.uncertain)

    def test_hard_limit_rejects_duplicate_call_id(self) -> None:
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="l", limit=1000)]
        ledger.begin_chat_budget(
            specs=specs, reserved_tokens=10, reserved_cost=0,
            scope_key="s", actor_id="u", model="m", call_id="dup", now=NOW,
        )
        with self.assertRaises(InvalidError):
            ledger.begin_chat_budget(
                specs=specs, reserved_tokens=10, reserved_cost=0,
                scope_key="s", actor_id="u", model="m", call_id="dup", now=NOW,
            )

    def test_hard_limit_insufficient_is_rejected(self) -> None:
        ledger = BudgetLedger()
        specs = [ChatBudgetLimitSpec(label="全局日聊天 token", limit=50)]
        with self.assertRaises(InvalidError) as caught:
            ledger.begin_chat_budget(
                specs=specs, reserved_tokens=100, reserved_cost=0,
                scope_key="s", actor_id="u", model="m", call_id="c", now=NOW,
            )
        self.assertIn("本次预占 100", str(caught.exception))

    def test_record_chat_usage_accumulates(self) -> None:
        price = ModelPriceConfig(input_per_million=1.0, output_per_million=1.0)
        ledger = BudgetLedger()
        usage = Usage(prompt_tokens=100, completion_tokens=100)
        ledger.record_chat_usage(
            "m", usage, scope_key="s", actor_id="u", call_id="c1",
            price_for=lambda model, at: price, now=NOW,
        )
        ledger.record_chat_usage(
            "m", usage, scope_key="s", actor_id="u", call_id="c1",
            price_for=lambda model, at: price, now=NOW,
        )
        tokens, costs = ledger.chat_usage_for_scope("s", NOW)
        self.assertEqual(tokens, 400)
        self.assertEqual(costs, 400)


class TestExecutionIdempotency(unittest.TestCase):
    def test_same_digest_is_suppressed(self) -> None:
        ledger = BudgetLedger()
        ok, _ = ledger.reserve_tool_execution("c1", "d1", scope_key="s", actor_id="u", now=NOW)
        self.assertTrue(ok)
        ok, reason = ledger.reserve_tool_execution("c1", "d1", scope_key="s", actor_id="u", now=NOW)
        self.assertFalse(ok)
        self.assertIn("已经执行过", reason)

    def test_different_digest_is_rejected(self) -> None:
        ledger = BudgetLedger()
        ledger.reserve_tool_execution("c1", "d1", scope_key="s", actor_id="u", now=NOW)
        ok, reason = ledger.reserve_tool_execution(
            "c1", "d2", scope_key="s", actor_id="u", now=NOW
        )
        self.assertFalse(ok)
        self.assertIn("不同参数", reason)

    def test_default_digest_and_release(self) -> None:
        ledger = BudgetLedger()
        ledger.reserve_tool_execution("c1", "", scope_key="s", actor_id="u", now=NOW)
        key = budget_key(NOW, "exec", "s", "u", "c1")
        self.assertEqual(ledger.executions[key], short_hash("c1"))
        ledger.release_tool_execution("c1", scope_key="s", actor_id="u", now=NOW)
        self.assertEqual(ledger.executions, {})

    def test_empty_call_id_is_noop(self) -> None:
        ledger = BudgetLedger()
        self.assertEqual(
            ledger.reserve_tool_execution("", "", scope_key="s", actor_id="u", now=NOW),
            (True, ""),
        )


class TestUsageQueriesAndState(unittest.TestCase):
    def test_usage_for_scope_counts_prefixes(self) -> None:
        ledger = BudgetLedger()
        ledger.reservations[budget_key(NOW, "image", "qq:g1", "u", "i1")] = 1
        ledger.reservations[budget_key(NOW, "image", "qq:g1", "u2", "i2")] = 1
        ledger.reservations[budget_key(NOW, "vision", "qq:g1", "u", "v1")] = 1
        ledger.reservations[budget_key(NOW, "asr", "qq:g1", "u", "a1")] = 1
        ledger.reservations[budget_key(NOW, "image", "qq:other", "u", "x")] = 1
        ledger.reservations[budget_key(OLD, "image", "qq:g1", "u", "old")] = 1
        self.assertEqual(ledger.usage_for_scope("qq:g1", NOW), (2, 1))
        self.assertEqual(ledger.asr_usage_for_scope("qq:g1", NOW), 1)
        self.assertEqual(ledger.usage_for_scope("qq:other", NOW), (1, 0))

    def test_load_and_write_state_round_trip(self) -> None:
        ledger = BudgetLedger()
        ledger.reservations[budget_key(NOW, "image", "s", "u", "i1")] = 1
        ledger.digests[budget_key(NOW, "image", "s", "u", "i1")] = "d"
        ledger.tokens[budget_key(NOW, "chat", "s", "u", "c")] = 42
        ledger.digests[budget_key(OLD, "image", "s", "u", "old")] = "x"
        state = StateConfig()
        ledger.write_into_state(state, NOW)
        self.assertEqual(len(state.budget.reservations), 1)
        self.assertEqual(len(state.budget.tokens), 1)
        # 三天前的摘要被裁掉，只留今天的
        self.assertEqual(list(state.budget.digests), [budget_key(NOW, "image", "s", "u", "i1")])

        other = BudgetLedger()
        other.load_from_state(state.budget)
        self.assertEqual(other.tokens[budget_key(NOW, "chat", "s", "u", "c")], 42)

    def test_state_snapshots_are_copies(self) -> None:
        budget = StateBudgetConfig(
            reservations={"k": 1}, digests={"d": "v"}, tokens={}, costs={}, retries={},
            executions={}, uncertain={},
        )
        ledger = BudgetLedger()
        ledger.load_from_state(budget)
        snapshot = ledger.reservations_snapshot()
        snapshot["k"] = 99
        self.assertEqual(ledger.reservations["k"], 1)
        self.assertEqual(ledger.state_snapshot()[1], {"d": "v"})


class _Actor:
    def __init__(self, actor_id: str = "u1") -> None:
        self.id = actor_id
        self.platform_user_id = ""


class _Scope:
    def __init__(self, scope_id: str = "qq:g1") -> None:
        self.platform = "qq"
        self.platform_scope_id = scope_id


class _PolicyTable:
    def __init__(self, prices: dict[str, ModelPriceConfig]) -> None:
        self.prices = prices

    def price_for(self, model: str, at: datetime):
        return self.prices.get(model)


class _BudgetHost(BudgetMixin):
    def __init__(self, limits: BudgetLimitsConfig, policy: GroupPolicyConfig | None = None,
                 group: bool = True) -> None:
        self.budget_limits = limits
        self.policy = policy or GroupPolicyConfig()
        self.group = group
        self.pricing = _PolicyTable({"m": ModelPriceConfig(input_per_million=1.0, output_per_million=1.0)})
        self.services: dict[str, object] = {}
        self.saved = 0
        self.audit_events: list[tuple[str, dict]] = []

    def is_group_scope(self, ctx) -> bool:
        return self.group

    def scope(self, ctx) -> _Scope:
        return _Scope()

    def actor(self, ctx) -> _Actor:
        return _Actor()

    def group_policy_for_scope(self, scope) -> GroupPolicyConfig:
        return self.policy

    def save_runtime_state(self) -> None:
        self.saved += 1

    def audit(self, event: str, **attrs) -> None:
        self.audit_events.append((event, attrs))


class TestBudgetMixin(unittest.TestCase):
    def test_call_limit_specs_group_and_global(self) -> None:
        host = _BudgetHost(
            BudgetLimitsConfig(global_image_daily=10, user_image_daily=3),
            GroupPolicyConfig(image_quota=5, user_image_quota=2),
        )
        specs = host.call_limit_specs(None, "image")
        self.assertEqual(
            [(s.label, s.limit) for s in specs],
            [("本群生图日", 5), ("本群单用户生图日", 2), ("全局生图日", 10), ("全局单用户生图日", 3)],
        )
        # 0 表示不限：全部被过滤
        self.assertEqual(_BudgetHost(BudgetLimitsConfig()).call_limit_specs(None, "image"), [])

    def test_chat_budget_limit_specs_cost_micros(self) -> None:
        host = _BudgetHost(
            BudgetLimitsConfig(global_chat_cost_daily=1.5), GroupPolicyConfig(chat_tokens_quota=7)
        )
        specs = host.chat_budget_limit_specs(None)
        by_label = {s.label: s for s in specs}
        self.assertEqual(by_label["本群日聊天 token"].limit, 7)
        self.assertEqual(by_label["全局日聊天费用"].limit, 1_500_000)

    def test_reserve_budget_request_via_mixin(self) -> None:
        host = _BudgetHost(BudgetLimitsConfig(global_image_daily=1))
        ok, _ = host.reserve_budget_request(None, "image", BudgetReservationRequest("c1"))
        self.assertTrue(ok)
        ok, reason = host.reserve_budget(None, "image", "c2")
        self.assertFalse(ok)
        self.assertIn("已用完", reason)
        self.assertTrue(host.audit_events == [] or isinstance(host.audit_events, list))

    def test_chat_budget_usage_for_scope_requires_group(self) -> None:
        host = _BudgetHost(BudgetLimitsConfig())
        host.group = False
        self.assertEqual(host.chat_budget_usage_for_scope(None), (0, 0))
        self.assertEqual(host.budget_usage_for_scope(None), (0, 0))
        self.assertEqual(host.asr_budget_usage_for_scope(None), 0)

    def test_begin_chat_budget_soft_path_returns_none(self) -> None:
        host = _BudgetHost(BudgetLimitsConfig(global_chat_tokens_daily=1000))
        reservation = host.begin_chat_budget(
            None, ModelSelection("p", "m"), [], [], 0, ""
        )
        self.assertIsNone(reservation)
        self.assertGreaterEqual(host.saved, 1)

    def test_begin_chat_budget_hard_path_reserves(self) -> None:
        host = _BudgetHost(
            BudgetLimitsConfig(global_chat_tokens_daily=100_000, chat_hard_limit=True)
        )
        reservation = host.begin_chat_budget(None, ModelSelection("p", "m"), [], [], 0, "cid")
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.call_id, "cid")
        # 预占 = 估算输入(>=1) + 输出预算（窗口的保守值）
        self.assertGreater(reservation.reserved_tokens, 1)
        host.settle_chat_budget(None, reservation, Usage(prompt_tokens=1, completion_tokens=1), False)
        self.assertEqual(host.budget.tokens[reservation.key], 2)

    def test_begin_chat_budget_hard_cost_requires_price(self) -> None:
        host = _BudgetHost(
            BudgetLimitsConfig(global_chat_cost_daily=1.0, chat_hard_limit=True)
        )
        with self.assertRaises(InvalidError) as caught:
            host.begin_chat_budget(None, ModelSelection("p", "missing"), [], [], 0, "cid")
        self.assertIn("没有配置价格", str(caught.exception))

    def test_install_budget_services(self) -> None:
        host = _BudgetHost(BudgetLimitsConfig(global_image_daily=1))
        service = install_budget_services(host)
        self.assertIs(host.services["budget"], service)
        ok, _ = host.reserve_budget(None, "image", "c1")
        self.assertTrue(ok)
        self.assertIsInstance(ensure_budget(host), BudgetLedger)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
