"""工具解析、风险裁决与执行的统一入口。

对应 Go：``internal/toolrun/toolrun.go``。

职责边界（与上游一致）：

- **解析**：把模型给出的名字解析成"原生工具"或"Atriwisp 工具"，并给出可用性
  与不可用原因。缓存里查得到但原生注册表已没有的，算"不再可用"。
- **风险评估 / 预检 / 风险详情**：都转发给原生工具的可选接口
  （``ConfirmationPreflightProvider`` / ``RiskDetailProvider``）。
- **执行**：原生走 ``tool.Executor``；Atriwisp 走 HTTP。执行后若配了媒体管理器，
  用 ``materialize`` 把消息段里的媒体引用物化成 LLM 能吃的形态。

**背景任务的工具白名单是安全边界**：``BackgroundToolAllowed`` 挡掉
``discover_tool`` 与 ``workspace``。上游注释写明了原因 —— 否则后台任务能靠
``discover_tool`` 重新拿到全部工具，从而绕过"只给预载工具"的约束。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.llm.llm import LLMMessage, Role, ToolCallRequest, ToolSchema, text_segments
from atri_x.security import (
    Actor,
    Policy,
    actor_from_context,
    default_policy,
    policy_from_context,
)
from atri_x.storage.models import (
    SessionModeChat,
    SessionModeWork,
    session_mode_supports_tools,
)
from atri_x.tool.executor import Executor
from atri_x.tool.sandbox import (
    background_context,
    background_path_instruction,
    info_available_in_context,
    sandbox_context_from_context,
)
from atri_x.tool.tool import (
    DiscoveryResult,
    Info,
    Registry,
    RiskAssessment,
    RiskLow,
    assess_risk,
    can_access_tool,
)
from atri_x.toolrun.atriwisp_exec import execute_atriwisp_tool
from atri_x.toolrun.availability import available_in_context, unavailable_reason
from atri_x.toolrun.cache import (
    SOURCE_KIND_ATRIWISP,
    SOURCE_KIND_NATIVE,
    CachedTool,
    sort_cached_tools,
)
from atri_x.toolrun.native import NativeSource

#: 后台任务里被禁用的工具。对应 Go ``BackgroundToolAllowed`` 的 switch。
BACKGROUND_DISALLOWED_TOOLS = frozenset({"discover_tool", "workspace"})

#: 后台需要追加路径说明的工具。对应 Go ``backgroundPathSchema``。
BACKGROUND_PATH_TOOLS = frozenset({"shell", "read_file", "edit_file", "send_file"})


@dataclass
class Context:
    """一次工具视图的上下文。对应 Go ``Context``。

    ``scope`` 按鸭子类型（对应 ``session.Scope``），只需要 ``actor_id`` / ``platform``。
    """

    mode: str = ""
    session: Any = None
    scope: Any = None
    actor: Actor = field(default_factory=Actor)
    disable_base_tools: bool = False


@dataclass
class ResolvedTool:
    """解析结果。对应 Go ``ResolvedTool``。"""

    name: str = ""
    source: str = SOURCE_KIND_NATIVE
    native: Any = None
    cached: CachedTool | None = None
    available: bool = False
    reason: str = ""


@dataclass
class ExecutionResult:
    """一次工具调用的结果。对应 Go ``ExecutionResult``。"""

    call: ToolCallRequest = field(default_factory=ToolCallRequest)
    message: LLMMessage = field(default_factory=LLMMessage)
    result: Any = None
    err: BaseException | None = None


class Manager:
    """对应 Go ``Manager``。"""

    def __init__(
        self,
        registry: Registry | None = None,
        policy: Policy | None = None,
        media: Any = None,
    ) -> None:
        self.native = NativeSource(registry)
        self.policy = policy
        self.media = media

    # -- 视图 ---------------------------------------------------------------
    async def tool_infos(self, ctx: Any, view: Context) -> list[Info]:
        """对应 Go ``ToolInfos``。"""
        _raise_if_cancelled(ctx)
        if (
            view.disable_base_tools
            or not session_mode_supports_tools(view.mode)
            or self.native is None
        ):
            return []
        return self.native.tool_infos(ctx, actor_for_view(ctx, view), policy_for_manager(ctx, self.policy))

    async def tool_names(self, ctx: Any, view: Context) -> list[str]:
        """对应 Go ``ToolNames``。"""
        return [info.name for info in await self.tool_infos(ctx, view)]

    async def base_schemas(self, ctx: Any, view: Context) -> list[ToolSchema]:
        """对应 Go ``BaseSchemas``。"""
        _raise_if_cancelled(ctx)
        if (
            view.disable_base_tools
            or not session_mode_supports_tools(view.mode)
            or self.native is None
        ):
            return []
        return self.native.base_schemas(ctx)

    async def schemas(
        self, ctx: Any, view: Context, cached: list[CachedTool]
    ) -> list[ToolSchema]:
        """基础 schema + 缓存工具 schema（去重，背景白名单过滤）。对应 Go ``Schemas``。"""
        base = await self.base_schemas(ctx, view)
        out: list[ToolSchema] = []
        seen: set[str] = set()

        def append_schema(schema: ToolSchema) -> None:
            name = schema.function.name
            if not name or name in seen:
                return
            seen.add(name)
            out.append(schema_for_context(ctx, schema))

        for schema in base:
            append_schema(schema)
        for cached_tool in cached:
            if view.disable_base_tools and not background_tool_allowed(cached_tool.name):
                continue
            if not cached_tool_available(ctx, cached_tool):
                continue
            append_schema(cached_tool.schema)
        return out

    # -- 解析 ---------------------------------------------------------------
    def resolve(self, ctx: Any, name: str, cached: list[CachedTool]) -> ResolvedTool:
        """对应 Go ``Resolve``。"""
        name = (name or "").strip()
        if not name:
            return ResolvedTool(name=name, available=False, reason="tool name is empty")
        background = background_context(ctx)
        for cached_tool in cached:
            if cached_tool.name != name and cached_tool.canonical_name != name:
                continue
            if background and not background_tool_allowed(cached_tool.name):
                return ResolvedTool(
                    name=name,
                    source=cached_tool.source,
                    cached=cached_tool,
                    available=False,
                    reason="tool is not allowed in this background task",
                )
            if not cached_tool_available(ctx, cached_tool):
                return ResolvedTool(
                    name=name,
                    source=cached_tool.source,
                    cached=cached_tool,
                    available=False,
                    reason="tool is unavailable in this context",
                )
            source = cached_tool.source or SOURCE_KIND_NATIVE
            if source == SOURCE_KIND_ATRIWISP:
                if not cached_tool.endpoint:
                    return ResolvedTool(
                        name=name,
                        source=source,
                        cached=cached_tool,
                        available=False,
                        reason="Atriwisp tool endpoint is missing",
                    )
                return ResolvedTool(
                    name=cached_tool.canonical_name,
                    source=source,
                    cached=cached_tool,
                    available=True,
                )
            tool, ok = self.native.get(cached_tool.name)
            if ok and tool is not None:
                return ResolvedTool(
                    name=cached_tool.name,
                    source=SOURCE_KIND_NATIVE,
                    native=tool,
                    cached=cached_tool,
                    available=True,
                )
            return ResolvedTool(
                name=cached_tool.name,
                source=SOURCE_KIND_NATIVE,
                cached=cached_tool,
                available=False,
                reason="native tool is no longer available",
            )

        tool, ok = self.native.get(name)
        if ok and tool is not None:
            if background:
                # 后台任务不得触达它从未预载的工具 —— discover_tool 就是这样
                # 绕过后台白名单的。
                return ResolvedTool(
                    name=name,
                    source=SOURCE_KIND_NATIVE,
                    native=tool,
                    available=False,
                    reason="tool is not allowed in this background task",
                )
            info = tool.info()
            if not info_available_in_context(ctx, info):
                return ResolvedTool(
                    name=name,
                    source=SOURCE_KIND_NATIVE,
                    native=tool,
                    available=False,
                    reason=unavailable_reason(ctx, info),
                )
            return ResolvedTool(
                name=name, source=SOURCE_KIND_NATIVE, native=tool, available=True
            )
        return ResolvedTool(name=name, available=False, reason=f'tool "{name}" not found')

    Resolve = resolve

    # -- 风险 ---------------------------------------------------------------
    async def assess_risk(
        self, ctx: Any, resolved: ResolvedTool, arguments: str
    ) -> RiskAssessment:
        """对应 Go ``AssessRisk``。

        Atriwisp 工具与不可用工具一律按最低风险处理（上游如此）：它们不经过
        原生注册表的风险声明，抬高等级只会挡住合法调用。
        """
        if resolved.source == SOURCE_KIND_ATRIWISP:
            return RiskAssessment(level=RiskLow)
        if not resolved.available or resolved.native is None:
            return RiskAssessment(level=RiskLow)
        return await assess_risk(ctx, resolved.native, _call_request(resolved.name, "", arguments))

    async def preflight_confirmation(
        self, ctx: Any, resolved: ResolvedTool, call: ToolCallRequest
    ) -> None:
        """对应 Go ``PreflightConfirmation``。"""
        if resolved.source == SOURCE_KIND_ATRIWISP or not resolved.available:
            return
        if resolved.native is None:
            return
        provider = getattr(resolved.native, "preflight_confirmation", None)
        if not callable(provider):
            return
        result = provider(ctx, _call_request(resolved.name, call.id, call.arguments))
        if hasattr(result, "__await__"):
            await result

    async def risk_detail(
        self, ctx: Any, resolved: ResolvedTool, call: ToolCallRequest
    ) -> str:
        """对应 Go ``RiskDetail``（出错返回空串，不向上传播）。"""
        if resolved.source == SOURCE_KIND_ATRIWISP or not resolved.available:
            return ""
        if resolved.native is None:
            return ""
        provider = getattr(resolved.native, "risk_detail", None)
        if not callable(provider):
            return ""
        try:
            detail = provider(ctx, _call_request(resolved.name, call.id, call.arguments))
            if hasattr(detail, "__await__"):
                detail = await detail
        except Exception:  # noqa: BLE001 - 风险详情失败不应阻断调用
            return ""
        return (detail or "").strip()

    # -- 执行 ---------------------------------------------------------------
    async def execute(
        self, ctx: Any, call: ToolCallRequest, resolved: ResolvedTool, actor: Actor
    ) -> ExecutionResult:
        """对应 Go ``Execute``。"""
        message = LLMMessage(role=Role.TOOL, name=call.name, tool_call_id=call.id)
        if not resolved.available:
            return _execution_error(call, message, ValueError(resolved.reason))
        if resolved.source == SOURCE_KIND_ATRIWISP:
            msg, result, err = execute_atriwisp_tool(ctx, call, resolved)
            return ExecutionResult(call=call, message=msg, result=result, err=err)

        registry = self.native.registry if self.native is not None else None
        executor = Executor(
            registry=registry, actor=actor, policy=policy_for_manager(ctx, self.policy)
        )
        inner = await executor.execute(ctx, call)
        result = ExecutionResult(
            call=inner.call, message=inner.message, result=inner.result, err=inner.err
        )
        if result.result is not None and self.media is not None:
            materialize = getattr(self.media, "materialize", None)
            if callable(materialize):
                materialized = materialize(ctx, result.message.segments)
                if hasattr(materialized, "__await__"):
                    materialized = await materialized
                result.message.segments = materialized
        result.call = call
        return result

    Execute = execute


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def background_tool_allowed(name: str) -> bool:
    """该工具是否允许在后台任务里运行。对应 Go ``BackgroundToolAllowed``。"""
    return (name or "").strip().lower() not in BACKGROUND_DISALLOWED_TOOLS


BackgroundToolAllowed = background_tool_allowed


def schema_for_context(ctx: Any, schema: ToolSchema) -> ToolSchema:
    """后台上下文里给路径类工具追加说明。对应 Go ``schemaForContext``。"""
    sandbox, ok = sandbox_context_from_context(ctx)
    if not ok or not getattr(sandbox, "background", False):
        return schema
    if schema.function.name not in BACKGROUND_PATH_TOOLS:
        return schema
    out = dataclasses.replace(
        schema,
        function=dataclasses.replace(
            schema.function,
            description=(
                (schema.function.description or "").strip()
                + " "
                + background_path_instruction()
            ).strip(),
        ),
    )
    return out


def cached_tool_available(ctx: Any, cached: CachedTool) -> bool:
    """对应 Go ``cachedToolAvailable``。"""
    return available_in_context(ctx, Info(foreground_only=cached.foreground_only))


def native_cached_tools_from_discovery(result: DiscoveryResult | None) -> list[CachedTool]:
    """把发现结果转成缓存条目。对应 Go ``NativeCachedToolsFromDiscovery``。"""
    if result is None:
        return []
    out: list[CachedTool] = []
    for discovered in getattr(result, "tools", []) or []:
        schema = getattr(discovered, "schema", None)
        info = getattr(discovered, "info", None)
        if schema is None or info is None or not getattr(info, "name", ""):
            continue
        out.append(
            CachedTool(
                name=info.name,
                source=SOURCE_KIND_NATIVE,
                description=getattr(info, "description", ""),
                schema=schema,
                foreground_only=bool(getattr(info, "foreground_only", False)),
            )
        )
    return out


NativeCachedToolsFromDiscovery = native_cached_tools_from_discovery


def actor_for_view(ctx: Any, view: Context) -> Actor:
    """解析本次视图的 Actor。对应 Go ``actorForView``。"""
    if getattr(view.actor, "role", ""):
        return view.actor
    actor, ok = actor_from_context(ctx)
    if ok:
        return actor
    policy, has_policy = policy_from_context(ctx)
    if has_policy and policy is not None:
        scope = view.scope
        actor_id = getattr(scope, "actor_id", "") or ""
        platform = getattr(scope, "platform", "") or ""
        return policy.actor(actor_id, platform, actor_id, "")
    scope = view.scope
    actor_id = getattr(scope, "actor_id", "") or ""
    platform = getattr(scope, "platform", "") or ""
    from atri_x.security import RoleUser

    return Actor(
        id=actor_id, platform=platform, platform_user_id=actor_id, role=RoleUser
    )


def policy_for_manager(ctx: Any, configured: Policy | None) -> Policy:
    """对应 Go ``policyForManager``。"""
    if configured is not None:
        return configured
    policy, ok = policy_from_context(ctx)
    if ok and policy is not None:
        return policy
    return default_policy()


def sort_cached_tools_alias(tools: list[CachedTool]) -> list[CachedTool]:
    """``SortCachedTools`` 的再导出（Go 里它在本文件）。"""
    return sort_cached_tools(tools)


SortCachedTools = sort_cached_tools_alias


def _call_request(name: str, ident: str, arguments: str) -> Any:
    from atri_x.tool.tool import CallRequest

    return CallRequest(id=ident, name=name, arguments=arguments)


def _execution_error(
    call: ToolCallRequest, message: LLMMessage, err: BaseException
) -> ExecutionResult:
    """对应 Go ``executionError``。"""
    message.segments = text_segments(f"tool call {call.name} failed: {err}")
    return ExecutionResult(call=call, message=message, err=err)


def _raise_if_cancelled(ctx: Any) -> None:
    """尽力而为地识别取消（对应 Go 的 ``ctx.Err()`` 早退）。"""
    if ctx is None:
        return
    checker = getattr(ctx, "is_cancelled", None)
    if callable(checker) and checker():
        error = getattr(ctx, "error", None)
        raise (error() if callable(error) and error() is not None else RuntimeError("cancelled"))


__all__ = [name for name in dir() if not name.startswith("_")]
