"""``internal/agent/commands`` 的 Python 移植：slash 命令族与注册入口。

对应 Go 包：``internal/agent/commands``（29 个生产文件 / 5,411 行）。

模块布局（Go 文件 → Python 模块）：

==============================  ====================================================
Python 模块                     对应 Go 文件
==============================  ====================================================
``deps.py``                     ``register.go``：注册机制、服务 Protocol、``Deps``
``model_types.py``              ``register.go`` 的模型服务数据类型（拆出便于阅读）
``help.py``                     ``help.go`` + ``help_util.go``
``completion.py``               ``completion_helpers.go``
``session_format.py``           ``session_format.go``
``session_lifecycle.py``        ``session_lifecycle.go``
``session_navigation.py``       ``session_navigation.go``
``session_core.py``             ``session_core.go`` + ``session_mode.go``
``session_module.py``           ``session_module.go``
``model.py``                    ``model.go``
``log_args.py``                 ``log.go`` 的参数解析部分（拆分）
``log_format.py``               ``log.go`` 的渲染部分（拆分）
``log.py``                      ``log.go`` 的命令与 ``LogModule``
``usage.py``                    ``usage.go``
``request.py``                  ``request.go``
``hook.py``                     ``hook.go``
``group_knowledge.py``          ``group_knowledge.go``
``group_policy.py``             ``group_policy.go``
``group_services.py``           ``group_services.go``
``character.py``                ``character.go``
``compact.py``                  ``compact.go``
``context_policy.py``           ``context_policy.go``
``doctor.py``                   ``doctor.go``
``state.py``                    ``state.go``
``tool.py``                     ``tool.go``
``rollback.py``                 ``rollback.go``
``resources.py``                Python 侧新增（1.1.4）：超管只读资源占用（修订后含硬件/显卡，不带路径）
``member_panel.py``             ``member_panel.go``
``memory_common.py``            ``memory_common.go``
``memory_forget.py``            ``memory_forget.go``
``memory_learning.py``          ``memory_learning.go``
==============================  ====================================================

**并行期依赖**：``atri_x.agent`` 的顶层模块（core / model / tools 三组）尚未交付，
因此本包对它们**零 import**：所有服务都从 :class:`atri_x.agent.commands.deps.Deps`
以鸭子类型注入，方法调用点用 ``_maybe_await`` 兼容"协程实现"与"同步实现"两种风格。
本包对已落地的 ``atri_x.command`` / ``atri_x.security`` / ``atri_x.storage`` /
``atri_x.session`` / ``atri_x.logging`` / ``atri_x.angelmemory`` /
``atri_x.selflearning`` / ``atri_x.config`` / ``atri_x.utils.fileops`` /
``atri_x.memory.resident`` / ``atri_x.character`` / ``atri_x.contextmgr`` /
``atri_x.turn`` / ``atri_x.request`` / ``atri_x.runtime`` 是直接 import。
"""

from __future__ import annotations

from typing import Sequence

from atri_x.command import Router, new_router
from atri_x.agent.commands.approval import (
    ApprovalModule,
    new_grant,
    new_panic,
)
from atri_x.agent.commands.character import CharacterModule, new_characters
from atri_x.agent.commands.compact import CompactModule, new_compact
from atri_x.agent.commands.completion import (
    CompletionOption,
    CompletionToken,
    complete_confirm_flag,
    complete_request_ids,
    complete_session_ids,
    complete_static_options,
    complete_string_options,
    complete_tool_names,
    current_completion_token,
    first_non_empty,
    is_first_arg,
)
from atri_x.agent.commands.context_policy import (
    ContextPolicyModule,
    new_context_policy,
)
from atri_x.agent.commands.deps import (
    CommandGroup,
    CompactService,
    ContextPolicyService,
    ContextStatusService,
    Deps,
    DoctorService,
    GroupKnowledgeService,
    GroupPolicyService,
    GroupServicesService,
    HandlerFactory,
    HookService,
    LogService,
    MemberPanelService,
    Module,
    Registrar,
    RuntimeStateReloadReport,
    RuntimeStateService,
    RuntimeStateStatus,
    ToolService,
    default_modules,
    new_command_group,
    register_default_modules,
    register_factories,
    register_modules,
)
from atri_x.agent.commands.doctor import DoctorModule, new_doctor
from atri_x.agent.commands.time import TimeModule, new_time
from atri_x.agent.commands.group_knowledge import (
    KnowledgeCommand,
    KnowledgeModule,
    new_knowledge,
    split_command_args,
)
from atri_x.agent.commands.group_policy import GroupPolicyModule, new_group_policy
from atri_x.agent.commands.group_services import (
    GroupServicesModule,
    new_join,
    new_poll,
    new_remind,
    new_signup,
    new_vote,
    parse_poll_args,
    parse_signup_args,
)
from atri_x.agent.commands.help import (
    HelpModule,
    canonical_help_text,
    command_usage,
    format_command_help,
    new_help,
    rewrite_help_prefix,
    wants_command_help,
)
from atri_x.agent.commands.hook import HookModule, new_hooks
from atri_x.agent.commands.log import (
    LogModule,
    atriwisp_info,
    audit_info,
    log_info,
    new_atriwisp,
    new_audit,
    new_log,
    query_logs,
)
from atri_x.agent.commands.log_args import (
    LogArgOptions,
    parse_atriwisp_log_query,
    parse_audit_query,
    parse_log_args,
    parse_log_args_with_options,
    parse_log_time,
    parse_positive_int,
    parse_runtime_log_query,
    split_log_args,
)
from atri_x.agent.commands.log_format import (
    format_atriwisp_log_entries,
    format_audit_entries,
    format_damaged_log_notice,
    format_runtime_log_entries,
)
from atri_x.agent.commands.member_panel import MemberPanelModule, new_member_panel
from atri_x.agent.commands.memory_common import (
    memory_can_delete,
    memory_can_manage_all,
    memory_entry_detail,
    memory_entry_line,
    memory_parse_limit,
    memory_visible_filter,
    resolve_scoped_memory,
    short_memory_id,
)
from atri_x.agent.commands.memory_forget import ForgetCommand, new_forget
from atri_x.agent.commands.memory_learning import (
    LearningCommand,
    MemoryCommand,
    MemoryLearningModule,
    learning_action_permission,
    new_learning,
    new_memory,
    reviewer_name,
)
from atri_x.agent.commands.model import (
    ModelModule,
    ModelTarget,
    model_command_info,
    new_check_model,
    new_model,
    parse_model_args,
    parse_model_list_args,
)
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
from atri_x.agent.commands.request import (
    RequestModule,
    format_active_requests,
    format_duration,
    format_request_tree,
    new_requests,
    new_stop,
    new_stop_all,
    resolve_request_arg,
    stoppable_requests,
)
from atri_x.agent.commands.resources import (
    ResourcesModule,
    new_resources,
    render_resources,
)
from atri_x.agent.commands.rollback import (
    FileModule,
    format_rollback_list,
    new_rollback,
)
from atri_x.agent.commands.session_core import (
    format_empty_status,
    format_tool_usage,
    new_chat,
    new_new,
    new_status,
    new_work,
)
from atri_x.agent.commands.session_format import (
    DEFAULT_SESSION_LIST_PAGE_SIZE,
    HISTORY_PREVIEW_RUNES,
    MESSAGE_LIST_PAGE_SIZE,
    RESUME_HISTORY_TURNS,
    empty_title,
    format_time,
    list_resumable_page,
    list_session_page,
    session_page_size,
)
from atri_x.agent.commands.session_lifecycle import (
    audit_command,
    cleanup_retention_days,
    confirm_command_message,
    new_archive,
    new_clean,
    new_delete,
    new_pin,
    new_rename,
    new_unarchive,
    new_unpin,
    parse_sessions_args,
    parse_target_confirm,
    resolve_session_target,
)
from atri_x.agent.commands.session_module import (
    SessionCommandState,
    SessionModule,
    new_session_command_state,
)
from atri_x.agent.commands.session_navigation import (
    ResumeCommand,
    new_archives,
    new_fork,
    new_messages,
    new_resume,
    new_sessions,
    resume_page,
)
from atri_x.agent.commands.state import (
    StateModule,
    format_runtime_state_reload,
    format_runtime_state_status,
    new_state_command,
)
from atri_x.agent.commands.tool import ToolModule, format_tools, new_tools
from atri_x.agent.commands.usage import (
    USAGE_QUERY_LIMIT,
    UsageBucket,
    UsageGroupKey,
    UsageValue,
    format_usage_summary,
    new_usage,
    parse_usage_query,
    usage_damaged_notice,
    usage_info,
)

__all__ = [
    "ApprovalModule",
    "CharacterModule",
    "CommandGroup",
    "CompactModule",
    "CompactService",
    "CompletionOption",
    "CompletionToken",
    "ContextPolicyModule",
    "ContextPolicyService",
    "ContextStatusService",
    "DEFAULT_SESSION_LIST_PAGE_SIZE",
    "Deps",
    "DoctorModule",
    "DoctorService",
    "FileModule",
    "ForgetCommand",
    "GroupKnowledgeService",
    "GroupPolicyModule",
    "GroupPolicyService",
    "GroupServicesModule",
    "GroupServicesService",
    "HISTORY_PREVIEW_RUNES",
    "HandlerFactory",
    "HelpModule",
    "HookModule",
    "HookService",
    "KnowledgeCommand",
    "KnowledgeModule",
    "LearningCommand",
    "LogArgOptions",
    "LogModule",
    "LogService",
    "MESSAGE_LIST_PAGE_SIZE",
    "MemberPanelModule",
    "MemberPanelService",
    "MemoryCommand",
    "MemoryLearningModule",
    "ModelListOptions",
    "ModelListResult",
    "ModelModule",
    "ModelOption",
    "ModelProfile",
    "ModelProviderError",
    "ModelService",
    "ModelSnapshot",
    "ModelSnapshotSlot",
    "ModelTarget",
    "Module",
    "RESUME_HISTORY_TURNS",
    "Registrar",
    "RequestModule",
    "ResumeCommand",
    "RuntimeStateReloadReport",
    "RuntimeStateService",
    "RuntimeStateStatus",
    "SessionCommandState",
    "SessionModule",
    "StateModule",
    "TimeModule",
    "ToolModule",
    "ToolService",
    "USAGE_QUERY_LIMIT",
    "UsageBucket",
    "UsageGroupKey",
    "UsageValue",
    "atriwisp_info",
    "audit_command",
    "audit_info",
    "canonical_help_text",
    "cleanup_retention_days",
    "command_usage",
    "complete_confirm_flag",
    "complete_request_ids",
    "complete_session_ids",
    "complete_static_options",
    "complete_string_options",
    "complete_tool_names",
    "confirm_command_message",
    "current_completion_token",
    "default_modules",
    "empty_title",
    "first_non_empty",
    "format_active_requests",
    "format_atriwisp_log_entries",
    "format_audit_entries",
    "format_command_help",
    "format_damaged_log_notice",
    "format_duration",
    "format_empty_status",
    "format_request_tree",
    "format_rollback_list",
    "ResourcesModule",
    "format_runtime_log_entries",
    "format_runtime_state_reload",
    "format_runtime_state_status",
    "format_time",
    "format_tool_usage",
    "format_tools",
    "format_usage_summary",
    "is_first_arg",
    "learning_action_permission",
    "list_resumable_page",
    "list_session_page",
    "log_info",
    "memory_can_delete",
    "memory_can_manage_all",
    "memory_entry_detail",
    "memory_entry_line",
    "memory_parse_limit",
    "memory_visible_filter",
    "model_command_info",
    "new_archive",
    "new_archives",
    "new_atriwisp",
    "new_audit",
    "new_characters",
    "new_chat",
    "new_check_model",
    "new_clean",
    "new_command_group",
    "new_compact",
    "new_context_policy",
    "new_default_router",
    "new_delete",
    "new_doctor",
    "new_fork",
    "new_forget",
    "new_group_policy",
    "new_help",
    "new_hooks",
    "new_join",
    "new_knowledge",
    "new_learning",
    "new_log",
    "new_member_panel",
    "new_memory",
    "new_messages",
    "new_model",
    "new_new",
    "new_pin",
    "new_poll",
    "new_remind",
    "new_rename",
    "new_requests",
    "new_resume",
    "new_resources",
    "new_rollback",
    "render_resources",
    "new_session_command_state",
    "new_sessions",
    "new_signup",
    "new_state_command",
    "new_time",
    "new_status",
    "new_stop",
    "new_stop_all",
    "new_tools",
    "new_unarchive",
    "new_unpin",
    "new_usage",
    "new_vote",
    "new_work",
    "parse_atriwisp_log_query",
    "parse_audit_query",
    "parse_log_args",
    "parse_log_args_with_options",
    "parse_log_time",
    "parse_model_args",
    "parse_model_list_args",
    "parse_poll_args",
    "parse_positive_int",
    "parse_runtime_log_query",
    "parse_sessions_args",
    "parse_signup_args",
    "parse_target_confirm",
    "parse_usage_query",
    "query_logs",
    "register_default_modules",
    "register_factories",
    "register_modules",
    "resolve_request_arg",
    "resolve_scoped_memory",
    "resolve_session_target",
    "resume_page",
    "reviewer_name",
    "rewrite_help_prefix",
    "session_page_size",
    "short_memory_id",
    "split_command_args",
    "split_log_args",
    "stoppable_requests",
    "usage_damaged_notice",
    "usage_info",
    "wants_command_help",
]


def new_default_router(deps: Deps, prefixes: Sequence[str] | None = None) -> Router:
    """建一个注册了全部默认命令的 :class:`atri_x.command.Router`。

    这是本包的"分发入口"：Go 侧 ``agent`` 装配时做的是
    ``commands.RegisterDefaultModules(router, deps)``，这里把"建 Router + 注册"
    收成一个调用，等价于 ``new_router(prefixes)`` 后跑 ``register_default_modules``。
    ``/help``、补全这些命令要回头查 Router（Go 的 ``Deps.Router``），所以这里先
    把它写回 ``deps.router``（Go 侧由调用方 ``core.go`` 显式填写同一个字段）。
    调用方拿到 Router 后即可用 ``await router.dispatch(ctx, text)`` 派发。
    """
    router = new_router(prefixes)
    deps.router = router
    register_default_modules(router, deps)
    return router


# ---------------------------------------------------------------------------
# 模型视图类型：**唯一出处**的再导出
# ---------------------------------------------------------------------------
#: 模型视图类型（``ModelOption`` / ``ModelProfile`` / ``ModelSnapshot`` / …）的定义
#: **只有一份**，在 :mod:`atri_x.agent.commands.model_types`；:mod:`atri_x.agent.model`
#: 按 PORTING.md §3.1 从本包取用。因此本包与 ``atri_x.agent.model`` 之间的名字必须
#: **是同一个类对象** —— 否则命令层与模型层各持一份 ``ModelOption``，``==`` /
#: ``isinstance`` 会静默失效（写起来像能跑，比较起来永远不等）。
#:
#: 为彻底堵死导入环，这里不改上面的写法，而是加一个模块级 ``__getattr__`` 兜底：
#: 若某个名字上面的显式 import 没绑上（例如 ``model_types`` 完全落地之前的历史状态），
#: 就延迟到首次访问时去 ``model_types`` 取一次。
_TYPE_ALIASES: dict[str, str] = {
    "ModelListOptions": "ModelListOptions",
    "ModelListResult": "ModelListResult",
    "ModelOption": "ModelOption",
    "ModelProfile": "ModelProfile",
    "ModelProviderError": "ModelProviderError",
    "ModelService": "ModelService",
    "ModelSnapshot": "ModelSnapshot",
    "ModelSnapshotSlot": "ModelSnapshotSlot",
}


def __getattr__(name: str) -> Any:
    """按需从 :mod:`atri_x.agent.commands.model_types` 取回模型视图类型。"""
    attribute = _TYPE_ALIASES.get(name)
    if attribute is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from atri_x.agent.commands import model_types

    value = getattr(model_types, attribute)
    globals()[name] = value
    return value
