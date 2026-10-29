"""零依赖 DOCX 写入器（标准库 zipfile + WordprocessingML）。

为什么自己写一份：``python-docx`` 依赖 ``lxml``（C 扩展，新 Python 版本常常没有
预编译轮子），而"导出 Word"是交付环节的刚需。这里用最少的 OOXML 生成一个
Word/WPS 都能打开、带标题样式、表格边框与中文字体的文档；``python-docx``
存在时仍优先用它（排版细节更完整），不存在时不再降级——功能是等价的。

生成的包结构::

    [Content_Types].xml
    _rels/.rels
    word/document.xml
    word/styles.xml
    word/_rels/document.xml.rels
"""

from __future__ import annotations

import os
import zipfile
from typing import Any, Iterable
from xml.sax.saxutils import escape

from .markdown_lite import Block, inline_runs

__all__ = ["write_docx"]

_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT = "application/vnd.openxmlformats-officedocument"

_CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{_CT}.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="{_CT}.wordprocessingml.styles+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_NS}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="Microsoft YaHei"/>
      <w:sz w:val="21"/><w:szCs w:val="21"/>
    </w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="240"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="360" w:after="160"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="300" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="2"/><w:spacing w:before="240" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="480"/></w:pPr><w:rPr><w:i/><w:color w:val="444444"/></w:rPr></w:style>
</w:styles>
"""


def _run(text: str, *, bold: bool = False, italic: bool = False, code: bool = False) -> str:
    if not text:
        return ""
    properties = ""
    if bold:
        properties += "<w:b/>"
    if italic:
        properties += "<w:i/>"
    if code:
        properties += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
    rpr = f"<w:rPr>{properties}</w:rPr>" if properties else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph(
    text: str,
    *,
    style: str = "",
    align: str = "",
    indent: int = 0,
) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if align:
        props += f'<w:jc w:val="{align}"/>'
    if indent:
        props += f'<w:ind w:left="{indent}"/>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    runs = "".join(
        _run(chunk, bold=bool(flags.get("bold")), italic=bool(flags.get("italic")), code=bool(flags.get("code")))
        for chunk, flags in inline_runs(text)
    )
    return f"<w:p>{ppr}{runs}</w:p>"


def _table(rows: list[list[str]], *, header: bool = True) -> str:
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    grid = "".join('<w:gridCol w:w="2400"/>' for _ in range(columns))
    border = (
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:color="C0C4CC"/>'
        '<w:left w:val="single" w:sz="4" w:color="C0C4CC"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="C0C4CC"/>'
        '<w:right w:val="single" w:sz="4" w:color="C0C4CC"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="C0C4CC"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="C0C4CC"/>'
        "</w:tblBorders>"
    )
    body: list[str] = []
    for row_index, row in enumerate(rows):
        cells: list[str] = []
        for column_index in range(columns):
            value = row[column_index] if column_index < len(row) else ""
            is_header = header and row_index == 0
            runs = "".join(
                _run(chunk, bold=is_header or bool(flags.get("bold")), italic=bool(flags.get("italic")))
                for chunk, flags in inline_runs(value)
            )
            shine = '<w:shd w:val="clear" w:color="auto" w:fill="F6F8FA"/>' if is_header else ""
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/>{shine}</w:tcPr>'
                f"<w:p>{runs}</w:p></w:tc>"
            )
        body.append("<w:tr>" + "".join(cells) + "</w:tr>")
    return f"<w:tbl><w:tblPr>{border}</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>"


def _document_xml(blocks: Iterable[Block], title: str) -> str:
    parts: list[str] = []
    if title:
        parts.append(_paragraph(title, style="Title", align="center"))
    for block in blocks:
        if block.kind == "heading":
            level = min(max(block.level, 1), 3)
            parts.append(_paragraph(block.text, style=f"Heading{level}"))
        elif block.kind == "para":
            parts.append(_paragraph(block.text))
        elif block.kind == "bullet":
            for item in block.items:
                parts.append(_paragraph("• " + item, indent=240))
        elif block.kind == "ordered":
            for index, item in enumerate(block.items, start=1):
                parts.append(_paragraph(f"{index}. {item}", indent=240))
        elif block.kind == "quote":
            parts.append(_paragraph(block.text, style="Quote"))
        elif block.kind == "code":
            parts.append(_paragraph(block.text, indent=240))
        elif block.kind == "hr":
            parts.append(_paragraph(""))
        elif block.kind == "table":
            parts.append(_table(block.rows, header=block.header))
            parts.append(_paragraph(""))
    body = "".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{_NS}"><w:body>{body}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )


def write_docx(
    blocks: list[Block],
    out_path: str,
    *,
    title: str = "",
) -> int:
    """写出 .docx，返回字节数。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    document = _document_xml(blocks, title)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _ROOT_RELS)
        package.writestr("word/document.xml", document)
        package.writestr("word/styles.xml", _STYLES)
        package.writestr("word/_rels/document.xml.rels", _DOC_RELS)
    return os.path.getsize(out_path)
