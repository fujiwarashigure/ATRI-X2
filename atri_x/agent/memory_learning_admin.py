"""后台记忆/学习的管理查询门面。

对应 Go：``internal/agent/memory_learning_admin.go``。

两个方法都是"给命令模块与运维面板用的只读计数"，未装配对应服务时返回 0 而不是
报错——与 Go 的 ``nil`` 早退一致。

**ctx 说明**：Go 把 ``context.Context`` 一路穿到 ``Service.Count(ctx, platform, scopeID)``，
本仓的 :mod:`atri_x.angelmemory` / :mod:`atri_x.selflearning` 是**同步存储**，签名里不带
ctx（``Service.count(platform, scope_id)``）。真实事故：这里照 Go 传了 ctx，两个运维端点
``GET /plugins/memory`` / ``GET /plugins/learning`` 每次都回
``500 {"error":"Service.count() takes 3 positional arguments but 4 were given"}`` ——
而全仓另外 3 个调用点用的都是 2 参形状。方法仍保留 ``ctx`` 形参（调用方按 Go 传）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["angel_memory_count", "self_learning_stats"]


async def angel_memory_count(agent: Any, ctx: Any, platform: str, scope_id: str) -> int:
    """某个作用域的长期记忆条数。对应 Go ``Agent.AngelMemoryCount``。"""
    memory = getattr(agent, "angel_memory", None) if agent is not None else None
    if memory is None:
        return 0
    result = memory.count(platform, scope_id)
    if hasattr(result, "__await__"):
        result = await result
    return int(result)


async def self_learning_stats(
    agent: Any, ctx: Any, platform: str, scope_id: str
) -> tuple[int, int]:
    """某个作用域的待审/已批准候选数。对应 Go ``Agent.SelfLearningStats``。"""
    learning = getattr(agent, "self_learning", None) if agent is not None else None
    if learning is None:
        return 0, 0
    result = learning.stats(platform, scope_id)
    if hasattr(result, "__await__"):
        result = await result
    if isinstance(result, tuple):
        pending, approved = result[0], result[1]
        return int(pending), int(approved)
    return 0, 0
