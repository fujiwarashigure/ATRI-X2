"""``read_file`` 与 ``edit_file``：带 revision 校验的文本读写。

对应 Go：``internal/tool/builtin/file_tools.go``。

与 Go 的差异（仅形式）：

* Go 的 ``(*Result, error)`` → Python 的 ``call`` 返回 :class:`Result` 并在失败时
  抛异常；``Executor`` 会把异常回灌给 LLM。
* ``decodeEditArgs`` 的 ``DisallowUnknownFields`` 用显式白名单复刻（键名与 Go tag
  逐字一致）。
* 路径解析 :func:`atri_x.tool.workspace.resolve_workspace_path` 在本仓是异步的，
  因此 ``call`` / ``assess_risk`` / ``preflight_confirmation`` / ``risk_detail``
  全部是 ``async def``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import first_file_guard, go_quote
from atri_x.tool.builtin.file_guard import FileGuard
from atri_x.tool.builtin.file_rollback import ROLLBACK_FILE_NAME
from atri_x.tool.builtin.file_tools_ast import (
    READ_FILE_MODE_AST,
    READ_FILE_MODE_AST_FUNCTION,
    READ_FILE_MODE_GREP,
    READ_FILE_MODE_READ,
    normalize_read_file_mode,
    read_file_ast_function_result,
    read_file_ast_result,
)
from atri_x.tool.builtin.file_tools_detail import (
    EDIT_DETAIL_MAX_BYTES,
    edit_line_range_text,
    file_tool_bool_text as detail_file_tool_bool_text,
    edit_operation_title as detail_edit_operation_title,
    truncate_utf8_bytes as detail_truncate_utf8_bytes,
    write_edit_content_detail as detail_write_edit_content_detail,
    write_edit_location as detail_write_edit_location,
    write_edit_match_detail as detail_write_edit_match_detail,
    write_indented_block as detail_write_indented_block,
)
from atri_x.tool.builtin.file_tools_read import (
    ReadFileArgs,
    is_sensitive_read_file,
    read_file_builder,
    read_file_grep_result,
    read_file_plain as _read_file_plain,
)
from atri_x.tool.builtin.file_tools_directory import (
    FILE_TOOL_DETAILED_MAX_BYTES,
    FILE_TOOL_MAX_BYTES,
    DirectoryASTCache,
    read_file_directory_search,
)
from atri_x.tool.sandbox import (
    sandbox_context_from_context,
    session_id_from_context,
)
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskAssessment,
    RiskHigh,
    RiskLow,
    RiskMedium,
)
from atri_x.tool.workspace import (
    PathResolveOptions,
    ResolvedPath,
    resolve_workspace_path,
)
from atri_x.utils.fileops import (
    Edit,
    EditFileOptions,
    EditResult,
    File,
    LineNumber,
    content_revision,
    edit_file_with_options,
    edit_operation_properties,
    normalize_grep_context_lines,
    normalize_max_matches,
    normalize_read_range,
    read_file_with_limit,
    split_lines,
)

_READ_FILE_ARG_KEYS = frozenset(
    {
        "path",
        "encoding",
        "start_line",
        "end_line",
        "mode",
        "query",
        "context_lines",
        "max_matches",
        "index",
    }
)
_EDIT_FILE_ARG_KEYS = frozenset(
    {
        "path",
        "encoding",
        "expected_revision",
        "create",
        "context_lines",
        "edits",
    }
)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
@dataclass
class ReadFileArgs:
    """对应 Go ``readFileArgs``。"""

    path: str = ""
    encoding: str = ""
    start_line: int = 0
    end_line: LineNumber | None = None
    mode: str = ""
    query: str = ""
    context_lines: int = 0
    max_matches: int = 0
    index: int = 0


@dataclass
class EditFileArgs:
    """对应 Go ``editFileArgs``。"""

    path: str = ""
    encoding: str = ""
    expected_revision: str = ""
    create: bool = False
    context_lines: int = 0
    edits: list[Edit] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.edits is None:
            self.edits = []


def parse_read_file_args(raw: str) -> ReadFileArgs:
    """对应 Go ``readFileArgs`` 的 json.Unmarshal（含 ``readFileInteger`` 语义）。"""
    data = _decode_object(raw, _READ_FILE_ARG_KEYS, "read_file")
    args = ReadFileArgs()
    args.path = _as_str(data.get("path"))
    args.encoding = _as_str(data.get("encoding"))
    args.start_line = _as_int_or_int_string(data.get("start_line"), "start_line")
    args.end_line = LineNumber.from_json(data.get("end_line"))
    args.mode = _as_str(data.get("mode"))
    args.query = _as_str(data.get("query"))
    args.context_lines = _as_int(data.get("context_lines"))
    args.max_matches = _as_int(data.get("max_matches"))
    args.index = _as_int(data.get("index"))
    return args


def parse_edit_file_args(raw: str) -> EditFileArgs:
    """对应 Go ``decodeEditArgs``（``DisallowUnknownFields``）。"""
    data = _decode_object(raw, _EDIT_FILE_ARG_KEYS, "edit_file")
    args = EditFileArgs()
    args.path = _as_str(data.get("path"))
    args.encoding = _as_str(data.get("encoding"))
    args.expected_revision = _as_str(data.get("expected_revision"))
    args.create = bool(data.get("create"))
    args.context_lines = _as_int(data.get("context_lines"))
    edits = data.get("edits") or []
    if not isinstance(edits, list):
        raise InvalidError("edits must be an array")
    args.edits = [_parse_edit(item) for item in edits]
    return args


def _parse_edit(item: Any) -> Edit:
    if not isinstance(item, dict):
        raise InvalidError("each edit must be an object")
    allowed = {
        "operation",
        "line",
        "end_line",
        "new_text",
        "old_text",
        "anchor",
        "index",
        "all_matches",
    }
    for key in item:
        if key not in allowed:
            raise InvalidError(f'json: unknown field "{key}"')
    new_text = item.get("new_text")
    index = item.get("index")
    return Edit(
        operation=_as_str(item.get("operation")),
        line=_as_int(item.get("line")),
        end_line=_as_int(item.get("end_line")),
        new_text=None if new_text is None else _as_str(new_text),
        old_text=_as_str(item.get("old_text")),
        anchor=_as_str(item.get("anchor")),
        index=None if index is None else _as_int(index),
        all_matches=bool(item.get("all_matches")),
    )


class ReadFileTool:
    """对应 Go ``ReadFileTool``。"""

    def __init__(self, file_guard: FileGuard | None = None) -> None:
        self.file_guard = file_guard
        self.ast_cache = DirectoryASTCache()

    def name(self) -> str:
        return "read_file"

    def info(self) -> Info:
        return read_file_builder().build_info()

    def schema(self) -> ToolSchema:
        return read_file_builder().build_schema()

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """对应 Go ``ReadFileTool.AssessRisk``。"""
        args = parse_read_file_args(req.arguments)
        mode = normalize_read_file_mode(args.mode)
        resolved = await resolve_workspace_path(
            ctx, args.path, PathResolveOptions(allow_directory=mode != READ_FILE_MODE_READ)
        )
        if is_sensitive_read_file(resolved.path):
            return RiskAssessment(
                level=RiskHigh, reasons=["读取可能包含凭据的敏感文件，需要用户确认"]
            )
        return RiskAssessment(level=RiskLow, reasons=["读取普通文本文件"])

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``ReadFileTool.Call``。"""
        args = parse_read_file_args(req.arguments)
        mode = normalize_read_file_mode(args.mode)
        resolved = await resolve_workspace_path(
            ctx, args.path, PathResolveOptions(allow_directory=mode != READ_FILE_MODE_READ)
        )
        path = resolved.path
        try:
            st = os.stat(path)
        except OSError as exc:
            raise InvalidError(f"stat file: {exc}") from exc
        if os.path.isdir(path):
            if mode == READ_FILE_MODE_READ:
                raise InvalidError("read mode requires a file path")
            return await read_file_directory_search(
                ctx, path, mode, args, list(resolved.warnings), self.ast_cache
            )
        if st.st_size > FILE_TOOL_MAX_BYTES:
            raise InvalidError(
                f"file too large: {st.st_size} bytes exceeds {FILE_TOOL_MAX_BYTES}"
            )
        large_file = st.st_size > FILE_TOOL_DETAILED_MAX_BYTES
        if large_file and mode in (READ_FILE_MODE_AST, READ_FILE_MODE_AST_FUNCTION):
            raise InvalidError(
                f"{mode} mode is disabled for this {st.st_size}-byte file; "
                "use read or grep mode"
            )
        file = read_file_with_limit(path, args.encoding, FILE_TOOL_MAX_BYTES)
        lines = split_lines(file.text)
        warnings = [*resolved.warnings]
        if self.file_guard is not None:
            warnings.extend(self.file_guard.read_warnings(path))
        if large_file:
            warnings.append(
                f"large file: {st.st_size} bytes; ast and ast_function modes are disabled"
            )
        if mode == READ_FILE_MODE_GREP:
            return read_file_grep_result(
                file,
                lines,
                args.query,
                args.context_lines,
                args.max_matches,
                args.index,
                warnings,
            )
        if mode == READ_FILE_MODE_AST:
            return read_file_ast_result(
                file,
                args.query,
                args.context_lines,
                args.max_matches,
                args.index,
                warnings,
            )
        if mode == READ_FILE_MODE_AST_FUNCTION:
            return read_file_ast_function_result(
                file, args.query, args.max_matches, args.index, warnings
            )
        return _read_file_plain(file, lines, args.start_line, args.end_line, warnings)


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------
class EditFileTool:
    """对应 Go ``EditFileTool``。"""

    def __init__(
        self, file_guard: FileGuard | None = None, backups: Any = None
    ) -> None:
        self.file_guard = file_guard
        self.backups = backups

    def name(self) -> str:
        return "edit_file"

    def info(self) -> Info:
        return edit_file_builder().build_info()

    def schema(self) -> ToolSchema:
        return edit_file_builder().build_schema()

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """对应 Go ``EditFileTool.AssessRisk``。"""
        args = parse_edit_file_args(req.arguments)
        resolved = await _resolve_file_tool_path(ctx, args.path, args.create)
        self._check_write(resolved.path)
        sandbox, ok = sandbox_context_from_context(ctx)
        if ok and sandbox.background:
            return RiskAssessment(
                level=RiskMedium, reasons=["后台文件编辑限制在当前任务工作目录内"]
            )
        return RiskAssessment(level=RiskHigh, reasons=["文件内容写入操作需要确认"])

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """对应 Go ``EditFileTool.PreflightConfirmation``。"""
        args = parse_edit_file_args(req.arguments)
        await preview_edit_file(ctx, args, self.file_guard)

    async def risk_detail(self, ctx: Any, req: CallRequest) -> str:
        """对应 Go ``EditFileTool.RiskDetail``。"""
        args = parse_edit_file_args(req.arguments)
        out: list[str] = [f"文件：{args.path.strip()}\n"]
        out.append("模式：确认后写入；确认前已自动预检\n")
        out.append(f"创建文件：{file_tool_bool_text(args.create)}\n")
        if args.encoding.strip():
            out.append(f"编码：{args.encoding.strip()}\n")
        else:
            out.append("编码：auto\n")
        out.append(f"编辑数：{len(args.edits)}\n")
        if args.expected_revision.strip():
            out.append("文件 revision 校验：有\n")
        for position, edit in enumerate(args.edits):
            out.append(
                f"\n编辑 {position + 1}/{len(args.edits)}："
                f"{edit_operation_title(edit.operation)}\n"
            )
            write_edit_location(out, edit)
            write_edit_match_detail(out, edit)
            write_edit_content_detail(out, edit)
        preview = await preview_edit_file(ctx, args, self.file_guard)
        out.append("\n预检 diff:\n")
        out.append(preview.diff)
        if not preview.diff.endswith("\n"):
            out.append("\n")
        return "".join(out).rstrip("\n")

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``EditFileTool.Call``。"""
        args = parse_edit_file_args(req.arguments)
        resolved = await _resolve_file_tool_path(ctx, args.path, args.create)
        self._check_write(resolved.path)
        before, before_mode, existed = capture_edit_backup(resolved.path, args.encoding)
        try:
            result = edit_file_with_options(
                resolved.path,
                args.encoding,
                args.expected_revision,
                args.create,
                False,
                args.context_lines,
                args.edits,
                file_tool_edit_options(),
            )
        except (ValueError, OSError) as exc:
            raise InvalidError(str(exc)) from exc
        if self.backups is not None and not result.dry_run:
            self.backups.record(
                session_id_from_context(ctx),
                result.path,
                before,
                before_mode,
                existed,
                result.revision_before,
                result.revision_after,
            )
        content = (
            f"dry_run: {'true' if result.dry_run else 'false'}\n"
            f"edited: {result.path}\n"
            f"created: {'true' if result.created else 'false'}\n"
            f"encoding: {result.encoding}\n"
            f"revision_before: {result.revision_before}\n"
            f"revision_after: {result.revision_after}\n"
            f"diff:\n{result.diff}"
        )
        return Result(content=content, warnings=list(resolved.warnings))

    def _check_write(self, path: str) -> None:
        if self.file_guard is not None:
            self.file_guard.check_write(path)


def edit_file_builder() -> Builder:
    """对应 Go ``editFileBuilder``。"""
    return (
        Builder("edit_file")
        .description(
            "批量编辑文本文件；使用 edits 一次提交多个修改；成功后返回 unified diff。"
            "任一 edit 失败则不写文件。"
        )
        .risk(RiskHigh)
        .depends_on("workspace", ROLLBACK_FILE_NAME)
        .tags("files", "agent")
        .string(
            "path",
            "要编辑的文件路径,基于当前 workspace 解析；也可传绝对路径。",
            required(),
        )
        .string(
            "encoding",
            "文本编码，默认 auto；非 UTF-8 文件应显式传入 gb18030、gbk、big5、shift_jis 等。",
        )
        .string("expected_revision", "可选，编辑前读取到的revision；用于防止外部并发修改。")
        .boolean(
            "create",
            "为 true 时允许创建不存在的文本文件；提供 expected_revision 时仍要求文件已存在。",
        )
        .integer(
            "context_lines",
            "diff 上下文行数，默认 3，范围 0-20。确认前自动预检和实际写入结果都会使用该上下文行数。",
        )
        .object_array(
            "edits",
            "批量编辑列表；所有目标均基于编辑前原文解析。编辑已有文件时，应在本次调用的"
            "顶层传入 expected_revision，其值来自 read_file 返回的 revision; "
            "创建新文件时不提供该字段。",
            edit_operation_properties(),
            ["operation"],
            required(),
        )
    )


def file_tool_bool_text(value: bool) -> str:
    """对应 Go ``fileToolBoolText``（实现见 ``file_tools_detail``）。"""
    return detail_file_tool_bool_text(value)


def edit_operation_title(operation: str) -> str:
    """对应 Go ``editOperationTitle``（实现见 ``file_tools_detail``）。"""
    return detail_edit_operation_title(operation)


def write_edit_location(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditLocation``（实现见 ``file_tools_detail``）。"""
    detail_write_edit_location(out, edit)


def write_edit_match_detail(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditMatchDetail``（实现见 ``file_tools_detail``）。"""
    detail_write_edit_match_detail(out, edit)


def write_edit_content_detail(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditContentDetail``（实现见 ``file_tools_detail``）。"""
    detail_write_edit_content_detail(out, edit)


def write_indented_block(out: list[str], title: str, text: str) -> None:
    """对应 Go ``writeIndentedBlock``（实现见 ``file_tools_detail``）。"""
    detail_write_indented_block(out, title, text)


def truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """对应 Go ``truncateUTF8Bytes``（实现见 ``file_tools_detail``）。"""
    return detail_truncate_utf8_bytes(text, max_bytes)


def file_tool_edit_options() -> EditFileOptions:
    """对应 Go ``fileToolEditOptions``。"""
    return EditFileOptions(
        max_input_bytes=FILE_TOOL_MAX_BYTES,
        max_output_bytes=FILE_TOOL_MAX_BYTES,
        detailed_diff_max_bytes=FILE_TOOL_DETAILED_MAX_BYTES,
    )


def capture_edit_backup(path: str, encoding: str) -> tuple[bytes, int, bool]:
    """对应 Go ``captureEditBackup``：尽力而为，读不到就没有备份。"""
    try:
        st = os.stat(path)
    except OSError:
        return b"", 0, False
    if os.path.isdir(path):
        return b"", 0, False
    try:
        file = read_file_with_limit(path, encoding, FILE_TOOL_MAX_BYTES)
    except (OSError, ValueError):
        return b"", 0, False
    return file.bytes, st.st_mode & 0o777, True


async def preview_edit_file(
    ctx: Any, args: EditFileArgs, file_guard: FileGuard | None
) -> EditResult:
    """对应 Go ``previewEditFile``。"""
    resolved = await _resolve_file_tool_path(ctx, args.path, args.create)
    if file_guard is not None:
        file_guard.check_write(resolved.path)
    try:
        return edit_file_with_options(
            resolved.path,
            args.encoding,
            args.expected_revision,
            args.create,
            True,
            args.context_lines,
            args.edits,
            file_tool_edit_options(),
        )
    except (ValueError, OSError) as exc:
        raise InvalidError(f"preflight edit_file: {exc}") from exc


async def _resolve_file_tool_path(
    ctx: Any, raw_path: str, allow_create: bool
) -> ResolvedPath:
    """对应 Go ``resolveFileToolPath``。"""
    return await resolve_workspace_path(
        ctx, raw_path, PathResolveOptions(allow_create=allow_create)
    )


# ---------------------------------------------------------------------------
# 参数解析助手
# ---------------------------------------------------------------------------
def _decode_object(raw: str, allowed: frozenset[str], tool_name: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse {tool_name} arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"parse {tool_name} arguments: expected a JSON object")
    for key in data:
        if key not in allowed:
            raise InvalidError(f'parse {tool_name} arguments: json: unknown field "{key}"')
    return data


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidError(f"expected an integer, got {value!r}") from exc


def _as_int_or_int_string(value: Any, field: str) -> int:
    """对应 Go ``readFileInteger.UnmarshalJSON``。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise InvalidError(f"{field} must be an integer or integer string")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text == "null":
            return 0
        try:
            return int(text)
        except ValueError as exc:
            raise InvalidError(f"{field} must be an integer or integer string") from exc
    raise InvalidError(f"{field} must be an integer or integer string")


__all__ = [name for name in dir() if not name.startswith("_")]
