"""可重试的后端清理与孤儿宽限期。

对应 Go：``internal/media/lifecycle.go``。

一轮清理的顺序（与 Go 逐字一致，顺序本身是语义的一部分）：

1. ``ReconcileHistory`` —— 跨库历史 owner 分页对账；
2. ``ExpireOutputs`` —— 释放已过期的输出关联（输出缓存到期释放引用；待重试与
   Session 引用独立保留，不由这里碰）；
3. ``CheckReferences`` —— **只读**一致性检查；有异常就保守停止并报告；
4. ``ClaimOrphans(cutoff = now - OrphanGrace)`` —— 认领孤儿。认领是 SQLite 写事务，
   与"引用添加"互斥，所以**认领之后不会再有新引用**；
5. 并发删除（最多 :data:`CLEANUP_CONCURRENCY` 个）：按记录里的**实际存储位置**选
   后端，双副本**先删远端再删本地**，全部成功后才 ``FinishDelete``。

删除失败（含所需后端不可用）时记录保留 ``deleting`` 状态，下一轮幂等重试：
``delete_object`` 从库里重读该行，仍然 ``deleting`` 才继续删。

取消语义：Go 返回 ``errors.Join(failures..., ctx.Err())``。本仓在 ctx 已取消时抛
:class:`asyncio.CancelledError`（其余失败挂成 ``__notes__``）；否则抛出唯一的失败，
多个失败时抛 :class:`ExceptionGroup`。**失败原因不被包装**，以便调用方按身份判定
（Go 用 ``%w`` 保留 ``errors.Is``，本仓用原样透传 + ``add_note``）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from atri_x.errors import NotFoundError
from atri_x.media.generation import forget_generation
from atri_x.media.types import MediaError, is_cancelled, raise_if_cancelled

__all__ = ["CLEANUP_CONCURRENCY", "ORPHAN_GRACE", "cleanup", "delete_object"]

#: 孤儿宽限期：从**最后一个引用释放**开始算，不是导入时间。对应 Go ``OrphanGrace``。
ORPHAN_GRACE = timedelta(hours=1)
#: 单轮清理的并发删除数。对应 Go ``cleanupConcurrency``。
CLEANUP_CONCURRENCY = 10


async def cleanup(manager: Any, ctx: Any = None) -> None:
    """跑一轮媒体清理。对应 Go ``Cleanup``。"""
    # 只有清理轮次之间在这里串行；媒体操作走 per-ID 锁。
    async with manager.cleanup_lock:
        raise_if_cancelled(ctx)
        await manager.reconcile_history(ctx)
        manager.store.media().expire_outputs(manager.now())
        issues = manager.store.media().check_references()
        if issues:
            raise MediaError(
                f"media reference inconsistencies prevent cleanup: {list(issues)}"
            )
        items = manager.store.media().claim_orphans(manager.now() - ORPHAN_GRACE)
        if not items:
            return
        failures: list[BaseException] = []
        slots = max(1, min(CLEANUP_CONCURRENCY, len(items)))
        gate = asyncio.Semaphore(slots)

        async def run(item: Any) -> None:
            async with gate:
                if is_cancelled(ctx):
                    return
                try:
                    await delete_object(manager, ctx, item.id)
                except BaseException as exc:  # noqa: BLE001 - 收集后统一汇报
                    failures.append(exc)

        await asyncio.gather(*(run(item) for item in items))

    if not failures:
        raise_if_cancelled(ctx)
        return
    cancellations = [f for f in failures if _is_cancellation(f)]
    others = [f for f in failures if not _is_cancellation(f)]
    if is_cancelled(ctx) or cancellations:
        error = asyncio.CancelledError("media cleanup cancelled")
        for extra in others + cancellations:
            error.add_note(f"media cleanup failure: {extra!r}")
        raise error
    if len(others) == 1:
        raise others[0]
    raise BaseExceptionGroup("media cleanup failed", others)


async def delete_object(manager: Any, ctx: Any, id: str) -> None:
    """删除一个已认领的对象。对应 Go ``Manager.deleteObject``。

    幂等：``Get`` 不存在、或该行不再 ``deleting`` 时直接返回；
    远端与本地副本都删掉之后才 ``FinishDelete``，并顺手遗忘生成参数记录
    （对象没了，那条记录再也不会被查到）。
    """
    unlock = await manager.objects.acquire(ctx, id)
    try:
        try:
            item = manager.store.media().get(id)
        except NotFoundError:
            return
        if not item.deleting:
            return
        primary = await manager.backend_for_stored_media(ctx, item)
        local_backend = None
        remote_backend = None
        if item.backend == "local":
            local_backend = primary
        else:
            remote_backend = primary
        if item.object_key and remote_backend is None:
            # 所需后端不可用 -> 报错保留 deleting，交由下一轮幂等重试。
            remote_backend = await manager.remote_backend(ctx)
        if remote_backend is not None:
            await _remove(remote_backend, ctx, item, "remote")
        if local_backend is not None:
            await _remove(local_backend, ctx, item, "local")
        manager.store.media().finish_delete(id)
        await forget_generation(manager, ctx, id)
    finally:
        unlock()


async def _remove(backend: Any, ctx: Any, item: Any, side: str) -> None:
    """删一侧副本；失败时附上"哪一侧"的上下文再原样抛出。"""
    try:
        await backend.remove(ctx, item)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, Exception):
            exc.add_note(f"media cleanup failed on the {side} copy of {item.id}")
        raise


def _is_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    return exc.__class__.__name__ in ("CancelledError", "DeadlineExceededError")
