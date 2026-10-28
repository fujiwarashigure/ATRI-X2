"""群友档案：数据模型、JSON 存储与提示词渲染的共享层。

依赖方向刻意保持单向：

.. code-block:: text

    atri_x.gui  ──┐
                  ├──→ atri_x.memberprofile
    atri_x.agent ─┘

GUI 只负责成员发现、头像缓存与编辑界面；Agent 只负责在每轮请求里按
``平台 + 完整作用域 + 用户 id`` 取当前发言人的档案并注入系统提示词。
两边共用这里的模型与存储，避免后台反向依赖 GUI（也避免两份 ``members.json``
解析逻辑各自漂移）。
"""

from __future__ import annotations

from atri_x.memberprofile.model import (
    MAX_PROFILE_RUNES,
    MAX_TAGS,
    MEMBERS_FILE,
    MEMBERS_VERSION,
    MemberDataError,
    MemberProfile,
    member_key,
    normalize_profile,
    profile_from_dict,
    profile_has_content,
    profile_to_dict,
)
from atri_x.memberprofile.prompt import (
    MEMBER_PROFILE_PROMPT_HEADER,
    MEMBER_PROFILE_TRUST_NOTE,
    escape_member_profile_text,
    render_member_profile,
)
from atri_x.memberprofile.store import (
    GUI_SUBDIR,
    UPDATED_AT_FORMAT,
    MemberProfileStore,
    load_profiles,
    members_path,
    remove_profile,
    save_profiles,
    upsert_profile,
)

__all__ = [
    "GUI_SUBDIR",
    "MAX_PROFILE_RUNES",
    "MAX_TAGS",
    "MEMBERS_FILE",
    "MEMBERS_VERSION",
    "MEMBER_PROFILE_PROMPT_HEADER",
    "MEMBER_PROFILE_TRUST_NOTE",
    "UPDATED_AT_FORMAT",
    "MemberDataError",
    "MemberProfile",
    "MemberProfileStore",
    "escape_member_profile_text",
    "load_profiles",
    "member_key",
    "members_path",
    "normalize_profile",
    "profile_from_dict",
    "profile_has_content",
    "profile_to_dict",
    "remove_profile",
    "render_member_profile",
    "save_profiles",
    "upsert_profile",
]
