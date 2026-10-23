"""「工具参数」子选项卡的注册表与读写（无 Tk）。

为什么需要这一层：以前桌面端只有「工具能不能调用」这一层，工具自己的参数（例如生图留档
``[image_generation] metadata_store``、搜索通道顺序 ``[web_search] engine_order``）只能
在群里得到"去改 services.toml"的答复。这里把「工具 → 配置键」的映射集中成静态注册表，
界面按工具渲染可展开 / 收起的参数面板；写盘仍走 :mod:`atri_x.gui.configedit` 的行级补丁
（保留注释、写前校验 TOML、留 ``.gui.bak`` 备份）。

模块分工：

* :mod:`atri_x.gui.toolsettings_base`：字段 / 面板类型与「界面值 → TOML 值」转换；
* :mod:`atri_x.gui.toolsettings_general` / :mod:`atri_x.gui.toolsettings_media`：参数定义；
* 本模块：``GROUPS`` 汇总、按工具 / 分类分组、读（:func:`load_all`）与写
  （:func:`save_group`）；
* :mod:`atri_x.gui.toolparams`：把一组参数渲染成可折叠面板。

生效时机：这些键都在 ``app.toml`` / ``services.toml``，**改完要重启后台服务**（与
``state.toml`` 里按群的工具白名单不同，后者 15 秒热加载）。
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from atri_x.gui.configedit import ConfigEditError, ConfigPaths, patch_section, read_toml
from atri_x.gui.toolcatalog import CATEGORY_ORDER, catalog_names, tool_meta
from atri_x.gui.toolsettings_base import (
    FILES,
    KINDS,
    TRILABELS,
    SettingField,
    SettingGroup,
    coerce_value,
    tri_value,
)
from atri_x.gui.toolsettings_general import GENERAL_GROUPS
from atri_x.gui.toolsettings_media import MEDIA_GROUPS

__all__ = [
    "GROUPS",
    "KINDS",
    "TRILABELS",
    "SettingField",
    "SettingGroup",
    "coerce_value",
    "global_groups",
    "group_by_key",
    "groups_by_category",
    "groups_for_tool",
    "has_settings",
    "load_all",
    "load_group",
    "save_group",
    "status_text",
    "tri_value",
    "uncovered_tools",
]

#: 全部参数面板：第一个是「全局参数」，其余按 :data:`GROUPS` 里的顺序排列。
GROUPS: tuple[SettingGroup, ...] = GENERAL_GROUPS + MEDIA_GROUPS

_BY_KEY: dict[str, SettingGroup] = {group.key: group for group in GROUPS}
_BY_TOOL: dict[str, list[SettingGroup]] = {}
for _group in GROUPS:
    for _tool in _group.tools:
        _BY_TOOL.setdefault(_tool, []).append(_group)


def _validate() -> None:
    """导入期自检：命名重复 / 工具名不存在 / 分类缺失都在这里炸出来。"""
    if len(_BY_KEY) != len(GROUPS):
        seen: set[str] = set()
        duplicated: list[str] = []
        for group in GROUPS:
            if group.key in seen:
                duplicated.append(group.key)
            seen.add(group.key)
        raise ValueError(f"工具参数面板 key 重复：{duplicated}")
    names = set(catalog_names())
    for group in GROUPS:
        if not group.fields:
            raise ValueError(f"工具参数面板 {group.key!r} 没有字段")
        keys = [field.key for field in group.fields]
        if len(set(keys)) != len(keys):
            raise ValueError(f"工具参数面板 {group.key!r} 的字段 key 重复：{keys}")
        for field in group.fields:
            if field.kind not in KINDS or field.file not in FILES:
                raise ValueError(f"工具参数 {group.key}.{field.key} 的类型 / 文件非法")
        for tool in group.tools:
            if tool not in names:
                raise ValueError(f"工具参数面板 {group.key!r} 引用了目录里没有的工具：{tool!r}")
    globals_ = [group for group in GROUPS if group.global_scope]
    # 允许**多个**「无工具」面板（1.0.3 起内容安全的 chat / work 覆盖面板也是这个作用域），
    # 只要求第一个是全局参数面板：工具参数页把它展开在最上面，其余无工具面板按 GROUPS
    # 顺序排在它后面、各工具分类之前。
    if not GROUPS[0].global_scope:
        raise ValueError(
            f"全局参数面板必须排在第一个，现在第一个是 {GROUPS[0].key!r}"
            f"（全局作用域面板共 {len(globals_)} 个）"
        )


_validate()


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------
def group_by_key(key: str) -> SettingGroup | None:
    """按面板 id 取定义（找不到返回 ``None``）。"""
    return _BY_KEY.get((key or "").strip())


def global_groups() -> tuple[SettingGroup, ...]:
    """所有「无工具」面板，按 :data:`GROUPS` 顺序（第一个总是「全局参数」）。

    除了 ``global``，还有内容安全的 ``safety_chat`` / ``safety_work``：它们不对应
    任何工具，但是整机生效的配置，排在各工具分类之前渲染。
    """
    return tuple(group for group in GROUPS if group.global_scope)


def groups_for_tool(name: str) -> tuple[SettingGroup, ...]:
    """某个工具对应的参数面板（可能 0 个，也可能多个，例如 image_generate）。"""
    return tuple(_BY_TOOL.get((name or "").strip(), ()))


def has_settings(name: str) -> bool:
    """该工具在「工具参数」页是否有可调参数。"""
    return bool(groups_for_tool(name))


def uncovered_tools() -> list[str]:
    """目录里没有可调参数的工具名（按目录顺序），页面上归到「其余工具」。"""
    return [name for name in catalog_names() if not has_settings(name)]


def _catalog_position(name: str) -> int:
    position = {item: index for index, item in enumerate(catalog_names())}
    return position.get(name, len(position))


def groups_by_category() -> list[tuple[str, list[SettingGroup]]]:
    """按工具目录的分类（联网 / 文件与执行 / 记忆 / …）组织面板，顺序稳定。

    分类顺序取 :data:`atri_x.gui.toolcatalog.CATEGORY_ORDER`；同分类内按"主工具在目录里的
    位置"排（``image_generate`` 的两个面板挨在一起）；全局面板不在这里返回。
    """
    category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    buckets: dict[str, list[tuple[int, int, SettingGroup]]] = {}
    for index, group in enumerate(GROUPS):
        if group.global_scope:
            continue
        primary = group.tools[0]
        category = tool_meta(primary).category
        order = (category_rank.get(category, len(category_rank)), _catalog_position(primary), index)
        buckets.setdefault(category, []).append((order[0], order[1], group))
    result: list[tuple[str, list[SettingGroup]]] = []
    for category in sorted(buckets, key=lambda item: category_rank.get(item, len(category_rank))):
        ordered = sorted(buckets[category], key=lambda item: (item[0], item[1]))
        result.append((category, [group for _rank, _position, group in ordered]))
    return result


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_all(paths: ConfigPaths) -> dict[str, dict[str, Any]]:
    """读所有面板的当前值：``{面板 id: {字段 key: 原始值或 None}}``。

    ``None`` 表示该键在文件里没写（用内置默认）；读取失败抛
    :class:`atri_x.gui.configedit.ConfigEditError`。
    """
    cache: dict[str, Mapping[str, Any]] = {}
    for name in FILES:
        cache[name] = _read_file(_target(paths, name))
    result: dict[str, dict[str, Any]] = {}
    for group in GROUPS:
        result[group.key] = {
            field.key: _section_of(cache[field.file], field.section).get(field.key)
            for field in group.fields
        }
    return result


def load_group(paths: ConfigPaths, group: SettingGroup) -> dict[str, Any]:
    """只读一个面板（保存后用落盘结果回填界面）。"""
    return load_all(paths)[group.key]


def status_text(group: SettingGroup, values: Mapping[str, Any]) -> str:
    """面板标题旁的配置状态：``已配置 3 / 9 项`` / ``未单独配置（默认值）``。"""
    configured = sum(1 for field in group.fields if values.get(field.key) is not None)
    if not configured:
        return "未单独配置（默认值）"
    return f"已配置 {configured} / {len(group.fields)} 项"


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def save_group(
    paths: ConfigPaths, group: SettingGroup, values: Mapping[str, Any]
) -> list[str]:
    """把一个面板的改动写回 TOML，返回真正被改动的文件名（没改动返回空表）。

    ``values`` 的键是字段 key，值是界面原始值（进入本函数后才做类型校验）；
    校验失败抛 :class:`atri_x.gui.configedit.ConfigEditError`，一个字节都不写。

    写之前先做一次**整组**校验（``SettingGroup.validate``）：入参是"文件里现有值 +
    本次改动"的合成结果，因此像"启用 A 却清空了 A 依赖的 B"这种跨字段错误也能在
    写盘前拒绝，而不是等后台服务重启时才炸。
    """
    merged: dict[str, Any] = dict(load_group(paths, group))
    coerced: dict[str, Any] = {}
    for field in group.fields:
        if field.key not in values:
            continue
        coerced[field.key] = coerce_value(field, values[field.key])
        merged[field.key] = coerced[field.key]
    validator = getattr(group, "validate", None)
    if callable(validator):
        validator(merged)
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for field in group.fields:
        if field.key not in coerced:
            continue
        payload = buckets.setdefault((field.file, field.section), {})
        payload[field.key] = coerced[field.key]
    changed: list[str] = []
    for (file_name, section), payload in buckets.items():
        target = _target(paths, file_name)
        if patch_section(target, section, payload, header_comment="桌面端「工具参数」页"):
            changed.append(os.path.basename(target))
    return changed


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _target(paths: ConfigPaths, file_name: str) -> str:
    attribute = FILES.get(file_name)
    if attribute is None:
        raise ConfigEditError(f"未知的配置文件：{file_name!r}")
    return str(getattr(paths, attribute) or "")


def _read_file(path: str) -> Mapping[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    return read_toml(path)


def _section_of(data: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    node: Any = data
    for part in section.split("."):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(part)
    return node if isinstance(node, Mapping) else {}
