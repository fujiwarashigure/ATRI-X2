"""``hook.v2`` 进程间协议。

对应 Go：``internal/hook/protocol``。

- :mod:`atri_x.hook.protocol.frames` —— 帧、返回值基座与 ID 校验
- :mod:`atri_x.hook.protocol.eventjson` —— ``hook.Event`` 的 JSON 投影
  （Go 靠 struct tag 自动完成；Python 的数据类没有这条通路）
"""

from __future__ import annotations

from atri_x.hook.protocol.eventjson import *  # noqa: F401,F403
from atri_x.hook.protocol.eventjson import __all__ as _eventjson_all
from atri_x.hook.protocol.frames import *  # noqa: F401,F403
from atri_x.hook.protocol.frames import __all__ as _frames_all
from atri_x.hook.protocol.stream import *  # noqa: F401,F403
from atri_x.hook.protocol.stream import __all__ as _stream_all

__all__ = sorted(set(_frames_all) | set(_eventjson_all) | set(_stream_all))
