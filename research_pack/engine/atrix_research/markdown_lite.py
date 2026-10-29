"""极简 Markdown 解析与 HTML 渲染（标准库实现）。

为什么自己写：引擎的核心路径不能依赖第三方 Markdown 库，而导出只需要
"论文里会用到的子集"——标题、段落、列表、引用块、表格、行内粗斜体/代码/链接。
解析结果是 :class:`Block` 列表，DOCX / PDF / HTML 三个导出器共用同一份 AST，
避免"三种格式各解析一遍、各错一处"。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["Block", "inline_html", "inline_runs", "parse", "render_html", "render_plain"]

BlockKind = Literal["heading", "para", "bullet", "ordered", "quote", "table", "hr", "code"]


@dataclass
class Block:
    kind: BlockKind
    level: int = 0
    text: str = ""
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    header: bool = False


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def parse(markdown: str) -> list[Block]:
    """把 Markdown 子集解析成块列表。"""
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            fence = stripped[:3]
            index += 1
            buffer: list[str] = []
            while index < total and not lines[index].strip().startswith(fence):
                buffer.append(lines[index])
                index += 1
            index += 1
            blocks.append(Block("code", text="\n".join(buffer)))
            continue

        heading = _HEADING.match(stripped)
        if heading:
            blocks.append(Block("heading", level=len(heading.group(1)), text=heading.group(2).strip()))
            index += 1
            continue

        if _HR.match(stripped):
            blocks.append(Block("hr"))
            index += 1
            continue

        # 表格：标题行 + 分隔行
        if "|" in stripped and index + 1 < total and _TABLE_SEP.match(lines[index + 1]):
            rows = [_split_row(stripped)]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            blocks.append(Block("table", rows=rows, header=True))
            continue

        if _UL.match(line):
            items = []
            while index < total:
                match = _UL.match(lines[index])
                if not match:
                    break
                items.append(match.group(1).strip())
                index += 1
            blocks.append(Block("bullet", items=items))
            continue

        if _OL.match(line):
            items = []
            while index < total:
                match = _OL.match(lines[index])
                if not match:
                    break
                items.append(match.group(2).strip())
                index += 1
            blocks.append(Block("ordered", items=items))
            continue

        if stripped.startswith(">"):
            buffer = []
            while index < total and lines[index].strip().startswith(">"):
                buffer.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(Block("quote", text=" ".join(buffer).strip()))
            continue

        # 段落：连续非空行，遇到块级标记停下
        buffer = [stripped]
        index += 1
        while index < total:
            candidate = lines[index].strip()
            if not candidate:
                break
            if (
                _HEADING.match(candidate)
                or _UL.match(lines[index])
                or _OL.match(lines[index])
                or candidate.startswith(">")
                or candidate.startswith("```")
                or _HR.match(candidate)
            ):
                break
            buffer.append(candidate)
            index += 1
        blocks.append(Block("para", text=" ".join(buffer)))
    return blocks


# ---------------------------------------------------------------------------
# 行内样式
# ---------------------------------------------------------------------------

_INLINE = re.compile(
    r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|`[^`]+`|\[[^\]]+\]\([^)]+\))"
)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline_runs(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把行内 Markdown 拆成 ``(文本, 样式)`` 片段，供 DOCX 使用。"""
    runs: list[tuple[str, dict[str, Any]]] = []
    position = 0
    for match in _INLINE.finditer(text or ""):
        if match.start() > position:
            runs.append((text[position : match.start()], {}))
        token = match.group(0)
        if token.startswith("**") or token.startswith("__"):
            runs.append((token[2:-2], {"bold": True}))
        elif token.startswith("`"):
            runs.append((token[1:-1], {"code": True}))
        elif token.startswith("["):
            link = _LINK.match(token)
            if link:
                runs.append((link.group(1), {"link": link.group(2)}))
            else:
                runs.append((token, {}))
        else:
            runs.append((token[1:-1], {"italic": True}))
        position = match.end()
    if position < len(text or ""):
        runs.append((text[position:], {}))
    return [(chunk, style) for chunk, style in runs if chunk]


def inline_html(text: str) -> str:
    """行内 Markdown → HTML（先转义，再加标签，避免注入）。"""
    output: list[str] = []
    position = 0
    for match in _INLINE.finditer(text or ""):
        if match.start() > position:
            output.append(html.escape(text[position : match.start()]))
        token = match.group(0)
        if token.startswith("**") or token.startswith("__"):
            output.append(f"<strong>{html.escape(token[2:-2])}</strong>")
        elif token.startswith("`"):
            output.append(f"<code>{html.escape(token[1:-1])}</code>")
        elif token.startswith("["):
            link = _LINK.match(token)
            if link:
                output.append(
                    f'<a href="{html.escape(link.group(2), quote=True)}">{html.escape(link.group(1))}</a>'
                )
            else:
                output.append(html.escape(token))
        else:
            output.append(f"<em>{html.escape(token[1:-1])}</em>")
        position = match.end()
    if position < len(text or ""):
        output.append(html.escape(text[position:]))
    return "".join(output)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "Noto Sans SC", sans-serif;
       max-width: 860px; margin: 2.4rem auto; padding: 0 1.2rem; line-height: 1.75; color: #1f2328; }
h1 { font-size: 1.9rem; border-bottom: 2px solid #d0d7de; padding-bottom: .4rem; }
h2 { font-size: 1.45rem; margin-top: 2rem; border-bottom: 1px solid #e5e7eb; padding-bottom: .25rem; }
h3 { font-size: 1.15rem; margin-top: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .92rem; }
th, td { border: 1px solid #d0d7de; padding: .45rem .6rem; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
blockquote { margin: 1rem 0; padding: .6rem 1rem; border-left: 4px solid #d0d7de;
             background: #f6f8fa; color: #4b5563; }
code { background: #f3f4f6; padding: .1rem .3rem; border-radius: 4px; font-size: .9em; }
pre { background: #f6f8fa; padding: .8rem 1rem; overflow-x: auto; border-radius: 6px; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 2rem 0; }
"""


def render_html(blocks: list[Block], *, title: str = "", css: str | None = None) -> str:
    """把块列表渲染成自包含 HTML 文档。"""
    parts: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            level = min(max(block.level, 1), 6)
            parts.append(f"<h{level}>{inline_html(block.text)}</h{level}>")
        elif block.kind == "para":
            parts.append(f"<p>{inline_html(block.text)}</p>")
        elif block.kind == "bullet":
            items = "".join(f"<li>{inline_html(item)}</li>" for item in block.items)
            parts.append(f"<ul>{items}</ul>")
        elif block.kind == "ordered":
            items = "".join(f"<li>{inline_html(item)}</li>" for item in block.items)
            parts.append(f"<ol>{items}</ol>")
        elif block.kind == "quote":
            parts.append(f"<blockquote>{inline_html(block.text)}</blockquote>")
        elif block.kind == "code":
            parts.append(f"<pre><code>{html.escape(block.text)}</code></pre>")
        elif block.kind == "hr":
            parts.append("<hr>")
        elif block.kind == "table" and block.rows:
            head = block.rows[0]
            body = block.rows[1:]
            thead = "".join(f"<th>{inline_html(cell)}</th>" for cell in head)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{inline_html(cell)}</td>" for cell in row) + "</tr>"
                for row in body
            )
            parts.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
    body = "\n".join(parts)
    safe_title = html.escape(title or "ATRI-X 研究成果")
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{safe_title}</title>\n<style>{css or _CSS}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def render_plain(blocks: list[Block]) -> str:
    """纯文本渲染（无 markdown 库时的兜底与调试用）。"""
    lines: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            lines.append(("  " * (block.level - 1)) + block.text)
            lines.append("")
        elif block.kind == "para":
            lines.append(block.text)
            lines.append("")
        elif block.kind in ("bullet", "ordered"):
            for index, item in enumerate(block.items, start=1):
                prefix = "- " if block.kind == "bullet" else f"{index}. "
                lines.append(prefix + item)
            lines.append("")
        elif block.kind == "quote":
            lines.append("> " + block.text)
            lines.append("")
        elif block.kind == "table":
            for row in block.rows:
                lines.append(" | ".join(row))
            lines.append("")
        elif block.kind == "code":
            lines.append(block.text)
            lines.append("")
        elif block.kind == "hr":
            lines.append("-----")
    return "\n".join(lines).rstrip() + "\n"
