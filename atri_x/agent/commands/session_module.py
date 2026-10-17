"""``SessionCommandState`` 与 ``SessionModule``。

对应 Go：``internal/agent/commands/session_module.go``（95 行）。

Go 用 ``sync.RWMutex`` 保护 ``ids`` / ``listPageSize`` / ``retentionDays``；
命令执行在 asyncio 事件循环里，且没有 ``await`` 落在临界区内，所以按
PORTING.md §1.6 用 ``threading.Lock``（同步临界区，最短持锁）而不是
``asyncio.Lock``（后者会把整段读取变成协程，与 Go 的同步访问语义不符）。

``SessionCommandState`` 只在 ``/sessions`` / ``/archives`` 之后被 ``/rename``、
``/archive`` 这类"按编号操作"的命令读取，保存的是"上次列表看到的顺序"。
"""

from __future__ import annotations

import threading
from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.session_core import (
    new_chat,
    new_new,
    new_status,
    new_work,
)
from atri_x.agent.commands.session_format import DEFAULT_SESSION_LIST_PAGE_SIZE
from atri_x.agent.commands.session_lifecycle import (
    new_archive,
    new_clean,
    new_delete,
    new_pin,
    new_rename,
    new_unarchive,
    new_unpin,
)
from atri_x.agent.commands.session_navigation import (
    new_archives,
    new_fork,
    new_messages,
    new_resume,
    new_sessions,
)

__all__ = [
    "SessionCommandState",
    "SessionModule",
    "new_session_command_state",
]


class SessionCommandState:
    """每个 scope 的"上次列出的 Session id"与页大小/保留天数配置。

    对应 Go ``SessionCommandState``。scope 用 ``(actor_id, platform,
    platform_scope_id)`` 三元组做键（Go 的 ``session.Scope`` 是可比较结构体；
    Python 的 ``Scope`` 是 dataclass，默认不可哈希，所以这里用显式键元组）。
    """

    def __init__(self, list_page_size: int = 0, retention_days: int = 0) -> None:
        self._mutex = threading.Lock()
        self._ids: dict[tuple[str, str, str], list[str]] = {}
        self._list_page_size = list_page_size
        self._retention_days = retention_days

    @staticmethod
    def _key(scope: Any) -> tuple[str, str, str]:
        return (
            getattr(scope, "actor_id", ""),
            getattr(scope, "platform", ""),
            getattr(scope, "platform_scope_id", ""),
        )

    def set(self, scope: Any, sessions: list[Any]) -> None:
        """记住本次列出的 Session id 顺序。对应 Go ``set``。"""
        ids = [session.id for session in sessions]
        with self._mutex:
            self._ids[self._key(scope)] = ids

    def get(self, scope: Any) -> list[str]:
        """取上次列出的 Session id 顺序。对应 Go ``get``。"""
        with self._mutex:
            return list(self._ids.get(self._key(scope), []))

    def set_list_page_size(self, size: int) -> None:
        """对应 Go ``SetListPageSize``（非正数忽略）。"""
        if size <= 0:
            return
        with self._mutex:
            self._list_page_size = size

    def set_retention_days(self, days: int) -> None:
        """对应 Go ``SetRetentionDays``（非正数忽略）。"""
        if days <= 0:
            return
        with self._mutex:
            self._retention_days = days

    def config(self) -> tuple[int, int]:
        """返回 ``(list_page_size, retention_days)``。对应 Go ``config``。"""
        with self._mutex:
            return self._list_page_size, self._retention_days


def new_session_command_state(
    list_page_size: int, retention_days: int
) -> SessionCommandState:
    """对应 Go ``NewSessionCommandState``。"""
    return SessionCommandState(list_page_size, retention_days)


class SessionModule:
    """对应 Go ``SessionModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        if deps.session_state is None:
            deps.session_state = SessionCommandState(
                DEFAULT_SESSION_LIST_PAGE_SIZE, 30
            )
        register_factories(
            registrar,
            deps,
            new_new,
            new_status,
            new_sessions,
            new_archives,
            new_archive,
            new_unarchive,
            new_pin,
            new_unpin,
            new_rename,
            new_delete,
            new_clean,
            new_resume,
            new_messages,
            new_fork,
            new_work,
            new_chat,
        )
