"""SKILL.md 的 front matter 解析与风险等级解析。

对应 Go：``internal/tool/skill/parser.go``。

只做纯文本处理：YAML front matter 走一个刻意最小化的解析器（Go 侧就是这样，
只认 ``key: value`` 与续行），正文即详情。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.tool.tool import (
    RiskCritical,
    RiskHigh,
    RiskLow,
    RiskMedium,
    RiskSafe,
)

__all__ = [
    "Definition",
    "first_markdown_paragraph",
    "join_sentences",
    "parse_front_matter",
    "parse_risk",
    "parse_skill_markdown",
    "split_front_matter",
    "trim_scalar",
]

#: Go 的 ``tool.RiskLevel`` 合法取值。
_RISK_LEVELS = (RiskSafe, RiskLow, RiskMedium, RiskHigh, RiskCritical)


@dataclass
class Definition:
    """一份 SKILL.md 的解析结果。对应 Go ``Definition``。"""

    name: str = ""
    description: str = ""
    when_to_use: str = ""
    risk: str = ""
    detail: str = ""
    format: str = ""


def parse_skill_markdown(raw: bytes | str, fallback_name: str) -> Definition:
    """解析 SKILL.md。对应 Go ``ParseSkillMarkdown``。

    名字缺失时退回目录名；描述缺失时取正文首段；``when_to_use`` 会拼到描述
    后面；正文为空时退回描述。名字最终仍为空则抛 ``ValueError``。
    """
    text = _decode(raw).replace("\r\n", "\n")
    front, body = split_front_matter(text)
    fields = parse_front_matter(front)
    definition = Definition(
        name=(fields.get("name") or "").strip(),
        description=(fields.get("description") or "").strip(),
        when_to_use=(fields.get("when_to_use") or "").strip(),
        detail=body.strip(),
        format="markdown",
    )
    if definition.name == "":
        definition.name = (fallback_name or "").strip()
    if definition.name == "":
        raise ValueError("skill name is required")
    if definition.description == "":
        definition.description = first_markdown_paragraph(definition.detail)
    if definition.when_to_use != "":
        definition.description = join_sentences(
            definition.description, definition.when_to_use
        )
    if definition.detail == "":
        definition.detail = definition.description
    return definition


def parse_risk(value: str) -> str:
    """解析风险等级。对应 Go ``parseRisk``。

    空值默认 ``high``（skill 默认按高风险处理）；非法值抛 ``ValueError``。
    注意与 :func:`atri_x.tool.tool.parse_risk` 不同：那个是「归一化到 fallback」，
    这里是「非法即报错」。
    """
    value = (value or "").strip()
    if value == "":
        return RiskHigh
    if value in _RISK_LEVELS:
        return value
    raise ValueError(f"invalid skill risk {value!r}")


def split_front_matter(text: str) -> tuple[str, str]:
    """拆出 ``---`` 围起来的 front matter。对应 Go ``splitFrontMatter``。"""
    if not text.startswith("---\n"):
        return "", text
    rest = text[len("---\n") :]
    index = rest.find("\n---")
    if index < 0:
        return "", text
    front = rest[:index]
    body = rest[index + len("\n---") :]
    if body.startswith("\n"):
        body = body[1:]
    return front, body


def parse_front_matter(front: str) -> dict[str, str]:
    """极简 ``key: value`` 解析（含缩进续行）。对应 Go ``parseFrontMatter``。"""
    fields: dict[str, str] = {}
    current = ""
    for line in front.split("\n"):
        trimmed = line.strip()
        if trimmed == "" or trimmed.startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if current != "":
                fields[current] = (fields[current] + " " + trimmed).strip()
            continue
        key, sep, value = trimmed.partition(":")
        if not sep:
            current = ""
            continue
        key = key.strip()
        if key == "":
            current = ""
            continue
        current = key
        fields[key] = trim_scalar(value)
    return fields


def trim_scalar(value: str) -> str:
    """去掉成对的引号。对应 Go ``trimScalar``。"""
    value = (value or "").strip()
    if len(value) >= 2:
        quote = value[0]
        if quote in ("'", '"') and value[-1] == quote:
            return value[1:-1].strip()
    return value


def first_markdown_paragraph(markdown: str) -> str:
    """取第一段非空正文（去掉行首 ``#``）。对应 Go ``firstMarkdownParagraph``。"""
    for block in (markdown or "").split("\n\n"):
        block = block.strip()
        if block == "":
            continue
        lines: list[str] = []
        for line in block.split("\n"):
            line = line.strip().removeprefix("#").strip()
            if line != "":
                lines.append(line)
        if lines:
            return " ".join(lines)
    return ""


def join_sentences(first: str, second: str) -> str:
    """用空格拼接两段文本。对应 Go ``joinSentences``。"""
    first = (first or "").strip()
    second = (second or "").strip()
    if first == "":
        return second
    if second == "":
        return first
    return first + " " + second


def _decode(raw: bytes | str) -> str:
    """Go 的 ``string(raw)`` 语义：字节当 UTF-8 文本，坏字节替换。"""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw or ""
