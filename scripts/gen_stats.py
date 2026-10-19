#!/usr/bin/env python3
"""README 数字的**可复算**来源：只读扫描统计（不跑测试、不改文件）。

为什么需要它：README 里的「651 个 ``.py`` 模块 / ``check_syntax`` 817 文件 /
5121 用例通过」曾经和仓库实际状态漂移（实测 685 文件级模块、861+ 个扫描文件，
用例数在本机甚至复现不出来）。手写的数字一定会再次腐烂，所以把**能静态数清**的
数字交给脚本，README 只粘贴脚本的输出。

用法::

    python scripts/gen_stats.py            # 打印全部统计（默认）
    python scripts/gen_stats.py --update   # 只打印可粘贴进 README 的数字（不写文件）
    python scripts/gen_stats.py --check    # 与 README.md 里声明的数字比对，不一致 exit 1

扫描口径与门禁**共用同一份实现**，避免两处各自漂移：

- 仓库 ``.py`` 文件数直接调 :func:`check_syntax.iter_py_files`
  （``atri_x/`` + ``tests/`` + ``scripts/``，跳过 ``__pycache__``）；
- ``atri_x`` 模块数直接调 :func:`check_imports.iter_modules`
  （``atri_x/**/*.py`` 去掉 ``__pycache__``，根 ``__init__.py`` 不算模块）。

**用例数不在这里统计**：unittest 的用例数只有真跑一遍才知道（参数化、动态生成、
不同 Python 版本的收集行为都会影响它），静态数不出来，所以 README 不再硬编码它——
``--check`` 会把「N 个用例」这类声明判为违规。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO / "atri_x"
TESTS_ROOT = REPO / "tests"
SCRIPTS_ROOT = REPO / "scripts"
README = REPO / "README.md"

#: 让 ``import check_syntax`` / ``import check_imports`` 成立（``scripts/`` 不是包）。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_imports  # noqa: E402  口径：atri_x 下可导入模块
import check_syntax  # noqa: E402  口径：全仓扫描的 .py 文件集合

#: 声明「N 个用例」这类不可复现数字的写法（README 里禁止）。
CASE_COUNT_RE = re.compile(r"\d+\s*个用例")

#: README 里声明数字的位置 → 规则。每条规则给出：名字、正则、取哪几个统计量。
#: 正则只匹配 README 当前的行文；改了行文要同步改这里（见 --check 的报错）。
README_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("atri_x 模块数", re.compile(r"(\d+)\s*个\s*`\.py`\s*模块")),
    ("atri_x 万行", re.compile(r"约\s*(\d+(?:\.\d+)?)\s*万行")),
    ("tests 文件数", re.compile(r"`tests/`\s*(\d+)\s*个 Python 文件（其中\s*(\d+)\s*个")),
    ("受版本控制文件数", re.compile(r"(\d+)\s*个受版本控制文件")),
    ("check_syntax 扫描文件数", re.compile(r"`check_syntax`\s*\|\s*(\d+)\s*个文件")),
    ("check_imports 模块数", re.compile(r"modules=(\d+) imported=(\d+)")),
)

#: 「约 N.N 万行」的容许偏差（行）：四舍五入到 0.1 万 = 1000 行，正常增删不该逼着改 README。
WAN_TOLERANCE_LINES = 1000


def _safe(value: str) -> str:
    """让输出在当前控制台编码下一定可打印（GBK 控制台遇到转义不出的字符会抛异常）。"""
    encoding = sys.stdout.encoding or "ascii"
    try:
        value.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return value.encode("ascii", errors="backslashreplace").decode("ascii")
    return value


def _iter_py_files(root: Path) -> list[Path]:
    """``root`` 下的 ``.py``（跳过 ``__pycache__``，与两个门禁同口径）。"""
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _count_lines(paths: list[Path]) -> int:
    """代码行数（含注释与 docstring，按物理行算）。坏文件跳过而不是崩掉统计。"""
    total = 0
    for path in paths:
        try:
            total += len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
    return total


def _static_counts(paths: list[Path]) -> dict[str, int]:
    """测试目录的静态计数：**不等于用例数**，只是「有多少个测试方法/跳过标记」。

    用例数（``Ran N tests``）受参数化、动态生成、skip 影响，静态数不出来；
    这里给的是可复核的近似指标。
    """
    test_defs = 0
    skip_decorators = 0
    skip_calls = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        test_defs += len(re.findall(r"^\s*def\s+test_\w+", text, re.MULTILINE))
        skip_decorators += len(re.findall(r"unittest\.skip\w*", text))
        skip_calls += len(re.findall(r"\bskipTest\(", text))
    return {
        "test_defs": test_defs,
        "skip_decorators": skip_decorators,
        "skip_calls": skip_calls,
    }


def _tracked_files() -> int | None:
    """``git ls-files`` 计数；没有 git（或不在工作区）时返回 ``None``。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return len([line for line in (out.stdout or "").splitlines() if line.strip()])


def collect() -> dict[str, int | None]:
    """只读扫描，返回全部统计量。"""
    atri_x_files = _iter_py_files(PKG_ROOT)
    tests_files = _iter_py_files(TESTS_ROOT)
    scripts_files = _iter_py_files(SCRIPTS_ROOT)
    stats: dict[str, int | None] = {
        "atri_x_files": len(atri_x_files),
        "atri_x_modules": len(check_imports.iter_modules()),
        "atri_x_lines": _count_lines(atri_x_files),
        "tests_files": len(tests_files),
        "tests_test_files": len([p for p in tests_files if p.name.startswith("test_")]),
        "scripts_files": len(scripts_files),
        "scanned_files": len(check_syntax.iter_py_files()),
        "tracked_files": _tracked_files(),
    }
    stats.update(_static_counts(tests_files))
    return stats


def format_report(stats: dict[str, int | None]) -> list[str]:
    """默认输出：一行一个 ``key=value``（ASCII 键，便于机器比对）。"""
    lines = [
        f"atri_x_files={stats['atri_x_files']}",
        f"atri_x_modules={stats['atri_x_modules']}",
        f"atri_x_lines={stats['atri_x_lines']}",
        f"tests_files={stats['tests_files']}",
        f"tests_test_files={stats['tests_test_files']}",
        f"scripts_files={stats['scripts_files']}",
        f"scanned_files={stats['scanned_files']}",
        f"tracked_files={stats['tracked_files'] if stats['tracked_files'] is not None else 'n/a'}",
        f"test_defs={stats['test_defs']}",
        f"skip_decorators={stats['skip_decorators']}",
        f"skip_calls={stats['skip_calls']}",
    ]
    return [_safe(line) for line in lines]


def format_update(stats: dict[str, int | None]) -> list[str]:
    """``--update``：只打印可粘贴进 README 的文本，**不写任何文件**。"""
    wan = (stats["atri_x_lines"] or 0) / 10000
    tracked = stats["tracked_files"]
    lines = [
        "README 数字（实测，直接粘贴；本模式不修改任何文件）：",
        "",
        "项目速览 · 本仓库行：",
        f"| 本仓库 | 平铺布局的 `atri_x/`：{stats['atri_x_modules']} 个 `.py` 模块，"
        f"约 {wan:.1f} 万行（物理行，含空行/注释/docstring） |",
        "",
        "项目速览 · 测试行：",
        f"| 测试 | `tests/` {stats['tests_files']} 个 Python 文件"
        f"（其中 {stats['tests_test_files']} 个 `test_*.py`）；用例数不硬编码"
        "（见「开发与验证」）；仓库自带四道质量门禁 |",
        "",
        "项目速览 · 规模行：",
        f"| 规模 | {tracked if tracked is not None else '（无 git，跳过）'} "
        "个受版本控制文件（不含 `.git`；`git ls-files` 计数） |",
        "",
        "门禁表：",
        f"| `check_syntax` | {stats['scanned_files']} 个文件，"
        "0 错（`bom=0`、`crlf=0`、`damaged=0`） |",
        f"| `check_imports` | `modules={stats['atri_x_modules']} "
        f"imported={stats['atri_x_modules']}`，0 失败 |",
        "",
        "静态计数（近似指标，**不等于用例数**）：",
        f"- `tests/` 下 `def test_*` 方法：{stats['test_defs']}",
        f"- `unittest.skip*` 装饰器：{stats['skip_decorators']}",
        f"- `skipTest(...)` 调用：{stats['skip_calls']}",
    ]
    return [_safe(line) for line in lines]


def check_readme(stats: dict[str, int | None], text: str) -> list[str]:
    """把 README 里声明的数字与实测值比对，返回差异描述（空列表 = 一致）。"""
    problems: list[str] = []
    seen: set[str] = set()

    def _expect(name: str, declared: object, measured: object, ok: bool, hint: str = "") -> None:
        seen.add(name)
        if not ok:
            suffix = f"（{hint}）" if hint else ""
            problems.append(f"{name}: README 声明 {declared}，实测 {measured}{suffix}")

    for name, pattern in README_RULES:
        match = pattern.search(text)
        if match is None:
            seen.add(name)  # 记进 seen，避免下面再报一次"未参与比对"
            problems.append(f"{name}: README 里找不到声明位置（正则 {pattern.pattern!r} 未命中）")
            continue
        declared = match.group(1)
        if name == "atri_x 模块数":
            measured = stats["atri_x_modules"]
            _expect(name, declared, measured, int(declared) == measured)
        elif name == "atri_x 万行":
            measured_lines = stats["atri_x_lines"] or 0
            declared_lines = int(round(float(declared) * 10000))
            _expect(
                name,
                f"约 {declared} 万行",
                f"{measured_lines} 行",
                abs(declared_lines - measured_lines) <= WAN_TOLERANCE_LINES,
                hint=f"容许偏差 ±{WAN_TOLERANCE_LINES} 行",
            )
        elif name == "tests 文件数":
            _expect("tests 文件数", match.group(1), stats["tests_files"],
                    int(match.group(1)) == stats["tests_files"])
            _expect("tests 中 test_*.py 数", match.group(2), stats["tests_test_files"],
                    int(match.group(2)) == stats["tests_test_files"])
        elif name == "受版本控制文件数":
            seen.add(name)  # 没有 git 就不比对这一条（不算差异）
            if stats["tracked_files"] is None:
                continue
            _expect(name, declared, stats["tracked_files"],
                    int(declared) == stats["tracked_files"])
        elif name == "check_syntax 扫描文件数":
            _expect(name, declared, stats["scanned_files"],
                    int(declared) == stats["scanned_files"])
        elif name == "check_imports 模块数":
            _expect("check_imports imported=", match.group(2), stats["atri_x_modules"],
                    int(match.group(2)) == stats["atri_x_modules"],
                    hint="imported 必须等于 modules")
            _expect(name, declared, stats["atri_x_modules"],
                    int(declared) == stats["atri_x_modules"])

    for match in CASE_COUNT_RE.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        problems.append(
            f"README:{line_no} 出现了不可复现的用例数声明 {match.group(0)!r}；"
            "用例数只有真跑一遍才知道，README 不要硬编码（改为由 CI 记录或现场跑）"
        )

    missing = [name for name, _ in README_RULES if name not in seen]
    for name in missing:
        problems.append(f"{name}: 未参与比对（README 里没有对应声明）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="与 README.md 声明的数字比对，不一致 exit 1")
    ap.add_argument("--update", action="store_true", help="只打印可粘贴的 README 数字（不写文件）")
    args = ap.parse_args(argv)

    stats = collect()
    if args.update:
        for line in format_update(stats):
            print(line)
        return 0
    if args.check:
        for line in format_report(stats):
            print(line)
        if not README.is_file():
            print(_safe(f"README 不存在：{README}"))
            return 2
        problems = check_readme(stats, README.read_text(encoding="utf-8"))
        if problems:
            print("")
            print("README 数字与实测不一致：")
            for item in problems:
                print(_safe(f"  - {item}"))
            print(_safe("（用 python scripts/gen_stats.py --update 拿正确数字）"))
            return 1
        print("README 数字与实测一致：OK")
        return 0
    for line in format_report(stats):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
