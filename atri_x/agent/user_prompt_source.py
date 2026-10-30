"""用户自建提示词的系统提示词来源。

桌面端「提示词」页里，用户自建的条目可以勾选「注入系统提示词」。勾选后的条目
会作为**独立片段**追加到每轮系统提示词末尾（不是拼进 SOUL.md），因此：

* 改完保存后下一轮生效（提示词库按文件指纹热加载），不需要重启后台服务；
* 每条一个 ``SystemPromptPart``，排查时可以按 ``user_prompt:<id>`` 找到来源；
* 只在用户显式勾选时注入；内置模板（生图改写、识图模板等）不走这里。

边界：这些文本会直接给模型看，属于**用户自己的系统提示词扩展**；它不改变
权限、工具边界与安全规则（那些由代码强制，不经过提示词）。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.promptlib import active as active_prompt_library

__all__ = ["UserPromptsSystemPromptSource"]


class UserPromptsSystemPromptSource:
    """把 ``inject=True`` 的用户条目注入系统提示词。"""

    def __init__(self, library: Any = None) -> None:
        self.library = library

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        prompts = self.library if self.library is not None else active_prompt_library()
        parts: list[SystemPromptPart] = []
        for entry in prompts.injected():
            content = entry.text().strip()
            if not content:
                continue
            parts.append(
                SystemPromptPart(name="user_prompt:" + entry.id, content=content)
            )
        return parts
