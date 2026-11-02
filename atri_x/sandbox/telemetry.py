"""沙箱服务的只读快照：状态、探测结果与后端可用性缓存。

放在单独模块是为了让 :class:`atri_x.sandbox.manager.SandboxService` 保持「执行与
生命周期」单一职责；GUI、doctor、CLI 都只读这里的结论。
"""

from __future__ import annotations

import time
from typing import Any

from atri_x.sandbox.backend import CapabilityReport
from atri_x.sandbox.errors import SandboxError

__all__ = ["backend_info_cached", "probe_service", "status_snapshot"]

#: 后端可用性快照的缓存时间（GUI 轮询时不反复拉起引擎命令）。
_INFO_TTL_SECONDS = 30.0


async def backend_info_cached(service: Any, *, refresh: bool = False) -> Any:
    """带 TTL 的后端可用性快照。"""
    now = time.time()
    cached = getattr(service, "_backend_info", None)
    cached_at = float(getattr(service, "_backend_info_at", 0.0) or 0.0)
    if cached is None or refresh or now - cached_at > _INFO_TTL_SECONDS:
        info = await service.backend.backend_info()
        service._backend_info = info
        service._backend_info_at = now
        return info
    return cached


async def probe_service(service: Any, *, refresh: bool = False) -> CapabilityReport:
    """隔离能力探测（缓存；``refresh=True`` 重新实测）。"""
    report = getattr(service, "_report", None)
    if report is not None and not refresh:
        return report
    report = await service.backend.probe()
    service._report = report
    return report


async def status_snapshot(service: Any, *, refresh: bool = False) -> dict:
    """GUI / doctor 用的只读快照（不启动环境、不构建镜像）。"""
    info = await backend_info_cached(service, refresh=refresh)
    environments = [record.dump() for record in service._envs.values()]
    if refresh:
        try:
            environments = [item.dump() for item in await service.backend.list_envs()]
        except SandboxError:
            pass
    report = getattr(service, "_report", None)
    return {
        "settings": service.settings.dump(),
        "backend": info.dump(),
        "probe": report.dump() if report is not None else None,
        "queue": service.queue.snapshot().dump(),
        "environments": environments,
        "ledger": service.ledger.snapshot(),
        "audit_writable": service.audit.writable(),
        "started": bool(getattr(service, "_started", False)),
    }
