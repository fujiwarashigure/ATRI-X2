"""构建 ATRI-X 的 Windows 免安装包，并做**产物级核验**。

为什么要有这个脚本（而不是直接让人敲 ``pyinstaller``）：

1. 传参容易漏。``--add-data`` 少一份 ``assets_data`` 时，冻结包能启动、``--version``
   正常，但首次运行生成不了默认配置——这类问题只有在**产物**上跑一次 ``config check``
   才会暴露。
2. 环境容易脏。``dist`` 里上一次构建的残留文件会被误当成这次的产物；脚本每次先删目标目录。
3. 平台容易错。PyInstaller 只产**本机**平台的包：要 Windows 包就得在 Windows 上、
   用目标 Python 版本构建；脚本开头会把解释器和 PyInstaller 版本打出来，便于对账。

用法::

    python packaging/build.py                       # onedir -> dist/windows/atri-x/（含窗口版）
    python packaging/build.py --onefile             # 单文件（只出控制台版）
    python packaging/build.py --with-pillow         # 不排除 Pillow（图像编解码加速）
    python packaging/build.py --with-kernel         # 把 runtime/dsh-kernel 打进包
    python packaging/build.py --no-gui              # 只出控制台版
    python packaging/build.py --verify-only         # 只核验已有产物
    python packaging/build.py --no-verify           # 只构建，不核验
    python packaging/build.py --release             # 一条命令出「便携版 zip + 安装包」并逐项自检

内置运行时默认**自动探测**：仓库根存在 ``runtime/dsh-kernel/kernel.json`` 就打包进去，
可用 ``--with-kernel`` / ``--no-kernel`` 强制开启/关闭。

产物用途见 ``packaging/README.md``；QQ OneBot 链路的连通性用
``atri-x.exe -m atri_x.platform.qq_onebot.smoke`` 单独验证（见 ``docs/windows-app.md``）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence, TextIO

#: 仓库根（本文件的上一级）。
REPO_ROOT = Path(__file__).resolve().parent.parent
#: 冻结入口。
ENTRY_PATH = Path(__file__).resolve().parent / "entry.py"
#: PyInstaller 描述文件。
SPEC_PATH = Path(__file__).resolve().parent / "atri-x.spec"

#: 默认产物目录（仓库根的 ``dist/`` 与 ``build/`` 已在 .gitignore 里）。
DEFAULT_DIST = REPO_ROOT / "dist" / "windows"
DEFAULT_WORK = REPO_ROOT / "build" / "pyinstaller"

#: 内置运行时目录（由 ``packaging/assemble_kernel.py`` 生成）。
RUNTIME_DIR = REPO_ROOT / "runtime"

#: 判断「运行时确实组装过」的标记文件。
RUNTIME_MARKER = RUNTIME_DIR / "dsh-kernel" / "kernel.json"

#: ``atri_x`` 要求的最低 Python。
MIN_PYTHON = (3, 11)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回退出码。"""
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:  # argparse 的 --help / 参数错误
        return int(exc.code or 0)

    _out("== ATRI-X windows packaging ==")
    _out("python      : %s (%d.%d.%d)" % (sys.executable, *sys.version_info[:3]))
    if sys.version_info[:2] < MIN_PYTHON:
        _err(
            "atri-x needs Python >= %d.%d, got %d.%d"
            % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info[0], sys.version_info[1])
        )
        return 2

    name = args.name
    artifact = artifact_path(args.dist, name, onefile=args.onefile)
    with_kernel = resolve_kernel_flag(args)
    with_gui = (not args.onefile) and (not args.no_gui)

    if args.verify_only:
        return verify(artifact, name, with_kernel=with_kernel, with_gui=with_gui)

    version = pyinstaller_version()
    if version is None:
        _err("PyInstaller is not installed for this interpreter.")
        _err("install it with: %s -m pip install pyinstaller" % sys.executable)
        return 2
    _out("pyinstaller : %s" % version)
    _out("mode        : %s" % ("onefile" if args.onefile else "onedir"))
    _out("dist        : %s" % args.dist)
    _out("work        : %s" % args.work)
    _out("gui exe     : %s" % ("yes" if with_gui else "no"))
    _out("dsh kernel  : %s" % ("yes" if with_kernel else "no"))
    if args.release:
        _out("release     : portable zip + installer（两者都会逐项自检）")
    if args.onefile and with_kernel:
        _out("note        : onefile 模式不打包内置运行时（体积与启动时间都不可接受）")

    code = run_build(args, name, with_kernel=with_kernel, with_gui=with_gui)
    if code != 0:
        _err("PyInstaller failed with exit code %d" % code)
        return code

    if not artifact.exists():
        _err("build finished but artifact is missing: %s" % artifact)
        return 1

    report(artifact, onefile=args.onefile)
    if args.no_verify:
        _out("verify      : skipped (--no-verify)")
        code = 0
    else:
        code = verify(artifact, name, with_kernel=with_kernel, with_gui=with_gui)
    if code != 0:
        return code
    want_portable = args.with_portable or args.portable_verify or args.release
    want_installer = args.with_installer or args.installer_verify or args.release
    if (want_portable or want_installer) and args.onefile:
        _err("便携版 zip / 安装包只支持 onedir 产物（--onefile 不是发行形态）")
        return 1
    if want_portable:
        code = run_portable_build(
            artifact.parent,
            output=args.portable_output,
            verify_zip=(args.portable_verify or args.release),
        )
        if code != 0:
            return code
    if want_installer:
        code = run_installer_build(
            artifact.parent,
            output=args.installer_output,
            verify_install=(args.installer_verify or args.release),
        )
        if code != 0:
            return code
    return 0


def run_portable_build(
    dist_root: Path, *, output: Path | None = None, verify_zip: bool = False
) -> int:
    """调 ``packaging/build_portable.py`` 打「解压即用」便携版 zip；返回退出码。"""
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import build_portable  # noqa: PLC0415 - 只在这里需要
    except ImportError as exc:  # pragma: no cover - 同目录文件缺失
        _err("cannot import build_portable: %s" % exc)
        return 1
    argv = ["--dist", str(dist_root)]
    if output is not None:
        argv.extend(["--output", str(output)])
    if verify_zip:
        argv.append("--verify")
    return int(build_portable.main(argv))


def run_installer_build(
    dist_root: Path, *, output: Path | None = None, verify_install: bool = False
) -> int:
    """调 ``packaging/build_installer.py`` 打安装包；返回退出码。"""
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import build_installer  # noqa: PLC0415 - 只在这里需要
    except ImportError as exc:  # pragma: no cover - 同目录文件缺失
        _err("cannot import build_installer: %s" % exc)
        return 1
    argv = ["--dist", str(dist_root)]
    if output is not None:
        argv.extend(["--output", str(output)])
    if verify_install:
        argv.append("--verify")
    return int(build_installer.main(argv))


def resolve_kernel_flag(args: argparse.Namespace) -> bool:
    """内置运行时是否打包：显式开关优先，否则按 ``runtime/dsh-kernel/kernel.json`` 自动判断。"""
    if args.no_kernel:
        return False
    if args.onefile:
        return False
    if args.with_kernel:
        if not RUNTIME_MARKER.is_file():
            _err("--with-kernel 需要先跑 packaging/assemble_kernel.py 生成 %s" % RUNTIME_MARKER)
            return False
        return True
    return RUNTIME_MARKER.is_file()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(
        prog="python packaging/build.py",
        description="Build the ATRI-X Windows bundle (PyInstaller).",
    )
    parser.add_argument("--onefile", action="store_true", help="build a single .exe")
    parser.add_argument("--name", default="atri-x", help="artifact name (default: atri-x)")
    parser.add_argument("--icon", default="", help="optional .ico path")
    parser.add_argument(
        "--with-pillow",
        action="store_true",
        help="keep optional Pillow/numpy/PyYAML instead of excluding them",
    )
    parser.add_argument(
        "--dist", type=Path, default=DEFAULT_DIST, help="output directory (default: dist/windows)"
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=DEFAULT_WORK,
        help="PyInstaller work directory (default: build/pyinstaller)",
    )
    parser.add_argument("--no-verify", action="store_true", help="skip artifact verification")
    parser.add_argument(
        "--verify-only", action="store_true", help="verify an existing artifact, do not build"
    )
    parser.add_argument(
        "--no-gui", action="store_true", help="do not build the windowed atri-x-gui.exe"
    )
    parser.add_argument(
        "--with-kernel", action="store_true", help="bundle runtime/dsh-kernel into the artifact"
    )
    parser.add_argument(
        "--no-kernel", action="store_true", help="never bundle runtime/ even if it exists"
    )
    parser.add_argument(
        "--with-installer",
        action="store_true",
        help="build an Inno Setup installer from the verified onedir artifact",
    )
    parser.add_argument(
        "--installer-verify",
        action="store_true",
        help="silently install/uninstall the built installer to verify it (implies --with-installer)",
    )
    parser.add_argument(
        "--installer-output",
        type=Path,
        default=None,
        help="installer output directory (default: dist/installer)",
    )
    parser.add_argument(
        "--with-portable",
        action="store_true",
        help="also zip the verified onedir artifact into an unzip-and-run portable bundle",
    )
    parser.add_argument(
        "--portable-verify",
        action="store_true",
        help="build the portable zip and extract-verify it (implies --with-portable)",
    )
    parser.add_argument(
        "--portable-output",
        type=Path,
        default=None,
        help="portable zip output directory (default: dist/portable)",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help=(
            "one command for both deliverables: portable zip + Inno installer, "
            "each verified (implies --with-portable --portable-verify "
            "--with-installer --installer-verify)"
        ),
    )
    return parser.parse_args(list(argv))


def artifact_path(dist: Path, name: str, onefile: bool) -> Path:
    """产物路径：单文件是 ``<dist>/<name>.exe``，onedir 是 ``<dist>/<name>/<name>.exe``。"""
    if onefile:
        return Path(dist) / f"{name}.exe"
    return Path(dist) / name / f"{name}.exe"


def pyinstaller_version() -> str | None:
    """取本解释器可见的 PyInstaller 版本；没装返回 ``None``。

    必须用 ``python -m PyInstaller`` 探测：PyInstaller 的 console script 装在
    user-site 的 ``Scripts/`` 下，那个目录经常不在 ``PATH`` 上，
    ``pyinstaller: command not found`` 不能当作"没装"的证据。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    return text[0].strip() if text else "unknown"


def run_build(args: argparse.Namespace, name: str, *, with_kernel: bool, with_gui: bool) -> int:
    """调用 PyInstaller 构建；返回退出码。"""
    dist = Path(args.dist).resolve()
    work = Path(args.work).resolve()
    # 先删目标产物目录：上一次的残留文件混进来，会让"产物级核验"核验到旧文件。
    target = artifact_path(dist, name, args.onefile)
    stale = target.parent if not args.onefile else target
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)
    elif stale.is_file():
        stale.unlink(missing_ok=True)

    env = os.environ.copy()
    env["ATRIX_PKG_NAME"] = name
    env["ATRIX_PKG_ONEFILE"] = "1" if args.onefile else "0"
    env["ATRIX_PKG_ICON"] = str(Path(args.icon).resolve()) if args.icon else ""
    env["ATRIX_PKG_KEEP_OPTIONAL"] = "1" if args.with_pillow else "0"
    env["ATRIX_PKG_GUI"] = "1" if with_gui else "0"
    env["ATRIX_PKG_RUNTIME_DIR"] = str(RUNTIME_DIR) if (with_kernel and RUNTIME_DIR.is_dir()) else ""

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        str(SPEC_PATH),
    ]
    _out("run         : " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(REPO_ROOT))
    except (OSError, subprocess.SubprocessError) as exc:
        _err("failed to start PyInstaller: %s" % exc)
        return 1
    return int(proc.returncode)


def report(artifact: Path, onefile: bool) -> None:
    """打印产物体积与接下来该跑什么。"""
    if onefile:
        total = artifact.stat().st_size
        count = 1
        runtime_bytes = 0
    else:
        files = [p for p in artifact.parent.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        count = len(files)
        runtime_root = artifact.parent / "_internal" / "runtime"
        runtime_bytes = (
            sum(p.stat().st_size for p in runtime_root.rglob("*") if p.is_file())
            if runtime_root.is_dir()
            else 0
        )
    _out("artifact    : %s" % artifact)
    _out("size        : %.1f MB in %d file(s)" % (total / (1024 * 1024), count))
    if runtime_bytes:
        _out(
            "  runtime   : %.1f MB (内置 DSH 内核；不想要就 --no-kernel)"
            % (runtime_bytes / (1024 * 1024))
        )


def verify(
    artifact: Path, name: str, *, with_kernel: bool = False, with_gui: bool = False
) -> int:
    """产物级核验：跑 exe 本身，而不是看构建退出码。

    这几步都不能省：

    * ``--version``：证明冻结入口/打包结构成立；
    * ``config check``：证明 ``assets_data`` 真的进包了、冻结路径能解出默认配置
      （漏 ``--add-data`` 时这一步才会失败）；
    * ``-m <模块>``：证明 ``runpy`` 转发可用（诊断入口都靠它）；
    * ``-m atri_x.sidecar status``：内置运行时是否可解析（没内置时跳过）；
    * 窗口版 ``--selftest``：证明 tkinter 真的进包、Tk 能建窗口（只看退出码，
      窗口版没有控制台）。
    """
    if not artifact.exists():
        _err("artifact not found: %s" % artifact)
        return 1
    _out("verify      : %s" % artifact)

    version = run_exe(artifact, ["--version"], timeout=120)
    if version["code"] != 0:
        _err("verify: --version failed (exit %d)" % version["code"])
        _err(_snippet(version))
        if "temporary directory" in (version["stdout"] + version["stderr"]).lower():
            _err(
                "hint: onefile 启动时要把自身解压到 %TEMP%；TEMP 不可写（或指向不存在的目录）"
                "时会直接失败。改用 onedir（去掉 --onefile），或把 TEMP 指到可写目录。"
            )
        return 1
    text = version["stdout"].strip()
    _out("  --version : %s" % (text or "<empty>"))
    if "atri-x" not in text.lower() and name.lower() not in text.lower():
        _err("verify: --version output does not look like atri-x: %r" % text)
        return 1

    check = run_exe(artifact, ["config", "check"], timeout=180, scratch_home=True)
    combined = (check["stdout"] + "\n" + check["stderr"]).strip()
    if check["code"] != 0 or "config OK" not in combined:
        _err("verify: 'config check' failed (exit %d)" % check["code"])
        _err(_snippet(check))
        return 1
    _out("  config    : ok (default assets unpacked)")

    module = run_exe(artifact, ["-m", "atri_x.platform.qq_onebot.smoke", "--help"], timeout=120)
    if module["code"] != 0:
        _err("verify: '-m <module>' dispatch failed (exit %d)" % module["code"])
        _err(_snippet(module))
        return 1
    _out("  module    : ok (-m dispatch works)")

    sidecar = run_exe(artifact, ["-m", "atri_x.sidecar", "status", "--json"], timeout=180)
    sidecar_text = (sidecar["stdout"] or "").strip()
    if with_kernel:
        if sidecar["code"] != 0 or '"found": true' not in sidecar_text.replace("'", '"'):
            _err("verify: 内置运行时不可用（--with-kernel 时应能找到 dsh-kernel）")
            _err(_snippet(sidecar))
            return 1
        _out("  sidecar   : ok (内置 DSH 内核可解析)")
    else:
        _out("  sidecar   : skipped (未打包内置运行时)")

    if with_gui:
        gui_exe = artifact.parent / (name + "-gui.exe")
        if not gui_exe.is_file():
            _err("verify: windowed exe missing: %s" % gui_exe)
            return 1
        gui = run_exe(gui_exe, ["--selftest"], timeout=180, scratch_home=True)
        if gui["code"] != 0:
            _err("verify: atri-x-gui.exe --selftest failed (exit %d)" % gui["code"])
            _err(_snippet(gui))
            return 1
        _out("  gui       : ok (Tk selftest, exit 0)")

    _out("verify      : passed")
    _out("")
    _out("next:")
    _out("  %s doctor --no-model" % artifact)
    _out("  %s -m atri_x.platform.qq_onebot.smoke --json" % artifact)
    if with_gui:
        _out("  %s" % (artifact.parent / (name + "-gui.exe")))
    _out("  %s -m atri_x.dist.install install --autostart" % artifact)
    return 0


def run_exe(
    artifact: Path, argv: Sequence[str], timeout: float, scratch_home: bool = False
) -> dict:
    """跑一次产物，返回 ``{code, stdout, stderr}``。

    ``scratch_home=True`` 时把 ``APPDATA`` / ``XDG_CONFIG_HOME`` 指到
    ``build/pyinstaller-verify/`` 下，避免核验过程往用户真实配置目录里写东西。
    """
    env = os.environ.copy()
    if scratch_home:
        home = Path(DEFAULT_WORK).parent / "pyinstaller-verify"
        appdata = home / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)
        env["APPDATA"] = str(appdata)
        env["XDG_CONFIG_HOME"] = str(appdata)
    try:
        proc = subprocess.run(
            [str(artifact), *argv],
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"code": 1, "stdout": "", "stderr": "failed to run artifact: %s" % exc}
    return {
        "code": int(proc.returncode),
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }


def _snippet(result: dict, limit: int = 800) -> str:
    """把子进程输出的尾部截一段，便于报错时定位。"""
    text = (result.get("stderr") or "").strip() or (result.get("stdout") or "").strip()
    return text[-limit:]


def _out(text: str) -> None:
    _write(sys.stdout, text + "\n")


def _err(text: str) -> None:
    _write(sys.stderr, text + "\n")


def _write(stream: TextIO, text: str) -> None:
    """写文本并按 PORTING §7.4 兜底（GBK 控制台写不了时退 ASCII 转义）。"""
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


if __name__ == "__main__":
    raise SystemExit(main())
