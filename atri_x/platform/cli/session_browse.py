"""远端 CLI 的会话浏览扩展（Python 版新增）。

从 :mod:`atri_x.platform.cli.remote_server` 拆出来的原因：Go 上游的 ``remoteServer``
本来就没有这两帧，相关代码全是我们加的；把它放成 mixin 可以让 ``remote_server.py``
继续贴着 Go 的结构与行数，也让「浏览帧」的出入站编码集中在一个文件里。

协议：客户端 ``sessions`` / ``history`` → 服务端 ``sessions_result`` /
``history_result``（见 :mod:`atri_x.platform.cli.protocol` 里的常量说明）。服务端只
转发给注入的 ``session_browser``（鸭子类型，见 :class:`atri_x.session.browse.SessionBrowser`），
没有注入时返回空结果，不影响原协议。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from atri_x.platform.cli.protocol import (
    HistoryEntry,
    RemoteMessage,
    SessionInfo,
    format_rfc3339_nano,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查用，避免循环导入
    from atri_x.platform.cli.remote_server import RemoteClientConn

__all__ = ["SessionBrowseMixin"]


class SessionBrowseMixin:
    """给 :class:`RemoteServer` 加上 ``sessions`` / ``history`` 帧的处理。

    宿主成员：``self.session_browser``（可空）、``self.logger``（可空）。
    """

    #: 由 ``RemoteServer.__init__`` 从 ``RemoteServerOptions.session_browser`` 赋值。
    session_browser: Any = None
    logger: Any = None

    def set_session_browser(self, browser: Any) -> None:
        """装上会话浏览实现（``None`` 表示不支持浏览帧）。

        与 ``set_completer`` 同一个装配时机（``app/integrations.py`` 的
        ``register_session_browsers``），只是数据来源是会话服务而不是补全服务。
        """
        self.session_browser = browser

    def _browse_sessions(
        self, client: "RemoteClientConn", msg: RemoteMessage
    ) -> list[SessionInfo]:
        """``sessions`` 帧 → 会话列表（没有浏览器时返回空）。"""
        browser = self.session_browser
        if browser is None:
            return []
        try:
            scope = browser.scope(client.id)
            views = browser.list(scope, msg.limit, msg.text)
        except Exception as exc:  # noqa: BLE001 - 浏览失败不能让连接断掉
            if self.logger is not None:
                self.logger.debug("cli session browse failed: %s", exc)
            return []
        return [_session_info(view) for view in views or []]

    def _browse_history(
        self, client: "RemoteClientConn", msg: RemoteMessage
    ) -> list[HistoryEntry]:
        """``history`` 帧 → 某会话历史（没有浏览器或无权访问时返回空）。"""
        browser = self.session_browser
        if browser is None:
            return []
        try:
            scope = browser.scope(client.id)
            views = browser.history(scope, msg.session_id, msg.limit)
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.debug("cli session history failed: %s", exc)
            return []
        return [_history_entry(view) for view in views or []]


def _session_info(view: Any) -> SessionInfo:
    """会话浏览视图 → 线上 ``SessionInfo``（Python 版新增）。"""
    return SessionInfo(
        id=str(getattr(view, "id", "") or ""),
        title=str(getattr(view, "title", "") or ""),
        mode=str(getattr(view, "mode", "") or ""),
        status=str(getattr(view, "status", "") or ""),
        platform=str(getattr(view, "platform", "") or ""),
        updated_at=format_rfc3339_nano(getattr(view, "updated_at", None)),
        created_at=format_rfc3339_nano(getattr(view, "created_at", None)),
        current=bool(getattr(view, "current", False)),
    )


def _history_entry(view: Any) -> HistoryEntry:
    """历史消息视图 → 线上 ``HistoryEntry``（Python 版新增）。"""
    return HistoryEntry(
        role=str(getattr(view, "role", "") or ""),
        text=str(getattr(view, "text", "") or ""),
        at=format_rfc3339_nano(getattr(view, "at", None)),
    )
