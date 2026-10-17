"""把 preset 渲染成某个方言下的最终画师串。

对应 Go：``internal/artist/render.py`` 的上游 ``internal/artist/render.go``。

两条不可动摇的规则（与 Go 注释里的意图一致）：

* **权重不能静默丢弃**：不表达权重的方言（``tags`` / ``natural``）会记一条 warning，
  运维才能区分"权重生效了"和"权重被忽略了"。
* **顺序原样保留**：这里不排序、不去重——重排画师串会改变生成结果。
* 已存的 ``raw_string`` 逐字保留：重新渲染会把权重近似掉，等于改用户的原文。

与 Go 的差异：

* Go 的 ``formatWarnings`` 用 ``fmt.Sprintf`` + 去重；Python 侧用 :meth:`_RenderWarnings.add`
  明确接收格式化后的文本（避免 ``%`` 与 Go 动词混用）。
* ``strconv.FormatFloat(w, 'f', 3, 64)`` → ``f"{w:.3f}"`` 后裁尾零，语义一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .parser import BRACE_WEIGHT, escape
from .types import Dialect, Preset

#: 显式权重范围（与 store/parser 口径一致）。对应 Go ``MinWeight`` / ``MaxWeight``。
MIN_WEIGHT = 0.01
MAX_WEIGHT = 10.0

#: NovelAI 花括号/方括号阶梯的最大层数。对应 Go ``naiLadderLevels``。
NAI_LADDER_LEVELS = 5


@dataclass
class WeightedTag:
    """一个标签 + 它应带的权重。对应 Go ``WeightedTag``。"""

    tag: str = ""
    weight: float = 1.0


@dataclass
class Render:
    """某个方言下编译好的 preset。对应 Go ``Render``。"""

    dialect: Dialect = Dialect.AUTO
    positive: str = ""
    negative: str = ""
    artist_raw: str = ""
    warnings: list[str] = field(default_factory=list)


class _RenderWarnings:
    """累加 warning 并去重。对应 Go ``renderWarnings``。"""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        if text == "" or text in self.items:
            return
        self.items.append(text)


def neutral_weight(weight: float) -> bool:
    """权重是否是"未加权"的默认值。对应 Go ``neutralWeight``。"""
    return abs(weight - 1) < 0.005


def format_weight(weight: float) -> str:
    """紧凑且无歧义地渲染权重。对应 Go ``FormatWeight``。"""
    if math.isnan(weight) or math.isinf(weight):
        return "1"
    if weight < MIN_WEIGHT:
        weight = MIN_WEIGHT
    if weight > MAX_WEIGHT:
        weight = MAX_WEIGHT
    text = f"{weight:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def render_tag(tag: str, weight: float, dialect: Dialect) -> tuple[str, list[str]]:
    """按方言渲染一个标签。对应 Go ``RenderTag``。"""
    tag = (tag or "").strip()
    if tag == "":
        return "", []
    warnings = _RenderWarnings()
    if neutral_weight(weight):
        return escape(tag), warnings.items
    if dialect == Dialect.SD_WEIGHTED:
        return f"({escape(tag)}:{format_weight(weight)})", warnings.items
    if dialect == Dialect.NAI:
        return _render_nai_tag(tag, weight, warnings), warnings.items
    if dialect == Dialect.TAGS:
        warnings.add(
            f"方言「{dialect.label()}」不能表达单个标签的权重，"
            f"{tag} 的权重 {format_weight(weight)} 已被忽略。"
        )
        return escape(tag), warnings.items
    if dialect == Dialect.NATURAL:
        warnings.add(
            f"方言「{dialect.label()}」不能表达单个标签的权重，"
            f"{tag} 的权重 {format_weight(weight)} 已改为文字强调。"
        )
        return "重点参考 " + tag, warnings.items
    warnings.add(
        f"未指定提示词方言，{tag} 的权重 {format_weight(weight)} "
        "按 SD 加权语法输出，请按目标模型核对。"
    )
    return f"({escape(tag)}:{format_weight(weight)})", warnings.items


def _render_nai_tag(tag: str, weight: float, warnings: _RenderWarnings) -> str:
    """用 NovelAI 的括号阶梯表达权重。对应 Go ``renderNAITag``。"""
    step = BRACE_WEIGHT
    opener, closer = "{", "}"
    if weight < 1:
        step = 1 / BRACE_WEIGHT
        opener, closer = "[", "]"
    levels = int(round(math.log(weight) / math.log(step)))
    if levels < 1 or levels > NAI_LADDER_LEVELS:
        warnings.add(
            f"NovelAI 权重语法随版本变化，{tag} 的权重 {format_weight(weight)} "
            "已按 (tag:weight) 输出，请按你的 NovelAI 版本核对。"
        )
        return f"({escape(tag)}:{format_weight(weight)})"
    achieved = math.pow(step, float(levels))
    if abs(achieved - weight) / weight > 0.02:
        ladder = "大括号" if opener == "{" else "方括号"
        warnings.add(
            f"NovelAI 的{ladder}只支持固定步进，{tag} 的权重 "
            f"{format_weight(weight)} 实际为 {format_weight(achieved)}。"
        )
    return opener * levels + escape(tag) + closer * levels


def render_tags(tags: list[WeightedTag], dialect: Dialect) -> tuple[str, list[str]]:
    """按顺序渲染标签列表。对应 Go ``RenderTags``。"""
    parts: list[str] = []
    warnings = _RenderWarnings()
    for item in tags or []:
        rendered, item_warnings = render_tag(item.tag, item.weight, dialect)
        if rendered == "":
            continue
        parts.append(rendered)
        for warning in item_warnings:
            warnings.add(warning)
    return ", ".join(parts), warnings.items


def render_preset(
    preset: Preset,
    lookup: Callable[[str], "tuple[str, bool]"] | None,
    dialect: Dialect,
) -> Render:
    """把一个 preset 编译成某方言下的正/负向画师串。对应 Go ``RenderPreset``。"""
    render = Render(dialect=dialect, artist_raw=(preset.raw_string or "").strip())
    if not dialect.valid() or dialect == Dialect.AUTO:
        render.dialect = Dialect.AUTO
    warnings = _RenderWarnings()

    tags: list[WeightedTag] = []
    missing: list[str] = []
    for slot in preset.artists or []:
        weight = slot.weight
        if weight <= 0:
            weight = 1.0
        tag = ""
        if lookup is not None:
            resolved, ok = lookup(slot.artist_id)
            if ok:
                tag = resolved
        if (tag or "").strip() == "":
            missing.append(slot.artist_id)
            continue
        tags.append(WeightedTag(tag=tag, weight=weight))
    if missing:
        warnings.add("组合引用的画师标签已从目录里删除：" + "、".join(missing) + "。")

    artist_text, artist_warnings = render_tags(tags, render.dialect)
    positive: list[str] = []
    if artist_text != "":
        if render.dialect == Dialect.NATURAL:
            # 自然语言目标吃不下加权标签串，改写成一句风格描述。
            positive.append("画风参考：" + artist_text.replace(", ", "、"))
        else:
            positive.append(artist_text)
    extra, extra_warnings = render_tags(_tag_list(preset.positive_tags), render.dialect)
    if extra != "":
        positive.append(extra)
        for warning in extra_warnings:
            warnings.add(warning)
    for warning in artist_warnings:
        warnings.add(warning)
    render.positive = ", ".join(positive)

    negative, negative_warnings = render_tags(
        _tag_list(preset.negative_tags), render.dialect
    )
    render.negative = negative
    for warning in negative_warnings:
        warnings.add(warning)
    if not tags and render.artist_raw == "" and render.positive == "":
        warnings.add(f"组合「{preset.name}」没有可用的画师标签或正向标签。")
    render.warnings = warnings.items
    return render


def _tag_list(values: list[str]) -> list[WeightedTag]:
    out: list[WeightedTag] = []
    for value in values or []:
        if (value or "").strip() == "":
            continue
        out.append(WeightedTag(tag=value, weight=1.0))
    return out


def describe_style(preset: Preset) -> str:
    """取风格描述，退到 raw_string，再退到名字。对应 Go ``DescribeStyle``。"""
    value = (preset.style_description or "").strip()
    if value != "":
        return value
    value = (preset.raw_string or "").strip()
    if value != "":
        return value
    return preset.name


__all__ = [name for name in dir() if not name.startswith("_")]
