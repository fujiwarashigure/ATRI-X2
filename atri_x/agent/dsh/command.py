"""DSH ACP 启动命令的解析（冒烟与路由共用，**唯一出处**）。

解析顺序（与 ``docs/windows-app.md`` 一致，**故意让内置内核优先**）：

1. 显式传入的命令（``[agent.dsh].command`` / ``smoke --command``）；
2. ``DSH_ACP_COMMAND`` 环境变量；
3. 发行包内置的 DSH 内核（``atri_x.sidecar.dsh_command``）；
4. 最后才回落到 PATH 上的 ``dsh --profile acp``。

找不到时返回 ``None``：路由据此判定「DSH 不可用」并保持原生链路，冒烟据此回退到
默认命令以便打印可读的失败原因。
"""

from __future__ import annotations

import os
import shutil
from typing import Sequence

from atri_x.agent.dsh.types import DEFAULT_COMMAND

__all__ = ["DEFAULT_COMMAND_FALLBACK", "format_command", "resolve_command"]

#: 找不到任何安装时冒烟仍然尝试执行的命令（失败信息更直观）。
DEFAULT_COMMAND_FALLBACK: str = "dsh --profile acp"


def resolve_command(explicit: str = "") -> Sequence[str] | str | None:
    """解析启动命令；都找不到时返回 ``None``。

    返回 ``str``（用户显式写的整行，交给 ``shlex`` 拆）或 ``Sequence[str]``
    （内置内核的 ``node.exe`` + ``bin.js``，路径里可能有空格，不能拼成字符串）。
    """
    value = (explicit or "").strip()
    if value:
        return value
    env_value = (os.environ.get("DSH_ACP_COMMAND") or "").strip()
    if env_value:
        return env_value
    try:
        from atri_x.sidecar import dsh_command
    except Exception:  # noqa: BLE001 - sidecar 缺失只意味着没有内置内核
        dsh_command = None  # type: ignore[assignment]
    if dsh_command is not None:
        kernel_command = dsh_command()
        if kernel_command:
            return list(kernel_command)
    if shutil.which("dsh"):
        return DEFAULT_COMMAND
    return None


def format_command(command: Sequence[str] | str) -> str:
    """把命令显示成一行（路径带空格时加引号）。"""
    if isinstance(command, str):
        return command
    parts = []
    for part in command:
        text = str(part)
        parts.append(f'"{text}"' if " " in text else text)
    return " ".join(parts)
