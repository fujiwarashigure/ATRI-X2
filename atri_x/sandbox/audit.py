"""沙箱审计：按租户/任务记录执行、队列、网络、重置与失败分类。

修改清单 §15 要求：审计记录租户、任务、镜像版本、资源档案、网络档案与退出原因；
区分普通失败 / 排队超时 / OOM / 磁盘超限 / 命令超时 / 后端故障；日志要脱敏且有
总量限制。

实现取舍：

* 单个 JSONL 文件 + 一个轮转文件（``.1``），总量上限约为 ``2 × max_bytes``；
* 只记**结论与计数**，不记命令原文/输出原文；``command`` 字段只保留截断后的预览，
  且经过 :func:`redact_text`（token / Bearer / JWT / 私钥头）；
* 写盘用 ``threading.Lock`` 串行，异步调用方不必 await（审计失败不影响主流程）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

__all__ = ["SandboxAudit", "redact_text"]

#: 常见凭据形态；命中后替换成 ``<redacted:kind>``。
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("openai", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("aws", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

#: 命令预览长度上限（审计不保存完整命令，避免把用户输入里的秘密落盘）。
_COMMAND_PREVIEW = 240


def redact_text(text: str, *, limit: int = _COMMAND_PREVIEW) -> str:
    """脱敏 + 截断：token / Bearer / JWT / 私钥头一律替换，其余保留可读预览。"""
    value = str(text or "")
    for kind, pattern in _PATTERNS:
        value = pattern.sub(f"<redacted:{kind}>", value)
    value = value.replace("\r", " ").replace("\n", " ")
    if len(value) > limit:
        value = value[: max(0, limit - 1)] + "…"
    return value


class SandboxAudit:
    """JSONL 审计写入器（轮转 + 脱敏）。"""

    def __init__(self, path: str, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.path = os.path.abspath(os.path.normpath(path))
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        """追加一条审计；字段值做 JSON 安全化与脱敏。"""
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": str(event or "unknown"),
        }
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("command", "detail", "error", "message"):
                record[key] = redact_text(str(value))
            elif isinstance(value, (int, float, bool)):
                record[key] = value
            else:
                record[key] = redact_text(str(value), limit=512)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._rotate_locked()
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError:
                # 审计失败不能影响任务执行；doctor 会报出审计不可写。
                return

    def tail(self, limit: int = 50) -> list[dict]:
        """读最近 ``limit`` 条（GUI 用；读不到返回空列表）。"""
        items: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        items.append(parsed)
        except OSError:
            return []
        return items[-max(1, int(limit)):]

    def writable(self) -> bool:
        """审计文件是否可写（doctor 用）。"""
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                probe = self.path + ".probe"
                with open(probe, "w", encoding="utf-8") as handle:
                    handle.write("")
                os.unlink(probe)
                return True
            except OSError:
                return False

    def _rotate_locked(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.max_bytes:
            return
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
