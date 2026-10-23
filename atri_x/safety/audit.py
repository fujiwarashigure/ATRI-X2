"""审计日志：``safety-audit.jsonl`` 的追加写入。

**不存原文**是本模块最重要的约束（与 1.0.2 外挂 PoC 一致）：

* 只写 ``sha256``（原文指纹）+ **脱敏预览**（:meth:`atri_x.safety.rules.RuleSet.mask`
  之后的文本，再截断到 ``audit_preview_chars``）；
* 命中标签、阶段、平台 / scope / actor、动作、时间；
* 原文、命中片段一律不落盘 —— 否则"做了内容安全"反而把敏感内容二次落盘。

失败策略：写审计**绝不能阻断消息发送**。磁盘满、目录不可写、JSON 无法序列化
都只记一条日志并计数（:attr:`AuditLog.write_errors`），不向上抛。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

__all__ = [
    "DEFAULT_AUDIT_MAX_BYTES",
    "AuditLog",
    "utc_now",
]


#: 单个审计文件的大小上限；超过就滚动成 ``.1``（只保留一份历史）。
DEFAULT_AUDIT_MAX_BYTES = 1024 * 1024


def utc_now() -> datetime:
    """当前时间（带 UTC 时区）。审计时间戳一律带 tz。"""
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    """把时间格式化成带时区的 ISO8601（RFC3339 兼容）。"""
    moment = value if isinstance(value, datetime) else utc_now()
    if moment.tzinfo is None:  # 宿主传进来的裸时间补成 UTC，避免审计时区含糊
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(timespec="seconds")


class AuditLog:
    """一份 JSONL 审计日志（``threading.Lock`` 保护，写失败只记日志）。"""

    def __init__(
        self,
        path: str,
        *,
        preview_chars: int = 120,
        max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
        logger: Any = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = (path or "").strip()
        self.preview_chars = max(1, int(preview_chars or 120))
        self.max_bytes = max(0, int(max_bytes or 0))
        self.logger = logger
        self.now = now if callable(now) else utc_now
        self._lock = threading.Lock()
        self.written = 0
        self.write_errors = 0

    # -- 写入 ---------------------------------------------------------------
    def append(self, record: Mapping[str, Any]) -> bool:
        """追加一条记录；返回是否写成功（失败不抛）。"""
        if not self.path:
            return False
        payload = dict(record)
        payload.setdefault("ts", _iso(self.now()))
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        except (TypeError, ValueError) as exc:
            self._fail(f"审计记录无法序列化（已丢弃）：{exc}")
            return False
        with self._lock:
            try:
                self._rotate_locked()
                parent = os.path.dirname(os.path.abspath(self.path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
            except OSError as exc:
                self._fail(f"审计写入失败（不影响主流程）：{exc}")
                return False
            self.written += 1
        return True

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部记录（测试与排障用；损坏的行直接跳过）。"""
        if not self.path:
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def stats(self) -> dict[str, int]:
        """``written`` / ``write_errors``。"""
        return {"written": self.written, "errors": self.write_errors}

    # -- 内部 ---------------------------------------------------------------
    def _rotate_locked(self) -> None:
        """单文件超限时滚动一次（只在持锁时调用）。"""
        if self.max_bytes <= 0:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.max_bytes:
            return
        with contextlib.suppress(OSError):
            os.replace(self.path, self.path + ".1")

    def _fail(self, message: str) -> None:
        self.write_errors += 1
        logger = self.logger
        if logger is None:
            return
        with contextlib.suppress(Exception):
            warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(warn):
                warn(message)
