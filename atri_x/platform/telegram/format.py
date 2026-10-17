"""Markdown → Telegram 实体文本（HTML）渲染。

对应 Go：``internal/platform/telegram/format.go``。

渲染管线与上游逐条一致：代码块 → 表格 → 引用块 → 单行（分割线 / 标题 / 行内），
行内按 ``行内代码 → 粗体 → 斜体 → 删除线 → 剧透`` 的顺序替换。

与 Go 的两处形式差异（行为等价）：

* ``html.EscapeString`` 用本模块的 :func:`escape_html` 复刻：Go 的替换表是
  ``&``→``&amp;``、``'``→``&#39;``、``<``→``&lt;``、``>``→``&gt;``、``"``→``&#34;``
  （**不是** Python ``html.escape`` 的 ``&quot;`` / ``&#x27;``），因此不能直接用标准库。
* Go 的 ``len(s)`` 是字节数、``[]rune`` 是码点数；本模块的 ``len(s)`` 就是码点数
  （Python ``str`` 天然按码点），在判长/补宽处与 ``[]rune`` 口径一致。

**已知上游缺陷（本次保真保留）**：:func:`telegram_html_from_markdown` 对整段文本
做 5 条正则替换，Telegram 流式编辑每次调用都付全量代价，累计 O(n²)
（见《ATRI-X Python/Go 研究报告》对
``internal/platform/telegram/stream.go:44,45,50,69,147-166`` 的记录）。
本次移植不改成增量渲染。
"""

from __future__ import annotations

import re

#: Go ``html.EscapeString`` 的替换表。
_HTML_ESCAPES = {
    ord("&"): "&amp;",
    ord("'"): "&#39;",
    ord("<"): "&lt;",
    ord(">"): "&gt;",
    ord('"'): "&#34;",
}

BOLD_PATTERN = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
ITALIC_PATTERN = re.compile(r"\*([^*]+)\*|_([^_]+)_")
STRIKE_PATTERN = re.compile(r"~~([^~]+)~~")
SPOILER_PATTERN = re.compile(r"\|\|([^|]+)\|\|")
INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")
CODE_FENCE_START_PATTERN = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")

#: 分割线的可读替代（对应 Go ``formatMarkdownLine`` 里的字面量）。
HORIZONTAL_RULE = "\u2500" * 8


def escape_html(text: str) -> str:
    """对应 Go ``html.EscapeString``（注意 ``"`` → ``&#34;``、``'`` → ``&#39;``）。"""
    return (text or "").translate(_HTML_ESCAPES)


def telegram_html_from_markdown(text: str) -> str:
    """把常见 Markdown 轻量转换成 Telegram HTML。对应 Go ``telegramHTMLFromMarkdown``。"""
    text = (text or "").replace("\r\n", "\n")
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        lang, is_fence = code_fence_start(lines[i])
        if is_fence:
            block: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                block.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code = escape_html("\n".join(block))
            if lang:
                out.append(
                    '<pre><code class="language-' + escape_html(lang) + '">' + code + "</code></pre>"
                )
            else:
                out.append("<pre>" + code + "</pre>")
            continue
        if is_table_start(lines, i):
            start = i
            while i < len(lines) and "|" in lines[i] and lines[i].strip() != "":
                i += 1
            out.append("<pre>" + escape_html(format_markdown_table(lines[start:i])) + "</pre>")
            continue
        if is_quote_line(lines[i]):
            quotes: list[str] = []
            while i < len(lines) and is_quote_line(lines[i]):
                quotes.append(lines[i].strip().removeprefix(">").strip())
                i += 1
            out.append("<blockquote>" + format_inline_html("\n".join(quotes)) + "</blockquote>")
            continue
        out.append(format_markdown_line(lines[i]))
        i += 1
    return "\n".join(out)


def plain_text_from_markdown(text: str) -> str:
    """纯文本模式：上游原样返回。对应 Go ``plainTextFromMarkdown``。"""
    return text


def code_fence_start(line: str) -> tuple[str, bool]:
    """识别代码围栏起始行，返回 ``(语言, 是否围栏)``。对应 Go ``codeFenceStart``。"""
    match = CODE_FENCE_START_PATTERN.fullmatch((line or "").strip())
    if match is None:
        return "", False
    return match.group(1), True


def format_markdown_line(line: str) -> str:
    """渲染一行（分割线 / 标题 / 行内）。对应 Go ``formatMarkdownLine``。"""
    trimmed = (line or "").strip()
    if trimmed == "":
        return ""
    if is_horizontal_rule(trimmed):
        return HORIZONTAL_RULE
    if trimmed.startswith("#"):
        level = 0
        while level < len(trimmed) and trimmed[level] == "#":
            level += 1
        if 0 < level <= 6 and level < len(trimmed) and trimmed[level] == " ":
            return "<b>" + format_inline_html(trimmed[level:].strip()) + "</b>"
    return format_inline_html(line)


def format_inline_html(text: str) -> str:
    """行内实体渲染。对应 Go ``formatInlineHTML``。"""
    text = escape_html(text)
    text = INLINE_CODE_PATTERN.sub(r"<code>\1</code>", text)
    text = BOLD_PATTERN.sub(lambda m: "<b>" + first_regex_group(m.groups()) + "</b>", text)
    text = ITALIC_PATTERN.sub(lambda m: "<i>" + first_regex_group(m.groups()) + "</i>", text)
    text = STRIKE_PATTERN.sub(r"<s>\1</s>", text)
    text = SPOILER_PATTERN.sub(r"<tg-spoiler>\1</tg-spoiler>", text)
    return text


def first_regex_group(groups: tuple[str, ...]) -> str:
    """取第一个非空捕获组。对应 Go ``firstRegexGroup``。"""
    for part in groups:
        if part:
            return part
    return ""


def is_horizontal_rule(line: str) -> bool:
    """对应 Go ``isHorizontalRule``。"""
    if len(line) < 3:
        return False
    for ch in line:
        if ch not in "-_*":
            return False
    return True


def is_quote_line(line: str) -> bool:
    """对应 Go ``isQuoteLine``。"""
    return (line or "").strip().startswith(">")


def is_table_start(lines: list[str], index: int) -> bool:
    """对应 Go ``isTableStart``。"""
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and is_markdown_table_separator(lines[index + 1])


def is_markdown_table_separator(line: str) -> bool:
    """对应 Go ``isMarkdownTableSeparator``。"""
    line = (line or "").strip()
    if "|" not in line:
        return False
    line = line.strip("|")
    for cell in line.split("|"):
        cell = cell.strip().strip(":")
        if len(cell) < 3 or cell.strip("-") != "":
            return False
    return True


def format_markdown_table(lines: list[str]) -> str:
    """把 Markdown 表格渲染成等宽文本（Telegram 里放在 ``<pre>`` 内）。"""
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        if index == 1 and is_markdown_table_separator(line):
            continue
        cells = split_markdown_table_row(line)
        if cells:
            rows.append(cells)
    if not rows:
        return "\n".join(lines)
    widths: list[int] = []
    for row in rows:
        for column, cell in enumerate(row):
            if column >= len(widths):
                widths.append(0)
            if len(cell) > widths[column]:
                widths[column] = len(cell)
    out: list[str] = []
    for row in rows:
        cells = []
        for column in range(len(widths)):
            cell = row[column] if column < len(row) else ""
            cells.append(pad_right(cell, widths[column]))
        out.append("  ".join(cells))
    return "\n".join(out)


def split_markdown_table_row(line: str) -> list[str]:
    """对应 Go ``splitMarkdownTableRow``。"""
    line = (line or "").strip().strip("|")
    return [part.strip() for part in line.split("|")]


def pad_right(text: str, width: int) -> str:
    """按码点数右侧补空格。对应 Go ``padRight``。"""
    while len(text) < width:
        text += " "
    return text


__all__ = [name for name in dir() if not name.startswith("_")]
