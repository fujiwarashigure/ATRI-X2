"""Tool Runtime 的执行适配：风险裁决、策略准入与结果回灌消息。

对应 Go：``internal/tool/executor.go``。

与 Go 的差异（仅形式）：Go 的 ``Execute`` 返回 ``ExecutionResult``（内含 ``Err``），
而不是 ``(T, error)``——调用方需要那条"失败也要回灌给 LLM"的消息，因此 Python
同样返回结构体并保留 ``err`` 字段，而不是抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import LLMMessage, Role, ToolCallRequest, text_segments
from atri_x.tool.tool import (
    Actor,
    CallRequest,
    DefaultPolicy,
    Policy,
    Registry,
    Result,
    RoleSuperadmin,
    RoleUser,
    assess_risk,
)


@dataclass
class ExecutionResult:
    """对应 Go ``ExecutionResult``。``err`` 非空表示这次调用失败。"""

    call: ToolCallRequest = field(default_factory=ToolCallRequest)
    message: LLMMessage = field(default_factory=LLMMessage)
    result: Result | None = None
    err: BaseException | None = None


@dataclass
class Executor:
    """一次工具调用的执行器。对应 Go ``Executor``。"""

    registry: Registry | None = None
    actor: Actor | None = None
    policy: Policy | None = None

    async def execute(self, ctx: Any, call: ToolCallRequest) -> ExecutionResult:
        """对应 Go ``Executor.Execute``。"""
        message = LLMMessage(
            role=Role.TOOL,
            name=call.name,
            tool_call_id=call.id,
        )
        if self.registry is None:
            return _execution_error(
                call, message, InvalidError("tool registry is not configured")
            )
        tool, ok = self.registry.get(call.name)
        if not ok or tool is None:
            return _execution_error(
                call, message, InvalidError(f"tool {call.name!r} not found")
            )
        request = CallRequest(id=call.id, name=call.name, arguments=call.arguments)
        try:
            assessment = await assess_risk(ctx, tool, request)
        except BaseException as exc:
            return _execution_error(call, message, InvalidError(f"assess risk: {exc}"))
        policy = self.policy
        if policy is None:
            policy = DefaultPolicy()
        actor = self.actor
        if actor is None or not getattr(actor, "role", ""):
            actor = Actor(role=RoleUser)
        info = tool.info()
        if info.superadmin_only and getattr(actor, "role", None) != RoleSuperadmin:
            return _execution_error(
                call,
                message,
                InvalidError(
                    f"tool {call.name!r} requires superadmin role; ask an administrator "
                    "to add you to security.superadmins"
                ),
            )
        if not policy.can_use_tool(actor, assessment.level, bool(info.owner_scoped)):
            return _execution_error(
                call,
                message,
                InvalidError(
                    f"risk {assessment.level} is above your allowed tool level; ask an "
                    "administrator to raise security.user_max_tool_risk or add you to "
                    "security.superadmins"
                ),
            )
        try:
            result = await tool.call(ctx, request)
        except BaseException as exc:
            return _execution_error(call, message, exc)
        if result is None:
            result = Result()
        # 只把 content/segments 回灌给 LLM；result.data 留给 Agent 内部结构化消费，
        # 避免 discover schema、内部状态或大对象污染上下文。
        message.segments = result.llm_segments()
        return ExecutionResult(call=call, message=message, result=result)


def _execution_error(
    call: ToolCallRequest, message: LLMMessage, err: BaseException
) -> ExecutionResult:
    """对应 Go ``executionError``。"""
    message.segments = text_segments(f"tool call {call.name} failed: {err}")
    return ExecutionResult(call=call, message=message, err=err)


__all__ = [name for name in dir() if not name.startswith("_")]
