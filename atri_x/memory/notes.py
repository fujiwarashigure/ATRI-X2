"""常驻记忆条目的会话可见性。**Python 版新增**（Go 侧没有对应文件）。

为什么需要：Go 的常驻记忆按 ``(platform, actor_id)`` 存整段 ``core`` / ``normal``，
读取时不带会话信息 —— 同一个人在私聊里说过的事，进群后仍会被注入。本模块把
「这条记忆从哪来、能出现在哪」变成显式的 :class:`MemoryNote`，并给出**唯一**的
可见性判定入口 :func:`note_visible_in`。所有注入点（系统提示词、记忆读取工具）
都必须走这里，不允许各自实现一套判断。

三档可见性（对应落地评估文档 §A）：

======  ============================================================
取值     含义
======  ============================================================
none    谁都不注入（保留给"用户要求别记"的场景）
identity  认得这个人：同一平台任意会话都能看到（默认档）
shared    跨会话共享：需要 ``share_status == "approved"`` 才算数
======  ============================================================

两条硬规则：

1. **私聊来源永不进群**：来源会话是 ``private:`` / ``c2c:`` 的条目只能出现在
   同一个私聊会话里 —— 与 visibility 写的是什么无关。
2. **合并取更严**：多条来源合成人物视图时用 :func:`stricter`，两群之间共享要
   两边都批（调用方负责只放行 approved 的来源），不会因为一条 memory 就放宽。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "PEOPLE_VIEW_ENABLED",
    "PRIVATE_SCOPE_PREFIXES",
    "PROMPT_NOTE_LIMIT",
    "PROMPT_NOTE_MAX_UNITS",
    "SHARE_STATUS_APPROVED",
    "VISIBILITY_IDENTITY",
    "VISIBILITY_NONE",
    "VISIBILITY_ORDER",
    "VISIBILITY_SHARED",
    "MemoryNote",
    "NoteSelection",
    "conversation_key",
    "filter_visible_notes",
    "has_private_origin",
    "is_private_scope_id",
    "note_visible_in",
    "normalize_visibility",
    "stricter",
]

#: 可见性档位（由严到宽）。
VISIBILITY_NONE = "none"
VISIBILITY_IDENTITY = "identity"
VISIBILITY_SHARED = "shared"
VISIBILITY_ORDER = (VISIBILITY_NONE, VISIBILITY_IDENTITY, VISIBILITY_SHARED)

#: 私聊会话的 scope id 前缀（QQ 是 ``private:``，其它平台可能是 ``c2c:``）。
PRIVATE_SCOPE_PREFIXES = ("private:", "c2c:")

#: 跨会话共享被批准后的分享状态。
SHARE_STATUS_APPROVED = "approved"

#: 单轮注入的条目数上限。
PROMPT_NOTE_LIMIT = 12
#: 单轮注入的条目总字数/单词数上限。
PROMPT_NOTE_MAX_UNITS = 500
#: 人物视图（把"本轮出现的其他人"的可见记忆也注入）默认关闭：先只注入本人
#: 条目把过滤跑通，验证没有串台再打开。
PEOPLE_VIEW_ENABLED = False

#: 人物的默认可见档。老数据（没有来源、没有 visibility 字段）按这一档处理。
DEFAULT_VISIBILITY = VISIBILITY_IDENTITY


@dataclass
class MemoryNote:
    """一条带来源与可见性的常驻记忆条目。

    ``memories.toml`` 里以 ``[[memory_notes]]`` 数组表保存；同一条目按
    ``(platform, actor_id, scope_id, text)`` 定位。
    """

    platform: str = ""
    actor_id: str = ""
    scope_id: str = ""
    text: str = ""
    visibility: str = DEFAULT_VISIBILITY
    share_status: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        """转成可写进 TOML 的普通字典（键序固定）。"""
        return {
            "platform": self.platform,
            "actor_id": self.actor_id,
            "scope_id": self.scope_id,
            "text": self.text,
            "visibility": self.visibility,
            "share_status": self.share_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def key(self) -> tuple[str, str, str, str]:
        """去重键：同一个人、同一会话、同一段内容的条目只留一条。"""
        return (
            (self.platform or "").strip(),
            (self.actor_id or "").strip(),
            (self.scope_id or "").strip(),
            (self.text or "").strip().lower(),
        )

    def visible_in(self, platform: str, scope_id: str) -> bool:
        """这条条目能否出现在 ``(platform, scope_id)`` 的会话里。"""
        return note_visible_in(self, platform, scope_id)


@dataclass
class NoteSelection:
    """可见条目筛选结果（带"被截断"标记，方便调用方提示模型）。"""

    notes: list[MemoryNote] = field(default_factory=list)
    dropped: int = 0


def normalize_visibility(value: str) -> str:
    """把配置/落盘里的字符串归一成三档之一；无法识别时按 ``identity``。"""
    normalized = (value or "").strip().lower()
    if normalized in VISIBILITY_ORDER:
        return normalized
    return DEFAULT_VISIBILITY


def stricter(left: str, right: str) -> str:
    """取更严的可见性档（``none`` < ``identity`` < ``shared``）。"""
    left = normalize_visibility(left)
    right = normalize_visibility(right)
    return left if VISIBILITY_ORDER.index(left) <= VISIBILITY_ORDER.index(right) else right


def is_private_scope_id(scope_id: str) -> bool:
    """该会话 id 是否是私聊。"""
    return (scope_id or "").strip().startswith(PRIVATE_SCOPE_PREFIXES)


def has_private_origin(note: MemoryNote) -> bool:
    """这条条目是否来自私聊（私聊来源永不进群）。"""
    return is_private_scope_id(note.scope_id)


def conversation_key(platform: str, scope_id: str) -> str:
    """会话的唯一键（平台 + 作用域 id），用于"只覆盖本会话条目"。"""
    return (platform or "").strip() + "\x00" + (scope_id or "").strip()


def note_visible_in(note: MemoryNote, platform: str, scope_id: str) -> bool:
    """判断一条记忆能否注入到 ``(platform, scope_id)`` 这个会话。

    规则顺序刻意如此（先硬规则、再看档位）：

    1. 平台不同 → 不可见。
    2. ``visibility == none`` → 不可见。
    3. 来源是私聊 → **只**在同一个私聊会话可见（永不进群）。
    4. ``shared`` → 必须 ``share_status == approved``。
    5. 其余（``identity``）→ 同平台任意会话可见。
    """
    if (note.platform or "").strip() != (platform or "").strip():
        return False
    visibility = normalize_visibility(note.visibility)
    if visibility == VISIBILITY_NONE:
        return False
    origin = (note.scope_id or "").strip()
    current = (scope_id or "").strip()
    if is_private_scope_id(origin):
        return origin == current
    if visibility == VISIBILITY_SHARED and (note.share_status or "").strip() != SHARE_STATUS_APPROVED:
        return False
    return True


def filter_visible_notes(
    notes: Iterable[MemoryNote],
    platform: str,
    scope_id: str,
    limit: int = 0,
) -> NoteSelection:
    """筛出可见条目并按 ``limit`` 截断（``limit <= 0`` 表示不限）。"""
    visible: list[MemoryNote] = []
    seen: set[tuple[str, str, str]] = set()
    for note in notes or ():
        text = (note.text or "").strip()
        if not text:
            continue
        if not note_visible_in(note, platform, scope_id):
            continue
        # 去重按「同一个人 + 同一段内容」：同一条事实从两个群写进来时只注入一次。
        key = (
            (note.platform or "").strip(),
            (note.actor_id or "").strip(),
            text.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        cloned = MemoryNote(
            platform=note.platform,
            actor_id=note.actor_id,
            scope_id=note.scope_id,
            text=text,
            visibility=normalize_visibility(note.visibility),
            share_status=note.share_status,
            created_at=note.created_at,
            updated_at=note.updated_at,
        )
        visible.append(cloned)
    visible.sort(key=lambda item: (item.created_at, item.text))
    if limit > 0 and len(visible) > limit:
        return NoteSelection(notes=visible[:limit], dropped=len(visible) - limit)
    return NoteSelection(notes=visible, dropped=0)
