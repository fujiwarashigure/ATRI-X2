"""``workspace`` 工具：设置当前 Session 的共享工作目录。

对应 Go：``internal/tool/builtin/workspace.go``。

与 Go 的差异（仅形式）：Go 的 ``(T, error)`` → 抛异常；``WorkspaceStore`` /
``WorkspaceAgentNoticeStore`` 在本仓是异步协议，因此 ``call`` / ``discovery_content``
都是 ``async def``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow
from atri_x.tool.workspace import (
    WorkspaceAgentNoticeStore,
    WorkspaceStore,
    current_workspace_dir,
    validate_workspace_dir,
    workspace_store_from_context,
)

MAX_WORKSPACE_AGENT_FILE_SIZE = 64 * 1024
_AGENT_FILE_BASES = ("AGENTS", "AGENT")


@dataclass
class WorkspaceArgs:
    """对应 Go ``workspaceArgs``。"""

    path: str = ""
    reset: bool = False


class WorkspaceTool:
    """对应 Go ``WorkspaceTool``。"""

    def name(self) -> str:
        return "workspace"

    def info(self) -> Info:
        return workspace_builder().build_info()

    def schema(self) -> ToolSchema:
        return workspace_builder().build_schema()

    async def discovery_content(self, ctx: Any) -> tuple[str, bool]:
        """对应 Go ``WorkspaceTool.DiscoveryContent``。"""
        store, ok = workspace_store_from_context(ctx)
        if not ok or store is None:
            return "", False
        if not isinstance(store, WorkspaceAgentNoticeStore):
            return "", False
        directory = await current_workspace_dir(ctx)
        instructions, mark_notice = await workspace_agent_instructions(
            ctx, store, directory
        )
        if mark_notice:
            try:
                await store.mark_workspace_agent_notice_dir(ctx, directory)
            except BaseException as exc:  # noqa: BLE001 - 统一转成可回喂的错误
                raise InvalidError(
                    f"mark workspace instructions notice: {exc}"
                ) from exc
        return instructions, False

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``WorkspaceTool.Call``。"""
        args = parse_workspace_args(req.arguments)
        sandbox = _sandbox_service()
        if sandbox is not None:
            return await self._call_in_sandbox(ctx, args, sandbox)
        store, has_store = workspace_store_from_context(ctx)
        if args.reset:
            directory = default_workspace_dir()
            content = f"workspace reset.\ncurrent workspace: {directory}"
            if has_store and store is not None:
                if isinstance(store, WorkspaceAgentNoticeStore):
                    instructions, mark_notice = await workspace_agent_instructions(
                        ctx, store, directory
                    )
                    if instructions != "":
                        content += "\n\n" + instructions
                    try:
                        # 契约差异：atri_x/tool/workspace.py 的
                        # WorkspaceAgentNoticeStore.clear_workspace_dir_with_agent_notice
                        # 只收 (ctx, mark_notice)，目录由 store 自己取默认值
                        # （Go 是 ClearWorkspaceDirWithAgentNotice(ctx, dir, markNotice)）。
                        await store.clear_workspace_dir_with_agent_notice(
                            ctx, mark_notice
                        )
                    except BaseException as exc:  # noqa: BLE001
                        raise InvalidError(f"reset workspace: {exc}") from exc
                    return Result(content=content)
                try:
                    await store.clear_workspace_dir(ctx)
                except BaseException as exc:  # noqa: BLE001
                    raise InvalidError(f"reset workspace: {exc}") from exc
            return Result(content=content)

        path = (args.path or "").strip()
        if path != "":
            if not has_store or store is None:
                raise InvalidError("workspace is unavailable in this context")
            directory = validate_workspace_dir(path)
            content = f"workspace set.\ncurrent workspace: {directory}"
            if isinstance(store, WorkspaceAgentNoticeStore):
                instructions, mark_notice = await workspace_agent_instructions(
                    ctx, store, directory
                )
                if instructions != "":
                    content += "\n\n" + instructions
                try:
                    await store.set_workspace_dir_with_agent_notice(
                        ctx, directory, mark_notice
                    )
                except BaseException as exc:  # noqa: BLE001
                    raise InvalidError(f"set workspace: {exc}") from exc
                return Result(content=content)
            try:
                await store.set_workspace_dir(ctx, directory)
            except BaseException as exc:  # noqa: BLE001
                raise InvalidError(f"set workspace: {exc}") from exc
            return Result(content=content)

        directory = await current_workspace_dir(ctx)
        return Result(content=f"current workspace: {directory}")


    async def _call_in_sandbox(self, ctx: Any, args: WorkspaceArgs, sandbox: Any) -> Result:
        """沙箱模式：工作区由用户安全域托管，不允许切到宿主目录。

        查询返回容器内路径 ``/workspace`` 与一句说明；``reset`` 引导用
        ``/sandbox reset``（分三档，其中清空文件需要用户确认）。设置任意宿主路径
        属于「宿主文件访问」能力，默认不开放。
        """
        workspace = await current_workspace_dir(ctx)
        content = (
            "current workspace: /workspace\n"
            f"（沙箱模式：工作区由当前用户安全域托管，宿主路径 {workspace} 不直接暴露；"
            "需要重建环境请用 `/sandbox reset`）"
        )
        if args.reset:
            result = await sandbox.reset(ctx, mode="rebuild")
            content = "workspace reset（保留文件重建）。\n" + str(result.get("message", ""))
        elif (args.path or "").strip():
            raise InvalidError(
                "沙箱模式不允许把工作区切到宿主目录；"
                "工作区固定为当前用户安全域的 /workspace"
            )
        return Result(content=content)


def _sandbox_service() -> Any:
    """当前生效的沙箱服务（未启用 / 显式 off 时返回 ``None``）。"""
    try:
        from atri_x.sandbox.runtime import service_or_none

        return service_or_none()
    except Exception:  # noqa: BLE001 - 沙箱未装配时维持宿主行为
        return None


def new_workspace_tool() -> WorkspaceTool:
    """对应 Go ``NewWorkspaceTool``。"""
    return WorkspaceTool()


def workspace_builder() -> Builder:
    """对应 Go ``workspaceBuilder``。"""
    return (
        Builder("workspace")
        .description(
            "设置当前 Session 的共享工作目录。所有需要路径的工具会基于该目录解析相对路径。"
        )
        .risk(RiskLow)
        .superadmin_only()
        .tags("agent", "files")
        .foreground_only()
        .string(
            "path",
            "要设置为 workspace 的已有目录路径。支持~或者$HOME开头；"
            "相对路径按当前工作目录解析（\".\" 即当前目录）。",
        )
        .boolean("reset", "为 true 时重置为默认工作目录。")
    )


def parse_workspace_args(raw: str) -> WorkspaceArgs:
    """对应 Go ``WorkspaceTool.Call`` 里的 json.Unmarshal。"""
    if not raw:
        return WorkspaceArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse workspace arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse workspace arguments: expected a JSON object")
    path = data.get("path")
    return WorkspaceArgs(
        path="" if path is None else str(path), reset=bool(data.get("reset"))
    )


def default_workspace_dir() -> str:
    """对应 Go ``defaultWorkspaceDir``。"""
    try:
        directory = os.getcwd()
    except OSError as exc:
        raise InvalidError(f"resolve workspace: {exc}") from exc
    return os.path.normpath(directory)


async def workspace_agent_instructions(
    ctx: Any, notice_store: WorkspaceAgentNoticeStore, directory: str
) -> tuple[str, bool]:
    """对应 Go ``workspaceAgentInstructions``。

    返回 ``(指令文本, 是否需要标记已提示)``。
    """
    directory = os.path.normpath((directory or "").strip())
    try:
        loaded = await notice_store.has_workspace_agent_notice_dir(ctx, directory)
    except BaseException as exc:  # noqa: BLE001
        raise InvalidError(f"load workspace instructions notice: {exc}") from exc
    if loaded:
        return "", False
    path, name, ok = find_workspace_agent_file(directory)
    if not ok:
        return "", False
    try:
        size = os.stat(path).st_size
    except OSError as exc:
        raise InvalidError(f"stat workspace instructions {name}: {exc}") from exc
    if size > MAX_WORKSPACE_AGENT_FILE_SIZE:
        return (
            "workspace instructions file is larger than 64 KiB and was not loaded: "
            f"{name}\nPlease tell the user to shorten or split it before switching "
            "workspace again.",
            False,
        )
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise InvalidError(f"read workspace instructions {name}: {exc}") from exc
    text = data.decode("utf-8", "replace").rstrip("\r\n")
    return (f"Loaded workspace instructions from {name}:\n{text}", True)


def find_workspace_agent_file(directory: str) -> tuple[str, str, bool]:
    """对应 Go ``findWorkspaceAgentFile``：找 ``AGENTS.md`` / ``AGENT.md``。"""
    try:
        entries = sorted(os.listdir(directory))
    except OSError as exc:
        raise InvalidError(f"read workspace directory: {exc}") from exc
    for base in _AGENT_FILE_BASES:
        for name in entries:
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            stem, ext = os.path.splitext(name)
            if stem != base or ext.lower() != ".md":
                continue
            return full, name, True
    return "", "", False


__all__ = [name for name in dir() if not name.startswith("_")]
