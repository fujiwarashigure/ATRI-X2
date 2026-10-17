"""内置工具共享的运行时信息 —— 现在只是 :mod:`atri_x.tool.runtimeinfo` 的再导出。

对应 Go：``internal/tool/runtimeinfo/runtimeinfo.go``。

历史说明：本模块原先自带一份 ``RuntimeInfo`` 实现（当时 ``atri_x/tool/runtimeinfo.py``
尚未落地）。Lead 已把规范实现落在 :mod:`atri_x.tool.runtimeinfo`，为遵循 PORTING.md
§4 的包映射，这里**收敛为再导出**，不再保留第二份定义（两份会让 ``isinstance``
与 ``==`` 跨模块失效）。

与旧实现的可见差异（各调用点已核对）：

* 名字：规范的类名是 ``Info``；``RuntimeInfo`` 作为**别名**保留，以免破坏既有调用。
* ``normalize()`` 返回**新实例**（Go 的值语义），不再原地修改；``Info`` 是 frozen
  dataclass。
* 空 ``now`` 回落到**本机时区**时间（Go ``time.Now()``），旧实现回落到 UTC。
  两者都带时区，差别只在 ``tzinfo``。
* ``first(*infos)`` 是可变参数（Go ``First(infos ...Info)``）；旧签名是
  ``first(list)``。
"""

from __future__ import annotations

from atri_x.tool.runtimeinfo import (  # noqa: F401
    Info,
    atriyph_rule_card,
    first,
)

#: 兼容别名：``Info`` 在本包内历史上叫 ``RuntimeInfo``。
RuntimeInfo = Info

__all__ = ["Info", "RuntimeInfo", "atriyph_rule_card", "first"]
