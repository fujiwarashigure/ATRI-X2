"""``create_atrix_skill``：创建 ATRI-X 原生 Atriyph skill。

对应 Go：``internal/tool/skill/creator.go``。

创建流程（逐条对齐 Go）：校验名字 → 校验描述/风险 → 有 Go 源码时校验并探测
``go`` 版本 → 校验 Atriyph → 补写首行描述与 ``** risk`` 行 → 检查工具名与目录
是否已存在 → 建目录、写 ``SKILL.atriyph``（可选 ``main.go`` + ``go.mod``）→
``go build`` → 重载 → 把 Atriyph warning 拼进结果。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from atri_x.atriyph import SKILL_FILE_NAME, validate_skill
from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.processenv import Environment
from atri_x.safecontext import with_timeout
from atri_x.tool.builder import new_builder, required
from atri_x.tool.skill.atriyph_warnings import append_atriyph_warnings
from atri_x.tool.skill.descriptor import atriyph_rule_card
from atri_x.tool.skill.go_source import DEFAULT_BUILD_TIMEOUT, executable_name
from atri_x.tool.skill.parser import parse_risk
from atri_x.tool.skill.process import decode_output, run_command, truncate_output
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, SourceBuiltin

__all__ = [
    "CREATE_ATRIX_SKILL_NAME",
    "CreateAtrixSkillArgs",
    "CreateAtrixSkillTool",
    "detect_go_mod_version",
    "go_mod_content",
    "is_windows_reserved_name",
    "new_create_atrix_skill_tool",
    "parse_go_mod_version",
    "skill_name_pattern",
    "validate_go_source",
    "validate_skill_name",
    "with_atriyph_metadata",
]

#: 工具名。对应 Go ``CreateAtrixSkillName``。
CREATE_ATRIX_SKILL_NAME = "create_atrix_skill"

#: 名字规则：小写字母开头，之后是字母/数字/下划线/短横线，总长 ≤ 64。
skill_name_pattern = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Windows 保留设备名（Go 侧同一张表）。
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{index}" for index in range(1, 10)]
    + [f"LPT{index}" for index in range(1, 10)]
)


@dataclass
class CreateAtrixSkillArgs:
    """``create_atrix_skill`` 的参数。对应 Go ``createAtrixSkillArgs``。"""

    name: str = ""
    description: str = ""
    risk: str = ""
    atriyph: str = ""
    go_source: str = ""
    timeout_ms: int = 0


class CreateAtrixSkillTool:
    """创建 Atriyph skill。对应 Go ``CreateAtrixSkillTool``。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    # -- Tool 协议 ----------------------------------------------------------

    def name(self) -> str:
        return CREATE_ATRIX_SKILL_NAME

    def info(self) -> Info:
        """对应 Go ``CreateAtrixSkillTool.Info``。

        ``DependsOn`` 里的三个名字定义在 modify/finalize 两个模块（与 Go 的落位
        一致），这里在调用期解析，避免同包循环 import。
        """
        from atri_x.tool.skill.finalize_atrix_skill import FINALIZE_ATRIX_SKILL_NAME
        from atri_x.tool.skill.modify_atrix_skill import (
            MODIFY_ATRIX_SKILL_NAME,
            READ_ATRIX_SKILL_NAME,
        )

        return (
            new_builder(CREATE_ATRIX_SKILL_NAME)
            .description(
                "创建 ATRI-X 原生 Atriyph skill。只有可复用经验、固定流程或稳定能力"
                "才应创建 skill；一次性任务不要创建。可只写 SKILL.atriyph，也可附带 "
                "Go 源码并通过 go_skill_run 调用。"
            )
            .source(SourceBuiltin)
            .risk(RiskHigh)
            .depends_on(
                READ_ATRIX_SKILL_NAME, MODIFY_ATRIX_SKILL_NAME, FINALIZE_ATRIX_SKILL_NAME
            )
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``CreateAtrixSkillTool.Schema``。"""
        return (
            new_builder(CREATE_ATRIX_SKILL_NAME)
            .description(
                "创建 ATRI-X 原生 Atriyph Skill，可附带 go_source 并编译。Go Skill 通过 "
                "go_skill_run 调用，宿主处理 payload 后将其作为 JSON 写入进程 stdin。"
                '调用方用 payload.media_inputs 传入 [{"media":"media:<sha256>"}]；宿主'
                "补充 path、name、mime_type、size、可选 base64 和 media_workspace。无"
                "媒体输入但需要输出目录时传空 media_inputs。stdout 可返回 content 和 "
                "segments；image/file 使用稳定 media ID 或 Skill 目录内的相对 path，"
                "宿主导入 path 后返回稳定 ID。调用结束自动清理临时目录和引用。"
                "SKILL.atriyph 必须写明 go_skill_run 的调用方式和业务 payload。"
            )
            .string(
                "name",
                "skill 名称，也是目录名和 binary 名；使用小写字母、数字、下划线或短横线。",
                required(),
            )
            .string("description", "skill 的可复用能力简述。", required())
            .string("risk", "风险等级：safe, low, medium, high, critical。", required())
            .string(
                "atriyph",
                "完整 SKILL.atriyph 内容。" + atriyph_rule_card(),
                required(),
            )
            .string(
                "go_source",
                "可选的 Go main 包源码。程序从 os.Stdin 读取 payload JSON，不使用 "
                "os.Args 或 ATRIX_MEDIA_N；媒体从 media_inputs.path 读取，输出写入 "
                "media_workspace，并通过 stdout segments 返回相对 path。不提供时创建"
                "纯 Atriyph 文本 Skill。",
            )
            .integer("timeout_ms", "可选，编译超时时间，默认 60000。")
            .build_schema()
        )

    # -- 调用 ---------------------------------------------------------------

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``CreateAtrixSkillTool.Call``。"""
        args = CreateAtrixSkillArgs()
        if (req.arguments or "").strip():
            try:
                raw = json.loads(req.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parse create_atrix_skill arguments: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("parse create_atrix_skill arguments: must be an object")
            args.name = str(raw.get("name") or "")
            args.description = str(raw.get("description") or "")
            args.risk = str(raw.get("risk") or "")
            args.atriyph = str(raw.get("atriyph") or "")
            args.go_source = str(raw.get("go_source") or "")
            timeout = raw.get("timeout_ms")
            args.timeout_ms = int(timeout) if isinstance(timeout, int) and not isinstance(timeout, bool) else 0
        if self.manager is None or self.manager.registry is None:
            raise ValueError("skill manager is not configured")
        name = (args.name or "").strip()
        validate_skill_name(name)
        args.description = (args.description or "").strip()
        if args.description == "":
            raise ValueError("description is required")
        parse_risk(args.risk)
        go_version = ""
        if (args.go_source or "").strip() != "":
            validate_go_source(args.go_source)
            go_version = await detect_go_mod_version(ctx, self.manager.process_env)
        _, diagnostics = validate_skill(args.atriyph, name)
        args.atriyph = with_atriyph_metadata(args.atriyph, args.description, args.risk)
        _, exists = self.manager.registry.get(name)
        if exists:
            raise ValueError(f"tool or skill {name!r} already exists")
        root = os.path.join(self.manager.root, "go", name)
        if os.path.exists(root):
            raise ValueError(f"skill directory {root!r} already exists")
        try:
            os.makedirs(root, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise OSError(f"create skill directory: {exc}") from exc
        try:
            _write_text(os.path.join(root, SKILL_FILE_NAME), args.atriyph, 0o644)
        except OSError as exc:
            raise OSError(f"write {SKILL_FILE_NAME}: {exc}") from exc
        if (args.go_source or "").strip() != "":
            try:
                _write_text(os.path.join(root, "main.go"), args.go_source, 0o644)
                _write_text(
                    os.path.join(root, "go.mod"), go_mod_content(name, go_version), 0o644
                )
            except OSError as exc:
                raise OSError(f"write {exc.filename}: {exc}") from exc
            await self._build(ctx, root, name, args.timeout_ms)
        try:
            await self.manager.reload(ctx)
        except BaseException as exc:
            raise RuntimeError(f"Atriyph skill written but reload failed: {exc}") from exc
        return Result(
            content=append_atriyph_warnings(f"created Atriyph skill {name}", diagnostics)
        )

    async def _build(self, ctx: Any, root: str, name: str, timeout_ms: int) -> None:
        """编译刚写下的 Go 源码。对应 Go 里那段内联 ``go build``。"""
        binary = executable_name(name)
        timeout = DEFAULT_BUILD_TIMEOUT
        if timeout_ms and timeout_ms > 0:
            timeout = timeout_ms / 1000.0
        run_ctx = with_timeout(ctx, timeout)
        environment = self.manager.process_env
        if environment is None:
            environment = Environment()
        try:
            command = environment.command_context(run_ctx, "go", "build", "-o", binary, ".")
            output = await run_command(run_ctx, command, cwd=root)
        finally:
            run_ctx.cancel("go build finished")
        if output.ctx_error is not None:
            raise RuntimeError(f"go build failed: {output.ctx_error}")
        if output.code != 0:
            raise RuntimeError(
                f"go build failed: exit status {output.code}\n"
                f"stdout:\n{truncate_output(decode_output(output.stdout))}\n"
                f"stderr:\n{truncate_output(decode_output(output.stderr))}"
            )


def new_create_atrix_skill_tool(manager: Any = None) -> CreateAtrixSkillTool:
    """对应 Go ``NewCreateAtrixSkillTool``。"""
    return CreateAtrixSkillTool(manager)


async def detect_go_mod_version(
    ctx: Any, environment: Environment | None = None
) -> str:
    """探测 ``go env GOVERSION``。对应 Go ``detectGoModVersion``。"""
    env = environment if environment is not None else Environment()
    try:
        go_path = env.look_path("go")
    except FileNotFoundError:
        raise ValueError(
            "system go executable not found; install Go and ensure it is in PATH "
            "before creating Go skill"
        ) from None
    command = env.command_context(ctx, go_path, "env", "GOVERSION")
    output = await run_command(ctx, command)
    if output.ctx_error is not None:
        raise RuntimeError(f"detect go version: {output.ctx_error}")
    if output.code != 0:
        raise RuntimeError(
            f"detect go version: exit status {output.code}\n"
            f"stderr:\n{truncate_output(output.stderr_text())}"
        )
    return parse_go_mod_version(output.stdout_text())


def parse_go_mod_version(output: str) -> str:
    """``go1.23.4`` → ``go1.23``。对应 Go ``parseGoModVersion``。"""
    version = (output or "").strip()
    if not version.startswith("go"):
        raise ValueError(f"detect go version: unsupported go version output {version!r}")
    version = version[len("go") :]
    parts = version.split(".")
    if len(parts) < 2 or parts[0] == "" or parts[1] == "":
        raise ValueError(
            f"detect go version: unsupported go version output {(output or '').strip()!r}"
        )
    minor = parts[1]
    for index, char in enumerate(minor):
        if not char.isdigit():
            minor = minor[:index]
            break
    if minor == "":
        raise ValueError(
            f"detect go version: unsupported go version output {(output or '').strip()!r}"
        )
    return parts[0] + "." + minor


def go_mod_content(name: str, version: str) -> str:
    """``go.mod`` 内容。对应 Go ``goModContent``。"""
    return f"module github.com/fujiwarashigure/ATRI-X-skill/{name}\n\ngo {version}\n"


def validate_skill_name(name: str) -> None:
    """校验 skill 名。对应 Go ``validateSkillName``。"""
    if not skill_name_pattern.match(name or ""):
        raise ValueError(f"invalid skill name {name!r}")
    if is_windows_reserved_name(name):
        raise ValueError(f"invalid skill name {name!r}: reserved file name")


def is_windows_reserved_name(name: str) -> bool:
    """是否是 Windows 保留设备名。对应 Go ``isWindowsReservedName``。"""
    return (name or "").strip().upper() in _WINDOWS_RESERVED_NAMES


def validate_go_source(source: str) -> None:
    """``go_source`` 必须含 ``package main``。对应 Go ``validateGoSource``。"""
    if "package main" not in (source or ""):
        raise ValueError("go_source must contain package main")


def with_atriyph_metadata(text: str, description: str, risk: str) -> str:
    """补写首行描述与 ``** risk`` 行。对应 Go ``withAtriyphMetadata``。"""
    text = (text or "").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines:
        return text
    header = lines[0].split()
    if len(header) >= 2:
        lines[0] = header[0] + " " + header[1]
        description = (description or "").strip()
        if description != "":
            lines[0] += " - " + description
    out = [lines[0]]
    risk = (risk or "").strip()
    if risk != "":
        out.append("** risk " + risk)
    out.extend(lines[1:])
    return "\n".join(out)


def _write_text(path: str, text: str, mode: int) -> None:
    """写 UTF-8 文本（``newline="\\n"``，不经 PowerShell）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
