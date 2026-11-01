"""DSH 会话亲和：同一聊天连续路由时复用会话、只发增量，省掉重复交接。

为什么需要：默认的"无状态交接"每轮都把系统设定 + 最近 N 条历史重新拼进 prompt
（:mod:`atri_x.agent.dsh.handoff`），代价是每轮多付 1~3k 输入 token 的 prefill。
如果同一聊天**连续**两轮都路由到 DSH，第二轮的上下文其实已经在 DSH 会话里了。

做法：命中后**不关会话**，下一轮只发「当前用户消息」这一条增量。正确性靠一道
"历史连续性"校验：

- 存亲和时记下 ``anchor_index = len(messages)`` 与当轮最后一条用户消息的摘要；
- 下一轮要求 ``len(messages) == anchor_index + 2``（助手回复 + 新用户消息各一条）
  且 ``messages[anchor_index - 1]`` 仍是同一条用户消息；
- 不满足（中间夹了原生轮次 / 工具轮次 / 历史被裁剪 / 会话过期）就**放弃复用**，
  退回完整交接 —— 宁可多花 token，也不把错误的上下文喂给模型。

会话失效（失败、看门狗取消、内核重启、空闲回收）时由调用方 ``pop`` 掉并
``session/close``；LRU 上限由 ``max_entries`` 控制，默认只留 4 个。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from atri_x.llm import segments_content_text

__all__ = ["AffinityEntry", "SessionAffinity", "message_digest"]


def _message_text(message: Any) -> str:
    """一条消息的可见文本（与 handoff 的拼接口径一致）。"""
    segments = list(getattr(message, "segments", None) or [])
    return segments_content_text(segments).strip()


def message_digest(message: Any) -> str:
    """一条消息文本的短摘要（判"历史是否连续"，不保存原文）。"""
    return hashlib.sha256(_message_text(message).encode("utf-8")).hexdigest()[:16]


@dataclass
class AffinityEntry:
    """一个可复用的 DSH 会话。"""

    key: str
    session_id: str
    #: 存亲和时 ATRI-X 历史的消息条数（``len(messages)``）。
    anchor_index: int
    #: 当轮最后一条用户消息的摘要。
    anchor_digest: str
    updated: float
    hits: int = 0


class SessionAffinity:
    """按"聊天键"缓存空闲 DSH 会话（线程安全、LRU + TTL）。"""

    def __init__(
        self,
        *,
        max_entries: int = 4,
        ttl_seconds: float = 1800.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.max_entries = max(1, int(max_entries or 1))
        self.ttl_seconds = max(0.0, float(ttl_seconds or 0.0))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, AffinityEntry] = {}

    # -- 查询/写入 -------------------------------------------------------------
    def consume(
        self, key: str, messages: Sequence[Any]
    ) -> tuple[AffinityEntry | None, list[AffinityEntry]]:
        """取可复用条目；顺带返回被判失效的旧条目（调用方负责关闭会话）。"""
        if not key:
            return None, []
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None, []
            if self._expired(entry) or not self._continues(entry, messages):
                return None, [self._entries.pop(key)]
            entry.hits += 1
            entry.updated = self._clock()
            return entry, []

    def lookup(self, key: str, messages: Sequence[Any]) -> AffinityEntry | None:
        """同 :meth:`consume`，但只返回可复用条目（丢弃失效条目不负责关闭）。"""
        entry, _stale = self.consume(key, messages)
        return entry

    def store(
        self, key: str, session_id: str, messages: Sequence[Any]
    ) -> list[AffinityEntry]:
        """登记会话；返回被替换/淘汰的条目（调用方负责关闭它们）。"""
        if not key or not session_id:
            return []
        body = list(messages or [])
        if not body:
            return []
        entry = AffinityEntry(
            key=key,
            session_id=session_id,
            anchor_index=len(body),
            anchor_digest=message_digest(body[-1]),
            updated=self._clock(),
        )
        with self._lock:
            replaced = self._entries.get(key)
            self._entries[key] = entry
            evicted = [replaced] if replaced is not None else []
            evicted.extend(self._trim_locked())
        return evicted

    def pop(self, key: str) -> AffinityEntry | None:
        """移除并返回某聊天的会话（失效/失败路径）。"""
        with self._lock:
            return self._entries.pop(key, None)

    def pop_expired(self) -> list[AffinityEntry]:
        """取出所有已过期的条目（监控线程/预热时清理）。"""
        with self._lock:
            expired = [entry for entry in self._entries.values() if self._expired(entry)]
            for entry in expired:
                self._entries.pop(entry.key, None)
        return expired

    def clear(self) -> list[AffinityEntry]:
        """清空并返回全部条目（内核重启/关停时调用方负责关闭会话）。"""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        return entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[str]:
        """当前缓存的聊天键（诊断用）。"""
        with self._lock:
            return list(self._entries)

    def snapshot(self) -> dict[str, Any]:
        """状态快照（``describe``/``/metrics`` 用，不含会话内容）。"""
        with self._lock:
            entries = list(self._entries.values())
        return {
            "maxEntries": self.max_entries,
            "ttlSeconds": self.ttl_seconds,
            "entries": len(entries),
            "hits": sum(entry.hits for entry in entries),
        }

    # -- 内部 -----------------------------------------------------------------
    def _expired(self, entry: AffinityEntry) -> bool:
        if self.ttl_seconds <= 0:
            return False
        return self._clock() - entry.updated >= self.ttl_seconds

    def _continues(self, entry: AffinityEntry, messages: Sequence[Any]) -> bool:
        """``messages`` 是否正好是"上一轮 + 助手回复 + 新用户消息"。"""
        body = list(messages or [])
        expected = entry.anchor_index + 2
        if len(body) != expected:
            return False
        anchor = body[entry.anchor_index - 1]
        return message_digest(anchor) == entry.anchor_digest

    def _trim_locked(self) -> list[AffinityEntry]:
        """超上限时按 ``updated`` 淘汰最旧的（调用方已持锁）。"""
        if len(self._entries) <= self.max_entries:
            return []
        ordered = sorted(self._entries.values(), key=lambda item: item.updated)
        evicted: list[AffinityEntry] = []
        for entry in ordered[: len(self._entries) - self.max_entries]:
            self._entries.pop(entry.key, None)
            evicted.append(entry)
        return evicted
