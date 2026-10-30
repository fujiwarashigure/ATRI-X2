"""提示词库的数据模型：条目结构、分类、归一化与 JSON 序列化。

提示词库解决的是「模板写死在源码里」的问题：内置模板放在
:mod:`atri_x.promptlib.defaults`，用户改动落在 ``<配置目录>/gui/prompts.json``，
两者由 :class:`~atri_x.promptlib.library.PromptLibrary` 合并成"当前生效的条目"。

设计边界（和 ``gui/members.json``、``tool_tags.toml`` 一致）：

* 条目只是**文本**，不是可执行内容；渲染只做 ``{变量}`` 替换，不解释任何表达式；
* 内置条目（``builtin=True``）可以覆盖、可以恢复默认，但**不会被删除**——
  删掉一条模板不会让功能消失，只会退回内置文本；
* 用户自建条目默认只在「提示词」页里查看；勾选 ``inject`` 后才会作为额外
  系统提示词片段注入（见 :mod:`atri_x.promptlib.library`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "MAX_PROMPT_CONTENT",
    "PROMPTS_FILE",
    "PROMPTS_VERSION",
    "PromptDataError",
    "PromptEntry",
    "normalize_entry",
    "prompt_from_dict",
    "prompt_to_dict",
    "same_entry",
]

#: 存储文件名（``<配置目录>/gui/prompts.json``）。
PROMPTS_FILE = "prompts.json"

#: 文件结构版本（与 ``members.json`` / ``reactions/catalog.json`` 一样自带版本号）。
PROMPTS_VERSION = 1

#: 单条模板正文上限（字符）。防止误把整本书粘进来撑爆配置与提示词预算。
MAX_PROMPT_CONTENT = 200_000

#: 分类顺序（GUI 左树按这个顺序分组）。
CATEGORIES: tuple[str, ...] = ("image", "vision", "member", "system", "comic", "custom")

#: 分类 → 界面标签。
CATEGORY_LABELS: dict[str, str] = {
    "image": "生图",
    "vision": "识图",
    "member": "群友与人设",
    "system": "系统提示",
    "comic": "漫画",
    "custom": "自定义",
}

#: 条目 id 规则：小写字母/数字开头，只含小写字母、数字、``_`` ``-`` ``.``。
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

#: 模板变量名规则（渲染时只替换这些形态的占位符）。
_VARIABLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class PromptDataError(Exception):
    """提示词库的可预期错误（消息直接展示给用户）。"""


@dataclass
class PromptEntry:
    """一条提示词条目。

    ``builtin`` 不是持久化字段：它由"这个 id 是否出现在内置默认表里"决定，
    读盘时由 :class:`~atri_x.promptlib.library.PromptLibrary` 补齐。
    """

    #: 稳定 id（内置模板由代码引用，改名等于换模板）。
    id: str = ""
    #: 界面标题。
    title: str = ""
    #: 分类，取值见 :data:`CATEGORIES`。
    category: str = "custom"
    #: 模板正文。
    content: str = ""
    #: 正文里用到的变量名（``{name}`` 形式）。
    variables: tuple[str, ...] = ()
    #: 一行说明（给用户看这个模板何时生效）。
    description: str = ""
    #: 是否作为额外系统提示词片段注入（只对用户自建条目有意义）。
    inject: bool = False
    #: 是否内置模板（运行期补齐，不落盘）。
    builtin: bool = False

    def text(self) -> str:
        """正文字符串（``None`` 安全）。"""
        return self.content or ""


def normalize_entry(entry: PromptEntry) -> PromptEntry:
    """校验并归一化一条条目；非法时抛 :class:`PromptDataError`。"""
    prompt_id = str(entry.id or "").strip()
    if not _ID_PATTERN.match(prompt_id):
        raise PromptDataError(
            f"提示词 id 不合法：{prompt_id!r}（只允许小写字母/数字/._-，以字母或数字开头）"
        )
    category = str(entry.category or "").strip().lower() or "custom"
    if category not in CATEGORIES:
        raise PromptDataError(
            f"提示词分类不合法：{category!r}（可选：{'、'.join(CATEGORIES)}）"
        )
    content = str(entry.content or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(content) > MAX_PROMPT_CONTENT:
        raise PromptDataError(
            f"提示词正文过长（{len(content)} 字符，上限 {MAX_PROMPT_CONTENT}）"
        )
    title = str(entry.title or "").strip() or prompt_id
    variables: list[str] = []
    for raw in entry.variables or ():
        name = str(raw or "").strip()
        if not name:
            continue
        if not _VARIABLE_PATTERN.match(name):
            raise PromptDataError(f"变量名不合法：{name!r}（只允许小写字母/数字/下划线）")
        if name not in variables:
            variables.append(name)
    return PromptEntry(
        id=prompt_id,
        title=title,
        category=category,
        content=content,
        variables=tuple(variables),
        description=str(entry.description or "").strip(),
        inject=bool(entry.inject),
        builtin=bool(entry.builtin),
    )


def prompt_from_dict(raw: Any, *, builtin: bool = False) -> PromptEntry | None:
    """把 JSON 里的一条记录解析成条目；结构无效时返回 ``None``（宽容读取）。"""
    if not isinstance(raw, Mapping):
        return None
    prompt_id = raw.get("id")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        return None
    variables_raw = raw.get("variables")
    variables: list[str] = []
    if isinstance(variables_raw, list):
        variables = [str(item) for item in variables_raw if str(item or "").strip()]
    try:
        return normalize_entry(
            PromptEntry(
                id=prompt_id,
                title=str(raw.get("title") or ""),
                category=str(raw.get("category") or "custom"),
                content=str(raw.get("content") or ""),
                variables=tuple(variables),
                description=str(raw.get("description") or ""),
                inject=bool(raw.get("inject", False)),
                builtin=builtin,
            )
        )
    except PromptDataError:
        return None


def prompt_to_dict(entry: PromptEntry) -> dict[str, Any]:
    """把条目渲染成可写入 ``prompts.json`` 的对象（含 id，便于人工阅读）。"""
    return {
        "id": entry.id,
        "title": entry.title,
        "category": entry.category,
        "content": entry.content,
        "variables": list(entry.variables),
        "description": entry.description,
        "inject": bool(entry.inject),
    }


def same_entry(left: PromptEntry, right: PromptEntry) -> bool:
    """两条条目的"用户可见内容"是否相同（忽略 ``builtin`` 标记）。

    用于判断"这条内置模板是否被用户改过"：没改过就不写进覆盖文件，
    这样升级内置模板时用户能自动拿到新文本。
    """
    return (
        left.id == right.id
        and left.title == right.title
        and left.category == right.category
        and left.content == right.content
        and tuple(left.variables) == tuple(right.variables)
        and left.description == right.description
        and bool(left.inject) == bool(right.inject)
    )
