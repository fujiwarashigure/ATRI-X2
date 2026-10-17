"""Angel 长期记忆工具族：写入、召回，以及可选的删除（angel_forget）。

对应 Go：``internal/tool/builtin/angel_memory.go`` + ``angel_forget.go``。

权限边界（照实复刻，不要放宽）：

* scope 只来自平台消息上下文，actor 只来自认证 ctx；
* ``angel_forget`` 只能删**来源成员等于当前发言人**的记忆，且
  ``source_actor_id`` 为空的历史行永不匹配；
* 删除按 ``(platform, scope_id, actor_id)`` 做每分钟 3 次的限流，
  模型无法逐条清空一个 scope（批量删除仍走运维命令 ``/memory delete``、``/forget``）；
* 首次调用只返回待删内容，必须再传 ``confirm=true`` 才真正删除。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.angelmemory import (
    ForgetGuard,
    Memory,
    Service,
    Source,
    SourceFilter,
)
from atri_x.errors import (
    AmbiguousIDError,
    AtriXError,
    InvalidError,
    NotFoundError,
)
from atri_x.llm.llm import ToolSchema
from atri_x.platform import message_context_from
from atri_x.ratelimit import new as new_rate_limit
from atri_x.security import actor_from_context
from atri_x.tool.builder import Builder, required
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskLow

ANGEL_REMEMBER_TOOL_NAME = "angel_remember"
ANGEL_RECALL_TOOL_NAME = "angel_recall"
ANGEL_FORGET_TOOL_NAME = "angel_forget"
ANGEL_FORGET_MAX_PER_MINUTE = 3

SERVICE_NOT_CONFIGURED = "angel memory 服务未配置。"
NO_PLATFORM_CONTEXT = "当前上下文没有平台聊天信息，无法确定记忆范围。"


@dataclass
class AngelMemoryToolOptions:
    """对应 Go ``AngelMemoryToolOptions``。"""

    allow_forget: bool = False


@dataclass
class AngelRememberArgs:
    """对应 Go ``angelRememberArgs``。"""

    content: str = ""
    tags: str = ""


@dataclass
class AngelRecallArgs:
    """对应 Go ``angelRecallArgs``。"""

    query: str = ""
    limit: int = 0


@dataclass
class AngelForgetArgs:
    """对应 Go ``angelForgetArgs``。"""

    id: str = ""
    confirm: bool = False


@dataclass
class AngelForgetTarget:
    """对应 Go ``angelForgetTarget``。"""

    memory: Memory | None = None
    actor_id: str = ""


@dataclass
class AngelForgetResolved:
    """对应 Go ``angelForgetResolved``。"""

    memory: Memory | None = None
    actor_id: str = ""
    confirmed: bool = False


class AngelRememberTool:
    """对应 Go ``AngelRememberTool``。"""

    def __init__(self, service: Service | None = None) -> None:
        self.service = service

    def name(self) -> str:
        return ANGEL_REMEMBER_TOOL_NAME

    def info(self) -> Info:
        return angel_remember_builder().build_info()

    def schema(self) -> ToolSchema:
        return angel_remember_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``AngelRememberTool.Call``。"""
        if self.service is None or not self.service.ready():
            return Result(content=SERVICE_NOT_CONFIGURED)
        msg, ok = message_context_from(ctx)
        if not ok or (msg.platform or "").strip() == "" or (msg.scope_id or "").strip() == "":
            return Result(content="当前上下文没有平台聊天信息，无法确定记忆范围。")
        args = _decode(req.arguments, AngelRememberArgs, ANGEL_REMEMBER_TOOL_NAME)
        content = (args.content or "").strip()
        if content == "":
            return Result(content="content 不能为空。")
        actor_id = _actor_id(ctx, msg)
        memory = self.service.remember_with_source(
            msg.platform,
            msg.scope_id,
            content,
            args.tags,
            Source(
                kind="tool",
                actor_id=actor_id,
                message_id=(msg.platform_message_id or "").strip(),
                session_id=(msg.session_id or "").strip(),
                label="tool",
            ),
        )
        return Result(content=f"已记住（强度 {memory.strength}）：{memory.content}")


class AngelRecallTool:
    """对应 Go ``AngelRecallTool``。"""

    def __init__(self, service: Service | None = None, expose_forget: bool = False) -> None:
        self.service = service
        self.expose_forget = expose_forget

    def name(self) -> str:
        return ANGEL_RECALL_TOOL_NAME

    def info(self) -> Info:
        return self.builder().build_info()

    def schema(self) -> ToolSchema:
        return self.builder().build_schema()

    def builder(self) -> Builder:
        """对应 Go ``AngelRecallTool.builder``：允许删除时把 angel_forget 挂成依赖。

        隐藏工具只能作为可见根工具的依赖抵达模型，这一步是为让 angel_forget 可达。
        """
        builder = angel_recall_builder()
        if self.expose_forget:
            builder = builder.depends_on(ANGEL_FORGET_TOOL_NAME)
        return builder

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``AngelRecallTool.Call``。"""
        if self.service is None or not self.service.ready():
            return Result(content=SERVICE_NOT_CONFIGURED)
        msg, ok = message_context_from(ctx)
        if not ok or (msg.platform or "").strip() == "" or (msg.scope_id or "").strip() == "":
            return Result(content=NO_PLATFORM_CONTEXT)
        args = _decode(req.arguments, AngelRecallArgs, ANGEL_RECALL_TOOL_NAME)
        memories = self.service.recall(
            msg.platform, msg.scope_id, args.query, args.limit
        )
        if not memories:
            return Result(content="没有找到符合条件的长期记忆。")
        lines = [f"找到 {len(memories)} 条相关记忆："]
        for position, memory in enumerate(memories):
            lines.append(
                f"{position + 1}. [{memory.strength}] id={memory.id} "
                f"{memory.content}{memory_trace_suffix(memory)}"
            )
        return Result(content="\n".join(lines))


class AngelForgetTool:
    """对应 Go ``AngelForgetTool``（高风险、owner_scoped、hidden）。"""

    def __init__(self, service: Service | None = None, deletions: Any = None) -> None:
        self.service = service
        self.deletions = deletions

    def name(self) -> str:
        return ANGEL_FORGET_TOOL_NAME

    def info(self) -> Info:
        return angel_forget_builder().build_info()

    def schema(self) -> ToolSchema:
        return angel_forget_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``AngelForgetTool.Call``。"""
        refusal, target = self.resolve(ctx, req)
        if refusal != "":
            return Result(content=refusal)
        assert target.memory is not None
        if not target.confirmed:
            return Result(content=angel_forget_preview(target.memory))
        if self.deletions is not None and not self.deletions.allow(
            angel_forget_rate_key(ctx, target.actor_id)
        ):
            return Result(
                content=(
                    f"同一范围内 1 分钟最多通过工具删除 {ANGEL_FORGET_MAX_PER_MINUTE} "
                    "条长期记忆，请稍后再试，或让用户使用 /forget。"
                )
            )
        assert self.service is not None
        self.service.delete(
            target.memory.platform, target.memory.scope_id, target.memory.id
        )
        return Result(
            content=(
                f"已删除长期记忆 {short_angel_memory_id(target.memory.id)}。\n"
                f"内容：{angel_forget_inline(target.memory.content)}"
            )
        )

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """对应 Go ``AngelForgetTool.PreflightConfirmation``：先解析再让用户确认。"""
        refusal, _ = self.resolve(ctx, req)
        if refusal != "":
            raise InvalidError(refusal)

    async def risk_detail(self, ctx: Any, req: CallRequest) -> str:
        """对应 Go ``AngelForgetTool.RiskDetail``：展示将被删除的确切内容。"""
        refusal, target = self.resolve(ctx, req)
        if refusal != "":
            raise InvalidError(refusal)
        assert target.memory is not None
        out = ["操作：删除长期记忆（仅限来源为当前发言人的记忆）\n"]
        out.append(f"id：{target.memory.id}\n")
        out.append(f"强度：{target.memory.strength}\n")
        out.append(f"内容：{angel_forget_inline(target.memory.content)}\n")
        source = angel_forget_source_text(target.memory)
        if source != "":
            out.append("来源：" + source + "\n")
        out.append("删除后无法恢复。")
        return "".join(out)

    def resolve(self, ctx: Any, req: CallRequest) -> tuple[str, AngelForgetResolved]:
        """对应 Go ``AngelForgetTool.resolve``：返回 ``(拒绝原因, 目标)``。

        不做网络/DB 之外的副作用；拒绝原因非空时调用方必须原样把它回喂给模型。
        """
        resolved = AngelForgetResolved()
        if self.service is None or not self.service.ready():
            return SERVICE_NOT_CONFIGURED, resolved
        msg, ok = message_context_from(ctx)
        if not ok or (msg.platform or "").strip() == "" or (msg.scope_id or "").strip() == "":
            return NO_PLATFORM_CONTEXT, resolved
        args = _decode(req.arguments, AngelForgetArgs, ANGEL_FORGET_TOOL_NAME)
        prefix = (args.id or "").strip()
        if prefix == "":
            return "id 不能为空。请先用 angel_recall 取得记忆 id，再删除。", resolved
        actor_id = _actor_id(ctx, msg)
        if actor_id == "":
            return (
                "当前上下文没有足够的用户信息，无法确认这条记忆是否属于当前用户，已拒绝删除。",
                resolved,
            )
        # 过滤条件就是权限边界：只有来源成员是当前发言人的记忆才可能被解析到。
        # 结构化来源之前写入的旧行（source_actor_id 为空）永远不匹配。
        try:
            memory_id = self.service.resolve_id(
                msg.platform,
                msg.scope_id,
                prefix,
                SourceFilter(actor_id=actor_id),
            )
        except NotFoundError:
            return (
                f"当前范围没有找到 id 前缀为 {prefix!r} 且来源为你自己的长期记忆。",
                resolved,
            )
        except AmbiguousIDError:
            return f"id 前缀 {prefix!r} 不唯一，请提供更多字符。", resolved
        memory = self.service.get(msg.platform, msg.scope_id, memory_id)
        if (memory.source_actor_id or "").strip() == "" or memory.source_actor_id != actor_id:
            return "这条记忆的来源成员不是当前用户，已拒绝删除。", resolved
        resolved.memory = memory
        resolved.actor_id = actor_id
        resolved.confirmed = args.confirm
        return "", resolved


def new_angel_memory_tools(
    service: Service | None,
    opts: AngelMemoryToolOptions | None = None,
    _infos: list[Any] | None = None,
) -> list[Any]:
    """对应 Go ``NewAngelMemoryTools``。"""
    opts = opts or AngelMemoryToolOptions()
    tools: list[Any] = [
        AngelRememberTool(service),
        AngelRecallTool(service, expose_forget=opts.allow_forget),
    ]
    if not opts.allow_forget:
        return tools
    tools.append(
        AngelForgetTool(
            service,
            deletions=new_rate_limit(ANGEL_FORGET_MAX_PER_MINUTE, 60.0),
        )
    )
    return tools


def memory_trace_suffix(memory: Memory) -> str:
    """对应 Go ``memoryTraceSuffix``。"""
    parts: list[str] = []
    if (memory.source_kind or "").strip():
        parts.append("source=" + memory.source_kind.strip())
    if (memory.source_actor_id or "").strip():
        parts.append("actor=" + memory.source_actor_id.strip())
    if (memory.source_message_id or "").strip():
        parts.append("message=" + memory.source_message_id.strip())
    if (memory.source_session_id or "").strip():
        parts.append("session=" + memory.source_session_id.strip())
    if not parts and (memory.source or "").strip():
        parts.append("source=" + memory.source.strip())
    if not parts:
        return ""
    return " (" + " ".join(parts) + ")"


def angel_forget_rate_key(ctx: Any, actor_id: str) -> str:
    """对应 Go ``angelForgetRateKey``：``platform\\x00scope\\x00actor``。"""
    msg, _ = message_context_from(ctx)
    return "\x00".join([msg.platform, msg.scope_id, actor_id])


def angel_forget_preview(memory: Memory) -> str:
    """对应 Go ``angelForgetPreview``。"""
    lines = [
        "待删除的长期记忆（尚未删除）：",
        f"id：{memory.id}",
        f"内容：{angel_forget_inline(memory.content)}",
    ]
    source = angel_forget_source_text(memory)
    if source != "":
        lines.append("来源：" + source)
    lines.append("确认删除请再次调用 angel_forget，并传 confirm=true；删除后无法恢复。")
    return "\n".join(lines)


def angel_forget_source_text(memory: Memory) -> str:
    """对应 Go ``angelForgetSourceText``。"""
    parts: list[str] = []
    if (memory.source_kind or "").strip():
        parts.append("kind=" + memory.source_kind.strip())
    if (memory.source_message_id or "").strip():
        parts.append("message=" + memory.source_message_id.strip())
    if (memory.source_session_id or "").strip():
        parts.append("session=" + memory.source_session_id.strip())
    return " ".join(parts)


def angel_forget_inline(content: str) -> str:
    """对应 Go ``angelForgetInline``：压空白后按字符截到 200。"""
    text = " ".join((content or "").split())
    if len(text) > 200:
        return text[:200] + "…"
    return text


def short_angel_memory_id(id: str) -> str:
    """对应 Go ``shortAngelMemoryID``。"""
    id = (id or "").strip()
    return id if len(id) <= 12 else id[:12]


def angel_remember_builder() -> Builder:
    """对应 Go ``angelRememberBuilder``。"""
    return (
        Builder(ANGEL_REMEMBER_TOOL_NAME)
        .description(
            "记录一条当前平台/会话范围内的长期记忆。用户明确表达重要偏好、事实或约定时使用。"
        )
        .risk(RiskLow)
        .tags("memory")
        .string("content", "要记住的内容，建议第三人称、一条一件事。", required())
        .string("tags", "可选标签，用逗号或空格分隔。")
    )


def angel_recall_builder() -> Builder:
    """对应 Go ``angelRecallBuilder``。"""
    return (
        Builder(ANGEL_RECALL_TOOL_NAME)
        .description(
            "按关键词检索当前平台/会话范围内的长期记忆；不传关键词时返回强度最高的记忆。"
        )
        .risk(RiskLow)
        .tags("memory")
        .string("query", "检索关键词，留空返回强度最高的记忆。")
        .integer("limit", "返回条数，默认 5，最大 50。")
    )


def angel_forget_builder() -> Builder:
    """对应 Go ``angelForgetBuilder``。"""
    return (
        Builder(ANGEL_FORGET_TOOL_NAME)
        .description(
            "删除一条来源为当前发言人的长期记忆；只在当前用户明确要求删除某条记忆时使用。"
            "第一次调用返回待删除内容，带 confirm=true 再次调用才会真正删除。"
        )
        .risk(RiskHigh)
        .tags("memory")
        .owner_scoped()
        .hidden()
        .string("id", "要删除的记忆 id 或唯一前缀，来自 angel_recall 的结果。", required())
        .boolean("confirm", "首次调用省略或传 false，只返回待删除内容；确认删除时传 true。")
    )


def _actor_id(ctx: Any, msg: Any) -> str:
    """actor 优先取认证 ctx，其次取平台消息上下文。"""
    actor, ok = actor_from_context(ctx)
    actor_id = (getattr(actor, "id", "") or "").strip() if ok else ""
    if actor_id == "":
        actor_id = (getattr(msg, "actor_id", "") or "").strip()
    return actor_id


def _decode(raw: str, kind: type, tool_name: str) -> Any:
    out = kind()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse {tool_name} arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"parse {tool_name} arguments: expected a JSON object")
    for key, value in data.items():
        if hasattr(out, key):
            setattr(out, key, value)
    return out


__all__ = [name for name in dir() if not name.startswith("_")]
