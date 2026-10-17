"""Go skill 源码的规范化、校验与编译。

对应 Go：``internal/tool/skill/go_source.go``。

**偏差（必须知道）**：Go 用 ``go/format`` 与 ``go/parser`` 在进程内做格式化与
语法校验，Python 标准库没有 Go 语法树。这里的对应做法是：

* 格式化 → 调 ``gofmt``（Go 工具链自带；skill 编译本来就必须有 Go），把它当
  权威解析器，语法错误原样回给 LLM；
* ``package main`` 校验 → 先用 gofmt 保证语法合法，再做一次「首个非注释 token
  是 ``package main``」的轻量检查（对应 Go ``parser.ParseFile`` 那两行判断的
  实际语义）。

两者都失败时给出与 Go 同形的错误文案，``finalize_atrix_skill`` 的逐步报告因此
保持可用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from atri_x.processenv import Command, Environment
from atri_x.safecontext import with_timeout
from atri_x.tool.skill.process import IS_WINDOWS, decode_output, run_command, truncate_output

__all__ = [
    "GO_BINARY_ENV",
    "DEFAULT_BUILD_TIMEOUT",
    "GoBuildResult",
    "build_go_skill",
    "executable_name",
    "existing_executable",
    "format_go_source",
    "go_skill_config_dir",
    "normalize_go_source",
    "resolve_go_executable",
    "run_go_build",
    "strip_go_comments",
    "validate_go_executable",
    "validate_go_main_source",
]

#: 指定 Go 可执行文件的环境变量名。对应 Go ``goBinaryEnv``。
GO_BINARY_ENV = "ATRIX_GO_BINARY"

#: 编译默认超时（秒）。对应 Go ``runGoBuild`` 里的 ``60 * time.Second``。
DEFAULT_BUILD_TIMEOUT = 60.0


def normalize_go_source(text: str) -> str:
    """CRLF → LF 并保证文末恰好一个换行。对应 Go ``normalizeGoSource``。"""
    text = (text or "").replace("\r\n", "\n")
    return text.rstrip("\n") + "\n"


async def format_go_source(
    source: str, environment: Environment | None = None, ctx: Any = None
) -> str:
    """用 ``gofmt`` 格式化源码。对应 Go ``formatGoSource``。

    失败抛 ``ValueError("gofmt source: ...")``（Go 是
    ``fmt.Errorf("gofmt source: %w", err)``）。
    """
    normalized = normalize_go_source(source)
    gofmt = _resolve_gofmt(environment)
    command = _command(environment, gofmt)
    output = await run_command(ctx, command, stdin_data=normalized.encode("utf-8"))
    if output.ctx_error is not None:
        raise ValueError(f"gofmt source: {output.ctx_error}")
    if output.code != 0:
        detail = output.stderr_text().strip() or output.stdout_text().strip()
        raise ValueError(f"gofmt source: {detail or 'gofmt failed'}")
    return output.stdout_text()


def validate_go_main_source(source: str) -> None:
    """校验源码声明了 ``package main``。对应 Go ``validateGoMainSource``。

    先剥掉注释（保留字符串字面量，避免把正文里的 ``package`` 当声明），再要求
    首个 token 是 ``package`` 且紧随 ``main``。语法本身由 gofmt/build 兜底。
    """
    tokens = strip_go_comments(source or "").split()
    if len(tokens) < 2 or tokens[0] != "package":
        raise ValueError("parse Go source: expected 'package' clause")
    if tokens[1] != "main":
        raise ValueError("go source must declare package main")


def strip_go_comments(source: str) -> str:
    """去掉 Go 行注释与块注释，保留字符串/字符字面量。"""
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                source[index] == "*" and source[index + 1] == "/"
            ):
                index += 1
            index = min(index + 2, length)
            out.append(" ")
            continue
        if char in ('"', "'", "`"):
            quote = char
            out.append(char)
            index += 1
            while index < length:
                current = source[index]
                out.append(current)
                index += 1
                if quote != "`" and current == "\\" and index < length:
                    out.append(source[index])
                    index += 1
                    continue
                if current == quote:
                    break
            continue
        out.append(char)
        index += 1
    return "".join(out)


def resolve_go_executable(
    skill_root: str, environment: Environment | None = None
) -> str:
    """定位 ``go`` 可执行文件。对应 Go ``resolveGoExecutable``。"""
    del skill_root
    env = environment if environment is not None else Environment()
    value, ok = env.lookup(GO_BINARY_ENV)
    if ok and (value or "").strip() != "":
        return validate_go_executable(value, GO_BINARY_ENV)
    goroot, has_goroot = env.lookup("GOROOT")
    if has_goroot and (goroot or "").strip() != "":
        candidate = os.path.join(goroot, "bin", executable_name("go"))
        found = existing_executable(candidate)
        if found is not None:
            return found
        raise ValueError(
            f"GOROOT is set but go executable is unavailable at {candidate!r}; "
            f"set {GO_BINARY_ENV}=/path/to/go in system environment or config .env"
        )
    try:
        return env.look_path("go")
    except FileNotFoundError:
        raise ValueError(
            "go executable not found in ATRI-X service PATH; set "
            f"{GO_BINARY_ENV}=/path/to/go in system environment or config .env, "
            "or configure service PATH/GOROOT, then restart ATRI-X"
        ) from None


def validate_go_executable(path: str, source: str) -> str:
    """校验一个 ``go`` 路径可用。对应 Go ``validateGoExecutable``。"""
    path = (path or "").strip()
    if path == "":
        raise ValueError(f"{source} is empty")
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ValueError(
            f"{source} points to unavailable go executable {path!r}: {exc}"
        ) from exc
    import stat as stat_module

    if stat_module.S_ISDIR(info.st_mode):
        raise ValueError(f"{source} points to a directory, not go executable: {path!r}")
    return path


def go_skill_config_dir(skill_root: str) -> str:
    """从 Go skill 目录反推配置目录。对应 Go ``goSkillConfigDir``。"""
    root = os.path.normpath((skill_root or "").strip())
    if os.path.basename(root) == "skills":
        return os.path.dirname(root)
    parent = os.path.dirname(root)
    if (
        os.path.basename(parent) == "go"
        and os.path.basename(os.path.dirname(parent)) == "skills"
    ):
        return os.path.dirname(os.path.dirname(parent))
    return os.path.dirname(root)


def existing_executable(path: str) -> str | None:
    """存在的可执行文件路径，否则 ``None``。对应 Go ``existingExecutable``。"""
    path = (path or "").strip()
    if path == "":
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    import stat as stat_module

    if stat_module.S_ISDIR(info.st_mode):
        return None
    return path


def executable_name(name: str) -> str:
    """平台可执行文件名。对应 Go ``executableName``。"""
    return name + ".exe" if IS_WINDOWS else name


@dataclass
class GoBuildResult:
    """一次 ``go build`` 的结果。对应 Go ``goBuildResult``。"""

    stdout: str = ""
    stderr: str = ""
    err: BaseException | None = None


async def build_go_skill(
    ctx: Any,
    root: str,
    name: str,
    timeout_ms: int,
    environment: Environment | None = None,
) -> None:
    """编译 Go skill，失败抛异常。对应 Go ``buildGoSkill``。"""
    result = await run_go_build(ctx, root, name, timeout_ms, environment)
    if result.err is not None:
        raise RuntimeError(
            f"go build failed: {result.err}\nstdout:\n{truncate_output(result.stdout)}"
            f"\nstderr:\n{truncate_output(result.stderr)}"
        )


async def run_go_build(
    ctx: Any,
    root: str,
    name: str,
    timeout_ms: int,
    environment: Environment | None = None,
) -> GoBuildResult:
    """执行 ``go build -o <name> .``。对应 Go ``runGoBuild``。

    只有「定位 go 可执行文件」失败才抛异常；编译失败放在
    :attr:`GoBuildResult.err` 里（与 Go 一致）。
    """
    go_path = resolve_go_executable(root, environment)
    binary = name + ".exe" if IS_WINDOWS else name
    timeout = DEFAULT_BUILD_TIMEOUT
    if timeout_ms and timeout_ms > 0:
        timeout = timeout_ms / 1000.0
    run_ctx = with_timeout(ctx, timeout)
    try:
        command = _command(environment, go_path, "build", "-o", binary, ".")
        output = await run_command(run_ctx, command, cwd=root)
    finally:
        run_ctx.cancel("go build finished")
    err: BaseException | None = output.ctx_error
    if err is None and output.code != 0:
        err = RuntimeError(f"exit status {output.code}")
    return GoBuildResult(
        stdout=decode_output(output.stdout),
        stderr=decode_output(output.stderr),
        err=err,
    )


def _command(environment: Environment | None, name: str, *args: str) -> Command:
    """构造一个子进程命令（沿用 ProcessEnv 的快照语义）。"""
    env = environment if environment is not None else Environment()
    return env.command(name, *args)


def _resolve_gofmt(environment: Environment | None) -> str:
    """找 ``gofmt``：先看 go 可执行文件同目录，再退回 PATH。"""
    try:
        go_path = resolve_go_executable("", environment)
    except ValueError:
        go_path = ""
    if go_path:
        candidate = os.path.join(os.path.dirname(go_path), executable_name("gofmt"))
        if existing_executable(candidate) is not None:
            return candidate
    env = environment if environment is not None else Environment()
    try:
        return env.look_path("gofmt")
    except FileNotFoundError:
        raise ValueError(
            "gofmt source: gofmt executable not found; install the Go toolchain "
            f"or set {GO_BINARY_ENV}"
        ) from None
