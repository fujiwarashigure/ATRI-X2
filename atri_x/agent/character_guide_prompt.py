"""人设卡操作指引（系统提示词来源）。

1.1.3 实测的失败模式：用户把一整段人设贴进对话并说「加入人设卡并启用」，模型没有
对应工具的直接路径，于是用 ``workspace`` / ``read_file`` 去翻配置文件，连翻 10 轮
撞上 ``max_rounds_per_turn`` 后整轮没有结果。工具本身（``character_manage``）是
可用的，缺的是"看到这类请求该走哪条路"的指引。

所以这里只写**路由信息**（哪句话该调哪个工具），不复述工具参数；角色库未启用时
整段不注入，对没开这个功能的人零开销。

1.1.6 起正文可从桌面端「提示词」页覆盖（条目 id = ``system.character_guide``）。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.promptlib import active as active_prompt_library
from atri_x.promptlib.defaults import PROMPT_CHARACTER_GUIDE
from atri_x.tool import background_context

__all__ = ["CHARACTER_GUIDE_TEXT", "CharacterGuideSystemPromptSource"]

#: 内置默认正文（用户覆盖见 ``system.character_guide`` 条目）。
CHARACTER_GUIDE_TEXT = PROMPT_CHARACTER_GUIDE


class CharacterGuideSystemPromptSource:
    """角色库启用时注入人设卡操作指引。"""

    def __init__(self, characters: Any = None, *, library: Any = None) -> None:
        self.characters = characters
        self.library = library

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
        prompts = self.library if self.library is not None else active_prompt_library()
        content = prompts.text("system.character_guide", CHARACTER_GUIDE_TEXT)
        return [SystemPromptPart(name="character_guide", content=content)]
