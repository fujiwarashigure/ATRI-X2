"""群聊统计（clean-room），只依赖 ``chat_history`` / ``outbound_messages``。

对应 Go：``internal/groupanalysis/service.go``（可选摘要器见
:mod:`atri_x.groupanalysis.summarizer`，来自 Go 的 ``summarizer.go``）。

本模块不做任何模型调用：:class:`Service` 只消费
:class:`atri_x.storage.repositories.ChatHistoryRangeRepository` 与
:class:`atri_x.storage.repositories.OutboundMessageRepository`；摘要由调用方注入的
``Summarizer`` 决定。

**与 Go 的对应关系**：``Analyze`` 分页扫描入站、只计数出站，逐页聚合而不保留
整段历史；统计口径（消息数、字数只算文本、活跃成员排行、小时分布、截断标记）
与 Go 逐项一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Callable, Sequence

from atri_x.errors import InvalidError
from atri_x.safecontext import inline, truncate_runes
from atri_x.storage.models import (
    ChatHistoryRangeRequest,
    ChatMessage,
    OutboundMessageRangeRequest,
)
from atri_x.storage.repositories import (
    ChatHistoryRangeRepository,
    OutboundMessageCounter,
    OutboundMessageRepository,
)

if TYPE_CHECKING:  # 只为类型标注；运行时不 import，避免与 summarizer.py 形成环。
    from atri_x.groupanalysis.summarizer import Summarizer

__all__ = [
    "MAX_RANGE_LIMIT",
    "MAX_SENDER_NAME_RUNES",
    "MAX_TOP_SENDERS",
    "DEFAULT_RANGE_LIMIT",
    "Report",
    "Request",
    "SenderStat",
    "Service",
    "format_time",
    "new_service",
    "peak_hour",
    "report_inline",
    "top_senders",
]

#: 单页读取上限（与 Go ``defaultRangeLimit`` 相同）。对应
#: ``storage.ChatHistoryRangeRequest.limit`` 的 5000 上限。
DEFAULT_RANGE_LIMIT = 5000
#: 一次分析最多读取的消息条数上限。对应 Go ``maxRangeLimit``。
MAX_RANGE_LIMIT = 200000
#: 活跃成员排行长度。对应 Go ``maxTopSenders``。
MAX_TOP_SENDERS = 10
#: 报告中平台/会话/昵称字段的 rune 上限。对应 Go ``maxSenderNameRunes``。
MAX_SENDER_NAME_RUNES = 32


def _require_repository(history: Any) -> None:
    if history is None:
        raise InvalidError("group analysis history repository is not configured")


def _location(value: datetime | None) -> tzinfo:
    """取 Go ``time.Time.Location()`` 的等价物（零值时间的 Location 是 UTC）。"""
    if value is None or value.tzinfo is None:
        return timezone.utc
    return value.tzinfo


def _location_name(loc: tzinfo) -> str:
    name = loc.tzname(None)
    return name or str(loc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Request:
    """一次群分析的平台/时间窗选择。对应 Go ``Request``。"""

    platform: str = ""
    scope_id: str = ""
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 0


@dataclass
class SenderStat:
    """单个活跃成员的汇总。对应 Go ``SenderStat``。"""

    sender_id: str = ""
    sender_name: str = ""
    messages: int = 0
    chars: int = 0


@dataclass
class Report:
    """一次群分析的结果（平台无关）。对应 Go ``Report``。"""

    platform: str = ""
    scope_id: str = ""
    since: datetime | None = None
    until: datetime | None = None
    time_zone: str = ""
    inbound_messages: int = 0
    outbound_messages: int = 0
    active_senders: int = 0
    total_chars: int = 0
    #: 24 个小时的入站条数（Go ``[24]int``）。
    hourly_counts: list[int] = field(default_factory=lambda: [0] * 24)
    top_senders: list[SenderStat] = field(default_factory=list)
    #: 任一方向触达读取上限时为真。
    truncated: bool = False
    #: 出站只做计数、入站做聚合，所以分别标记是哪一侧被截断。
    inbound_truncated: bool = False
    outbound_truncated: bool = False
    #: 实际读取到的入站行数。
    scanned_messages: int = 0

    def format_text(self) -> str:
        """渲染确定性文本报告。对应 Go ``Report.FormatText``。

        昵称等不可信字段一律走 :func:`report_inline`（折叠成一行 + 转义边界 +
        rune 截断），因此无法注入换行、控制字符或伪造 prompt 边界。
        """
        loc = _location(self.since)
        time_zone = (self.time_zone or "").strip() or _location_name(loc)
        lines: list[str] = [
            "群聊分析报告",
            f"平台：{report_inline(self.platform, MAX_SENDER_NAME_RUNES)}",
            f"会话：{report_inline(self.scope_id, MAX_SENDER_NAME_RUNES)}",
            "统计区间：{} ~ {}（{}）".format(
                format_time(self.since, loc), format_time(self.until, loc), time_zone
            ),
            f"消息数：入站 {self.inbound_messages}，出站 {self.outbound_messages}",
            f"实际扫描：入站 {self.scanned_messages} 条",
            f"活跃成员：{self.active_senders}",
            f"总字数：{self.total_chars}",
            f"说明：消息数按聊天记录统计，字数只统计文本；短时间窗按本地时区 {time_zone} 计算。",
        ]
        if self.inbound_truncated and self.outbound_truncated:
            lines.append(
                "注意：入站和出站消息都达到了读取上限，统计可能不完整；请缩小时间范围或提高 max_messages。"
            )
        elif self.inbound_truncated:
            lines.append(
                "注意：入站消息达到读取上限，入站统计可能不完整；请缩小时间范围或提高 max_messages。"
            )
        elif self.outbound_truncated:
            lines.append(
                "注意：出站消息达到读取上限，出站统计可能不完整；请缩小时间范围或提高 max_messages。"
            )
        elif self.truncated:
            lines.append("注意：已达到读取上限，统计可能不完整；请缩小时间范围或提高 max_messages。")
        if self.top_senders:
            lines.append("活跃成员 Top：")
            for index, stat in enumerate(self.top_senders):
                name = report_inline(stat.sender_name, MAX_SENDER_NAME_RUNES)
                if name == "":
                    name = report_inline(stat.sender_id, MAX_SENDER_NAME_RUNES)
                lines.append(f"{index + 1}. {name}：{stat.messages} 条 / {stat.chars} 字")
        hour, count = peak_hour(self.hourly_counts)
        if count > 0:
            lines.append(f"最活跃时段：{hour:02d}:00，{count} 条")
        return "\n".join(lines).strip()


class Service:
    """读取本地历史并计算统计。对应 Go ``Service``。

    刻意不依赖 LLM provider 或报告渲染器：摘要器是可选的，缺省时
    :meth:`summarize` 返回空串。
    """

    def __init__(
        self,
        history: ChatHistoryRangeRepository | None,
        outbound: OutboundMessageRepository | None = None,
        summarizer: "Summarizer | None" = None,
    ) -> None:
        self.history = history
        self.outbound = outbound
        self.summarizer = summarizer

    # -- 统计 ---------------------------------------------------------------

    def analyze(self, req: Request) -> Report:
        """计算时间窗内的入站/出站统计。对应 Go ``Service.Analyze``。"""
        _require_repository(self.history)
        platform = (req.platform or "").strip()
        scope_id = (req.scope_id or "").strip()
        if platform == "" or scope_id == "":
            raise InvalidError("group analysis platform and scope are required")
        since = req.since
        until = req.until
        if _is_zero_time(since):
            since = _now() - timedelta(days=1)
        if _is_zero_time(until):
            until = _now()
        if not since < until:  # type: ignore[operator]
            raise InvalidError("group analysis since must be before until")
        limit = req.limit
        if limit <= 0:
            limit = DEFAULT_RANGE_LIMIT
        if limit > MAX_RANGE_LIMIT:
            limit = MAX_RANGE_LIMIT
        loc = _location(since)

        report = Report(
            platform=platform,
            scope_id=scope_id,
            since=since,
            until=until,
            time_zone=_location_name(loc),
        )
        senders: dict[str, SenderStat] = {}

        # 入站逐页聚合、不整段保留，因此 20 万条的时间窗也不必装进内存。
        scanned, inbound_truncated = self._scan_inbound(
            platform, scope_id, since, until, limit, lambda page: _visit(report, senders, page, loc)
        )
        report.scanned_messages = scanned
        report.inbound_truncated = inbound_truncated

        if self.outbound is not None:
            outbound_count, outbound_truncated = self._count_outbound(
                platform, scope_id, since, until, limit
            )
            report.outbound_messages = outbound_count
            report.outbound_truncated = outbound_truncated

        report.truncated = report.inbound_truncated or report.outbound_truncated
        report.active_senders = len(senders)
        report.top_senders = top_senders(senders, MAX_TOP_SENDERS)
        return report

    def _scan_inbound(
        self,
        platform: str,
        scope_id: str,
        since: datetime,
        until: datetime,
        total_limit: int,
        visit: Callable[[Sequence[ChatMessage]], None],
    ) -> tuple[int, bool]:
        """按 5000 行分页读取入站，逐页交给 ``visit``。对应 Go ``scanInbound``。"""
        scanned = 0
        after_seq = 0
        while scanned < total_limit:
            fetch = total_limit - scanned
            if fetch > DEFAULT_RANGE_LIMIT:
                fetch = DEFAULT_RANGE_LIMIT
            page = self.history.list_range(  # type: ignore[union-attr]
                ChatHistoryRangeRequest(
                    platform=platform,
                    platform_scope_id=scope_id,
                    since=since,
                    until=until,
                    after_seq=after_seq,
                    limit=fetch,
                )
            )
            if page:
                visit(page)
                scanned += len(page)
                after_seq = page[-1].seq
            if len(page) < fetch:
                return scanned, False
            if scanned >= total_limit:
                probe = self.history.list_range(  # type: ignore[union-attr]
                    ChatHistoryRangeRequest(
                        platform=platform,
                        platform_scope_id=scope_id,
                        since=since,
                        until=until,
                        after_seq=after_seq,
                        limit=1,
                    )
                )
                return scanned, len(probe) > 0
        return scanned, False

    def _count_outbound(
        self,
        platform: str,
        scope_id: str,
        since: datetime,
        until: datetime,
        total_limit: int,
    ) -> tuple[int, bool]:
        """统计出站条数。实现 ``count_range`` 时走单条 COUNT，否则分页计数。"""
        counter = self.outbound
        if isinstance(counter, OutboundMessageCounter):
            count = counter.count_range(
                OutboundMessageRangeRequest(
                    platform=platform,
                    platform_scope_id=scope_id,
                    since=since,
                    until=until,
                )
            )
            return count, False
        count = 0
        after_seq = 0
        while count < total_limit:
            fetch = total_limit - count
            if fetch > DEFAULT_RANGE_LIMIT:
                fetch = DEFAULT_RANGE_LIMIT
            page = counter.list_range(  # type: ignore[union-attr]
                OutboundMessageRangeRequest(
                    platform=platform,
                    platform_scope_id=scope_id,
                    since=since,
                    until=until,
                    after_seq=after_seq,
                    limit=fetch,
                )
            )
            count += len(page)
            if len(page) < fetch:
                return count, False
            after_seq = page[-1].seq
            if count >= total_limit:
                probe = counter.list_range(  # type: ignore[union-attr]
                    OutboundMessageRangeRequest(
                        platform=platform,
                        platform_scope_id=scope_id,
                        since=since,
                        until=until,
                        after_seq=after_seq,
                        limit=1,
                    )
                )
                return count, len(probe) > 0
        return count, False

    # -- 摘要 ---------------------------------------------------------------

    async def summarize(self, ctx: Any, report: Report) -> str:
        """使用可选摘要器。对应 Go ``Service.Summarize``（未注入时返回空串）。"""
        if self.summarizer is None:
            return ""
        return await self.summarizer.summarize(ctx, report)


def _visit(
    report: Report,
    senders: dict[str, SenderStat],
    page: Sequence[ChatMessage],
    loc: tzinfo,
) -> None:
    """把一页入站行聚合进报告（Go ``Analyze`` 里的 page 回调）。"""
    for row in page:
        # 没有文本的消息（例如纯图片/文件，带 segments）也算一条消息；
        # 字数只统计文本。
        report.inbound_messages += 1
        text = (row.text or "").strip()
        report.total_chars += len(text)
        created_at = row.created_at
        if not _is_zero_time(created_at):
            hour = _hour_in(created_at, loc)
            if 0 <= hour < 24:
                report.hourly_counts[hour] += 1
        sender_id = (row.sender_id or "").strip()
        if sender_id == "":
            continue
        stat = senders.get(sender_id)
        if stat is None:
            stat = SenderStat(sender_id=sender_id, sender_name=(row.sender_name or "").strip())
            senders[sender_id] = stat
        name = (row.sender_name or "").strip()
        if stat.sender_name == "" and name != "":
            stat.sender_name = name
        stat.messages += 1
        stat.chars += len(text)


def _hour_in(value: datetime, loc: tzinfo) -> int:
    if value.tzinfo is None:
        # PORTING §1.7 要求 aware；naive 值按时间窗所在时区解释，而不是静默丢进 UTC。
        return value.hour
    return value.astimezone(loc).hour


def _is_zero_time(value: datetime | None) -> bool:
    """``None`` 或 Go 的零值时间（``0001-01-01T00:00:00``）。"""
    if value is None:
        return True
    return value.replace(tzinfo=None) == datetime(1, 1, 1)


def top_senders(senders: dict[str, SenderStat], limit: int) -> list[SenderStat]:
    """按条数 → 字数 → 成员 ID 排序取前 ``limit``。对应 Go ``topSenders``。"""
    out = list(senders.values())
    out.sort(key=lambda stat: (-stat.messages, -stat.chars, stat.sender_id))
    if len(out) > limit:
        out = out[:limit]
    return out


def peak_hour(counts: Sequence[int]) -> tuple[int, int]:
    """最活跃小时（并列取最小小时号）。对应 Go ``peakHour``。"""
    hour, count = 0, 0
    for index, value in enumerate(counts):
        if value > count:
            hour, count = index, value
    return hour, count


def report_inline(value: str, limit: int) -> str:
    """把不可信的单个字段折叠成有界的一行。对应 Go ``reportInline``。"""
    return truncate_runes(inline(value or ""), limit)


def format_time(value: datetime | None, loc: tzinfo | None) -> str:
    """报告里的时间渲染。对应 Go ``formatTime``（零值时间为 ``-``）。"""
    if _is_zero_time(value):
        return "-"
    if loc is None:
        loc = timezone.utc
    moment = value if value.tzinfo is not None else value.replace(tzinfo=loc)
    return moment.astimezone(loc).strftime("%Y-%m-%d %H:%M")


def new_service(
    history: ChatHistoryRangeRepository | None,
    outbound: OutboundMessageRepository | None = None,
    summarizer: "Summarizer | None" = None,
) -> Service:
    """对应 Go ``NewService``（``summarizer`` 是本仓新增的可选注入点）。"""
    return Service(history, outbound, summarizer)


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
NewService = new_service
