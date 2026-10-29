"""成员专属人设的系统提示词来源。

与 :mod:`atri_x.agent.member_profile_prompt` 的分工：

* 群友档案（``member_profile``）回答"说话的人是谁"，只描述对方；
* 专属人设（``member_persona``）回答"和这个人说话时机器人演谁"，**替换**全局
  ``SOUL.md`` —— 通过 :class:`~atri_x.agent.system_prompt.SystemPromptPart` 的
  ``replaces=("soul",)`` 在拼接阶段生效。

边界：

* 只按 ``平台 + 用户 id`` 找绑定（见 :mod:`atri_x.memberprofile.persona`），与群
  无关：同一个 QQ 换群 / 私聊都跟着他，这也是成员自己开关时最直观的语义；
* 私聊与群聊都生效；频道 / 后台沙盒不注入；
* 绑定的卡不存在或对该成员不可见时**静默回退 SOUL.md**（不让坏绑定把身份搞没）；
* 关闭开关只停用绑定，不删记录，成员随时可以再打开。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.memberprofile.persona import PERSONA_PROMPT_RUNES, render_member_persona
from atri_x.tool import background_context

__all__ = ["MemberPersonaSystemPromptSource"]


class MemberPersonaSystemPromptSource:
    """注入当前发言人的专属人设，并顶掉全局 SOUL 段。"""

    def __init__(self, store: Any = None, characters: Any = None) -> None:
        self.store = store
        self.characters = characters

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.store is None or self.characters is None:
            return []
        if background_context(ctx):
            return []
        meta = req.meta
        if meta is None:
            return []
        kind = (getattr(meta, "kind", "") or "").strip()
        platform = (getattr(meta, "platform", "") or "").strip()
        user_id = (getattr(meta, "user_id", "") or "").strip()
        if not platform or not user_id:
            return []
        # 群聊 / 私聊都支持；频道暂不注入（频道里"当前发言人"语义不稳定）。
        # CLI 的 scope 是 ``local``、kind 为空，但用户就是本机操作者，同样支持。
        if kind not in ("group", "private", "") or (kind == "" and platform != "cli"):
            return []
        getter = getattr(self.store, "get", None)
        if not callable(getter):
            return []
        persona = getter(platform=platform, user_id=user_id)
        if persona is None or not getattr(persona, "active", False):
            return []
        content = render_member_persona(
            persona, self.characters, max_runes=PERSONA_PROMPT_RUNES
        )
        if not content:
            return []
        return [
            SystemPromptPart(
                name="member_persona",
                content=content,
                replaces=("soul",),
            )
        ]
