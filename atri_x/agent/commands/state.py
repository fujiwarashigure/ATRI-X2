"""``/state``：``state.toml`` 热加载状态与手动重载。

对应 Go：``internal/agent/commands/state.go``（109 行）。

``RuntimeStateReloadReport`` / ``RuntimeStateStatus`` 是 Go ``register.go`` 里的
类型；本仓把它们放在 :mod:`atri_x.agent.commands.deps` 并在此再导出。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Completion, Info, Request, Result
from atri_x.security import RoleSuperadmin
from atri_x.agent.commands.completion import (
    CompletionOption,
    complete_static_options,
    current_completion_token,
    is_first_arg,
)
from atri_x.agent.commands.deps import (
    Deps,
    Registrar,
    RuntimeStateReloadReport,  # noqa: F401  (Go 在本文件被消费)
    RuntimeStateStatus,  # noqa: F401
)
from atri_x.agent.commands.session_lifecycle import audit_command

__all__ = [
    "StateModule",
    "format_runtime_state_reload",
    "format_runtime_state_status",
    "new_state_command",
]

_HELP = """Usage:
  /state          # 查看 state.toml 路径、已加载时间和是否有未生效的外部修改
  /state reload   # 立即重新读取 state.toml 并应用

说明:
  进程每 15 秒自动检测一次 state.toml 的外部修改并生效，无需重启；
  热加载覆盖 mode_models / compact_model / naming_model / context_overflow /
  group_policy / group_knowledge / group_services / group_runtime。
  budget 额度账本由运行中的进程独占，只在进程启动时从文件恢复。"""


def _format_time(value: Any) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_runtime_state_status(status: RuntimeStateStatus) -> str:
    """渲染 ``/state`` 的状态块。对应 Go ``formatRuntimeStateStatus``。"""
    if (status.path or "").strip() == "":
        return "未配置 state.toml（运行时状态不会持久化）。"
    lines = ["运行时状态文件：" + status.path]
    if status.loaded_mod_time is None:
        lines.append("已加载：尚未加载")
    else:
        lines.append("已加载：" + _format_time(status.loaded_mod_time))
    if status.file_mod_time is not None:
        lines.append("文件修改时间：" + _format_time(status.file_mod_time))
    if status.pending:
        lines.append(
            "存在未生效的外部修改：可用 /state reload 立即生效"
            "（进程也会在 15 秒内自动生效）"
        )
    else:
        lines.append("当前内存状态与文件一致。")
    return "\n".join(lines)


def format_runtime_state_reload(report: RuntimeStateReloadReport) -> str:
    """渲染 ``/state reload`` 的结果。对应 Go ``formatRuntimeStateReload``。"""
    if not report.applied:
        return "state.toml 未加载（未配置或文件不存在）。"
    when = ""
    if report.mod_time is not None:
        when = "（" + _format_time(report.mod_time) + "）"
    if not report.changed:
        return "state.toml 已重新加载" + when + "，运行状态无变化。"
    return "state.toml 已重新加载%s，生效内容：%s。" % (when, "、".join(report.changed))


class StateCommand:
    """对应 Go ``stateCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="state",
            usage="/state | /state reload",
            description=(
                "查看运行时状态文件 state.toml 的加载状态，或立即重新加载外部修改。"
            ),
            min_role=RoleSuperadmin,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if self._deps.runtime_state is None:
            return Result(content="运行时状态热加载未配置。")
        fields = (req.args or "").split()
        action = "status"
        if fields:
            action = (fields[0] or "").strip().lower()
        if action in ("", "status", "show"):
            return Result(
                content=format_runtime_state_status(
                    self._deps.runtime_state.runtime_state_status()
                )
            )
        if action == "reload":
            try:
                report = self._deps.runtime_state.reload_runtime_state(ctx)
                if hasattr(report, "__await__"):  # 实现可能同步也可能异步
                    report = await report
            except Exception as exc:  # noqa: BLE001 - Go 里错误直接渲染成文本
                return Result(content=f"重新加载 state.toml 失败：{exc}")
            audit_command(
                self._deps,
                "runtime_state_reload_command",
                "path",
                report.path,
                "sections",
                ",".join(report.changed),
                "applied",
                report.applied,
            )
            return Result(content=format_runtime_state_reload(report))
        return Result(content="用法：/state | /state reload")

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        fields = (req.args or "").split()
        if not fields or is_first_arg(req, token):
            return complete_static_options(
                [
                    CompletionOption("status", description="查看 state.toml 加载状态"),
                    CompletionOption("reload", description="立即重新加载 state.toml"),
                ],
                token.text,
                token.start,
                token.end,
                "state_action",
            )
        return []


def new_state_command(deps: Deps) -> Any:
    """对应 Go ``newStateCommand``（Go 里是内联的 ``stateCommand{deps: deps}``）。"""
    return StateCommand(deps)


class StateModule:
    """对应 Go ``StateModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        # Go 直接 registrar.Register(stateCommand{deps}) —— 不经过工厂，
        # 因为 /state 没有可配置的构造参数。
        registrar.register(new_state_command(deps))
