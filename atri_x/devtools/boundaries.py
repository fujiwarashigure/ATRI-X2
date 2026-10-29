"""枢纽模块的公开面声明 + "跨域绕开 facade"的判定。

为什么需要：全仓是平铺布局，任何模块都能 import 任何模块（实测跨一级域 import 边
占四成），所以"我只改这一块"在类型层面不成立。目录层级的搬迁成本高（git blame
断代、所有 PR 冲突），**先把枢纽包的公开面钉住**是收益最高、改动最小的一步。

判定规则（:func:`find_violations`）：

- 枢纽（:data:`HUB_FACADES`）的 ``__init__`` 是**对外唯一入口**；
- 同域内部随便 import（``tool/xxx`` 里的模块可以互相深 import）；
- **跨域**模块 import 枢纽的**子模块**（而不是枢纽本身）就算一次"绕开 facade"；
- 例外：在 :data:`PUBLIC_SUBMODULES` 里显式声明为公开的子模块。

违规数是**预算指标**（见 ``scripts/complexity_budget.json``）：本版把现状冻结成
基线，只允许下降。下降的办法有两条：把调用方改成 ``from atri_x.<hub> import X``
（让枢纽 ``__init__`` 再导出），或把该子模块登记为公开。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.devtools.astindex import ModuleInfo, RepoIndex, one_level_domain

__all__ = [
    "HUB_FACADES",
    "PUBLIC_SUBMODULES",
    "BoundaryViolation",
    "find_violations",
    "hub_of",
]

#: 枢纽包 → 它在 ``PORTING.md`` §3 里承担的契约（人读的说明，不参与判定）。
HUB_FACADES: dict[str, str] = {
    "atri_x.errors": "全仓唯一的哨兵异常出处（单文件，无子模块）",
    "atri_x.tool.tool": "工具契约：Tool / Info / Result / Registry（单文件）",
    "atri_x.llm.llm": "LLM 契约：LLMMessage / ChatRequest / StreamChunk / ToolSchema（单文件）",
    "atri_x.security": "安全契约：Actor / Policy / Role / RiskLevel 的唯一出处",
    "atri_x.storage.models": "存储模型：Session / Message 等值对象（单文件）",
    "atri_x.delivery.delivery": "投递契约：Output / Receipt / Target（单文件）",
    "atri_x.config": "配置契约：Config 结构与加载入口",
}

#: 允许被跨域直接 import 的枢纽子模块（登记即视为公开面的一部分）。
#: 本版为空：先冻结现状，靠预算只降不升把调用方逐步收回到 ``__init__``。
PUBLIC_SUBMODULES: dict[str, tuple[str, ...]] = {}


@dataclass(frozen=True)
class BoundaryViolation:
    """一次"跨域绕开 facade"。"""

    source: str
    target: str
    hub: str

    def __str__(self) -> str:  # pragma: no cover - 只用于打印
        return f"{self.source} → {self.target}（枢纽 {self.hub} 的子模块）"


def hub_of(module_name: str) -> str:
    """``module_name`` 命中的枢纽包（没有则返回空串）。"""
    for hub in HUB_FACADES:
        if module_name == hub or module_name.startswith(hub + "."):
            return hub
    return ""


def find_violations(index: RepoIndex) -> list[BoundaryViolation]:
    """找出全部"跨域绕开 facade"的 import（稳定排序）。"""
    by_name = index.by_name()
    violations: list[BoundaryViolation] = []
    for module in index.modules:
        for target in module.imports:
            hub = hub_of(target)
            if not hub or target == hub:
                continue
            allowed = PUBLIC_SUBMODULES.get(hub, ())
            if target in allowed:
                continue
            if one_level_domain(module.name) == one_level_domain(hub):
                # 同域内部可以互相深 import：这里是域内实现细节，不是跨域耦合。
                continue
            if target not in by_name:
                continue
            violations.append(BoundaryViolation(module.name, target, hub))
    return sorted(violations, key=lambda item: (item.hub, item.source, item.target))


def summarize(index: RepoIndex) -> dict[str, int]:
    """按枢纽统计违规数（给 ``check_complexity --list`` 用）。"""
    counts: dict[str, int] = {hub: 0 for hub in HUB_FACADES}
    for violation in find_violations(index):
        counts[violation.hub] = counts.get(violation.hub, 0) + 1
    return dict(sorted(counts.items()))


def modules_of(index: RepoIndex, hub: str) -> list[ModuleInfo]:
    """枢纽自己的模块（含 ``__init__``），供报告渲染。"""
    return index.modules_in(hub.split(".")[1])
