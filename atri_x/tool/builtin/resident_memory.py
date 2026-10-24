"""常驻记忆工具族：入口 + 读取 + 普通记忆 + 核心记忆。

对应 Go：``internal/tool/builtin/resident_memory.go``。

与 Go 的差异（仅形式）：

* ``store.Read`` / ``WriteCore`` / ``WriteNormal`` / ``AppendNormal`` /
  ``DeleteNormal`` 在本仓是**同步**方法（Go 的 ``*resident.Store`` 也是同步的），
  工具侧仍按 ``async def`` 暴露以符合 ``Tool`` 协议。
* Go 用 ``json.Marshal`` 输出读取结果；Python 用 ``json.dumps(..., separators)`` 复刻
  ``omitempty`` 与键名。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import ToolSchema
from atri_x.memory.resident import (
    DEFAULT_CORE_MAX_UNITS,
    DEFAULT_NORMAL_MAX_UNITS,
    Limits,
    Memory,
    NormalWritePolicy,
    Store,
    actor_scope,
)
from atri_x.platform import message_context_from
from atri_x.security import actor_from_context
from atri_x.tool.builder import Builder, required
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskLow

RESIDENT_MEMORY_TOOL_NAME = "resident_memory"
RESIDENT_MEMORY_READ_TOOL_NAME = "resident_memory_read"
RESIDENT_MEMORY_NORMAL_TOOL_NAME = "resident_memory_normal"
RESIDENT_MEMORY_CORE_TOOL_NAME = "resident_memory_core"
RESIDENT_MEMORY_WRITING_RULE = (
    "内容必须使用第三人称：当前用户称为“用户”，模型自身称为“assistant”；"
    "只记录背景信息，不记录对模型的指令"
)


@dataclass
class MemoryReadArgs:
    """对应 Go ``memoryReadArgs``。"""

    section: str = ""


@dataclass
class MemoryNormalArgs:
    """对应 Go ``memoryNormalArgs``。"""

    action: str = ""
    content: str | None = None


@dataclass
class MemoryCoreArgs:
    """对应 Go ``memoryCoreArgs``。"""

    content: str | None = None


@dataclass
class MemoryReadResult:
    """对应 Go ``memoryReadResult``（``omitempty`` 在 :meth:`to_payload` 里复刻）。"""

    core: str = ""
    normal: str = ""
    limits: Limits = field(default_factory=Limits)
    status: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.core:
            payload["core"] = self.core
        if self.normal:
            payload["normal"] = self.normal
        payload["limits"] = {
            "core": int(getattr(self.limits, "core", 0)),
            "normal": int(getattr(self.limits, "normal", 0)),
        }
        payload["status"] = self.status
        return payload


class ResidentMemoryTool:
    """入口工具。对应 Go ``ResidentMemoryTool``。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return RESIDENT_MEMORY_TOOL_NAME

    def info(self) -> Info:
        return (
            Builder(self.name())
            .description(
                "管理当前用户在当前平台的常驻记忆入口。先用 resident_memory_read 查看；"
                "普通记忆用 resident_memory_normal（append/write/delete，低风险），"
                "核心记忆用 resident_memory_core（整体覆盖，高风险需用户确认）。"
            )
            .risk(RiskLow)
            .depends_on(
                RESIDENT_MEMORY_READ_TOOL_NAME,
                RESIDENT_MEMORY_NORMAL_TOOL_NAME,
                RESIDENT_MEMORY_CORE_TOOL_NAME,
            )
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            Builder(self.name()).description(self.info().description).build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        validate_memory_store(self.store)
        return Result(
            content=(
                "resident_memory 是入口，本身不读写记忆。请先调用 resident_memory_read "
                "查看当前记忆，然后按需调用：\n"
                "- resident_memory_normal：append/write/delete 普通记忆（低风险，可自动执行）；\n"
                "- resident_memory_core：整体覆盖核心记忆（高风险，需用户确认）。"
            )
        )


class ResidentMemoryReadTool:
    """读取工具（hidden）。对应 Go ``ResidentMemoryReadTool``。"""

    _DESCRIPTION = "读取当前用户在当前平台的常驻记忆。section 可为 all、core 或 normal，默认 all。"

    def __init__(self, store: Store | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return RESIDENT_MEMORY_READ_TOOL_NAME

    def info(self) -> Info:
        return (
            memory_builder(self.name(), self._DESCRIPTION, RiskLow)
            .hidden()
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            memory_builder(self.name(), self._DESCRIPTION, RiskLow)
            .string("section", "读取范围：all、core 或 normal。默认 all。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        scope = note_scope(ctx, self.store)
        args = decode_memory_args(req.arguments, MemoryReadArgs)
        assert self.store is not None  # note_scope 已校验
        section = (args.section or "").strip().lower() or "all"
        if section not in ("all", "core", "normal"):
            raise InvalidError("section must be all, core or normal")
        try:
            memory = self.store.read_for_prompt(scope)
        except NotFoundError:
            memory = Memory()
        result = MemoryReadResult(
            limits=self.store.limits_or_default(), status="ok"
        )
        if section in ("all", "core"):
            result.core = memory.core
        if section in ("all", "normal"):
            result.normal = memory.normal
        return memory_json_result(result)


class ResidentMemoryNormalTool:
    """普通记忆工具（hidden）。对应 Go ``ResidentMemoryNormalTool``。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return RESIDENT_MEMORY_NORMAL_TOOL_NAME

    def info(self) -> Info:
        return (
            memory_builder(self.name(), normal_description(self.store), RiskLow)
            .hidden()
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            memory_builder(self.name(), normal_description(self.store), RiskLow)
            .string("action", "操作类型：append、write 或 delete。", required())
            .string(
                "content",
                "append/write 时使用。每条一行、一行一件事，重复条目会去重。"
                "write 只覆盖当前会话的条目（其它会话的不动），使用前必须先调用 "
                "resident_memory_read 读取 normal 或 all。delete 不需要 content。",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        scope = note_scope(ctx, self.store)
        args = decode_memory_args(req.arguments, MemoryNormalArgs)
        assert self.store is not None
        action = (args.action or "").strip().lower()
        if action in ("append", "add"):
            if args.content is None or args.content.strip() == "":
                raise InvalidError("normal content is required")
            self.store.append_note(scope, args.content)
            return Result(content="已追加普通常驻记忆。")
        if action in ("write", "set", "update"):
            if args.content is None:
                raise InvalidError("normal content is required")
            self.store.write_notes(scope, args.content)
            return Result(content="已更新普通常驻记忆（只覆盖当前会话的条目）。")
        if action in ("delete", "clear"):
            self.store.delete_notes(scope)
            return Result(content="已清空当前会话的普通常驻记忆。")
        raise InvalidError("action must be append, write or delete")


class ResidentMemoryCoreTool:
    """核心记忆工具（hidden，高风险）。对应 Go ``ResidentMemoryCoreTool``。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store

    def name(self) -> str:
        return RESIDENT_MEMORY_CORE_TOOL_NAME

    def info(self) -> Info:
        return (
            memory_builder(self.name(), core_description(self.store), RiskHigh)
            .hidden()
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            memory_builder(self.name(), core_description(self.store), RiskHigh)
            .string("content", "完整 core 内容。允许空字符串，表示清空 core。", required())
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        scope = memory_scope(ctx, self.store)
        args = decode_memory_args(req.arguments, MemoryCoreArgs)
        if args.content is None:
            raise InvalidError("core content is required")
        assert self.store is not None
        self.store.write_core(scope, args.content)
        return Result(content="已更新核心常驻记忆。")


def new_resident_memory_tools(store: Store | None) -> list[Any]:
    """对应 Go ``NewResidentMemoryTools``。"""
    return [
        ResidentMemoryTool(store),
        ResidentMemoryReadTool(store),
        ResidentMemoryNormalTool(store),
        ResidentMemoryCoreTool(store),
    ]


def new_resident_memory_tool(store: Store | None) -> ResidentMemoryTool:
    """对应 Go ``NewResidentMemoryTool``。"""
    return ResidentMemoryTool(store)


def memory_builder(name: str, description: str, risk: str) -> Builder:
    """对应 Go ``memoryBuilder``。"""
    return Builder(name).description(description).risk(risk).owner_scoped()


def normal_description(store: Store | None) -> str:
    """对应 Go ``normalDescription``：把上限与逐条约束写进描述。"""
    limits = memory_limits(store)
    policy = memory_normal_policy(store)
    constraints = f"normal 上限 {limits.normal} 字数或单词"
    if getattr(policy, "max_lines", 0) > 0:
        constraints += f"，最多 {policy.max_lines} 条"
    if getattr(policy, "max_units_per_entry", 0) > 0:
        constraints += f"，单条最多 {policy.max_units_per_entry} 字数或单词"
    return (
        "修改当前用户在当前平台的普通常驻记忆。支持 append、write、delete。"
        "每条一行、一行一件事，重复条目会去重；记忆按会话隔离："
        "write 只覆盖**当前会话**的条目（不会动私聊或其它群写下的条目），"
        "append 追加到当前会话，delete 清空当前会话的条目。"
        f"使用前必须先调用 resident_memory_read。{constraints}。"
        f"{RESIDENT_MEMORY_WRITING_RULE}"
    )


def core_description(store: Store | None) -> str:
    """对应 Go ``coreDescription``。"""
    limits = memory_limits(store)
    return (
        "覆盖当前用户在当前平台的核心常驻记忆。仅当用户明确要求修改核心记忆时使用；"
        "必须先调用 resident_memory_read 读取完整 core，再写入完整 core。"
        f"允许传空字符串清空 core。core 上限 {limits.core} 字数或单词。"
        f"{RESIDENT_MEMORY_WRITING_RULE}"
    )


def memory_normal_policy(store: Store | None) -> NormalWritePolicy:
    """对应 Go ``memoryNormalPolicy``。"""
    if store is None:
        return NormalWritePolicy()
    return getattr(store, "normal_policy", NormalWritePolicy())


def memory_limits(store: Store | None) -> Limits:
    """对应 Go ``memoryLimits``。"""
    if store is None:
        return Limits(core=DEFAULT_CORE_MAX_UNITS, normal=DEFAULT_NORMAL_MAX_UNITS)
    return store.limits_or_default()


def validate_memory_store(store: Store | None) -> None:
    """对应 Go ``validateMemoryStore``。"""
    if store is None:
        raise InvalidError("resident memory store is not configured")


def memory_scope(ctx: Any, store: Store | None) -> Any:
    """对应 Go ``memoryScope``：身份只来自认证过的 ctx。"""
    validate_memory_store(store)
    actor, ok = _actor_from_context(ctx)
    if not ok:
        raise InvalidError("resident memory actor is not available")
    return actor_scope(actor)


def note_scope(ctx: Any, store: Store | None) -> Any:
    """记忆条目的作用域：身份 + 来源会话（``resident_memory_normal`` 用）。

    会话 id 取自平台消息上下文。拿不到时留空 —— 空来源按 ``identity`` 档处理
    （同平台任意会话可见），不会被当成私聊来源挡在群外。
    """
    scope = memory_scope(ctx, store)
    msg, ok = message_context_from(ctx)
    if ok and (msg.scope_id or "").strip():
        scope.platform_scope_id = msg.scope_id.strip()
    return scope


def _actor_from_context(ctx: Any) -> tuple[Any, bool]:
    """``atri_x.security.actor_from_context`` 返回 ``(Actor, ok)``，与 Go 一致。"""
    actor, ok = actor_from_context(ctx)
    if not ok:
        return None, False
    return actor, True


def decode_memory_args(raw: str, kind: type) -> Any:
    """对应 Go ``decodeMemoryArgs``。"""
    out = kind()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse resident memory arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse resident memory arguments: expected a JSON object")
    for key, value in data.items():
        if not hasattr(out, key):
            continue
        setattr(out, key, value)
    return out


def memory_json_result(value: Any) -> Result:
    """对应 Go ``memoryJSONResult``（紧凑 JSON，键名与 Go 一致）。"""
    payload = value.to_payload() if hasattr(value, "to_payload") else value
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return Result(content=text)


__all__ = [name for name in dir() if not name.startswith("_")]
