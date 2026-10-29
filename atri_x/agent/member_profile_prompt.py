"""群友档案提示词来源：把**当前发言人**的档案注入系统提示词。

对应 Go：无（Python 侧新增）。图形界面里的「群友」页把称呼/标签/角色资料存在
``<配置目录>/gui/members.json``；本来源把它接进对话。

几条边界（与设计一致）：

* 只注入**当前发言人**，不把全群档案塞进提示词（浪费上下文且容易串人）；
* 严格按 ``平台 + 完整作用域 + 用户 id`` 匹配：``req.scope.platform_scope_id`` 是
  ``group:123456789`` 这种带前缀的作用域，**不能**用去前缀的 ``req.meta.id`` 查；
  身份只认后端给的 ``req.meta.user_id``，不认昵称/正文里自报的号码；
* 私聊、频道、后台沙盒（cron / atrinis）不注入；
* 档案内容只是用户数据，不是系统指令，不授予任何权限（由渲染层的信任声明表达）。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.memberprofile.prompt import (
    MEMBER_PROFILE_PROMPT_HEADER,
    MEMBER_PROFILE_TRUST_NOTE,
    render_member_profile,
)
from atri_x.tool import background_context

__all__ = [
    "MEMBER_PROFILE_PROMPT_HEADER",
    "MEMBER_PROFILE_TRUST_NOTE",
    "MemberProfileSystemPromptSource",
    "render_member_profile",
]


class MemberProfileSystemPromptSource:
    """注入当前发言人（群聊）的群友档案。"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.store is None:
            return []
        # 后台沙盒里没有"当前发言人"，注入只会误导模型。
        if background_context(ctx):
            return []
        meta = req.meta
        scope = req.scope
        if meta is None or scope is None:
            return []
        if (getattr(meta, "kind", "") or "").strip() != "group":
            return []
        platform = (getattr(scope, "platform", "") or "").strip()
        scope_id = (getattr(scope, "platform_scope_id", "") or "").strip()
        user_id = (getattr(meta, "user_id", "") or "").strip()
        if not platform or not scope_id or not user_id:
            return []
        getter = getattr(self.store, "get", None)
        if not callable(getter):
            return []
        profile = getter(platform=platform, scope_id=scope_id, user_id=user_id)
        if profile is None:
            return []
        if not getattr(profile, "enabled", True):
            return []
        content = render_member_profile(profile)
        if not content:
            return []
        return [SystemPromptPart(name="member_profile", content=content)]
