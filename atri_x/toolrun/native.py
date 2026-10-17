"""原生工具来源：把 ``tool.Registry`` 适配成 toolrun 的来源。

对应 Go：``internal/toolrun/native.go``。

两个过滤点要注意（上游注释里没有，但行为上是刻意的）：

- **``discover_tool`` 永远不出现在 ``tool_infos`` 里**：发现类工具本身不该被
  当成"可发现的工具"暴露给模型，否则模型会反复发现它自己。
- ``hidden`` / 上下文不可用 / 权限不允许的条目一律过滤掉。
"""

from __future__ import annotations

from typing import Any

from atri_x.llm.llm import ToolSchema
from atri_x.tool.tool import Info, Registry, can_access_tool
from atri_x.toolrun.availability import available_in_context

#: 发现类工具名。对应 Go 里的字面量 ``"discover_tool"``。
DISCOVER_TOOL_NAME = "discover_tool"


class NativeSource:
    """对应 Go ``NativeSource``。"""

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry

    def get(self, name: str) -> tuple[Any | None, bool]:
        """对应 Go ``NativeSource.Get``。"""
        if self.registry is None:
            return None, False
        return self.registry.get(name)

    def base_schemas(self, ctx: Any) -> list[ToolSchema]:
        """对应 Go ``NativeSource.BaseSchemas``。"""
        if self.registry is None:
            return []
        return list(
            self.registry.schemas_for_context(lambda info: available_in_context(ctx, info))
        )

    def tool_infos(self, ctx: Any, actor: Any, policy: Any) -> list[Info]:
        """对应 Go ``NativeSource.ToolInfos``。"""
        if self.registry is None:
            return []
        out: list[Info] = []
        for info in self.registry.list():
            if getattr(info, "name", "") == DISCOVER_TOOL_NAME:
                continue
            if getattr(info, "hidden", False):
                continue
            if not available_in_context(ctx, info):
                continue
            if not can_access_tool(actor, policy, info):
                continue
            out.append(info)
        return out

    def tool_names(self, ctx: Any, actor: Any, policy: Any) -> list[str]:
        """对应 Go ``NativeSource.ToolNames``。"""
        return [info.name for info in self.tool_infos(ctx, actor, policy)]

    # Go 的导出名
    BaseSchemas = base_schemas
    ToolInfos = tool_infos
    ToolNames = tool_names


def new_native_source(registry: Registry | None = None) -> NativeSource:
    """对应 Go ``NewNativeSource``。"""
    return NativeSource(registry)


__all__ = ["DISCOVER_TOOL_NAME", "NativeSource", "new_native_source"]
