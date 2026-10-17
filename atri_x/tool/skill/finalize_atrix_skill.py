"""``finalize_atrix_skill``：校验、gofmt、编译并重载。

对应 Go：``internal/tool/skill/finalize_atrix_skill.go``。

它是「改完 skill 之后的收口工具」：Atriyph 校验失败、gofmt 失败、``package
main`` 校验失败、``go build`` 失败**都不抛异常**，而是把逐步报告作为结果内容
返回，让 LLM 接着修；只有基础设施错误（读不到文件、reload 挂掉）才抛。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from atri_x.atriyph import SKILL_FILE_NAME, format_diagnostics, validate_skill, warning_diagnostics
from atri_x.errors import AtriXError
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import new_builder, required
from atri_x.tool.skill.go_source import (
    format_go_source,
    run_go_build,
    validate_go_main_source,
)
from atri_x.tool.skill.process import truncate_output
from atri_x.tool.tool import CallRequest, Info, Result, RiskMedium, SourceBuiltin
from atri_x.utils.fileops import read_file, write_text_file

__all__ = [
    "FINALIZE_ATRIX_SKILL_NAME",
    "FinalizeAtrixSkillArgs",
    "FinalizeAtrixSkillTool",
    "append_command_output",
    "new_finalize_atrix_skill_tool",
]

#: 工具名。对应 Go ``FinalizeAtrixSkillName``。
FINALIZE_ATRIX_SKILL_NAME = "finalize_atrix_skill"


@dataclass
class FinalizeAtrixSkillArgs:
    """``finalize_atrix_skill`` 的参数。对应 Go ``finalizeAtrixSkillArgs``。"""

    name: str = ""
    timeout_ms: int = 0


class FinalizeAtrixSkillTool:
    """完成一次 skill 修改。对应 Go ``FinalizeAtrixSkillTool``。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    # -- Tool 协议 ----------------------------------------------------------

    def name(self) -> str:
        return FINALIZE_ATRIX_SKILL_NAME

    def info(self) -> Info:
        """对应 Go ``FinalizeAtrixSkillTool.Info``。"""
        return (
            new_builder(FINALIZE_ATRIX_SKILL_NAME)
            .description(
                "完成 ATRI-X Skill 修改：校验 SKILL.atriyph；如有 main.go，则 gofmt、"
                "校验 package main、go build；成功后 reload。错误会返回给 LLM 继续修。"
            )
            .source(SourceBuiltin)
            .risk(RiskMedium)
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``FinalizeAtrixSkillTool.Schema``。"""
        return (
            new_builder(FINALIZE_ATRIX_SKILL_NAME)
            .description(
                "完成 ATRI-X Skill 修改并检查结果。会校验 SKILL.atriyph；如果存在 "
                "main.go，会自动 gofmt 写回、校验 package main、go build；成功后 "
                "reload。gofmt/build/语法错误会作为结果内容返回，供继续修改。"
            )
            .string("name", "skill 名称。", required())
            .integer("timeout_ms", "可选，Go 编译超时时间，默认 60000。")
            .build_schema()
        )

    # -- 调用 ---------------------------------------------------------------

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``FinalizeAtrixSkillTool.Call``。"""
        from atri_x.tool.skill.creator import validate_skill_name

        args = FinalizeAtrixSkillArgs()
        if (req.arguments or "").strip():
            try:
                raw = json.loads(req.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parse finalize_atrix_skill arguments: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    "parse finalize_atrix_skill arguments: must be an object"
                )
            args.name = str(raw.get("name") or "")
            timeout = raw.get("timeout_ms")
            args.timeout_ms = (
                int(timeout)
                if isinstance(timeout, int) and not isinstance(timeout, bool)
                else 0
            )
        if self.manager is None or self.manager.registry is None:
            raise ValueError("skill manager is not configured")
        name = (args.name or "").strip()
        validate_skill_name(name)
        root = os.path.join(self.manager.root, "go", name)
        atriyph_path = os.path.join(root, SKILL_FILE_NAME)
        try:
            with open(atriyph_path, "rb") as handle:
                atriyph_data = handle.read()
        except OSError as exc:
            raise OSError(f"read {SKILL_FILE_NAME}: {exc}") from exc
        text = atriyph_data.decode("utf-8", errors="replace")
        lines: list[str] = [f"finalize ATRI-X skill {name}", ""]
        try:
            _, diagnostics = validate_skill(text, name)
        except AtriXError as exc:
            lines.append("Atriyph: failed")
            lines.append(str(exc))
            return Result(content="\n".join(lines) + "\n")
        lines.append("Atriyph: ok")
        warnings = warning_diagnostics(list(diagnostics))
        if warnings:
            lines.append("Atriyph warnings:")
            lines.append(format_diagnostics(warnings))

        source_path = os.path.join(root, "main.go")
        if not os.path.exists(source_path):
            await self._reload(ctx)
            lines.append("Go source: not found, text-only skill")
            lines.append("reload: ok")
            return Result(content="\n".join(lines) + "\n")
        try:
            file = read_file(source_path, "")
        except OSError as exc:
            raise OSError(f"read main.go: {exc}") from exc
        lines.append("Go source: found")

        try:
            formatted = await format_go_source(file.text, self.manager.process_env, ctx)
        except ValueError as exc:
            lines.append("gofmt: failed")
            lines.append(str(exc))
            return Result(content="\n".join(lines) + "\n")
        if formatted != file.text:
            try:
                write_text_file(source_path, file, formatted)
            except OSError as exc:
                raise OSError(f"write main.go: {exc}") from exc
            lines.append("gofmt: changed")
        else:
            lines.append("gofmt: unchanged")
        try:
            validate_go_main_source(formatted)
        except ValueError as exc:
            lines.append("Go source validation: failed")
            lines.append(str(exc))
            return Result(content="\n".join(lines) + "\n")
        lines.append("Go source validation: ok")

        build = await run_go_build(
            ctx, root, name, args.timeout_ms, self.manager.process_env
        )
        if build.err is not None:
            lines.append("go build: failed")
            lines.append(str(build.err))
            append_command_output(lines, build.stdout, build.stderr)
            return Result(content="\n".join(lines) + "\n")
        lines.append("go build: ok")
        append_command_output(lines, build.stdout, build.stderr)

        await self._reload(ctx)
        lines.append("reload: ok")
        return Result(content="\n".join(lines) + "\n")

    async def _reload(self, ctx: Any) -> None:
        try:
            await self.manager.reload(ctx)
        except BaseException as exc:
            raise RuntimeError(f"ATRI-X skill finalized but reload failed: {exc}") from exc


def append_command_output(lines: list[str], stdout: str, stderr: str) -> None:
    """把 stdout/stderr 追加到报告里。对应 Go ``appendCommandOutput``。"""
    if (stdout or "").strip() != "":
        lines.append("stdout:")
        lines.append(truncate_output(stdout))
    if (stderr or "").strip() != "":
        lines.append("stderr:")
        lines.append(truncate_output(stderr))


def new_finalize_atrix_skill_tool(manager: Any = None) -> FinalizeAtrixSkillTool:
    """对应 Go ``NewFinalizeAtrixSkillTool``。"""
    return FinalizeAtrixSkillTool(manager)
