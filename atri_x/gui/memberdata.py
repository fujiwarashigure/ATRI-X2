"""群友角色数据：成员档案的 GUI 适配层 + 群友发现。

两件事拆开看：

1. **档案**：``<配置目录>/gui/members.json``。模型、JSON 读写、热更新缓存与
   ``enabled`` 语义都在 :mod:`atri_x.memberprofile`（GUI 与后台 Agent 共用），本模块
   只保留旧接口名并补上"删档案时顺手清头像缓存"这类界面细节。
2. **群友从哪来**：只读打开后台服务写的聊天历史库（``chat_messages`` 表），按
   ``platform + platform_scope_id + sender_id`` 聚合，得到「谁在这个群说过话、说了
   多少条、最后一次什么时候」。库不存在/表不存在/被占用时抛 :class:`MemberDataError`，
   由界面提示而不是崩。

头像缓存（下载/转换/落盘）仍在 :mod:`atri_x.gui.avatars`：它只服务界面展示，
不会发给模型，也不会自动成为生图参考图。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from atri_x.gui import avatars
from atri_x.memberprofile import (
    MAX_PROFILE_RUNES,
    MAX_TAGS,
    MEMBERS_FILE,
    MemberDataError,
    MemberProfile,
    load_profiles,
    member_key,
    members_path,
    normalize_profile,
    remove_profile,
    save_profiles,
    upsert_profile,
)

__all__ = [
    "MEMBERS_FILE",
    "MAX_PROFILE_RUNES",
    "MAX_TAGS",
    "GroupScope",
    "MemberDataError",
    "MemberProfile",
    "MemberSeen",
    "delete_profile",
    "discover_members",
    "group_label",
    "list_scopes",
    "load_profiles",
    "member_key",
    "members_path",
    "normalize_profile",
    "save_profiles",
    "set_avatar",
    "upsert_profile",
]

#: 单个群最多列多少成员。
DISCOVER_LIMIT = 2000

#: 最多列多少个群/会话作用域。
SCOPE_LIMIT = 200


@dataclass(frozen=True)
class MemberSeen:
    """从聊天历史里发现的群友（只读快照）。"""

    platform: str
    scope_id: str
    user_id: str
    display_name: str = ""
    message_count: int = 0
    last_at: str = ""

    @property
    def key(self) -> str:
        return member_key(self.platform, self.scope_id, self.user_id)


@dataclass(frozen=True)
class GroupScope:
    """聊天历史里的一个群/会话作用域。"""

    platform: str
    scope_id: str
    scope_type: str = ""
    messages: int = 0
    members: int = 0
    last_at: str = ""

    @property
    def label(self) -> str:
        return group_label(self.platform, self.scope_id, self.scope_type)


def group_label(platform: str, scope_id: str, scope_type: str = "") -> str:
    """群/会话作用域的显示名。"""
    platform = (platform or "").strip()
    scope = (scope_id or "").strip()
    kind = (scope_type or "").strip()
    suffix = {"group": "群", "private": "私聊", "channel": "频道"}.get(kind, kind)
    body = f"{platform} · {scope}"
    return f"{body}（{suffix}）" if suffix else body


# ---------------------------------------------------------------------------
# 档案读写（委托共享存储，保留旧 GUI 接口）
# ---------------------------------------------------------------------------
def delete_profile(config_dir: str, key: str) -> bool:
    """删一个成员档案（连同头像缓存）；返回是否真的删了。"""
    profile = remove_profile(config_dir, key)
    if profile is None:
        return False
    if profile.avatar:
        avatars.clear_avatar(config_dir, profile.avatar)
    return True


def set_avatar(config_dir: str, profile: MemberProfile, png: bytes) -> MemberProfile:
    """把 PNG 字节存成这个成员的头像（覆盖旧的），返回更新后的档案对象。"""
    old = (profile.avatar or "").strip()
    profile.avatar = avatars.cache_avatar(config_dir, profile.key, png)
    if old and old != profile.avatar:
        avatars.clear_avatar(config_dir, old)
    return profile


# ---------------------------------------------------------------------------
# 从聊天历史发现群友
# ---------------------------------------------------------------------------
def list_scopes(paths: object) -> list[GroupScope]:
    """列出聊天历史里出现过的群/会话作用域（按消息数降序）。"""
    sql = """
SELECT platform, platform_scope_id, COALESCE(scope_type, ''), COUNT(*) AS messages,
       COUNT(DISTINCT sender_id) AS members, COALESCE(MAX(created_at), '')
FROM chat_messages
GROUP BY platform, platform_scope_id, scope_type
ORDER BY messages DESC
LIMIT ?
"""
    rows = _query(paths, sql, (SCOPE_LIMIT,))
    return [
        GroupScope(
            platform=str(row[0]),
            scope_id=str(row[1]),
            scope_type=str(row[2] or ""),
            messages=int(row[3] or 0),
            members=int(row[4] or 0),
            last_at=str(row[5] or ""),
        )
        for row in rows
    ]


def discover_members(
    paths: object, platform: str, scope_id: str, *, limit: int = DISCOVER_LIMIT
) -> list[MemberSeen]:
    """列出一个群/会话里发过言的群友（按发言条数降序）。"""
    sql = """
SELECT m.sender_id,
       COALESCE((
           SELECT x.sender_name FROM chat_messages x
           WHERE x.platform = m.platform AND x.platform_scope_id = m.platform_scope_id
             AND x.sender_id = m.sender_id
             AND x.sender_name IS NOT NULL AND x.sender_name <> ''
           ORDER BY x.seq DESC LIMIT 1
       ), '') AS display_name,
       COUNT(*) AS messages,
       COALESCE(MAX(m.created_at), '') AS last_at
FROM chat_messages m
WHERE m.platform = ? AND m.platform_scope_id = ? AND m.sender_id <> ''
GROUP BY m.sender_id
ORDER BY messages DESC, m.sender_id
LIMIT ?
"""
    rows = _query(paths, sql, ((platform or "").strip(), (scope_id or "").strip(), int(limit)))
    return [
        MemberSeen(
            platform=str(platform or "").strip(),
            scope_id=str(scope_id or "").strip(),
            user_id=str(row[0]),
            display_name=str(row[1] or ""),
            message_count=int(row[2] or 0),
            last_at=str(row[3] or ""),
        )
        for row in rows
    ]


def _query(paths: object, sql: str, params: tuple) -> list[tuple]:
    path = str(getattr(paths, "chat_history_db_path", "") or "")
    if not path or not os.path.exists(path):
        raise MemberDataError(
            f"找不到聊天历史库：{path or '（app.toml 的 [storage] 未配置）'}\n"
            "先用桌面端启动一次后台服务，等机器人收过消息后再回来刷新。"
        )
    try:
        connection = _connect(path)
    except sqlite3.Error as exc:
        raise MemberDataError(f"打不开聊天历史库 {path}：{exc}") from exc
    try:
        return list(connection.execute(sql, params).fetchall())
    except sqlite3.Error as exc:
        raise MemberDataError(
            f"读取聊天历史失败：{exc}\n"
            "如果后台服务正在写库，稍后点「刷新群友」重试；库还没建表说明服务没跑过。"
        ) from exc
    finally:
        connection.close()


def _connect(path: str) -> sqlite3.Connection:
    """优先只读打开（不动服务的库），失败再退回普通连接（只跑 SELECT）。"""
    try:
        uri = Path(path).absolute().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return sqlite3.connect(path, timeout=5.0)
