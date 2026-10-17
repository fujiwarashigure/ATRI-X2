"""规则模块：装载 ``hooks.toml`` 并把每条规则注册成 Hook。

对应 Go：``internal/hook/rules/rules.go``。
"""

from __future__ import annotations

from typing import Any

from atri_x.hook.event import Event, HandlerFunc
from atri_x.hook.manager import Registration
from atri_x.hook.rules.config import load_config, report_config_error
from atri_x.hook.rules.detail import format_rule_detail, rule_description
from atri_x.hook.rules.rule import rule_registrations, run_rule, validate_rule
from atri_x.hook.rules.types import (
    CONFIG_FILE,
    DEFAULT_PRIORITY,
    Config,
    Options,
    Rule,
)

__all__ = [
    "CONFIG_FILE",
    "DEFAULT_PRIORITY",
    "Module",
    "NewModule",
    "new_module",
]


class Module:
    """一个已经装载好的规则模块。对应 Go ``Module``。"""

    def __init__(
        self,
        rules: list[Rule] | None = None,
        runtimes: list[Any] | None = None,
        opts: Options | None = None,
        logger: Any = None,
    ) -> None:
        self.rules: list[Rule] = list(rules or [])
        self.runtimes: list[Any] = list(runtimes or [])
        self.opts: Options = opts or Options()
        self.logger: Any = logger if logger is not None else self.opts.logger
        #: 记录已注册的规则名，便于诊断（Go 没这个字段，纯附加）。
        self.registered: list[str] = []

    # -- 注册 ---------------------------------------------------------------
    def register_hooks(self, registrar: Any) -> None:
        """把全部启用的规则注册到 registrar。对应 Go ``RegisterHooks``。"""
        for index, rule in enumerate(self.rules):
            if not rule.is_enabled():
                continue
            validate_rule(rule)
            priority = rule.priority or DEFAULT_PRIORITY
            name = (rule.source.final_name or "").strip()
            if not name:
                name = (rule.name or "").strip()
            if not name:
                name = f"rule.{index + 1}"
            if self.logger is not None:
                self.logger.info(
                    "hook rule registered",
                    name=name,
                    point=rule.on,
                    priority=priority,
                    matches=len(rule.match),
                    actions=len(rule.actions),
                    config_path=rule.source.config_path,
                )
            registrations = rule_registrations(rule)
            for role_index, match in enumerate(registrations):
                reg_name = name
                if len(registrations) > 1:
                    reg_name = f"{reg_name}.role.{role_index + 1}"
                description = rule_description(rule)
                detail = format_rule_detail(rule)
                if len(registrations) > 1:
                    detail = (
                        f"{detail}\n\n(role partition {role_index + 1}/"
                        f"{len(registrations)})"
                    )
                self.registered.append(reg_name)
                registrar.register(
                    Registration(
                        point=rule.on,
                        priority=priority,
                        plugin_id=rule.source.plugin_name,
                        name=reg_name,
                        description=description,
                        match=match,
                        block=rule.source.block,
                        detail=detail,
                        wakeup=rule.wakeup,
                        handler=HandlerFunc(_rule_handler(self, rule)),
                    )
                )

    # -- 执行 ---------------------------------------------------------------
    async def run_rule(self, ctx: Any, rule: Rule, event: Event) -> Event:
        """执行规则（有状态 Hook 走 runtime）。对应 Go ``Module.runRule``。"""
        return await run_rule(self, ctx, rule, event)

    async def run_exec(
        self, ctx: Any, event: Event, action: Any, state: Any
    ) -> tuple[Event, Any]:
        """``exec`` 动作（实现在 :mod:`atri_x.hook.rules.exec`）。"""
        from atri_x.hook.rules.exec import run_exec

        return await run_exec(self, ctx, event, action, state)

    def audit(self, event: str, *attrs: Any) -> None:
        """审计回调。对应 Go ``Module.audit``。"""
        if self.opts.audit is not None:
            self.opts.audit(event, *attrs)


def _rule_handler(module: Module, rule: Rule) -> Any:
    """构造一条规则的 handler。

    对应 Go ``RegisterHooks`` 里内联的 ``hook.HandlerFunc``：有状态 Hook 直接把
    事件转给 runtime，其余走 ``runRule``。
    """

    async def handle(ctx: Any, event: Event) -> Event:
        if rule.source.runtime_id:
            if module.opts.runtime is None:
                raise RuntimeError("stateful hook runtime is not configured")
            from atri_x.hook.event import Control

            return await module.opts.runtime.handle(
                ctx,
                rule.source.runtime_id,
                event,
                Control(
                    consume=rule.consume,
                    stop_propagation=rule.stop_propagation,
                ),
            )
        return await run_rule(module, ctx, rule, event)

    return handle


def NewModule(opts: Options) -> Module:
    """装载配置并构造模块。对应 Go ``NewModule``；装载失败抛异常。

    Go 的签名是 ``(Module, error)``，这里抛异常（PORTING §1.5）。
    """
    try:
        cfg, path = load_config(opts)
    except Exception as exc:  # noqa: BLE001 - 上报后再抛
        report_config_error(opts, _failure_path(exc), exc)
        raise
    module = Module(
        rules=cfg.rules, runtimes=cfg.runtimes, opts=opts, logger=opts.logger
    )
    if module.logger is not None:
        enabled = sum(1 for rule in module.rules if rule.is_enabled())
        module.logger.info(
            "hook rule config loaded",
            path=path,
            rules=len(module.rules),
            enabled=enabled,
        )
    return module


def new_module(opts: Options) -> Module:
    """``NewModule`` 的 snake_case 别名。"""
    return NewModule(opts)


def _failure_path(err: BaseException) -> str:
    """从装载错误里尽力还原配置文件路径（Go 直接有 ``path`` 返回值）。"""
    text = str(err)
    marker = "parse hook rule config "
    if text.startswith(marker):
        rest = text[len(marker) :]
        if rest.startswith(("'", '"')) and len(rest) > 1:
            quote = rest[0]
            end = rest.find(quote, 1)
            if end > 0:
                return rest[1:end]
    return ""


# 交叉检索：Go 里的 ``Config`` 类型。
_CONFIG = Config
