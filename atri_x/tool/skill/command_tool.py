"""可调用的 AgentSkill 工具（执行清单里的 command）。

对应 Go：``internal/tool/skill/command_tool.go``。

``CommandTool`` 是「AgentSkill + 有效 ATRIX_SKILL.toml + command」的产物：
它把 LLM 参数按 ``[args]`` 映射成命令行开关，按 schema 里的 ``type: "media"``
把媒体 ID 落成 skill 目录内的相对路径，再把 stdout 解析回 :class:`Result`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.llm.llm import ToolSchema
from atri_x.processenv import Environment
from atri_x.safecontext import with_timeout
from atri_x.tool.detail import DetailBlock, render_detail_blocks
from atri_x.tool.skill.agent_manifest import AgentSkillManifest, schema_required
from atri_x.tool.skill.catalog import Record, source_for_kind
from atri_x.tool.skill.descriptor import AgentSkillManagerName, load_record_detail
from atri_x.tool.skill.media import prepare_command_media
from atri_x.tool.skill.runner import DEFAULT_RUNNER_TIMEOUT, run_media_command
from atri_x.tool.tool import CallRequest, Info, Result

__all__ = [
    "CommandTool",
    "JsonNumber",
    "argument_text",
    "command_arguments",
    "new_command_tool",
    "parse_json",
]

#: Skill 的 shell 命令超时下限；对应 Go ``defaultRunnerTimeout``。
DEFAULT_COMMAND_TIMEOUT = DEFAULT_RUNNER_TIMEOUT


class JsonNumber(str):
    """JSON 数字的原始字面量。对应 Go ``json.Number``。

    用 ``str`` 子类是为了在 :func:`json.loads` 时既能保留字面量（``1.50`` 不会
    被规整成 ``1.5``），又能和真正的 JSON 字符串区分开（判定时必须**先**看
    :class:`JsonNumber`）。
    """


def parse_json(text: str) -> Any:
    """按 Go ``UseNumber`` 的语义解析 JSON。"""
    return json.loads(text, parse_float=JsonNumber, parse_int=JsonNumber)


@dataclass
class CommandTool:
    """一个可调用的 AgentSkill。对应 Go ``CommandTool``。"""

    record: Record
    manifest: AgentSkillManifest
    media: Any = None

    # -- Tool 协议 ----------------------------------------------------------

    def name(self) -> str:
        return self.record.name

    def info(self) -> Info:
        """对应 Go ``CommandTool.Info``：风险/可见性取自清单而不是记录。"""
        return Info(
            name=self.record.name,
            description=self.record.description,
            source=source_for_kind(self.record.kind),
            risk=self.manifest.risk,
            superadmin_only=self.manifest.superadmin_only,
            tags=list(self.manifest.tags),
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``CommandTool.Schema``。"""
        return self.manifest.schema(self.record.name, self.record.description)

    # -- 详情 ---------------------------------------------------------------

    def detail(self) -> str:
        """对应 Go ``CommandTool.Detail``。"""
        return render_detail_blocks([self.detail_block()])

    def detail_block(self) -> DetailBlock:
        """对应 Go ``CommandTool.DetailBlock``。"""
        try:
            return self._load_detail()
        except (OSError, ValueError):
            return DetailBlock()

    async def load_detail(self, ctx: Any) -> DetailBlock:
        """对应 Go ``CommandTool.LoadDetail``。"""
        del ctx
        return self._load_detail()

    def _load_detail(self) -> DetailBlock:
        block = load_record_detail(self.record)
        if self.manifest.expose_root:
            block.content = (block.content or "").strip() + "\n\nAgentSkill root: " + self.record.root
        block.content = (block.content or "").strip()
        return block

    def activate_tools(self) -> list[str]:
        """对应 Go ``CommandTool.ActivateTools``。"""
        return [AgentSkillManagerName, self.record.name]

    # -- 调用 ---------------------------------------------------------------

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """执行清单里的 command。对应 Go ``CommandTool.Call``。"""
        timeout = DEFAULT_COMMAND_TIMEOUT
        if self.manifest.timeout_seconds > 0:
            timeout = float(self.manifest.timeout_seconds)
        run_ctx = with_timeout(ctx, timeout)
        try:
            media_call = None
            if self.media is not None:
                media_call = self.media.new_call()
            try:
                raw = prepare_command_media(
                    run_ctx,
                    req.arguments,
                    self.manifest,
                    media_call,
                    self.record.root,
                )
                args = command_arguments(self.manifest, raw)
                parts = [*self.manifest.command, *args]
                command = Environment().command(*parts)
                return await run_media_command(
                    run_ctx, "AgentSkill command", command, media_call, cwd=self.record.root
                )
            finally:
                if media_call is not None:
                    close = getattr(media_call, "close", None)
                    if close is not None:
                        await _maybe_await(close())
        finally:
            run_ctx.cancel("AgentSkill command finished")


async def _maybe_await(value: Any) -> Any:
    """``MediaCall.Close`` 可能是同步或异步实现，两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def new_command_tool(record: Record) -> CommandTool:
    """对应 Go ``NewCommandTool``。"""
    return CommandTool(record=record, manifest=record.manifest)


def command_arguments(manifest: AgentSkillManifest, raw: str) -> list[str]:
    """把 LLM 参数映射成命令行开关。对应 Go ``commandArguments``。

    先校验 schema ``required`` 都给了值；随后按 **参数名排序** 输出
    ``flag, value`` 序列（Go 对 map 键排序，顺序稳定可测）。值为 JSON null
    或缺失的参数被跳过。
    """
    values: dict[str, Any] = {}
    if raw and raw.strip() != "":
        try:
            parsed = parse_json(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"parse skill tool arguments: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("parse skill tool arguments: must be an object")
        values = parsed
    for name in schema_required(manifest.parameters):
        if name not in values:
            raise ValueError(f"required argument {name} is missing")
    out: list[str] = []
    for name in sorted(manifest.args):
        if name not in values:
            continue
        value = values[name]
        if value is None:
            continue
        try:
            text = argument_text(value)
        except ValueError as exc:
            raise ValueError(f"argument {name}: {exc}") from exc
        out.append(manifest.args[name])
        out.append(text)
    return out


def argument_text(value: Any) -> str:
    """把一个参数值转成命令行文本。对应 Go ``argumentText``。"""
    if isinstance(value, JsonNumber):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError("must be string, number, boolean, array, or object")
