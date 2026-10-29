"""命令行入口：把十二个硬工具包装成稳定的 argv 契约 + JSON 信封。

信封（成功）::

    {"ok": true, "tool": "search", "run": "r-...", "engine": "0.1.0",
     "elapsed_ms": 1234, "data": {...}, "artifacts": [...],
     "degraded": [...], "warnings": [...]}

信封（失败）::

    {"ok": false, "tool": "search", "engine": "0.1.0", "error": {"code": ..., "message": ..., "hint": ...}}

退出码：0 成功（即使质量闸门为 WARN/FAIL——闸门是数据，不是进程失败）；
2 调用方错误（参数/运行不存在/产物缺失）；3 内部错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from typing import Any

from . import VERSION
from .errors import ARG_INVALID, INTERNAL, ResearchError
from .tool_schemas import TOOL_NAMES, TOOLS, render_schema, skill_manifest
from .capabilities import HANDLERS as NEW_HANDLERS
from .tools import TOOL_HANDLERS

__all__ = ["build_parser", "main"]


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--run", default="", help="运行 id（'latest' 表示最近一次）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atrix-research",
        description="ATRI-X 科研研究引擎：检索/筛选/核验/引用/质检/评审/导出（确定性硬工具）。",
    )
    parser.add_argument("--home", default="", help="研究数据根目录（缺省 %%APPDATA%%/ATRI-X/research）")
    parser.add_argument("--human", action="store_true", help="打印人类可读摘要而不是 JSON")

    subparsers = parser.add_subparsers(dest="tool", required=True)

    plan = subparsers.add_parser("plan", help="新建运行 / 查看状态 / 生成大纲")
    plan.add_argument("--action", default="status", choices=["init", "status", "outline"])
    _add_common(plan)
    plan.add_argument("--title", default="")
    plan.add_argument("--question", default="")
    plan.add_argument("--language", default="")
    plan.add_argument("--style", default="")
    plan.add_argument("--doc-type", dest="doc_type", default="")
    plan.add_argument("--target-words", dest="target_words", type=int, default=0)
    plan.add_argument("--owner", default="")
    plan.add_argument("--sections", type=int, default=0)

    search = subparsers.add_parser("search", help="多源检索 + 去重")
    _add_common(search)
    search.add_argument("--queries", default="", help="JSON 数组或逗号分隔")
    search.add_argument("--query", action="append", default=[], help="可重复的单个检索式")
    search.add_argument("--sources", default="", help="JSON 数组或逗号分隔")
    search.add_argument("--limit", type=int, default=0)
    search.add_argument("--year-from", dest="year_from", type=int, default=0)
    search.add_argument("--year-to", dest="year_to", type=int, default=0)

    screen = subparsers.add_parser("screen", help="PRISMA 纳排筛选")
    _add_common(screen)
    screen.add_argument("--stage", default="title_abstract", choices=["title_abstract", "full_text"])
    screen.add_argument("--criteria", default="", help="JSON 对象或 JSON 文件路径")
    screen.add_argument("--decisions", default="", help="JSON 对象：{key: {decision, reason}}")
    screen.add_argument("--pdf-dir", dest="pdf_dir", default="")
    screen.add_argument("--fulltext-keywords", dest="fulltext_keywords", default="")

    verify = subparsers.add_parser("verify", help="引文核验（DOI 反查）")
    _add_common(verify)
    verify.add_argument("--limit", type=int, default=0)
    verify.add_argument("--min-ratio", dest="min_ratio", type=float, default=0.0)
    verify.add_argument("--suggest-doi", dest="suggest_doi", default="")

    cite = subparsers.add_parser("cite", help="生成参考文献（APA 7 / GB/T 7714）")
    _add_common(cite)
    cite.add_argument("--style", default="", choices=["", "apa7", "gbt7714"])

    write = subparsers.add_parser("write", help="稿件写入与质检")
    _add_common(write)
    write.add_argument("--action", default="status", choices=["save", "append", "check", "status"])
    write.add_argument("--content", default="")
    write.add_argument("--content-file", dest="content_file", default="", help="从文件读取正文（长文用）")
    write.add_argument("--min-section-citations", dest="min_section_citations", type=int, default=0)

    panel = subparsers.add_parser("panel", help="评审聚合")
    _add_common(panel)
    panel.add_argument("--reviews", default="", help="JSON 对象（不传则返回模板）")

    export = subparsers.add_parser("export", help="研究报告与多格式导出")
    _add_common(export)
    export.add_argument("--action", default="report", choices=["report", "export", "pdf", "all"])
    export.add_argument("--formats", default="", help="JSON 数组或逗号分隔")
    export.add_argument("--pdf-dir", dest="pdf_dir", default="")

    doctor = subparsers.add_parser("doctor", help="环境体检 / 运行列表 / 离线自检")
    doctor.add_argument("--action", default="env", choices=["env", "status", "selftest", "parity"])
    doctor.add_argument("--network", default="")

    flow = subparsers.add_parser("flow", help="科研流程调度（确定性状态机）")
    flow.add_argument("--action", default="plan", choices=["plan", "markdown", "list"])
    _add_common(flow)

    compose = subparsers.add_parser("compose", help="结构化成稿 + 契约校验 + 自动质检")
    compose.add_argument("--action", default="validate", choices=["validate", "append", "save"])
    _add_common(compose)
    compose.add_argument("--sections", default="", help="JSON（或保存该 JSON 的文件路径）")
    compose.add_argument("--allow-extra", dest="allow_extra", default="")

    audit = subparsers.add_parser("audit", help="确定性评审打分")
    _add_common(audit)

    skills = subparsers.add_parser("skills", help="生成/校验技能文档附录（安装器用）")
    skills.add_argument("--digests", action="store_true", help="打印各技能附录指纹")
    skills.add_argument("--appendix", default="", help="打印某个技能的附录文本")
    skills.add_argument("--doc", default="", help="生成完整 SKILL.md（无手写文档时兜底）")
    skills.add_argument("--check", default="", help="校验目录下的已安装技能是否与引擎同源")
    skills.add_argument("--list", action="store_true", help="列出全部技能")

    schema = subparsers.add_parser("schema", help="打印工具 schema 或生成 ATRIX_SKILL.toml")
    # 注意：不能再用 dest="tool"——子解析器 dest 与主解析器的 subparsers dest 同名会互相覆盖
    schema.add_argument("--tool", dest="which_tool", default="", choices=["", *TOOL_NAMES])
    schema.add_argument("--emit-toml", dest="emit_toml", default="", choices=["", *TOOL_NAMES])

    subparsers.add_parser("version", help="打印引擎版本与可用工具")
    return parser


def _coerce_file_json(value: str) -> str:
    """``--criteria`` / ``--decisions`` 既收 JSON 文本，也收文件路径。"""
    text = (value or "").strip()
    if text and not text.startswith(("{", "[")):
        import os

        if os.path.isfile(text):
            with open(text, "r", encoding="utf-8") as handle:
                return handle.read()
    return value


def _apply_global(args: argparse.Namespace) -> None:
    home = (getattr(args, "home", "") or "").strip()
    if home:
        import os

        os.environ["ATRIX_RESEARCH_HOME"] = os.path.abspath(home)
        from . import config

        config.reset_cache()


def _human_summary(tool: str, data: dict[str, Any]) -> str:
    if tool == "plan":
        if "record" in data:
            return (
                f"已创建运行 {data['run']}\n题目：{data['record'].get('title')}\n"
                f"下一步：{'; '.join(data.get('next') or [])}"
            )
        if "runs" in data:
            lines = [f"研究数据目录：{data.get('home', '')}"]
            for run in data.get("runs") or []:
                lines.append(f"- {run['id']} [{run.get('stage')}] {run.get('title')}")
            return "\n".join(lines)
        return json.dumps(data, ensure_ascii=False, indent=2)
    if tool == "search":
        return (
            f"检索 {data.get('identified')} 条，去重后 {data.get('deduped')} 条"
            f"（删除重复 {data.get('duplicates_removed')}）；来源分布 {data.get('by_source')}"
        )
    if tool == "screen":
        counts = data.get("counts") or {}
        return (
            f"筛选完成：纳入 {counts.get('included')}，排除 {counts.get('excluded')}，"
            f"待判定 {counts.get('excluded_pending', 0)}；闸门 {data.get('gate')}"
        )
    if tool == "verify":
        return f"核验闸门 {data.get('gate')}，通过率 {data.get('verified_ratio')}，明细 {data.get('summary')}"
    if tool == "cite":
        return f"已生成 {data.get('count')} 条参考文献（{data.get('style')}）；示例：{data.get('sample')}"
    if tool == "write":
        return f"写作 {data.get('action')}：{data.get('words')} 字；下一步：{'; '.join(data.get('next') or [])}"
    if tool == "panel":
        return f"评审决定：{data.get('decision')}（{data.get('weighted_total')}）"
    if tool == "export":
        return f"导出：{json.dumps(data.get('exports'), ensure_ascii=False)}"
    if tool == "flow":
        progress = data.get("progress") or {}
        nxt = (data.get("next") or [{}])[0]
        return (
            f"流程 {progress.get('done')}/{progress.get('total')}（{progress.get('percent')}%），"
            f"闸门 {data.get('gate')}\n"
            f"下一步：{nxt.get('tool')} {json.dumps(nxt.get('args') or {}, ensure_ascii=False)}"
        )
    if tool == "compose":
        qa = data.get("qa") or {}
        return (
            f"契约{'通过' if data.get('ok') else '拒绝'}：小节 {data.get('stats', {}).get('sections')}，"
            f"引用 {data.get('stats', {}).get('citations')}，字数 {data.get('stats', {}).get('words')}"
            + (f"；质检 {qa.get('verdict')}（{qa.get('score')}）" if qa else "")
        )
    if tool == "audit":
        return f"确定性评审：{data.get('weighted_total')} / {data.get('decision')}（闸门 {data.get('gate')}）"
    if tool == "doctor":
        if "checks" in data:
            failed = [c["name"] for c in data["checks"] if not c["ok"]]
            return f"环境体检：{'通过' if data.get('ok') else '未通过'}" + (
                f"；未通过项：{', '.join(failed)}" if failed else ""
            )
        if "checks" in data or "runs" in data:
            return f"共 {data.get('count')} 个运行；最近：{data.get('latest')}"
        return f"自检 {data.get('passed')}/{data.get('total')} 通过"
    return json.dumps(data, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_global(args)
    tool = args.tool
    started = time.time()

    if tool == "version":
        payload = {"engine": VERSION, "tools": TOOL_NAMES}
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if tool == "schema":
        if args.emit_toml:
            sys.stdout.write(skill_manifest(args.emit_toml))
            return 0
        if args.which_tool:
            print(json.dumps(render_schema(args.which_tool), ensure_ascii=False, indent=2))
        else:
            print(json.dumps({name: render_schema(name) for name in TOOL_NAMES}, ensure_ascii=False, indent=2))
        return 0

    if tool == "skills":
        from . import skilldocs

        if args.digests:
            print(json.dumps(skilldocs.digests(), ensure_ascii=False, indent=2))
            return 0
        if args.appendix:
            specs = skilldocs.all_skill_specs()
            if args.appendix not in specs:
                print(json.dumps({"ok": False, "error": {"code": "ARG_INVALID", "message": f"未知技能: {args.appendix}", "hint": "用 --list 查看技能名。"}}, ensure_ascii=False, indent=2))
                return 2
            sys.stdout.write(skilldocs.appendix_of(args.appendix))
            return 0
        if args.doc:
            specs = skilldocs.all_skill_specs()
            if args.doc not in specs:
                print(json.dumps({"ok": False, "error": {"code": "ARG_INVALID", "message": f"未知技能: {args.doc}", "hint": "用 --list 查看技能名。"}}, ensure_ascii=False, indent=2))
                return 2
            sys.stdout.write(skilldocs.markdown_for(args.doc))
            return 0
        if args.check:
            report = skilldocs.check_installed(args.check)
            good = all(item["ok"] for item in report)
            print(json.dumps({"ok": good, "total": len(report), "failed": [item for item in report if not item["ok"]], "checks": report}, ensure_ascii=False, indent=2))
            return 0 if good else 1
        specs = skilldocs.all_skill_specs()
        print(json.dumps({"count": len(specs), "skills": [{"skill": skill, "tool": spec["name"], "kind": spec.get("kind", "tool")} for skill, spec in specs.items()]}, ensure_ascii=False, indent=2))
        return 0

    if tool in ("screen",):
        args.criteria = _coerce_file_json(getattr(args, "criteria", ""))
        args.decisions = _coerce_file_json(getattr(args, "decisions", ""))
    if tool == "panel":
        args.reviews = _coerce_file_json(getattr(args, "reviews", ""))
    if tool == "compose":
        args.sections = _coerce_file_json(getattr(args, "sections", ""))
    if tool == "write":
        content_file = (getattr(args, "content_file", "") or "").strip()
        if content_file:
            import os

            if not os.path.isfile(content_file):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "tool": "write",
                            "engine": VERSION,
                            "error": {
                                "code": ARG_INVALID,
                                "message": f"content-file 不存在: {content_file}",
                                "hint": "给出存在的 Markdown 文件路径。",
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 2
            with open(content_file, "r", encoding="utf-8") as handle:
                args.content = handle.read()

    handler = {**TOOL_HANDLERS, **NEW_HANDLERS}[tool]
    try:
        outcome = handler(args)
    except ResearchError as exc:
        envelope = {
            "ok": False,
            "tool": tool,
            "engine": VERSION,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": exc.to_dict(),
        }
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - 内部错误也要给结构化结果
        envelope = {
            "ok": False,
            "tool": tool,
            "engine": VERSION,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": {
                "code": INTERNAL,
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "这是引擎内部错误；把该输出与复现步骤一起报告。",
            },
        }
        if args.human:
            traceback.print_exc()
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 3

    data = outcome.get("data") or {}
    envelope = {
        "ok": True,
        "tool": tool,
        "run": data.get("run", ""),
        "engine": VERSION,
        "elapsed_ms": int((time.time() - started) * 1000),
        "data": data,
        "artifacts": outcome.get("artifacts") or [],
        "degraded": outcome.get("degraded") or [],
        "warnings": outcome.get("warnings") or [],
    }
    if args.human:
        print(_human_summary(tool, data))
        if envelope["degraded"]:
            print("\n[降级] " + "; ".join(envelope["degraded"]))
        if envelope["warnings"]:
            print("[提示] " + "; ".join(envelope["warnings"]))
    else:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return 0
