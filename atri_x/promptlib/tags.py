"""标签库：给"群友标签 / 人设标签"一个可复用的词表（``<配置目录>/gui/tags.json``）。

解决的问题：以前标签只是逗号分隔的自由文本，注入提示词时只有一串裸词
（``标签：a、b``），模型不知道「傲娇」和「技术宅」是什么意思，同一含义还会
出现好几种写法。标签库给每个标签一份说明与别名，GUI 里可以多选复用：

* 群友档案 / 人设卡的标签编辑仍然是文本（兼容旧档案），旁边多一个「标签库…」
  按钮从词表里挑；
* 群友档案注入提示词时，命中词表的标签渲染成 ``名字（说明）``，模型不用猜；
* 没登记在词表里的标签原样输出，行为与以前完全一致。

存储口径与 ``gui/members.json`` 一致：``(mtime_ns, size)`` 指纹热更新、``.tmp``
+ ``os.replace`` 原子写、文件损坏时拒绝覆盖。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

__all__ = [
    "MAX_TAG_ALIASES",
    "MAX_TAG_DESCRIPTION",
    "MAX_TAG_NAME",
    "TAGS_FILE",
    "TAGS_VERSION",
    "TagCatalog",
    "TagDataError",
    "TagEntry",
    "active_tags",
    "configure_tags",
    "load_tags",
    "normalize_tag",
    "reset_active_tags",
    "save_tags",
    "tags_path",
]

#: ``gui/`` 下的文件名。
TAGS_FILE = "tags.json"

#: JSON 结构版本。
TAGS_VERSION = 1

#: 单条标签的字数上限（名字 / 说明 / 别名各一项）。
MAX_TAG_NAME = 40
MAX_TAG_DESCRIPTION = 500
MAX_TAG_ALIASES = 10

#: 标签文件更新时间格式（与群友档案一致）。
UPDATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 群友 / 人设数据所在的 GUI 子目录。
GUI_SUBDIR = "gui"


class TagDataError(Exception):
    """标签库的可预期错误（消息直接展示给用户）。"""


@dataclass
class TagEntry:
    """一个标签词条。"""

    name: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    group: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        """大小写不敏感的键（``傲娇``/``傲嬌`` 视为不同标签，英文不区分大小写）。"""
        return self.name.strip().lower()


def normalize_tag(entry: TagEntry) -> TagEntry:
    """校验并归一化一个词条；非法时抛 :class:`TagDataError`。"""
    name = str(entry.name or "").strip()
    if not name:
        raise TagDataError("标签名不能为空")
    if len(name) > MAX_TAG_NAME:
        raise TagDataError(f"标签名过长：{name!r}（上限 {MAX_TAG_NAME} 字符）")
    description = str(entry.description or "").strip()
    if len(description) > MAX_TAG_DESCRIPTION:
        raise TagDataError(
            f"标签说明过长：{name!r}（上限 {MAX_TAG_DESCRIPTION} 字符）"
        )
    aliases: list[str] = []
    for raw in entry.aliases or ():
        alias = str(raw or "").strip()
        if not alias or alias == name:
            continue
        if len(alias) > MAX_TAG_NAME:
            raise TagDataError(f"标签别名过长：{alias!r}（上限 {MAX_TAG_NAME} 字符）")
        if alias.lower() in {item.lower() for item in aliases}:
            continue
        aliases.append(alias)
        if len(aliases) >= MAX_TAG_ALIASES:
            break
    return TagEntry(
        name=name,
        description=description,
        aliases=aliases,
        group=str(entry.group or "").strip(),
        updated_at=str(entry.updated_at or "").strip(),
    )


def tag_from_dict(raw: Any) -> TagEntry | None:
    """宽容解析 JSON 里的一项；结构无效返回 ``None``。"""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    aliases = raw.get("aliases")
    try:
        return normalize_tag(
            TagEntry(
                name=name,
                description=str(raw.get("description") or ""),
                aliases=[str(item) for item in aliases] if isinstance(aliases, list) else [],
                group=str(raw.get("group") or ""),
                updated_at=str(raw.get("updated_at") or ""),
            )
        )
    except TagDataError:
        return None


def tag_to_dict(entry: TagEntry) -> dict[str, Any]:
    """词条 → JSON 对象。"""
    return {
        "name": entry.name,
        "description": entry.description,
        "aliases": list(entry.aliases),
        "group": entry.group,
        "updated_at": entry.updated_at,
    }


def tags_path(config_dir: str) -> str:
    """标签库文件路径（``<配置目录>/gui/tags.json``）。"""
    return os.path.join(config_dir or "", GUI_SUBDIR, TAGS_FILE)


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except (FileNotFoundError, OSError):
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _read_file(path: str) -> tuple[list[TagEntry], str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError) as exc:
        return [], f"读取标签库失败：{exc}"
    if not isinstance(data, dict):
        return [], "标签库结构无效：顶层不是对象"
    raw_tags = data.get("tags")
    if raw_tags is None:
        raw_tags = []
    if not isinstance(raw_tags, list):
        return [], "标签库结构无效：tags 不是数组"
    entries: list[TagEntry] = []
    seen: set[str] = set()
    for raw in raw_tags:
        entry = tag_from_dict(raw)
        if entry is None or entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries, ""


class TagCatalog:
    """标签库的读写（进程内缓存 + 文件指纹热更新；线程安全）。"""

    def __init__(self, config_dir: str, *, logger: Any = None) -> None:
        self.config_dir = config_dir or ""
        self.logger = logger
        self._lock = threading.RLock()
        self._loaded = False
        self._signature: tuple[int, int] | None = None
        self._entries: list[TagEntry] = []
        self._error = ""
        self._logged_error = ""

    @property
    def path(self) -> str:
        """标签库文件路径。"""
        return tags_path(self.config_dir)

    @property
    def last_error(self) -> str:
        """最近一次读取失败的原因（成功读取后清空）。"""
        with self._lock:
            return self._error

    # -- 读取 ---------------------------------------------------------------
    def load(self) -> list[TagEntry]:
        """全部词条（文件不存在/损坏时返回空表）。"""
        with self._lock:
            self._refresh_locked()
            return list(self._entries)

    def names(self) -> list[str]:
        """全部标签名（展示顺序）。"""
        return [entry.name for entry in self.load()]

    def get(self, name: str) -> TagEntry | None:
        """按名字或别名查词条（英文不区分大小写）。"""
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None
        for entry in self.load():
            if entry.key == wanted:
                return entry
            if any(alias.lower() == wanted for alias in entry.aliases):
                return entry
        return None

    def describe(self, name: str) -> str:
        """渲染一个标签：命中词表时 ``名字（说明）``，否则原样返回。"""
        text = str(name or "").strip()
        entry = self.get(text)
        if entry is None:
            return text
        if entry.description:
            return f"{entry.name}（{entry.description}）"
        return entry.name

    def describe_all(self, names: Iterable[str]) -> list[str]:
        """批量渲染标签（保持传入顺序、跳过空项）。"""
        out: list[str] = []
        for name in names or ():
            text = str(name or "").strip()
            if not text:
                continue
            out.append(self.describe(text))
        return out

    def _refresh_locked(self) -> bool:
        signature = _file_signature(self.path)
        if self._loaded and signature == self._signature:
            return False
        entries, error = _read_file(self.path)
        self._loaded = True
        self._entries = entries
        self._error = error
        if error:
            self._loaded = False
            self._signature = None
            if error != self._logged_error:
                self._logged_error = error
                self._warn("标签库读取失败，已按空表处理", error)
            return True
        self._logged_error = ""
        self._signature = signature
        return True

    # -- 写入 ---------------------------------------------------------------
    def save(self, entries: Iterable[TagEntry]) -> str:
        """整表写回；返回文件路径。空表也会写（保留合法空文件）。"""
        normalized: list[TagEntry] = []
        seen: set[str] = set()
        for raw in entries or ():
            entry = normalize_tag(raw)
            if entry.key in seen:
                raise TagDataError(f"标签重复：{entry.name!r}（名字或别名与其他词条冲突）")
            seen.add(entry.key)
            if not entry.updated_at:
                entry.updated_at = datetime.now().strftime(UPDATED_AT_FORMAT)
            normalized.append(entry)
        with self._lock:
            self._read_for_write_locked()
            return self._save_locked(normalized)

    def _read_for_write_locked(self) -> None:
        self._refresh_locked()
        if self._error:
            raise TagDataError(
                "标签库文件损坏或无法读取，已拒绝覆盖；请先修复或删除 "
                f"{self.path}（{self._error}）"
            )

    def _save_locked(self, entries: list[TagEntry]) -> str:
        path = self.path
        directory = os.path.dirname(path)
        payload = {
            "version": TAGS_VERSION,
            "tags": [tag_to_dict(entry) for entry in entries],
        }
        temporary = path + ".tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise TagDataError(f"写入标签库失败：{exc}") from exc
        self._loaded = True
        self._entries = list(entries)
        self._signature = _file_signature(path)
        self._error = ""
        return path

    def _warn(self, message: str, error: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.warn(message, event="tag_library_unreadable", error=error)
        except Exception:  # noqa: BLE001 - 日志失败不能影响对话
            pass


# ---------------------------------------------------------------------------
# 便捷函数 + 进程级当前标签库（与提示词库同一个 configure 时机）
# ---------------------------------------------------------------------------
_TAGS_LOCK = threading.RLock()
_ACTIVE_TAGS: TagCatalog | None = None
_EMPTY_TAGS = TagCatalog("")


def configure_tags(config_dir: str, *, logger: Any = None) -> TagCatalog:
    """绑定进程级标签库（后台启动时与提示词库一起调用）。"""
    global _ACTIVE_TAGS
    catalog = TagCatalog(config_dir, logger=logger) if str(config_dir or "").strip() else TagCatalog("")
    with _TAGS_LOCK:
        _ACTIVE_TAGS = catalog
    return catalog


def active_tags() -> TagCatalog:
    """当前进程级标签库；未 configure 时是空词表（渲染行为与以前一致）。"""
    with _TAGS_LOCK:
        current = _ACTIVE_TAGS
    return current if current is not None else _EMPTY_TAGS


def reset_active_tags() -> None:
    """解除进程级绑定（测试清理用）。"""
    global _ACTIVE_TAGS
    with _TAGS_LOCK:
        _ACTIVE_TAGS = None


def load_tags(config_dir: str) -> list[TagEntry]:
    """读全部词条（文件不存在/损坏时返回空表）。"""
    return TagCatalog(config_dir).load()


def save_tags(config_dir: str, entries: Iterable[TagEntry]) -> str:
    """整表写回词条；返回文件路径。"""
    return TagCatalog(config_dir).save(entries)
