"""子进程与 Docker Compose 前端探测。

对应上游：各 shell 脚本里的 ``detect_compose()``、``compose()``、
``compose_cmd()``、``command -v`` 以及 ``"${COMPOSE[@]}" -f <file> ...``。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。

设计要点：``run_capture`` **永不抛异常**，非零退出码由调用方判断——这直接
对应 bash 里到处出现的 ``$(cmd 2>&1 || true)``。运维脚本最怕的就是“一条
探测命令把整个脚本带崩”，所以这里把失败收敛成明确的返回码：
    127 = 命令不存在，124 = 超时，126 = 无法执行，其余为真实退出码。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = [
    "compose_argv",
    "compose_command",
    "has_command",
    "host_path",
    "record_external",
    "run_capture",
    "run_inherit",
    "run_lines",
]


def record_external(argv: Sequence[str]) -> None:
    """把一次外部命令追加到 ``ATRIX_DEPLOY_RECORD`` 指向的日志文件。

    存在的理由（只影响测试，不影响生产）：``deploy/*.py`` 会在 PATH 上找
    ``git`` / ``docker`` 并执行。在 Windows 上用 ``PATH`` 前置替身来伪造这两个
    命令是**不可靠**的——``CreateProcess`` 的名称解析不按 ``PATHEXT`` 走，
    会命中系统里真实的 ``git.exe``；退而求其次做 ``python.exe`` 的副本又会因为
    没有参数而进入 REPL 挂死。所以测试改用「设置一个记录文件路径」的方式拿到
    脚本到底调了什么命令，再配合 ``ATRIX_DEPLOY_FAKE_CAPTURE`` 提供预置输出。

    未设置该环境变量时本函数是空操作，生产路径零开销。
    """
    target = os.environ.get("ATRIX_DEPLOY_RECORD")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(" ".join(str(part) for part in argv) + "\n")
    except OSError:
        # 记录失败绝不能影响被测脚本的行为
        return


def _fake_capture(argv: Sequence[str]) -> subprocess.CompletedProcess | None:
    """按 ``ATRIX_DEPLOY_FAKE_CAPTURE`` 里预置的规则返回伪造结果。

    文件格式（每行一条，``<匹配子串>|<退出码>|<stdout>|<stderr>``；先匹配先赢）。
    未命中返回 ``None``，调用方继续走真实执行路径。
    """
    import json

    target = os.environ.get("ATRIX_DEPLOY_FAKE_CAPTURE")
    if not target or not os.path.exists(target):
        return None
    joined = " ".join(str(part) for part in argv)
    try:
        with open(target, encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return None
    for entry in entries:
        if entry.get("match", "") and entry["match"] in joined:
            return subprocess.CompletedProcess(
                list(argv),
                int(entry.get("code", 0)),
                entry.get("stdout", ""),
                entry.get("stderr", ""),
            )
    return None


def has_command(name: str) -> bool:
    """``command -v`` 的等价物。"""
    return shutil.which(name) is not None


def run_capture(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 60.0,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """跑命令并捕获输出，**不抛异常**。

    失败被折叠成 ``CompletedProcess``：``FileNotFoundError`` → 127，
    ``TimeoutExpired`` → 124，其它 ``OSError`` → 126。调用方按 ``returncode``
    判断即可，不需要 try/except。
    """
    record_external(argv)
    fake = _fake_capture(argv)
    if fake is not None:
        return fake
    try:
        return subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", f"未找到命令：{argv[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"命令超时：{' '.join(argv)}")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 126, "", f"无法执行：{exc}")


def run_inherit(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """跑命令并把 stdout/stderr 直接接到终端，返回退出码。

    用于 ``docker compose build`` / ``docker load`` 这类要给人看进度的命令。
    """
    record_external(argv)
    fake = _fake_capture(argv)
    if fake is not None:
        if fake.stdout:
            sys.stdout.write(fake.stdout)
        if fake.stderr:
            sys.stderr.write(fake.stderr)
        return int(fake.returncode)
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return 127
    except OSError:
        return 126
    return completed.returncode


def run_lines(result: subprocess.CompletedProcess) -> list[str]:
    """把捕获结果拆成非空行（stdout + stderr 合并，逐行 strip）。"""
    merged = (result.stdout or "") + (result.stderr or "")
    return [line.strip() for line in merged.splitlines() if line.strip()]


def compose_command() -> list[str] | None:
    """探测可用的 Compose 前端：``docker compose`` 优先，退回 ``docker-compose``。

    对应各脚本里的 ``detect_compose()``。返回 ``None`` 表示两者都不可用。
    注意：``docker compose version`` 会真的启一个子进程，所以在“只想问一句”
    的场景下不要反复调用——调用方应缓存结果。
    """
    if has_command("docker"):
        probe = run_capture(["docker", "compose", "version"], timeout=20.0)
        if probe.returncode == 0:
            return ["docker", "compose"]
    if has_command("docker-compose"):
        return ["docker-compose"]
    return None


def compose_argv(
    compose: Sequence[str],
    compose_file: str | os.PathLike[str],
    args: Iterable[str],
) -> list[str]:
    """拼 ``<compose> -f <compose_file> <args...>``。

    始终显式带 ``-f``：脚本可能从 cron 或其他目录被调用，不指定文件会命中
    另一个 Compose 项目或丢失 ``.env``。
    """
    return [*compose, "-f", str(compose_file), *args]


def host_path(path: str | os.PathLike[str]) -> str:
    """把宿主路径转成 Docker 能识别的形式（对应 bash ``host_path``）。

    上游在 Windows Git Bash 里调 ``cygpath -w``。Python 在 Windows 上直接给
    ``str(Path)``（``C:\\work\\...``）就能被 Docker Desktop 接受；非 Windows
    原样返回。
    """
    return str(Path(path))
