"""硬工具的参数 schema 与命令行开关映射（唯一出处）。

安装器读取这里生成每个 AgentSkill 的 ``ATRIX_SKILL.toml``，引擎读取这里
渲染 ``schema`` 子命令，两边不会漂移。约定：

* ``parameters`` 是 JSON Schema 对象，直接放进 TOML 的 ``parameters = '''...'''``；
* ``args`` 是"参数名 → 命令行开关"，必须与 ``parameters.properties`` 的键一一对应；
* 复杂类型（数组/对象）由模型传 JSON，CLI 侧按 JSON 解析，不经过 shell。
"""

from __future__ import annotations

from typing import Any

__all__ = ["TOOLS", "TOOL_NAMES", "render_schema", "skill_manifest"]

_RUN = {
    "run": {
        "type": "string",
        "description": "运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。",
    }
}

TOOLS: dict[str, dict[str, Any]] = {
    "plan": {
        "skill": "research_plan",
        "risk": "low",
        "timeout_seconds": 120,
        "tags": ["research", "academic", "plan"],
        "description": (
            "科研项目起步与大纲：新建一次研究运行（题目/研究问题/文体/语言/引用样式），"
            "查询状态与产物，或按文体生成逐节写作契约与证据池。"
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["init", "status", "outline"],
                    "description": "init=新建运行；status=查看运行/产物；outline=生成大纲与写作契约。",
                },
                **{key: dict(value) for key, value in _RUN.items()},
                "title": {"type": "string", "description": "题目（init 必填）。"},
                "question": {"type": "string", "description": "一句话研究问题（init 必填）。"},
                "language": {"type": "string", "enum": ["zh", "en"], "description": "写作语言，默认 zh。"},
                "style": {
                    "type": "string",
                    "enum": ["gbt7714", "apa7"],
                    "description": "引用样式：gbt7714=GB/T 7714-2015，apa7=APA 第 7 版。",
                },
                "doc_type": {
                    "type": "string",
                    "enum": ["review", "empirical", "proposal"],
                    "description": "文体：综述 / 实证 / 开题（默认 review）。",
                },
                "target_words": {"type": "integer", "description": "目标篇幅（字/词），默认 6000。"},
                "owner": {"type": "string", "description": "发起人标记（可选，写入运行记录）。"},
            },
        },
        "args": {
            "action": "--action",
            "run": "--run",
            "title": "--title",
            "question": "--question",
            "language": "--language",
            "style": "--style",
            "doc_type": "--doc-type",
            "target_words": "--target-words",
            "owner": "--owner",
        },
    },
    "search": {
        "skill": "research_search",
        "risk": "low",
        "timeout_seconds": 240,
        "tags": ["research", "academic", "literature"],
        "description": (
            "多源文献检索与去重：在 OpenAlex/Crossref/arXiv/PubMed/Semantic Scholar 上"
            "按关键词检索，按 DOI/标题合并重复条目，写入本次运行的文献库并回报每个源的成败。"
        ),
        "parameters": {
            "type": "object",
            "required": ["run", "queries"],
            "properties": {
                **{key: dict(value) for key, value in _RUN.items()},
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "检索式列表（从研究问题拆出的 2-5 组关键词，中英文都可）。",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数据源子集，默认 openalex/crossref/arxiv；可选 pubmed、semanticscholar。",
                },
                "limit": {"type": "integer", "description": "每个源每个检索式的条数上限，默认 20。"},
                "year_from": {"type": "integer", "description": "最早年份（可选）。"},
                "year_to": {"type": "integer", "description": "最晚年份（可选）。"},
            },
        },
        "args": {
            "run": "--run",
            "queries": "--queries",
            "sources": "--sources",
            "limit": "--limit",
            "year_from": "--year-from",
            "year_to": "--year-to",
        },
    },
    "screen": {
        "skill": "research_screen",
        "risk": "low",
        "timeout_seconds": 180,
        "tags": ["research", "academic", "screening"],
        "description": (
            "按 PRISMA 做纳排筛选：用固定规则与原因码筛选标题摘要或全文阶段，"
            "支持人工判定覆盖，输出每条判定明细与 PRISMA 计数（可复算、可审计）。"
        ),
        "parameters": {
            "type": "object",
            "required": ["run", "stage"],
            "properties": {
                **{key: dict(value) for key, value in _RUN.items()},
                "stage": {
                    "type": "string",
                    "enum": ["title_abstract", "full_text"],
                    "description": "筛选阶段；full_text 需要 decisions 给出逐条判断。",
                },
                "criteria": {
                    "type": "object",
                    "description": (
                        "纳排标准；不传则用运行内 criteria.json（首次自动生成模板）。"
                        "结构：{include:{year_from,year_to,languages,types,require_abstract,"
                        "title_keywords},exclude:{title_keywords,abstract_keywords,venues,retracted}}"
                    ),
                },
                "decisions": {
                    "type": "object",
                    "description": "人工判定覆盖：{文献key: {decision:'include'|'exclude', reason:'...'}}。",
                },
                "pdf_dir": {
                    "type": "string",
                    "description": "全文 PDF 所在目录（仅 full_text 阶段；需要 venv 里的 pypdf）。",
                },
                "fulltext_keywords": {
                    "type": "object",
                    "description": "全文自动判定关键词：{include:[...],exclude:[...]}，仅作初筛建议。",
                },
            },
        },
        "args": {
            "run": "--run",
            "stage": "--stage",
            "criteria": "--criteria",
            "decisions": "--decisions",
            "pdf_dir": "--pdf-dir",
            "fulltext_keywords": "--fulltext-keywords",
        },
    },
    "verify": {
        "skill": "research_verify",
        "risk": "low",
        "timeout_seconds": 300,
        "tags": ["research", "academic", "citation"],
        "description": (
            "引文核验：逐条用 DOI 反查 Crossref，比对标题/年份/第一作者，标记撤稿与"
            "疑似错配；缺 DOI 时按标题反查给出候选（不自动写入）。输出通过率与闸门。"
        ),
        "parameters": {
            "type": "object",
            "required": ["run"],
            "properties": {
                **{key: dict(value) for key, value in _RUN.items()},
                "limit": {"type": "integer", "description": "最多核验多少条（默认全部纳入文献）。"},
                "min_ratio": {
                    "type": "number",
                    "description": "核验通过率闸门，默认 0.9；低于它闸门为 WARN。",
                },
                "suggest_doi": {
                    "type": "string",
                    "enum": ["true", "false"],
                    "description": "缺 DOI 时是否按标题反查候选，默认 true。",
                },
            },
        },
        "args": {
            "run": "--run",
            "limit": "--limit",
            "min_ratio": "--min-ratio",
            "suggest_doi": "--suggest-doi",
        },
    },
    "cite": {
        "skill": "research_cite",
        "risk": "low",
        "timeout_seconds": 120,
        "tags": ["research", "academic", "reference"],
        "description": (
            "生成参考文献：把纳入文献按 APA 7 或 GB/T 7714-2015 格式化，分配唯一引用键"
            "（正文用 [@key] 标注），输出参考文献表与键映射。"
        ),
        "parameters": {
            "type": "object",
            "required": ["run"],
            "properties": {
                **{key: dict(value) for key, value in _RUN.items()},
                "style": {
                    "type": "string",
                    "enum": ["gbt7714", "apa7"],
                    "description": "引用样式，缺省用运行记录的样式。",
                },
            },
        },
        "args": {"run": "--run", "style": "--style"},
    },
    "write": {
        "skill": "research_write",
        "risk": "low",
        "timeout_seconds": 120,
        "tags": ["research", "academic", "writing"],
        "description": (
            "稿件写入与质检：分段保存/追加正文（避免超长参数），或对全文跑固定规则检查"
            "（引用键闭合、占位符、引用预算、绝对化措辞、长度、参考文献格式）。"
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "append", "check", "status"],
                    "description": "save=整体覆盖；append=追加一段；check=质检；status=字数与节数。",
                },
                **{key: dict(value) for key, value in _RUN.items()},
                "content": {
                    "type": "string",
                    "description": "Markdown 正文（save/append 用；单次建议 ≤ 20000 字符）。",
                },
                "min_section_citations": {
                    "type": "integer",
                    "description": "每节最少引用数；缺省按大纲的引用预算。",
                },
            },
        },
        "args": {
            "action": "--action",
            "run": "--run",
            "content": "--content",
            "min_section_citations": "--min-section-citations",
        },
    },
    "panel": {
        "skill": "research_panel",
        "risk": "low",
        "timeout_seconds": 90,
        "tags": ["research", "academic", "review"],
        "description": (
            "评审聚合：按五维评分（创新/严谨/清晰/重要/伦理）计算加权总分、分歧与决定，"
            "并对同模型评审显式降级声明；不传 reviews 时返回空白评审模板。"
        ),
        "parameters": {
            "type": "object",
            "required": ["run"],
            "properties": {
                **{key: dict(value) for key, value in _RUN.items()},
                "reviews": {
                    "type": "object",
                    "description": (
                        "评审数据：{reviews:[{reviewer,independence:'independent'|'same-model',"
                        "confidence,scores:{originality,rigor,clarity,significance,ethics},"
                        "comments,required_fixes,recommendation}]}。"
                    ),
                },
            },
        },
        "args": {"run": "--run", "reviews": "--reviews"},
    },
    "export": {
        "skill": "research_export",
        "risk": "low",
        "timeout_seconds": 240,
        "tags": ["research", "academic", "export"],
        "description": (
            "交付物生成：装配研究报告（方法/PRISMA/证据表/局限），或把正文与报告导出为"
            "Markdown/HTML/DOCX/PDF；缺第三方库时降级并明确说明。"
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["report", "export", "pdf", "all"],
                    "description": "report=只装配报告；export=导出已有稿件；pdf=抽取 PDF 全文；all=报告+导出。",
                },
                **{key: dict(value) for key, value in _RUN.items()},
                "formats": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "导出格式子集，默认 ['md','html','docx','pdf']。",
                },
                "pdf_dir": {"type": "string", "description": "pdf 动作的源目录。"},
            },
        },
        "args": {
            "action": "--action",
            "run": "--run",
            "formats": "--formats",
            "pdf_dir": "--pdf-dir",
        },
    },
    "doctor": {
        "skill": "research_doctor",
        "risk": "low",
        "timeout_seconds": 120,
        "tags": ["research", "academic", "diagnostics"],
        "description": (
            "环境与运行诊断：检查引擎/venv/可选依赖/网络/数据目录，列出全部研究运行，"
            "或跑离线自检（去重/筛选/引用/质检/评审的确定性用例）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["env", "status", "selftest", "parity"],
                    "description": (
                        "env=环境体检；status=运行列表；selftest=离线自检；"
                        "parity=模型无关性回归（好/敷衍/乱编三份样本，默认 env）。"
                    ),
                },
                "network": {
                    "type": "string",
                    "enum": ["true", "false"],
                    "description": "env 是否探测网络，默认 true。",
                },
            },
        },
        "args": {"action": "--action", "network": "--network"},
    },
}

#: 与 CLI 子命令同名，顺序会影响 ``--help`` 展示。
# ---------------------------------------------------------------------------
# 合并新增硬工具（capabilities.py）与"引擎强制执行规则"
# ---------------------------------------------------------------------------

from .capabilities import INVARIANTS, extra_specs  # noqa: E402  （capabilities 不导入本模块，无循环）

for _tool_name, _tool_spec in TOOLS.items():
    _tool_spec.setdefault("name", _tool_name)
    _tool_spec.setdefault("invariants", list(INVARIANTS.get(_tool_name, ())))
    _tool_spec.setdefault("outputs", {})
    _tool_spec.setdefault("kind", "tool")

TOOLS.update(extra_specs())

TOOL_NAMES = list(TOOLS)


def render_schema(tool: str) -> dict[str, Any]:
    """给安装器/调试用的完整描述（不含任何执行逻辑）。"""
    spec = TOOLS[tool]
    return {
        "tool": tool,
        "skill": spec["skill"],
        "description": spec["description"],
        "risk": spec["risk"],
        "tags": spec["tags"],
        "timeout_seconds": spec["timeout_seconds"],
        "parameters": spec["parameters"],
        "args": spec["args"],
        "kind": spec.get("kind", "tool"),
        "invariants": list(spec.get("invariants") or []),
        "outputs": dict(spec.get("outputs") or {}),
    }


def skill_manifest(tool: str) -> str:
    """生成 ``ATRIX_SKILL.toml`` 文本（安装器把 command 替换成真实路径后写入）。"""
    import json

    spec = TOOLS[tool]
    lines = [
        f"# 由 atri-research 安装器生成；改这里请改 engine/atrix_research/tool_schemas.py",
        f'risk = "{spec["risk"]}"',
        "superadmin_only = false",
        "tags = [" + ", ".join(f'"{tag}"' for tag in spec["tags"]) + "]",
        f'timeout_seconds = {spec["timeout_seconds"]}',
        "expose_root = false",
        "",
    ]
    params = json.dumps(spec["parameters"], ensure_ascii=False, indent=2)
    if "'''" in params:  # 防御：JSON 内容里出现三引号会破坏 TOML 字面量
        params = params.replace("'''", "\\u0027\\u0027\\u0027")
    lines.append("parameters = '''")
    lines.append(params)
    lines.append("'''")
    lines.append("")
    lines.append("[args]")
    for name, flag in spec["args"].items():
        lines.append(f'{name} = "{flag}"')
    return "\n".join(lines) + "\n"
