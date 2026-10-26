"""``python -m atri_x.app.time_check``：时区数据与解析探针。

为什么单独做一个模块：**冻结产物**（便携版 / 安装版）里 ``zoneinfo`` 能不能真的读到
IANA 数据，光看源码或看 ``_internal/tzdata`` 文件在不在都不算证据。这个探针不碰
网络、不建 Agent、不读数据库，可以在干净的 Windows 机器、容器、CI 里直接跑：

    atri-x.exe -m atri_x.app.time_check --json

输出 JSON，``ok=true`` 表示：``tzdata`` 可读、默认时区可解析、DST 冬夏偏移正确。
``packaging/build.py`` 的产物级核验会跑它；部署后也可以手动跑。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import Any, Sequence

from atri_x.errors import AtriXError
from atri_x.timezones import (
    DEFAULT_TIMEZONE,
    format_offset,
    iana_zone_count,
    resolve_timezone,
    tzdata_version,
    zoneinfo_available,
)

__all__ = ["check_time_data", "main"]

#: DST 探针用的时区与时刻（与机器本地时区无关，纯 IANA 规则）。
_DST_ZONE = "America/New_York"
_DST_WINTER = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
_DST_SUMMER = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)


def check_time_data(timezone: str = DEFAULT_TIMEZONE, *, dst_probe: bool = True) -> dict[str, Any]:
    """返回一份可 JSON 序列化的时区健康报告（不抛异常）。"""
    report: dict[str, Any] = {
        "ok": False,
        "tzdata": tzdata_version(),
        "zones": iana_zone_count(),
        "zoneinfo_available": zoneinfo_available(),
        "timezone": timezone,
    }
    if not zoneinfo_available():
        report["error"] = (
            "no IANA timezone data: install the tzdata package (pip install tzdata) or "
            "check that this build bundles _internal/tzdata"
        )
        return report
    try:
        resolution = resolve_timezone(timezone)
    except AtriXError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["timezone"] = resolution.name
    report["rules"] = resolution.rules
    report["utc_offset"] = format_offset(resolution.tz)
    if dst_probe:
        report["dst_probe"] = _dst_report()
        if not report["dst_probe"].get("ok", False):
            report["error"] = "DST rules do not behave as expected"
            return report
    report["ok"] = True
    return report


def _dst_report() -> dict[str, Any]:
    """用冬/夏两个固定时刻验证 DST 规则（与本地时钟无关）。"""
    try:
        zone = resolve_timezone(_DST_ZONE)
    except AtriXError as exc:
        return {"zone": _DST_ZONE, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    winter = _DST_WINTER.astimezone(zone.tz)
    summer = _DST_SUMMER.astimezone(zone.tz)
    winter_offset = winter.utcoffset()
    summer_offset = summer.utcoffset()
    ok = winter_offset is not None and summer_offset is not None and winter_offset != summer_offset
    return {
        "zone": zone.name,
        "ok": ok,
        "winter": f"{winter.isoformat()} ({format_offset(zone.tz, _DST_WINTER)})",
        "summer": f"{summer.isoformat()} ({format_offset(zone.tz, _DST_SUMMER)})",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.app.time_check",
        description="Check IANA timezone data and DST rules (no network access).",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone to resolve (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = check_time_data(args.timezone)
    try:
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    except UnicodeEncodeError:  # pragma: no cover - GBK 控制台兜底
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
