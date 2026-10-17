"""unified diff 渲染（LCS + hunk 切分）。

对应 Go：``internal/utils/fileops/diff.go``。

两个上限与 Go 一致：比较格子数 ``2_000_000``、渲染字节数 ``64 KiB``；超限时
不渲染细节，改为输出 ``# diff omitted: ...`` 说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "DiffHunk",
    "MAX_DIFF_CELLS",
    "MAX_DIFF_OUTPUT_BYTES",
    "build_diff_hunks",
    "diff_lines",
    "diff_too_large",
    "edit_diff",
    "omitted_diff",
    "unified_diff",
]

MAX_DIFF_CELLS = 2_000_000
MAX_DIFF_OUTPUT_BYTES = 64 * 1024

EQUAL = 0
DELETE = 1
INSERT = 2


@dataclass
class DiffHunk:
    """一个 hunk（对应 Go ``DiffHunk``）。"""

    ops: list[tuple[int, str]] = field(default_factory=list)
    old_start: int = 1
    new_start: int = 1


def edit_diff(
    path: str,
    old_text: str,
    new_text: str,
    old_bytes: int,
    new_bytes: int,
    context_lines: int,
    detailed_max_bytes: int = 0,
) -> str:
    """编辑结果 diff：超过 ``detailed_max_bytes`` 时只给省略说明。"""
    if detailed_max_bytes > 0 and (
        old_bytes > detailed_max_bytes or new_bytes > detailed_max_bytes
    ):
        return omitted_diff(
            path,
            f"file exceeds detailed diff limit {detailed_max_bytes} bytes "
            f"(old {old_bytes} bytes, new {new_bytes} bytes)",
        )
    from atri_x.utils.fileops.text import split_lines

    return unified_diff(path, split_lines(old_text), split_lines(new_text), context_lines)


def omitted_diff(path: str, reason: str) -> str:
    """省略版 diff（保持 ``---/+++`` 头）。"""
    return f"--- {path}\n+++ {path}\n# diff omitted: {reason}\n"


def unified_diff(
    path: str, old_lines: list[str], new_lines: list[str], context_lines: int
) -> str:
    """渲染 unified diff。"""
    if diff_too_large(old_lines, new_lines):
        return omitted_diff(
            path,
            f"too many line comparisons ({len(old_lines)} x {len(new_lines)})",
        )
    ops = diff_lines(old_lines, new_lines)
    hunks = build_diff_hunks(ops, context_lines)
    if diff_output_too_large(path, hunks):
        return omitted_diff(path, f"rendered diff exceeds {MAX_DIFF_OUTPUT_BYTES} bytes")
    out: list[str] = [f"--- {path}\n", f"+++ {path}\n"]
    for hunk in hunks:
        old_count, new_count = diff_hunk_counts(hunk.ops)
        out.append(f"@@ -{hunk.old_start},{old_count} +{hunk.new_start},{new_count} @@\n")
        for kind, text in hunk.ops:
            if kind == EQUAL:
                out.append(f" {text}\n")
            elif kind == DELETE:
                out.append(f"-{text}\n")
            else:
                out.append(f"+{text}\n")
    return "".join(out)


def diff_too_large(old_lines: list[str], new_lines: list[str]) -> bool:
    """比较格子数是否超限。"""
    if not old_lines or not new_lines:
        return False
    return len(old_lines) > MAX_DIFF_CELLS // len(new_lines)


def diff_output_too_large(path: str, hunks: list[DiffHunk]) -> bool:
    """渲染结果是否超过 ``MAX_DIFF_OUTPUT_BYTES``。"""
    size = len(path) * 2 + len("--- \n+++ \n")
    for hunk in hunks:
        size += 64
        for _kind, text in hunk.ops:
            size += len(text) + 2
            if size > MAX_DIFF_OUTPUT_BYTES:
                return True
    return size > MAX_DIFF_OUTPUT_BYTES


def diff_lines(a: list[str], b: list[str]) -> list[tuple[int, str]]:
    """LCS 动态规划，返回等长/删除/插入序列（与 Go ``diffLines`` 同序）。"""
    n, m = len(a), len(b)
    width = m + 1
    dp = [0] * ((n + 1) * width)
    for i in range(n - 1, -1, -1):
        base = i * width
        below = base + width
        left_value = a[i]
        for j in range(m - 1, -1, -1):
            if left_value == b[j]:
                dp[base + j] = dp[below + j + 1] + 1
            elif dp[below + j] >= dp[base + j + 1]:
                dp[base + j] = dp[below + j]
            else:
                dp[base + j] = dp[base + j + 1]
    ops: list[tuple[int, str]] = []
    i = j = 0
    while i < n or j < m:
        if i < n and j < m and a[i] == b[j]:
            ops.append((EQUAL, a[i]))
            i += 1
            j += 1
        elif j >= m or (i < n and dp[(i + 1) * width + j] >= dp[i * width + j + 1]):
            ops.append((DELETE, a[i]))
            i += 1
        else:
            ops.append((INSERT, b[j]))
            j += 1
    return ops


def build_diff_hunks(ops: list[tuple[int, str]], context_lines: int) -> list[DiffHunk]:
    """按上下文行数切 hunk（对应 Go ``buildDiffHunks``）。"""
    first_change = -1
    last_change = -1
    for index, op in enumerate(ops):
        if op[0] != EQUAL:
            if first_change == -1:
                first_change = index
            last_change = index
    if first_change == -1:
        return []
    ranges: list[tuple[int, int]] = []
    start = first_change
    last = first_change
    for index in range(first_change + 1, last_change + 1):
        if ops[index][0] == EQUAL:
            continue
        if _equal_ops_between(ops, last + 1, index) > context_lines * 2:
            ranges.append((start, last))
            start = index
        last = index
    ranges.append((start, last))
    return [_trim_diff_context_range(ops, first, last, context_lines) for first, last in ranges]


def _equal_ops_between(ops: list[tuple[int, str]], start: int, end: int) -> int:
    return sum(1 for index in range(start, end) if ops[index][0] == EQUAL)


def _trim_diff_context_range(
    ops: list[tuple[int, str]], first: int, last: int, context_lines: int
) -> DiffHunk:
    start = max(first - context_lines, 0)
    end = min(last + context_lines + 1, len(ops))
    old_start, new_start = 1, 1
    for kind, _text in ops[:start]:
        if kind == EQUAL:
            old_start += 1
            new_start += 1
        elif kind == DELETE:
            old_start += 1
        else:
            new_start += 1
    return DiffHunk(ops=ops[start:end], old_start=old_start, new_start=new_start)


def diff_hunk_counts(ops: list[tuple[int, str]]) -> tuple[int, int]:
    """hunk 的 ``(old_count, new_count)``。"""
    old_count = new_count = 0
    for kind, _text in ops:
        if kind == EQUAL:
            old_count += 1
            new_count += 1
        elif kind == DELETE:
            old_count += 1
        else:
            new_count += 1
    return old_count, new_count


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
UnifiedDiff = unified_diff
