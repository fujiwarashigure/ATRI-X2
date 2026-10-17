"""指令前缀识别与补全 token 解析。

对应 Go：``internal/directive/tool.go``。

支持的指令前缀（半角 ``:`` 与全角 ``：`` 等价）：

- ``@tool:`` / ``@t:``   工具
- ``@skill:`` / ``@s:``  技能
- ``@char:`` / ``@c:``   角色
- ``@model:`` / ``@m:``  模型 profile
- ``@image:`` / ``@img:`` 生图 profile
- ``@use:``              工具 profile

注意：Go 侧 ``ToolMatch.Start`` / ``End`` 是**字节**偏移，Python 侧统一用
**字符**偏移（调用方切片时也用字符偏移，语义自洽）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# -- 前缀常量（对齐 Go 的 const 块） ----------------------------------------
TOOL_PREFIX = "@tool:"
TOOL_FULL_PREFIX = "@tool："
TOOL_SHORT_PREFIX = "@t:"
TOOL_SHORT_FULL = "@t："

SKILL_PREFIX = "@skill:"
SKILL_FULL_PREFIX = "@skill："
SKILL_SHORT_PREFIX = "@s:"
SKILL_SHORT_FULL = "@s："

CHARACTER_PREFIX = "@char:"
CHARACTER_FULL_PREFIX = "@char："
CHARACTER_SHORT_PREFIX = "@c:"
CHARACTER_SHORT_FULL = "@c："

MODEL_PROFILE_PREFIX = "@model:"
MODEL_PROFILE_FULL_PREFIX = "@model："
MODEL_PROFILE_SHORT_PREFIX = "@m:"
MODEL_PROFILE_SHORT_FULL = "@m："

IMAGE_PROFILE_PREFIX = "@image:"
IMAGE_PROFILE_FULL_PREFIX = "@image："
IMAGE_PROFILE_SHORT_PREFIX = "@img:"
IMAGE_PROFILE_SHORT_FULL = "@img："

TOOL_PROFILE_PREFIX = "@use:"
TOOL_PROFILE_FULL_PREFIX = "@use："

# -- 正则（对齐 Go 的 var 块；长选项放前面避免短选项先命中） ----------------
TOOL_PATTERN = re.compile(r"@(?:tool|t)[:：]([A-Za-z0-9_.-]+)")
SKILL_PATTERN = re.compile(r"@(?:skill|s)[:：]([A-Za-z0-9_.-]+)")
CHARACTER_PATTERN = re.compile(r"@(?:char|c)[:：]([A-Za-z0-9_.-]+)")
MODEL_PROFILE_PATTERN = re.compile(r"@(?:model|m)[:：]([A-Za-z0-9_.-]+)")
IMAGE_PROFILE_PATTERN = re.compile(r"@(?:image|img)[:：]([A-Za-z0-9_.-]+)")
TOOL_PROFILE_PATTERN = re.compile(r"@use[:：]([A-Za-z0-9_.-]+)")


@dataclass
class ToolMatch:
    """一次指令命中。对应 Go ``ToolMatch``。"""

    start: int = 0
    end: int = 0
    name: str = ""


# Go 里这些是类型别名，Python 直接用同一类。
SkillMatch = ToolMatch
CharacterMatch = ToolMatch
ModelProfileMatch = ToolMatch
ImageProfileMatch = ToolMatch
ToolProfileMatch = ToolMatch


@dataclass
class ToolCompletionToken:
    """补全输入框里正在输入的一个指令 token。对应 Go ``ToolCompletionToken``。"""

    start: int = 0
    query: str = ""
    prefix: str = ""
    prefix_only: bool = False
    ok: bool = False


SkillCompletionToken = ToolCompletionToken


def tool_matches(text: str) -> list[ToolMatch]:
    """对应 Go ``ToolMatches``。"""
    return _matches(text, TOOL_PATTERN)


def skill_matches(text: str) -> list[ToolMatch]:
    return _matches(text, SKILL_PATTERN)


def character_matches(text: str) -> list[ToolMatch]:
    return _matches(text, CHARACTER_PATTERN)


def model_profile_matches(text: str) -> list[ToolMatch]:
    return _matches(text, MODEL_PROFILE_PATTERN)


def image_profile_matches(text: str) -> list[ToolMatch]:
    return _matches(text, IMAGE_PROFILE_PATTERN)


def tool_profile_matches(text: str) -> list[ToolMatch]:
    return _matches(text, TOOL_PROFILE_PATTERN)


def _matches(text: str, pattern: re.Pattern[str]) -> list[ToolMatch]:
    out: list[ToolMatch] = []
    for m in pattern.finditer(text):
        out.append(ToolMatch(start=m.start(), end=m.end(), name=m.group(1)))
    return out


def strip_tool_matches(text: str, matches: list[ToolMatch], remove: list[bool]) -> str:
    """删除指定的指令命中，并把剩余文本按空白折叠。

    对应 Go ``StripToolMatches``（用 ``strings.Fields`` + ``Join`` 归一空白）。
    """
    pieces: list[str] = []
    last = 0
    for i, match in enumerate(matches):
        if i >= len(remove) or not remove[i]:
            continue
        pieces.append(text[last:match.start])
        last = match.end
    pieces.append(text[last:])
    return " ".join("".join(pieces).split())


def parse_tool_completion_token(text: str, cursor: int) -> ToolCompletionToken:
    """对应 Go ``ParseToolCompletionToken``。"""
    return _parse_completion_token(
        text, cursor, [TOOL_SHORT_PREFIX, TOOL_SHORT_FULL, TOOL_PREFIX, TOOL_FULL_PREFIX]
    )


def parse_skill_completion_token(text: str, cursor: int) -> ToolCompletionToken:
    return _parse_completion_token(
        text, cursor, [SKILL_SHORT_PREFIX, SKILL_SHORT_FULL, SKILL_PREFIX, SKILL_FULL_PREFIX]
    )


def parse_character_completion_token(text: str, cursor: int) -> ToolCompletionToken:
    return _parse_completion_token(
        text,
        cursor,
        [CHARACTER_SHORT_PREFIX, CHARACTER_SHORT_FULL, CHARACTER_PREFIX, CHARACTER_FULL_PREFIX],
    )


def _parse_completion_token(
    text: str, cursor: int, prefixes: list[str]
) -> ToolCompletionToken:
    """对应 Go ``parseCompletionToken``。

    ``cursor`` 是字符偏移；越界时夹到文本末尾（与 Go 一致）。
    """
    if cursor < 0 or cursor > len(text):
        cursor = len(text)
    start = cursor
    while start > 0:
        if _is_space(text[start - 1]):
            break
        start -= 1
    token = text[start:cursor]
    if not token or token[0] != "@":
        return ToolCompletionToken()
    for prefix in prefixes:
        # 只输入了前缀的一部分（例如 "@to"）：交给调用方列出全部候选。
        if token != prefix and prefix.startswith(token):
            return ToolCompletionToken(start=start, prefix=prefix, prefix_only=True, ok=True)
        if token.startswith(prefix):
            query = token[len(prefix):]
            for ch in query:
                if not is_tool_name_char(ch):
                    return ToolCompletionToken()
            return ToolCompletionToken(start=start, query=query, prefix=prefix, ok=True)
    return ToolCompletionToken()


def is_tool_name_char(c: str) -> bool:
    """对应 Go ``IsToolNameByte``。"""
    return (
        ("a" <= c <= "z")
        or ("A" <= c <= "Z")
        or ("0" <= c <= "9")
        or c in ("_", "-", ".")
    )


# 历史别名（Go 里函数名含 Byte，Python 用 char 更准确，两个名字都提供）
IsToolNameByte = is_tool_name_char


def _is_space(c: str) -> bool:
    """对应 Go ``isSpace``：只认这四个 ASCII 空白，不用 ``str.isspace()``。"""
    return c in (" ", "\t", "\n", "\r")


__all__ = [name for name in dir() if not name.startswith("_")]
