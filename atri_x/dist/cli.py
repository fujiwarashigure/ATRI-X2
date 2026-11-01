"""``python -m atri_x.dist.install`` 的命令行（解析 + 文本输出）。

从 :mod:`atri_x.dist.install` 拆出来的原因：安装/卸载/状态三套逻辑已经占满 600 行
上限，CLI 再长就超；拆开后两边各管一件事，入口保持 ``-m atri_x.dist.install`` 不变。

``status --json`` 里的 ``runtime`` 是运行期实际解析的数据根（见
:func:`atri_x.dist.paths.home_snapshot`）：``dataDir`` 只是安装时的选择，用户设了
``ATRIX_HOME`` 时以 ``runtime.homeDir`` 为准。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from atri_x import __version__
from atri_x.dist.install import InstallError, install, status, uninstall
from atri_x.dist.marker import write_install_state
from atri_x.dist.migrate import MigrationError, migrate

__all__ = ["finalize", "main"]


def finalize(data_dir: str, *, migrated: bool = False, target: str = "") -> dict[str, Any]:
    """写 Inno 安装标记（安装器 ``ssPostInstall`` 调用）并返回摘要。"""
    state = write_install_state(data_dir, migrated=migrated, target=target)
    return {
        "ok": True,
        "action": "finalize",
        "target": state["target"],
        "dataDir": state["dataDir"],
        "migrated": bool(state["dataMigrated"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.dist.install",
        description=f"ATRI-X {__version__} 安装器（用户级安装 / 卸载 / 状态 / 数据迁移）",
    )
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    common = argparse.ArgumentParser(add_help=False)
    # 允许 ``--json`` 写在子命令后面（argparse 的全局选项默认只能写在前面）。
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出一行 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install", parents=[common], help="安装到用户目录")
    install_parser.add_argument("--target", default="", help="安装目录（默认 %%LOCALAPPDATA%%\\ATRI-X\\app）")
    install_parser.add_argument(
        "--data-dir",
        default="",
        help="用户数据与配置目录（默认 <安装目录>\\data；写绝对路径）",
    )
    install_parser.add_argument("--no-desktop", action="store_true", help="不建桌面快捷方式")
    install_parser.add_argument("--autostart", action="store_true", help="同时开启开机自启")
    install_parser.add_argument("--add-path", action="store_true", help="把安装目录加进用户 PATH")
    install_parser.add_argument("--force", action="store_true", help="目标目录非空也覆盖")
    install_parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")

    uninstall_parser = sub.add_parser("uninstall", parents=[common], help="卸载")
    uninstall_parser.add_argument("--yes", action="store_true", help="确认执行（否则只打印计划）")
    uninstall_parser.add_argument("--purge-data", action="store_true", help="连用户数据一起删")
    uninstall_parser.add_argument("--keep-app", action="store_true", help="保留程序目录")
    uninstall_parser.add_argument(
        "--force", action="store_true", help="Inno Setup 安装的包也用脚本强删（不推荐）"
    )

    sub.add_parser("status", parents=[common], help="显示安装状态与数据目录")

    finalize_parser = sub.add_parser(
        "finalize", parents=[common], help="写 Inno 安装标记（安装器 ssPostInstall 调用）"
    )
    finalize_parser.add_argument("--data-dir", required=True, help="用户数据与配置目录（绝对路径）")
    finalize_parser.add_argument("--migrated", action="store_true", help="旧数据已迁移")
    finalize_parser.add_argument("--target", default="", help="安装目录（默认当前 exe 所在目录）")

    migrate_parser = sub.add_parser("migrate", parents=[common], help="把旧数据目录搬到新位置")
    migrate_parser.add_argument("--from", dest="source", required=True, help="旧数据目录（绝对路径）")
    migrate_parser.add_argument("--to", dest="target", required=True, help="新数据目录（绝对路径）")
    migrate_parser.add_argument("--move", action="store_true", help="复制核对通过后删除旧目录")
    migrate_parser.add_argument("--overwrite", action="store_true", help="目标非空也合并覆盖")
    migrate_parser.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回进程退出码（0 成功，1 可预期失败）。"""
    parser = _parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "install":
            report = install(
                args.target,
                data_dir=args.data_dir,
                desktop=not args.no_desktop,
                autostart=args.autostart,
                add_path=args.add_path,
                force=args.force,
                dry_run=args.dry_run,
            )
            return _emit(report.to_dict(), args.json)
        if args.command == "uninstall":
            report = uninstall(
                yes=args.yes,
                purge_data=args.purge_data,
                keep_app=args.keep_app,
                force=args.force,
            )
            return _emit(report.to_dict(), args.json)
        if args.command == "migrate":
            result = migrate(
                args.source,
                args.target,
                move=args.move,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
            return _emit(result.to_dict(), args.json)
        if args.command == "finalize":
            payload = finalize(args.data_dir, migrated=args.migrated, target=args.target)
            return _emit(payload, args.json)
        return _emit(status(), args.json)
    except (InstallError, MigrationError, ValueError) as exc:
        _stderr(f"atri-x install: {exc}")
        return 1


def _emit(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        text = json.dumps(payload, ensure_ascii=False)
    elif payload.get("action") == "uninstall":
        text = str(payload.get("detail") or "")
        if payload.get("dataDir"):
            text += f"\n  数据目录：{payload.get('dataDir')}（{'已删除' if not payload.get('kept_data') else '保留'}）"
        for path in payload.get("shortcuts") or []:
            text += "\n  删除快捷方式：" + path
        for warning in payload.get("warnings") or []:
            text += "\n  警告：" + warning
    elif payload.get("action") == "finalize":
        text = "安装标记已写入"
        text += f"\n  安装目录：{payload.get('target')}"
        text += f"\n  数据目录：{payload.get('dataDir')}"
        if payload.get("migrated"):
            text += "\n  旧数据：已迁移"
    elif payload.get("action") == "install":
        text = str(payload.get("detail") or "安装完成")
        text += f"\n  安装目录：{payload.get('target')}"
        text += f"\n  数据目录：{payload.get('dataDir')}"
        for path in payload.get("shortcuts") or []:
            text += "\n  快捷方式：" + path
        if payload.get("autostart"):
            text += "\n  开机自启：已启用"
        for warning in payload.get("warnings") or []:
            text += "\n  警告：" + warning
    elif "source" in payload and "target" in payload:  # migrate
        text = str(payload.get("detail") or "迁移完成")
        text += f"\n  源目录：{payload.get('source')}"
        text += f"\n  目标目录：{payload.get('target')}"
        for warning in payload.get("warnings") or []:
            text += "\n  警告：" + warning
    elif "installed" in payload:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:  # pragma: no cover - 兜底
        text = json.dumps(payload, ensure_ascii=False)
    _stdout(text)
    # 计划性结果（dry-run/未确认）是 ok=True；被拒绝（例如 Inno 安装包）是 ok=False。
    return 0 if payload.get("ok", True) else 1


def _stdout(text: str) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:  # windowed exe 没有控制台
        return
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    stream.flush()


def _stderr(text: str) -> None:
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
