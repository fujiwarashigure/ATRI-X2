"""命令注册表、依赖容器与服务协议。

对应 Go：``internal/agent/commands/register.go``。

分工：

- 本模块承载 Go ``register.go`` 的注册机制（``Registrar`` / ``Module`` /
  ``HandlerFactory`` / ``CommandGroup`` / ``RegisterFactories`` /
  ``RegisterModules`` / ``DefaultModules``）、服务 ``Protocol`` 形状、
  ``state.toml`` 热加载的两个报告类型，以及依赖容器 :class:`Deps`。
- Go ``register.go`` 里的模型服务数据类型（``ModelOption`` / ``ModelProfile`` /
  ``ModelSnapshot`` 等）拆到 :mod:`atri_x.agent.commands.model_types`，本模块
  再导出，保持"按 Go 文件名可以检索到"的一致性。

**并行期约定**：服务实现由 ``atri_x.agent`` 的顶层模块（core / model / tools）
注入，此刻尚未交付。因此 ``Deps`` 的字段一律是"鸭子类型 + 可选"，
本模块只声明 ``Protocol`` 形状，不对实现做 ``isinstance`` 判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable

from atri_x.command import Handler, Request, Result, Router
from atri_x.agent.commands.model_types import (
    ModelListOptions,
    ModelListResult,
    ModelOption,
    ModelProfile,
    ModelProviderError,
    ModelService,
    ModelSnapshot,
    ModelSnapshotSlot,
)

__all__ = [
    "CommandGroup",
    "CompactService",
    "ContextPolicyService",
    "ContextStatusService",
    "Deps",
    "DoctorService",
    "GroupKnowledgeService",
    "GroupPolicyService",
    "GroupServicesService",
    "HandlerFactory",
    "HookService",
    "LogService",
    "MemberPanelService",
    "ModelListOptions",
    "ModelListResult",
    "ModelOption",
    "ModelProfile",
    "ModelProviderError",
    "ModelService",
    "ModelSnapshot",
    "ModelSnapshotSlot",
    "Module",
    "Registrar",
    "RuntimeStateReloadReport",
    "RuntimeStateService",
    "RuntimeStateStatus",
    "ToolService",
    "default_modules",
    "new_command_group",
    "register_default_modules",
    "register_factories",
    "register_modules",
]


@runtime_checkable
class Registrar(Protocol):
    """能把一个 handler 登记进路由的东西。对应 Go ``Registrar``。

    Go 的 ``Register`` 返回 ``error``；Python 侧 :meth:`atri_x.command.Router.register`
    直接抛异常，所以这里没有返回值。
    """

    def register(self, handler: Handler) -> None: ...


@runtime_checkable
class Module(Protocol):
    """一组命令的注册模块。对应 Go ``Module``。"""

    def register_commands(self, registrar: Registrar, deps: "Deps") -> None: ...


# HandlerFactory 由 Deps 造出一个命令。对应 Go ``HandlerFactory``。
HandlerFactory = Callable[["Deps"], Handler]


@dataclass
class CommandGroup:
    """把若干工厂打包成一个 Module。对应 Go ``CommandGroup``。"""

    factories: list[HandlerFactory] = field(default_factory=list)

    def register_commands(self, registrar: Registrar, deps: "Deps") -> None:
        """对应 Go ``CommandGroup.RegisterCommands``。"""
        register_factories(registrar, deps, *self.factories)


def new_command_group(*factories: HandlerFactory) -> CommandGroup:
    """对应 Go ``NewCommandGroup``。"""
    return CommandGroup(factories=list(factories))


# ---------------------------------------------------------------------------
# 服务协议（Go 里的 interface，Python 侧只用鸭子类型消费）
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextStatusService(Protocol):
    """对应 Go ``ContextStatusService``。"""

    def context_status(self, ctx: Any, session: Any) -> str: ...


@runtime_checkable
class CompactService(Protocol):
    """对应 Go ``CompactService``。"""

    async def compact_current(self, ctx: Any, trigger_reason: str) -> str: ...


@runtime_checkable
class ContextPolicyService(Protocol):
    """对应 Go ``ContextPolicyService``。"""

    def context_policy_status(self, ctx: Any) -> str: ...

    async def set_context_policy(self, ctx: Any, target: str, value: str) -> str: ...

    async def reset_context_policy(self, ctx: Any, target: str) -> str: ...


@runtime_checkable
class GroupKnowledgeService(Protocol):
    """对应 Go ``GroupKnowledgeService``。"""

    async def group_knowledge_list(self, ctx: Any) -> list[Any]: ...

    async def group_knowledge_add(
        self,
        ctx: Any,
        question: str,
        answer: str,
        match: str,
        aliases: list[str],
        keywords: list[str],
    ) -> Any: ...

    async def group_knowledge_remove(self, ctx: Any, id: str) -> bool: ...

    async def group_knowledge_clear(self, ctx: Any) -> int: ...

    async def group_knowledge_test(self, ctx: Any, text: str) -> tuple[Any, bool]: ...


@runtime_checkable
class MemberPanelService(Protocol):
    """普通成员的自助任务/额度视图。对应 Go ``MemberPanelService``。"""

    async def member_panel(self, ctx: Any, view: str) -> str: ...


@runtime_checkable
class GroupServicesService(Protocol):
    """确定性的提醒、投票与报名。对应 Go ``GroupServicesService``。"""

    async def reminder_create(self, ctx: Any, when_text: str, text: str) -> str: ...

    async def reminder_list(self, ctx: Any) -> str: ...

    async def reminder_remove(self, ctx: Any, id: str) -> str: ...

    async def poll_create(self, ctx: Any, question: str, options: list[str]) -> str: ...

    async def poll_list(self, ctx: Any) -> str: ...

    async def poll_show(self, ctx: Any, id: str) -> str: ...

    async def poll_vote(self, ctx: Any, id: str, option: str) -> str: ...

    async def poll_close(self, ctx: Any, id: str) -> str: ...

    async def signup_create(self, ctx: Any, title: str, capacity: int) -> str: ...

    async def signup_list(self, ctx: Any) -> str: ...

    async def signup_show(self, ctx: Any, id: str) -> str: ...

    async def signup_join(self, ctx: Any, id: str) -> str: ...

    async def signup_leave(self, ctx: Any, id: str) -> str: ...

    async def signup_close(self, ctx: Any, id: str) -> str: ...


@runtime_checkable
class GroupPolicyService(Protocol):
    """对应 Go ``GroupPolicyService``。"""

    def group_policy_status(self, ctx: Any) -> str: ...

    async def set_group_policy(self, ctx: Any, field: str, value: str) -> str: ...

    async def reset_group_policy(self, ctx: Any, field: str) -> str: ...

    def authorize_learning_action(self, ctx: Any, action: str) -> bool: ...

    def group_learning_enabled(self, ctx: Any) -> bool: ...


@runtime_checkable
class ToolService(Protocol):
    """对应 Go ``ToolService``。"""

    def list(self) -> list[Any]: ...

    def unregister(self, name: str) -> None: ...

    async def remove(self, ctx: Any, name: str) -> None: ...

    async def reload(self, ctx: Any) -> None: ...


@runtime_checkable
class HookService(Protocol):
    """对应 Go ``HookService``。

    Go 的 ``HookReload`` / ``StartStatefulHook`` / ``StopHook`` /
    ``RestartStatefulHook`` 都返回 ``error``（会做 I/O），因此声明为异步；命令侧统一
    用 ``_maybe_await`` 调用，同步实现同样可用。
    """

    def hook_list(self) -> list[Any]: ...

    async def hook_reload(self) -> Any: ...

    def stateful_hooks(self) -> list[Any]: ...

    async def start_stateful_hook(self, id: str) -> None: ...

    async def stop_hook(self, ctx: Any, id: str) -> bool: ...

    async def restart_stateful_hook(self, ctx: Any, id: str) -> None: ...


@runtime_checkable
class LogService(Protocol):
    """对应 Go ``LogService``。

    Go 的 ``QueryLogs`` 是同步方法（本地日志读取）；命令侧用 ``_maybe_await`` 调用，
    同步实现与协程实现都能用。
    """

    def query_logs(self, ctx: Any, query: Any) -> Any: ...


@runtime_checkable
class DoctorService(Protocol):
    """``/doctor`` 背后的只读配置检查。对应 Go ``DoctorService``。"""

    async def doctor(self, ctx: Any) -> str: ...


# ---------------------------------------------------------------------------
# state.toml 热加载
# ---------------------------------------------------------------------------


@dataclass
class RuntimeStateReloadReport:
    """一次 ``state.toml`` 热加载的报告。对应 Go ``RuntimeStateReloadReport``。

    ``applied`` 为真表示文件确实被读入并合并进运行时状态；``changed`` 列出
    生效值发生变化的运行时分区；``mod_time`` 是本次应用的文件 mtime。
    """

    path: str = ""
    applied: bool = False
    changed: list[str] = field(default_factory=list)
    mod_time: datetime | None = None


@dataclass
class RuntimeStateStatus:
    """已加载状态文件与磁盘文件的新旧关系。对应 Go ``RuntimeStateStatus``。"""

    path: str = ""
    loaded_mod_time: datetime | None = None
    file_mod_time: datetime | None = None
    pending: bool = False


@runtime_checkable
class RuntimeStateService(Protocol):
    """把 ``state.toml`` 暴露给操作者以便免重启生效。对应 Go ``RuntimeStateService``。"""

    async def reload_runtime_state(self, ctx: Any) -> RuntimeStateReloadReport: ...

    def runtime_state_status(self) -> RuntimeStateStatus: ...


# ---------------------------------------------------------------------------
# 依赖容器
# ---------------------------------------------------------------------------


@dataclass
class Deps:
    """所有命令共享的依赖容器。对应 Go ``Deps``。

    字段与 Go 逐字对应（PascalCase → snake_case）。除 ``router`` 外都允许为
    ``None``：可选服务（``compact`` / ``hooks`` / ``characters`` 等）缺失时命令
    按 Go 的行为回退成一句中英文提示；而 Go 里直接解引用的必填服务
    （``sessions`` / ``scope`` / ``turns`` / ``models``）缺失时同样会抛
    ``AttributeError``，与 Go 的 nil 解引用 panic 一致。
    """

    router: Router | None = None
    sessions: Any = None
    requests: Any = None
    turns: Any = None
    store: Any = None
    scope: Callable[[Any], Any] | None = None
    models: ModelService | None = None
    compact: CompactService | None = None
    context_status: ContextStatusService | None = None
    context_policy: ContextPolicyService | None = None
    group_policy: GroupPolicyService | None = None
    group_knowledge: GroupKnowledgeService | None = None
    member_panel: MemberPanelService | None = None
    group_services: GroupServicesService | None = None
    tools: ToolService | None = None
    hooks: HookService | None = None
    session_state: Any = None
    characters: Any = None
    angel_memory: Any = None
    resident_memory: Any = None
    self_learning: Any = None
    audit: Callable[..., None] | None = None
    logs: LogService | None = None
    runtime_state: RuntimeStateService | None = None
    runtime_status: Callable[[str], Any] | None = None
    # cancel_session_inbox 丢弃还在 agent 侧合并/串行队列里排队的普通聊天消息。
    cancel_session_inbox: Callable[[str], int] | None = None
    # tool_allowlist 渲染当前群的工具白名单状态（``/tools`` 用；``None`` 表示未接线，
    # 命令退回不带权限状态的老输出）。
    tool_allowlist: Callable[[Any], str] | None = None
    # file_backups 保存 ATRI-X 自己编辑过的文件在编辑前的内容，供 /rollback 撤销。
    file_backups: Any = None
    # doctor 执行 /doctor 背后的只读配置检查。
    doctor: DoctorService | None = None
    # grants 是授权登记表（``[security]`` 相关的高风险放行凭据）；``/grant`` 与
    # ``/panic`` 命令直接读写它，``None`` 表示未接线（命令退回"未启用"）。
    grants: Any = None
    #: Python 版新增：统一时间服务（``/time`` 用）；``None`` = 未装配。
    time_service: Any = None
    #: Python 版新增：网络时间校验器（``/time verify`` 用）；``None`` = 未装配。
    time_verifier: Any = None


def register_factories(
    registrar: Registrar, deps: Deps, *factories: HandlerFactory
) -> None:
    """逐个用 ``deps`` 造出 handler 并注册。对应 Go ``RegisterFactories``。"""
    for factory in factories:
        registrar.register(factory(deps))


def register_modules(registrar: Registrar, deps: Deps, *modules: Module) -> None:
    """逐个调用模块的注册入口。对应 Go ``RegisterModules``。"""
    for module in modules:
        module.register_commands(registrar, deps)


def default_modules() -> list[Module]:
    """默认命令模块清单，顺序与 Go ``DefaultModules`` 一致。"""
    from atri_x.agent.commands.approval import ApprovalModule
    from atri_x.agent.commands.character import CharacterModule
    from atri_x.agent.commands.compact import CompactModule
    from atri_x.agent.commands.context_policy import ContextPolicyModule
    from atri_x.agent.commands.doctor import DoctorModule
    from atri_x.agent.commands.group_knowledge import KnowledgeModule
    from atri_x.agent.commands.group_policy import GroupPolicyModule
    from atri_x.agent.commands.group_services import GroupServicesModule
    from atri_x.agent.commands.help import HelpModule
    from atri_x.agent.commands.hook import HookModule
    from atri_x.agent.commands.log import LogModule
    from atri_x.agent.commands.member_panel import MemberPanelModule
    from atri_x.agent.commands.memory_learning import MemoryLearningModule
    from atri_x.agent.commands.model import ModelModule
    from atri_x.agent.commands.request import RequestModule
    from atri_x.agent.commands.research import ResearchModule  # [atri-research]
    from atri_x.agent.commands.rollback import FileModule
    from atri_x.agent.commands.session_module import SessionModule
    from atri_x.agent.commands.state import StateModule
    from atri_x.agent.commands.time import TimeModule
    from atri_x.agent.commands.tool import ToolModule

    return [
        HelpModule(),
        ApprovalModule(),
        ModelModule(),
        SessionModule(),
        CompactModule(),
        ContextPolicyModule(),
        GroupPolicyModule(),
        KnowledgeModule(),
        MemberPanelModule(),
        GroupServicesModule(),
        RequestModule(),
        FileModule(),
        DoctorModule(),
        LogModule(),
        ToolModule(),
        CharacterModule(),
        HookModule(),
        StateModule(),
        MemoryLearningModule(),
        TimeModule(),
        ResearchModule(),  # [atri-research]
    ]


def register_default_modules(
    registrar: Registrar, deps: Deps, *extra: Module
) -> None:
    """注册默认模块，外加调用方追加的 ``extra``。对应 Go ``RegisterDefaultModules``。"""
    register_modules(registrar, deps, *default_modules(), *extra)
