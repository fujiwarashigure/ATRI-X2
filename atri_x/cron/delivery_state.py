"""一轮投递的状态模型与编解码。

对应 Go：``internal/cron/model.go``（投递状态类型：``DeliveryStatus`` /
``CronDeliveryState`` / ``CronDeliveryTargetState`` / ``CronDeliveryOutputState`` /
``legacyCronDeliveryMetadata``）与 ``internal/cron/delivery.go`` 的
``encodeDeliveryState`` / ``decodeDeliveryState``。

**拆分说明**（``PORTING.md`` §2 的"单文件"约束）：Go 的这些类型与 :mod:`atri_x.cron.model`
的 Metadata 同在一个文件；本仓把"任务配置"与"投递进度"分成两个模块，职责边界更清楚，
并把 Go 定义在 ``delivery.go`` 的编解码放在类型旁边。:mod:`atri_x.cron.model` 会再导出
这里的全部名字，所以 ``from atri_x.cron.model import CronDeliveryState`` 依旧可用。

投递状态是**逐实际收件目标**（``key``：``<platform>|superadmins`` 或
``<platform>|private|<id>``）、**逐实际输出**（``id``：``text`` / ``segment:<i>``）
两级记录；每次写库都走 ``CronJobRepository.CompareAndSwapDelivery`` 条件更新，
令牌是 ``run_id``（报告就绪后换成 ``report_session_id``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import MessageSegment

__all__ = [
    "CronDeliveryOutputState",
    "CronDeliveryState",
    "CronDeliveryTargetState",
    "DeliveryDelivered",
    "DeliveryFallbackDelivered",
    "DeliveryFallbackPending",
    "DeliveryPending",
    "DeliveryStatus",
    "LegacyCronDeliveryMetadata",
    "decode_delivery_state",
    "decode_legacy_delivery_metadata",
    "encode_delivery_state",
    "has_legacy_delivery_metadata",
]


class DeliveryStatus:
    """单目标单输出的投递状态。对应 Go ``Delivery*``。"""

    PENDING = "pending"
    DELIVERED = "delivered"
    FALLBACK_PENDING = "fallback_pending"
    FALLBACK_DELIVERED = "fallback_delivered"


DeliveryPending = DeliveryStatus.PENDING
DeliveryDelivered = DeliveryStatus.DELIVERED
DeliveryFallbackPending = DeliveryStatus.FALLBACK_PENDING
DeliveryFallbackDelivered = DeliveryStatus.FALLBACK_DELIVERED


@dataclass
class CronDeliveryOutputState:
    """逐输出的投递状态。对应 Go ``CronDeliveryOutputState``。"""

    id: str = ""
    status: str = ""
    fallback_text: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status}
        if self.fallback_text:
            out["fallback_text"] = self.fallback_text
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronDeliveryOutputState":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            id=_text(data.get("id")),
            status=_text(data.get("status")),
            fallback_text=_text(data.get("fallback_text")),
        )


@dataclass
class CronDeliveryTargetState:
    """逐实际收件目标的投递状态。对应 Go ``CronDeliveryTargetState``。"""

    key: str = ""
    outputs: list[CronDeliveryOutputState] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key}
        if self.outputs:
            out["outputs"] = [item.to_json() for item in self.outputs]
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronDeliveryTargetState":
        data = raw if isinstance(raw, dict) else {}
        outputs = data.get("outputs")
        items = (
            [CronDeliveryOutputState.from_json(item) for item in outputs]
            if isinstance(outputs, list)
            else []
        )
        return cls(key=_text(data.get("key")), outputs=items)


@dataclass
class CronDeliveryState:
    """一轮投递的状态（落库 ``cron_jobs.delivery_state``）。对应 Go ``CronDeliveryState``。

    ``report_ready`` 表示报告已生成、可复用（补发只读它，不再执行 LLM）；
    ``task_completed`` 只记录 LLM 给出的任务结论，不决定是否投递。
    """

    run_id: str = ""
    report_ready: bool = False
    task_completed: bool = False
    report: str = ""
    report_segments: list[MessageSegment] = field(default_factory=list)
    report_session_id: str = ""
    report_message_id: str = ""
    targets: list[CronDeliveryTargetState] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"run_id": self.run_id}
        if self.report_ready:
            out["report_ready"] = True
        if self.task_completed:
            out["task_completed"] = True
        if self.report:
            out["report"] = self.report
        if self.report_segments:
            out["report_segments"] = [segment.to_json() for segment in self.report_segments]
        if self.report_session_id:
            out["report_session_id"] = self.report_session_id
        if self.report_message_id:
            out["report_message_id"] = self.report_message_id
        if self.targets:
            out["targets"] = [item.to_json() for item in self.targets]
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronDeliveryState":
        data = raw if isinstance(raw, dict) else {}
        segments = data.get("report_segments")
        targets = data.get("targets")
        return cls(
            run_id=_text(data.get("run_id")),
            report_ready=bool(data.get("report_ready", False)),
            task_completed=bool(data.get("task_completed", False)),
            report=_text(data.get("report")),
            report_segments=(
                [MessageSegment.from_json(item) for item in segments]
                if isinstance(segments, list)
                else []
            ),
            report_session_id=_text(data.get("report_session_id")),
            report_message_id=_text(data.get("report_message_id")),
            targets=(
                [CronDeliveryTargetState.from_json(item) for item in targets]
                if isinstance(targets, list)
                else []
            ),
        )


@dataclass
class LegacyCronDeliveryMetadata:
    """历史版本写在 metadata 里的投递数据。对应 Go ``legacyCronDeliveryMetadata``。"""

    completed: bool = False
    report: str = ""
    report_segments: list[MessageSegment] = field(default_factory=list)
    report_session_id: str = ""
    report_message_id: str = ""
    delivered_platforms: list[str] = field(default_factory=list)


def encode_delivery_state(state: CronDeliveryState) -> str:
    """序列化投递状态。对应 Go ``encodeDeliveryState``（定义在 ``delivery.go``）。"""
    return json.dumps(state.to_json(), ensure_ascii=False, separators=(",", ":"))


def decode_delivery_state(raw: str) -> CronDeliveryState:
    """解析投递状态；空文本表示"还没有投递状态"。对应 Go ``decodeDeliveryState``。"""
    if not (raw or "").strip():
        return CronDeliveryState()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"decode cron delivery state: {exc}") from exc
    return CronDeliveryState.from_json(parsed)


def decode_legacy_delivery_metadata(raw: str) -> LegacyCronDeliveryMetadata:
    """解析历史版本 metadata 里的 ``delivery`` 段。对应 Go ``decodeLegacyDeliveryMetadata``。"""
    try:
        parsed = json.loads(raw or "")
    except ValueError as exc:
        raise InvalidError(f"decode legacy cron delivery metadata: {exc}") from exc
    if not isinstance(parsed, dict):
        return LegacyCronDeliveryMetadata()
    legacy = parsed.get("delivery")
    if not isinstance(legacy, dict):
        return LegacyCronDeliveryMetadata()
    segments = legacy.get("report_segments")
    platforms = legacy.get("delivered_platforms")
    return LegacyCronDeliveryMetadata(
        completed=bool(legacy.get("completed", False)),
        report=_text(legacy.get("report")),
        report_segments=(
            [MessageSegment.from_json(item) for item in segments]
            if isinstance(segments, list)
            else []
        ),
        report_session_id=_text(legacy.get("report_session_id")),
        report_message_id=_text(legacy.get("report_message_id")),
        delivered_platforms=(
            [_text(item) for item in platforms] if isinstance(platforms, list) else []
        ),
    )


def has_legacy_delivery_metadata(delivery: LegacyCronDeliveryMetadata) -> bool:
    """历史 metadata 里是否真的有投递数据。对应 Go ``hasLegacyDeliveryMetadata``。"""
    return bool(
        delivery.completed
        or delivery.report.strip()
        or delivery.report_segments
        or delivery.report_session_id.strip()
        or delivery.report_message_id.strip()
        or delivery.delivered_platforms
    )


def _text(value: Any) -> str:
    """按 Go ``string`` 字段的口径取文本：``None`` → 空串。"""
    return value if isinstance(value, str) else ""
