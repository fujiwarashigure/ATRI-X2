"""定时任务的领域模型、JSON 编解码与校验。对应 Go：``internal/cron/model.go``。

包含：

* 常量与枚举（``ScheduleMode`` / ``TriggerMode`` / ``DeliveryStatus``）；
* 任务 Metadata（``Metadata`` 及其四个子结构）与投递状态
  （``CronDeliveryState`` / ``CronDeliveryTargetState`` / ``CronDeliveryOutputState``）；
* CRUD 请求结构（``UpsertRequest`` / ``PatchRequest``）与视图（``JobView``）；
* 规范性 helper（``normalize_job_name`` / ``cron_scope_id`` / ``schedule_expr`` /
  ``parse_run_at`` / ``validate_metadata`` / ``require_superadmin`` …）。

JSON 编解码**不复用** ``dataclasses.asdict``：Go 侧每个字段都有明确的 json tag 与
``omitempty``，逐字段对齐才能让落库的 metadata 文本与 Go 完全同形（``"llm":{}`` 这种
零值结构体必须写出，空 map/slice 才省略）。

时间语义：Go 的 ``timeLayout`` 是**本地墙钟**（``time.ParseInLocation(..., time.Local)``，
``Format(timeLayout)`` 亦然）。Python 侧全部落库/比较用 UTC aware，只在解析与格式化
``run_at``、以及把 cron 表达式解释成墙钟时切到本地时区；本地时区默认取系统时区
（等价 Go 的 ``time.Local``），也可由 ``location`` 显式注入以便测试固定时区。
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any

from atri_x.background import JSONResult
from atri_x.errors import ForbiddenError, InvalidError
from atri_x.llm.llm import MessageSegment
from atri_x.security import Actor, RoleSuperadmin
from atri_x.storage.models import SessionModeChat, SessionModeWork
from atri_x.storage.storage import CronJob
from atri_x.cron.delivery_state import (
    CronDeliveryOutputState,
    CronDeliveryState,
    CronDeliveryTargetState,
    DeliveryDelivered,
    DeliveryFallbackDelivered,
    DeliveryFallbackPending,
    DeliveryPending,
    DeliveryStatus,
    LegacyCronDeliveryMetadata,
    decode_delivery_state,
    decode_legacy_delivery_metadata,
    encode_delivery_state,
    has_legacy_delivery_metadata,
)

__all__ = [
    "METADATA_KIND",
    "RUN_AT_LAYOUT",
    "USER_HANDLER_NAME",
    "CronActor",
    "CronDeliveryOutputState",
    "CronDeliveryState",
    "CronDeliveryTargetState",
    "CronLLMMetadata",
    "CronLLMResult",
    "CronSchedule",
    "CronTarget",
    "CronTrigger",
    "DeliveryDelivered",
    "DeliveryFallbackDelivered",
    "DeliveryFallbackPending",
    "DeliveryPending",
    "DeliveryStatus",
    "JobView",
    "LegacyCronDeliveryMetadata",
    "Metadata",
    "PatchRequest",
    "ScheduleCron",
    "ScheduleMode",
    "ScheduleOnce",
    "TriggerDirect",
    "TriggerLLM",
    "TriggerMode",
    "UpsertRequest",
    "actor_metadata",
    "cron_sandbox_subdir",
    "cron_scope_id",
    "cron_session_metadata",
    "contains_string",
    "decode_delivery_state",
    "decode_legacy_delivery_metadata",
    "decode_metadata",
    "encode_delivery_state",
    "first_non_empty",
    "format_run_at",
    "has_legacy_delivery_metadata",
    "normalize_job_name",
    "normalize_llm_session_mode",
    "normalize_tool_list_names",
    "parse_run_at",
    "require_superadmin",
    "schedule_expr",
    "system_location",
    "trim_strings",
    "unique_strings",
    "validate_llm_session_mode",
    "validate_metadata",
]

#: 用户 cron 的 handler 名。对应 Go ``cron.UserHandlerName``。
#:
#: 注：Go 把 ``UserHandlerName`` / ``metadataKind`` / ``timeLayout`` 定义在
#: ``service.go``；本仓放在本模块（共用叶模块）以避免 service ↔ model 循环 import。
USER_HANDLER_NAME = "builtin.cron"

#: Metadata 的 kind 标识。对应 Go ``metadataKind``。
METADATA_KIND = "llm_cron"

#: ``run_at`` 的本地墙钟格式。对应 Go ``timeLayout``。
RUN_AT_LAYOUT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class ScheduleMode:
    """调度模式。对应 Go ``ScheduleOnce`` / ``ScheduleCron``。"""

    ONCE = "once"
    CRON = "cron"


ScheduleOnce = ScheduleMode.ONCE
ScheduleCron = ScheduleMode.CRON


class TriggerMode:
    """触发模式。对应 Go ``TriggerDirect`` / ``TriggerLLM``。"""

    DIRECT = "direct"
    LLM = "llm"


TriggerDirect = TriggerMode.DIRECT
TriggerLLM = TriggerMode.LLM


# ---------------------------------------------------------------------------
# 时间 helper
# ---------------------------------------------------------------------------
def system_location() -> tzinfo:
    """系统本地时区（等价 Go 的 ``time.Local``）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_run_at(value: str, location: tzinfo | None = None) -> datetime:
    """解析本地墙钟 ``run_at``，返回 UTC aware 时间。对应 Go ``parseRunAt``。

    非法值抛 :class:`~atri_x.errors.InvalidError`（Go 返回 error）。Go 的
    ``time.ParseInLocation`` 错误文本无法逐字复刻，本仓保持前缀
    ``run_at must use YYYY-MM-DD HH:MM:SS`` 并在其后接 Python 的解析错误。
    """
    text = (value or "").strip()
    if not text:
        raise InvalidError("run_at is required")
    try:
        naive = datetime.strptime(text, RUN_AT_LAYOUT)
    except ValueError as exc:
        raise InvalidError(f"run_at must use YYYY-MM-DD HH:MM:SS: {exc}") from exc
    return naive.replace(tzinfo=location or system_location()).astimezone(timezone.utc)


def format_run_at(value: datetime, location: tzinfo | None = None) -> str:
    """把时间格式化成本地墙钟 ``run_at``。对应 Go 的 ``Format(timeLayout)``。"""
    return value.astimezone(location or system_location()).strftime(RUN_AT_LAYOUT)


def schedule_expr(schedule: "CronSchedule", location: tzinfo | None = None) -> str:
    """把任务调度换算成 5 段 cron 表达式。对应 Go ``scheduleExpr``。

    ``once`` 是**分钟级**调度：直接取本地墙钟的分/时/日/月，组成当年当月的定点表达式。
    """
    if schedule.mode == ScheduleMode.ONCE:
        run_at = parse_run_at(schedule.run_at, location).astimezone(location or system_location())
        return f"{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *"
    if schedule.mode == ScheduleMode.CRON:
        expr = (schedule.cron_expr or "").strip()
        if not expr:
            raise InvalidError("cron_expr is required")
        if len(expr.split()) != 5:
            raise InvalidError("cron_expr must be a 5-field cron expression")
        return expr
    raise InvalidError(f'unsupported schedule mode "{schedule.mode}"')


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
@dataclass
class CronActor:
    """任务创建者。对应 Go ``CronActor``。"""

    actor_id: str = ""
    platform: str = ""
    platform_user_id: str = ""
    display_name: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "actor_id": self.actor_id,
            "platform": self.platform,
            "platform_user_id": self.platform_user_id,
        }
        if self.display_name:
            out["display_name"] = self.display_name
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronActor":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            actor_id=_text(data.get("actor_id")),
            platform=_text(data.get("platform")),
            platform_user_id=_text(data.get("platform_user_id")),
            display_name=_text(data.get("display_name")),
        )


@dataclass
class CronSchedule:
    """任务调度。对应 Go ``CronSchedule``。"""

    mode: str = ""
    run_at: str = ""
    cron_expr: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode}
        if self.run_at:
            out["run_at"] = self.run_at
        if self.cron_expr:
            out["cron_expr"] = self.cron_expr
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronSchedule":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            mode=_text(data.get("mode")),
            run_at=_text(data.get("run_at")),
            cron_expr=_text(data.get("cron_expr")),
        )


@dataclass
class CronTrigger:
    """触发方式与任务正文。对应 Go ``CronTrigger``。"""

    mode: str = ""
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "message": self.message}

    @classmethod
    def from_json(cls, raw: Any) -> "CronTrigger":
        data = raw if isinstance(raw, dict) else {}
        return cls(mode=_text(data.get("mode")), message=_text(data.get("message")))


@dataclass
class CronTarget:
    """投递目标。对应 Go ``CronTarget``。"""

    all_enabled_platforms: bool = False
    source_platform: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "all_enabled_platforms": self.all_enabled_platforms,
            "source_platform": self.source_platform,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "CronTarget":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            all_enabled_platforms=bool(data.get("all_enabled_platforms", False)),
            source_platform=_text(data.get("source_platform")),
        )


@dataclass
class CronLLMMetadata:
    """LLM 触发专用的预注入声明。对应 Go ``CronLLMMetadata``。"""

    tool_list_names: list[str] = field(default_factory=list)
    session_mode: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tool_list_names:
            out["tool_list_names"] = list(self.tool_list_names)
        if self.session_mode:
            out["session_mode"] = self.session_mode
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "CronLLMMetadata":
        data = raw if isinstance(raw, dict) else {}
        tools = data.get("tool_list_names")
        names = [_text(item) for item in tools] if isinstance(tools, list) else []
        return cls(tool_list_names=names, session_mode=_text(data.get("session_mode")))


@dataclass
class Metadata:
    """任务 Metadata（落库 ``cron_jobs.metadata`` 的 JSON）。对应 Go ``Metadata``。"""

    kind: str = ""
    version: int = 0
    title: str = ""
    created_by: CronActor = field(default_factory=CronActor)
    schedule: CronSchedule = field(default_factory=CronSchedule)
    trigger: CronTrigger = field(default_factory=CronTrigger)
    target: CronTarget = field(default_factory=CronTarget)
    llm: CronLLMMetadata = field(default_factory=CronLLMMetadata)

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 json tag 序列化。

        ``llm`` 带 ``omitempty``，但 Go 对**结构体**的 omitempty 不生效，因此零值也要
        写出 ``"llm":{}``；``created_by`` / ``trigger`` / ``target`` 同理始终出现。
        """
        return {
            "kind": self.kind,
            "version": self.version,
            "title": self.title,
            "created_by": self.created_by.to_json(),
            "schedule": self.schedule.to_json(),
            "trigger": self.trigger.to_json(),
            "target": self.target.to_json(),
            "llm": self.llm.to_json(),
        }

    def encode(self) -> str:
        """等价 Go ``json.Marshal(meta)``（紧凑分隔符，不转义非 ASCII）。"""
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: Any) -> "Metadata":
        if not isinstance(raw, dict):
            raise InvalidError("cron metadata must be a JSON object")
        version = raw.get("version", 0)
        return cls(
            kind=_text(raw.get("kind")),
            version=int(version) if isinstance(version, (int, float)) else 0,
            title=_text(raw.get("title")),
            created_by=CronActor.from_json(raw.get("created_by")),
            schedule=CronSchedule.from_json(raw.get("schedule")),
            trigger=CronTrigger.from_json(raw.get("trigger")),
            target=CronTarget.from_json(raw.get("target")),
            llm=CronLLMMetadata.from_json(raw.get("llm")),
        )


#: LLM cron 的结果类型。对应 Go ``type CronLLMResult = background.JSONResult``。
CronLLMResult = JSONResult


# ---------------------------------------------------------------------------
# CRUD 请求 / 视图
# ---------------------------------------------------------------------------
@dataclass
class UpsertRequest:
    """创建任务的请求。对应 Go ``UpsertRequest``。"""

    name: str = ""
    title: str = ""
    schedule_mode: str = ""
    run_at: str = ""
    cron_expr: str = ""
    trigger_mode: str = ""
    message: str = ""
    tool_list_names: list[str] = field(default_factory=list)
    session_mode: str = ""
    all_enabled_platforms: bool = False
    enabled: bool = False
    actor: Actor = field(default_factory=Actor)
    source_platform: str = ""


@dataclass
class PatchRequest:
    """更新任务的请求。对应 Go ``PatchRequest``。

    Go 用指针表达"这次没改这个字段"；本仓用 ``None``：``None`` = 未提供，
    空串/空列表 = 显式清空（``tool_list_names=[]`` 与 Go 的 ``&[]string{}`` 等价）。
    """

    name: str = ""
    title: str | None = None
    schedule_mode: str | None = None
    run_at: str | None = None
    cron_expr: str | None = None
    trigger_mode: str | None = None
    message: str | None = None
    tool_list_names: list[str] | None = None
    session_mode: str | None = None
    all_enabled_platforms: bool | None = None
    enabled: bool | None = None
    actor: Actor = field(default_factory=Actor)


@dataclass
class JobView:
    """任务视图（任务行 + 解析后的 Metadata + 投递状态）。对应 Go ``JobView``。"""

    job: CronJob = field(default_factory=CronJob)
    metadata: Metadata = field(default_factory=Metadata)
    delivery: CronDeliveryState = field(default_factory=CronDeliveryState)


# ---------------------------------------------------------------------------
# 编解码 / 校验 / 规范化
# ---------------------------------------------------------------------------
def decode_metadata(raw: str) -> Metadata:
    """解析 metadata 文本。对应 Go ``decodeMetadata``。"""
    try:
        parsed = json.loads(raw or "")
    except ValueError as exc:
        raise InvalidError(f"decode cron metadata: {exc}") from exc
    meta = Metadata.from_json(parsed)
    if meta.kind != METADATA_KIND:
        raise InvalidError(f'unsupported cron metadata kind "{meta.kind}"')
    return meta


def validate_metadata(meta: Metadata) -> None:
    """校验 Metadata 的必要字段。对应 Go ``validateMetadata``。"""
    if not (meta.trigger.message or "").strip():
        raise InvalidError("message is required")
    if meta.trigger.mode not in (TriggerMode.DIRECT, TriggerMode.LLM):
        raise InvalidError(f'unsupported trigger mode "{meta.trigger.mode}"')
    if not meta.target.source_platform:
        raise InvalidError("source platform is required")
    schedule_expr(meta.schedule)


def normalize_job_name(name: str) -> str:
    """规范化任务名（统一 ``user.cron.`` 前缀并过滤非法字符）。对应 Go ``normalizeJobName``。"""
    value = (name or "").strip()
    if value.startswith("user.cron."):
        value = value[len("user.cron.") :]
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)
    value = value.strip("._-")
    if not value:
        return ""
    return "user.cron." + value


def cron_sandbox_subdir(job_name: str) -> str:
    """任务的沙盒子目录。对应 Go ``cronSandboxSubdir``。"""
    name = normalize_job_name(job_name)
    if name.startswith("user.cron."):
        name = name[len("user.cron.") :]
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    name = name.strip("_-")
    if not name:
        name = "default"
    return posixpath.join("cron", name)


def cron_scope_id(job_name: str) -> str:
    """任务的 scope ID。对应 Go ``cronScopeID``。"""
    return "cron:" + normalize_job_name(job_name)


def cron_session_metadata(job_name: str, source_session_id: str, copied: bool) -> str:
    """广播时复制出来的 Session 的 metadata。对应 Go ``cronSessionMetadata``。"""
    payload = {
        "title_renamed": True,
        "title_source": "cron",
        "cron_job_name": job_name,
        "cron_source_session_id": source_session_id,
        "cron_broadcast_copy": copied,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def actor_metadata(actor: Actor) -> CronActor:
    """把权限主体转成 Metadata 里的创建者。对应 Go ``actorMetadata``。"""
    return CronActor(
        actor_id=actor.id,
        platform=actor.platform,
        platform_user_id=actor.platform_user_id,
        display_name=actor.display_name,
    )


def require_superadmin(actor: Actor) -> None:
    """cron 只允许机器人超级管理员操作。对应 Go ``requireSuperadmin``。"""
    if actor.role != RoleSuperadmin:
        raise ForbiddenError("cron requires superadmin role")


def normalize_tool_list_names(names: list[str] | None) -> list[str]:
    """去空白、去重（保序）。对应 Go ``normalizeToolListNames``。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def validate_llm_session_mode(mode: str) -> str:
    """校验预注入的 Session 模式。对应 Go ``validateLLMSessionMode``。"""
    value = (mode or "").strip()
    if value == "":
        return ""
    if value in (SessionModeWork, SessionModeChat):
        return value
    raise InvalidError(f'unsupported session_mode "{value}"')


def normalize_llm_session_mode(mode: str) -> str:
    """规范化 Session 模式；非法值退化为空串。对应 Go ``normalizeLLMSessionMode``。"""
    try:
        value = validate_llm_session_mode(mode)
    except InvalidError:
        return ""
    return value


def _text(value: Any) -> str:
    """按 Go ``string`` 字段的口径取文本：``None`` → 空串。"""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 通用字符串 helper（Go 定义在 service.go，本仓放共用叶模块以避免循环 import）
# ---------------------------------------------------------------------------
def trim_strings(values: list[str] | None) -> list[str]:
    """去空白并丢弃空串。对应 Go ``trimStrings``。"""
    return [value.strip() for value in values or [] if (value or "").strip()]


def unique_strings(values: list[str] | None) -> list[str]:
    """去空白、去重（保序）。对应 Go ``uniqueStrings``。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = (raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def first_non_empty(*values: str) -> str:
    """返回第一个非空白值（已 strip）。对应 Go ``firstNonEmpty``。"""
    for value in values:
        if (value or "").strip():
            return value.strip()
    return ""


def contains_string(values: list[str] | None, value: str) -> bool:
    """列表里是否有等价（去空白后相等）的元素。对应 Go ``containsString``。"""
    target = (value or "").strip()
    for existing in values or []:
        if (existing or "").strip() == target:
            return True
    return False
