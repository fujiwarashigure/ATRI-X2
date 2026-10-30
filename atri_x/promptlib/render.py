"""模板渲染：只做 ``{变量}`` 文本替换，不解释任何表达式。

为什么不用 :meth:`str.format`：模板正文是用户可编辑的普通文本，里面出现
``{`` / ``}``（例如示例 JSON、代码片段）是很正常的；``format`` 会把这些当
占位符直接抛错。这里只替换**登记过的变量**，其余字符原样保留。

"整行变量"规则：若一行的内容**只有**一个变量占位符，且该变量值为空串，则整行
被丢掉。群友档案模板依赖这条规则：没有昵称 / 标签 / 资料时不应该留下空行
（迁移前的渲染器是条件式 append，输出里没有空行）。
"""

from __future__ import annotations

import re

__all__ = ["PromptTemplateError", "render_template", "template_variables"]

#: 模板里可识别的变量占位符（与 ``model._VARIABLE_PATTERN`` 保持一致的口径）。
_VARIABLE_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")


class PromptTemplateError(Exception):
    """模板渲染的可预期错误（变量缺失或类型不对）。"""


def template_variables(text: str) -> tuple[str, ...]:
    """按出现顺序提取模板里的变量名（去重）。"""
    seen: list[str] = []
    for match in _VARIABLE_RE.finditer(str(text or "")):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def render_template(
    text: str,
    values: dict[str, object] | None = None,
    *,
    drop_empty_lines: bool = True,
) -> str:
    """把 ``{name}`` 替换成 ``values[name]``，返回渲染结果。

    * 未在 ``values`` 里登记的占位符原样保留（用户正文里的 ``{`` 不受影响）；
    * ``drop_empty_lines=True`` 时，"整行只有一个变量、且该变量值为空"的行被删掉；
    * 变量值不是字符串时按 ``str()`` 渲染。

    渲染是纯函数，不做转义：调用方（例如群友档案）必须先把用户数据里的
    ``<`` / ``>`` 转义再传进来，否则模板边界可被数据提前闭合。
    """
    body = str(text or "")
    mapping: dict[str, str] = {}
    for name, value in (values or {}).items():
        mapping[str(name)] = "" if value is None else str(value)
    if not body:
        return ""
    lines = body.split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        match = _VARIABLE_RE.fullmatch(stripped)
        if (
            drop_empty_lines
            and match is not None
            and match.group(1) in mapping
            and mapping[match.group(1)] == ""
        ):
            continue
        rendered = _VARIABLE_RE.sub(
            lambda item: mapping.get(item.group(1), item.group(0)), line
        )
        kept.append(rendered)
    return "\n".join(kept)
