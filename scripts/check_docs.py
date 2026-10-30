#!/usr/bin/env python3
"""文档漂移门禁：把"文档会不会漂"变成 CI 能回答的问题。

背景：代码面有 7 道可执行门禁（syntax / imports / integration / secrets / except /
coverage / unittest），文档面原来只有 ``gen_stats.py --check`` 一道，而且它只校验
README 里的数字。于是路径引用、锚点、索引覆盖全靠人记得去改 —— 文档是"软副本"。

本脚本把 6 条断言变成红灯（全部只读，失败 exit 1）：

1. **相对链接可达** —— Markdown 里 ``[文本](相对路径)`` 的目标必须存在；
2. **内联路径存在** —— 反引号里的 ``atri_x/**.py`` 之类路径必须命中真实文件；
3. **索引覆盖** —— 磁盘上每个 ``atri_x`` 模块都必须出现在
   ``devdocs/generated/module-map.md``；
4. **locator 锚点** —— 同一文件内不得重复定义，且每个被引用的名字必须真有定义；
5. **行数棘轮** —— ``scripts/line_budget_baseline.json`` 冻结超线文件现状：
   基线里的文件只能变短、不能变长；**新文件**不得超线；
6. **数字一致性** —— 复用 ``gen_stats`` 的 README 规则，并扩展到任何 Markdown 里
   声称的全局模块数。

显著例外：``devdocs/generated/`` 是**生成物**，它的路径/链接由生成器保证，
所以第 1、2、4 条跳过它；第 3 条恰恰以它为目标。

用法::

    python scripts/check_docs.py            # 检查（默认）
    python scripts/check_docs.py --list     # 打印每条断言的实测计数
    python scripts/check_docs.py --update   # 重新冻结两个 baseline（人工 review 后提交）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENERATED = REPO / "devdocs" / "generated"
MODULE_MAP = GENERATED / "module-map.md"
LINE_BASELINE = REPO / "scripts" / "line_budget_baseline.json"
LINK_BASELINE = REPO / "scripts" / "doc_link_baseline.json"

#: 单文件行数上限（与 AGENTS.md 的约定一致）。
LINE_LIMIT = 600

#: 视为"仓库路径"的根：反引号里以这些开头的片段会被当作路径校验。
PATH_ROOTS: tuple[str, ...] = (
    "atri_x/",
    "tests/",
    "scripts/",
    "docs/",
    "devdocs/",
    "packaging/",
    "research_pack/",
    "verification/",
    "deploy/",
    ".github/",
    ".translation-cache/",
)

#: 根级文件名（同样当作路径校验）。
ROOT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CHANGELOG.md",
    "LEGAL.md",
    "LICENSE",
    "PORTING.md",
    "README.md",
    "pyproject.toml",
)

#: **允许不存在**的路径前缀：运行时生成 / 被 .gitignore 忽略 / 部署时才有。
#: 这些不是文档漂移，而是"按需生成"的目录；集中列在这里，而不是散落成例外。
ALLOWED_MISSING: tuple[str, ...] = (
    "deploy/.env",
    "deploy/dist/",
    "deploy/reports/",
    "deploy/data/",
    "runtime/",
    "dist/",
    "build/",
    "tests/_tmp",
    "tests/tmp_",
    "tmp_",
    "atri_x/userdata/",
)

#: 被排除在"内联路径存在性"之外的文档：CHANGELOG 是**补丁式历史记录**，
#: 它写的是"当时代码长什么样"，引用过的临时目录/已删除文件不该被判成漂移；
#: ``verification/`` 是逐轮验收的历史存档，同理；``drawio_pack/`` 是**上游
#: drawio-skill 原样随包的技能文档**（含 20 篇 references），里面的 ``scripts/...``
#: 路径是相对**技能根**写的，按仓库根解析必然不存在；改写会偏离上游、也过不了
#: 上游一致性门禁，因此整目录豁免。
INLINE_PATH_EXEMPT: tuple[str, ...] = ("CHANGELOG.md", "verification/", "drawio_pack/")

#: 不参与 locator **引用**收集的文档（历史记录里的旧锚点名不算失效引用）。
LOCATOR_REF_EXEMPT: tuple[str, ...] = ("CHANGELOG.md",)

#: 认识的"文件后缀"；反引号片段最后一段带点但不在这个集合里 → 它是 ``模块.符号``。
FILE_SUFFIXES: tuple[str, ...] = (
    ".py", ".md", ".toml", ".json", ".yaml", ".yml", ".iss", ".spec", ".sha256",
    ".txt", ".bat", ".ps1", ".cfg", ".ini", ".ts", ".js", ".css", ".html", ".png",
    ".svg", ".zip", ".exe", ".go", ".sh", ".lock", ".mmd", ".csv", ".db",
)

#: 六条断言的 id（顺序即输出顺序）。删检查项必须改 baseline，review 可见。
CHECK_IDS: tuple[str, ...] = (
    "relative_links",
    "inline_paths",
    "index_coverage",
    "locators",
    "line_budget",
    "numbers",
)

#: 反引号代码片段。
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
#: Markdown 链接。
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")
#: ``路径:12`` / ``路径:112-127`` 的行号后缀。
LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
#: locator 定义。
LOCATOR_DEF_RE = re.compile(r"<!--\s*locator:([A-Za-z0-9_-]+)\s*-->")
#: locator 引用（定义行之外的 ``locator:name``）。
LOCATOR_REF_RE = re.compile(r"locator:([A-Za-z0-9_-]+)")
#: 全局模块数声明（README 的"777 个 `.py` 模块"）。
MODULE_CLAIM_RE = re.compile(r"(\d+)\s*个\s*`\.py`\s*模块")
#: 判断一个反引号片段是否像路径时排除的字符。
_NOT_PATH_CHARS = set("{}<>|()*?=,;$%@")


def _safe(value: str) -> str:
    """让输出在当前控制台编码下一定可打印（GBK 控制台会抛 UnicodeEncodeError）。"""
    encoding = sys.stdout.encoding or "ascii"
    try:
        value.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return value.encode("ascii", errors="backslashreplace").decode("ascii")
    return value


#: 生成物头部的指纹：凡是开头带这行的 Markdown 都算生成物（不进链接/路径/锚点统计）。
GENERATED_MARKER = "生成，禁止手改"


def _is_generated(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except (OSError, UnicodeDecodeError):
        return False
    return GENERATED_MARKER in head


def iter_markdown(root: Path, *, include_generated: bool = False) -> list[Path]:
    """按路径排序列出 Markdown；默认跳过生成物与缓存目录。"""
    out: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        parts = set(path.relative_to(root).parts)
        if "__pycache__" in parts or "node_modules" in parts:
            continue
        if not include_generated:
            if "generated" in parts or _is_generated(path):
                continue
        out.append(path)
    return out


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_python_files(root: Path) -> list[Path]:
    """``atri_x/`` 下的 ``.py``（与 ``check_imports`` 同口径）。"""
    pkg = root / "atri_x"
    if not pkg.is_dir():
        return []
    return sorted(
        path
        for path in pkg.rglob("*.py")
        if "__pycache__" not in path.parts
    )


# --- 断言 1：相对链接可达 --------------------------------------------------


def check_relative_links(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    total = 0
    for path in iter_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{_relative(path, root)}: 读取失败（{exc}）")
            continue
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            total += 1
            if target.startswith("file://"):
                problems.append(f"{_relative(path, root)}: 不要用 file:// 链接（{target}）")
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{_relative(path, root)}:{line}: 相对链接目标不存在 → {target}"
                )
    return problems, total


# --- 断言 2：内联路径存在 --------------------------------------------------


def _looks_like_repo_path(candidate: str) -> bool:
    if len(candidate) < 4 or any(char in _NOT_PATH_CHARS for char in candidate):
        return False
    if " " in candidate or "`" in candidate:
        return False
    if "::" in candidate:
        # ``tests/test_x.py::Case`` / ``atri_x/a.py::sym``：只校验前半段的文件。
        candidate = candidate.split("::", 1)[0]
    if not candidate.startswith(PATH_ROOTS) and candidate not in ROOT_FILES:
        return False
    last = candidate.rstrip("/").rsplit("/", 1)[-1]
    if "." in last and not last.lower().endswith(FILE_SUFFIXES):
        # ``atri_x/app.Run`` 是「模块.符号」，不是文件路径。
        return False
    return bool(candidate.strip("/"))


def _is_allowed_missing(candidate: str) -> bool:
    return any(candidate.startswith(prefix) for prefix in ALLOWED_MISSING)


def _path_matches(rel: str, patterns: tuple[str, ...]) -> bool:
    """``rel`` 是否命中条目：``CHANGELOG.md`` 是文件名，``verification/`` 是目录前缀。"""
    for pattern in patterns:
        if pattern.endswith("/"):
            if rel.startswith(pattern) or f"/{pattern}" in rel:
                return True
            continue
        if rel == pattern or rel.endswith("/" + pattern):
            return True
    return False


def _is_inline_exempt(path: Path, root: Path) -> bool:
    return _path_matches(path.relative_to(root).as_posix(), INLINE_PATH_EXEMPT)


def _is_locator_ref_exempt(path: Path, root: Path) -> bool:
    return _path_matches(path.relative_to(root).as_posix(), LOCATOR_REF_EXEMPT)


def _clean_path_candidate(candidate: str) -> str:
    value = candidate.strip()
    value = value.rstrip(".,;:，。）)】]》\"'")
    value = LINE_SUFFIX_RE.sub("", value)
    return value


def check_inline_paths(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    total = 0
    for path in iter_markdown(root):
        if _is_inline_exempt(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in CODE_SPAN_RE.finditer(text):
            candidate = _clean_path_candidate(match.group(1))
            if "::" in candidate:
                candidate = candidate.split("::", 1)[0]
            if not _looks_like_repo_path(candidate):
                continue
            total += 1
            target = root / candidate
            if target.exists() or _is_allowed_missing(candidate):
                continue
            line = text.count("\n", 0, match.start()) + 1
            problems.append(
                f"{_relative(path, root)}:{line}: 内联路径不存在 → {candidate}"
            )
    return problems, total


# --- 断言 3：索引覆盖 ------------------------------------------------------


def _indexed_modules(root: Path) -> set[str]:
    """从生成的 module-map 里抽出已收录的模块名。"""
    if not MODULE_MAP.is_file():
        return set()
    text = MODULE_MAP.read_text(encoding="utf-8")
    return set(re.findall(r"`(atri_x(?:\.[A-Za-z0-9_]+)+)`", text))


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def check_index_coverage(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    indexed = _indexed_modules(root)
    if not indexed:
        return (
            [f"{_relative(MODULE_MAP, root)} 不存在或没有模块行"
             "（跑 python scripts/gen_index.py）"],
            0,
        )
    modules = [
        name for name in (_module_name(path, root) for path in iter_python_files(root))
        if name and name != "atri_x"
    ]
    missing = [name for name in modules if name not in indexed]
    for name in missing[:20]:
        problems.append(f"模块未进索引：{name}（跑 python scripts/gen_index.py）")
    if len(missing) > 20:
        problems.append(f"……另有 {len(missing) - 20} 个模块未进索引")
    return problems, len(modules)


# --- 断言 4：locator 锚点 --------------------------------------------------


def check_locators(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    definitions: dict[str, list[str]] = {}
    per_file: dict[tuple[str, str], int] = {}
    references: dict[str, list[str]] = {}
    total = 0
    for path in iter_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = _relative(path, root)
        for number, line in enumerate(text.splitlines(), start=1):
            for match in LOCATOR_DEF_RE.finditer(line):
                name = match.group(1)
                definitions.setdefault(name, []).append(f"{rel}:{number}")
                per_file[(rel, name)] = per_file.get((rel, name), 0) + 1
                total += 1
            stripped = LOCATOR_DEF_RE.sub("", line)
            if _is_locator_ref_exempt(path, root):
                continue
            for match in LOCATOR_REF_RE.finditer(stripped):
                references.setdefault(match.group(1), []).append(f"{rel}:{number}")
    for (rel, name), count in sorted(per_file.items()):
        if count > 1:
            problems.append(
                f"{rel}: locator:{name} 在同一文件里定义了 {count} 次（应只定义一次）"
            )
    for name, places in sorted(references.items()):
        if name not in definitions:
            problems.append(
                f"locator:{name} 被引用但没有定义 → " + "、".join(places[:3])
            )
    return problems, total


# --- 断言 5：行数棘轮 ------------------------------------------------------


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_long_files(root: Path) -> dict[str, int]:
    """超过行数上限的 ``.py``（``路径 → 行数``）。"""
    out: dict[str, int] = {}
    for path in iter_python_files(root):
        try:
            lines = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        if lines > LINE_LIMIT:
            out[_relative(path, root)] = lines
    return out


def check_line_budget(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    baseline = _load_json(LINE_BASELINE)
    frozen: dict[str, int] = dict(baseline.get("files") or {})
    current = current_long_files(root)
    for path, lines in sorted(current.items()):
        if path not in frozen:
            problems.append(
                f"{path}: {lines} 行，超过 {LINE_LIMIT} 行且不在基线里"
                "（拆分，或用 --update 冻结并 review）"
            )
        elif lines > frozen[path]:
            problems.append(
                f"{path}: {lines} 行，比基线的 {frozen[path]} 行更长（只允许变短）"
            )
    return problems, len(current)


# --- 断言 6：数字一致性 ----------------------------------------------------


def _gen_stats():
    """把 ``scripts/`` 放进 sys.path 后 import ``gen_stats``（它自带同口径扫描）。"""
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import gen_stats  # noqa: PLC0415 - 故意延迟，避免与 REPO 常量循环

    return gen_stats


def check_numbers(root: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    gen_stats = _gen_stats()
    stats = gen_stats.collect()
    readme = root / "README.md"
    if readme.is_file():
        problems.extend(gen_stats.check_readme(stats, readme.read_text(encoding="utf-8")))
    measured = stats.get("atri_x_modules") or 0
    total = 0
    for path in iter_markdown(root):
        # README 由 gen_stats 的规则比对；CHANGELOG 是历史记录（当时确实 686 个）。
        if path.name in {"README.md", "CHANGELOG.md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in MODULE_CLAIM_RE.finditer(line):
                total += 1
                claimed = int(match.group(1))
                if claimed != measured:
                    problems.append(
                        f"{_relative(path, root)}:{number}: 声称 {claimed} 个模块，"
                        f"实测 {measured}"
                    )
    return problems, total


# --- 汇总 / 基线更新 -------------------------------------------------------


def run_checks(root: Path) -> dict[str, tuple[list[str], int]]:
    """跑全部断言，返回 ``断言 id → (问题列表, 实测计数)``。"""
    results: dict[str, tuple[list[str], int]] = {}
    results["relative_links"] = check_relative_links(root)
    results["inline_paths"] = check_inline_paths(root)
    results["index_coverage"] = check_index_coverage(root)
    results["locators"] = check_locators(root)
    results["line_budget"] = check_line_budget(root)
    results["numbers"] = check_numbers(root)
    return results


def update_baselines(root: Path) -> list[str]:
    """重新冻结两个 baseline，返回写出的文件（**人工 review 后提交**）。"""
    long_files = current_long_files(root)
    line_baseline = {
        "version": 1,
        "generated_by": "python scripts/check_docs.py --update",
        "limit": LINE_LIMIT,
        "note": "棘轮：基线里的文件只能变短；新文件不得超线。收紧后再提交。",
        "files": dict(sorted(long_files.items())),
    }
    LINE_BASELINE.write_text(
        json.dumps(line_baseline, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _link_problems, inline_total = check_inline_paths(root)
    link_baseline = {
        "version": 1,
        "generated_by": "python scripts/check_docs.py --update",
        "note": "checks 是防止「静默删检查项」的指纹；inline_path_refs_max 是手写路径引用棘轮。",
        "checks": list(CHECK_IDS),
        "inline_path_refs_max": inline_total,
        "line_limit": LINE_LIMIT,
    }
    LINK_BASELINE.write_text(
        json.dumps(link_baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return [LINE_BASELINE.name, LINK_BASELINE.name]


def check_baseline_fingerprint() -> list[str]:
    """baseline 里登记的检查项集合必须与代码里的断言集合一致（防静默删检查）。"""
    baseline = _load_json(LINK_BASELINE)
    declared = baseline.get("checks")
    if declared is None:
        return []
    problems: list[str] = []
    if set(declared) != set(CHECK_IDS):
        problems.append(
            "doc_link_baseline.json 的 checks 与实际断言不一致："
            f"baseline={sorted(declared)} 实际={sorted(CHECK_IDS)}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO), help="仓库根目录")
    parser.add_argument("--update", action="store_true", help="重新冻结 baseline")
    parser.add_argument("--list", action="store_true", help="只打印实测计数")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.update:
        written = update_baselines(root)
        print(_safe("已更新 baseline：" + "、".join(written)))
        print(_safe("请 review 这两个 JSON（它们只应该变紧，不该变松）后提交。"))
        return 0

    results = run_checks(root)
    if args.list:
        for check_id in CHECK_IDS:
            problems, total = results.get(check_id, ([], 0))
            status = "OK" if not problems else f"FAIL({len(problems)})"
            print(_safe(f"{check_id:<16} measured={total:<6} {status}"))
        return 0

    problems: list[str] = list(check_baseline_fingerprint())
    for check_id in CHECK_IDS:
        check_problems, total = results.get(check_id, ([], 0))
        status = "OK" if not check_problems else "FAIL"
        print(_safe(f"{check_id:<16} measured={total:<6} {status}"))
        problems.extend(check_problems)

    if problems:
        print("")
        print(_safe(f"文档门禁失败 {len(problems)} 项："))
        for item in problems:
            print(_safe(f"  - {item}"))
        print(_safe("（路径/链接类：改文档；行数类：拆文件或 python scripts/check_docs.py "
                    "--update 冻结；数字类：python scripts/gen_stats.py --update）"))
        return 1
    print("")
    print(_safe("文档门禁全部通过"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
