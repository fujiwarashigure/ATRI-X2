"""本地表情图库目录（``reactions/catalog.json``）的读取与校验。

图库放在配置目录下的 ``reactions/``::

    reactions/
    ├── catalog.json
    └── assets/
        ├── deadpan_01.webp
        └── smug_01.png

``catalog.json`` 只描述**元数据**（情绪、文件、适用语境、权重、启用状态），
图片本体仍是磁盘上的普通文件。这样模型只需要知道情绪标签，永远不接触磁盘路径、
URL 或 Base64；真正选中的路径由本模块解析并做越界检查后交给交付层。

catalog 的结构::

    {
      "version": 1,
      "items": [
        {
          "id": "altina_deadpan_01",
          "emotion": "无语",
          "path": "assets/deadpan_01.webp",
          "name": "亚尔缇娜·无语",
          "description": "半眯眼，轻微无语",
          "contexts": ["轻松吐槽", "熟人玩笑"],
          "avoid": ["严肃安慰", "真实冲突"],
          "weight": 3,
          "enabled": true
        }
      ]
    }

容错原则（GUI 会手编这个文件，坏一条不能拖垮整个功能）：

* 文件不存在 / JSON 解析失败 → 返回空图库 + ``warnings``，功能静默降级为"不发图"；
* 单条条目非法 → 跳过该条并记一条 warning，其它条目照常可用；
* 图片文件缺失 → 该条不进入候选（不算错误，用户可能还没把图放进来）；
* 路径逃出 ``reactions/`` 目录（``..`` / 绝对路径 / 符号链接）→ 拒绝该条。

``catalog.json`` 里允许出现当前版本不认识的字段：GUI 可能写入说明性字段，
严格拒绝只会让手编文件变成启动故障。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "CATALOG_FILENAME",
    "DEFAULT_ASSETS_DIR",
    "DEFAULT_EMOTIONS",
    "MAX_CATALOG_ITEMS",
    "MAX_WEIGHT",
    "Catalog",
    "CatalogItem",
    "load_catalog",
    "parse_catalog",
    "resolve_gallery_path",
]

#: 图库清单文件名。
CATALOG_FILENAME = "catalog.json"

#: 默认图片子目录名（只是约定，条目里也可以写别的相对路径）。
DEFAULT_ASSETS_DIR = "assets"

#: 功能首次启用时给 GUI/文档用的默认情绪集合（模型提示词与 catalog 的推荐值）。
DEFAULT_EMOTIONS: tuple[str, ...] = (
    "开心",
    "期待",
    "得意",
    "害羞",
    "惊讶",
    "困惑",
    "难过",
    "生气",
    "思考",
    "无奈",
    "无语",
    "嘲笑",
    "ok",
)

#: 单次加载最多接受的条目数：防止误把巨大 JSON 当图库拖慢每轮回复。
MAX_CATALOG_ITEMS = 500

#: 权重上限（只做防呆，避免 ``1e300`` 这类值把随机选择压成恒定）。
MAX_WEIGHT = 1000.0

#: catalog 里文件名的长度上限。
_MAX_PATH_CHARS = 512


@dataclass(frozen=True)
class CatalogItem:
    """图库里的一张表情图。"""

    id: str
    emotion: str
    path: str
    name: str = ""
    description: str = ""
    contexts: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    weight: float = 1.0
    enabled: bool = True

    @property
    def display_name(self) -> str:
        """发送时用的可读名字（原生表情 fallback / 日志）。"""
        return self.name or (self.emotion + "表情")

    def matches_scene(self, scene: frozenset[str]) -> bool:
        """按场景标签判断这张图是否可用。

        没有任何场景信息时一律可用；给了场景标签时，“命中 avoid”直接排除，
        条目声明了 contexts 则至少命中一个才可用。场景标签目前由调用方
        （第二阶段的核心层）提供，Hook/轻量集成不传，等于不过滤。
        """
        if not scene:
            return True
        if any(tag in scene for tag in self.avoid):
            return False
        if self.contexts and not any(tag in scene for tag in self.contexts):
            return False
        return True


@dataclass
class Catalog:
    """一份加载完成的图库。"""

    items: list[CatalogItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def emotions(self) -> list[str]:
        """图库里出现过的情绪（按首次出现顺序，去重）。"""
        seen: list[str] = []
        for item in self.items:
            if item.emotion not in seen:
                seen.append(item.emotion)
        return seen

    def items_for(self, emotion: str) -> list[CatalogItem]:
        """某个情绪下全部可用条目。"""
        label = (emotion or "").strip().casefold()
        if not label:
            return []
        return [item for item in self.items if item.emotion.casefold() == label]

    def ids(self) -> list[str]:
        return [item.id for item in self.items]

    def __bool__(self) -> bool:
        return bool(self.items)


def load_catalog(
    gallery_dir: str,
    *,
    filename: str = CATALOG_FILENAME,
    check_files: bool = True,
) -> Catalog:
    """读取 ``<gallery_dir>/<filename>`` 并返回图库。

    读取失败（文件缺失、JSON 坏、编码错）不会抛异常：返回空图库并把原因写进
    ``warnings``，让上层按"没有可用表情"降级。真正的 IO 意外（权限等）同样
    吞掉 —— 一条表情图的失败不应该让整轮对话失败。
    """
    base = os.path.abspath(gallery_dir or "")
    path = os.path.join(base, filename or CATALOG_FILENAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return Catalog(warnings=[f"catalog not found: {path}"])
    except (OSError, ValueError, UnicodeError) as exc:
        return Catalog(warnings=[f"read catalog {path}: {exc}"])
    return parse_catalog(raw, base, check_files=check_files)


def parse_catalog(
    raw: Any,
    gallery_dir: str,
    *,
    check_files: bool = True,
) -> Catalog:
    """把已解析的 JSON 对象规范成 :class:`Catalog`（GUI 保存前也可复用）。"""
    base = os.path.abspath(gallery_dir or "")
    if not isinstance(raw, Mapping):
        return Catalog(warnings=["catalog must be a JSON object"])
    items_raw = raw.get("items")
    if items_raw is None:
        return Catalog(warnings=["catalog has no items"])
    if not isinstance(items_raw, list):
        return Catalog(warnings=["catalog.items must be an array"])

    catalog = Catalog()
    seen_ids: set[str] = set()
    for index, value in enumerate(items_raw):
        if index >= MAX_CATALOG_ITEMS:
            catalog.warnings.append(
                f"catalog truncated after {MAX_CATALOG_ITEMS} items"
            )
            break
        item, problem = _parse_item(value, base, index, check_files=check_files)
        if item is None:
            catalog.warnings.append(problem)
            continue
        if item.id in seen_ids:
            catalog.warnings.append(f"items[{index}]: duplicate id {item.id!r}")
            continue
        seen_ids.add(item.id)
        catalog.items.append(item)
    return catalog


def resolve_gallery_path(gallery_dir: str, relative: str) -> str | None:
    """把条目里的相对路径解析成绝对路径；越界或非法返回 ``None``。"""
    rel = (relative or "").strip()
    if not rel or len(rel) > _MAX_PATH_CHARS:
        return None
    if "\x00" in rel or rel.startswith(("/", "\\")):
        return None
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        return None
    base = os.path.realpath(os.path.abspath(gallery_dir or ""))
    candidate = os.path.realpath(os.path.join(base, rel))
    try:
        common = os.path.commonpath((base, candidate))
    except ValueError:  # 不同盘符（Windows）
        return None
    if common != base:
        return None
    return candidate


def _parse_item(
    value: Any, gallery_dir: str, index: int, *, check_files: bool
) -> tuple[CatalogItem | None, str]:
    """解析单条条目，返回 ``(item, problem)``。"""
    where = f"items[{index}]"
    if not isinstance(value, Mapping):
        return None, f"{where}: item must be an object"
    item_id = _text(value.get("id"), 64)
    if not item_id:
        return None, f"{where}: missing id"
    emotion = _text(value.get("emotion"), 24)
    if not emotion:
        return None, f"{where}: missing emotion"
    relative = _text(value.get("path"), _MAX_PATH_CHARS)
    if not relative:
        return None, f"{where}: missing path"
    resolved = resolve_gallery_path(gallery_dir, relative)
    if resolved is None:
        return None, f"{where}: path escapes gallery directory: {relative!r}"
    if check_files and not os.path.isfile(resolved):
        return None, f"{where}: image file not found: {relative!r}"
    weight = _number(value.get("weight"), 1.0)
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() not in ("false", "0", "no", "off")
    contexts = _text_list(value.get("contexts"))
    avoid = _text_list(value.get("avoid"))
    return (
        CatalogItem(
            id=item_id,
            emotion=emotion,
            path=resolved,
            name=_text(value.get("name"), 64),
            description=_text(value.get("description"), 240),
            contexts=contexts,
            avoid=avoid,
            weight=weight,
            enabled=bool(enabled) and weight > 0,
        ),
        "",
    )


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:limit]


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):  # NaN/Inf
        return default
    return max(0.0, min(MAX_WEIGHT, number))


def _text_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable) or isinstance(value, (bytes, Mapping)):
        return ()
    out: list[str] = []
    for entry in value:
        text = _text(entry, 32)
        if text and text not in out:
            out.append(text)
    return tuple(out)
