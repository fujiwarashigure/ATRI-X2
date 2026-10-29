"""人设卡操作指引（系统提示词来源）。

1.1.3 实测的失败模式：用户把一整段人设贴进对话并说「加入人设卡并启用」，模型没有
对应工具的直接路径，于是用 ``workspace`` / ``read_file`` 去翻配置文件，连翻 10 轮
撞上 ``max_rounds_per_turn`` 后整轮没有结果。工具本身（``character_manage``）是
可用的，缺的是"看到这类请求该走哪条路"的指引。

所以这里只写**路由信息**（哪句话该调哪个工具），不复述工具参数；角色库未启用时
整段不注入，对没开这个功能的人零开销。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.tool import background_context

__all__ = ["CHARACTER_GUIDE_TEXT", "CharacterGuideSystemPromptSource"]

CHARACTER_GUIDE_TEXT = (
    "## 人设卡操作指引\n"
    "- 用户粘贴一段人设文本并说「保存成人设卡 / 导入 / 建卡 / 启用」时：直接调用 "
    "character_import（一次调用完成建卡）。不要用 read_file / shell / workspace 去翻"
    "配置文件或 SOUL.md，也不要先查目录结构。\n"
    "- 用户说「切换 / 启用 / 关闭我的专属人设」时：调用 persona_control。关闭后该成员"
    "恢复默认系统人设，只影响他本人，不会影响其他人。\n"
    "- 在桌面端 / 本机 CLI 对话里（platform=cli，用户就是本机操作者），用户对一段人设说"
    "「启用」通常指设为全局人设：可以用 character_import(activate=\"soul\") 一步完成；"
    "只有用户明确要求全局、且你是超级管理员时才这样做。\n"
    "- 普通成员建的卡默认私有（只有本人和超级管理员可见/可改）；公开卡需要超级管理员。\n"
    "- @char:<id> 只对当前这一条消息生效；要长期生效请用 persona_control 或 "
    "character_activate。"
)


class CharacterGuideSystemPromptSource:
    """角色库启用时注入人设卡操作指引。"""

    def __init__(self, characters: Any = None) -> None:
        self.characters = characters

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.characters is None:
            return []
        if background_context(ctx):
            return []
        enabled = getattr(self.characters, "enabled", None)
        if callable(enabled) and not enabled():
            return []
        return [
            SystemPromptPart(name="character_guide", content=CHARACTER_GUIDE_TEXT)
        ]
