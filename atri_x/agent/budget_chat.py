"""聊天预算的 Agent 方法 + 次数限额规格 + 预算服务装配。

对应 Go：``internal/agent/budget.go`` 里挂在 ``*Agent`` 上的方法
（``callLimitSpecs`` / ``chatBudgetLimitSpecs`` / ``reserveBudget*`` /
``checkChatBudget`` / ``beginChatBudget`` / ``settleChatBudget`` /
``recordChatUsage`` / ``recordProviderRetry`` / ``reserveToolExecution`` /
``releaseToolExecution`` 与三个用量查询）。

账本本体在 :mod:`atri_x.agent.budget_ledger`，键与纯函数在
:mod:`atri_x.agent.budget_keys`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from atri_x.agent.budget_keys import (
    LOG_RESULT_SUCCEEDED,
    BudgetLimitSpec,
    BudgetReservationRequest,
    ChatBudgetLimitSpec,
    ChatBudgetReservation,
    _now,
    first_non_empty,
    metric_unit,
)
from atri_x.agent.budget_ledger import ensure_budget
from atri_x.agent.model import AgentService, install_methods
from atri_x.config import BudgetLimitsConfig, ModelPriceConfig, ModelSelection
from atri_x.contextmgr import estimate_messages_tokens, estimate_tools_tokens
from atri_x.errors import InvalidError
from atri_x.llm.llm import Usage

__all__ = [
    "BUDGET_METHODS",
    "BudgetMixin",
    "BudgetService",
    "install_budget_services",
]


class BudgetMixin:
    """``internal/agent/budget.go`` 里挂在 ``*Agent`` 上的方法。

    宿主成员：``self.budget``（:class:`BudgetLedger`）、``self.budget_limits``
    （``config.BudgetLimitsConfig``）、``self.is_group_scope(ctx)`` / ``self.scope(ctx)`` /
    ``self.actor(ctx)`` / ``self.group_policy_for_scope(scope)``、``self.pricing``
    （``price_for(model, now)``）、``self.context_runtime``（``prompt_budget``）、
    ``self.save_runtime_state()``。
    """

    # -- 限额规格 -----------------------------------------------------------
    def call_limit_specs(self, ctx: Any, kind: str) -> list[BudgetLimitSpec]:
        """当前生效的次数限额。对应 Go ``callLimitSpecs``。"""
        kind = (kind or "").strip().lower()
        limits: BudgetLimitsConfig = self.budget_limits.normalized()
        specs: list[BudgetLimitSpec] = []
        if self.is_group_scope(ctx):
            policy = self.group_policy_for_scope(self.scope(ctx))
            if kind == "image":
                specs.append(
                    BudgetLimitSpec(
                        label="本群生图日",
                        limit=int(getattr(policy, "image_quota", 0) or 0),
                        scope_match=True,
                    )
                )
                specs.append(
                    BudgetLimitSpec(
                        label="本群单用户生图日",
                        limit=int(getattr(policy, "user_image_quota", 0) or 0),
                        scope_match=True,
                        actor_match=True,
                    )
                )
            elif kind == "vision":
                specs.append(
                    BudgetLimitSpec(
                        label="本群视觉日",
                        limit=int(getattr(policy, "vision_quota", 0) or 0),
                        scope_match=True,
                    )
                )
                specs.append(
                    BudgetLimitSpec(
                        label="本群单用户视觉日",
                        limit=int(getattr(policy, "user_vision_quota", 0) or 0),
                        scope_match=True,
                        actor_match=True,
                    )
                )
            elif kind == "asr":
                specs.append(
                    BudgetLimitSpec(
                        label="本群语音转写日",
                        limit=int(getattr(policy, "asr_quota", 0) or 0),
                        scope_match=True,
                    )
                )
                specs.append(
                    BudgetLimitSpec(
                        label="本群单用户语音转写日",
                        limit=int(getattr(policy, "user_asr_quota", 0) or 0),
                        scope_match=True,
                        actor_match=True,
                    )
                )
        if kind == "image":
            specs.append(
                BudgetLimitSpec(label="全局生图日", limit=limits.global_image_daily)
            )
            specs.append(
                BudgetLimitSpec(
                    label="全局单用户生图日",
                    limit=limits.user_image_daily,
                    actor_match=True,
                )
            )
        elif kind == "vision":
            specs.append(
                BudgetLimitSpec(label="全局视觉日", limit=limits.global_vision_daily)
            )
            specs.append(
                BudgetLimitSpec(
                    label="全局单用户视觉日",
                    limit=limits.user_vision_daily,
                    actor_match=True,
                )
            )
        elif kind == "asr":
            specs.append(
                BudgetLimitSpec(label="全局语音转写日", limit=limits.global_asr_daily)
            )
            specs.append(
                BudgetLimitSpec(
                    label="全局单用户语音转写日",
                    limit=limits.user_asr_daily,
                    actor_match=True,
                )
            )
        return [spec for spec in specs if spec.limit > 0]

    def chat_budget_limit_specs(self, ctx: Any) -> list[ChatBudgetLimitSpec]:
        """当前生效的聊天 token/费用限额。对应 Go ``chatBudgetLimitSpecs``。"""
        limits: BudgetLimitsConfig = self.budget_limits.normalized()
        specs: list[ChatBudgetLimitSpec] = []
        if self.is_group_scope(ctx):
            policy = self.group_policy_for_scope(self.scope(ctx))
            specs.append(
                ChatBudgetLimitSpec(
                    label="本群日聊天 token",
                    limit=int(getattr(policy, "chat_tokens_quota", 0) or 0),
                    metric="tokens",
                    scope_match=True,
                )
            )
            specs.append(
                ChatBudgetLimitSpec(
                    label="本群日聊天费用",
                    limit=int(float(getattr(policy, "chat_cost_quota", 0.0) or 0.0) * 1_000_000),
                    metric="cost",
                    scope_match=True,
                )
            )
        specs.append(
            ChatBudgetLimitSpec(
                label="全局日聊天 token", limit=limits.global_chat_tokens_daily
            )
        )
        specs.append(
            ChatBudgetLimitSpec(
                label="全局单用户日聊天 token",
                limit=limits.user_chat_tokens_daily,
                metric="tokens",
                actor_match=True,
            )
        )
        specs.append(
            ChatBudgetLimitSpec(
                label="全局日聊天费用",
                limit=int(limits.global_chat_cost_daily * 1_000_000),
                metric="cost",
            )
        )
        specs.append(
            ChatBudgetLimitSpec(
                label="全局单用户日聊天费用",
                limit=int(limits.user_chat_cost_daily * 1_000_000),
                metric="cost",
                actor_match=True,
            )
        )
        return [spec for spec in specs if spec.limit > 0]

    # -- 次数预留 -----------------------------------------------------------
    def reserve_budget_batch(
        self, ctx: Any, kind: str, call_ids: Iterable[str]
    ) -> tuple[bool, str]:
        """对应 Go ``reserveBudgetBatch``。"""
        requests = [BudgetReservationRequest(call_id=call_id) for call_id in call_ids]
        return self.reserve_budget_batch_requests(ctx, kind, requests)

    def reserve_budget(self, ctx: Any, kind: str, call_id: str) -> tuple[bool, str]:
        """对应 Go ``reserveBudget``。"""
        return self.reserve_budget_batch(ctx, kind, [call_id])

    def reserve_budget_request(
        self, ctx: Any, kind: str, request: BudgetReservationRequest
    ) -> tuple[bool, str]:
        """对应 Go ``reserveBudgetRequest``。"""
        return self.reserve_budget_batch_requests(ctx, kind, [request])

    def reserve_budget_batch_requests(
        self, ctx: Any, kind: str, requests: Sequence[BudgetReservationRequest]
    ) -> tuple[bool, str]:
        """对应 Go ``reserveBudgetBatchRequests``。"""
        specs = self.call_limit_specs(ctx, kind)
        if not specs:
            return True, ""
        scope_key, actor_id = self._budget_scope(ctx)
        return ensure_budget(self).reserve_batch(
            kind,
            requests,
            specs=specs,
            scope_key=scope_key,
            actor_id=actor_id,
        )

    # -- 聊天预算 -----------------------------------------------------------
    def check_chat_budget(self, ctx: Any, selection: ModelSelection) -> None:
        """每次付费 LLM 调用前的软检查。对应 Go ``checkChatBudget``。"""
        specs = self.chat_budget_limit_specs(ctx)
        if not specs:
            return
        scope_key, actor_id = self._budget_scope(ctx)
        ensure_budget(self).check_chat_budget(
            specs, scope_key=scope_key, actor_id=actor_id
        )

    def begin_chat_budget(
        self,
        ctx: Any,
        selection: ModelSelection,
        messages: Sequence[Any],
        tools: Sequence[Any],
        max_output: int,
        call_id: str = "",
    ) -> ChatBudgetReservation | None:
        """软检查 + （启用硬限制时）调用前原子预占。对应 Go ``beginChatBudget``。

        同步方法（``AgentSeams.begin_chat_budget`` 直接返回它的结果），因此窗口解析走
        :meth:`ContextRuntimeState.prompt_budget_sync`（缓存/手工配置/默认值）。
        """
        specs = self.chat_budget_limit_specs(ctx)
        if not specs:
            return None
        limits: BudgetLimitsConfig = self.budget_limits.normalized()
        if not limits.chat_hard_limit:
            self.check_chat_budget(ctx, selection)
            return None
        ledger = ensure_budget(self)
        if not ledger.writable():
            raise InvalidError("额度账本当前不可写，已拒绝新的受限调用")

        from atri_x.agent.context_runtime import ensure_context_runtime

        estimated_input = estimate_messages_tokens(messages) + estimate_tools_tokens(
            tools
        )
        if estimated_input < 1:
            estimated_input = 1
        output_reserve = int(max_output or 0)
        if limits.chat_hard_limit_reserve_output_tokens > 0:
            output_reserve = int(limits.chat_hard_limit_reserve_output_tokens)
        if output_reserve <= 0:
            output_reserve = int(
                ensure_context_runtime(self)
                .prompt_budget_sync(selection)
                .reserve_output_tokens
            )
        if output_reserve <= 0:
            output_reserve = 512
        reserved_tokens = estimated_input + output_reserve

        reserved_cost = 0
        if any(spec.metric == "cost" for spec in specs):
            model = (selection.model or "").strip()
            price = self.pricing.price_for(model, _now())
            if price is None:
                raise InvalidError(
                    f'已启用聊天费用硬限制，但模型 "{model}" 没有配置价格；'
                    "请先配置 [maintenance.daily_report].prices"
                )
            reserved_cost = int(
                price.compute_cost(estimated_input, output_reserve, 0) * 1_000_000 + 0.5
            )

        scope_key, actor_id = self._budget_scope(ctx)
        return ledger.begin_chat_budget(
            specs=specs,
            reserved_tokens=reserved_tokens,
            reserved_cost=reserved_cost,
            scope_key=scope_key,
            actor_id=actor_id,
            model=selection.model,
            call_id=call_id,
        )

    def settle_chat_budget(
        self,
        ctx: Any,
        reservation: ChatBudgetReservation | None,
        usage: Usage | None,
        release: bool = False,
    ) -> None:
        """结算一次硬预算预留。对应 Go ``settleChatBudget``。"""
        if reservation is None:
            return
        ensure_budget(self).settle_chat_budget(
            reservation, usage, release, price_for=self._price_for
        )

    def record_chat_usage(
        self, ctx: Any, model: str, usage: Usage | None, call_id: str = ""
    ) -> None:
        """记录调用后的 token/费用。对应 Go ``recordChatUsage``。"""
        if usage is None:
            return
        scope_key, actor_id = self._budget_scope(ctx)
        ensure_budget(self).record_chat_usage(
            model,
            usage,
            scope_key=scope_key,
            actor_id=actor_id,
            call_id=call_id,
            price_for=self._price_for,
        )

    def record_provider_retry(self, ctx: Any, provider: str) -> None:
        """记录一次 provider 重试。对应 Go ``recordProviderRetry``。"""
        scope_key, actor_id = self._budget_scope(ctx)
        ensure_budget(self).record_provider_retry(
            provider, scope_key=scope_key, actor_id=actor_id
        )

    # -- 执行幂等 -----------------------------------------------------------
    def reserve_tool_execution(
        self, ctx: Any, call_id: str, digest: str = ""
    ) -> tuple[bool, str]:
        """对应 Go ``reserveToolExecution``。"""
        scope_key, actor_id = self._budget_scope(ctx)
        return ensure_budget(self).reserve_tool_execution(
            call_id, digest, scope_key=scope_key, actor_id=actor_id
        )

    def release_tool_execution(self, ctx: Any, call_id: str) -> None:
        """对应 Go ``releaseToolExecution``。"""
        scope_key, actor_id = self._budget_scope(ctx)
        ensure_budget(self).release_tool_execution(
            call_id, scope_key=scope_key, actor_id=actor_id
        )

    # -- 用量查询 -----------------------------------------------------------
    def chat_budget_usage_for_scope(self, ctx: Any) -> tuple[int, int]:
        """今日群级聊天 token/费用（非群作用域返回 0）。对应 Go ``chatBudgetUsageForScope``。"""
        if not self.is_group_scope(ctx):
            return 0, 0
        scope_key, _ = self._budget_scope(ctx)
        return ensure_budget(self).chat_usage_for_scope(scope_key)

    def budget_usage_for_scope(self, ctx: Any) -> tuple[int, int]:
        """今日群级生图/视觉用量。对应 Go ``budgetUsageForScope``。"""
        if not self.is_group_scope(ctx):
            return 0, 0
        scope_key, _ = self._budget_scope(ctx)
        return ensure_budget(self).usage_for_scope(scope_key)

    def asr_budget_usage_for_scope(self, ctx: Any) -> int:
        """今日群级语音转写用量。对应 Go ``asrBudgetUsageForScope``。"""
        if not self.is_group_scope(ctx):
            return 0
        scope_key, _ = self._budget_scope(ctx)
        return ensure_budget(self).asr_usage_for_scope(scope_key)

    def budget_writable(self) -> bool:
        """账本当前是否可写。对应 Go ``budgetWritable``。"""
        return ensure_budget(self).writable()

    def mark_budget_write_failed(self, failed: bool) -> None:
        """对应 Go ``markBudgetWriteFailed``。"""
        ensure_budget(self).mark_write_failed(failed)

    # -- 内部 ---------------------------------------------------------------
    def _budget_scope(self, ctx: Any) -> tuple[str, str]:
        """``(scope_key, actor_id)``：账本的群桶与用户桶。"""
        from atri_x.agent.context_policy import context_overflow_key

        scope = self.scope(ctx)
        actor = self.actor(ctx)
        actor_id = first_non_empty(
            str(getattr(actor, "id", "") or ""),
            str(getattr(actor, "platform_user_id", "") or ""),
        )
        return context_overflow_key(scope), actor_id

    def _price_for(self, model: str, at: datetime) -> ModelPriceConfig | None:
        """价格表查询（未配置返回 ``None``）。"""
        pricing = getattr(self, "pricing", None)
        if pricing is None:
            return None
        try:
            return pricing.price_for(model, at)
        except Exception:  # noqa: BLE001 - 价格查询失败按未配置处理
            return None


#: 可以装到 Agent 实例上的方法。
BUDGET_METHODS: tuple[str, ...] = (
    "call_limit_specs",
    "chat_budget_limit_specs",
    "reserve_budget_batch",
    "reserve_budget",
    "reserve_budget_request",
    "reserve_budget_batch_requests",
    "check_chat_budget",
    "begin_chat_budget",
    "settle_chat_budget",
    "record_chat_usage",
    "record_provider_retry",
    "reserve_tool_execution",
    "release_tool_execution",
    "chat_budget_usage_for_scope",
    "budget_usage_for_scope",
    "asr_budget_usage_for_scope",
    "budget_writable",
    "mark_budget_write_failed",
)


class BudgetService(AgentService):
    """预算服务门面（``agent.services['budget']``）。

    方法都由装到 Agent 上的同名单方法提供（见 :func:`install_budget_services`）。
    """


def install_budget_services(agent: Any) -> BudgetService:
    """装配预算服务：把方法装到 Agent 上并注册门面（幂等）。"""
    install_methods(agent, BudgetMixin, BUDGET_METHODS)
    ensure_budget(agent)
    services = getattr(agent, "services", None)
    existing = services.get("budget") if isinstance(services, dict) else None
    if isinstance(existing, BudgetService):
        return existing
    service = BudgetService(agent)
    if isinstance(services, dict):
        services["budget"] = service
    return service
