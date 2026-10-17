"""聊天历史工具族：关键词检索、上下文窗口、引用回复。

对应 Go：``internal/tool/builtin/chat_history.go``。

与 Go 的差异（仅形式）：

* ``storage.ChatHistoryRepository`` 在本仓是**同步**协议，``media.Manager`` 的
  ``history_ids`` 是**异步**方法；因此读取记录同步做、拼媒体标签异步做。
* 时间一律带时区；``parseChatHistoryTime`` 的 ``YYYY-MM-DD HH:MM[:SS]`` 按**本地
  时区**解释（与 Go 的 ``time.ParseInLocation(..., time.Local)`` 一致），其余按 UTC。
* ``reference_text`` / ``chat_message_reply`` 直接复用
  :mod:`atri_x.platform.refcontext`，不重复实现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.delivery import reply as delivery_reply
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import ToolSchema
from atri_x.media import HistorySegments
from atri_x.platform import message_context_from
from atri_x.platform.refcontext import ChatMessageReply, FormatReferenceText
from atri_x.storage import (
    ChatHistoryAroundRequest,
    ChatHistoryRepository,
    ChatHistorySearchRequest,
    ChatMessage,
)
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo, first as first_runtime_info
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

CHAT_HISTORY_DEFAULT_LIMIT = 10
CHAT_HISTORY_MAX_LIMIT = 50
CHAT_HISTORY_TEXT_LIMIT = 6000
CHAT_HISTORY_MESSAGE_LIMIT = 300

_AT_ID_PATTERN = re.compile(r"^@(.+)$")
_TERM_SPLIT_PATTERN = re.compile(r"[\s,，|]+")
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
_SELF_ALIASES = ("我", "本人", "自己")


@dataclass
class SearchChatHistoryArgs:
    """对应 Go ``searchChatHistoryArgs``。"""

    limit: int = 0
    query: str = ""
    query_mode: str = ""
    user: str = ""
    since: str = ""
    until: str = ""
    hours: int = 0
    days: int = 0


@dataclass
class AroundChatHistoryArgs:
    """对应 Go ``aroundChatHistoryArgs``。"""

    message_id: str = ""
    before: int = 0
    after: int = 0


@dataclass
class ReplyChatHistoryArgs:
    """对应 Go ``replyChatHistoryArgs``。"""

    message_id: str = ""
    message: str = ""


@dataclass
class ChatHistoryContext:
    """对应 Go ``chatHistoryContext``。"""

    platform: str = ""
    scope_id: str = ""
    platform_user_id: str = ""


class SearchChatHistoryTool:
    """对应 Go ``SearchChatHistoryTool``。"""

    def __init__(
        self,
        history: ChatHistoryRepository | None = None,
        info: RuntimeInfo | None = None,
        center: Any = None,
    ) -> None:
        self.center = center
        self.history = history
        self.info_ = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return "search_chat_history"

    def info(self) -> Info:
        return search_chat_history_builder().build_info()

    def schema(self) -> ToolSchema:
        return search_chat_history_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``SearchChatHistoryTool.Call``。"""
        if self.history is None:
            raise InvalidError("chat history storage is not configured")
        chat_ctx, err_text = current_chat_history_context(ctx)
        if err_text != "":
            return Result(content=err_text)
        args = _decode(req.arguments, SearchChatHistoryArgs, "search_chat_history")
        since, until = chat_history_time_range_at(
            args.since, args.until, args.hours, args.days, self.info_.current_time()
        )
        sender_id, sender_name, user_err = chat_history_user_filter(args.user, chat_ctx)
        if user_err != "":
            return Result(content=user_err)
        rows = self.history.search(
            ChatHistorySearchRequest(
                platform=chat_ctx.platform,
                platform_scope_id=chat_ctx.scope_id,
                query_terms=split_chat_history_terms(args.query),
                query_mode=args.query_mode,
                sender_id=sender_id,
                sender_name_query=sender_name,
                since=since,
                until=until,
                limit=clamp_chat_history_limit(args.limit),
            )
        )
        if not rows:
            return Result(content="没有查到符合条件的当前聊天历史记录。")
        lines = [f"当前聊天历史查询结果：返回 {len(rows)} 条。"]
        for row in rows:
            lines.append(await format_chat_history_media(ctx, row, "", self.center))
        lines.append(
            '仅在需要媒体时调用 get_media(message_id=["..."], media_index=[[1]]) '
            "获取媒体 ID；可用 get_chat_history_around(message_id=\"...\") 查看附近上下文；"
            '可用 reply_to_chat_history_message(message_id="...", message="...") 引用回复。'
        )
        return Result(content=chat_history_result_lines(lines))


class GetChatHistoryAroundTool:
    """对应 Go ``GetChatHistoryAroundTool``。"""

    def __init__(
        self,
        history: ChatHistoryRepository | None = None,
        info: RuntimeInfo | None = None,
        center: Any = None,
    ) -> None:
        self.center = center
        self.history = history
        self.info_ = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return "get_chat_history_around"

    def info(self) -> Info:
        return around_chat_history_builder().build_info()

    def schema(self) -> ToolSchema:
        return around_chat_history_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``GetChatHistoryAroundTool.Call``。"""
        if self.history is None:
            raise InvalidError("chat history storage is not configured")
        chat_ctx, err_text = current_chat_history_context(ctx)
        if err_text != "":
            return Result(content=err_text)
        args = _decode(req.arguments, AroundChatHistoryArgs, "get_chat_history_around")
        message_id = parse_chat_history_message_id(args.message_id)
        if message_id == "":
            return Result(
                content="message_id 必须是 search_chat_history 返回的 #message_id "
                "或纯平台消息 ID。"
            )
        try:
            rows = self.history.around(
                ChatHistoryAroundRequest(
                    platform=chat_ctx.platform,
                    platform_scope_id=chat_ctx.scope_id,
                    platform_message_id=message_id,
                    before=clamp_chat_history_window(args.before),
                    after=clamp_chat_history_window(args.after),
                )
            )
        except NotFoundError:
            return Result(
                content=f"没有在当前聊天找到 message_id 为 {message_id} 的历史记录。"
            )
        lines = [f"当前聊天消息 #{message_id} 附近上下文：返回 {len(rows)} 条。"]
        for row in rows:
            lines.append(await format_chat_history_media(ctx, row, message_id, self.center))
        return Result(content=chat_history_result_lines(lines))


class ReplyToChatHistoryMessageTool:
    """对应 Go ``ReplyToChatHistoryMessageTool``。"""

    def __init__(
        self,
        history: ChatHistoryRepository | None = None,
        info: RuntimeInfo | None = None,
    ) -> None:
        self.history = history
        self.info_ = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return "reply_to_chat_history_message"

    def info(self) -> Info:
        return reply_chat_history_builder().build_info()

    def schema(self) -> ToolSchema:
        return reply_chat_history_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``ReplyToChatHistoryMessageTool.Call``。"""
        if self.history is None:
            raise InvalidError("chat history storage is not configured")
        chat_ctx, err_text = current_chat_history_context(ctx)
        if err_text != "":
            return Result(content=err_text)
        args = _decode(
            req.arguments, ReplyChatHistoryArgs, "reply_to_chat_history_message"
        )
        message_id = parse_chat_history_message_id(args.message_id)
        if message_id == "":
            return Result(
                content="message_id 必须是 search_chat_history 返回的 #message_id "
                "或纯平台消息 ID。"
            )
        reply_text = (args.message or "").strip()
        if reply_text == "":
            return Result(content="回复内容不能为空。")
        try:
            target = self.history.get_by_platform_message(
                chat_ctx.platform, chat_ctx.scope_id, message_id
            )
        except NotFoundError:
            return Result(
                content=f"没有在当前聊天找到 message_id 为 {message_id} 的历史记录。"
            )
        return Result(
            content=(
                f"已引用回复 [#{target.platform_message_id}] "
                f"{target.sender_name}({target.sender_id}): "
                f"{truncate_chat_history_message(target.text)}\n"
                "回复内容已发送到当前聊天，请不要重复发送。"
            ),
            outputs=[delivery_reply(message_id, reply_text)],
        )


def new_search_chat_history_tool(
    history: ChatHistoryRepository | None, *infos: RuntimeInfo
) -> SearchChatHistoryTool:
    """对应 Go ``NewSearchChatHistoryTool``。"""
    return SearchChatHistoryTool(history, _first_info(infos))


def new_get_chat_history_around_tool(
    history: ChatHistoryRepository | None, *infos: RuntimeInfo
) -> GetChatHistoryAroundTool:
    """对应 Go ``NewGetChatHistoryAroundTool``。"""
    return GetChatHistoryAroundTool(history, _first_info(infos))


def new_reply_to_chat_history_message_tool(
    history: ChatHistoryRepository | None, *infos: RuntimeInfo
) -> ReplyToChatHistoryMessageTool:
    """对应 Go ``NewReplyToChatHistoryMessageTool``。"""
    return ReplyToChatHistoryMessageTool(history, _first_info(infos))


def _first_info(infos: tuple[RuntimeInfo, ...]) -> RuntimeInfo | None:
    return infos[0] if infos else None


def search_chat_history_builder() -> Builder:
    """对应 Go ``searchChatHistoryBuilder``。"""
    return (
        Builder("search_chat_history")
        .description(
            "查询当前平台当前群聊或私聊的聊天历史，可按关键词、用户和时间过滤；"
            "返回的 #message_id 可继续查看上下文或引用回复。"
        )
        .risk(RiskLow)
        .tags("chat")
        .depends_on(
            "get_chat_history_around", "reply_to_chat_history_message", "get_media"
        )
        .string(
            "query",
            "按消息正文搜索的关键词；可传多个搜索词，用空格、逗号、中文逗号、竖线或"
            "换行分隔；留空表示不过滤。",
        )
        .string("query_mode", "多个搜索词的匹配规则：or 或 and；默认 or。")
        .string("user", "按用户过滤。可传平台用户 ID、@ID、昵称，或“我”。")
        .string(
            "since",
            "起始时间。支持 Unix 时间戳、YYYY-MM-DD、YYYY-MM-DD HH:MM、"
            "YYYY-MM-DD HH:MM:SS；留空表示不限制。",
        )
        .string("until", "结束时间。格式同 since；留空表示不限制。")
        .integer("hours", "查询最近多少小时。since 为空时生效；0 表示不限制。")
        .integer("days", "查询最近多少天。since 和 hours 为空时生效；0 表示不限制。")
        .integer("limit", "返回记录数量，默认 10，最大 50。")
    )


def around_chat_history_builder() -> Builder:
    """对应 Go ``aroundChatHistoryBuilder``。"""
    return (
        Builder("get_chat_history_around")
        .description(
            "根据 search_chat_history 返回的 #message_id 查询当前聊天中该条消息附近的上下文。"
        )
        .risk(RiskLow)
        .hidden()
        .tags("chat")
        .depends_on(
            "search_chat_history", "reply_to_chat_history_message", "get_media"
        )
        .string(
            "message_id",
            "search_chat_history 返回结果中的平台消息 ID，可传 # 开头或纯 ID。",
            required(),
        )
        .integer("before", "向该消息之前查询多少条当前聊天记录，默认 10，最大 50。")
        .integer("after", "向该消息之后查询多少条当前聊天记录，默认 10，最大 50。")
    )


def reply_chat_history_builder() -> Builder:
    """对应 Go ``replyChatHistoryBuilder``。"""
    return (
        Builder("reply_to_chat_history_message")
        .description(
            "引用当前聊天历史中的某条平台消息，并发送回复到当前群聊或私聊。"
            "调用成功后不要重复发送相同内容。"
        )
        .hidden()
        .risk(RiskLow)
        .tags("chat")
        .depends_on("search_chat_history", "get_chat_history_around", "get_media")
        .string(
            "message_id",
            "search_chat_history 或 get_chat_history_around 返回结果中的平台消息 ID，"
            "可传 # 开头或纯 ID。",
            required(),
        )
        .string("message", "引用该历史消息时要发送到当前聊天的回复内容。", required())
    )


def current_chat_history_context(ctx: Any) -> tuple[ChatHistoryContext, str]:
    """对应 Go ``currentChatHistoryContext``：平台与 scope 只来自消息上下文。"""
    msg, ok = message_context_from(ctx)
    if not ok or (msg.platform or "").strip() == "" or (msg.scope_id or "").strip() == "":
        return (
            ChatHistoryContext(),
            "当前上下文没有平台聊天信息，无法自动确定要查询哪个聊天。",
        )
    return (
        ChatHistoryContext(
            platform=msg.platform,
            scope_id=msg.scope_id,
            platform_user_id=msg.platform_user_id,
        ),
        "",
    )


def chat_history_user_filter(
    user: str, ctx: ChatHistoryContext
) -> tuple[str, str, str]:
    """对应 Go ``chatHistoryUserFilter``：返回 ``(sender_id, sender_name, err)``。"""
    user = (user or "").strip()
    if user == "":
        return "", "", ""
    if user in _SELF_ALIASES:
        if (ctx.platform_user_id or "").strip() == "":
            return "", "", "无法解析“我”：当前平台上下文没有用户 ID。"
        return ctx.platform_user_id, "", ""
    match = _AT_ID_PATTERN.match(user)
    if match:
        return match.group(1).strip(), "", ""
    if is_digits(user):
        return user, "", ""
    return "", user, ""


def chat_history_time_range(
    since_raw: str, until_raw: str, hours: int, days: int
) -> tuple[datetime | None, datetime | None]:
    """对应 Go ``chatHistoryTimeRange``。"""
    return chat_history_time_range_at(
        since_raw, until_raw, hours, days, datetime.now(timezone.utc)
    )


def chat_history_time_range_at(
    since_raw: str,
    until_raw: str,
    hours: int,
    days: int,
    now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """对应 Go ``chatHistoryTimeRangeAt``（``AddDate(0,0,-days)`` 用日历天复刻）。"""
    since = parse_chat_history_time(since_raw)
    until = parse_chat_history_time(until_raw)
    if since is None:
        if hours > 0:
            since = now - timedelta(hours=hours)
        elif days > 0:
            since = _add_days(now, -days)
    return since, until


def _add_days(value: datetime, days: int) -> datetime:
    """近似 Go ``time.AddDate(0, 0, days)``：跨月按日历推进。"""
    target = value.day + days
    month = value.month
    year = value.year
    while target < 1:
        month -= 1
        if month < 1:
            month = 12
            year -= 1
        target += _days_in_month(year, month)
    while True:
        length = _days_in_month(year, month)
        if target <= length:
            break
        target -= length
        month += 1
        if month > 12:
            month = 1
            year += 1
    return value.replace(year=year, month=month, day=target)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def parse_chat_history_time(value: str) -> datetime | None:
    """对应 Go ``parseChatHistoryTime``。"""
    value = (value or "").strip()
    if value == "":
        return None
    if is_digits(value):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for layout in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(value, layout)
        except ValueError:
            continue
        # Go 的 ParseInLocation(..., time.Local)：先按本地时区解释，再转 UTC。
        return parsed.astimezone().astimezone(timezone.utc)
    return None


def split_chat_history_terms(value: str) -> list[str]:
    """对应 Go ``splitChatHistoryTerms``。"""
    fields = _TERM_SPLIT_PATTERN.split((value or "").strip())
    return [field.strip() for field in fields if field.strip() != ""]


def parse_chat_history_message_id(value: str) -> str:
    """对应 Go ``parseChatHistoryMessageID``：去掉可选的前导 ``#``。"""
    return (value or "").strip().removeprefix("#").strip()


def clamp_chat_history_limit(value: int) -> int:
    """对应 Go ``clampChatHistoryLimit``。"""
    if value <= 0:
        return CHAT_HISTORY_DEFAULT_LIMIT
    return min(value, CHAT_HISTORY_MAX_LIMIT)


def clamp_chat_history_window(value: int) -> int:
    """对应 Go ``clampChatHistoryWindow``。"""
    if value < 0:
        return 0
    return clamp_chat_history_limit(value)


async def format_chat_history_media(
    ctx: Any, row: ChatMessage, target_id: str, center: Any
) -> str:
    """对应 Go ``formatChatHistoryMedia``。"""
    line = format_chat_history_line(row, target_id)
    ids: dict[int, str] = {}
    if center is not None:
        ids = await center.history_ids(ctx, row)
    for position, segment in enumerate(HistorySegments(row)):
        line += f"\n  {position + 1}. {history_media_label(segment.type, ids.get(position + 1, ''))}"
    return line


def chat_history_result_lines(blocks: list[str]) -> str:
    """对应 Go ``chatHistoryResultLines``：只在行边界截断，绝不切断媒体 ID。"""
    lines: list[str] = []
    size = 0
    for line in "\n".join(blocks).split("\n"):
        if size + len(line) + 1 > CHAT_HISTORY_TEXT_LIMIT:
            return "\n".join(lines) + "\n...[查询结果过长，已截断]"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def format_chat_history_line(row: ChatMessage, target_id: str) -> str:
    """对应 Go ``formatChatHistoryLine``。"""
    prefix = "=> " if (target_id != "" and row.platform_message_id == target_id) else ""
    name = (row.sender_name or "").strip() or row.sender_id
    body = truncate_chat_history_message(row.text)
    if (row.reply_to_platform_message_id or "").strip() != "":
        body = truncate_chat_history_reference(
            FormatReferenceText(row.platform, ChatMessageReply(row), row.text)
        )
    created = row.created_at or datetime.now(timezone.utc)
    return (
        f"{prefix}[#{row.platform_message_id}] "
        f"{created.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{name}({row.sender_id}): {body}"
    )


def truncate_chat_history_reference(text: str) -> str:
    """对应 Go ``truncateChatHistoryReference``。"""
    text = (text or "").replace("\r", "").strip()
    if len(text) <= CHAT_HISTORY_MESSAGE_LIMIT:
        return text
    return text[:CHAT_HISTORY_MESSAGE_LIMIT] + "...[单条消息过长，已截断]"


def truncate_chat_history_message(text: str) -> str:
    """对应 Go ``truncateChatHistoryMessage``。"""
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= CHAT_HISTORY_MESSAGE_LIMIT:
        return text
    return text[:CHAT_HISTORY_MESSAGE_LIMIT] + "...[单条消息过长，已截断]"


def history_media_label(kind: str, media_id: str) -> str:
    """对应 Go ``historyMediaLabel``：无 media ID 时退化为 ``<kind> (待入库)``。"""
    label = (kind or "").strip() or "media"
    media_id = (media_id or "").strip()
    if media_id == "":
        return f"{label} (待入库)"
    return f"{label} {media_id}"


def is_digits(value: str) -> bool:
    """对应 Go ``isDigits``。"""
    if value == "":
        return False
    return all("0" <= ch <= "9" for ch in value)


def _decode(raw: str, kind: type, tool_name: str) -> Any:
    out = kind()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse {tool_name} arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"parse {tool_name} arguments: expected a JSON object")
    for key, value in data.items():
        if not hasattr(out, key):
            continue
        setattr(out, key, value)
    return out


__all__ = [name for name in dir() if not name.startswith("_")]
