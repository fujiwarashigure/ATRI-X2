"""日志中心：文本行格式、按日文件轮转、三个日志通道。对应 Go：``internal/logging/logging.go``。

与 Go 的对应关系：

* Go 用 ``log/slog`` 的 ``TextHandler`` + ``ReplaceAttr``；本模块自带等价的文本序列化，
  因为它必须与 :mod:`atri_x.logging.reader` 的"文本键值契约"互为逆运算。
* 行格式：``time="2026-10-13 12:00:00" level=INFO msg=... key=value``。
  ``time`` 是**本地墙钟**（读取侧按本地时间解析，与 Go 的 ``time.ParseInLocation`` 对齐）；
  值含空格/引号/控制字符时按 Go ``strconv.Quote`` 规则加引号转义。
* 三个通道：``atri-x-<date>.log``（运行）、``audit-<date>.log``（审计）、
  ``atrinis-<date>.log``（Atrinis）。审计通道有独立下限：运行级别高于 info 时
  审计仍至少保留 info（usage 事实、命名诊断、Hook 工具调用不能因为调高日志级别而消失）。
* 目录是 ``<sessions.sqlite 所在目录>/logs``；保留天数默认 30 天，``cleanup_old_logs``
  只删自己管的三种前缀文件。

本包名叫 ``atri_x.logging``，与标准库 ``logging`` 同名；包内**不 import 标准库 logging**
（本模块自带 ``Logger``，也不需要它）。确实需要标准库时请写 ``import logging as std_logging``。

脱敏走共享契约模块 :mod:`atri_x.redact.redact`：字符串属性值走 ``redact_secrets``
（= Go ``redact.Secrets``），异常值走 ``redact_text``（= Go ``redact.Error``）。
**非标量属性值同样要脱敏**：dict / list / tuple / set 与其它对象先 ``str()`` 再
过 ``redact_secrets``（结构化正文里的凭据不再因为"不是 str"而明文落盘）。
消息正文（``msg``）与 Go 一样不做脱敏。该模块同时导出 ``secrets`` / ``error``
与 PORTING 口径的 ``redact_secrets`` / ``redact_text``，本模块两种命名都接。

**降级语义（不再原样透传）**：脱敏模块取不到时 ``REDACT_AVAILABLE`` 置 False，改用
:data:`_FALLBACK_RULES` 正则兜底（只保证"不再明文"）；:func:`announce_redact_degradation`
启动期报一次 ERROR，:func:`redact_status` 供 ``/diagnostics`` 与 ``doctor`` 自检。
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections.abc import Callable
from copy import copy
from datetime import datetime, timedelta
from typing import Any, TextIO

from atri_x.errors import ClosedError
from atri_x.storage.times import format_time

# 脱敏由 atri_x.redact.redact 提供（优先 PORTING 口径的名字，其次 secrets / error）；
# 两者都取不到时降级为正则兜底（见 atri_x.logging.redact_fallback）并置
# REDACT_AVAILABLE=False。
try:
    from atri_x.redact import redact as _redact
except ImportError:  # pragma: no cover - 脱敏模块缺失时
    _redact = None

from atri_x.logging.redact_fallback import (
    announce_once as _announce_fallback,
    fallback_redact as _fallback_redact,
    status as _fallback_status,
)

redact_secrets = None
redact_text = None
if _redact is not None:
    redact_secrets = getattr(_redact, "redact_secrets", None) or getattr(_redact, "secrets", None)
    redact_text = getattr(_redact, "redact_text", None) or getattr(_redact, "error", None)

#: 是否接通了真实的脱敏模块（False = 当前走 :mod:`atri_x.logging.redact_fallback`）。
REDACT_AVAILABLE = callable(redact_secrets) and callable(redact_text)

if not REDACT_AVAILABLE:  # pragma: no cover - 脱敏模块缺失时才生效
    redact_secrets = _fallback_redact
    redact_text = _fallback_redact


def announce_redact_degradation(logger: Any = None) -> bool:
    """降级时打一条 ERROR（进程内只发一次）；严格脱敏时返回 ``True``。"""
    if REDACT_AVAILABLE:
        return True
    return _announce_fallback(logger)


def redact_status() -> dict[str, Any]:
    """脱敏接线自检快照（``/diagnostics`` 与 ``doctor`` 共用）。"""
    return _fallback_status(REDACT_AVAILABLE)


#: Go ``logging.DefaultRetentionDays``。
DEFAULT_RETENTION_DAYS = 30

#: 受管的日志文件前缀（Go ``isManagedLogFile``）。
MANAGED_LOG_PREFIXES = ("atri-x-", "audit-", "atrinis-")

_LEVEL_NAMES = ("DEBUG", "INFO", "WARN", "ERROR")


# ---------------------------------------------------------------------------
# 级别
# ---------------------------------------------------------------------------


def level_rank(level: str) -> int:
    """把级别名映射成序号（Go ``logging.parseLevel`` / ``reader.levelRank``）。

    未知级别按 ``INFO``（Go 两个实现的默认分支一致）。
    """
    name = (level or "").strip().upper()
    if name == "DEBUG":
        return 0
    if name in ("WARN", "WARNING"):
        return 2
    if name == "ERROR":
        return 3
    return 1


#: Go ``logging.parseLevel``（同名，返回序号而不是 slog.Level）。
parse_level = level_rank


def level_name(level: str) -> str:
    """规范成 Go ``slog.Level.String()`` 的形式（``warning`` → ``WARN``）。"""
    return _LEVEL_NAMES[level_rank(level)]


def audit_floor_level(level: str) -> str:
    """审计通道的级别下限：至少 info。对应 Go ``auditFloorLevel``。"""
    if level_rank(level) > level_rank("info"):
        return "info"
    return level


# ---------------------------------------------------------------------------
# 文本键值编解码（写入侧）
# ---------------------------------------------------------------------------

#: Go ``slog`` 的 ``safeSet`` 等价物：不需要加引号的 ASCII 字符。
_SAFE_ASCII = frozenset(
    " !#$%&'()*+,-./0123456789:;<>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_`"
    "abcdefghijklmnopqrstuvwxyz{|}~"
)

_GO_ESCAPES = {
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "\\": "\\\\",
    '"': '\\"',
}


def needs_quoting(value: str) -> bool:
    """报告文本值是否需要加引号。对应 Go ``slog`` 的 ``needsQuoting``。

    需要加引号：空串、含空格或 ``=``、含非安全 ASCII（含控制字符）、含非打印字符或
    非 ASCII 空白字符。反斜杠本身不触发加引号（Go 同样如此），但一旦触发引号，
    引号内的反斜杠会被转义。
    """
    if not value:
        return True
    for ch in value:
        if ch == "\\":
            continue
        code = ord(ch)
        if code < 0x80:
            if ch == " " or ch == "=" or ch not in _SAFE_ASCII:
                return True
            continue
        if ch.isspace() or not ch.isprintable():
            return True
    return False


def quote_go(value: str) -> str:
    """按 Go ``strconv.Quote`` 的规则给文本加双引号。"""
    out = ['"']
    for ch in value:
        escaped = _GO_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif ch.isprintable():
            out.append(ch)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    out.append('"')
    return "".join(out)


def quote_if_needed(value: str) -> str:
    """需要时加引号。对应 Go ``slog`` 的 ``appendString`` / ``appendKey``。"""
    return quote_go(value) if needs_quoting(value) else value


def _attr_text(value: Any) -> str:
    """把一个属性值渲染成文本（未加引号），并施加与 Go ``replaceAttr`` 等价的脱敏。

    ``str`` 与异常直接走脱敏；dict / list / tuple / set 等结构化值先 ``str()`` 再
    脱敏——它们过去落到最后一个分支原样输出，等于给凭据留了一条绕过路径。
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return format_time(value) or "0001-01-01T00:00:00Z"
    if isinstance(value, BaseException):
        return redact_text(str(value))
    if value is None:
        return "<nil>"
    return redact_secrets(str(value))


def format_attrs(attrs: "list[tuple[Any, Any]]") -> str:
    """把 (key, value) 序列渲染成 ``k=v`` 片段（不含前导空格）。

    键不是字符串时按 Go ``slog`` 的 ``!BADKEY`` 处理（Go 只保留值）。
    """
    parts: list[str] = []
    for key, value in attrs:
        if not isinstance(key, str):
            parts.append(quote_if_needed("!BADKEY") + "=" + quote_if_needed(_attr_text(value)))
            continue
        parts.append(quote_if_needed(key) + "=" + quote_if_needed(_attr_text(value)))
    return " ".join(parts)


def pair_attrs(args: tuple[Any, ...]) -> list[tuple[Any, Any]]:
    """把 Go ``slog`` 风格的 ``key, value, key, value`` 位置参数配对。

    参数个数为奇数时，最后一个键的值按 Go 的约定写成 ``!BADKEY``。
    """
    attrs: list[tuple[Any, Any]] = []
    for index in range(0, len(args), 2):
        key = args[index]
        if index + 1 >= len(args):
            attrs.append((key, "!BADKEY"))
            continue
        attrs.append((key, args[index + 1]))
    return attrs


# ---------------------------------------------------------------------------
# 按日文件
# ---------------------------------------------------------------------------


def log_dir_for(sqlite_path: str) -> str:
    """日志目录：``<sqlite 路径所在目录>/logs``。对应 Go ``newPrefixedFile``。"""
    return os.path.join(os.path.dirname(sqlite_path), "logs")


class DailyFileWriter:
    """按日轮转的追加写文件，对应 Go ``dailyFileWriter``。

    线程安全（``threading.Lock``）：一条日志行是一次 :meth:`write` 调用，锁内完成
    "必要时换日 + 写入 + flush"，避免多线程交错出半行。
    """

    def __init__(
        self,
        log_dir: str,
        prefix: str,
        now: Callable[[], datetime] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        if not (log_dir or "").strip():
            raise ValueError("log directory is not configured")
        if not (prefix or "").strip():
            raise ValueError("log prefix is not configured")
        self._log_dir = log_dir
        self._prefix = prefix
        self._now = now or (lambda: datetime.now().astimezone())
        self._encoding = encoding
        self._lock = threading.Lock()
        self._file: TextIO | None = None
        self._day = ""
        self._closed = False
        self._rotate(self.current_day())

    @property
    def log_dir(self) -> str:
        return self._log_dir

    @property
    def prefix(self) -> str:
        return self._prefix

    def path_for_day(self, day: str) -> str:
        """某一天的日志文件路径（``<prefix>-<YYYY-MM-DD>.log``）。"""
        return os.path.join(self._log_dir, f"{self._prefix}-{day}.log")

    def current_day(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def write(self, text: str) -> int:
        """写入一段文本（调用方保证一条记录一次调用）。"""
        with self._lock:
            if self._closed:
                raise ClosedError("log file writer is closed")
            self._rotate(self.current_day())
            handle = self._file
            if handle is None:  # pragma: no cover - _rotate 成功时不为 None
                raise ClosedError("log file writer is closed")
            written = handle.write(text)
            handle.flush()
            return written

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._file is None:
                return
            self._file.close()
            self._file = None

    def _rotate(self, day: str) -> None:
        if self._file is not None and self._day == day:
            return
        os.makedirs(self._log_dir, exist_ok=True)
        # newline="\n"：日志行一律 LF 结尾（Go 写字节，不随平台变 CRLF）。
        handle = open(  # noqa: SIM115 - 生命周期由本类管理
            self.path_for_day(day),
            "a",
            encoding=self._encoding,
            newline="\n",
        )
        old = self._file
        self._file = handle
        self._day = day
        if old is not None:
            old.close()


def is_managed_log_file(name: str) -> bool:
    """是否是本模块管理的日志文件。对应 Go ``isManagedLogFile``。"""
    if not name.endswith(".log"):
        return False
    return name.startswith(MANAGED_LOG_PREFIXES)


def normalize_retention_days(days: int) -> int:
    """保留天数下限补齐。对应 Go ``normalizeRetentionDays``。"""
    if not isinstance(days, int) or days <= 0:
        return DEFAULT_RETENTION_DAYS
    return days


def cleanup_old_logs(log_dir: str, days: int) -> None:
    """删除超过保留期的受管日志文件。对应 Go ``cleanupOldLogs``。

    只删 ``atri-x-*`` / ``audit-*`` / ``atrinis-*`` 且以 ``.log`` 结尾的文件，
    依据是文件 ``mtime``（不是文件名里的日期）。
    """
    days = normalize_retention_days(days)
    cutoff = datetime.now().timestamp() - timedelta(days=days).total_seconds()
    with os.scandir(log_dir) as entries:
        for entry in entries:
            if not entry.is_file() or not is_managed_log_file(entry.name):
                continue
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


class Logger:
    """文本行日志器，对应 Go ``slog.Logger``（``logging.New`` 返回的那个）。

    字段的传参支持两种写法，等价于 Go 的 ``Info(msg string, args ...any)``：

    * Go 风格位置参数：``logger.info("msg", "key", value, "key2", value2)``
    * Python 关键字：``logger.info("msg", key=value, key2=value2)``

    值类型渲染：``str`` → 脱敏文本；``bool`` → ``true``/``false``；``int``/``float``
    → 十进制；``datetime`` → RFC3339；``Exception`` → 脱敏错误文本；``None`` → ``<nil>``；
    其余 → ``str(value)``。
    """

    def __init__(
        self,
        level: str = "info",
        output: TextIO | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        attrs: "tuple[tuple[Any, Any], ...] | None" = None,
    ) -> None:
        self._threshold = level_rank(level)
        self._output = output
        self._now = now or (lambda: datetime.now().astimezone())
        self._attrs: tuple[tuple[Any, Any], ...] = attrs or ()

    # -- 查询 ---------------------------------------------------------------
    def enabled(self, level: str) -> bool:
        """报告该级别是否会被写出（Go ``slog.Logger.Enabled``）。"""
        return level_rank(level) >= self._threshold

    @property
    def threshold(self) -> int:
        """级别阈值序号。"""
        return self._threshold

    @property
    def output(self) -> TextIO | None:
        """当前输出流（``DailyFileWriter`` 或调用方给的 ``io.StringIO`` 等）。"""
        return self._output

    # -- 派生 ---------------------------------------------------------------
    def with_(self, *args: Any, **fields: Any) -> "Logger":
        """派生一个带预置字段的日志器。对应 Go ``slog.Logger.With``。

        名字带下划线是因为 ``with`` 是 Python 关键字。
        """
        child = copy(self)
        child._attrs = self._attrs + tuple(pair_attrs(args)) + tuple(fields.items())
        return child

    # -- 写入 ---------------------------------------------------------------
    def debug(self, message: str, *args: Any, **fields: Any) -> None:
        self.log("debug", message, *args, **fields)

    def info(self, message: str, *args: Any, **fields: Any) -> None:
        self.log("info", message, *args, **fields)

    def warn(self, message: str, *args: Any, **fields: Any) -> None:
        self.log("warn", message, *args, **fields)

    # 别名：Python 习惯写 warning。
    warning = warn

    def error(self, message: str, *args: Any, **fields: Any) -> None:
        self.log("error", message, *args, **fields)

    def log(self, level: str, message: str, *args: Any, **fields: Any) -> None:
        """写一条日志。对应 Go ``slog.Logger.Log(ctx, level, msg, args...)``。"""
        if level_rank(level) < self._threshold:
            return
        if self._output is None:
            raise ValueError("log output is not configured")
        line = self.format_line(level, message, *args, **fields)
        self._output.write(line)

    def format_line(self, level: str, message: str, *args: Any, **fields: Any) -> str:
        """渲染出完整的一行（含结尾换行）；便于测试与自定义输出。"""
        attrs = list(self._attrs)
        attrs.extend(pair_attrs(args))
        attrs.extend(fields.items())
        segments = [
            "time=" + quote_if_needed(self._now().astimezone().strftime("%Y-%m-%d %H:%M:%S")),
            "level=" + level_name(level),
            "msg=" + quote_if_needed(message),
        ]
        rendered = format_attrs(attrs)
        if rendered:
            segments.append(rendered)
        return " ".join(segments) + "\n"


def new_logger(level: str, output: TextIO) -> Logger:
    """构造一个写往 output 的日志器。对应 Go ``logging.New``。"""
    return Logger(level, output)


def new_prefixed_file(level: str, sqlite_path: str, prefix: str) -> "tuple[Logger, DailyFileWriter]":
    """构造一个写往 ``<prefix>-<date>.log`` 的日志器与它的文件句柄。

    对应 Go ``logging.newPrefixedFile``。
    """
    writer = DailyFileWriter(log_dir_for(sqlite_path), prefix)
    return Logger(level, writer), writer


def new_file(level: str, sqlite_path: str) -> "tuple[Logger, DailyFileWriter]":
    """构造一个写往 ``atri-x-<date>.log`` 的日志器与它的文件句柄。

    对应 Go ``logging.NewFile``（返回 ``(*slog.Logger, io.Closer, error)``）。
    """
    return new_prefixed_file(level, sqlite_path, "atri-x")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class Manager:
    """日志中心：三个通道 + 目录 + 保留期。对应 Go ``logging.Manager``。"""

    def __init__(
        self,
        level: str,
        sqlite_path: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._log_dir = log_dir_for(sqlite_path)
        self._retention_days = normalize_retention_days(retention_days)
        runtime_logger, runtime_writer = new_prefixed_file(level, sqlite_path, "atri-x")
        try:
            # 审计通道有独立下限：运行级别高于 info 时审计仍保留 info。
            audit_logger, audit_writer = new_prefixed_file(
                audit_floor_level(level), sqlite_path, "audit"
            )
        except Exception:
            runtime_writer.close()
            raise
        try:
            atrinis_logger, atrinis_writer = new_prefixed_file(level, sqlite_path, "atrinis")
        except Exception:
            runtime_writer.close()
            audit_writer.close()
            raise
        self._runtime = runtime_logger
        self._audit = audit_logger
        self._atrinis = atrinis_logger
        self._writers = (runtime_writer, audit_writer, atrinis_writer)

    def runtime(self) -> Logger:
        """运行日志通道（``atri-x-*``）。"""
        return self._runtime

    def audit(self) -> Logger:
        """审计通道（``audit-*``，级别下限至少 info）。"""
        return self._audit

    def atrinis(self) -> Logger:
        """Atrinis 通道（``atrinis-*``）。"""
        return self._atrinis

    def log_dir(self) -> str:
        """日志目录。对应 Go ``Manager.LogDir``。"""
        return self._log_dir

    def retention_days(self) -> int:
        """保留天数。对应 Go ``Manager.RetentionDays``。"""
        return self._retention_days

    def cleanup_old_logs(self) -> None:
        """按保留期清理旧日志。对应 Go ``Manager.CleanupOldLogs``。"""
        cleanup_old_logs(self._log_dir, self._retention_days)

    def close(self) -> None:
        """关闭全部文件句柄（对应 Go ``Manager.Close``，出错时抛第一个异常）。"""
        first_error: Exception | None = None
        for writer in self._writers:
            if writer is None:
                continue
            try:
                writer.close()
            except Exception as exc:  # noqa: BLE001 - 与 Go 一致：先全部关闭再报第一个错
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "MANAGED_LOG_PREFIXES",
    "REDACT_AVAILABLE",
    "DailyFileWriter",
    "Logger",
    "Manager",
    "audit_floor_level",
    "announce_redact_degradation",
    "cleanup_old_logs",
    "format_attrs",
    "is_managed_log_file",
    "level_name",
    "level_rank",
    "log_dir_for",
    "needs_quoting",
    "new_file",
    "new_logger",
    "new_prefixed_file",
    "normalize_retention_days",
    "pair_attrs",
    "parse_level",
    "quote_go",
    "quote_if_needed",
    "redact_secrets",
    "redact_status",
    "redact_text",
]
