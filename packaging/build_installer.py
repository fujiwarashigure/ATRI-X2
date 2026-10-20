"""构建 ATRI-X 的 Windows 安装包（Inno Setup）。

用法（在仓库根执行）::

    python packaging/build.py --with-kernel                 # 先出 dist/windows/atri-x
    python packaging/build_installer.py                     # 再打安装包
    python packaging/build_installer.py --verify            # 打包 + 静默安装/卸载自检
    python packaging/build_installer.py --dist <目录> --name ATRI-X-0.9.0-lite-setup

ISCC 查找顺序：``--iscc`` → ``ATRIX_ISCC`` 环境变量 → ``packaging/tools/ISCC.exe``
→ 常见安装路径（Inno Setup 6 / 7）→ ``PATH`` 上的 ``iscc``。找不到时给出下载地址。
``docs/windows-app.md`` 记录了把官方安装包便携安装到任意工具目录的做法。

**不修改** ``dist/`` 里的产物：安装包只是把 onedir 目录打进去。自检（``--verify``）
把安装包真的静默装到 ``build/installer/verify``（可用 ``--verify-dir`` 改），断言文件、
``install.json`` 标记、卸载器都在，然后再静默卸载并断言目录被清空。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from atri_x import __version__  # noqa: E402 - 路径修正后才能导入

__all__ = ["build", "find_iscc", "main", "verify"]

#: Inno Setup 编译器文件名。
ISCC_NAME = "ISCC.exe"
#: 安装包脚本。
ISS_PATH = Path(__file__).resolve().parent / "atri-x.iss"
#: 默认 dist 目录（``packaging/build.py`` 的输出）。
DEFAULT_DIST = _REPO / "dist" / "windows" / "atri-x"
#: 默认安装包输出目录。
DEFAULT_OUTPUT = _REPO / "dist" / "installer"
#: 默认工作目录（日志与自检安装目录）。
DEFAULT_WORK = _REPO / "build" / "installer"
#: 自检安装/卸载的最长等待（秒）：含内置内核与内置 QQ 协议端（约 1.2 GB）时解压要几分钟。
VERIFY_TIMEOUT = 1800.0
#: 必须存在的 exe。
REQUIRED_EXES = ("atri-x.exe", "atri-x-gui.exe")
#: 内置 QQ 协议端环境目录与其中必须存在的文件（``--with-qqproto`` 构建时才有）。
QQPROTO_DIR = "qqproto"
QQPROTO_REQUIRED = ("Start-NapCat.bat", "Stop-NapCat.bat", "proto-info.json")


def _log(message: str) -> None:
    """ASCII 安全输出（GBK 控制台不抛编码异常）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


class InstallerError(RuntimeError):
    """可预期的构建/自检失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# ISCC
# ---------------------------------------------------------------------------


def _iscc_candidates() -> list[Path]:
    """按优先级列出可能的 ISCC 路径。"""
    env_path = (os.environ.get("ATRIX_ISCC") or "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parent / "tools" / ISCC_NAME)
    # 本仓库常见布局：便携安装在仓库内 `_tools/` 或仓库父目录 `_tools/`（如
    # `<任意工具目录>\InnoSetup7`，由 docs/windows-app.md 的安装命令产生）。
    tool_roots = [Path(__file__).resolve().parent.parent / "_tools"]
    if _REPO.parent not in (_REPO, Path(_REPO.anchor)):
        tool_roots.append(_REPO.parent / "_tools")
    for root in tool_roots:
        for name in ("InnoSetup7", "InnoSetup6", "Inno Setup 7", "Inno Setup 6", "InnoSetup"):
            candidates.append(root / name / ISCC_NAME)
    for base in (
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramW6432", ""),
    ):
        if not base:
            continue
        for name in ("Inno Setup 7", "Inno Setup 6"):
            candidates.append(Path(base) / name / ISCC_NAME)
        # 便携安装（/DIR=... 指定到某个工具目录）时常见的位置。
        candidates.append(Path(base) / "Programs" / "Inno Setup 7" / ISCC_NAME)
        candidates.append(Path(base) / "Programs" / "Inno Setup 6" / ISCC_NAME)
    return candidates


def find_iscc(explicit: str = "") -> str:
    """找到可用的 ISCC.exe；找不到抛 :class:`InstallerError`。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise InstallerError(f"--iscc 指定的文件不存在：{path}")
    for candidate in _iscc_candidates():
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    found = shutil.which("iscc")
    if found:
        return found
    raise InstallerError(
        "找不到 Inno Setup 编译器 ISCC.exe。\n"
        "  1) 下载 https://jrsoftware.org/isdl.php（或使用本机已有的安装包）；\n"
        "  2) 用户级静默安装：\n"
        '     "<安装包>.exe" /PORTABLE=1 /CURRENTUSER /VERYSILENT /DIR="<目录>"\n'
        "  3) 设置环境变量 ATRIX_ISCC=<目录>\\ISCC.exe 或传 --iscc。"
    )


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def _check_dist(dist: Path) -> dict[str, Any]:
    """校验 dist 目录并汇总体积信息。"""
    if not dist.is_dir():
        raise InstallerError(
            f"找不到发行目录：{dist}\n先跑 python packaging/build.py --with-kernel"
        )
    missing = [name for name in REQUIRED_EXES if not (dist / name).is_file()]
    if missing:
        raise InstallerError(f"发行目录缺少可执行文件：{', '.join(missing)}（{dist}）")
    files = [path for path in dist.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    kernel = dist / "_internal" / "runtime" / "dsh-kernel" / "kernel.json"
    if not kernel.is_file():
        kernel = dist / "runtime" / "dsh-kernel" / "kernel.json"
    dsh_version = ""
    if kernel.is_file():
        try:
            dsh_version = str(json.loads(kernel.read_text(encoding="utf-8")).get("dshVersion") or "")
        except (OSError, ValueError):
            dsh_version = ""
    qqproto = dist / QQPROTO_DIR
    return {
        "dist": str(dist),
        "files": len(files),
        "bytes": total,
        "kernel": bool(kernel.is_file()),
        "dshVersion": dsh_version,
        "qqproto": qqproto.is_dir(),
    }


def _quote_define(value: str) -> str:
    """Inno 的 /D 值：带空格时用双引号包住（脚本里按字符串字面量收）。"""
    return f'"{value}"' if " " in value else value


def build(
    *,
    dist: Path,
    output: Path,
    name: str,
    version: str,
    iscc: str,
    work: Path,
    keep_work: bool = True,
) -> dict[str, Any]:
    """调 ISCC 编译安装包；返回产物信息。"""
    info = _check_dist(dist)
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if not ISS_PATH.is_file():
        raise InstallerError(f"缺少安装包脚本：{ISS_PATH}")
    log_path = work / (name + ".iscc.log")
    # 简体中文语言包不一定随编译器安装（Inno 7 自带、6.7 便携版没有）。
    zh_isl = Path(iscc).resolve().parent / "Languages" / "ChineseSimplified.isl"
    args = [
        iscc,
        "/Qp",
        f"/DSOURCE_DIR={_quote_define(str(dist))}",
        f"/DOUTPUT_DIR={_quote_define(str(output))}",
        f"/DAPP_VERSION={version}",
        f"/DOUTPUT_BASENAME={name}",
        f"/O{output}",
    ]
    if zh_isl.is_file():
        args.append("/DWITH_ZH=1")
    else:
        _log("[installer] 未找到 ChineseSimplified.isl，安装包只有英文界面")
    args.append(str(ISS_PATH))
    started = time.time()
    _log("[installer] ISCC " + " ".join(args))
    completed = subprocess.run(  # noqa: S603 - 参数来自本脚本
        args,
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    output_text = (completed.stdout or "") + (completed.stderr or "")
    try:
        log_path.write_text(output_text, encoding="utf-8")
    except OSError:
        pass
    if completed.returncode != 0:
        tail = "\n".join(output_text.strip().splitlines()[-30:])
        raise InstallerError(f"ISCC 编译失败（exit={completed.returncode}）：\n{tail}")
    installer = output / (name + ".exe")
    if not installer.is_file():
        raise InstallerError(f"ISCC 报告成功但没有产物：{installer}")
    compiled = {
        "installer": str(installer),
        "bytes": installer.stat().st_size,
        "seconds": round(time.time() - started, 1),
        "log": str(log_path),
        "source": info,
        "version": version,
    }
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return compiled


# ---------------------------------------------------------------------------
# 自检：静默安装 → 断言 → 静默卸载 → 断言
# ---------------------------------------------------------------------------


def _run_installer(
    exe: Path, args: list[str], timeout: float, *, env: dict[str, str] | None = None
) -> int:
    """跑安装/卸载器并等它退出；返回退出码。"""
    command = [str(exe), *args]
    completed = subprocess.run(  # noqa: S603 - 参数由本脚本组装
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        detail = ((completed.stdout or "") + (completed.stderr or "")).strip()
        raise InstallerError(
            f"{exe.name} 退出码 {completed.returncode}：\n{detail[-2000:]}"
        )
    return completed.returncode


def _with_log_tail(message: str, log_path: Path, limit: int = 3000) -> str:
    """把 Inno 安装日志尾部拼进错误信息（silent 模式下这是唯一线索）。"""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return message
    tail = "\n".join(text.strip().splitlines()[-30:])
    return f"{message}\n--- {log_path.name} ---\n{tail[-limit:]}"


def _short_verify_dir() -> Path:
    """挑一个**短**的本地盘目录做自检安装。

    内置内核里有 190 字符以上的相对路径（``@opentelemetry`` 的嵌套包）。安装根目录
    太长时 Inno 会报 ``MoveFile failed; code 3``（超过 MAX_PATH 260）。隔离环境
    （例如 dpx 的 ``LOCALAPPDATA``）路径很深，因此优先选短目录。
    """
    import tempfile

    candidates = [Path(tempfile.gettempdir()) / "atri-x-installer-verify"]
    username = (os.environ.get("USERNAME") or "").strip()
    if username:
        candidates.append(Path("C:/Users") / username / "AtriXVerify")
    for candidate in candidates:
        if len(str(candidate)) + 200 <= 250:
            return candidate
    return candidates[0]


def verify(installer: Path, verify_dir: Path, timeout: float = VERIFY_TIMEOUT) -> dict[str, Any]:
    """把安装包静默装到 ``verify_dir``，断言产物，再静默卸载。

    **会在本地磁盘上跑**：安装包先复制到 ``%TEMP%``，安装目录也默认在本地盘。
    原因（实测）：从仓库所在的 M:（映射盘/网络盘）直接执行安装包时，进程可能被加上
    受限令牌或直接被 SmartScreen/执行策略挡掉 —— 表现为无日志的退出码 1，或
    ``RegCreateKeyEx code 5``（连 HKCU 卸载项都建不出来）。真实用户从下载目录
    （本地盘）安装不受影响。
    """
    if verify_dir.exists():
        shutil.rmtree(verify_dir, ignore_errors=True)
    verify_dir.mkdir(parents=True, exist_ok=True)
    work = verify_dir.parent
    import tempfile

    local_setup = Path(tempfile.gettempdir()) / f"atri-x-installer-verify-{os.getpid()}.exe"
    try:
        shutil.copy2(installer, local_setup)
    except OSError as exc:
        raise InstallerError(f"复制安装包到本地临时目录失败：{local_setup}：{exc}") from exc
    # 注意：**不要**把 TEMP/TMP 指到仓库（M: 之类的映射盘/网络盘）上。Inno 会把自身
    # 解压到 %TEMP%\is-*.tmp 再以该副本运行；从映射盘运行时进程会被加上受限令牌，
    # HKCU 写一律 RegCreateKeyEx code 5（实测；卸载项因此建不出来）。默认 TEMP 是本地
    # 盘，保持继承即可。
    env = dict(os.environ)
    install_log = work / "install.log"
    uninstall_log = work / "uninstall.log"
    common = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS"]
    # 自检不勾任何 [Tasks]：既不动 HKCU Run（开机自启），也不建桌面图标。
    _log(f"[installer] verify install -> {verify_dir}")
    started = time.time()
    try:
        _run_installer(
            local_setup,
            [
                *common,
                "/MERGETASKS=!autostart,!desktopicon",
                f"/DIR={verify_dir}",
                f"/LOG={install_log}",
            ],
            timeout,
            env=env,
        )
    except InstallerError as exc:
        raise InstallerError(_with_log_tail(str(exc), install_log)) from exc
    finally:
        try:
            local_setup.unlink()
        except OSError:
            pass
    report: dict[str, Any] = {
        "verifyDir": str(verify_dir),
        "installSeconds": round(time.time() - started, 1),
        "installLog": str(install_log),
        "files": [],
    }
    for name in REQUIRED_EXES:
        path = verify_dir / name
        if not path.is_file():
            raise InstallerError(f"安装后缺少 {name}：{path}")
    marker_path = verify_dir / "install.json"
    if not marker_path.is_file():
        raise InstallerError(f"安装后缺少 install.json 标记：{marker_path}")
    qqproto_dir = verify_dir / QQPROTO_DIR
    if qqproto_dir.is_dir():
        missing = [name for name in QQPROTO_REQUIRED if not (qqproto_dir / name).is_file()]
        if missing:
            raise InstallerError(f"安装后内置协议端缺少文件：{', '.join(missing)}（{qqproto_dir}）")
        shells = sorted(
            path
            for path in qqproto_dir.iterdir()
            if path.is_dir() and (path / "NapCatWinBootMain.exe").is_file()
        )
        if not shells:
            raise InstallerError(f"安装后内置协议端缺少 NapCat Shell 目录：{qqproto_dir}")
        report["qqproto"] = {"shell": shells[0].name, "files": sum(1 for p in qqproto_dir.rglob('*') if p.is_file())}
    else:
        report["qqproto"] = None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise InstallerError(f"install.json 不是合法 JSON：{exc}") from exc
    if marker.get("installer") != "inno":
        raise InstallerError(f"install.json 的 installer 字段不是 inno：{marker!r}")
    if os.path.normcase(str(marker.get("target") or "")) != os.path.normcase(str(verify_dir)):
        raise InstallerError(
            f"install.json 的 target 与安装目录不一致：{marker.get('target')!r} != {verify_dir}"
        )
    uninstaller = verify_dir / "unins000.exe"
    if not uninstaller.is_file():
        raise InstallerError(f"安装后缺少卸载器：{uninstaller}")
    report["marker"] = marker
    report["fileCount"] = sum(1 for path in verify_dir.rglob("*") if path.is_file())

    # 程序本体能否运行（版本命令必须可用；防止把空壳打进去）。
    exe = verify_dir / "atri-x.exe"
    version_run = subprocess.run(  # noqa: S603
        [str(exe), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(verify_dir),
    )
    if version_run.returncode != 0:
        raise InstallerError(
            f"安装后的 atri-x.exe --version 失败：{(version_run.stderr or '').strip()[:500]}"
        )
    report["versionOutput"] = (version_run.stdout or "").strip().splitlines()[-1:]
    try:
        _run_installer(
            uninstaller,
            ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={uninstall_log}"],
            timeout,
            env=env,
        )
    except InstallerError as exc:
        raise InstallerError(_with_log_tail(str(exc), uninstall_log)) from exc
    leftover = _wait_for_removal(verify_dir, timeout=15.0)
    if leftover:
        names = ", ".join(path.name for path in leftover[:8])
        raise InstallerError(f"卸载后仍有残留文件（{len(leftover)} 个）：{names}")
    report["uninstalled"] = True
    report["uninstallSeconds"] = round(time.time() - started - report["installSeconds"], 1)
    return report


def _wait_for_removal(directory: Path, timeout: float) -> list[Path]:
    """等安装目录被卸载器清空。

    Inno 的卸载器是**先返回、再延迟自删**（``unins000.exe`` 把自己交给一个临时进程后才
    退出）。实测卸载命令返回时目录里可能只剩 ``unins000.exe``，一两秒后消失。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        leftover = (
            [path for path in directory.rglob("*") if path.is_file()]
            if directory.exists()
            else []
        )
        if not leftover:
            return []
        if time.time() >= deadline:
            return leftover
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python packaging/build_installer.py",
        description="用 Inno Setup 把 dist/windows/atri-x 打成单个安装包",
    )
    parser.add_argument("--dist", default=str(DEFAULT_DIST), help="onedir 发行目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="安装包输出目录")
    parser.add_argument("--work", default=str(DEFAULT_WORK), help="工作目录（ISCC 日志/自检安装）")
    parser.add_argument("--name", default="", help="输出文件名（不含 .exe）")
    parser.add_argument("--version", default=__version__, help="显示在安装包里的版本号")
    parser.add_argument("--iscc", default="", help="ISCC.exe 路径（也可用 ATRIX_ISCC）")
    parser.add_argument("--verify", action="store_true", help="打包后静默安装/卸载自检")
    parser.add_argument("--no-verify", action="store_true", help="兼容参数：不做什么（默认不自检）")
    parser.add_argument("--verify-dir", default="", help="自检安装目录（默认 %%TEMP%%\\atri-x-installer-verify）")
    parser.add_argument("--keep-work", action="store_true", help="保留工作目录里的日志")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码（0 成功，1 失败）。"""
    args = _parse_args(argv)
    dist = Path(args.dist).resolve()
    output = Path(args.output).resolve()
    work = Path(args.work).resolve()
    name = args.name.strip() or f"ATRI-X-{args.version}-setup"
    try:
        iscc = find_iscc(args.iscc)
        _log(f"[installer] ISCC = {iscc}")
        compiled = build(
            dist=dist,
            output=output,
            name=name,
            version=args.version,
            iscc=iscc,
            work=work,
            keep_work=args.keep_work,
        )
        summary: dict[str, Any] = {"ok": True, **compiled}
        size_mb = compiled["bytes"] / (1024 * 1024)
        _log(
            f"[installer] ok: {compiled['installer']} "
            f"({size_mb:.1f} MB, {compiled['seconds']}s, source files={compiled['source']['files']}, "
            f"kernel={compiled['source']['kernel']}, qqproto={compiled['source']['qqproto']})"
        )
        if args.verify:
            if args.verify_dir:
                verify_dir = Path(args.verify_dir).resolve()
            else:
                # 默认放本地盘短路径：见 _short_verify_dir 的说明。
                verify_dir = _short_verify_dir()
            report = verify(Path(compiled["installer"]), verify_dir)
            summary["verify"] = report
            _log(
                "[installer] verify ok: "
                f"install={report['installSeconds']}s fileCount={report['fileCount']} "
                f"uninstall={report['uninstallSeconds']}s"
            )
        if args.json:
            _log(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (InstallerError, OSError, subprocess.SubprocessError) as exc:
        _log(f"[installer] FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
