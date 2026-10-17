"""Tool Runtime：工具契约、注册表、发现、风险准入与执行适配。

对应 Go 包：``internal/tool``（本包覆盖 ``tool.go`` / ``builder.go`` /
``capability.go`` / ``detail.go`` / ``availability.go`` / ``sandbox.go`` /
``workspace.go`` / ``executor.go`` / ``provider.go`` / ``media_runtime.go`` /
``discover.go``；``builtin/``、``skill/``、``runtimeinfo.py`` 在各自的
Python 模块里）。

风险等级与访问策略的原定义在 ``internal/security/security.go``；``RiskLevel``
按 PORTING.md §3 归到 :mod:`atri_x.tool.tool`，本包一并再导出。
"""

from __future__ import annotations

from atri_x.tool.availability import (  # noqa: F401
    TOOL_REASON_CONTEXT_UNAVAILABLE,
    TOOL_REASON_HIDDEN,
    TOOL_REASON_NOT_FOUND,
    TOOL_REASON_NO_SCHEMA,
    TOOL_REASON_RISK_DENIED,
    TOOL_REASON_SUPERADMIN_ONLY,
    ToolReasonContextUnavailable,
    ToolReasonHidden,
    ToolReasonNoSchema,
    ToolReasonNotFound,
    ToolReasonRiskDenied,
    ToolReasonSuperadminOnly,
    registry_tool_availability_reason,
    tool_access_reason,
    tool_availability_reason,
)
from atri_x.tool.builder import (  # noqa: F401
    Builder,
    ParamConfig,
    ParamOption,
    enum,
    items,
    new_builder,
    required,
)
from atri_x.tool.capability import (  # noqa: F401
    Capabilities,
    capabilities_from_context,
    ctx_error,
    ctx_raise_if_cancelled,
    ctx_value,
    ctx_with_value,
    vision_available,
    with_capabilities,
)
from atri_x.tool.detail import (  # noqa: F401
    ContextDiscoveryContentProvider,
    DetailBlock,
    DetailProvider,
    DiscoveredTool,
    DiscoveryContentProvider,
    DiscoveryError,
    DiscoveryResult,
    LazyDetailProvider,
    PublicInfo,
    RuleCardState,
    StructuredDetailProvider,
    load_discovery_content,
    new_rule_card_formats_from_context,
    public_info,
    render_detail_blocks,
    render_detail_blocks_with_context,
    rule_card_key,
    with_shown_rule_card_formats,
)
from atri_x.tool.discover import (  # noqa: F401
    DISCOVER_TOOL_NAME,
    DiscoverArgs,
    DiscoverFilter,
    DiscoverTool,
    discover_filter_allowed,
    discovery_content,
    discovery_security,
    expand_discovery_tag_names,
    marshal_json_no_escape,
    new_discover_tool,
    with_discover_filter,
)
from atri_x.tool.executor import ExecutionResult, Executor  # noqa: F401
from atri_x.tool.media_runtime import (  # noqa: F401
    CLOSE_TIMEOUT_SECONDS,
    MEDIA_CALL_DIR_PREFIX,
    MEDIA_INPUTS_DIR,
    MediaCall,
    MediaInput,
    MediaRuntime,
    media_filename,
)
from atri_x.tool.provider import (  # noqa: F401
    SESSION_MODE_CHAT,
    SESSION_MODE_WORK,
    PromptNames,
    SchemaProvider,
    actor_from_scope,
    prompt_names_from_infos,
)
from atri_x.tool.runtimeinfo import (  # noqa: F401
    Info as RuntimeInfo,
    atriyph_rule_card,
    first as first_runtime_info,
)
from atri_x.tool.sandbox import (  # noqa: F401
    BackgroundKind,
    BackgroundKindAtrinis,
    BackgroundKindCron,
    SandboxContext,
    background_context,
    background_path_instruction,
    info_available_in_context,
    resolve_sandbox_relative_path,
    sandbox_context_from_context,
    session_id_from_context,
    with_sandbox_context,
    with_session_id,
)
from atri_x.tool.tool import (  # noqa: F401
    METADATA_ACTIVATE_TOOLS,
    METADATA_SHOWN_RULE_CARD_FORMATS,
    Actor,
    ActorFromContext,
    ActorID,
    CallRequest,
    CompareRisk,
    ConfirmationPreflightProvider,
    DefaultPolicy,
    GroupRole,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
    Info,
    ParseGroupRole,
    ParseRisk,
    Policy,
    PolicyFromContext,
    Registry,
    Result,
    RiskAssessment,
    RiskAssessor,
    RiskCritical,
    RiskDetailProvider,
    RiskHigh,
    RiskLevel,
    RiskLow,
    RiskMedium,
    RiskSafe,
    Role,
    RoleSuperadmin,
    RoleUser,
    Source,
    SourceBuiltin,
    SourceSkillAgent,
    SourceSkillGo,
    Tool,
    WithActor,
    WithPolicy,
    append_warnings,
    assess_risk,
    can_access_tool,
    compare_risk,
    default_policy,
    format_warnings,
    has_tag,
    new_registry,
    normalize_names,
    normalize_risk,
    normalize_tag,
    normalize_tags,
    normalize_warnings,
    parse_risk,
    warnings_suffix,
)
from atri_x.tool.workspace import (  # noqa: F401
    ABSOLUTE_PATH_WORKSPACE_WARNING,
    PathResolveOptions,
    ResolvedPath,
    WorkspaceAgentNoticeStore,
    WorkspaceStore,
    current_workspace_dir,
    resolve_workspace_path,
    validate_workspace_dir,
    with_workspace_store,
    workspace_store_from_context,
)

__all__ = [name for name in dir() if not name.startswith("_")]
