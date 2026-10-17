"""存储领域模型。对应 Go：``internal/storage/storage.go``（领域模型部分；repository
抽象见 :mod:`atri_x.storage.repositories`）。

字段与 ``internal/storage/sqlite/migrations.go`` 以及 ``chat_history_repository.go``
里的建表语句逐列对应（下表注明迁移版本 / 建表位置）：

====== ===========================================================
表     模型
====== ===========================================================
``sessions`` (v1) ``Session``
``messages`` (v1, ``segments`` 见 v11) ``Message``
``context_summaries`` (v2) ``ContextSummary``
``tool_call_records`` (v4) ``ToolCallRecord``
``cron_jobs`` (v5, v10) ``CronJob``
``atrinis_events`` (v7, v8) ``AtrinisEvent``
``atrinis_report_deliveries`` (v9) ``AtrinisReportDelivery``
``media`` (v12, v14) ``Media``
``media_references`` (v12) ``MediaReference``
``media_outputs`` (v15) ``MediaOutput``
``media_history`` (v16) ``HistoryMedia``
``platform_message_map`` (v1) ``PlatformMessageMap``
``chat_messages`` (``chat_history_repository.go``) ``ChatMessage``
``outbound_messages`` (``chat_history_repository.go``) ``OutboundMessage``
====== ===========================================================

可空列的 Python 语义：

* 可空 TEXT 列在 Go 侧用 ``string`` + ``sql.NullString`` 承载，本仓同样用 ``str``，
  空串即 NULL（``metadata`` / ``segments`` / ``raw`` / ``title`` 等）。
* 可空时间列在 Go 侧是 ``*time.Time``，本仓用 ``datetime | None``。
* 非空时间列（``created_at`` 等）用 ``datetime | None = None``，``None`` 表示 Go 的
  零值时间（尚未设置），``atri_x.storage.times.format_time`` 对它写空串。
* SQLite 的 INTEGER 布尔列（``success`` / ``enabled`` / ``deleting``）用 ``bool``。

本模块只放定义；对外出口与 ``__all__`` 见 :mod:`atri_x.storage.storage`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from atri_x.llm.llm import MessageSegment

# --- 常量（Go 同名导出常量） -----------------------------------------------


class SessionMode:
    """会话模式。对应 Go ``SessionModeWork`` / ``SessionModeChat``。"""

    WORK = "work"
    CHAT = "chat"


SessionModeWork = SessionMode.WORK
SessionModeChat = SessionMode.CHAT


class SessionStatus:
    """会话状态。对应 Go ``SessionStatusActive/Paused/Closed``。"""

    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


SessionStatusActive = SessionStatus.ACTIVE
SessionStatusPaused = SessionStatus.PAUSED
SessionStatusClosed = SessionStatus.CLOSED


class Role:
    """消息角色。对应 Go ``RoleSystem/RoleUser/RoleAssistant/RoleTool``。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


RoleSystem = Role.SYSTEM
RoleUser = Role.USER
RoleAssistant = Role.ASSISTANT
RoleTool = Role.TOOL


class AtrinisReportDeliveryStatus:
    """Atrinis 报告投递状态。对应 Go ``AtrinisReportDelivery*``。"""

    PENDING = "pending"
    FAILED = "failed"
    DELIVERED = "delivered"


AtrinisReportDeliveryPending = AtrinisReportDeliveryStatus.PENDING
AtrinisReportDeliveryFailed = AtrinisReportDeliveryStatus.FAILED
AtrinisReportDeliveryDelivered = AtrinisReportDeliveryStatus.DELIVERED


# --- segments 列（JSON 文本）helper ----------------------------------------


def parse_segments(raw: str) -> list[MessageSegment]:
    """解析 ``messages.segments`` / ``chat_messages.segments`` 列。

    列语义与 Go 一致：空串/NULL 表示"没有多模态正文"；不是合法 JSON 数组时按空列表处理
    （读取方不该因为一行脏数据崩溃）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [MessageSegment.from_json(item) for item in parsed]


def segments_json(segments: list[MessageSegment] | None) -> str:
    """把消息段序列化成 ``segments`` 列文本。

    键与 :meth:`atri_x.llm.llm.MessageSegment.to_json` 一致（Go struct tag：
    ``type``/``text``/``url``/``media``/``mime_type``/``name``）。空列表写成空串（NULL），
    与 Go 的 ``storedMessageSegments``（纯文本时不落 segments）保持一致。
    """
    if not segments:
        return ""
    return json.dumps([segment.to_json() for segment in segments], ensure_ascii=False, separators=(",", ":"))


# --- 领域模型 --------------------------------------------------------------


@dataclass
class Session:
    """会话。对应表 ``sessions``（migrations v1）。"""

    id: str = ""
    parent_session_id: str = ""
    fork_from_message_id: str = ""
    owner_id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    mode: str = ""
    title: str = ""
    status: str = ""
    metadata: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    pinned_at: datetime | None = None


@dataclass
class Message:
    """会话消息。对应表 ``messages``（migrations v1，``segments`` 列见 v11）。

    ``content`` 是纯文本快速路径；``segments`` 保存可选多模态正文（JSON 文本，
    空串 = 无多模态内容），两者可以同时存在。
    """

    id: str = ""
    session_id: str = ""
    role: str = ""
    content: str = ""
    parent_message_id: str = ""
    reply_to_platform_message_id: str = ""
    reply_to_message_id: str = ""
    tool_call_id: str = ""
    segments: str = ""
    metadata: str = ""
    created_at: datetime | None = None


@dataclass
class ContextSummary:
    """上下文摘要。对应表 ``context_summaries``（migrations v2）。"""

    id: str = ""
    session_id: str = ""
    from_message_id: str = ""
    to_message_id: str = ""
    summary: str = ""
    provider: str = ""
    model: str = ""
    source_tokens: int = 0
    summary_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    trigger_reason: str = ""
    metadata: str = ""
    created_at: datetime | None = None


@dataclass
class ToolCallRecord:
    """工具调用审计记录。对应表 ``tool_call_records``（migrations v4）。"""

    id: str = ""
    session_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    actor_id: str = ""
    risk_level: str = ""
    success: bool = False
    error: str = ""
    result_preview: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


@dataclass
class CronJob:
    """定时任务。对应表 ``cron_jobs``（migrations v5，``delivery_*`` 见 v10）。"""

    id: str = ""
    name: str = ""
    handler: str = ""
    schedule: str = ""
    enabled: bool = False
    metadata: str = ""
    delivery_state: str = ""
    delivery_token: str = ""
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AtrinisEvent:
    """Atrinis 事件。对应表 ``atrinis_events``（migrations v7，v8 追加工具声明列）。"""

    id: str = ""
    event_key: str = ""
    token_name: str = ""
    atriwisp_name: str = ""
    source: str = ""
    source_id: str = ""
    tags: str = ""
    mode: str = ""
    model_slot: str = ""
    content_hash: str = ""
    tool_declarations: str = ""
    tool_hash: str = ""
    requested_targets: str = ""
    resolved_targets: str = ""
    status: str = ""
    session_id: str = ""
    result: str = ""
    error: str = ""
    received_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AtrinisReportDelivery:
    """Atrinis 报告投递行。对应表 ``atrinis_report_deliveries``（migrations v9）。"""

    id: str = ""
    event_id: str = ""
    ordinal: int = 0
    target: str = ""
    output: str = ""
    message_id: str = ""
    status: str = ""
    receipt: str = ""
    error: str = ""
    attempts: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ChatMessage:
    """入站聊天历史。对应表 ``chat_messages``（``chat_history_repository.go``）。

    ``seq`` 是 SQLite ``INTEGER PRIMARY KEY AUTOINCREMENT``，由库里分配。
    """

    seq: int = 0
    id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    scope_type: str = ""
    platform_message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    raw: str = ""
    segments: str = ""
    reply_to_platform_message_id: str = ""
    metadata: str = ""
    created_at: datetime | None = None


@dataclass
class OutboundMessage:
    """实际发送出去的助手消息。对应表 ``outbound_messages``。

    与入站聊天历史分开存放，供学习/分析重建"用户 → 助手"配对，不污染普通历史。
    """

    seq: int = 0
    id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    platform_message_id: str = ""
    text: str = ""
    segments: str = ""
    created_at: datetime | None = None


@dataclass
class Media:
    """媒体对象。对应表 ``media``（migrations v12，v14 追加 ``deleting``）。

    ``orphaned_at`` 列由 SQLite 触发器维护（v14），Go 领域模型不含该字段，本仓同样不暴露。
    """

    deleting: bool = False
    id: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0
    local_path: str = ""
    backend: str = ""
    object_key: str = ""
    source_platform: str = ""
    source_url: str = ""
    source_file_id: str = ""
    created_at: datetime | None = None
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class HistoryMedia:
    """一条历史消息里的有序媒体位置。对应表 ``media_history``（migrations v16）。

    ``media_index`` 从 1 开始（表上有 ``CHECK(media_index>0)``）；``owner_id`` 列带
    UNIQUE 约束，主键是 ``(platform, scope_id, message_id, media_index)``。
    """

    history_id: str = ""
    platform: str = ""
    scope_id: str = ""
    message_id: str = ""
    media_index: int = 0
    kind: str = ""
    media_id: str = ""
    owner_id: str = ""


@dataclass
class MediaReference:
    """媒体引用。对应表 ``media_references``（migrations v12）。

    主键是 ``(media_id, owner_type, owner_id, purpose)``。
    """

    media_id: str = ""
    owner_type: str = ""
    owner_id: str = ""
    purpose: str = ""
    session_id: str = ""
    created_at: datetime | None = None


@dataclass
class MediaOutput:
    """发送输出索引。对应表 ``media_outputs``（migrations v15）。

    主键是 ``(platform, scope_id, message_id, segment_index)``。
    """

    platform: str = ""
    scope_id: str = ""
    message_id: str = ""
    segment_index: int = 0
    kind: str = ""
    media_id: str = ""
    owner_id: str = ""
    expires_at: datetime | None = None


@dataclass
class PlatformMessageMap:
    """平台消息 ID → 会话消息 ID 映射。对应表 ``platform_message_map``（migrations v1）。

    唯一键是 ``(platform, platform_scope_id, platform_message_id)``。
    """

    id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    platform_message_id: str = ""
    message_id: str = ""
    session_id: str = ""
    created_at: datetime | None = None


@dataclass
class SessionSummary:
    """会话列表行（``ListSessions`` 的返回）。不是一张表，字段来自 ``sessions`` 加聚合列。"""

    id: str = ""
    owner_id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    title: str = ""
    mode: str = ""
    status: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    pinned_at: datetime | None = None
    message_count: int = 0
    last_user_preview: str = ""
    last_bot_preview: str = ""
    message_preview: str = ""


@dataclass
class ToolUsageSummary:
    """单个工具在一段会话里的调用次数（``ToolCallRepository.UsageBySession``）。"""

    tool_name: str = ""
    count: int = 0


# --- 请求参数结构（Go 同名 struct，均不落库） ------------------------------


@dataclass
class ChatHistoryRangeRequest:
    """``ChatHistoryRangeRepository.ListRange`` 的时间窗参数。"""

    platform: str = ""
    platform_scope_id: str = ""
    since: datetime | None = None
    until: datetime | None = None
    after_seq: int = 0
    limit: int = 0


@dataclass
class OutboundMessageRangeRequest:
    """``OutboundMessageRepository.ListRange`` / ``CountRange`` 的时间窗参数。"""

    platform: str = ""
    platform_scope_id: str = ""
    since: datetime | None = None
    until: datetime | None = None
    after_seq: int = 0
    limit: int = 0


@dataclass
class ChatHistorySearchRequest:
    """``ChatHistoryRepository.Search`` 的检索参数。"""

    platform: str = ""
    platform_scope_id: str = ""
    query_terms: list[str] = field(default_factory=list)
    query_mode: str = ""
    sender_id: str = ""
    sender_name_query: str = ""
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 0


@dataclass
class ChatHistoryAroundRequest:
    """``ChatHistoryRepository.Around`` 的上下文窗口参数。"""

    platform: str = ""
    platform_scope_id: str = ""
    platform_message_id: str = ""
    before: int = 0
    after: int = 0


@dataclass
class CronJobRunState:
    """``CronJobRepository.UpdateRunState`` 的写入参数。"""

    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    last_error: str = ""
    enabled: bool = False
    updated_at: datetime | None = None


@dataclass
class UpsertCronJobRequest:
    """``CronJobRepository.Upsert`` 的写入参数。"""

    name: str = ""
    handler: str = ""
    schedule: str = ""
    enabled: bool = False
    metadata: str = ""
    next_run_at: datetime | None = None
    reset_delivery: bool = False


@dataclass
class CreateAtrinisEventRequest:
    """``AtrinisEventRepository.Create`` 的写入参数。"""

    media_ids: list[str] = field(default_factory=list)
    event_key: str = ""
    token_name: str = ""
    atriwisp_name: str = ""
    source: str = ""
    source_id: str = ""
    tags: str = ""
    mode: str = ""
    model_slot: str = ""
    content_hash: str = ""
    tool_declarations: str = ""
    tool_hash: str = ""
    requested_targets: str = ""
    resolved_targets: str = ""
    status: str = ""
    result: str = ""
    error: str = ""
    received_at: datetime | None = None
    created_at: datetime | None = None


@dataclass
class UpdateAtrinisEventRequest:
    """``AtrinisEventRepository.Update`` 的写入参数。"""

    id: str = ""
    resolved_targets: str = ""
    status: str = ""
    session_id: str = ""
    result: str = ""
    error: str = ""


@dataclass
class CreateAtrinisReportDeliveryRequest:
    """``AtrinisEventRepository.PrepareReport`` 里的一行投递参数。"""

    target: str = ""
    output: str = ""
    message_id: str = ""


@dataclass
class PrepareAtrinisReportRequest:
    """``AtrinisEventRepository.PrepareReport`` 的写入参数（单事务）。"""

    event_id: str = ""
    resolved_targets: str = ""
    session_id: str = ""
    result: str = ""
    deliveries: list[CreateAtrinisReportDeliveryRequest] = field(default_factory=list)
    result_ready_status: str = ""


@dataclass
class ListSessionsRequest:
    """``SessionRepository.List`` 的过滤参数。"""

    actor_id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    include_all_platforms: bool = False
    include_same_platform_cron: bool = False
    include_archived: bool = False
    archived_only: bool = False
    exclude_session_id: str = ""
    order_by_updated_at: bool = False
    query: str = ""
    limit: int = 0
    offset: int = 0
