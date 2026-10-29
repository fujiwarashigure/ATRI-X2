"""PDF 全文抽取（可选能力，依赖 ``pypdf``）。

全文筛选需要先有全文。为了避免"让模型读 PDF 二进制"，先由这个模块把
PDF 里的文字抽成 ``fulltext/<key>.txt``，引擎再用固定规则做全文筛选。

缺 ``pypdf`` 时抛 ``DEPENDENCY_MISSING`` 并给出安装方式——绝不用空文本
冒充"已读全文"，因为那会直接把 PRISMA 的全文阶段变成假数据。
"""

from __future__ import annotations

import os
import re
from typing import Any

from .errors import DEPENDENCY_MISSING, ResearchError

__all__ = ["available", "extract_directory"]

_WS = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_PAGE_HEADER = re.compile(r"^\s*(?:-\s*)?\d{1,4}\s*(?:-\s*)?$")


def available() -> bool:
    try:
        import pypdf  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _normalize(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = _WS.sub(" ", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _PAGE_HEADER.match(stripped):
            continue
        lines.append(stripped)
    return _MULTI_NEWLINE.sub("\n\n", "\n".join(lines)).strip()


def extract_directory(
    pdf_dir: str,
    out_dir: str,
    *,
    max_bytes: int = 8 * 1024 * 1024,
    key_for: dict[str, str] | None = None,
) -> dict[str, Any]:
    """批量抽取 ``pdf_dir`` 下的 PDF。

    ``key_for`` 可把文件名映射成文献库 key（例如 ``{"smith2020.pdf": "doi:10..."}``）；
    没有映射时用文件名（去扩展名）作为键。
    """
    if not available():
        raise ResearchError(
            DEPENDENCY_MISSING,
            "缺少 pypdf，无法抽取 PDF 全文",
            hint="用安装器重建 venv（install.py --with-venv），或在该 venv 里 pip install pypdf。",
        )
    from pypdf import PdfReader

    if not os.path.isdir(pdf_dir):
        raise ResearchError(
            "ARG_INVALID",
            f"PDF 目录不存在: {pdf_dir}",
            hint="先用工作区工具把 PDF 放进一个目录，再传 --pdf-dir。",
        )
    os.makedirs(out_dir, exist_ok=True)
    extracted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for name in sorted(os.listdir(pdf_dir)):
        if not name.lower().endswith(".pdf"):
            continue
        source = os.path.join(pdf_dir, name)
        size = os.path.getsize(source)
        if size > max_bytes:
            skipped.append({"file": name, "reason": f"超过 {max_bytes} 字节上限"})
            continue
        try:
            reader = PdfReader(source)
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = _normalize("\n".join(pages))
        except Exception as exc:  # noqa: BLE001 - 损坏 PDF 不能中断整批
            skipped.append({"file": name, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if len(text) < 200:
            skipped.append({"file": name, "reason": "抽取文本过短（可能是扫描件）"})
            continue
        key = (key_for or {}).get(name) or os.path.splitext(name)[0]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80] or "doc"
        target = os.path.join(out_dir, f"{safe}.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        extracted.append({"file": name, "key": key, "path": target, "chars": len(text)})
    return {
        "schema": "atrix-research/fulltext/1",
        "extracted": extracted,
        "skipped": skipped,
        "count": len(extracted),
    }
