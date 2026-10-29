"""AgentSkill 硬工具框架：把"能力"从提示词搬进代码。

一个硬工具 = **声明式规格**（名字/参数 JSON Schema/风险/不变量）+ **纯函数实现**。
本模块只负责规格这一半：校验规格、生成 ``ATRIX_SKILL.toml``、生成 SKILL.md 的
"工具契约"附录、算附录指纹（用来检测技能文档与引擎是否漂移）。

这样做的意义（也是它要解决的问题）：

* 提示词技能的质量随模型变化；硬工具的**判定、排版、计数、门禁全在代码里**，
  换模型只会影响"写什么内容"，不会影响"怎么算、怎么排版、怎么判"。
* 新增一个硬工具不需要改安装器、不需要手写 TOML：实现一个纯函数 + 一个
  :class:`Capability`，``make_skills.py`` 就会把契约附录写进 SKILL.md，安装器
  扫到 ``schema`` 输出后自动装上。
* SPEC 校验会在安装阶段就拦住"参数声明与命令行开关不一致"这类漂移——
  这类错误以前要靠跑一次真实调用才发现。

约定：本模块不导入引擎其它模块（除了标准库），便于被安装器单独调用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CAPABILITIES",
    "appendix_text",
    "appendix_digest_spec",
    "Capability",
    "all_specs",
    "appendix_digest",
    "get",
    "register",
    "render_appendix",
    "render_skill_md",
    "render_toml",
    "spec_of",
]

#: 允许的风险级别；``superadmin_only`` 由安装器按 ``risk`` 之外单独控制。
RISK_LEVELS = ("safe", "low", "medium", "high")

#: 生成附录/SKILL.md 时使用的标记；``make_skills.py`` 与安装器校验都认它。
APPENDIX_BEGIN = "<!-- hardtool-contract:begin -->"
APPENDIX_END = "<!-- hardtool-contract:end -->"


@dataclass(frozen=True)
class Capability:
    """一个可被 AgentSkill 调用的确定性能力。"""

    #: CLI 子命令名，例如 ``compose``。
    name: str
    #: 对应的 AgentSkill 目录名，例如 ``research_compose``。
    skill: str
    #: 人类可读短名（技能标题）。
    title: str
    #: 一句话说明（写进 ``ATRIX_SKILL.toml`` 的 description 与技能 frontmatter）。
    summary: str
    parameters: dict[str, Any]
    args: dict[str, str]
    risk: str = "low"
    timeout_seconds: int = 180
    tags: tuple[str, ...] = ()
    #: 引擎强制执行的规则（写进附录；模型读了知道"这些不是建议，是代码"）。
    invariants: tuple[str, ...] = ()
    #: 单轮协议步骤（写进自动生成的 SKILL.md；手工 SKILL.md 可覆盖）。
    steps: tuple[str, ...] = ()
    #: 输出 JSON 的关键字段说明：``{字段: 说明}``（写进附录，方便模型解析结果）。
    outputs: dict[str, str] = field(default_factory=dict)
    #: ``tool`` = 有命令行的硬工具；``doc`` = 纯协议技能（无命令）。
    kind: str = "tool"

    # -- 校验 --------------------------------------------------------------

    def validate(self) -> None:
        """规格自检：参数与命令行开关必须一一对应，否则安装出来就是坏的。"""
        if not self.name or not self.skill:
            raise ValueError(f"Capability 缺少 name/skill: {self!r}")
        if self.kind not in ("tool", "doc"):
            raise ValueError(f"{self.name}: kind 只能是 tool/doc")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"{self.name}: risk 必须是 {RISK_LEVELS}，收到 {self.risk!r}")
        if self.kind == "tool":
            params = self.parameters or {}
            if params.get("type") != "object":
                raise ValueError(f"{self.name}: parameters.type 必须是 object")
            properties = params.get("properties") or {}
            if not properties:
                raise ValueError(f"{self.name}: parameters.properties 不能为空")
            missing = set(properties) - set(self.args)
            extra = set(self.args) - set(properties)
            if missing or extra:
                raise ValueError(
                    f"{self.name}: 参数与命令行开关不一致；"
                    f"缺少开关={sorted(missing)}，多余开关={sorted(extra)}"
                )
            required = params.get("required") or []
            unknown_required = set(required) - set(properties)
            if unknown_required:
                raise ValueError(f"{self.name}: required 里有未声明的参数 {sorted(unknown_required)}")

    # -- 导出 --------------------------------------------------------------

    def spec(self) -> dict[str, Any]:
        """与既有 ``tool_schemas.TOOLS`` 条目同形的字典（安装器/CLI 共用）。"""
        return {
            "skill": self.skill,
            "title": self.title,
            "risk": self.risk,
            "timeout_seconds": self.timeout_seconds,
            "tags": list(self.tags),
            "description": self.summary,
            "parameters": self.parameters,
            "args": dict(self.args),
            "invariants": list(self.invariants),
            "steps": list(self.steps),
            "outputs": dict(self.outputs),
            "kind": self.kind,
        }


#: 全局能力表（只增不改；重名直接报错，避免安装出两个同名字段）。
CAPABILITIES: dict[str, Capability] = {}


def register(capability: Capability) -> Capability:
    capability.validate()
    if capability.name in CAPABILITIES:
        raise ValueError(f"硬工具重名: {capability.name}")
    CAPABILITIES[capability.name] = capability
    return capability


def get(name: str) -> Capability:
    try:
        return CAPABILITIES[name]
    except KeyError:
        raise KeyError(f"未注册的硬工具: {name}（已注册：{', '.join(sorted(CAPABILITIES))}）") from None


def spec_of(name: str) -> dict[str, Any]:
    return get(name).spec()


def all_specs() -> dict[str, dict[str, Any]]:
    return {name: cap.spec() for name, cap in CAPABILITIES.items()}


# ---------------------------------------------------------------------------
# 文档生成
# ---------------------------------------------------------------------------


def _type_label(schema: dict[str, Any]) -> str:
    """把 JSON Schema 片段翻成一句人话类型。"""
    kind = str(schema.get("type") or "")
    if kind == "string":
        enum = schema.get("enum")
        if enum:
            return "枚举(" + " / ".join(str(item) for item in enum) + ")"
        return "字符串"
    if kind == "integer":
        return "整数"
    if kind == "number":
        return "数字"
    if kind == "boolean":
        return "布尔"
    if kind == "array":
        item = schema.get("items") or {}
        return "数组<" + _type_label(item) + ">"
    if kind == "object":
        return "对象(JSON)"
    return kind or "任意"


def appendix_text(spec: dict[str, Any]) -> str:
    """由 spec 字典生成 SKILL.md 末尾的"工具契约"附录（确定性文本）。

    ``spec`` 与 ``TOOLS`` 条目同形，另需 ``name``（CLI 子命令）与可选 ``kind``。
    """
    name = str(spec.get("name") or spec.get("skill") or "")
    skill = str(spec.get("skill") or "")
    kind = str(spec.get("kind") or "tool")
    risk = str(spec.get("risk") or "low")
    lines = [
        APPENDIX_BEGIN,
        "## 工具契约（由引擎自动生成，勿手改）",
        "",
        f"- 工具：`{name}` · 技能：`{skill}`",
    ]
    if kind == "doc":
        lines.append("- 形态：协议技能（无命令行，只规定必须调用哪些硬工具、哪些判断不许自己做）")
    else:
        lines.append(f"- 风险：`{risk}`" + ("（普通成员可调用）" if risk in ("safe", "low") else "（需更高权限）"))
        lines.append(f"- 超时：{int(spec.get('timeout_seconds') or 0)} 秒")
        lines.append("- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。")
    lines.append("")
    properties = (spec.get("parameters") or {}).get("properties") or {}
    required = set((spec.get("parameters") or {}).get("required") or [])
    if properties:
        lines.append("| 参数 | 类型 | 必填 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for prop, schema in properties.items():
            description = str(schema.get("description") or "").replace("|", "\\|")
            lines.append(
                f"| `{prop}` | {_type_label(schema)} | {'是' if prop in required else '否'} | {description} |"
            )
        lines.append("")
    outputs = spec.get("outputs") or {}
    if outputs:
        lines.append("结果里的关键字段：")
        lines.append("")
        for key, description in outputs.items():
            lines.append(f"- `{key}`：{description}")
        lines.append("")
    invariants = spec.get("invariants") or []
    if invariants:
        lines.append("引擎强制执行的规则（不是建议）：")
        lines.append("")
        for invariant in invariants:
            lines.append(f"- {invariant}")
        lines.append("")
    lines.extend([APPENDIX_END, ""])
    return "\n".join(lines)


def render_appendix(capability: Capability) -> str:
    """由 :class:`Capability` 生成附录。"""
    return appendix_text(capability.spec())


def _canonical(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def appendix_digest_spec(spec: dict[str, Any]) -> str:
    """附录内容指纹（spec 版）；安装器用它判断技能文档是否与引擎同源。"""
    return hashlib.sha256(_canonical(appendix_text(spec)).encode("utf-8")).hexdigest()


def appendix_digest(capability: Capability) -> str:
    """附录内容指纹（Capability 版）。"""
    return appendix_digest_spec(capability.spec())


def render_skill_md(capability: Capability) -> str:
    """没有手写 SKILL.md 时的兜底生成（新硬工具可以直接用）。"""
    lines = [
        "---",
        f"name: {capability.skill}",
        f"description: {capability.summary}",
        "---",
        "",
        f"# {capability.title}",
        "",
        capability.summary,
        "",
    ]
    if capability.steps:
        lines.append("## 执行顺序")
        lines.append("")
        for index, step in enumerate(capability.steps, start=1):
            lines.append(f"{index}. {step}")
        lines.append("")
    lines.append(render_appendix(capability).strip())
    lines.append("")
    return "\n".join(lines)


def render_toml(capability: Capability, *, command: list[str] | None = None) -> str:
    """生成 ``ATRIX_SKILL.toml`` 文本（与安装器既有格式保持一致）。"""
    if capability.kind != "tool":
        raise ValueError(f"{capability.name}: doc 类技能没有命令行，不能生成 TOML")
    lines = [
        "# 由 atri-research 安装器生成；规格源文件 engine/atrix_research/capabilities.py",
        f'risk = "{capability.risk}"',
        "superadmin_only = false",
        "tags = [" + ", ".join(json.dumps(tag, ensure_ascii=False) for tag in capability.tags) + "]",
    ]
    if command:
        lines.append("command = [" + ", ".join(json.dumps(part, ensure_ascii=False) for part in command) + "]")
    lines.append(f"timeout_seconds = {int(capability.timeout_seconds)}")
    lines.append("expose_root = false")
    lines.append("")
    lines.append("parameters = \'\'\'")
    params = json.dumps(capability.parameters, ensure_ascii=False, indent=2)
    if "\'\'\'" in params:
        params = params.replace("\'\'\'", "\\u0027\\u0027\\u0027")
    lines.append(params)
    lines.append("\'\'\'")
    lines.append("")
    lines.append("[args]")
    for prop, flag in capability.args.items():
        lines.append(f'{prop} = "{flag}"')
    return "\n".join(lines) + "\n"
