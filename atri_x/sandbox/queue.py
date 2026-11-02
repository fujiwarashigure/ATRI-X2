"""有界、公平、可取消的准入队列。

修改清单 §9：资源不足时**排队**而不是让所有环境同时抢内存；每租户限制排队数；
交互任务优先但后台任务不能永久饿死；**拿到名额后才启动环境**；等待有上限、可取消、
可查状态。

设计要点：

* 名额（``Lease``）的单位是「一个正在运行的环境」——运行中的环境占用一个名额，
  空闲回收/停止/重置时归还；同一个环境里的命令不再重复占名额；
* 释放名额时按「交互优先 → 轮转避开上一轮租户 → 最早入队」挑选下一个；
  后台等待者连续被跳过 ``starvation_rounds`` 次后强制提升，避免饿死；
* 队列有界：全局超过 ``queue_max_size`` 或单租户超过 ``queue_max_per_tenant``
  立即抛配额错误，不无限堆积；
* 整个等待过程是 ``await``，不占模型循环，Agent 不需要轮询。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x.sandbox.errors import (
    SandboxDeniedError,
    SandboxQueueTimeoutError,
    SandboxQuotaError,
)

__all__ = ["AdmissionController", "Lease", "QueueStats"]


@dataclass
class QueueStats:
    """队列快照（GUI / doctor 用）。"""

    capacity: int = 0
    running: int = 0
    waiting: int = 0
    per_tenant: dict[str, int] = field(default_factory=dict)
    last_served: str = ""
    acquired_total: int = 0
    timed_out_total: int = 0
    rejected_total: int = 0

    def dump(self) -> dict:
        """字典形态。"""
        return {
            "capacity": self.capacity,
            "running": self.running,
            "waiting": self.waiting,
            "per_tenant": dict(self.per_tenant),
            "last_served": self.last_served,
            "acquired_total": self.acquired_total,
            "timed_out_total": self.timed_out_total,
            "rejected_total": self.rejected_total,
        }


class Lease:
    """一个已获得的环境名额。``release()`` 可重复调用（幂等）。"""

    def __init__(self, controller: "AdmissionController", tenant: str, kind: str) -> None:
        self._controller = controller
        self.tenant = tenant
        self.kind = kind
        self._released = False
        self.acquired_at = time.time()

    @property
    def released(self) -> bool:
        """是否已归还。"""
        return self._released

    def release(self) -> None:
        """归还名额并唤醒下一个等待者。"""
        if self._released:
            return
        self._released = True
        self._controller._release(self)  # noqa: SLF001 - 控制器内部协议

    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.release()


@dataclass
class _Waiter:
    tenant: str
    kind: str
    seq: int
    future: asyncio.Future
    queued_at: float = field(default_factory=time.time)
    skipped: int = 0


class AdmissionController:
    """全局环境名额 + 公平排队。"""

    def __init__(
        self,
        *,
        capacity: int = 4,
        queue_max_size: int = 64,
        per_tenant_limit: int = 4,
        wait_timeout: float = 300.0,
        starvation_rounds: int = 8,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.queue_max_size = max(1, int(queue_max_size))
        self.per_tenant_limit = max(1, int(per_tenant_limit))
        self.wait_timeout = max(1.0, float(wait_timeout))
        self.starvation_rounds = max(1, int(starvation_rounds))
        self._running = 0
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self._last_served = ""
        self._acquired_total = 0
        self._timed_out_total = 0
        self._rejected_total = 0
        self._active: dict[str, int] = {}

    # -- 公共接口 -----------------------------------------------------------
    async def acquire(
        self, tenant: str, *, kind: str = "interactive", timeout: float | None = None
    ) -> Lease:
        """申请一个环境名额；队列满/单租户超限立即拒绝，等待超时抛排队超时。"""
        tenant = tenant or "local"
        if self._running < self.capacity and not self._waiters:
            self._check_tenant_limit(tenant)
            return self._grant(tenant, kind)
        self._check_bounds(tenant)
        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            tenant=tenant, kind=_normalize_kind(kind), seq=self._next_seq(), future=loop.create_future()
        )
        self._waiters.append(waiter)
        self._active[tenant] = self._active.get(tenant, 0) + 1
        limit = self.wait_timeout if timeout is None else max(0.1, float(timeout))
        try:
            lease = await asyncio.wait_for(waiter.future, timeout=limit)
        except asyncio.TimeoutError as exc:
            self._drop_waiter(waiter)
            self._timed_out_total += 1
            raise SandboxQueueTimeoutError(
                "沙箱排队超时：前面的任务占满了执行名额",
                detail=f"已等待 {limit:.0f}s（可用 [sandbox].queue_wait_seconds 调整上限）",
            ) from exc
        except asyncio.CancelledError:
            self._drop_waiter(waiter)
            raise
        return lease

    def snapshot(self) -> QueueStats:
        """当前队列状态。"""
        per_tenant = dict(self._active)
        return QueueStats(
            capacity=self.capacity,
            running=self._running,
            waiting=len(self._waiters),
            per_tenant=per_tenant,
            last_served=self._last_served,
            acquired_total=self._acquired_total,
            timed_out_total=self._timed_out_total,
            rejected_total=self._rejected_total,
        )

    def cancel_all(self) -> int:
        """取消全部等待者（``/panic`` 用）：返回取消数量。"""
        waiters = list(self._waiters)
        self._waiters.clear()
        self._active.clear()
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(
                    SandboxDeniedError("沙箱排队已被 /panic 紧急停止")
                )
        return len(waiters)

    # -- 内部 ---------------------------------------------------------------
    def _release(self, lease: Lease) -> None:
        self._running = max(0, self._running - 1)
        count = self._active.get(lease.tenant, 0)
        if count <= 1:
            self._active.pop(lease.tenant, None)
        else:
            self._active[lease.tenant] = count - 1
        self._dispatch()

    def _dispatch(self) -> None:
        while self._running < self.capacity and self._waiters:
            index = self._pick_index()
            waiter = self._waiters.pop(index)
            count = self._active.get(waiter.tenant, 0)
            if count <= 1:
                self._active.pop(waiter.tenant, None)
            else:
                self._active[waiter.tenant] = count - 1
            self._running += 1
            self._acquired_total += 1
            self._last_served = waiter.tenant
            for other in self._waiters:
                other.skipped += 1
            if not waiter.future.done():
                waiter.future.set_result(Lease(self, waiter.tenant, waiter.kind))

    def _pick_index(self) -> int:
        promoted = [
            index
            for index, waiter in enumerate(self._waiters)
            if waiter.kind == "background" and waiter.skipped >= self.starvation_rounds
        ]
        if promoted:
            return promoted[0]
        interactive = [
            index
            for index, waiter in enumerate(self._waiters)
            if waiter.kind != "background"
        ]
        candidates = interactive or list(range(len(self._waiters)))
        for index in candidates:
            if self._waiters[index].tenant != self._last_served:
                return index
        return candidates[0]

    def _check_bounds(self, tenant: str) -> None:
        if len(self._waiters) >= self.queue_max_size:
            self._rejected_total += 1
            raise SandboxQuotaError(
                "沙箱排队已满",
                detail=f"全局队列上限 {self.queue_max_size}；请稍后重试",
            )
        self._check_tenant_limit(tenant)

    def _check_tenant_limit(self, tenant: str) -> None:
        used = self._active.get(tenant, 0)
        if used >= self.per_tenant_limit:
            self._rejected_total += 1
            raise SandboxQuotaError(
                "该用户同时运行/排队的沙箱任务过多",
                detail=f"单租户上限 {self.per_tenant_limit}；先完成或取消已有任务",
            )

    def _grant(self, tenant: str, kind: str) -> Lease:
        self._running += 1
        self._acquired_total += 1
        self._last_served = tenant
        self._active[tenant] = self._active.get(tenant, 0) + 1
        return Lease(self, tenant, _normalize_kind(kind))

    def _drop_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        count = self._active.get(waiter.tenant, 0)
        if count <= 1:
            self._active.pop(waiter.tenant, None)
        else:
            self._active[waiter.tenant] = count - 1

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq


def _normalize_kind(kind: str) -> str:
    """只区分 ``background`` 与交互类，其余按交互处理。"""
    return "background" if str(kind or "").strip().lower() == "background" else "interactive"
