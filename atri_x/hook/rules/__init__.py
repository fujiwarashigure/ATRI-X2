"""``hooks.toml`` 规则引擎。

对应 Go：``internal/hook/rules``。

子模块：

- :mod:`atri_x.hook.rules.types` —— 配置结构体（``Rule`` / ``Action`` / ``Options``）
- :mod:`atri_x.hook.rules.rules` —— ``Module``、``NewModule`` 与注册
- :mod:`atri_x.hook.rules.config` —— ``hooks.toml`` / 插件 ``hook.toml`` 装载
- :mod:`atri_x.hook.rules.toml_error` —— 严格 TOML 解码与详细错误
- :mod:`atri_x.hook.rules.rule` —— 校验、角色条件与规则执行
- :mod:`atri_x.hook.rules.action` —— 动作执行（改文本 / 发送 / 工具）
- :mod:`atri_x.hook.rules.exec` —— ``exec`` 一次性插件
- :mod:`atri_x.hook.rules.detail` —— ``/hooks`` 展示文本
"""

from __future__ import annotations

from atri_x.hook.rules.action import (
    ActionResult,
    State,
    render,
    run_action,
)
from atri_x.hook.rules.config import (
    load_config,
    normalize_loaded_rules,
    validate_loaded_rules,
)
from atri_x.hook.rules.detail import format_rule_detail, rule_description
from atri_x.hook.rules.rule import (
    RuleError,
    rule_registrations,
    rule_role_conditions,
    run_rule,
    validate_rule,
)
from atri_x.hook.rules.rules import Module, NewModule, new_module
from atri_x.hook.rules.types import (
    CONFIG_FILE,
    DEFAULT_PRIORITY,
    Action,
    Config,
    Options,
    PluginInfo,
    PluginRef,
    PlatformAPICaller,
    PlatformCallerResolver,
    Rule,
    SegmentSpec,
    Target,
    plugin_config,
    rule_source,
)

__all__ = [
    "CONFIG_FILE",
    "DEFAULT_PRIORITY",
    "Action",
    "ActionResult",
    "Config",
    "Module",
    "NewModule",
    "Options",
    "PlatformAPICaller",
    "PlatformCallerResolver",
    "PluginInfo",
    "PluginRef",
    "Rule",
    "RuleError",
    "SegmentSpec",
    "State",
    "Target",
    "format_rule_detail",
    "load_config",
    "new_module",
    "normalize_loaded_rules",
    "plugin_config",
    "render",
    "rule_description",
    "rule_registrations",
    "rule_role_conditions",
    "rule_source",
    "run_action",
    "run_rule",
    "validate_loaded_rules",
    "validate_rule",
]
