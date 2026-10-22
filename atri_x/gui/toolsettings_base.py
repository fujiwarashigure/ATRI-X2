"""工具参数的字段类型与「界面值 → TOML 值」转换。

为什么单独一层：「工具参数」子选项卡的**参数定义**（:mod:`atri_x.gui.toolsettings_general`
与 :mod:`atri_x.gui.toolsettings_media`）、**读写**（:mod:`atri_x.gui.toolsettings`）和
**界面渲染**（:mod:`atri_x.gui.toolparams`）用的是同一套字段规则。把类型放在这里，
数据模块只依赖本模块，就不会和读写模块互相 import 成环。

字段类型（``SettingField.kind``）与界面控件的对应：

| kind | 界面控件 | 写回 TOML |
|---|---|---|
| ``bool`` | 复选框 | ``true`` / ``false`` |
| ``tristate`` | 下拉（默认 / 开 / 关） | 默认 = 删掉该键，开 / 关 = 布尔 |
| ``int`` / ``float`` | 输入框 | 数字；留空 = 删掉该键 |
| ``str`` | 输入框 | 字符串（留空也写空串，语义是"清空"） |
| ``text`` | 多行文本框 | 字符串 |
| ``choice`` | 下拉 | 候选值；选「（默认）」= 删掉该键 |
| ``list`` | 输入框（逗号 / 顿号分隔） | 字符串数组；留空按 ``empty`` 规则 |
| ``path`` | 输入框 + 「浏览…」 | 字符串；留空 = 删掉该键 |

``empty`` 只对 ``list`` 生效：``delete``（留空删掉键，等于用内置默认链）或
``empty_list``（留空写成 ``[]``，语义是"一个都不允许"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from atri_x.gui.configedit import ConfigEditError

__all__ = [
    "FILES",
    "KINDS",
    "TRILABELS",
    "SettingField",
    "SettingGroup",
    "coerce_value",
    "tri_value",
]

#: 支持写入的配置文件：``app.toml`` / ``services.toml``（都在配置目录里）。
FILES: dict[str, str] = {"app": "config_path", "services": "services_path"}

#: 支持的字段类型（与界面控件的对应见模块 docstring）。
KINDS = frozenset(
    {"bool", "tristate", "int", "float", "str", "text", "choice", "list", "path"}
)

#: 三态字段的界面标签 → 数据层的内部值。
TRILABELS: tuple[tuple[str, str], ...] = (
    ("default", "（默认）"),
    ("on", "开"),
    ("off", "关"),
)
_TRI_BY_LABEL = {label: value for value, label in TRILABELS}

#: 列表字段里表示"空数组"的特殊写法（否则留空按 ``empty`` 规则处理）。
_EMPTY_LIST_TOKENS = frozenset({"none", "[]", "空", "全拒"})


@dataclass(frozen=True)
class SettingField:
    """一个可编辑的配置键：界面标签 + TOML 位置 + 类型与默认值。"""

    #: 配置键名（同时也是面板内部 ``values`` 字典的键；同一组内必须唯一）。
    key: str
    #: 界面上的中文标签。
    label: str
    #: 字段类型，取值见 :data:`KINDS`。
    kind: str = "str"
    #: TOML 小节（点号分隔，例如 ``image_generation.artist_library``）。
    section: str = ""
    #: 写在哪个文件：``app`` / ``services``。
    file: str = "app"
    #: 未配置时生效的默认值（只用于界面提示，不写回文件）。
    default: Any = None
    #: 下拉候选 ``[(值, 界面标签), ...]``；``choice`` 类型必填。
    choices: tuple[tuple[str, str], ...] = ()
    #: 一行中文说明（会跟"写入哪个文件哪个小节"一起显示）。
    help: str = ""
    #: 列表字段留空的语义：``delete`` / ``empty_list``。
    empty: str = "delete"
    #: ``path`` 字段的「浏览…」是选文件还是选目录。
    path_kind: str = "file"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"工具参数 {self.key!r} 的类型未知：{self.kind!r}")
        if self.file not in FILES:
            raise ValueError(f"工具参数 {self.key!r} 的配置文件未知：{self.file!r}")
        if not self.section.strip():
            raise ValueError(f"工具参数 {self.key!r} 没有写 TOML 小节")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"下拉参数 {self.key!r} 没有候选值")
        if self.empty not in ("delete", "empty_list"):
            raise ValueError(f"工具参数 {self.key!r} 的 empty 只能是 delete / empty_list")
        if self.path_kind not in ("file", "dir"):
            raise ValueError(f"工具参数 {self.key!r} 的 path_kind 只能是 file / dir")

    @property
    def file_name(self) -> str:
        """写回的文件名（界面提示用）。"""
        return "app.toml" if self.file == "app" else "services.toml"

    @property
    def location(self) -> str:
        """``app.toml [tools]`` 这种给用户看的位置说明。"""
        return f"{self.file_name} [{self.section}] {self.key}"

    def choice_label(self, value: str) -> str:
        """候选值 → 界面标签（找不到就原样返回）。"""
        for item, label in self.choices:
            if item == value:
                return label
        return value

    def choice_value(self, label: str) -> str:
        """界面标签 → 候选值（找不到就原样返回，交给 :func:`coerce_value` 校验）。"""
        for item, text in self.choices:
            if text == label:
                return item
        return label


@dataclass(frozen=True)
class SettingGroup:
    """一组参数 = 界面上一个可展开 / 收起的面板（通常对应一个工具或一族工具）。"""

    #: 面板 id（唯一；写进日志和测试断言）。
    key: str
    #: 面板标题（中文）。
    title: str
    #: 参数影响的工具名；空元组表示"所有工具"（全局参数）。
    tools: tuple[str, ...] = ()
    #: 面板里的字段（顺序即界面顺序）。
    fields: tuple[SettingField, ...] = ()
    #: 面板底部的一段补充说明（可选）。
    note: str = ""
    #: 这些参数在别的界面页也能改时的提示（可选）。
    where: str = ""
    #: 跨字段校验（可选）：入参是"改动后的整组值"（键 = 字段 key），违规抛
    #: :class:`~atri_x.gui.configedit.ConfigEditError`。保存前调用，失败时一个字节都不写。
    #:
    #: 存在的理由：单字段类型校验看不出"启用某功能但没填它依赖的键"这类组合错误。
    #: 真机事故：在「图片反推提示词」面板只把「启用反推」拨到「开」、没填 provider /
    #: model，界面照样写盘；后台服务下次重启时
    #: :func:`atri_x.app.image_to_prompt.build_image_prompt_service` 直接抛
    #: ``[image_to_prompt] is enabled but incomplete`` 起不来。
    validate: Callable[[Mapping[str, Any]], None] | None = None

    @property
    def global_scope(self) -> bool:
        """是否是"全局参数"面板（不对应单个工具）。"""
        return not self.tools

    @property
    def tools_label(self) -> str:
        """面板标题右边那行"影响哪些工具"。"""
        return " / ".join(self.tools) if self.tools else "所有工具"


def tri_value(raw: Any) -> bool | None:
    """把三态字段的界面值转成 ``None`` / ``True`` / ``False``。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    if text in ("", "default", "（默认）", "默认"):
        return None
    if text in ("on", "true", "1", "开", "是"):
        return True
    if text in ("off", "false", "0", "关", "否"):
        return False
    raise ConfigEditError(f"三态参数只能是「默认 / 开 / 关」：{text!r}")


def coerce_value(field: SettingField, raw: Any) -> Any:
    """把界面给的原始值转成要写进 TOML 的值；``None`` 表示删除该键。

    界面上可预期的输入错误（不是整数、下拉值不在候选里）抛
    :class:`atri_x.gui.configedit.ConfigEditError`，消息直接给用户看。
    """
    kind = field.kind
    if kind == "bool":
        if raw is None:
            return False
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "on", "yes", "是", "开"):
            return True
        if text in ("false", "0", "off", "no", "否", "关", ""):
            return False
        raise ConfigEditError(f"「{field.label}」只能是开 / 关：{raw!r}")
    if kind == "tristate":
        return tri_value(raw)
    if kind == "int":
        return _coerce_number(field, raw, int)
    if kind == "float":
        return _coerce_number(field, raw, float)
    if kind == "choice":
        text = ("" if raw is None else str(raw)).strip()
        if not text:
            return None
        valid = [item for item, _label in field.choices]
        if text not in valid:
            labels = "、".join(
                f"{label}（{item}）" if item else label for item, label in field.choices
            )
            raise ConfigEditError(
                f"「{field.label}」不支持 {text!r}；可选：{labels}"
            )
        return text
    if kind == "list":
        return _coerce_list(field, raw)
    if kind == "path":
        text = ("" if raw is None else str(raw)).strip()
        return text or None
    # str / text：留空也写空串（"清空这个提示词"是有意义的状态）。
    return "" if raw is None else str(raw).strip()


def _coerce_number(field: SettingField, raw: Any, kind: type) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ConfigEditError(f"「{field.label}」要填数字，不是开关：{raw!r}")
    text = str(raw).strip().replace(",", "").replace("_", "")
    if not text:
        return None
    try:
        return kind(text)
    except (TypeError, ValueError) as exc:
        name = "整数" if kind is int else "数字"
        raise ConfigEditError(f"「{field.label}」要填{name}：{raw!r}") from exc


def _coerce_list(field: SettingField, raw: Any) -> list[str] | None:
    if isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw if str(item).strip()]
    else:
        text = "" if raw is None else str(raw).strip()
        if not text:
            items = []
        elif text.lower() in _EMPTY_LIST_TOKENS:
            return []
        else:
            for separator in ("，", "、", "；", ";", "\n", "\t", " "):
                text = text.replace(separator, ",")
            items = [part.strip() for part in text.split(",") if part.strip()]
    if items:
        return items
    return None if field.empty == "delete" else []
