"""``agent_skill`` 工具：读写 AgentSkill 的 ``ATRIX_SKILL.toml``。

对应 Go：``internal/tool/skill/agent_skill_tool.go``。

``read`` 返回当前清单、有效性状态与格式模板；``write`` 先校验 TOML 再落盘并
重载（重载失败由 :meth:`atri_x.tool.skill.manager.Manager.write_agent_skill_config`
回滚）。工具本身是 hidden 内建、高风险，且支持确认前预检。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.tool.builder import new_builder
from atri_x.tool.skill.agent_manifest import (
    AGENT_SKILL_CONFIG_FILE,
    agent_skill_config_path,
    parse_agent_skill_manifest,
)
from atri_x.tool.skill.catalog import Kind
from atri_x.tool.skill.creator import validate_skill_name
from atri_x.tool.skill.descriptor import AgentSkillManagerName
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, SourceBuiltin

__all__ = [
    "AgentSkillArgs",
    "AgentSkillPreview",
    "AgentSkillTool",
    "agent_skill_tool_description",
    "new_agent_skill_tool",
    "read_agent_skill_config",
]


@dataclass
class AgentSkillArgs:
    """``agent_skill`` 的参数。对应 Go ``agentSkillArgs``。"""

    action: str = ""
    name: str = ""
    toml: str = ""


@dataclass
class AgentSkillPreview:
    """一次调用的预检结果。对应 Go ``agentSkillPreview``。"""

    action: str = ""
    name: str = ""
    path: str = ""
    toml: str = ""
    read_content: str = ""


class AgentSkillTool:
    """读或写 AgentSkill 清单。对应 Go ``AgentSkillTool``。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    # -- Tool 协议 ----------------------------------------------------------

    def name(self) -> str:
        return AgentSkillManagerName

    def info(self) -> Info:
        """对应 Go ``AgentSkillTool.Info``。"""
        return (
            new_builder(AgentSkillManagerName)
            .description(agent_skill_tool_description())
            .source(SourceBuiltin)
            .risk(RiskHigh)
            .hidden()
            .build_info()
        )

    def schema(self) -> ToolSchema:
        """对应 Go ``AgentSkillTool.Schema``。"""
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=AgentSkillManagerName,
                description=agent_skill_tool_description(),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["read", "write"],
                            "description": "read 或 write。",
                        },
                        "name": {"type": "string", "description": "AgentSkill 名称。"},
                        "toml": {
                            "type": "string",
                            "description": "write 时写入的完整 "
                            + AGENT_SKILL_CONFIG_FILE
                            + " 内容。",
                        },
                    },
                    "required": ["action", "name"],
                },
            ),
        )

    # -- 预检与确认 ---------------------------------------------------------

    async def preflight_confirmation(self, ctx: Any, req: CallRequest) -> None:
        """确认前预检。对应 Go ``PreflightConfirmation``。"""
        await self.preview(ctx, req)

    async def risk_detail(self, ctx: Any, req: CallRequest) -> str:
        """风险确认详情。对应 Go ``RiskDetail``。"""
        preview = await self.preview(ctx, req)
        if preview.action == "read":
            return f"读取 AgentSkill {preview.name} 的 {AGENT_SKILL_CONFIG_FILE}"
        return (
            f"写入 AgentSkill {preview.name} 的 {AGENT_SKILL_CONFIG_FILE}\n"
            f"文件：{preview.path}"
        )

    # -- 调用 ---------------------------------------------------------------

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``AgentSkillTool.Call``。"""
        preview = await self.preview(ctx, req)
        if preview.action == "read":
            return Result(content=preview.read_content)
        if self.manager is None:
            raise ValueError("skill manager is not configured")
        try:
            await self.manager.write_agent_skill_config(ctx, preview.name, preview.toml)
        except BaseException as exc:
            raise RuntimeError(
                f"write {AGENT_SKILL_CONFIG_FILE} for AgentSkill {preview.name}: {exc}"
            ) from exc
        return Result(
            content=(
                f"wrote {AGENT_SKILL_CONFIG_FILE} for AgentSkill {preview.name} "
                "and reloaded skills"
            )
        )

    async def preview(self, ctx: Any, req: CallRequest) -> AgentSkillPreview:
        """解析并校验参数。对应 Go ``AgentSkillTool.preview``。"""
        error = _ctx_error(ctx)
        if error is not None:
            raise error
        args = AgentSkillArgs()
        if (req.arguments or "").strip():
            try:
                raw = json.loads(req.arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"parse agent_skill arguments: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("parse agent_skill arguments: must be an object")
            args.action = str(raw.get("action") or "")
            args.name = str(raw.get("name") or "")
            args.toml = str(raw.get("toml") or "")
        manager = self.manager
        if manager is None or manager.catalog is None:
            raise ValueError("skill manager is not configured")
        name = (args.name or "").strip()
        validate_skill_name(name)
        record, ok = manager.catalog.get(name)
        if not ok or record is None or record.kind != Kind.AGENT:
            raise ValueError(f"AgentSkill {name!r} not found")
        path = agent_skill_config_path(record.root)
        action = (args.action or "").strip()
        if action == "read":
            return AgentSkillPreview(
                action=action,
                name=name,
                path=path,
                read_content=read_agent_skill_config(record, path),
            )
        if action == "write":
            if (args.toml or "").strip() == "":
                raise ValueError("toml is required for write")
            parse_agent_skill_manifest(args.toml)
            return AgentSkillPreview(
                action=action, name=name, path=path, toml=args.toml
            )
        raise ValueError(f"invalid action {args.action!r}; use read or write")


def agent_skill_tool_description() -> str:
    """工具描述。对应 Go ``agentSkillToolDescription``。"""
    return "读或写 AgentSkill 的 ATRIX_SKILL.toml。"


def read_agent_skill_config(record: Any, path: str) -> str:
    """渲染 ``read`` 的结果（状态 + 当前 TOML + 格式模板）。"""
    lines: list[str] = [
        f"AgentSkill: {record.name}",
        f"Config file: {AGENT_SKILL_CONFIG_FILE}",
    ]
    if record.manifest_found and record.manifest_error == "" and record.manifest.callable:
        lines.append("Status: callable")
    elif record.manifest_found and record.manifest_error == "":
        lines.append("Status: document-only")
        lines.append("Policy: configured")
    elif record.manifest_found:
        lines.append("Status: invalid")
        lines.append(f"Error: {record.manifest_error}")
    else:
        lines.append("Status: document-only")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        lines.append("")
        lines.append("No current TOML.")
    except OSError:
        data = None
    else:
        text = data.decode("utf-8", errors="replace")
        lines.append("")
        lines.append("Current TOML:")
        lines.append(text if text.endswith("\n") else text + "\n")
    lines.append("")
    lines.append("Format:")
    lines.append(_FORMAT_EXAMPLE)
    return "\n".join(lines).rstrip("\n")


#: 格式模板：与 Go ``readAgentSkillConfig`` 里的字面量逐字一致。
_FORMAT_EXAMPLE = """# 文档型 Skill 只限制可见性时，可以只写：
risk = "high"
superadmin_only = true

# 注册成普通工具时，补齐命令和 schema：
risk = "medium"
superadmin_only = false
tags = ["doc"]
command = ["python", "foo.py"]
timeout_seconds = 30
expose_root = false

parameters = '''
{
  "type": "object",
  "required": ["input"],
  "properties": {
    "input": {"type": "string", "description": "输入文本"},
    "mode": {"type": "string", "description": "处理模式"}
  }
}
'''

[args]
input = "--input"
mode = "--mode"
"""


def new_agent_skill_tool(manager: Any = None) -> AgentSkillTool:
    """对应 Go ``NewAgentSkillTool``。"""
    return AgentSkillTool(manager)


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
