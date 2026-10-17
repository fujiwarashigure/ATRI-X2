"""Hook 输出的线格式。

对应 Go：``internal/hook/output``。

- :mod:`atri_x.hook.output.output` —— ``outputs`` 段、目标、构造与严格解码
- :mod:`atri_x.hook.output.message` —— ``message.segments`` 段
- :mod:`atri_x.hook.output.jsonstrict` —— 严格 JSON 工具（Go 的
  ``DisallowUnknownFields`` 语义）
"""

from __future__ import annotations

from atri_x.hook.output import jsonstrict as _jsonstrict
from atri_x.hook.output.jsonstrict import *  # noqa: F401,F403
from atri_x.hook.output.jsonstrict import __all__ as _jsonstrict_all
from atri_x.hook.output.message import *  # noqa: F401,F403
from atri_x.hook.output.message import __all__ as _message_all
from atri_x.hook.output.output import *  # noqa: F401,F403
from atri_x.hook.output.output import __all__ as _output_all

__all__ = sorted(set(_output_all) | set(_message_all) | set(_jsonstrict_all))
