"""生成物用到的 Markdown 小工具（无模板引擎）。

单独成一个模块的原因很实际：``render`` / ``entrypoints`` / ``domaindocs`` 三个渲染器都要
"把任意文本变成安全的表格单元格"和"禁止手改"的头部注释；放在这里避免三份复制，也避免
它们互相 import 造成循环。
"""

from __future__ import annotations

import re

__all__ = ["HEADER", "artifact", "cell", "code"]

#: 生成物的固定头部：**禁止手改**，漂移由 ``scripts/gen_index.py --check`` 判定。
HEADER = (
    "<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->\n"
    "<!-- 改动请落在 `atri_x/devtools/` 的渲染实现或代码本身，"
    "然后重新生成；手改会被 `--check` 判为漂移。 -->\n"
)

_PIPE_RE = re.compile(r"\|")


def cell(text: str, limit: int = 90) -> str:
    """把任意文本变成安全的表格单元格（转义竖线、压平换行、截断）。"""
    value = _PIPE_RE.sub("\\|", (text or "").replace("\n", " "))
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value or "—"


def code(text: str, limit: int = 90) -> str:
    """表格里渲染成行内代码（没有内容时给破折号）。"""
    value = cell(text, limit)
    return "—" if value == "—" else f"`{value}`"


def artifact(title: str, body: str) -> str:
    """拼一份完整生成物：禁改头 + 一级标题 + 正文（末尾统一一个换行）。"""
    return f"{HEADER}\n# {title}\n\n{body.rstrip()}\n"
