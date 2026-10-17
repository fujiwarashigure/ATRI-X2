"""``hooks.toml`` 与插件 ``hook.toml`` 的装载。

对应 Go：``internal/hook/rules/config.go``（严格解码的细节在
:mod:`atri_x.hook.rules.toml_error`）。
"""

from __future__ import annotations

import copy
import os
from typing import Any

from atri_x.config.loader import load_env_file
from atri_x.errors import AtriXError
from atri_x.hook.match import BlockPolicy, Condition, new_block_policy
from atri_x.hook.output import Segment, Target
from atri_x.hook.rules.toml_error import (
    StrictDecodeError,
    decode_toml_file,
    strict_array,
    strict_bool,
    strict_int,
    strict_string,
    strict_string_list,
    strict_table,
)
from atri_x.hook.rules.types import (
    CONFIG_FILE,
    Action,
    Config,
    Options,
    PluginInfo,
    PluginRef,
    Rule,
    plugin_config,
    rule_source,
)
from atri_x.hook.runtime.config import (
    Config as RuntimeConfig,
    RestartConfig,
    ToolsConfig,
)

__all__ = [
    "load_config",
    "decode_config",
    "decode_plugin_config",
    "plugin_config_path",
    "safe_relative_path",
    "path_within",
    "normalize_loaded_rules",
    "validate_loaded_rules",
    "report_plugin_config_error",
    "report_plugin_config_warning",
    "report_config_error",
    "warn_ignored_plugin_rule_blocks",
]


def load_config(opts: Options) -> tuple[Config, str]:
    """装载 ``<config_dir>/hooks.toml`` 及其引用的插件。

    对应 Go ``loadConfig``；返回 ``(配置, 配置文件路径)``，失败抛异常。
    """
    config_dir = (opts.config_dir or "").strip()
    if not config_dir:
        return Config(), ""
    path = os.path.join(config_dir, CONFIG_FILE)
    try:
        cfg = decode_toml_file(path, decode_config)
    except FileNotFoundError:
        return Config(), path

    shared_env_path = os.path.join(config_dir, ".env")
    try:
        shared_values = load_env_file(shared_env_path)
    except Exception as exc:  # noqa: BLE001 - 统一成装载错误
        raise RuntimeError(
            f"load shared hook environment {shared_env_path!r}: {exc}"
        ) from exc
    shared_process_env = opts.process_env.overlay(shared_values)
    root_source = rule_source(
        config_path=path,
        base_dir=config_dir,
        process_env=shared_process_env,
    )
    for index, rule in enumerate(cfg.rules):
        try:
            block = rule_block_policy(rule)
        except ValueError as exc:
            raise RuntimeError(
                f"parse hook rule config {path!r}: rule {index + 1}: {exc}"
            ) from exc
        source = _copy_source(root_source)
        source.block = block
        rule.source = source

    for ref in cfg.plugins:
        if not ref.is_enabled():
            continue
        try:
            plugin_path = plugin_config_path(config_dir, ref)
        except ValueError as exc:
            report_plugin_config_error(opts, (ref.name or "").strip(), "", exc)
            continue
        try:
            pcfg = decode_toml_file(plugin_path, decode_plugin_config)
        except (OSError, ValueError, AtriXError) as exc:
            report_plugin_config_error(opts, (ref.name or "").strip(), plugin_path, exc)
            continue
        name = (ref.name or "").strip()
        info_name = (pcfg.plugin.name or "").strip()
        if info_name and info_name != name:
            report_plugin_config_warning(
                opts,
                name,
                plugin_path,
                f"[plugin].name {info_name!r} differs from [[plugins]].name {name!r}; "
                "using the referenced plugin name",
            )
        warn_ignored_plugin_rule_blocks(opts, name, plugin_path, pcfg.rules)
        for rule in pcfg.rules:
            clear_block_config(rule)
        plugin_dir = os.path.dirname(plugin_path)
        plugin_env_path = os.path.join(plugin_dir, ".env")
        try:
            plugin_values = load_env_file(plugin_env_path)
        except Exception as exc:  # noqa: BLE001
            report_plugin_config_error(opts, name, plugin_env_path, exc)
            continue
        plugin_process_env = shared_process_env.overlay(plugin_values)
        try:
            block = new_block_policy(
                pcfg.plugin.blocked_platforms,
                pcfg.plugin.blocked_groups,
                pcfg.plugin.blocked_ids,
            )
        except ValueError as exc:
            report_plugin_config_error(opts, name, plugin_path, exc)
            continue
        runtime_id = ""
        if pcfg.plugin.runtime is not None:
            runtime_config = _copy_runtime_config(pcfg.plugin.runtime)
            runtime_config.id = name
            runtime_config.description = (pcfg.plugin.description or "").strip()
            runtime_config.dir = plugin_dir
            runtime_config.config_path = plugin_path
            runtime_config.block = block
            runtime_config.process_env = plugin_process_env
            if runtime_config.is_worker():
                try:
                    runtime_config.validate()
                except Exception as exc:  # noqa: BLE001
                    report_plugin_config_error(opts, name, plugin_path, exc)
                    continue
                cfg.runtimes.append(runtime_config)
                runtime_id = runtime_config.id
        source = rule_source(
            plugin_name=name,
            plugin_description=(pcfg.plugin.description or "").strip(),
            config_path=plugin_path,
            base_dir=plugin_dir,
            strict_dir=plugin_dir,
            runtime_id=runtime_id,
            block=block,
            process_env=plugin_process_env,
        )
        for rule in pcfg.rules:
            # Go 的 Rule 是值类型，每条规则各持一份 ruleSource 副本；Python 里必须
            # 显式复制，否则同一插件的多条规则会共享一个 source（final_name 互相覆盖）。
            rule.source = _copy_source(source)
        # Go 的 ``Config{Rules: append([]Rule(nil), pcfg.Rules...)}`` 是**结构体拷贝**，
        # 规整/校验作用在副本上，随后追加进 cfg 的是未规整的原件（第 9 步再规整一次）。
        # Python 的列表元素是引用，因此这里深拷贝，保持与 Go 相同的两段式流程。
        plugin_rules = Config(rules=[copy.deepcopy(rule) for rule in pcfg.rules])
        try:
            normalize_loaded_rules(plugin_rules, opts)
            validate_loaded_rules(plugin_rules.rules)
        except (ValueError, RuntimeError) as exc:
            report_plugin_config_error(opts, name, plugin_path, exc)
            continue
        cfg.rules.extend(pcfg.rules)

    try:
        normalize_loaded_rules(cfg, opts)
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f"parse hook rule config {path!r}: {exc}") from exc
    return cfg, path


def plugin_config_path(config_dir: str, ref: PluginRef) -> str:
    """插件配置文件的绝对路径。对应 Go ``pluginConfigPath``。"""
    name = (ref.name or "").strip()
    if not name:
        raise ValueError("plugin name is required")
    rel = (ref.path or "").strip() or os.path.join(name, "hook.toml")
    return safe_relative_path(config_dir, rel)


def safe_relative_path(base: str, rel: str) -> str:
    """把相对路径安全地拼到 base 下。对应 Go ``safeRelativePath``。"""
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        raise ValueError(f"path {rel!r} must be relative")
    clean = os.path.normpath(rel)
    if clean in (".", "..") or clean.startswith(".." + os.sep) or clean.startswith("../"):
        raise ValueError(f"path {rel!r} escapes plugins directory")
    joined = os.path.join(base, clean)
    if not path_within(base, joined):
        raise ValueError(f"path {rel!r} escapes plugins directory")
    return joined


def path_within(base: str, path: str) -> bool:
    """``path`` 是否在 ``base`` 之内。对应 Go ``pathWithin``。"""
    base = os.path.normpath(base)
    path = os.path.normpath(path)
    rel = os.path.relpath(path, base)
    if rel == os.curdir:
        return True
    return rel != ".." and not rel.startswith(".." + os.sep)


def normalize_loaded_rules(cfg: Config, opts: Options) -> None:
    """规整规则并给重名规则编号。对应 Go ``normalizeLoadedRules``。"""
    seen: dict[str, int] = {}
    for index, rule in enumerate(cfg.rules):
        try:
            rule_normalize(rule)
        except ValueError as exc:
            raise ValueError(f"rule {index + 1}: {exc}") from exc
        original = (rule.name or "").strip()
        if not original:
            original = f"rule.{index + 1}"
            rule.name = original
        rule.source.original_name = original
        final = original
        count = seen.get(original, 0)
        if count > 0:
            final = f"{original}.{count}"
            if opts.logger is not None:
                warn = getattr(opts.logger, "warn", None) or getattr(
                    opts.logger, "warning", None
                )
                if callable(warn):
                    warn(
                        "duplicate hook rule name renamed",
                        name=original,
                        final_name=final,
                        config_path=rule.source.config_path,
                    )
        seen[original] = count + 1
        rule.source.final_name = final
        for action in rule.actions:
            action.source = rule.source


def validate_loaded_rules(rules: list[Rule]) -> None:
    """逐条校验。对应 Go ``validateLoadedRules``。"""
    from atri_x.hook.rules.rule import validate_rule

    for index, rule in enumerate(rules):
        try:
            validate_rule(rule)
        except ValueError as exc:
            raise ValueError(f"rule {index + 1}: {exc}") from exc


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def report_plugin_config_error(
    opts: Options, name: str, path: str, err: BaseException
) -> None:
    """插件被跳过。对应 Go ``reportPluginConfigError``。"""
    if opts.logger is not None:
        warn = getattr(opts.logger, "warn", None) or getattr(
            opts.logger, "warning", None
        )
        if callable(warn):
            warn("hook plugin skipped", plugin=name, path=path, error=str(err))
    if opts.notify is not None:
        label = (name or "").strip() or path
        opts.notify(None, f"Hook 插件 {label} 已跳过：{err}")


def report_plugin_config_warning(
    opts: Options, name: str, path: str, message: str
) -> None:
    """插件配置告警。对应 Go ``reportPluginConfigWarning``。"""
    if opts.logger is not None:
        warn = getattr(opts.logger, "warn", None) or getattr(
            opts.logger, "warning", None
        )
        if callable(warn):
            warn("hook plugin warning", plugin=name, path=path, warning=message)
    if opts.notify is not None:
        label = (name or "").strip() or path
        opts.notify(None, f"Hook 插件 {label} 警告：{message}")


def report_config_error(opts: Options, path: str, err: BaseException) -> None:
    """根配置错误。对应 Go ``reportConfigError``。"""
    if opts.logger is not None:
        error = getattr(opts.logger, "error", None)
        if callable(error):
            error("hook rule config error", path=path, error=str(err))
    if opts.notify is not None:
        opts.notify(None, f"Hook rules 配置错误：{err}")


def warn_ignored_plugin_rule_blocks(
    opts: Options, plugin_name: str, path: str, rules: list[Rule]
) -> None:
    """插件规则里写 block 字段时的告警。对应 Go ``warnIgnoredPluginRuleBlocks``。"""
    names: list[str] = []
    for index, rule in enumerate(rules):
        if not has_block_config(rule):
            continue
        name = (rule.name or "").strip() or f"rule.{index + 1}"
        names.append(name)
    if not names:
        return
    report_plugin_config_warning(
        opts,
        plugin_name,
        path,
        "rules "
        + ", ".join(names)
        + " declare blocked_platform/blocked_group/blocked_id; these fields are "
        "ignored in plugin rules, configure them under [plugin] instead",
    )


# ---------------------------------------------------------------------------
# Rule 上的小块 helper（Go 是 Rule 的方法）
# ---------------------------------------------------------------------------
def has_block_config(rule: Rule) -> bool:
    """对应 Go ``Rule.hasBlockConfig``。"""
    return bool(rule.blocked_platforms or rule.blocked_groups or rule.blocked_ids)


def rule_block_policy(rule: Rule) -> BlockPolicy:
    """对应 Go ``Rule.blockPolicy``。"""
    return new_block_policy(
        rule.blocked_platforms, rule.blocked_groups, rule.blocked_ids
    )


def clear_block_config(rule: Rule) -> None:
    """对应 Go ``Rule.clearBlockConfig``。"""
    rule.blocked_platforms = []
    rule.blocked_groups = []
    rule.blocked_ids = []


def rule_normalize(rule: Rule) -> None:
    """规整规则：条件展开 + 内联 action。对应 Go ``Rule.normalize``。"""
    from atri_x.hook.rules.rule import rule_conditions

    rule.match = rule_conditions(rule)
    for action in rule.actions:
        action.normalize()
    if not (rule.action or "").strip():
        return
    if rule.actions:
        raise ValueError("action cannot be combined with actions")
    rule.actions = [inline_action(rule)]


def inline_action(rule: Rule) -> Action:
    """把内联字段拼成一个 action。对应 Go ``Rule.inlineAction``。"""
    action = Action(
        type=rule.action,
        field=rule.field,
        text=rule.text,
        pattern=rule.pattern,
        replace=rule.replace,
        kind=rule.kind,
        path=rule.path,
        timing=rule.timing,
        tool=rule.tool,
        arguments=rule.arguments,
        command=list(rule.command),
        cwd=rule.cwd,
        timeout_seconds=rule.timeout_seconds,
        all=rule.all,
        target=rule.target,
    )
    action.normalize()
    return action


# ---------------------------------------------------------------------------
# TOML → 结构体（替代 Go 的反射解码）
# ---------------------------------------------------------------------------
def decode_config(data: dict[str, Any]) -> Config:
    """严格解码 ``hooks.toml``。"""
    values = strict_table(
        data,
        "hooks.toml",
        {
            "plugins": lambda value, path: strict_array(
                value, path, _plugin_ref
            ),
            "rules": lambda value, path: strict_array(value, path, _rule),
        },
    )
    return Config(**values)


def decode_plugin_config(data: dict[str, Any]) -> plugin_config:
    """严格解码插件 ``hook.toml``。"""
    values = strict_table(
        data,
        "hook.toml",
        {
            "plugin": _plugin_info,
            "rules": lambda value, path: strict_array(value, path, _rule),
        },
    )
    return plugin_config(**values)


def _plugin_ref(value: Any, path: str) -> PluginRef:
    values = strict_table(
        value,
        path,
        {
            "name": strict_string,
            "enabled": strict_bool,
            "path": strict_string,
        },
    )
    return PluginRef(**values)


def _plugin_info(value: Any, path: str) -> PluginInfo:
    values = strict_table(
        value,
        path,
        {
            "name": strict_string,
            "description": strict_string,
            "blocked_platform": strict_string_list,
            "blocked_group": strict_string_list,
            "blocked_id": strict_string_list,
            "runtime": _runtime_config,
        },
    )
    _rename_block_keys(values)
    return PluginInfo(**values)


#: TOML 键名 → 数据类字段名（Go 用 tag，Python 用字段名）。
_BLOCK_KEY_MAP = (
    ("blocked_platform", "blocked_platforms"),
    ("blocked_group", "blocked_groups"),
    ("blocked_id", "blocked_ids"),
)


def _rename_block_keys(values: dict[str, Any]) -> None:
    for source, target in _BLOCK_KEY_MAP:
        if source in values:
            values[target] = values.pop(source)


def _runtime_config(value: Any, path: str) -> RuntimeConfig:
    values = strict_table(
        value,
        path,
        {
            "mode": strict_string,
            "command": strict_string_list,
            "cwd": strict_string,
            "startup_timeout_seconds": strict_int,
            "shutdown_timeout_seconds": strict_int,
            "event_timeout_seconds": strict_int,
            "max_wait_seconds": strict_int,
            "restart": _restart_config,
            "tools": _tools_config,
        },
    )
    return RuntimeConfig(**values)


def _restart_config(value: Any, path: str) -> RestartConfig:
    values = strict_table(
        value,
        path,
        {
            "strategy": strict_string,
            "initial_delay_seconds": strict_int,
            "max_delay_seconds": strict_int,
        },
    )
    return RestartConfig(**values)


def _tools_config(value: Any, path: str) -> ToolsConfig:
    values = strict_table(
        value,
        path,
        {
            "allow": strict_string_list,
            "background_allow": strict_string_list,
        },
    )
    return ToolsConfig(**values)


def _rule(value: Any, path: str) -> Rule:
    values = strict_table(
        value,
        path,
        {
            "name": strict_string,
            "description": strict_string,
            "on": strict_string,
            "priority": strict_int,
            "enabled": strict_bool,
            "wakeup": strict_string,
            "blocked_platform": strict_string_list,
            "blocked_group": strict_string_list,
            "blocked_id": strict_string_list,
            "if": strict_string,
            "op": strict_string,
            "value": strict_string,
            "always": strict_bool,
            "match": lambda item, where: strict_array(item, where, _condition),
            "roles": strict_string_list,
            "actor_roles": strict_string_list,
            "group_roles": strict_string_list,
            "action": strict_string,
            "actions": lambda item, where: strict_array(item, where, _action),
            "field": strict_string,
            "text": strict_string,
            "pattern": strict_string,
            "replace": strict_string,
            "kind": strict_string,
            "path": strict_string,
            "timing": strict_string,
            "tool": strict_string,
            "arguments": strict_string,
            "command": strict_string_list,
            "cwd": strict_string,
            "timeout_seconds": strict_int,
            "all": strict_bool,
            "target": _target,
            "consume": strict_bool,
            "stop_propagation": strict_bool,
        },
    )
    if "if" in values:
        values["if_"] = values.pop("if")
    _rename_block_keys(values)
    return Rule(**values)


def _action(value: Any, path: str) -> Action:
    values = strict_table(
        value,
        path,
        {
            "action_name": strict_string,
            "type": strict_string,
            "field": strict_string,
            "text": strict_string,
            "pattern": strict_string,
            "replace": strict_string,
            "kind": strict_string,
            "url": strict_string,
            "path": strict_string,
            "base64": strict_string,
            "name": strict_string,
            "mime_type": strict_string,
            "user_id": strict_string,
            "message_id": strict_string,
            "emoticon_id": strict_string,
            "timing": strict_string,
            "tool": strict_string,
            "arguments": strict_string,
            "all": strict_bool,
            "command": strict_string_list,
            "cwd": strict_string,
            "timeout_seconds": strict_int,
            "target": _target,
            "outputs": lambda item, where: strict_array(item, where, _segment),
        },
    )
    return Action(**values)


def _condition(value: Any, path: str) -> Condition:
    values = strict_table(
        value,
        path,
        {"field": strict_string, "op": strict_string, "value": strict_string},
    )
    return Condition(**values)


def _target(value: Any, path: str) -> Target:
    values = strict_table(
        value,
        path,
        {
            "platform": strict_string,
            "scope_id": strict_string,
            "private_user_id": strict_string,
            "group_id": strict_string,
            "superadmins": strict_bool,
        },
    )
    return Target(**values)


def _segment(value: Any, path: str) -> Segment:
    values = strict_table(
        value,
        path,
        {
            "kind": strict_string,
            "text": strict_string,
            "url": strict_string,
            "path": strict_string,
            "base64": strict_string,
            "name": strict_string,
            "mime_type": strict_string,
            "user_id": strict_string,
            "message_id": strict_string,
            "emoticon_id": strict_string,
        },
    )
    return Segment(**values)


def _copy_source(source: rule_source) -> rule_source:
    return rule_source(
        plugin_name=source.plugin_name,
        plugin_description=source.plugin_description,
        config_path=source.config_path,
        base_dir=source.base_dir,
        strict_dir=source.strict_dir,
        original_name=source.original_name,
        final_name=source.final_name,
        runtime_id=source.runtime_id,
        block=source.block,
        process_env=source.process_env,
    )


def _copy_runtime_config(config: RuntimeConfig) -> RuntimeConfig:
    return RuntimeConfig(
        mode=config.mode,
        command=list(config.command),
        cwd=config.cwd,
        startup_timeout_seconds=config.startup_timeout_seconds,
        shutdown_timeout_seconds=config.shutdown_timeout_seconds,
        event_timeout_seconds=config.event_timeout_seconds,
        max_wait_seconds=config.max_wait_seconds,
        restart=RestartConfig(
            strategy=config.restart.strategy,
            initial_delay_seconds=config.restart.initial_delay_seconds,
            max_delay_seconds=config.restart.max_delay_seconds,
        ),
        tools=ToolsConfig(
            allow=list(config.tools.allow),
            background_allow=list(config.tools.background_allow),
        ),
        id=config.id,
        description=config.description,
        dir=config.dir,
        config_path=config.config_path,
        block=config.block,
        process_env=config.process_env,
    )


# 交叉检索：Go 的 Config.normalize 只是带空 Options 的 normalizeLoadedRules。
def config_normalize(cfg: Config) -> None:
    """对应 Go ``Config.normalize``。"""
    normalize_loaded_rules(cfg, Options())


StrictDecodeError = StrictDecodeError
