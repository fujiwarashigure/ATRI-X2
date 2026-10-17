"""Atrinis 汇报出箱：准备投递行、认领、逐批发送、失败回退与重启恢复。

对应 Go：``internal/atrinis/outbox.go``。

**为什么要有 outbox 表**：后台任务跑完之后，汇报可能发给多个目标；把"每个目标 ×
每条输出"先落成 ``atrinis_report_deliveries`` 行，再逐批投递，就能做到

* 进程重启后 :meth:`OutboxMixin.recover_reports` 能接着投没投完的；
* 已成功的目标不会被重复投递（``delivered`` 行直接跳过）；
* 失败只把 **事件** 放回 ``result_ready``，已成功的行保持 ``delivered``。

状态机（两个常量都来自 ``atriena``）：``result_ready`` →(claim)→ ``delivering``
→(全部成功)→ ``completed``；投递中途失败 → :meth:`OutboxMixin.release_report`
把事件放回 ``result_ready``（下次重试时跳过已成功的行）；单行解码失败 →
:meth:`OutboxMixin.fail_report_delivery` 直接把该行标失败。

**wire 编码说明**：落库的 ``target`` / ``output`` / ``receipt`` 三列都是 Go
``json.Marshal`` 的产物。``delivery.Target`` 有全部小写 tag（Python 侧已有
``Target.to_json``）；但 ``delivery.Output`` / ``Source`` / ``Receipt`` 在 Go 里
**没有** tag（只有 ``Source.MediaID`` 带 ``media,omitempty``），所以 marshal 出来
是 PascalCase 字段名。本模块因此自带 :func:`output_to_json` /
:func:`output_from_json` / :func:`receipt_to_json`：``atri_x.delivery`` 目前只提供
``Target``/``Source`` 的 helper，没有这两者（它的 ``Source.to_json`` 只输出
``media``，是给 LLM 段用的另一种形状）。集成期若要复用，应把这三个函数上移到
``atri_x.delivery``；在那边落地之前，本模块是 output/receipt wire 的唯一实现。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from atri_x.atriena import Target
from atri_x.atrinis.background import maybe_await
from atri_x.atrinis.targets import decode_resolved_targets
from atri_x.atrinis.types import (
    StatusCompleted,
    StatusDelivering,
    StatusResultReady,
)
from atri_x.background import BuildReportOutputs, JSONResult
from atri_x.delivery import Kind, Output, Receipt, SentMessage, Source
from atri_x.delivery import Target as DeliveryTarget
from atri_x.errors import InvalidError
from atri_x.storage import (
    AtrinisReportDeliveryDelivered,
    CreateAtrinisReportDeliveryRequest,
    PrepareAtrinisReportRequest,
)
from atri_x.tool import BackgroundKindAtrinis, SandboxContext

__all__ = [
    "OutboxMixin",
    "output_from_json",
    "output_to_json",
    "receipt_from_json",
    "receipt_to_json",
]


# ---------------------------------------------------------------------------
# delivery.Output / Receipt 的 wire 形态（Go 无 tag → PascalCase 字段名）
# ---------------------------------------------------------------------------


def output_to_json(out: Output) -> dict[str, Any]:
    """``delivery.Output`` 的 Go marshal 形态。对应 Go ``json.Marshal(Output)``。"""
    return {
        "Kind": out.kind,
        "Text": out.text,
        "Name": out.name,
        "EmoticonID": out.emoticon_id,
        "AltText": out.alt_text,
        "ReplyToPlatformMessageID": out.reply_to_platform_message_id,
        "Source": _source_to_json(out.source),
        "Target": out.target.to_json() if out.target is not None else {},
        "Meta": dict(out.meta) if out.meta else None,
    }


def output_from_json(raw: str | dict[str, Any]) -> Output:
    """还原 :func:`output_to_json` 的产物。对应 Go ``json.Unmarshal(...Output)``。"""
    payload = _decode(raw)
    if not isinstance(payload, dict):
        raise ValueError("json: cannot unmarshal non-object into delivery.Output")
    return Output(
        kind=_text(payload.get("Kind")),
        text=_text(payload.get("Text")),
        name=_text(payload.get("Name")),
        emoticon_id=_text(payload.get("EmoticonID")),
        alt_text=_text(payload.get("AltText")),
        reply_to_platform_message_id=_text(payload.get("ReplyToPlatformMessageID")),
        source=_source_from_json(payload.get("Source")),
        target=_target_from_json(payload.get("Target")),
        meta=dict(payload["Meta"]) if isinstance(payload.get("Meta"), dict) else None,
    )


def receipt_to_json(receipt: Receipt | None) -> dict[str, Any]:
    """``delivery.Receipt`` 的 Go marshal 形态。对应 Go ``json.Marshal(Receipt)``。"""
    if receipt is None:
        return {
            "PlatformMessageIDs": None,
            "SentMessages": None,
            "Failed": False,
            "Failure": "",
        }
    return {
        "PlatformMessageIDs": list(receipt.platform_message_ids) or None,
        "SentMessages": [
            {
                "PlatformMessageID": item.platform_message_id,
                "Platform": item.platform,
                "ScopeID": item.scope_id,
                "OutputIndexes": list(item.output_indexes),
            }
            for item in receipt.sent_messages
        ]
        or None,
        "Failed": bool(receipt.failed),
        "Failure": receipt.failure or "",
    }


def receipt_from_json(raw: str | dict[str, Any]) -> Receipt:
    """还原 :func:`receipt_to_json` 的产物（供历史回执读取/测试使用）。"""
    payload = _decode(raw)
    if not isinstance(payload, dict):
        raise ValueError("json: cannot unmarshal non-object into delivery.Receipt")
    ids = payload.get("PlatformMessageIDs")
    sent = payload.get("SentMessages")
    return Receipt(
        platform_message_ids=[_text(item) for item in ids] if isinstance(ids, list) else [],
        sent_messages=[
            SentMessage(
                platform_message_id=_text(item.get("PlatformMessageID")),
                platform=_text(item.get("Platform")),
                scope_id=_text(item.get("ScopeID")),
                output_indexes=[
                    int(index)
                    for index in (item.get("OutputIndexes") or [])
                    if isinstance(index, int)
                ],
            )
            for item in (sent if isinstance(sent, list) else [])
            if isinstance(item, dict)
        ],
        failed=bool(payload.get("Failed")),
        failure=_text(payload.get("Failure")),
    )


def _source_to_json(source: Source | None) -> dict[str, Any]:
    if source is None:
        return {"URL": "", "Path": "", "MIMEType": "", "Data": None}
    out: dict[str, Any] = {
        "URL": source.url,
        "Path": source.path,
        "MIMEType": source.mime_type,
        "Data": base64.b64encode(source.data).decode("ascii") if source.data else None,
    }
    if source.media_id:
        out["media"] = source.media_id
    return out


def _source_from_json(raw: Any) -> Source:
    if not isinstance(raw, dict):
        return Source()
    data = raw.get("Data")
    if isinstance(data, str) and data:
        try:
            decoded = base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"json: cannot unmarshal Data: {exc}") from exc
    else:
        decoded = b""
    return Source(
        url=_text(raw.get("URL")),
        path=_text(raw.get("Path")),
        media_id=_text(raw.get("media")),
        mime_type=_text(raw.get("MIMEType")),
        data=decoded,
    )


def _target_from_json(raw: Any) -> DeliveryTarget:
    """``delivery.Target`` 的还原（键名与 Go 的 struct tag 一致，全 omitempty）。

    ``atri_x.delivery.Target`` 提供 ``to_json`` 但没有反解，故在此补齐；
    这是 ``Output.Target`` 字段的 wire 解码，不是对 ``atriena.Target`` 的复用。
    """
    if not isinstance(raw, dict):
        return DeliveryTarget()
    return DeliveryTarget(
        platform=_text(raw.get("platform")),
        scope_id=_text(raw.get("scope_id")),
        private_user_id=_text(raw.get("private_user_id")),
        group_id=_text(raw.get("group_id")),
        superadmins=bool(raw.get("superadmins")),
    )


def _decode(raw: str | dict[str, Any]) -> Any:
    if isinstance(raw, dict):
        return raw
    return json.loads(raw or "null")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise ValueError("json: cannot unmarshal non-string into delivery field")


def _marshal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


class OutboxMixin:
    """汇报投递。对应 Go ``outbox.go`` 里的 ``Service`` 方法。"""

    async def prepare_report(
        self,
        ctx: Any,
        event: Any,
        event_id: str,
        result_json: str,
        session_id: str,
        message_id: str,
        result: JSONResult,
    ) -> bool:
        """把汇报落成投递行。对应 Go ``prepareReport``。

        返回 ``True`` 表示"已有待投递行，去调 :meth:`deliver_report`"；
        返回 ``False`` 表示"已经就地按完成收尾了"（没有目标、或没有产出输出）。
        """
        resolved = decode_resolved_targets(event.resolved_targets)
        if not resolved:
            self.complete_event_with_session(
                ctx,
                event_id,
                event.resolved_targets,
                StatusCompleted,
                session_id,
                result_json,
                "",
            )
            return False
        workspace = self.workspace_dir(event.request.atriwisp.name)
        if self.media is not None:
            result.report_segments = await self.import_report_segments(
                ctx, result.report_segments, event_id, workspace
            )
            result_json = _marshal(result.to_json())
        outputs = BuildReportOutputs(
            result.report,
            result.report_segments,
            SandboxContext(dir=workspace, background=True, background_kind=BackgroundKindAtrinis),
        )
        if not resolved or not outputs:
            self.complete_event_with_session(
                ctx,
                event_id,
                event.resolved_targets,
                StatusCompleted,
                session_id,
                result_json,
                "",
            )
            return False

        deliveries: list[CreateAtrinisReportDeliveryRequest] = []
        for target in resolved:
            target_json = _marshal(target.to_json())
            for output in outputs:
                deliveries.append(
                    CreateAtrinisReportDeliveryRequest(
                        target=target_json,
                        output=_marshal(output_to_json(output)),
                        message_id=message_id,
                    )
                )
        self.store.atrinis_events().prepare_report(
            PrepareAtrinisReportRequest(
                event_id=event_id,
                resolved_targets=event.resolved_targets,
                session_id=session_id,
                result=result_json,
                deliveries=deliveries,
                result_ready_status=StatusResultReady,
            )
        )
        return True

    async def deliver_report(self, ctx: Any, event_id: str) -> None:
        """认领并投递一次汇报。对应 Go ``deliverReport``（失败时抛异常）。"""
        if self.send is None:
            raise InvalidError("atrinis sender is not configured")
        repo = self.store.atrinis_events()
        claimed = repo.claim_report(event_id, StatusResultReady, StatusDelivering)
        if not claimed:
            return None
        try:
            event = repo.get(event_id)
        except Exception as exc:  # noqa: BLE001 - 统一走"释放认领"
            raise self.release_report(ctx, event_id, exc) from exc
        try:
            deliveries = repo.list_report_deliveries(event_id)
        except Exception as exc:  # noqa: BLE001
            raise self.release_report(ctx, event_id, exc) from exc

        index = 0
        while index < len(deliveries):
            item = deliveries[index]
            if item.status == AtrinisReportDeliveryDelivered:
                index += 1
                continue
            try:
                target = Target.from_json(json.loads(item.target or "null"))
            except (TypeError, ValueError) as exc:
                raise self.fail_report_delivery(
                    ctx, event_id, item.id, InvalidError(f"decode atrinis report target: {exc}")
                ) from exc
            batch = [item]
            index += 1
            while index < len(deliveries):
                nxt = deliveries[index]
                if nxt.status == AtrinisReportDeliveryDelivered or nxt.target != item.target:
                    break
                batch.append(nxt)
                index += 1
            outputs: list[Output] = []
            for delivery_item in batch:
                try:
                    outputs.append(output_from_json(delivery_item.output))
                except (TypeError, ValueError) as exc:
                    raise self.fail_report_delivery(
                        ctx,
                        event_id,
                        delivery_item.id,
                        InvalidError(f"decode atrinis report output: {exc}"),
                    ) from exc
                try:
                    repo.start_report_delivery(delivery_item.id)
                except Exception as exc:  # noqa: BLE001
                    raise self.fail_report_delivery(ctx, event_id, delivery_item.id, exc) from exc
            try:
                receipt = await maybe_await(
                    self.send(ctx, target.to_delivery_target(), outputs)
                )
            except Exception as exc:  # noqa: BLE001 - 发送失败 → 该批首行标失败
                raise self.fail_report_delivery(ctx, event_id, batch[0].id, exc) from exc
            receipt_json = _marshal(receipt_to_json(receipt))
            try:
                for delivery_item in batch:
                    repo.mark_report_delivery_delivered(delivery_item.id, receipt_json)
            except Exception as exc:  # noqa: BLE001 - 回执落库失败 → 释放认领重试
                raise self.release_report(ctx, event_id, exc) from exc
            self.map_report_receipt(
                ctx, event.event_key, target, event.session_id, batch[0].message_id, receipt
            )
        try:
            repo.complete_report(event_id, StatusDelivering, StatusCompleted)
        except Exception as exc:  # noqa: BLE001
            raise self.release_report(ctx, event_id, exc) from exc
        self.audit_event(
            "atrinis.llm_completed",
            "event_id",
            event_id,
            "event_key",
            event.event_key,
            "session_id",
            event.session_id,
        )
        self.log_info(
            "atrinis llm completed",
            "event_id",
            event_id,
            "event_key",
            event.event_key,
            "session_id",
            event.session_id,
        )
        return None

    def fail_report_delivery(
        self, ctx: Any, event_id: str, delivery_id: str, delivery_err: BaseException
    ) -> BaseException:
        """把一行标失败并把事件放回待投递。对应 Go ``failReportDelivery``。

        返回**要抛出的异常**而不是自己抛：调用点用 ``raise self.fail_report_delivery(...)``
        以保留原始 traceback 链（Go 的 ``return err`` 语义）。
        """
        try:
            self.store.atrinis_events().mark_report_delivery_failed(
                event_id, delivery_id, StatusResultReady, str(delivery_err)
            )
        except Exception as persist_err:  # noqa: BLE001
            return InvalidError(
                f"{delivery_err}; persist delivery failure: {persist_err}"
            )
        return _as_exception(delivery_err)

    def release_report(
        self, ctx: Any, event_id: str, delivery_err: BaseException
    ) -> BaseException:
        """释放认领（投递失败但可重试）。对应 Go ``releaseReport``。"""
        try:
            self.store.atrinis_events().release_report(
                event_id, StatusDelivering, StatusResultReady, str(delivery_err)
            )
        except Exception as release_err:  # noqa: BLE001
            return InvalidError(f"{delivery_err}; release report claim: {release_err}")
        return _as_exception(delivery_err)

    def reset_delivering_reports(self, ctx: Any) -> None:
        """重启恢复：把卡在 ``delivering`` 的投递复位。对应 Go ``resetDeliveringReports``。"""
        self.store.atrinis_events().reset_delivering_reports(
            StatusDelivering, StatusResultReady
        )
        return None

    async def recover_reports(self, ctx: Any, reset_delivering: bool) -> BaseException | None:
        """重投所有待投递汇报。对应 Go ``recoverReports``。

        返回**第一个**失败（``None`` 表示全部成功）；单条失败不中断后续重投，
        与 Go 的 ``firstErr`` 语义一致。
        """
        repo = self.store.atrinis_events()
        if reset_delivering:
            self.reset_delivering_reports(ctx)
        ids = repo.list_result_ready_report_ids(StatusResultReady)
        first_err: BaseException | None = None
        for event_id in ids:
            try:
                await self.deliver_report(ctx, event_id)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
                if first_err is None:
                    first_err = exc
                self.log_warn("recover atrinis report failed", "event_id", event_id, "error", str(exc))
        return first_err


def _as_exception(err: BaseException) -> BaseException:
    """把任意值转成异常对象（对应 Go 的 ``error`` 返回值）。"""
    if isinstance(err, BaseException):
        return err
    return InvalidError(str(err))
