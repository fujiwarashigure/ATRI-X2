"""``atri_x.agent.dsh``：ATRI-X 编排 DSH 的纯标准库 ACP 客户端。

模块分工
========
- :mod:`atri_x.agent.dsh.types`：协议常量、异常、会话/事件/用量数据形状与解析函数；
- :mod:`atri_x.agent.dsh.transport`：``dsh --profile acp`` 子进程与 NDJSON JSON-RPC 帧收发；
- :mod:`atri_x.agent.dsh.client`：``AcpClient``（initialize / session/* / prompt / cancel）；
- :mod:`atri_x.agent.dsh.command`：启动命令解析（显式 > 环境变量 > 内置内核 > PATH）；
- :mod:`atri_x.agent.dsh.router`：任务特征打分 + 成本闸门 + 熔断（``[agent.dsh]``）；
- :mod:`atri_x.agent.dsh.handoff`：把最近对话拼成一次性 prompt、ACP 用量折算；
- :mod:`atri_x.agent.dsh.runtime`：长驻 ``AcpClient`` + 串行 prompt 执行器；
- :mod:`atri_x.agent.dsh.route_call`：把路由接进 ``call_llm``（预算/流式/失败回退）；
- :mod:`atri_x.agent.dsh.smoke`：``python -m atri_x.agent.dsh.smoke`` 最小闭环冒烟。

只依赖标准库。进程配置、路由阈值、预算记账、平台输出由 ATRI-X 的其他模块负责；
本包不主动参与启动流程，导入它没有副作用（路由状态在第一次真正需要时才创建）。
"""

from __future__ import annotations

from atri_x.agent.dsh.client import AcpClient
from atri_x.agent.dsh.types import (
    ACP_PROTOCOL_VERSION,
    DEFAULT_COMMAND,
    MAX_FRAME_BYTES,
    AcpError,
    AcpEvent,
    AcpProcessError,
    AcpPromptResult,
    AcpProtocolError,
    AcpServerInfo,
    AcpSession,
    AcpTimeoutError,
    AcpUsage,
)

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "DEFAULT_COMMAND",
    "MAX_FRAME_BYTES",
    "AcpClient",
    "AcpError",
    "AcpEvent",
    "AcpProcessError",
    "AcpPromptResult",
    "AcpProtocolError",
    "AcpServerInfo",
    "AcpSession",
    "AcpTimeoutError",
    "AcpUsage",
]
