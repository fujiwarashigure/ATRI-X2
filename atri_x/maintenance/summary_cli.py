"""``python -m atri_x.maintenance.summary_cli``：查看/立即生成每日会话总结。

为什么 CLI 单独一个模块（而不是写在 :mod:`atri_x.maintenance.summary` 里）：
``atri_x.maintenance.__init__`` 会 import ``summary``（它是库），如果 CLI 也在同一
模块，``python -m atri_x.maintenance.summary`` 就会触发 runpy 的
``found in sys.modules ... prior to execution`` 警告、并可能按两套 ``__main__``
语义执行。把入口拆开是仓库里 ``status`` / ``smoke`` / ``sidecar.__main__`` 的既有做法。

用法::

    python -m atri_x.maintenance.summary_cli                 # 最近 3 份
    python -m atri_x.maintenance.summary_cli --build         # 现在生成并落盘
    python -m atri_x.maintenance.summary_cli --build --json  # 机器可读
    atri-x.exe -m atri_x.maintenance.summary_cli --build     # 冻结后同语义
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

from atri_x.maintenance.summary import (
    DailySummary,
    collect_digest,
    data_dir_of,
    list_summaries,
    save_summary,
    summary_dir,
    window,
)

__all__ = ["build_now", "main"]


def build_now(cfg: Any, days: int = 1, max_sessions: int = 20) -> DailySummary:
    """按当前库生成一份总结（不落盘；CLI 与排障用）。"""
    from atri_x.storage.sqlite import new as new_store  # noqa: PLC0415 - 只在构建路径需要

    from atri_x.config.app_sections import shanghai_tz  # noqa: PLC0415

    path = str(getattr(getattr(cfg, "storage", None), "sessions_sqlite_path", "") or "")
    if not path:
        raise ValueError("未配置 [storage].sessions_sqlite_path，无法生成会话总结")
    store = new_store(path)
    since, until = window(days)
    text, sessions, messages = collect_digest(
        store, since, until, max_sessions=max_sessions
    )
    day = until.astimezone(shanghai_tz()).strftime("%Y-%m-%d")
    return DailySummary(
        day=day,
        created_at=until.isoformat(),
        sessions=sessions,
        messages=messages,
        text=text,
        source="digest",
    )


def _write(line: str) -> None:
    """按当前控制台编码安全输出（GBK 控制台不抛 UnicodeEncodeError）。"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码（0 成功，2 配置/参数问题）。"""
    import argparse  # noqa: PLC0415 - 只在 CLI 路径需要

    parser = argparse.ArgumentParser(
        prog="python -m atri_x.maintenance.summary_cli",
        description="查看/生成每日会话总结（[maintenance.daily_summary]）",
    )
    parser.add_argument("--config", default="", help="app.toml 路径（默认按标准顺序查找）")
    parser.add_argument("--dir", default="", help="总结目录（默认 <数据目录>/daily_summaries）")
    parser.add_argument("--build", action="store_true", help="现在生成一份并落盘")
    parser.add_argument("--days", type=int, default=0, help="统计最近几天（0 = 用配置/1）")
    parser.add_argument("--max-sessions", type=int, default=0, help="最多汇总多少个会话")
    parser.add_argument("--limit", type=int, default=3, help="展示最近几份（默认 3）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from atri_x.config import load as load_config  # noqa: PLC0415

        cfg = load_config(args.config) if args.config else load_config()
    except Exception as exc:  # noqa: BLE001 - 配置坏了要给出可读错误而不是 traceback
        _write("config error: " + str(exc))
        return 2

    section = getattr(getattr(cfg, "maintenance", None), "daily_summary", None)
    days = args.days or int(getattr(section, "days", 0) or 1)
    max_sessions = args.max_sessions or int(getattr(section, "max_sessions", 0) or 20)
    directory = args.dir.strip() or summary_dir(data_dir_of(cfg))

    if args.build:
        try:
            summary = build_now(cfg, days=days, max_sessions=max_sessions)
        except Exception as exc:  # noqa: BLE001
            _write("build error: " + str(exc))
            return 2
        path = ""
        if directory:
            try:
                path = save_summary(directory, summary)
            except OSError as exc:
                _write("save error: " + str(exc))
                return 2
        if args.json:
            _write(
                json.dumps(
                    {
                        "day": summary.day,
                        "sessions": summary.sessions,
                        "messages": summary.messages,
                        "path": path,
                        "text": summary.text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            _write(summary.text)
            if path:
                _write("")
                _write("saved: " + path)
        return 0

    summaries = list_summaries(directory, limit=args.limit)
    if args.json:
        _write(
            json.dumps(
                [
                    {
                        "day": item.day,
                        "createdAt": item.created_at,
                        "sessions": item.sessions,
                        "messages": item.messages,
                        "source": item.source,
                        "text": item.text,
                    }
                    for item in summaries
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if not summaries:
        _write("暂无会话总结（目录：" + (directory or "<未配置>") + "）")
        _write("生成一份：python -m atri_x.maintenance.summary_cli --build")
        return 0
    for index, item in enumerate(summaries):
        if index:
            _write("")
        _write(item.text)
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
