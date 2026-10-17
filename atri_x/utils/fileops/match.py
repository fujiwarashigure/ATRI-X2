"""编辑目标的匹配与候选展示。

对应 Go：``internal/utils/fileops/match.go``。

偏移量在 Python 里是**字符串下标**（Go 用字节下标）；因为匹配串都是合法
UTF-8，两者的切分结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.utils.fileops.text import normalize_edit_text

__all__ = [
    "MATCH_PREVIEW_LIMIT",
    "MATCH_PREVIEW_WIDTH",
    "MatchLocation",
    "SourceLine",
    "find_content_matches",
    "find_line_matches",
    "format_line_range",
    "format_match_locations",
    "match_preview",
    "offset_to_line",
    "select_matches",
    "truncate_line",
]

MATCH_PREVIEW_LIMIT = 5
MATCH_PREVIEW_WIDTH = 40


@dataclass
class MatchLocation:
    """一处匹配。对应 Go ``matchLocation``（偏移为字符串下标）。"""

    byte_start: int = 0
    byte_end: int = 0
    start_line: int = 0
    end_line: int = 0
    preview: str = ""
    line_index: int = 0


@dataclass
class SourceLine:
    """编辑期扫描到的源码行（对应 Go ``editSourceLine``）。

    Go 把它放在 ``edit.go``；Python 放在这里以避免 ``edit`` ↔ ``match`` 的
    循环 import（``find_line_matches`` 的入参就是它）。
    """

    start: int = 0
    end: int = 0
    full_end: int = 0
    text: str = ""


def find_content_matches(text: str, needle: str) -> list[MatchLocation]:
    """找出 ``needle`` 在 ``text`` 中的所有出现（不重叠，按位置递增）。"""
    needle_norm = normalize_edit_text(needle)
    if needle_norm == "":
        return []
    locations: list[MatchLocation] = []
    offset = 0
    while True:
        index = text.find(needle_norm, offset)
        if index < 0:
            break
        start = index
        end = start + len(needle_norm)
        locations.append(
            MatchLocation(
                byte_start=start,
                byte_end=end,
                start_line=offset_to_line(text, start),
                end_line=offset_to_line(text, end - 1),
                preview=match_preview(text, start, end),
            )
        )
        offset = end
    return locations


def find_line_matches(lines: list[SourceLine], needle: str) -> list[MatchLocation]:
    """找出以 ``needle`` 为前缀（忽略前导空白）的行。"""
    locations: list[MatchLocation] = []
    for index, line in enumerate(lines):
        stripped = line.text.lstrip(" \t")
        if stripped.startswith(needle):
            locations.append(
                MatchLocation(
                    byte_start=line.start,
                    byte_end=line.end,
                    start_line=index + 1,
                    end_line=index + 1,
                    preview=truncate_line(stripped),
                    line_index=index,
                )
            )
    return locations


def select_matches(
    locations: list[MatchLocation],
    index: int | None,
    all_matches: bool,
    label: str,
) -> list[MatchLocation]:
    """按 ``index`` / ``all_matches`` 选择匹配；歧义时给出候选列表。"""
    total = len(locations)
    if total == 0:
        raise ValueError(f"{label} not found")
    if index is not None and all_matches:
        raise ValueError("index and all_matches are mutually exclusive")
    if all_matches:
        return locations
    if index is not None:
        if index < 1 or index > total:
            raise ValueError(
                f"index {index} out of range: {total} {label} found, use 1-{total}"
            )
        return [locations[index - 1]]
    if total > 1:
        raise ValueError(
            f"{label} matched {total} locations; provide a longer {label}, "
            f"index, or all_matches:\n{format_match_locations(locations)}"
        )
    return locations


def format_match_locations(locations: list[MatchLocation]) -> str:
    """渲染候选匹配（最多 5 条，其余折叠计数）。"""
    limit = len(locations)
    if limit > MATCH_PREVIEW_LIMIT:
        limit = MATCH_PREVIEW_LIMIT
    out: list[str] = []
    for position in range(limit):
        location = locations[position]
        out.append(
            f'  #{position + 1} {format_line_range(location)} "{location.preview}"\n'
        )
    if len(locations) > limit:
        out.append(f"  ...and {len(locations) - limit} more\n")
    return "".join(out)


def format_line_range(location: MatchLocation) -> str:
    """``L12`` 或 ``L12-15``。"""
    if location.start_line == location.end_line:
        return f"L{location.start_line}"
    return f"L{location.start_line}-{location.end_line}"


def offset_to_line(text: str, offset: int) -> int:
    """下标 → 1-based 行号（对应 Go ``byteOffsetToLine``）。"""
    if offset < 0:
        offset = 0
    if offset > len(text):
        offset = len(text)
    return text.count("\n", 0, offset) + 1


def match_preview(text: str, start: int, end: int) -> str:
    """从匹配所在行的行首截到匹配结尾，作为可读预览。"""
    line_start = start
    while line_start > 0 and text[line_start - 1] != "\n":
        line_start -= 1
    return truncate_line(text[line_start:end])


def truncate_line(value: str) -> str:
    """把换行转义并截断到 40 个字符。

    Go 在这里按**字节**截断（可能切坏多字节字符）；Python 按字符截断，是有意
    的修正。
    """
    value = (value or "").replace("\n", "\\n")
    if len(value) > MATCH_PREVIEW_WIDTH:
        return value[:MATCH_PREVIEW_WIDTH] + "..."
    return value
