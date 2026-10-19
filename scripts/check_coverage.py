#!/usr/bin/env python3
"""重写覆盖率闸门：检查 Go 的每个生产包是否都有对应的 Python 模块。

「全面全量重写」这条要求必须是**可测量**的，否则只能靠感觉声称完成。
本脚本从 :file:`PORTING.md` §4 的映射表（唯一事实来源）读出 Go 包 → Python
模块的对应关系，展开 glob，逐个检查存在性，并按 Go 生产行数加权给出进度。

**口径提醒（别把 100% 读成"实现正确"）**：这里量的是**文件存在性**——
映射表里的每个 Go 生产包是否已经有对应的 Python 模块文件；它**不衡量行为
正确性**（那是 ``tests/`` 和 ``verification/`` 的职责），也不看行数是否等价。
因此 ``weighted=100.0%`` 的含义是"没有整包缺失"，不是"没有 bug"。

用法::

    python scripts/check_coverage.py                # 报告
    python scripts/check_coverage.py --strict       # 有缺失就退出码 1
    python scripts/check_coverage.py --json out.json
    python scripts/check_coverage.py --list-missing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORTING = REPO / "PORTING.md"
#: 上游 Go 仓库位置：环境变量 ``ATRI_X_UPSTREAM`` 优先，否则取与本仓库同级的 ``ATRI-X``。
UPSTREAM = Path(os.environ.get("ATRI_X_UPSTREAM") or (REPO.parent / "ATRI-X"))

# PORTING.md §4 表格里的一行： | `go/pkg` | `atri_x/x/y.py` | 123 |
MAP_ROW_RE = re.compile(
    r"^\|\s*`(?P<go>[^`]+)`\s*\|\s*`(?P<pymod>[^`]+)`\s*\|\s*(?P<lines>\d+)\s*\|\s*$",
    re.MULTILINE,
)

# 上游测试/非生产目录，不计入覆盖率
EXCLUDED_GO_PREFIXES = ("cmd/",)


@dataclass
class Mapping:
    go_package: str
    py_pattern: str
    go_lines: int

    @property
    def is_package_glob(self) -> bool:
        return "*" in self.py_pattern


def parse_mapping() -> list[Mapping]:
    text = PORTING.read_text(encoding="utf-8")
    out: list[Mapping] = []
    for m in MAP_ROW_RE.finditer(text):
        out.append(
            Mapping(
                go_package=m.group("go").strip(),
                py_pattern=m.group("pymod").strip(),
                go_lines=int(m.group("lines")),
            )
        )
    return out


def expand_braces(pattern: str) -> list[str]:
    """展开 ``{a,b}.py`` 这类花括号候选（只处理单层，够用且不易出错）。"""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[: m.start()] + alt + pattern[m.end():]))
    return out


def resolve(pattern: str) -> list[Path]:
    """把一个映射条目解析成实际存在的文件/目录列表。

    - ``atri_x/app/*.py``      → 该目录下全部 .py（不含 ``__init__.py``）
    - ``atri_x/x/{a,b}.py``    → 花括号展开后的每个文件
    - ``atri_x/foo.py``        → 该文件
    - ``atri_x/foo/``          → 该包目录存在（含 ``__init__.py``）即可
    """
    found: list[Path] = []
    for pat in expand_braces(pattern):
        found.extend(_resolve_one(pat))
    # 去重并保序
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _resolve_one(pattern: str) -> list[Path]:
    if pattern.endswith("/"):
        pkg = REPO / pattern.rstrip("/")
        return [pkg] if (pkg / "__init__.py").is_file() else []
    if "*" in pattern:
        parent = REPO / Path(pattern).parent
        name_glob = Path(pattern).name
        if not parent.is_dir():
            return []
        return sorted(
            p for p in parent.glob(name_glob)
            if p.is_file() and p.name != "__init__.py"
        )
    target = REPO / pattern
    return [target] if target.is_file() else []


def upstream_go_files() -> dict[str, int]:
    """上游每个 Go 生产包的文件数与行数（若上游不可读则返回空）。"""
    stats: dict[str, int] = {}
    if not UPSTREAM.is_dir():
        return stats
    for path in UPSTREAM.rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        rel = path.relative_to(UPSTREAM)
        pkg = str(rel.parent).replace("\\", "/")
        try:
            n = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        stats[pkg] = stats.get(pkg, 0) + n
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="有缺失时退出码 1")
    ap.add_argument("--json", help="把结果写到指定 JSON 文件")
    ap.add_argument("--list-missing", action="store_true", help="只列缺失项")
    ap.add_argument(
        "--summary",
        action="store_true",
        help="只打 ASCII 汇总（避免 Windows 控制台码页把中文变成乱码）",
    )
    ap.add_argument("--top", type=int, default=25, help="--summary 时列出的最大缺失项数")
    args = ap.parse_args(argv)

    mappings = parse_mapping()
    if not mappings:
        print("没有从 PORTING.md 解析出映射表", file=sys.stderr)
        return 2

    go_stats = upstream_go_files()
    rows: list[dict[str, object]] = []
    covered_lines = 0
    total_lines = 0

    for m in mappings:
        found = resolve(m.py_pattern)
        covered = bool(found)
        total_lines += m.go_lines
        if covered:
            covered_lines += m.go_lines
        rows.append(
            {
                "go_package": m.go_package,
                "py_pattern": m.py_pattern,
                "go_lines": m.go_lines,
                "covered": covered,
                "files": [str(p.relative_to(REPO)).replace("\\", "/") for p in found],
                "upstream_go_lines": go_stats.get(m.go_package, 0),
            }
        )

    missing = [r for r in rows if not r["covered"]]
    pct = (covered_lines / total_lines * 100) if total_lines else 0.0

    if args.summary:
        print(
            f"packages={len(rows)} covered={len(rows) - len(missing)} "
            f"missing={len(missing)} weighted={pct:.1f}% "
            f"go_lines={covered_lines}/{total_lines}"
        )
        # 口径说明：这一行数字说明的是"映射表里的模块有对应文件"，不是"实现正确"。
        # 保持 ASCII（--summary 存在的理由就是避开 Windows 控制台码页把中文变乱码）。
        print(
            "scope=file-existence only (PORTING.md 4 mapping: python module file "
            "present); behaviour correctness is NOT measured here"
        )
        for r in sorted(missing, key=lambda x: -int(x["go_lines"]))[: args.top]:
            print(f"MISSING {r['go_lines']:>6} {r['go_package']} -> {r['py_pattern']}")
        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    {
                        "total_packages": len(rows),
                        "covered": len(rows) - len(missing),
                        "missing": len(missing),
                        "weighted_coverage": round(pct, 2),
                        "rows": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return 1 if (missing and args.strict) else 0

    if args.list_missing:
        for r in missing:
            print(f"{r['go_package']}\t{r['py_pattern']}")
        return 1 if (missing and args.strict) else 0

    width = max(len(str(r["go_package"])) for r in rows)
    print(f"{'Go 包'.ljust(width)}  {'状态':<4} {'Go行':>7}  Python 模块")
    print("-" * (width + 40))
    for r in rows:
        mark = "OK" if r["covered"] else "--"
        shown = r["py_pattern"] if not r["covered"] else f"{r['py_pattern']} ({len(r['files'])} 文件)"
        print(f"{str(r['go_package']).ljust(width)}  {mark:<4} {r['go_lines']:>7}  {shown}")

    pct = (covered_lines / total_lines * 100) if total_lines else 0.0
    print()
    print(f"Go 生产包: {len(rows)}  已覆盖: {len(rows) - len(missing)}  缺失: {len(missing)}")
    print(f"按 Go 生产行数加权覆盖: {covered_lines}/{total_lines} = {pct:.1f}%")
    if missing:
        print()
        print("缺失清单（按 Go 行数从多到少）:")
        for r in sorted(missing, key=lambda x: -int(x["go_lines"])):
            print(f"  {r['go_lines']:>6} 行  {r['go_package']}  →  {r['py_pattern']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "total_packages": len(rows),
                    "covered": len(rows) - len(missing),
                    "missing": len(missing),
                    "weighted_coverage": round(pct, 2),
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写出 {args.json}")

    return 1 if (missing and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
