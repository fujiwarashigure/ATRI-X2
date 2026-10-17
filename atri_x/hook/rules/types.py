"""``hooks.toml`` 的配置类型。

对应 Go：``internal/hook/rules/rules.go`` 里的 ``Options`` / ``Config`` /
``Rule`` / ``Action`` / ``ruleSource`` 等结构体。

拆出这个文件是为了打断循环 import：Go 同包跨文件共享类型，Python 必须把
类型放在兄弟模块都能 import 的地方。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from atri_x.delivery.delivery import Output, Target as DeliveryTarget
from atri_x.hook.match import BlockPolicy, Condition
from atri_x.hook.output import Segment, Target
from atri_x.hook.process import ProcessEnvironment
from atri_x.hook.runtime.config import Config as RuntimeConfig
from atri_x.llm import llm

#: 配置文件名。对应 Go ``ConfigFile``。
CONFIG_FILE = "hooks.toml"
#: 规则默认优先级。对应 Go ``DefaultPriority``。
DEFAULT_PRIORITY = 1000

#: ``SegmentSpec`` 就是输出层的 ``Segment``。对应 Go 的类型别名。
SegmentSpec = Segment

__all__ = [
    "CONFIG_FILE",
    "DEFAULT_PRIORITY",
    "SegmentSpec",
    "Target",
    "Options",
    "Config",
    "PluginRef",
    "PluginInfo",
    "plugin_config",
    "Rule",
    "Action",
    "rule_source",
    "PlatformAPICaller",
    "PlatformCallerResolver",
]


@dataclass
class Options:
    """``rules`` 模块的外部依赖。对应 Go ``Options``。

    ``send`` 是 async 可调用：``await send(ctx, target, outputs) -> Receipt``；
    ``notify`` 是同步可调用：``notify(ctx, text)``。
    """

    config_dir: str = ""
    tools: Any = None
    logger: Any = None
    audit: Callable[..., None] | None = None
    notify: Callable[..., None] | None = None
    send: Callable[..., Any] | None = None
    platform_callers: Any = None
    runtime: Any = None
    process_env: ProcessEnvironment = field(default_factory=ProcessEnvironment)


@dataclass
class Config:
    """``hooks.toml`` 顶层。对应 Go ``Config``。"""

    plugins: list["PluginRef"] = field(default_factory=list)
    rules: list["Rule"] = field(default_factory=list)
    runtimes: list[RuntimeConfig] = field(default_factory=list)


@dataclass
class PluginRef:
    """``[[plugins]]`` 里的一条引用。对应 Go ``PluginRef``。"""

    name: str = ""
    enabled: bool | None = None
    path: str = ""

    def is_enabled(self) -> bool:
        """对应 Go ``PluginRef.enabled``（``nil`` 视为启用）。"""
        return self.enabled is None or bool(self.enabled)


@dataclass
class PluginInfo:
    """插件 ``hook.toml`` 里的 ``[plugin]``。对应 Go ``PluginInfo``。"""

    name: str = ""
    description: str = ""
    blocked_platforms: list[str] = field(default_factory=list)
    blocked_groups: list[str] = field(default_factory=list)
    blocked_ids: list[str] = field(default_factory=list)
    runtime: RuntimeConfig | None = None


@dataclass
class plugin_config:
    """插件 ``hook.toml`` 的整体。对应 Go ``pluginConfig``。"""

    plugin: PluginInfo = field(default_factory=PluginInfo)
    rules: list["Rule"] = field(default_factory=list)


@dataclass
class rule_source:
    """一条规则的来源信息（Go 里是非导出的 ``ruleSource``）。"""

    plugin_name: str = ""
    plugin_description: str = ""
    config_path: str = ""
    base_dir: str = ""
    strict_dir: str = ""
    original_name: str = ""
    final_name: str = ""
    runtime_id: str = ""
    block: BlockPolicy = field(default_factory=BlockPolicy)
    process_env: ProcessEnvironment = field(default_factory=ProcessEnvironment)


@dataclass
class Action:
    """一条动作。对应 Go ``Action``。

    注意：本类有一个名为 ``field`` 的属性（对齐 Go 的 ``Field``），它会在类体里
    遮蔽 ``dataclasses.field``，所以下面的默认值必须用 ``dataclasses.field``
    全名限定（与 ``atri_x.hook.match.RegexMatch`` 同一个坑）。
    """

    action_name: str = ""
    type: str = ""
    field: str = ""
    text: str = ""
    pattern: str = ""
    #: Go 是 ``toml:"-"``：由 :meth:`normalize` 从 ``pattern`` 复制过来。
    match: str = ""
    replace: str = ""
    kind: str = ""
    url: str = ""
    path: str = ""
    base64: str = ""
    name: str = ""
    mime_type: str = ""
    user_id: str = ""
    message_id: str = ""
    emoticon_id: str = ""
    timing: str = ""
    tool: str = ""
    arguments: str = ""
    all: bool = False
    command: list[str] = dataclasses.field(default_factory=list)
    cwd: str = ""
    timeout_seconds: int = 0
    target: Target = dataclasses.field(default_factory=Target)
    outputs: list[SegmentSpec] = dataclasses.field(default_factory=list)
    source: rule_source = dataclasses.field(default_factory=rule_source)

    def normalize(self) -> None:
        """对应 Go ``Action.normalize``：``match`` 缺省取 ``pattern``。"""
        if not self.match:
            self.match = self.pattern


@dataclass
class Rule:
    """一条 Hook 规则。对应 Go ``Rule``（TOML 字段名逐字照抄 tag）。

    与 ``Action`` 一样，``field`` 属性会遮蔽 ``dataclasses.field``，因此下面
    一律用全名限定。
    """

    name: str = ""
    description: str = ""
    on: str = ""
    priority: int = 0
    enabled: bool | None = None
    wakeup: str = ""
    blocked_platforms: list[str] = dataclasses.field(default_factory=list)
    blocked_groups: list[str] = dataclasses.field(default_factory=list)
    blocked_ids: list[str] = dataclasses.field(default_factory=list)
    #: Go 的字段名是 ``If``（``if`` 是 Python 关键字）。
    if_: str = ""
    op: str = ""
    value: str = ""
    always: bool = False
    match: list[Condition] = dataclasses.field(default_factory=list)
    roles: list[str] = dataclasses.field(default_factory=list)
    actor_roles: list[str] = dataclasses.field(default_factory=list)
    group_roles: list[str] = dataclasses.field(default_factory=list)
    action: str = ""
    actions: list[Action] = dataclasses.field(default_factory=list)
    field: str = ""
    text: str = ""
    pattern: str = ""
    replace: str = ""
    kind: str = ""
    path: str = ""
    timing: str = ""
    tool: str = ""
    arguments: str = ""
    command: list[str] = dataclasses.field(default_factory=list)
    cwd: str = ""
    timeout_seconds: int = 0
    all: bool = False
    target: Target = dataclasses.field(default_factory=Target)
    consume: bool = False
    stop_propagation: bool = False
    source: rule_source = dataclasses.field(default_factory=rule_source)

    def is_enabled(self) -> bool:
        """对应 Go ``Rule.enabled``（``nil`` 视为启用）。"""
        return self.enabled is None or bool(self.enabled)


@runtime_checkable
class PlatformAPICaller(Protocol):
    """能代替插件调用平台 API。对应 Go ``PlatformAPICaller``。"""

    async def call_platform_api(
        self, ctx: Any, api: str, params: dict[str, Any]
    ) -> Any: ...


@runtime_checkable
class PlatformCallerResolver(Protocol):
    """按平台名找 caller。对应 Go ``PlatformCallerResolver``。"""

    def platform_caller(self, platform: str) -> tuple[Any, bool]: ...


# 交叉检索：Go 里这些动作/输出类型来自 delivery 与 llm。
_DELIVERY_TARGET = DeliveryTarget
_OUTPUT = Output
_MESSAGE_SEGMENT = llm.MessageSegment
