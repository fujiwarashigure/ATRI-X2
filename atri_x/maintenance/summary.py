"""每日会话总结：把一段时间内的会话信息汇总成一段可读文本并落盘（Python 版新增件）。

Go 上游只有「资源日报」（:mod:`atri_x.maintenance.report`，内容是生图/Token/费用/
磁盘/内存）与「群分析日报」（:mod:`atri_x.groupanalysis`，读平台群聊历史）。本模块
补的是**桌面/单机形态也需要的那一块**：把 ATRI-X 自己的会话（SQLite ``sessions`` +
``messages``）按天汇总，产出审阅用的文本，供 GUI/CLI/运维读取。

产出位置：``<数据目录>/daily_summaries/<YYYY-MM-DD>.json``（``<数据目录>`` 取
``[storage].sessions_sqlite_path`` 所在目录）。**纯标准库、不联网**：默认只用
确定性摘要（标题 + 条数 + 最后一条用户/助手内容），``use_llm=true`` 时才由
``Service.summary_generator``（应用装配时接到压缩模型）再摘要一遍。

设计取舍：

* 不新增 SQLite 表：总结是「一次性产物 + 保留期由文件系统 mtime 决定」，
  走文件比加一版 migration 轻，也便于用户直接打开看；
* 不按自然日切窗口：``days`` 是「最近 N×24 小时」，与资源日报的 ``window_hours``
  口径一致，避免时区/边界争议；
* 条数上限与截断都显式写进结果，调用方不必再猜。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from atri_x.config.app_sections import shanghai_tz
from atri_x.storage import ListSessionsRequest

__all__ = [
    "DAILY_SUMMARY_DIR",
    "DailySummary",
    "RunDailySummaryMixin",
    "collect_digest",
    "list_summaries",
    "load_summary",
    "save_summary",
    "summary_dir",
]

#: 数据目录下存放总结的子目录名。
DAILY_SUMMARY_DIR = "daily_summaries"
#: 单个会话里「最后用户/助手内容」最多保留多少字符。
SNIPPET_CHARS = 160
#: 摘要正文的默认上限（字符）。超出按行截断并注明。
DEFAULT_MAX_CHARS = 6000


@dataclass
class DailySummary:
    """一份每日会话总结（落盘结构即本类字段，``camelCase`` 只用于 JSON）。"""

    day: str = ""
    created_at: str = ""
    sessions: int = 0
    messages: int = 0
    text: str = ""
    source: str = "digest"


def summary_dir(data_dir: str) -> str:
    """数据目录 → 总结目录（``""`` 原样返回，调用方据此跳过）。"""
    root = (data_dir or "").strip()
    if not root:
        return ""
    return os.path.join(root, DAILY_SUMMARY_DIR)


def collect_digest(
    store: Any,
    since: datetime,
    until: datetime,
    *,
    max_sessions: int = 20,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, int, int]:
    """把窗口内更新过的会话渲染成确定性摘要，返回 ``(正文, 会话数, 消息数)``。

    只读：``Store.sessions().list`` 的列表行已经带 ``message_count`` 与最后一条
    用户/助手预览，因此不再逐会话回表。
    """
    limit = max(1, int(max_sessions or 20))
    request = ListSessionsRequest(
        include_all_platforms=True,
        include_archived=True,
        order_by_updated_at=True,
        limit=max(limit * 4, 40),
    )
    rows = [row for row in store.sessions().list(request) if _in_window(row, since, until)]
    rows = rows[:limit]
    sessions = len(rows)
    messages = sum(int(getattr(row, "message_count", 0) or 0) for row in rows)
    text = render_digest(rows, since, until, sessions=sessions, messages=messages)
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 10].rstrip() + "\n…（已截断）"
    return text, sessions, messages


def render_digest(
    rows: Sequence[Any],
    since: datetime,
    until: datetime,
    *,
    sessions: int,
    messages: int,
) -> str:
    """渲染摘要正文。``until`` 决定「日期」标签（按 Asia/Shanghai）。"""
    zone = shanghai_tz()
    start = since.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    end = until.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    lines = [
        "ATRI-X 会话总结 " + until.astimezone(zone).strftime("%Y-%m-%d"),
        "窗口：" + start + " ~ " + end,
        "会话 " + str(sessions) + " 个 / 消息 " + str(messages) + " 条",
    ]
    if not rows:
        lines.append("")
        lines.append("（窗口内没有会话活动）")
        return "\n".join(lines)
    for index, row in enumerate(rows, 1):
        lines.append("")
        lines.append(("%d) " % index) + _session_head(row, zone))
        user = (getattr(row, "last_user_preview", "") or "").strip()
        bot = (getattr(row, "last_bot_preview", "") or "").strip()
        if user:
            lines.append("   用户：" + _snippet(user))
        if bot:
            lines.append("   助手：" + _snippet(bot))
    return "\n".join(lines)


def save_summary(directory: str, summary: DailySummary) -> str:
    """原子写入 ``<directory>/<day>.json``，返回文件路径。

    先写同目录临时文件再 ``os.replace``：读取方不会看到半截 JSON（与仓库里
    ``state.toml`` 的写盘口径一致）。
    """
    root = (directory or "").strip()
    if not root:
        raise ValueError("summary directory is required")
    os.makedirs(root, exist_ok=True)
    day = (summary.day or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary.day = day
    target = os.path.join(root, day + ".json")
    payload = {
        "day": day,
        "createdAt": summary.created_at,
        "sessions": summary.sessions,
        "messages": summary.messages,
        "source": summary.source,
        "text": summary.text,
    }
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    return target


def load_summary(directory: str, day: str) -> DailySummary | None:
    """按日期读取一份总结；文件缺失或损坏时返回 ``None``（只读展示不该抛错）。"""
    root = (directory or "").strip()
    if not root:
        return None
    return _load_file(os.path.join(root, (day or "").strip() + ".json"))


def list_summaries(directory: str, limit: int = 7) -> list[DailySummary]:
    """按日期倒序列出最近几份总结。"""
    root = (directory or "").strip()
    if not root or not os.path.isdir(root):
        return []
    names = sorted(
        (name for name in os.listdir(root) if name.endswith(".json")), reverse=True
    )
    if limit > 0:
        names = names[:limit]
    out: list[DailySummary] = []
    for name in names:
        summary = _load_file(os.path.join(root, name))
        if summary is not None:
            out.append(summary)
    return out


def window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """``days`` → ``(since, until)``；非法天数按 1 天处理（与配置默认一致）。"""
    until = now or datetime.now(timezone.utc)
    span = max(1, int(days or 1))
    return until - timedelta(days=span), until


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _in_window(row: Any, since: datetime, until: datetime) -> bool:
    updated = getattr(row, "updated_at", None)
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return since <= updated <= until


def _session_head(row: Any, zone: Any) -> str:
    updated = getattr(row, "updated_at", None)
    clock = updated.astimezone(zone).strftime("%m-%d %H:%M") if updated else "--"
    platform = str(getattr(row, "platform", "") or "?").strip() or "?"
    title = str(getattr(row, "title", "") or "").strip() or "（未命名会话）"
    count = int(getattr(row, "message_count", 0) or 0)
    archived = "（已归档）" if getattr(row, "archived_at", None) else ""
    return clock + " · " + platform + " · 《" + title + "》" + archived + " · " + str(count) + " 条"


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _load_file(path: str) -> DailySummary | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return DailySummary(
        day=str(payload.get("day") or ""),
        created_at=str(payload.get("createdAt") or ""),
        sessions=int(payload.get("sessions") or 0),
        messages=int(payload.get("messages") or 0),
        text=str(payload.get("text") or ""),
        source=str(payload.get("source") or "digest"),
    )


# ---------------------------------------------------------------------------
# Service 混入（cron handler；CLI 入口在 summary_cli.py，避免 runpy 重复导入）
# ---------------------------------------------------------------------------


class RunDailySummaryMixin:
    """``Service`` 的每日会话总结部分（Python 版新增件）。

    拆到本模块（而不是留在 ``maintenance.py``）是为了压住单文件行数；混入 ``Service``
    后与 ``RunDailyReportMixin`` 的形态一致。
    """

    async def run_daily_summary(self, ctx: Any) -> None:
        """生成每日会话总结。

        窗口是「最近 ``days×24`` 小时」；正文默认走确定性摘要，``use_llm`` 为真且
        app 装配了 ``summary_generator`` 时再用压缩模型摘要一遍（失败回退）。
        结果落到 ``<数据目录>/daily_summaries/<日期>.json``；``notify`` 为真时复用
        资源报告的投递回调发出去。
        """
        if not self.daily_summary.enabled:
            return None
        if self.store is None:
            self.warn("daily summary skipped: store is not configured")
            return None
        days = self.daily_summary.days or 1
        since, until = window(days)
        text, sessions, messages = collect_digest(
            self.store,
            since,
            until,
            max_sessions=self.daily_summary.max_sessions or 20,
        )
        source = "digest"
        if self.daily_summary.use_llm and self.summary_generator is not None and sessions > 0:
            try:
                polished = str(
                    await _maybe_await(self.summary_generator(ctx, text)) or ""
                ).strip()
            except Exception as exc:  # noqa: BLE001 - LLM 失败不丢确定性摘要
                self.warn("daily summary llm failed, fallback to digest", "error", exc)
                polished = ""
            if polished:
                text, source = polished, "llm"
        summary = DailySummary(
            day=until.astimezone(shanghai_tz()).strftime("%Y-%m-%d"),
            created_at=until.isoformat(),
            sessions=sessions,
            messages=messages,
            text=text,
            source=source,
        )
        saved = ""
        if self.summary_dir:
            try:
                saved = save_summary(self.summary_dir, summary)
            except OSError as exc:
                self.warn("daily summary save failed", "error", exc, "dir", self.summary_dir)
        self.info(
            "maintenance daily summary completed",
            "day",
            summary.day,
            "sessions",
            sessions,
            "messages",
            messages,
            "source",
            source,
            "path",
            saved,
        )
        if self.daily_summary.notify and self.report_sender is not None and sessions > 0:
            try:
                await _maybe_await(self.report_sender(ctx, text))
            except Exception as exc:  # noqa: BLE001 - 投递失败只告警
                self.warn("daily summary notify failed", "error", exc)
        return None


async def _maybe_await(value: Any) -> Any:
    """``await`` 一个可能是同步的返回值（模板回调既允许同步也允许协程）。"""
    import inspect  # noqa: PLC0415 - 只有混入路径用得到

    if inspect.isawaitable(value):
        return await value
    return value



def data_dir_of(cfg: Any) -> str:
    """数据目录 = ``[storage].sessions_sqlite_path`` 所在目录。"""
    path = str(getattr(getattr(cfg, "storage", None), "sessions_sqlite_path", "") or "")
    return os.path.dirname(path) if path else ""
