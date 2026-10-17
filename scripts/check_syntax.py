#!/usr/bin/env python3
"""全仓 Python 文件语法与编码体检。

并行重写过程中出现过三类真实损坏：

1. **语法错误**：写到一半的文件被提交（``SyntaxError``）。
2. **UTF-8 BOM**：本机 ``pwsh`` 是 Windows PowerShell 5.1，它的
   ``Set-Content -Encoding utf8`` 会写 BOM，Python 报
   ``invalid non-printable character U+FEFF``，整个模块无法导入。
3. **不可逆的字符丢失**：写入管线在目标编码里表达不出某个字符时写下
   ``U+FFFD``，原文永久丢失。另有可逆的 GBK 往返 mojibake（``鍐欏叆`` /
   ``澶瑰叿`` 之类）。

设计要点：mojibake 特征序列 **在运行时推导**，不写死在源码里。
写死过一次的教训：这些"看起来是乱码"的字面量本身会被别的编码修复工具
当成乱码去"修好"，于是检测器把正常的 ``写入`` / ``文件`` 当成了特征，
把 160+ 个健康文件误报成损坏。推导出来的序列不可能被这样改坏。

用法::

    python scripts/check_syntax.py              # 报告
    python scripts/check_syntax.py --strict     # 有问题则退出码 1
    python scripts/check_syntax.py --list       # 只列有问题的文件
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("atri_x", "tests", "scripts")

#: 用来推导 mojibake 特征序列的**正常中文词**。
#: 它们在正常文档里出现频率高，因此一旦文件被 GBK 往返过，命中数会立刻上去。
_PROBE_WORDS = (
    "写入",
    "夹具",
    "文件",
    "测试",
    "这个",
    "一个",
    "推进",
    "显示",
    "设置",
    "时间",
    "的是",
    "函数",
    "路径",
    "同时",
    "缓存",
    "格式",
    "配置",
    "加载",
    "验证",
    "错误",
    "会话",
    "默认",
)


def _mojibake_of(word: str) -> str | None:
    """把正常中文词按「UTF-8 编码 → GBK 解码」变成 mojibake 形态。

    这正是损坏管线做的事，因此推导出的序列与真实损坏完全一致。
    词太短可能无法往返时返回 ``None``（跳过即可）。
    """
    try:
        return word.encode("utf-8").decode("gbk")
    except UnicodeDecodeError:
        return None


MOJIBAKE_SEQUENCES: tuple[str, ...] = tuple(
    value for value in (_mojibake_of(w) for w in _PROBE_WORDS) if value
)

#: **硬损坏指示符**：U+FFFD 替换字符。出现即代表原文已丢失，必须人工改写。
REPLACEMENT_CHAR = "\ufffd"

#: 曾真实截断过字符串字面量的字符：``€``(U+20AC)。GBK 误读 UTF-8 中文时会大量产出它，
#: 但它本身是**合法字符**（例如 ``atri_x/platform/cli`` 有测试专门断言它被转义成
#: ``\u20ac``）。因此按**出现次数**判定（见 ``--threshold``），单个不算损坏；
#: 真正的成片 mojibake 由 ``MOJIBAKE_SEQUENCES`` 负责抓。
SUSPICIOUS_CHARS = ("\u20ac",)

#: 自身豁免：本文件的 docstring 里**故意**写了 mojibake 示例（``鍐欏叆``）与
#: ``€``(U+20AC)，否则这些说明文字会被自己的检测器判成损坏。
SELF_EXEMPT = frozenset({"scripts/check_syntax.py"})


def iter_py_files() -> list[Path]:
    out: list[Path] = []
    for name in SCAN_DIRS:
        root = REPO / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def _safe(value: str) -> str:
    """让输出在当前控制台编码下一定可打印。

    本机控制台是 GBK；报错行里常带 ``€``(U+20AC) 之类字符，直接 print 会把
    "报告一次编码损坏" 变成 "报告器自己崩掉"。
    """
    encoding = sys.stdout.encoding or "ascii"
    try:
        value.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return value.encode("ascii", errors="backslashreplace").decode("ascii")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="有问题则退出码 1")
    ap.add_argument("--list", action="store_true", help="只列有问题的文件")
    ap.add_argument(
        "--threshold", type=int, default=3, help="mojibake 序列命中数阈值（默认 3）"
    )
    ap.add_argument(
        "--fix-eol",
        action="store_true",
        help="把 CRLF 文件就地改写为 LF（UTF-8 无 BOM），然后再扫描一次",
    )
    ap.add_argument(
        "--fix-bom",
        action="store_true",
        help="去掉文件开头的 UTF-8 BOM（停留在 CRLF 时请配合 --fix-eol）",
    )
    args = ap.parse_args(argv)

    if args.fix_bom:
        stripped = 0
        for path in iter_py_files():
            raw = path.read_bytes()
            if not raw.startswith(b"\xef\xbb\xbf"):
                continue
            path.write_bytes(raw[3:])
            stripped += 1
        print(f"bom_stripped={stripped}")

    if args.fix_eol:
        converted = 0
        for path in iter_py_files():
            raw = path.read_bytes()
            if b"\r\n" not in raw:
                continue
            path.write_bytes(raw.replace(b"\r\n", b"\n"))
            converted += 1
        print(f"eol_converted={converted}")

    syntax_errors: list[tuple[Path, str]] = []
    encoding_errors: list[tuple[Path, str]] = []
    bom_files: list[Path] = []
    crlf_files: list[Path] = []
    syntax_warnings: list[tuple[Path, str]] = []
    damaged: list[tuple[Path, int, int, int]] = []
    total = 0

    for path in iter_py_files():
        total += 1
        rel = path.relative_to(REPO)
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            bom_files.append(rel)
        if b"\r\n" in raw:
            crlf_files.append(rel)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            encoding_errors.append((rel, str(exc)))
            continue

        try:
            compile(text, str(path), "exec")
        except SyntaxError as exc:
            syntax_errors.append((rel, f"{exc.msg} (line {exc.lineno})"))

        # ``SyntaxWarning``（docstring 里未转义的反斜杠等）不会让 import 失败，但会在
        # 每次冷启动时往 stderr 喷噪声，而且未来 Python 版本会把它升级成错误。
        # 这里把它升级成门禁项：编译两次以触发全部告警（编译结果会被缓存）。
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(text, str(path), "exec")
        for warning in caught:
            if issubclass(warning.category, SyntaxWarning):
                syntax_warnings.append(
                    (rel, f"{warning.message} (line {warning.lineno})")
                )

        replacement = text.count(REPLACEMENT_CHAR)
        suspicious = sum(text.count(ch) for ch in SUSPICIOUS_CHARS)
        hits = sum(text.count(seq) for seq in MOJIBAKE_SEQUENCES)
        if str(rel).replace("\\", "/") in SELF_EXEMPT:
            continue
        if replacement or suspicious >= args.threshold or hits >= args.threshold:
            damaged.append((rel, replacement, suspicious, hits))

    if args.list:
        for rel, replacement, suspicious, hits in damaged:
            print(
                _safe(
                    f"DAMAGED {rel}: u+fffd={replacement} suspicious={suspicious} "
                    f"mojibake_hits={hits}"
                )
            )
        for rel, message in syntax_warnings:
            print(_safe(f"WARNING {rel}: {message}"))
        print(
            f"scanned={total} damaged={len(damaged)} "
            f"syntax_errors={len(syntax_errors)} bom={len(bom_files)} "
            f"crlf={len(crlf_files)} syntax_warnings={len(syntax_warnings)}"
        )
        bad = bool(
            syntax_errors or encoding_errors or bom_files or damaged or syntax_warnings
        )
        return 1 if (bad and args.strict) else 0

    print(f"scanned={total}")
    print(f"syntax_errors={len(syntax_errors)}")
    print(f"encoding_errors={len(encoding_errors)}")
    print(f"bom={len(bom_files)} crlf={len(crlf_files)}")
    print(f"syntax_warnings={len(syntax_warnings)}")
    print(f"damaged_or_suspect={len(damaged)}")
    for rel, message in syntax_errors:
        print(_safe(f"SYNTAX   {rel}: {message}"))
    for rel, message in encoding_errors:
        print(_safe(f"ENCODING {rel}: {message}"))
    for rel in bom_files:
        print(_safe(f"BOM      {rel}"))
    for rel in crlf_files:
        print(_safe(f"CRLF     {rel}"))
    for rel, message in syntax_warnings:
        print(_safe(f"WARNING  {rel}: {message}"))
    for rel, replacement, suspicious, hits in damaged:
        print(
            _safe(
                f"DAMAGED  {rel}: u+fffd={replacement} suspicious={suspicious} "
                f"mojibake_hits={hits}"
            )
        )

    bad = bool(
        syntax_errors or encoding_errors or bom_files or damaged or syntax_warnings
    )
    return 1 if (bad and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
