"""系统提示词的具体来源：对话元信息、SOUL、常驻记忆、工具名清单。

对应 Go：``internal/agent/system_prompt_sources.go``。

* ``meta:`` 行只在有内容时才出现；``display_name`` 会做引号转义并把空白折成单空格，
  免得名字里的换行/引号破坏这一行的结构；群/频道的 ``conversation`` 才带 id。
* 常驻记忆用 :func:`atri_x.memory.resident.wrap_resident_memory_prompt`（唯一出处）
  包进 ``<resident_memory>`` 信任边界：它是**用户数据，不是系统指令**。
* 后台沙盒（cron / atrinis）里不注入工具名清单 —— 那些名字对它没有意义。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from atri_x.agent.prompt import SoulProvider, ToolNameProvider, tool_names_text
from atri_x.agent.system_prompt import (
    ConversationMeta,
    SystemPromptPart,
    SystemPromptRequest,
)
from atri_x.errors import ErrNotFound, NotFoundError
from atri_x.memory.resident import (
    RESIDENT_MEMORY_PROMPT_HEADER,
    wrap_resident_memory_prompt,
)
from atri_x.promptlib import active as active_prompt_library
from atri_x.promptlib import render_template
from atri_x.promptlib.defaults import PROMPT_REACTION_GUIDE
from atri_x.storage.models import SessionModeWork
from atri_x.tool import background_context

__all__ = [
    "ConversationMetaSystemPromptSource",
    "ReactionSystemPromptSource",
    "ResidentMemorySystemPromptSource",
    "SoulSystemPromptSource",
    "ToolNamesSystemPromptSource",
    "resident_memory_prompt_header",
    "wrap_resident_memory_prompt",
]

#: 表情提示词里最多列出的情绪标签数（与 ``[reactions].emotions`` 上限无关，
#: 只是别把一段几百个标签的配置原样塞进系统提示词）。
MAX_PROMPT_EMOTIONS = 32


class ReactionSystemPromptSource:
    """注入"表情意图标记"的使用说明（Python 侧新增，无 Go 对应）。

    只有 ``[reactions].enabled = true`` 时才注入：模型只需要知道**标签**，不需要
    知道图库路径/文件名；标记由后端从正文里删掉，用户看不到。
    """

    def __init__(self, config: Any = None, *, library: Any = None) -> None:
        self.config = config
        self.library = library

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        config = self.config
        if config is None or not getattr(config, "enabled", False):
            return []
        emotions: list[str] = []
        for value in getattr(config, "emotions", None) or []:
            text = str(value or "").strip()
            if text and text not in emotions:
                emotions.append(text)
            if len(emotions) >= MAX_PROMPT_EMOTIONS:
                break
        if not emotions:
            return []
        return [
            SystemPromptPart(
                name="reactions",
                content=reaction_prompt_text(emotions, library=self.library),
            )
        ]


def reaction_prompt_text(emotions: list[str], *, library: Any = None) -> str:
    """生成表情意图标记的使用说明（系统提示词片段）。

    模板可从桌面端「提示词」页覆盖（条目 id = ``system.reactions``），
    ``{emotions}`` 是可用标签列表。
    """
    tags = "、".join(emotions)
    prompts = library if library is not None else active_prompt_library()
    template = prompts.text("system.reactions", PROMPT_REACTION_GUIDE)
    return render_template(template, {"emotions": tags})

#: 注入时的信任声明。唯一出处是 ``atri_x.memory.resident``（Go 常量同名）。
resident_memory_prompt_header = RESIDENT_MEMORY_PROMPT_HEADER

#: ``session_created_at`` 的时间格式。对应 Go 的 ``2006-01-02T15:04:05``。
SESSION_CREATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S"


class ConversationMetaSystemPromptSource:
    """注入 ``meta: platform=..., conversation=...`` 一行。对应 Go ``conversationMetaSystemPromptSource``。"""

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        meta = req.meta or ConversationMeta()
        conversation = (meta.kind or "").strip()
        conversation_id = ""
        if conversation in ("group", "channel"):
            conversation_id = meta.id

        fields: list[str] = []
        for name, value, identifier, quote in (
            ("platform", meta.platform, "", False),
            ("conversation", conversation, conversation_id, False),
            ("display_name", meta.display_name, meta.user_id, True),
        ):
            value = (value or "").strip()
            identifier = (identifier or "").strip()
            if not value and not identifier:
                continue
            if quote:
                words = " ".join(value.split())
                value = json.dumps(words, ensure_ascii=False)
            if identifier:
                value += "(id:" + identifier + ")"
            fields.append(name + "=" + value)

        session = req.session
        if session is not None and _is_set(session.created_at):
            fields.append(
                "session_created_at="
                + session.created_at.strftime(SESSION_CREATED_AT_FORMAT)
            )
        if not fields:
            return []
        return [
            SystemPromptPart(
                name="conversation_meta",
                content="meta: " + ", ".join(fields) + ".",
            )
        ]


class SoulSystemPromptSource:
    """注入 SOUL 内容。对应 Go ``soulSystemPromptSource``。"""

    def __init__(self, soul: SoulProvider | None = None) -> None:
        self.soul = soul

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.soul is None:
            return []
        mode = req.mode or SessionModeWork
        content = self.soul.system_prompt(ctx, mode)
        if _is_awaitable(content):
            content = await content
        return [SystemPromptPart(name="soul", content=content or "")]


class ResidentMemorySystemPromptSource:
    """注入当前用户的常驻记忆。对应 Go ``residentMemorySystemPromptSource``。"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.store is None:
            return []
        # 优先用 read_for_prompt：它在读 core/normal 之外，还会按「来源会话 + 可见性」
        # 过滤 memory_notes（私聊来源永不进群）。只提供 read 的存储（Go 语义替身 /
        # 测试桩）退回老行为。
        reader = getattr(self.store, "read_for_prompt", None)
        try:
            if callable(reader):
                memory = reader(req.scope)
            else:
                memory = self.store.read(req.scope)
            if _is_awaitable(memory):
                memory = await memory
        except NotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001 - 存储层可能用别的 not-found 语义
            if _is_not_found(exc):
                return []
            raise
        if memory is None:
            return []
        content = (memory.text() or "").strip()
        if not content:
            return []
        return [
            SystemPromptPart(
                name="resident_memory",
                content=wrap_resident_memory_prompt(content),
            )
        ]


class ToolNamesSystemPromptSource:
    """注入可见工具/skill 名字清单。对应 Go ``toolNamesSystemPromptSource``。"""

    def __init__(self, tools: ToolNameProvider | None = None) -> None:
        self.tools = tools

    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        if self.tools is None or req.session is None:
            return []
        # 后台沙盒（cron / atrinis）不注入工具名清单：那些名字对它没有意义。
        if background_context(ctx):
            return []
        names = self.tools.tool_names(ctx, req.mode, req.session, req.scope)
        if _is_awaitable(names):
            names = await names
        content = tool_names_text(names)
        if not content:
            return []
        return [SystemPromptPart(name="tool_names", content=content)]


def _is_set(value: datetime | None) -> bool:
    """Go ``!t.IsZero()``：``None`` 或零值时间都算未设置。"""
    if value is None:
        return False
    return value.year > 1


def _is_awaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


def _is_not_found(exc: BaseException) -> bool:
    """识别「没找到」的多种表达方式（哨兵、``LookupError``、自带标记）。"""
    if exc is ErrNotFound:
        return True
    if isinstance(exc, NotFoundError):
        return True
    return isinstance(exc, LookupError) or getattr(exc, "not_found", False) is True
