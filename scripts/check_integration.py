#!/usr/bin/env python3
"""跨模块集成一致性检查。

重写由多个并行 agent 分头完成，最容易出的不是"某个模块跑不通"，而是
**同一个概念在两个模块里各有一份定义**：两边各自都能通过测试，跨模块的
``isinstance`` / ``==`` 却失效。本脚本专门抓这一类问题。

检查项：

1. **唯一出处**：`Actor`/`Policy`/`Role`/`GroupRole`/`RiskLevel` 在
   `atri_x.security` 与 `atri_x.tool.capability`（及其再导出）里是**同一个对象**。
2. **ctx 互操作**：`security.with_actor` 存的值，`tool.capability.actor_from_context`
   能取到，反之亦然（键名契约一致）。
3. **脱敏契约**：`atri_x.redact.redact` 同时提供 `redact_text` / `redact_secrets`。
4. **ID 契约**：`atri_x.storage.ids.new_id()` 是带连字符的小写 UUIDv4。
5. **契约名清单**：`PORTING.md` §3 列出的模块与名字都存在。
6. **协程丢弃**：没有「``async`` 函数被当同步语句调用」（Go 同步方法被移植成
   `async def` 而调用点没改，是第二轮验证里最高产的缺陷形态）。

用法::

    python scripts/check_integration.py            # 报告
    python scripts/check_integration.py --strict   # 有失败则退出码 1
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: PORTING.md §3 的「共享词汇表」：模块 → 必须存在的名字
CORE_CONTRACT: dict[str, tuple[str, ...]] = {
    "atri_x.errors": ("AtriXError", "ErrNotFound", "ErrConflict", "ErrForbidden"),
    "atri_x.llm.segment": (
        "MessageSegment",
        "SegmentType",
        "text_segments",
        "segments_text_only",
        "segments_content_text",
    ),
    "atri_x.llm.llm": ("LLMMessage", "ChatRequest", "StreamChunk", "Usage", "ToolSchema"),
    "atri_x.llm.protocol": ("ProtocolCapabilities",),
    "atri_x.llm.origin": ("Origin",),
    "atri_x.llm.errors": ("APIError",),
    "atri_x.tool.tool": ("Tool", "Info", "Result"),
    # Actor/Policy/RiskLevel 的唯一出处是 atri_x.security（对应 internal/security）
    "atri_x.security": ("Actor", "Policy", "Role", "GroupRole", "RiskLevel"),
    # platform 再导出 delivery 的接收类型，方便两种 import 路径
    "atri_x.platform.platform": ("Receipt", "SentMessage", "PlatformAdapter"),
    "atri_x.storage.storage": ("Session", "Message"),
    "atri_x.delivery.delivery": ("Output", "Receipt", "SentMessage", "Target"),
    "atri_x.command.types": ("Info", "Result"),
}

# delivery 的第三个名字用可选校验（不同实现命名可能不同）
OPTIONAL_NAMES: dict[str, tuple[str, ...]] = {
    "atri_x.delivery.delivery": ("META_HOOK_POINT", "TOOL_PREVIEW_PREFIX"),
}


def _import(module: str) -> tuple[object | None, str]:
    try:
        return importlib.import_module(module), ""
    except Exception as exc:  # noqa: BLE001 - 报告而不是崩掉
        return None, f"{type(exc).__name__}: {exc}"


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> None:
        self.checks.append((ok, name, detail))

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [c for c in self.checks if not c[0]]

    def print(self) -> None:
        width = max((len(c[1]) for c in self.checks), default=10)
        for ok, name, detail in self.checks:
            mark = "OK  " if ok else "FAIL"
            line = f"{mark} {name.ljust(width)}"
            if detail:
                line += f"  {detail}"
            print(line)
        print()
        print(f"共 {len(self.checks)} 项，失败 {len(self.failed)} 项")


def check_single_source(report: Report) -> None:
    """安全词汇必须是同一个类对象（不是"看起来一样的两份"）。"""
    security, err_s = _import("atri_x.security")
    capability, err_c = _import("atri_x.tool.capability")
    if security is None or capability is None:
        report.add(False, "安全词汇唯一出处", f"security={err_s or 'ok'} capability={err_c or 'ok'}")
        return
    for name in ("Actor", "Policy", "Role", "GroupRole", "RiskLevel"):
        a = getattr(security, name, None)
        b = getattr(capability, name, None)
        report.add(
            a is not None and a is b,
            f"security.{name} is capability.{name}",
            "" if a is b else "两份不同定义（isinstance/== 会失效）",
        )
    for name in ("parse_risk", "compare_risk", "actor_id", "default_policy"):
        a = getattr(security, name, None)
        b = getattr(capability, name, None)
        report.add(a is not None and a is b, f"security.{name} is capability.{name}")


def check_ctx_interop(report: Report) -> None:
    """两边写入的 ctx 值必须能互相读到（键名契约）。"""
    security, _ = _import("atri_x.security")
    capability, _ = _import("atri_x.tool.capability")
    if security is None or capability is None:
        report.add(False, "ctx 互操作", "模块不可导入")
        return
    actor = security.Actor(id="cli:local", platform="cli", platform_user_id="local")
    # security 写 → capability 读
    try:
        ctx = capability.with_actor({}, actor)
        got, ok = capability.actor_from_context(ctx)
        report.add(ok and got == actor, "capability 写 → capability 读")
    except Exception as exc:  # noqa: BLE001
        report.add(False, "capability 写 → capability 读", f"{type(exc).__name__}: {exc}")
    # security 写 → capability 读（键必须一致）
    try:
        ctx = security.with_actor({}, actor)
        got, ok = capability.actor_from_context(ctx)
        report.add(ok and got == actor, "security 写 → capability 读")
    except Exception as exc:  # noqa: BLE001
        report.add(False, "security 写 → capability 读", f"{type(exc).__name__}: {exc}")
    # capability 写 → security 读
    try:
        ctx = capability.with_actor({}, actor)
        got, ok = security.actor_from_context(ctx)
        report.add(ok and got == actor, "capability 写 → security 读")
    except Exception as exc:  # noqa: BLE001
        report.add(False, "capability 写 → security 读", f"{type(exc).__name__}: {exc}")
    # 零值语义（Go 返回零值 + false）
    try:
        got, ok = security.actor_from_context(None)
        report.add(
            (not ok) and got == security.Actor(),
            "actor_from_context(None) 返回零值 + false",
            f"got={got!r} ok={ok}",
        )
    except Exception as exc:  # noqa: BLE001
        report.add(False, "actor_from_context(None)", f"{type(exc).__name__}: {exc}")


def check_redact_contract(report: Report) -> None:
    redact, err = _import("atri_x.redact.redact")
    if redact is None:
        report.add(False, "redact 契约", err)
        return
    for name in ("redact_text", "redact_secrets", "secrets"):
        fn = getattr(redact, name, None)
        if fn is None:
            report.add(False, f"redact.{name} 存在")
            continue
        try:
            out = fn("token=abcdef123456")
        except Exception as exc:  # noqa: BLE001
            report.add(False, f"redact.{name} 可单参数调用", f"{type(exc).__name__}: {exc}")
            continue
        report.add(
            isinstance(out, str) and "abcdef123456" not in out,
            f"redact.{name} 可单参数调用且生效",
            "" if "abcdef123456" not in out else f"未脱敏: {out!r}",
        )


def check_id_contract(report: Report) -> None:
    ids, err = _import("atri_x.storage.ids")
    if ids is None:
        report.add(False, "storage.ids 契约", err)
        return
    value = ids.new_id()
    ok = bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
        )
    )
    report.add(ok, "new_id 是带连字符的小写 UUIDv4", value if not ok else "")
    try:
        report.add(uuid.UUID(value).version == 4, "new_id 版本位为 4")
    except Exception as exc:  # noqa: BLE001
        report.add(False, "new_id 版本位为 4", f"{type(exc).__name__}: {exc}")


def check_core_contract(report: Report) -> None:
    for module, names in CORE_CONTRACT.items():
        mod, err = _import(module)
        if mod is None:
            report.add(False, f"{module} 可导入", err)
            continue
        report.add(True, f"{module} 可导入")
        for name in names:
            report.add(hasattr(mod, name), f"{module}.{name} 存在")
    for module, names in OPTIONAL_NAMES.items():
        mod, _ = _import(module)
        if mod is None:
            continue
        for name in names:
            report.add(hasattr(mod, name), f"{module}.{name} 存在（可选）")


def check_error_sentinels(report: Report) -> None:
    """``atri_x.errors`` 的哨兵必须是**异常实例**，且不能与类同名。

    真实事故：文件里先有 ``class ErrQueueFull(AtriXError)``，后面又被
    ``ErrQueueFull = QueueFullError("queue full")`` 覆盖成实例。于是
    ``isinstance(x, ErrQueueFull)`` 抛 ``TypeError: arg 2 must be a type``，
    ``except ErrQueueFull`` 直到运行时才炸 —— 而单看模块导入是完全正常的。
    这里把这类"名字被覆盖"的问题钉死在门禁上。
    """
    errors, err = _import("atri_x.errors")
    if errors is None:
        report.add(False, "errors 哨兵契约", err)
        return

    sentinels = {
        name: value
        for name, value in vars(errors).items()
        if name.startswith("Err") and not name.startswith("__")
    }
    report.add(bool(sentinels), "errors 里有 Err* 哨兵", f"{len(sentinels)} 个")
    bad_type: list[str] = []
    for name, value in sentinels.items():
        if not isinstance(value, BaseException):
            bad_type.append(f"{name}={type(value).__name__}")
        elif type(value) is type:
            # 哨兵位置上放着类本身：不是实例，判等会失效
            bad_type.append(f"{name} 是类而非实例")
    report.add(not bad_type, "每个 Err* 都是异常实例", ", ".join(bad_type))

    # 同名类 + 同名单例 = 后者把前者覆盖成实例
    shadowed: list[str] = []
    for name in sentinels:
        cls = getattr(errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            shadowed.append(name)
    report.add(
        not shadowed,
        "Err* 名字没有被同名类占用后又被实例覆盖",
        ", ".join(shadowed),
    )

    # 哨兵必须能真的被 except 捕获（这是它们唯一的用途）
    unusable: list[str] = []
    for name, value in sentinels.items():
        try:
            raise value
        except type(value):
            pass
        except BaseException as exc:  # noqa: BLE001
            unusable.append(f"{name}: {type(exc).__name__}")
    report.add(not unusable, "每个 Err* 都能被自身类型捕获", ", ".join(unusable))

    # isinstance 必须可用（arg 2 是类型）
    isinstance_broken: list[str] = []
    for name, value in sentinels.items():
        try:
            isinstance(value, type(value))
        except TypeError:
            isinstance_broken.append(name)
    report.add(not isinstance_broken, "对 Err* 调 isinstance 不会抛 TypeError",
               ", ".join(isinstance_broken))


def _is_async_generator(node: ast.AST) -> bool:
    """``async def`` 里含 ``yield`` 的是异步生成器：调用返回异步迭代器，不是协程。"""
    return any(isinstance(sub, (ast.Yield, ast.YieldFrom)) for sub in ast.walk(node))


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _async_discard_hits() -> list[tuple[str, int, str, str]]:
    """静态找出「async 函数被当同步语句调用」的位置。

    只报能解析到具体 async 定义的调用，避免 ``.append`` / ``.write`` 这类同名噪声：

    1. ``from atri_x.x import f`` 且 ``x.f`` 是模块级 ``async def``（排除异步生成器）
       → 裸语句调用 ``f(...)``；
    2. ``self.g(...)`` 且本类里 ``g`` 是 ``async def``（排除异步生成器）→ 裸语句调用。

    ``return f(...)`` 之类**有意**把协程交给上层 await 的写法不算命中（本函数只
    检查裸表达式语句，以及同步上下文里的 ``self.`` 调用）。
    """
    async_funcs: dict[str, set[str]] = {}
    trees: dict[str, ast.Module] = {}
    parents: dict[str, dict[int, ast.AST]] = {}
    for path in sorted((REPO / "atri_x").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        mod = _module_name(path)
        trees[mod] = tree
        pm: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                pm[id(child)] = node
        parents[mod] = pm
        async_funcs[mod] = {
            node.name
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and not _is_async_generator(node)
        }

    hits: list[tuple[str, int, str, str]] = []
    for mod, tree in trees.items():
        pm = parents[mod]
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("atri_x"):
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        class_async: dict[int, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_async[id(node)] = {
                    item.name
                    for item in node.body
                    if isinstance(item, ast.AsyncFunctionDef) and not _is_async_generator(item)
                }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            target = ""
            if isinstance(func, ast.Name) and func.id in imports:
                owner, _, fn = imports[func.id].rpartition(".")
                if fn in async_funcs.get(owner, set()):
                    target = f"{owner}.{fn}"
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                cls = None
                cur: ast.AST | None = node
                for _ in range(60):
                    cur = pm.get(id(cur))
                    if cur is None:
                        break
                    if isinstance(cur, ast.ClassDef):
                        cls = cur
                        break
                if cls is not None and func.attr in class_async.get(id(cls), set()):
                    target = f"self.{func.attr}"
            if target:
                hits.append((mod, node.lineno, target, ast.unparse(node)[:100]))
    return sorted(hits)


def check_async_discard(report: Report) -> None:
    """协程被丢弃：``async`` 函数被当同步语句调用。

    真实事故（第二轮对抗性验证）：Go 的同步方法被移植成 ``async def``，调用点照抄
    Go 的同步形状 —— Python 不报错，只是把协程丢掉，``except Exception`` 也抓不到。
    ``cron_query`` / ``cron_write`` 因此整个失效、delete/disable 还假报成功；CLI
    每轮对话失败；TUI 状态栏永不更新。CI 里钉死这类形状。
    """
    hits = _async_discard_hits()
    detail = "; ".join(f"{mod}:{line} {target}" for mod, line, target, _ in hits[:8])
    report.add(not hits, "没有「async 函数被当同步语句调用」", detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="有失败则退出码 1")
    args = ap.parse_args(argv)

    report = Report()
    check_single_source(report)
    check_ctx_interop(report)
    check_redact_contract(report)
    check_id_contract(report)
    check_error_sentinels(report)
    check_async_discard(report)
    check_core_contract(report)
    report.print()
    return 1 if (report.failed and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
