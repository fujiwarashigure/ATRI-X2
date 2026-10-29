"""``python -m atri_x.devtools <cmd>``：生成索引的命令行入口。

子命令：

- ``map`` / ``deps`` / ``entrypoints`` / ``test-map`` / ``go-map``：单个产物；
- ``all``：全部产物写进 ``--out``（默认 ``devdocs/generated``）；
- ``metrics``：只打印复杂度指标（JSON 一行一个键）；
- ``--check``：不写文件，重新渲染后与磁盘逐字节比对，有漂移即 exit 1。

退出码：``0`` 一致 / ``1`` 漂移或参数错误 / ``2`` 仓库根不对。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atri_x.devtools import astindex, render

__all__ = ["main"]

#: 子命令 → 产物名。``all`` 与 ``metrics`` 不在这里。
_COMMANDS: dict[str, str] = {
    "map": "module-map.md",
    "deps": "deps.mmd",
    "entrypoints": "entrypoints.md",
    "test-map": "test-map.md",
    "go-map": "go-map.md",
}

_DEFAULT_OUT = "devdocs/generated"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.devtools",
        description="ATRI-X 生成索引：模块地图 / 依赖图 / 入口 / 测试地图 / Go 映射",
    )
    parser.add_argument(
        "command",
        choices=[*_COMMANDS, "all", "metrics"],
        help="要生成（或检查）的产物",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="仓库根目录（默认当前目录）",
    )
    parser.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help=f"产物输出目录（相对仓库根，默认 {_DEFAULT_OUT}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="不写文件，只比对磁盘产物与重新渲染的结果",
    )
    return parser


def _metrics_text(metrics: dict) -> str:
    """指标输出：一行一个 ASCII 键，便于日志与 CI 比对。"""
    lines: list[str] = []
    for key in (
        "modules",
        "lines",
        "packages",
        "domains",
        "edges",
        "cross_domain_edges",
        "long_files",
        "tested_modules",
        "tests",
        "go_mapped",
        "broken",
    ):
        lines.append(f"{key}={metrics[key]}")
    lines.append(f"cross_domain_ratio={metrics['cross_domain_ratio']:.4f}")
    lines.append(f"test_coverage_ratio={metrics['test_coverage_ratio']:.4f}")
    longest_path, longest_lines = metrics["longest"]
    lines.append(f"longest_file={longest_path}:{longest_lines}")
    hub_name, hub_fan_in = metrics["max_fan_in"]
    lines.append(f"max_fan_in={hub_name}:{hub_fan_in}")
    hubs = ",".join(f"{name}:{count}" for name, count in metrics["hubs"])
    lines.append(f"hubs={hubs}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / astindex.PACKAGE_ROOT).is_dir():
        print(f"不是 ATRI-X 仓库根（缺少 {astindex.PACKAGE_ROOT}/）：{root}")
        return 2
    out_dir = root / args.out

    index = astindex.scan(root)
    if index.broken:
        # 语法坏掉的文件不阻断生成（索引其它部分仍然有用），但要显式报出来。
        print("警告：以下文件无法解析，已按空事实收录：")
        for path in index.broken:
            print(f"  - {path}")

    if args.command == "metrics":
        print(_metrics_text(astindex.collect_metrics(index)))
        return 0

    if args.command == "all":
        if args.check:
            problems = render.check_all(index, root, args.out)
            for problem in problems:
                print(f"DRIFT {problem}")
            if problems:
                print(f"索引漂移 {len(problems)} 处（跑 python scripts/gen_index.py 重新生成）")
                return 1
            print(f"索引一致：{len(render.all_artifacts(index, args.out))} 个生成物")
            return 0
        written = render.write_all(index, root, args.out)
        for path in written:
            print(f"写入 {path.relative_to(root).as_posix()}")
        return 0

    name = _COMMANDS[args.command]
    text = render.render_artifacts(index)[name]
    if args.check:
        target = out_dir / name
        if not target.is_file():
            print(f"缺少生成物：{target.relative_to(root).as_posix()}")
            return 1
        actual = target.read_text(encoding="utf-8")
        if actual != text:
            print(f"DRIFT {name}：{render._first_difference(text, actual)}")
            return 1
        print(f"{name} 一致")
        return 0
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"写入 {target.relative_to(root).as_posix()}")
    return 0


def metrics_json(root: Path | str = ".") -> str:
    """便捷函数：给发行脚本 / 测试拿一份 JSON 指标。"""
    index = astindex.scan(Path(root))
    return json.dumps(astindex.collect_metrics(index), ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
