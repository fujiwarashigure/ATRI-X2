"""导出器：Markdown → HTML / DOCX / PDF。

原则：

* 三个格式共用 :mod:`markdown_lite` 的 AST，排版差异只体现在渲染层；
* 第三方库（python-docx / reportlab）缺失时**降级**而不是失败：仍然产出
  HTML/Markdown，并在结果里写明缺哪个库、怎么装；
* 导出结果逐文件回报字节数与状态，方便 AgentSkill 把"实际交付了什么"说清楚。
"""

from __future__ import annotations

import os
from typing import Any

from .errors import ARG_INVALID, DEPENDENCY_MISSING, ResearchError
from .markdown_lite import Block, inline_runs, parse, render_html

__all__ = ["EXPORT_FORMATS", "export_document"]

EXPORT_FORMATS = ("md", "html", "docx", "pdf")

#: 中文字体回退链（DOCX 用 eastAsia 属性；PDF 用 reportlab 的 CID 字体）。
_CJK_FONT_CANDIDATES = ("Microsoft YaHei", "SimSun", "Noto Sans CJK SC", "Source Han Sans SC")


def _write_bytes(path: str, payload: bytes) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return len(payload)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _docx_available() -> bool:
    try:
        import docx  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何导入失败都视为不可用
        return False
    return True


def _docx_setup(document: Any) -> None:
    """设置正文与标题的中英文字体。"""
    from docx.shared import Pt

    try:
        style = document.styles["Normal"]
        style.font.name = "Times New Roman"
        style.font.size = Pt(10.5)
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
        if fonts is None:
            from docx.oxml import OxmlElement

            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        fonts.set(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia",
            _CJK_FONT_CANDIDATES[0],
        )
    except Exception:  # noqa: BLE001 - 字体设置失败不应中断导出
        pass


def _docx_add_runs(paragraph: Any, text: str) -> None:
    for chunk, style in inline_runs(text):
        run = paragraph.add_run(chunk)
        if style.get("bold"):
            run.bold = True
        if style.get("italic"):
            run.italic = True
        if style.get("code"):
            run.font.name = "Consolas"
        if style.get("link"):
            # python-docx 没有公开的超链接 API；这里保留文本，URL 交给脚注/参考文献
            run.font.underline = True


def _docx_render(blocks: list[Block], out_path: str, *, title: str = "") -> int:
    """优先用 python-docx（排版更全）；没有该库时用零依赖写入器，功能不降级。"""
    if not _docx_available():
        from .docx_writer import write_docx

        return write_docx(blocks, out_path, title=title)
    return _docx_render_python_docx(blocks, out_path, title=title)


def _docx_render_python_docx(blocks: list[Block], out_path: str, *, title: str = "") -> int:
    import docx

    document = docx.Document()
    _docx_setup(document)
    if title:
        heading = document.add_heading(title, level=0)
        heading.alignment = 1
    for block in blocks:
        if block.kind == "heading":
            document.add_heading(block.text, level=min(max(block.level, 1), 5))
        elif block.kind == "para":
            paragraph = document.add_paragraph()
            _docx_add_runs(paragraph, block.text)
        elif block.kind == "bullet":
            for item in block.items:
                paragraph = document.add_paragraph(style="List Bullet")
                _docx_add_runs(paragraph, item)
        elif block.kind == "ordered":
            for item in block.items:
                paragraph = document.add_paragraph(style="List Number")
                _docx_add_runs(paragraph, item)
        elif block.kind == "quote":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = 240
            run = paragraph.add_run(block.text)
            run.italic = True
        elif block.kind == "hr":
            document.add_page_break()
        elif block.kind == "code":
            paragraph = document.add_paragraph()
            run = paragraph.add_run(block.text)
            run.font.name = "Consolas"
        elif block.kind == "table" and block.rows:
            columns = max(len(row) for row in block.rows)
            table = document.add_table(rows=len(block.rows), cols=columns)
            table.style = "Table Grid"
            for row_index, row in enumerate(block.rows):
                for column_index in range(columns):
                    cell = table.cell(row_index, column_index)
                    cell.text = ""
                    paragraph = cell.paragraphs[0]
                    value = row[column_index] if column_index < len(row) else ""
                    _docx_add_runs(paragraph, value)
                    if row_index == 0:
                        for run in paragraph.runs:
                            run.bold = True
    document.save(out_path)
    return os.path.getsize(out_path)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pdf_available() -> bool:
    try:
        import reportlab  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _pdf_escape(text: str) -> str:
    """先把 Markdown 行内标记转成 reportlab 支持的迷你 HTML。"""
    from .markdown_lite import inline_html

    return inline_html(text)


def _pdf_render(blocks: list[Block], out_path: str, *, title: str = "") -> int:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font_name = "STSong-Light"  # reportlab 自带的 Adobe CID 中文字体，无需外部字体文件
    registered = False
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        registered = True
    except Exception:  # noqa: BLE001
        font_name = "Helvetica"

    base = ParagraphStyle(
        "body",
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        spaceAfter=6,
        wordWrap="CJK",
    )
    styles = {
        1: ParagraphStyle("h1", parent=base, fontSize=18, leading=24, spaceBefore=10, spaceAfter=8),
        2: ParagraphStyle("h2", parent=base, fontSize=14, leading=20, spaceBefore=12, spaceAfter=6),
        3: ParagraphStyle("h3", parent=base, fontSize=12, leading=18, spaceBefore=8, spaceAfter=4),
    }
    small = ParagraphStyle("small", parent=base, fontSize=9, textColor=colors.HexColor("#444444"))

    story: list[Any] = []
    if title:
        story.append(Paragraph(_pdf_escape(title), ParagraphStyle("title", parent=base, fontSize=20, leading=26, alignment=1)))
        story.append(Spacer(1, 8))
    for block in blocks:
        if block.kind == "heading":
            story.append(Paragraph(_pdf_escape(block.text), styles.get(min(block.level, 3), base)))
        elif block.kind == "para":
            story.append(Paragraph(_pdf_escape(block.text), base))
        elif block.kind in ("bullet", "ordered"):
            for index, item in enumerate(block.items, start=1):
                prefix = "• " if block.kind == "bullet" else f"{index}. "
                story.append(Paragraph(prefix + _pdf_escape(item), base))
            story.append(Spacer(1, 4))
        elif block.kind == "quote":
            story.append(Paragraph(_pdf_escape(block.text), small))
        elif block.kind == "code":
            story.append(Paragraph(block.text.replace("<", "&lt;").replace(">", "&gt;"), small))
        elif block.kind == "hr":
            story.append(Spacer(1, 10))
        elif block.kind == "table" and block.rows:
            data = []
            for row_index, row in enumerate(block.rows):
                if row_index == 0:
                    data.append([Paragraph(f"<b>{_pdf_escape(cell)}</b>", small) for cell in row])
                else:
                    data.append([Paragraph(_pdf_escape(cell), small) for cell in row])
            columns = max(len(row) for row in block.rows)
            width = (A4[0] - 40 * mm) / max(1, columns)
            table = Table(data, colWidths=[width] * columns, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c0c4cc")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f3f5")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 8))
    document = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=title or "ATRI-X 研究成果",
        author="ATRI-X Research",
    )
    document.build(story)
    return os.path.getsize(out_path)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def export_document(
    markdown_text: str,
    out_base: str,
    *,
    formats: list[str] | None = None,
    title: str = "",
) -> dict[str, Any]:
    """把一份 Markdown 导出成多种格式。

    ``out_base`` 是去掉扩展名的输出前缀；返回::

        {"ok": True, "results": [{"format","path","bytes","status","note"}],
         "degraded": ["docx: 缺少 python-docx ..."]}
    """
    wanted = [item.strip().lower() for item in (formats or ["md", "html", "docx", "pdf"]) if item.strip()]
    for item in wanted:
        if item not in EXPORT_FORMATS:
            raise ResearchError(
                ARG_INVALID,
                f"未知导出格式: {item}",
                hint=f"可用格式: {', '.join(EXPORT_FORMATS)}。",
            )
    os.makedirs(os.path.dirname(os.path.abspath(out_base)) or ".", exist_ok=True)
    blocks = parse(markdown_text)
    results: list[dict[str, Any]] = []
    degraded: list[str] = []

    for fmt in wanted:
        path = f"{out_base}.{fmt}"
        try:
            if fmt == "md":
                payload = markdown_text.encode("utf-8")
                size = _write_bytes(path, payload)
            elif fmt == "html":
                size = _write_bytes(path, render_html(blocks, title=title).encode("utf-8"))
            elif fmt == "docx":
                size = _docx_render(blocks, path, title=title)
            elif fmt == "pdf":
                if not _pdf_available():
                    degraded.append("pdf: 缺少 reportlab，已跳过（仍产出 md/html）")
                    results.append({"format": fmt, "status": "degraded", "path": "", "bytes": 0,
                                    "note": "pip install reportlab"})
                    continue
                size = _pdf_render(blocks, path, title=title)
            else:  # pragma: no cover - 上面已校验
                continue
            results.append({"format": fmt, "status": "ok", "path": path, "bytes": size, "note": ""})
        except Exception as exc:  # noqa: BLE001 - 单格式失败不拖垮整体
            results.append(
                {
                    "format": fmt,
                    "status": "failed",
                    "path": "",
                    "bytes": 0,
                    "note": f"{type(exc).__name__}: {exc}",
                }
            )
            degraded.append(f"{fmt}: 导出失败 {type(exc).__name__}: {exc}")
    ok = any(item["status"] == "ok" for item in results)
    if not ok:
        raise ResearchError(
            DEPENDENCY_MISSING,
            "所有导出格式都失败了",
            hint="先导出 md/html（零依赖）；docx/pdf 需要安装器创建的 venv。",
            details={"results": results},
        )
    return {"ok": True, "results": results, "degraded": degraded}
