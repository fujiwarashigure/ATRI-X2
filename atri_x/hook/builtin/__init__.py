"""内置 Hook 插件：规则引擎、天使记忆、自我学习、出站历史。

对应 Go：``internal/hook/builtin``。

- :mod:`atri_x.hook.builtin.register` —— ``Options`` 与 ``RegisterAll``
- :mod:`atri_x.hook.builtin.angel_memory` —— 长期记忆召回
- :mod:`atri_x.hook.builtin.self_learning` —— 群语料观察与上下文注入
"""

from __future__ import annotations

from atri_x.hook.builtin.angel_memory import register_angel_memory
from atri_x.hook.builtin.register import (
    Options,
    RegisterAll,
    record_outbound_message,
    register_module,
    report_plugin_error,
)
from atri_x.hook.builtin.self_learning import register_self_learning

__all__ = [
    "Options",
    "RegisterAll",
    "record_outbound_message",
    "register_angel_memory",
    "register_module",
    "register_self_learning",
    "report_plugin_error",
]
