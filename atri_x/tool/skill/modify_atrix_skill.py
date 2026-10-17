"""``read_atrix_skill`` / ``modify_atrix_skill``：按行读、按编辑协议改原生 skill。

对应 Go：``internal/tool/skill/modify_atrix_skill.go``。

两个目标：``skill_atriyph``（``SKILL.atriyph``，改完会校验 Atriyph 并需要
``finalize_atrix_skill`` 收口）与 ``code_source``（``main.go``，只写文件、
返回 diff，不格式化/编译/reload）。编辑协议与 ``edit_file`` 完全一致
（复用 :func:`atri_x.utils.fileops.edit_file`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.atriyph import SKILL_FILE_NAME, validate_skill
from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.tool.builder import new_builder
from atri_x.tool.skill.creator import validate_skill_name
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskMedium, SourceBuiltin
from atri_x.utils.fileops import (
    Edit,
    atomic_write_file,
    content_revision,
    decode_bytes,
    edit_file,
    edit_operation_properties,
    read_file,
    split_lines,
)

__all__ = [
    "ATRIX_SKILL_TARGET_ATRIYPH",
    "ATRIX_SKILL_TARGET_CODE_SOURCE",
    "MODIFY_ATRIX_SKILL_NAME",
    "READ_ATRIX_SKILL_NAME",
    "ModifyAtrixSkillArgs",
    "ModifyAtrixSkillPreview",
    "ModifyAtrixSkillTool",
    "ReadAtrixSkillArgs",
    "ReadAtrixSkillTool",
    "atrix_skill_target_path",
    "existing_file_mode",
    "new_modify_atrix_skill_tool",
    "new_read_atrix_skill_tool",
    "parse_edits",
]

#: 工具名。对应 Go ``ReadAtrixSkillName`` / ``ModifyAtrixSkillName``。
READ_ATRIX_SKILL_NAME = "read_atrix_skill"
MODIFY_ATRIX_SKILL_NAME = "modify_atrix_skill"

#: 目标常量。对应 Go ``atrixSkillTargetAtriyph`` / ``atrixSkillTargetCodeSource``。
ATRIX_SKILL_TARGET_ATRIYPH = "skill_atriyph"
ATRIX_SKILL_TARGET_CODE_SOURCE = "code_source"

#: 编辑对象允许的键（对应 Go 的 ``DisallowUnknownFields``）。
_EDIT_KEYS = frozenset(
    {
        "operation",
        "line",
        "end_line",
        "new_text",
        "old_text",
        "anchor",
        "index",
        "all_matches",
    }
)


@dataclass
class ReadAtrixSkillArgs:
    """``read_atrix_skill`` 的参数。对应 Go ``readAtrixSkillArgs``。"""

    name: str = ""
    target: str = ""
    start_line: int = 0
    end_line: int = 0


@dataclass
class ModifyAtrixSkillArgs:
    """``modify_atrix_skill`` 的参数。对应 Go ``modifyAtrixSkillArgs``。"""

    name: str = ""
    target: str = ""
    encoding: str = ""
    expected_revision: str = ""
    context_lines: int = 0
    edits: list[Edit] = field(default_factory=list)


@dataclass
class ModifyAtrixSkillPreview:
    """一次修改的预检结果。对应 Go ``modifyAtrixSkillPreview``。"""

    name: str = ""
    target: str = ""
    path: str = ""
    result: Any = None
    next: str = ""
    diff: str = ""


class ReadAtrixSkillTool:
    """按行读取 skill 文件。对应 Go ``ReadAtrixSkillTool``。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def name(self) -> str:
        return READ_ATRIX_SKILL_NAME

    def info(self) -> Info:
        """对应 Go ``ReadAtrixSkillTool.Info``。"""
        return (
            new_builder(READ_ATRIX_SKILL_NAME)
            .description(
                "按行读取 ATRI-X 原生 ATRI-X Skill 的 SKILL.atriyph 或 main.go，"
                "返回 revision 和 1-based 行号，供修改前定位使用。"
            )
            .depends_on(MODIFY_ATRIX_SKILL_NAME)
            .source(SourceBuiltin)
            .risk(RiskMedium)
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``ReadAtrixSkillTool.Schema``。"""
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=READ_ATRIX_SKILL_NAME,
                description=(
                    "按行读取 ATRI-X 原生 ATRI-X Skill 文件并返回 revision。target 可选："
                    "skill_atriyph 读取 SKILL.atriyph；code_source 读取 main.go。默认读取 "
                    "SKILL.atriyph。start_line/end_line 为 1-based，可省略以读取全文。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "skill 名称。"},
                        "target": {
                            "type": "string",
                            "enum": [
                                ATRIX_SKILL_TARGET_ATRIYPH,
                                ATRIX_SKILL_TARGET_CODE_SOURCE,
                            ],
                            "description": (
                                "读取目标：skill_atriyph 表示 SKILL.atriyph；code_source "
                                "表示 main.go。默认 skill_atriyph。"
                            ),
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "可选，1-based 起始行，默认 1。",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "可选，1-based 结束行，默认文件末尾。",
                        },
                    },
                    "required": ["name"],
                },
            ),
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``ReadAtrixSkillTool.Call``。"""
        error = _ctx_error(ctx)
        if error is not None:
            raise error
        args = ReadAtrixSkillArgs()
        if (req.arguments or "").strip():
            try:
                raw = json.loads(req.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parse read_atrix_skill arguments: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("parse read_atrix_skill arguments: must be an object")
            args.name = str(raw.get("name") or "")
            args.target = str(raw.get("target") or "")
            args.start_line = _as_int(raw.get("start_line"))
            args.end_line = _as_int(raw.get("end_line"))
        path, _ = atrix_skill_target_path(
            self.manager, (args.name or "").strip(), (args.target or "").strip()
        )
        file = read_file(path, "")
        lines = split_lines(file.text)
        start = args.start_line if args.start_line > 0 else 1
        end = args.end_line
        if end <= 0 or end > len(lines):
            end = len(lines)
        if start > end + 1 or start > len(lines) + 1:
            raise ValueError(f"start_line {args.start_line} is out of range")
        out: list[str] = [f"revision: {content_revision(file.bytes)}"]
        for index in range(start, end + 1):
            out.append(f"{index}: {lines[index - 1]}")
        return Result(content="\n".join(out).rstrip("\n"))


class ModifyAtrixSkillTool:
    """按编辑协议修改 skill 文件。对应 Go ``ModifyAtrixSkillTool``。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def name(self) -> str:
        return MODIFY_ATRIX_SKILL_NAME

    def info(self) -> Info:
        """对应 Go ``ModifyAtrixSkillTool.Info``。"""
        return (
            new_builder(MODIFY_ATRIX_SKILL_NAME)
            .description(
                "修改 ATRI-X 原生 ATRI-X Skill 的 SKILL.atriyph 或 main.go；技能定义会"
                "校验 Atriyph 并 reload，源码只写入文件，完成后用 finalize_atrix_skill "
                "格式化和编译。"
            )
            .depends_on("finalize_atrix_skill", "read_atrix_skill")
            .source(SourceBuiltin)
            .risk(RiskHigh)
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``ModifyAtrixSkillTool.Schema``。"""
        edit_properties = edit_operation_properties()
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=MODIFY_ATRIX_SKILL_NAME,
                description=(
                    "修改 ATRI-X 原生 ATRI-X Skill 文件。target 可选：skill_atriyph 修改 "
                    "SKILL.atriyph；code_source 修改 main.go；默认 skill_atriyph。使用与 "
                    "edit_file 相同的精确文本、anchor、行号插入/删除协议；所有目标基于"
                    "编辑前原文解析，确认前自动预检并生成 diff。code_source 只写源码并"
                    "返回 diff，不会格式化、编译或 reload；完成后调用 finalize_atrix_skill "
                    "检查。skill_atriyph 会校验 Atriyph 并 reload。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "skill 名称。"},
                        "target": {
                            "type": "string",
                            "enum": [
                                ATRIX_SKILL_TARGET_ATRIYPH,
                                ATRIX_SKILL_TARGET_CODE_SOURCE,
                            ],
                            "description": (
                                "修改目标：skill_atriyph 表示 SKILL.atriyph；code_source "
                                "表示 main.go。默认 skill_atriyph。"
                            ),
                        },
                        "encoding": {
                            "type": "string",
                            "description": (
                                "文本编码，默认 auto；非 UTF-8 文件应显式传入 gb18030、"
                                "gbk、big5、shift_jis 等。"
                            ),
                        },
                        "expected_revision": {
                            "type": "string",
                            "description": (
                                "可选，编辑前读取到的 revision；用于防止外部并发修改。"
                            ),
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": (
                                "diff 上下文行数，默认 3，范围 0-20。确认前自动预检和实际"
                                "写入结果都会使用该上下文行数。"
                            ),
                        },
                        "edits": {
                            "type": "array",
                            "description": (
                                "批量编辑列表；所有目标均基于编辑前原文解析。已有文件的 "
                                "replace/insert/delete/overwrite 必须提供 expected_revision。"
                            ),
                            "items": {
                                "type": "object",
                                "properties": edit_properties,
                                "required": ["operation"],
                            },
                        },
                    },
                    "required": ["name", "edits"],
                },
            ),
        )

    # -- 预检与确认 ---------------------------------------------------------

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """确认前预检。对应 Go ``PreflightConfirmation``。"""
        await self.preview(ctx, req)

    async def risk_detail(self, ctx: Any, req: CallRequest) -> str:
        """风险确认详情。对应 Go ``RiskDetail``。"""
        preview = await self.preview(ctx, req)
        lines = [
            f"技能：{preview.name}",
            f"目标：{preview.target}",
            f"文件：{preview.path}",
            "模式：确认后写入；确认前已自动预检",
            "预检 diff:",
            preview.diff,
        ]
        return "\n".join(lines).rstrip("\n")

    # -- 调用 ---------------------------------------------------------------

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``ModifyAtrixSkillTool.Call``。"""
        preview = await self.preview(ctx, req)
        if preview.target == ATRIX_SKILL_TARGET_ATRIYPH:
            atomic_write_file(
                preview.path, preview.result.new_bytes, existing_file_mode(preview.path)
            )
            return Result(
                content=(
                    f"modified ATRI-X skill {preview.name} target "
                    f"{ATRIX_SKILL_TARGET_ATRIYPH}\n"
                    f"revision_after: {preview.result.revision_after}\n"
                    f"diff:\n{preview.diff}\n\n"
                    "SKILL.atriyph 已写入但尚未 reload；完成修改后调用 finalize_atrix_skill。"
                )
            )
        if preview.target == ATRIX_SKILL_TARGET_CODE_SOURCE:
            atomic_write_file(
                preview.path, preview.result.new_bytes, existing_file_mode(preview.path)
            )
            return Result(
                content=(
                    f"modified ATRI-X skill {preview.name} target "
                    f"{ATRIX_SKILL_TARGET_CODE_SOURCE}\n"
                    f"revision_after: {preview.result.revision_after}\n"
                    f"diff:\n{preview.diff}\n\n"
                    "源码已写入但尚未格式化/编译；完成修改后调用 finalize_atrix_skill。"
                )
            )
        raise ValueError(f"unsupported target {preview.target!r}")

    async def preview(self, ctx: Any, req: CallRequest) -> ModifyAtrixSkillPreview:
        """解析参数、预检编辑并校验新内容。对应 Go ``preview``。"""
        error = _ctx_error(ctx)
        if error is not None:
            raise error
        args = ModifyAtrixSkillArgs()
        if (req.arguments or "").strip():
            try:
                args = decode_modify_atrix_skill_args(req.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parse modify_atrix_skill arguments: {exc}") from exc
            except ValueError as exc:
                raise ValueError(f"parse modify_atrix_skill arguments: {exc}") from exc
        name = (args.name or "").strip()
        path, target = atrix_skill_target_path(
            self.manager, name, (args.target or "").strip()
        )
        if not args.edits:
            raise ValueError("edits is required")
        result = edit_file(
            path,
            args.encoding,
            args.expected_revision,
            False,
            True,
            args.context_lines,
            args.edits,
        )
        next_text, _, _ = decode_bytes(result.new_bytes, result.encoding)
        if target == ATRIX_SKILL_TARGET_ATRIYPH:
            validate_skill(next_text, name)
        return ModifyAtrixSkillPreview(
            name=name,
            target=target,
            path=path,
            result=result,
            next=next_text,
            diff=result.diff,
        )


def decode_modify_atrix_skill_args(raw: str) -> ModifyAtrixSkillArgs:
    """解析 ``modify_atrix_skill`` 参数（拒绝未知字段）。对应 Go ``decodeModifyAtrixSkillArgs``。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("arguments must be a JSON object")
    allowed = {"name", "target", "encoding", "expected_revision", "context_lines", "edits"}
    for key in data:
        if key not in allowed:
            raise ValueError(f'json: unknown field "{key}"')
    edits = data.get("edits") or []
    if not isinstance(edits, list):
        raise ValueError("edits must be an array")
    return ModifyAtrixSkillArgs(
        name=_as_text(data.get("name")),
        target=_as_text(data.get("target")),
        encoding=_as_text(data.get("encoding")),
        expected_revision=_as_text(data.get("expected_revision")),
        context_lines=_as_int(data.get("context_lines")),
        edits=parse_edits(edits),
    )


def parse_edits(items: list[Any]) -> list[Edit]:
    """把 JSON 编辑对象转成 :class:`Edit`（未知字段报错）。"""
    return [_parse_edit(item) for item in items]


def _parse_edit(item: Any) -> Edit:
    if not isinstance(item, dict):
        raise ValueError("each edit must be an object")
    for key in item:
        if key not in _EDIT_KEYS:
            raise ValueError(f'json: unknown field "{key}"')
    new_text = item.get("new_text")
    index = item.get("index")
    return Edit(
        operation=_as_text(item.get("operation")),
        line=_as_int(item.get("line")),
        end_line=_as_int(item.get("end_line")),
        new_text=None if new_text is None else _as_text(new_text),
        old_text=_as_text(item.get("old_text")),
        anchor=_as_text(item.get("anchor")),
        index=None if index is None else _as_int(index),
        all_matches=bool(item.get("all_matches")),
    )


def atrix_skill_target_path(
    manager: Any, name: str, target: str
) -> tuple[str, str]:
    """解析目标文件路径。对应 Go ``atrixSkillTargetPath``。"""
    if manager is None or manager.registry is None:
        raise ValueError("skill manager is not configured")
    validate_skill_name(name)
    if target == "":
        target = ATRIX_SKILL_TARGET_ATRIYPH
    if target == ATRIX_SKILL_TARGET_ATRIYPH:
        filename = SKILL_FILE_NAME
    elif target == ATRIX_SKILL_TARGET_CODE_SOURCE:
        filename = "main.go"
    else:
        raise ValueError(
            f"invalid target {target!r}; use {ATRIX_SKILL_TARGET_ATRIYPH!r} or "
            f"{ATRIX_SKILL_TARGET_CODE_SOURCE!r}"
        )
    path = os.path.join(manager.root, "go", name, filename)
    if not os.path.exists(path):
        raise ValueError(f"ATRI-X skill {target} {name!r} not found")
    return path, target


def existing_file_mode(path: str) -> int:
    """已存在文件的权限位，否则 ``0o644``。对应 Go ``existingFileMode``。"""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return 0o644


def new_read_atrix_skill_tool(manager: Any = None) -> ReadAtrixSkillTool:
    """对应 Go ``NewReadAtrixSkillTool``。"""
    return ReadAtrixSkillTool(manager)


def new_modify_atrix_skill_tool(manager: Any = None) -> ModifyAtrixSkillTool:
    """对应 Go ``NewModifyAtrixSkillTool``。"""
    return ModifyAtrixSkillTool(manager)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _ctx_error(ctx: Any) -> BaseException | None:
    if ctx is None:
        return None
    error = ctx.error()
    if error is not None:
        return error
    if ctx.is_cancelled():
        from atri_x.safecontext import CancelledError

        return CancelledError("context canceled")
    return None
