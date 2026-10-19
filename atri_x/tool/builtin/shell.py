"""``shell`` 工具：带风险分级、目录约束与后台沙盒的执行器。

对应 Go：``internal/tool/builtin/shell.go``。

与 Go 的差异（仅形式）：

* ``exec.CommandContext`` + goroutine → :func:`asyncio.create_subprocess_exec` +
  :func:`asyncio.wait_for`；超时后杀整棵进程树（Windows ``taskkill /T /F``，
  Unix 杀进程组）。
* Go 的 ``shellOutputBuffer`` 在写入时截断但仍排空管道；Python 用
  :func:`_read_capped` 边读边丢，语义相同。
* ``tool.MediaRuntime``（Go 的 ``internal/tool/media_runtime.go``）已落地为
  :class:`atri_x.tool.media_runtime.MediaRuntime`，``media`` 字段现在按真实类型
  接线；同时保留鸭子兼容（任何提供 ``new_call()`` 的对象都可以），因为
  ``MediaCall`` 的公开面只有 ``cached_export`` / ``close``。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import ForbiddenError, InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.processenv import Environment
from atri_x.security import compare_risk
from atri_x.shellparse import ShellParseError, SimpleCommand, iter_nodes, parse
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import first_file_guard
from atri_x.tool.builtin.file_guard import FileGuard
from atri_x.tool.builtin.shell_env import (
    configure_shell_process,
    current_shell_desc,
    is_power_shell_env,
    kill_shell_process_tree,
    powershell_utf8_command,
    resolve_unix_shell,
    resolve_windows_shell,
)
from atri_x.tool.builtin.shell_risk import classify_shell_command
from atri_x.tool.builtin.shell_sandbox import (
    BackgroundShellPolicy,
    apply_background_shell_policy,
    apply_shell_sandbox_risk,
    is_unattended_background,
)
from atri_x.tool.builtin.shell_syntax import command_base, literal_word
from atri_x.tool.builtin.shell_warnings import analyze_shell_advice
from atri_x.tool.media_runtime import MediaCall, MediaInput, MediaRuntime
from atri_x.tool.sandbox import sandbox_context_from_context
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskAssessment,
    RiskCritical,
    RiskHigh,
)
from atri_x.tool.workspace import current_workspace_dir

DEFAULT_SHELL_TIMEOUT = 10.0
MAX_SHELL_OUTPUT = 256 * 1024
SHELL_CMD_REQUIRED = 'cmd is required; use {"cmd":"..."}'
_READ_CHUNK = 65536


@dataclass
class ShellArgs:
    """对应 Go ``shellArgs``。"""

    media_inputs: list[dict[str, Any]] = field(default_factory=list)
    cmd: str = ""
    timeout_ms: int = 0


@dataclass
class ShellData:
    """对应 Go ``shellData``。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class ShellTool:
    """对应 Go ``ShellTool``。"""

    def __init__(
        self,
        environment: Environment | None = None,
        file_guard: FileGuard | None = None,
    ) -> None:
        self.media: MediaRuntime | None = None
        self.file_guard = file_guard
        self.process_env = environment or Environment()
        #: 后台（cron / Atrinis）无人值守时的放行策略；由装配层按 ``[security]`` 覆盖。
        self.background_shell_policy = BackgroundShellPolicy()

    def name(self) -> str:
        return "shell"

    def info(self) -> Info:
        return shell_builder().build_info()

    def schema(self) -> ToolSchema:
        """对应 Go ``ShellTool.Schema``：给 ``media_inputs`` 补上 ``additionalProperties``。"""
        schema = shell_builder().build_schema()
        parameters = schema.function.parameters
        media_inputs = parameters.get("properties", {}).get("media_inputs")
        if isinstance(media_inputs, dict):
            items = media_inputs.get("items")
            if isinstance(items, dict):
                items["additionalProperties"] = False
        return schema

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """对应 Go ``ShellTool.AssessRisk``。"""
        _, cmd_text = decode_shell_args(req)
        assessment = classify_shell_command(cmd_text)
        sandbox, ok = sandbox_context_from_context(ctx)
        if ok and sandbox.background:
            assessment = apply_shell_sandbox_risk(cmd_text, assessment)
            if is_unattended_background(sandbox):
                # 无人值守：默认拒绝 + 只读白名单（研究报告 §6 的 P0）。
                assessment = apply_background_shell_policy(
                    cmd_text, assessment, self.background_shell_policy
                )
        if assessment.level == "":
            assessment.level = self.info().risk
        return assessment

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """对应 Go ``ShellTool.PreflightConfirmation``。"""
        _, cmd_text = decode_shell_args(req)
        reject_shell_directory_change(cmd_text)
        work_dir = await resolve_shell_work_dir(ctx)
        advice = analyze_shell_advice(cmd_text, work_dir, self.file_guard)
        if advice.block_err is not None:
            raise advice.block_err

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``ShellTool.Call``。

        与 Go 的差异（研究报告 §6 的 P0）：真正启动进程**之前**用执行时的 ctx 再做
        一次后台策略复核（``background_execution_denial``）。评估阶段可能没有沙盒
        ctx、也可能被确认阶段绕过（"本会话自动确认"），所以"排队之后、执行之前"必须
        自己重新裁决；拒绝时抛 :class:`ForbiddenError`，不启动进程。
        """
        args, cmd_text = decode_shell_args(req)
        denial = background_execution_denial(ctx, cmd_text, self.background_shell_policy)
        if denial != "":
            raise ForbiddenError(f"background shell denied: {denial}")
        timeout = (
            args.timeout_ms / 1000 if args.timeout_ms > 0 else DEFAULT_SHELL_TIMEOUT
        )
        reject_shell_directory_change(cmd_text)
        work_dir = await resolve_shell_work_dir(ctx)
        advice = analyze_shell_advice(cmd_text, work_dir, self.file_guard)
        if advice.block_err is not None:
            raise advice.block_err

        environment = self.process_env
        media_call: MediaCall | None = None
        try:
            if args.media_inputs:
                if self.media is None:
                    raise InvalidError(
                        "shell media inputs are unavailable in this context"
                    )
                # 与 Go 一致：``defer mediaCall.Close()`` 覆盖整次调用，而不是
                # 只覆盖变量准备阶段——引用必须活到命令跑完。
                media_call = self.media.new_call()
                variables = await _media_variables(media_call, args.media_inputs)
                environment = environment.overlay(variables)
            data = await run_shell_command(environment, cmd_text, work_dir, timeout)
        finally:
            await _close_media_call(media_call)
        return Result(content=format_shell_content(data), warnings=list(advice.warnings))


def shell_builder() -> Builder:
    """对应 Go ``shellBuilder``。"""
    return (
        Builder("shell")
        .description(f"执行 shell 命令。当前使用 {current_shell_desc()}，请使用相应语法。")
        .risk(RiskHigh)
        .depends_on("workspace")
        .tags("agent")
        .string("cmd", "要执行的 shell 命令。", required())
        .integer("timeout_ms", "可选，命令超时时间，默认 10000。")
        .object_array(
            "media_inputs",
            "可选，显式媒体输入；宿主导出并注入 ATRIX_MEDIA_1 等环境变量，"
            "命令需按当前 shell 语法引用变量。",
            {"media": {"type": "string", "pattern": "^media:[0-9a-f]{64}$"}},
            ["media"],
        )
    )


def new_shell_tool(*file_guard: FileGuard) -> ShellTool:
    """对应 Go ``NewShellTool``。"""
    return ShellTool(Environment(), first_file_guard(list(file_guard)))


def new_shell_tool_with_environment(
    environment: Environment, *file_guard: FileGuard
) -> ShellTool:
    """对应 Go ``NewShellToolWithEnvironment``。"""
    return ShellTool(environment, first_file_guard(list(file_guard)))


def decode_shell_args(req: CallRequest) -> tuple[ShellArgs, str]:
    """对应 Go ``decodeShellArgs``。"""
    args = ShellArgs()
    if req.arguments:
        try:
            data = json.loads(req.arguments)
        except ValueError as exc:
            raise InvalidError(f"parse shell arguments: {exc}") from exc
        if not isinstance(data, dict):
            raise InvalidError("parse shell arguments: expected a JSON object")
        raw_inputs = data.get("media_inputs") or []
        if not isinstance(raw_inputs, list):
            raise InvalidError("parse shell arguments: media_inputs must be an array")
        args.media_inputs = [item for item in raw_inputs if isinstance(item, dict)]
        args.cmd = _as_str(data.get("cmd"))
        raw_timeout = data.get("timeout_ms")
        try:
            args.timeout_ms = 0 if raw_timeout is None else int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise InvalidError("timeout_ms must be an integer") from exc
    cmd_text = args.cmd.strip()
    if cmd_text == "":
        raise InvalidError(SHELL_CMD_REQUIRED)
    return args, cmd_text


async def resolve_shell_work_dir(ctx: Any) -> str:
    """对应 Go ``resolveShellWorkDir``。"""
    sandbox, ok = sandbox_context_from_context(ctx)
    if ok and (sandbox.dir or "").strip() != "":
        try:
            os.makedirs(sandbox.dir, exist_ok=True)
        except OSError as exc:
            raise InvalidError(f"create shell sandbox: {exc}") from exc
        return os.path.normpath(sandbox.dir)
    return await current_workspace_dir(ctx)


def background_execution_denial(
    ctx: Any, cmd_text: str, policy: BackgroundShellPolicy
) -> str:
    """执行前的后台策略复核：返回拒绝原因，空串表示放行（非无人值守后台也放行）。

    这里**不依赖工具 schema、也不依赖评估阶段的结果**：重新做一遍
    ``classify + 沙盒加固 + 后台策略``，把"评估时看到的是 low、排队期间换成了别的
    命令"或"确认阶段被前台自动确认放行"这两类绕过都堵在执行点上。
    """
    sandbox, ok = sandbox_context_from_context(ctx)
    if not ok or not is_unattended_background(sandbox):
        return ""
    assessment = classify_shell_command(cmd_text)
    assessment = apply_shell_sandbox_risk(cmd_text, assessment)
    assessment = apply_background_shell_policy(cmd_text, assessment, policy)
    if compare_risk(RiskCritical, assessment.level) > 0:
        return ""
    reasons = [reason for reason in assessment.reasons if reason]
    return reasons[-1] if reasons else "后台无人值守策略拒绝执行"


def reject_shell_directory_change(cmd_text: str) -> None:
    """对应 Go ``rejectShellDirectoryChange``。"""
    if is_power_shell_env():
        return
    reject_bash_shell_directory_change(cmd_text)


def reject_bash_shell_directory_change(cmd_text: str) -> None:
    """对应 Go ``rejectBashShellDirectoryChange``。"""
    try:
        script = parse(cmd_text)
    except ShellParseError:
        return
    blocked = ""
    for node in iter_nodes(script):
        if not isinstance(node, SimpleCommand) or not node.words:
            continue
        name, ok = literal_word(node.words[0])
        if not ok:
            continue
        base = command_base(name)
        if base in ("cd", "chdir", "pushd", "popd", "set-location", "sl"):
            blocked = base
            break
    if blocked != "":
        raise InvalidError(
            f"shell command {blocked!r} is not allowed; "
            "use the workspace tool to set the working directory"
        )


async def run_shell_command(
    environment: Environment, cmd_text: str, work_dir: str, timeout: float
) -> ShellData:
    """对应 Go ``shellCommand`` + ``runShellCommand``。"""
    if os.name == "nt":
        shell_name, shell_args = resolve_windows_shell()
        argv = [shell_name, *shell_args, powershell_utf8_command(shell_name, cmd_text)]
    else:
        shell_name, shell_args = resolve_unix_shell()
        argv = [shell_name, *shell_args, cmd_text]

    command = environment.command(*argv)
    kwargs: dict[str, Any] = {
        "cwd": work_dir,
        "env": command.env_dict(),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    configure_shell_process(kwargs)
    try:
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except OSError as exc:
        raise InvalidError(f"run shell: {exc}") from exc
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(
            asyncio.gather(
                _read_capped(process.stdout), _read_capped(process.stderr)
            ),
            timeout=timeout,
        )
        exit_code = await process.wait()
    except asyncio.TimeoutError as exc:
        kill_shell_process_tree(process)
        raise InvalidError(f"run shell: command timed out after {timeout:g}s") from exc
    except asyncio.CancelledError:
        kill_shell_process_tree(process)
        raise
    return ShellData(
        stdout=_decode_output(stdout_raw),
        stderr=_decode_output(stderr_raw),
        exit_code=exit_code,
    )


async def _read_capped(stream: Any) -> tuple[bytes, bool]:
    """有界读取：保留前 ``MAX_SHELL_OUTPUT`` 字节，其余排空丢弃。"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        if total < MAX_SHELL_OUTPUT:
            keep = min(len(chunk), MAX_SHELL_OUTPUT - total)
            chunks.append(chunk[:keep])
            total += keep
            if keep < len(chunk):
                truncated = True
        else:
            truncated = True
    return b"".join(chunks), truncated


def _decode_output(raw: tuple[bytes, bool]) -> str:
    """对应 Go ``shellOutputBuffer.String``（含截断提示）。"""
    data, truncated = raw
    text = data.decode("utf-8", "replace")
    if truncated:
        text += (
            f"\n... output too long; truncated to first {MAX_SHELL_OUTPUT // 1024} KiB ...\n"
        )
    return text


def format_shell_content(data: ShellData) -> str:
    """对应 Go ``formatShellContent``。"""
    parts: list[str] = []
    if data.stdout != "":
        parts.append(data.stdout)
    if data.stderr != "":
        parts.append("stderr:\n" + data.stderr)
    if data.exit_code != 0:
        parts.append(f"exit_code: {data.exit_code}")
    return "\n".join(parts)


async def _media_variables(
    media_call: Any, inputs: list[dict[str, Any]]
) -> dict[str, str]:
    """对应 Go ``ShellTool.Call`` 里的 ``mediaCall.CachedExport`` 循环。

    Go 的 ``[]tool.MediaInput`` 反序列化时用 ``DisallowUnknownFields`` 并校验
    media ID；这里显式走 :meth:`MediaInput.from_dict` 复刻同一套校验。
    """
    variables: dict[str, str] = {}
    for position, item in enumerate(inputs):
        media_id = MediaInput.from_dict(item).media_id
        try:
            path = await _cached_export(media_call, media_id)
        except asyncio.CancelledError:
            # 取消必须传播：转成 InvalidError 会让上游以为"只是媒体准备失败"，
            # 把已经取消的调用继续当成普通错误处理。
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成可回喂的错误
            raise InvalidError(f"prepare shell media: {exc}") from exc
        variables[f"ATRIX_MEDIA_{position + 1}"] = path
    return variables


async def _cached_export(media_call: Any, media_id: str) -> str:
    """调用 ``cached_export``，兼容 ``(ctx, id)`` 与 ``(id)`` 两种鸭子签名。"""
    fn = getattr(media_call, "cached_export", None)
    if not callable(fn):
        raise InvalidError("media runtime does not support cached exports")
    if _takes_context(fn):
        return await fn(None, media_id)
    return await fn(media_id)


def _takes_context(fn: Any) -> bool:
    """该绑定方法除 ``id`` 外是否还收一个 ctx 参数。"""
    import inspect

    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - 内建/无法内省
        return True
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 2


async def _close_media_call(media_call: Any) -> None:
    """对应 Go ``defer mediaCall.Close()``（含 ``errors.Join`` 不覆盖主错误）。"""
    if media_call is None:
        return
    closer = getattr(media_call, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - Go 用 errors.Join，这里不覆盖主错误
        # 只吞普通异常：CancelledError 属于取消传播，不能被"关闭失败"顺手抹掉。
        pass


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


__all__ = [name for name in dir() if not name.startswith("_")]
