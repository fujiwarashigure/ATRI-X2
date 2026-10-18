"""会话浏览：给远程前端（桌面端）提供"列出会话 / 读历史"的只读查询。

为什么单独一层：``atri_x.session.Service`` 是给 Agent 用的写路径（建/恢复/改状态），
而前端需要的是**只读、可截断、按作用域鉴权**的浏览视图。这里：

- 复用 :meth:`atri_x.session.Service.list_page` 与 ``can_access``，不另写一套过滤；
- 只读 ``store.messages().list_by_session``，不碰 Agent 的上下文装载；
- 返回纯 dataclass（时间保留 ``datetime``），线上编码由
  :mod:`atri_x.platform.cli.protocol` 负责 —— 本模块不依赖任何平台层。

对应线上帧：``sessions`` / ``history``（见
:data:`atri_x.platform.cli.protocol.REMOTE_MSG_SESSIONS` 的说明，Python 版新增）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from atri_x.errors import NotFoundError
from atri_x.session.types import Scope

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SESSION_LIMIT",
    "MessageView",
    "SessionBrowser",
    "SessionView",
    "cli_scope",
]

#: 默认列多少条会话。
DEFAULT_SESSION_LIMIT = 30
#: 默认读多少条历史消息。
DEFAULT_HISTORY_LIMIT = 200
#: 单条消息最多回传多少字符（防止一条超长工具输出撑爆前端）。
MAX_MESSAGE_CHARS = 8000
#: 历史里保留的角色（工具/系统的中间消息不展示）。
_CHAT_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class SessionView:
    """会话列表里的一条（只读视图）。"""

    id: str
    title: str = ""
    mode: str = ""
    status: str = ""
    platform: str = ""
    updated_at: datetime | None = None
    created_at: datetime | None = None
    current: bool = False


@dataclass(frozen=True)
class MessageView:
    """历史里的一条消息（只读视图）。"""

    role: str
    text: str = ""
    at: datetime | None = None


def cli_scope(client_id: str) -> Scope:
    """远程 CLI 客户端的作用域。

    CLI 客户端经 token 鉴权后即视为超管作用域（``is_cli=True``）——与 ``RemoteServer``
    对同一条连接的 ``input`` 处理口径一致：能发消息就能看自己能操作的会话。
    """
    client_id = (client_id or "").strip()
    return Scope(
        actor_id=client_id,
        platform="cli",
        platform_scope_id=client_id,
        is_cli=True,
    )


class SessionBrowser:
    """``sessions`` / ``history`` 帧的查询实现（复用会话服务的鉴权与查询）。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    def scope(self, client_id: str) -> Scope:
        """远程客户端 id → 浏览作用域（服务端注入本对象后按它构造）。"""
        return cli_scope(client_id)

    # -- 列表 -----------------------------------------------------------------
    def list(
        self, scope: Scope, limit: int = DEFAULT_SESSION_LIMIT, query: str = ""
    ) -> list[SessionView]:
        """列出该作用域可见的未归档会话（按最近活跃倒序由仓储保证）。"""
        size = _clamp(limit, DEFAULT_SESSION_LIMIT, 1, 200)
        try:
            sessions = self.service.list_page(scope, (query or "").strip(), size, 0, False)
        except Exception:  # noqa: BLE001 - 查询失败按"没有会话"返回，不让前端断线
            return []
        current_id = self._current_id(scope)
        return [self._view(session, current_id) for session in sessions or []]

    def _current_id(self, scope: Scope) -> str:
        try:
            current = self.service.current(scope)
        except NotFoundError:
            return ""
        return str(getattr(current, "id", "") or "")

    @staticmethod
    def _view(session: Any, current_id: str) -> SessionView:
        session_id = str(getattr(session, "id", "") or "")
        return SessionView(
            id=session_id,
            title=str(getattr(session, "title", "") or ""),
            mode=str(getattr(session, "mode", "") or ""),
            status=str(getattr(session, "status", "") or ""),
            platform=str(getattr(session, "platform", "") or ""),
            updated_at=getattr(session, "updated_at", None),
            created_at=getattr(session, "created_at", None),
            current=bool(current_id and session_id == current_id),
        )

    # -- 历史 -----------------------------------------------------------------
    def history(
        self, scope: Scope, session_id: str, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[MessageView]:
        """读某会话最近 ``limit`` 条用户/助手消息（按时间正序返回）。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return []
        store = getattr(self.service, "store", None)
        if store is None:
            return []
        try:
            session = store.sessions().get(session_id)
        except NotFoundError:
            return []
        except Exception:  # noqa: BLE001 - 存储异常按不可见处理
            return []
        try:
            allowed = self.service.can_access(scope, session)
        except Exception:  # noqa: BLE001 - 鉴权实现异常时按拒绝处理
            allowed = False
        if not allowed:
            return []
        try:
            messages = store.messages().list_by_session(session_id)
        except Exception:  # noqa: BLE001
            return []
        views = [_message_view(message) for message in messages or []]
        views = [view for view in views if view is not None]
        size = _clamp(limit, DEFAULT_HISTORY_LIMIT, 1, 2000)
        return views[-size:]

    # -- 便捷 ---------------------------------------------------------------
    def session_ids(self, scope: Scope, limit: int = DEFAULT_SESSION_LIMIT) -> list[str]:
        """只取 id 列表（测试/诊断用）。"""
        return [view.id for view in self.list(scope, limit)]


def _message_view(message: Any) -> MessageView | None:
    """一条存储消息 → 视图；非对话角色或空文本返回 ``None``。"""
    role = str(getattr(message, "role", "") or "")
    if role not in _CHAT_ROLES:
        return None
    text = str(getattr(message, "content", "") or "")
    if not text.strip():
        # 多模态消息的 content 可能是空串，但 segments 里有正文占位；这里不做解析，
        # 直接跳过空内容，避免前端出现一堆空行。
        return None
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "\n…（已截断）"
    at = getattr(message, "created_at", None)
    return MessageView(role=role, text=text, at=at if isinstance(at, datetime) else None)


def _clamp(value: int, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(low, min(high, number))
