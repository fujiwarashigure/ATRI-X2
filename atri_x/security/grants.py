"""授权登记表（grant registry）：把"这次 / 这个会话后续"的放行变成有生命周期的凭据。

对应本仓《dsh-qq 借鉴落地评估》§B1（授权生命周期）。

旧状态（1.0.5 之前）：Agent 上两个进程内 dict
（``auto_confirm_session`` / ``auto_confirm_tools``）记"本会话全放行"与"某工具全放行"，
问题有三个：

* **永不过期**：一次 ``/confirmall`` 之后整个进程生命周期都放行；
* **不绑定会话作用域与发言人**：Session 复用 / 共享线程群场景下，另一名成员也能吃到；
* **不可撤销**：权限配置或工具白名单改了、Session 删了，放行依旧生效。

本模块给出唯一入口：

* :class:`Grant`：``id / kind(session|tool) / tool / session_id / scope_key /
  actor_id / created_at / expires_at / max_uses / uses``；
* :class:`GrantRegistry`：线程安全的**进程内**登记表（重启不继承，符合评估要求）；
* 默认 TTL 10 分钟（:data:`DEFAULT_GRANT_TTL_SECONDS`），``0`` 表示不过期（只给测试
  与兼容调用用）；
* ``scope_key`` 形如 ``<platform>:<scope_id>``，与 ``long_memory_scope`` 的键同一口径；
* ``revoke_scope`` / ``revoke_session`` / ``revoke_actor`` / ``clear`` 供权限变更、
  Session 删除与 ``/panic`` 调用；
* 过期条目在每次查/写时顺带清理，不需要后台线程。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

#: 会话级 / 工具级放行的默认有效期（10 分钟）。``0`` = 不过期。
DEFAULT_GRANT_TTL_SECONDS = 600.0

#: 放行整条会话（``/confirmall``）。
GRANT_KIND_SESSION = "session"

#: 放行某一个工具（``/confirmtool``）。
GRANT_KIND_TOOL = "tool"

#: ``expires_at`` 取这个值表示"永不过期"（测试与显式关闭 TTL 时用）。
NO_EXPIRY = 0.0

_GRANT_ID_PREFIX = "g"


@dataclass
class Grant:
    """一条放行凭据。对应评估 §B1 的 ``Grant``。"""

    id: str = ""
    kind: str = GRANT_KIND_SESSION
    tool: str = ""
    session_id: str = ""
    scope_key: str = ""
    actor_id: str = ""
    created_at: float = 0.0
    expires_at: float = NO_EXPIRY
    max_uses: int = 0
    uses: int = 0
    #: 最近一次被使用的墙钟时间（RFC3339），只用于展示与审计。
    last_used_at: str = ""
    labels: list[str] = field(default_factory=list)

    def expired(self, now: float) -> bool:
        """是否已过期（``expires_at == NO_EXPIRY`` 表示永不过期）。"""
        return self.expires_at > NO_EXPIRY and now >= self.expires_at

    def exhausted(self) -> bool:
        """次数是否已用完（``max_uses == 0`` 表示不限次数）。"""
        return self.max_uses > 0 and self.uses >= self.max_uses

    def remaining_seconds(self, now: float) -> float:
        """剩余有效秒数；永不过期返回 ``-1``。"""
        if self.expires_at <= NO_EXPIRY:
            return -1.0
        return max(0.0, self.expires_at - now)

    def matches(
        self,
        kind: str,
        tool: str = "",
        session_id: str = "",
        scope_key: str = "",
        actor_id: str = "",
    ) -> bool:
        """这条凭据是否覆盖一次调用。

        绑定的字段（非空）必须与请求一致；请求侧留空表示"不限定"（兼容老的
        两参调用 ``is_session_auto_confirmed(agent, session_id)``，真实工具路径
        一律带 ctx，因此 scope / actor 总是参与比对）。
        """
        if self.kind != kind:
            return False
        if kind == GRANT_KIND_TOOL and self.tool != tool:
            return False
        for bound, requested in (
            (self.session_id, session_id),
            (self.scope_key, scope_key),
            (self.actor_id, actor_id),
        ):
            if bound and requested and bound != requested:
                return False
        return True

    def to_payload(self) -> dict[str, object]:
        """紧凑 JSON 形状（``/grant list`` 与审计共用）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "tool": self.tool,
            "session_id": self.session_id,
            "scope_key": self.scope_key,
            "actor_id": self.actor_id,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "uses": self.uses,
        }


class GrantRegistry:
    """线程安全的进程内放行登记表。对应评估 §B1 的 ``GrantRegistry``。"""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
        clock: object = None,
    ) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, Grant] = {}
        self.ttl_seconds = float(ttl_seconds or 0.0)
        self._clock = clock if callable(clock) else time.monotonic
        self._counter = 0

    # -- 时间 / 基础 ---------------------------------------------------------
    def now(self) -> float:
        """单调时钟（与 ``expires_at`` 同一刻度）。"""
        return float(self._clock())

    def _new_id(self) -> str:
        self._counter += 1
        return f"{_GRANT_ID_PREFIX}{uuid.uuid4().hex[:8]}{self._counter:02d}"

    def _sweep_locked(self, now: float) -> int:
        expired = [
            grant_id
            for grant_id, grant in self._grants.items()
            if grant.expired(now) or grant.exhausted()
        ]
        for grant_id in expired:
            self._grants.pop(grant_id, None)
        return len(expired)

    def sweep(self) -> int:
        """清理过期 / 用完的凭据，返回清理条数。"""
        with self._lock:
            return self._sweep_locked(self.now())

    # -- 发放 ---------------------------------------------------------------
    def grant(
        self,
        kind: str,
        tool: str = "",
        session_id: str = "",
        scope_key: str = "",
        actor_id: str = "",
        ttl_seconds: float | None = None,
        max_uses: int = 0,
    ) -> Grant:
        """登记一条放行凭据。``ttl_seconds=None`` 用登记表的默认 TTL。"""
        ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        with self._lock:
            now = self.now()
            self._sweep_locked(now)
            grant = Grant(
                id=self._new_id(),
                kind=kind,
                tool=tool,
                session_id=session_id,
                scope_key=scope_key,
                actor_id=actor_id,
                created_at=now,
                expires_at=(now + ttl) if ttl > 0 else NO_EXPIRY,
                max_uses=max(0, int(max_uses or 0)),
            )
            self._grants[grant.id] = grant
            return grant

    # -- 查询 / 消费 ---------------------------------------------------------
    def find(
        self,
        kind: str,
        tool: str = "",
        session_id: str = "",
        scope_key: str = "",
        actor_id: str = "",
    ) -> Grant | None:
        """找一条覆盖该调用的凭据（不消耗次数）。"""
        with self._lock:
            now = self.now()
            self._sweep_locked(now)
            for grant in self._grants.values():
                if grant.matches(kind, tool, session_id, scope_key, actor_id):
                    return grant
            return None

    def consume(
        self,
        kind: str,
        tool: str = "",
        session_id: str = "",
        scope_key: str = "",
        actor_id: str = "",
    ) -> Grant | None:
        """找一条凭据并记一次使用；用完即撤销。"""
        with self._lock:
            now = self.now()
            self._sweep_locked(now)
            for grant in self._grants.values():
                if not grant.matches(kind, tool, session_id, scope_key, actor_id):
                    continue
                grant.uses += 1
                grant.last_used_at = _wall_clock_text()
                if grant.exhausted():
                    self._grants.pop(grant.id, None)
                return grant
            return None

    # -- 撤销 ---------------------------------------------------------------
    def revoke(self, grant_id: str) -> Grant | None:
        """按 id 撤销；也接受唯一前缀（``/grant revoke g1a2b``）。"""
        grant_id = (grant_id or "").strip()
        if grant_id == "":
            return None
        with self._lock:
            self._sweep_locked(self.now())
            grant = self._grants.get(grant_id)
            if grant is not None:
                return self._grants.pop(grant_id)
            matches = [
                item for key, item in self._grants.items() if key.startswith(grant_id)
            ]
            if len(matches) != 1:
                return None
            return self._grants.pop(matches[0].id)

    def revoke_scope(self, scope_key: str) -> int:
        """撤销某个会话作用域下的全部凭据（权限配置变更后用）。"""
        scope_key = (scope_key or "").strip()
        if scope_key == "":
            return 0
        with self._lock:
            self._sweep_locked(self.now())
            targets = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.scope_key == scope_key
            ]
            for grant_id in targets:
                self._grants.pop(grant_id, None)
            return len(targets)

    def revoke_session(self, session_id: str) -> int:
        """撤销某个 Session 的全部凭据（Session 删除 / 归档后用）。"""
        session_id = (session_id or "").strip()
        if session_id == "":
            return 0
        with self._lock:
            self._sweep_locked(self.now())
            targets = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.session_id == session_id
            ]
            for grant_id in targets:
                self._grants.pop(grant_id, None)
            return len(targets)

    def revoke_actor(self, actor_id: str) -> int:
        """撤销某个主体的全部凭据（权限被收回后用）。"""
        actor_id = (actor_id or "").strip()
        if actor_id == "":
            return 0
        with self._lock:
            self._sweep_locked(self.now())
            targets = [
                grant_id
                for grant_id, grant in self._grants.items()
                if grant.actor_id == actor_id
            ]
            for grant_id in targets:
                self._grants.pop(grant_id, None)
            return len(targets)

    def clear(self) -> int:
        """撤销全部凭据（``/panic``）。"""
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
            return count

    def active(self) -> list[Grant]:
        """当前仍然有效的凭据快照（按创建顺序）。"""
        with self._lock:
            self._sweep_locked(self.now())
            return list(self._grants.values())

    def count(self) -> int:
        """当前有效凭据条数。"""
        return len(self.active())


def _wall_clock_text() -> str:
    """本地墙钟时间（RFC3339 秒精度），只用于展示。"""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = [name for name in dir() if not name.startswith("_")]
