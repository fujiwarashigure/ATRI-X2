"""``read_file`` 的声明与结果渲染。

对应 Go：``internal/tool/builtin/file_tools.go`` 的 read 分支
（``readFileBuilder`` / ``isSensitiveReadFile`` / ``ReadFileTool.Call`` 的
read 与 grep 渲染部分）。

拆分原因：``file_tools.py`` 主体接近 700 行上限，按「读侧 / 写侧」职责剥离。
参数解析仍留在 ``file_tools.py``（与 ``edit_file`` 共用同一套解码助手）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import go_quote
from atri_x.tool.builtin.file_rollback import ROLLBACK_FILE_NAME
from atri_x.tool.tool import Result, RiskLow
from atri_x.utils.fileops import (
    File,
    LineNumber,
    content_revision,
    normalize_grep_context_lines,
    normalize_max_matches,
    normalize_read_range,
)

_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "kubeconfig",
        "id_rsa",
        "id_ed25519",
    }
)

#: ``read_file`` 允许的 JSON 字段（对应 Go 的 ``readFileArgs`` tag）。
READ_FILE_ARG_KEYS = frozenset(
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


def is_sensitive_read_file(path: str) -> bool:
    """对应 Go ``isSensitiveReadFile``。"""
    name = os.path.basename(path).lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SENSITIVE_NAMES:
        return True
    return "secret" in name or "token" in name or "credential" in name


def read_file_builder() -> Builder:
    """对应 Go ``readFileBuilder``。"""
    return (
        Builder("read_file")
        .description(
            "读取文本文件并返回行号和 revision；支持按行读取，以及在文件或目录中"
            "进行文本、AST 名称和函数名搜索。"
        )
        .risk(RiskLow)
        .superadmin_only()
        .tags("files", "agent")
        .depends_on("workspace", ROLLBACK_FILE_NAME)
        .string(
            "path",
            "文件或目录路径；read 模式仅支持文件，搜索模式可递归搜索目录。",
            required(),
        )
        .string("encoding", "文本编码，默认 auto。")
        .string(
            "mode",
            "模式：read（默认，可不填）、grep、ast、ast_function；"
            "ast/ast_function 仅支持 Go 和 Shell。",
        )
        .string(
            "query",
            "grep/ast/ast_function 模式的搜索内容；ast 按名称精确匹配，"
            "ast_function 按函数名精确匹配。",
        )
        .integer(
            "index",
            "可选，搜索结果序号，默认不填。grep/ast/ast_function返回内容有多个时，"
            "根据返回内容的编号再次搜索时填写。",
        )
        .integer("start_line", "read 模式起始行，1-based，默认 1。")
        .string("end_line", "read 模式结束行（含），可传 end；默认最多读取 200 行。")
        .integer("context_lines", "grep/ast 模式上下文行数，默认 2，范围 0-20。")
        .integer("max_matches", "grep/ast/ast_function 模式最大匹配数，默认 20，范围 1-100。")
    )


def read_file_plain(
    file: File,
    lines: list[str],
    start_line: int,
    end_line: LineNumber | None,
    warnings: list[str],
) -> Result:
    """对应 Go ``ReadFileTool.Call`` 的 read 分支。"""
    try:
        start, end, truncated = normalize_read_range(
            len(lines), start_line, end_line or LineNumber()
        )
    except ValueError as exc:
        raise InvalidError(str(exc)) from exc
    out = [
        f"file: {file.path}\n",
        f"encoding: {file.encoding}\n",
        f"revision: {content_revision(file.bytes)}\n",
    ]
    if not lines:
        out.append("lines: 0/0\n")
        out.append("empty: true\n")
        out.append(f"truncated: {'true' if truncated else 'false'}\n")
        out.append("content:\n")
        return Result(content="".join(out), warnings=warnings)
    out.append(f"lines: {start}-{end}/{len(lines)}\n")
    out.append(f"truncated: {'true' if truncated else 'false'}\n")
    out.append("content:\n")
    width = len(str(end))
    for number in range(start, end + 1):
        out.append(f"{number:>{width}} | {lines[number - 1]}\n")
    return Result(content="".join(out), warnings=warnings)


def read_file_grep_result(
    file: File,
    lines: list[str],
    grep: str,
    context_lines: int,
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Result:
    """对应 Go ``readFileGrepResult``。"""
    query = (grep or "").strip()
    if query == "":
        raise InvalidError("grep is required")
    context_lines = normalize_grep_context_lines(context_lines)
    max_matches = normalize_max_matches(max_matches)
    matches: list[int] = []
    for position, line in enumerate(lines):
        if query in line:
            matches.append(position + 1)
            if len(matches) >= max_matches:
                break
    if index > 0:
        if index > len(matches):
            raise InvalidError(
                f"index {index} is out of range; found {len(matches)} matches"
            )
        matches = matches[index - 1 : index]
    out = [
        f"file: {file.path}\n",
        f"encoding: {file.encoding}\n",
        f"revision: {content_revision(file.bytes)}\n",
        f"grep: {go_quote(query)}\n",
        f"matches: {len(matches)}\n",
    ]
    if index > 0:
        out.append(f"index: {index}\n")
    out.append(f"context_lines: {context_lines}\n")
    out.append("content:\n")
    if not matches or not lines:
        return Result(content="".join(out), warnings=warnings)
    width = len(str(len(lines)))
    last_printed = 0
    for match_index, match_line in enumerate(matches):
        start = max(1, match_line - context_lines)
        end = min(len(lines), match_line + context_lines)
        if last_printed > 0 and start > last_printed + 1:
            out.append("--\n")
        if start <= last_printed:
            start = last_printed + 1
        out.append(f"{match_index + 1}. match: {match_line}\n")
        for number in range(start, end + 1):
            marker = ">" if number == match_line else " "
            out.append(f"{marker} {number:>{width}} | {lines[number - 1]}\n")
        last_printed = end
    return Result(content="".join(out), warnings=warnings)


__all__ = [name for name in dir() if not name.startswith("_")]
