"""``/rollback``：列出并撤销 ATRI-X 自己对文件的最新一次编辑。

对应 Go：``internal/agent/commands/rollback.go``（83 行）。

只对超管开放：``Info.MinRole`` 留空，路由把空值当超管。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result, new_func
from atri_x.errors import RollbackConflictError, RollbackNotFoundError
from atri_x.utils.fileops import RollbackInfo
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = ["FileModule", "format_rollback_list", "new_rollback"]


def format_rollback_list(records: list[RollbackInfo]) -> str:
    """渲染可回滚列表。对应 Go ``formatRollbackList``。"""
    if not records:
        return "当前会话没有可回滚的文件编辑。"
    out = ["可回滚的文件编辑："]
    for record in records:
        kind = "编辑"
        if record.created:
            kind = "新建"
        edited_at = ""
        if record.edited_at is not None:
            edited_at = record.edited_at.strftime("%Y-%m-%d %H:%M:%S")
        out.append(
            f"\n  {record.id}. {record.path}（{kind}，{edited_at}，{record.bytes} 字节）"
        )
    out.append(
        "\n使用 /rollback <编号> 回滚其中一个文件；切换 Session、重启或容量淘汰后记录失效。"
    )
    return "".join(out)


def new_rollback(deps: Deps) -> Any:
    """对应 Go ``NewRollback``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        if deps.file_backups is None:
            return Result(content="文件回滚未启用。")
        if deps.sessions is None or deps.scope is None:
            return Result(content="当前没有可用会话。")
        current = deps.sessions.current(deps.scope(ctx))
        arg = (req.args or "").strip()
        if arg == "":
            return Result(content=format_rollback_list(deps.file_backups.list(current.id)))
        try:
            record_id = int(arg)
        except ValueError:
            record_id = 0
        if record_id <= 0:
            return Result(
                content="用法：/rollback [编号]；编号来自不带参数的 /rollback 列表。"
            )
        info, ok = deps.file_backups.get(current.id, record_id)
        if not ok:
            return Result(
                content=(
                    f"没有编号 {record_id} 的回滚记录，可能已被使用、被更新或已淘汰。"
                )
            )
        try:
            restored = deps.file_backups.restore(ctx, current.id, record_id)
        except RollbackConflictError:
            return Result(
                content=f"文件在编辑之后被 Shell 或外部程序修改，未回滚：{info.path}"
            )
        except RollbackNotFoundError:
            return Result(
                content=(
                    f"没有编号 {record_id} 的回滚记录，可能已被使用、被更新或已淘汰。"
                )
            )
        if restored.created:
            return Result(
                content=f"已回滚：删除 {restored.path}（这次编辑创建的文件）。"
            )
        return Result(
            content=(
                f"已回滚 {restored.path}：恢复编辑前内容"
                f"（编辑前 revision {restored.revision_before}）。"
            )
        )

    return new_func(
        Info(
            name="rollback",
            usage="/rollback [编号]",
            description=(
                "List the files ATRI-X edited in the current session and undo one of "
                "them. Without an argument it only lists records; /rollback <number> "
                "restores that file. Only superadmins can use it."
            ),
        ),
        handle,
    )


class FileModule:
    """对应 Go ``FileModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_rollback)
