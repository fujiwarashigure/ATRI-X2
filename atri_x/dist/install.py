"""安装/卸载（用户级，不写系统目录、不需要管理员权限）。

把「一个能双击的桌面程序」补齐到可交付状态：

- **安装**：把发行包（PyInstaller onedir）复制到 ``%LOCALAPPDATA%\\ATRI-X\\app``，
  建开始菜单/桌面快捷方式，写 ``HKCU`` 的「应用和功能」卸载项，可选开机自启；
- **卸载**：删快捷方式、卸载项、自启动项，删除程序目录；**默认保留用户数据**
  （``%APPDATA%\\ATRI-X``：配置、记忆、SQLite、日志），要连数据一起删得显式
  ``--purge-data``；
- **状态**：读 ``install.json`` 报告装在哪、快捷方式在哪、是否自启。

三条边界：

1. 只写 ``HKCU`` 与用户目录，不动 ``HKLM``/``Program Files``；
2. 源码运行（未冻结）时**不复制**代码，只写一个开发版启动器（快捷方式指向
   ``python -m atri_x.gui``），避免把仓库拷来拷去；
3. ``--dry-run`` 只打印计划，不落盘（测试与确认安装内容时用）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from atri_x import __version__
from atri_x.dist.marker import INSTALL_MARKER
from atri_x.dist.marker import read_marker as _read_marker
from atri_x.dist.marker import write_marker as _write_marker
from atri_x.dist.paths import (
    APP_NAME,
    InstallError,
    data_dir,
    default_target,
    desktop_dir,
    is_frozen,
    source_root,
    start_menu_dir,
)

__all__ = [
    "APP_NAME",
    "INSTALL_MARKER",
    "UNINSTALL_KEY",
    "InstallError",
    "InstallReport",
    "default_target",
    "install",
    "is_frozen",
    "source_root",
    "status",
    "uninstall",
]

#: 「应用和功能」里的卸载项（``HKCU``）。
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ATRI-X"

#: 复制安装目录时跳过的名字（只对源码树有意义；冻结包本来就没有这些）。
_SKIP_NAMES = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "devdocs",
        "docs",
        "tests",
        "runtime",
    }
)

#: 默认快捷方式文件名。
_SHORTCUT_NAME = APP_NAME + ".lnk"


@dataclass
class InstallReport:
    """安装/卸载结果（``--json`` 直接序列化它）。"""

    ok: bool
    action: str
    target: str = ""
    version: str = __version__
    frozen: bool = False
    dry_run: bool = False
    shortcuts: list[str] = field(default_factory=list)
    registry: bool = False
    autostart: bool = False
    kept_data: bool = True
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转 dict（``--json`` 输出用）。"""
        return {
            "ok": self.ok,
            "action": self.action,
            "target": self.target,
            "version": self.version,
            "frozen": self.frozen,
            "dry_run": self.dry_run,
            "shortcuts": list(self.shortcuts),
            "registry": self.registry,
            "autostart": self.autostart,
            "kept_data": self.kept_data,
            "detail": self.detail,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def install(
    target: str = "",
    *,
    desktop: bool = True,
    autostart: bool = False,
    add_path: bool = False,
    force: bool = False,
    dry_run: bool = False,
    shortcut_writer: Callable[..., str] | None = None,
    registry: Any = None,
    start_menu: str = "",
    desktop_folder: str = "",
) -> InstallReport:
    """安装到用户目录；返回 :class:`InstallReport`。

    ``shortcut_writer`` / ``registry`` 可注入（测试用假实现，避免碰真注册表/桌面）。
    """
    frozen = is_frozen()
    destination = os.path.abspath(os.path.expanduser(target or default_target()))
    report = InstallReport(ok=False, action="install", target=destination, frozen=frozen, dry_run=dry_run)
    writer = shortcut_writer or _default_shortcut_writer()
    registry_impl = registry if registry is not None else _registry_backend()

    if os.path.abspath(source_root()) == destination and frozen:
        # 已经在安装目录里运行：只补快捷方式/注册表（修复安装）。
        report.detail = "已在安装目录运行，只补快捷方式与卸载项"
    elif frozen:
        if os.path.isdir(destination) and os.listdir(destination) and not force:
            raise InstallError(f"目标目录已存在且非空：{destination}（加 --force 覆盖）")
        if not dry_run:
            _copy_tree(source_root(), destination)
    else:
        report.detail = "源码模式：不复制代码，只写启动器与快捷方式"
        if not dry_run:
            os.makedirs(destination, exist_ok=True)
            _write_launcher(destination)

    target_exe, arguments, working_dir = _launch_command(destination, frozen)
    links: list[str] = []
    for folder in (start_menu or start_menu_dir(), desktop_folder or (desktop_dir() if desktop else "")):
        if not folder:
            continue
        link_path = os.path.join(folder, _SHORTCUT_NAME)
        links.append(link_path)
        if dry_run:
            continue
        try:
            writer(
                link_path,
                target_exe,
                arguments=arguments,
                working_dir=working_dir,
                description=f"{APP_NAME} 桌面端（后台常驻）",
            )
        except Exception as exc:  # noqa: BLE001 - 快捷方式失败不该让安装整体失败
            report.warnings.append(f"创建快捷方式失败：{link_path}：{exc}")
    report.shortcuts = links

    marker = {
        "app": APP_NAME,
        "version": __version__,
        "installedAt": _now(),
        "frozen": frozen,
        "source": source_root(),
        "target": destination,
        "launchExe": target_exe,
        "arguments": arguments,
        "shortcuts": links,
        "desktop": bool(desktop),
        "autostart": bool(autostart),
    }
    if not dry_run:
        _write_marker(destination, marker)

    if autostart:
        detail = _enable_autostart(destination, frozen, dry_run)
        report.autostart = True
        if detail:
            report.warnings.append(detail)

    if add_path and not dry_run:
        try:
            registry_impl.add_to_user_path(destination)
        except Exception as exc:  # noqa: BLE001 - PATH 失败不阻断安装
            report.warnings.append(f"加入 PATH 失败：{exc}")

    if not dry_run:
        try:
            registry_impl.write_uninstall(_uninstall_payload(destination, frozen))
            report.registry = True
        except Exception as exc:  # noqa: BLE001 - 注册表失败不该丢安装结果
            report.warnings.append(f"写卸载项失败：{exc}")

    report.ok = True
    if not report.detail:
        report.detail = "安装完成"
    return report


def uninstall(
    *,
    yes: bool = False,
    purge_data: bool = False,
    keep_app: bool = False,
    dry_run: bool = False,
    shortcut_remover: Callable[[str], None] | None = None,
    registry: Any = None,
    force: bool = False,
) -> InstallReport:
    """卸载；``yes=False`` 时只返回计划（调用方负责确认）。

    ``force=False`` 且安装由 Inno Setup 安装包创建（``install.json`` 里
    ``installer == "inno"``）时**拒绝**用脚本卸载：程序目录、快捷方式、卸载项和
    开机自启都由 Inno 自己的 ``unins000.exe`` 管理，脚本再删一遍会留下一个指向
    已删除目录的卸载器。
    """
    marker = _read_marker()
    target = str(marker.get("target") or default_target())
    report = InstallReport(ok=False, action="uninstall", target=target, frozen=is_frozen(), dry_run=dry_run)
    if marker.get("installer") == "inno" and not force:
        report.detail = (
            "该安装由 Inno Setup 安装包创建，请用「设置 → 应用 → 已安装的应用」"
            "或安装目录里的 unins000.exe 卸载"
            + (f"（卸载器：{marker.get('uninstaller')}）" if marker.get("uninstaller") else "")
            + "；确实要用脚本强删请加 --force"
        )
        report.kept_data = True
        return report
    report.kept_data = not purge_data
    remove_link = shortcut_remover or _remove_file
    links = list(marker.get("shortcuts") or [])
    if not links:
        links = [os.path.join(start_menu_dir() or "", _SHORTCUT_NAME)]
    if dry_run or not yes:
        report.shortcuts = links
        report.registry = bool(marker)
        report.detail = "计划卸载（未执行；加 --yes 才真的删）"
        report.ok = True
        return report

    removed: list[str] = []
    for link in links:
        if not link:
            continue
        try:
            remove_link(link)
            removed.append(link)
        except OSError as exc:
            report.warnings.append(f"删除快捷方式失败：{link}：{exc}")
    report.shortcuts = removed

    detail = _disable_autostart()
    if detail:
        report.warnings.append(detail)

    registry_impl = registry if registry is not None else _registry_backend()
    try:
        registry_impl.delete_uninstall()
        registry_impl.remove_from_user_path(target)
        report.registry = True
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"清理注册表失败：{exc}")

    if not keep_app and os.path.isdir(target):
        leftover = _remove_install_dir(target)
        if leftover:
            report.warnings.append(leftover)
    if purge_data:
        directory = data_dir()
        if directory and os.path.isdir(directory):
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                report.warnings.append(f"删除数据目录失败：{directory}：{exc}")
    report.ok = True
    report.detail = "卸载完成（用户数据保留）" if not purge_data else "卸载完成（含用户数据）"
    return report


def status() -> dict[str, Any]:
    """安装状态快照（``--json`` 输出）。"""
    marker = _read_marker()
    target = str(marker.get("target") or "")
    return {
        "installed": bool(marker),
        "installer": str(marker.get("installer") or "portable"),
        "frozen": is_frozen(),
        "version": __version__,
        "target": target,
        "targetExists": bool(target and os.path.isdir(target)),
        "shortcuts": [
            {"path": path, "exists": os.path.isfile(path)}
            for path in (marker.get("shortcuts") or [])
        ],
        "autostart": _read_autostart(),
        "dataDir": data_dir(),
        "marker": marker,
    }


# ---------------------------------------------------------------------------
# 内部：复制 / 启动器 / 快捷方式
# ---------------------------------------------------------------------------


def _copy_tree(source: str, destination: str) -> None:
    """复制发行包到安装目录（跳过源码树里的大目录）。"""
    os.makedirs(destination, exist_ok=True)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _SKIP_NAMES}

    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=_ignore)


def _write_launcher(directory: str) -> None:
    """源码模式：写一个 ``atri-x.cmd`` 启动器（不带 BOM、CRLF 不重要但用 CRLF 更稳）。"""
    content = (
        "@echo off\r\n"
        f'cd /d "%~dp0"\r\n'
        f'"{sys.executable}" -m atri_x.gui %*\r\n'
    )
    with open(os.path.join(directory, "atri-x.cmd"), "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _launch_command(destination: str, frozen: bool) -> tuple[str, str, str]:
    """快捷方式的目标/参数/工作目录。"""
    if frozen:
        for name in ("atri-x-gui.exe", "ATRI-X.exe", "atri-x.exe"):
            candidate = os.path.join(destination, name)
            if os.path.isfile(candidate):
                return candidate, "", destination
        return os.path.join(destination, "atri-x.exe"), "", destination
    return sys.executable, "-m atri_x.gui", destination


def _default_shortcut_writer() -> Callable[..., str]:
    from atri_x.dist.shell_link import create_shortcut

    return create_shortcut


def _remove_file(path: str) -> None:
    if os.path.isfile(path):
        os.remove(path)


def _remove_install_dir(target: str) -> str:
    """删安装目录；返回残留说明（空串 = 已删干净）。

    Windows 不允许删除**正在运行**的 exe，所以从安装目录里跑卸载时，第一次
    ``rmtree`` 必然剩下 ``atri-x.exe`` / ``atri-x-gui.exe``。这里退一步：先尽力删，
    还有残留就起一个**脱离的** ``cmd`` 循环，等进程退出后再删（最多重试 60 秒）。
    """
    if not os.path.isdir(target):
        return ""
    shutil.rmtree(target, ignore_errors=True)
    if not os.path.isdir(target):
        return ""
    _schedule_removal(target)
    return f"程序目录仍被占用，已在后台安排删除（约 1 分钟内）：{target}"


def _schedule_removal(target: str) -> None:
    """起一个脱离的控制台进程，等文件锁释放后循环删除目录。"""
    quoted = '"' + target.replace('"', "") + '"'
    script = (
        "for /l %i in (1,1,60) do ("
        f"rmdir /s /q {quoted} >nul 2>&1 & "
        f"if not exist {quoted} exit /b 0 & "
        "timeout /t 1 /nobreak >nul)"
    )
    try:
        subprocess.Popen(  # noqa: S603 - 命令由本模块拼装，目标路径已去引号
            ["cmd", "/c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=0x00000008 | 0x08000000 if os.name == "nt" else 0,  # DETACHED|NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _uninstall_payload(destination: str, frozen: bool) -> dict[str, Any]:
    exe = os.path.join(destination, "atri-x.exe")
    uninstall_command = (
        f'"{exe}" -m atri_x.dist.install uninstall --yes'
        if frozen
        else f'"{sys.executable}" -m atri_x.dist.install uninstall --yes'
    )
    return {
        "display_name": APP_NAME,
        "display_version": __version__,
        "publisher": "ATRI-X",
        "install_location": destination,
        "display_icon": os.path.join(destination, "atri-x-gui.exe"),
        "uninstall": uninstall_command,
        "estimated_size_kb": _tree_kb(destination),
    }


def _tree_kb(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return int(total / 1024)


# ---------------------------------------------------------------------------
# 内部：注册表（真实实现在 atri_x.dist.registry，测试注入假后端）
# ---------------------------------------------------------------------------


def _registry_backend() -> Any:
    """默认注册表后端；测试可以通过 ``install(registry=...)`` 注入假实现。"""
    from atri_x.dist.registry import WindowsRegistry

    return WindowsRegistry()


# ---------------------------------------------------------------------------
# 内部：自启动
# ---------------------------------------------------------------------------


def _enable_autostart(destination: str, frozen: bool, dry_run: bool) -> str:
    """开启自启；返回警告文本（空串表示成功）。"""
    try:
        from atri_x.gui import autostart
    except Exception as exc:  # noqa: BLE001 - 没有 Tk 的机器也要能装
        return f"自启动不可用：{exc}"
    command = (
        f'"{os.path.join(destination, "atri-x-gui.exe")}" --minimized'
        if frozen
        else f'"{sys.executable}" -m atri_x.gui --minimized'
    )
    if dry_run:
        return ""
    ok, detail = autostart.enable(command)
    return "" if ok else detail


def _disable_autostart() -> str:
    try:
        from atri_x.gui import autostart
    except Exception:  # noqa: BLE001
        return ""
    ok, detail = autostart.disable()
    return "" if ok else detail


def _read_autostart() -> dict[str, Any]:
    try:
        from atri_x.gui import autostart
    except Exception:  # noqa: BLE001
        return {"supported": False}
    return autostart.status().to_dict()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m atri_x.dist.install install|uninstall|status``。"""
    parser = argparse.ArgumentParser(prog="python -m atri_x.dist.install", description="ATRI-X 安装器")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    common = argparse.ArgumentParser(add_help=False)
    # 允许 ``--json`` 写在子命令后面（argparse 的全局选项默认只能写在前面）。
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出一行 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install", parents=[common], help="安装到用户目录")
    install_parser.add_argument("--target", default="", help="安装目录（默认 %%LOCALAPPDATA%%\\ATRI-X\\app）")
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

    sub.add_parser("status", parents=[common], help="显示安装状态")

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "install":
            report = install(
                args.target,
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
        return _emit(status(), args.json)
    except InstallError as exc:
        sys.stderr.write(f"atri-x install: {exc}\n")
        return 1


def _emit(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        text = json.dumps(payload, ensure_ascii=False)
    elif payload.get("action") == "uninstall":
        text = str(payload.get("detail") or "")
        for path in payload.get("shortcuts") or []:
            text += "\n  删除快捷方式：" + path
        for warning in payload.get("warnings") or []:
            text += "\n  警告：" + warning
    elif payload.get("action") == "install":
        text = str(payload.get("detail") or "安装完成")
        text += f"\n  安装目录：{payload.get('target')}"
        for path in payload.get("shortcuts") or []:
            text += "\n  快捷方式：" + path
        if payload.get("autostart"):
            text += "\n  开机自启：已启用"
        for warning in payload.get("warnings") or []:
            text += "\n  警告：" + warning
    elif "installed" in payload:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:  # pragma: no cover - 兜底
        text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    sys.stdout.flush()
    # 计划性结果（dry-run/未确认）是 ok=True；被拒绝（例如 Inno 安装包）是 ok=False。
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
