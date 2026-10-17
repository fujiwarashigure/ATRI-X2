"""常驻记忆文件的原子落盘。对应 Go：``internal/memory/resident/atomic.go``（``writeFileAtomic`` /
``syncDirBestEffort``）。

**实现选择**：不另写一份原子替换，直接复用已完成的
:mod:`atri_x.utils.fileops.atomic_write`（同目录临时文件 → 写入 → chmod → **fsync** →
close → 原子替换；Windows 走 ``MoveFileExW`` + ``WRITE_THROUGH``，POSIX 走
``os.replace`` + 目录 fsync）。本模块只补齐 Go 侧另外两件上游有、helper 没有的事：

1. 目标目录不存在时先 ``MkdirAll(dir, 0o755)``；
2. 目标文件已存在时**保留运维设置的权限位**（例如 0600），不要把每次替换都重置回
   默认值。

崩溃或掉电只会留下"旧的完整文件"或"新的完整文件"，不会出现被截断的
``memories.toml``。
"""

from __future__ import annotations

import os
import stat

from atri_x.utils.fileops.atomic_write import atomic_write_file

__all__ = ["write_file_atomic"]


def write_file_atomic(path: str, data: bytes, perm: int = 0o644) -> None:
    """原子且持久地把 ``data`` 写到 ``path``。对应 Go ``writeFileAtomic``。

    临时文件与目标同目录，保证最后一步 rename 不跨文件系统。
    """
    path = os.path.normpath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    if os.path.exists(path):
        try:
            perm = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:  # pragma: no cover - 与 Go 一样：取不到就沿用传入值
            pass
    atomic_write_file(path, data, perm)
