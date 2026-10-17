"""``edit_file`` 的人读确认详情渲染。

对应 Go：``internal/tool/builtin/file_tools.go`` 的 ``RiskDetail`` 渲染部分
（``fileToolBoolText`` / ``editOperationTitle`` / ``writeEditLocation`` /
``editLineRangeText`` / ``writeEditMatchDetail`` / ``writeEditContentDetail`` /
``writeIndentedBlock`` / ``truncateUTF8Bytes``）。

拆分原因：``file_tools.py`` 主体已接近 700 行上限，把纯文本渲染按职责剥离。
"""

from __future__ import annotations

from atri_x.utils.fileops import Edit

EDIT_DETAIL_MAX_BYTES = 4 * 1024


def file_tool_bool_text(value: bool) -> str:
    """对应 Go ``fileToolBoolText``。"""
    return "是" if value else "否"


def edit_operation_title(operation: str) -> str:
    """对应 Go ``editOperationTitle``。"""
    titles = {
        "replace_text": "精确文本替换",
        "replace": "按行号范围替换",
        "insert": "按行间位置插入",
        "delete": "按行号删除",
        "insert_before": "在 anchor 行前插入",
        "insert_after": "在 anchor 行后插入",
        "replace_line": "替换 anchor 行",
        "delete_line": "删除 anchor 行",
        "overwrite": "覆盖整个文件",
    }
    if operation in titles:
        return titles[operation]
    if not (operation or "").strip():
        return "未知操作"
    return operation


def edit_line_range_text(start: int, end: int) -> str:
    """对应 Go ``editLineRangeText``。"""
    if end <= 0 or end == start:
        return str(start)
    return f"{start}-{end}"


def write_edit_location(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditLocation``。"""
    if edit.operation == "insert":
        if edit.line > 0:
            out.append(f"原始行间位置：{edit.line}\n")
    elif edit.operation in ("replace", "delete"):
        if edit.line > 0:
            out.append(f"原始位置：{edit_line_range_text(edit.line, edit.end_line)}\n")


def write_edit_match_detail(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditMatchDetail``。"""
    if edit.operation == "replace_text":
        match_text = edit.old_text
    elif edit.operation in (
        "insert_before",
        "insert_after",
        "replace_line",
        "delete_line",
    ):
        match_text = edit.anchor
    else:
        return
    if edit.index is not None:
        out.append(f"第几处匹配：{edit.index}\n")
    if edit.all_matches:
        out.append("匹配范围：全部\n")
    write_indented_block(out, "匹配内容：", match_text)


def write_edit_content_detail(out: list[str], edit: Edit) -> None:
    """对应 Go ``writeEditContentDetail``。"""
    if edit.new_text is None:
        return
    if edit.operation in ("insert", "insert_before", "insert_after"):
        write_indented_block(out, "插入内容：", edit.new_text)
    elif edit.operation == "overwrite":
        write_indented_block(out, "文件新内容：", edit.new_text)
    else:
        write_indented_block(out, "新内容：", edit.new_text)


def write_indented_block(out: list[str], title: str, text: str) -> None:
    """对应 Go ``writeIndentedBlock``：缩进两格，超出 4 KiB 时截断并标注原文规模。"""
    out.append(title + "\n")
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if text == "":
        out.append("  (空)\n")
        return
    original_bytes = len(text.encode("utf-8"))
    original_lines = text.count("\n") + 1
    displayed, truncated = truncate_utf8_bytes(text, EDIT_DETAIL_MAX_BYTES)
    for line in displayed.split("\n"):
        out.append("  " + line + "\n")
    if truncated:
        out.append(f"  ...（已省略；原文 {original_bytes} 字节，{original_lines} 行）\n")


def truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """对应 Go ``truncateUTF8Bytes``：按 UTF-8 字节截断且不切断字符。"""
    encoded = text.encode("utf-8")
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return text, False
    end = max_bytes
    while end > 0:
        try:
            encoded[:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    return encoded[:end].decode("utf-8", "ignore"), True


__all__ = [name for name in dir() if not name.startswith("_")]
