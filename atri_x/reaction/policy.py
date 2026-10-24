"""表情发送策略：启用范围、场景抑制、冷却、去重与概率（Python 侧新增，无 Go 对应）。

设计原则和功能文档一致：**概率只做最后一道调节，不是核心决策。**

决策顺序（见 :meth:`atri_x.reaction.service.ReactionService.decide`）::

    启用范围（群/私聊） → 场景抑制（长文、严肃关键词）
      → 会话文字回复冷却 → 会话/群时间冷却
      → 情绪匹配 → 最近图片去重 → 权重随机 → 概率

``[reactions]`` 小节的字段与 :class:`ReactionSettings` 一一对应；各字段都做了
容错解析（类型不对、越界都回落默认值），因为这份配置会同时被 GUI 和手编 TOML
写入，一条写坏不应该让整个功能失效。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping

__all__ = [
    "MAX_SUPPRESS_KEYWORDS",
    "ReactionSettings",
    "normalize_suppress_keywords",
]

#: ``suppress_keywords`` 最多保留的条数（防呆）。
MAX_SUPPRESS_KEYWORDS = 64

#: 默认抑制词：报错、道歉、明确拒绝这类场景不该配轻松的表情。
_DEFAULT_SUPPRESS_KEYWORDS: tuple[str, ...] = (
    "错误",
    "报错",
    "失败",
    "异常",
    "抱歉",
    "对不起",
    "无法",
    "不能",
)


@dataclass(frozen=True)
class ReactionSettings:
    """``[reactions]`` 的规范化结果（全部字段都有安全默认值）。"""

    #: 总开关。默认关：新装用户手动在 GUI 里打开，避免"一升级就刷图"。
    enabled: bool = False
    #: 图库目录（相对配置目录；绝对路径也接受，见配置层）。
    gallery_dir: str = "reactions"
    #: 群聊 / 私聊是否允许发表情。
    show_in_group: bool = True
    show_in_private: bool = True
    #: 发图之后，至少还要经过几条"不带图"的回复才允许再发。
    cooldown_text_replies: int = 2
    #: 同一会话两次发图的最短间隔（秒）。
    session_cooldown_seconds: int = 60
    #: 同一群两次发图的最短间隔（秒）；私聊忽略。
    group_cooldown_seconds: int = 120
    #: 最近多少张图不重复（0 = 不做去重）。
    dedupe_recent: int = 5
    #: 通过全部检查后的发送概率（0.0 ~ 1.0，1.0 = 一定发）。
    probability: float = 1.0
    #: 正文超过这个字符数就不配图（0 = 不限制）；长文通常是正经说明。
    max_text_chars: int = 160
    #: 命中了就不配图的关键词（严肃安慰、报错通知等场景的兜底）。
    suppress_keywords: tuple[str, ...] = _DEFAULT_SUPPRESS_KEYWORDS

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_config(cls, section: Mapping[str, Any] | None) -> "ReactionSettings":
        """从 ``[reactions]`` 小节字典构造（缺字段/类型不对都回落默认值）。"""
        data: Mapping[str, Any] = section if isinstance(section, Mapping) else {}
        defaults = cls()
        keyword_values = data.get("suppress_keywords")
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            gallery_dir=_as_text(data.get("gallery_dir"), defaults.gallery_dir),
            show_in_group=_as_bool(
                data.get("show_in_group"), defaults.show_in_group
            ),
            show_in_private=_as_bool(
                data.get("show_in_private"), defaults.show_in_private
            ),
            cooldown_text_replies=_as_int(
                data.get("cooldown_text_replies"),
                defaults.cooldown_text_replies,
                minimum=0,
                maximum=20,
            ),
            session_cooldown_seconds=_as_int(
                data.get("session_cooldown_seconds"),
                defaults.session_cooldown_seconds,
                minimum=0,
                maximum=86400,
            ),
            group_cooldown_seconds=_as_int(
                data.get("group_cooldown_seconds"),
                defaults.group_cooldown_seconds,
                minimum=0,
                maximum=86400,
            ),
            dedupe_recent=_as_int(
                data.get("dedupe_recent"),
                defaults.dedupe_recent,
                minimum=0,
                maximum=50,
            ),
            probability=_as_float(
                data.get("probability"), defaults.probability, minimum=0.0, maximum=1.0
            ),
            max_text_chars=_as_int(
                data.get("max_text_chars"),
                defaults.max_text_chars,
                minimum=0,
                maximum=4000,
            ),
            suppress_keywords=normalize_suppress_keywords(
                keyword_values,
                fallback=defaults.suppress_keywords,
            ),
        )

    # -- 查询 ---------------------------------------------------------------
    def allows_group(self) -> bool:
        return self.enabled and self.show_in_group

    def allows_private(self) -> bool:
        return self.enabled and self.show_in_private

    def suppression_reason(self, text: str) -> str:
        """正文是否命中"不适合配图"的场景；命中返回原因，否则返回空串。"""
        body = (text or "").strip()
        if not body:
            return "empty_text"
        if self.max_text_chars > 0 and len(body) > self.max_text_chars:
            return "long_text"
        for keyword in self.suppress_keywords:
            if keyword and keyword in body:
                return "suppress_keyword:" + keyword
        return ""

    def to_config(self) -> dict[str, Any]:
        """把当前设置渲染成可写回 ``[reactions]`` 的字典（GUI 保存用）。"""
        out: dict[str, Any] = {}
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if name == "suppress_keywords":
                out[name] = list(value)
            else:
                out[name] = value
        return out


def normalize_suppress_keywords(
    value: Any, *, fallback: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """把配置里的关键词列表规范成去重、去空白、有上限的元组。

    ``[]``（显式空列表）表示"不启用抑制词"，与"字段缺失"不同 —— 后者沿用
    ``fallback``。GUI 勾掉全部关键词时写的就是空列表。
    """
    if value is None:
        return tuple(fallback)
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (bytes, Mapping)) or not isinstance(value, (list, tuple, set)):
        return tuple(fallback)
    out: list[str] = []
    for entry in value:
        text = str(entry or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_SUPPRESS_KEYWORDS:
            break
    return tuple(out)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return max(minimum, min(maximum, number))


def _as_text(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or default
