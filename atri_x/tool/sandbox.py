"""工具执行的轻量沙盒运行态、后台上下文与相对路径解析。

对应 Go：``internal/tool/sandbox.go``。

``SandboxContext`` 只随 ctx 传播，不持久化。ctx 是鸭子类型 ``Mapping``。
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atri_x.errors import InvalidError
from atri_x.tool.capability import ctx_value, ctx_with_value, vision_available

if TYPE_CHECKING:  # 只为类型标注，避免与 tool.py 形成循环 import
    from atri_x.tool.tool import Info

_SANDBOX_KEY = object()
_SESSION_ID_KEY = object()


class BackgroundKind:
    """后台任务的种类。对应 Go ``BackgroundKind``。"""

    CRON = "cron"
    ATRINIS = "atrinis"

    ALL = (CRON, ATRINIS)


BackgroundKindCron = BackgroundKind.CRON
BackgroundKindAtrinis = BackgroundKind.ATRINIS


@dataclass
class SandboxContext:
    """本次工具执行的沙盒运行态。对应 Go ``SandboxContext``。"""

    root: str = ""
    dir: str = ""
    background: bool = False
    background_kind: str = ""


def with_sandbox_context(ctx: Any, sandbox: SandboxContext) -> Any:
    """对应 Go ``WithSandboxContext``。返回新的 ctx。"""
    return ctx_with_value(ctx, _SANDBOX_KEY, sandbox)


def sandbox_context_from_context(ctx: Any) -> tuple[SandboxContext, bool]:
    """对应 Go ``SandboxContextFromContext``。"""
    sandbox = ctx_value(ctx, _SANDBOX_KEY)
    if isinstance(sandbox, SandboxContext):
        return sandbox, True
    return SandboxContext(), False


def background_context(ctx: Any) -> bool:
    """对应 Go ``BackgroundContext``。"""
    sandbox, ok = sandbox_context_from_context(ctx)
    if not ok:
        return False
    return bool(sandbox.background or (sandbox.background_kind or "").strip())


def with_session_id(ctx: Any, session_id: str) -> Any:
    """把当前工具调用的所属 Session 写进 ctx。对应 Go ``WithSessionID``。

    保留进程内状态的工具（文件备份）用它限定记录范围。空 session_id 原样返回 ctx。
    """
    session_id = (session_id or "").strip()
    if session_id == "":
        return ctx
    return ctx_with_value(ctx, _SESSION_ID_KEY, session_id)


def session_id_from_context(ctx: Any) -> str:
    """对应 Go ``SessionIDFromContext``。"""
    value = ctx_value(ctx, _SESSION_ID_KEY)
    return value if isinstance(value, str) else ""


def info_available_in_context(ctx: Any, info: "Info") -> bool:
    """对应 Go ``InfoAvailableInContext``。"""
    if info.foreground_only and background_context(ctx):
        return False
    if info.vision_required and not vision_available(ctx):
        return False
    return True


def background_path_instruction() -> str:
    """对应 Go ``BackgroundPathInstruction``。"""
    return "所有路径参数必须使用相对路径，基于当前任务工作目录解析；不要使用绝对路径、~、.. 或 cd。"


def resolve_sandbox_relative_path(sandbox: SandboxContext, raw_path: str) -> str:
    """对应 Go ``ResolveSandboxRelativePath``：把后台任务里的相对路径钉进沙盒。

    不合法时抛 :class:`atri_x.errors.InvalidError`。
    """
    raw_path = (raw_path or "").strip()
    if raw_path == "":
        raise InvalidError("path is required")
    if raw_path.startswith("~"):
        raise InvalidError("background path must be relative and must not use ~")
    slash_path = raw_path.replace("\\", "/")
    if (
        slash_path.startswith("/")
        or os.path.isabs(raw_path)
        or _is_windows_abs_path(slash_path)
    ):
        raise InvalidError("background path must be relative")
    clean_slash = posixpath.normpath(slash_path)
    if clean_slash == ".." or clean_slash.startswith("../"):
        raise InvalidError("background path must not escape task workspace")
    root = (sandbox.dir or "").strip() or (sandbox.root or "").strip()
    if root == "":
        raise InvalidError("background sandbox is not configured")
    root_abs = os.path.abspath(os.path.normpath(root))
    candidate_abs = os.path.abspath(
        os.path.join(root_abs, *clean_slash.split("/"))
    )
    try:
        rel = os.path.relpath(candidate_abs, root_abs)
    except ValueError as exc:
        raise InvalidError(f"check background path: {exc}") from exc
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise InvalidError("background path must not escape task workspace")
    return candidate_abs


def _is_windows_abs_path(value: str) -> bool:
    """对应 Go ``isWindowsAbsPath``：``X:/...``。"""
    return (
        len(value) >= 3
        and _is_ascii_alpha(value[0])
        and value[1] == ":"
        and value[2] == "/"
    )


def _is_ascii_alpha(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


__all__ = [name for name in dir() if not name.startswith("_")]
