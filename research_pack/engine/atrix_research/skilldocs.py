"""技能文档生成与校验（安装器、``make_skills.py`` 共用）。

为什么把"技能文档"也算进硬工具：SKILL.md 里最容易漂移的是**参数表**——模型看到
过时的参数就会传错。所以文档里与引擎有关的那一段由 ``hardtool.appendix_text``
生成，安装器装完后再用指纹核对一次；手写部分（写作协议、注意事项）保持不动。

对外接口：

* :func:`all_skill_specs` —— 15 个技能（12 硬工具 + 3 协议）的规格视图；
* :func:`appendix_of` / :func:`digests` —— 生成附录文本与指纹；
* :func:`extract_appendix` —— 从 SKILL.md 里取出已安装的附录；
* :func:`check_installed` —— 逐个技能比对指纹，返回漂移清单。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from .capabilities import DOC_CAPABILITIES
from .hardtool import APPENDIX_BEGIN, APPENDIX_END, appendix_digest_spec, appendix_text
from .tool_schemas import TOOLS

__all__ = [
    "all_skill_specs",
    "appendix_of",
    "check_installed",
    "digest_of_markdown",
    "digests",
    "extract_appendix",
    "markdown_for",
    "replace_appendix",
]


def all_skill_specs() -> dict[str, dict[str, Any]]:
    """``{技能目录名: 规格}``；硬工具取 ``TOOLS``，协议技能取 ``DOC_CAPABILITIES``。"""
    specs: dict[str, dict[str, Any]] = {}
    for name, spec in TOOLS.items():
        skill = str(spec.get("skill") or "")
        if skill:
            specs[skill] = {"name": name, **spec}
    for name, capability in DOC_CAPABILITIES.items():
        spec = capability.spec()
        spec.pop("name", None)
        specs[capability.skill] = {"name": name, **spec, "kind": "doc"}
    return specs


def appendix_of(skill: str) -> str:
    return appendix_text(all_skill_specs()[skill])


def digests() -> dict[str, str]:
    return {skill: appendix_digest_spec(spec) for skill, spec in all_skill_specs().items()}


def _canonical(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def extract_appendix(markdown: str) -> str | None:
    """取出 SKILL.md 里的附录块（含两端的标记行）；没有返回 ``None``。"""
    text = (markdown or "").replace("\r\n", "\n")
    start = text.find(APPENDIX_BEGIN)
    end = text.find(APPENDIX_END)
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + len(APPENDIX_END)]


def digest_of_markdown(markdown: str) -> str | None:
    block = extract_appendix(markdown)
    if block is None:
        return None
    return hashlib.sha256(_canonical(block).encode("utf-8")).hexdigest()


def replace_appendix(markdown: str, appendix: str) -> str:
    """把附录块替换/追加到 SKILL.md（幂等：重复执行结果不变）。"""
    text = (markdown or "").replace("\r\n", "\n").rstrip()
    block = appendix.replace("\r\n", "\n").strip()
    existing = extract_appendix(text)
    if existing is not None:
        return text.replace(existing, block) + "\n"
    return text + "\n\n" + block + "\n"


def markdown_for(skill: str, guide: str = "") -> str:
    """生成完整 SKILL.md（手写 guide + 自动附录）；供没有手写文档的新技能兜底。"""
    spec = all_skill_specs()[skill]
    title = str(spec.get("title") or skill)
    summary = str(spec.get("description") or "")
    lines = [
        "---",
        f"name: {skill}",
        f"description: {summary}",
        "---",
        "",
        f"# {title}",
        "",
        summary,
        "",
    ]
    if guide.strip():
        lines.append(guide.strip())
        lines.append("")
    lines.append(appendix_of(skill).strip())
    lines.append("")
    return "\n".join(lines)


def check_installed(skills_root: str) -> list[dict[str, Any]]:
    """逐个技能比对附录指纹，返回 ``[{skill, ok, detail}]``（缺失文档也算不通过）。"""
    expected = digests()
    report: list[dict[str, Any]] = []
    for skill, want in sorted(expected.items()):
        path = os.path.join(skills_root, skill, "SKILL.md")
        if not os.path.isfile(path):
            report.append({"skill": skill, "ok": False, "detail": "缺少 SKILL.md"})
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            report.append({"skill": skill, "ok": False, "detail": f"读取失败: {exc}"})
            continue
        got = digest_of_markdown(text)
        if got is None:
            report.append({"skill": skill, "ok": False, "detail": "缺少工具契约附录（未由引擎生成）"})
        elif got != want:
            report.append({"skill": skill, "ok": False, "detail": f"附录与引擎不同源（{got[:10]}… ≠ {want[:10]}…）"})
        else:
            report.append({"skill": skill, "ok": True, "detail": "附录与引擎同源"})
    return report
