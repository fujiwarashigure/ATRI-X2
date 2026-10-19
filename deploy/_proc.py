"""子进程与 Docker Compose 前端探测。

对应上游：各 shell 脚本里的 ``detect_compose()``、``compose()``、
``compose_cmd()``、``command -v`` 以及 ``"${COMPOSE[@]}" -f <file> ...``。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。

设计要点：``run_capture`` **永不抛异常**，非零退出码由调用方判断——这直接
对应 bash 里到处出现的 ``$(cmd 2>&1 || true)``。运维脚本最怕的就是“一条
探测命令把整个脚本带崩”，所以这里把失败收敛成明确的返回码：
    127 = 命令不存在，124 = 超时，126 = 无法执行，其余为真实退出码。

唯一的例外是**伪造模式**（见 :func:`_fake_capture`）：``ATRIX_DEPLOY_FAKE_CAPTURE``
一旦设置，规则文件缺失 / 损坏 / 未命中都抛 ``AssertionError``，**绝不回落到真实
执行**。这是测试夹具的配置错误，必须响亮地失败，而不是悄悄去调真实的
``docker`` / ``git``（在没有凭据的机器上会连不上守护进程、甚至改动真实仓库）。
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

#: 伪造模式开关：设置即启用（值本身不参与判断）。
FAKE_CAPTURE_ENV = "ATRIX_DEPLOY_FAKE_CAPTURE"
#: 伪造模式下**显式**允许回落到真实执行的逃生开关（默认关闭，fail-closed）。
FAKE_ALLOW_REAL_ENV = "ATRIX_DEPLOY_FAKE_ALLOW_REAL"
#: 规则条目允许出现的键；拼错键名会被当成夹具错误直接报出来。
_FAKE_ENTRY_KEYS = frozenset(
    {"argv", "argv_prefix", "match", "code", "stdout", "stderr", "real", "note"}
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _fake_allow_real() -> bool:
    """是否显式允许伪造模式回落到真实执行（``ATRIX_DEPLOY_FAKE_ALLOW_REAL=1``）。"""
    return os.environ.get(FAKE_ALLOW_REAL_ENV, "").strip().lower() in _TRUTHY


def _fake_problem(target: str, argv: Sequence[str], detail: str) -> AssertionError:
    """构造伪造模式的失败信息：必须同时带上全 argv 与规则文件路径。"""
    return AssertionError(
        f"ATRIX_DEPLOY_FAKE_CAPTURE={target} 伪造失败：{detail}\n"
        f"  argv: {' '.join(str(part) for part in argv)}\n"
        f"  规则文件: {target}\n"
        f"  修好规则文件；确实要回落到真实执行请显式设置 {FAKE_ALLOW_REAL_ENV}=1"
    )


def _fake_entry_matches(entry: dict, argv: Sequence[str], joined: str) -> bool:
    """结构化匹配一条规则。

    条目里的 ``argv`` / ``argv_prefix`` / ``match`` 是**与**关系：声明的每个
    谓词都必须成立；至少要声明一个（只写 ``code`` 的条目会被当成夹具错误）。
    """
    argv_text = [str(part) for part in argv]
    declared = 0
    if "argv" in entry:
        declared += 1
        if [str(part) for part in entry["argv"]] != argv_text:
            return False
    if "argv_prefix" in entry:
        declared += 1
        prefix = entry["argv_prefix"]
        if argv_text[: len(prefix)] != [str(part) for part in prefix]:
            return False
    if "match" in entry:
        declared += 1
        if str(entry["match"]) not in joined:
            return False
    return declared > 0


def _fake_validate_entry(target: str, argv: Sequence[str], index: int, entry: object) -> dict:
    """校验一条规则；不合法就按夹具错误抛 ``AssertionError``。"""
    if not isinstance(entry, dict):
        raise _fake_problem(target, argv, f"第 {index} 条规则不是 JSON 对象：{entry!r}")
    unknown = sorted(set(entry) - _FAKE_ENTRY_KEYS)
    if unknown:
        raise _fake_problem(
            target, argv, f"第 {index} 条规则含未知键 {unknown}（可用键：{sorted(_FAKE_ENTRY_KEYS)}）"
        )
    for key in ("argv", "argv_prefix"):
        value = entry.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not value or not all(isinstance(part, str) for part in value):
            raise _fake_problem(
                target, argv, f"第 {index} 条规则的 {key} 必须是非空字符串列表：{value!r}"
            )
    if "match" in entry and not isinstance(entry["match"], str):
        raise _fake_problem(target, argv, f"第 {index} 条规则的 match 必须是字符串")
    if "real" in entry:
        if not isinstance(entry["real"], bool):
            raise _fake_problem(target, argv, f"第 {index} 条规则的 real 必须是 true/false")
        if entry["real"] and any(key in entry for key in ("code", "stdout", "stderr")):
            raise _fake_problem(
                target, argv, f"第 {index} 条规则同时声明了 real=true 与 code/stdout/stderr"
            )
    if not any(key in entry for key in ("argv", "argv_prefix", "match")):
        raise _fake_problem(
            target, argv, f"第 {index} 条规则没有匹配谓词（argv / argv_prefix / match 至少一个）"
        )
    try:
        int(entry.get("code", 0))
    except (TypeError, ValueError) as exc:
        raise _fake_problem(target, argv, f"第 {index} 条规则的 code 不是整数：{entry.get('code')!r}") from exc
    return entry


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
    """按 ``ATRIX_DEPLOY_FAKE_CAPTURE`` 里预置的规则返回伪造结果（严格模式）。

    ``ATRIX_DEPLOY_FAKE_CAPTURE`` **已设置即视为开启伪造模式**，此时本函数
    fail-closed：

    * 规则文件不存在 → ``AssertionError``；
    * JSON 损坏 / 顶层不是数组 / 条目字段非法（未知键、没有匹配谓词、
      ``argv``/``argv_prefix`` 不是非空字符串列表）→ ``AssertionError``；
    * 没有任何规则命中 → ``AssertionError``（消息里带全 argv 与规则文件路径）。

    也就是说：**未命中绝不回落到真实执行**。两种显式例外，都要在规则文件/环境里
    写明白：

    * 某条规则写 ``"real": true``：命中它的 argv **允许**真正执行（例如夹具里
      必须真跑的本地替身脚本 ``backup.py``）——此时不得再写 code/stdout/stderr；
    * 环境变量 ``ATRIX_DEPLOY_FAKE_ALLOW_REAL=1``：**全局**允许未命中时回落，
      默认关闭（写它等于承认"这次就是要在真机上跑"，仅限人工排查）。

    未设置 ``ATRIX_DEPLOY_FAKE_CAPTURE`` 时本函数是空操作，生产零开销。

    文件格式：JSON 数组，每条一个对象，**先匹配先赢**。匹配谓词（同时声明
    即为「与」关系）：

    * ``argv``：完整参数序列（含程序名）**完全相等**；
    * ``argv_prefix``：参数序列前缀相等；
    * ``match``：拼接后的命令串**子串**命中（兼容旧夹具，新夹具优先用结构化谓词）。

    其余键：``code`` / ``stdout`` / ``stderr`` / ``real`` / ``note``（``note`` 只当注释）。
    """
    import json

    target = os.environ.get(FAKE_CAPTURE_ENV)
    if not target:
        return None
    joined = " ".join(str(part) for part in argv)
    if not os.path.exists(target):
        raise _fake_problem(target, argv, "规则文件不存在")
    try:
        with open(target, encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _fake_problem(target, argv, f"规则文件无法读取或 JSON 损坏：{exc}") from exc
    if not isinstance(entries, list):
        raise _fake_problem(target, argv, f"规则文件顶层必须是 JSON 数组，实际是 {type(entries).__name__}")
    # 先把整份规则表校验完：后面条目里的拼写错误也要报出来，而不是"先命中先赢"地跳过
    for index, entry in enumerate(entries):
        _fake_validate_entry(target, argv, index, entry)
    for entry in entries:
        if _fake_entry_matches(entry, argv, joined):
            if entry.get("real"):
                return None  # 规则显式声明：这条命令允许真跑（如夹具里的本地替身脚本）
            return subprocess.CompletedProcess(
                list(argv),
                int(entry.get("code", 0)),
                entry.get("stdout", ""),
                entry.get("stderr", ""),
            )
    if _fake_allow_real():
        return None
    raise _fake_problem(
        target,
        argv,
        f"{len(entries)} 条规则全部未命中（先匹配先赢；检查规则顺序与谓词）",
    )


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

    例外：伪造模式（``ATRIX_DEPLOY_FAKE_CAPTURE`` 已设置）下规则缺失/损坏/未命中
    会抛 ``AssertionError``——见 :func:`_fake_capture`，那是夹具错误，不该被吞掉。
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
    伪造模式下与 :func:`run_capture` 同一套严格规则（未命中即 ``AssertionError``）。
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
