"""``agent.services["tools"]`` / ``command_deps.tools`` 的装配对象。

对应 Go：``internal/agent/tools.go`` 的 ``toolRuntimeState`` 能力集合 —— 本模块把本组
负责的工具链路方法聚成一个对象，供 app 层注册：

* ``atri_x.agent.seams.AgentSeams`` 按 ``services["tools"]`` 取
  ``cached_tools_for_session`` / ``tool_run_manager``；
* ``atri_x.agent.commands.deps.ToolService`` 需要 ``list`` / ``unregister`` /
  ``remove`` / ``reload``；
* 回合落库与发现缓存分别走 ``persist_turn_message(s)`` / ``remember_*``。

``agent-core`` 的 ``Agent`` 只继承 ``AgentSeams``（不继承本组 mixin），因此**建议**
app 层构造一次 :func:`new_tools_service` 并塞进 ``agent.services["tools"]``。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.tool_cache import ToolCacheMixin
from atri_x.agent.tool_transcript import ToolTranscriptMixin
from atri_x.agent.toolrun_adapter import AgentToolRunDeps, cached_tools_for_session, tool_run_manager
from atri_x.agent.tools import AgentService, ToolRuntimeMixin

__all__ = ["AgentToolsService", "new_tools_service"]


class AgentToolsService(
    AgentService,
    ToolRuntimeMixin,
    ToolCacheMixin,
    ToolTranscriptMixin,
):
    """工具运行时 + 发现缓存 + 回合落库的聚合服务。"""

    # -- seams 需要的两个入口 ----------------------------------------------
    def cached_tools_for_session(self, session: Any) -> list[Any]:
        from atri_x.agent.tools import service_host

        return cached_tools_for_session(service_host(self), session)

    def tool_run_manager(self) -> Any:
        from atri_x.agent.tools import service_host

        return tool_run_manager(service_host(self))

    def tool_run_deps(self, output: Any = None) -> AgentToolRunDeps:
        """构造一次批量工具运行所需的 RunnerDeps。"""
        from atri_x.agent.tools import service_host

        return AgentToolRunDeps(agent=service_host(self), output=output)

    # -- 命令侧的名称兼容 ---------------------------------------------------
    def remove_tool(self, ctx: Any, name: str) -> Any:
        return self.remove(ctx, name)


def new_tools_service(agent: Any) -> AgentToolsService:
    """建一个绑定到 ``agent`` 的工具服务对象。"""
    return AgentToolsService(agent)
