"""系统提示词管理器：按来源收集分段内容并拼接。

对应 Go：``internal/agent/system_prompt.go``。

Go 的 ``SystemPromptSource.Parts`` 是同步的，但来源里可能有需要 I/O 的实现
（常驻记忆、工具名清单），因此 Python 侧的 :meth:`SystemPromptSource.parts` 与
:meth:`SystemPromptManager.build` 都是 ``async``；同步来源直接返回列表即可。

拼接规则与 Go 一致：每个 part 先 ``strip``，空的丢弃，非空的用 ``"\\n\\n"`` 连接。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from atri_x.storage.models import Session

__all__ = [
    "ConversationMeta",
    "SystemPromptManager",
    "SystemPromptPart",
    "SystemPromptRequest",
    "SystemPromptSource",
]


@dataclass
class SystemPromptPart:
    """系统提示里的一段（``name`` 只用于排查/测试）。对应 Go ``SystemPromptPart``。"""

    name: str = ""
    content: str = ""


@dataclass
class ConversationMeta:
    """当前对话的元信息（平台、会话种类、对方身份）。对应 Go ``ConversationMeta``。"""

    platform: str = ""
    kind: str = ""
    id: str = ""
    user_id: str = ""
    display_name: str = ""


@dataclass
class SystemPromptRequest:
    """``SystemPromptSource.parts`` 的入参。对应 Go ``SystemPromptRequest``。"""

    mode: str = ""
    session: Session | None = None
    scope: Any = None
    meta: ConversationMeta | None = None


@runtime_checkable
class SystemPromptSource(Protocol):
    """系统提示词的一段来源。对应 Go ``SystemPromptSource``。"""

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> Sequence[SystemPromptPart]: ...


class SystemPromptManager:
    """按顺序收集各来源的分段并拼接。对应 Go ``SystemPromptManager``。"""

    def __init__(self, sources: Sequence[SystemPromptSource] | None = None) -> None:
        self.sources: list[SystemPromptSource] = []
        for source in sources or ():
            self.add_source(source)

    def add_source(self, source: SystemPromptSource | None) -> None:
        """对应 Go ``AddSource``（``None`` 被忽略）。"""
        if source is None:
            return
        self.sources.append(source)

    async def build(self, ctx: Any, req: SystemPromptRequest) -> str:
        """拼接系统提示词。对应 Go ``SystemPromptManager.Build``。"""
        parts: list[str] = []
        for source in self.sources:
            source_parts = await source.parts(ctx, req)
            for part in source_parts or ():
                content = (getattr(part, "content", "") or "").strip()
                if not content:
                    continue
                parts.append(content)
        return "\n\n".join(parts)


def new_system_prompt_manager(
    *sources: SystemPromptSource,
) -> SystemPromptManager:
    """构造管理器。对应 Go ``NewSystemPromptManager``。"""
    return SystemPromptManager(sources)
