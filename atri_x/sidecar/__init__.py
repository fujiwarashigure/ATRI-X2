"""``atri_x.sidecar``：随发行包一起安装的本地运行时（sidecar）。

ATRI-X 的核心运行时是 Python 本身（纯标准库）。有些能力需要**额外的运行时**才能跑：

- DSH 内核（``dsh-kernel``）：Node 运行时 + ``@deepseek-ai/dsh`` 的 ACP 依赖闭包，
  让 :mod:`atri_x.agent.dsh` 不必依赖机器上另装的 ``dsh``；
- 以后可以按同一套「目录 + 清单」再加别的运行时（嵌入式 Python、ffmpeg 等）。

查找顺序（见 :func:`atri_x.sidecar.registry.runtime_dirs`）：

1. ``ATRIX_RUNTIME_DIR`` 环境变量：显式指定的运行时根目录（优先级最高，便于本地调试）；
2. 发行包内部：``<bundle>/runtime``——冻结后 ``<bundle>`` 是 PyInstaller 的 ``_MEIPASS``，
   源码运行时时是仓库根；
3. 用户数据目录：``<配置目录>/runtime``——允许不重新打包就替换某个运行时。

模块职责：

| 模块 | 职责 |
|---|---|
| :mod:`atri_x.sidecar.registry` | 运行时目录发现、清单解析、按 id 查找 |
| :mod:`atri_x.sidecar.kernel` | DSH 内核的定位、可用性判断、启动命令组装 |
| :mod:`atri_x.sidecar.__main__` | ``python -m atri_x.sidecar list|verify|dsh-command`` |

运行时目录布局（以 DSH 内核为例，由 ``packaging/assemble_kernel.py`` 生成）::

    runtime/dsh-kernel/
      kernel.json        # 清单：id/kind/入口/版本/包数
      node/node.exe
      node_modules/...
"""

from __future__ import annotations

from atri_x.sidecar.kernel import DshKernel, describe, dsh_command, find_kernel
from atri_x.sidecar.registry import (
    RUNTIME_DIR_ENV,
    RUNTIME_MANIFEST,
    Runtime,
    bundle_root,
    discover,
    find,
    runtime_dirs,
)

__all__ = [
    "RUNTIME_DIR_ENV",
    "RUNTIME_MANIFEST",
    "DshKernel",
    "Runtime",
    "bundle_root",
    "describe",
    "discover",
    "dsh_command",
    "find",
    "find_kernel",
    "runtime_dirs",
]
