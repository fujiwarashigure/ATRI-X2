"""硬工具规格注册：新增能力只在这里加一段声明 + 一个纯函数。

文件分工：

* ``tool_schemas.py`` 里是 9 个既有工具的**参数声明**（历史出处，保持不动）；
* 本文件用 :class:`hardtool.Capability` 注册 3 个新硬工具（``flow`` / ``compose`` /
  ``audit``），并集中维护**所有**工具的"引擎强制执行规则"文案；
* 三个协议技能（``academic_research`` / ``academic_writing`` / ``academic_review``）
  也作为 ``kind="doc"`` 的能力登记：它们没有命令行，只负责告诉模型"哪些判断必须
  交给硬工具，不要自己拍板"。

新增一个硬工具的完整步骤（不需要改安装器）：

1. 写一个纯函数 ``tool_xxx(args) -> {"data", "artifacts", ...}``；
2. 在本文件 ``@register(Capability(...))`` 声明参数与不变量；
3. 在 ``HANDLERS`` 里登记 ``"xxx": tool_xxx``；
4. ``python make_skills.py --write`` 生成技能文档附录，重跑安装器。
"""

from __future__ import annotations

from typing import Any

from .audit import tool_audit
from .compose import tool_compose
from .flow import tool_flow
from .hardtool import Capability, register

__all__ = ["DOC_CAPABILITIES", "HANDLERS", "INVARIANTS", "extra_specs"]

_RUN = {"type": "string", "description": "运行 id；'latest' 表示最近一次。"}

# ---------------------------------------------------------------------------
# 新硬工具
# ---------------------------------------------------------------------------

FLOW = register(
    Capability(
        name="flow",
        skill="research_flow",
        title="科研流程调度器",
        summary=(
            "把「下一步该做什么」变成可计算的状态机：读运行目录里真实存在的产物，"
            "输出有序步骤、唯一可执行的下一步、阻塞项与闸门；顺序判断不再交给模型。"
        ),
        timeout_seconds=60,
        tags=("research", "academic", "workflow"),
        parameters={
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["plan", "markdown", "list"],
                    "description": "plan=返回计划（默认）；markdown=同时写 notes.md；list=列出全部运行。",
                },
                "run": dict(_RUN),
            },
        },
        args={"action": "--action", "run": "--run"},
        invariants=(
            "步骤顺序是常量（检索→筛选→核验→引用→大纲→成稿→质检→评审→导出），不由模型决定。",
            "每一步的状态只由对应产物的存在性与闸门计算，同一份产物必然得到同一份计划。",
            "有阻塞项时 gate=FAIL，next 只给出解除阻塞的动作；不允许跳步。",
        ),
        outputs={
            "next": "[{tool, args, why}]，只执行 next[0]，做完再调用一次本工具。",
            "steps": "[{step, status: done|ready|blocked|todo, action}]，可据此向用户解释进度。",
            "blockers": "[{code, message, fix}]，非空说明流程被硬条件卡住。",
        },
        steps=(
            "调 research_flow（action=plan）拿到 next[0]。",
            "执行 next[0].action 指向的工具，不要自己换顺序。",
            "再次调 research_flow；有 blockers 时先按 fix 解除，再继续。",
        ),
    )
)

COMPOSE = register(
    Capability(
        name="compose",
        skill="research_compose",
        title="结构化成稿与契约校验",
        summary=(
            "写作只提交结构化内容（小节标题 + 段落 + cite_key），由引擎校验标题/引用键/占位符、"
            "按大纲顺序重排、渲染 [@key] 标记、统计引用预算，并在写盘后立刻用同一套规则质检。"
        ),
        timeout_seconds=120,
        tags=("research", "academic", "writing"),
        parameters={
            "type": "object",
            "required": ["run", "action"],
            "properties": {
                "run": dict(_RUN),
                "action": {
                    "type": "string",
                    "enum": ["validate", "append", "save"],
                    "description": "validate=只校验不写盘；append=追加未写过的小节；save=整体覆盖（须覆盖全部小节）。",
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "结构化小节：[{heading: 大纲里的节标题, paragraphs: [{text: 正文段落, "
                        "cites: [cite_key, ...]}]}]；也可传保存该 JSON 的文件路径。"
                    ),
                },
                "allow_extra": {
                    "type": "string",
                    "enum": ["true", "false"],
                    "description": "是否允许大纲之外的子节标题，默认 false（会报 E_UNKNOWN_HEADING）。",
                },
            },
        },
        args={"run": "--run", "action": "--action", "sections": "--sections", "allow_extra": "--allow-extra"},
        invariants=(
            "小节标题必须来自 outline.json；引用键必须来自 references.json，否则拒绝写盘（E_UNKNOWN_HEADING / E_UNKNOWN_CITE）。",
            "渲染是纯函数：同一份结构化输入在任何模型下产生逐字节相同的 Markdown。",
            "小节顺序按大纲重排；重复提交已写过的小节会被 E_ALREADY_WRITTEN 拒绝，避免重试写重。",
            "写盘后自动跑 qa.py，verdict/score/issues 来自规则，不采纳模型自评。",
        ),
        outputs={
            "ok": "false 表示契约拒绝写入，按 errors[].fix 修正后重提。",
            "errors": "[{code, path, message, fix}]，硬缺陷，必须修。",
            "warnings": "[{code, path, message, fix}]，写入但会被质检/评审看到的风险。",
            "qa": "写盘后自动质检结果（verdict/score/errors/issues）。",
            "remaining_sections": "还没写入正文的大纲小节。",
        },
        steps=(
            "读 outline.json 与 references.json（用 research_plan action=status 确认存在）。",
            "把一到两节写成结构化段落（每段 text + cites），先 action=validate。",
            "校验通过后 action=append 写盘；按 remaining_sections 继续。",
            "全部写完后 research_audit → research_export。",
        ),
    )
)

AUDIT = register(
    Capability(
        name="audit",
        skill="research_audit",
        title="确定性评审打分",
        summary=(
            "按固定公式给六项可度量维度打分（证据覆盖/引文可靠性/方法可复现性/结构完整度/"
            "表述克制/交付完整度），列出必须处理的缺陷；原创性与伦理明确标为未度量，交给 research_panel。"
        ),
        timeout_seconds=90,
        tags=("research", "academic", "review"),
        parameters={
            "type": "object",
            "required": ["run"],
            "properties": {"run": dict(_RUN)},
        },
        args={"run": "--run"},
        invariants=(
            "分数来自产物（qa.json / verification.json / screening.json / search.json / exports）的固定公式，与模型无关。",
            "缺失维度不按 0 分计入（权重归一化），并在 unmeasured 里如实列出。",
            "qa.json 有 error、筛选有待判定、核验全 FAIL 时 gate 直接 FAIL。",
        ),
        outputs={
            "weighted_total": "0-100 加权总分；85+ accept，70-85 minor，55-70 major，<55 reject。",
            "dimensions": "六项维度的得分、权重、算法出处。",
            "unmeasured": "无法由产物度量的维度（原创性/伦理），必须由 panel 覆盖。",
            "blocks": "[{code, message, fix}]，必须处理的缺陷清单。",
        },
        steps=(
            "稿件质检通过后调 research_audit。",
            "按 blocks[].fix 修复，再重跑 research_audit，直到 gate != FAIL。",
            "需要主观评审时再调 research_panel（同模型多份意见会被降级声明）。",
        ),
    )
)

#: 新硬工具 → handler（cli 会与 tools.TOOL_HANDLERS 合并）。
HANDLERS = {
    "flow": tool_flow,
    "compose": tool_compose,
    "audit": tool_audit,
}

# ---------------------------------------------------------------------------
# 既有 9 个工具的"引擎强制执行规则"（写进技能附录，避免模型误以为可协商）
# ---------------------------------------------------------------------------

INVARIANTS: dict[str, tuple[str, ...]] = {
    "plan": (
        "大纲与逐节引用预算是模板 + 文献数计算的，不接受模型改写结构。",
        "运行 id 只允许 a-z/0-9/连字符（白名单校验，防止路径穿越）。",
        "init 之后的阶段推进以 research_flow 的 next 为准。",
    ),
    "search": (
        "每个源的成败都记录在 search.json 的 steps 里，失败不会静默吞掉。",
        "去重按 DOI 与归一化标题合并，结果可复算；每源条数上限 100。",
        "检索式原样落盘，评审可据此复现检索过程。",
    ),
    "screen": (
        "纳排判定使用固定原因码（I-OK / E-YEAR / E-TYPE / …），每条排除都有据可查。",
        "全文阶段没给 decisions 的条目记为 pending，gate=FAIL，不允许带着未决条目进入写作。",
        "人工覆盖（decisions）优先于规则，并标记 manual=true。",
    ),
    "verify": (
        "只按 DOI 反查 Crossref/DataCite，不自动改写文献元数据。",
        "通过率低于 min_ratio 时 gate=WARN（如实报警，不粉饰）。",
        "缺 DOI 时只给 ≥0.92 相似度的候选，仍需人工确认。",
    ),
    "cite": (
        "cite_key 由第一作者与年份生成，重名自动加 a/b 后缀，全局唯一。",
        "APA 7 与 GB/T 7714-2015 的格式化由代码完成，不接受模型手写参考文献表。",
        "正文标记统一为 [@cite_key]，与 qa.py 的正则一致。",
    ),
    "write": (
        "只写运行目录内的 draft.md（产物白名单），不接受任意路径。",
        "质检规则固定：未知引用键/占位符/数字越界为 error，引用预算与绝对化措辞为 warn。",
        "新写作建议用 research_compose，结构、标记与预算由契约强制执行。",
    ),
    "panel": (
        "五维加权总分与接受阈值是固定算式，不是模型感觉。",
        "independence=same-model 的评审会被显式降级声明，不能冒充独立评审。",
        "分歧超过阈值的维度会列进 needs_discussion。",
    ),
    "export": (
        "缺 docx/pdf 依赖时降级并在 degraded 里逐项说明，不会假装成功。",
        "报告里的方法/PRISMA/证据表数字直接取自产物，不允许模型重写数字。",
        "产物只写运行目录的 exports/ 与固定文件名。",
    ),
    "doctor": (
        "selftest 是离线确定性用例（去重/筛选/引用/质检/评审/解析），不联网。",
        "parity 用三份固定样本分别模拟好/敷衍/乱编模型，断言判定与分数排序不变。",
        "env 体检会区分 critical 与可选依赖，缺可选依赖不算失败。",
    ),
}

# ---------------------------------------------------------------------------
# 三个协议技能（无命令行）：把"判断权"明确交回硬工具
# ---------------------------------------------------------------------------

DOC_CAPABILITIES: dict[str, Capability] = {
    "academic_research": register(
        Capability(
            name="academic_research",
            skill="academic_research",
            title="科研助理入口协议",
            summary=(
                "科研任务的统一入口：确认题目与范围、按 research_flow 的 plan 逐步执行、"
                "每轮只做一个阶段，并把闸门与降级如实汇报给用户。"
            ),
            parameters={},
            args={},
            kind="doc",
            tags=("research", "academic", "protocol"),
            invariants=(
                "流程顺序、下一步动作、阻塞项一律以 research_flow 的输出为准，不自行判断。",
                "每轮最多推进一个阶段（受 ATRI-X max_rounds_per_turn 限制），做完立即再调 research_flow。",
                "WARN/FAIL 与 degraded 必须如实汇报，不得把降级说成成功。",
            ),
        )
    ),
    "academic_writing": register(
        Capability(
            name="academic_writing",
            skill="academic_writing",
            title="科研写作协议",
            summary=(
                "写作阶段只产出结构化内容（小节 + 段落 + cite_key），由 research_compose 负责校验、"
                "排序、引用标记、预算与写盘；模型不手写参考文献表、不自行排版。"
            ),
            parameters={},
            args={},
            kind="doc",
            tags=("research", "academic", "protocol"),
            invariants=(
                "正文必须经 research_compose 写盘，禁止直接拼 Markdown 写 draft.md（否则绕过契约与自动质检）。",
                "引用只能来自 references.json 的 by_cite_key；编造 key 会被 E_UNKNOWN_CITE 拒绝。",
                "残留占位符、绝对化措辞会被契约与质检同时拦下。",
            ),
        )
    ),
    "academic_review": register(
        Capability(
            name="academic_review",
            skill="academic_review",
            title="科研评审协议",
            summary=(
                "评审先取 research_audit 的确定性分数与缺陷清单，再补不可度量的原创性/伦理判断；"
                "分数不靠模型自评，缺陷必须逐条给出可执行修法。"
            ),
            parameters={},
            args={},
            kind="doc",
            tags=("research", "academic", "protocol"),
            invariants=(
                "六项可度量维度以 research_audit 的结果为准，不重新打分。",
                "原创性/伦理两项必须显式标注为人工判断，不得伪装成引擎结论。",
                "同一模型产出的多份评审在 research_panel 里会被声明为 same-model，不能当独立评审。",
            ),
        )
    ),
}


def extra_specs() -> dict[str, dict[str, Any]]:
    """新硬工具的 spec（与 ``tool_schemas.TOOLS`` 条目同形，供 CLI/安装器合并）。"""
    return {
        name: {"name": name, **capability.spec()}
        for name, capability in (("flow", FLOW), ("compose", COMPOSE), ("audit", AUDIT))
    }
