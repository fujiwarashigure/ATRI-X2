"""存储 repository 抽象。对应 Go：``internal/storage/storage.go``（repository
interface 部分；领域模型见 :mod:`atri_x.storage.models`）。

移植约定：

* Go 的 interface → :class:`typing.Protocol`，方法名 PascalCase → ``snake_case``。
* Go 的 ``(T, error)`` → 返回值 + 抛异常；查不到时抛 :data:`atri_x.errors.ErrNotFound`
  （Go 的 ``storage.ErrNotFound``，本包在 :mod:`atri_x.storage.storage` 再导出）。
* Go 的 ``ctx context.Context`` **不进入签名**：本仓 SQLite 实现是同步的
  （``threading.Lock``），取消由调用方在协程边界处理。异步实现若需要取消信号，
  在自己的类上追加参数即可，调用点 ``repo.get(id)`` 不受影响。
* 可空列用空串 / ``None`` 表示 NULL，与 :mod:`atri_x.storage.models` 的字段语义一致。

方法是抽象而不是实现：具体 SQL 由 ``atri_x/storage/sqlite/`` 提供。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from atri_x.storage.models import (
    AtrinisEvent,
    AtrinisReportDelivery,
    ChatHistoryAroundRequest,
    ChatHistoryRangeRequest,
    ChatHistorySearchRequest,
    ChatMessage,
    ContextSummary,
    CreateAtrinisEventRequest,
    CronJob,
    CronJobRunState,
    HistoryMedia,
    ListSessionsRequest,
    Media,
    MediaOutput,
    MediaReference,
    Message,
    OutboundMessage,
    OutboundMessageRangeRequest,
    PlatformMessageMap,
    PrepareAtrinisReportRequest,
    Session,
    SessionSummary,
    ToolCallRecord,
    ToolUsageSummary,
    UpdateAtrinisEventRequest,
    UpsertCronJobRequest,
)


@runtime_checkable
class SessionRepository(Protocol):
    """会话仓储。对应 Go ``SessionRepository``。"""

    def create(self, session: Session) -> None:
        """插入一行会话（``session.id`` 为空时由实现生成）。"""
        ...

    def get(self, id: str) -> Session:
        """按主键取会话；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def update(self, session: Session) -> None:
        """整行覆盖更新；**只用于测试夹具**。

        并发写入方（活动时间、workspace、工具缓存、命名）必须走 :meth:`mutate`：
        拿着陈旧快照整行写回会静默丢掉别人的字段。
        """
        ...

    def mutate(self, id: str, update: Callable[[Session], None]) -> Session:
        """单事务读-改-写同一行并返回最新会话，是会话字段的唯一并发安全写入口。

        对应 Go ``Mutate(ctx, id, update func(*Session) error) (*Session, error)``。
        ``update`` 在事务内被调用，直接原地修改传入的 :class:`Session`；抛异常即中止
        整个事务（Go 里返回 error 让事务回滚）并向上传播。
        """
        ...

    def list(self, req: ListSessionsRequest) -> list[SessionSummary]:
        """按过滤条件列会话摘要。"""
        ...

    def delete(self, id: str) -> None:
        """删除会话（``messages`` / ``context_summaries`` 由外键级联删除）。"""
        ...

    def delete_expired(self, cutoff: datetime) -> int:
        """删除 ``updated_at`` 早于 cutoff 的会话，返回删除行数。"""
        ...


@runtime_checkable
class MessageRepository(Protocol):
    """会话消息仓储。对应 Go ``MessageRepository``。"""

    def append(self, message: Message) -> None:
        """追加一条消息（``created_at`` 为零值时由实现填充）。"""
        ...

    def get(self, id: str) -> Message:
        """按主键取消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def list_by_session(self, session_id: str) -> list[Message]:
        """按创建顺序列出会话全部消息。"""
        ...

    def list_by_session_up_to(self, session_id: str, to_message_id: str) -> list[Message]:
        """列出会话里截至 ``to_message_id``（含）的消息。"""
        ...

    def list_by_session_after(self, session_id: str, after_message_id: str) -> list[Message]:
        """列出会话里 ``after_message_id`` 之后的消息。"""
        ...

    def list_by_session_after_up_to(
        self, session_id: str, after_message_id: str, to_message_id: str
    ) -> list[Message]:
        """列出会话里 ``after_message_id``（不含）到 ``to_message_id``（含）之间的消息。"""
        ...

    def map_platform_message(self, mapping: PlatformMessageMap) -> None:
        """写入 ``platform_message_map``（平台消息 ID → 会话消息 ID）。"""
        ...

    def find_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> Message:
        """按平台消息反查会话消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...


@runtime_checkable
class MediaRepository(Protocol):
    """媒体仓储。对应 Go ``MediaRepository``。"""

    def save_history(self, item: HistoryMedia) -> None:
        """写入一条历史媒体位置（``media_history`` + 引用关系由触发器维护）。"""
        ...

    def find_history(self, platform: str, scope_id: str, message_id: str) -> list[HistoryMedia]:
        """按历史消息查媒体位置，按 ``media_index`` 升序。"""
        ...

    def list_history(self, after_owner_id: str, limit: int) -> list[HistoryMedia]:
        """按 ``owner_id`` 分页遍历历史媒体位置（跨库对账用）。"""
        ...

    def delete_history(self, owner_id: str) -> None:
        """按 ``owner_id`` 删除历史媒体位置。"""
        ...

    def get(self, id: str) -> Media:
        """按主键取媒体；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def upsert(self, media: Media) -> None:
        """插入或更新一行媒体。"""
        ...

    def delete_orphans(self, cutoff: datetime) -> list[Media]:
        """**只查询**孤儿媒体并返回，不删除任何行。

        ⚠️ 名字有歧义，但这是 Go ``DeleteOrphans`` 的真实行为：它只做 SELECT。
        真正删除由 :meth:`finish_delete` 完成，且要求那行已经 ``deleting=1``
        且没有任何引用。语义错误的 docstring 曾让集成方写过错的调用顺序。
        """
        ...

    def claim_orphans(self, cutoff: datetime) -> list[Media]:
        """认领孤儿媒体（先标记 ``deleting`` 再返回），供可重试后端清理。"""
        ...

    def finish_delete(self, id: str) -> None:
        """清理完成后删除媒体行。"""
        ...

    def touch(self, id: str, now: datetime) -> None:
        """更新 ``last_accessed_at``。"""
        ...

    def save_output(self, output: MediaOutput) -> None:
        """写入一条发送输出索引（``media_outputs``）。"""
        ...

    def find_outputs(
        self, platform: str, scope_id: str, message_id: str, now: datetime
    ) -> list[MediaOutput]:
        """按发送消息查未过期的输出索引。"""
        ...

    def expire_outputs(self, now: datetime) -> None:
        """清理已过期的输出索引。"""
        ...

    def check_references(self) -> list[str]:
        """只读一致性检查，返回没有引用行的媒体 ID。"""
        ...

    def recover_interrupted(self) -> None:
        """启动恢复：把中断的 ``deleting`` 标记复位。"""
        ...


@runtime_checkable
class MediaReferenceRepository(Protocol):
    """媒体引用仓储。对应 Go ``MediaReferenceRepository``。"""

    def add(self, reference: MediaReference) -> None:
        """新增一条引用（重复主键按幂等处理）。"""
        ...

    def add_all(self, references: Sequence[MediaReference]) -> None:
        """批量新增引用（单事务）。"""
        ...

    def remove(self, reference: MediaReference) -> None:
        """删除一条引用（按主键四元组匹配）。"""
        ...

    def list_by_owner(self, owner_type: str, owner_id: str) -> list[MediaReference]:
        """列出某个 owner 的全部引用。"""
        ...

    def list_media_ids(self, media_id: str) -> list[MediaReference]:
        """列出某个媒体的全部引用。"""
        ...


@runtime_checkable
class ToolCallRepository(Protocol):
    """工具调用审计仓储。对应 Go ``ToolCallRepository``。"""

    def create(self, record: ToolCallRecord) -> None:
        """写入一条工具调用记录。"""
        ...

    def successful_ids(self, tool_call_ids: Sequence[str]) -> dict[str, bool]:
        """返回给定工具调用 ID 中"成功过"的子集（Go 返回 ``map[string]bool``）。"""
        ...

    def usage_by_session(self, session_id: str) -> list[ToolUsageSummary]:
        """按工具名统计会话内的调用次数。"""
        ...


@runtime_checkable
class CronJobRepository(Protocol):
    """定时任务仓储。对应 Go ``CronJobRepository``。"""

    def upsert(self, req: UpsertCronJobRequest) -> CronJob:
        """按 ``name`` 插入或更新任务并返回最新行。"""
        ...

    def get_by_name(self, name: str) -> CronJob:
        """按名字取任务；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def list(self, include_disabled: bool) -> list[CronJob]:
        """列出任务（``include_disabled`` 为假时只返回启用的）。"""
        ...

    def list_enabled(self) -> list[CronJob]:
        """列出启用中的任务。"""
        ...

    def update_next_run_at(self, id: str, next_run_at: datetime | None, updated_at: datetime) -> None:
        """只更新 ``next_run_at`` / ``updated_at``。"""
        ...

    def update_run_state(self, id: str, state: CronJobRunState) -> None:
        """写入一次执行的运行状态。"""
        ...

    def compare_and_swap_delivery(
        self, id: str, expected_token: str, next_token: str, delivery_state: str
    ) -> bool:
        """投递令牌 CAS；令牌不匹配时返回 ``False``（不写任何东西）。"""
        ...

    def disable_by_name(self, name: str) -> None:
        """按名字停用任务。"""
        ...

    def disable_by_name_if_delivery_token(self, name: str, delivery_token: str) -> bool:
        """仅当投递令牌匹配时停用任务；不匹配返回 ``False``。"""
        ...

    def delete_by_name(self, name: str) -> None:
        """按名字删除任务。"""
        ...


@runtime_checkable
class AtrinisEventRepository(Protocol):
    """Atrinis 事件与报告投递仓储。对应 Go ``AtrinisEventRepository``。"""

    def create(self, req: CreateAtrinisEventRequest) -> AtrinisEvent:
        """写入事件（含媒体引用），返回落库后的事件行。"""
        ...

    def get(self, id: str) -> AtrinisEvent:
        """按主键取事件；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def get_by_key(self, atriwisp_name: str, source: str, source_id: str) -> AtrinisEvent:
        """按 ``(atriwisp_name, source, source_id)`` 去重键取事件；不存在抛 ErrNotFound。"""
        ...

    def update(self, req: UpdateAtrinisEventRequest) -> None:
        """更新事件的解析目标、状态、会话与结果。"""
        ...

    def prepare_report(self, req: PrepareAtrinisReportRequest) -> None:
        """单事务写入报告结果与全部投递行。

        ``ordinal`` 按 ``deliveries`` 顺序从 **0** 起（对齐 Go 的
        ``for ordinal, delivery := range req.Deliveries``）。
        文档里曾写成"从 1 起"，那是错的。
        """
        ...

    def reset_delivering_reports(self, delivering_status: str, result_ready_status: str) -> None:
        """重启恢复：把卡在 ``delivering`` 的投递复位回 ``result_ready``。"""
        ...

    def list_result_ready_report_ids(self, result_ready_status: str) -> list[str]:
        """列出待投递（``result_ready``）的事件 ID。"""
        ...

    def claim_report(self, event_id: str, result_ready_status: str, delivering_status: str) -> bool:
        """认领一次报告投递（状态 CAS）；已被别人认领时返回 ``False``。"""
        ...

    def release_report(
        self,
        event_id: str,
        delivering_status: str,
        result_ready_status: str,
        delivery_error: str,
    ) -> None:
        """释放认领（投递失败但可重试）。"""
        ...

    def list_report_deliveries(self, event_id: str) -> list[AtrinisReportDelivery]:
        """按 ``ordinal`` 升序列出事件的投递行。"""
        ...

    def start_report_delivery(self, id: str) -> None:
        """标记一条投递开始（``attempts`` 加一）。"""
        ...

    def mark_report_delivery_failed(
        self, event_id: str, delivery_id: str, result_ready_status: str, delivery_error: str
    ) -> None:
        """标记一条投递失败，并把事件放回待投递状态。"""
        ...

    def mark_report_delivery_delivered(self, delivery_id: str, receipt: str) -> None:
        """标记一条投递成功（含发送回执）。"""
        ...

    def complete_report(self, event_id: str, delivering_status: str, completed_status: str) -> None:
        """全部投递完成后把事件推进到 ``completed``。"""
        ...


@runtime_checkable
class ChatHistoryRepository(Protocol):
    """入站聊天历史仓储。对应 Go ``ChatHistoryRepository``。"""

    def append(self, message: ChatMessage) -> None:
        """写入一条历史消息（按平台消息 ID 冲突覆盖）。"""
        ...

    def get_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> ChatMessage:
        """按平台消息 ID 取历史消息；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def search(self, req: ChatHistorySearchRequest) -> list[ChatMessage]:
        """按关键词/发送者/时间窗检索（返回按 ``seq`` 升序）。"""
        ...

    def around(self, req: ChatHistoryAroundRequest) -> list[ChatMessage]:
        """取目标消息前后的上下文窗口（含目标本身）。"""
        ...

    def delete_before(self, cutoff: datetime) -> int:
        """删除 ``created_at`` 早于 cutoff 的历史消息，返回删除行数。"""
        ...


@runtime_checkable
class ChatHistoryRangeRepository(Protocol):
    """历史批量时间窗读取（可选能力）。对应 Go ``ChatHistoryRangeRepository``。

    与 :class:`ChatHistoryRepository` 分开，老的假实现/适配器不必跟着改。
    """

    def list_range(self, req: ChatHistoryRangeRequest) -> list[ChatMessage]:
        """按 ``seq`` 升序批量读取时间窗内的历史消息（``limit`` 上限 5000）。"""
        ...


@runtime_checkable
class OutboundMessageRepository(Protocol):
    """已发送助手消息仓储。对应 Go ``OutboundMessageRepository``。"""

    def append(self, message: OutboundMessage) -> None:
        """写入一条已发送消息。"""
        ...

    def list_range(self, req: OutboundMessageRangeRequest) -> list[OutboundMessage]:
        """按 ``seq`` 升序批量读取时间窗内的已发送消息（``limit`` 上限 5000）。"""
        ...

    def delete_before(self, cutoff: datetime) -> int:
        """删除 ``created_at`` 早于 cutoff 的已发送消息，返回删除行数。"""
        ...


@runtime_checkable
class OutboundMessageCounter(Protocol):
    """已发送消息计数（可选能力）。对应 Go ``OutboundMessageCounter``。

    群分析用它统计出站量，避免为此加载整页行。
    """

    def count_range(self, req: OutboundMessageRangeRequest) -> int:
        """统计时间窗内的已发送消息条数。"""
        ...


@runtime_checkable
class ContextSummaryRepository(Protocol):
    """上下文摘要仓储。对应 Go ``ContextSummaryRepository``。"""

    def create(self, summary: ContextSummary) -> None:
        """写入一条摘要。"""
        ...

    def latest_by_session(self, session_id: str) -> ContextSummary:
        """取会话最新摘要；不存在时抛 :data:`atri_x.errors.ErrNotFound`。"""
        ...

    def latest_by_session_up_to(self, session_id: str, to_message_id: str) -> ContextSummary:
        """取会话里截至 ``to_message_id``（含）的最新摘要；不存在抛 ErrNotFound。"""
        ...


@runtime_checkable
class Store(Protocol):
    """存储门面。对应 Go ``Store``。

    聊天历史（``chat_messages`` / ``outbound_messages``）在 Go 里是独立 store，
    因此这里也不出现在 :class:`Store` 上。
    """

    def sessions(self) -> SessionRepository:
        ...

    def messages(self) -> MessageRepository:
        ...

    def media(self) -> MediaRepository:
        ...

    def media_references(self) -> MediaReferenceRepository:
        ...

    def context_summaries(self) -> ContextSummaryRepository:
        ...

    def tool_calls(self) -> ToolCallRepository:
        ...

    def cron_jobs(self) -> CronJobRepository:
        ...

    def atrinis_events(self) -> AtrinisEventRepository:
        ...

    def close(self) -> None:
        ...


__all__ = [
    "AtrinisEventRepository",
    "ChatHistoryRangeRepository",
    "ChatHistoryRepository",
    "ContextSummaryRepository",
    "CronJobRepository",
    "MediaReferenceRepository",
    "MediaRepository",
    "MessageRepository",
    "OutboundMessageCounter",
    "OutboundMessageRepository",
    "SessionRepository",
    "Store",
    "ToolCallRepository",
]
