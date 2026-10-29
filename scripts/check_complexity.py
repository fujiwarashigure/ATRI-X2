#!/usr/bin/env python3
"""复杂度预算门禁：结构指标**只降不升**。

参照 ``scripts/check_except.py`` 的 baseline 棘轮机制（项目里已验证过的模式）：
把当前结构指标冻结成预算，之后每次改动都只能"不高于预算"。要放宽必须改
``scripts/complexity_budget.json`` —— 这是一次**在 review 里看得见**的动作，
而不是悄悄漂移。

预算项（全部可由 ``atri_x/devtools`` 从 AST 算出，不依赖 git / 不跑测试）：

===============  ==================================================
``modules``      模块数（目标 777 → ~650：合并微型模块，非强制）
``long_files``   超过 ``long_file_lines`` 行的文件数（目标 → ≤10）
``max_file_lines`` 最长文件的行数（目标 → ≤600）
``cross_domain_edges`` 跨一级域的 import 边数（封装边界是否在收敛）
``max_fan_in``   单模块最大 fan-in（枢纽是否在变胖）
``untested_modules`` 没有被测试触达的模块数（目标 → 越少越好）
``boundary_violations`` 跨域绕开枢纽 facade 的次数（见 ``devtools/boundaries``）
===============  ==================================================

用法::

    python scripts/check_complexity.py           # 与预算比对，超预算 exit 1
    python scripts/check_complexity.py --list    # 打印全部指标
    python scripts/check_complexity.py --update  # 重新冻结预算（人工 review 后提交）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUDGET = REPO / "scripts" / "complexity_budget.json"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from atri_x.devtools import boundaries, collect_metrics, scan  # noqa: E402

#: 指标名 → "越小越好"（当前全部都是越小越好）。
BUDGET_KEYS: tuple[str, ...] = (
    "modules",
    "long_files",
    "max_file_lines",
    "cross_domain_edges",
    "max_fan_in",
    "untested_modules",
    "boundary_violations",
)


def measure(root: Path = REPO) -> dict[str, int]:
    """算出当前全部预算指标。"""
    index = scan(root)
    metrics = collect_metrics(index)
    longest_path, longest_lines = metrics["longest"]
    _hub, hub_fan_in = metrics["max_fan_in"]
    violations = boundaries.find_violations(index)
    return {
        "modules": int(metrics["modules"]),
        "long_files": int(metrics["long_files"]),
        "max_file_lines": int(longest_lines),
        "cross_domain_edges": int(metrics["cross_domain_edges"]),
        "max_fan_in": int(hub_fan_in),
        "untested_modules": int(metrics["modules"]) - int(metrics["tested_modules"]),
        "boundary_violations": len(violations),
        # 方便人工核对（不参与判定）
        "_longest_file": longest_path,
        "_hub": _hub,
    }


def load_budget() -> dict:
    if not BUDGET.is_file():
        return {}
    try:
        return json.loads(BUDGET.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check(budget: dict, measured: dict[str, int]) -> list[str]:
    problems: list[str] = []
    frozen = budget.get("budget") or {}
    if not frozen:
        return [
            "scripts/complexity_budget.json 不存在或没有 budget 段"
            "（跑 python scripts/check_complexity.py --update 冻结当前值）"
        ]
    for key in BUDGET_KEYS:
        if key not in frozen:
            problems.append(f"预算缺少 {key}（用 --update 补齐）")
            continue
        limit = frozen[key]
        current = measured[key]
        if not isinstance(limit, int):
            problems.append(f"预算 {key} 不是整数：{limit!r}")
            continue
        if current > limit:
            problems.append(
                f"{key}: 当前 {current} > 预算 {limit}（结构指标只降不升；"
                "要么修，要么在 review 里显式放宽预算）"
            )
    return problems


def update(measured: dict[str, int]) -> None:
    payload = {
        "version": 1,
        "generated_by": "python scripts/check_complexity.py --update",
        "note": (
            "结构指标棘轮：只降不升。放宽预算必须改这个文件（review 可见）。"
            "口径见 atri_x/devtools/astindex.py 与 boundaries.py。"
        ),
        "long_file_lines": 600,
        "budget": {key: measured[key] for key in BUDGET_KEYS},
        "context": {
            "longest_file": measured["_longest_file"],
            "top_hub": measured["_hub"],
        },
    }
    BUDGET.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO), help="仓库根目录")
    parser.add_argument("--update", action="store_true", help="重新冻结预算")
    parser.add_argument("--list", action="store_true", help="打印全部指标")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    measured = measure(root)
    budget = load_budget()
    frozen = budget.get("budget") or {}

    if args.list:
        for key in BUDGET_KEYS:
            limit = frozen.get(key, "—")
            marker = "OK"
            if isinstance(limit, int) and measured[key] > limit:
                marker = "FAIL"
            print(f"{key:<22} current={measured[key]:<8} budget={limit:<8} {marker}")
        print(f"{'(longest file)':<22} {measured['_longest_file']}")
        print(f"{'(top hub)':<22} {measured['_hub']}")
        return 0

    if args.update:
        update(measured)
        print(f"已冻结预算：{BUDGET.name}（diff 只应该变紧，不该变松）")
        return 0

    problems = check(budget, measured)
    for key in BUDGET_KEYS:
        limit = frozen.get(key, "—")
        print(f"{key:<22} current={measured[key]:<8} budget={limit}")
    if problems:
        print("")
        print(f"复杂度预算超支 {len(problems)} 项：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("")
    print("复杂度预算内：OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
