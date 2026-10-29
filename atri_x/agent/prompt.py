"""提示词构造：SOUL 提供者、工具名/schema 提供者、``PromptBuilder``。

对应 Go：``internal/agent/prompt.go``。

四个要点：

* :class:`FileSoulProvider` 按 ``(size, mtime)`` 缓存文件内容：两种模式共用同一个
  ``SOUL.md``，``mode`` 只是预留给将来的多 Soul 策略。
* ``PromptBuilder`` 把「系统提示 + 会话历史」翻译成 ``[]llm.LLMMessage``：
  assistant 消息优先用 metadata 里的 ``raw_text``（那是模型真正生成的原始文本），
  摘要固定注入到 checkpoint 之后的**第一条** user 消息里，形成稳定的新上下文起点，
  同时保留该 user 的图片/文件 segment，不破坏多模态结构。
* ``tool_names_text`` 输出给模型看的工具/skill 名字列表（含 ``discover_tool`` 提示语）。
* ``assistantMetadata``（``tool_transcript.go``，属 agent-tools）在这里延迟导入，
  缺失时用字段等价的本地实现兜底。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from atri_x.llm.llm import (
    LLMMessage,
    MessageSegment,
    Role,
    ToolCallRequest,
    ToolSchema,
    prepend_segment_text,
    text_segments,
)
from atri_x.storage.models import (
    ContextSummary,
    Message,
    Session,
    SessionModeWork,
    parse_segments,
)
from atri_x.tool import PromptNames, ctx_raise_if_cancelled

__all__ = [
    "AssistantMetadata",
    "FileSoulProvider",
    "NoopToolSchemaProvider",
    "PromptBuildRequest",
    "PromptBuilder",
    "PromptToolNames",
    "SoulProvider",
    "SoulFileState",
    "ToolNameProvider",
    "ToolSchemaProvider",
    "assistant_message_metadata",
    "current_soul_file_state",
    "message_segments",
    "message_segments_from_storage",
    "new_prompt_builder",
    "same_soul_file_state",
    "storage_message_to_llm",
    "summary_user_prefix",
    "tool_name_from_metadata",
    "tool_names_text",
]

#: 工具/skill 名字类型别名。对应 Go ``PromptToolNames = tool.PromptNames``。
PromptToolNames = PromptNames


@runtime_checkable
class SoulProvider(Protocol):
    """系统提示词（Soul）来源。对应 Go ``SoulProvider``。"""

    async def system_prompt(self, ctx: Any, mode: str) -> str: ...


@dataclass
class SoulFileState:
    """SOUL 文件的 ``(size, mtime)`` 指纹。对应 Go ``soulFileState``。"""

    size: int = 0
    mod_time: datetime | None = None


@dataclass
class _SoulPromptCache:
    loaded: bool = False
    content: str = ""
    state: SoulFileState = field(default_factory=SoulFileState)


class FileSoulProvider:
    """从磁盘读 ``SOUL.md``（按 ``(size, mtime)`` 缓存）。对应 Go ``FileSoulProvider``。"""

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.mu = threading.RLock()
        self.cache = _SoulPromptCache()

    async def system_prompt(self, ctx: Any, mode: str) -> str:
        """对应 Go ``FileSoulProvider.SystemPrompt``（``mode`` 目前不参与选择）。"""
        ctx_raise_if_cancelled(ctx)
        with self.mu:
            state = current_soul_file_state(self.path)
            if self.cache.loaded and same_soul_file_state(self.cache.state, state):
                return self.cache.content
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except OSError as exc:
                raise OSError(f'read soul prompt "{self.path}": {exc}') from exc
            self.cache = _SoulPromptCache(loaded=True, content=content, state=state)
            return content


def current_soul_file_state(path: str) -> SoulFileState:
    """对应 Go ``currentSoulFileState``（文件不存在时返回零值，不报错）。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return SoulFileState()
    except OSError as exc:
        raise OSError(f'stat soul prompt "{path}": {exc}') from exc
    seconds, remainder = divmod(info.st_mtime_ns, 1_000_000_000)
    return SoulFileState(
        size=info.st_size,
        mod_time=datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder // 1000
        ),
    )


def same_soul_file_state(left: SoulFileState, right: SoulFileState) -> bool:
    """对应 Go ``sameSoulFileState``。"""
    return left.size == right.size and left.mod_time == right.mod_time


@runtime_checkable
class ToolSchemaProvider(Protocol):
    """工具 schema 来源。对应 Go ``ToolSchemaProvider``。"""

    async def schemas(
        self, ctx: Any, mode: str, session: Session | None, scope: Any
    ) -> list[ToolSchema]: ...


@runtime_checkable
class ToolNameProvider(Protocol):
    """提示里可见的工具/skill 名字来源。对应 Go ``ToolNameProvider``。"""

    async def tool_names(
        self, ctx: Any, mode: str, session: Session | None, scope: Any
    ) -> PromptToolNames: ...


class NoopToolSchemaProvider:
    """Tool Runtime 未配置时的空实现。对应 Go ``noopToolSchemaProvider``。"""

    async def schemas(
        self, ctx: Any, mode: str, session: Session | None, scope: Any
    ) -> list[ToolSchema]:
        return []

    async def tool_names(
        self, ctx: Any, mode: str, session: Session | None, scope: Any
    ) -> PromptToolNames:
        return PromptToolNames()


@dataclass
class PromptBuildRequest:
    """``PromptBuilder.build`` 的入参。对应 Go ``PromptBuildRequest``。"""

    session: Session | None = None
    scope: Any = None
    meta: Any = None
    messages: list[Message] = field(default_factory=list)
    summary: ContextSummary | None = None


class PromptBuilder:
    """把系统提示与会话历史拼成 LLM 消息序列。对应 Go ``PromptBuilder``。"""

    def __init__(self, system: Any = None) -> None:
        self.system = system

    async def build(self, ctx: Any, req: PromptBuildRequest) -> list[LLMMessage]:
        """对应 Go ``PromptBuilder.Build``。"""
        from atri_x.agent.system_prompt import SystemPromptRequest

        session = req.session
        mode = (
            session.mode
            if session is not None and session.mode
            else SessionModeWork
        )
        manager = self.system
        if manager is None:
            raise ValueError("system prompt is required")
        system_prompt = await manager.build(
            ctx,
            SystemPromptRequest(
                mode=mode, session=session, scope=req.scope, meta=req.meta
            ),
        )
        if not (system_prompt or "").strip():
            raise ValueError("system prompt is required")

        out: list[LLMMessage] = [
            LLMMessage(role=Role.SYSTEM, segments=text_segments(system_prompt))
        ]
        summary_injected = False
        for message in req.messages:
            role = str(getattr(message, "role", "") or "")
            if role not in (Role.USER, Role.ASSISTANT, Role.TOOL, Role.SYSTEM):
                continue
            content = str(getattr(message, "content", "") or "")
            metadata = assistant_message_metadata(
                str(getattr(message, "metadata", "") or "")
            )
            if role == Role.ASSISTANT and metadata.raw_text:
                content = metadata.raw_text
            segments = message_segments(content, message)
            # 摘要固定在 checkpoint 后的第一条 user 消息中，形成稳定的新上下文起点。
            # 仍保留原 user 的图片/文件 segments，避免破坏多模态输入结构。
            if req.summary is not None and not summary_injected and role == Role.USER:
                segments = prepend_segment_text(
                    segments, summary_user_prefix(req.summary.summary)
                )
                summary_injected = True
            out.append(storage_message_to_llm(role, segments, message))
        return out


def storage_message_to_llm(
    role: str, segments: list[MessageSegment], message: Message
) -> LLMMessage:
    """把存储消息转成 LLM 消息（补齐 ``name`` / ``tool_calls``）。对应 Go ``storageMessageToLLM``。"""
    out = LLMMessage(
        role=role,
        segments=list(segments),
        tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
    )
    metadata_raw = str(getattr(message, "metadata", "") or "")
    if role == Role.TOOL:
        out.name = tool_name_from_metadata(metadata_raw)
    if role == Role.ASSISTANT:
        assistant_meta = assistant_message_metadata(metadata_raw)
        out.tool_calls = assistant_meta.tool_calls
        out.reasoning_content = assistant_meta.reasoning_content
    return out


def message_segments(content: str, message: Message) -> list[MessageSegment]:
    """优先用 ``segments`` 列，缺失时退回纯文本。对应 Go ``messageSegments``。"""
    segments = message_segments_from_storage(str(getattr(message, "segments", "") or ""))
    if segments:
        return segments
    return text_segments(content)


def message_segments_from_storage(raw: str) -> list[MessageSegment]:
    """解析 ``messages.segments`` 列；非法 JSON 按空列表处理。对应 Go ``messageSegmentsFromStorage``。"""
    if not raw:
        return []
    return parse_segments(raw)


@dataclass
class AssistantMetadata:
    """assistant 消息 metadata。对应 Go ``assistantMetadata``（``tool_transcript.go``）。

    ``reasoning_content`` 是 Python 侧追加字段：思考模式的历史思维链必须能在重建
    请求时原样回传（携带 tools 的请求，上游按协议强制要求）。
    """

    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw_text: str = ""
    reasoning_content: str = ""


def assistant_message_metadata(metadata: str) -> AssistantMetadata:
    """解析 assistant 消息 metadata。

    唯一出处在 agent-tools 的 ``tool_transcript.py``（Go ``tool_transcript.go`` 的
    ``assistantMetadata``）；这里函数内延迟导入复用，缺失时用字段等价的本地解析兜底。
    """
    if not metadata:
        return AssistantMetadata()
    import importlib

    try:
        module = importlib.import_module("atri_x.agent.tool_transcript")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        module = None
    if module is not None:
        decoder = getattr(
            getattr(module, "AssistantMetadata", None), "from_json", None
        )
        if callable(decoder):
            parsed = decoder(metadata)
            if parsed is not None and hasattr(parsed, "tool_calls"):
                return parsed
    try:
        data = json.loads(metadata)
    except ValueError:
        return AssistantMetadata()
    if not isinstance(data, dict):
        return AssistantMetadata()
    calls: list[ToolCallRequest] = []
    for raw in data.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        calls.append(
            ToolCallRequest(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                arguments=str(raw.get("arguments") or ""),
            )
        )
    return AssistantMetadata(
        tool_calls=calls,
        raw_text=str(data.get("raw_text") or ""),
        reasoning_content=str(data.get("reasoning_content") or ""),
    )


def tool_name_from_metadata(metadata: str) -> str:
    """从 tool 消息 metadata 里取 ``name``。对应 Go ``toolNameFromMetadata``。"""
    if not metadata:
        return ""
    try:
        data = json.loads(metadata)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("name")
    return "" if value is None else str(value)


def tool_names_text(names: PromptToolNames | None) -> str:
    """渲染工具/skill 名字列表。对应 Go ``toolNamesText``（文本逐字一致）。"""
    parts: list[str] = []
    if names is not None and names.tools:
        parts.append("tools: " + ",".join(names.tools) + ".")
    if names is not None and names.skills:
        parts.append("skills: " + ",".join(names.skills) + ".")
    if not parts:
        return ""
    parts.append(
        "Use discover_tool(names) for all possibly relevant tools/skills, "
        "including uncertain ones; prefer extras over omissions."
    )
    return " ".join(parts)


def summary_user_prefix(summary: str) -> str:
    """摘要注入 user 消息时的前缀。对应 Go ``summaryUserPrefix``。"""
    return f"{(summary or '').strip()}\n\n当前用户输入：\n"


def new_prompt_builder(opts: Any) -> PromptBuilder:
    """按装配参数构造 :class:`PromptBuilder`。

    对应 Go 装配代码里 ``SystemPromptManager`` 的组装（``internal/app``）：按
    「对话元信息 → SOUL → 群友档案 → 常驻记忆 → 工具名清单 → 工具标签提示词」的顺序加来源。
    任何一项没配置就跳过（对应 Go 里传 ``nil`` 的写法）。
    """
    from atri_x.agent.character_guide_prompt import CharacterGuideSystemPromptSource
    from atri_x.agent.member_persona_prompt import MemberPersonaSystemPromptSource
    from atri_x.agent.member_profile_prompt import MemberProfileSystemPromptSource
    from atri_x.agent.system_prompt import SystemPromptManager
    from atri_x.agent.system_prompt_sources import (
        ConversationMetaSystemPromptSource,
        ReactionSystemPromptSource,
        ResidentMemorySystemPromptSource,
        SoulSystemPromptSource,
        ToolNamesSystemPromptSource,
    )

    soul = getattr(opts, "soul_provider", None)
    if soul is None:
        path = str(getattr(opts, "soul_path", "") or "").strip()
        soul = FileSoulProvider(path) if path else None

    sources: list[Any] = [ConversationMetaSystemPromptSource()]
    if soul is not None:
        sources.append(SoulSystemPromptSource(soul))
    # 成员专属人设必须排在 SOUL 之后：它用 ``replaces=("soul",)`` 在拼接阶段顶掉
    # 全局身份，这样同一个成员的所有对话都会按他的卡说话，其他人仍用 SOUL.md。
    characters = getattr(opts, "character_store", None)
    member_personas = getattr(opts, "member_persona_store", None)
    if characters is not None and member_personas is not None:
        sources.append(MemberPersonaSystemPromptSource(member_personas, characters))
    member_profiles = getattr(opts, "member_profile_store", None)
    if member_profiles is not None:
        sources.append(MemberProfileSystemPromptSource(member_profiles))
    resident = getattr(opts, "resident_memory_store", None)
    if resident is not None:
        sources.append(ResidentMemorySystemPromptSource(resident))
    tools = getattr(opts, "tool_provider", None)
    if tools is not None:
        sources.append(ToolNamesSystemPromptSource(tools))
    # 表情图库启用时才告诉模型可以用 [[emo:标签]]；关闭时提示词保持原样。
    reactions = getattr(opts, "reactions_config", None)
    if reactions is not None and getattr(reactions, "enabled", False):
        sources.append(ReactionSystemPromptSource(reactions))

    from atri_x.agent.tool_tag_config import ToolTagConfigSource, tool_tag_source_of

    tag_path = str(getattr(opts, "tool_tags_path", "") or "").strip()
    initial = tool_tag_source_of(getattr(opts, "tool_tags", None))
    if tag_path or initial.tags:
        sources.append(ToolTagConfigSource(tag_path, initial))
    # 人设卡操作指引：只写"什么请求走哪个工具"，避免模型用文件工具瞎翻（见模块 docstring）。
    if characters is not None:
        sources.append(CharacterGuideSystemPromptSource(characters))
    return PromptBuilder(SystemPromptManager(sources))
