"""工具解析、风险裁决、执行与批量编排。

对应 Go：``internal/toolrun``。

模块划分：

- :mod:`atri_x.toolrun.toolrun` —— ``Manager``：解析 / 风险 / 预检 / 执行
- :mod:`atri_x.toolrun.runner` —— 一个 assistant 回合里整批工具调用的编排
- :mod:`atri_x.toolrun.native` —— 把 ``tool.Registry`` 适配成工具来源
- :mod:`atri_x.toolrun.cache` —— ``CachedTool`` 的编解码、去重与合并
- :mod:`atri_x.toolrun.atriwisp` —— Atriwisp 注入的声明 → 缓存条目
- :mod:`atri_x.toolrun.atriwisp_exec` —— Atriwisp 工具的 HTTP 执行
- :mod:`atri_x.toolrun.availability` —— 上下文可用性与不可用原因
"""

from __future__ import annotations

from atri_x.toolrun.atriwisp import (
    AtriwispInjection,
    AtriwispToolDeclaration,
    atriwisp_tool_name,
    cached_tools_from_atriwisp,
)
from atri_x.toolrun.atriwisp_exec import (
    DEFAULT_ATRIWISP_TIMEOUT_SECONDS,
    execute_atriwisp_tool,
    first_atriwisp_result,
)
from atri_x.toolrun.availability import (
    available_in_context,
    unavailable_reason,
)
from atri_x.toolrun.cache import (
    SOURCE_KIND_ATRIWISP,
    SOURCE_KIND_NATIVE,
    Cache,
    CachedTool,
    decode_cache,
    encode_cache,
    merge_cached_tools,
    normalize_cached_tools,
    sort_cached_tools,
)
from atri_x.toolrun.native import NativeSource, new_native_source
from atri_x.toolrun.runner import (
    MAX_PREVIEW_RUNES,
    ConfirmResult,
    RunRequest,
    RunResult,
    RunnerDeps,
    run,
)
from atri_x.toolrun.toolrun import (
    BACKGROUND_DISALLOWED_TOOLS,
    BACKGROUND_PATH_TOOLS,
    Manager,
    background_tool_allowed,
    cached_tool_available,
    native_cached_tools_from_discovery,
    schema_for_context,
)

__all__ = [
    "BACKGROUND_DISALLOWED_TOOLS",
    "BACKGROUND_PATH_TOOLS",
    "DEFAULT_ATRIWISP_TIMEOUT_SECONDS",
    "MAX_PREVIEW_RUNES",
    "SOURCE_KIND_ATRIWISP",
    "SOURCE_KIND_NATIVE",
    "AtriwispInjection",
    "AtriwispToolDeclaration",
    "Cache",
    "CachedTool",
    "ConfirmResult",
    "Manager",
    "NativeSource",
    "RunRequest",
    "RunResult",
    "RunnerDeps",
    "atriwisp_tool_name",
    "available_in_context",
    "background_tool_allowed",
    "cached_tool_available",
    "cached_tools_from_atriwisp",
    "decode_cache",
    "encode_cache",
    "execute_atriwisp_tool",
    "first_atriwisp_result",
    "merge_cached_tools",
    "native_cached_tools_from_discovery",
    "new_native_source",
    "normalize_cached_tools",
    "run",
    "schema_for_context",
    "sort_cached_tools",
    "unavailable_reason",
]
