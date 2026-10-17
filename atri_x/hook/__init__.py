"""Hook 与插件。

对应 Go：``internal/hook``。

子模块：

- :mod:`atri_x.hook.event` —— 事件点、事件 payload、Handler 基础类型
- :mod:`atri_x.hook.match` —— 条件匹配、字段取值、模板值、BlockPolicy
- :mod:`atri_x.hook.media` —— 宿主 Media API（Go Hook 用）
- :mod:`atri_x.hook.manager` —— 普通 Hook 的注册、排序、执行与原子快照替换
- :mod:`atri_x.hook.process` —— 进程 Hook 继承的环境
"""

from __future__ import annotations

from atri_x.hook.event import *  # noqa: F401,F403
from atri_x.hook.event import __all__ as _event_all
from atri_x.hook.media import MediaAPI  # noqa: F401

__all__ = list(_event_all) + ["MediaAPI"]
