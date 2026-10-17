"""闲置过期策略：按「群聊/私聊」×「普通用户/超管」选 TTL。对应 Go：``internal/session/expiration.go``。

约定（``devdocs/architecture.md`` locator:session）：

* TTL 为 0 表示**禁用**该组合的闲置过期（默认配置里私聊超管就是 0）。
* 过期只清除**内存中的 current**，不删持久行；下一条普通消息才会创建并持久化新
  Session（``/new`` 同理，见 :meth:`atri_x.session.service.Service.reset_current`）。
* 空闲判定必须在事务内基于**最新行**重做：读取与写入之间可能有别的写入者刷新了
  活跃时间，此时要放弃过期并抛 :data:`atri_x.errors.ErrSessionNotIdle`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from atri_x.errors import ErrSessionNotIdle, NotFoundError, SessionNotIdleError
from atri_x.session.types import Scope
from atri_x.storage.times import is_zero_time, now

__all__ = [
    "ExpirationMixin",
    "ExpireIdleRequest",
    "ExpireIdleResult",
    "IdleExpirationConfig",
    "is_group_scope",
    "scope_ttl_minutes",
]


@dataclass
class IdleExpirationConfig:
    """闲置过期配置（四种组合的 TTL，单位分钟）。对应 Go ``session.IdleExpirationConfig``。"""

    group_user_ttl_minutes: int = 0
    group_superadmin_ttl_minutes: int = 0
    private_user_ttl_minutes: int = 0
    private_superadmin_ttl_minutes: int = 0

    def ttl_minutes(self, scope: Scope, is_superadmin: bool) -> int:
        """按作用域与角色取 TTL 分钟数。对应 Go ``IdleExpirationConfig.TTLMinutes``。"""
        return scope_ttl_minutes(self, scope, is_superadmin)


@dataclass
class ExpireIdleRequest:
    """闲置过期请求。对应 Go ``session.ExpireIdleRequest``。"""

    scope: Scope | None = None
    is_superadmin: bool = False
    config: IdleExpirationConfig | None = None
    now: datetime | None = None


@dataclass
class ExpireIdleResult:
    """闲置过期结果。对应 Go ``session.ExpireIdleResult``。"""

    expired: bool = False
    session_id: str = ""
    ttl_minutes: int = 0


class ExpirationMixin:
    """``Service`` 的闲置过期部分（Go ``expiration.go`` 的方法）。"""

    def expire_idle_current(self, req: ExpireIdleRequest) -> ExpireIdleResult:
        """按配置过期闲置的当前会话。对应 Go ``ExpireIdleCurrent``。"""
        scope = req.scope if req.scope is not None else Scope()
        config = req.config if req.config is not None else IdleExpirationConfig()
        ttl_minutes = config.ttl_minutes(scope, req.is_superadmin)
        if ttl_minutes <= 0:
            return ExpireIdleResult()
        try:
            session = self.current(scope)
        except NotFoundError:
            return ExpireIdleResult()
        moment = req.now if not is_zero_time(req.now) else now()
        cutoff = moment - timedelta(minutes=ttl_minutes)
        if not _is_before(session.updated_at, cutoff):
            return ExpireIdleResult()

        def update(current) -> None:
            # 事务内重新判定：读取与这次写入之间可能有别的写入者刷新了活跃时间。
            if not _is_before(current.updated_at, cutoff):
                raise ErrSessionNotIdle
            current.updated_at = moment

        try:
            self.store.sessions().mutate(session.id, update)
        except SessionNotIdleError:
            return ExpireIdleResult()
        except NotFoundError:
            return ExpireIdleResult()
        self.clear_current_if(scope, session.id)
        return ExpireIdleResult(
            expired=True, session_id=session.id, ttl_minutes=ttl_minutes
        )


def scope_ttl_minutes(
    config: IdleExpirationConfig, scope: Scope, is_superadmin: bool
) -> int:
    """四种组合的 TTL 选择。对应 Go ``IdleExpirationConfig.TTLMinutes``。"""
    if is_group_scope(scope.platform_scope_id):
        if is_superadmin:
            return config.group_superadmin_ttl_minutes
        return config.group_user_ttl_minutes
    if is_superadmin:
        return config.private_superadmin_ttl_minutes
    return config.private_user_ttl_minutes


def is_group_scope(scope_id: str) -> bool:
    """scope ID 是否是群聊。对应 Go ``isGroupScope``。"""
    scope_id = (scope_id or "").strip()
    return scope_id.startswith("group:") or scope_id.startswith("supergroup:")


def _is_before(moment: datetime | None, cutoff: datetime) -> bool:
    """``moment`` 是否早于 ``cutoff``；``None`` 是 Go 的零值时间，早于一切。"""
    if is_zero_time(moment):
        return True
    return moment < cutoff
