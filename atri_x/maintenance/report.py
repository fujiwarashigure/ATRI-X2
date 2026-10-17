"""定时资源报告：从审计日志聚合生图/Token/费用，附磁盘与内存快照后发出。

对应 Go：``internal/maintenance/report.go``。

数据来源全部是 ``audit`` 日志（不是数据库）：

* 生图统计 —— ``event=tool_call`` 且 ``tool=image_generate``，按 ``success`` 字段分组；
* Token 消耗 —— ``event=llm_usage``，按 ``model`` 分桶累加，可按 ``provider`` 过滤；
* 费用 —— 用配置里的单价按 **高峰/空闲双档** 计价（DeepSeek 规则：周末整天与
  工作日 09:00–12:00 / 14:00–18:00 之外都是空闲；``holidays`` 列出的日期也算空闲）。

**时间口径**：:func:`is_off_peak` 必须在 **Asia/Shanghai** 下判定。标准库缺 tzdata
时退回固定 +08:00（与 Go 的 ``time.FixedZone("CST", 8*3600)`` 兜底一致）。时间未知
（``None`` / 零值）时**按高峰**处理，避免低估账单。

**与 Go 的文本差异（有意）**：报告正文的排版、全角分隔符、``%.1f%%`` 等与 Go 逐字
一致，只有两个明确指向 Go 运行时的标签改写为 Python 对应物（PORTING §8 要求把
Go 专属表述替换为 Python 对应物）：``Go 堆`` → ``Python 堆``、``goroutine`` →
``线程``。
"""

from __future__ import annotations

import inspect
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.config import DailyReportConfig, ModelPriceConfig
from atri_x.config import off_peak as config_off_peak
from atri_x.config import offpeak_price as config_offpeak_price
from atri_x.errors import InvalidError
from atri_x.logging import LogQuery, Reader
from atri_x.sysinfo import Snapshot, collect, format_bytes, format_uint_bytes

__all__ = [
    "ImageReportStats",
    "ModelUsageReport",
    "RunDailyReportMixin",
    "compute_entry_cost",
    "currency_symbol",
    "format_daily_report",
    "format_money",
    "human_int",
    "is_off_peak",
    "log_query_days",
    "offpeak_price",
    "parse_log_int",
    "price_for",
]

#: 生图统计的扫描上限。对应 Go ``queryImageStats`` 的 ``Limit: 200000``。
IMAGE_QUERY_LIMIT = 200_000
#: Token 统计的扫描上限。对应 Go ``queryUsage`` 的 ``Limit: 400000``。
USAGE_QUERY_LIMIT = 400_000
#: 未知模型的占位名。对应 Go 里的字面量 ``"(unknown)"``。
UNKNOWN_MODEL = "(unknown)"


class ImageReportStats:
    """生图统计。对应 Go ``imageReportStats``。"""

    __slots__ = ("failed", "success")

    def __init__(self, success: int = 0, failed: int = 0) -> None:
        self.success = success
        self.failed = failed


class ModelUsageReport:
    """单模型的用量与费用。对应 Go ``modelUsageReport``。"""

    __slots__ = (
        "cache_hit_tokens",
        "calls",
        "completion_tokens",
        "cost",
        "model",
        "priced",
        "prompt_tokens",
        "total_tokens",
    )

    def __init__(
        self,
        model: str = "",
        calls: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cost: float = 0.0,
        priced: bool = False,
    ) -> None:
        self.model = model
        self.calls = calls
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.cache_hit_tokens = cache_hit_tokens
        self.cost = cost
        self.priced = priced


def is_off_peak(at: datetime | None, holidays: list[str] | None) -> bool:
    """该时刻是否处于空闲计价档。对应 Go ``isOffPeak``。

    委托给 :func:`atri_x.config.off_peak`（同一规则的唯一出处：``DailyReportConfig``
    的双档计价也用它），因此本函数只是语义化别名 + 文档落点。

    时间未知时返回 ``False``（== 高峰），避免低估账单。
    """
    return bool(config_off_peak(at, holidays))


def offpeak_price(price: ModelPriceConfig) -> ModelPriceConfig:
    """取空闲档单价（未设置的字段沿用基准价）。对应 Go ``offpeakPrice``。"""
    return config_offpeak_price(price)


def price_for(cfg: DailyReportConfig, model: str, at: datetime | None) -> ModelPriceConfig | None:
    """该时刻生效的单价；未配置该模型时返回 ``None``。对应 Go ``priceFor``。"""
    return cfg.price_for(model, at)


def compute_entry_cost(
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int,
    price: ModelPriceConfig,
) -> float:
    """按一次 usage 计算费用。对应 Go ``computeEntryCost``。

    缓存命中数先钳到 ``[0, prompt_tokens]``，未命中部分 = prompt - cache_hit；
    缓存单价 <= 0 时回退未命中输入价。
    """
    return price.compute_cost(prompt_tokens, completion_tokens, cache_hit_tokens)


def log_query_days(since: datetime, until: datetime) -> int:
    """日志查询覆盖的天数（+2 冗余，钳到 ``[1, 7]``）。对应 Go ``logQueryDays``。"""
    days = int((until - since).total_seconds() / 86400) + 2
    return max(1, min(7, days))


def parse_log_int(value: str) -> int:
    """解析日志里的整数字段；失败返回 0。对应 Go ``parseLogInt``。"""
    try:
        return int((value or "").strip(), 10)
    except (TypeError, ValueError):
        return 0


def currency_symbol(currency: str) -> str:
    """货币符号。对应 Go ``currencySymbol``。"""
    normalized = (currency or "").strip().upper()
    if normalized in ("", "CNY", "RMB", "¥", "￥"):
        return "¥"
    if normalized in ("USD", "$"):
        return "$"
    return (currency or "").strip() + " "


def format_money(value: float, symbol: str) -> str:
    """金额渲染：符号 + 固定 4 位小数。对应 Go ``formatMoney``。"""
    return symbol + f"{value:.4f}"


def human_int(value: int) -> str:
    """千位分隔整数（**不是** 1.2k 这类缩写）。对应 Go ``humanInt``。"""
    text = str(int(value))
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    out: list[str] = []
    for index, digit in enumerate(text):
        if index > 0 and (len(text) - index) % 3 == 0:
            out.append(",")
        out.append(digit)
    joined = "".join(out)
    return "-" + joined if negative else joined


def format_daily_report(
    cfg: DailyReportConfig,
    images: ImageReportStats,
    usage: list[ModelUsageReport],
    snapshot: Snapshot,
    since: datetime,
    until: datetime,
) -> str:
    """渲染报告正文。对应 Go ``formatDailyReport``（排版逐字对齐）。"""
    lines: list[str] = []
    lines.append("ATRI-X 定时报告")
    lines.append("周期：" + since.strftime("%Y-%m-%d %H:%M") + " ~ " + until.strftime("%Y-%m-%d %H:%M"))
    lines.append(f"窗口：最近 {cfg.window_hours} 小时")

    lines.append("")
    lines.append("【生图】")
    lines.append(f"- 成功：{images.success} 次")
    lines.append(f"- 失败：{images.failed} 次")
    lines.append(f"- 合计：{images.success + images.failed} 次")

    lines.append("")
    lines.append("【Token 消耗】")
    provider = (cfg.provider or "").strip() or "全部 provider"
    if not usage:
        lines.append("- 无 llm_usage 记录（" + provider + "）")
    else:
        prompt_total = completion_total = total_total = cache_total = 0
        calls_total = 0
        for item in usage:
            prompt_total += item.prompt_tokens
            completion_total += item.completion_tokens
            total_total += item.total_tokens
            cache_total += item.cache_hit_tokens
            calls_total += item.calls
            lines.append(
                f"- {item.model}：调用 {item.calls} 次｜输入 {human_int(item.prompt_tokens)}"
                f"｜输出 {human_int(item.completion_tokens)}｜缓存命中 "
                f"{human_int(item.cache_hit_tokens)}｜合计 {human_int(item.total_tokens)}"
            )
        lines.append(
            f"- 合计：调用 {calls_total} 次｜输入 {human_int(prompt_total)}"
            f"｜输出 {human_int(completion_total)}｜缓存命中 {human_int(cache_total)}"
            f"｜合计 {human_int(total_total)}"
        )

    lines.append("")
    lines.append("【费用】")
    symbol = currency_symbol(cfg.currency)
    unpriced: list[str] = []
    llm_cost = 0.0
    for item in usage:
        if not item.priced:
            unpriced.append(item.model)
            continue
        llm_cost += item.cost
        lines.append(f"- {item.model}：{format_money(item.cost, symbol)}")
    image_cost = float(images.success) * cfg.image_price_per_image
    if cfg.image_price_per_image > 0:
        lines.append(
            f"- 生图：{images.success} 张 × {format_money(cfg.image_price_per_image, symbol)}"
            f" = {format_money(image_cost, symbol)}"
        )
    if llm_cost == 0 and image_cost == 0 and not usage and cfg.image_price_per_image <= 0:
        lines.append("- 无数据")
    else:
        lines.append(f"- 合计：{format_money(llm_cost + image_cost, symbol)}")
    if cfg.is_peak_pricing():
        lines.append(
            "- 计价：高峰/空闲双档（北京时间周一至周五 9-12、14-18 为高峰；"
            "周末及配置的节假日为空闲）"
        )
    if unpriced:
        lines.append("- 未配置单价：" + ", ".join(unpriced) + "（只统计 token，不计金额）")

    lines.append("")
    lines.append("【磁盘】")
    lines.append("- 数据目录：" + format_bytes(snapshot.dir_bytes))
    if snapshot.disk_total_bytes > 0:
        used = snapshot.disk_total_bytes - snapshot.disk_free_bytes
        percent = float(used) / float(snapshot.disk_total_bytes) * 100
        lines.append(
            f"- 文件系统：已用 {format_uint_bytes(used)} / "
            f"{format_uint_bytes(snapshot.disk_total_bytes)}"
            f"（剩余 {format_uint_bytes(snapshot.disk_free_bytes)}，{percent:.1f}%）"
        )
    elif snapshot.disk_err:
        lines.append("- 文件系统：暂不支持（" + snapshot.disk_err + "）")

    lines.append("")
    lines.append("【内存】")
    if snapshot.rss_bytes > 0:
        lines.append("- 进程 RSS：" + format_uint_bytes(snapshot.rss_bytes))
    # Go 原文是 "Go 堆" / "goroutine"；本仓是 Python 进程，按 PORTING §8 换成对应物。
    lines.append(
        "- Python 堆："
        + format_uint_bytes(snapshot.heap_alloc_bytes)
        + " / 系统 "
        + format_uint_bytes(snapshot.heap_sys_bytes)
    )
    lines.append(f"- 线程：{snapshot.goroutines}")
    return "\n".join(lines).rstrip("\n")


class RunDailyReportMixin:
    """日报的查询与渲染。对应 Go ``report.go`` 里的 ``Service`` 方法。"""

    async def run_daily_report(self, ctx: Any) -> None:
        """生成并投递定时报告。对应 Go ``RunDailyReport``。

        未配置投递回调时只告警不报错（Go 的行为）；窗口 ``<= 0`` 时用 12 小时。
        """
        if self.report_sender is None:
            self.warn("daily report skipped: no delivery target configured")
            return None
        window_hours = getattr(self.report, "window_hours", 0) or 0
        window = window_hours if window_hours > 0 else 12
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=window)
        text = await self.build_daily_report(ctx, since, until)
        await _maybe_await(self.report_sender(ctx, text))
        return None

    async def build_daily_report(
        self, ctx: Any, since: datetime, until: datetime
    ) -> str:
        """构造报告正文。对应 Go ``buildDailyReport``。"""
        if self.logs is None:
            raise InvalidError("log manager is not configured")
        reader = Reader(dir=self.logs.log_dir())
        images = await self.query_image_stats(ctx, reader, since, until)
        usage = await self.query_usage(ctx, reader, since, until)
        root = (getattr(self.report, "data_root", "") or "").strip()
        if not root:
            root = os.path.dirname(self.logs.log_dir())
        snapshot = collect(root)
        return format_daily_report(self.report, images, usage, snapshot, since, until)

    async def query_image_stats(
        self, ctx: Any, reader: Reader, since: datetime, until: datetime
    ) -> ImageReportStats:
        """统计生图成功/失败。对应 Go ``queryImageStats``。"""
        scanned = reader.query(
            ctx,
            LogQuery(
                prefix="audit",
                limit=IMAGE_QUERY_LIMIT,
                days=log_query_days(since, until),
                min_level="debug",
                since=since,
                until=until,
                fields={"event": "tool_call", "tool": "image_generate"},
            ),
        )
        stats = ImageReportStats()
        for entry in scanned.entries:
            if (entry.fields.get("success") or "").strip().lower() == "true":
                stats.success += 1
            else:
                stats.failed += 1
        return stats

    async def query_usage(
        self, ctx: Any, reader: Reader, since: datetime, until: datetime
    ) -> list[ModelUsageReport]:
        """按模型聚合 token 与费用。对应 Go ``queryUsage``。"""
        scanned = reader.query(
            ctx,
            LogQuery(
                prefix="audit",
                limit=USAGE_QUERY_LIMIT,
                days=log_query_days(since, until),
                min_level="debug",
                since=since,
                until=until,
                fields={"event": "llm_usage"},
            ),
        )
        provider = (getattr(self.report, "provider", "") or "").strip().lower()
        buckets: dict[str, ModelUsageReport] = {}
        order: list[str] = []
        for entry in scanned.entries:
            if provider and (entry.fields.get("provider") or "").strip().lower() != provider:
                continue
            model = (entry.fields.get("model") or "").strip() or UNKNOWN_MODEL
            bucket = buckets.get(model)
            if bucket is None:
                bucket = ModelUsageReport(model=model)
                buckets[model] = bucket
                order.append(model)
            prompt_tokens = parse_log_int(entry.fields.get("prompt_tokens", ""))
            completion_tokens = parse_log_int(entry.fields.get("completion_tokens", ""))
            cache_hit_tokens = parse_log_int(entry.fields.get("cache_hit_tokens", ""))
            bucket.calls += 1
            bucket.prompt_tokens += prompt_tokens
            bucket.completion_tokens += completion_tokens
            bucket.total_tokens += parse_log_int(entry.fields.get("total_tokens", ""))
            bucket.cache_hit_tokens += cache_hit_tokens
            price = price_for(self.report, model, entry.time)
            if price is not None:
                bucket.priced = True
                bucket.cost += compute_entry_cost(
                    prompt_tokens, completion_tokens, cache_hit_tokens, price
                )
        out = [buckets[model] for model in order]
        # 稳定排序：token 多的在前，同量按模型名升序（与 Go 的 SliceStable 一致）。
        out.sort(key=lambda item: (-item.total_tokens, item.model))
        return out


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """``await`` 一个可能是同步的返回值（Python 侧新增件）。"""
    if inspect.isawaitable(value):
        return await value
    return value
