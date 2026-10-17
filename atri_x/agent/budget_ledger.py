"""每日计量账本本体：原子预留、执行幂等、用量与状态桥。

对应 Go：``internal/agent/budget.go`` 里 ``Agent`` 的账本字段与
``reserveBudgetBatchRequests`` / ``setBudget*Snapshot`` / ``budgetStateSnapshot`` /
``reserveToolExecution`` / ``releaseToolExecution`` 与三个用量查询，以及聊天软/硬
预算的账本操作。键与纯函数见 ``budget_keys``，Agent 方法见 ``budget_chat``。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from atri_x.agent.budget_keys import (
    BUDGET_ID_REUSED,
    BUDGET_WRITE_FAILED,
    LOG_RESULT_FAILED,
    LOG_RESULT_SUCCEEDED,
    BudgetLimitSpec,
    BudgetReservationRequest,
    ChatBudgetLimitSpec,
    ChatBudgetReservation,
    _Pending,
    _now,
    budget_key,
    budget_prefix,
    clone_budget_digests,
    clone_budget_reservations,
    clone_budget_values,
    count_budget_prefix,
    count_budget_reservation_matches,
    count_budget_usage_matches,
    llm_usage_cost_micros,
    llm_usage_total_tokens,
    metric_unit,
    prune_budget_digests,
    prune_budget_reservations,
    prune_budget_usage,
    short_hash,
)
from atri_x.agent.model import audit_event, host_of
from atri_x.config import ModelPriceConfig
from atri_x.errors import InvalidError
from atri_x.llm.llm import Usage
from atri_x.storage import new_id

__all__ = ["BudgetLedger", "ensure_budget"]


class BudgetLedger:
    """每日计量账本。对应 Go ``Agent`` 上的 ``budget*`` 字段与 ``budgetMu``。

    ``save_state`` / ``audit`` 由宿主注入（对应 Go 的 ``saveRuntimeState`` 与 ``audit``）；
    两者为空时账本只在内存里工作（``statePath == ""`` 的等价物）。
    """

    def __init__(
        self,
        *,
        save_state: Callable[[], None] | None = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.mu = threading.RLock()
        self.reservations: dict[str, int] = {}
        self.digests: dict[str, str] = {}
        self.tokens: dict[str, int] = {}
        self.costs: dict[str, int] = {}
        self.retries: dict[str, int] = {}
        self.uncertain: dict[str, int] = {}
        self.executions: dict[str, str] = {}
        self.write_failed = False
        self.save_state = save_state
        self.audit_event = audit

    # -- 快照 ---------------------------------------------------------------
    def set_reservations(self, snapshot: Mapping[str, int] | None) -> None:
        """对应 Go ``setBudgetSnapshot``。"""
        with self.mu:
            self.reservations = clone_budget_reservations(snapshot)

    def set_digests(self, snapshot: Mapping[str, str] | None) -> None:
        """对应 Go ``setBudgetDigestSnapshot``。"""
        with self.mu:
            self.digests = clone_budget_digests(snapshot)

    def set_usage(
        self,
        tokens: Mapping[str, int] | None,
        costs: Mapping[str, int] | None,
        retries: Mapping[str, int] | None,
    ) -> None:
        """对应 Go ``setBudgetUsageSnapshot``。"""
        with self.mu:
            self.tokens = clone_budget_values(tokens)
            self.costs = clone_budget_values(costs)
            self.retries = clone_budget_values(retries)

    def set_uncertain(self, snapshot: Mapping[str, int] | None) -> None:
        """对应 Go ``setBudgetUncertainSnapshot``。"""
        with self.mu:
            self.uncertain = clone_budget_values(snapshot)

    def set_executions(self, snapshot: Mapping[str, str] | None) -> None:
        """对应 Go ``setBudgetExecutionSnapshot``。"""
        with self.mu:
            self.executions = clone_budget_digests(snapshot)

    def state_snapshot(self) -> tuple[dict[str, int], dict[str, str]]:
        """``(reservations, digests)``。对应 Go ``budgetStateSnapshot``。"""
        with self.mu:
            return (
                clone_budget_reservations(self.reservations),
                clone_budget_digests(self.digests),
            )

    def usage_snapshot(self) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """``(tokens, costs, retries)``。对应 Go ``budgetUsageSnapshot``。"""
        with self.mu:
            return (
                clone_budget_values(self.tokens),
                clone_budget_values(self.costs),
                clone_budget_values(self.retries),
            )

    def uncertain_snapshot(self) -> dict[str, int]:
        """对应 Go ``budgetUncertainSnapshot``。"""
        with self.mu:
            return clone_budget_values(self.uncertain)

    def reservations_snapshot(self) -> dict[str, int]:
        """对应 Go ``budgetSnapshot``。"""
        with self.mu:
            return clone_budget_reservations(self.reservations)

    def digests_snapshot(self) -> dict[str, str]:
        """对应 Go ``budgetDigestSnapshot``。"""
        with self.mu:
            return clone_budget_digests(self.digests)

    def executions_snapshot(self) -> dict[str, str]:
        """对应 Go ``budgetExecutionSnapshot``。"""
        with self.mu:
            return clone_budget_digests(self.executions)

    def mark_write_failed(self, failed: bool) -> None:
        """对应 Go ``markBudgetWriteFailed``。"""
        with self.mu:
            self.write_failed = failed

    def writable(self) -> bool:
        """对应 Go ``budgetWritable``。"""
        with self.mu:
            return not self.write_failed

    # -- 落盘桥 -------------------------------------------------------------
    def load_from_state(self, budget_state: Any) -> None:
        """从 ``state.toml`` 的 ``[budget]`` 恢复账本。对应 Go ``loadRuntimeStateAtStartup``。"""
        if budget_state is None:
            return
        self.set_reservations(getattr(budget_state, "reservations", None))
        self.set_digests(getattr(budget_state, "digests", None))
        self.set_usage(
            getattr(budget_state, "tokens", None),
            getattr(budget_state, "costs", None),
            getattr(budget_state, "retries", None),
        )
        self.set_uncertain(getattr(budget_state, "uncertain", None))
        self.set_executions(getattr(budget_state, "executions", None))

    def write_into_state(self, state: Any, now: datetime | None = None) -> None:
        """把（裁剪过的）账本写进 ``StateConfig.budget``。对应 Go ``saveRuntimeState`` 的 budget 段。"""
        now = now or _now()
        reservations, digests = self.state_snapshot()
        tokens, costs, retries = self.usage_snapshot()
        budget = getattr(state, "budget", None)
        if budget is None:
            return
        budget.reservations = prune_budget_reservations(reservations, now)
        budget.digests = prune_budget_digests(digests, now)
        budget.tokens = prune_budget_usage(tokens, now)
        budget.costs = prune_budget_usage(costs, now)
        budget.retries = prune_budget_usage(retries, now)
        budget.executions = prune_budget_digests(self.executions_snapshot(), now)
        budget.uncertain = prune_budget_usage(self.uncertain_snapshot(), now)

    # -- 落盘 ---------------------------------------------------------------
    def _persist(self) -> BaseException | None:
        """调用宿主的落盘；失败时返回异常而不是抛出（Go 侧错误是返回值）。"""
        if self.save_state is None:
            return None
        try:
            self.save_state()
        except Exception as exc:  # noqa: BLE001 - 调用方决定怎么降级
            return exc
        return None

    def _audit(self, event: str, **attrs: Any) -> None:
        if self.audit_event is None:
            return
        try:
            self.audit_event(event, **attrs)
        except Exception:  # noqa: BLE001 - 审计失败不影响主流程
            pass

    # -- 次数预留 -----------------------------------------------------------
    def reserve_batch(
        self,
        kind: str,
        requests: Sequence[BudgetReservationRequest],
        *,
        specs: Sequence[BudgetLimitSpec],
        scope_key: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """原子地按 call ID 预留每日单位。对应 Go ``reserveBudgetBatchRequests``。

        任何一条超限就一条也不预留；重放的 call ID 幂等、绝不重复计数。
        """
        if not specs:
            return True, ""
        now = now or _now()
        pending: list[_Pending] = []
        seen: set[str] = set()
        for request in requests:
            call_id = (request.call_id or "").strip()
            if not call_id:
                continue
            key = budget_key(now, kind, scope_key, actor_id, call_id)
            if key in seen:
                continue
            seen.add(key)
            pending.append(_Pending(key=key, digest=(request.digest or "").strip()))
        if not pending:
            return True, ""

        with self.mu:
            self.reservations = prune_budget_reservations(self.reservations, now)
            self.digests = prune_budget_digests(self.digests, now)
            unique = 0
            for item in pending:
                reused = self._digest_conflict(item)
                if reused:
                    return False, BUDGET_ID_REUSED
                if item.key in self.reservations:
                    continue
                unique += 1
            for spec in specs:
                used = count_budget_reservation_matches(
                    self.reservations,
                    kind,
                    scope_key,
                    actor_id,
                    spec.scope_match,
                    spec.actor_match,
                    now,
                )
                if used + unique > spec.limit:
                    return (
                        False,
                        f"{spec.label}额度 {spec.limit} 已用完（已用 {used}）",
                    )
            added: list[_Pending] = []
            for item in pending:
                if item.key in self.reservations:
                    continue
                self.reservations[item.key] = int(now.timestamp())
                if item.digest:
                    self.digests[item.key] = item.digest
                added.append(item)

        err = self._persist()
        if err is not None:
            with self.mu:
                for item in added:
                    self.reservations.pop(item.key, None)
                    self.digests.pop(item.key, None)
            self.mark_write_failed(True)
            return False, f"{BUDGET_WRITE_FAILED}{err}"
        self.mark_write_failed(False)
        return True, ""

    def _digest_conflict(self, item: _Pending) -> bool:
        """``item`` 的摘要与已记录摘要冲突时为真（调用方持锁）。"""
        if not item.digest:
            return False
        existing = (self.digests.get(item.key) or "").strip()
        return bool(existing) and existing != item.digest

    # -- 聊天软预算 ---------------------------------------------------------
    def check_chat_budget(
        self,
        specs: Sequence[ChatBudgetLimitSpec],
        *,
        scope_key: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """每次付费调用前的软检查。对应 Go ``checkChatBudget``。

        有限额在生效时还会先证明账本能落盘：状态文件坏掉必须**拒绝**受限调用，
        而不是静默放行成无限用量。
        """
        if not specs:
            return
        if not self.writable():
            raise InvalidError("额度账本当前不可写，已拒绝新的受限调用")
        err = self._persist()
        if err is not None:
            self.mark_write_failed(True)
            raise InvalidError(f"{BUDGET_WRITE_FAILED}{err}")
        self.mark_write_failed(False)
        now = now or _now()
        with self.mu:
            self.tokens = prune_budget_usage(self.tokens, now)
            self.costs = prune_budget_usage(self.costs, now)
            for spec in specs:
                values = self.costs if spec.metric == "cost" else self.tokens
                used = count_budget_usage_matches(
                    values,
                    "chat",
                    scope_key,
                    actor_id,
                    spec.scope_match,
                    spec.actor_match,
                    now,
                )
                if used >= spec.limit:
                    unit = metric_unit(spec.metric)
                    raise InvalidError(
                        f"{spec.label}额度 {spec.limit} {unit} 已用完（已用 {used}）"
                    )

    # -- 聊天硬预算 ---------------------------------------------------------
    def begin_chat_budget(
        self,
        *,
        specs: Sequence[ChatBudgetLimitSpec],
        reserved_tokens: int,
        reserved_cost: int,
        scope_key: str,
        actor_id: str,
        model: str,
        call_id: str = "",
        now: datetime | None = None,
    ) -> ChatBudgetReservation:
        """调用前原子预占（估算输入 + 输出预算）。对应 Go ``beginChatBudget`` 的预留段。"""
        now = now or _now()
        call_id = (call_id or "").strip() or new_id()
        key = budget_key(now, "chat", scope_key, actor_id, call_id)
        with self.mu:
            self.tokens = prune_budget_usage(self.tokens, now)
            self.costs = prune_budget_usage(self.costs, now)
            if key in self.tokens or key in self.costs:
                raise InvalidError(
                    "聊天预算调用 ID 已被预占；请检查调用方是否重复使用 call ID"
                )
            for spec in specs:
                values = self.costs if spec.metric == "cost" else self.tokens
                reserve = reserved_cost if spec.metric == "cost" else reserved_tokens
                used = count_budget_usage_matches(
                    values,
                    "chat",
                    scope_key,
                    actor_id,
                    spec.scope_match,
                    spec.actor_match,
                    now,
                )
                if used + reserve > spec.limit:
                    unit = metric_unit(spec.metric)
                    raise InvalidError(
                        f"{spec.label}额度 {spec.limit} {unit} 不足："
                        f"已用 {used}，本次预占 {reserve}"
                    )
            self.tokens[key] = reserved_tokens
            if reserved_cost > 0:
                self.costs[key] = reserved_cost
            self.uncertain[key] = int(now.timestamp())

        err = self._persist()
        if err is not None:
            with self.mu:
                self.tokens.pop(key, None)
                self.costs.pop(key, None)
                self.uncertain.pop(key, None)
            self.mark_write_failed(True)
            raise InvalidError(f"聊天预算预占写入失败：{err}")
        self.mark_write_failed(False)
        reservation = ChatBudgetReservation(
            key=key,
            call_id=call_id,
            model=(model or "").strip(),
            reserved_tokens=reserved_tokens,
            reserved_cost=reserved_cost,
        )
        self._audit(
            "chat_budget_reserved",
            model=reservation.model,
            call_id=call_id,
            reserved_tokens=reserved_tokens,
            reserved_cost_micros=reserved_cost,
            result=LOG_RESULT_SUCCEEDED,
        )
        return reservation

    def settle_chat_budget(
        self,
        reservation: ChatBudgetReservation | None,
        usage: Usage | None,
        release: bool = False,
        *,
        price_for: Callable[[str, datetime], ModelPriceConfig | None] | None = None,
        now: datetime | None = None,
    ) -> None:
        """结算一次硬预算预留。对应 Go ``settleChatBudget``。

        ``release=True`` 只用于「调用前就确定失败、provider 从未受理」的路径；
        拿不到 usage 时保留预占金额并标记 uncertain —— 上游永不返回 usage 时不能
        变成无限花。
        """
        if reservation is None:
            return
        now = now or _now()
        actual_tokens = 0
        actual_cost = 0
        uncertain = False
        if release:
            pass
        elif usage is not None:
            actual_tokens = llm_usage_total_tokens(usage)
            price = price_for(reservation.model, now) if price_for else None
            if price is not None:
                actual_cost = llm_usage_cost_micros(price, usage)
            elif reservation.reserved_cost > 0:
                actual_cost = reservation.reserved_cost
                uncertain = True
        else:
            actual_tokens = reservation.reserved_tokens
            actual_cost = reservation.reserved_cost
            uncertain = True

        with self.mu:
            if actual_tokens > 0:
                self.tokens[reservation.key] = actual_tokens
            else:
                self.tokens.pop(reservation.key, None)
            if actual_cost > 0:
                self.costs[reservation.key] = actual_cost
            else:
                self.costs.pop(reservation.key, None)
            if uncertain:
                self.uncertain[reservation.key] = int(now.timestamp())
            else:
                self.uncertain.pop(reservation.key, None)

        if release:
            self._audit(
                "chat_budget_released",
                model=reservation.model,
                call_id=reservation.call_id,
                result=LOG_RESULT_SUCCEEDED,
            )
        elif uncertain:
            self._audit(
                "chat_budget_uncertain",
                model=reservation.model,
                call_id=reservation.call_id,
                charged_tokens=actual_tokens,
                charged_cost_micros=actual_cost,
                result=LOG_RESULT_FAILED,
            )
        else:
            self._audit(
                "chat_budget_settled",
                model=reservation.model,
                call_id=reservation.call_id,
                tokens=actual_tokens,
                cost_micros=actual_cost,
                result=LOG_RESULT_SUCCEEDED,
            )
        err = self._persist()
        if err is not None:
            self.mark_write_failed(True)
            self._audit(
                "budget_write_error",
                kind="chat_settle",
                call_id=reservation.call_id,
                error=str(err),
                result=LOG_RESULT_FAILED,
            )
            return
        self.mark_write_failed(False)

    def record_chat_usage(
        self,
        model: str,
        usage: Usage | None,
        *,
        scope_key: str,
        actor_id: str,
        call_id: str = "",
        price_for: Callable[[str, datetime], ModelPriceConfig | None] | None = None,
        now: datetime | None = None,
    ) -> None:
        """把调用后的 token/费用写进持久账本。对应 Go ``recordChatUsage``。"""
        if usage is None:
            return
        now = now or _now()
        tokens = int(usage.total_tokens or 0)
        if tokens <= 0:
            tokens = int(usage.prompt_tokens or 0) + int(usage.completion_tokens or 0)
        cost_micros = 0
        price = price_for(model, now) if price_for else None
        if price is not None:
            cost = price.compute_cost(
                int(usage.prompt_tokens or 0),
                int(usage.completion_tokens or 0),
                int(usage.cache_hit_tokens or 0),
            )
            cost_micros = int(cost * 1_000_000 + 0.5)
        call_id = (call_id or "").strip() or new_id()
        key = budget_key(now, "chat", scope_key, actor_id, call_id)
        with self.mu:
            self.tokens = prune_budget_usage(self.tokens, now)
            self.costs = prune_budget_usage(self.costs, now)
            if tokens > 0:
                self.tokens[key] = self.tokens.get(key, 0) + tokens
            if cost_micros > 0:
                self.costs[key] = self.costs.get(key, 0) + cost_micros
        err = self._persist()
        if err is not None:
            self.mark_write_failed(True)
            self._audit(
                "budget_write_error",
                kind="chat_usage",
                model=model,
                error=str(err),
                result=LOG_RESULT_FAILED,
            )
            return
        self.mark_write_failed(False)

    def record_provider_retry(
        self,
        provider: str,
        *,
        scope_key: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """重试事件单独记账：不虚增调用次数预算，但仍然可审计。对应 Go ``recordProviderRetry``。"""
        now = now or _now()
        key = budget_key(
            now, "retry", scope_key, actor_id, (provider or "").strip() + ":" + new_id()
        )
        with self.mu:
            self.retries[key] = self.retries.get(key, 0) + 1
        if self.save_state is None:
            return
        err = self._persist()
        if err is not None:
            self.mark_write_failed(True)
            self._audit(
                "budget_write_error",
                kind="provider_retry",
                provider=provider,
                error=str(err),
                result=LOG_RESULT_FAILED,
            )

    # -- 执行幂等 -----------------------------------------------------------
    def reserve_tool_execution(
        self,
        call_id: str,
        digest: str = "",
        *,
        scope_key: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """记录一次工具调用即将执行。对应 Go ``reserveToolExecution``。

        预留是刻意保守的：外部操作可能已经跑过，就不再静默释放。
        """
        call_id = (call_id or "").strip()
        if not call_id:
            return True, ""
        now = now or _now()
        key = budget_key(now, "exec", scope_key, actor_id, call_id)
        digest = (digest or "").strip() or short_hash(call_id)
        with self.mu:
            self.executions = prune_budget_digests(self.executions, now)
            existing = (self.executions.get(key) or "").strip()
            if existing:
                if existing == digest:
                    return False, "该工具调用已经执行过，已抑制重复执行"
                return False, "工具调用 ID 被复用到了不同参数；已拒绝重复执行"
            self.executions[key] = digest
        err = self._persist()
        if err is not None:
            with self.mu:
                self.executions.pop(key, None)
            self.mark_write_failed(True)
            return False, f"执行幂等账本写入失败：{err}"
        self.mark_write_failed(False)
        return True, ""

    def release_tool_execution(
        self,
        call_id: str,
        *,
        scope_key: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """对应 Go ``releaseToolExecution``。"""
        call_id = (call_id or "").strip()
        if not call_id:
            return
        now = now or _now()
        key = budget_key(now, "exec", scope_key, actor_id, call_id)
        with self.mu:
            self.executions.pop(key, None)
        if self.save_state is None:
            return
        err = self._persist()
        if err is not None:
            self.mark_write_failed(True)
            self._audit(
                "budget_write_error",
                kind="release_execution",
                call_id=call_id,
                error=str(err),
                result=LOG_RESULT_FAILED,
            )

    # -- 用量查询 -----------------------------------------------------------
    def chat_usage_for_scope(
        self, scope_key: str, now: datetime | None = None
    ) -> tuple[int, int]:
        """今日群级聊天 token 与费用。对应 Go ``chatBudgetUsageForScope``。"""
        now = now or _now()
        with self.mu:
            self.tokens = prune_budget_usage(self.tokens, now)
            self.costs = prune_budget_usage(self.costs, now)
            tokens = count_budget_usage_matches(
                self.tokens, "chat", scope_key, "", True, False, now
            )
            costs = count_budget_usage_matches(
                self.costs, "chat", scope_key, "", True, False, now
            )
        return tokens, costs

    def usage_for_scope(
        self, scope_key: str, now: datetime | None = None
    ) -> tuple[int, int]:
        """今日群级生图与视觉用量（按次数）。对应 Go ``budgetUsageForScope``。"""
        now = now or _now()
        with self.mu:
            image = count_budget_prefix(
                self.reservations, budget_prefix(now, "image", scope_key)
            )
            vision = count_budget_prefix(
                self.reservations, budget_prefix(now, "vision", scope_key)
            )
        return image, vision

    def asr_usage_for_scope(self, scope_key: str, now: datetime | None = None) -> int:
        """今日群级语音转写用量。对应 Go ``asrBudgetUsageForScope``。"""
        now = now or _now()
        with self.mu:
            return count_budget_prefix(
                self.reservations, budget_prefix(now, "asr", scope_key)
            )


def ensure_budget(host: Any) -> BudgetLedger:
    """取宿主的账本；缺失时补建并接上宿主落盘/审计回调。"""
    agent = host_of(host)
    ledger = getattr(agent, "budget", None)
    if isinstance(ledger, BudgetLedger):
        return ledger

    def save_state() -> None:
        saver = getattr(agent, "save_runtime_state", None)
        if callable(saver):
            saver()

    def audit(event: str, **attrs: Any) -> None:
        audit_event(agent, event, **attrs)

    ledger = BudgetLedger(save_state=save_state, audit=audit)
    agent.budget = ledger
    return ledger


# ---------------------------------------------------------------------------
# Agent 方法
# ---------------------------------------------------------------------------
