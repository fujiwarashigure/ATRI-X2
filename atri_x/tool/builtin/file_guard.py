"""文件读写导流守卫：按路径把 read_file / edit_file 引向专用工具。

对应 Go：``internal/tool/builtin/file_guard.go``。

规则命中后：读操作只追加 warning（不阻止），写操作直接拒绝并给出应使用的工具名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from atri_x.atriyph import SKILL_FILE_NAME
from atri_x.errors import InvalidError
from atri_x.tool.builtin.common import abs_clean, same_path

WARN_USE_READ_FILE = "读取本地文件请优先使用 read_file 工具。"
WARN_USE_EDIT_FILE = "编辑本地文件请优先使用 edit_file 工具。"
WARN_USE_READ_ATRIX_SKILL = "读取 ATRI-X Skill 文件请使用 read_atrix_skill。"
WARN_USE_MODIFY_ATRIX_SKILL = "ATRI-X Skill 文件请使用 modify_atrix_skill 修改。"
WARN_USE_READ_MEMORY = "读取常驻记忆请使用 resident_memory_read。"
WARN_USE_MODIFY_MEMORY = "常驻记忆文件请使用 resident_memory_normal 或 resident_memory_core 修改。"
WARN_USE_READ_LONG_MEMORY = "读取长期记忆请使用 long_memory_search。"
WARN_USE_MODIFY_LONG_MEMORY = "长期记忆文件请使用 long_memory_write 修改。"


@dataclass
class FileGuardRule:
    """一条路径规则。对应 Go ``FileGuardRule``。

    ``match`` 收到 ``(abs_path, rel_slash_path)``；为 ``None`` 时命中 root 下全部。
    """

    name: str = ""
    root: str = ""
    exact_path: str = ""
    read_warnings: list[str] = field(default_factory=list)
    write_error: str = ""
    match: Callable[[str, str], bool] | None = None

    def matches(self, path: str) -> bool:
        """对应 Go ``FileGuardRule.matches``。"""
        try:
            path = abs_clean(path)
        except InvalidError:
            return False
        exact = (self.exact_path or "").strip()
        if exact != "":
            try:
                exact = abs_clean(exact)
            except InvalidError:
                return False
            return same_path(path, exact)
        root = (self.root or "").strip()
        if root == "":
            return False
        try:
            root = abs_clean(root)
        except InvalidError:
            return False
        rel = _relpath(root, path)
        if rel is None or rel == "." or rel.startswith("..") or os.path.isabs(rel):
            return False
        if self.match is None:
            return True
        return bool(self.match(path, rel.replace("\\", "/")))


class FileGuard:
    """一组规则的集合。对应 Go ``FileGuard``。"""

    def __init__(self, rules: list[FileGuardRule] | None = None) -> None:
        self.rules: list[FileGuardRule] = []
        for rule in rules or []:
            self.add_rule(rule)

    def add_rule(self, rule: FileGuardRule | None) -> None:
        """对应 Go ``FileGuard.AddRule``；root 与 exact_path 都空则忽略。"""
        if rule is None:
            return
        if (rule.root or "").strip() == "" and (rule.exact_path or "").strip() == "":
            return
        self.rules.append(rule)

    def read_warnings(self, path: str) -> list[str]:
        """对应 Go ``FileGuard.ReadWarnings``。"""
        warnings: list[str] = []
        for rule in self.rules:
            if rule.matches(path):
                warnings.extend(rule.read_warnings)
        return warnings

    def check_write(self, path: str) -> None:
        """对应 Go ``FileGuard.CheckWrite``；命中则抛 :class:`InvalidError`。"""
        for rule in self.rules:
            if not rule.matches(path):
                continue
            message = (rule.write_error or "").strip()
            if message == "":
                continue
            raise InvalidError(message)


def new_file_guard(*rules: FileGuardRule) -> FileGuard:
    """对应 Go ``NewFileGuard``。"""
    return FileGuard(list(rules))


def new_atrix_skill_file_guard_rule(skill_root: str) -> FileGuardRule:
    """对应 Go ``NewAtrixSkillFileGuardRule``：``go/<id>/SKILL.atriyph|main.go``。"""
    skill_file_name = SKILL_FILE_NAME

    def match(_abs_path: str, rel: str) -> bool:
        parts = rel.replace("\\", "/").split("/")
        if len(parts) != 3 or parts[0] != "go" or parts[1].strip() == "":
            return False
        return parts[2] in (skill_file_name, "main.go")

    return FileGuardRule(
        name="atrix_skill",
        root=skill_root,
        read_warnings=[WARN_USE_READ_ATRIX_SKILL],
        write_error=WARN_USE_MODIFY_ATRIX_SKILL,
        match=match,
    )


def new_resident_memory_file_guard_rule(path: str) -> FileGuardRule:
    """对应 Go ``NewResidentMemoryFileGuardRule``。"""
    return FileGuardRule(
        name="resident_memory",
        exact_path=path,
        read_warnings=[WARN_USE_READ_MEMORY],
        write_error=WARN_USE_MODIFY_MEMORY,
    )


def new_long_memory_file_guard_rule(root_dir: str) -> FileGuardRule:
    """对应 Go ``NewLongMemoryFileGuardRule``：``<root>/memories/*.md``。"""
    return FileGuardRule(
        name="long_memory",
        root=os.path.join(root_dir, "memories"),
        read_warnings=[WARN_USE_READ_LONG_MEMORY],
        write_error=WARN_USE_MODIFY_LONG_MEMORY,
        match=lambda _abs_path, rel: os.path.splitext(rel)[1].lower() == ".md",
    )


def _relpath(root: str, path: str) -> str | None:
    """对应 Go ``filepath.Rel``；跨盘符返回 None。"""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return None


__all__ = [name for name in dir() if not name.startswith("_")]
