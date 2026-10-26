"""网络时间校验子系统（首版 ``verify_only``）。

模块分工：

* :mod:`atri_x.timesync.model` —— 观测/轮次数据与判定（纯函数，无 I/O）；
* :mod:`atri_x.timesync.policy` —— 出站策略（按来源白名单、私网例外、地址分类）；
* :mod:`atri_x.timesync.sntp` —— 受限 SNTP 客户端（单播、48 字节、无认证）；
* :mod:`atri_x.timesync.https_time` —— HTTPS JSON 时间适配器（TLS 永不关闭）；
* :mod:`atri_x.timesync.verifier` —— 后台/手动共用的单飞校验器；
* :mod:`atri_x.timesync.diagnostics` —— 只读诊断聚合。

**不做**：修改系统时钟、NTP 服务端/转发、NTS、把普通 SNTP 标记为已认证、
用过期样本外推当前时间、让模型或普通成员配置来源。
"""

from __future__ import annotations

from atri_x.timesync.diagnostics import time_diagnostics
from atri_x.timesync.model import (
    ROUND_TRIGGER_MANUAL,
    ROUND_TRIGGER_SCHEDULED,
    STATUS_OK,
    RoundResult,
    TimeObservation,
    evaluate_round,
    trust_level,
)
from atri_x.timesync.policy import OutboundPolicy, redact_url, validate_source
from atri_x.timesync.verifier import Verifier, VerifierOptions

__all__ = [
    "ROUND_TRIGGER_MANUAL",
    "ROUND_TRIGGER_SCHEDULED",
    "STATUS_OK",
    "OutboundPolicy",
    "RoundResult",
    "TimeObservation",
    "Verifier",
    "VerifierOptions",
    "evaluate_round",
    "redact_url",
    "time_diagnostics",
    "trust_level",
    "validate_source",
]
