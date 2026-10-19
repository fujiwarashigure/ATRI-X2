"""脱敏模块不可用时的正则兜底（``atri_x/logging/logging.py`` 的降级实现）。

对应研究报告 §5.2：``atri_x.redact`` 取不到时，旧实现把 ``redact_secrets`` /
``redact_text`` 换成**恒等函数**（密钥原样落日志），且除 ``REDACT_AVAILABLE``
外没有任何信号。本模块提供：

* :data:`FALLBACK_RULES` + :func:`fallback_redact`：只保证"常见密钥形态不再明文"，
  不替代 ``atri_x.redact`` 的严格脱敏（宁可多遮）；
* :func:`announce_once`：进程内只发一次 ERROR（没有日志器时退回 stderr）；
* :func:`status`：``/diagnostics`` 与 ``doctor`` 共用的自检快照。

单独成模块是为了让 ``logging.py`` 保持在单文件 600 行目标内。
"""

from __future__ import annotations

import re
import sys
import threading
from typing import Any

__all__ = [
    "FALLBACK_RULES",
    "announce_once",
    "fallback_redact",
    "status",
]

#: 兜底遮盖规则 ``(模式, 替换)``：与 :mod:`atri_x.redact.redact` 的规则集**同口径**
#: （PEM 私钥块 / Telegram bot URL 与裸 token / sk-、GitHub PAT、AWS AKID / JWT /
#: Basic / Cookie / DSN userinfo / URL query 签名 / key=value），只覆盖常见形态，
#: 宁可多遮。这里是降级实现，不替代严格脱敏。
FALLBACK_RULES: tuple[tuple[str, str], ...] = (
    (
        r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z0-9 ]*PRIVATE KEY-----",
        "[REDACTED]",
    ),
    (r"(?i)(https?://[^/\s]+/bot)\d+:[A-Za-z0-9_\-]+", r"\1[REDACTED]"),
    (r"(?i)\bbot\d{6,}:[A-Za-z0-9_\-]{20,}", "bot[REDACTED]"),
    (r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b", "[REDACTED]"),
    (
        r"(?i)\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{5,}"
        r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
        r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
        r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}"
        r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        "[REDACTED]",
    ),
    (r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}", r"\1[REDACTED]"),
    (r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]{8,}", r"\1[REDACTED]"),
    (r"(?i)((?:set-)?cookie\s*:\s*)[^\r\n]+", r"\1[REDACTED]"),
    (r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b", "[REDACTED]"),
    (r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]{1,64}:[^/\s@]{1,128}@", r"\1[REDACTED]@"),
    (
        r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token"
        r"|x-goog-signature|signature|sig|auth[_-]?key|rkey"
        r"|access[_-]?token|api[_-]?key)=)[^&\s]+",
        r"\1[REDACTED]",
    ),
    (
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"private[_-]?key|secret|password|passwd|token|authorization|cookie)"
        r"\b[\"']?(\s*[:=]\s*)([\"']?)[^\s\"',;&]{4,}\3",
        r"\1\2\3[REDACTED]\3",
    ),
)

#: 启动期告警的"只发一次"守卫（进程级，线程安全）。
_ALERT_MU = threading.Lock()
_ALERT_SENT = False


def fallback_redact(value: str) -> str:
    """降级脱敏：按 :data:`FALLBACK_RULES` 遮盖常见密钥形态（不再是恒等透传）。"""
    text = "" if value is None else str(value)
    for pattern, replacement in FALLBACK_RULES:
        text = re.sub(pattern, replacement, text)
    return text


def announce_once(logger: Any = None) -> bool:
    """降级时打一条 ERROR（进程内只发一次，防止刷屏）；没有日志器时退回 stderr。

    返回 ``True`` 表示"当前是严格脱敏，无需告警"，``False`` 表示"本次确实降级"。
    """
    global _ALERT_SENT
    with _ALERT_MU:
        if _ALERT_SENT:
            return False
        _ALERT_SENT = True
    text = (
        "log redaction is unavailable (atri_x.redact import failed); "
        "regex fallback masks common secret shapes only"
    )
    if logger is None:
        sys.stderr.write("atri-x: " + text + "\n")
    else:
        logger.error(text, event="redact_unavailable", fallback="regex")
    return False


def status(available: bool) -> dict[str, Any]:
    """脱敏接线自检快照（``/diagnostics`` 与 ``doctor`` 共用）。"""
    if available:
        return {"available": True, "mode": "strict"}
    return {"available": False, "mode": "fallback", "fallback_rules": len(FALLBACK_RULES)}
