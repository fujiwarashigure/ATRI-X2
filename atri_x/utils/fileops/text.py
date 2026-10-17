"""文本规范化、行号与内容 revision。

对应 Go：``internal/utils/fileops/text.go``。

所有编辑都基于"LF 归一化后"的文本；写回时再按文件原有的行尾还原。
``content_revision`` 取 sha256 前 8 字节的十六进制（16 个字符），与 Go 一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONTENT_REVISION_BYTES",
    "DEFAULT_READ_LINE_LIMIT",
    "LineNumber",
    "content_revision",
    "detect_line_ending",
    "ensure_trailing_newline",
    "looks_binary_utf8",
    "normalize_context_lines",
    "normalize_edit_text",
    "normalize_grep_context_lines",
    "normalize_max_matches",
    "normalize_read_range",
    "restore_line_endings",
    "split_lines",
    "validate_line_range",
]

DEFAULT_READ_LINE_LIMIT = 200
CONTENT_REVISION_BYTES = 8


@dataclass
class LineNumber:
    """行号或 ``"end"``。对应 Go ``fileops.LineNumber``。"""

    value: int = 0
    end: bool = False

    @classmethod
    def from_json(cls, data: Any) -> "LineNumber":
        """对应 Go 的 ``LineNumber.UnmarshalJSON``。"""
        if data is None:
            return cls()
        if isinstance(data, str):
            text = data.strip()
            if text == "" or text.lower() == "null":
                return cls()
            lowered = text.lower()
            if lowered == "end":
                return cls(value=0, end=True)
            try:
                return cls(value=int(lowered))
            except ValueError as exc:
                raise ValueError('line number string must be integer or "end"') from exc
        if isinstance(data, int):
            return cls(value=int(data))
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            parsed = data
        if isinstance(parsed, int):
            return cls(value=int(parsed))
        if isinstance(parsed, str):
            return cls.from_json(parsed)
        raise ValueError('line number must be integer or "end"')


def normalize_read_range(
    total: int, start: int, end_line: LineNumber
) -> tuple[int, int, bool]:
    """把读取请求归一成 ``(start, end, truncated)``（1-based，含两端）。"""
    if total == 0:
        return 1, 0, False
    if start <= 0:
        start = 1
    if start > total:
        raise ValueError(f"start_line {start} exceeds total lines {total}")
    truncated = False
    end = end_line.value
    if end_line.end:
        end = total
    elif end <= 0:
        end = start + DEFAULT_READ_LINE_LIMIT - 1
        truncated = end < total
    if end > total:
        end = total
    if end < start:
        raise ValueError("end_line must be >= start_line")
    if not end_line.end and end - start + 1 > DEFAULT_READ_LINE_LIMIT:
        end = start + DEFAULT_READ_LINE_LIMIT - 1
        truncated = True
    return start, end, truncated


def ensure_trailing_newline(text: str) -> str:
    """归一化行尾并在末尾补一个 ``\\n``。"""
    text = normalize_edit_text(text)
    if text == "" or text.endswith("\n"):
        return text
    return text + "\n"


def normalize_edit_text(text: str) -> str:
    """把 CRLF / CR 统一成 LF。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, line_ending: str) -> str:
    """按原文件行尾还原。"""
    if not line_ending or line_ending == "\n":
        return text
    return text.replace("\n", line_ending)


def normalize_grep_context_lines(value: int) -> int:
    """grep 上下文行数：缺省 2，上限 20。"""
    if value < 0:
        return 0
    if value == 0:
        return 2
    return min(value, 20)


def normalize_max_matches(value: int) -> int:
    """最大匹配数：缺省 20，上限 100。"""
    if value <= 0:
        return 20
    return min(value, 100)


def normalize_context_lines(value: int) -> int:
    """diff 上下文行数：缺省 3，上限 20。"""
    if value < 0:
        return 0
    if value == 0:
        return 3
    return min(value, 20)


def validate_line_range(total: int, start: int, end: int) -> None:
    """校验 1-based 行区间是否落在文件内。"""
    if start <= 0:
        raise ValueError("line must be >= 1")
    if end < start:
        raise ValueError("end_line must be >= line")
    if total == 0:
        raise ValueError("file has no lines")
    if start > total or end > total:
        raise ValueError(f"line range {start}-{end} exceeds total lines {total}")


def split_lines(text: str) -> list[str]:
    """按行切分（归一化行尾，末尾换行不产生空行）。"""
    text = normalize_edit_text(text)
    if text == "":
        return []
    if text.endswith("\n"):
        text = text[:-1]
    if text == "":
        return [""]
    return text.split("\n")


def detect_line_ending(text: str) -> str:
    """探测行尾（优先 CRLF）。"""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def looks_binary_utf8(text: str) -> bool:
    """前 4096 字节里是否含 NUL（与 Go 的判定窗口一致）。"""
    return b"\x00" in text.encode("utf-8", errors="replace")[:4096]


def content_revision(data: bytes) -> str:
    """内容 revision：sha256 前 8 字节的十六进制。"""
    return hashlib.sha256(data or b"").hexdigest()[: CONTENT_REVISION_BYTES * 2]
