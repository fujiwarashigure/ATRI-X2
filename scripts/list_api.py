#!/usr/bin/env python3
"""列出某个 ``atri_x`` 模块的公开名，并检查指定名字是否存在。

用途：并行开发时反复要确认"对方模块到底导出了哪些名字"，用命令行内联 Python
在 PowerShell 下会被引号剥壳（本机实测），因此固化成脚本。

用法::

    python scripts/list_api.py atri_x.tool
    python scripts/list_api.py atri_x.tool Registry Info RiskAssessment
    python scripts/list_api.py --grep Risk atri_x.tool
    python scripts/list_api.py --module-path atri_x.tool.tool RiskLevel
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module", help="模块名，如 atri_x.tool")
    ap.add_argument("names", nargs="*", help="要检查是否存在的名字")
    ap.add_argument("--grep", help="只列出包含该子串的名字（大小写不敏感）")
    ap.add_argument("--all", action="store_true", help="连下划线开头的也列出来")
    args = ap.parse_args(argv)

    try:
        module = importlib.import_module(args.module)
    except Exception as exc:  # noqa: BLE001
        print(f"IMPORT-FAILED {args.module}: {type(exc).__name__}: {exc}")
        return 2

    public = [
        name
        for name in dir(module)
        if args.all or not name.startswith("_")
    ]
    declared = getattr(module, "__all__", None)
    if declared:
        missing_from_all = sorted(set(public) - set(declared))
        extra_in_all = sorted(set(declared) - set(dir(module)))
        print(f"__all__ = {len(declared)} names")
        if extra_in_all:
            print(f"  __all__ 里声明但不存在的名字: {extra_in_all}")
        if missing_from_all:
            print(f"  dir() 有但未列入 __all__（{len(missing_from_all)} 个，多为子模块）: "
                  f"{missing_from_all[:12]}{' ...' if len(missing_from_all) > 12 else ''}")

    if args.names:
        missing = [n for n in args.names if not hasattr(module, n)]
        present = [n for n in args.names if hasattr(module, n)]
        print(f"PRESENT ({len(present)}): {present}")
        print(f"MISSING ({len(missing)}): {missing}")

    if args.grep:
        needle = args.grep.lower()
        hits = [n for n in public if needle in n.lower()]
        print(f"GREP {args.grep!r} -> {len(hits)} hits")
        for name in hits:
            print(f"  {name}")
        return 0

    if not args.names:
        print(f"{args.module}: {len(public)} public names")
        print("  " + ", ".join(public))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
