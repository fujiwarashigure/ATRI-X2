"""每日计量账本（对外统一入口）。

对应 Go：``internal/agent/budget.go``。实现按职责拆在同组三个模块里：

* :mod:`atri_x.agent.budget_keys` —— 账本键、克隆/裁剪、匹配计数与请求结构（纯函数）
* :mod:`atri_x.agent.budget_ledger` —— 账本本体 :class:`BudgetLedger`
* :mod:`atri_x.agent.budget_chat` —— 聊天软/硬预算与挂在 Agent 上的方法

本模块只做再导出，保持 ``from atri_x.agent.budget import X`` 对同僚继续成立
（例如 ``tool_auth`` 取 ``BudgetReservationRequest``）。
"""

from __future__ import annotations

from atri_x.agent.budget_chat import (
    BUDGET_METHODS,
    BudgetMixin,
    BudgetService,
    install_budget_services,
)
from atri_x.agent.budget_keys import (
    BUDGET_ID_REUSED,
    BUDGET_WRITE_FAILED,
    BudgetLimitSpec,
    BudgetReservationRequest,
    ChatBudgetLimitSpec,
    ChatBudgetReservation,
    budget_key,
    budget_key_parts,
    budget_prefix,
    clone_budget_digests,
    clone_budget_reservations,
    clone_budget_values,
    count_budget_prefix,
    count_budget_reservation_matches,
    count_budget_usage_matches,
    first_non_empty,
    llm_usage_cost_micros,
    llm_usage_total_tokens,
    metric_unit,
    prune_budget_digests,
    prune_budget_reservations,
    prune_budget_usage,
    short_hash,
)
from atri_x.agent.budget_ledger import BudgetLedger, ensure_budget
from atri_x.agent.model import ModelOption  # noqa: F401  (命令层共用同一批视图类型)

__all__ = [
    "BUDGET_ID_REUSED",
    "BUDGET_METHODS",
    "BUDGET_WRITE_FAILED",
    "BudgetLedger",
    "BudgetLimitSpec",
    "BudgetMixin",
    "BudgetReservationRequest",
    "BudgetService",
    "ChatBudgetLimitSpec",
    "ChatBudgetReservation",
    "budget_key",
    "budget_key_parts",
    "budget_prefix",
    "clone_budget_digests",
    "clone_budget_reservations",
    "clone_budget_values",
    "count_budget_prefix",
    "count_budget_reservation_matches",
    "count_budget_usage_matches",
    "ensure_budget",
    "first_non_empty",
    "install_budget_services",
    "llm_usage_cost_micros",
    "llm_usage_total_tokens",
    "metric_unit",
    "prune_budget_digests",
    "prune_budget_reservations",
    "prune_budget_usage",
    "short_hash",
]
