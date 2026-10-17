"""Session 服务主体：当前会话、创建、恢复、活跃时间与访问判定。对应 Go：``internal/session/service.go``（:class:`Service` 与三个构造函数）。

**拆分说明**：Go 把 ``Create`` / ``Current`` / ``Resume`` / ``Touch`` 与 mode、
lifecycle、query、fork、expiration、naming 的方法都挂在同一个 ``*Service`` 上。本仓
按 Go 文件切成多个模块，每个模块提供一个 mixin，:class:`Service` 组合它们 —— 调用
点仍是 ``service.create(...)`` / ``service.resume(...)``，与 Go 逐方法对应。

字段写入统一走 ``Sessions().Mutate``（事务内读-改-写）：``Resume`` / ``Touch`` 的
``updated_at`` 也在事务内设置，``Update`` 只留给测试夹具。

两处成员改名（都是 Go 的**未导出字段**与导出方法重名的必然结果）：

* Go ``Service.current``（内存映射）→ ``current_ids``；``Service.Current()`` →
  :meth:`Service.current`。
* Go ``Service.defaultMode`` → ``_default_mode``；``Service.DefaultMode()`` →
  :meth:`Service.default_mode`。
"""

from __future__ import annotations

import threading

from atri_x.errors import ErrNotFound, ForbiddenError, InvalidError, NotFoundError
from atri_x.session.expiration import ExpirationMixin
from atri_x.session.fork import ForkMixin
from atri_x.session.lifecycle import LifecycleMixin
from atri_x.session.mode import ModeMixin, validate_mode
from atri_x.session.naming import NamingMixin, NoopNamingNotifier
from atri_x.session.promotion import is_background, promote_to_foreground
from atri_x.session.query import QueryMixin
from atri_x.session.shared import (
    is_shared_thread_session,
    session_owner_id,
    shared_thread_metadata_for_scope,
)
from atri_x.session.text import default_title, fork_title, preview  # noqa: F401  (再导出)
from atri_x.session.types import (
    Config,
    CreateRequest,
    NamingConfig,
    Scope,
    TitleGenerator,
    NamingNotifier,
)
from atri_x.storage.models import Session, SessionModeWork, SessionStatusActive
from atri_x.storage.repositories import Store
from atri_x.storage.times import now

__all__ = [
    "Service",
    "default_title",
    "fork_title",
    "new_service",
    "new_service_with_config",
    "new_service_with_naming",
    "preview",
]


class Service(
    NamingMixin,
    ModeMixin,
    LifecycleMixin,
    QueryMixin,
    ForkMixin,
    ExpirationMixin,
):
    """会话服务。对应 Go ``session.Service``。"""

    def __init__(
        self,
        store: Store,
        cfg: Config | None = None,
        title_gen: TitleGenerator | None = None,
        notifier: NamingNotifier | None = None,
    ) -> None:
        cfg = cfg or Config()
        trigger_step = cfg.trigger_step if cfg.trigger_step > 0 else 1
        default_mode = cfg.default_mode or SessionModeWork
        try:
            validate_mode(default_mode)
        except InvalidError:
            default_mode = SessionModeWork
        self.store = store
        self.mu = threading.RLock()
        self.current_ids: dict[str, str] = {}
        self.naming_config = NamingConfig(trigger_step=trigger_step)
        self.title_gen = title_gen
        self.notifier = notifier if notifier is not None else NoopNamingNotifier()
        self.naming_states: dict[str, object] = {}
        self._default_mode = default_mode

    # -- 当前会话 -----------------------------------------------------------

    def get_or_create_current(self, scope: Scope, first_message: str) -> Session:
        """取当前会话，没有就按首条消息新建。对应 Go ``GetOrCreateCurrent``。"""
        return self.get_or_create_current_with_mode(scope, first_message, "")

    def get_or_create_current_with_mode(
        self, scope: Scope, first_message: str, mode: str
    ) -> Session:
        """按显式模式创建新会话的 ``GetOrCreateCurrent``。对应 Go ``GetOrCreateCurrentWithMode``。

        空 ``mode`` 用服务默认模式；**已存在的会话不受群级默认模式影响**。
        """
        try:
            return self.current(scope)
        except NotFoundError:
            pass
        return self.create(scope, CreateRequest(title=default_title(first_message), mode=mode))

    def create(self, scope: Scope, req: CreateRequest) -> Session:
        """创建会话并设为当前。对应 Go ``Create``。"""
        mode = req.mode or self.default_mode()
        validate_mode(mode)
        title = (req.title or "").strip() or "New session"
        session = Session(
            owner_id=session_owner_id(scope),
            platform=scope.platform,
            platform_scope_id=scope.platform_scope_id,
            mode=mode,
            status=SessionStatusActive,
            title=title,
            metadata=shared_thread_metadata_for_scope(scope, req.metadata),
        )
        self.store.sessions().create(session)
        self.set_current(scope, session.id)
        return session

    def default_mode(self) -> str:
        """服务默认模式。对应 Go ``DefaultMode``。"""
        if not self._default_mode:
            return SessionModeWork
        return self._default_mode

    def resume(self, scope: Scope, session_id: str) -> Session:
        """恢复历史会话为当前会话。对应 Go ``Resume``。

        后台 Session 在这里**原地**被前台接管：记录后台来源、清掉后台身份、切到当前
        scope（见 :func:`atri_x.session.promotion.promote_to_foreground`）。
        """
        existing = self.store.sessions().get(session_id)
        if not self.can_access(scope, existing):
            raise _scope_error(session_id)

        def update(current: Session) -> None:
            # 后台 Session 被前台激活时在这里原地接管：记录后台来源、清掉后台身份、
            # 切到当前 scope。在途的后台 turn 会在下一个安全点看到接管标记并退出。
            if is_background(current):
                promote_to_foreground(current, scope)
            current.updated_at = now()

        session = self.store.sessions().mutate(session_id, update)
        self.set_current(scope, session.id)
        return session

    def current(self, scope: Scope) -> Session:
        """取当前会话；没有记录或记录已失效时抛 :data:`atri_x.errors.ErrNotFound`。

        对应 Go ``Current``。
        """
        with self.mu:
            session_id = self.current_ids.get(self.scope_key(scope), "")
        if not session_id:
            raise ErrNotFound
        return self.store.sessions().get(session_id)

    def touch(self, session: Session | None) -> None:
        """刷新会话活跃时间（事务内写 ``updated_at``）。对应 Go ``Touch``。"""
        if session is None or not session.id:
            raise ErrNotFound

        def update(current: Session) -> None:
            current.updated_at = now()

        updated = self.store.sessions().mutate(session.id, update)
        session.updated_at = updated.updated_at

    def reset_current(self, scope: Scope) -> None:
        """清掉内存中的 current（不删持久行）。对应 Go ``ResetCurrent``。"""
        self.clear_current_if(scope, "")

    def set_current(self, scope: Scope, session_id: str) -> None:
        """记录内存中的 current。对应 Go ``setCurrent``。"""
        with self.mu:
            self.current_ids[self.scope_key(scope)] = session_id

    def clear_current_if(self, scope: Scope, session_id: str) -> None:
        """清 current：``session_id`` 为空时无条件清，否则只在匹配时清。

        对应 Go ``clearCurrentIf``。
        """
        with self.mu:
            key = self.scope_key(scope)
            if not session_id or self.current_ids.get(key) == session_id:
                self.current_ids.pop(key, None)

    def scope_key(self, scope: Scope) -> str:
        """内存 current 的键（含共享线程与平台隔离）。对应 Go ``scopeKey``。"""
        if scope.shared:
            return "\x00shared\x00" + scope.platform + "\x00" + scope.platform_scope_id
        return scope.actor_id + "\x00" + scope.platform + "\x00" + scope.platform_scope_id

    def can_access(self, scope: Scope, session: Session) -> bool:
        """该作用域能否访问该会话。对应 Go ``canAccess``。

        * CLI 作用域放行一切（跨平台可见）。
        * 共享线程作用域要求同平台同 scope 且会话带 ``thread_mode=group``。
        * 其余要求同 owner + 同平台，且 scope 相同 —— 或者该会话仍是后台 Session
          （后台 Session 允许被跨 scope 恢复，恢复时会被提升到当前 scope）。
        """
        if scope.is_cli:
            return True
        if scope.shared:
            return (
                session.platform == scope.platform
                and session.platform_scope_id == scope.platform_scope_id
                and is_shared_thread_session(session)
            )
        if session.owner_id != scope.actor_id or session.platform != scope.platform:
            return False
        return session.platform_scope_id == scope.platform_scope_id or is_background(session)


def new_service(store: Store) -> Service:
    """默认服务（命名触发步数 1、默认 work 模式）。对应 Go ``NewService``。"""
    return new_service_with_naming(store, NamingConfig(trigger_step=1), None, None)


def new_service_with_naming(
    store: Store,
    cfg: NamingConfig,
    title_gen: TitleGenerator | None = None,
    notifier: NamingNotifier | None = None,
) -> Service:
    """带命名配置的服务（默认模式固定 work）。对应 Go ``NewServiceWithNaming``。"""
    return new_service_with_config(
        store,
        Config(trigger_step=cfg.trigger_step, default_mode=SessionModeWork),
        title_gen,
        notifier,
    )


def new_service_with_config(
    store: Store,
    cfg: Config,
    title_gen: TitleGenerator | None = None,
    notifier: NamingNotifier | None = None,
) -> Service:
    """完整配置的服务。对应 Go ``NewServiceWithConfig``。"""
    return Service(store, cfg, title_gen, notifier)


def _scope_error(session_id: str) -> ForbiddenError:
    """跨作用域访问的错误。对应 Go 的 ``fmt.Errorf("session %s is not in ...")``。"""
    return ForbiddenError(f"session {session_id} is not in current platform scope")
