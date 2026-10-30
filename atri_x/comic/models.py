"""聊天漫画的计划与生成记录模型（纯值对象 + 校验 + 序列化）。

对应上游 Go 仓库没有漫画业务域，这是 Python 侧新增的完整功能；上层对照的是
``PORTING.md`` §2 的拆分约定（单文件 ≤ 600 行、纯标准库、值对象不吞异常）。

设计要点：

* **计划是可校验的结构**，不是一段自然语言。`ComicPlan` 必须能回答：用了哪些
  群聊消息、哪个人物对应第几张参考图、每格台词是什么、哪些台词是原文、哪些是
  改编。任何一项缺失都在 :func:`validate_plan` 里被拒绝，不靠模型"猜字段"。
* **对白要区分来源**：``original``（聊天原文逐字）、``abridged``（原文缩写）、
  ``invented``（创作台词）。只有 ``original`` 允许声称"群友真的这么说过"。
* **参考图编号在计划里固定**：``ComicCharacter.reference`` 决定它在最终提示词里
  是 ``参考图#k`` 的第几张；顺序由计划决定，渲染阶段不得重排。
* 时间统一走 :mod:`atri_x.storage` 的 ``now`` / ``format_time``（RFC3339），
  ID 统一走 ``new_id()``（带连字符的小写 UUIDv4）。

**不带任何网络/磁盘/模型调用**：本模块只做数据结构与校验，方便单测。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from atri_x.comic.errors import (
    ComicCharacterError,
    ComicContextError,
    ComicDataError,
    ComicError,
    ComicPlanError,
    ComicPromptError,
    ComicRenderError,
    ComicStoreError,
    is_maybe_unknown_error,
)
from atri_x.storage import format_time, new_id, now

__all__ = [
    "CHARACTER_SOURCES",
    "DIALOGUE_KINDS",
    "LAYOUTS",
    "LANGUAGES",
    "MAX_COMIC_CHARACTERS",
    "MAX_DIALOGUE_RUNES",
    "MAX_PLAN_PANELS",
    "MAX_SCENE_RUNES",
    "MAX_TITLE_RUNES",
    "MIN_PLAN_PANELS",
    "PLAN_SCHEMA_VERSION",
    "RECORD_SCHEMA_VERSION",
    "REFERENCE_KINDS",
    "ComicCharacter",
    "ComicCharacterError",
    "ComicCharacterReference",
    "ComicContext",
    "ComicContextMessage",
    "ComicDataError",
    "ComicDialogue",
    "ComicError",
    "ComicPanel",
    "ComicPlan",
    "ComicSource",
    "is_maybe_unknown_error",
    "new_plan_id",
    "new_task_id",
    "validate_plan",
]

#: 计划/记录的结构版本。升级时旧文件仍要能读（``from_dict`` 兼容低版本）。
PLAN_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1

#: 一页漫画最多几格。上限不是模型自由度，而是提示词可控性的经验值。
MAX_PLAN_PANELS = 12
MIN_PLAN_PANELS = 1

#: 一页最多几个可区分的参考图人物（与 ``imagegen`` 的最多 4 张参考图对齐）。
MAX_COMIC_CHARACTERS = 4

#: 文本长度上限（rune ≈ Python 字符数）。
MAX_TITLE_RUNES = 60
MAX_SCENE_RUNES = 240
MAX_DIALOGUE_RUNES = 120

#: 允许的语言。首版只承诺中文与英文；其它语言一律拒绝（避免"中文台词被翻译"）。
LANGUAGES: tuple[str, ...] = ("zh", "en")

#: 版面形态：九宫格感的网格页 vs 竖排条漫。
LAYOUTS: tuple[str, ...] = ("grid", "strip")

#: 人物素材来源。``none`` 表示没有参考图，只能靠文字描述（必须明确告知用户）。
CHARACTER_SOURCES: tuple[str, ...] = ("character", "avatar", "media", "none")

#: 参考图描述的种类。
REFERENCE_KINDS: tuple[str, ...] = ("media", "character_image", "avatar", "none")

#: 台词来源种类。
DIALOGUE_KINDS: tuple[str, ...] = ("original", "abridged", "invented")

#: 允许出现在 ID 里的字符（计划 ID / 人物稳定 ID）。冒号用于 ``member:qq:123``。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def new_plan_id() -> str:
    """新计划 ID（与全仓 ID 契约一致：带连字符的小写 UUIDv4）。"""
    return new_id()


def new_task_id() -> str:
    """新生成任务 ID。"""
    return new_id()


def _clean_text(value: Any, limit: int = 0) -> str:
    """压空白、去控制字符；超长**不截断**（截断由调用方显式做并告警）。"""
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _clip(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _require_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text or not _ID_PATTERN.match(text):
        raise ComicDataError(f"{what} 不合法：{value!r}")
    return text


def _require_choice(value: Any, choices: tuple[str, ...], what: str) -> str:
    text = str(value or "").strip()
    if text not in choices:
        raise ComicDataError(f"{what} 不合法：{value!r}（可选：{'、'.join(choices)}）")
    return text


@dataclass
class ComicCharacterReference:
    """一个人物的参考图来源。

    ``kind`` 三选一（外加 ``none``）：

    * ``media`` —— 已入库媒体，``source`` 是 ``media:<sha256>``；
    * ``character_image`` —— 角色卡图片，``source`` 是图片文件名；
    * ``avatar`` —— 平台头像 URL，渲染时才通过媒体中心下载（规划阶段不触网）。
    """

    kind: str = "none"
    source: str = ""

    def validate(self) -> None:
        if self.kind not in REFERENCE_KINDS:
            raise ComicDataError(f"参考图类型不合法：{self.kind!r}")
        if self.kind == "none":
            self.source = ""
            return
        text = str(self.source or "").strip()
        if not text:
            raise ComicDataError(f"参考图类型 {self.kind} 缺少来源")
        self.source = text

    def describe(self) -> str:
        if self.kind == "none":
            return "无参考图（仅文字描述）"
        if self.kind == "media":
            return "媒体图 " + self.source
        if self.kind == "character_image":
            return "角色卡图片 " + self.source
        if self.kind == "avatar":
            return "平台头像"
        return self.kind

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source}

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicCharacterReference":
        if not isinstance(raw, dict):
            return cls()
        return cls(kind=str(raw.get("kind") or "none"), source=str(raw.get("source") or ""))


@dataclass
class ComicCharacter:
    """漫画里的一个人物。

    ``id`` 是**稳定身份**：群友用 ``member:<platform>:<sender_id>``，角色卡用
    ``character:<角色 ID>``。显示名可以重名，但 ``id`` 不会，因此同名不同人不会
    串人物。
    """

    id: str = ""
    display_name: str = ""
    source: str = "none"
    member_platform: str = ""
    member_id: str = ""
    character_id: str = ""
    reference: ComicCharacterReference = field(default_factory=ComicCharacterReference)
    notes: str = ""

    def validate(self) -> None:
        self.id = _require_id(self.id, "人物稳定 ID")
        self.display_name = _clip(self.display_name, 40)
        if not self.display_name:
            raise ComicDataError("人物显示名不能为空")
        self.source = _require_choice(self.source, CHARACTER_SOURCES, "人物素材来源")
        self.reference.validate()
        if self.source == "character" and not (self.character_id or "").strip():
            raise ComicDataError(f"人物 {self.id} 的素材来源是角色卡，但没给 character_id")
        self.notes = _clean_text(self.notes, 80)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source": self.source,
            "member_platform": self.member_platform,
            "member_id": self.member_id,
            "character_id": self.character_id,
            "reference": self.reference.to_dict(),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicCharacter":
        if not isinstance(raw, dict):
            raise ComicDataError("人物条目必须是对象")
        return cls(
            id=str(raw.get("id") or ""),
            display_name=str(raw.get("display_name") or ""),
            source=str(raw.get("source") or "none"),
            member_platform=str(raw.get("member_platform") or ""),
            member_id=str(raw.get("member_id") or ""),
            character_id=str(raw.get("character_id") or ""),
            reference=ComicCharacterReference.from_dict(raw.get("reference")),
            notes=str(raw.get("notes") or ""),
        )


@dataclass
class ComicDialogue:
    """一格里的台词。"""

    speaker_id: str = ""
    text: str = ""
    kind: str = "original"
    source_message_ids: list[str] = field(default_factory=list)

    def validate(self, character_ids: set[str], allowed_message_ids: set[str]) -> None:
        self.speaker_id = _require_id(self.speaker_id, "台词说话人 ID")
        if self.speaker_id not in character_ids:
            raise ComicDataError(f"台词说话人 {self.speaker_id} 不在人物表里")
        self.text = _clean_text(self.text)
        if not self.text:
            raise ComicDataError("台词文本不能为空")
        if len(self.text) > MAX_DIALOGUE_RUNES:
            raise ComicDataError(
                f"台词过长（{len(self.text)} 字符，上限 {MAX_DIALOGUE_RUNES}）：{self.text[:20]}…"
            )
        self.kind = _require_choice(self.kind, DIALOGUE_KINDS, "台词来源")
        self.source_message_ids = [str(item) for item in self.source_message_ids or []]
        if self.kind == "original" and not self.source_message_ids:
            raise ComicDataError("标记为原文的台词必须给出 source_message_ids（不能凭空声称）")
        for message_id in self.source_message_ids:
            if message_id not in allowed_message_ids:
                raise ComicDataError(f"台词引用了不存在的来源消息：{message_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "text": self.text,
            "kind": self.kind,
            "source_message_ids": list(self.source_message_ids),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicDialogue":
        if not isinstance(raw, dict):
            raise ComicDataError("台词条目必须是对象")
        return cls(
            speaker_id=str(raw.get("speaker_id") or ""),
            text=str(raw.get("text") or ""),
            kind=str(raw.get("kind") or "original"),
            source_message_ids=[str(item) for item in raw.get("source_message_ids") or []],
        )


@dataclass
class ComicPanel:
    """一格分镜。"""

    index: int = 0
    scene: str = ""
    character_ids: list[str] = field(default_factory=list)
    dialogue: list[ComicDialogue] = field(default_factory=list)
    caption: str = ""
    source_message_ids: list[str] = field(default_factory=list)

    def validate(self, character_ids: set[str], allowed_message_ids: set[str]) -> None:
        if not isinstance(self.index, int) or self.index <= 0:
            raise ComicDataError("分镜格序必须是从 1 开始的整数")
        self.scene = _clean_text(self.scene)
        if not self.scene:
            raise ComicDataError(f"第 {self.index} 格缺少画面描述")
        if len(self.scene) > MAX_SCENE_RUNES:
            raise ComicDataError(
                f"第 {self.index} 格画面描述过长（{len(self.scene)} 字符，上限 {MAX_SCENE_RUNES}）"
            )
        self.character_ids = [str(item) for item in self.character_ids or []]
        unique_ids: list[str] = []
        seen: set[str] = set()
        for character_id in self.character_ids:
            if character_id in seen:
                continue
            seen.add(character_id)
            if character_id not in character_ids:
                raise ComicDataError(f"第 {self.index} 格出场人物 {character_id} 不在人物表里")
            unique_ids.append(character_id)
        self.character_ids = unique_ids
        self.source_message_ids = [str(item) for item in self.source_message_ids or []]
        for message_id in self.source_message_ids:
            if message_id not in allowed_message_ids:
                raise ComicDataError(f"第 {self.index} 格引用了不存在的来源消息：{message_id}")
        for item in self.dialogue:
            item.validate(character_ids, allowed_message_ids)
            if item.speaker_id not in self.character_ids:
                self.character_ids.append(item.speaker_id)
        self.caption = _clip(self.caption, 200)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "scene": self.scene,
            "character_ids": list(self.character_ids),
            "dialogue": [item.to_dict() for item in self.dialogue],
            "caption": self.caption,
            "source_message_ids": list(self.source_message_ids),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicPanel":
        if not isinstance(raw, dict):
            raise ComicDataError("分镜条目必须是对象")
        return cls(
            index=int(raw.get("index") or 0),
            scene=str(raw.get("scene") or ""),
            character_ids=[str(item) for item in raw.get("character_ids") or []],
            dialogue=[
                ComicDialogue.from_dict(item) for item in raw.get("dialogue") or []
            ],
            caption=str(raw.get("caption") or ""),
            source_message_ids=[
                str(item) for item in raw.get("source_message_ids") or []
            ],
        )


@dataclass
class ComicSource:
    """计划的取材范围：哪个平台、哪个会话、用了哪些消息。"""

    platform: str = ""
    scope_id: str = ""
    scope_type: str = ""
    message_ids: list[str] = field(default_factory=list)
    anchor_message_id: str = ""
    message_count: int = 0
    truncated: bool = False

    def validate(self) -> None:
        self.platform = str(self.platform or "").strip()
        self.scope_id = str(self.scope_id or "").strip()
        if not self.platform or not self.scope_id:
            raise ComicDataError("计划必须绑定平台与会话（不能跨聊天取材）")
        self.message_ids = [str(item) for item in self.message_ids or []]
        if not self.message_ids:
            raise ComicDataError("计划必须至少引用一条真实聊天消息")
        if len(set(self.message_ids)) != len(self.message_ids):
            deduped: list[str] = []
            seen: set[str] = set()
            for item in self.message_ids:
                if item not in seen:
                    seen.add(item)
                    deduped.append(item)
            self.message_ids = deduped
        if self.message_count <= 0:
            self.message_count = len(self.message_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "scope_id": self.scope_id,
            "scope_type": self.scope_type,
            "message_ids": list(self.message_ids),
            "anchor_message_id": self.anchor_message_id,
            "message_count": self.message_count,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicSource":
        if not isinstance(raw, dict):
            raise ComicDataError("来源范围必须是对象")
        return cls(
            platform=str(raw.get("platform") or ""),
            scope_id=str(raw.get("scope_id") or ""),
            scope_type=str(raw.get("scope_type") or ""),
            message_ids=[str(item) for item in raw.get("message_ids") or []],
            anchor_message_id=str(raw.get("anchor_message_id") or ""),
            message_count=int(raw.get("message_count") or 0),
            truncated=bool(raw.get("truncated")),
        )


@dataclass
class ComicContextMessage:
    """取到的聊天素材（渲染/规划用；不直接落盘，落盘的是它的 stable id）。"""

    message_id: str = ""
    seq: int = 0
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    created_at: str = ""
    has_media: bool = False


@dataclass
class ComicContext:
    """一次漫画取材的结果。"""

    platform: str = ""
    scope_id: str = ""
    scope_type: str = ""
    messages: list[ComicContextMessage] = field(default_factory=list)
    anchor_message_id: str = ""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def message_ids(self) -> list[str]:
        return [item.message_id for item in self.messages]

    def by_id(self) -> dict[str, ComicContextMessage]:
        return {item.message_id: item for item in self.messages}


@dataclass
class ComicPlan:
    """一页漫画的结构化计划。"""

    plan_id: str = ""
    revision: int = 1
    schema_version: int = PLAN_SCHEMA_VERSION
    title: str = ""
    language: str = "zh"
    layout: str = "grid"
    panel_count: int = 0
    characters: list[ComicCharacter] = field(default_factory=list)
    panels: list[ComicPanel] = field(default_factory=list)
    source: ComicSource = field(default_factory=ComicSource)
    adaptation_notes: str = ""
    planner: str = "rules"
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""

    def validate(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ComicDataError(f"计划结构版本不支持：{self.schema_version}")
        self.plan_id = _require_id(self.plan_id, "计划 ID")
        if not isinstance(self.revision, int) or self.revision <= 0:
            raise ComicDataError("计划版本号必须是从 1 开始的整数")
        self.title = _clip(self.title, MAX_TITLE_RUNES)
        if not self.title:
            raise ComicDataError("漫画标题不能为空")
        self.language = _require_choice(self.language, LANGUAGES, "漫画语言")
        self.layout = _require_choice(self.layout, LAYOUTS, "漫画版面")
        if not isinstance(self.panel_count, int):
            raise ComicDataError("panel_count 必须是整数")
        if not MIN_PLAN_PANELS <= self.panel_count <= MAX_PLAN_PANELS:
            raise ComicDataError(
                f"格数必须在 {MIN_PLAN_PANELS}..{MAX_PLAN_PANELS} 之间，收到 {self.panel_count}"
            )
        if len(self.panels) != self.panel_count:
            raise ComicDataError(
                f"计划声明 {self.panel_count} 格，实际有 {len(self.panels)} 格"
            )
        ids: set[str] = set()
        for item in self.characters:
            item.validate()
            if item.id in ids:
                raise ComicDataError(f"人物 ID 重复：{item.id}")
            ids.add(item.id)
        if not ids:
            raise ComicDataError("漫画至少需要一个可识别的人物")
        if len(ids) > MAX_COMIC_CHARACTERS:
            raise ComicDataError(
                f"一页最多 {MAX_COMIC_CHARACTERS} 个人物（收到 {len(ids)} 个）"
            )
        self.source.validate()
        allowed = set(self.source.message_ids)
        indexes: set[int] = set()
        for item in self.panels:
            item.validate(ids, allowed)
            if item.index in indexes:
                raise ComicDataError(f"分镜格序重复：{item.index}")
            indexes.add(item.index)
        if indexes != set(range(1, self.panel_count + 1)):
            raise ComicDataError("分镜格序必须是从 1 到 panel_count 连续编号")
        if not self.created_at:
            self.created_at = format_time(now())
        self.adaptation_notes = _clean_text(self.adaptation_notes, 400)

    def character_by_id(self) -> dict[str, ComicCharacter]:
        return {item.id: item for item in self.characters}

    def reference_order(self) -> list[ComicCharacter]:
        """有参考图的人物，按提示词里的 ``参考图#k`` 编号顺序。"""
        return [item for item in self.characters if item.reference.kind != "none"]

    def fingerprint(self) -> str:
        """计划内容指纹（生成记录绑定它，防止改台词后用旧版本生成）。"""
        payload = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "title": self.title,
            "language": self.language,
            "layout": self.layout,
            "panel_count": self.panel_count,
            "characters": [item.to_dict() for item in self.characters],
            "panels": [item.to_dict() for item in self.panels],
            "source": self.source.to_dict(),
            "adaptation_notes": self.adaptation_notes,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "title": self.title,
            "language": self.language,
            "layout": self.layout,
            "panel_count": self.panel_count,
            "characters": [item.to_dict() for item in self.characters],
            "panels": [item.to_dict() for item in self.panels],
            "source": self.source.to_dict(),
            "adaptation_notes": self.adaptation_notes,
            "planner": self.planner,
            "warnings": list(self.warnings),
            "created_at": self.created_at,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicPlan":
        if not isinstance(raw, dict):
            raise ComicDataError("计划的顶层必须是对象")
        return cls(
            schema_version=int(raw.get("schema_version") or 0),
            plan_id=str(raw.get("plan_id") or ""),
            revision=int(raw.get("revision") or 0),
            title=str(raw.get("title") or ""),
            language=str(raw.get("language") or "zh"),
            layout=str(raw.get("layout") or "grid"),
            panel_count=int(raw.get("panel_count") or 0),
            characters=[
                ComicCharacter.from_dict(item) for item in raw.get("characters") or []
            ],
            panels=[ComicPanel.from_dict(item) for item in raw.get("panels") or []],
            source=ComicSource.from_dict(raw.get("source")),
            adaptation_notes=str(raw.get("adaptation_notes") or ""),
            planner=str(raw.get("planner") or "rules"),
            warnings=[str(item) for item in raw.get("warnings") or []],
            created_at=str(raw.get("created_at") or ""),
        )


def validate_plan(plan: ComicPlan) -> None:
    """独立入口，方便调用方先构造再校验。"""
    plan.validate()

