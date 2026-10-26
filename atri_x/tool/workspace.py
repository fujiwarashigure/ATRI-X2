"""工具 workspace 上下文与路径解析。

对应 Go：``internal/tool/workspace.go``。

``WorkspaceStore`` 是持久化适配器（由 agent 层实现）；它做 I/O，因此 Python 侧
是异步协议。ctx 是鸭子类型 ``Mapping``。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from atri_x.errors import InvalidError
from atri_x.tool.capability import ctx_value, ctx_with_value
from atri_x.tool.sandbox import (
    sandbox_context_from_context,
    resolve_sandbox_relative_path,
)

_WORKSPACE_KEY = object()

ABSOLUTE_PATH_WORKSPACE_WARNING = (
    "使用了绝对路径，不会改变 workspace。若多次在该目录工作，建议先使用 workspace 工具切换。"
)


@runtime_checkable
class WorkspaceStore(Protocol):
    """workspace 目录的持久化。对应 Go ``WorkspaceStore``。"""

    async def get_workspace_dir(self, ctx: Any) -> str: ...

    async def set_workspace_dir(self, ctx: Any, dir: str) -> None: ...

    async def clear_workspace_dir(self, ctx: Any) -> None: ...


@runtime_checkable
class WorkspaceAgentNoticeStore(Protocol):
    """带"已提示过"标记的 workspace 持久化。对应 Go ``WorkspaceAgentNoticeStore``。"""

    async def has_workspace_agent_notice_dir(self, ctx: Any, dir: str) -> bool: ...

    async def mark_workspace_agent_notice_dir(self, ctx: Any, dir: str) -> None: ...

    async def set_workspace_dir_with_agent_notice(
        self, ctx: Any, dir: str, mark_notice: bool
    ) -> None: ...

    async def clear_workspace_dir_with_agent_notice(
        self, ctx: Any, mark_notice: bool
    ) -> None: ...


@dataclass
class PathResolveOptions:
    """对应 Go ``PathResolveOptions``。"""

    allow_create: bool = False
    allow_directory: bool = False


@dataclass
class ResolvedPath:
    """对应 Go ``ResolvedPath``。"""

    path: str = ""
    base_dir: str = ""
    was_abs: bool = False
    warnings: list[str] = field(default_factory=list)


def with_workspace_store(ctx: Any, store: WorkspaceStore | None) -> Any:
    """对应 Go ``WithWorkspaceStore``。store 为 None 时原样返回 ctx。"""
    if store is None:
        return ctx
    return ctx_with_value(ctx, _WORKSPACE_KEY, store)


def workspace_store_from_context(ctx: Any) -> tuple[WorkspaceStore | None, bool]:
    """对应 Go ``WorkspaceStoreFromContext``。"""
    store = ctx_value(ctx, _WORKSPACE_KEY)
    if store is None:
        return None, False
    return store, True


async def current_workspace_dir(ctx: Any) -> str:
    """对应 Go ``CurrentWorkspaceDir``。"""
    store, ok = workspace_store_from_context(ctx)
    if ok and store is not None:
        try:
            dir_value = await store.get_workspace_dir(ctx)
        except BaseException as exc:
            raise InvalidError(f"load workspace: {exc}") from exc
        if (dir_value or "").strip():
            return validate_workspace_dir(dir_value)
    return os.path.normpath(os.getcwd())


async def resolve_workspace_path(
    ctx: Any, raw_path: str, opts: PathResolveOptions | None = None
) -> ResolvedPath:
    """对应 Go ``ResolveWorkspacePath``。

    后台沙盒上下文里只接受相对路径并钉进沙盒；否则相对路径基于当前 workspace，
    绝对路径原样使用并附一条提示（不改变 workspace）。
    """
    opts = opts or PathResolveOptions()
    raw_path = (raw_path or "").strip()
    if raw_path == "":
        raise InvalidError("path is required")
    sandbox, ok = sandbox_context_from_context(ctx)
    if ok and sandbox.background:
        path = resolve_sandbox_relative_path(sandbox, raw_path)
        _validate_resolved_path(path, opts)
        base = _first_non_empty_string(sandbox.dir, sandbox.root)
        return ResolvedPath(path=os.path.normpath(path), base_dir=os.path.normpath(base))
    expanded_path = _expand_workspace_path(raw_path)
    path = _normalize_workspace_local_path(expanded_path)
    was_abs = os.path.isabs(path)
    base_dir = await current_workspace_dir(ctx)
    if not was_abs:
        path = os.path.join(base_dir, path)
    path = os.path.normpath(path)
    _validate_resolved_path(path, opts)
    resolved = ResolvedPath(path=path, base_dir=base_dir, was_abs=was_abs)
    if was_abs:
        resolved.warnings = [ABSOLUTE_PATH_WORKSPACE_WARNING]
    return resolved


def validate_workspace_dir(path: str) -> str:
    """对应 Go ``ValidateWorkspaceDir``：必须是存在的目录。

    与 Go 的差异（Python 版扩展）：**相对路径按当前工作目录解析**后再校验。
    Go 只认绝对路径，``"."`` 会被 ``filepath.Clean`` 归一成 ``"."`` 然后报
    "workspace path is required"；模型很自然会把「就用当前目录」写成 ``"."``，
    这条误导性报错会让它反复重试（1.0.8 实测）。现在相对路径先钉成绝对路径：
    空路径才报 ``workspace path is required``，存在但不是目录报
    ``workspace path is not a directory``。
    """
    raw_path = (path or "").strip()
    if raw_path == "":
        raise InvalidError("workspace path is required")
    expanded_path = _expand_workspace_path(raw_path)
    path = os.path.normpath(_normalize_workspace_local_path(expanded_path))
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(os.getcwd(), path))
    try:
        st = os.stat(path)
    except OSError as exc:
        raise InvalidError(f"resolve workspace path: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise InvalidError(f"workspace path is not a directory: {path}")
    return path


def _expand_workspace_path(path: str) -> str:
    """对应 Go ``expandWorkspacePath``：展开 ``~`` 与 ``$HOME`` 前缀。"""
    prefix = "~"
    if not _is_home_path(path, prefix):
        prefix = "$HOME"
        if not _is_home_path(path, prefix):
            return path
    home = _user_home_dir()
    if (home or "").strip() == "":
        raise InvalidError("home directory is not configured")
    if path == prefix:
        return home
    return os.path.join(home, path[len(prefix) + 1:])


def _is_home_path(value: str, prefix: str) -> bool:
    return (
        value == prefix
        or value.startswith(prefix + "/")
        or value.startswith(prefix + "\\")
    )


def _user_home_dir() -> str:
    """对应 Go ``os.UserHomeDir``：Windows 用 ``USERPROFILE``，其余用 ``HOME``。"""
    key = "USERPROFILE" if os.name == "nt" else "HOME"
    value = os.environ.get(key, "")
    if (value or "").strip():
        return value
    return os.path.expanduser("~")


def _validate_resolved_path(path: str, opts: PathResolveOptions) -> None:
    """对应 Go ``validateResolvedPath``。"""
    try:
        st = os.stat(path)
    except FileNotFoundError as exc:
        if opts.allow_create:
            return
        raise InvalidError(f"stat file: {exc}") from exc
    except OSError as exc:
        raise InvalidError(f"stat file: {exc}") from exc
    if stat.S_ISDIR(st.st_mode) and not opts.allow_directory:
        raise InvalidError("path is a directory")


def _normalize_workspace_local_path(path: str) -> str:
    """对应 Go ``normalizeWorkspaceLocalPath``（只在 Windows 改写）。"""
    if os.name != "nt":
        return path
    path = path.replace("\\", "/")
    if len(path) >= 3 and path[0] == "/" and path[2] == "/" and _is_ascii_alpha(path[1]):
        return path[1].upper() + ":" + path[2:].replace("/", "\\")
    return path.replace("/", "\\")


def _is_ascii_alpha(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _first_non_empty_string(*values: str) -> str:
    for value in values:
        if (value or "").strip():
            return value.strip()
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
