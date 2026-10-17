"""Go skill 运行器：把 payload 写进子进程 stdin 并解析 stdout。

对应 Go：``internal/tool/skill/runner.go``（``process_*.go`` 的进程控制见
:mod:`atri_x.tool.skill.process`）。

``go_skill_run`` 是一个 hidden 内建工具：它的风险等级由**被选中的 skill**的
记录决定，所以在 ``assess_risk`` 里现查目录——目录里没有或不是 Go skill 时
一律按 ``high`` 处理，绝不放行未知二进制。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from atri_x.llm.llm import ToolSchema
from atri_x.processenv import Command, Environment
from atri_x.safecontext import DeadlineExceededError, with_timeout
from atri_x.tool.builder import new_builder, required
from atri_x.tool.skill.catalog import Catalog, Kind
from atri_x.tool.skill.media import (
    prepare_go_media,
    result_from_stdout,
    result_from_stdout_with_media,
    truncate_output,
)
from atri_x.tool.skill.process import run_command
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskAssessment,
    RiskHigh,
    RiskLow,
    SourceBuiltin,
)

__all__ = [
    "DEFAULT_RUNNER_TIMEOUT",
    "GoRunner",
    "GoRunnerArgs",
    "GoRunnerName",
    "command_error",
    "command_output_suffix",
    "new_go_runner",
    "parse_go_runner_args",
    "result_from_stdout",
    "run_media_command",
    "runner_timeout",
    "safe_relative_path",
    "truncate_output",
]

#: 运行 Go skill 的默认超时（秒）。对应 Go ``defaultRunnerTimeout``。
DEFAULT_RUNNER_TIMEOUT = 30.0

#: ``go_skill_run`` 的工具名。对应 Go ``GoRunnerName``。
GoRunnerName = "go_skill_run"


@dataclass
class GoRunnerArgs:
    """``go_skill_run`` 的参数。对应 Go ``goRunnerArgs``。

    ``payload`` 保持 JSON 文本（Go 是 ``json.RawMessage``），因为它会被原样
    写进子进程 stdin。
    """

    skill_name: str = ""
    timeout_ms: int = 0
    payload: str = ""


class GoRunner:
    """运行 Go skill。对应 Go ``GoRunner``。"""

    def __init__(
        self,
        catalog: Catalog | None = None,
        environment: Environment | None = None,
        media: Any = None,
    ) -> None:
        self.media = media
        self.catalog = catalog
        self.process_env = environment

    # -- Tool 协议 ----------------------------------------------------------

    def name(self) -> str:
        return GoRunnerName

    def info(self) -> Info:
        """对应 Go ``GoRunner.Info``（内建、隐藏、低风险）。"""
        return (
            new_builder(GoRunnerName)
            .description("运行指定 Go skill，并把 arguments JSON 写入 stdin。")
            .source(SourceBuiltin)
            .risk(RiskLow)
            .hidden()
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``GoRunner.Schema``。"""
        return (
            new_builder(GoRunnerName)
            .description(
                "运行指定 Go skill；skill_name 选择 skill，payload 放业务参数，"
                "timeout_ms 设置超时。可在 payload.media_inputs 显式传 "
                '[{"media":"media:<sha256>"}]；宿主补充相对路径、元数据、小文件 '
                "base64 和 media_workspace。stdout 可返回 content、segments"
                "（text/image/file，媒体使用 media 或 Skill 目录内相对 path）。"
            )
            .string("skill_name", "Go skill 名称。", required())
            .object("payload", "业务参数 JSON 对象；除显式媒体输入外原样写入 stdin。", required())
            .integer("timeout_ms", "可选，超时时间，默认 30000。")
            .build_schema()
        )

    async def assess_risk(self, ctx: Any, req: CallRequest) -> RiskAssessment:
        """按记录风险定级。对应 Go ``GoRunner.AssessRisk``。"""
        del ctx
        args = GoRunnerArgs()
        if req.arguments:
            args = parse_go_runner_args(req.arguments)
        if self.catalog is None:
            return RiskAssessment(level=RiskHigh)
        record, ok = self.catalog.get((args.skill_name or "").strip())
        if not ok or record is None or record.kind != Kind.GO:
            return RiskAssessment(level=RiskHigh)
        return RiskAssessment(level=record.risk)

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """执行一次 Go skill。对应 Go ``GoRunner.Call``。"""
        args = GoRunnerArgs()
        if req.arguments:
            args = parse_go_runner_args(req.arguments)
        if self.catalog is None:
            raise ValueError("skill catalog is not configured")
        record, ok = self.catalog.get((args.skill_name or "").strip())
        if not ok or record is None or record.kind != Kind.GO or record.binary_path == "":
            raise ValueError(f"go skill {args.skill_name!r} not found")
        timeout = runner_timeout(args.timeout_ms)
        run_ctx = with_timeout(ctx, timeout)
        try:
            if self.media is None:
                raise ValueError("skill media runtime is not configured")
            media_call = self.media.new_call()
            try:
                payload = prepare_go_media(run_ctx, args.payload, media_call, record.root)
                command = self._command(run_ctx, record.binary_path)
                return await run_media_command(
                    run_ctx,
                    "go skill",
                    command,
                    media_call,
                    cwd=record.root,
                    stdin_data=payload.encode("utf-8"),
                )
            finally:
                close = getattr(media_call, "close", None)
                if close is not None:
                    await _maybe_await(close())
        finally:
            run_ctx.cancel("go skill finished")

    def _command(self, ctx: Any, binary: str) -> Command:
        """构造子进程命令（对应 ``ProcessEnv.CommandContext``）。"""
        environment = self.process_env
        if environment is None:
            environment = Environment()
        return environment.command_context(ctx, binary)


def new_go_runner(
    catalog: Catalog | None = None, environment: Environment | None = None
) -> GoRunner:
    """对应 Go ``NewGoRunner``（``media`` 由 :class:`atri_x.tool.skill.scanner.FilesystemScanner`
    在装配时注入，与 Go 的 ``scanner.toolForRecord`` 一致）。"""
    return GoRunner(catalog=catalog, environment=environment)


async def _maybe_await(value: Any) -> Any:
    """``MediaCall.Close`` 可能是同步或异步实现，两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def parse_go_runner_args(data: str | bytes) -> GoRunnerArgs:
    """解析 ``go_skill_run`` 参数。对应 Go ``parseGoRunnerArgs``。

    ``payload`` 必填且必须是 JSON 对象；``skill_name`` / ``timeout_ms`` 类型
    不符时抛 ``ValueError``（Go 返回 unmarshal 错误）。
    """
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError("go_skill_run arguments must be a JSON object")
    args = GoRunnerArgs()
    if "skill_name" in raw:
        value = raw["skill_name"]
        if not isinstance(value, str):
            raise ValueError("skill_name: must be a string")
        args.skill_name = value
    if "timeout_ms" in raw:
        value = raw["timeout_ms"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("timeout_ms: must be a number")
        args.timeout_ms = value
    if "payload" not in raw:
        raise ValueError("payload is required")
    payload = raw["payload"]
    if not isinstance(payload, dict):
        raise ValueError("payload must be object")
    args.payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return args


def runner_timeout(timeout_ms: int) -> float:
    """超时（秒）。对应 Go ``runnerTimeout``。"""
    if timeout_ms and timeout_ms > 0:
        return timeout_ms / 1000.0
    return DEFAULT_RUNNER_TIMEOUT


def safe_relative_path(root: str, rel: str) -> str:
    """校验 ``rel`` 落在 ``root`` 内并返回规范化路径。对应 Go ``safeRelativePath``。

    返回的是**相对**路径（Go 同样返回 ``clean``），越界、绝对路径、``..``
    开头都抛 ``ValueError``。
    """
    rel = (rel or "").strip()
    if rel == "":
        raise ValueError("script is required")
    if os.path.isabs(rel):
        raise ValueError("script must be relative to skill directory")
    clean = os.path.normpath(rel)
    if clean == "." or clean.startswith(".."):
        raise ValueError("script must stay inside skill directory")
    full = os.path.join(root, clean)
    back = os.path.relpath(full, root)
    if back.startswith("..") or os.path.isabs(back):
        raise ValueError("script must stay inside skill directory")
    return clean


async def run_media_command(
    ctx: Any,
    label: str,
    command: Command,
    media_call: Any,
    *,
    cwd: str = "",
    stdin_data: bytes = b"",
) -> Result:
    """跑一个 skill 子进程并解析 stdout。对应 Go ``runMediaCommand``。"""
    try:
        output = await run_command(ctx, command, cwd=cwd, stdin_data=stdin_data)
    except OSError as exc:
        raise ValueError(f"start {label} failed: {exc}") from exc
    stdout_text = output.stdout_text()
    stderr_text = output.stderr_text()
    out = truncate_output(stdout_text)
    err_text = truncate_output(stderr_text)
    if output.ctx_error is not None:
        raise command_error(ctx, label, output.ctx_error, out, err_text)
    if output.code != 0:
        raise command_error(
            ctx,
            label,
            subprocess.CalledProcessError(output.code, command.args),
            out,
            err_text,
        )
    return result_from_stdout_with_media(ctx, stdout_text, media_call, cwd)


def command_error(
    ctx: Any, label: str, err: BaseException, stdout: str, stderr: str
) -> BaseException:
    """把子进程失败包成可读错误。对应 Go ``commandError``。

    优先看上下文：超时与取消各有专门文案；否则区分「非零退出」与「启动失败」。
    """
    current = _ctx_error(ctx)
    if current is not None and isinstance(current, (TimeoutError, DeadlineExceededError)):
        return TimeoutError(
            f"{label} timed out: {current}" + command_output_suffix(stdout, stderr)
        )
    if current is not None:
        return RuntimeError(
            f"{label} canceled: {current}" + command_output_suffix(stdout, stderr)
        )
    if isinstance(err, subprocess.CalledProcessError):
        return RuntimeError(
            f"{label} process failed with exit code {err.returncode}: {err}"
            + command_output_suffix(stdout, stderr)
        )
    return RuntimeError(f"start {label} failed: {err}")


def command_output_suffix(stdout: str, stderr: str) -> str:
    """错误尾部附带的 stdout/stderr。对应 Go ``commandOutputSuffix``。"""
    parts: list[str] = []
    if (stdout or "").strip() != "":
        parts.append("stdout:\n" + stdout)
    if (stderr or "").strip() != "":
        parts.append("stderr:\n" + stderr)
    if not parts:
        return ""
    return "\n" + "\n".join(parts)


def _ctx_error(ctx: Any) -> BaseException | None:
    if ctx is None:
        return None
    error = ctx.error()
    if error is not None:
        return error
    if ctx.is_cancelled():
        from atri_x.safecontext import CancelledError

        return CancelledError("context canceled")
    return None
