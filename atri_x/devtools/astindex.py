"""AST 索引：把 ``atri_x/`` 的模块事实与 import 边解析成结构化数据。

**只依赖标准库，而且绝不 import ``atri_x``**：生成索引时不该触发整包导入。

口径（与 ``scripts/check_imports.py`` / ``scripts/gen_stats.py`` 保持一致）：

- 模块 = ``atri_x/**/*.py``，去掉 ``__pycache__``；包根的 ``atri_x/__init__.py``
  不算模块（与 ``check_imports.iter_modules`` 同口径）；
- ``atri_x/a/__init__.py`` 的模块名是 ``atri_x.a``；``atri_x/a/b.py`` 是 ``atri_x.a.b``；
- import 边只统计"指向本仓内部模块"的那些：绝对 import 以 ``atri_x`` 开头，
  相对 import 按 PEP 328 的 ``level`` 解析成绝对名；
- "对应 Go" 映射从模块 docstring 里取第一个 ``internal/...go`` 或 ``cmd/...go`` 路径。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "ModuleInfo",
    "RepoIndex",
    "TestFile",
    "collect_metrics",
    "go_path_from_docstring",
    "iter_module_files",
    "module_name_for",
    "one_level_domain",
    "scan",
    "two_level_domain",
]

#: 包根目录名。
PACKAGE_ROOT = "atri_x"

#: 从 docstring 里抽 Go 路径（``internal/xxx/yyy.go`` / ``cmd/xxx.go``）。
GO_PATH_RE = re.compile(r"(?:internal|cmd)/[A-Za-z0-9_./\-]+\.go")

#: 索引跳过的目录名。
_SKIP_DIRS = frozenset({"__pycache__"})


@dataclass(frozen=True)
class ModuleInfo:
    """一个 Python 模块的静态事实。"""

    name: str
    path: str
    lines: int
    summary: str = ""
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    go_path: str = ""
    is_package: bool = False
    syntax_ok: bool = True
    #: 一级域：``atri_x.tool.builtin.shell`` → ``tool``（根级模块就是模块名本身）。
    domain: str = ""
    #: 二级域：只有第二段确实是**包目录**时才取两段（``tool.builtin``），
    #: 否则退化为一级域（``atri_x.agent.core`` → ``agent``，不是 ``agent.core``）。
    domain2: str = ""


@dataclass(frozen=True)
class TestFile:
    """一个测试文件的静态事实。"""

    path: str
    lines: int
    modules: tuple[str, ...] = ()


@dataclass
class RepoIndex:
    """整仓索引。"""

    root: Path
    modules: tuple[ModuleInfo, ...] = ()
    tests: tuple[TestFile, ...] = ()
    #: 模块名 → 直接 import 该模块的模块名（fan-in）。
    imported_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 模块名 → 直接/间接（前缀）覆盖它的测试文件。
    tests_by_module: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 解析失败的文件（只记路径，不阻断生成）。
    broken: tuple[str, ...] = ()

    def by_name(self) -> dict[str, ModuleInfo]:
        return {module.name: module for module in self.modules}

    def modules_in(self, prefix: str) -> list[ModuleInfo]:
        """``prefix`` 域下的模块（``tool`` 命中 ``atri_x.tool`` 及其子模块）。"""
        head = f"{PACKAGE_ROOT}.{prefix}" if prefix else PACKAGE_ROOT
        return [m for m in self.modules if m.name == head or m.name.startswith(head + ".")]


def module_name_for(path: Path, root: Path) -> str:
    """把文件路径换成模块名；``__init__.py`` 归到包名。"""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def two_level_domain(name: str, packages: frozenset[str] | set[str] | None = None) -> str:
    """模块的二级域。

    ``atri_x/tool/builtin/`` 是包目录，所以 ``atri_x.tool.builtin.shell`` 的二级域是
    ``tool.builtin``；而 ``atri_x/agent/core.py`` 的 ``core`` 是模块不是包，二级域退化
    成 ``agent``。不传 ``packages`` 时按"第二段之后一律不算包"处理（保守）。
    """
    parts = name.split(".")
    if len(parts) <= 1:
        return ""
    if len(parts) >= 3 and packages is not None:
        # 只有当第三段确实是**包目录**时才取两段：``tool.builtin`` 成立，
        # ``agent.core``（core 是模块）不成立。
        if f"{parts[0]}.{parts[1]}.{parts[2]}" in packages:
            return f"{parts[1]}.{parts[2]}"
    return parts[1]


def one_level_domain(name: str) -> str:
    """模块的一级域：``atri_x.tool.builtin.shell`` → ``tool``。"""
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else ""


def iter_module_files(root: Path) -> list[Path]:
    """按路径排序列出 ``root/atri_x`` 下的 ``.py``（跳过 ``__pycache__``）。"""
    pkg = root / PACKAGE_ROOT
    if not pkg.is_dir():
        return []
    out: list[Path] = []
    for path in pkg.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out, key=lambda item: item.as_posix())


def go_path_from_docstring(docstring: str) -> str:
    """从 docstring 里取"对应 Go"的路径；没有则返回空串。"""
    match = GO_PATH_RE.search(docstring or "")
    return match.group(0) if match else ""


def _resolve_relative(module_name: str, is_package: bool, level: int, target: str) -> str:
    """把 ``from ..x import y`` 的 ``level`` 解析成绝对模块名。"""
    if is_package:
        package_parts = module_name.split(".")
    else:
        package_parts = module_name.split(".")[:-1]
    # level=1 表示"当前包"，level=2 表示"上一级"，依此类推。
    drop = level - 1
    if drop > 0:
        package_parts = package_parts[: len(package_parts) - drop]
    if target:
        package_parts = package_parts + target.split(".")
    return ".".join(part for part in package_parts if part)


def _imports_of(tree: ast.AST, module_name: str, is_package: bool) -> tuple[str, ...]:
    """收集一个模块 import 的全部**内部**模块名（去重、稳定排序）。"""
    found: set[str] = set()

    def add(name: str) -> None:
        if name == PACKAGE_ROOT or name.startswith(PACKAGE_ROOT + "."):
            found.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                resolved = _resolve_relative(module_name, is_package, node.level, base)
                add(resolved)
            else:
                add(base)
                # ``from atri_x.tool import tool`` 里的 ``tool`` 是个模块名。
                for alias in node.names:
                    if alias.name != "*":
                        add(f"{base}.{alias.name}" if base else alias.name)
    return tuple(sorted(found))


def _public_symbols(tree: ast.AST) -> tuple[str, ...]:
    """``__all__``（字面量）优先；否则取模块级公开 class/def。"""
    literal: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
            value = node.value
            if isinstance(value, (ast.List, ast.Tuple)):
                for item in value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        literal.append(item.value)
    if literal:
        return tuple(sorted(set(literal)))
    symbols = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]
    return tuple(sorted(set(symbols)))


def _summarize(tree: ast.AST) -> str:
    doc = ast.get_docstring(tree, clean=True) or ""
    for line in doc.splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def _read_module(path: Path, root: Path) -> ModuleInfo | None:
    name = module_name_for(path, root)
    if name == PACKAGE_ROOT:  # 包根不算模块（与 check_imports 同口径）
        return None
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ModuleInfo(name=name, path=path.relative_to(root).as_posix(), lines=0,
                          syntax_ok=False)
    is_package = path.name == "__init__.py"
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return ModuleInfo(
            name=name,
            path=path.relative_to(root).as_posix(),
            lines=len(source.splitlines()),
            is_package=is_package,
            syntax_ok=False,
        )
    doc = ast.get_docstring(tree, clean=True) or ""
    return ModuleInfo(
        name=name,
        path=path.relative_to(root).as_posix(),
        lines=len(source.splitlines()),
        summary=_summarize(tree),
        symbols=_public_symbols(tree),
        imports=_imports_of(tree, name, is_package),
        go_path=go_path_from_docstring(doc),
        is_package=is_package,
    )


def _read_test(path: Path, root: Path) -> TestFile:
    rel = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return TestFile(path=rel, lines=0)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return TestFile(path=rel, lines=len(source.splitlines()))
    return TestFile(
        path=rel,
        lines=len(source.splitlines()),
        modules=_imports_of(tree, path.stem, False),
    )


def _ancestors(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]


def _build_imported_by(modules: tuple[ModuleInfo, ...]) -> dict[str, tuple[str, ...]]:
    known = {module.name for module in modules}
    edges: dict[str, set[str]] = {name: set() for name in known}
    for module in modules:
        for target in module.imports:
            if target in known:
                edges[target].add(module.name)
    return {name: tuple(sorted(sources)) for name, sources in edges.items()}


def _build_tests_by_module(
    modules: tuple[ModuleInfo, ...], tests: tuple[TestFile, ...]
) -> dict[str, tuple[str, ...]]:
    """测试 → 模块反查。

    一个测试 import ``atri_x.tool.builtin.shell`` 时，被"覆盖"的不只是那个模块，
    还包括它的每一级包（``atri_x.tool.builtin`` / ``atri_x.tool`` / ``atri_x``），
    因为 import 子模块必然先执行包的 ``__init__``。
    """
    known = {module.name for module in modules}
    reached: dict[str, set[str]] = {name: set() for name in known}
    for test in tests:
        for imported in test.modules:
            for candidate in _ancestors(imported):
                if candidate in known:
                    reached[candidate].add(test.path)
    return {name: tuple(sorted(paths)) for name, paths in reached.items()}


def _with_domains(module: ModuleInfo, packages: frozenset[str]) -> ModuleInfo:
    """回填 ``domain`` / ``domain2``（需要先知道全部包名，所以放在扫完之后）。"""
    from dataclasses import replace

    return replace(
        module,
        domain=one_level_domain(module.name),
        domain2=two_level_domain(module.name, packages),
    )


def scan(root: Path | str) -> RepoIndex:
    """扫描整仓，返回 :class:`RepoIndex`。"""
    root = Path(root).resolve()
    files = iter_module_files(root)
    modules: list[ModuleInfo] = []
    broken: list[str] = []
    for path in files:
        module = _read_module(path, root)
        if module is None:
            continue
        if not module.syntax_ok:
            broken.append(module.path)
        modules.append(module)
    modules.sort(key=lambda item: item.name)
    packages = frozenset(module.name for module in modules if module.is_package)
    modules = [
        _with_domains(module, packages) for module in modules
    ]
    tests_root = root / "tests"
    test_files: list[TestFile] = []
    if tests_root.is_dir():
        for path in sorted(tests_root.glob("*.py")):
            test_files.append(_read_test(path, root))
    tests = tuple(t for t in test_files if t.path.endswith(".py"))
    module_tuple = tuple(modules)
    return RepoIndex(
        root=root,
        modules=module_tuple,
        tests=tests,
        imported_by=_build_imported_by(module_tuple),
        tests_by_module=_build_tests_by_module(module_tuple, tests),
        broken=tuple(broken),
    )


def _cross_domain(module: ModuleInfo, target: ModuleInfo) -> bool:
    """这条边是否跨一级域（``tool`` 与 ``tool.builtin`` 同属 ``tool``，不算跨）。"""
    return bool(module.domain) and module.domain != target.domain


def collect_metrics(index: RepoIndex, *, long_file_lines: int = 600) -> dict[str, object]:
    """复杂度指标（``scripts/check_docs.py`` 的棘轮与发行说明都用它）。"""
    modules = index.modules
    by_name = index.by_name()
    edges = 0
    cross = 0
    for module in modules:
        for target in module.imports:
            target_module = by_name.get(target)
            if target_module is None:
                continue
            edges += 1
            if _cross_domain(module, target_module):
                cross += 1
    fan_in = sorted(
        ((name, len(sources)) for name, sources in index.imported_by.items()),
        key=lambda item: (-item[1], item[0]),
    )
    long_files = sorted(
        ((module.path, module.lines) for module in modules if module.lines >= long_file_lines),
        key=lambda item: (-item[1], item[0]),
    )
    covered = [m for m in modules if index.tests_by_module.get(m.name)]
    return {
        "modules": len(modules),
        "lines": sum(module.lines for module in modules),
        "packages": sum(1 for module in modules if module.is_package),
        "edges": edges,
        "cross_domain_edges": cross,
        "cross_domain_ratio": (cross / edges) if edges else 0.0,
        "domains": len({module.domain for module in modules if module.domain}),
        "long_files": len(long_files),
        "long_file_lines": long_file_lines,
        "longest": long_files[0] if long_files else ("", 0),
        "max_fan_in": fan_in[0] if fan_in else ("", 0),
        "hubs": fan_in[:7],
        "tested_modules": len(covered),
        "test_coverage_ratio": (len(covered) / len(modules)) if modules else 0.0,
        "go_mapped": sum(1 for module in modules if module.go_path),
        "broken": len(index.broken),
        "tests": len(index.tests),
    }


def test_paths_for(index: RepoIndex, module: str) -> tuple[str, ...]:
    """便捷查询：某模块的测试文件。"""
    return index.tests_by_module.get(module, ())


def flatten(values: Iterable[Iterable[str]]) -> list[str]:
    """把多个可迭代对象拍平成一个列表（渲染里用来汇总）。"""
    out: list[str] = []
    for group in values:
        out.extend(group)
    return out
