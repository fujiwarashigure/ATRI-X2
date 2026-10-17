"""``atri_x.agent``：Agent 核心、回合流程、事件、收件箱与外围能力。

对应 Go：``internal/agent``。

本包的公开面分四层：

1. **核心**：:class:`~atri_x.agent.core.Agent` 与三个构造入口
   （``new_options`` / ``new`` / ``new_with_prefixes``）；``core`` 会把各主题方法
   转发到同名兄弟模块（``output`` / ``input`` / ``inbox`` / ``hooks`` …），并按
   PORTING.md §3.1 组合各组提供的 mixin。
2. **装配**：:func:`~atri_x.agent.bootstrap.install_agent_services` —— 唯一的可选服务
   装配入口。**不调用它，预算 / 限流 / 上下文压缩 / 工具预载全部静默降级。**
3. **主题模块**：``chat`` / ``chat_llm`` / ``chat_tools``（回合流程）、``events`` /
   ``hooks`` / ``wakeup`` / ``inbox`` / ``turn_output`` / ``output`` / ``input``
   等，函数名与 Go 方法名逐一对应，便于交叉检索。
4. **异常**：``atri_x.errors`` 里的全部异常类与哨兵实例在本模块再导出，
   调用方可以统一 ``from atri_x.agent import ErrNotFound``。

**导入健壮性**：本模块的每一个子模块引用都包在 ``try/except`` 里，任何一个子模块缺失
都只少几个再导出名，不会让 ``import atri_x.agent`` 失败。``__all__`` 只收真正绑定成功的
名字（``scripts/check_imports.py`` 会校验这一点）。
"""

from __future__ import annotations

from typing import Any

__all__: list[str] = []


def _export(name: str, value: Any) -> None:
    """绑定一个公开名并登记进 ``__all__``。"""
    globals()[name] = value
    __all__.append(name)


def _export_from(module_name: str, attribute: str, alias: str = "") -> Any:
    """按需从某个子模块再导出符号；模块或符号缺失时静默跳过。"""
    import importlib

    name = alias or attribute
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - 子模块缺失不能让整包导入失败
        return None
    value = getattr(module, attribute, None)
    if value is None:
        return None
    _export(name, value)
    return value


# ---------------------------------------------------------------------------
# 1. 核心：Agent 与构造入口
# ---------------------------------------------------------------------------
from atri_x.agent.core import (  # noqa: E402
    Agent,
    FileSoulProvider,
    SoulProvider,
    StaticSoulProvider,
    new,
    new_options,
    new_with_prefixes,
)

_export("Agent", Agent)
_export("FileSoulProvider", FileSoulProvider)
_export("SoulProvider", SoulProvider)
_export("StaticSoulProvider", StaticSoulProvider)
_export("new", new)
_export("new_options", new_options)
_export("new_with_prefixes", new_with_prefixes)

# ---------------------------------------------------------------------------
# 2. 装配入口（bootstrap）
# ---------------------------------------------------------------------------
from atri_x.agent.bootstrap import (  # noqa: E402
    AgentServicesReport,
    install_agent_services,
    is_bootstrapped,
    previous_report,
)

_export("AgentServicesReport", AgentServicesReport)
_export("install_agent_services", install_agent_services)
_export("is_bootstrapped", is_bootstrapped)
_export("previous_report", previous_report)

#: 生产装配常用的一组门面类型（命令层与接缝共享同一份）。
from atri_x.agent.model_service import ModelService  # noqa: E402
from atri_x.agent.options import Options  # noqa: E402
from atri_x.agent.turn_output import (  # noqa: E402
    BackgroundTurnOutput,
    ForegroundTurnOutput,
    TurnOutput,
)

_export("BackgroundTurnOutput", BackgroundTurnOutput)
_export("ForegroundTurnOutput", ForegroundTurnOutput)
_export("ModelService", ModelService)
_export("Options", Options)
_export("TurnOutput", TurnOutput)

# ---------------------------------------------------------------------------
# 3. 主题模块（本组归属，必然存在）
# ---------------------------------------------------------------------------
_TOPIC_MODULES = (
    "asr",
    "bootstrap",
    "budget",
    "budget_chat",
    "budget_keys",
    "budget_ledger",
    "character_directive",
    "chat",
    "chat_llm",
    "chat_observe",
    "chat_stream",
    "chat_tools",
    "chat_turn",
    "chat_vision",
    "command_runtime",
    "completion",
    "context_compact",
    "context_policy",
    "context_runtime",
    "context_seed",
    "context_usage",
    "core",
    "core_build",
    "core_forward",
    "cron",
    "cron_tools",
    "errors",
    "events",
    "group_knowledge",
    "group_policy",
    "group_policy_rules",
    "group_runtime",
    "group_services_polls",
    "group_services_reminders",
    "group_services_signups",
    "group_services_state",
    "hooks",
    "identity",
    "inbox",
    "inbound_media",
    "inbound_speaker",
    "input",
    "logging",
    "media_output",
    "member_panel",
    "memory_learning_admin",
    "message",
    "model",
    "model_agent",
    "model_list",
    "model_policy",
    "model_service",
    "model_snapshot",
    "model_state",
    "notice_queue",
    "ops",
    "options",
    "output",
    "platform_signals",
    "prefix",
    "prompt",
    "ratelimit",
    "request_context",
    "risk_confirmation",
    "seams",
    "segments",
    "session_metadata",
    "state_watch",
    "status",
    "system_prompt",
    "system_prompt_sources",
    "title",
    "tool_auth",
    "tool_cache",
    "tool_directive",
    "tool_directive_skill",
    "tool_schemas",
    "tool_tag_config",
    "tool_transcript",
    "toolrun_adapter",
    "toolrun_prompt_provider",
    "tools",
    "tools_service",
    "turn_gate",
    "turn_output",
    "turn_override",
    "vision",
    "wakeup",
    "workspace",
)


def _install_topic_modules() -> list[str]:
    """把所有已落地的主题子模块绑定到包命名空间。"""
    import importlib

    installed: list[str] = []
    for module_name in _TOPIC_MODULES:
        if module_name in globals():
            continue
        try:
            module = importlib.import_module(f"atri_x.agent.{module_name}")
        except Exception:  # noqa: BLE001 - 可选子模块
            continue
        _export(module_name, module)
        installed.append(module_name)
    return installed


_install_topic_modules()

# ---------------------------------------------------------------------------
# 4. 同僚组的类型 / mixin（缺哪个跳哪个）
# ---------------------------------------------------------------------------
_OPTIONAL_EXPORTS: dict[str, tuple[str, str]] = {
    # 模型组
    "AgentService": ("atri_x.agent.model", "AgentService"),
    "ModelMixin": ("atri_x.agent.model_service", "ModelMixin"),
    "ModelListMixin": ("atri_x.agent.model_list", "ModelListMixin"),
    "ModelPolicyMixin": ("atri_x.agent.model_policy", "ModelPolicyMixin"),
    "ModelProfile": ("atri_x.agent.model", "ModelProfile"),
    "ModelRuntimeState": ("atri_x.agent.model", "ModelRuntimeState"),
    "ModelSnapshot": ("atri_x.agent.model", "ModelSnapshot"),
    "ModelSnapshotMixin": ("atri_x.agent.model_snapshot", "ModelSnapshotMixin"),
    "ModelOption": ("atri_x.agent.model", "ModelOption"),
    "RuntimeStateMixin": ("atri_x.agent.model_state", "RuntimeStateMixin"),
    "install_model_services": (
        "atri_x.agent.model_service",
        "install_model_services",
    ),
    "new_model_runtime_state": ("atri_x.agent.model", "new_model_runtime_state"),
    # 上下文组
    "ContextCompactMixin": ("atri_x.agent.context_compact", "ContextCompactMixin"),
    "ContextPolicyMixin": ("atri_x.agent.context_policy", "ContextPolicyMixin"),
    "ContextRuntimeMixin": ("atri_x.agent.context_runtime", "ContextRuntimeMixin"),
    "ContextRuntimeState": ("atri_x.agent.context_runtime", "ContextRuntimeState"),
    "ContextSeedMixin": ("atri_x.agent.context_seed", "ContextSeedMixin"),
    "ContextUsageMixin": ("atri_x.agent.context_usage", "ContextUsageMixin"),
    "install_context_services": (
        "atri_x.agent.context_runtime",
        "install_context_services",
    ),
    # 预算 / 限流
    "BudgetMixin": ("atri_x.agent.budget_chat", "BudgetMixin"),
    "BudgetService": ("atri_x.agent.budget_chat", "BudgetService"),
    "RateLimitMixin": ("atri_x.agent.ratelimit", "RateLimitMixin"),
    "install_budget_services": ("atri_x.agent.budget_chat", "install_budget_services"),
    "install_ratelimit_services": ("atri_x.agent.ratelimit", "install_ratelimit_services"),
    # 群策略 / 群知识 / 群运行态
    "GroupKnowledgeMixin": ("atri_x.agent.group_knowledge", "GroupKnowledgeMixin"),
    "GroupPolicyMixin": ("atri_x.agent.group_policy", "GroupPolicyMixin"),
    "GroupRuntimeMixin": ("atri_x.agent.group_runtime", "GroupRuntimeMixin"),
    "GroupServicesMixin": ("atri_x.agent.group_services_reminders", "GroupServicesMixin"),
    "RuntimeSectionsMixin": ("atri_x.agent.group_runtime", "RuntimeSectionsMixin"),
    "SignupServicesMixin": ("atri_x.agent.group_services_signups", "SignupServicesMixin"),
    # 工具组
    "AgentToolRunDeps": ("atri_x.agent.toolrun_adapter", "AgentToolRunDeps"),
    "AgentToolsService": ("atri_x.agent.tools_service", "AgentToolsService"),
    "CronMixin": ("atri_x.agent.cron", "CronMixin"),
    "MemberPanelMixin": ("atri_x.agent.member_panel", "MemberPanelMixin"),
    "ToolAuthMixin": ("atri_x.agent.tool_auth", "ToolAuthMixin"),
    "ToolCacheMixin": ("atri_x.agent.tool_cache", "ToolCacheMixin"),
    "ToolDirectiveMixin": ("atri_x.agent.tool_directive", "ToolDirectiveMixin"),
    "ToolRuntimeMixin": ("atri_x.agent.tools", "ToolRuntimeMixin"),
    "ToolRuntimeState": ("atri_x.agent.tools", "ToolRuntimeState"),
    "ToolTagConfigMixin": ("atri_x.agent.tool_tag_config", "ToolTagConfigMixin"),
    "ToolTranscriptMixin": ("atri_x.agent.tool_transcript", "ToolTranscriptMixin"),
    "install_tool_directive_services": (
        "atri_x.agent.tool_directive",
        "install_tool_directive_services",
    ),
    "new_tools_service": ("atri_x.agent.tools_service", "new_tools_service"),
    # 媒体 / 语音 / 视觉
    "AsrMixin": ("atri_x.agent.asr", "AsrMixin"),
    "InboundMediaMixin": ("atri_x.agent.inbound_media", "InboundMediaMixin"),
    "InboundSpeakerMixin": ("atri_x.agent.inbound_speaker", "InboundSpeakerMixin"),
    "MediaOutputMixin": ("atri_x.agent.media_output", "MediaOutputMixin"),
    "VisionMixin": ("atri_x.agent.vision", "VisionMixin"),
    # 提示词
    "PromptBuildRequest": ("atri_x.agent.prompt", "PromptBuildRequest"),
    "PromptBuilder": ("atri_x.agent.prompt", "PromptBuilder"),
}

for _name, (_module_name, _attribute) in _OPTIONAL_EXPORTS.items():
    _export_from(_module_name, _attribute, _name)
del _name, _module_name, _attribute

# ---------------------------------------------------------------------------
# 命令层：注册入口 + 模型视图类型的唯一出处再导出
# ---------------------------------------------------------------------------


def _install_command_exports() -> list[str]:
    """从 :mod:`atri_x.agent.commands` 再导出命令层的公共名。"""
    import importlib

    try:
        commands = importlib.import_module("atri_x.agent.commands")
    except Exception:  # noqa: BLE001 - 命令层可选
        return []
    installed: list[str] = []
    for name in (
        "CommandGroup",
        "Deps",
        "ModelListOptions",
        "ModelListResult",
        "ModelOption",
        "ModelProfile",
        "ModelProviderError",
        "ModelSnapshot",
        "ModelSnapshotSlot",
        "default_modules",
        "new_command_group",
        "new_default_router",
        "register_default_modules",
        "register_factories",
        "register_modules",
    ):
        value = getattr(commands, name, None)
        if value is None or name in globals():
            continue
        _export(name, value)
        installed.append(name)
    return installed


_install_command_exports()

# ---------------------------------------------------------------------------
# 异常：``atri_x.errors`` 全量再导出
# ---------------------------------------------------------------------------


def _install_exception_exports() -> list[str]:
    """把 :mod:`atri_x.errors` 的异常类与哨兵实例全部再导出。"""
    from atri_x import errors as shared_errors
    from atri_x.agent import errors as agent_errors

    installed: list[str] = []
    for name in dir(shared_errors):
        if name.startswith("_"):
            continue
        if not (name.startswith("Err") or name.endswith("Error")):
            continue
        _export(name, getattr(shared_errors, name))
        installed.append(name)
    # ``atri_x.agent.errors`` 里的用户可见错误助手（对应 Go ``internal/agent/errors.go``）。
    for name in (
        "MAX_USER_ERROR_RUNES",
        "UserErrorDetails",
        "new_user_error_details",
        "safe_error",
        "should_notify_user_error",
    ):
        value = getattr(agent_errors, name, None)
        if value is not None and name not in globals():
            _export(name, value)
            installed.append(name)
    return installed


_install_exception_exports()

#: 供交叉检索：``atri_x.errors`` 里没有被命名为 ``ErrXxx``/``XxxError`` 的公开符号
#: （当前没有），这里保留一个占位以便将来补。
SHARED_ERRORS_MODULE = "atri_x.errors"
_export("SHARED_ERRORS_MODULE", SHARED_ERRORS_MODULE)


def agent_package_info() -> dict[str, Any]:
    """本包的自检信息（模块清单 + 可选导出是否落地）。"""
    return {
        "modules": [name for name in _TOPIC_MODULES if name in globals()],
        "optional_exports": sorted(
            name for name in _OPTIONAL_EXPORTS if name in globals()
        ),
        "errors_module": SHARED_ERRORS_MODULE,
        "exports": len(__all__),
    }


_export("agent_package_info", agent_package_info)
