"""启动环境：运行模式解析、启动剖析器、平台模式映射。

对应 Go：``internal/app/environment.go``。

Go 里 ``Environment`` 是一个接口，生产实现叫 ``defaultEnvironment``。Python 侧
同一件事拆成两半：

* :class:`StartupProfiler` / :func:`new_startup_profiler` —— Go 的 ``startupProfiler``；
* :class:`DefaultEnvironment` —— Go 的 ``defaultEnvironment``，实现
  ``ResolveMode`` / ``ClaimServiceMarker`` / ``NewStartupProfiler`` 三个方法。

模式与平台模式的字符串常量与 Go 逐字一致（``auto`` / ``full`` / ``cli`` /
``service``）。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from atri_x.app.app import (
    RUN_MODE_AUTO,
    RUN_MODE_CLI_ONLY,
    RUN_MODE_FULL,
    RUN_MODE_SERVICE,
    RunMode,
)
from atri_x.app.service_marker import claim_service_marker, service_marker_running
from atri_x.errors import InvalidError

__all__ = [
    "StartupProfileEntry",
    "StartupProfiler",
    "new_startup_profiler",
    "DefaultEnvironment",
    "startup_profile_enabled",
    "resolve_run_mode",
    "platform_mode",
    "should_start_cron",
    "MODE_FULL",
    "MODE_CLI_ONLY",
    "MODE_SERVICE",
]


def _safe(text: str) -> str:
    """控制台码页可能是 GBK；不可表示时退到 ASCII 转义（PORTING §7.4）。"""
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "backslashreplace").decode("ascii")
    return text


def _eprint(text: str) -> None:
    try:
        print(_safe(text), file=sys.stderr)
    except Exception:  # noqa: BLE001 - 启动期打印失败不得影响启动
        pass


@dataclass
class StartupProfileEntry:
    """对应 Go ``startupProfileEntry``。"""

    name: str
    duration: float
    total: float


class StartupProfiler:
    """启动阶段耗时剖析器。对应 Go ``startupProfiler``。

    时间用 :func:`time.monotonic`（等价 Go 的 ``time.Now`` 差值语义），
    只影响剖析输出，不参与持久化。
    """

    def __init__(self, started_at: datetime | None = None) -> None:
        now = time.monotonic()
        self._started_wall = started_at or datetime.now(timezone.utc)
        self._started = now
        self._last = now
        self._enabled = False
        self._entries: list[StartupProfileEntry] = []

    @property
    def started_at(self) -> datetime:
        return self._started_wall

    @property
    def entries(self) -> list[StartupProfileEntry]:
        return list(self._entries)

    def set_enabled(self, enabled: bool) -> None:
        """对应 Go ``SetEnabled``。"""
        self._enabled = bool(enabled)

    def mark(self, name: str) -> None:
        """对应 Go ``Mark``。"""
        now = time.monotonic()
        duration = now - self._last
        total = now - self._started
        self._last = now
        if self._enabled:
            self._entries.append(
                StartupProfileEntry(name=name, duration=duration, total=total)
            )

    def flush(self) -> float:
        """对应 Go ``Flush``：打印并返回总耗时（秒）。"""
        total = time.monotonic() - self._started
        if self._enabled:
            for entry in self._entries:
                _eprint(
                    "[startup] %-24s took=%s total=%s"
                    % (
                        entry.name,
                        _format_duration(entry.duration),
                        _format_duration(entry.total),
                    )
                )
        _eprint(f"atri-x startup completed in {_format_duration(total)}")
        return total

    # Go 命名别名，便于交叉检索。
    SetEnabled = set_enabled
    Mark = mark
    Flush = flush


def _format_duration(seconds: float) -> str:
    """近似 Go ``time.Duration.String()``（只覆盖启动耗时会用到的量级）。"""
    if seconds < 1e-6:
        return f"{int(seconds * 1e9)}ns"
    if seconds < 1e-3:
        return _trim(seconds * 1e6) + "µs"
    if seconds < 1.0:
        return _trim(seconds * 1e3) + "ms"
    return _trim(seconds) + "s"


def _trim(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def new_startup_profiler(started_at: datetime | None = None) -> StartupProfiler:
    """对应 Go ``newStartupProfiler``。"""
    return StartupProfiler(started_at)


def startup_profile_enabled(level: str) -> bool:
    """只有 ``debug`` 打开启动剖析。对应 Go ``startupProfileEnabled``。"""
    return (level or "").strip().lower() == "debug"


#: 平台模式常量。Go 里在 ``internal/platform/builtin``，此处按字面值复刻，
#: 避免 app 层在模块顶层 import 尚未交付的 ``atri_x.platform.builtin``。
MODE_FULL = "full"
MODE_CLI_ONLY = "cli"
MODE_SERVICE = "service"


def resolve_run_mode(mode: RunMode | str) -> RunMode:
    """对应 Go ``resolveRunMode``。

    ``auto`` 时探测服务标记：已在跑则退到 CLI-only（避免第二个实例抢平台连接），
    否则全前台模式。未知模式抛 :class:`atri_x.errors.InvalidError`。
    """
    value = str(mode or "")
    if value in ("", RUN_MODE_AUTO):
        if service_marker_running():
            _eprint(
                "ATRI-X service detected, starting local CLI-only mode. "
                "Use `atri-x run` to force full foreground mode."
            )
            return RUN_MODE_CLI_ONLY
        _eprint(
            "No ATRI-X service detected, starting full foreground mode. "
            "Use `atri-x cli` to start local CLI only."
        )
        return RUN_MODE_FULL
    if value in (RUN_MODE_FULL, RUN_MODE_CLI_ONLY, RUN_MODE_SERVICE):
        return RunMode(value)
    raise InvalidError(f"unknown run mode {value!r}")


def platform_mode(mode: RunMode | str) -> str:
    """RunMode → 平台层 Mode。对应 Go ``platformMode``。"""
    value = str(mode or "")
    if value == RUN_MODE_CLI_ONLY:
        return MODE_CLI_ONLY
    if value == RUN_MODE_SERVICE:
        return MODE_SERVICE
    return MODE_FULL


def should_start_cron(mode: RunMode | str) -> bool:
    """CLI-only 不启动 cron。对应 Go ``shouldStartCron``。"""
    return str(mode or "") != RUN_MODE_CLI_ONLY


class DefaultEnvironment:
    """生产环境实现。对应 Go ``defaultEnvironment``。"""

    async def resolve_mode(self, mode: RunMode | str) -> RunMode:
        """对应 Go ``ResolveMode``（Go 侧同步，Python 侧统一协程）。"""
        return resolve_run_mode(mode)

    async def claim_service_marker(self, mode: RunMode | str) -> Any:
        """对应 Go ``ClaimServiceMarker``：非 service 模式返回 ``None``。"""
        if str(mode or "") != RUN_MODE_SERVICE:
            return None
        return claim_service_marker()

    def new_startup_profiler(self, started_at: datetime | None) -> StartupProfiler:
        """对应 Go ``NewStartupProfiler``。"""
        return new_startup_profiler(started_at)

    # Go 命名别名。
    ResolveMode = resolve_mode
    ClaimServiceMarker = claim_service_marker
    NewStartupProfiler = new_startup_profiler
