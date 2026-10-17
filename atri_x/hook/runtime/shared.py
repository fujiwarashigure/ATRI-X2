"""Hook 共享的有界 JSON 协调存储。

对应 Go：``internal/hook/runtime/shared.go``。

:class:`SharedState` 是**进程内**存储：它跨 Hook 重启存活，但不跨 ATRI-X
进程重启（与 Go 一致）。值一律是 JSON，键有长度上限、总量有 LRU 淘汰。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.errors import AtriXError
from atri_x.hook.protocol.eventjson import RawJSON, dumps

#: 键的字节上限。对应 Go ``maxSharedKeyBytes``。
MAX_SHARED_KEY_BYTES = 256
#: 单个值的字节上限（1 MiB）。对应 Go ``maxSharedValueBytes``。
MAX_SHARED_VALUE_BYTES = 1 << 20
#: 存储总量上限（32 MiB）。对应 Go ``maxSharedStateBytes``。
MAX_SHARED_STATE_BYTES = 32 << 20
#: 条目数上限。对应 Go ``maxSharedStateEntries``。
MAX_SHARED_STATE_ENTRIES = 10_000
#: 默认 TTL（10 分钟）。对应 Go ``defaultSharedTTL``。
DEFAULT_SHARED_TTL = 600.0
#: 过期清理间隔（1 分钟）。对应 Go ``sharedCleanupInterval``。
SHARED_CLEANUP_INTERVAL = 60.0
#: TTL 秒数上限。对应 Go ``maxSharedTTLSeconds`` = (1<<63-1)/1e9。
MAX_SHARED_TTL_SECONDS = (1 << 63) - 1 // 1_000_000_000

__all__ = [
    "SharedState",
    "SharedStateError",
    "MAX_SHARED_KEY_BYTES",
    "MAX_SHARED_VALUE_BYTES",
    "MAX_SHARED_STATE_BYTES",
    "MAX_SHARED_STATE_ENTRIES",
    "DEFAULT_SHARED_TTL",
    "SHARED_CLEANUP_INTERVAL",
    "MAX_SHARED_TTL_SECONDS",
]


class SharedStateError(AtriXError):
    """共享存储的操作错误。对应 Go 里这一层的 ``fmt.Errorf``。"""


@dataclass
class SharedEntry:
    """一条共享条目。对应 Go ``sharedEntry``。"""

    value: str = ""
    ttl: float = 0.0
    last_access: datetime | None = None

    def expired(self, now: datetime) -> bool:
        """对应 Go ``sharedEntry.expired``。"""
        if self.ttl <= 0 or self.last_access is None:
            return False
        return not now < self.last_access + timedelta(seconds=self.ttl)


class SharedState:
    """进程内的 JSON 协调存储。对应 Go ``SharedState``。

    并发说明：Go 用 ``sync.RWMutex``，这里用 ``threading.RLock``（与
    ``atri_x.hook.manager`` 的既有做法一致）；所有临界区都不含 ``await``。
    """

    def __init__(self, now: Any = None) -> None:
        self._mu = threading.RLock()
        self._items: dict[str, SharedEntry] = {}
        self._size = 0
        self._max_entries = MAX_SHARED_STATE_ENTRIES
        self._max_size = MAX_SHARED_STATE_BYTES
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- 请求分发 -----------------------------------------------------------
    def handle_request(self, method: str, params: Any = None) -> Any:
        """处理一个 ``shared.*`` 请求。对应 Go ``HandleRequest``。"""
        values = _object(params)
        if method == "shared.get":
            value, ok = self.get(_str(values.get("key")))
            return {"found": ok, "value": RawJSON(value) if ok and value else None}
        if method == "shared.set":
            ttl = shared_ttl(_optional_int(values.get("ttl_seconds")))
            self.set_with_ttl(
                _str(values.get("key")), _raw(values.get("value")), ttl
            )
            return {"ok": True}
        if method == "shared.delete":
            return {"deleted": self.delete(_str(values.get("key")))}
        if method == "shared.list":
            return {"keys": self.list(_str(values.get("prefix")))}
        if method == "shared.compare_and_swap":
            ttl = shared_ttl(_optional_int(values.get("ttl_seconds")))
            swapped = self.compare_and_swap_with_ttl(
                _str(values.get("key")),
                _raw(values.get("expected")),
                _raw(values.get("value")),
                ttl,
            )
            return {"swapped": swapped}
        raise SharedStateError(f"unsupported hook shared method {method!r}")

    # -- 基本操作 -----------------------------------------------------------
    def get(self, key: str) -> tuple[str | None, bool]:
        """取一个值。对应 Go ``Get``。"""
        key = (key or "").strip()
        now = self._current_time()
        with self._mu:
            entry = self._items.get(key)
            if entry is None:
                return None, False
            if entry.expired(now):
                self._delete_locked(key, entry)
                return None, False
            entry.last_access = now
            self._items[key] = entry
            return entry.value, True

    def set(self, key: str, value: Any) -> None:
        """写入并套用默认 TTL。对应 Go ``Set``。"""
        self.set_with_ttl(key, value, DEFAULT_SHARED_TTL)

    def set_with_ttl(self, key: str, value: Any, ttl: float) -> None:
        """写入并指定 TTL。对应 Go ``SetWithTTL``。"""
        key, normalized = normalize_shared_value(key, value)
        if ttl < 0:
            raise SharedStateError("shared ttl must not be negative")
        now = self._current_time()
        entry = SharedEntry(value=normalized, ttl=ttl, last_access=now)
        with self._mu:
            self._prune_expired_locked(now)
            self._make_room_locked(key, entry)
            previous = self._items.get(key)
            if previous is not None:
                self._size -= shared_entry_size(key, previous)
            self._items[key] = entry
            self._size += shared_entry_size(key, entry)

    def delete(self, key: str) -> bool:
        """删除一个键，返回它此前是否存在且未过期。对应 Go ``Delete``。"""
        key = (key or "").strip()
        now = self._current_time()
        with self._mu:
            entry = self._items.get(key)
            if entry is None:
                return False
            self._delete_locked(key, entry)
            return not entry.expired(now)

    def list(self, prefix: str = "") -> list[str]:
        """列出前缀匹配的未过期键（已排序）。对应 Go ``List``。"""
        prefix = (prefix or "").strip()
        now = self._current_time()
        with self._mu:
            self._prune_expired_locked(now)
            keys = [
                key
                for key in self._items
                if not prefix or key.startswith(prefix)
            ]
        keys.sort()
        return keys

    # -- 比较并交换 ---------------------------------------------------------
    def compare_and_swap(self, key: str, expected: Any, value: Any) -> bool:
        """默认 TTL 的 CAS。对应 Go ``CompareAndSwap``。"""
        return self.compare_and_swap_with_ttl(key, expected, value, DEFAULT_SHARED_TTL)

    def compare_and_swap_with_ttl(
        self, key: str, expected: Any, value: Any, ttl: float
    ) -> bool:
        """``expected`` 为空表示"键必须不存在"。对应 Go ``CompareAndSwapWithTTL``。"""
        key, normalized = normalize_shared_value(key, value)
        if ttl < 0:
            raise SharedStateError("shared ttl must not be negative")
        expected_text = ""
        if expected is not None and str(expected).strip() != "":
            expected_text = compact_json(expected)
            if expected_text is None:
                raise SharedStateError("shared expected value must be valid JSON")
        now = self._current_time()
        with self._mu:
            current = self._items.get(key)
            exists = current is not None
            if current is not None and current.expired(now):
                self._delete_locked(key, current)
                current, exists = None, False
            if not expected_text:
                if exists:
                    return False
            elif current is None or current.value != expected_text:
                return False
            entry = SharedEntry(value=normalized, ttl=ttl, last_access=now)
            self._make_room_locked(key, entry)
            if current is not None:
                self._size -= shared_entry_size(key, current)
            self._items[key] = entry
            self._size += shared_entry_size(key, entry)
            return True

    # -- 清理 ---------------------------------------------------------------
    def prune_expired(self) -> int:
        """清掉全部过期条目，返回条数。对应 Go ``PruneExpired``。"""
        now = self._current_time()
        with self._mu:
            return self._prune_expired_locked(now)

    # -- 内部 ---------------------------------------------------------------
    def _make_room_locked(self, key: str, incoming: SharedEntry) -> None:
        if shared_entry_size(key, incoming) > self._max_size:
            raise SharedStateError(
                f"hook shared state entry exceeds {self._max_size} byte limit"
            )
        current = self._items.get(key)
        current_size = shared_entry_size(key, current) if current is not None else 0
        incoming_size = shared_entry_size(key, incoming)
        exists = current is not None

        def fits() -> bool:
            count = len(self._items) + (0 if exists else 1)
            return (
                count <= self._max_entries
                and self._size - current_size + incoming_size <= self._max_size
            )

        if fits():
            return
        candidates = sorted(
            (
                (entry.last_access or self._current_time(), other)
                for other, entry in self._items.items()
                if other != key
            ),
            key=lambda item: (item[0], item[1]),
        )
        for _stamp, other in candidates:
            entry = self._items.get(other)
            if entry is None:
                continue
            self._delete_locked(other, entry)
            if fits():
                return
        raise SharedStateError(f"hook shared state cannot make room for key {key!r}")

    def _prune_expired_locked(self, now: datetime) -> int:
        removed = 0
        for key, entry in list(self._items.items()):
            if entry.expired(now):
                self._delete_locked(key, entry)
                removed += 1
        return removed

    def _delete_locked(self, key: str, entry: SharedEntry) -> None:
        self._items.pop(key, None)
        self._size -= shared_entry_size(key, entry)

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def shared_entry_size(key: str, entry: SharedEntry) -> int:
    """条目占用的字节数。对应 Go ``sharedEntrySize``（Go 的 len 是字节）。"""
    return len(key.encode("utf-8")) + len(entry.value.encode("utf-8"))


def normalize_shared_value(key: str, value: Any) -> tuple[str, str]:
    """校验并压缩一个共享值。对应 Go ``normalizeSharedValue``。"""
    key = (key or "").strip()
    if not key:
        raise SharedStateError("shared key must not be empty")
    if len(key.encode("utf-8")) > MAX_SHARED_KEY_BYTES:
        raise SharedStateError(
            f"shared key exceeds {MAX_SHARED_KEY_BYTES} byte limit"
        )
    compact = compact_json(value)
    if compact is None:
        raise SharedStateError("shared value must be valid JSON")
    if len(compact.encode("utf-8")) > MAX_SHARED_VALUE_BYTES:
        raise SharedStateError(
            f"shared value exceeds {MAX_SHARED_VALUE_BYTES >> 20} MiB limit"
        )
    return key, compact


def compact_json(value: Any) -> str | None:
    """把值规范成紧凑 JSON 文本；非法时返回 ``None``。对应 Go ``json.Compact``。"""
    if value is None:
        return None
    if isinstance(value, RawJSON):
        text = str(value)
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return dumps(parsed)
    if isinstance(value, (bytes, bytearray)):
        return compact_json(bytes(value).decode("utf-8", "replace"))
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return dumps(parsed)
    try:
        return dumps(value)
    except (TypeError, ValueError):
        return None


def shared_ttl(ttl_seconds: int | None) -> float:
    """把 ``ttl_seconds`` 换成秒；``None`` 表示默认 TTL。对应 Go ``sharedTTL``。"""
    if ttl_seconds is None:
        return DEFAULT_SHARED_TTL
    if ttl_seconds < 0:
        raise SharedStateError("ttl_seconds must not be negative")
    if ttl_seconds > MAX_SHARED_TTL_SECONDS:
        raise SharedStateError("ttl_seconds is too large")
    return float(ttl_seconds)


def _object(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    if isinstance(params, (bytes, bytearray, str)):
        text = (
            bytes(params).decode("utf-8", "replace")
            if isinstance(params, (bytes, bytearray))
            else params
        )
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise SharedStateError(f"decode shared request: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SharedStateError("shared request must be a JSON object")
        return parsed
    raise SharedStateError("shared request must be a JSON object")


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _raw(value: Any) -> Any:
    """把 JSON 值转成 :class:`RawJSON`，便于后续压缩/比较。"""
    if value is None:
        return None
    if isinstance(value, RawJSON):
        return value
    if isinstance(value, str):
        return RawJSON(value)
    return dumps(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SharedStateError("ttl_seconds must be a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise SharedStateError("ttl_seconds must be a number")
