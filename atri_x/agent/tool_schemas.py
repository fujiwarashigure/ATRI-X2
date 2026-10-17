"""工具 schema 装配与日志正文预览。

对应 Go：``internal/agent/chat_tools.go`` 里的 ``maxToolRoundsPerTurn`` /
``skippedToolMessages`` / ``toolsForSession`` / ``sessionToolsEnabled`` /
``withToolCapabilities`` / ``withModelCapabilities`` /
``filterToolSchemasByAllowlist`` / ``mergeToolSchemas`` / ``isBackgroundSession``
以及 ``previewRunes`` / ``compactArguments`` / ``joinAssistantText`` 一族。
``chat_tools.py`` 在文件末尾再导出，导入路径不变。
"""

from __future__ import annotations

from typing import Any
from atri_x.tool import Capabilities, with_capabilities
from atri_x.llm import LLMMessage, RoleTool, ToolCallRequest, ToolSchema, text_segments
from atri_x.agent.session_metadata import decode_session_metadata
import json


#: 用户输入、助手正文这类非工具正文的预览长度。对应 Go ``previewTextRunes``。
PREVIEW_TEXT_RUNES = 120
#: 读取/查询/说明类工具的结果预览长度。对应 Go ``toolLogQueryLimit``。
TOOL_LOG_QUERY_LIMIT = 128
#: 执行/编辑/写入/媒体类工具的结果预览长度。对应 Go ``toolLogActionLimit``。
TOOL_LOG_ACTION_LIMIT = 256
#: 工具参数正文预览长度。对应 Go ``toolLogArgumentLimit``。
TOOL_LOG_ARGUMENT_LIMIT = 256
#: "读取／查询／说明"类工具。对应 Go ``toolLogQueryTools``。
TOOL_LOG_QUERY_TOOLS = frozenset(
    {
        "read_file",
        "web_search",
        "web_extract",
        "discover_tool",
        "workspace",
        "resident_memory_read",
        "long_memory_search",
        "atrix_skill",
        "read_atrix_skill",
        "agent_skill",
        "character_list",
        "character_read",
        "character_search",
        "prompt_library_search",
        "angel_recall",
    }
)


def max_tool_rounds_per_turn(agent: Any) -> int:
    """一回合最多几轮工具调用（缺省 2）。对应 Go ``Agent.maxToolRoundsPerTurn``。"""
    config = getattr(getattr(agent, "tool_runtime", None), "config", None)
    rounds = int(getattr(config, "max_rounds_per_turn", 0) or 0)
    return rounds if rounds > 0 else 2


def skipped_tool_messages(calls: list[ToolCallRequest], max_rounds: int) -> list[LLMMessage]:
    """超过轮次上限时给每个未执行的调用补一条 tool 消息。对应 Go ``skippedToolMessages``。"""
    text = (
        f"tool call skipped: max_rounds_per_turn={max_rounds} reached. "
        "Please summarize current progress without calling more tools."
    )
    return [
        LLMMessage(
            role=RoleTool, name=call.name, tool_call_id=call.id, segments=text_segments(text)
        )
        for call in calls
    ]


async def tools_for_session(agent: Any, ctx: Any, session: Any) -> list[ToolSchema]:
    """这一轮该给模型哪些工具 schema。对应 Go ``Agent.toolsForSession``。"""
    if not session_tools_enabled(session):
        return []
    runtime = getattr(agent, "tool_runtime", None)
    provider = getattr(runtime, "provider", None)
    default_provider = bool(getattr(runtime, "default_provider", False))
    if provider is not None and not default_provider:
        schemas = provider.schemas(ctx, session.mode, session, agent.scope(ctx))
    else:
        manager = agent.tool_run_manager()
        if manager is None:
            # 没装配工具运行时：这一轮没有工具可用，而不是崩掉整轮。
            return []
        schemas = manager.schemas(
            ctx, _toolrun_view(agent, ctx, session), agent.cached_tools_for_session(session)
        )
    schemas = list(await _maybe_await(schemas) or [])
    from atri_x.agent.turn_override import turn_tool_profile, turn_tool_schemas

    name = turn_tool_profile(ctx)
    if name:
        schemas = merge_tool_schemas(schemas, turn_tool_schemas(agent, ctx, name))
    allowlist = agent.group_tool_allowlist(ctx)
    if allowlist is not None:
        schemas = filter_tool_schemas_by_allowlist(schemas, allowlist)
    return list(agent.filter_group_feature_schemas(ctx, schemas))


def _toolrun_view(agent: Any, ctx: Any, session: Any) -> Any:
    try:
        from atri_x.toolrun.toolrun import Context as ToolRunContext
    except ImportError:  # pragma: no cover - 同僚模块缺失时降级
        return None
    return ToolRunContext(
        mode=session.mode,
        session=session,
        scope=agent.scope(ctx),
        actor=agent.actor(ctx),
        disable_base_tools=is_background_session(session),
    )


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def filter_tool_schemas_by_allowlist(
    schemas: list[ToolSchema], allowlist: dict[str, bool] | None
) -> list[ToolSchema]:
    """按群白名单裁 schema。对应 Go ``filterToolSchemasByAllowlist``。"""
    if allowlist is None:
        return schemas
    out: list[ToolSchema] = []
    for schema in schemas:
        name = str(getattr(schema.function, "name", "") or "").strip().lower()
        if allowlist.get(name):
            out.append(schema)
    return out


def merge_tool_schemas(
    base: list[ToolSchema], extra: list[ToolSchema] | None
) -> list[ToolSchema]:
    """合并两批 schema（按函数名去重，先到先得）。对应 Go ``mergeToolSchemas``。"""
    if not extra:
        return base
    seen: set[str] = set()
    out: list[ToolSchema] = []
    for schema in list(base) + list(extra):
        name = str(getattr(schema.function, "name", "") or "")
        if name and name not in seen:
            seen.add(name)
            out.append(schema)
    return out


def session_tools_enabled(session: Any) -> bool:
    """只有 work 会话能用工具。对应 Go ``sessionToolsEnabled``。"""
    from atri_x.storage import SessionModeWork

    return session is not None and getattr(session, "mode", "") == SessionModeWork


def with_tool_capabilities(agent: Any, ctx: Any, session: Any) -> Any:
    """按会话当前模型标注能力。对应 Go ``Agent.withToolCapabilities``。"""
    if session is None:
        return ctx
    selected = agent.model_for_mode(session.mode)
    return with_model_capabilities(agent, ctx, selected.provider, selected.model)


def with_model_capabilities(agent: Any, ctx: Any, provider_name: str, model: str) -> Any:
    """按显式 provider/model 标注能力。对应 Go ``Agent.withModelCapabilities``。"""
    from atri_x.config import VisionSupport

    providers = getattr(getattr(agent, "model_runtime", None), "providers", None) or {}
    provider = providers.get(provider_name)
    if provider is None:
        return ctx
    vision = provider.vision_support(model) != VisionSupport.UNSUPPORTED
    return with_capabilities(ctx, Capabilities(vision=vision))


def is_background_session(session: Any) -> bool:
    """是否后台会话。对应 Go ``isBackgroundSession``。"""
    if session is None:
        return False
    metadata = decode_session_metadata(getattr(session, "metadata", "") or "")
    return bool(str(metadata.background_kind or "").strip())


def preview_log_text(text: str) -> str:
    """日志里的正文预览。对应 Go ``previewLogText``。"""
    return preview_runes(text, PREVIEW_TEXT_RUNES)


def tool_log_result_limit(tool_name: str) -> int:
    """按工具类别取结果预览长度。对应 Go ``toolLogResultLimit``。"""
    name = str(tool_name or "").strip()
    if name in TOOL_LOG_QUERY_TOOLS:
        return TOOL_LOG_QUERY_LIMIT
    return TOOL_LOG_ACTION_LIMIT


def preview_runes(text: str, max_runes: int) -> str:
    """按 rune 截断并补省略号。对应 Go ``previewRunes``。"""
    value = str(text or "").strip()
    if not value or max_runes <= 0:
        return ""
    if len(value) <= max_runes:
        return value
    return value[:max_runes] + "..."


def join_assistant_text(first: str, second: str) -> str:
    """把两段助手文本接起来（空段跳过，段间空行）。对应 Go ``joinAssistantText``。"""
    left = str(first or "").strip()
    right = str(second or "").strip()
    if not left:
        return right
    if not right:
        return left
    return left + "\n\n" + right


def join_tool_names(calls: list[ToolCallRequest]) -> str:
    """工具名列表。对应 Go ``joinToolNames``。"""
    return ", ".join(call.name for call in calls if call.name)


def compact_arguments(args: str) -> str:
    """把 JSON 参数压成一行。对应 Go ``compactArguments``。"""
    value = str(args or "").strip()
    if not value:
        return "{}"
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return value


def preview_arguments(args: str) -> str:
    """参数预览。对应 Go ``previewArguments``。"""
    return preview_runes(compact_arguments(args), TOOL_LOG_ARGUMENT_LIMIT)
