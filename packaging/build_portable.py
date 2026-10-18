"""把 PyInstaller onedir 产物打成「解压即用」的便携版 zip（绿色版）。

为什么单独一个脚本
------------------

* 免安装版的交付形态是**一个 zip**：解压到任意目录就能双击运行，不写注册表、
  不建快捷方式、不依赖 Python / Node / Docker。
* 便携模式的数据目录要落在**解压目录里的** ``data\\appdata``：排查问题时把整个文件夹
  （程序 + 配置 + 会话库 + 日志）压缩发走就能复现，这是它和安装版最大的差别。
* 打包错误只有**解压并运行之后**才暴露（zip 里漏了 ``_internal``、启动器写错、
  长路径把文件截断），所以本脚本自带解压级核验：解压到本地短路径 → 跑 exe 本体 →
  跑启动器 → 断言数据确实落在包内。

用法::

    python packaging/build_portable.py                  # dist/windows/atri-x -> dist/portable/*.zip
    python packaging/build_portable.py --verify         # 打完解压到本地临时目录并跑一遍
    python packaging/build_portable.py --json --verify

常规路径由 :mod:`packaging.build` 调用（``--with-portable`` / ``--portable-verify`` /
``--release``），一般不用手动跑。

zip 内布局::

    ATRI-X-<版本>/
    ├── atri-x.exe / atri-x-gui.exe / _internal/…   ← PyInstaller onedir 原样
    ├── start-portable.bat    便携模式启动（数据留在本文件夹 data\\ 下）
    ├── start-normal.bat      标准模式启动（数据在 %APPDATA%\\ATRI-X，与安装版共用）
    ├── debug-console.bat     调试控制台（doctor 报告 + 命令行窗口）
    ├── README.txt            使用与排障说明（UTF-8 BOM + CRLF，记事本可读）
    └── build-info.json       构建元数据（版本、内核、文件数）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from atri_x import __version__  # noqa: E402 - 路径修正后才能导入

__all__ = [
    "DEFAULT_DIST",
    "DEFAULT_OUTPUT",
    "PORTABLE_SUFFIX",
    "build",
    "main",
    "verify",
]

#: 默认发行目录（``packaging/build.py`` 的 onedir 产物）。
DEFAULT_DIST = _REPO / "dist" / "windows" / "atri-x"
#: 默认便携包输出目录。
DEFAULT_OUTPUT = _REPO / "dist" / "portable"
#: 默认工作目录（自检解压目录）。
DEFAULT_WORK = _REPO / "build" / "portable"

#: 压缩包名后缀（``ATRI-X-0.9.3-portable-win64.zip``）。
PORTABLE_SUFFIX = "-portable-win64"
#: zip 顶层目录名（短名换取 Windows MAX_PATH 余量）。
FOLDER_PREFIX = "ATRI-X-"
#: 必须存在的 exe。
REQUIRED_EXES = ("atri-x.exe",)
#: 窗口版 exe（``--no-gui`` 构建时缺失，属于可选）。
GUI_EXE = "atri-x-gui.exe"
#: 便携包内额外文件。
INFO_NAME = "build-info.json"
README_NAME = "README.txt"
LAUNCHER_PORTABLE = "start-portable.bat"
LAUNCHER_NORMAL = "start-normal.bat"
LAUNCHER_DEBUG = "debug-console.bat"
#: 发行目录里的顶层垃圾文件：IME 日志（搜狗会往 cwd 写）与安装版标记，不应进便携包。
EXCLUDED_ROOT_NAMES = ("debug.log", "install.json")
#: zip 压缩级别（6 = zlib 默认，够快；node_modules 文本压缩比很高）。
ZIP_COMPRESSLEVEL = 6
#: 解压级核验的默认超时（秒）。
VERIFY_TIMEOUT = 900.0


class PortableError(RuntimeError):
    """可预期的构建/核验失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 文件与发行目录
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    """ASCII 安全输出（GBK 控制台不抛编码异常）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _fmt_bytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    """算文件 SHA256（分块读，别把 300 MB 读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_kernel(dist: Path) -> dict[str, Any] | None:
    """读内置 DSH 内核清单；没打内核返回 ``None``。"""
    for relative in (("_internal", "runtime"), ("runtime",)):
        manifest = Path(dist).joinpath(*relative, "dsh-kernel", "kernel.json")
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"manifest": "dsh-kernel/kernel.json", "error": "kernel.json 解析失败"}
        return {
            "manifest": manifest.relative_to(Path(dist)).as_posix(),
            "dshVersion": str(data.get("dshVersion") or ""),
            "nodeVersion": str(data.get("nodeVersion") or ""),
            "packages": int(data.get("packages") or 0),
            "verified": bool(data.get("verified")),
        }
    return None


def dist_tree(dist: Path) -> dict[str, Any]:
    """枚举发行目录里要打进去的文件（跳过顶层垃圾文件）。"""
    dist = Path(dist)
    if not dist.is_dir():
        raise PortableError(f"找不到发行目录：{dist}\n先跑 python packaging/build.py")
    missing = [name for name in REQUIRED_EXES if not (dist / name).is_file()]
    if missing:
        raise PortableError(f"发行目录缺少可执行文件：{', '.join(missing)}（{dist}）")
    entries: list[tuple[str, Path, int]] = []
    total = 0
    longest = ""
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(dist)
        if len(relative.parts) == 1 and relative.name in EXCLUDED_ROOT_NAMES:
            _log(f"[portable] 跳过非产物顶层文件：{relative.name}")
            continue
        arc = relative.as_posix()
        size = path.stat().st_size
        total += size
        entries.append((arc, path, size))
        if len(arc) > len(longest):
            longest = arc
    if not entries:
        raise PortableError(f"发行目录里没有文件：{dist}")
    entries.sort(key=lambda item: item[0])
    return {
        "dist": str(dist),
        "entries": entries,
        "files": len(entries),
        "bytes": total,
        "longest": longest,
        "gui": (dist / GUI_EXE).is_file(),
        "kernel": read_kernel(dist),
    }


def _pyinstaller_version() -> str:
    """探测本解释器可见的 PyInstaller 版本（探测失败返回空串）。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 固定命令
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    text = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    return text[0].strip() if text else ""


# ---------------------------------------------------------------------------
# 启动器与说明文件
# ---------------------------------------------------------------------------


def launcher_portable() -> str:
    """便携模式启动器：APPDATA 指到解压目录的 ``data\\appdata``。"""
    lines = [
        "@echo off",
        "rem ATRI-X portable launcher: keeps ALL user data inside .\\data\\appdata\\ATRI-X",
        "rem Double-click to start the desktop UI in portable mode; --args are forwarded.",
        "setlocal",
        'set "HERE=%~dp0"',
        'if not exist "%HERE%data\\appdata" mkdir "%HERE%data\\appdata"',
        'set "APPDATA=%HERE%data\\appdata"',
        'set "XDG_CONFIG_HOME=%HERE%data\\appdata"',
        'cd /d "%HERE%"',
        'if not "%~1"=="" goto withargs',
        'start "" "%HERE%atri-x-gui.exe"',
        "exit /b 0",
        ":withargs",
        '"%HERE%atri-x-gui.exe" %*',
        "exit /b %ERRORLEVEL%",
    ]
    return "\n".join(lines) + "\n"


def launcher_normal() -> str:
    """标准模式启动器：数据在 ``%APPDATA%\\ATRI-X``（与安装版共用）。"""
    lines = [
        "@echo off",
        "rem ATRI-X standard launcher: uses %APPDATA%\\ATRI-X (same data as the installer).",
        "rem Double-click to start the desktop UI; --args are forwarded.",
        "setlocal",
        'cd /d "%~dp0"',
        'if not "%~1"=="" goto withargs',
        'start "" "%~dp0atri-x-gui.exe"',
        "exit /b 0",
        ":withargs",
        '"%~dp0atri-x-gui.exe" %*',
        "exit /b %ERRORLEVEL%",
    ]
    return "\n".join(lines) + "\n"


def launcher_debug() -> str:
    """调试控制台：便携数据目录 + doctor 报告 + 命令行窗口；带参数时直接转发。"""
    lines = [
        "@echo off",
        "rem ATRI-X debug console: portable data dir + doctor report + a shell window.",
        "rem Args are forwarded to atri-x.exe, e.g.:  debug-console.bat config check",
        "setlocal",
        'set "HERE=%~dp0"',
        'if not exist "%HERE%data\\appdata" mkdir "%HERE%data\\appdata"',
        'set "APPDATA=%HERE%data\\appdata"',
        'set "XDG_CONFIG_HOME=%HERE%data\\appdata"',
        'cd /d "%HERE%"',
        'if not "%~1"=="" goto withargs',
        "echo === ATRI-X debug console ===",
        "echo data dir : %HERE%data\\appdata\\ATRI-X",
        "echo logs     : %HERE%data\\appdata\\ATRI-X\\logs",
        "echo.",
        '"%HERE%atri-x.exe" --version',
        "echo.",
        '"%HERE%atri-x.exe" doctor --no-model',
        "echo.",
        "echo Useful commands:",
        "echo   atri-x.exe config check",
        "echo   atri-x.exe -m atri_x.sidecar status --json",
        "echo   atri-x.exe -m atri_x.platform.qq_onebot.smoke --json",
        "echo.",
        "cmd /k",
        "exit /b 0",
        ":withargs",
        '"%HERE%atri-x.exe" %*',
        "exit /b %ERRORLEVEL%",
    ]
    return "\n".join(lines) + "\n"


def readme_text(meta: Mapping[str, Any]) -> str:
    """便携包里的中文说明（UTF-8 BOM + CRLF，双击用记事本打开不乱码）。"""
    kernel = meta.get("kernel") or {}
    if kernel.get("dshVersion"):
        kernel_line = (
            f"已内置 DSH 内核 {kernel['dshVersion']}（Node {kernel.get('nodeVersion') or '?'}，"
            f"{kernel.get('packages') or 0} 个包），目标机不需要另装 Node/dsh。"
        )
    else:
        kernel_line = (
            "本包**没有**内置 DSH 内核（构建时没带 runtime/dsh-kernel）；"
            "需要深度路由时设 ATRIX_DSH_KERNEL 指向现成内核目录，或在目标机装 DSH。"
        )
    folder = meta.get("folder") or "ATRI-X"
    lines = [
        f"ATRI-X {meta.get('version')} — Windows 便携版（解压即用）",
        "",
        "一、这是什么",
        "  已经冻结好的完整运行时（Python + tkinter 都在里面），不需要安装 Python / Node / Docker。",
        "  解压后双击就能用；便携模式下删掉整个文件夹就等于卸载（不写注册表、不建快捷方式）。",
        f"  {kernel_line}",
        "",
        "二、快速开始（推荐）",
        "  1. 解压到**路径较短**的目录，例如 D:\\ATRI-X。",
        "     包里有 190 字符级的相对路径，解压根目录太深（总长超过 260）会解压失败。",
        "  2. 双击 start-portable.bat  → 桌面端启动，所有数据留在本文件夹 data\\ 下。",
        "  3. 首次启动会在配置目录生成 app.toml / services.toml / state.toml / SOUL.md。",
        "     在「模型」选项卡里填 Provider 与 API Key，点「重启后台服务」就能开始对话。",
        "",
        "三、两种数据模式（入口不同，数据目录不同，互不可见）",
        "  start-portable.bat   数据在 <本文件夹>\\data\\appdata\\ATRI-X  → 便携/排查用，整个文件夹可拷走",
        "  start-normal.bat     数据在 %APPDATA%\\ATRI-X                → 与安装版共用同一份配置与会话",
        "  （直接双击 atri-x-gui.exe 等同于标准模式。）",
        "",
        "四、目录说明",
        "  atri-x-gui.exe      桌面端（窗口 + 托盘）",
        "  atri-x.exe          控制台版：service run / doctor / config check / -m <模块>",
        "  _internal\\          Python 运行时、tkinter、默认配置资产、可选的内置 DSH 内核",
        "  start-portable.bat  便携模式启动（推荐）",
        "  start-normal.bat    标准模式启动（数据在 %APPDATA%）",
        "  debug-console.bat   调试控制台：打印 doctor、留一个命令行窗口",
        "  build-info.json     构建信息（版本、Python/PyInstaller、内核版本、文件数）",
        "  data\\              便携模式运行时生成；删掉它等于清空本便携版的配置、会话与日志",
        "",
        "五、排查问题（这也是本包存在的主要用途）",
        "  * 双击 debug-console.bat：打印版本 + doctor 报告，并留下命令行窗口，可以直接跑",
        "      atri-x.exe config check",
        "      atri-x.exe -m atri_x.sidecar status --json",
        "      atri-x.exe -m atri_x.platform.qq_onebot.smoke --json",
        "  * 日志：便携模式在 data\\appdata\\ATRI-X\\logs\\；GUI 拉起的后台服务输出在",
        "    data\\appdata\\ATRI-X\\gui\\service.out.log。",
        "  * 界面显示「已断开」：用 debug-console.bat 里手动跑 atri-x.exe service run，看前台日志。",
        "  * 整个文件夹打包发走就能复现问题（配置、会话库、日志都在 data\\ 下）。",
        "  * 同一台机器上已经装了安装版时，不要和便携版同时运行（本地 CLI 服务端口 32172 会冲突）。",
        "",
        "六、与安装版的区别",
        f"  安装版 ATRI-X-{meta.get('version')}-setup.exe：装到 %LOCALAPPDATA%\\ATRI-X\\app，",
        "  建开始菜单/可选桌面快捷方式、可选开机自启、带卸载项；用户数据在 %APPDATA%\\ATRI-X。",
        "  便携版：不写注册表、不建快捷方式、不需要管理员；删文件夹即卸载。",
        "",
        "七、已知事项",
        "  * 无代码签名，首次运行时 SmartScreen 可能提示，点「更多信息 → 仍要运行」。",
        "  * 不要在压缩包里直接运行 exe，必须先完整解压。",
        f"  * 本目录 {meta.get('files')} 个文件 / {_fmt_bytes(int(meta.get('bytes') or 0))}；",
        "    压缩包的 SHA256 见旁边的 .zip.sha256 文件。",
        "",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_info(meta: Mapping[str, Any]) -> str:
    """``build-info.json`` 内容（UTF-8 无 BOM，便于脚本读取）。"""
    payload = dict(meta)
    payload.pop("zipBytes", None)
    payload.pop("sha256", None)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def _write_entry(
    archive: zipfile.ZipFile,
    arcname: str,
    text: str,
    *,
    encoding: str = "ascii",
    bom: bool = False,
    crlf: bool = True,
) -> None:
    """往 zip 里写一个文本条目（统一 CRLF / 可选 BOM / 固定时间戳）。"""
    body = text.replace("\r\n", "\n")
    if crlf:
        body = body.replace("\n", "\r\n")
    data = body.encode(encoding)
    if bom:
        data = b"\xef\xbb\xbf" + data
    info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def build(
    *,
    dist: Path = DEFAULT_DIST,
    output: Path = DEFAULT_OUTPUT,
    version: str = "",
    name: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 onedir 产物打成便携版 zip；返回产物信息。"""
    tree = dist_tree(Path(dist))
    resolved_version = (version or "").strip() or __version__
    folder = f"{FOLDER_PREFIX}{resolved_version}"
    base = (name or "").strip() or f"{FOLDER_PREFIX}{resolved_version}{PORTABLE_SUFFIX}"
    out_dir = Path(output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{base}.zip"
    if zip_path.exists():
        zip_path.unlink()

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta: dict[str, Any] = {
        "app": "ATRI-X",
        "kind": "portable",
        "version": resolved_version,
        "builtAt": built_at,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "pyinstaller": _pyinstaller_version(),
        "platform": "windows-x64",
        "folder": folder,
        "exes": [name for name in (REQUIRED_EXES + (GUI_EXE,)) if (Path(dist) / name).is_file()],
        "launchers": [LAUNCHER_PORTABLE, LAUNCHER_NORMAL, LAUNCHER_DEBUG],
        "files": tree["files"],
        "bytes": tree["bytes"],
        "longestRelativePath": tree["longest"],
        "dataDirs": {
            "portable": "data\\appdata\\ATRI-X",
            "standard": "%APPDATA%\\ATRI-X",
        },
        "kernel": tree["kernel"],
        "compression": {"format": "zip/deflate", "level": ZIP_COMPRESSLEVEL},
    }
    if extra:
        meta.update(dict(extra))

    _log(f"[portable] source  : {tree['dist']}")
    _log(f"[portable] version : {resolved_version}")
    _log(
        f"[portable] input   : {tree['files']} file(s) / {_fmt_bytes(tree['bytes'])}"
        f"（内置内核：{'有' if tree['kernel'] else '无'}）"
    )
    longest_total = len(folder) + 1 + len(tree["longest"])
    _log(f"[portable] longest : {longest_total} 字符（解压根目录建议不超过 {260 - longest_total - 1} 字符）")
    if longest_total > 240:
        _log("[portable] 注意：解压根目录过长时 Windows 内置解压会失败，请解压到 D:\\ATRI-X 这类短路径")

    started = time.time()
    try:
        with zipfile.ZipFile(
            zip_path,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=ZIP_COMPRESSLEVEL,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            for index, (arc, path, _size) in enumerate(tree["entries"], 1):
                archive.write(str(path), f"{folder}/{arc}")
                if index % 5000 == 0:
                    _log(f"[portable] packed {index}/{tree['files']} …")
            _write_entry(archive, f"{folder}/{README_NAME}", readme_text(meta), encoding="utf-8", bom=True)
            _write_entry(archive, f"{folder}/{LAUNCHER_PORTABLE}", launcher_portable())
            _write_entry(archive, f"{folder}/{LAUNCHER_NORMAL}", launcher_normal())
            _write_entry(archive, f"{folder}/{LAUNCHER_DEBUG}", launcher_debug())
            _write_entry(archive, f"{folder}/{INFO_NAME}", build_info(meta), encoding="utf-8")
    except OSError as exc:
        raise PortableError(f"写 zip 失败：{zip_path}：{exc}") from exc

    seconds = round(time.time() - started, 1)
    zip_bytes = zip_path.stat().st_size
    digest = sha256_file(zip_path)
    sidecar = zip_path.with_suffix(".zip.sha256")
    sidecar.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii", newline="\n")
    meta["zipBytes"] = zip_bytes
    meta["sha256"] = digest
    meta["seconds"] = seconds
    meta["zip"] = str(zip_path)
    meta["sidecar"] = str(sidecar)
    _log(
        f"[portable] ok: {zip_path}（{_fmt_bytes(zip_bytes)}，"
        f"压缩比 {zip_bytes / max(1, tree['bytes']) * 100:.0f}%，{seconds}s）"
    )
    _log(f"[portable] sha256: {digest}")
    return meta


# ---------------------------------------------------------------------------
# 解压级核验
# ---------------------------------------------------------------------------


def _short_work_dir() -> Path:
    """挑一个**短**的本地目录做解压自检。

    包里有 190 字符级的相对路径；解压根 + 目录名 + 相对路径必须 ≤ 260（MAX_PATH）。
    隔离环境（例如 dpx 的 ``TEMP``/``USERPROFILE``）路径很深，所以优先用
    ``C:\\Users\\<用户>`` 下的短目录。
    """
    import tempfile

    candidates: list[Path] = []
    username = (os.environ.get("USERNAME") or "").strip()
    if username:
        candidates.append(Path("C:/Users") / username / "AtriXPortableVerify")
    candidates.append(Path("C:/AtriXPortableVerify"))
    candidates.append(Path(tempfile.gettempdir()) / "atri-x-portable-verify")
    for candidate in candidates:
        if len(str(candidate)) + 210 <= 260:
            return candidate
    return candidates[-1]


def _extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """解压；Python 3.12+ 用 PEP 706 的 ``filter="data"`` 过滤危险条目。"""
    try:
        archive.extractall(dest, filter="data")
    except TypeError:  # 3.11 及更早没有 filter 参数
        archive.extractall(dest)


def _run(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> dict[str, Any]:
    """跑一条命令，返回 ``{code, stdout, stderr, seconds}``。"""
    started = time.time()
    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本脚本组装
            list(command),
            capture_output=True,
            timeout=timeout,
            env=dict(env),
            cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"code": 1, "stdout": "", "stderr": f"failed to run: {exc}", "seconds": 0.0}
    return {
        "code": int(completed.returncode),
        "stdout": completed.stdout.decode("utf-8", "replace"),
        "stderr": completed.stderr.decode("utf-8", "replace"),
        "seconds": round(time.time() - started, 1),
    }


def _snippet(result: Mapping[str, Any], limit: int = 800) -> str:
    text = (result.get("stderr") or "").strip() or (result.get("stdout") or "").strip()
    return text[-limit:]


def verify(
    zip_path: Path,
    *,
    work_dir: Path | None = None,
    timeout: float = VERIFY_TIMEOUT,
    keep_work: bool = False,
) -> dict[str, Any]:
    """解压到本地短路径并跑一遍：exe、配置资产、诊断模块、内置内核、GUI、启动器。

    关键断言是**数据落点**：便携模式跑完之后，配置必须出现在
    ``<解压目录>\\data\\appdata\\ATRI-X`` 里，而不是用户真实的 ``%APPDATA%``。
    """
    archive_path = Path(zip_path).resolve()
    if not archive_path.is_file():
        raise PortableError(f"找不到便携包：{archive_path}")
    dest = Path(work_dir).resolve() if work_dir else _short_work_dir()
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "zip": str(archive_path),
        "bytes": archive_path.stat().st_size,
        "sha256": sha256_file(archive_path),
        "workDir": str(dest),
        "steps": [],
    }
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            tops = {name.split("/", 1)[0] for name in names if name}
            if len(tops) != 1:
                raise PortableError(f"zip 顶层不只一个目录：{sorted(tops)[:5]}（共 {len(tops)} 个）")
            folder = tops.pop()
            if not any(name == f"{folder}/atri-x.exe" for name in names):
                raise PortableError(f"zip 里没有 {folder}/atri-x.exe")
            if any(name == f"{folder}/install.json" for name in names):
                raise PortableError("zip 里混入了 install.json（安装版标记），便携包不应带")
            report.update({"folder": folder, "entries": len(names)})
            _log(f"[portable] verify: 解压到 {dest}")
            started = time.time()
            _extract(archive, dest)
            report["extractSeconds"] = round(time.time() - started, 1)
            _log(f"[portable] verify: 解压完成（{report['extractSeconds']}s，{len(names)} 条目）")

        root = dest / folder
        exe = root / "atri-x.exe"
        gui = root / GUI_EXE
        appdata = root / "data" / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["APPDATA"] = str(appdata)
        env["XDG_CONFIG_HOME"] = str(appdata)
        expected_version = root.joinpath(INFO_NAME)
        version_text = ""
        if expected_version.is_file():
            try:
                version_text = str(json.loads(expected_version.read_text(encoding="utf-8")).get("version") or "")
            except ValueError:
                version_text = ""

        def step(name: str, result: Mapping[str, Any], ok: bool, detail: str = "") -> None:
            report["steps"].append(
                {"step": name, "ok": bool(ok), "seconds": result.get("seconds", 0.0), "detail": detail}
            )
            if not ok:
                raise PortableError(
                    f"核验失败：{name}（exit={result.get('code')}）\n{_snippet(result)}"
                )
            _log(f"  {name:<12}: ok ({result.get('seconds')}s)")

        result = _run([str(exe), "--version"], cwd=root, env=env, timeout=timeout)
        ok = result["code"] == 0 and (
            not version_text or version_text in result["stdout"]
        )
        step("--version", result, ok, result["stdout"].strip())

        result = _run([str(exe), "config", "check"], cwd=root, env=env, timeout=timeout)
        combined = (result["stdout"] + "\n" + result["stderr"])
        step("config check", result, result["code"] == 0 and "config OK" in combined)

        app_toml = appdata / "ATRI-X" / "app.toml"
        if not app_toml.is_file():
            raise PortableError(f"配置没有落在便携数据目录里：{app_toml} 不存在")
        report["portableDataDir"] = str(app_toml.parent)
        _log(f"  data dir    : ok（{app_toml.parent}）")

        result = _run(
            [str(exe), "-m", "atri_x.platform.qq_onebot.smoke", "--help"],
            cwd=root,
            env=env,
            timeout=timeout,
        )
        step("-m dispatch", result, result["code"] == 0)

        result = _run(
            [str(exe), "-m", "atri_x.sidecar", "status", "--json"],
            cwd=root,
            env=env,
            timeout=timeout,
        )
        has_kernel = (root / "_internal" / "runtime" / "dsh-kernel" / "kernel.json").is_file()
        normalized = (result["stdout"] or "").replace("'", '"')
        if has_kernel:
            step("sidecar", result, result["code"] == 0 and '"found": true' in normalized)
        else:
            report["steps"].append(
                {"step": "sidecar", "ok": True, "seconds": result.get("seconds", 0.0), "detail": "skipped"}
            )
            _log("  sidecar     : skipped（未内置 DSH 内核）")

        if gui.is_file():
            result = _run([str(gui), "--selftest"], cwd=root, env=env, timeout=timeout)
            step("gui selftest", result, result["code"] == 0)
            # 启动器：cmd 从批处理里调用 GUI 会等它退出，exit /b 能带回退出码。
            result = _run(
                ["cmd", "/c", str(root / LAUNCHER_PORTABLE), "--selftest"],
                cwd=root,
                env=env,
                timeout=timeout,
            )
            step("start-portable", result, result["code"] == 0)
        else:
            _log("  gui         : skipped（构建时 --no-gui）")

        # 便携数据落点：清掉 data\ 后由「调试控制台启动器」重建，证明启动器真的改了 APPDATA。
        shutil.rmtree(appdata, ignore_errors=True)
        result = _run(
            ["cmd", "/c", str(root / LAUNCHER_DEBUG), "--version"],
            cwd=root,
            env=env,
            timeout=timeout,
        )
        step(
            "debug-console",
            result,
            result["code"] == 0 and appdata.is_dir(),
            result["stdout"].strip(),
        )
        if not appdata.is_dir():
            raise PortableError(f"启动器没有把数据目录建在包内：{appdata}")
        report["redirectVerified"] = True
        _log(f"  redirect     : ok（启动器把 APPDATA 指向 {appdata}）")
    finally:
        if not keep_work:
            shutil.rmtree(dest, ignore_errors=True)
            report["workDirRemoved"] = True

    report["ok"] = True
    _log("[portable] verify ok")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python packaging/build_portable.py",
        description="把 dist/windows/atri-x 打成解压即用的便携版 zip",
    )
    parser.add_argument("--dist", default=str(DEFAULT_DIST), help="onedir 发行目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="zip 输出目录")
    parser.add_argument("--version", default=__version__, help="版本号（写进包名与说明）")
    parser.add_argument("--name", default="", help="zip 文件名（不含 .zip，默认 ATRI-X-<版本>-portable-win64）")
    parser.add_argument("--verify", action="store_true", help="打完解压到本地目录并跑一遍")
    parser.add_argument("--no-verify", action="store_true", help="兼容参数：不做什么（默认不自检）")
    parser.add_argument("--verify-dir", default="", help="自检解压目录（默认 C:\\Users\\<用户>\\AtriXPortableVerify）")
    parser.add_argument("--keep-work", action="store_true", help="自检后保留解压目录")
    parser.add_argument("--timeout", type=float, default=VERIFY_TIMEOUT, help="单步超时（秒）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码（0 成功，1 失败）。"""
    args = _parse_args(argv)
    try:
        summary = build(
            dist=Path(args.dist),
            output=Path(args.output),
            version=args.version,
            name=args.name,
        )
        if args.verify:
            work = Path(args.verify_dir) if args.verify_dir else None
            summary["verify"] = verify(
                Path(summary["zip"]),
                work_dir=work,
                timeout=args.timeout,
                keep_work=args.keep_work,
            )
        if args.json:
            _log(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except (PortableError, OSError, subprocess.SubprocessError) as exc:
        _log(f"[portable] FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
