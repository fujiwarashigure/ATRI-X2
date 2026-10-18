"""群友角色数据：成员档案（备注名/标签/角色数据/头像）的本地存储 + 群友发现。

两件事拆开看：

1. **档案**：``<配置目录>/gui/members.json``。GUI 私有，不写 ``app.toml`` /
   ``services.toml``（那里没有这些键，严格解码会报未知键）。档案字段都是"给人看的"：
   备注名、标签、角色数据正文（性格/关系/称呼习惯/雷点这类自由文本）、头像文件名。
2. **群友从哪来**：只读打开后台服务写的聊天历史库（``chat_messages`` 表），按
   ``platform + platform_scope_id + sender_id`` 聚合，得到「谁在这个群说过话、说了
   多少条、最后一次什么时候」。库不存在/表不存在/被占用时抛 :class:`MemberDataError`，
   由界面提示而不是崩。

写入与读取都用 UTF-8 无 BOM + LF，落盘先写 ``.tmp`` 再 ``os.replace``；JSON 损坏时
读回空表（界面照常能打开，重新保存即可覆盖）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from atri_x.gui import avatars

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
    "save_profiles",
    "set_avatar",
    "upsert_profile",
]

#: 档案文件名（相对 ``<配置目录>/gui``）。
MEMBERS_FILE = "members.json"

#: 角色数据正文字符上限。
MAX_PROFILE_RUNES = 4000

#: 标签个数上限。
MAX_TAGS = 12

#: 单个群最多列多少成员。
DISCOVER_LIMIT = 2000

#: 最多列多少个群/会话作用域。
SCOPE_LIMIT = 200

#: JSON 结构版本。
MEMBERS_VERSION = 1


class MemberDataError(Exception):
    """群友数据的可预期失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class MemberProfile:
    """一个群友的档案（键 = platform + scope_id + user_id）。"""

    platform: str = ""
    scope_id: str = ""
    user_id: str = ""
    display_name: str = ""
    alias: str = ""
    tags: list[str] = field(default_factory=list)
    profile: str = ""
    avatar: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return member_key(self.platform, self.scope_id, self.user_id)

    @property
    def title(self) -> str:
        """界面上第一优先显示的名字：备注名 → 昵称 → id。"""
        return self.alias or self.display_name or self.user_id


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


def member_key(platform: str, scope_id: str, user_id: str) -> str:
    """成员唯一键：``平台/作用域/用户``。"""
    parts = ((platform or "").strip(), (scope_id or "").strip(), (user_id or "").strip())
    return "/".join(parts)


def group_label(platform: str, scope_id: str, scope_type: str = "") -> str:
    """群/会话作用域的显示名。"""
    platform = (platform or "").strip()
    scope = (scope_id or "").strip()
    kind = (scope_type or "").strip()
    suffix = {"group": "群", "private": "私聊", "channel": "频道"}.get(kind, kind)
    body = f"{platform} · {scope}"
    return f"{body}（{suffix}）" if suffix else body


# ---------------------------------------------------------------------------
# 档案读写
# ---------------------------------------------------------------------------
def members_path(config_dir: str) -> str:
    """档案文件路径（``<配置目录>/gui/members.json``）。"""
    return os.path.join(config_dir or "", "gui", MEMBERS_FILE)


def load_profiles(config_dir: str) -> dict[str, MemberProfile]:
    """读全部档案；文件不存在/损坏时返回空表。"""
    path = members_path(config_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw_members = data.get("members")
    if not isinstance(raw_members, dict):
        return {}
    profiles: dict[str, MemberProfile] = {}
    for key, raw in raw_members.items():
        if not isinstance(raw, dict):
            continue
        profile = _profile_from(raw)
        if profile.platform and profile.user_id:
            profiles[str(key)] = profile
    return profiles


def _profile_from(raw: dict) -> MemberProfile:
    tags = raw.get("tags")
    return MemberProfile(
        platform=str(raw.get("platform") or "").strip(),
        scope_id=str(raw.get("scope_id") or "").strip(),
        user_id=str(raw.get("user_id") or "").strip(),
        display_name=str(raw.get("display_name") or "").strip(),
        alias=str(raw.get("alias") or "").strip(),
        tags=[str(item).strip() for item in tags if str(item).strip()] if isinstance(tags, list) else [],
        profile=str(raw.get("profile") or ""),
        avatar=str(raw.get("avatar") or "").strip(),
        updated_at=str(raw.get("updated_at") or "").strip(),
    )


def save_profiles(config_dir: str, profiles: dict[str, MemberProfile]) -> str:
    """整表写回；返回文件路径。空表也会写（保留一个合法的空文件）。"""
    path = members_path(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": MEMBERS_VERSION,
        "members": {key: _profile_to(profile) for key, profile in sorted(profiles.items())},
    }
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise MemberDataError(f"写入群友档案失败：{exc}") from exc
    return path


def _profile_to(profile: MemberProfile) -> dict:
    return {
        "platform": profile.platform,
        "scope_id": profile.scope_id,
        "user_id": profile.user_id,
        "display_name": profile.display_name,
        "alias": profile.alias,
        "tags": list(profile.tags),
        "profile": profile.profile,
        "avatar": profile.avatar,
        "updated_at": profile.updated_at,
    }


def normalize_profile(profile: MemberProfile) -> MemberProfile:
    """裁剪/去空白；缺 platform/user_id 时抛错（没有键就存不了）。"""
    platform = (profile.platform or "").strip()
    user_id = (profile.user_id or "").strip()
    if not platform or not user_id:
        raise MemberDataError("成员缺少平台或用户 id，无法保存")
    tags: list[str] = []
    seen: set[str] = set()
    for raw in profile.tags:
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return MemberProfile(
        platform=platform,
        scope_id=(profile.scope_id or "").strip(),
        user_id=user_id,
        display_name=(profile.display_name or "").strip(),
        alias=(profile.alias or "").strip(),
        tags=tags,
        profile=(profile.profile or "").strip()[:MAX_PROFILE_RUNES],
        avatar=(profile.avatar or "").strip(),
        updated_at=(profile.updated_at or "").strip(),
    )


def upsert_profile(config_dir: str, profile: MemberProfile) -> MemberProfile:
    """保存一个成员档案（新增或覆盖），返回归一化后并盖了时间戳的对象。"""
    normalized = normalize_profile(profile)
    normalized.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    profiles = load_profiles(config_dir)
    profiles[normalized.key] = normalized
    save_profiles(config_dir, profiles)
    return normalized


def delete_profile(config_dir: str, key: str) -> bool:
    """删一个成员档案（连同头像缓存）；返回是否真的删了。"""
    profiles = load_profiles(config_dir)
    profile = profiles.pop((key or "").strip(), None)
    if profile is None:
        return False
    if profile.avatar:
        avatars.clear_avatar(config_dir, profile.avatar)
    save_profiles(config_dir, profiles)
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
