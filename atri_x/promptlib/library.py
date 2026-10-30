"""提示词库：把"内置默认 + 用户覆盖"合并成当前生效的条目。

用法（后台）：

    from atri_x.promptlib import configure, active
    configure(config_dir)            # 启动时一次
    active().text("image.rewrite.natural")
    active().render("member.profile", {...})

用法（GUI / 测试）：

    lib = PromptLibrary(PromptStore(config_dir))
    lib.entries()                    # 左树数据
    lib.save(edited_entries)         # 落盘（只写覆盖条目）

合并规则：

* 内置条目按 :data:`atri_x.promptlib.defaults.DEFAULT_PROMPTS` 的顺序在前；
* 用户自建条目按分类、标题排在后面；
* 覆盖条目替代同 id 的内置条目（``builtin`` 仍为 ``True``，GUI 才能提供
  "恢复默认"）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Sequence

from atri_x.promptlib.defaults import DEFAULT_BY_ID, DEFAULT_PROMPTS
from atri_x.promptlib.model import (
    PromptDataError,
    PromptEntry,
    normalize_entry,
    same_entry,
)
from atri_x.promptlib.render import render_template
from atri_x.promptlib.store import PromptStore

__all__ = [
    "PromptLibrary",
    "entries_by_category",
    "library_of",
    "new_library",
]


def _copy(entry: PromptEntry) -> PromptEntry:
    """返回条目的独立副本（默认表是模块级常量，不能被调用方原地改）。"""
    return replace(entry, variables=tuple(entry.variables))


class PromptLibrary:
    """提示词库的合并视图。

    ``store`` 为 ``None`` 时只看内置默认（测试 / 无配置目录的宿主）；
    ``store`` 存在时每次读取都会检查文件指纹，所以后台不需要重启就能拿到
    GUI 刚保存的改动。
    """

    def __init__(self, store: PromptStore | None = None) -> None:
        self.store = store

    # -- 读取 ---------------------------------------------------------------
    def overrides(self) -> dict[str, PromptEntry]:
        """用户覆盖条目（读失败时为空表，错误在 ``store.last_error``）。"""
        if self.store is None:
            return {}
        return self.store.load_overrides()

    def entries(self) -> list[PromptEntry]:
        """合并后的全部条目（内置在前，用户自建在后）。"""
        overrides = self.overrides()
        out: list[PromptEntry] = []
        for default in DEFAULT_PROMPTS:
            user = overrides.get(default.id)
            if user is None:
                out.append(_copy(default))
                continue
            out.append(
                replace(
                    _copy(user),
                    builtin=True,
                    variables=tuple(user.variables) or tuple(default.variables),
                )
            )
        custom = [
            entry for key, entry in overrides.items() if key not in DEFAULT_BY_ID
        ]
        custom.sort(key=lambda item: (item.category, item.title, item.id))
        out.extend(replace(entry, builtin=False) for entry in custom)
        return out

    def custom_entries(self) -> list[PromptEntry]:
        """只返回用户自建条目（``builtin=False``）。"""
        return [entry for entry in self.entries() if not entry.builtin]

    def get(self, prompt_id: str) -> PromptEntry | None:
        """按 id 取一条；不存在返回 ``None``。"""
        wanted = str(prompt_id or "").strip()
        if not wanted:
            return None
        for entry in self.entries():
            if entry.id == wanted:
                return entry
        return None

    def text(self, prompt_id: str, default: str = "") -> str:
        """取条目正文；条目不存在时返回 ``default``。"""
        entry = self.get(prompt_id)
        if entry is None:
            return default
        return entry.text()

    def render(self, prompt_id: str, values: dict[str, object] | None = None) -> str:
        """按变量渲染一条模板；条目不存在时返回空串。"""
        entry = self.get(prompt_id)
        if entry is None:
            return ""
        return render_template(entry.text(), values)

    def injected(self) -> list[PromptEntry]:
        """勾选了"注入系统提示词"且正文非空的条目（后台系统提示词来源用）。"""
        return [
            entry
            for entry in self.entries()
            if entry.inject and entry.text().strip()
        ]

    # -- 写入 ---------------------------------------------------------------
    def save(self, entries: Sequence[PromptEntry]) -> str:
        """整表保存：只把"和内置默认不同"的条目与自建条目写进覆盖文件。

        没改过的内置条目**不写**，这样以后升级内置模板时用户能自动拿到新文本。
        """
        if self.store is None:
            raise PromptDataError("提示词库未绑定配置目录，无法保存")
        overrides: dict[str, PromptEntry] = {}
        for raw in entries or ():
            entry = normalize_entry(raw)
            default = DEFAULT_BY_ID.get(entry.id)
            if default is not None:
                entry = replace(entry, builtin=True)
                if same_entry(entry, default):
                    continue
            overrides[entry.id] = entry
        return self.store.save_overrides(overrides)

    def reset(self, prompt_id: str) -> str:
        """删除一条覆盖，恢复内置默认（自建条目等于删除）。"""
        if self.store is None:
            raise PromptDataError("提示词库未绑定配置目录，无法修改")
        wanted = str(prompt_id or "").strip()
        overrides = self.overrides()
        if wanted not in overrides:
            return self.store.path
        overrides.pop(wanted)
        return self.store.save_overrides(overrides)

    def reset_all(self) -> str:
        """清空全部覆盖（恢复所有内置默认，并删除自建条目）。"""
        if self.store is None:
            raise PromptDataError("提示词库未绑定配置目录，无法修改")
        return self.store.save_overrides({})


def new_library(store: PromptStore | None = None) -> PromptLibrary:
    """构造提示词库（Go 风格命名；等价于直接实例化）。"""
    return PromptLibrary(store)


def entries_by_category(entries: Iterable[PromptEntry]) -> dict[str, list[PromptEntry]]:
    """按分类分组（保持传入顺序），给 GUI 左树用。"""
    grouped: dict[str, list[PromptEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(entry)
    return grouped


def library_of(*, config_dir: str = "", logger: Any = None) -> PromptLibrary:
    """按配置目录构造提示词库；目录为空时只看内置默认。"""
    if not str(config_dir or "").strip():
        return PromptLibrary()
    return PromptLibrary(PromptStore(str(config_dir), logger=logger))
