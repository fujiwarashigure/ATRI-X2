"""进程内、按 Session 的编辑前内容备份（rollback）。

对应 Go：``internal/utils/fileops/rollback.go``。

每个文件只保留**最近一次**编辑前的状态；总数 1024 条、总量 256 MiB 为上限，
超出时淘汰最旧的记录。:meth:`RollbackStore.restore` 用 ``revision_after``
校验"自那次编辑后没人改过这个文件"，Shell 或外部程序的修改一律拒绝覆盖；
**新建文件同样先核对内容一致才删除**。

store 是进程内的：重启即丢失所有记录。

Session 身份由调用方以**鸭子类型**传入（只当字符串用），本模块不 import 任何
agent 模块。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from atri_x.errors import ErrRollbackConflict, ErrRollbackNotFound
from atri_x.utils.fileops.atomic_write import atomic_write_file
from atri_x.utils.fileops.text import content_revision

__all__ = [
    "ROLLBACK_MAX_BYTES",
    "ROLLBACK_MAX_RECORDS",
    "RollbackInfo",
    "RollbackStore",
    "new_rollback_store",
]

#: 进程内保留的编辑前内容上限。
ROLLBACK_MAX_BYTES: int = 256 * 1024 * 1024
#: 可回滚的文件数上限。
ROLLBACK_MAX_RECORDS = 1024


@dataclass
class RollbackInfo:
    """一条回滚记录的描述（不暴露文件内容）。对应 Go ``RollbackInfo``。"""

    id: int = 0
    path: str = ""
    created: bool = False
    edited_at: datetime | None = None
    revision_before: str = ""
    revision_after: str = ""
    bytes: int = 0


class _Record:
    __slots__ = ("before", "existed", "info", "mode", "session_id")

    def __init__(
        self,
        info: RollbackInfo,
        session_id: str,
        before: bytes,
        mode: int,
        existed: bool,
    ) -> None:
        self.info = info
        self.session_id = session_id
        self.before = before
        self.mode = mode
        self.existed = existed


class RollbackStore:
    """按 Session 隔离的回滚记录表。对应 Go ``RollbackStore``。"""

    def __init__(self, max_bytes: int = ROLLBACK_MAX_BYTES, max_records: int = ROLLBACK_MAX_RECORDS) -> None:
        self._lock = threading.Lock()
        self._scopes: dict[str, dict[str, _Record]] = {}
        self._records: dict[int, _Record] = {}
        self._next_id = 0
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_records = max_records

    # -- 记录 ---------------------------------------------------------------

    def record(
        self,
        session_id: str,
        path: str,
        before: bytes | None,
        mode: int,
        existed: bool,
        revision_before: str,
        revision_after: str,
    ) -> tuple[RollbackInfo, bool]:
        """登记一次成功编辑的编辑前状态；同一文件的新记录替换旧记录。

        返回 ``(info, available)``；``available`` 为假表示这条记录自己就被
        淘汰上限挤掉了。
        """
        session_id = (session_id or "").strip()
        path = (path or "").strip()
        if session_id == "" or path == "":
            return RollbackInfo(), False
        payload = bytes(before or b"")
        with self._lock:
            scope = self._scopes.setdefault(session_id, {})
            previous = scope.get(path)
            if previous is not None:
                self._remove_locked(previous)
            self._next_id += 1
            info = RollbackInfo(
                id=self._next_id,
                path=path,
                created=not existed,
                edited_at=datetime.now(timezone.utc),
                revision_before=revision_before,
                revision_after=revision_after,
                bytes=len(payload),
            )
            record = _Record(info, session_id, payload, mode, existed)
            self._records[record.info.id] = record
            scope[path] = record
            self._bytes += record.info.bytes
            available = True
            while self._bytes > self._max_bytes or len(self._records) > self._max_records:
                if not self._evict_oldest_locked():
                    break
                if record.info.id not in self._records:
                    available = False
            return info, available

    def list(self, session_id: str) -> list[RollbackInfo]:
        """返回某个 Session 的记录，按年龄排序（旧→新，与 Go 一致）。"""
        with self._lock:
            scope = self._scopes.get((session_id or "").strip())
            out = [record.info for record in (scope or {}).values()]
        out.sort(key=lambda info: info.id)
        return out

    def get(self, session_id: str, record_id: int) -> tuple[RollbackInfo, bool]:
        """按编号取一条记录。"""
        with self._lock:
            record = self._lookup_locked(session_id, record_id)
            if record is None:
                return RollbackInfo(), False
            return record.info, True

    def forget(self, session_id: str) -> None:
        """丢弃某个 Session 的全部记录（例如切走该 Session）。"""
        session_id = (session_id or "").strip()
        with self._lock:
            scope = self._scopes.get(session_id)
            for record in list((scope or {}).values()):
                self._remove_locked(record)
            self._scopes.pop(session_id, None)

    # -- 回滚 ---------------------------------------------------------------

    def restore(self, ctx, session_id: str, record_id: int) -> RollbackInfo:
        """把记录的内容放回去并消费该记录。

        - 文件自那次编辑后又被改动（Shell / 其它程序 / 外部编辑器）→
          :data:`atri_x.errors.ErrRollbackConflict`；
        - 记录不存在、已被消费、被更新的编辑替换或已被淘汰 →
          :data:`atri_x.errors.ErrRollbackNotFound`。
        """
        if ctx is not None and ctx.is_cancelled():
            raise ctx.error() if ctx.error() is not None else ErrRollbackNotFound
        with self._lock:
            record = self._lookup_locked(session_id, record_id)
        if record is None:
            raise ErrRollbackNotFound

        existed = record.existed
        try:
            with open(record.info.path, "rb") as handle:
                current = handle.read()
        except FileNotFoundError as exc:
            raise ErrRollbackNotFound from exc
        except OSError as exc:
            raise OSError(f"read file before rollback: {exc}") from exc
        if content_revision(current) != record.info.revision_after:
            raise ErrRollbackConflict
        if existed:
            mode = record.mode or 0o644
            try:
                atomic_write_file(record.info.path, record.before, mode)
            except OSError as exc:
                raise OSError(f"write file before rollback: {exc}") from exc
        else:
            try:
                os.remove(record.info.path)
            except OSError as exc:
                raise OSError(f"remove created file: {exc}") from exc

        with self._lock:
            self._remove_locked(record)
        return record.info

    # -- 内部 ---------------------------------------------------------------

    def _lookup_locked(self, session_id: str, record_id: int) -> _Record | None:
        scope = self._scopes.get((session_id or "").strip())
        if not scope:
            return None
        record = self._records.get(record_id)
        if record is None:
            return None
        if scope.get(record.info.path) is not record:
            return None
        return record

    def _evict_oldest_locked(self) -> bool:
        oldest: _Record | None = None
        for record in self._records.values():
            if oldest is None or record.info.id < oldest.info.id:
                oldest = record
        if oldest is None:
            return False
        self._remove_locked(oldest)
        return True

    def _remove_locked(self, record: _Record) -> None:
        self._records.pop(record.info.id, None)
        scope = self._scopes.get(record.session_id)
        if scope is not None:
            if scope.get(record.info.path) is record:
                del scope[record.info.path]
            if not scope:
                del self._scopes[record.session_id]
        self._bytes -= record.info.bytes
        if self._bytes < 0:
            self._bytes = 0


def new_rollback_store() -> RollbackStore:
    """对应 Go ``fileops.NewRollbackStore``。"""
    return RollbackStore()
