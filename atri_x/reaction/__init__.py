"""本地表情图库与情绪选择器（Python 侧新增，无 Go 对应）。

一个"轻量反应层"：模型只在正文末尾提出一个情绪意图标记，后端负责把标记
从正文里清掉，再按配置决定**这次要不要补一张图、补哪张**。

分层：

* :mod:`atri_x.reaction.marker` —— 标记解析与清除（纯文本，无 IO）；
* :mod:`atri_x.reaction.catalog` —— ``reactions/catalog.json`` 图库读取与路径校验；
* :mod:`atri_x.reaction.policy` —— ``[reactions]`` 配置的规范化与场景抑制；
* :mod:`atri_x.reaction.service` —— 决策与冷却/去重状态（进程内，无落库）。

真正的发送仍走原有交付层：调用方把选中的 :class:`ReactionPick` 变成一条
``kind = image``、``timing = after_assistant`` 的输出，由 Output Manager 统一投递。
"""

from __future__ import annotations

from atri_x.reaction.catalog import (
    CATALOG_FILENAME,
    DEFAULT_ASSETS_DIR,
    DEFAULT_EMOTIONS,
    Catalog,
    CatalogItem,
    load_catalog,
    parse_catalog,
    resolve_gallery_path,
)
from atri_x.reaction.marker import (
    MAX_EMOTION_RUNES,
    MAX_EMOTIONS_PER_REPLY,
    MarkerParse,
    extract_marker,
    has_marker,
    marker_tag,
)
from atri_x.reaction.policy import ReactionSettings
from atri_x.reaction.service import Decision, ReactionPick, ReactionService

__all__ = [
    "CATALOG_FILENAME",
    "DEFAULT_ASSETS_DIR",
    "DEFAULT_EMOTIONS",
    "MAX_EMOTIONS_PER_REPLY",
    "MAX_EMOTION_RUNES",
    "Catalog",
    "CatalogItem",
    "Decision",
    "MarkerParse",
    "ReactionPick",
    "ReactionService",
    "ReactionSettings",
    "extract_marker",
    "has_marker",
    "load_catalog",
    "marker_tag",
    "parse_catalog",
    "resolve_gallery_path",
]
