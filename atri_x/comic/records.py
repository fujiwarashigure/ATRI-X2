"""一次漫画生成任务的持久化记录（P0-8：能解释"这张图怎么来的"）。

``generation`` / ``storage`` / ``delivery`` 是**三个独立状态**：生成成功、入库成功、
投递成功不是同一件事。投递由 Agent 在工具返回后完成，这里只能记
``delivery="requested"``，成功回执不伪造。

单独成文件的原因：记录是"审计视图"，和计划（创作视图）的字段与校验规则完全不同，
放在同一个 600 行文件里会同时超线又难读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.comic.errors import ComicDataError
from atri_x.comic.models import (
    RECORD_SCHEMA_VERSION,
    _require_choice,
    _require_id,
)
from atri_x.storage import format_time, now

__all__ = ["ComicRecord", "RECORD_SCHEMA_VERSION", "validate_record"]


@dataclass
class ComicRecord:
    """一次生成任务的持久化记录。"""

    task_id: str = ""
    plan_id: str = ""
    plan_revision: int = 0
    plan_fingerprint: str = ""
    schema_version: int = RECORD_SCHEMA_VERSION
    profile: str = ""
    model: str = ""
    size: str = ""
    quality: str = ""
    state: str = "rendering"
    generation: str = "pending"
    storage: str = "pending"
    delivery: str = "not_requested"
    media_id: str = ""
    media_name: str = ""
    mime_type: str = ""
    prompt: str = ""
    negative: str = ""
    reference_descriptors: list[str] = field(default_factory=list)
    revised_prompt: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""

    def validate(self) -> None:
        self.task_id = _require_id(self.task_id, "任务 ID")
        self.plan_id = _require_id(self.plan_id, "计划 ID")
        if not isinstance(self.plan_revision, int) or self.plan_revision <= 0:
            raise ComicDataError("记录必须绑定计划版本号（plan_revision）")
        if not self.plan_fingerprint:
            raise ComicDataError("记录必须绑定计划指纹（plan_fingerprint）")
        self.state = _require_choice(
            self.state,
            ("rendering", "succeeded", "failed", "unknown"),
            "任务状态",
        )
        self.generation = _require_choice(
            self.generation, ("pending", "ok", "failed", "unknown"), "生成状态"
        )
        self.storage = _require_choice(
            self.storage, ("pending", "ok", "failed"), "入库状态"
        )
        self.delivery = _require_choice(
            self.delivery, ("not_requested", "requested"), "投递状态"
        )
        if not self.created_at:
            self.created_at = format_time(now())
        self.updated_at = format_time(now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_fingerprint": self.plan_fingerprint,
            "profile": self.profile,
            "model": self.model,
            "size": self.size,
            "quality": self.quality,
            "state": self.state,
            "generation": self.generation,
            "storage": self.storage,
            "delivery": self.delivery,
            "media_id": self.media_id,
            "media_name": self.media_name,
            "mime_type": self.mime_type,
            "prompt": self.prompt,
            "negative": self.negative,
            "reference_descriptors": list(self.reference_descriptors),
            "revised_prompt": self.revised_prompt,
            "error": self.error,
            "warnings": list(self.warnings),
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComicRecord":
        if not isinstance(raw, dict):
            raise ComicDataError("生成记录的顶层必须是对象")
        return cls(
            schema_version=int(raw.get("schema_version") or 0),
            task_id=str(raw.get("task_id") or ""),
            plan_id=str(raw.get("plan_id") or ""),
            plan_revision=int(raw.get("plan_revision") or 0),
            plan_fingerprint=str(raw.get("plan_fingerprint") or ""),
            profile=str(raw.get("profile") or ""),
            model=str(raw.get("model") or ""),
            size=str(raw.get("size") or ""),
            quality=str(raw.get("quality") or ""),
            state=str(raw.get("state") or "rendering"),
            generation=str(raw.get("generation") or "pending"),
            storage=str(raw.get("storage") or "pending"),
            delivery=str(raw.get("delivery") or "not_requested"),
            media_id=str(raw.get("media_id") or ""),
            media_name=str(raw.get("media_name") or ""),
            mime_type=str(raw.get("mime_type") or ""),
            prompt=str(raw.get("prompt") or ""),
            negative=str(raw.get("negative") or ""),
            reference_descriptors=[
                str(item) for item in raw.get("reference_descriptors") or []
            ],
            revised_prompt=str(raw.get("revised_prompt") or ""),
            error=str(raw.get("error") or ""),
            warnings=[str(item) for item in raw.get("warnings") or []],
            attempts=int(raw.get("attempts") or 0),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


def validate_record(record: ComicRecord) -> None:
    record.validate()
