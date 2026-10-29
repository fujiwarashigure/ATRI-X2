"""入口索引的渲染：进程入口 / 装配层 / 命令 / 工具 / 平台 / Hook / GUI / 任务入口。

从 :mod:`atri_x.devtools.render` 拆出来的原因：那个文件已接近 600 行上限（ADR 0002），
而"入口"是独立主题——它读的是**注册表字面量**（``ALWAYS_REGISTERED_TOOLS`` /
``CONDITIONAL_TOOLS``）与目录约定，不依赖依赖图与测试地图的渲染逻辑。

任务入口表（``TASK_ENTRIES``）是**数据**而不是散文：``check_docs.py`` 会校验其中每个代码
路径都存在、每个 locator 都有定义；渲染结果进 ``devdocs/generated/entrypoints.md``。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from atri_x.devtools.astindex import RepoIndex
from atri_x.devtools.markdown import HEADER, cell as _cell

__all__ = [
    "TASK_ENTRIES",
    "TaskEntry",
    "locator_definitions",
    "render_entrypoints",
]


# --- 任务入口数据 ----------------------------------------------------------

#: 任务 → 入口。这是**数据**（不是散文）：``check_docs.py`` 会校验每个代码路径存在、
#: 每个 locator 有定义，渲染进 ``entrypoints.md`` 供人和 Agent 查。
@dataclass(frozen=True)
class TaskEntry:
    task: str
    locators: tuple[str, ...]
    modules: tuple[str, ...]
    docs: tuple[str, ...] = ()


TASK_ENTRIES: tuple[TaskEntry, ...] = (
    TaskEntry("启动、装配、配置", ("startup", "config"),
              ("atri_x/app/app.py", "atri_x/config"),
              ("devdocs/architecture.md", "docs/configuration.md")),
    TaskEntry("Agent 对话流程", ("agent-chat",),
              ("atri_x/agent/core.py", "atri_x/agent/chat.py"),
              ("devdocs/architecture.md",)),
    TaskEntry("Slash 命令与补全", ("commands",),
              ("atri_x/agent/commands", "atri_x/command"),
              ("docs/commands.md",)),
    TaskEntry("Request / Turn 状态", ("request-turn",),
              ("atri_x/request/manager.py", "atri_x/turn/manager.py")),
    TaskEntry("工具调用链路", ("tool", "tool-flow"),
              ("atri_x/tool", "atri_x/toolrun"),
              ("devdocs/code-map.md",)),
    TaskEntry("内置工具实现", ("tool",),
              ("atri_x/tool/builtin",),
              ("devdocs/code-map.md", "docs/dev/find-it.md")),
    TaskEntry("内容安全（道德限制 / 敏感信息）", ("safety",),
              ("atri_x/safety",), ("docs/safety.md",)),
    TaskEntry("识图（两段式 / 隐私开关）", ("vision",),
              ("atri_x/vision", "atri_x/agent/vision.py"), ("docs/vision.md",)),
    TaskEntry("Skill / Atriyph", ("skill",),
              ("atri_x/tool/skill", "atri_x/atriyph")),
    TaskEntry("Hook / 插件", ("hook",),
              ("atri_x/hook",), ("docs/hooks.md",)),
    TaskEntry("信号与日志边界", ("signals",),
              ("atri_x/signal",), ("devdocs/signals-and-logging.md",)),
    TaskEntry("健康状态与运维接口", ("health-ops",),
              ("atri_x/health", "atri_x/app/ops_health.py")),
    TaskEntry("Output / 发送", ("output",),
              ("atri_x/delivery", "atri_x/agent/output.py")),
    TaskEntry("平台适配", ("platform",),
              ("atri_x/platform",), ("docs/snowluma.md",)),
    TaskEntry("Session / 上下文", ("session", "context"),
              ("atri_x/session", "atri_x/contextmgr")),
    TaskEntry("SQLite / 存储", ("storage",),
              ("atri_x/storage", "atri_x/storage/sqlite")),
    TaskEntry("Atrinis / Atriena / Atriwisp", ("atrinis",),
              ("atri_x/atrinis", "atri_x/atriena"), ("docs/atrinis.md",)),
    TaskEntry("Cron / 维护任务", ("cron",),
              ("atri_x/cron", "atri_x/maintenance")),
    TaskEntry("时间服务 / 时区 / 网络校验", ("time",),
              ("atri_x/time_service.py", "atri_x/timezones.py", "atri_x/timesync")),
    TaskEntry("Go → Python 移植状态", ("porting",),
              ("atri_x",), ("PORTING.md", "devdocs/porting-status.md")),
    TaskEntry("Windows 打包 / 桌面端 / 安装器", ("packaging",),
              ("packaging", "atri_x/gui", "atri_x/sidecar"),
              ("packaging/README.md", "docs/windows-app.md")),
    TaskEntry("文档维护", ("docs",),
              ("docs", "devdocs"), ("docs/dev/START-HERE.md",)),
    TaskEntry("测试与门禁", ("testing",),
              ("scripts", "tests"), ("docs/dev/START-HERE.md",)),
)


# --- entrypoints -----------------------------------------------------------


def _scan_markdown(articles: Iterable[Path]) -> list[Path]:
    return [path for path in articles if path.suffix == ".md"]


def locator_definitions(root: Path) -> list[tuple[str, str, int]]:
    """扫描全部 Markdown，返回 ``(locator 名, 相对路径, 行号)``。"""
    pattern = re.compile(r"<!--\s*locator:([A-Za-z0-9_-]+)\s*-->")
    found: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*.md")):
        if any(part in {"__pycache__", "generated", "node_modules"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                found.append((match.group(1), path.relative_to(root).as_posix(), number))
    return found


def _literal_from_module(path: Path, name: str) -> object | None:
    """从源码里取 ``name = <字面量>`` 的值（不 import，不执行）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        try:
            return ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return None
    return None


def _entrypoint_sections(index: RepoIndex) -> list[str]:
    sections: list[str] = []
    modules = index.modules
    by_name = index.by_name()
    sections.append("## 进程入口")
    sections.append("")
    sections.append("| 入口 | 模块 | 说明 |")
    sections.append("|---|---|---|")
    entries = [
        ("CLI", "atri_x.__main__", "`python -m atri_x`（与 `atri-x` console script 同一实现）"),
        ("桌面端 GUI", "atri_x.gui.__main__", "`atri-x-gui` / 托盘"),
        ("启动器参数解析", "atri_x.launcher.cli", "命令行参数与用法文本"),
    ]
    for label, module_name, note in entries:
        if module_name in by_name:
            sections.append(f"| {label} | `{module_name}` | {note} |")
    sections.append("")

    sections.append("## 装配层（`atri_x/app/`）")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in index.modules_in("app"):
        sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## Slash 命令模块（`atri_x/agent/commands/`）")
    sections.append("")
    command_modules = index.modules_in("agent.commands")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in command_modules:
        sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## 内置工具（`atri_x/tool/builtin/` + 注册表字面量）")
    sections.append("")
    register = index.root / "atri_x" / "tool" / "builtin" / "register.py"
    always = _literal_from_module(register, "ALWAYS_REGISTERED_TOOLS")
    conditional = _literal_from_module(register, "CONDITIONAL_TOOLS")
    if isinstance(always, tuple):
        sections.append(f"- 无条件注册（最低配置必然出现，{len(always)} 个）："
                        + "、".join(f"`{name}`" for name in always))
    if isinstance(conditional, dict):
        total = sum(len(value) for value in conditional.values())
        sections.append(f"- 条件注册（{len(conditional)} 组 / {total} 个，按依赖守卫出现）：")
        for option, names in conditional.items():
            sections.append(f"  - `{option}` → " + "、".join(f"`{name}`" for name in names))
    sections.append("")
    sections.append("工具实现模块（含 builder 的模块）：")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in index.modules_in("tool.builtin"):
        if module.symbols and any(symbol.endswith("_builder") for symbol in module.symbols):
            sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## 平台适配器（`atri_x/platform/`）")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in index.modules_in("platform"):
        sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## Hook（`atri_x/hook/`）")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in index.modules_in("hook"):
        sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## GUI 页（`atri_x/gui/tabs/`）")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in index.modules_in("gui.tabs"):
        sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")

    sections.append("## 存储与网络")
    sections.append("")
    sections.append("| 模块 | 职责 |")
    sections.append("|---|---|")
    for module in modules:
        if module.domain in {"storage", "net"} or module.name.startswith("atri_x.llm."):
            sections.append(f"| `{module.name}` | {_cell(module.summary)} |")
    sections.append("")
    return sections


def render_entrypoints(index: RepoIndex) -> str:
    lines = [
        HEADER,
        "# 入口索引（自动生成）",
        "",
        "上游/架构说明见 `devdocs/architecture.md`，逐文件职责见 "
        "`devdocs/generated/module-map.md`，按任务找入口见 `docs/dev/find-it.md`。",
        "",
    ]
    lines.extend(_entrypoint_sections(index))
    lines.append("## 任务入口（task → locator → 代码）")
    lines.append("")
    lines.append("| 任务 | 文档锚点 | 先看代码 | 相关文档 |")
    lines.append("|---|---|---|---|")
    for entry in TASK_ENTRIES:
        locators = "、".join(f"`locator:{name}`" for name in entry.locators) or "—"
        modules = "、".join(f"`{name}`" for name in entry.modules) or "—"
        docs = "、".join(f"`{name}`" for name in entry.docs) or "—"
        lines.append(f"| {_cell(entry.task)} | {locators} | {modules} | {docs} |")
    lines.append("")
    lines.append("## 文档锚点（locator 定义全量）")
    lines.append("")
    lines.append("定义 = 文档里的 `<!-- locator:NAME -->` 注释；用 "
                 "`rg -n \"locator:NAME\" .` 跳到对应章节。")
    lines.append("")
    lines.append("| locator | 定义位置 |")
    lines.append("|---|---|")
    definitions = locator_definitions(index.root)
    grouped: dict[str, list[str]] = {}
    for name, path, number in definitions:
        grouped.setdefault(name, []).append(f"`{path}:{number}`")
    for name in sorted(grouped):
        lines.append(f"| `locator:{name}` | " + "、".join(grouped[name]) + " |")
    lines.append("")
    return "\n".join(lines) + "\n"


