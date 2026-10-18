"""``Agent`` 的薄转发方法（唤醒 / 消息 / 聊天 / 模型 / 工具 / 完成）。

对应 Go：``internal/agent/core.go`` 中同名方法。

这些方法只做一件事：把调用转给主题模块。为了不让 :mod:`atri_x.agent.core`
越过 PORTING.md 的单文件行数上限，它们在这里以 mixin 形式定义，
由 ``Agent`` 继承（``class Agent(CoreForwardMixin, AgentSeams)``）。
所有主题模块都在方法体内惰性导入，因此本模块没有包内模块级依赖，不会引入环。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供类型注解
    from atri_x.hook import Event
    from atri_x.storage import Session

__all__ = ["CoreForwardMixin"]


def _topic(module_name: str) -> Any:
    """取主题子模块（``atri_x.agent.<module_name>``）。

    从 :data:`sys.modules` 里取已经绑定的模块对象；没绑定时按需导入。这样在包初始化
    的过程中（``atri_x.agent.__init__`` → ``core`` → 本模块）也不会因为
    "包属性尚未绑定" 而拿到 ``None``。
    """
    import importlib

    full = "atri_x.agent." + module_name
    module = sys.modules.get(full)
    if module is not None:
        return module
    package = sys.modules.get("atri_x.agent")
    module = getattr(package, module_name, None) if package is not None else None
    if module is not None:
        return module
    return importlib.import_module(full)


class CoreForwardMixin:
    """唤醒 / 消息 / 聊天 / 模型调用 / 工具批次的转发方法。"""

    # -- 唤醒 ---------------------------------------------------------------

    def hook_wakeup(self, ctx: Any, event: Event) -> bool:
        from atri_x.agent.wakeup import hook_wakeup

        return hook_wakeup(self, ctx, event)

    def message_wakeup(self, ctx: Any, text: str) -> bool:
        from atri_x.agent.wakeup import message_wakeup

        return message_wakeup(self, ctx, text)

    def strip_wakeup_prefix(self, ctx: Any, text: str) -> str:
        from atri_x.agent.wakeup import strip_wakeup_prefix

        return strip_wakeup_prefix(self, ctx, text)

    # -- 消息 ---------------------------------------------------------------

    async def handle_message(self, ctx: Any, text: str) -> None:
        await _topic("message").handle_message(self, ctx, text)

    def command_infos(self) -> list[Any]:
        return _topic("message").command_infos(self)

    # -- 聊天 ---------------------------------------------------------------

    async def handle_chat(self, ctx: Any, text: str) -> None:
        from atri_x.agent.chat import handle_chat

        await handle_chat(self, ctx, text)

    async def start_chat(self, ctx: Any, session: Session, text: str) -> None:
        from atri_x.agent.chat import start_chat

        await start_chat(self, ctx, session, text)

    async def start_background_chat(self, ctx: Any, session: Session, text: str) -> None:
        from atri_x.agent.chat import start_background_chat

        await start_background_chat(self, ctx, session, text)

    async def start_chat_with_output(
        self, ctx: Any, session: Session, text: str, out: Any
    ) -> None:
        from atri_x.agent.chat import start_chat_with_output

        await start_chat_with_output(self, ctx, session, text, out)

    def model_selection_for_turn(self, ctx: Any, session: Session | None) -> Any:
        from atri_x.agent.chat import model_selection_for_turn

        return model_selection_for_turn(self, ctx, session)

    # -- 模型调用 -----------------------------------------------------------

    async def call_llm(
        self,
        ctx: Any,
        session_id: str,
        tools_enabled: bool,
        selection: Any,
        messages: list[Any],
        tools: list[Any] | None,
        pending: Any,
        request_options: Any,
        stream: Any,
        out: Any,
        *,
        allow_route: bool = False,
    ) -> Any:
        from atri_x.agent.chat_llm import call_llm

        return await call_llm(
            self,
            ctx,
            session_id,
            tools_enabled,
            selection,
            messages,
            tools,
            pending,
            request_options,
            stream,
            out,
            allow_route=allow_route,
        )

    def user_message_segments(self, ctx: Any, text: str) -> list[Any]:
        from atri_x.agent.chat_llm import user_message_segments

        return user_message_segments(ctx, text)

    def is_cli_context(self, ctx: Any) -> bool:
        from atri_x.agent.chat_llm import is_cli_context

        return is_cli_context(self, ctx)

    def should_show_cli_reasoning(self, ctx: Any) -> bool:
        from atri_x.agent.chat_llm import should_show_cli_reasoning

        return should_show_cli_reasoning(self, ctx)

    # -- 工具批次 -----------------------------------------------------------

    def max_tool_rounds_per_turn(self) -> int:
        from atri_x.agent.chat_tools import max_tool_rounds_per_turn

        return max_tool_rounds_per_turn(self)

    async def tools_for_session(self, ctx: Any, session: Session | None) -> list[Any]:
        from atri_x.agent.chat_tools import tools_for_session

        return await tools_for_session(self, ctx, session)

    async def execute_tool_calls(
        self,
        ctx: Any,
        session: Session | None,
        calls: list[Any],
        assistant_text: str,
        assistant_raw_text: str,
        out: Any,
    ) -> Any:
        from atri_x.agent.chat_tools import execute_tool_calls

        return await execute_tool_calls(
            self, ctx, session, calls, assistant_text, assistant_raw_text, out
        )

    def drain_pending_user_input(
        self, ctx: Any, session_id: str, messages: list[Any]
    ) -> tuple[list[Any], Any]:
        from atri_x.agent.chat_tools import drain_pending_user_input

        return drain_pending_user_input(self, ctx, session_id, messages)

    def tool_call_risk(self, ctx: Any, call: Any) -> str:
        from atri_x.agent.chat_tools import tool_call_risk

        return tool_call_risk(self, ctx, call)

    def record_tool_call(
        self,
        ctx: Any,
        session_id: str,
        call: Any,
        risk: str,
        started_at: Any,
        result: str,
        call_err: BaseException | None,
    ) -> None:
        from atri_x.agent.chat_tools import record_tool_call

        record_tool_call(
            self, ctx, session_id, call, risk, started_at, result, call_err
        )

    def is_session_auto_confirmed(self, session_id: str) -> bool:
        from atri_x.agent.chat_tools import is_session_auto_confirmed

        return is_session_auto_confirmed(self, session_id)

    def set_session_auto_confirmed(self, session_id: str) -> None:
        from atri_x.agent.chat_tools import set_session_auto_confirmed

        set_session_auto_confirmed(self, session_id)

    def is_tool_auto_confirmed(self, session_id: str, tool_name: str) -> bool:
        from atri_x.agent.chat_tools import is_tool_auto_confirmed

        return is_tool_auto_confirmed(self, session_id, tool_name)

    def set_tool_auto_confirmed(self, session_id: str, tool_name: str) -> None:
        from atri_x.agent.chat_tools import set_tool_auto_confirmed

        set_tool_auto_confirmed(self, session_id, tool_name)

    def with_tool_capabilities(self, ctx: Any, session: Session | None) -> Any:
        from atri_x.agent.chat_tools import with_tool_capabilities

        return with_tool_capabilities(self, ctx, session)

    def with_model_capabilities(self, ctx: Any, provider: str, model: str) -> Any:
        from atri_x.agent.chat_tools import with_model_capabilities

        return with_model_capabilities(self, ctx, provider, model)

    def audit_partial_platform_send(
        self, session_id: str, operation: str, receipt: Any, err: BaseException | None
    ) -> None:
        from atri_x.agent.chat import audit_partial_platform_send

        audit_partial_platform_send(self, session_id, operation, receipt, err)

    def complete(self, text: str) -> list[str]:
        return _topic("completion").complete(self, text)
