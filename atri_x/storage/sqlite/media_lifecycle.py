"""媒体清理认领、输出索引、启动恢复与只读审计。对应 Go：``internal/storage/sqlite/media_lifecycle.go``。

Go 里的这些方法同样挂在 ``MediaRepository`` 上，本仓用 :class:`MediaLifecycleMixin` 承载
（方法名与语义不变），由
:class:`atri_x.storage.sqlite.media_repository.MediaRepository` 组合。

清理认领的并发要点（与 Go 逐行对齐）：

* :meth:`MediaLifecycleMixin.claim_orphans` 是一条 ``UPDATE ... RETURNING id``：
  **单条 SQLite 写语句**同时完成"标记 ``deleting=1``"和"读回被认领的 ID"，
  因此它与所有引用插入串行化。已经在 ``deleting=1`` 的行也会被再次返回
  （``deleting=1 OR (...)``），让重启后的清理能接着做完。
* ``media_reference_guard`` 触发器保证认领之后新引用插不进来（``RAISE(ABORT)`）——
  调用方要么在认领前拿到引用，要么拿不到并且知道媒体正在被删。
* :meth:`MediaLifecycleMixin.touch` 拒绝 ``deleting=1`` 的行（对应 Go 的
  ``media unavailable or being deleted``，本仓抛 :class:`atri_x.errors.DisabledError`）。
* :meth:`MediaLifecycleMixin.delete_orphans` 在 Go 里叫 ``DeleteOrphans`` 但**只查不删**；
  真正的删除是 :meth:`MediaLifecycleMixin.finish_delete`，命名沿用上游以免交叉检索错位。

> 与 Go 的可见差异：Go 的 ``SaveOutput(ctx, out storage.MediaOutput)`` 按值传参，
> 自动生成的 ``owner_id`` 只存在于函数内；本仓把它**回填**到传入的 :class:`MediaOutput` 上。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from atri_x.errors import DisabledError, InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import Media, MediaOutput
from atri_x.storage.times import format_time, parse_time
from atri_x.storage.times import now as current_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["MediaLifecycleMixin"]


class MediaLifecycleMixin:
    """媒体生命周期：孤儿认领、清理收尾、输出缓存、启动恢复、审计。"""

    _db: "SQLiteDatabase"

    # -- 清理 --------------------------------------------------------------

    def claim_orphans(self, cutoff: datetime) -> list[Media]:
        """认领孤儿媒体（先标记 ``deleting`` 再返回），供可重试后端清理。"""
        rows = self._db.query(
            """
UPDATE media SET deleting = 1 WHERE deleting = 1 OR
(orphaned_at IS NOT NULL AND julianday(orphaned_at) <= julianday(?) AND NOT EXISTS(SELECT 1 FROM media_references WHERE media_id = media.id))
RETURNING id""",
            (format_time(cutoff),),
        )
        # list[Media] 由 get 逐个补全（Go 也是认领后再逐个 Get）。
        return [self.get(row["id"]) for row in rows]

    def finish_delete(self, id: str) -> None:
        """清理完成后删除媒体行（只删已认领且已无引用的行）。"""
        self._db.execute(
            """
DELETE FROM media
WHERE id = ? AND deleting = 1
  AND NOT EXISTS (SELECT 1 FROM media_references WHERE media_id = media.id)""",
            (id,),
        )

    def touch(self, id: str, now: datetime) -> None:
        """更新 ``last_accessed_at``；媒体不存在或正在被清理时抛 :class:`atri_x.errors.DisabledError`。

        孤儿时间戳的处理与 Go 一致：本来就有 ``orphaned_at`` 的行把它一起推到本次访问时间
        （续上宽限期），没有 ``orphaned_at`` 的行保持 NULL。
        """
        cursor = self._db.execute(
            """
UPDATE media
SET last_accessed_at = ?, orphaned_at = CASE WHEN orphaned_at IS NOT NULL THEN ? ELSE NULL END
WHERE id = ? AND deleting = 0""",
            (format_time(now), format_time(now), id),
        )
        if cursor.rowcount == 0:
            raise DisabledError("media unavailable or being deleted")

    def recover_interrupted(self) -> None:
        """启动恢复：清掉进程中断留下的临时引用，并把半途的 Atrinis 事件标失败。"""
        with self._db.with_transaction() as tx:
            tx.execute(
                """
DELETE FROM media_references
WHERE purpose = 'temporary' AND owner_type IN ('hook', 'skill', 'request', 'output_send')"""
            )
            tx.execute(
                """
UPDATE atrinis_events
SET status = 'failed', error = 'interrupted before durable report', updated_at = ?
WHERE status IN ('accepted', 'queued', 'running')""",
                (format_time(current_time()),),
            )

    # -- 发送输出索引 -------------------------------------------------------

    def save_output(self, output: MediaOutput) -> None:
        """写入一条发送输出索引（同一 ``segment_index`` 覆盖旧行）。"""
        if (
            not output.platform
            or not output.scope_id
            or not output.message_id
            or output.segment_index < 0
            or not output.kind
            or not output.media_id
        ):
            raise InvalidError(
                "output media requires platform, scope, message ID, segment index, kind and media ID"
            )
        if not output.owner_id:
            output.owner_id = new_id()
        with self._db.with_transaction() as tx:
            tx.execute(
                """
DELETE FROM media_outputs
WHERE platform = ? AND scope_id = ? AND message_id = ? AND segment_index = ?""",
                (output.platform, output.scope_id, output.message_id, output.segment_index),
            )
            tx.execute(
                """
INSERT INTO media_outputs(platform, scope_id, message_id, segment_index, kind, media_id, owner_id, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    output.platform,
                    output.scope_id,
                    output.message_id,
                    output.segment_index,
                    output.kind,
                    output.media_id,
                    output.owner_id,
                    format_time(output.expires_at),
                ),
            )

    def find_outputs(self, platform: str, scope_id: str, message_id: str, now: datetime) -> list[MediaOutput]:
        """按发送消息查未过期的输出索引（``segment_index`` 升序）。"""
        rows = self._db.query(
            """
SELECT segment_index, kind, media_id, owner_id, expires_at
FROM media_outputs
WHERE platform = ? AND scope_id = ? AND message_id = ? AND julianday(expires_at) > julianday(?)
ORDER BY segment_index""",
            (platform, scope_id, message_id, format_time(now)),
        )
        return [
            MediaOutput(
                platform=platform,
                scope_id=scope_id,
                message_id=message_id,
                segment_index=int(row["segment_index"]),
                kind=row["kind"],
                media_id=row["media_id"],
                owner_id=row["owner_id"],
                expires_at=parse_time(row["expires_at"]),
            )
            for row in rows
        ]

    def expire_outputs(self, now: datetime) -> None:
        """清理已过期的输出索引（触发器随即释放 ``output`` 引用）。"""
        self._db.execute(
            "DELETE FROM media_outputs WHERE julianday(expires_at) <= julianday(?)",
            (format_time(now),),
        )

    # -- 只读审计 -----------------------------------------------------------

    def check_references(self) -> list[str]:
        """只读一致性检查，返回有问题的行描述（不改、不删任何东西）。"""
        rows = self._db.query(
            """
 SELECT 'missing message reference: '||m.id FROM messages m,json_each(CASE WHEN json_valid(m.segments) THEN m.segments ELSE '[]' END) s
 WHERE COALESCE(json_extract(s.value,'$.media'),'')<>'' AND NOT EXISTS
 (SELECT 1 FROM media_references r WHERE r.media_id=json_extract(s.value,'$.media') AND r.owner_id=m.id AND r.owner_type=CASE WHEN m.role='tool' THEN 'tool_result' ELSE 'message' END)
 UNION ALL SELECT 'missing history reference: '||h.owner_id FROM media_history h WHERE NOT EXISTS(SELECT 1 FROM media_references r WHERE r.media_id=h.media_id AND r.owner_type='chat_history' AND r.owner_id=h.owner_id AND r.purpose='content')
 UNION ALL SELECT 'missing output reference: '||o.owner_id FROM media_outputs o WHERE NOT EXISTS(SELECT 1 FROM media_references r WHERE r.media_id=o.media_id AND r.owner_type='output' AND r.owner_id=o.owner_id)
 UNION ALL SELECT 'missing report reference: '||d.id FROM atrinis_report_deliveries d JOIN atrinis_events e ON e.id=d.event_id WHERE e.status<>'completed' AND COALESCE(json_extract(d.output,'$.Source.media'),'')<>'' AND NOT EXISTS(SELECT 1 FROM media_references r WHERE r.owner_type='atrinis_report' AND r.owner_id=d.id AND r.media_id=json_extract(d.output,'$.Source.media'))

 UNION ALL SELECT 'missing cron reference: '||c.id FROM cron_jobs c,json_each(CASE WHEN json_valid(c.delivery_state) THEN c.delivery_state ELSE '{}' END,'$.report_segments') s
 WHERE COALESCE(json_extract(s.value,'$.media'),'')<>'' AND NOT EXISTS
 (SELECT 1 FROM media_references r WHERE r.owner_type='cron' AND r.owner_id=c.id AND r.media_id=json_extract(s.value,'$.media'))
 UNION ALL SELECT 'missing fork reference: '||f.session_id FROM media_fork_history f WHERE NOT EXISTS(SELECT 1 FROM media_references r WHERE r.owner_type='session_fork' AND r.owner_id=f.session_id AND r.media_id=f.media_id)
 UNION ALL SELECT 'dangling owner reference: '||r.owner_type||':'||r.owner_id FROM media_references r WHERE
 (r.owner_type IN ('message','tool_result') AND NOT EXISTS(SELECT 1 FROM messages WHERE id=r.owner_id)) OR
 (r.owner_type='chat_history' AND NOT EXISTS(SELECT 1 FROM media_history WHERE owner_id=r.owner_id AND media_id=r.media_id)) OR
 (r.owner_type='output' AND NOT EXISTS(SELECT 1 FROM media_outputs WHERE owner_id=r.owner_id)) OR
 (r.owner_type='atrinis_report' AND NOT EXISTS(SELECT 1 FROM atrinis_report_deliveries WHERE id=r.owner_id)) OR
 (r.owner_type='atrinis_event' AND NOT EXISTS(SELECT 1 FROM atrinis_events WHERE id=r.owner_id)) OR
 (r.owner_type='session_fork' AND NOT EXISTS(SELECT 1 FROM sessions WHERE id=r.owner_id)) OR
 (r.owner_type='session_tool' AND (r.session_id IS NULL OR r.session_id<>r.owner_id OR NOT EXISTS(SELECT 1 FROM sessions WHERE id=r.owner_id))) OR
 (r.owner_type='cron' AND NOT EXISTS(SELECT 1 FROM cron_jobs WHERE id=r.owner_id))"""
        )
        return [row[0] for row in rows]
