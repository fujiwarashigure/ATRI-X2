"""会话工作区目录的持久化适配（``tool.WorkspaceStore`` 的实现）。

对应 Go：``internal/agent/workspace.go``。

工作区目录与"已提示过"标记都放在**会话 metadata** 里（``workspace_dir`` /
``workspace_agent_notice_dirs``），因此每个会话自带工作目录，不需要额外表。

两处与 Go 的差异（都是 Python 存储层的既定形状）：

* Go 的方法是同步的，Python 的 :class:`atri_x.tool.WorkspaceStore` 协议是 ``async``；
* 写入统一走 agent-tools 的 ``mutate_session_metadata``（``Sessions().Mutate`` 事务），
  从过期快照做读-改-写会静默丢掉并发写者刚写进去的字段。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SessionWorkspaceStore",
    "new_session_workspace_store",
]


def _metadata_module() -> Any:
    from atri_x.agent import session_metadata

    return session_metadata


def _mutate(agent: Any, ctx: Any, session: Any, mutate: Any) -> Any:
    """取事务写入入口（agent-tools 的 ``mutate_session_metadata``）。"""
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.tool_cache")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(module, "mutate_session_metadata", None)
    if not callable(fn):
        return None
    return fn(agent, ctx, session, mutate)


class SessionWorkspaceStore:
    """某个 Session 的工作区目录存取。对应 Go ``sessionWorkspaceStore``。"""

    def __init__(self, agent: Any, session: Any) -> None:
        self.agent = agent
        self.session = session

    # -- WorkspaceStore -----------------------------------------------------
    async def get_workspace_dir(self, ctx: Any) -> str:
        """对应 Go ``GetWorkspaceDir``（总是读库里的最新行）。"""
        if self._unavailable():
            return ""
        latest = self.agent.store.sessions().get(self.session.id)
        if hasattr(latest, "__await__"):
            latest = await latest
        metadata = _metadata_module().decode_session_metadata(
            getattr(latest, "metadata", "") or ""
        )
        return (metadata.workspace_dir or "").strip()

    async def set_workspace_dir(self, ctx: Any, dir: str) -> None:
        """对应 Go ``SetWorkspaceDir``。"""
        await self.set_workspace_dir_with_agent_notice(ctx, dir, False)

    async def clear_workspace_dir(self, ctx: Any) -> None:
        """对应 Go ``ClearWorkspaceDir``。"""
        await self.clear_workspace_dir_with_agent_notice(ctx, False)

    # -- WorkspaceAgentNoticeStore ------------------------------------------
    async def has_workspace_agent_notice_dir(self, ctx: Any, dir: str) -> bool:
        """对应 Go ``HasWorkspaceAgentNoticeDir``（顺带刷新本地会话快照）。"""
        dir = (dir or "").strip()
        if self._unavailable() or not dir:
            return False
        latest = self.agent.store.sessions().get(self.session.id)
        if hasattr(latest, "__await__"):
            latest = await latest
        metadata = _metadata_module().decode_session_metadata(
            getattr(latest, "metadata", "") or ""
        )
        self.session.metadata = getattr(latest, "metadata", "") or ""
        return dir in list(metadata.workspace_agent_notice_dirs or [])

    async def mark_workspace_agent_notice_dir(self, ctx: Any, dir: str) -> None:
        """对应 Go ``MarkWorkspaceAgentNoticeDir``。"""
        dir = (dir or "").strip()
        if not dir:
            return

        def edit(metadata: Any) -> bool:
            if dir in metadata.workspace_agent_notice_dirs:
                return False
            metadata.workspace_agent_notice_dirs.append(dir)
            return True

        await self._mutate_metadata(ctx, edit)

    async def set_workspace_dir_with_agent_notice(
        self, ctx: Any, dir: str, mark_notice: bool
    ) -> None:
        """对应 Go ``SetWorkspaceDirWithAgentNotice``。"""
        dir = (dir or "").strip()
        if not dir:
            return
        await self._save(ctx, dir, dir, mark_notice)

    async def clear_workspace_dir_with_agent_notice(
        self, ctx: Any, mark_notice: bool
    ) -> None:
        """对应 Go ``ClearWorkspaceDirWithAgentNotice``。"""
        await self._save(ctx, "", "", mark_notice)

    # -- 内部 ---------------------------------------------------------------
    def _unavailable(self) -> bool:
        return (
            self.agent is None
            or getattr(self.agent, "store", None) is None
            or self.session is None
            or not getattr(self.session, "id", "")
        )

    async def _save(
        self, ctx: Any, workspace_dir: str, notice_dir: str, mark_notice: bool
    ) -> None:
        def edit(metadata: Any) -> bool:
            changed = metadata.workspace_dir != workspace_dir
            metadata.workspace_dir = workspace_dir
            dirs = list(metadata.workspace_agent_notice_dirs or [])
            if mark_notice and notice_dir and notice_dir not in dirs:
                dirs.append(notice_dir)
                metadata.workspace_agent_notice_dirs = dirs
                changed = True
            return changed

        await self._mutate_metadata(ctx, edit)

    async def _mutate_metadata(self, ctx: Any, mutate: Any) -> None:
        """在一个事务里改 metadata（对应 Go ``mutateMetadata``）。"""
        if self._unavailable():
            return
        result = _mutate(self.agent, ctx, self.session, mutate)
        if result is None:
            return
        if hasattr(result, "__await__"):
            await result


def new_session_workspace_store(agent: Any, session: Any) -> SessionWorkspaceStore:
    """构造会话工作区存储。对应 Go 的 ``sessionWorkspaceStore{...}`` 字面量。"""
    return SessionWorkspaceStore(agent, session)
