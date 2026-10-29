"""把 :class:`atri_x.devtools.astindex.RepoIndex` 渲染成可提交的索引产物。

产物清单（全部写进 ``devdocs/generated/``，带"禁止手改"头）：

``module-map.md``
    777+ 行表：模块 → 一句话职责 → 公共符号 → 行数 → 测试文件。
``deps.mmd``
    Mermaid 域依赖图（按二级域聚合）+ ``%%`` 注释里的域依赖矩阵。
``entrypoints.md``
    进程入口 / 装配层 / 命令 / 工具 / 平台适配器 / Hook / GUI 页 / 任务入口 /
    文档锚点（locator）。
``test-map.md``
    模块 ↔ 测试双向表，外加"没有任何测试直接覆盖"的模块清单。
``go-map.md``
    模块 → 上游 Go 文件，并校验 ``PORTING.md`` §4 的映射表还成立。

渲染只做字符串拼接，不用模板引擎：项目约定零第三方运行时依赖，开发期也不该引。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from atri_x.devtools.astindex import RepoIndex, collect_metrics

__all__ = [
    "ARTIFACTS",
    "HEADER",
    "TASK_ENTRIES",
    "TaskEntry",
    "all_artifacts",
    "check_all",
    "check_artifacts",
    "render_artifacts",
    "render_deps",
    "render_entrypoints",
    "render_go_map",
    "render_module_map",
    "render_test_map",
    "write_all",
    "write_artifacts",
]

#: 生成物的固定头部：**禁止手改**，漂移由 ``scripts/gen_index.py --check`` 判定。
HEADER = (
    "<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->\n"
    "<!-- 改动请落在 `atri_x/devtools/render.py` 或代码本身，"
    "然后重新生成；手改会被 `--check` 判为漂移。 -->\n"
)

#: 产物名 → 渲染函数。``.mmd`` 也是产物之一（Mermaid 域依赖图）。
ARTIFACTS: dict[str, Callable[[RepoIndex], str]] = {
    "module-map.md": lambda index: render_module_map(index),
    "deps.mmd": lambda index: render_deps(index),
    "entrypoints.md": lambda index: render_entrypoints(index),
    "test-map.md": lambda index: render_test_map(index),
    "go-map.md": lambda index: render_go_map(index),
}

from atri_x.devtools.markdown import (
    HEADER,
    artifact as _artifact,
    cell as _cell,
    code as _code,
)
from atri_x.devtools.entrypoints import (
    TASK_ENTRIES,
    TaskEntry,
    render_entrypoints,
)


# --- module-map ------------------------------------------------------------


def render_module_map(index: RepoIndex) -> str:
    rows = [
        "| 模块 | 一句话职责 | 公共符号 | 行数 | 测试文件 |",
        "|---|---|---|---:|---|",
    ]
    for module in index.modules:
        tests = index.tests_by_module.get(module.name, ())
        test_cell = "、".join(f"`{Path(path).name}`" for path in tests) if tests else "—"
        symbols = ", ".join(module.symbols[:8]) or "—"
        rows.append(
            f"| `{module.name}` | {_cell(module.summary)} | {_cell(symbols, 60)} "
            f"| {module.lines} | {test_cell} |"
        )
    metrics = collect_metrics(index)
    summary = (
        f"共 **{metrics['modules']}** 个模块 / **{metrics['lines']}** 物理行 / "
        f"**{metrics['domains']}** 个一级域。"
        f"被测试直接覆盖 **{metrics['tested_modules']}** 个"
        f"（{metrics['test_coverage_ratio'] * 100:.1f}%）。\n\n"
        "查模块：`rg -n \"atri_x\\.tool\\.builtin\" devdocs/generated/module-map.md`。"
    )
    return _artifact("模块索引（自动生成）", summary + "\n\n" + "\n".join(rows))


# --- deps.mmd --------------------------------------------------------------


def _domain_edges(index: RepoIndex) -> dict[tuple[str, str], int]:
    known = {module.name for module in index.modules}
    by_name = index.by_name()
    counts: dict[tuple[str, str], int] = {}
    for module in index.modules:
        for target in module.imports:
            if target not in known:
                continue
            target_module = by_name[target]
            source_domain = module.domain2
            target_domain = target_module.domain2
            if not source_domain or not target_domain:
                continue
            key = (source_domain, target_domain)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _mermaid_id(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", domain)


def render_deps(index: RepoIndex) -> str:
    counts = _domain_edges(index)
    domains = sorted({key[0] for key in counts} | {key[1] for key in counts})
    lines = ["graph LR"]
    for domain in domains:
        lines.append(f'  {_mermaid_id(domain)}["{domain}"]')
    for (source, target), count in sorted(counts.items()):
        if source == target:
            continue
        if count < 2:  # 噪音边不画，矩阵里仍然完整
            continue
        lines.append(f"  {_mermaid_id(source)} -->|{count}| {_mermaid_id(target)}")
    lines.append("  %% 只画 >=2 条的边；完整矩阵见下方注释与 module-map.md")
    matrix: list[str] = ["%%", "%% 域依赖矩阵（行 = 源，列 = 目标，值 = import 条数）", "%%"]
    header = "%% " + " | ".join(["源\\目标", *domains])
    matrix.append(header)
    for source in domains:
        cells = [str(counts.get((source, target), 0)) for target in domains]
        matrix.append("%% " + " | ".join([source, *cells]))
    metrics = collect_metrics(index)
    summary = [
        "%% ATRI-X 域依赖图（自动生成，禁止手改）",
        f"%% 模块 {metrics['modules']} 个 / 内部 import 边 {metrics['edges']} 条 / "
        f"跨一级域边占比 {metrics['cross_domain_ratio'] * 100:.1f}%",
        "%% 渲染：```mermaid " + " ... " + "```（去掉本行说明）",
        "",
    ]
    return "\n".join(summary + lines + [""] + matrix) + "\n"


# --- test-map --------------------------------------------------------------


def _untested_by_domain(index: RepoIndex) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for module in index.modules:
        if index.tests_by_module.get(module.name):
            continue
        groups.setdefault(module.domain or "(root)", []).append(module.name)
    return groups


def render_test_map(index: RepoIndex) -> str:
    metrics = collect_metrics(index)
    lines = [
        HEADER,
        "# 测试地图（自动生成）",
        "",
        f"- 模块 **{metrics['modules']}** 个，其中被测试直接/间接覆盖 "
        f"**{metrics['tested_modules']}** 个"
        f"（{metrics['test_coverage_ratio'] * 100:.1f}%）；"
        f"无覆盖 **{metrics['modules'] - metrics['tested_modules']}** 个。",
        f"- 测试文件 **{metrics['tests']}** 个。",
        "",
        "口径：测试文件 import 了某模块（或其子模块）就算覆盖该模块；"
        "「无覆盖」不代表没测过，但**改那个模块时没有同名测试可跑**，只能靠调用方测试。",
        "",
        "## 模块 → 测试",
        "",
        "| 模块 | 测试文件 |",
        "|---|---|",
    ]
    for module in index.modules:
        tests = index.tests_by_module.get(module.name)
        if not tests:
            continue
        lines.append(
            f"| `{module.name}` | " + "、".join(f"`{path}`" for path in tests) + " |"
        )
    lines.append("")
    lines.append("## 无直接测试覆盖的模块（按一级域）")
    lines.append("")
    for domain in sorted(_untested_by_domain(index)):
        names = _untested_by_domain(index)[domain]
        lines.append(f"### {domain}（{len(names)}）")
        lines.append("")
        lines.append("、".join(f"`{name}`" for name in names))
        lines.append("")
    return "\n".join(lines) + "\n"


# --- go-map ----------------------------------------------------------------


def _porting_rows(index: RepoIndex) -> list[tuple[str, str, str]]:
    """解析 ``PORTING.md`` §4 的三列表格：``(Go 包, Python 模块, 状态)``。"""
    path = index.root / "PORTING.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[tuple[str, str, str]] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## 4.")
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0].startswith("---") or cells[0] == "Go 包":
            continue
        go_pkg = cells[0].strip("`")
        python_cell = cells[1]
        rows.append((go_pkg, python_cell, _check_porting_row(index, python_cell)))
    return rows


def _check_porting_row(index: RepoIndex, python_cell: str) -> str:
    """校验映射行描述的 Python 模块是否真的存在（``*`` 按前缀匹配）。"""
    names = {module.name for module in index.modules}
    paths = {module.path for module in index.modules}
    candidates = re.findall(r"`([^`]+)`", python_cell)
    if not candidates:
        return "—"
    missing: list[str] = []
    for candidate in candidates:
        if candidate.endswith("*"):
            prefix = candidate[:-1]
            if not any(path.startswith(prefix) for path in paths):
                missing.append(candidate)
        elif candidate.endswith(".py"):
            if candidate not in paths:
                missing.append(candidate)
        else:
            module_name = candidate.replace("/", ".")
            if module_name not in names and not any(
                name.startswith(module_name + ".") for name in names
            ):
                missing.append(candidate)
    return "OK" if not missing else "缺失：" + "、".join(missing)


def render_go_map(index: RepoIndex) -> str:
    by_go: dict[str, list[str]] = {}
    for module in index.modules:
        if module.go_path:
            by_go.setdefault(module.go_path, []).append(module.name)
    metrics = collect_metrics(index)
    lines = [
        HEADER,
        "# Go ↔ Python 映射（自动生成）",
        "",
        f"- 有「对应 Go」声明的模块：**{metrics['go_mapped']}** / {metrics['modules']}。"
        "未声明的通常是 Python 侧新增能力。",
        "- 权威映射是 `PORTING.md` §4（包级）；这里是模块级反查，用于查"
        "「这段代码对应上游哪个文件」。",
        "",
        "## 模块 → Go 文件",
        "",
        "| Go 文件 | Python 模块 |",
        "|---|---|",
    ]
    for go_path in sorted(by_go):
        modules = "、".join(f"`{name}`" for name in sorted(by_go[go_path]))
        lines.append(f"| `{go_path}` | {modules} |")
    lines.append("")
    lines.append("## PORTING.md §4 映射表体检")
    lines.append("")
    lines.append("| Go 包 | Python 模块 | 状态 |")
    lines.append("|---|---|---|")
    rows = _porting_rows(index)
    for go_pkg, python_cell, status in rows:
        lines.append(f"| `{go_pkg}` | {_cell(python_cell, 80)} | {status} |")
    broken = [row for row in rows if row[2] not in {"OK", "—"}]
    lines.append("")
    lines.append(f"映射行 {len(rows)} 条，失效 **{len(broken)}** 条。")
    lines.append("")
    return "\n".join(lines) + "\n"


# --- 读写与 --check --------------------------------------------------------


def render_artifacts(index: RepoIndex) -> dict[str, str]:
    """渲染 ``devdocs/generated/`` 下的全部产物，返回 ``文件名 → 内容``。"""
    return {name: render(index) for name, render in ARTIFACTS.items()}


def all_artifacts(index: RepoIndex, generated_dir: str = "devdocs/generated") -> dict[str, str]:
    """全部生成物的 ``仓库相对路径 → 内容``。

    除了 ``devdocs/generated/*``，还包括 10 个域的 ``atri_x/<domain>/README.md``
    与 ``docs/dev/glossary.md``：它们的"可生成部分"各自也是同一套索引的投影，
    所以同样受 ``--check`` 保护。
    """
    from atri_x.devtools.domaindocs import GLOSSARY_PATH, domain_readme_paths, render_glossary

    prefix = (generated_dir or "devdocs/generated").replace("\\", "/").strip("/")
    out = {f"{prefix}/{name}": text for name, text in render_artifacts(index).items()}
    out.update(domain_readme_paths(index))
    out[GLOSSARY_PATH] = render_glossary(index)
    return out


def write_all(index: RepoIndex, root: Path | str, generated_dir: str = "devdocs/generated") -> list[Path]:
    """把全部生成物写进仓库（覆盖），返回写入的绝对路径。"""
    base = Path(root)
    written: list[Path] = []
    for rel, text in all_artifacts(index, generated_dir).items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def check_all(index: RepoIndex, root: Path | str, generated_dir: str = "devdocs/generated") -> list[str]:
    """比对仓库里的生成物与重新渲染的结果，返回差异描述（空列表 = 一致）。"""
    base = Path(root)
    problems: list[str] = []
    for rel, expected in all_artifacts(index, generated_dir).items():
        path = base / rel
        if not path.is_file():
            problems.append(f"{rel}: 缺少生成物（跑 python scripts/gen_index.py）")
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: 读取失败（{exc}）")
            continue
        if actual != expected:
            problem = _first_difference(expected, actual)
            problems.append(f"{rel}: 与当前代码不一致（{problem}）；手改生成物或漏跑生成器")
    return problems


def write_artifacts(index: RepoIndex, out_dir: Path | str) -> list[Path]:
    """只写 ``devdocs/generated/``（保留给单产物场景与旧调用）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_artifacts(index).items():
        path = target / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def check_artifacts(index: RepoIndex, out_dir: Path | str) -> list[str]:
    """只校验 ``devdocs/generated/``（保留给单产物场景与旧调用）。"""
    target = Path(out_dir)
    problems: list[str] = []
    for name, expected in render_artifacts(index).items():
        path = target / name
        if not path.is_file():
            problems.append(f"{name}: 缺少生成物（跑 python scripts/gen_index.py）")
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{name}: 读取失败（{exc}）")
            continue
        if actual != expected:
            problem = _first_difference(expected, actual)
            problems.append(
                f"{name}: 与当前代码不一致（{problem}）；手改生成物或漏跑生成器"
            )
    return problems


def _first_difference(expected: str, actual: str) -> str:
    """定位第一处差异，报错时不用贴整份文件。"""
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for number in range(max(len(expected_lines), len(actual_lines))):
        left = expected_lines[number] if number < len(expected_lines) else "<缺失>"
        right = actual_lines[number] if number < len(actual_lines) else "<缺失>"
        if left != right:
            return f"第 {number + 1} 行期望 {left[:60]!r}，实际 {right[:60]!r}"
    return "行数相同但内容不同"
