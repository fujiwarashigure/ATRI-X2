"""每日计量账本的键、裁剪、计数与请求结构。

对应 Go：``internal/agent/budget.go`` 的纯函数部分：账本键（``budgetKey`` /
``budgetPrefix`` / ``budgetKeyParts``）、克隆与裁剪（``cloneBudget*`` /
``pruneBudget*``）、匹配计数（``countBudget*``）、用量与费用换算
（``llmUsageTotalTokens`` / ``llmUsageCostMicros``）以及各 ``*Spec`` 结构。

**拆分说明**：``budget.go`` 把这些纯函数与 ``Agent`` 的账本方法混在一个文件里；
Python 侧按职责拆成 ``budget_keys``（本文件，纯函数）/ ``budget_ledger``（账本本体）/
``budget_chat``（聊天预算与 Agent 方法），``atri_x.agent.budget`` 作为统一入口再导出。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from atri_x.config import ModelPriceConfig
from atri_x.llm.llm import Usage

__all__ = [
    "BUDGET_ID_REUSED",
    "BUDGET_WRITE_FAILED",
    "BudgetLimitSpec",
    "BudgetReservationRequest",
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
    "first_non_empty",
    "llm_usage_cost_micros",
    "llm_usage_total_tokens",
    "metric_unit",
    "prune_budget_digests",
    "prune_budget_reservations",
    "prune_budget_usage",
    "short_hash",
]

#: 账本写盘失败时给用户的统一前缀。对应 Go 里的字面量。
BUDGET_WRITE_FAILED = "额度账本写入失败："
#: call ID 被复用到不同参数。对应 Go 里的字面量。
BUDGET_ID_REUSED = "调用 ID 被复用到了不同参数；请检查工具调用方是否错误重放"

LOG_RESULT_SUCCEEDED = "succeeded"
LOG_RESULT_FAILED = "failed"


DATE_FORMAT = "%Y-%m-%d"


def _now() -> datetime:
    """本地时区的 aware 当前时间。对应 Go ``time.Now()``。"""
    return datetime.now().astimezone()


def _date_of(now: datetime) -> str:
    return now.strftime(DATE_FORMAT)


# ---------------------------------------------------------------------------
# 共享助手（唯一出处在 identity.go / tool_auth.go：延迟导入，缺失时本地兜底）
# ---------------------------------------------------------------------------
def _shared(module: str, name: str) -> Any:
    import importlib

    try:
        mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


def first_non_empty(*values: str) -> str:
    """第一个 strip 后非空的值（返回 strip 后的值）。对应 Go ``firstNonEmpty``。"""
    shared = _shared("atri_x.agent.identity", "first_non_empty")
    if shared is not None and shared is not first_non_empty:
        return shared(*values)
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


def short_hash(value: str) -> str:
    """FNV-32a 的十六进制短哈希。对应 Go ``shortHash``（tool_auth.go）。"""
    shared = _shared("atri_x.agent.tool_auth", "short_hash")
    if shared is not None and shared is not short_hash:
        return shared(value)
    digest = 0x811C9DC5
    for byte in (value or "").encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return format(digest, "x")


# ---------------------------------------------------------------------------
# 键与克隆
# ---------------------------------------------------------------------------
def budget_key(
    now: datetime, kind: str, scope_key: str, actor_id: str, call_id: str
) -> str:
    """账本键。对应 Go ``budgetKey``。"""
    scope_key = (scope_key or "").strip() or "unknown"
    actor_id = (actor_id or "").strip()
    call_id = (call_id or "").strip()
    return "|".join([_date_of(now), (kind or "").strip(), scope_key, actor_id, call_id])


def budget_prefix(now: datetime, kind: str, scope_key: str) -> str:
    """按「日期|种类|scope|」前缀统计的键前缀。对应 Go ``budgetPrefix``。"""
    return "|".join(
        [_date_of(now), (kind or "").strip(), (scope_key or "").strip(), ""]
    )


def budget_key_parts(key: str) -> tuple[str, str, str, str, bool]:
    """拆键；段数不是 5 时返回 ``ok=False``。对应 Go ``budgetKeyParts``。"""
    parts = (key or "").strip().split("|", 4)
    if len(parts) != 5:
        return "", "", "", "", False
    return parts[0], parts[1], parts[2], parts[3], True


def clone_budget_reservations(input: Mapping[str, int] | None) -> dict[str, int]:
    """拷贝并丢掉空键/非正值。对应 Go ``cloneBudgetReservations``。"""
    if not input:
        return {}
    out: dict[str, int] = {}
    for key, value in input.items():
        key = (key or "").strip()
        if not key or value <= 0:
            continue
        out[key] = int(value)
    return out


def clone_budget_values(input: Mapping[str, int] | None) -> dict[str, int]:
    """与 :func:`clone_budget_reservations` 同义（Go 里是另一个名字）。"""
    return clone_budget_reservations(input)


def clone_budget_digests(input: Mapping[str, str] | None) -> dict[str, str]:
    """拷贝并丢掉空键/空值。对应 Go ``cloneBudgetDigests``。"""
    if not input:
        return {}
    out: dict[str, str] = {}
    for key, value in input.items():
        key = (key or "").strip()
        value = (value or "").strip()
        if not key or not value:
            continue
        out[key] = value
    return out


def prune_budget_reservations(
    reservations: Mapping[str, int] | None, now: datetime
) -> dict[str, int]:
    """只保留当天与前一天的条目。对应 Go ``pruneBudgetReservations``。"""
    if not reservations:
        return {}
    cutoff = _date_of(now - timedelta(days=1))
    out: dict[str, int] = {}
    for key, value in reservations.items():
        date, _, ok = (key or "").partition("|")
        if not ok or date < cutoff or value <= 0:
            continue
        out[key] = int(value)
    return out


def prune_budget_usage(
    values: Mapping[str, int] | None, now: datetime
) -> dict[str, int]:
    """与 :func:`prune_budget_reservations` 同义。对应 Go ``pruneBudgetUsage``。"""
    return prune_budget_reservations(values, now)


def prune_budget_digests(
    digests: Mapping[str, str] | None, now: datetime
) -> dict[str, str]:
    """只保留当天与前一天的非空摘要。对应 Go ``pruneBudgetDigests``。"""
    if not digests:
        return {}
    cutoff = _date_of(now - timedelta(days=1))
    out: dict[str, str] = {}
    for key, value in digests.items():
        date, _, ok = (key or "").partition("|")
        if not ok or date < cutoff or (value or "").strip() == "":
            continue
        out[key] = value
    return out


def count_budget_prefix(reservations: Mapping[str, int] | None, prefix: str) -> int:
    """前缀命中的条目数。对应 Go ``countBudgetPrefixLocked``。"""
    if not reservations:
        return 0
    return sum(1 for key in reservations if key.startswith(prefix))


def _matches(
    values: Mapping[str, int] | None,
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool,
    actor_match: bool,
    today: str,
) -> list[int]:
    if not values:
        return []
    kind = (kind or "").strip()
    scope_key = (scope_key or "").strip()
    actor_id = (actor_id or "").strip()
    out: list[int] = []
    for key, value in values.items():
        date, key_kind, key_scope, key_actor, ok = budget_key_parts(key)
        if not ok or date != today or key_kind != kind or value <= 0:
            continue
        if scope_match and key_scope != scope_key:
            continue
        if actor_match and key_actor != actor_id:
            continue
        out.append(int(value))
    return out


def count_budget_reservation_matches(
    values: Mapping[str, int] | None,
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool = False,
    actor_match: bool = False,
    now: datetime | None = None,
) -> int:
    """命中的**条目数**（生图/视觉/语音按次数计）。对应 Go ``countBudgetReservationMatchesLocked``。"""
    today = _date_of(now or _now())
    return len(
        _matches(values, kind, scope_key, actor_id, scope_match, actor_match, today)
    )


def count_budget_usage_matches(
    values: Mapping[str, int] | None,
    kind: str,
    scope_key: str,
    actor_id: str,
    scope_match: bool = False,
    actor_match: bool = False,
    now: datetime | None = None,
) -> int:
    """命中的**数值之和**（聊天 token/费用）。对应 Go ``countBudgetUsageMatchesLocked``。"""
    today = _date_of(now or _now())
    return sum(
        _matches(values, kind, scope_key, actor_id, scope_match, actor_match, today)
    )


# ---------------------------------------------------------------------------
# 用量与费用
# ---------------------------------------------------------------------------
def llm_usage_total_tokens(usage: Usage | None) -> int:
    """总 token（缺失时用 prompt+completion）。对应 Go ``llmUsageTotalTokens``。"""
    if usage is None:
        return 0
    tokens = int(usage.total_tokens or 0)
    if tokens <= 0:
        tokens = int(usage.prompt_tokens or 0) + int(usage.completion_tokens or 0)
    return tokens


def llm_usage_cost_micros(price: ModelPriceConfig, usage: Usage | None) -> int:
    """按价格表算费用（微单位）。对应 Go ``llmUsageCostMicros``。"""
    if usage is None:
        return 0
    cost = price.compute_cost(
        int(usage.prompt_tokens or 0),
        int(usage.completion_tokens or 0),
        int(usage.cache_hit_tokens or 0),
    )
    return int(cost * 1_000_000 + 0.5)


def metric_unit(metric: str) -> str:
    """限额单位（费用是微单位）。对应 Go ``checkChatBudget`` 里的字面量。"""
    return "微单位" if metric == "cost" else "token"


# ---------------------------------------------------------------------------
# 规格与请求
# ---------------------------------------------------------------------------
@dataclass
class BudgetLimitSpec:
    """一条次数限额。对应 Go ``budgetLimitSpec``。"""

    label: str = ""
    limit: int = 0
    scope_match: bool = False
    actor_match: bool = False


@dataclass
class ChatBudgetLimitSpec:
    """一条聊天 token/费用限额。对应 Go ``chatBudgetLimitSpec``。"""

    label: str = ""
    limit: int = 0
    metric: str = "tokens"
    scope_match: bool = False
    actor_match: bool = False


@dataclass
class BudgetReservationRequest:
    """一次预留请求（call ID + 参数摘要）。对应 Go ``budgetReservationRequest``。"""

    call_id: str = ""
    digest: str = ""


@dataclass
class ChatBudgetReservation:
    """硬预算下的一次预占，必须在所有返回路径上结算。对应 Go ``chatBudgetReservation``。"""

    key: str = ""
    call_id: str = ""
    model: str = ""
    reserved_tokens: int = 0
    reserved_cost: int = 0


@dataclass
class _Pending:
    key: str = ""
    digest: str = ""


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
