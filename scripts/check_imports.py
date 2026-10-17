#!/usr/bin/env python3
"""导入完整性门禁：逐个导入 ``atri_x`` 下的每个模块，并校验 ``__all__``。

用途（对应 PORTING.md §8）：并行移植期间最容易出现的两类破损是

1. **import 期异常** —— 某个模块引用了尚未落地/被改名/被删掉的符号；
2. **``__all__`` 名不副实** —— ``__all__`` 里列了名字，但该名字其实没定义
   （只在 ``from m import *`` 或显式 ``from m import X`` 时才炸，平时静默）。

这两类都不会被语法检查发现，也不会被只覆盖部分模块的单元测试发现。本脚本
把 ``atri_x`` 全量导出一遍并逐个解析 ``__all__``，是"所有模块可导入"这条
验收标准的可执行形式。

用法::

    python scripts/check_imports.py              # 只报告
    python scripts/check_imports.py --strict     # 有失败则退出码 1
    python scripts/check_imports.py --list       # 只列失败项
"""

from __future__ import annotations

import argparse
import importlib
import io
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO / "atri_x"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _safe(value: str) -> str:
    """把文本降级到当前控制台能编码的字符集（Windows GBK 控制台会直接抛异常）。"""
    for encoding in ("utf-8", sys.stdout.encoding or "utf-8", "gbk"):
        try:
            value.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            continue
        return value
    return value.encode("ascii", errors="backslashreplace").decode("ascii")


def iter_modules() -> list[str]:
    """列出 ``atri_x`` 下所有可导入的模块名（跳过 ``__pycache__``）。"""
    names: list[str] = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO)
        parts = list(rel.parts)
        parts[-1] = parts[-1][: -len(".py")]
        if parts[-1] == "__init__":
            parts.pop()
            if not parts:
                continue
        names.append(".".join(parts))
    return names


def import_module(name: str) -> tuple[object | None, str]:
    """导入单个模块，返回 ``(模块, 错误摘要)``。导入期的 stdout/stderr 被吞掉。"""
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            module = importlib.import_module(name)
    except BaseException:  # noqa: BLE001 - 任何导入期异常都要报出来
        lines = traceback.format_exc().strip().splitlines()
        tail = " | ".join(line.strip() for line in lines[-4:])
        return None, tail
    return module, ""


def check_dunder_all(module: object, name: str) -> list[str]:
    """校验 ``__all__`` 里的每个名字都能在该模块上解析到。"""
    exported = getattr(module, "__all__", None)
    if not isinstance(exported, (list, tuple)):
        return []
    problems: list[str] = []
    for entry in exported:
        if not isinstance(entry, str):
            problems.append(f"{name}: __all__ 含非字符串项 {entry!r}")
            continue
        try:
            getattr(module, entry)
        except AttributeError:
            problems.append(f"{name}: __all__ 列出 {entry!r} 但模块里没有该名字")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="有失败则退出码 1")
    ap.add_argument("--list", action="store_true", help="只列失败项")
    args = ap.parse_args(argv)

    modules = iter_modules()
    import_failures: list[tuple[str, str]] = []
    export_failures: list[str] = []
    imported_modules: list[tuple[str, object]] = []

    # 第一遍：全部导入。必须先导完再校验 __all__，否则「__all__ 里列了子包名」
    # 会在子包被导入之前被误判为缺失。
    for name in modules:
        module, error = import_module(name)
        if module is None:
            import_failures.append((name, error))
            continue
        imported_modules.append((name, module))

    # 第二遍：校验 __all__。
    for name, module in imported_modules:
        export_failures.extend(check_dunder_all(module, name))

    imported = len(imported_modules)

    if args.list:
        for name, error in import_failures:
            print(_safe(f"IMPORT {name}: {error}"))
        for problem in export_failures:
            print(_safe(f"EXPORT {problem}"))
        print(
            f"modules={len(modules)} imported={imported} "
            f"import_failures={len(import_failures)} export_failures={len(export_failures)}"
        )
    else:
        print(f"modules={len(modules)}")
        print(f"imported={imported}")
        print(f"import_failures={len(import_failures)}")
        print(f"export_failures={len(export_failures)}")
        for name, error in import_failures:
            print(_safe(f"IMPORT {name}: {error}"))
        for problem in export_failures:
            print(_safe(f"EXPORT {problem}"))

    bad = bool(import_failures or export_failures)
    return 1 if (bad and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
