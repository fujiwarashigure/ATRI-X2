"""内置 DSH 内核状态目录（``DSH_HOME``）的保留清理（Python 版新增件）。

Go 上游没有 DSH 深度路由（见 ``devdocs/dsh-acp.md``），因此这份清理逻辑是
**Python 版独有**的：ATRI-X 把复杂任务交给内置内核时，每轮都新建一个一次性会话
（:mod:`atri_x.agent.dsh.runtime`），结束时只发 ``session/close`` —— 那是结算与
落盘，**不删历史**。DSH 把会话历史写在 ``DSH_HOME/sessions/<cwd 摘要>/<session-id>/``，
用得越久这个目录越大；而 ``atri_x.maintenance`` 原有的保留期只覆盖 ATRI-X 自己的
SQLite / 日志 / 沙盒，管不到内核这一侧。

安全边界（删错的代价很高，所以用**白名单**而不是黑名单）：

* 只碰 ``sessions/`` 与 ``storages/session_projcache/sessions/``；
* ``profiles/``（ACP profile 与插件依赖）、``storages/`` 的其它子目录、顶层文件
  （如 ``.anonymous-user-id``）一律保留；
* 逐项按「目录里最新一个文件的 mtime」判定过期（空目录用自己的 mtime），符号链接跳过；
* 目标必须仍在 ``home`` 之内（防止 ``..`` 或链接把删除引到目录外）。

实测布局（内置内核 ``0.2.0-rc.2`` / Node ``v24.19.0``，ACP ``initialize`` 起一次）：:

    DSH_HOME/
      profiles/acp/{cordis.yml,cordis.patch.yml,package.json,pnpm-workspace.yaml}
      sessions/--<cwd-slug>--/<session-id>/session.v4.jsonl.zstd
      storages/session_projcache/sessions/
      .anonymous-user-id
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "DSH_CACHE_DIRS",
    "DSH_SESSIONS_DIR",
    "DshCleanupResult",
    "RunDshCleanupMixin",
    "cleanup_dsh",
    "resolve_dsh_home",
]

#: 会话语历史根（相对 ``DSH_HOME``）。
DSH_SESSIONS_DIR = "sessions"
#: 会话工程缓存目录（相对 ``DSH_HOME``）：DSH 自己的 session 缓存。
DSH_CACHE_DIRS = ("storages", "session_projcache", "sessions")


@dataclass
class DshCleanupResult:
    """一次 DSH 清理的结果；``sessions`` / ``cache_entries`` 是删除的条目数。"""

    sessions: int = 0
    cache_entries: int = 0
    files: int = 0
    bytes: int = 0


def resolve_dsh_home(cfg: Any) -> str:
    """从配置解出 ``DSH_HOME``（与 :func:`atri_x.agent.dsh.runtime.resolve_paths` 同口径）。

    ``[agent.dsh].home`` 为空串时落在「配置目录/dsh/home」；相对路径按配置目录展开。
    拿不到配置目录（例如只构造了默认 :class:`AppConfig`）时返回空串 —— 调用方
    据此跳过清理，而不是去猜一个路径。
    """
    config_path = str(getattr(cfg, "config_path", "") or "").strip()
    base = os.path.dirname(os.path.abspath(config_path)) if config_path else ""
    agent = getattr(cfg, "agent", None)
    dsh = getattr(agent, "dsh", None)
    home = str(getattr(dsh, "home", "") or "").strip()
    if not home:
        return os.path.join(base, "dsh", "home") if base else ""
    home = os.path.expanduser(home)
    if not os.path.isabs(home):
        if not base:
            return ""
        home = os.path.join(base, home)
    return os.path.abspath(home)


async def cleanup_dsh(ctx: Any, home: str, cutoff: datetime) -> DshCleanupResult:
    """删除 ``home`` 下早于 ``cutoff`` 的一次性会话历史与会话缓存。

    ``home`` 不存在时返回空结果（不报错，与 :func:`cleanup_sandbox` 一致）。
    与同组清理函数一样是 ``async``：全部是同步文件操作，只为让调用方（cron
    handler）不必区分「有的要 await 有的不要」。
    """
    result = DshCleanupResult()
    root = (home or "").strip()
    if not root or not os.path.isdir(root):
        return result
    root = os.path.abspath(root)
    cutoff_ts = cutoff.timestamp()

    sessions_root = os.path.join(root, DSH_SESSIONS_DIR)
    if os.path.isdir(sessions_root):
        for cwd_entry in _entries(sessions_root):
            if not _is_real_dir(cwd_entry, root):
                continue
            for session_entry in _entries(cwd_entry.path):
                if not _is_real_dir(session_entry, root):
                    continue
                if _newest_mtime(session_entry.path) >= cutoff_ts:
                    continue
                _raise_if_cancelled(ctx)
                result.sessions += 1
                _delete_tree(ctx, session_entry.path, result)
            _remove_if_empty(cwd_entry.path)

    cache_root = os.path.join(root, *DSH_CACHE_DIRS)
    if os.path.isdir(cache_root):
        for entry in _entries(cache_root):
            if not _inside(root, entry.path):
                continue
            if _newest_mtime(entry.path) >= cutoff_ts:
                continue
            _raise_if_cancelled(ctx)
            result.cache_entries += 1
            _delete_tree(ctx, entry.path, result)
    return result


def _entries(path: str) -> list[Any]:
    """``os.scandir`` 的容错版：目录读不动就当空（清理是尽力而为）。"""
    try:
        with os.scandir(path) as iterator:
            return list(iterator)
    except OSError:
        return []


def _is_real_dir(entry: Any, root: str) -> bool:
    """是真目录、不是符号链接、且仍在 ``root`` 内。"""
    try:
        if not entry.is_dir(follow_symlinks=False):
            return False
    except OSError:
        return False
    return _inside(root, entry.path)


def _inside(root: str, path: str) -> bool:
    """``path`` 解析符号链接后是否仍在 ``root`` 内。"""
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _newest_mtime(path: str) -> float:
    """目录里最新一个**文件**的 mtime；空目录退回自己的 mtime。

    不把目录自身的 mtime 混进来：新建/删除文件会把父目录 mtime 刷到「现在」，
    用它判定会让一份很久没写过的会话因为「刚刚被扫到」而永远不过期。
    读不动按「很新」处理（不删）。
    """
    try:
        newest = os.stat(path).st_mtime
    except OSError:
        return float("inf")
    if not os.path.isdir(path):
        return newest
    newest = float("-inf")
    for current, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                newest = max(newest, os.stat(os.path.join(current, name)).st_mtime)
            except OSError:
                return float("inf")
    if newest == float("-inf"):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return float("inf")
    return newest


def _delete_tree(ctx: Any, path: str, result: DshCleanupResult) -> None:
    """删文件或整棵目录，并累计 ``files`` / ``bytes``。"""
    if os.path.isdir(path) and not os.path.islink(path):
        for current, _dirnames, filenames in os.walk(path):
            _raise_if_cancelled(ctx)
            for name in filenames:
                target = os.path.join(current, name)
                try:
                    result.bytes += os.stat(target).st_size
                except OSError:
                    pass
                result.files += 1
        shutil.rmtree(path)
        return
    try:
        result.bytes += os.stat(path).st_size
    except OSError:
        pass
    result.files += 1
    os.remove(path)


def _remove_if_empty(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def _raise_if_cancelled(ctx: Any) -> None:
    checker = getattr(ctx, "raise_if_cancelled", None)
    if callable(checker):
        checker()


class RunDshCleanupMixin:
    """``Service`` 的 DSH 内核清理部分（Python 版新增件，对应 Go 里挂在同一个
    ``Service`` 上的那些方法；拆到这里是为了压住单文件行数）。"""

    async def run_dsh_cleanup(self, ctx: Any) -> None:
        """清理内置 DSH 内核的过期会话历史。

        ``[maintenance.dsh_cleanup].enabled`` 为假或解析不到 ``DSH_HOME`` 时直接返回。
        只动 ``sessions/`` 与 ``storages/session_projcache/sessions/`` 下过期条目，
        ``profiles/`` 等运行配置一律保留（见本模块上半部分的说明）。
        """
        if not self.dsh_cleanup.enabled:
            return None
        home = self.dsh_home
        if not home:
            self.warn("maintenance dsh cleanup skipped: DSH_HOME 未解析")
            return None
        retention = self.dsh_cleanup.retention_days or 1
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        try:
            result = await cleanup_dsh(ctx, home, cutoff)
        except Exception as exc:  # noqa: BLE001 - 记录后向上抛（同其它清理任务的 err 语义）
            self.warn("maintenance dsh cleanup failed", "error", exc, "home", home)
            raise
        self.info(
            "maintenance dsh cleanup completed",
            "home",
            home,
            "sessions",
            result.sessions,
            "cache_entries",
            result.cache_entries,
            "files",
            result.files,
            "bytes",
            result.bytes,
            "retention_days",
            retention,
        )
        return None
