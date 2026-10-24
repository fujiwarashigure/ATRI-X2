"""表情反应的决策与状态：要不要发、发哪张、冷却与去重。

本模块把三件事收敛到一处：

1. **决策**（:meth:`ReactionService.decide`）——只看配置、正文、情绪标签与历史状态，
   返回"选中的图 / 不发的理由"，不做任何 IO 或发送；
2. **状态**（会话冷却、群冷却、最近发过的图）——进程内保存，不落库；进程重启后
   冷却重置属于可接受行为（宁可重启后多发一张，也不要写坏用户数据）；
3. **提交**（:meth:`ReactionService.commit_image` / :meth:`note_text_reply`）——
   由调用方在真正把图片交给平台之后/之后调用，保证"发失败不占冷却"的设计意图在
   调用方有可靠回执时成立。

调用方（Agent 回合输出）只负责把 ``ReactionPick.path`` 变成一条
``timing = "after_assistant"`` 的输出；路径全部来自可信图库解析，模型提供的
情绪标签只用来索引，永远不会被当成文件路径。
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from atri_x.reaction.catalog import (
    CATALOG_FILENAME,
    Catalog,
    CatalogItem,
    load_catalog,
)
from atri_x.reaction.policy import ReactionSettings

__all__ = [
    "Decision",
    "ReactionPick",
    "ReactionService",
]

#: 历史状态里最多保留的"最近图片 ID"条数上限（防呆；实际由 dedupe_recent 截断）。
_MAX_RECENT_IDS = 64

#: ``note_text_reply`` 计数的上限：冷却只需要知道"够不够"，不需要无限累加。
_MAX_REPLY_COUNTER = 1000

#: 区分"从未加载过图库"与"图库文件当前不存在"的哨兵。
_UNSET = object()


@dataclass(frozen=True)
class ReactionPick:
    """一次选图的结果。"""

    item_id: str
    emotion: str
    path: str
    name: str
    weight: float = 1.0

    @classmethod
    def from_item(cls, item: CatalogItem) -> "ReactionPick":
        return cls(
            item_id=item.id,
            emotion=item.emotion,
            path=item.path,
            name=item.display_name,
            weight=item.weight,
        )


@dataclass(frozen=True)
class Decision:
    """一次决策：要么有 ``pick``，要么有 ``reason``（便于日志/测试断言）。"""

    pick: ReactionPick | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.pick is not None


@dataclass
class _ScopeState:
    """单个会话的表情状态。"""

    last_image_at: float = 0.0
    replies_without_image: int = _MAX_REPLY_COUNTER // 2
    recent_ids: list[str] = field(default_factory=list)


class ReactionService:
    """表情图库的选图器（进程内单例，由 Agent 持有）。

    :param settings: 规范化后的 ``[reactions]`` 配置。
    :param gallery_dir: 图库目录的**绝对路径**（相对路径由配置层解析）。
    :param clock: 取当前时间（秒）；测试注入假时钟。
    :param rng: 随机源；测试注入固定种子。
    :param catalog_loader: 图库加载函数；测试注入假图库。
    """

    def __init__(
        self,
        settings: ReactionSettings,
        gallery_dir: str,
        *,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
        catalog_loader: Callable[[str], Catalog] | None = None,
    ) -> None:
        self.settings = settings
        self.gallery_dir = os.path.abspath(gallery_dir or "")
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self._catalog_loader = catalog_loader or load_catalog
        self._catalog: Catalog | None = None
        self._catalog_stamp: tuple[int, int] | None | object = _UNSET
        self._scopes: dict[str, _ScopeState] = {}
        self._groups: dict[str, _ScopeState] = {}
        self.warnings: list[str] = []

    # -- 图库 ---------------------------------------------------------------
    @property
    def catalog(self) -> Catalog:
        """当前图库（``catalog.json`` 变了会自动重载，GUI 改完不用重启）。"""
        self._reload_catalog_if_changed()
        return self._catalog or Catalog()

    def _reload_catalog_if_changed(self) -> None:
        path = os.path.join(self.gallery_dir, CATALOG_FILENAME)
        stamp: tuple[int, int] | None
        try:
            info = os.stat(path)
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if self._catalog is not None and stamp == self._catalog_stamp:
            return
        catalog = self._catalog_loader(self.gallery_dir)
        self._catalog = catalog
        self._catalog_stamp = stamp
        if catalog.warnings:
            self.warnings.extend(catalog.warnings)
            # 只保留最近一小段，避免每轮都追加同样的"catalog not found"。
            del self.warnings[:-32]

    # -- 决策 ---------------------------------------------------------------
    def decide(
        self,
        *,
        scope_key: str,
        group_key: str = "",
        is_group: bool = False,
        text: str = "",
        emotions: Sequence[str] = (),
        scene: Iterable[str] = (),
    ) -> Decision:
        """判断这次回复要不要补一张表情，返回选中的图或跳过原因。

        ``emotions`` 是模型提出的情绪意图（按正文出现顺序）；这里取最后一个作为
        主情绪，找不到可用图时再回退到前一个。
        """
        settings = self.settings
        if not settings.enabled:
            return Decision(reason="disabled")
        if is_group and not settings.show_in_group:
            return Decision(reason="group_disabled")
        if not is_group and not settings.show_in_private:
            return Decision(reason="private_disabled")
        scope = (scope_key or "").strip()
        if not scope:
            return Decision(reason="no_scope")
        labels = _normalize_emotions(emotions)
        if not labels:
            return Decision(reason="no_emotion")
        suppression = settings.suppression_reason(text)
        if suppression:
            return Decision(reason=suppression)

        now = self._clock()
        state = self._scopes.setdefault(scope, _ScopeState())
        cooldown = _cooldown_reason(settings, state, now)
        if cooldown:
            return Decision(reason=cooldown)
        if is_group and group_key:
            group_state = self._groups.setdefault(group_key, _ScopeState())
            if (
                settings.group_cooldown_seconds > 0
                and group_state.last_image_at > 0
                and now - group_state.last_image_at < settings.group_cooldown_seconds
            ):
                return Decision(reason="group_cooldown")

        catalog = self.catalog
        if not catalog.items:
            return Decision(reason="empty_catalog")
        candidates, emotion = _pick_candidates(
            catalog, labels, state.recent_ids, frozenset(scene)
        )
        if not candidates:
            return Decision(reason="no_candidate")
        if settings.probability < 1.0 and self._rng.random() > settings.probability:
            return Decision(reason="probability")
        item = _weighted_choice(candidates, self._rng)
        if item is None:
            return Decision(reason="no_candidate")
        reason = "" if emotion else "fallback_emotion"
        return Decision(pick=ReactionPick.from_item(item), reason=reason)

    # -- 提交 ---------------------------------------------------------------
    def commit_image(
        self, pick: ReactionPick, *, scope_key: str, group_key: str = ""
    ) -> None:
        """图片已经发出后提交冷却与去重状态。"""
        now = self._clock()
        scope = (scope_key or "").strip()
        if scope:
            state = self._scopes.setdefault(scope, _ScopeState())
            state.last_image_at = now
            state.replies_without_image = 0
            limit = max(0, self.settings.dedupe_recent)
            state.recent_ids.append(pick.item_id)
            if limit <= 0:
                state.recent_ids.clear()
            elif len(state.recent_ids) > min(limit, _MAX_RECENT_IDS):
                del state.recent_ids[: -min(limit, _MAX_RECENT_IDS)]
        group = (group_key or "").strip()
        if group:
            self._groups.setdefault(group, _ScopeState()).last_image_at = now

    def note_text_reply(self, *, scope_key: str, group_key: str = "") -> None:
        """记一条"不带图"的回复，用于 ``cooldown_text_replies`` 计数。"""
        scope = (scope_key or "").strip()
        if not scope:
            return
        state = self._scopes.setdefault(scope, _ScopeState())
        if state.replies_without_image < _MAX_REPLY_COUNTER:
            state.replies_without_image += 1

    # -- 诊断/测试 ----------------------------------------------------------
    def reset(self) -> None:
        """清空全部冷却与去重状态（测试与 ``/reactions reset`` 用）。"""
        self._scopes.clear()
        self._groups.clear()
        self.warnings.clear()

    def snapshot(self, scope_key: str) -> dict[str, object]:
        """导出某个会话的冷却状态（GUI 预览/诊断用）。"""
        state = self._scopes.get((scope_key or "").strip())
        if state is None:
            return {"last_image_at": 0.0, "replies_without_image": 0, "recent_ids": []}
        return {
            "last_image_at": state.last_image_at,
            "replies_without_image": state.replies_without_image,
            "recent_ids": list(state.recent_ids),
        }


def _cooldown_reason(
    settings: ReactionSettings, state: _ScopeState, now: float
) -> str:
    if (
        settings.cooldown_text_replies > 0
        and state.replies_without_image < settings.cooldown_text_replies
    ):
        return "text_reply_cooldown"
    if (
        settings.session_cooldown_seconds > 0
        and state.last_image_at > 0
        and now - state.last_image_at < settings.session_cooldown_seconds
    ):
        return "session_cooldown"
    return ""


def _normalize_emotions(emotions: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in emotions or ():
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in out}:
            out.append(text)
    return out


def _pick_candidates(
    catalog: Catalog,
    labels: Sequence[str],
    recent_ids: Sequence[str],
    scene: frozenset[str],
) -> tuple[list[CatalogItem], str]:
    """按情绪优先级挑候选；优先排除最近发过的图，全部被排除时退回全量。"""
    recent = {item for item in (recent_ids or ()) if item}
    fallback: tuple[list[CatalogItem], str] = ([], "")
    for label in reversed(list(labels)):
        items = [
            item
            for item in catalog.items_for(label)
            if item.enabled and item.matches_scene(scene)
        ]
        if not items:
            continue
        fresh = [item for item in items if item.id not in recent]
        if fresh:
            return fresh, label
        if not fallback[0]:
            fallback = (items, label)
    return fallback


def _weighted_choice(
    items: Sequence[CatalogItem], rng: random.Random
) -> CatalogItem | None:
    if not items:
        return None
    total = sum(max(0.0, item.weight) for item in items)
    if total <= 0:
        return items[rng.randrange(len(items))]
    threshold = rng.random() * total
    accumulated = 0.0
    for item in items:
        accumulated += max(0.0, item.weight)
        if threshold <= accumulated:
            return item
    return items[-1]
