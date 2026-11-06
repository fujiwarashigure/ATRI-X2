"""沙箱生命周期操作：重置、销毁、紧急停止、空闲回收与过期清理。

从 :class:`atri_x.sandbox.manager.SandboxService` 抽出来，保持 manager 聚焦
「执行链路」；这里只操作服务已有的字段与方法（鸭子类型 ``service``），不反向 import
manager，避免循环依赖。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from atri_x.sandbox.errors import SandboxDeniedError, SandboxError
from atri_x.sandbox.files import prune_tree
from atri_x.sandbox.tenant import TenantEnvPaths, TenantKey

__all__ = ["destroy_tenant", "gc_loop", "gc_once", "reset_tenant", "stop_all"]

_GC_INTERVAL_MIN = 15.0
_GC_INTERVAL_MAX = 300.0


async def reset_tenant(service: Any, tenant: TenantKey, *, mode: str = "rebuild") -> dict:
    """重置租户环境：``restart`` / ``rebuild`` / ``wipe``（账本与审计不清空）。"""
    choice = (mode or "rebuild").strip().lower()
    if choice not in ("restart", "rebuild", "wipe"):
        raise SandboxDeniedError("reset mode 只能是 restart / rebuild / wipe")
    slug = tenant.slug
    async with service._tenant_lock(slug):
        record = service._envs.pop(slug, None)
        if record is not None:
            try:
                await service.backend.stop_env(record.name)
            except SandboxError:
                pass
            service._release_lease(record)
        if choice in ("rebuild", "wipe"):
            await service.backend.remove_env(
                tenant.container_name, proxy_name=tenant.proxy_name
            )
        paths = TenantEnvPaths.for_root(service.settings.root, tenant)
        removed = 0
        if choice == "wipe" and os.path.isdir(paths.root):
            removed = prune_tree(paths.root)
        os.makedirs(paths.workspace, exist_ok=True)
        os.makedirs(paths.home, exist_ok=True)
        service.ledger.mark_reset(slug)
        service.audit.write(
            "reset", tenant=tenant.display, slug=slug, mode=choice, removed=removed
        )
    return {
        "tenant": tenant.display,
        "mode": choice,
        "removed": removed,
        "ledger_kept": True,
        "message": {
            "restart": "已重启环境（文件保留）",
            "rebuild": "已按干净镜像重建环境（工作区/私有 home 保留）",
            "wipe": "已清空环境全部文件并重建",
        }[choice],
    }


async def destroy_tenant(service: Any, tenant: TenantKey) -> dict:
    """销毁环境与数据目录（配额/滥用账本保留，用于追溯）。"""
    result = await reset_tenant(service, tenant, mode="wipe")
    # 销毁环境不清理安全账本；prune 的参数是保留集合，会误删其他租户。
    return result


async def stop_all(service: Any, reason: str = "panic") -> int:
    """紧急停止：取消排队 + 停止全部环境（``/panic`` 用）。"""
    cancelled = service.queue.cancel_all()
    stopped = 0
    for record in list(service._envs.values()):
        try:
            await service.backend.stop_env(record.name)
            stopped += 1
        except SandboxError:
            continue
        service._release_lease(record)
    service._envs.clear()
    service.audit.write("stop_all", reason=reason, stopped=stopped, cancelled=cancelled)
    return stopped


async def gc_loop(service: Any) -> None:
    """后台空闲回收/保留期清理循环（只运行一次由 manager 保证）。"""
    interval = max(
        _GC_INTERVAL_MIN,
        min(_GC_INTERVAL_MAX, float(service.settings.resources.idle_ttl_s) / 4),
    )
    while not getattr(service, "_closed", False):
        await asyncio.sleep(interval)
        try:
            await gc_once(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - GC 不能拖垮服务
            service.audit.write("gc_error", error=f"{type(exc).__name__}: {exc}")


async def gc_once(service: Any) -> None:
    """执行一轮回收：空闲环境停止 + 过期租户数据清理。"""
    now = service._clock()
    ttl = float(service.settings.resources.idle_ttl_s)
    for record in list(service._envs.values()):
        if record.busy > 0 or not record.running:
            continue
        if now - record.last_used < ttl:
            continue
        slug = record.tenant.slug
        async with service._tenant_lock(slug):
            current = service._envs.get(slug)
            if current is not record or record.busy > 0:
                continue
            await service._stop_record(record, reason="idle")
            service._envs.pop(slug, None)
    await prune_expired(service, now)


async def prune_expired(service: Any, now: float) -> None:
    """清理超过数据保留期且不在运行的租户目录。"""
    root = service.settings.tenants_root
    if not os.path.isdir(root):
        return
    retention = float(service.settings.resources.data_retention_hours) * 3600
    active = {record.tenant.slug for record in service._envs.values()}
    for name in os.listdir(root):
        if name in active:
            continue
        path = os.path.join(root, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if now - mtime < retention:
            continue
        prune_tree(path)
        service.audit.write("tenant_pruned", slug=name, age_seconds=int(now - mtime))
