"""运行状态快照与展示格式化。

对应 Go：``internal/runtime/status.go``。

Agent 每进入一个阶段都会发布一份 :class:`Snapshot`；本模块只负责模型与
纯函数格式化（时长、模型名、token 用量、单行紧凑状态）。

约定：Go 的 ``time.Duration`` → Python ``float`` 秒；``time.Time`` →
``datetime.datetime``（UTC aware，Go 的零值 ``time.Time{}`` → ``None``）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from atri_x.llm.llm import Usage

__all__ = [
    "Phase",
    "PhaseDone",
    "PhaseError",
    "PhaseIdle",
    "PhaseLLM",
    "PhasePreparing",
    "PhaseSending",
    "PhaseTool",
    "Snapshot",
    "format_compact",
    "format_duration",
    "format_int",
    "format_model",
    "format_usage",
    "truncate_runes",
]


class Phase:
    """运行阶段。对应 Go 的 ``runtime.Phase`` 常量组。"""

    IDLE = "idle"
    PREPARING = "preparing"
    LLM = "llm"
    TOOL = "tool"
    SENDING = "sending"
    DONE = "done"
    ERROR = "error"


PhaseIdle = Phase.IDLE
PhasePreparing = Phase.PREPARING
PhaseLLM = Phase.LLM
PhaseTool = Phase.TOOL
PhaseSending = Phase.SENDING
PhaseDone = Phase.DONE
PhaseError = Phase.ERROR


@dataclass
class Snapshot:
    """一次运行状态快照。对应 Go ``runtime.Snapshot``。

    ``kind`` 取 :class:`atri_x.request.manager.Kind` 的常量值（Go 里它的类型
    就是 ``request.Kind``，Python 里是 ``str``）。
    """

    session_id: str = ""
    phase: str = PhaseIdle
    provider: str = ""
    model: str = ""
    mode: str = ""
    request_id: str = ""
    kind: str = ""
    label: str = ""
    tool_name: str = ""
    turn_started_at: datetime | None = None
    stage_started_at: datetime | None = None
    finished_at: datetime | None = None
    usage: Usage | None = None
    error: str = ""

    def running(self) -> bool:
        """是否仍在运行（对应 Go ``Snapshot.Running``）。"""
        return (
            self.stage_started_at is not None
            and self.finished_at is None
            and self.phase not in (PhaseIdle, PhaseDone, PhaseError)
        )

    def elapsed(self, now: datetime | None = None) -> float:
        """本轮耗时（秒）。对应 Go ``Snapshot.Elapsed``。"""
        start = self.turn_started_at or self.stage_started_at
        if start is None:
            return 0.0
        end = self.finished_at or now or datetime.now(timezone.utc)
        if end < start:
            return 0.0
        return (end - start).total_seconds()

    def stage_elapsed(self, now: datetime | None = None) -> float:
        """当前阶段耗时（秒）。对应 Go ``Snapshot.StageElapsed``。"""
        if self.stage_started_at is None:
            return self.elapsed(now)
        end = self.finished_at or now or datetime.now(timezone.utc)
        if end < self.stage_started_at:
            return 0.0
        return (end - self.stage_started_at).total_seconds()


def format_compact(snapshot: Snapshot, now: datetime | None = None, width: int = 0) -> str:
    """渲染 ``phase · 时长 · provider/model · tokens`` 单行状态。"""
    parts: list[str] = []
    phase = snapshot.phase or PhaseIdle
    if snapshot.phase == PhaseTool and snapshot.tool_name:
        phase = phase + " " + snapshot.tool_name
    elif snapshot.phase == PhaseError and snapshot.error:
        phase = phase + " " + truncate_runes(snapshot.error.strip(), 40)
    parts.append(phase)
    elapsed = snapshot.elapsed(now)
    if elapsed > 0:
        parts.append(format_duration(elapsed))
    model = format_model(snapshot.provider, snapshot.model)
    if model:
        parts.append(model)
    token = format_usage(snapshot.usage)
    if token:
        parts.append(token)
    if not parts:  # pragma: no cover - phase 永不为空，保留 Go 的兜底
        return "idle"
    return truncate_runes(" · ".join(parts), width)


def format_model(provider: str, model: str) -> str:
    """``provider/model``；缺一个时只返回另一个。"""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if provider and model:
        return provider + "/" + model
    if model:
        return model
    return provider


def format_usage(usage: Usage | None) -> str:
    """``tokens N`` / ``tokens N · cache M``。"""
    if usage is None or usage.total_tokens <= 0:
        return ""
    text = "tokens " + format_int(usage.total_tokens)
    if usage.cache_hit_tokens > 0:
        text += " · cache " + format_int(usage.cache_hit_tokens)
    return text


def format_duration(seconds: float) -> str:
    """``MM:SS``（不足一小时）或 ``H:MM:SS``。对应 Go ``FormatDuration``。"""
    if seconds is None or seconds < 0:
        seconds = 0.0
    # Go 的 Duration.Round 对半值远离零取整；正数等价于 floor(x+0.5)。
    total = int(math.floor(seconds + 0.5))
    if total < 3600:
        return "%02d:%02d" % (total // 60, total % 60)
    return "%d:%02d:%02d" % (total // 3600, total // 60 % 60, total % 60)


def format_int(value: int) -> str:
    """千分位整数，例如 ``18240`` → ``18,240``。"""
    text = str(value)
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if len(text) <= 3:
        return ("-" if negative else "") + text
    chunks: list[str] = []
    while len(text) > 3:
        chunks.insert(0, text[-3:])
        text = text[:-3]
    chunks.insert(0, text)
    return ("-" if negative else "") + ",".join(chunks)


def truncate_runes(text: str, max_runes: int) -> str:
    """按 rune 截断并补省略号（``max_runes <= 0`` 时原样返回）。"""
    if max_runes <= 0 or len(text) <= max_runes:
        return text
    if max_runes <= 1:
        return text[:max_runes]
    return text[: max_runes - 1] + "…"


def short_text(text: str, max_runes: int) -> str:
    """先 strip 再截断（对应 Go 的 ``shortText``）。"""
    return truncate_runes((text or "").strip(), max_runes)


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
FormatCompact = format_compact
FormatModel = format_model
FormatUsage = format_usage
FormatDuration = format_duration
