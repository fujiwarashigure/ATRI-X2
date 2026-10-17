"""原子编辑：解析操作、在**原文**上定位目标、按位置从后往前套用。

对应 Go：``internal/utils/fileops/edit.go``。

所有目标都基于编辑前原文解析，因此编辑之间不能重叠；操作按起始位置降序
套用，保证前面的偏移不失效；同一位置的插入按请求顺序输出。

``new_text`` / ``index`` 在 Go 里是指针（用于区分"未提供"与零值），Python
侧对应 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.utils.fileops.match import (
    SourceLine,
    find_content_matches,
    find_line_matches,
    select_matches,
)
from atri_x.utils.fileops.text import (
    normalize_edit_text,
    split_lines,
    validate_line_range,
    ensure_trailing_newline,
)

__all__ = [
    "EDIT_OPERATIONS",
    "Edit",
    "SourceLine",
    "apply_edits",
    "edit_operation_properties",
    "edits_require_revision",
    "scan_edit_lines",
]

EDIT_OPERATIONS = (
    "replace_text",
    "replace",
    "insert",
    "delete",
    "insert_before",
    "insert_after",
    "replace_line",
    "delete_line",
    "overwrite",
)


@dataclass
class Edit:
    """一次编辑操作。对应 Go ``fileops.Edit``。"""

    operation: str = ""
    line: int = 0
    end_line: int = 0
    new_text: str | None = None
    old_text: str = ""
    anchor: str = ""
    index: int | None = None
    all_matches: bool = False


@dataclass
class _ResolvedEdit:
    start: int = 0
    end: int = 0
    text: str = ""
    order: int = 0


def edit_operation_properties() -> dict[str, Any]:
    """编辑操作的 JSON schema 片段。对应 Go ``EditOperationProperties``。"""
    return {
        "operation": {
            "type": "string",
            "enum": list(EDIT_OPERATIONS),
            "description": (
                "编辑操作选择：replace_text 通过 old_text 精确定位并替换，可跨行；"
                "replace 通过 line/end_line 替换指定行范围，new_text 原样写入且"
                "不自动补换行，若需保留换行必须手动传入 \\n；replace_line 通过"
                " anchor 匹配并替换一整行，缩进需手动，换行自动追加。所有目标都"
                "基于编辑前原文解析，不能重叠目标；overwrite 必须是批次中的唯一操作。"
            ),
        },
        "line": {
            "type": "integer",
            "description": (
                "replace/delete 的起始行或 insert 的行间位置，1-based。"
                "insert: 1=文件开头，N+1=文件末尾；空文件仅支持 1。"
            ),
        },
        "end_line": {
            "type": "integer",
            "description": "replace/delete 的可选结束行，1-based 且包含该行；省略时只操作 line。",
        },
        "new_text": {
            "type": "string",
            "description": (
                "写入文本。replace_text/replace/overwrite 使用原样文本；replace "
                "不自动补换行，若需换行，需手动添加。insert/insert_before/"
                "insert_after/replace_line 把内容作为完整行块，缩进需手动，换行"
                "自动追加。replace_text 和 replace 可传空字符串进行删除。"
            ),
        },
        "old_text": {
            "type": "string",
            "description": "replace_text 的精确单行或多行匹配文本。",
        },
        "anchor": {
            "type": "string",
            "description": (
                "insert_before/insert_after/replace_line/delete_line 的单行前缀；"
                "匹配时忽略目标行的前导空格和 Tab。匹配多行但没传入 index 时，会返回 index"
            ),
        },
        "index": {
            "type": "integer",
            "description": "匹配到多处时选择第几处，1-based。与 all_matches 互斥。",
        },
        "all_matches": {
            "type": "boolean",
            "description": "对所有匹配执行操作。与 index 互斥；仅用于 old_text/anchor 操作。",
        },
    }


def edits_require_revision(edits: list[Edit] | None) -> bool:
    """位置类操作是否必须提供 ``expected_revision``。"""
    for edit in edits or []:
        if (edit.operation or "").strip().lower() in (
            "replace",
            "insert",
            "delete",
            "overwrite",
        ):
            return True
    return False


def apply_edits(text: str, edits: list[Edit] | None) -> str:
    """在 ``text`` 上按顺序解析并套用编辑，返回新文本。"""
    if not edits:
        raise ValueError("edits is required")
    text = normalize_edit_text(text)
    if (edits[0].operation or "").strip().lower() == "overwrite":
        if len(edits) != 1:
            raise ValueError("edit 1: overwrite must be the only edit")
        _validate_edit_fields(edits[0], "overwrite")
        return normalize_edit_text(edits[0].new_text or "")

    lines = scan_edit_lines(text)
    resolved: list[_ResolvedEdit] = []
    for position, edit in enumerate(edits):
        operation = (edit.operation or "").strip().lower()
        if operation == "overwrite":
            raise ValueError(f"edit {position + 1}: overwrite must be the only edit")
        try:
            items = _resolve_edit(text, lines, edit, operation)
        except ValueError as exc:
            raise ValueError(f"edit {position + 1}: {exc}") from exc
        for item in items:
            item.order = len(resolved)
            resolved.append(item)
    _validate_resolved_edits(resolved)
    ordered = sorted(
        resolved,
        key=lambda item: (
            -item.start,
            0 if item.start != item.end else 1,
            -item.order,
        ),
    )
    out = text
    for item in ordered:
        out = out[: item.start] + item.text + out[item.end :]
    return out


def scan_edit_lines(text: str) -> list[SourceLine]:
    """扫描 1-based 行区间（``start``/``end`` 不含换行，``full_end`` 含）。"""
    if text == "":
        return []
    lines: list[SourceLine] = []
    start = 0
    while start < len(text):
        relative = text.find("\n", start)
        if relative < 0:
            lines.append(
                SourceLine(start=start, end=len(text), full_end=len(text), text=text[start:])
            )
            break
        end = relative
        lines.append(
            SourceLine(start=start, end=end, full_end=end + 1, text=text[start:end])
        )
        start = end + 1
    return lines


def _resolve_edit(
    text: str, lines: list[SourceLine], edit: Edit, operation: str
) -> list[_ResolvedEdit]:
    _validate_edit_fields(edit, operation)
    if operation == "replace_text":
        old_text = normalize_edit_text(edit.old_text)
        locations = select_matches(
            find_content_matches(text, old_text), edit.index, edit.all_matches, "old_text"
        )
        return [
            _ResolvedEdit(
                start=loc.byte_start,
                end=loc.byte_end,
                text=normalize_edit_text(edit.new_text or ""),
            )
            for loc in locations
        ]
    if operation == "replace":
        end_line = edit.end_line or edit.line
        validate_line_range(len(lines), edit.line, end_line)
        start, end = _line_delete_range(lines, edit.line, end_line)
        return [
            _ResolvedEdit(
                start=start, end=end, text=normalize_edit_text(edit.new_text or "")
            )
        ]
    if operation == "insert":
        block = _normalize_line_block(edit.new_text or "")
        start, inserted = _resolve_line_insert(text, lines, edit.line, block)
        return [_ResolvedEdit(start=start, end=start, text=inserted)]
    if operation == "delete":
        end_line = edit.end_line or edit.line
        validate_line_range(len(lines), edit.line, end_line)
        start, end = _line_delete_range(lines, edit.line, end_line)
        return [_ResolvedEdit(start=start, end=end)]
    if operation in ("insert_before", "insert_after", "replace_line", "delete_line"):
        anchor = normalize_edit_text(edit.anchor)
        locations = select_matches(
            find_line_matches(lines, anchor), edit.index, edit.all_matches, "anchor"
        )
        block = ""
        if operation != "delete_line":
            block = _normalize_line_block(edit.new_text or "")
        out: list[_ResolvedEdit] = []
        for loc in locations:
            line = lines[loc.line_index]
            if operation == "insert_before":
                out.append(_ResolvedEdit(start=line.start, end=line.start, text=block + "\n"))
            elif operation == "insert_after":
                if line.full_end > line.end:
                    out.append(
                        _ResolvedEdit(start=line.full_end, end=line.full_end, text=block + "\n")
                    )
                else:
                    out.append(
                        _ResolvedEdit(start=line.end, end=line.end, text="\n" + block)
                    )
            elif operation == "replace_line":
                out.append(_ResolvedEdit(start=line.start, end=line.end, text=block))
            else:
                start, end = _line_delete_range(lines, loc.line_index + 1, loc.line_index + 1)
                out.append(_ResolvedEdit(start=start, end=end))
        return out
    raise ValueError(f"unsupported operation {edit.operation!r}")


def _validate_edit_fields(edit: Edit, operation: str) -> None:
    if operation == "":
        raise ValueError("operation is required")
    if edit.index is not None and edit.all_matches:
        raise ValueError("index and all_matches are mutually exclusive")

    def reject_line_fields() -> None:
        if edit.line != 0 or edit.end_line != 0:
            raise ValueError(f'operation "{operation}" does not use line or end_line')

    def reject_match_fields() -> None:
        if edit.index is not None or edit.all_matches:
            raise ValueError(
                f'operation "{operation}" does not support index or all_matches'
            )

    if operation == "replace_text":
        if normalize_edit_text(edit.old_text) == "":
            raise ValueError("old_text is required")
        if edit.new_text is None:
            raise ValueError("new_text is required")
        if (edit.anchor or "").strip() != "":
            raise ValueError(f'operation "{operation}" uses old_text, not anchor')
        reject_line_fields()
        return
    if operation == "replace":
        if edit.line < 1:
            raise ValueError("line must be >= 1")
        if edit.new_text is None:
            raise ValueError("new_text is required")
        if edit.old_text != "" or edit.anchor != "":
            raise ValueError(
                f'operation "{operation}" only uses line, optional end_line, and new_text'
            )
        reject_match_fields()
        return
    if operation == "insert":
        if edit.line < 1:
            raise ValueError("line must be >= 1")
        if edit.end_line != 0 or edit.old_text != "" or edit.anchor != "":
            raise ValueError(f'operation "{operation}" only uses line and new_text')
        if edit.new_text is None:
            raise ValueError("new_text is required")
        reject_match_fields()
        return
    if operation == "delete":
        if edit.line < 1:
            raise ValueError("line must be >= 1")
        if edit.new_text is not None or edit.old_text != "" or edit.anchor != "":
            raise ValueError(
                f'operation "{operation}" only uses line and optional end_line'
            )
        reject_match_fields()
        return
    if operation in ("insert_before", "insert_after", "replace_line", "delete_line"):
        anchor = normalize_edit_text(edit.anchor)
        if anchor == "":
            raise ValueError("anchor is required")
        if "\n" in anchor:
            raise ValueError("anchor must be a single-line prefix")
        if edit.old_text != "":
            raise ValueError(f'operation "{operation}" uses anchor, not old_text')
        reject_line_fields()
        if operation == "delete_line":
            if edit.new_text is not None:
                raise ValueError(f'operation "{operation}" does not use new_text')
        elif edit.new_text is None:
            raise ValueError("new_text is required")
        return
    if operation == "overwrite":
        if edit.new_text is None:
            raise ValueError("new_text is required")
        if (
            edit.line != 0
            or edit.end_line != 0
            or edit.old_text != ""
            or edit.anchor != ""
            or edit.index is not None
            or edit.all_matches
        ):
            raise ValueError(f'operation "{operation}" only uses new_text')
        return
    raise ValueError(f"unsupported operation {edit.operation!r}")


def _normalize_line_block(text: str) -> str:
    lines = split_lines(ensure_trailing_newline(text))
    if not lines:
        raise ValueError("new_text must contain at least one line")
    return "\n".join(lines)


def _resolve_line_insert(
    text: str, lines: list[SourceLine], line: int, block: str
) -> tuple[int, str]:
    total = len(lines)
    if line < 1 or line > total + 1:
        raise ValueError(f"insert line {line} exceeds valid positions 1-{total + 1}")
    if total == 0:
        return 0, block
    if line <= total:
        return lines[line - 1].start, block + "\n"
    if text.endswith("\n"):
        return len(text), block + "\n"
    return len(text), "\n" + block


def _line_delete_range(
    lines: list[SourceLine], start_line: int, end_line: int
) -> tuple[int, int]:
    start = lines[start_line - 1].start
    end = lines[end_line - 1].full_end
    last = lines[end_line - 1]
    if end_line == len(lines) and last.full_end == last.end and start_line > 1:
        start = lines[start_line - 2].end
    return start, end


def _validate_resolved_edits(edits: list[_ResolvedEdit]) -> None:
    for i in range(len(edits)):
        for j in range(i + 1, len(edits)):
            a, b = edits[i], edits[j]
            a_insert = a.start == a.end
            b_insert = b.start == b.end
            if not a_insert and not b_insert and a.start < b.end and b.start < a.end:
                raise ValueError(
                    f"edits overlap in original content at byte ranges "
                    f"{a.start}-{a.end} and {b.start}-{b.end}"
                )
            if a_insert and not b_insert and a.start > b.start and a.start < b.end:
                raise ValueError(
                    f"insertion at byte {a.start} falls inside edited range {b.start}-{b.end}"
                )
            if b_insert and not a_insert and b.start > a.start and b.start < a.end:
                raise ValueError(
                    f"insertion at byte {b.start} falls inside edited range {a.start}-{a.end}"
                )
