"""Agent 的可选服务装配入口（幂等）。

对应 Go：``internal/agent/core.go`` 里 ``NewWithOptions`` 末尾那一长串装配
（``SetContextOptions`` / ``RegisterDefaultModules`` / ``attachLLMRetryNotifier`` …）。

Python 侧这些能力被拆进三个并行实现的组，各自提供 ``install_*_services`` 装配函数：

============================  ==========================================
装配函数                       注册的门面 / 落点
============================  ==========================================
``model_service``              ``services["models"]``（``ModelService``）
``context_runtime``            ``services["context_runtime"]``
``budget_chat``                ``services["budget"]``
``ratelimit``                  ``services["ratelimit"]``
``tool_directive``             ``services["tool_directive"]`` / ``["skill_directive"]``
============================  ==========================================

**没有调用它们，接缝（:mod:`atri_x.agent.seams`）就只走保守降级** —— 预算、限流、
上下文压缩、工具预载会全部静默失效。本模块是**唯一**的调用点。

两条关键约定：

1. **``deps.models`` 必须是 :class:`~atri_x.agent.model_service.ModelService` 门面**，
   不能是 ``Agent`` 本身 —— ``Agent`` 自带 ``model_profiles`` **字典字段**，实例字段会
   盖住同名方法，命令层的 ``deps.models.model_profiles()`` 会当场 ``TypeError``。
   其余门面同理（``AgentService`` 把方法留在类上、把字段读写转发给宿主）。
2. **幂等**：重复调用不重复绑定、不重复注册。方法安装本身是"缺什么装什么"，
   本模块再用 :data:`_STATE_ATTR` 记住装配结果，第二次调用不会重复登记门面。

装配**逐项尽力而为**：某一项的同僚模块缺失或安装抛错时，只有该项记 warning 并跳过，
其余照装，绝不整体抛出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "AgentServicesReport",
    "install_agent_services",
    "is_bootstrapped",
    "previous_report",
    "services_snapshot",
]


#: 记在宿主上的装配标记（避免重复登记门面）。
_STATE_ATTR = "_agent_services_report"

#: 五项核心装配：``(报告名, 模块名, 函数名)``。
_CORE_INSTALLERS: tuple[tuple[str, str, str], ...] = (
    ("models", "atri_x.agent.model_service", "install_model_services"),
    ("context_runtime", "atri_x.agent.context_runtime", "install_context_services"),
    ("budget", "atri_x.agent.budget_chat", "install_budget_services"),
    ("ratelimit", "atri_x.agent.ratelimit", "install_ratelimit_services"),
    ("tool_directive", "atri_x.agent.tool_directive", "install_tool_directive_services"),
)

#: 不叫 ``install_*_services``、但同样要在装配阶段补上的入口（都由 agent-model 提供，
#: 内部同样是"缺什么装什么"）：``state_watch`` 取 ``refresh_runtime_state``、
#: ``group_policy`` 取 ``save_runtime_state``，都是鸭子类型调用。
_SUPPLEMENTARY_INSTALLERS: tuple[tuple[str, str, str], ...] = (
    ("runtime_state", "atri_x.agent.model_state", "install_runtime_state"),
    ("model_snapshots", "atri_x.agent.model_snapshot", "install_snapshot_services"),
)

#: ``Deps`` 槽位 → ``agent.services`` 键。命令层与接缝是两套键，装配阶段对齐。
_DEPS_MAPPING: tuple[tuple[str, str], ...] = (
    ("models", "models"),
    ("compact", "context_runtime"),
    ("context_status", "context_runtime"),
    ("context_policy", "context_runtime"),
    ("group_policy", "group_policy"),
    ("group_knowledge", "group_knowledge"),
    ("member_panel", "member_panel"),
    ("group_services", "group_services"),
    ("tools", "tools"),
    ("hooks", "hooks"),
    ("logs", "logs"),
)


@dataclass
class AgentServicesReport:
    """一次装配的结果报告（"装了哪些 / 跳过了哪些 / 为什么跳"）。

    ``installed`` / ``skipped`` / ``failed`` 里每一项都是 ``(名字, 说明)``；
    ``services`` 保存门面对象本身，``models`` 是命令层 ``deps.models`` 真正拿到的那一个。
    """

    installed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    services: dict[str, Any] = field(default_factory=dict)
    models: Any = None
    deps: Any = None
    reused: bool = False

    @property
    def ok(self) -> bool:
        """核心装配有没有硬失败（``failed`` 为空即视为可用）。"""
        return not self.failed

    @property
    def installed_names(self) -> list[str]:
        return [name for name, _ in self.installed]

    @property
    def skipped_names(self) -> list[str]:
        return [name for name, _ in self.skipped]

    def to_dict(self) -> dict[str, Any]:
        """机器可读形式（可直接进日志 / 审计）。"""
        return {
            "installed": list(self.installed),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "warnings": list(self.warnings),
            "services": sorted(self.services),
            "models": type(self.models).__name__ if self.models is not None else "",
            "reused": self.reused,
            "ok": self.ok,
        }

    def summary(self) -> str:
        """一行摘要。"""
        return "agent services: installed=%d skipped=%d failed=%d%s" % (
            len(self.installed),
            len(self.skipped),
            len(self.failed),
            " (reused)" if self.reused else "",
        )


def install_agent_services(
    agent: Any, *, deps: Any = None, opts: Any = None
) -> AgentServicesReport:
    """把三组同僚提供的可选服务装到 ``agent`` 上（幂等）。

    :param agent: :class:`atri_x.agent.core.Agent`（或任何提供 ``services`` 字典的宿主）。
    :param deps: ``atri_x.agent.commands.deps.Deps``。``None`` 时用 ``agent.command_deps``；
        仍然没有就跳过 ``Deps`` 接线（命令层未装配）。
    :param opts: 可选的装配覆盖（认得的键见 :func:`_apply_options`）；缺的键一律回落到
        ``agent`` 自己已有的字段。
    :returns: :class:`AgentServicesReport`。
    """
    report = AgentServicesReport()
    report.reused = is_bootstrapped(agent)
    services = _services_dict(agent)
    if services is None:
        report.failed.append(("services", "宿主没有可写的 agent.services 字典"))
        _publish_snapshot(agent, report)
        return report

    options: dict[str, Any] = dict(opts) if opts else {}
    _apply_options(agent, options, report)
    _install_all(agent, report)
    _install_command_facades(agent, report)
    _wire_deps(agent, deps, report)
    _wire_extra_host_fields(agent, options, report)
    if report.reused:
        _restore_ledger(agent, report)

    try:
        setattr(agent, _STATE_ATTR, report)
    except AttributeError:  # pragma: no cover - 宿主用了 __slots__
        pass
    _publish_snapshot(agent, report)
    return report


#: 机器可读装配快照的宿主属性名（``/diagnostics``、doctor 与排障脚本共用）。
_SNAPSHOT_ATTR = "agent_services_report"


def _publish_snapshot(agent: Any, report: AgentServicesReport) -> None:
    """把报告以 :meth:`AgentServicesReport.to_dict` 的形式挂到宿主上。

    ``to_dict()`` 一直只有测试在消费；挂到宿主之后 ``/diagnostics`` 的
    ``agent_services`` 段与 doctor 的能力自检都能读到同一份事实，不必再装配一遍。
    """
    try:
        setattr(agent, _SNAPSHOT_ATTR, report.to_dict())
    except AttributeError:  # pragma: no cover - 宿主用了 __slots__
        pass


def is_bootstrapped(agent: Any) -> bool:
    """宿主是否已经跑过 :func:`install_agent_services`。"""
    return isinstance(getattr(agent, _STATE_ATTR, None), AgentServicesReport)


def previous_report(agent: Any) -> AgentServicesReport | None:
    """取上一次装配报告（没装配过返回 ``None``）。"""
    report = getattr(agent, _STATE_ATTR, None)
    return report if isinstance(report, AgentServicesReport) else None


def services_snapshot(agent: Any) -> dict[str, Any] | None:
    """取宿主上的机器可读装配快照（``/diagnostics`` 与 doctor 共用）。

    生产路径由 :func:`install_agent_services` 写在 ``agent.agent_services_report``；
    只调过 bootstrap 的宿主回落到装配状态对象；都没有返回 ``None``。
    """
    payload = getattr(agent, _SNAPSHOT_ATTR, None)
    if isinstance(payload, dict):
        return dict(payload)
    to_dict = getattr(previous_report(agent), "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        value = to_dict()
    except Exception:  # noqa: BLE001 - 快照读取失败不该让诊断端点整体 500
        return None
    return dict(value) if isinstance(value, dict) else None


def _restore_ledger(agent: Any, report: AgentServicesReport) -> None:
    """把第一次装配的"装了哪些"补进重复调用的报告里。

    第二次调用时各组的 ``install_*`` 是空操作，``_install_all`` 会把它们记成
    ``skipped: already installed``；这里再把上一次的台账原样搬回来，调用方
    无论调几次都能从报告里看出当前装了什么。
    """
    previous = getattr(agent, _STATE_ATTR, None)
    if not isinstance(previous, AgentServicesReport):
        return
    known = set(report.installed_names) | set(report.skipped_names)
    for name, description in previous.installed:
        if name in known:
            continue
        report.skipped.append((name, "already installed: %s" % (description or "ok")))
        known.add(name)
    for name, description in previous.services.items():
        report.services.setdefault(name, description)
    if report.models is None:
        report.models = previous.models


# ---------------------------------------------------------------------------
# 五项核心装配 + 两项补充装配
# ---------------------------------------------------------------------------
def _install_all(agent: Any, report: AgentServicesReport) -> None:
    # 第二次调用时各组的 ``install_*`` 都是空操作（门面已存在），因此不再记 ``installed``。
    first = not report.reused
    for name, module_name, function_name in _CORE_INSTALLERS:
        _install_one(agent, report, name, module_name, function_name, core=True, first=first)
    for name, module_name, function_name in _SUPPLEMENTARY_INSTALLERS:
        _install_one(
            agent, report, name, module_name, function_name, core=False, first=first
        )


def _install_one(
    agent: Any,
    report: AgentServicesReport,
    name: str,
    module_name: str,
    function_name: str,
    *,
    core: bool,
    first: bool,
) -> Any:
    """跑一个 ``install_*`` 入口并把结果记进报告。"""
    return _guard(
        report,
        name,
        lambda: _invoke(module_name, function_name, agent),
        core=core,
        first=first,
    )


def _invoke(module_name: str, function_name: str, agent: Any) -> Any:
    import importlib

    module = importlib.import_module(module_name)
    installer: Callable[[Any], Any] = getattr(module, function_name)
    return installer(agent)


def _guard(
    report: AgentServicesReport,
    name: str,
    target: Callable[[], Any],
    *,
    core: bool,
    first: bool = True,
) -> Any:
    """执行 ``target``；失败或缺失时落到 ``report``，不向上抛。"""
    try:
        result = target()
    except ImportError as exc:
        reason = "module or symbol missing: %s" % exc
        _skip(report, name, reason, core)
        return None
    except Exception as exc:  # noqa: BLE001 - 单项失败不能拖垮整体装配
        _skip(report, name, "%s: %s" % (type(exc).__name__, exc), core)
        return None
    if first:
        report.installed.append((name, _describe(result)))
    else:
        report.skipped.append((name, "already installed"))
    return result


def _skip(report: AgentServicesReport, name: str, reason: str, core: bool) -> None:
    if core:
        report.failed.append((name, reason))
    else:
        report.skipped.append((name, reason))
    report.warnings.append("%s skipped: %s" % (name, reason))


def _describe(result: Any) -> str:
    """把装配返回值渲染成一行说明。"""
    if result is None:
        return ""
    if isinstance(result, (list, tuple)):
        items = [str(item) for item in result]
        if items and all(item and "." not in item for item in items):
            return "methods: " + ",".join(items)
        return ",".join(type(item).__name__ for item in items)
    return type(result).__name__


# ---------------------------------------------------------------------------
# 命令层门面（``services`` 里那些按协议消费的槽位）
# ---------------------------------------------------------------------------
def _install_command_facades(agent: Any, report: AgentServicesReport) -> None:
    """补齐 ``Deps`` 上按协议消费的服务槽位。

    命令层的 ``deps.<槽位>`` 与接缝的 ``services["..."]`` 是**两套键**，这里按
    "已有就用、没有就现建"的原则补齐，并同步进 ``agent.services``。
    """
    services = _services_dict(agent) or {}
    for protocol, factory in (
        ("tools", _tools_facade),
        ("member_panel", _member_panel_facade),
    ):
        if services.get(protocol) is not None:
            report.skipped.append((protocol, "already registered"))
            continue
        try:
            services[protocol] = factory(agent)
        except Exception as exc:  # noqa: BLE001 - 门面构造失败只影响该槽位
            reason = "%s: %s" % (type(exc).__name__, exc)
            report.skipped.append((protocol, reason))
            report.warnings.append("%s facade skipped: %s" % (protocol, reason))
            continue
        report.installed.append((protocol, type(services[protocol]).__name__))
        report.services[protocol] = services[protocol]

    # ``context_runtime`` 门面同时满足 compact / context_status / context_policy 三个协议。
    runtime = services.get("context_runtime")
    if runtime is not None:
        for alias in ("compact", "context_status", "context_policy"):
            services.setdefault(alias, runtime)

    # ``logs`` / ``hooks`` / ``doctor`` 由 app 层注入，装配阶段只做转发登记。
    # ``hooks`` 额外要求"真的是个 hook 服务"（至少能 ``hook_list()``）：``Agent.hooks``
    # 是执行期的 HookManager，只实现了 ``run``，把它当 ``Deps.Hooks`` 会让 ``/hooks``
    # 抛 AttributeError（Go 里这两者是不同类型，由 app 层各自注入）。
    for key, attribute in (("logs", "logs"), ("doctor", "doctor")):
        if services.get(key) is not None:
            continue
        value = getattr(agent, attribute, None)
        if value is not None:
            services[key] = value
    if services.get("hooks") is None:
        hook_service = getattr(agent, "hook_service", None)
        if hook_service is not None:
            services["hooks"] = hook_service
    if services.get("hooks") is None:
        manager = getattr(agent, "hooks", None)
        if manager is not None and callable(getattr(manager, "hook_list", None)):
            services["hooks"] = manager


def _tools_facade(agent: Any) -> Any:
    """``Deps.Tools`` 门面：``list`` / ``unregister`` / ``remove`` / ``reload``。"""
    from atri_x.agent.tools_service import new_tools_service

    return new_tools_service(agent)


def _member_panel_facade(agent: Any) -> Any:
    """``Deps.MemberPanel`` 门面：``member_panel(ctx, view)``。"""
    return _MemberPanelService(agent)


class _HostService:
    """转发型门面的公共基类（宿主缺什么就转发什么）。

    与 :class:`atri_x.agent.tools.AgentService` 同构：字段读写落到宿主，方法留在
    子类上。这样门面可以安全地持有 ``Agent``，而不会被 ``Agent`` 的同名字段盖住。
    """

    def __init__(self, agent: Any) -> None:
        object.__setattr__(self, "_agent", agent)

    @property
    def agent(self) -> Any:
        return object.__getattribute__(self, "_agent")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_agent"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, "_agent"), name, value)


class _MemberPanelService(_HostService):
    """``MemberPanelMixin.member_panel`` 的服务包装。

    ``Deps.MemberPanel`` 的协议声明是 ``async def``，但命令侧统一用 ``_maybe_await``
    调用，因此这里直接返回同步结果即可（``member_panel`` 本身是同步实现）。
    """

    def member_panel(self, ctx: Any, view: str) -> str:
        from atri_x.agent.member_panel import member_panel

        return member_panel(self.agent, ctx, view)


# ---------------------------------------------------------------------------
# ``Deps`` 接线
# ---------------------------------------------------------------------------
def _wire_deps(agent: Any, deps: Any, report: AgentServicesReport) -> None:
    """把装配好的服务写进 ``deps`` 的对应槽位。"""
    target = deps if deps is not None else getattr(agent, "command_deps", None)
    if target is None:
        report.skipped.append(("deps", "命令层未装配（没有 Deps 实例）"))
        report.warnings.append("deps wiring skipped: no Deps instance")
        return
    report.deps = target

    services = _services_dict(agent) or {}
    # 群策略 / 群知识 / 群服务：Go 里 ``Deps`` 直接接 ``a``（Agent 自己持有那些方法）。
    # Python 侧 Agent 已经继承 ``GroupPolicyMixin`` / ``GroupKnowledgeMixin`` /
    # ``SignupServicesMixin``，所以"服务"就是宿主自己；``agent.services`` 里若另有门面
    # （app 层注入）则优先用门面。
    for attribute in ("group_policy", "group_knowledge", "group_services"):
        service = services.get(attribute)
        if service is None:
            service = agent
        if _assign_dep(target, attribute, service):
            report.services.setdefault(attribute, service)
    for attribute, key in _DEPS_MAPPING:
        if key in ("group_policy", "group_knowledge", "group_services"):
            continue  # 上面已经处理
        service = services.get(key)
        if service is None:
            continue
        if _assign_dep(target, attribute, service):
            report.services.setdefault(attribute, service)

    report.models = services.get("models")
    if report.models is None:
        report.warnings.append("deps.models 未装配：模型服务缺失，模型命令会整条禁用")
    elif report.models is agent:  # pragma: no cover - 必须拦下的错误接线
        report.warnings.append(
            "deps.models 接成了 Agent 本身：model_profiles 字段会盖住同名方法"
        )


def _assign_dep(deps: Any, attribute: str, value: Any) -> bool:
    """写 ``Deps`` 字段；字段不存在或已有真实值时不覆盖。"""
    if not hasattr(deps, attribute):
        return False
    current = getattr(deps, attribute, None)
    if current is value or current is not None:
        return False
    try:
        setattr(deps, attribute, value)
    except AttributeError:  # pragma: no cover - 冻结的宿主
        return False
    return True


# ---------------------------------------------------------------------------
# 宿主的其余装配点
# ---------------------------------------------------------------------------
def _apply_options(
    agent: Any, options: dict[str, Any], report: AgentServicesReport
) -> None:
    """把 ``opts`` 里认得的键补到宿主字段上（缺的保持原值）。"""
    from atri_x.config import ContextConfig, ModelMetadataConfig

    if getattr(agent, "context_config", None) is None:
        agent.context_config = options.get("context_config") or ContextConfig()
    if getattr(agent, "model_metadata", None) is None:
        agent.model_metadata = options.get("model_metadata") or ModelMetadataConfig()
    for attribute in (
        "budget_limits",
        "pricing",
        "tool_provider",
        "skills",
        "rate_limit_user",
        "rate_limit_group",
        # 阈值是纯展示字段（``RateLimitStatus`` 要读），限流器对象才是判定依据；
        # 两者由装配方（``app/runtime_agent.py``）按 ``[ops]`` 配置成对传入。
        "rate_limit_user_per_minute",
        "rate_limit_user_burst",
        "rate_limit_group_per_minute",
        "rate_limit_group_burst",
        "state_store",
        "tools_config",
        "hook_service",
    ):
        value = options.get(attribute)
        if value is not None and getattr(agent, attribute, None) is None:
            try:
                setattr(agent, attribute, value)
            except AttributeError:  # pragma: no cover - 只读宿主
                report.warnings.append("option %s not applied: read-only host" % attribute)


def _wire_extra_host_fields(
    agent: Any, options: dict[str, Any], report: AgentServicesReport
) -> None:
    """那些"装完服务才成立"的宿主字段：上下文压缩配置与工具运行时。"""
    try:
        _configure_context(agent, options)
    except Exception as exc:  # noqa: BLE001 - 压缩配置失败只降级
        reason = "%s: %s" % (type(exc).__name__, exc)
        report.skipped.append(("context_options", reason))
        report.warnings.append("context configure skipped: %s" % reason)

    registry = options.get("registry")
    tools = (_services_dict(agent) or {}).get("tools")
    if registry is None or tools is None:
        return
    setter = getattr(tools, "set_tool_runtime", None)
    if not callable(setter):
        return
    try:
        setter(registry, options.get("skills") or getattr(agent, "skills", None))
    except Exception as exc:  # noqa: BLE001 - 工具运行时可选
        reason = "%s: %s" % (type(exc).__name__, exc)
        report.skipped.append(("tool_runtime", reason))
        report.warnings.append("set_tool_runtime skipped: %s" % reason)


def _configure_context(agent: Any, options: dict[str, Any]) -> None:
    """把上下文运行态接上窗口解析器与压缩后端（对应 Go ``SetContextOptions``）。"""
    from atri_x.agent.context_runtime import ensure_context_runtime
    from atri_x.config import ContextConfig, ModelMetadataConfig, ModelSelection

    state = ensure_context_runtime(agent)
    ctx_cfg = options.get("context_config") or getattr(agent, "context_config", None)
    if not isinstance(ctx_cfg, ContextConfig):
        ctx_cfg = ContextConfig()
    metadata = options.get("model_metadata") or getattr(agent, "model_metadata", None)
    if not isinstance(metadata, ModelMetadataConfig):
        metadata = ModelMetadataConfig()
    providers = options.get("providers") or getattr(agent, "providers", None) or {}
    compact = options.get("compact_model") or state.configured_compact_model()
    if not isinstance(compact, ModelSelection):
        compact = ModelSelection()
    state.configure(ctx_cfg, metadata, providers, compact, agent.client_for_provider)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _services_dict(agent: Any) -> dict[str, Any] | None:
    services = getattr(agent, "services", None)
    return services if isinstance(services, dict) else None
