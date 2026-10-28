"""群友档案的数据模型与序列化（GUI 与 Agent 的唯一出处）。

一份档案的键是 ``平台 + 完整作用域 + 用户 id``：同一个 QQ 在不同群可以有不同设定，
私聊与群聊也不共享。档案字段都是"给人看的"：备注名、昵称、标签、角色资料正文、
头像文件名，以及 ``enabled``（是否参与对话注入）。

``enabled`` 是后加的字段，缺省为 ``True``：升级前保存的档案没有这个键，读回时按
"参与对话"处理，保证旧档案接进提示词后立刻生效，而不是升级后静默失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MAX_PROFILE_RUNES",
    "MAX_TAGS",
    "MEMBERS_FILE",
    "MEMBERS_VERSION",
    "MemberDataError",
    "MemberProfile",
    "member_key",
    "normalize_profile",
    "profile_from_dict",
    "profile_has_content",
    "profile_to_dict",
]

#: 档案文件名（相对 ``<配置目录>/gui``）。
MEMBERS_FILE = "members.json"

#: JSON 结构版本。``enabled`` 是向后兼容的追加字段，因此版本号不变。
MEMBERS_VERSION = 1

#: 角色资料正文字符上限。
MAX_PROFILE_RUNES = 4000

#: 标签个数上限。
MAX_TAGS = 12


class MemberDataError(Exception):
    """群友数据的可预期失败（消息可直接展示给用户）。"""


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
    #: 是否把这份档案注入当前发言人的系统提示词。缺省参与。
    enabled: bool = True

    @property
    def key(self) -> str:
        return member_key(self.platform, self.scope_id, self.user_id)

    @property
    def title(self) -> str:
        """界面上第一优先显示的名字：备注名 → 昵称 → id。"""
        return self.alias or self.display_name or self.user_id

    @property
    def speaking_name(self) -> str:
        """提示词里用的称呼：备注名 → 昵称 → 用户 id。"""
        return self.title


def member_key(platform: str, scope_id: str, user_id: str) -> str:
    """成员唯一键：``平台/作用域/用户``。"""
    parts = ((platform or "").strip(), (scope_id or "").strip(), (user_id or "").strip())
    return "/".join(parts)


def profile_from_dict(raw: object) -> MemberProfile:
    """把 JSON 里的一项转成 :class:`MemberProfile`（宽松解析，坏字段按空处理）。"""
    if not isinstance(raw, dict):
        return MemberProfile()
    tags = raw.get("tags")
    enabled = raw.get("enabled", None)
    return MemberProfile(
        platform=str(raw.get("platform") or "").strip(),
        scope_id=str(raw.get("scope_id") or "").strip(),
        user_id=str(raw.get("user_id") or "").strip(),
        display_name=str(raw.get("display_name") or "").strip(),
        alias=str(raw.get("alias") or "").strip(),
        tags=[str(item).strip() for item in tags if str(item).strip()]
        if isinstance(tags, list)
        else [],
        profile=str(raw.get("profile") or ""),
        avatar=str(raw.get("avatar") or "").strip(),
        updated_at=str(raw.get("updated_at") or "").strip(),
        # 老档案没有该键：缺省按"参与对话"处理，保证已有设定升级后直接生效。
        enabled=True if enabled is None else bool(enabled),
    )


def profile_to_dict(profile: MemberProfile) -> dict:
    """转成可写进 ``members.json`` 的字典（字段顺序稳定，便于 diff）。"""
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
        "enabled": bool(profile.enabled),
    }


def profile_has_content(profile: MemberProfile) -> bool:
    """这份档案是否有任何可以注入提示词的内容。"""
    if not profile:
        return False
    return bool(
        (profile.alias or "").strip()
        or (profile.display_name or "").strip()
        or (profile.profile or "").strip()
        or [tag for tag in profile.tags if str(tag).strip()]
    )


def normalize_profile(profile: MemberProfile) -> MemberProfile:
    """裁剪/去空白/去重；缺 platform 或 user_id 时抛错（没有键就存不了）。"""
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
        enabled=bool(profile.enabled),
    )
