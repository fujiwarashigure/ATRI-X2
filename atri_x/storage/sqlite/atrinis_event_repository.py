"""Atrinis 事件与报告投递（outbox）仓储。对应 Go：``internal/storage/sqlite/atrinis_event_repository.go``。

两块状态机都靠**条件更新**在同一事务里推进，不靠调用方协调：

* 事件去重键 ``UNIQUE(atriwisp_name, source, source_id)``；:meth:`create` 顺带在同一个事务里
  为本次输入媒体写 ``atrinis_event`` 引用（``purpose='input'``）。
* 报告投递 outbox：:meth:`prepare_report` 一个事务里写事件状态 + 全部投递行；
  :meth:`claim_report` 是 ``result_ready → delivering`` 的 CAS（抢不到就返回 ``False``，
  说明别的 worker 已经在投）；一条投递失败会 :meth:`mark_report_delivery_failed`
  把事件放回 ``result_ready``；只有 ``atrinis_report_deliveries`` 里**不再有**非 ``delivered``
  行时 :meth:`complete_report` 才能把事件推进到 ``completed``（否则抛
  :class:`atri_x.errors.ConflictError`）。

``ordinal`` 是 **0 起**（Go 的 ``for ordinal, delivery := range req.Deliveries``）。
``atri_x/storage/repositories.py`` 里 :meth:`prepare_report` 的 docstring 写的是"从 1 起"，
与 Go 源码不符 —— 以 Go 为准，见本次移植的契约缺口报告。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atri_x.errors import ConflictError, ErrNotFound, InvalidError
from atri_x.storage.ids import new_id
from atri_x.storage.models import (
    AtrinisEvent,
    AtrinisReportDelivery,
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    AtrinisReportDeliveryPending,
    CreateAtrinisEventRequest,
    PrepareAtrinisReportRequest,
    UpdateAtrinisEventRequest,
)
from atri_x.storage.sqlite.store import null_string, text_of
from atri_x.storage.times import format_time, is_zero_time, now, parse_time

if TYPE_CHECKING:  # 只为注解
    from atri_x.storage.sqlite.store import SQLiteDatabase

__all__ = ["AtrinisEventRepository"]

EVENT_COLUMNS = """id, event_key, token_name, atriwisp_name, source, source_id, tags, mode,
       model_slot, content_hash, tool_declarations, tool_hash, requested_targets, resolved_targets, status,
       session_id, result, error, received_at, created_at, updated_at"""

DELIVERY_COLUMNS = """id, event_id, ordinal, target, output, message_id, status, receipt, error, attempts, created_at, updated_at"""


class AtrinisEventRepository:
    """Atrinis 事件仓储。对应 Go ``sqlite.AtrinisEventRepository``。"""

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._db = database

    # -- 事件 --------------------------------------------------------------

    def create(self, req: CreateAtrinisEventRequest) -> AtrinisEvent:
        """写入事件（含本次输入媒体引用），返回落库后的事件行。"""
        moment = now()
        received_at = req.received_at
        if is_zero_time(received_at):
            received_at = moment
        created_at = req.created_at
        if is_zero_time(created_at):
            created_at = received_at
        event = AtrinisEvent(
            id=new_id(),
            event_key=req.event_key,
            token_name=req.token_name,
            atriwisp_name=req.atriwisp_name,
            source=req.source,
            source_id=req.source_id,
            tags=req.tags,
            mode=req.mode,
            model_slot=req.model_slot,
            content_hash=req.content_hash,
            tool_declarations=req.tool_declarations,
            tool_hash=req.tool_hash,
            requested_targets=req.requested_targets,
            resolved_targets=req.resolved_targets,
            status=req.status,
            result=req.result,
            error=req.error,
            received_at=received_at,
            created_at=created_at,
            updated_at=moment,
        )
        with self._db.with_transaction() as tx:
            tx.execute(
                """
INSERT INTO atrinis_events (
    id, event_key, token_name, atriwisp_name, source, source_id, tags, mode,
    model_slot, content_hash, tool_declarations, tool_hash, requested_targets, resolved_targets, status,
    session_id, result, error, received_at, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.event_key,
                    event.token_name,
                    event.atriwisp_name,
                    event.source,
                    event.source_id,
                    null_string(event.tags),
                    event.mode,
                    null_string(event.model_slot),
                    event.content_hash,
                    null_string(event.tool_declarations),
                    null_string(event.tool_hash),
                    null_string(event.requested_targets),
                    null_string(event.resolved_targets),
                    event.status,
                    null_string(event.session_id),
                    null_string(event.result),
                    null_string(event.error),
                    format_time(event.received_at),
                    format_time(event.created_at),
                    format_time(event.updated_at),
                ),
            )
            for media_id in req.media_ids:
                tx.execute(
                    """
INSERT INTO media_references(media_id, owner_type, owner_id, purpose, created_at)
VALUES (?, 'atrinis_event', ?, 'input', ?) ON CONFLICT DO NOTHING""",
                    (media_id, event.id, format_time(moment)),
                )
        return event

    def get(self, id: str) -> AtrinisEvent:
        """按主键取事件；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        row = self._db.query_row(
            f"""
SELECT {EVENT_COLUMNS}
FROM atrinis_events
WHERE id = ?""",
            (id,),
        )
        if row is None:
            raise ErrNotFound
        return _scan_atrinis_event(row)

    def get_by_key(self, atriwisp_name: str, source: str, source_id: str) -> AtrinisEvent:
        """按去重键 ``(atriwisp_name, source, source_id)`` 取事件；不存在抛 ErrNotFound。"""
        row = self._db.query_row(
            f"""
SELECT {EVENT_COLUMNS}
FROM atrinis_events
WHERE atriwisp_name = ? AND source = ? AND source_id = ?""",
            (atriwisp_name, source, source_id),
        )
        if row is None:
            raise ErrNotFound
        return _scan_atrinis_event(row)

    def update(self, req: UpdateAtrinisEventRequest) -> None:
        """更新事件的解析目标、状态、会话与结果；不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE atrinis_events
SET resolved_targets = ?, status = ?, session_id = ?, result = ?, error = ?, updated_at = ?
WHERE id = ?""",
            (
                null_string(req.resolved_targets),
                req.status,
                null_string(req.session_id),
                null_string(req.result),
                null_string(req.error),
                format_time(now()),
                req.id,
            ),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    # -- 报告 outbox -------------------------------------------------------

    def prepare_report(self, req: PrepareAtrinisReportRequest) -> None:
        """单事务写入报告结果与全部投递行（``ordinal`` 按 deliveries 顺序从 **0** 起）。"""
        if not req.deliveries:
            raise InvalidError("prepare atrinis report: no deliveries")
        with self._db.with_transaction() as tx:
            moment = now()
            cursor = tx.execute(
                """
UPDATE atrinis_events
SET resolved_targets = ?, status = ?, session_id = ?, result = ?, error = NULL, updated_at = ?
WHERE id = ?""",
                (
                    null_string(req.resolved_targets),
                    req.result_ready_status,
                    null_string(req.session_id),
                    null_string(req.result),
                    format_time(moment),
                    req.event_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ErrNotFound
            for ordinal, delivery in enumerate(req.deliveries):
                tx.execute(
                    """
INSERT INTO atrinis_report_deliveries (
    id, event_id, ordinal, target, output, message_id, status, receipt, error, attempts, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)""",
                    (
                        new_id(),
                        req.event_id,
                        ordinal,
                        delivery.target,
                        delivery.output,
                        null_string(delivery.message_id),
                        AtrinisReportDeliveryPending,
                        format_time(moment),
                        format_time(moment),
                    ),
                )

    def reset_delivering_reports(self, delivering_status: str, result_ready_status: str) -> None:
        """重启恢复：把卡在 ``delivering`` 的投递复位回 ``result_ready``（只碰有投递行的事件）。"""
        self._db.execute(
            """
UPDATE atrinis_events
SET status = ?, updated_at = ?
WHERE status = ?
  AND EXISTS (SELECT 1 FROM atrinis_report_deliveries d WHERE d.event_id = atrinis_events.id)""",
            (result_ready_status, format_time(now()), delivering_status),
        )

    def list_result_ready_report_ids(self, result_ready_status: str) -> list[str]:
        """列出待投递（``result_ready`` 且有投递行）的事件 ID，按 ``updated_at, id`` 升序。"""
        rows = self._db.query(
            """
SELECT e.id
FROM atrinis_events e
WHERE e.status = ?
  AND EXISTS (SELECT 1 FROM atrinis_report_deliveries d WHERE d.event_id = e.id)
ORDER BY e.updated_at, e.id""",
            (result_ready_status,),
        )
        return [row["id"] for row in rows]

    def claim_report(self, event_id: str, result_ready_status: str, delivering_status: str) -> bool:
        """认领一次报告投递（状态 CAS）；已被别人认领时返回 ``False``。"""
        cursor = self._db.execute(
            """
UPDATE atrinis_events
SET status = ?, error = NULL, updated_at = ?
WHERE id = ? AND status = ?""",
            (delivering_status, format_time(now()), event_id, result_ready_status),
        )
        return cursor.rowcount > 0

    def release_report(
        self,
        event_id: str,
        delivering_status: str,
        result_ready_status: str,
        delivery_error: str,
    ) -> None:
        """释放认领（投递失败但可重试）；事件不在 ``delivering`` 时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE atrinis_events
SET status = ?, error = ?, updated_at = ?
WHERE id = ? AND status = ?""",
            (
                result_ready_status,
                null_string(delivery_error),
                format_time(now()),
                event_id,
                delivering_status,
            ),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def list_report_deliveries(self, event_id: str) -> list[AtrinisReportDelivery]:
        """按 ``ordinal`` 升序列出事件的投递行。"""
        rows = self._db.query(
            f"""
SELECT {DELIVERY_COLUMNS}
FROM atrinis_report_deliveries
WHERE event_id = ?
ORDER BY ordinal""",
            (event_id,),
        )
        return [_scan_report_delivery(row) for row in rows]

    def start_report_delivery(self, id: str) -> None:
        """标记一条投递开始（``attempts`` 加一、清空 ``error``）；已投递或不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE atrinis_report_deliveries
SET attempts = attempts + 1, error = NULL, updated_at = ?
WHERE id = ? AND status != ?""",
            (format_time(now()), id, AtrinisReportDeliveryDelivered),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def mark_report_delivery_failed(
        self, event_id: str, delivery_id: str, result_ready_status: str, delivery_error: str
    ) -> None:
        """标记一条投递失败，并把事件放回待投递状态（同一事务）。"""
        with self._db.with_transaction() as tx:
            moment = format_time(now())
            cursor = tx.execute(
                """
UPDATE atrinis_report_deliveries
SET status = ?, error = ?, updated_at = ?
WHERE id = ? AND event_id = ? AND status != ?""",
                (
                    AtrinisReportDeliveryFailed,
                    null_string(delivery_error),
                    moment,
                    delivery_id,
                    event_id,
                    AtrinisReportDeliveryDelivered,
                ),
            )
            if cursor.rowcount == 0:
                raise ErrNotFound
            tx.execute(
                """
UPDATE atrinis_events
SET status = ?, error = ?, updated_at = ?
WHERE id = ?""",
                (result_ready_status, null_string(delivery_error), moment, event_id),
            )

    def mark_report_delivery_delivered(self, delivery_id: str, receipt: str) -> None:
        """标记一条投递成功（含发送回执）；已投递或不存在时抛 ErrNotFound。"""
        cursor = self._db.execute(
            """
UPDATE atrinis_report_deliveries
SET status = ?, receipt = ?, error = NULL, updated_at = ?
WHERE id = ? AND status != ?""",
            (
                AtrinisReportDeliveryDelivered,
                null_string(receipt),
                format_time(now()),
                delivery_id,
                AtrinisReportDeliveryDelivered,
            ),
        )
        if cursor.rowcount == 0:
            raise ErrNotFound

    def complete_report(self, event_id: str, delivering_status: str, completed_status: str) -> None:
        """全部投递完成后把事件推进到 ``completed``。

        只要还有非 ``delivered`` 的投递行（或事件不在 ``delivering``）就不写任何东西，
        抛 :class:`atri_x.errors.ConflictError`；Go 侧是 ``fmt.Errorf("...deliveries are not complete")``。
        """
        cursor = self._db.execute(
            """
UPDATE atrinis_events
SET status = ?, error = NULL, updated_at = ?
WHERE id = ? AND status = ?
  AND NOT EXISTS (
      SELECT 1
      FROM atrinis_report_deliveries d
      WHERE d.event_id = atrinis_events.id AND d.status != ?
  )""",
            (
                completed_status,
                format_time(now()),
                event_id,
                delivering_status,
                AtrinisReportDeliveryDelivered,
            ),
        )
        if cursor.rowcount == 0:
            raise ConflictError("complete atrinis report: deliveries are not complete")


def _scan_atrinis_event(row) -> AtrinisEvent:
    """事件行 → :class:`AtrinisEvent`（Go ``scanAtrinisEvent``）。"""
    return AtrinisEvent(
        id=row["id"],
        event_key=row["event_key"],
        token_name=row["token_name"],
        atriwisp_name=row["atriwisp_name"],
        source=row["source"],
        source_id=row["source_id"],
        tags=text_of(row["tags"]),
        mode=row["mode"],
        model_slot=text_of(row["model_slot"]),
        content_hash=row["content_hash"],
        tool_declarations=text_of(row["tool_declarations"]),
        tool_hash=text_of(row["tool_hash"]),
        requested_targets=text_of(row["requested_targets"]),
        resolved_targets=text_of(row["resolved_targets"]),
        status=row["status"],
        session_id=text_of(row["session_id"]),
        result=text_of(row["result"]),
        error=text_of(row["error"]),
        received_at=parse_time(row["received_at"]),
        created_at=parse_time(row["created_at"]),
        updated_at=parse_time(row["updated_at"]),
    )


def _scan_report_delivery(row) -> AtrinisReportDelivery:
    """投递行 → :class:`AtrinisReportDelivery`（Go ``scanAtrinisReportDelivery``）。"""
    return AtrinisReportDelivery(
        id=row["id"],
        event_id=row["event_id"],
        ordinal=int(row["ordinal"]),
        target=row["target"],
        output=row["output"],
        message_id=text_of(row["message_id"]),
        status=row["status"],
        receipt=text_of(row["receipt"]),
        error=text_of(row["error"]),
        attempts=int(row["attempts"]),
        created_at=parse_time(row["created_at"]),
        updated_at=parse_time(row["updated_at"]),
    )
