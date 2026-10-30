"""把群友档案渲染成系统提示词片段（GUI 预览与 Agent 注入共用）。

提示词只描述"当前说话的这位群友"，不改变机器人自身身份，也不授予任何权限。
渲染时做两件事：

* 用一句明确的信任声明说明这是**用户数据**；
* 把自由文本包进 ``<member_profile>`` 边界，并转义自由文本里的尖括号，
  使它无法提前闭合边界、伪造系统片段（与常驻记忆的做法一致）。

1.1.6 起整段模板可从桌面端「提示词」页覆盖（条目 id = ``member.profile``）。
模板由 :mod:`atri_x.promptlib` 提供；**转义仍然在这里做**——用户能改的是
版式与说明文字，不能改"数据必须转义"这条硬约束。
"""

from __future__ import annotations

from typing import Any

from atri_x.memberprofile.model import (
    MAX_PROFILE_RUNES,
    MemberProfile,
    profile_has_content,
)
from atri_x.promptlib import active as active_prompt_library
from atri_x.promptlib import active_tags, render_template
from atri_x.promptlib.defaults import (
    PROMPT_MEMBER_PROFILE_HEADER,
    PROMPT_MEMBER_PROFILE_TEMPLATE,
    PROMPT_MEMBER_PROFILE_TRUST_NOTE,
)

__all__ = [
    "MEMBER_PROFILE_PROMPT_HEADER",
    "MEMBER_PROFILE_TRUST_NOTE",
    "escape_member_profile_text",
    "render_member_profile",
]

#: 提示词片段标题（内置默认；用户覆盖见 ``member.profile`` 条目）。
MEMBER_PROFILE_PROMPT_HEADER = PROMPT_MEMBER_PROFILE_HEADER

#: 信任声明：明确这是用户数据，不是系统指令；身份与权限都不因此改变。
MEMBER_PROFILE_TRUST_NOTE = PROMPT_MEMBER_PROFILE_TRUST_NOTE


def escape_member_profile_text(content: str) -> str:
    """转义尖括号，使自由文本无法开合 ``<member_profile>`` 边界。"""
    return (content or "").replace("<", "&lt;").replace(">", "&gt;")


def render_member_profile(
    profile: MemberProfile | None,
    *,
    library: Any = None,
    tag_catalog: Any = None,
) -> str:
    """把一份档案渲染成提示词片段；没有可注入内容时返回空串。

    ``library`` 为 ``None`` 时用进程级当前提示词库（GUI 预览可传显式实例）；
    ``tag_catalog`` 为 ``None`` 时用进程级当前标签库（命中词表的标签会带上说明）。
    """
    if profile is None or not profile_has_content(profile):
        return ""
    display_name = (profile.display_name or "").strip()
    alias = (profile.alias or "").strip()
    nickname_line = ""
    if display_name and display_name != alias:
        nickname_line = "昵称：" + escape_member_profile_text(display_name)
    tags = [str(tag).strip() for tag in profile.tags if str(tag).strip()]
    tags_line = ""
    if tags:
        catalog = tag_catalog if tag_catalog is not None else active_tags()
        described = catalog.describe_all(tags)
        tags_line = "标签：" + escape_member_profile_text("、".join(described))
    body = escape_member_profile_text(
        (profile.profile or "").strip()[:MAX_PROFILE_RUNES]
    )
    profile_body = "角色资料：\n" + body if body else ""
    values = {
        "platform": escape_member_profile_text((profile.platform or "").strip()),
        "scope_id": escape_member_profile_text((profile.scope_id or "").strip()),
        "user_id": escape_member_profile_text((profile.user_id or "").strip()),
        "speaking_name": escape_member_profile_text(profile.speaking_name),
        "nickname_line": nickname_line,
        "tags_line": tags_line,
        "profile_body": profile_body,
    }
    prompts = library if library is not None else active_prompt_library()
    template = prompts.text("member.profile", PROMPT_MEMBER_PROFILE_TEMPLATE)
    return render_template(template, values)
