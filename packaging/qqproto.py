"""把 NapCat / 便携 QQ 协议端环境**脱敏后**嵌进 Windows 发行包。

用法（打包脚本会调用；也可以单独跑）::

    python packaging/qqproto.py --source "D:\\NapCat.52230.Shell" --dest dist\\windows\\atri-x\\qqproto
    python packaging/qqproto.py --check dist\\windows\\atri-x\\qqproto --json

来源目录是 NapCat 一键安装器解出来的 ``NapCat.<版本>.Shell``（含
``NapCatWinBootMain.exe``、``QQ.exe`` 与 ``versions\\<QQ 版本>\\``）。本模块做四件事：

1. **只复制运行必需的部分**：Shell 根 + ``versions`` 树（便携 QQ + NapCat 本体）；
2. **脱敏**：跳过 ``resources\\app\\napcat\\`` 下的 ``config`` / ``cache`` / ``logs``
   （那里面有本机生成的 OneBot / WebUI 令牌和登录痕迹），并跳过所有 ``*.log``；
3. 写 ``Start-NapCat.bat`` / ``Stop-NapCat.bat`` / ``NapCat-使用说明.txt``
   （纯相对路径、不含任何本机绝对路径或令牌）；
4. **核验**：必需文件在、脱敏目录不在、最长相对路径不超限、生成的文本里没有盘符路径。

运行期配置（令牌 / 端口 / ``onebot11.json`` / ``webui.json``）由
:mod:`atri_x.gui.qqproto` 在目标机首次启动时生成 —— 也就是说**发出去的包里不含任何令牌**。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "ENV_DIR_NAME",
    "LAUNCHER_START",
    "LAUNCHER_STOP",
    "README_NAME",
    "INFO_NAME",
    "QqProtoError",
    "find_shell",
    "find_qq_version",
    "find_napcat_version",
    "main",
    "stage",
    "verify_payload",
]

#: 发行包里内置协议端环境的目录名（与 ``atri_x.gui.qqproto.ENV_DIR_NAME`` 一致）。
ENV_DIR_NAME = "qqproto"
#: Shell 根里的引导程序 / 便携 QQ 启动器。
SHELL_EXE = "NapCatWinBootMain.exe"
QQ_EXE = "QQ.exe"
#: 包内额外文件。
LAUNCHER_START = "Start-NapCat.bat"
LAUNCHER_STOP = "Stop-NapCat.bat"
README_NAME = "NapCat-使用说明.txt"
INFO_NAME = "proto-info.json"
#: NapCat 配置目录里**不能**进发行包的三个目录（令牌 / 缓存 / 日志）。
SENSITIVE_DIRS = ("config", "cache", "logs")
#: 解压根目录 + 包内相对路径的总长要留出 Windows MAX_PATH 余量。
MAX_RELATIVE_PATH = 200
#: 生成文本里不允许出现的「本机绝对路径 / 令牌」形状。
_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")
_PERSONAL_PATH_RE = re.compile(r"(?:Users|AppData)[\\/]", re.IGNORECASE)
_TOKEN_SHAPE_RE = re.compile(r"(?i)(?:token|secret|密码|令牌)\s*[=:]\s*[0-9a-f]{16,}")


class QqProtoError(RuntimeError):
    """可预期的打包失败（消息直接展示给用户）。"""


def _log(message: str) -> None:
    """ASCII 安全输出（GBK 控制台不抛编码异常）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


# ---------------------------------------------------------------------------
# 来源定位
# ---------------------------------------------------------------------------


def find_shell(source: Path) -> Path:
    """在 ``source`` 里找 NapCat Shell 目录（``source`` 本身或它的一级子目录）。"""
    root = Path(source).expanduser()
    if not root.is_dir():
        raise QqProtoError(f"找不到 NapCat 环境目录：{root}")
    if _looks_like_shell(root):
        return root
    for child in sorted(root.iterdir()):
        if child.is_dir() and _looks_like_shell(child):
            return child
    raise QqProtoError(
        f"{root} 不是 NapCat Shell 目录：需要 {SHELL_EXE}、{QQ_EXE} 与 versions\\ 子目录。\n"
        "请指向一键安装器解出来的 NapCat.<版本>.Shell 目录。"
    )


def _looks_like_shell(path: Path) -> bool:
    return (
        (path / SHELL_EXE).is_file()
        and (path / "versions").is_dir()
        and (path / QQ_EXE).is_file()
    )


def _shell_app_dir(shell: Path) -> Path:
    """``<shell>\\versions\\<QQ 版本>\\resources\\app\\napcat``；找不到抛错。"""
    for candidate in sorted((shell / "versions").iterdir()):
        app = candidate / "resources" / "app" / "napcat"
        if (app / "napcat.mjs").is_file():
            return app
    raise QqProtoError(f"{shell} 里没有 versions\\<版本>\\resources\\app\\napcat（NapCat 不完整）")


def find_qq_version(shell: Path) -> str:
    """从 ``versions\\<版本>`` 目录名取 QQ 版本（如 ``9.9.33-52230``）。"""
    try:
        names = sorted(
            item.name
            for item in (shell / "versions").iterdir()
            if (item / "resources" / "app" / "napcat" / "napcat.mjs").is_file()
        )
    except OSError:
        return ""
    return names[0] if names else ""


def find_napcat_version(app_dir: Path) -> str:
    """从 ``napcat.mjs`` 里读 NapCat 版本（``&& "4.18.38" || "1.0.0-dev"`` 形状）。"""
    path = app_dir / "napcat.mjs"
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    match = re.search(rb'&&\s*"(\d+\.\d+\.\d+)"\s*\|\|', data)
    if match is None:
        return ""
    return match.group(1).decode("ascii", "replace")


# ---------------------------------------------------------------------------
# 复制（脱敏）
# ---------------------------------------------------------------------------


def _ignore_factory(app_dir: Path):
    """返回 ``shutil.copytree`` 的 ignore 回调：跳过 NapCat 配置/缓存/日志与 ``*.log``。"""

    def ignore(directory: str, names: list[str]) -> list[str]:
        current = Path(directory).resolve()
        skipped: list[str] = []
        try:
            current.relative_to(app_dir)
            in_app = True
        except ValueError:
            in_app = False
        for name in names:
            if in_app and name in SENSITIVE_DIRS:
                skipped.append(name)
                continue
            if name.endswith(".log"):
                skipped.append(name)
                continue
            if name in ("__pycache__", ".cache"):
                skipped.append(name)
        return skipped

    return ignore


def _copy_shell(shell: Path, target: Path) -> tuple[int, int]:
    """复制 Shell 树（脱敏），返回 ``(文件数, 字节数)``。"""
    app_dir = _shell_app_dir(shell)
    ignore = _ignore_factory(app_dir.resolve())
    try:
        shutil.copytree(shell, target, ignore=ignore, symlinks=False)
    except OSError as exc:
        raise QqProtoError(f"复制 NapCat 环境失败：{exc}") from exc
    files = 0
    total = 0
    for path in target.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    return files, total


#: 复制后**不许**出现在产物里的目录名（NapCat 运行期在本机生成的配置/缓存/日志）。
_NAPCAT_RUNTIME_DIRS = ("config", "cache", "logs")


def find_sanitize_offenders(target: Path) -> list[str]:
    """扫出 ``target`` 里残留的 NapCat 运行期文件（相对路径，POSIX 分隔符）。

    :func:`verify_payload` 在 ``stage()`` 末尾已经查过一次，但它只看
    ``versions/<版本>/resources/app/napcat`` 内部。这里**有意放宽**到整棵负载树
    （任何路径里带 ``/napcat/`` 的 config/cache/logs、任何 ``*.log``、任何
    ``onebot11.json`` / ``webui.json``），给「拿现成产物目录重打安装包」这种路径用：
    目录一旦被运行期写过配置（程序会把自己的令牌写回 qqproto 目录），重打包就会把
    来源机令牌带进发行物，而这个路径上没有 stage 的核验兜底。
    """
    offenders: list[str] = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(target).as_posix()
        parts = rel.split("/")
        if "/napcat/" in f"/{rel}" and any(d in parts for d in _NAPCAT_RUNTIME_DIRS):
            offenders.append(rel)
        elif path.suffix.lower() == ".log":
            offenders.append(rel)
        elif path.name.lower() in ("onebot11.json", "webui.json"):
            offenders.append(rel)
    return offenders


def assert_sanitized(target: Path) -> None:
    """打包前硬校验：负载树里残留 NapCat 运行期文件就失败（含疑似令牌）。"""
    offenders = find_sanitize_offenders(Path(target))
    if not offenders:
        return
    preview = "、".join(offenders[:5])
    more = f" 等 {len(offenders)} 个" if len(offenders) > 5 else ""
    raise QqProtoError(
        "内置协议端脱敏失败：产物里残留 NapCat 运行期文件"
        f"（{preview}{more}）。这些文件可能带来源机令牌，请重跑 qqproto 复制或删掉它们。"
    )


# ---------------------------------------------------------------------------
# 生成文件（启动脚本 / 说明 / 元数据）
# ---------------------------------------------------------------------------


def start_launcher(shell_name: str) -> str:
    """``Start-NapCat.bat``：纯 ASCII + 相对路径（cmd 码页无关）。"""
    lines = [
        "@echo off",
        "rem ============================================================",
        "rem  ATRI-X built-in QQ protocol environment (portable QQ + NapCat)",
        "rem  Keep THIS window open: closing it takes the bot offline.",
        "rem  Login QR / WebUI: ATRI-X GUI -> tab Online -> Open WebUI.",
        "rem ============================================================",
        "setlocal",
        f'cd /d "%~dp0{shell_name}"',
        "echo Starting NapCat (portable QQ). Keep this window open.",
        '".\\NapCatWinBootMain.exe"',
        'set "NAP =%ERRORLEVEL%"',
        "echo.",
        "echo NapCat exited with code %NAP%.",
        "pause",
        "exit /b %NAP%",
    ]
    return "\r\n".join(lines) + "\r\n"


def stop_launcher() -> str:
    """``Stop-NapCat.bat``：只停**本程序托管的**协议端进程树。

    旧版本这里是 ``taskkill /f /im QQ.exe``——会结束本机**所有** QQ 进程（包括与
    ATRI-X 无关的会话）。现在改成委托给 ``atri-x.exe -m atri_x.gui.qqproto --stop``：
    它按 PID 归属停止（PID + 镜像名 + 启动时间都记在 ``atri-x-proto-process.json``），
    不是本程序启动的进程一律不动。找不到 exe 时只打印指引，不猜、不杀。
    """
    lines = [
        "@echo off",
        "rem Stop the protocol backend that ATRI-X started (PID-owned).",
        "rem This script does NOT kill every QQ.exe on the machine.",
        "setlocal",
        'set "ATRIX=%~dp0..\\atri-x.exe"',
        'if not exist "%ATRIX%" goto nogui',
        '"%ATRIX%" -m atri_x.gui.qqproto --stop',
        "exit /b %ERRORLEVEL%",
        ":nogui",
        "echo ATRI-X executable not found next to this folder.",
        "echo Stop the protocol backend from the ATRI-X GUI: tab Online -^> Stop protocol backend.",
        "timeout /t 3 >nul",
        "exit /b 1",
    ]
    return "\r\n".join(lines) + "\r\n"


def readme_text(shell_name: str, qq_version: str, napcat_version: str) -> str:
    """``NapCat-使用说明.txt``（UTF-8 BOM + CRLF，记事本直接可读）。"""
    version_line = "QQ {}".format(qq_version or "9.x")
    if napcat_version:
        version_line += " + NapCat {}".format(napcat_version)
    lines = [
        "ATRI-X 内置 QQ 协议端（NapCat + 便携 QQ）",
        "=========================================",
        "",
        "一、这是什么",
        f"  本目录是随 ATRI-X 一起分发的 QQ 协议端环境：{version_line}。",
        "  ATRI-X 通过 OneBot v11 正向 WebSocket 连接它，从而收发 QQ 群 / 私聊消息。",
        "  打包时已经脱敏：不含任何令牌、登录态或 QQ 用户数据；令牌由 ATRI-X 首次启动时",
        "  自动生成并写进配置目录的 .env（QQONEBOT_ACCESS_TOKEN）。",
        "",
        "二、怎么用（推荐全程在 ATRI-X 桌面端操作）",
        "  1. 启动 ATRI-X 桌面端（便携版 start-portable.bat / 安装版开始菜单）。",
        "  2. 「在线」页 →「启动协议端」：会打开本目录的 NapCat 控制台窗口，保持它开着",
        "     （关掉窗口 = 下线）。",
        "  3. 「在线」页 →「打开 WebUI」：浏览器里用手机 QQ 扫码登录（建议用小号）。",
        "  4. 回到「在线」页点「连接（上线）」，状态变成「已连接（QQ 在线）」即可。",
        "  不想用桌面端时，也可以直接双击本目录的 Start-NapCat.bat。",
        "",
        "三、目录",
        f"  {shell_name}\\   便携 QQ + NapCat 本体（约 1.2 GB）",
        f"  {LAUNCHER_START}      启动协议端（等于运行 NapCatWinBootMain.exe）",
        f"  {LAUNCHER_STOP}       停止协议端（只停 ATRI-X 自己启动的那个进程，不影响其它 QQ）",
        f"  {INFO_NAME}       构建元数据（协议端类型/版本、文件数、脱敏标记，不含令牌）",
        "",
        "四、注意",
        "  * 默认端口：OneBot 3001 / WebUI 6099；实际端口见 ATRI-X「在线」页。",
        "    端口若被 Windows 的动态保留段（Hyper-V / WSL / Docker）或别的进程占着，",
        "    ATRI-X 首次准备时会自动顺延到可用端口，并把 ws_url 一起改过来。",
        "  * 同一台机器不要同时运行两份 NapCat（端口会冲突）。",
        "  * 整个程序目录可以移动；ATRI-X 会在新位置沿用同一份协议端配置。",
        "  * 非官方协议端有账号风控风险，请使用小号并自担风险。",
        "  * 便携 QQ 与 NapCat 的版权归各自作者 / 腾讯所有，本包只做随包分发。",
        "",
    ]
    return "\r\n".join(lines) + "\r\n"


def _write_text(path: Path, text: str, *, bom: bool = False) -> None:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def stage(
    source: Path,
    dest: Path,
    *,
    generated_at: str = "",
) -> dict[str, Any]:
    """把 ``source`` 的 NapCat Shell 脱敏复制到 ``dest``（``<dist>/atri-x/qqproto``）。"""
    shell = find_shell(Path(source))
    app_dir = _shell_app_dir(shell)
    qq_version = find_qq_version(shell)
    napcat_version = find_napcat_version(app_dir)

    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    _log(f"[qqproto] source  : {shell}")
    _log(f"[qqproto] versions: qq={qq_version or '?'} napcat={napcat_version or '?'}")
    target_shell = dest / shell.name
    files, total = _copy_shell(shell, target_shell)
    _log(f"[qqproto] copied  : {files} file(s) / {total / (1024 * 1024):.1f} MB（已跳过 config/cache/logs 与 *.log）")

    _write_text(dest / LAUNCHER_START, start_launcher(shell.name), bom=False)
    _write_text(dest / LAUNCHER_STOP, stop_launcher(), bom=False)
    _write_text(
        dest / README_NAME,
        readme_text(shell.name, qq_version, napcat_version),
        bom=True,
    )
    info = {
        "app": "ATRI-X",
        "kind": "qqproto",
        # 协议端类型：发行包里目前只允许内置 NapCat。SnowLuma 的专有原生组件并入
        # 安装包需要书面授权（SnowLuma EULA §5.4），所以不内置、走外置接入。
        "backend": "napcat",
        "backendVersion": napcat_version,
        "shell": shell.name,
        "qqVersion": qq_version,
        "napcatVersion": napcat_version,
        "sanitized": True,
        "generatedAt": generated_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "bytes": total,
        "launchers": [LAUNCHER_START, LAUNCHER_STOP],
        "configPolicy": "config/cache/logs excluded; tokens generated on the target machine",
    }
    _write_text(dest / INFO_NAME, json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    summary = verify_payload(dest)
    summary.update({"seconds": round(time.time() - started, 1), "source": str(shell)})
    _log(
        f"[qqproto] ok: {dest}（{summary['files']} 文件 / "
        f"{summary['bytes'] / (1024 * 1024):.1f} MB，最长相对路径 {summary['longestPath']}）"
    )
    return summary


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------


def verify_payload(payload: Path) -> dict[str, Any]:
    """核验已经复制好的 ``qqproto`` 目录（必需文件 / 脱敏 / 路径长度 / 文本内容）。"""
    payload = Path(payload).resolve()
    if not payload.is_dir():
        raise QqProtoError(f"找不到协议端负载目录：{payload}")
    shell = find_shell(payload)
    app_dir = _shell_app_dir(shell)
    qq_version = find_qq_version(shell)
    napcat_version = find_napcat_version(app_dir)
    required = [
        shell / SHELL_EXE,
        shell / QQ_EXE,
        app_dir / "napcat.mjs",
        app_dir / "node_modules",
        app_dir / "native",
        payload / LAUNCHER_START,
        payload / LAUNCHER_STOP,
        payload / README_NAME,
        payload / INFO_NAME,
    ]
    missing = [str(path.relative_to(payload)) for path in required if not path.exists()]
    if missing:
        raise QqProtoError("协议端负载缺少必需文件：\n  " + "\n  ".join(missing))

    forbidden: list[str] = []
    files = 0
    total = 0
    longest = 0
    for path in payload.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
            relative = path.relative_to(payload)
            longest = max(longest, len(relative.as_posix()))
            try:
                inside_app = app_dir in path.parents
            except ValueError:  # pragma: no cover - Path.parents 不会抛，只是保守
                inside_app = False
            if inside_app and path.parent.name in SENSITIVE_DIRS:
                forbidden.append(relative.as_posix())
            if path.suffix.lower() == ".log":
                forbidden.append(relative.as_posix())
    if forbidden:
        raise QqProtoError(
            "协议端负载没有脱敏干净（不应包含 NapCat 配置/缓存/日志）：\n  "
            + "\n  ".join(sorted(forbidden)[:10])
        )
    if longest > MAX_RELATIVE_PATH:
        raise QqProtoError(
            f"协议端负载里有超长相对路径（{longest} > {MAX_RELATIVE_PATH}），"
            "解压时可能撞 Windows MAX_PATH"
        )

    _check_generated_text(payload)
    return {
        "payload": str(payload),
        "shell": shell.name,
        "qqVersion": qq_version,
        "napcatVersion": napcat_version,
        "files": files,
        "bytes": total,
        "longestPath": longest,
        "sanitized": True,
    }


def _check_generated_text(payload: Path) -> None:
    """生成的文本里不许出现盘符绝对路径、个人目录或令牌明文。"""
    for name in (LAUNCHER_START, LAUNCHER_STOP, README_NAME, INFO_NAME):
        path = payload / name
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise QqProtoError(f"读不了 {name}：{exc}") from exc
        if _ABSOLUTE_PATH_RE.search(text):
            raise QqProtoError(f"{name} 里出现了盘符绝对路径（换机器后会失效）")
        if _PERSONAL_PATH_RE.search(text):
            raise QqProtoError(f"{name} 里出现了个人目录痕迹（Users/AppData）")
        if _TOKEN_SHAPE_RE.search(text):
            raise QqProtoError(f"{name} 里出现了令牌形状的文本（必须由目标机生成）")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python packaging/qqproto.py",
        description="把 NapCat / 便携 QQ 协议端环境脱敏后嵌进发行包",
    )
    parser.add_argument("--source", default="", help="NapCat.<版本>.Shell 目录（或其父目录）")
    parser.add_argument("--dest", default="", help="目标目录，通常是 <dist>/atri-x/qqproto")
    parser.add_argument("--check", default="", help="只核验已有的 qqproto 目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回退出码（0 成功，1 失败）。"""
    args = _parse_args(argv)
    try:
        if args.check:
            summary = verify_payload(Path(args.check))
        elif args.source and args.dest:
            summary = stage(Path(args.source), Path(args.dest))
        else:
            _log("用法：--source <Shell 目录> --dest <qqproto 目录>，或 --check <qqproto 目录>")
            return 2
    except (QqProtoError, OSError) as exc:
        _log(f"[qqproto] FAILED: {exc}")
        return 1
    if args.json:
        _log(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
