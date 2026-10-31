#!/usr/bin/env python3
"""把全量单测按「模块实测耗时」均分成 N 片，供 CI 并行执行。

为什么需要它：`python -m unittest discover -s tests -t . -v` 在 GitHub 托管 runner 上要跑
**45 分钟以上**，而且反复出现「The hosted runner lost communication with the server」
（步骤永远停在 in_progress、job 却已经结束），把质量门禁整条拖红——这不是用例失败，是
长任务把 runner 拖失联。拆成 6 片并行跑，单片约 6–10 分钟，既缩短墙钟时间，也避免单个
job 跑太久。

划分是**确定性**的：只依赖 `tests/test_*.py` 的集合、每个模块的用例数与
``scripts/ci_test_weights.json`` 里的实测耗时（可缺省，缺省时按用例数估）。各片自己算出
同一份划分，不需要中心调度、不需要中间产物；加/删测试模块不会漏片。

注意：光按用例数分片**并不均衡**——`test_gui_tabs` 这类要反复建 Tk 根的模块单条用例就比
纯计算用例慢一个量级。所以权重用「逐模块实测秒数」（``--measure`` 在本机跑一遍生成，提交
进仓库、review 可见），新模块缺项时按 ``用例数 × 0.05s`` 估，仍能保证覆盖与大致均衡。

用法::

    python scripts/ci_shard_tests.py --shard 1/6     # 跑第 1 片（共 6 片）
    python scripts/ci_shard_tests.py --plan          # 只打印分片计划（覆盖 + 均衡自检）
    python scripts/ci_shard_tests.py --measure       # 逐模块实测耗时（打印 JSON，不动文件）
    python scripts/ci_shard_tests.py --measure --write   # 实测并写入权重文件（人工 review 后提交）
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
WEIGHTS_FILE = REPO / "scripts" / "ci_test_weights.json"

#: 权重文件缺项时按「用例数 × 该值」估算秒数（只用于分片均衡，不参与判定）。
FALLBACK_SECONDS_PER_CASE = 0.05


def test_modules() -> list[str]:
    """``tests/`` 下全部测试模块名（不含 ``tests.`` 前缀），已排序。"""
    if not TESTS.is_dir():
        raise SystemExit(f"找不到测试目录：{TESTS}")
    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def case_counts(modules: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """每个模块的用例数；导入即失败的模块记 1 个用例并返回错误，保证它仍会被分到某片里失败。"""
    loader = unittest.TestLoader()
    counts: dict[str, int] = {}
    broken: dict[str, str] = {}
    for name in modules:
        try:
            counts[name] = loader.loadTestsFromName(f"tests.{name}").countTestCases()
        except Exception as exc:  # noqa: BLE001 - 收集后交给运行阶段原样报错
            counts[name] = 1
            broken[name] = f"{type(exc).__name__}: {exc}"
    return counts, broken


def load_weights() -> dict[str, float]:
    """读 ``scripts/ci_test_weights.json``（缺失 / 损坏时返回空表，退回用例数估算）。"""
    try:
        payload = json.loads(WEIGHTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = payload.get("seconds") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    weights: dict[str, float] = {}
    for key, value in raw.items():
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            weights[str(key)] = seconds
    return weights


@dataclass(frozen=True)
class ShardPlan:
    buckets: list[list[str]]
    cases: list[int]
    seconds: list[float]
    counts: dict[str, int]
    broken: dict[str, str]
    weights: dict[str, float]


def plan(modules: list[str], shards: int) -> ShardPlan:
    """贪心均分：按实测耗时从大到小，放进当前预计耗时最少的那片（同分取编号小的）。"""
    counts, broken = case_counts(modules)
    known = load_weights()
    weights = {
        name: known.get(name) or counts[name] * FALLBACK_SECONDS_PER_CASE for name in modules
    }
    buckets: list[list[str]] = [[] for _ in range(shards)]
    cases = [0] * shards
    seconds = [0.0] * shards
    for name in sorted(modules, key=lambda item: (-weights[item], item)):
        index = min(range(shards), key=lambda col: (seconds[col], col))
        buckets[index].append(name)
        cases[index] += counts[name]
        seconds[index] += weights[name]
    return ShardPlan(
        buckets=[sorted(bucket) for bucket in buckets],
        cases=cases,
        seconds=[round(value, 1) for value in seconds],
        counts=counts,
        broken=broken,
        weights=weights,
    )


def _failed_module(name: str, exc: Exception) -> unittest.FunctionTestCase:
    """把「模块导入失败」变成一个必定失败的用例，让它在片内正常报红。"""

    def _raise() -> None:
        raise exc

    _raise.__name__ = name
    return unittest.FunctionTestCase(_raise, description=f"tests.{name}（导入失败）")


def run_shard(index: int, shards: int) -> int:
    modules = test_modules()
    planned = plan(modules, shards)
    names = planned.buckets[index]

    print(
        f"=== shard {index + 1}/{shards}: modules={len(names)} cases={planned.cases[index]} "
        f"预计 {planned.seconds[index]:.0f}s（全量 {len(modules)} 模块 / {sum(planned.cases)} 用例 / "
        f"预计 {sum(planned.seconds):.0f}s）===",
        flush=True,
    )
    print(
        "模块：" + "、".join(f"{name}({planned.counts[name]})" for name in names),
        flush=True,
    )
    for name in names:
        if name in planned.broken:
            print(f"!! 模块导入失败 {name}: {planned.broken[name]}", flush=True)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        try:
            suite.addTests(loader.loadTestsFromName(f"tests.{name}"))
        except Exception as exc:  # noqa: BLE001 - 同上：让失败可见而不是静默跳过
            print(f"!! 加载失败 {name}: {type(exc).__name__}: {exc}", flush=True)
            suite.addTest(_failed_module(name, exc))

    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    elapsed = time.perf_counter() - started
    print(
        f"=== shard {index + 1}/{shards} 结束：{result.testsRun} 用例，"
        f"失败 {len(result.failures)} / 错误 {len(result.errors)} / 跳过 {len(result.skipped)}，"
        f"实际 {elapsed:.0f}s ===",
        flush=True,
    )
    return 0 if result.wasSuccessful() else 1


def print_plan(shards: int) -> int:
    modules = test_modules()
    if shards < 1:
        raise SystemExit("--shards 至少为 1")
    planned = plan(modules, shards)
    assigned = [name for bucket in planned.buckets for name in bucket]
    missing = sorted(set(modules) - set(assigned))
    print(
        f"用例总数={sum(planned.cases)} 模块数={len(modules)} 导入失败={len(planned.broken)} "
        f"有实测权重的模块={len([name for name in modules if name in load_weights()])}"
    )
    for index, bucket in enumerate(planned.buckets):
        print(
            f"  shard {index + 1}/{shards}: modules={len(bucket):<3} "
            f"cases={planned.cases[index]:<5} 预计 {planned.seconds[index]:.0f}s"
        )
    problems: list[str] = []
    if sorted(assigned) != modules:
        problems.append(f"分片没有恰好覆盖全部测试模块（遗漏 {missing[:5]}）")
    if sum(planned.cases) != sum(planned.counts.values()):
        problems.append("分片用例数之和与全量用例数不一致")
    if problems:
        for item in problems:
            print(f"!! {item}")
        return 1
    mean = sum(planned.seconds) / shards
    spread = max(planned.seconds) / mean if mean else 1.0
    heaviest = max(planned.weights.items(), key=lambda item: (item[1], item[0]))
    print(f"覆盖自检：OK；预计耗时最大片/均值 = {spread:.2f}x")
    print(
        f"最长单元：{heaviest[0]} {heaviest[1]:.1f}s（模块是最小分片单元；"
        "headless runner 上 Tk 用例会按 `skipTest` 跳过，那类片实际更轻）"
    )
    return 0


def measure_modules() -> dict[str, float]:
    """逐模块真跑一遍并计时（含导入/加载），返回 ``模块 → 秒``。"""
    modules = test_modules()
    loader = unittest.TestLoader()
    seconds: dict[str, float] = {}
    broken = 0
    for index, name in enumerate(modules, start=1):
        started = time.perf_counter()
        try:
            suite = loader.loadTestsFromName(f"tests.{name}")
            runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = runner.run(suite)
            if not result.wasSuccessful():
                broken += 1
                print(f"!! {name}: 实测中失败 {len(result.failures)} / 错误 {len(result.errors)}",
                      file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - 实测不因单模块导入失败中断
            broken += 1
            print(f"!! {name}: 导入失败 {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        seconds[name] = round(time.perf_counter() - started, 3)
        print(f"[{index:>3}/{len(modules)}] {name}: {seconds[name]:.2f}s",
              file=sys.stderr, flush=True)
    print(f"实测完成：{len(seconds)} 模块，失败/导入失败 {broken}", file=sys.stderr, flush=True)
    return seconds


def write_weights(seconds: dict[str, float]) -> None:
    payload = {
        "version": 1,
        "generated_by": "python scripts/ci_shard_tests.py --measure --write",
        "note": (
            "逐模块实测秒数（本机 Windows x64 / Python 3.14.7，temporary 无并行负载），"
            "只用于 CI 分片均衡；新模块缺项时按「用例数×0.05s」估，不影响覆盖与判定。"
        ),
        "seconds": dict(sorted(seconds.items())),
    }
    WEIGHTS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"权重已写入 {WEIGHTS_FILE}（{len(seconds)} 个模块）", flush=True)


def _parse_shard(text: str) -> tuple[int, int]:
    try:
        left, right = text.split("/", 1)
        index, shards = int(left) - 1, int(right)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--shard 形如 1/6") from exc
    if shards < 1 or not 0 <= index < shards:
        raise argparse.ArgumentTypeError(f"--shard {text} 越界")
    return index, shards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=_parse_shard, help="要跑的片，形如 1/6")
    parser.add_argument("--plan", action="store_true", help="只打印分片计划与覆盖自检")
    parser.add_argument("--shards", type=int, default=6, help="--plan 时的片数（默认 6）")
    parser.add_argument("--measure", action="store_true", help="逐模块实测耗时并打印 JSON")
    parser.add_argument("--write", action="store_true", help="配合 --measure：写入权重文件")
    args = parser.parse_args(argv)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import os

    os.chdir(REPO)  # 与 CI 在仓库根执行 `python -m unittest` 的口径一致
    if args.measure:
        seconds = measure_modules()
        if args.write:
            write_weights(seconds)
        else:
            print(json.dumps(dict(sorted(seconds.items())), ensure_ascii=False, indent=2))
        return 0
    if args.plan:
        return print_plan(args.shards)
    if args.shard is None:
        parser.error("要么给 --shard i/n，要么给 --plan / --measure")
    index, shards = args.shard
    return run_shard(index, shards)


if __name__ == "__main__":
    raise SystemExit(main())
