"""默认的工具 schema / 名单提供者（Agent 侧适配）。对应 Go：``internal/agent/toolrun_prompt_provider.go``。

职责：``toolRunPromptProvider`` —— 把 ``toolrun.Manager`` 投影成
``ToolSchemaProvider``（``Schemas`` / ``ToolNames``），供系统提示词与模型请求使用。

与 Go 的差异（形式，不是简化）：

* ``atri_x.tool.provider.SchemaProvider`` 的 ``schemas`` / ``tool_names`` 是**同步**的，
  而 ``atri_x.toolrun.Manager`` 的 ``base_schemas`` / ``tool_infos`` 是 async（Python 侧
  它们要读 ctx 与注册表）。因此本 Provider 的对应方法是 ``async``：
  调用方（agent-core 的提示词/请求装配）需要 ``await``。
* ``DisableBaseTools`` = ``isBackgroundSession(session)``：后台任务不注入基础工具。
"""

from __future__ import annotations

from typing import Any

from atri_x.llm import ToolSchema
from atri_x.tool.provider import PromptNames, prompt_names_from_infos
from atri_x.toolrun import Manager
from atri_x.toolrun.toolrun import Context

from atri_x.agent.tools import is_background_session

__all__ = [
    "ToolRunPromptProvider",
    "new_tool_run_prompt_provider",
]


class ToolRunPromptProvider:
    """对应 Go ``toolRunPromptProvider``。"""

    def __init__(self, agent: Any = None) -> None:
        self.agent = agent

    # -- 视图 ---------------------------------------------------------------
    def _manager(self) -> Manager:
        from atri_x.agent.toolrun_adapter import tool_run_manager

        return tool_run_manager(self.agent)

    def _view(self, ctx: Any, mode: str, session: Any, scope: Any) -> Context:
        return Context(
            mode=mode,
            session=session,
            scope=scope,
            actor=_actor(self.agent, ctx),
            disable_base_tools=is_background_session(session),
        )

    # -- SchemaProvider -----------------------------------------------------
    async def schemas(
        self, ctx: Any, mode: str, session: Any = None, scope: Any = None
    ) -> list[ToolSchema]:
        """对应 Go ``Schemas``。"""
        if self.agent is None or session is None:
            return []
        return await self._manager().base_schemas(ctx, self._view(ctx, mode, session, scope))

    async def tool_names(
        self, ctx: Any, mode: str, session: Any = None, scope: Any = None
    ) -> PromptNames:
        """对应 Go ``ToolNames``。"""
        if self.agent is None or session is None:
            return PromptNames()
        infos = await self._manager().tool_infos(ctx, self._view(ctx, mode, session, scope))
        return prompt_names_from_infos(infos)


def new_tool_run_prompt_provider(agent: Any) -> ToolRunPromptProvider:
    """对应 Go ``toolRunPromptProvider{agent: a}``。"""
    return ToolRunPromptProvider(agent)


def _actor(agent: Any, ctx: Any) -> Any:
    """取当前 actor：优先 agent.actor(ctx)，其次 agent.actor_identity，最后匿名 Actor。"""
    if agent is not None:
        helper = getattr(agent, "actor", None)
        if helper is not None and ctx is not None:
            return helper(ctx)
        identity = getattr(agent, "actor_identity", None)
        if identity is not None:
            return identity
    from atri_x.security import Actor

    return Actor()
