"""历史消息媒体位置。对应 Go：``internal/storage/sqlite/media_history.go``。

``MediaRepository`` 的这部分方法在 Go 里也在 ``MediaRepository`` 上，只是源码分在
另一个文件；本仓用 :class:`MediaHistoryMixin` 承载，由
:class:`atri_x.storage.sqlite.media_repository.MediaRepository` 组合（方法名与语义不变）。

关键语义：每次 :meth:`MediaHistoryMixin.save_history` 都**换一个新的 owner_id**
（``storage.NewID()``）。旧行先删、新行再插，都在一个事务里；因为 owner 每次都是新的，
迟到的对账（按旧 owner_id 删除）不可能误删新关联。删旧行会触发
``media_history_removed`` 释放引用，插新行会触发 ``media_history_added`` 建引用 ——
所以"引用"这件事不需要这里手写 SQL。

> 与 Go 的可见差异：Go 的 ``SaveHistory(ctx, item storage.HistoryMedia)`` 按值传参，
> 新 owner_id 只存在于函数内；本仓把生成的 ``owner_id`` **回填**到传入的
> :class:`HistoryMedia` 上（调用方因此能拿到它）。其余字段与落库结果完全一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atri_x.errors import InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import HistoryMedia

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["MediaHistoryMixin"]

_HISTORY_KINDS = ("image", "file", "record")


class MediaHistoryMixin:
    """``media_history`` 表（迁移 v16）的读写。"""

    _db: "SQLiteDatabase"

    def save_history(self, item: HistoryMedia) -> None:
        """写入一条历史媒体位置（同一 ``media_index`` 覆盖旧行，事务内原子替换）。"""
        if (
            not item.history_id
            or not item.platform
            or not item.scope_id
            or not item.message_id
            or item.media_index < 1
            or not item.media_id
        ):
            raise InvalidError("history media requires message identity, positive media index and media ID")
        if item.kind not in _HISTORY_KINDS:
            raise InvalidError(f'invalid history media kind "{item.kind}"')
        # 每次替换换一个新 owner，迟到的对账删不掉更新的关联。
        item.owner_id = new_id()
        with self._db.with_transaction() as tx:
            tx.execute(
                """
DELETE FROM media_history
WHERE platform = ? AND scope_id = ? AND message_id = ? AND media_index = ?""",
                (item.platform, item.scope_id, item.message_id, item.media_index),
            )
            tx.execute(
                """
INSERT INTO media_history(history_id, platform, scope_id, message_id, media_index, kind, media_id, owner_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.history_id,
                    item.platform,
                    item.scope_id,
                    item.message_id,
                    item.media_index,
                    item.kind,
                    item.media_id,
                    item.owner_id,
                ),
            )

    def find_history(self, platform: str, scope_id: str, message_id: str) -> list[HistoryMedia]:
        """按历史消息查媒体位置，按 ``media_index`` 升序。"""
        return self._query_history(
            "WHERE platform = ? AND scope_id = ? AND message_id = ? ORDER BY media_index",
            (platform, scope_id, message_id),
        )

    def list_history(self, after_owner_id: str, limit: int) -> list[HistoryMedia]:
        """按 ``owner_id`` 分页遍历历史媒体位置（跨库对账用）。"""
        return self._query_history("WHERE owner_id > ? ORDER BY owner_id LIMIT ?", (after_owner_id, limit))

    def delete_history(self, owner_id: str) -> None:
        """按 ``owner_id`` 删除历史媒体位置（触发器随即释放引用）。"""
        self._db.execute("DELETE FROM media_history WHERE owner_id = ?", (owner_id,))

    def _query_history(self, suffix: str, args: tuple[object, ...]) -> list[HistoryMedia]:
        rows = self._db.query(
            f"""
SELECT history_id, platform, scope_id, message_id, media_index, kind, media_id, owner_id
FROM media_history {suffix}""",
            args,
        )
        return [
            HistoryMedia(
                history_id=row["history_id"],
                platform=row["platform"],
                scope_id=row["scope_id"],
                message_id=row["message_id"],
                media_index=int(row["media_index"]),
                kind=row["kind"],
                media_id=row["media_id"],
                owner_id=row["owner_id"],
            )
            for row in rows
        ]
