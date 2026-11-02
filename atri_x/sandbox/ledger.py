"""用量账本：流量额度、执行计数与失败分类。

修改清单 §7 的硬要求：**配额跨容器重建累计，重置沙箱不能清零**。所以账本独立于
租户环境目录（放在 ``<sandbox root>/usage-ledger.json``），``reset`` / ``destroy``
只删工作区与私有 home，不碰这里。

落盘用「临时文件 + ``os.replace``」保证读到的永远是完整 JSON；进程内用
``threading.Lock`` 串行化（GUI 线程会读快照，asyncio 线程会写）。

口径：

* ``bytes_day`` / ``bytes_total``：上下行合计的代理流量（字节）；
* ``execs`` / ``failures``：执行次数与按 ``kind`` 分类的失败次数；
* ``denials``：被策略/配额拒绝的次数（滥用记录，重置不清）；
* ``last_used`` / ``last_reset``：RFC3339 时间，供保留期清理与 GUI 显示。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["UsageLedger", "UsageRecord"]

_LEDGER_VERSION = 1


@dataclass
class UsageRecord:
    """一个租户的累计用量。"""

    bytes_day: int = 0
    bytes_total: int = 0
    execs: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    denials: int = 0
    last_used: str = ""
    last_reset: str = ""

    def dump(self) -> dict:
        """GUI/审计用字典。"""
        return {
            "bytes_day": self.bytes_day,
            "bytes_total": self.bytes_total,
            "execs": self.execs,
            "failures": dict(self.failures),
            "denials": self.denials,
            "last_used": self.last_used,
            "last_reset": self.last_reset,
        }


class UsageLedger:
    """租户用量账本（JSON 持久化）。"""

    def __init__(self, path: str, *, clock: Any = None) -> None:
        self.path = os.path.abspath(os.path.normpath(path))
        self._lock = threading.Lock()
        self._clock = clock or time.time
        self._day = ""
        self._tenants: dict[str, UsageRecord] = {}
        self._load()

    # -- 读取 ---------------------------------------------------------------
    def record(self, tenant_slug: str) -> UsageRecord:
        """取某租户的累计用量（不存在时返回零值副本）。"""
        with self._lock:
            self._roll_day_locked()
            item = self._tenants.get(tenant_slug)
            if item is None:
                return UsageRecord()
            return UsageRecord(
                bytes_day=item.bytes_day,
                bytes_total=item.bytes_total,
                execs=item.execs,
                failures=dict(item.failures),
                denials=item.denials,
                last_used=item.last_used,
                last_reset=item.last_reset,
            )

    def daily_bytes(self, tenant_slug: str) -> int:
        """今日已用流量（字节）。"""
        return self.record(tenant_slug).bytes_day

    def check_quota(self, tenant_slug: str, limit_bytes: int) -> None:
        """今日流量是否还有余量；超限抛 :class:`atri_x.sandbox.errors.SandboxQuotaError`。"""
        if limit_bytes <= 0:
            return
        used = self.daily_bytes(tenant_slug)
        if used >= limit_bytes:
            from atri_x.sandbox.errors import SandboxQuotaError

            raise SandboxQuotaError(
                "沙箱今日联网流量额度已用尽",
                detail=f"已用 {used / 1048576:.1f} MiB / 额度 {limit_bytes / 1048576:.0f} MiB；"
                "额度按自然日累计，重置沙箱不会清零",
            )

    def remaining_bytes(self, tenant_slug: str, limit_bytes: int) -> int:
        """今日剩余流量（``limit_bytes<=0`` 表示不限，返回 0）。"""
        if limit_bytes <= 0:
            return 0
        return max(0, int(limit_bytes) - self.daily_bytes(tenant_slug))

    # -- 写入 ---------------------------------------------------------------
    def add_traffic(self, tenant_slug: str, up_bytes: int, down_bytes: int) -> None:
        """累计一次代理流量（字节）。"""
        delta = max(0, int(up_bytes)) + max(0, int(down_bytes))
        if delta == 0:
            return
        with self._lock:
            self._roll_day_locked()
            item = self._ensure_locked(tenant_slug)
            item.bytes_day += delta
            item.bytes_total += delta
            item.last_used = self._now_text()
            self._save_locked()

    def record_exec(
        self,
        tenant_slug: str,
        *,
        failure_kind: str = "",
        denied: bool = False,
    ) -> None:
        """记一次执行（失败时按类别累计，拒绝单独计滥用）。"""
        with self._lock:
            self._roll_day_locked()
            item = self._ensure_locked(tenant_slug)
            item.execs += 1
            if failure_kind:
                item.failures[failure_kind] = int(item.failures.get(failure_kind, 0)) + 1
            if denied:
                item.denials += 1
            item.last_used = self._now_text()
            self._save_locked()

    def mark_reset(self, tenant_slug: str) -> None:
        """记录一次重置（**不清零**用量，只记时间）。"""
        with self._lock:
            self._roll_day_locked()
            item = self._ensure_locked(tenant_slug)
            item.last_reset = self._now_text()
            self._save_locked()

    def snapshot(self) -> dict:
        """全部租户的用量快照（GUI 用；键是 slug）。"""
        with self._lock:
            self._roll_day_locked()
            return {
                "day": self._day,
                "tenants": {key: item.dump() for key, item in self._tenants.items()},
            }

    def prune(self, keep_slugs: set[str]) -> int:
        """清掉已不存在的租户记录（``destroy`` 才调用；配额账本本身不主动清）。"""
        with self._lock:
            removed = [key for key in self._tenants if key not in keep_slugs]
            for key in removed:
                del self._tenants[key]
            if removed:
                self._save_locked()
            return len(removed)

    # -- 内部 ---------------------------------------------------------------
    def _ensure_locked(self, tenant_slug: str) -> UsageRecord:
        item = self._tenants.get(tenant_slug)
        if item is None:
            item = UsageRecord()
            self._tenants[tenant_slug] = item
        return item

    def _roll_day_locked(self) -> None:
        day = self._day_text()
        if day == self._day:
            return
        # 换日：只把「今日」清零，累计与滥用记录保留。
        self._day = day
        for item in self._tenants.values():
            item.bytes_day = 0

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            self._day = self._day_text()
            return
        if not isinstance(data, dict) or int(data.get("version", 0) or 0) != _LEDGER_VERSION:
            self._day = self._day_text()
            return
        self._day = str(data.get("day", "") or self._day_text())
        tenants = data.get("tenants")
        if isinstance(tenants, dict):
            for key, raw in tenants.items():
                if not isinstance(raw, dict):
                    continue
                failures = raw.get("failures")
                self._tenants[str(key)] = UsageRecord(
                    bytes_day=int(raw.get("bytes_day", 0) or 0),
                    bytes_total=int(raw.get("bytes_total", 0) or 0),
                    execs=int(raw.get("execs", 0) or 0),
                    failures={
                        str(name): int(count or 0)
                        for name, count in (failures or {}).items()
                    }
                    if isinstance(failures, dict)
                    else {},
                    denials=int(raw.get("denials", 0) or 0),
                    last_used=str(raw.get("last_used", "") or ""),
                    last_reset=str(raw.get("last_reset", "") or ""),
                )

    def _save_locked(self) -> None:
        payload = {
            "version": _LEDGER_VERSION,
            "day": self._day,
            "updated_at": self._now_text(),
            "tenants": {key: item.dump() for key, item in self._tenants.items()},
        }
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def _day_text(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def _now_text(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
