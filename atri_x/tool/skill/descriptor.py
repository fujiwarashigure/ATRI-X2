"""文档型 skill 的工具描述符。

对应 Go：``internal/tool/skill/descriptor.go``。

Skill 本体是**说明文档**，不是可直接调用的 function schema：``Descriptor``
的 ``schema()`` 返回空 schema，``call()`` 永远报错（提示改用 ``discover_tool``），
详情走 ``DetailBlock``（含 Atriyph 规则卡与 AgentSkill 使用提示）。
"""

from __future__ import annotations

from typing import Any

from atri_x.atriyph import FORMAT as ATRIYPH_FORMAT
from atri_x.llm.llm import ToolSchema
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.tool.detail import DetailBlock, render_detail_blocks
from atri_x.tool.skill.agent_manifest import AGENT_SKILL_CONFIG_FILE
from atri_x.tool.skill.catalog import Kind, Record, source_for_kind
from atri_x.tool.skill.parser import parse_skill_markdown
from atri_x.tool.tool import CallRequest, Info, Result

__all__ = [
    "AgentSkillManagerName",
    "Descriptor",
    "GoRunnerName",
    "agent_skill_notice",
    "atriyph_rule_card",
    "load_record_detail",
    "new_descriptor",
]

#: ``agent_skill`` 管理工具名。对应 Go ``AgentSkillManagerName``。
AgentSkillManagerName = "agent_skill"
#: ``go_skill_run`` 运行器名（Go ``GoRunnerName``，定义在 descriptor.go）。
GoRunnerName = "go_skill_run"


def atriyph_rule_card() -> str:
    """Atriyph 规则卡。

    Go 走 ``internal/tool/runtimeinfo.AtriyphRuleCard()``，而 ``runtimeinfo``
    只是转发 ``atriyph.RuleCard()``；Python 侧 ``atri_x.tool.runtimeinfo`` 由
    另一位实现者负责，所以这里直接取唯一出处 :func:`atri_x.atriyph.rule_card`，
    行为与 Go 完全一致（顺带避免了对尚未落地模块的硬依赖）。
    """
    from atri_x.atriyph import rule_card

    return rule_card()


class Descriptor:
    """一个文档型 skill。对应 Go ``Descriptor``。"""

    def __init__(self, record: Record) -> None:
        self.record = record

    def name(self) -> str:
        return self.record.name

    def info(self) -> Info:
        """对应 Go ``Descriptor.Info``。"""
        return Info(
            name=self.record.name,
            description=self.record.description,
            source=source_for_kind(self.record.kind),
            risk=self.record.risk,
            superadmin_only=self.record.superadmin_only,
            tags=list(self.record.tags),
        )

    def schema(self) -> ToolSchema:
        """Skill 本体是说明文档，不是可直接调用的 function schema。"""
        return ToolSchema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``Descriptor.Call``：文档型 skill 不能直接调用。"""
        del ctx, req
        raise ValueError(
            f"skill {self.record.name!r} is a document skill; query it with discover_tool"
        )

    # -- 详情 ---------------------------------------------------------------

    def detail(self) -> str:
        """同步详情文本。对应 Go ``Descriptor.Detail``（无 actor 上下文）。"""
        return render_detail_blocks([self.detail_block()])

    def detail_block(self) -> DetailBlock:
        """同步详情块。对应 Go ``Descriptor.DetailBlock``。"""
        try:
            return load_record_detail(self.record)
        except (OSError, ValueError):
            return DetailBlock()

    async def load_detail(self, ctx: Any) -> DetailBlock:
        """加载详情并按 actor 追加提示。对应 Go ``Descriptor.LoadDetail``。"""
        block = load_record_detail(self.record)
        if self.record.kind == Kind.AGENT:
            notice = agent_skill_notice(ctx, self.record)
            if notice != "":
                block.content = (block.content + "\n\n" + notice).strip()
        return block

    def activate_tools(self) -> list[str]:
        """发现该 skill 时同时激活的工具。对应 Go ``Descriptor.ActivateTools``。"""
        if self.record.kind == Kind.AGENT:
            return [AgentSkillManagerName]
        if self.record.kind == Kind.GO and self.record.binary_path != "":
            return [GoRunnerName]
        return []


def new_descriptor(record: Record) -> Descriptor:
    """对应 Go ``NewDescriptor``。"""
    return Descriptor(record)


def agent_skill_notice(ctx: Any, record: Record) -> str:
    """文档型 AgentSkill 的尾部提示。对应 Go ``agentSkillNotice``。

    ``agent_skill_creator`` 的引导只对超级管理员有用（只有他们可能持有该
    工具），因此对其他人省略；``ATRIX_SKILL.toml`` 无效的警告对所有角色可见。
    """
    lines: list[str] = []
    actor, ok = actor_from_context(ctx)
    if ok and actor.role == RoleSuperadmin:
        lines.append("ATRI-X AgentSkill 使用提示：")
        lines.append("")
        lines.append(
            "- 如该文档有脚本，请发现 agent_skill_creator，参考其说明是否把他注册成普通工具。"
        )
    if record.manifest_found and record.manifest_error:
        if lines:
            lines.append("")
        lines.append(
            "- 当前 " + AGENT_SKILL_CONFIG_FILE + " 无效：" + record.manifest_error
        )
    return "\n".join(lines)


def load_record_detail(record: Record) -> DetailBlock:
    """加载一条记录的详情块。对应 Go ``loadRecordDetail``。

    有 ``detail_path``（SKILL.md）时现读现解析，否则用扫描期存下的正文。
    Atriyph 格式会附上规则卡（由渲染层按 ``rule_card`` 去重）。
    """
    content = record.detail
    format_ = record.format
    if record.detail_path != "":
        try:
            with open(record.detail_path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise OSError(f"read SKILL.md for {record.name!r}: {exc}") from exc
        try:
            definition = parse_skill_markdown(data, record.name)
        except ValueError as exc:
            raise ValueError(f"parse SKILL.md for {record.name!r}: {exc}") from exc
        content = definition.detail
        format_ = definition.format
    block = DetailBlock(content=content, format=format_)
    if format_ == ATRIYPH_FORMAT:
        block.rule_card = atriyph_rule_card()
    return block
