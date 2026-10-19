"""凭据脱敏与错误 ID。

对应 Go：``internal/redact/redact.go``。

用户可见错误、Hook 失败、日志/audit 与健康快照共用这一份"尽力而为"的
脱敏。它只是纵深防御：调用方仍然不应该把凭据放进错误或日志字段里。

与 Go 的**有意差异**：规则集是 Go ``redact.go`` 的超集（补齐 PEM 私钥块、
裸 bot token、DSN userinfo、AWS access key id、GitHub 细粒度 PAT、JWT、
``Authorization: Basic``、``Cookie``/``Set-Cookie`` 与 URL query 签名参数），
因为 Python 版把 ``secrets()`` 用在了会重复扫描同一份文件的部署脚本上，
"漏遮"的代价比上游更高；单遍输出仍与 Go 在共有形态上逐字一致。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Pattern

__all__ = [
    "Error",
    "NewErrorID",
    "Secrets",
    "Summarize",
    "error",
    "new_error_id",
    "redact_secrets",
    "redact_text",
    "secrets",
    "summarize",
]

# 按顺序匹配错误信息里常见的凭据形态。顺序有意义：先处理结构化前缀
# （私钥块、URL、sk-、bot token），再处理请求头（Basic/Cookie/Bearer）、
# URL query 里的签名，最后才处理 key=value 形式（它的取值分支最贪心）。
_RULES: list[tuple[Pattern[str], str]] = [
    # PEM 私钥块（多行，必须最先处理，否则后续规则只遮盖其中一行）
    (
        re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED]",
    ),
    # Telegram: https://api.telegram.org/bot<id>:<token>/method
    (re.compile(r"(?i)(https?://[^/\s]+/bot)\d+:[A-Za-z0-9_-]+"), r"\1[REDACTED]"),
    # 裸的 bot token（非 URL 形态：``bot123456:AA...``）
    (re.compile(r"(?i)\bbot(\d{6,}:[A-Za-z0-9_-]{20,})"), "bot[REDACTED]"),
    # URL 中的 userinfo，例如 https://user:pass@example.com/hook
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    # 数据源 DSN 的 userinfo，例如 postgres://admin:s3cr3t@db:5432/app
    (
        re.compile(
            r"(?i)\b((?:postgres|postgresql|mysql|mariadb|redis|rediss|mongodb|amqp)"
            r"(?:\+[a-z0-9]+)?://)[^/\s:@]+:[^/\s@]+@"
        ),
        r"\1[REDACTED]@",
    ),
    # 常见带前缀的 API Key / Token（含 AWS access key id 与 GitHub 细粒度 PAT）
    (
        re.compile(
            r"(?:sk-[A-Za-z0-9_-]{8,}"
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"
            r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b)"
        ),
        "[REDACTED]",
    ),
    # JWT（三段 base64url，第一段 ``eyJ``）
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
        "[REDACTED]",
    ),
    # Authorization: Basic <base64>
    (re.compile(r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]{8,}"), r"\1[REDACTED]"),
    # Cookie / Set-Cookie（整个头值都可能带会话 id）
    (re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[^\r\n]+"), r"\1[REDACTED]"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}"), r"\1[REDACTED]"),
    # URL query 里的签名 / 令牌参数（先于 key=value，避免把 ``&`` 后面一起吃掉）
    (
        re.compile(
            r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token"
            r"|x-goog-signature|signature|sig|auth[_-]?key|rkey"
            r"|access[_-]?token|api[_-]?key)=)[^&\s]+"
        ),
        r"\1[REDACTED]",
    ),
    # key=value / "key": "value" 形式的凭据
    #
    # 第三分支（无引号值）比 Go 多一个 ``(?!\[REDACTED\])`` 先行断言：Go 的
    # ``[^\s,;]+`` 会把**已经脱敏过**的值连同后面的 ``}`` / ``]`` / ``)`` 一起吃掉，
    # 于是 ``secrets(secrets(x)) != secrets(x)``——重复应用会持续啃掉文本尾部。
    # Go 侧只在日志行上调用一次，所以这个缺陷在上游不可见；但 Python 版把
    # ``secrets()`` 用在了会**重复扫描同一份文件**的部署脚本（watchdog）上，
    # 非幂等就会真的损坏日志。加断言后，首次脱敏的输出与 Go 逐字一致（引号分支
    # 优先且未改动），只有「值已经是 [REDACTED]」的二次扫描不再重复匹配。
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret"
            r"|bot[_-]?token|refresh[_-]?token|access[_-]?token|auth[_-]?token|token"
            r"|secret|password|passwd)[\"']?\s*[=:]\s*)"
            r"(\"[^\"]*\"|'[^']*'|(?!\[REDACTED\])[^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
]


def secrets(value: str) -> str:
    """移除 ``value`` 里常见的凭据形态。"""
    value = (value or "").strip()
    if not value:
        return value
    for pattern, replacement in _RULES:
        value = pattern.sub(replacement, value)
    return value


def error(err: BaseException | None) -> str:
    """返回脱敏后的错误文本（对应 Go ``redact.Error``）。"""
    if err is None:
        return ""
    return secrets(str(err))


def summarize(value: str, limit: int = 0) -> str:
    """脱敏并截断，用于用户可见通知；长度按 rune 计。"""
    if limit <= 0:
        limit = 400
    value = secrets((value or "").strip())
    if len(value) <= limit:
        return value
    return value[:limit] + "...（已截断）"


def new_error_id() -> str:
    """返回一个随机 UUID 形态的错误 ID（对应 Go ``NewErrorID``）。

    Go 用 16 字节随机数并手动置 version/variant 位，输出
    ``%08x-%04x-%04x-%04x-%012x``（8-4-4-4-12 小写）；标准库 ``uuid4()``
    的格式与位设置完全一致。
    """
    try:
        os.urandom(16)
    except Exception:  # pragma: no cover - 随机源不可用时与 Go 同样降级
        return "error-unknown"
    return str(uuid.uuid4())


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
Secrets = secrets
Summarize = summarize
NewErrorID = new_error_id
Error = error

# ``PORTING.md`` §3 约定的契约名。``logging`` 等模块按这两个名字导入，
# 早期只有 ``secrets``/``error`` 导致调用点踩空；这里补齐别名，
# 语义等价（都要求**单参数、返回 str**）。
redact_text = secrets
redact_secrets = secrets
