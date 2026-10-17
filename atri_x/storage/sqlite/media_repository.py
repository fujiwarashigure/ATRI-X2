"""媒体与媒体引用仓储。对应 Go：``internal/storage/sqlite/media_repository.go``。

引用事实的**唯一来源**是 ``media_references`` 表：本仓不维护整数引用计数，
"还有没有人引用"永远现查（Go 同样是 ``NOT EXISTS (SELECT 1 FROM media_references ...)``）。
同步靠"事务 + v13/v14/v16 的触发器"：owner 行被删、``segments`` 被改、
``media_history`` / ``media_outputs`` / cron 投递 JSON 被改，引用都会跟着走。

``MediaRepository`` 的方法按 Go 的三文件拆分布在不同模块里，这里用 mixin 组合：

* :mod:`atri_x.storage.sqlite.media_history` —— ``media_history`` 表（历史消息里的媒体位置）；
* :mod:`atri_x.storage.sqlite.media_lifecycle` —— 认领清理、输出索引、启动恢复、只读审计。

本模块保留 Go ``media_repository.go`` 的内容：``Get`` / ``Upsert`` / ``DeleteOrphans``
与 ``MediaReferenceRepository``。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from atri_x.errors import ErrNotFound
from atri_x.storage.models import Media, MediaReference
from atri_x.storage.sqlite.media_history import MediaHistoryMixin
from atri_x.storage.sqlite.media_lifecycle import MediaLifecycleMixin
from atri_x.storage.sqlite.store import null_string, soft_time, text_of
from atri_x.storage.times import format_time, is_zero_time, now

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["MediaReferenceRepository", "MediaRepository"]

# Get 读全部列（含 deleting / expires_at）。
MEDIA_COLUMNS = """id, name, mime_type, size, local_path, backend, object_key, source_platform,
    source_url, source_file_id, created_at, last_accessed_at, expires_at, deleting"""

# 写入列（Go 的 Upsert 不写 deleting，它由清理流程维护）。
MEDIA_WRITE_COLUMNS = """id, name, mime_type, size, local_path, backend, object_key, source_platform,
    source_url, source_file_id, created_at, last_accessed_at, expires_at"""

# DeleteOrphans 只读这些列（Go 原样：不读 deleting，且**不**回填 expires_at）。
ORPHAN_MEDIA_COLUMNS = """id, name, mime_type, size, local_path, backend, object_key, source_platform,
    source_url, source_file_id, created_at, last_accessed_at, expires_at"""


class MediaRepository(MediaHistoryMixin, MediaLifecycleMixin):
    """媒体仓储。对应 Go ``sqlite.MediaRepository``（三个 Go 文件里的方法合起来）。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    def get(self, id: str) -> Media:
        """按主键取媒体；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(f"SELECT {MEDIA_COLUMNS} FROM media WHERE id = ?", (id,))
        if row is None:
            raise ErrNotFound
        return _scan_media(row, with_deleting=True)

    def upsert(self, media: Media) -> None:
        """插入或更新一行媒体（``deleting=1`` 的行**不**被覆盖）。

        Go 的 ``ON CONFLICT ... DO UPDATE ... WHERE media.deleting = 0``：正在被清理的
        媒体不会被迟到的 upsert 复活；``created_at`` 在冲突时保持不变。
        """
        if is_zero_time(media.created_at):
            media.created_at = now()
        if is_zero_time(media.last_accessed_at):
            media.last_accessed_at = media.created_at
        self._db.execute(
            f"""
INSERT INTO media ({MEDIA_WRITE_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    mime_type = excluded.mime_type,
    size = excluded.size,
    local_path = excluded.local_path,
    backend = excluded.backend,
    object_key = excluded.object_key,
    source_platform = excluded.source_platform,
    source_url = excluded.source_url,
    source_file_id = excluded.source_file_id,
    last_accessed_at = excluded.last_accessed_at,
    expires_at = excluded.expires_at
WHERE media.deleting = 0""",
            (
                media.id,
                null_string(media.name),
                media.mime_type,
                media.size,
                null_string(media.local_path),
                media.backend,
                null_string(media.object_key),
                null_string(media.source_platform),
                null_string(media.source_url),
                null_string(media.source_file_id),
                format_time(media.created_at),
                format_time(media.last_accessed_at),
                _nullable_time(media.expires_at),
            ),
        )

    def delete_orphans(self, cutoff: datetime) -> list[Media]:
        """列出 ``created_at`` 早于 cutoff 且没有任何引用的媒体。

        命名沿用 Go ``DeleteOrphans``，但**它只查不删**：真正的删除由调用方在
        后端对象清理成功之后走 :meth:`finish_delete`。返回行不含 ``expires_at``
        （Go 的 scan 没有把它写回结构体，本仓保持同样的返回形状）。
        """
        rows = self._db.query(
            f"""
SELECT {ORPHAN_MEDIA_COLUMNS}
FROM media
WHERE created_at < ?
  AND NOT EXISTS (SELECT 1 FROM media_references WHERE media_references.media_id = media.id)""",
            (format_time(cutoff),),
        )
        return [_scan_media(row, with_deleting=False) for row in rows]


class MediaReferenceRepository:
    """媒体引用仓储。对应 Go ``sqlite.MediaReferenceRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    def add(self, reference: MediaReference) -> None:
        """新增一条引用（重复主键按幂等处理；媒体正在被清理时由触发器拒绝）。"""
        if is_zero_time(reference.created_at):
            reference.created_at = now()
        self.add_all([reference])

    def add_all(self, references: Sequence[MediaReference]) -> None:
        """批量新增引用（单事务）。空列表直接返回。"""
        if not references:
            return
        with self._db.with_transaction() as tx:
            for reference in references:
                if is_zero_time(reference.created_at):
                    reference.created_at = now()
                tx.execute(
                    """
INSERT INTO media_references (media_id, owner_type, owner_id, purpose, session_id, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(media_id, owner_type, owner_id, purpose) DO NOTHING""",
                    (
                        reference.media_id,
                        reference.owner_type,
                        reference.owner_id,
                        reference.purpose,
                        null_string(reference.session_id),
                        format_time(reference.created_at),
                    ),
                )

    def remove(self, reference: MediaReference) -> None:
        """删除一条引用（按主键四元组匹配）。"""
        self._db.execute(
            """
DELETE FROM media_references
WHERE media_id = ? AND owner_type = ? AND owner_id = ? AND purpose = ?""",
            (reference.media_id, reference.owner_type, reference.owner_id, reference.purpose),
        )

    def list_by_owner(self, owner_type: str, owner_id: str) -> list[MediaReference]:
        """列出某个 owner 的全部引用。"""
        return self._list("owner_type = ? AND owner_id = ?", (owner_type, owner_id))

    def list_media_ids(self, media_id: str) -> list[MediaReference]:
        """列出某个媒体的全部引用。"""
        return self._list("media_id = ?", (media_id,))

    def _list(self, where: str, args: Sequence[object]) -> list[MediaReference]:
        rows = self._db.query(
            f"""
SELECT media_id, owner_type, owner_id, purpose, session_id, created_at
FROM media_references
WHERE {where}""",
            args,
        )
        return [
            MediaReference(
                media_id=row["media_id"],
                owner_type=row["owner_type"],
                owner_id=row["owner_id"],
                purpose=row["purpose"],
                session_id=text_of(row["session_id"]),
                created_at=soft_time(row["created_at"]),
            )
            for row in rows
        ]


def _scan_media(row, *, with_deleting: bool) -> Media:
    """媒体行 → :class:`Media`。

    ``created_at`` / ``last_accessed_at`` 用 :func:`soft_time`：Go 在这里是
    ``m.CreatedAt, _ = storage.ParseTime(...)``（解析失败只拿到零值时间，不报错）。
    """
    media = Media(
        id=row["id"],
        name=text_of(row["name"]),
        mime_type=row["mime_type"],
        size=int(row["size"]),
        local_path=text_of(row["local_path"]),
        backend=row["backend"],
        object_key=text_of(row["object_key"]),
        source_platform=text_of(row["source_platform"]),
        source_url=text_of(row["source_url"]),
        source_file_id=text_of(row["source_file_id"]),
        created_at=soft_time(row["created_at"]),
        last_accessed_at=soft_time(row["last_accessed_at"]),
    )
    if with_deleting:
        media.deleting = bool(row["deleting"])
        media.expires_at = soft_time(row["expires_at"])
    return media


def _nullable_time(value: datetime | None):
    """``nullableTime``：``None`` 写 NULL，否则写 RFC3339 文本。"""
    if value is None:
        return None
    return format_time(value)
