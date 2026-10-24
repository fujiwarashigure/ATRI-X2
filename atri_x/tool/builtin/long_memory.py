"""``long_memory`` / ``long_memory_search`` / ``long_memory_write`` 三个工具。

对应 Go：``internal/tool/builtin/long_memory.go`` 的工具声明与三个 ``Call``。

拆分来源：``long_memory.go``（1181 行）→ 本模块（工具）、
:mod:`atri_x.tool.builtin.long_memory_store`（索引/检索/写盘）、
:mod:`atri_x.tool.builtin.long_memory_model`（模型与文本）。

与 Go 的差异：

* 三个 ``Call`` 是 async（Go 是同步 + ``context.Context``）。
* Go 的 ``PreflightConfirmation`` / ``RiskDetail`` → 工具协议里的
  ``preflight_confirmation`` / ``risk_detail``（均为 async）。
"""

from __future__ import annotations

from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.long_memory_model import (
    LongMemorySaveArgs,
    LongMemorySearchArgs,
    LongMemoryWriteArgs,
    decode_long_memory_args,
    update_args_from_write,
)
from atri_x.tool.builtin.long_memory_scope import scope_label
from atri_x.tool.builtin.long_memory_store import LongMemoryStore
from atri_x.tool.tool import CallRequest, Info, Result, RiskMedium, RiskHigh
from atri_x.utils.fileops import edit_operation_properties

LONG_MEMORY_TOOL_NAME = "long_memory"
LONG_MEMORY_SEARCH_TOOL_NAME = "long_memory_search"
LONG_MEMORY_WRITE_TOOL_NAME = "long_memory_write"

_ENTRY_DESCRIPTION = (
    "Long Memory 长期记忆入口。用于检索、保存、更新、删除长期记忆"
    "（默认按会话隔离，超级管理员可显式跨会话检索）"
)
_SEARCH_DESCRIPTION = (
    "检索长期记忆；不传 keywords/category 时返回分类目录。"
    "默认只检索当前会话与旧数据；传 scope=all（仅超管）可跨会话检索。"
    "默认只返回标题和摘要，需要详细内容时传 brief_only=false。"
)
_WRITE_DESCRIPTION = (
    "保存、局部更新或永久删除长期记忆。operation 为 save、update 或 delete。"
    "新记忆自动归属当前会话；更新/删除默认只能操作当前会话的记录。"
    "保存前建议先用 long_memory_search 查看分类和避免重复。"
)
_ENTRY_REPLY = (
    "long_memory 是长期记忆入口。请按需调用 long_memory_search 或 long_memory_write。"
    "long_memory_search 不传 keywords/category 时会返回分类目录。"
    "记忆默认按会话隔离，只有超级管理员能跨会话检索或操作其他会话的记忆。"
)


def long_memory_builder(name: str, description: str) -> Builder:
    """对应 Go ``longMemoryBuilder``。"""
    return (
        Builder(name)
        .description(description)
        .risk(RiskMedium)
        .tags("agent")
        .superadmin_only()
    )


def hidden_long_memory_info(name: str, description: str, risk: str) -> Info:
    """对应 Go ``hiddenLongMemoryInfo``。"""
    return (
        long_memory_builder(name, description)
        .risk(risk)
        .hidden()
        .superadmin_only()
        .build_info()
    )


def long_memory_search_builder() -> Builder:
    """``long_memory_search`` 的参数表。对应 Go ``LongMemorySearchTool.Schema``。"""
    return (
        long_memory_builder(LONG_MEMORY_SEARCH_TOOL_NAME, _SEARCH_DESCRIPTION)
        .string("keywords", "关键词。留空且 category 也为空时返回当前分类目录。")
        .string("category", "指定分类。留空且 keywords 也为空时返回当前分类目录。")
        .string(
            "match_mode",
            "多关键词匹配模式：AND=必须全包含；OR=包含任一即可。默认 OR。",
        )
        .boolean(
            "brief_only",
            "true=仅返回标题和摘要；false=包含完整内容。默认 true。需要详细内容时再传 false。",
        )
        .integer("limit", "最大结果数，默认 3，上限 10。")
        .string(
            "scope",
            "作用域：留空=当前会话；all=全部会话（仅超管）；"
            "也可传某个具体来源作用域键（仅超管，用于定向查看别的会话）。",
        )
    )


def long_memory_write_builder() -> Builder:
    """``long_memory_write`` 的参数表。对应 Go ``LongMemoryWriteTool.Schema``。"""
    return (
        long_memory_builder(LONG_MEMORY_WRITE_TOOL_NAME, _WRITE_DESCRIPTION)
        .string("operation", "写操作：save、update、delete。", required())
        .integer("id", "update/delete 需要的记忆 ID。")
        .string(
            "category",
            "save 需要；update 时不填表示保持不变，填写表示修改分类。"
            "优先复用已有分类，避免创建语义重叠的新分类。",
        )
        .string(
            "title",
            "save 需要；update 时不填表示保持不变，填写表示修改标题。",
        )
        .string(
            "summary",
            "save 需要；update 时不填表示保持不变，填写表示修改摘要，建议 50 字以内。",
        )
        .string(
            "keywords",
            "save 需要；update 时不填表示保持不变，填写表示修改搜索关键词；"
            "用空格、逗号或换行分隔。",
        )
        .string(
            "content",
            "save 需要；update 时不填表示保持不变，填写表示整段替换正文；delete 会忽略。"
            "update 时不能和 content_edits 同时填写。",
        )
        .object_array(
            "content_edits",
            "仅 update 有效；不能和 content 同时填写。"
            "使用与 edit_file 相同的精确文本、anchor、行号插入/删除协议。",
            edit_operation_properties(),
            ["operation"],
        )
        .string(
            "scope",
            "跨会话 update/delete 时必填：目标记录在检索结果里显示的来源作用域键"
            "（仅超管）。本会话操作留空。",
        )
    )


class LongMemoryTool:
    """入口工具：只解释怎么用，不落盘。对应 Go ``LongMemoryTool``。"""

    def __init__(self, store: LongMemoryStore | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return LONG_MEMORY_TOOL_NAME

    def info(self) -> Info:
        return (
            long_memory_builder(self.name(), _ENTRY_DESCRIPTION)
            .depends_on(LONG_MEMORY_SEARCH_TOOL_NAME, LONG_MEMORY_WRITE_TOOL_NAME)
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            long_memory_builder(self.name(), self.info().description)
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        return Result(content=_ENTRY_REPLY)


class LongMemorySearchTool:
    """检索工具。对应 Go ``LongMemorySearchTool``。"""

    def __init__(self, store: LongMemoryStore | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return LONG_MEMORY_SEARCH_TOOL_NAME

    def info(self) -> Info:
        return hidden_long_memory_info(self.name(), _SEARCH_DESCRIPTION, RiskMedium)

    def schema(self) -> ToolSchema:
        return long_memory_search_builder().description(self.info().description).build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_long_memory_args(
            req.arguments, LongMemorySearchArgs, self.name()
        )
        store = self._store()
        if (args.keywords or "").strip() == "" and (args.category or "").strip() == "":
            return Result(content=store.categories(ctx, args.scope))
        return Result(content=store.search(ctx, args))

    def _store(self) -> LongMemoryStore:
        if self.store is None:
            raise InvalidError("long memory directory is not configured")
        return self.store


class LongMemoryWriteTool:
    """写入工具（save / update / delete）。对应 Go ``LongMemoryWriteTool``。"""

    def __init__(self, store: LongMemoryStore | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return LONG_MEMORY_WRITE_TOOL_NAME

    def info(self) -> Info:
        return hidden_long_memory_info(self.name(), _WRITE_DESCRIPTION, RiskHigh)

    def schema(self) -> ToolSchema:
        return long_memory_write_builder().description(self.info().description).build_schema()

    def _decode(self, req: CallRequest) -> LongMemoryWriteArgs:
        return decode_long_memory_args(req.arguments, LongMemoryWriteArgs, self.name())

    def _store(self) -> LongMemoryStore:
        if self.store is None:
            raise InvalidError("long memory directory is not configured")
        return self.store

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """对应 Go ``LongMemoryWriteTool.PreflightConfirmation``。"""
        args = self._decode(req)
        if args.operation.strip().lower() != "update":
            return
        self._store().preview_update(ctx, update_args_from_write(args))

    async def risk_detail(self, ctx: Any, req: CallRequest) -> str:
        """对应 Go ``LongMemoryWriteTool.RiskDetail``。"""
        args = self._decode(req)
        if args.operation.strip().lower() != "update":
            return ""
        preview = self._store().preview_update(ctx, update_args_from_write(args))
        parts = [
            "操作：更新长期记忆\n",
            f"ID：{preview.after.id}\n",
            f"文件：{preview.after.file_path}\n",
            f"分类：{preview.after.category}\n",
            f"标题：{preview.after.title}\n",
            "模式：确认后写入；确认前已自动预检\n",
        ]
        if len(args.content_edits) > 0:
            parts.append(
                "正文编辑：content_edits 只作用于正文；所有目标均基于更新前正文解析\n"
            )
        if preview.changes:
            parts.append("字段变化：\n")
            for change in preview.changes:
                parts.append(f"- {change}\n")
        parts.append("预检 diff:\n")
        parts.append(preview.diff)
        if not preview.diff.endswith("\n"):
            parts.append("\n")
        return "".join(parts).rstrip("\n")

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = self._decode(req)
        store = self._store()
        operation = args.operation.strip().lower()
        if operation == "save":
            record = store.save(
                ctx,
                LongMemorySaveArgs(
                    category=args.category,
                    title=args.title,
                    summary=args.summary,
                    keywords=args.keywords,
                    content=args.content,
                ),
            )
            return Result(
                content=(
                    f"记忆写入成功！\nID：{record.id}\n分类：{record.category}\n"
                    f"标题：{record.title}\n文件：{record.file_path}\n"
                    f"归属会话：{scope_label(record.source_scope)}\n"
                    "可通过 long_memory_search 检索，或用 long_memory_write 更新/删除。"
                )
            )
        if operation == "update":
            record = store.update(ctx, update_args_from_write(args))
            return Result(
                content=(
                    f"ID：{record.id} 更新成功！\n分类：{record.category}\n"
                    f"标题：{record.title}\n文件：{record.file_path}"
                )
            )
        if operation == "delete":
            path = store.delete(ctx, args.id, args.scope)
            return Result(content=f"记忆ID：{args.id}已永久删除。\n文件：{path}")
        raise InvalidError("operation must be save, update, or delete")


def new_long_memory_tools(root_dir: str) -> list[Any]:
    """对应 Go ``NewLongMemoryTools``：三个工具共享同一个 store。"""
    store = LongMemoryStore(root_dir)
    return [
        LongMemoryTool(store),
        LongMemorySearchTool(store),
        LongMemoryWriteTool(store),
    ]


def new_long_memory_store(root_dir: str) -> LongMemoryStore:
    """对应 Go ``newLongMemoryStore``（导出以便测试与 Lead 装配）。"""
    return LongMemoryStore(root_dir)


__all__ = [name for name in dir() if not name.startswith("_")]
