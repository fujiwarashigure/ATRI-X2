"""``rollback_file``：撤销 ATRI-X 最近一次 ``edit_file`` 对某个文件的修改。

对应 Go：``internal/tool/builtin/file_rollback.go``。

与 Go 的差异（仅形式）：``RollbackStore.restore`` 在本仓是同步方法（内部用
``threading.Lock``），因此这里直接调用而不是 ``await``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import (
    InvalidError,
    RollbackConflictError,
    RollbackNotFoundError,
)
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder
from atri_x.tool.builtin.common import cancel_token_from_ctx, first_file_guard
from atri_x.tool.builtin.file_guard import FileGuard
from atri_x.tool.sandbox import session_id_from_context
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh
from atri_x.tool.workspace import PathResolveOptions, resolve_workspace_path
from atri_x.utils.fileops import RollbackInfo, RollbackStore

# RollbackFileName 是撤销 ATRI-X 自己最近一次 edit_file 的内置工具名。
ROLLBACK_FILE_NAME = "rollback_file"

_ROLLBACK_ARG_KEYS = frozenset({"id", "path"})


@dataclass
class RollbackFileArgs:
    """对应 Go ``rollbackFileArgs``。"""

    id: int = 0
    path: str = ""


class RollbackFileTool:
    """对应 Go ``RollbackFileTool``。"""

    def __init__(
        self, backups: RollbackStore | None = None, file_guard: FileGuard | None = None
    ) -> None:
        self.backups = backups
        self.file_guard = file_guard

    def name(self) -> str:
        return ROLLBACK_FILE_NAME

    def info(self) -> Info:
        return rollback_file_builder().build_info()

    def schema(self) -> ToolSchema:
        return rollback_file_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``RollbackFileTool.Call``。"""
        args = parse_rollback_file_args(req.arguments)
        if self.backups is None:
            return Result(content="文件回滚未启用。")
        session_id = session_id_from_context(ctx)
        if session_id == "":
            return Result(content="无法确定当前会话，未回滚。")
        records = self.backups.list(session_id)
        record_id = args.id
        if record_id == 0 and args.path.strip() != "":
            resolved = await resolve_workspace_path(ctx, args.path, PathResolveOptions())
            for info in records:
                if info.path == resolved.path:
                    record_id = info.id
                    break
            if record_id == 0:
                return Result(content=f"没有该文件的可回滚记录：{resolved.path}")
        if record_id == 0:
            return Result(content=format_rollback_records(records))
        info, ok = self.backups.get(session_id, record_id)
        if not ok:
            return Result(
                content=f"没有编号 {record_id} 的回滚记录，可能已被使用、被更新或已淘汰。"
            )
        if self.file_guard is not None:
            self.file_guard.check_write(info.path)
        try:
            restored = self.backups.restore(
                cancel_token_from_ctx(ctx), session_id, record_id
            )
        except RollbackConflictError:
            return Result(
                content=f"文件在编辑之后被 Shell 或外部程序修改，未回滚：{info.path}"
            )
        except RollbackNotFoundError:
            return Result(
                content=f"没有编号 {record_id} 的回滚记录，可能已被使用、被更新或已淘汰。"
            )
        if restored.created:
            return Result(
                content=f"已回滚 {restored.path}：删除这次编辑创建的文件（记录 {restored.id}）。"
            )
        return Result(
            content=(
                f"已回滚 {restored.path}：恢复编辑前内容（记录 {restored.id}，"
                f"编辑前 revision {restored.revision_before}）。"
            )
        )


def rollback_file_builder() -> Builder:
    """对应 Go ``rollbackFileBuilder``。"""
    return (
        Builder(ROLLBACK_FILE_NAME)
        .description(
            "撤销 ATRI-X 最近一次 edit_file 对某个文件的修改，恢复到编辑前的内容。"
            "只在用户明确要求回滚时调用；用 id（来自当前会话的回滚记录）或 path 指定文件；"
            "不传参数时只列出可回滚记录。不覆盖 Shell 或外部程序在编辑之后做的修改。"
        )
        .superadmin_only()
        .risk(RiskHigh)
        .depends_on("read_file", "edit_file")
        .tags("files", "agent")
        .integer("id", "回滚记录编号；与 path 二选一。")
        .string("path", "要回滚的文件路径，基于当前 workspace 解析；也可传绝对路径。")
    )


def parse_rollback_file_args(raw: str) -> RollbackFileArgs:
    """对应 Go ``RollbackFileTool.Call`` 里的 ``DisallowUnknownFields`` 解码。"""
    if not raw:
        return RollbackFileArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse rollback_file arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse rollback_file arguments: expected a JSON object")
    for key in data:
        if key not in _ROLLBACK_ARG_KEYS:
            raise InvalidError(
                f'parse rollback_file arguments: json: unknown field "{key}"'
            )
    raw_id = data.get("id")
    try:
        record_id = 0 if raw_id is None else int(raw_id)
    except (TypeError, ValueError) as exc:
        raise InvalidError("id must be an integer") from exc
    path = data.get("path")
    return RollbackFileArgs(id=record_id, path="" if path is None else str(path))


def new_rollback_file_tool(
    backups: RollbackStore | None, *file_guard: FileGuard
) -> RollbackFileTool:
    """对应 Go ``NewRollbackFileTool``。"""
    return RollbackFileTool(backups, first_file_guard(list(file_guard)))


def format_rollback_records(records: list[RollbackInfo]) -> str:
    """渲染可回滚记录列表；工具与 ``/rollback`` 共用。对应 Go ``formatRollbackRecords``。"""
    if not records:
        return "当前会话没有可回滚的文件编辑。"
    out = ["可回滚的文件编辑："]
    for record in records:
        kind = "新建" if record.created else "编辑"
        edited_at = record.edited_at.strftime("%Y-%m-%d %H:%M:%S")
        out.append(
            f"\n  {record.id}. {record.path}（{kind}，{edited_at}，{record.bytes} 字节）"
        )
    return "".join(out)


__all__ = [name for name in dir() if not name.startswith("_")]
