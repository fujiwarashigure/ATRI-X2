#!/usr/bin/env python3
"""``except BaseException`` 的棘轮检查（研究报告 §5.1/§5.3 的 CI 规则）。

背景：全仓有 200+ 处 ``except BaseException``，其中一部分既不 re-raise 也不转换异常，
在 asyncio 下会顺手吞掉 ``CancelledError``（取消传播被静默吃掉）。本脚本统计
"捕获 ``BaseException`` 但处理器体内没有 ``raise``"的位置，并与基线文件
``scripts/except_baseexception_baseline.json`` 比较。

**为什么按位置而不是只按总数**（v1 的漏洞）：总数不变仍然可能是
"删掉一个旧问题、加进一个新问题"——1 换 1 时 ``count`` 完全看不出变化。
所以 v2 的基线是「文件 + 函数 qualname + 处理器指纹」的集合，其中指纹是处理器
源码归一化后的 sha256 前缀（**不含行号**，所以上下移动代码不会误报）：

* 某个位置**新增**或计数**变大** → 退出 1（即使总数与基线相同）；
* 某个基线位置**消失/变小** → 允许（是改进），但会提示可以 ``--update`` 收紧；
  若总数没降下来，说明消失的位置被别处顶替了，上一条会把它抓出来；
* 基线是 v1 旧格式（只有 ``count``）→ 退回按总数比较并提示 ``--update`` 迁移。

用法::

    python scripts/check_except.py              # 与基线比较（新增即非零退出）
    python scripts/check_except.py --list       # 打印每一处位置
    python scripts/check_except.py --update     # 重写基线并打印 diff 摘要
    python scripts/check_except.py --self-test  # 最小自测（含"总数不变但位置换了"）
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "atri_x"
BASELINE = REPO / "scripts" / "except_baseexception_baseline.json"
SELFTEST_DIR = REPO / "tests" / "_tmp_check_except_selftest"

#: 基线格式版本：1 = 只有 count；2 = 位置集合（file + qualname + fingerprint）。
BASELINE_VERSION = 2
NOTE = "except BaseException 且不 raise 的处理器：按「文件+函数+归一化处理器指纹」定位"

_WHITESPACE = re.compile(r"\s+")
_LINE_TEXT_LIMIT = 160


# ---------------------------------------------------------------------------
# AST 扫描
# ---------------------------------------------------------------------------
def _is_base_exception(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if node is None:
        return True  # 裸 except 等价于 except BaseException
    if isinstance(node, ast.Name):
        return node.id == "BaseException"
    if isinstance(node, ast.Attribute):
        return node.attr == "BaseException"
    if isinstance(node, ast.Tuple):
        return any(
            (isinstance(item, ast.Name) and item.id == "BaseException")
            or (isinstance(item, ast.Attribute) and item.attr == "BaseException")
            for item in node.elts
        )
    return False


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(handler))


class _HandlerScope(ast.NodeVisitor):
    """给每个 ``ExceptHandler`` 标注它所在函数/类的 qualname。"""

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.scopes: dict[int, str] = {}

    def _visit_named(self, node) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast 接口
        self._visit_named(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_named(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_named(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        self.scopes[id(node)] = ".".join(self._stack) if self._stack else "<module>"
        self.generic_visit(node)


def _normalize(text: str) -> str:
    """归一化处理器源码：折叠所有空白，去掉缩进/换行带来的噪声。"""
    return _WHITESPACE.sub(" ", text).strip()


def _fingerprint(text: str) -> str:
    """行号无关的处理器指纹（归一化文本的 sha256 前 12 位）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _handler_text(source: str, handler: ast.ExceptHandler) -> str:
    segment = ast.get_source_segment(source, handler)
    if segment is None:  # pragma: no cover - 只有源码不可用时才会走到
        segment = ast.dump(handler, annotate_fields=True, include_attributes=False)
    return _normalize(segment)


def scan(root: Path | str = PACKAGE) -> list[dict]:
    """扫描目录，返回每个违例的定位条目（``file`` 相对仓库根）。

    条目字段：``file`` / ``qualname`` / ``line`` / ``line_text`` / ``fingerprint`` /
    ``count``。无法解析的文件记成 ``qualname="<parse-error>"`` 的条目（同样参与比较）。
    """
    base = Path(root)
    findings: list[dict] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix() if _is_within(path, REPO) else path.as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:  # pragma: no cover - 由 check_syntax 负责
            message = _normalize(f"无法解析: {exc}")
            findings.append(
                {
                    "file": rel,
                    "qualname": "<parse-error>",
                    "line": 0,
                    "line_text": message[:_LINE_TEXT_LIMIT],
                    "fingerprint": _fingerprint(message),
                    "count": 1,
                }
            )
            continue
        scopes = _HandlerScope()
        scopes.visit(tree)
        for handler in ast.walk(tree):
            if isinstance(handler, ast.ExceptHandler) and _is_base_exception(handler):
                if _reraises(handler):
                    continue
                text = _handler_text(source, handler)
                findings.append(
                    {
                        "file": rel,
                        "qualname": scopes.scopes.get(id(handler), "<module>"),
                        "line": handler.lineno,
                        "line_text": text[:_LINE_TEXT_LIMIT],
                        "fingerprint": _fingerprint(text),
                        "count": 1,
                    }
                )
    return findings


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def group(findings: list[dict]) -> list[dict]:
    """把逐处违例聚合成「位置 → 次数」，按 (file, qualname, fingerprint) 稳定排序。"""
    merged: dict[tuple[str, str, str], dict] = {}
    for item in findings:
        key = (item["file"], item["qualname"], item["fingerprint"])
        entry = merged.get(key)
        if entry is None:
            entry = {
                "file": item["file"],
                "qualname": item["qualname"],
                "line_text": item["line_text"],
                "fingerprint": item["fingerprint"],
                "count": 0,
                "lines": [],
            }
            merged[key] = entry
        entry["count"] += int(item.get("count", 1))
        if item.get("line"):
            entry["lines"].append(int(item["line"]))
    return [
        {key: value for key, value in entry.items() if key != "lines"}
        for entry in sorted(merged.values(), key=lambda e: (e["file"], e["qualname"], e["fingerprint"]))
    ]


def _key(entry: dict) -> tuple[str, str, str]:
    return (entry["file"], entry["qualname"], entry["fingerprint"])


def _count_map(entries: list[dict]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        counts[_key(entry)] = counts.get(_key(entry), 0) + int(entry.get("count", 1))
    return counts


def _describe(entry: dict) -> str:
    return f"{entry['file']}::{entry['qualname']} [{entry['fingerprint']}] {entry['line_text']}"


# ---------------------------------------------------------------------------
# 基线与比较
# ---------------------------------------------------------------------------
def load_baseline() -> dict | None:
    """读基线；文件缺失/损坏/类型不对返回 ``None``。"""
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _baseline_entries(data: dict) -> list[dict] | None:
    """取 v2 基线条目；v1（只有 count）或格式不对返回 ``None``。"""
    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        return None
    if version < BASELINE_VERSION:
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        if not all(isinstance(entry.get(key), str) for key in ("file", "qualname", "fingerprint")):
            return None
        cleaned.append(entry)
    return cleaned


def _baseline_total(data: dict) -> int | None:
    count = data.get("count")
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def compare(current: list[dict], baseline: dict, *, out=print) -> int:
    """把当前扫描结果与基线比较，返回退出码（0 = 通过）。

    * v1 基线：按总数比较（兼容旧基线，提示迁移）；
    * v2 基线：新增位置 / 计数变大 → 1；位置消失 → 0 并提示可收紧。
    """
    baseline_entries = _baseline_entries(baseline)
    current_total = sum(int(entry.get("count", 1)) for entry in current)
    baseline_total = _baseline_total(baseline)
    if baseline_total is None and baseline_entries is not None:
        baseline_total = sum(int(entry.get("count", 1)) for entry in baseline_entries)
    if baseline_entries is None:
        if baseline_total is None:
            out(f"FAIL: 基线格式无法识别：{BASELINE.relative_to(REPO).as_posix()}（运行 --update）")
            return 1
        out(
            f"except_baseexception_no_raise={current_total} baseline={baseline_total} "
            f"(v1 旧基线：只比总数)"
        )
        if current_total > baseline_total:
            out("FAIL: 新增了捕获 BaseException 却不 re-raise 的处理器；请 re-raise 或转换异常")
            return 1
        if current_total < baseline_total:
            out("OK（比基线更好）")
        else:
            out("OK")
        out("提示：基线还是 v1（只有 count）；运行 --update 升级为按位置比较，能发现「总数不变但位置换了」")
        return 0

    base_counts = _count_map(baseline_entries)
    cur_counts = _count_map(current)
    grown = [
        (key, cur_counts[key], base_counts.get(key, 0))
        for key in sorted(cur_counts)
        if cur_counts[key] > base_counts.get(key, 0)
    ]
    shrunk_or_gone = sorted(key for key in base_counts if cur_counts.get(key, 0) < base_counts[key])
    out(
        f"except_baseexception_no_raise={current_total} baseline={baseline_total} "
        f"(v2 位置基线：{len(cur_counts)} 个位置 / 基线 {len(base_counts)} 个)"
    )
    if grown:
        out(f"FAIL: {len(grown)} 个位置新增或变多（总数相同也会被抓到）：")
        current_by_key = {_key(entry): entry for entry in current}
        for key, now, before in grown:
            entry = current_by_key.get(
                key,
                {"file": key[0], "qualname": key[1], "line_text": "", "fingerprint": key[2]},
            )
            out(f"  + {_describe(entry)}  （{before} -> {now}）")
        out("请 re-raise 或转换异常；确实要放宽时人工确认后再 --update")
        return 1
    if shrunk_or_gone:
        base_by_key = {_key(entry): entry for entry in baseline_entries}
        out(f"OK（{len(shrunk_or_gone)} 个基线位置已减少或消失，可运行 --update 收紧）：")
        for key in shrunk_or_gone:
            out(f"  - {_describe(base_by_key[key])}  （{base_counts[key]} -> {cur_counts.get(key, 0)}）")
        return 0
    out("OK")
    return 0


def _diff_summary(old_entries: list[dict] | None, new_entries: list[dict]) -> list[str]:
    """--update 的 diff 摘要（新增 / 消失 / 计数变化）。"""
    if old_entries is None:
        return [f"迁移：旧基线没有位置信息，写入 {len(new_entries)} 个位置"]
    old_counts = _count_map(old_entries)
    new_counts = _count_map(new_entries)
    old_by_key = {_key(entry): entry for entry in old_entries}
    new_by_key = {_key(entry): entry for entry in new_entries}
    lines: list[str] = []
    for key in sorted(new_counts):
        if key not in old_counts:
            lines.append(f"  + {_describe(new_by_key[key])}（新增，{new_counts[key]}）")
        elif new_counts[key] > old_counts[key]:
            lines.append(f"  ~ {_describe(new_by_key[key])}（{old_counts[key]} -> {new_counts[key]}）")
    for key in sorted(old_counts):
        if key not in new_counts:
            lines.append(f"  - {_describe(old_by_key[key])}（消失，原 {old_counts[key]}）")
        elif new_counts[key] < old_counts[key]:
            lines.append(f"  ~ {_describe(new_by_key[key])}（{old_counts[key]} -> {new_counts[key]}）")
    if not lines:
        lines.append("  （无变化）")
    return lines


def write_baseline(entries: list[dict]) -> None:
    """把位置集合写成 v2 基线（UTF-8 无 BOM、\\n 换行）。"""
    payload = {
        "version": BASELINE_VERSION,
        "count": sum(int(entry.get("count", 1)) for entry in entries),
        "note": NOTE,
        "entries": entries,
    }
    BASELINE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
SELFTEST_SWALLOW = '''\
def swallow():
    try:
        pass
    except BaseException:
        pass


def rethrow():
    try:
        pass
    except BaseException:
        raise


def converted():
    try:
        pass
    except Exception:
        pass
'''

SELFTEST_MOVED = '''\
def rethrow():
    try:
        pass
    except BaseException:
        raise


def somewhere_else():
    try:
        pass
    except BaseException:
        pass
'''


def self_test(*, out=print) -> int:
    """最小自测：位置检测、总数不变但位置换了、消失后可通过、v1 兼容。"""
    import io

    checks: list[str] = []

    def case(
        name: str, current: list[dict], baseline: dict, expected: int, *, must_contain: str = ""
    ) -> None:
        buffer = io.StringIO()
        code = compare(current, baseline, out=lambda text: buffer.write(str(text) + "\n"))
        text = buffer.getvalue()
        checks.append(name)
        if code != expected:
            raise AssertionError(f"自测失败[{name}]：退出码 {code} != {expected}\n{text}")
        if must_contain and must_contain not in text:
            raise AssertionError(f"自测失败[{name}]：输出缺少 {must_contain!r}\n{text}")

    shutil.rmtree(SELFTEST_DIR, ignore_errors=True)
    SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
    try:
        module = SELFTEST_DIR / "sample_module.py"
        module.write_text(SELFTEST_SWALLOW, encoding="utf-8", newline="\n")
        found = scan(SELFTEST_DIR)
        if len(found) != 1:
            raise AssertionError(f"自测失败[scan]：期望 1 处违例，实际 {len(found)}：{found}")
        if found[0]["qualname"] != "swallow":
            raise AssertionError(f"自测失败[qualname]：{found[0]['qualname']}")
        if "raise" in found[0]["line_text"].split("except", 1)[-1]:
            raise AssertionError(f"自测失败[fingerprint]：把 re-raise 也算进来了：{found[0]}")

        entries = group(found)
        baseline_v2 = {"version": 2, "count": 1, "entries": entries}
        case("unchanged", entries, baseline_v2, 0, must_contain="OK")

        # 位置换了但总数仍是 1：v2 必须抓到，v1 抓不到（这正是本次加固的漏洞）。
        module.write_text(SELFTEST_MOVED, encoding="utf-8", newline="\n")
        moved = group(scan(SELFTEST_DIR))
        if len(moved) != 1:
            raise AssertionError(f"自测失败[moved]：期望 1 处，实际 {len(moved)}")
        case("same-total-new-place", moved, baseline_v2, 1, must_contain="新增或变多")
        case("v1-same-total-misses", moved, {"count": 1}, 0, must_contain="v1")

        # 位置消失：允许通过，但要提示收紧。
        module.write_text(
            "def only_rethrows():\n"
            "    try:\n"
            "        pass\n"
            "    except BaseException:\n"
            "        raise\n",
            encoding="utf-8",
            newline="\n",
        )
        case("disappeared", group(scan(SELFTEST_DIR)), baseline_v2, 0, must_contain="消失")

        # 同一位置变多（重复代码）→ 计数变大也要失败。
        module.write_text(SELFTEST_SWALLOW * 2, encoding="utf-8", newline="\n")
        case("count-grown", group(scan(SELFTEST_DIR)), baseline_v2, 1, must_contain="->")
    finally:
        shutil.rmtree(SELFTEST_DIR, ignore_errors=True)
    out(f"self-test OK（{len(checks)} 项：{', '.join(checks)}）")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="except BaseException 棘轮检查（按位置基线）")
    parser.add_argument("--list", action="store_true", help="列出全部位置")
    parser.add_argument("--update", action="store_true", help="用当前位置集合重写基线并打印 diff")
    parser.add_argument("--self-test", action="store_true", help="跑脚本自带的最小自测")
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            return self_test()
        except AssertionError as exc:
            print(f"FAIL: {exc}")
            return 1

    findings = scan()
    entries = group(findings)
    if args.list:
        for item in findings:
            print(
                f"{item['file']}:{item['line']} {item['qualname']} "
                f"[{item['fingerprint']}] {item['line_text']}"
            )
    if args.update:
        old = load_baseline()
        old_entries = _baseline_entries(old) if isinstance(old, dict) else None
        write_baseline(entries)
        total = sum(int(entry.get("count", 1)) for entry in entries)
        print(
            f"baseline updated: {total} 处 / {len(entries)} 个位置"
            f"（{BASELINE.relative_to(REPO).as_posix()}）"
        )
        print("diff vs 旧基线：")
        for line in _diff_summary(old_entries, entries):
            print(line)
        return 0

    baseline = load_baseline()
    if baseline is None:
        print(f"missing baseline: {BASELINE.relative_to(REPO).as_posix()} (run --update)")
        return 1
    return compare(entries, baseline)


if __name__ == "__main__":
    raise SystemExit(main())
