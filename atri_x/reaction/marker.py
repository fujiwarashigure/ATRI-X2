"""表情意图标记的解析与清除（Python 侧新增，无 Go 对应）。

模型只在回复正文里追加一个约定标记，例如::

    这个需求已经从「修一个小问题」进化成「重写整个项目」了吧！[[emo:无语]]

后端在把这段正文交给平台之前调用 :func:`extract_marker`：

* 标记（含同一段正文里可能重复出现的多个）从正文里全部删除，正文其余部分
  的换行与空白尽量保持原样；
* 返回去重后的情绪标签，由反应层（:mod:`atri_x.reaction.service`）决定
  「这次要不要发、发哪张图」。

标记格式刻意收得很紧：``[[emo:无语]]`` / ``[[表情:无语]]`` / ``[[emotion：无语]]``
都接受（大小写、中文冒号、标记内空白都容忍），但标签最长 24 个字符且不许跨行，
避免把模型正常的方括号文本（例如 ``[[1]]`` 引用标记、代码块里的 ``[[x]]``）
误当成表情标记删掉。

标记本身**从不出现在平台正文与会话历史里**：调用方拿 ``MarkerParse.text`` 作为
后续一切可见/落库文本，``MarkerParse.emotions`` 只用于选图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "MAX_EMOTION_RUNES",
    "MAX_EMOTIONS_PER_REPLY",
    "MarkerParse",
    "extract_marker",
    "has_marker",
    "marker_tag",
]

#: 单个情绪标签允许的最大字符数（超过就不当成标记，避免误删正文）。
MAX_EMOTION_RUNES = 24

#: 同一段正文里最多采纳的情绪意图数（正常只会有一个，多写是模型抖动）。
MAX_EMOTIONS_PER_REPLY = 4

#: 标记名前缀。``[[emo:x]]`` 是主写法；其余是国内模型常见的等价写法。
_MARKER_NAMES = ("emo", "emotion", "表情", "情绪")

#: 一个完整的标记：``[[`` + 名字 + ``:``/``：`` + 标签 + ``]]``。
#: 标记内部只允许水平空白（不能跨行），否则模型多行输出会被误删。
_HS = r"[^\S\r\n]"
_MARKER_RE = re.compile(
    r"\[\[" + _HS + r"*(?:" + "|".join(_MARKER_NAMES) + r")" + _HS + r"*[:：]"
    + _HS + r"*([^\[\]\r\n]{1,%d}?)" % MAX_EMOTION_RUNES
    + _HS + r"*\]\]",
    re.IGNORECASE,
)

#: 连续空行折叠上限：删完标记留下的空行最多保留一个空行。
_BLANK_RUN_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class MarkerParse:
    """一次标记解析的结果。

    :param text: 去掉全部标记后的正文（已做最小化的空行/行尾清理）。
    :param emotions: 去重后的情绪标签（按出现顺序）。
    :param found: 原文里匹配到的标记个数（可能大于 ``len(emotions)``）。
    """

    text: str = ""
    emotions: tuple[str, ...] = ()
    found: int = 0

    @property
    def changed(self) -> bool:
        """正文是否因为去标记而发生了变化。"""
        return self.found > 0

    @property
    def primary(self) -> str:
        """主情绪：最后一个标签（模型按"先正文、后标记"的顺序写，最后一个最贴近）。"""
        return self.emotions[-1] if self.emotions else ""


def extract_marker(value: str) -> MarkerParse:
    """解析正文里的表情意图标记并返回去标记后的正文。"""
    text = value or ""
    emotions: list[str] = []
    seen: set[str] = set()
    found = 0
    for match in _MARKER_RE.finditer(text):
        found += 1
        label = (match.group(1) or "").strip()
        if not label:
            continue
        key = label.casefold()
        if key in seen or len(emotions) >= MAX_EMOTIONS_PER_REPLY:
            continue
        seen.add(key)
        emotions.append(label)
    if not found:
        return MarkerParse(text=text, emotions=(), found=0)
    cleaned = _MARKER_RE.sub("", text)
    return MarkerParse(text=_tidy(cleaned), emotions=tuple(emotions), found=found)


def has_marker(value: str) -> bool:
    """正文里是否出现至少一个表情意图标记。"""
    return bool(_MARKER_RE.search(value or ""))


def marker_tag(emotion: str) -> str:
    """把情绪标签渲染回标准标记写法（供提示词/文档/测试复用）。"""
    label = (emotion or "").strip()
    return "[[" + "emo" + ":" + label + "]]" if label else ""


def _tidy(text: str) -> str:
    """去标记后的最小清理：行尾空白 + 折叠连续空行，不动正文其它排版。"""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    cleaned = _BLANK_RUN_RE.sub("\n\n", "\n".join(lines))
    return cleaned.strip()
