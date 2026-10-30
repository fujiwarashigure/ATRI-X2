"""自定义提示词库：内置模板 + 用户覆盖的统一入口。

后台启动时调一次 :func:`configure` 绑定配置目录；之后所有提示词读取都走
:func:`active`（同一进程只服务一个配置目录）。GUI 是独立进程，直接构造
:class:`~atri_x.promptlib.library.PromptLibrary` 编辑，不需要 :func:`configure`。

为什么用进程级默认实例而不是给每个构造器都加参数：提示词读取点很分散
（系统提示词来源、生图改写、识图模板、群友档案渲染……），逐个注入会让
十几个构造函数各多一个参数，且真实装配路径容易漏（本仓库有过"只写进
Options、真实路径不生效"的先例）。这里对齐 ``atri_x.time_service`` 的
``_DEFAULT_SERVICE`` 做法：一个模块级默认实例 + 显式 ``configure``。
"""

from __future__ import annotations

import threading
from typing import Any

from atri_x.promptlib.defaults import DEFAULT_BY_ID, DEFAULT_PROMPTS
from atri_x.promptlib.library import (
    PromptLibrary,
    entries_by_category,
    library_of,
    new_library,
)
from atri_x.promptlib.model import (
    CATEGORIES,
    CATEGORY_LABELS,
    MAX_PROMPT_CONTENT,
    PROMPTS_FILE,
    PROMPTS_VERSION,
    PromptDataError,
    PromptEntry,
    normalize_entry,
    prompt_from_dict,
    prompt_to_dict,
    same_entry,
)
from atri_x.promptlib.render import PromptTemplateError, render_template, template_variables
from atri_x.promptlib.store import (
    PromptStore,
    load_prompt_overrides,
    prompts_path,
    save_prompt_overrides,
)
from atri_x.promptlib.tags import (
    MAX_TAG_ALIASES,
    MAX_TAG_DESCRIPTION,
    MAX_TAG_NAME,
    TAGS_FILE,
    TAGS_VERSION,
    TagCatalog,
    TagDataError,
    TagEntry,
    active_tags,
    configure_tags,
    load_tags,
    normalize_tag,
    reset_active_tags,
    save_tags,
    tags_path,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "DEFAULT_BY_ID",
    "DEFAULT_PROMPTS",
    "MAX_PROMPT_CONTENT",
    "MAX_TAG_ALIASES",
    "MAX_TAG_DESCRIPTION",
    "MAX_TAG_NAME",
    "PROMPTS_FILE",
    "PROMPTS_VERSION",
    "PromptDataError",
    "PromptEntry",
    "PromptLibrary",
    "PromptStore",
    "PromptTemplateError",
    "TAGS_FILE",
    "TAGS_VERSION",
    "TagCatalog",
    "TagDataError",
    "TagEntry",
    "active",
    "active_tags",
    "configure",
    "configure_tags",
    "entries_by_category",
    "library_of",
    "load_prompt_overrides",
    "load_tags",
    "new_library",
    "normalize_entry",
    "normalize_tag",
    "prompt_entry",
    "prompt_from_dict",
    "prompt_text",
    "prompt_to_dict",
    "prompts_path",
    "render_prompt",
    "reset_active",
    "reset_active_tags",
    "same_entry",
    "save_prompt_overrides",
    "save_tags",
    "tags_path",
    "template_variables",
]

_LOCK = threading.RLock()
_ACTIVE: PromptLibrary | None = None

#: 没有配置目录时使用的只读默认库（测试 / 尚未启动的后台宿主）。
_FALLBACK = PromptLibrary()


def configure(config_dir: str, *, logger: Any = None) -> PromptLibrary:
    """绑定进程级提示词库到 ``<配置目录>/gui/prompts.json``；返回该库。"""
    global _ACTIVE
    library = library_of(config_dir=config_dir, logger=logger)
    with _LOCK:
        _ACTIVE = library
    return library


def active() -> PromptLibrary:
    """当前进程级提示词库；未 configure 时返回只看内置默认的实例。"""
    with _LOCK:
        current = _ACTIVE
    return current if current is not None else _FALLBACK


def reset_active() -> None:
    """解除进程级绑定（测试清理用；后台运行期不需要调用）。"""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = None


def prompt_entry(prompt_id: str) -> PromptEntry | None:
    """按 id 取当前生效条目。"""
    return active().get(prompt_id)


def prompt_text(prompt_id: str, default: str = "") -> str:
    """按 id 取当前生效正文。"""
    return active().text(prompt_id, default)


def render_prompt(prompt_id: str, values: dict[str, object] | None = None) -> str:
    """按 id 渲染当前生效模板。"""
    return active().render(prompt_id, values)
