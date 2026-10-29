<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 改动请落在 `atri_x/devtools/` 的渲染实现或代码本身，然后重新生成；手改会被 `--check` 判为漂移。 -->

# 模块索引（自动生成）

共 **787** 个模块 / **231563** 物理行 / **64** 个一级域。被测试直接覆盖 **477** 个（60.6%）。

查模块：`rg -n "atri_x\.tool\.builtin" devdocs/generated/module-map.md`。

| 模块 | 一句话职责 | 公共符号 | 行数 | 测试文件 |
|---|---|---|---:|---|
| `atri_x.__main__` | ``cmd/atri-x`` 的 Python 移植：进程入口 ``python -m atri_x``。 | DEFAULT_HEALTH_PORT, HEALTH_PROBE_TIMEOUT, ZERO_TIME_TEXT, … | 535 | `test_main.py` |
| `atri_x.agent` | ``atri_x.agent``：Agent 核心、回合流程、事件、收件箱与外围能力。 | agent_package_info | 389 | `agent_commands_fakes.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_chat_anomaly.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_group_policy.py`、`test_agent_inbound_media.py`、`test_agent_model.py`、`test_agent_participation.py`、`test_agent_prompt.py`、`test_agent_reactions.py`、`test_agent_risk.py`、`test_agent_segments.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_agent_wakeup.py`、`test_app_runtime_agent.py`、`test_builtin_runtime_resources.py`、`test_dsh_acp_client.py`、`test_dsh_route.py`、`test_hook_runtime.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_request_manager.py`、`test_safety_integration.py`、`test_security_grants.py`、`test_session_naming_contract.py`、`test_time_context.py`、`test_toolrun.py`、`test_usage_attribution.py`、`test_vision_prechat.py`、`test_vision_privacy.py` |
| `atri_x.agent.admission` | 计算静默：静默时段里"哪些模型调用允许发生"的唯一判定入口。 | Admission, admit, background_reason, global_quiet_active, q… | 224 | `test_agent_admission.py` |
| `atri_x.agent.asr` | 入站语音转写（ASR）。 | AsrMixin, AudioTranscriber, AudioTranscriptionRequest, Audi… | 355 | — |
| `atri_x.agent.bootstrap` | Agent 的可选服务装配入口（幂等）。 | AgentServicesReport, install_agent_services, is_bootstrappe… | 576 | `test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_commands_integration.py`、`test_app_runtime_agent.py` |
| `atri_x.agent.budget` | 每日计量账本（对外统一入口）。 | BUDGET_ID_REUSED, BUDGET_METHODS, BUDGET_WRITE_FAILED, Budg… | 79 | `test_agent_budget.py` |
| `atri_x.agent.budget_chat` | 聊天预算的 Agent 方法 + 次数限额规格 + 预算服务装配。 | BUDGET_METHODS, BudgetMixin, BudgetService, install_budget_… | 459 | — |
| `atri_x.agent.budget_keys` | 每日计量账本的键、裁剪、计数与请求结构。 | BUDGET_ID_REUSED, BUDGET_WRITE_FAILED, BudgetLimitSpec, Bud… | 354 | — |
| `atri_x.agent.budget_ledger` | 每日计量账本本体：原子预留、执行幂等、用量与状态桥。 | BudgetLedger, ensure_budget | 701 | — |
| `atri_x.agent.call_reason` | 一次 LLM 调用的**原因**标签（Python 版新增）。 | AMBIENT_PARTICIPATION, CRON, DIRECT_MESSAGE, EXTERNAL_EVENT… | 89 | `test_agent_admission.py`、`test_usage_attribution.py` |
| `atri_x.agent.character_directive` | ``@char:`` / ``@c:`` 单回合角色声明的解析与应用。 | CHARACTER_DIRECTIVE_METHODS, CHARACTER_DIRECTIVE_PROMPT_RUN… | 145 | `test_agent_assembly.py` |
| `atri_x.agent.character_guide_prompt` | 人设卡操作指引（系统提示词来源）。 | CHARACTER_GUIDE_TEXT, CharacterGuideSystemPromptSource | 55 | — |
| `atri_x.agent.chat` | 回合主流程：从入站文本到助手消息落库。 | audit_partial_platform_send, handle_chat, handle_turn_conte… | 412 | `test_agent_chat.py`、`test_agent_core.py` |
| `atri_x.agent.chat_anomaly` | 上游把工具调用写成正文时的补救与清洗。 | apply_text_tool_recovery | 157 | `test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_llm` | 单次 LLM 调用：请求构造、流式读取、用量与错误审计。 | LEVEL_ERROR, LEVEL_WARN, LLMCallResult, LLMRequestOptions, … | 634 | `test_agent_chat.py`、`test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_observe` | 模型调用的日志、审计与上下文助手。 | audit_side_usage, audit_usage, elapsed_millis, is_cli_conte… | 232 | `test_usage_attribution.py` |
| `atri_x.agent.chat_request_options` | LLM 请求的临时参数（``LLMRequestOptions``）与其合并/解析。 | LLMRequestOptions, llm_request_options_from_payload, merge_… | 79 | — |
| `atri_x.agent.chat_stream` | 模型流式调用的叶子助手（打开流、并发槽、记账结算、媒体补全）。 | — | 154 | — |
| `atri_x.agent.chat_tools` | 回合里的工具批次：待发用户消息、工具执行、自动确认与 schema 过滤。 | PREVIEW_TEXT_RUNES, PendingUserMessage, TOOL_LOG_ACTION_LIM… | 747 | `test_agent_chat.py`、`test_agent_core.py`、`test_security_grants.py` |
| `atri_x.agent.chat_turn` | 一轮对话的主体循环。 | — | 625 | `test_agent_chat.py` |
| `atri_x.agent.chat_vision` | 图片不受支持时的透明兜底。 | fallback_vision_messages, should_fallback_vision, vision_fa… | 86 | — |
| `atri_x.agent.command_runtime` | Slash 命令的执行与权限裁决。 | CommandExecutor, blocks_during_active_turn, blocks_during_c… | 243 | — |
| `atri_x.agent.commands` | ``internal/agent/commands`` 的 Python 移植：slash 命令族与注册入口。 | ApprovalModule, CharacterModule, CommandGroup, CompactModul… | 541 | `agent_commands_fakes.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_builtin_runtime_resources.py`、`test_security_grants.py`、`test_usage_attribution.py` |
| `atri_x.agent.commands.approval` | ``/grant`` 与 ``/panic``：授权生命周期的手动入口。 | ApprovalModule, new_grant, new_panic | 186 | `test_security_grants.py` |
| `atri_x.agent.commands.character` | ``/chars``：列出角色素材库，或重建角色索引。 | CharacterModule, character_matches_query, character_viewer_… | 118 | — |
| `atri_x.agent.commands.compact` | ``/compact``：手动压缩当前会话上下文。 | CompactModule, new_compact | 50 | — |
| `atri_x.agent.commands.completion` | 命令参数补全的公共助手。 | CompletionOption, CompletionToken, complete_confirm_flag, c… | 212 | — |
| `atri_x.agent.commands.context_policy` | ``/overflow``（别名 ``/ctxpolicy``、``/longmsg``）：长消息溢出策略。 | ContextPolicyModule, new_context_policy | 103 | — |
| `atri_x.agent.commands.deps` | 命令注册表、依赖容器与服务协议。 | CommandGroup, CompactService, ContextPolicyService, Context… | 442 | `test_agent_assembly.py`、`test_agent_commands_integration.py`、`test_builtin_runtime_resources.py`、`test_security_grants.py` |
| `atri_x.agent.commands.doctor` | ``/doctor``：只读配置检查。 | DoctorModule, new_doctor | 55 | — |
| `atri_x.agent.commands.group_knowledge` | ``/faq``（别名 ``/knowledge``、``/kb``、``/groupkb``）：群确定性知识库。 | KnowledgeCommand, KnowledgeModule, knowledge_entry_contains… | 293 | — |
| `atri_x.agent.commands.group_policy` | ``/grouppolicy``（别名 ``/gpolicy``、``/grouppolicies``）：当前群的策略配置。 | GroupPolicyModule, new_group_policy | 109 | — |
| `atri_x.agent.commands.group_services` | ``/remind``、``/poll``、``/vote``、``/signup``、``/join``：群确定性服务。 | GroupServicesModule, new_join, new_poll, new_remind, new_si… | 323 | — |
| `atri_x.agent.commands.help` | ``/help`` 命令与帮助文本渲染。 | HelpModule, canonical_help_text, command_usage, format_comm… | 175 | — |
| `atri_x.agent.commands.hook` | ``/hooks``：列出/查看 hook、重载配置、控制有状态 hook。 | HOOK_LIST_DESCRIPTION_LIMIT, HookGroup, HookModule, collect… | 355 | — |
| `atri_x.agent.commands.log` | ``/log``、``/audit``、``/atriwisp``、``/usage`` 的注册与派发。 | LogModule, atriwisp_info, audit_info, log_completion_option… | 374 | — |
| `atri_x.agent.commands.log_args` | 日志类命令的参数解析（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 | DEFAULT_LOG_LIST_LIMIT, LogArgOptions, base_log_query, json… | 422 | — |
| `atri_x.agent.commands.log_format` | 日志类命令的输出渲染（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 | append_field, append_llm_usage_fields, append_runtime_log_f… | 248 | — |
| `atri_x.agent.commands.member_panel` | ``/me``（别名 ``/mytasks``、``/my``、``/mypanel``）：普通成员自助面板。 | MemberPanelModule, new_member_panel | 63 | — |
| `atri_x.agent.commands.memory_common` | 长期记忆命令的共享助手。 | audit_command, is_group_scope, memory_can_delete, memory_ca… | 195 | — |
| `atri_x.agent.commands.memory_forget` | ``/forget``（别名 ``/forget_memory``、``/delete_memory``）：删除长期/常驻记忆。 | ForgetCommand, new_forget | 220 | — |
| `atri_x.agent.commands.memory_learning` | ``/memory``（别名 ``/angel_memory``）与 ``/learning``（别名 ``/selflearning``）。 | LearningCommand, MemoryCommand, MemoryLearningModule, forma… | 443 | — |
| `atri_x.agent.commands.model` | ``/model`` 与 ``/checkmodel``（别名 ``/models``）。 | ModelModule, ModelTarget, append_model_provider_errors, com… | 560 | — |
| `atri_x.agent.commands.model_types` | 模型服务的共享数据类型与形状。 | ModelListOptions, ModelListResult, ModelOption, ModelProfil… | 147 | `test_agent_bootstrap.py`、`test_agent_commands_integration.py` |
| `atri_x.agent.commands.request` | ``/requests``、``/stop``、``/stopall`` 与请求树渲染。 | RequestInfo, RequestModule, RequestNode, RequestTree, build… | 375 | — |
| `atri_x.agent.commands.research` | ``/research``：科研研究助理的只读操作面板命令。 | ResearchModule, new_research, research_home | 308 | — |
| `atri_x.agent.commands.resources` | ``/resources``：超管直接查看本机与 ATRI-X 进程的硬件资源占用。 | ResourcesModule, default_resource_paths, new_resources, ren… | 76 | `test_builtin_runtime_resources.py` |
| `atri_x.agent.commands.rollback` | ``/rollback``：列出并撤销 ATRI-X 自己对文件的最新一次编辑。 | FileModule, format_rollback_list, new_rollback | 109 | — |
| `atri_x.agent.commands.session_core` | ``/new``、``/status``、``/work``、``/chat``。 | format_empty_status, format_tool_usage, new_chat, new_new, … | 266 | — |
| `atri_x.agent.commands.session_format` | Session 列表/消息的查询与文本渲染。 | DEFAULT_SESSION_LIST_PAGE_SIZE, HISTORY_PREVIEW_RUNES, MESS… | 366 | — |
| `atri_x.agent.commands.session_lifecycle` | Session 生命周期命令：``/archive`` 系列、``/rename``、``/delete``、``/clean``。 | DEFAULT_SESSION_LIST_PAGE_SIZE, HISTORY_PREVIEW_RUNES, MESS… | 523 | — |
| `atri_x.agent.commands.session_module` | ``SessionCommandState`` 与 ``SessionModule``。 | SessionCommandState, SessionModule, new_session_command_sta… | 138 | — |
| `atri_x.agent.commands.session_navigation` | ``/sessions``、``/archives``、``/resume``、``/messages``、``/fork``。 | ResumeCommand, new_archives, new_fork, new_messages, new_re… | 270 | — |
| `atri_x.agent.commands.state` | ``/state``：``state.toml`` 热加载状态与手动重载。 | StateModule, format_runtime_state_reload, format_runtime_st… | 164 | — |
| `atri_x.agent.commands.time` | ``/time``：查看统一时间服务状态，手动触发一次网络校验，列出可用时区。 | TimeModule, new_time | 221 | — |
| `atri_x.agent.commands.tool` | ``/tools``：查看工具与当前群的工具授权，群主/管理员可在群里直接授权。 | ToolModule, format_tool_allowlist_status, format_tools, has… | 342 | — |
| `atri_x.agent.commands.usage` | ``/usage``：从审计日志聚合 token 用量。 | USAGE_QUERY_LIMIT, UsageBucket, UsageGroupKey, UsageValue, … | 451 | `test_agent_commands_services.py`、`test_usage_attribution.py` |
| `atri_x.agent.completion` | 结构化补全服务对外的两个门面。 | complete, completion_service | 28 | — |
| `atri_x.agent.context_compact` | 上下文压缩：``/compact`` 的会话切换、摘要 metadata 与状态。 | ContextCompactMixin, ContextCompactState, build_compact_mes… | 524 | `test_agent_context_runtime.py` |
| `atri_x.agent.context_policy` | 长消息保护（上下文溢出策略）：判定、截断/摘要/拒绝与群级覆盖。 | ContextOverflowState, ContextPolicyMixin, ErrPromptOverflow… | 532 | `test_agent_context_policy.py`、`test_agent_group_policy.py` |
| `atri_x.agent.context_runtime` | 上下文运行态：窗口/预算解析、压缩后端选择、加载器与最近用量。 | CONTEXT_METHODS, ContextRuntimeMixin, ContextRuntimeService… | 557 | `test_agent_context_policy.py`、`test_agent_context_runtime.py` |
| `atri_x.agent.context_seed` | 压缩种子（``context_compact``）的消费。 | ContextSeedMixin, consume_context_compact_seed, pending_con… | 113 | `test_agent_context_runtime.py` |
| `atri_x.agent.context_usage` | 上下文用量：``/context`` 状态文本、最近用量持久化、协议归属记录、压缩触发判定。 | ContextUsageMixin, LLMOriginState, render_context_status | 315 | `test_agent_context_policy.py` |
| `atri_x.agent.core` | Agent 核心：状态、构造与跨模块方法的薄转发。 | Agent, FileSoulProvider, SoulProvider, StaticSoulProvider, … | 995 | `test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_core.py`、`test_request_manager.py` |
| `atri_x.agent.core_build` | Agent 构造期的支撑件：默认配置、模型运行态、构造入口。 | FileSoulProvider, new, new_options, new_with_prefixes | 225 | — |
| `atri_x.agent.core_forward` | ``Agent`` 的薄转发方法（唤醒 / 消息 / 聊天 / 模型 / 工具 / 完成）。 | CoreForwardMixin | 283 | — |
| `atri_x.agent.cron` | 后台运行（cron / Atrinis 共用的 ``RunBackground``）与会话预处理。 | BackgroundPreloadResult, BackgroundTakenOverError, CronMixi… | 605 | `test_agent_admission.py`、`test_agent_cron.py`、`test_agent_cron_background.py` |
| `atri_x.agent.cron_background` | 后台任务的工具 / Skill 预载与 Session 元数据（Agent 侧）。 | BackgroundPreloadResult, audit_event, background_cached_too… | 563 | — |
| `atri_x.agent.cron_tools` | 后台任务里 shell 工具的风险裁决（Agent 侧）。对应 Go：``internal/agent/cron_tools.go``。 | background_shell_decision, confirm_background_sandbox_shell | 163 | `test_agent_cron.py` |
| `atri_x.agent.dsh` | ``atri_x.agent.dsh``：ATRI-X 编排 DSH 的纯标准库 ACP 客户端。 | ACP_PROTOCOL_VERSION, AcpClient, AcpError, AcpEvent, AcpPro… | 51 | `test_dsh_acp_client.py`、`test_dsh_route.py` |
| `atri_x.agent.dsh.client` | ``atri_x.agent.dsh.client``：DSH ACP 客户端（会话语义与 prompt 生命周期）。 | AcpClient | 423 | — |
| `atri_x.agent.dsh.command` | DSH ACP 启动命令的解析（冒烟与路由共用，**唯一出处**）。 | DEFAULT_COMMAND_FALLBACK, format_command, resolve_command | 61 | — |
| `atri_x.agent.dsh.handoff` | 上下文交接：把 ATRI-X 的对话拼成 DSH 的一次性 prompt，并把 ACP 用量折算成账本口径。 | HANDOFF_HEADER, HANDOFF_INSTRUCTION, build_handoff_prompt, … | 163 | `test_dsh_route.py` |
| `atri_x.agent.dsh.route_call` | 把「路由判定」接到对话链路上：预算、执行、流式回传、熔断与失败回退。 | DshRouteService, close_route_service, describe_route, ensur… | 596 | `test_dsh_route.py` |
| `atri_x.agent.dsh.router` | DSH 深度路由：任务特征打分 + 成本闸门 + 熔断。 | DshRouteConfig, DshRouter, RouteDecision, TaskFeatures, cla… | 483 | `test_dsh_route.py` |
| `atri_x.agent.dsh.runtime` | DSH 运行时的进程与并发管理：长驻 AcpClient + 每次路由一个一次性会话。 | DshRunResult, DshRuntime, WorkspacePaths, allow_permission,… | 425 | `test_dsh_route.py` |
| `atri_x.agent.dsh.smoke` | ``python -m atri_x.agent.dsh.smoke``：DSH ACP 最小闭环冒烟。 | main | 196 | `test_dsh_acp_client.py` |
| `atri_x.agent.dsh.status` | ``python -m atri_x.agent.dsh.status``：DSH 内核与深度路由的状态自检。 | collect_status, main | 172 | `test_dsh_route.py` |
| `atri_x.agent.dsh.transport` | ``atri_x.agent.dsh.transport``：ACP 子进程的 NDJSON stdio JSON-RPC 传输。 | JsonRpcStdio | 376 | — |
| `atri_x.agent.dsh.types` | ``atri_x.agent.dsh`` 的协议类型、常量与解析函数。 | ACP_PROTOCOL_VERSION, AcpError, AcpEvent, AcpProcessError, … | 295 | `test_dsh_acp_client.py`、`test_dsh_route.py` |
| `atri_x.agent.errors` | 面向用户的错误呈现与"已通知用户"标记。 | MAX_USER_ERROR_RUNES, UserErrorDetails, UserNotifiedError, … | 145 | `test_agent_chat.py` |
| `atri_x.agent.events` | 平台生命周期事件的确定性处理（不调用模型）。 | MessageWorkKeys, MessageWorkRef, cancel_message_work, cance… | 466 | — |
| `atri_x.agent.group_knowledge` | 群知识库（确定性 FAQ）：命中即本地回答，不进模型。 | GroupKnowledgeMixin, GroupKnowledgeState, clone_group_knowl… | 563 | `test_agent_group_policy.py` |
| `atri_x.agent.group_policy` | 群级策略：状态快照、视图、模型目录与授权闸门。 | GroupPolicyMixin, GroupPolicyState, authorize_learning_acti… | 834 | `agent_fakes.py`、`test_agent_group_policy.py` |
| `atri_x.agent.group_policy_rules` | 群策略字段的规范化与校验规则。 | FIELD_ALIASES, FIELD_LABELS, SUPERADMIN_ONLY_FIELDS, apply_… | 536 | `test_agent_group_policy.py` |
| `atri_x.agent.group_runtime` | 群运行态状态机：禁言 / 退群 / 不可用。 | GROUP_RUNTIME_ACTIVE, GROUP_RUNTIME_MUTED, GROUP_RUNTIME_RE… | 533 | `test_agent_bootstrap.py`、`test_agent_group_policy.py` |
| `atri_x.agent.group_services_polls` | 群投票：创建 / 列表 / 查看 / 投票 / 关闭。 | GROUP_POLL_PREFIX, POLL_STATUS_CLOSED, POLL_STATUS_OPEN, Po… | 316 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_reminders` | 群提醒：创建 / 列表 / 删除，以及到点派发。 | GROUP_REMINDER_PREFIX, GroupServicesMixin, REMINDER_STATUS_… | 479 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_signups` | 群报名：创建 / 列表 / 查看 / 加入 / 退出 / 关闭。 | GROUP_SIGNUP_PREFIX, SIGNUP_STATUS_CLOSED, SIGNUP_STATUS_OP… | 345 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_state` | 群服务（提醒 / 投票 / 报名）的快照、开关与落库。 | GroupServicesState, check_reminder_when, clone_group_polls,… | 661 | `test_agent_core.py`、`test_agent_group_policy.py` |
| `atri_x.agent.hooks` | Hook 运行、路由与失败通知。 | HookRouter, HookRunner, actor_context, cancel_hook_route, f… | 389 | `test_hook_runtime.py` |
| `atri_x.agent.identity` | 身份、作用域与对话元数据推导。 | ConversationMeta, actor, base_scope, conversation_id, conve… | 191 | — |
| `atri_x.agent.inbound_media` | 入站平台媒体的本地化。 | InboundMediaMixin, associate_inbound_history, materialize_p… | 214 | `test_agent_inbound_media.py` |
| `atri_x.agent.inbound_speaker` | 发言人身份：回合输入的快照、显示名与群共享线程标记。 | InboundSpeakerMixin, attach_speaker_marker, input_speaker_m… | 179 | — |
| `atri_x.agent.inbox` | 会话收件箱：串行化一个 Session 的普通回合，并合并静默窗口内的连续消息。 | InboxBatch, InboxItem, InboxQueue, MAX_INBOX_ITEMS_PER_SESS… | 507 | — |
| `atri_x.agent.input` | 入站输入的准入、确认与回合切换。 | DEFAULT_USER_CONFIRMATION_TIMEOUT, active_turn_command_bloc… | 587 | `test_agent_risk.py` |
| `atri_x.agent.logging` | Agent 的日志与审计通道。 | LogManager, audit_attrs_have_key, normalize_audit_attrs, se… | 141 | — |
| `atri_x.agent.media_output` | 媒体输出的准备、解析与回执缓存。 | MediaOutputMixin, cache_media_receipt, is_media_output_kind… | 218 | — |
| `atri_x.agent.member_panel` | 成员自助面板：任务与额度视图。 | MemberPanelMixin, format_member_cost_micros, format_member_… | 494 | — |
| `atri_x.agent.member_persona_prompt` | 成员专属人设的系统提示词来源。 | MemberPersonaSystemPromptSource | 73 | `test_member_persona.py` |
| `atri_x.agent.member_profile_prompt` | 群友档案提示词来源：把**当前发言人**的档案注入系统提示词。 | MEMBER_PROFILE_PROMPT_HEADER, MEMBER_PROFILE_TRUST_NOTE, Me… | 72 | `test_member_profile.py` |
| `atri_x.agent.memory_learning_admin` | 后台记忆/学习的管理查询门面。 | angel_memory_count, self_learning_stats | 47 | — |
| `atri_x.agent.message` | 入站消息总入口：命令派发与聊天。 | CommandInfos, command_infos, handle_message, is_cancellatio… | 257 | `test_safety_integration.py` |
| `atri_x.agent.model` | 模型运行时状态、模式模型选择、客户端查找与重试通知。 | AgentService, INSTALLABLE_MODEL_METHODS, MAX_USER_ERROR_RUN… | 484 | `test_agent_bootstrap.py`、`test_agent_context_runtime.py`、`test_agent_model.py` |
| `atri_x.agent.model_agent` | ``ModelMixin``：``internal/agent/model.go`` 里挂在 ``*Agent`` 上的模型方法。 | ModelMixin, ensure_context_runtime | 428 | `test_agent_context_runtime.py`、`test_agent_model.py` |
| `atri_x.agent.model_list` | 模型列表：缓存、并发合并、按 provider 聚合与命名模型档。 | FetchModels, MODEL_LIST_ERROR_TTL, MODEL_LIST_FETCH_TIMEOUT… | 448 | `test_agent_model.py` |
| `atri_x.agent.model_policy` | 执行前的模型授权（群模型目录）。 | ModelPolicyMixin, authorize_execution_model_selection | 102 | `test_agent_model.py` |
| `atri_x.agent.model_service` | 模型服务的门面与装配（从 ``model.py`` 尾部拆出）。 | INSTALLABLE_MODEL_METHODS, ModelService, install_model_serv… | 189 | `test_agent_bootstrap.py`、`test_agent_model.py` |
| `atri_x.agent.model_snapshot` | 命名模型快照（``/model --save`` / ``--apply`` / ``--delete``）。 | MODEL_SNAPSHOT_MODE_ORDER, MODEL_SNAPSHOT_NAME_LIMIT, Model… | 337 | `test_agent_model.py` |
| `atri_x.agent.model_state` | 运行态（``state.toml``）的启动恢复、热加载与回写。 | RUNTIME_STATE_METHODS, RuntimeStateMixin, ensure_state_stor… | 323 | `test_agent_bootstrap.py`、`test_agent_model.py` |
| `atri_x.agent.notice_queue` | 按平台的通知发送队列与背压。 | NOTICE_QUEUE_CAPACITY, NOTICE_QUEUE_CONCURRENCY, NoticeQueu… | 232 | `test_agent_core.py` |
| `atri_x.agent.ops` | 运维可见的请求快照。 | active_requests | 22 | — |
| `atri_x.agent.options` | 构造期依赖与配置容器，以及若干 setter。 | Options, session_idle_expiration_config, validate_model_sel… | 239 | `test_agent_core.py` |
| `atri_x.agent.output` | 出站：发送管理器的装配、部分失败记账与平台消息映射。 | AgentOutputSender, ContextTextSender, NoticeDispatch, map_s… | 408 | `test_safety_integration.py` |
| `atri_x.agent.participation` | 群聊参与状态机 MVP（评估 §D）：只做**抑制**，不主动找话题。 | EchoState, ParticipationState, cooldown_active, cooldown_se… | 345 | `test_agent_participation.py` |
| `atri_x.agent.platform_signals` | 平台连接信号的订阅与按平台执行队列。 | PlatformSubscriptions, begin_close_platform_events, close_p… | 249 | — |
| `atri_x.agent.prefix` | 命令前缀的读取与取消命令识别。 | command_prefix, command_prefixes, is_cancel_command | 47 | — |
| `atri_x.agent.prompt` | 提示词构造：SOUL 提供者、工具名/schema 提供者、``PromptBuilder``。 | AssistantMetadata, FileSoulProvider, NoopToolSchemaProvider… | 421 | `test_agent_prompt.py`、`test_member_profile.py` |
| `atri_x.agent.ratelimit` | 入站限流：用户桶 + 群桶、计数与可观测状态。 | RATE_LIMIT_METHODS, RateLimitMixin, RateLimitService, RateL… | 275 | `test_agent_bootstrap.py`、`test_agent_directives.py` |
| `atri_x.agent.reactions` | 表情反应层与 Agent 回合输出的接线（Python 侧新增，无 Go 对应）。 | build_reaction_service, conversation_kind, finalize_turn_re… | 164 | `test_agent_chat.py`、`test_agent_reactions.py` |
| `atri_x.agent.request_context` | 回合请求 ID 在 ctx 上的读写。 | TurnRequestID, turn_request_id_from_context, with_turn_requ… | 32 | `test_agent_core.py` |
| `atri_x.agent.risk_confirmation` | 高风险工具确认的文案与指令识别。对应 Go：``internal/agent/risk_confirmation.go``。 | REDACTED_ARGUMENTS_TEXT, RiskConfirmationCommand, confirmat… | 366 | `test_agent_risk.py` |
| `atri_x.agent.seams` | Agent 的可选能力接缝（seam）。 | AgentSeams, DirectiveOutcome | 595 | `test_agent_bootstrap.py` |
| `atri_x.agent.segments` | 入站消息段在 ctx / LLM / 平台三种表示之间的搬运。 | InboundTurnInput, file_segment_text, has_inbound_non_text_s… | 299 | `test_agent_segments.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.session_metadata` | Session metadata 的编解码。 | ContextCompactState, LLMOriginState, SessionMetadata, decod… | 332 | `test_agent_core.py` |
| `atri_x.agent.state_watch` | ``state.toml`` 的外部编辑热加载与状态查询。 | RUNTIME_STATE_WATCH_EVERY, RuntimeStateReload, poll_runtime… | 223 | — |
| `atri_x.agent.status` | 运行状态快照的记录与发布。 | merge_runtime_status, publish_runtime_status, record_runtim… | 192 | `test_agent_assembly.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.system_prompt` | 系统提示词管理器：按来源收集分段内容并拼接。 | ConversationMeta, SystemPromptManager, SystemPromptPart, Sy… | 112 | `test_agent_prompt.py`、`test_member_persona.py`、`test_member_profile.py` |
| `atri_x.agent.system_prompt_sources` | 系统提示词的具体来源：对话元信息、SOUL、常驻记忆、工具名清单。 | ConversationMetaSystemPromptSource, ReactionSystemPromptSou… | 246 | `test_agent_prompt.py`、`test_agent_reactions.py`、`test_memory_notes.py` |
| `atri_x.agent.time_context` | 请求级时间上下文：入站消息时间基准 + 系统提示里的时间片段。 | TIME_BLOCK_MARKER, build_time_block, inbound_times, inject_… | 357 | `test_time_context.py` |
| `atri_x.agent.title` | 会话标题生成器。 | TitleGenerator, title_prompt | 164 | `test_agent_title.py`、`test_session_naming_contract.py` |
| `atri_x.agent.tool_auth` | 群级工具授权与执行闸门。对应 Go：``internal/agent/tool_auth.go``。 | ToolAuthMixin, artist_extract_uses_vision, authorize_tool_c… | 379 | `test_agent_group_policy.py`、`test_agent_risk.py`、`test_agent_tools.py` |
| `atri_x.agent.tool_cache` | 会话级工具缓存（发现结果的持久化）。对应 Go：``internal/agent/tool_cache.go``。 | ToolCacheMixin, ToolCacheState, cached_tool_name_set, cache… | 341 | `test_agent_tools.py` |
| `atri_x.agent.tool_directive` | ``@tool:`` / ``@skill:`` 指令：预载工具与 Skill。 | TOOL_DIRECTIVE_METHODS, ToolDirectiveMixin, ToolDirectiveRe… | 691 | `test_agent_directives.py` |
| `atri_x.agent.tool_directive_skill` | ``@skill:`` 指令：Skill 详情注入与包装工具预载。 | SKILL_METHODS, SkillDirectiveMixin, SkillDirectiveResult, S… | 329 | `test_agent_directives.py` |
| `atri_x.agent.tool_schemas` | 工具 schema 装配与日志正文预览。 | compact_arguments, filter_tool_schemas_by_allowlist, is_bac… | 244 | `test_agent_assembly.py` |
| `atri_x.agent.tool_tag_config` | ``tool_tags.toml``：标签 → 工具集合 + 提示词，以及按标签取工具名。 | ToolTagConfigSource, ToolTagFileState, completion_tool_name… | 372 | `test_agent_directives.py`、`test_agent_prompt.py` |
| `atri_x.agent.tool_transcript` | 工具调用在会话记录里的落库形态。对应 Go：``internal/agent/tool_transcript.go``。 | AssistantMetadata, ToolTranscriptMixin, assistant_raw_text_… | 476 | `test_agent_chat.py`、`test_agent_tools.py` |
| `atri_x.agent.toolrun_adapter` | 工具运行适配层：把 ``Agent`` 接到 ``atri_x.toolrun`` 的 Runner 上。 | AgentToolRunDeps, ConfirmResult, cached_tools_for_session, … | 980 | `test_agent_risk.py`、`test_safety_integration.py`、`test_toolrun.py` |
| `atri_x.agent.toolrun_prompt_provider` | 默认的工具 schema / 名单提供者（Agent 侧适配）。对应 Go：``internal/agent/toolrun_prompt_provider.go``。 | ToolRunPromptProvider, new_tool_run_prompt_provider | 88 | `test_agent_tools.py` |
| `atri_x.agent.tools` | 工具运行时状态装配。对应 Go：``internal/agent/tools.go``。 | AgentService, SkillLifecycle, ToolRuntimeMixin, ToolRuntime… | 518 | `test_agent_assembly.py`、`test_agent_risk.py`、`test_agent_tools.py` |
| `atri_x.agent.tools_service` | ``agent.services["tools"]`` / ``command_deps.tools`` 的装配对象。 | AgentToolsService, new_tools_service | 60 | — |
| `atri_x.agent.turn_gate` | 回合输出的"最后一公里"闸门。 | clear_turn_state, mark_turn_canceled, turn_output_allowed | 75 | — |
| `atri_x.agent.turn_output` | 回合输出端口：前台流式输出与后台静默输出。 | BackgroundTurnOutput, ForegroundTurnOutput, TurnOutput, buf… | 273 | `test_agent_chat.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.turn_override` | 单回合指令：``@model:`` / ``@image:`` / ``@use:`` 的解析、裁决与剥离。 | TURN_OVERRIDE_METHODS, TurnDirectiveMatch, TurnDirectiveRes… | 337 | `test_agent_assembly.py`、`test_agent_chat.py` |
| `atri_x.agent.vision` | 多图兜底：把图片段换成模型生成的文字描述。 | DEFAULT_VISION_FALLBACK_BUDGET_SECONDS, DEFAULT_VISION_FALL… | 518 | `test_vision_prechat.py`、`test_vision_privacy.py` |
| `atri_x.agent.wakeup` | 唤醒判定：群聊里这条消息要不要叫醒模型，以及唤醒词的剥离。 | MessageWakeup, first_token_bounds, hook_wakeup, inbound_raw… | 262 | `test_agent_participation.py`、`test_agent_wakeup.py` |
| `atri_x.agent.workspace` | 会话工作区目录的持久化适配（``tool.WorkspaceStore`` 的实现）。 | SessionWorkspaceStore, new_session_workspace_store | 154 | `test_agent_directives.py`、`test_toolrun.py` |
| `atri_x.angelmemory` | ATRI-X 的 clean-room 长期记忆层（scope-local）。 | ANGEL_FORGET_MAX_PER_MINUTE, DEFAULT_MAX_CONTENT_RUNES, DEF… | 102 | `agent_commands_fakes.py`、`test_agent_commands_services.py`、`test_agent_commands_session.py`、`test_angelmemory.py`、`test_app_ops_plugins.py`、`test_builtin_tools2.py` |
| `atri_x.angelmemory.models` | 长期记忆的领域模型（scope-local）。 | LegacySourceBackfill, Memory, RecallQuery, SourceFilter, St… | 105 | — |
| `atri_x.angelmemory.schema` | angelmemory 的建表、迁移与文本 helper。 | CREATE_MEMORIES_TABLE, DEFAULT_MAX_CONTENT_RUNES, DEFAULT_M… | 238 | — |
| `atri_x.angelmemory.service` | 长期记忆服务层：写入限额、召回与上下文构造。 | ANGEL_FORGET_MAX_PER_MINUTE, DEFAULT_MAX_CONTENT_RUNES, DEF… | 378 | `test_app_ops_plugins.py` |
| `atri_x.angelmemory.store` | scope-local 长期记忆的 SQLite store。 | Open, Store, open_store | 565 | — |
| `atri_x.app` | ``atri_x.app`` —— 装配层。 | AtrinisRuntimeAdapter, AudioTranscriptionRequest, AudioTran… | 418 | `test_app_bootstrap.py`、`test_app_doctor_capabilities.py`、`test_app_health.py`、`test_app_llm_adapters.py`、`test_app_ops_plugins.py`、`test_app_platforms.py`、`test_app_runner.py`、`test_app_runtime_agent.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py`、`test_deploy_harness.py`、`test_llm_headers.py`、`test_main.py`、`test_member_profile.py`、`test_ops_health.py`、`test_packaging_tzdata.py`、`test_safety_integration.py`、`test_session_lifecycle.py`、`test_time_config.py` |
| `atri_x.app.app` | 应用入口：运行模式、启动选项与 ``Run``。 | Options, Run, RunMode, new_runner, run, run_sync | 87 | `test_app_bootstrap.py`、`test_app_runner.py` |
| `atri_x.app.asr` | ASR（语音转写）服务装配。 | AsrTranscriberAdapter, asrTranscriberAdapter, audioCapabili… | 175 | — |
| `atri_x.app.breaker_llm` | 熔断 / 兜底包装的 LLM 客户端。 | BreakerLLM, breakerLLM, withFallbackOnError, withFallbackTo… | 374 | `test_app_llm_adapters.py` |
| `atri_x.app.cli_client` | CLI 远程客户端入口：把 CLI 平台配置成"连远端服务"的瘦客户端。 | CLIClientOptions, RunCLIClient, TryRunCLIClient, isLocalCLI… | 130 | `test_app_platforms.py` |
| `atri_x.app.config_check` | ``atri-x config check``：加载 app.toml 并给出人类可读的摘要与告警。 | CheckConfig, checkConfigWarnings, check_config, check_confi… | 250 | `test_app_platforms.py`、`test_time_config.py` |
| `atri_x.app.cron` | Cron 启动编排：异步准备 cron 任务表。 | enabledCronPlatforms, enabled_cron_platforms, platformConfi… | 115 | `test_app_bootstrap.py` |
| `atri_x.app.dependencies` | 装配层依赖注入点：各阶段请求/组件与 :class:`Dependencies`。 | ChatHistoryStoreProtocol, DefaultDependencies, Dependencies… | 333 | `test_app_platforms.py`、`test_app_runner.py` |
| `atri_x.app.doctor` | 部署验收（doctor）：配置、本地依赖、平台连通性与端到端往返。 | DEFAULT_DOCTOR_TIMEOUT, DoctorCheck, DoctorOptions, DoctorR… | 696 | `test_app_doctor_capabilities.py`、`test_app_platforms.py` |
| `atri_x.app.doctor_checks` | Doctor 的单项检查实现。 | DEFAULT_DOCTOR_TIMEOUT, HEALTH_ADDR_ENV, HEALTH_OPS_TOKEN_E… | 502 | `test_app_platforms.py` |
| `atri_x.app.environment` | 启动环境：运行模式解析、启动剖析器、平台模式映射。 | DefaultEnvironment, MODE_CLI_ONLY, MODE_FULL, MODE_SERVICE,… | 226 | `test_app_bootstrap.py` |
| `atri_x.app.foundation` | 装配根：配置、日志、主存储、聊天历史、维护服务与 cron 管理器。 | DefaultFoundationFactory, FoundationLifecycle, defaultFound… | 395 | `test_app_bootstrap.py` |
| `atri_x.app.health` | 健康与运维 HTTP 端点的启动装配。 | DEFAULT_HEALTH_LIVE_STALE_SECONDS, HEALTH_ADDR_ENV, HEALTH_… | 274 | `test_app_health.py` |
| `atri_x.app.health_handler` | 平台消息处理器包装：把每条消息的结果记进健康状态。 | HealthHandler, healthHandler | 56 | `test_app_health.py` |
| `atri_x.app.health_llm` | 健康统计包装的 LLM 客户端。 | HealthLLM, healthLLM, wrapHealthLLM, wrap_health_llm | 131 | `test_app_llm_adapters.py` |
| `atri_x.app.image_rewriter` | ``image_generate`` 的可选 LLM 改写通道。 | IMAGE_REWRITE_SYSTEM_NATURAL, IMAGE_REWRITE_SYSTEM_TAGS, Im… | 267 | `test_app_platforms.py` |
| `atri_x.app.image_to_prompt` | ``image_to_prompt`` 工具共用的视觉引擎装配。 | buildImagePromptService, build_image_prompt_service | 102 | — |
| `atri_x.app.integrations` | 集成阶段：把平台运行时接到 Agent 上，并按需拉起 atrinis。 | DefaultIntegrationFactory, defaultIntegrationFactory, resol… | 186 | `test_app_platforms.py`、`test_app_runner.py` |
| `atri_x.app.models` | LLM 客户端装配：provider → 适配器（含协议路由、健康包装、熔断包装）。 | DefaultModelFactory, ProtocolRouter, appLLMRequestOptions, … | 323 | `test_app_llm_adapters.py`、`test_llm_headers.py` |
| `atri_x.app.naming` | 会话命名事件的日志/审计记录器。 | NamingLogger, namingLogger | 110 | `test_session_lifecycle.py` |
| `atri_x.app.ops_health` | ``/metrics`` 与 ``/diagnostics`` 的载荷收集，以及两个延迟接线的 HTTP 组件。 | ImageLimitMetrics, LazyHTTPHandler, LazyJSONProvider, OMITE… | 619 | `test_app_health.py`、`test_app_ops_plugins.py`、`test_app_runtime_agent.py`、`test_member_profile.py`、`test_ops_health.py` |
| `atri_x.app.platform_caller` | 按平台名把运行时解析成"平台 API 调用器"。 | HookPlatformCallerResolver, PlatformCallerResolver, hookPla… | 71 | `test_app_platforms.py` |
| `atri_x.app.platforms` | 平台装配与运行循环。 | AtrinisRuntimeAdapter, CONNECT_NOTICE_PLATFORM, DefaultPlat… | 692 | `test_app_platforms.py`、`test_deploy_harness.py` |
| `atri_x.app.runner` | 装配运行器：按固定顺序装配各阶段并保证逆序清理。 | CleanupStep, DEFAULT_SHUTDOWN_TIMEOUT, NewRunner, Runner, m… | 543 | `test_app_ops_plugins.py`、`test_app_runner.py` |
| `atri_x.app.runtime` | 运行期装配主链：媒体中心 → 工具运行时 → Hook → Agent → 生命周期。 | DefaultRuntimeFactory, HookRuntimeLifecycle, audit_func, bu… | 620 | `test_app_runner.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py` |
| `atri_x.app.runtime_agent` | Agent 装配。 | buildAgent, build_agent | 711 | `test_app_runtime_agent.py`、`test_member_profile.py`、`test_safety_integration.py` |
| `atri_x.app.runtime_hooks` | Hook 控制面装配。 | buildHookService, build_hook_service | 120 | — |
| `atri_x.app.runtime_support` | 运行期装配的共享助手：审计入口、凭据解析、cron 服务、Hook 生命周期。 | HookRuntimeLifecycle, audit_func, build_cron_service, confi… | 319 | — |
| `atri_x.app.service_marker` | 服务实例标记：防止同一台机器上跑起第二个 ``atri-x`` service。 | ServiceMarker, ServiceMarkerError, claim_service_marker, re… | 226 | `test_app_bootstrap.py` |
| `atri_x.app.service_types` | app 内部使用的服务侧数据类型。 | AudioTranscriptionRequest, AudioTranscriptionResult, ImageP… | 94 | `test_app_platforms.py` |
| `atri_x.app.support` | 装配层的叶子级共享助手。 | call_bool, log_error, log_info, log_warn, maybe_await, mayb… | 71 | `test_app_runner.py` |
| `atri_x.app.time_check` | ``python -m atri_x.app.time_check``：时区数据与解析探针。 | check_time_data, main | 113 | `test_packaging_tzdata.py` |
| `atri_x.app.tool_runtime` | 内置工具运行时的 app 侧装配。 | ToolRuntime, ToolRuntimeOptions, build_tool_runtime | 537 | `test_app_runtime_agent.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py`、`test_member_profile.py` |
| `atri_x.app.vision` | 视觉服务装配：把共享 ``vision.Service`` 接到 Agent 的聊天视觉兜底接口。 | VisionChatDescriber, buildVisionDescriber, build_vision_des… | 261 | — |
| `atri_x.artist` | 画师串领域包。 | Artist, ArtistConflictError, ArtistRef, Compiled, DEFAULT_S… | 258 | `test_app_tool_runtime_wiring.py`、`test_artist_metadata.py`、`test_artist_service.py`、`test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_media_manager.py` |
| `atri_x.artist.metadata` | 图片容器里的生成参数恢复（只读）：PNG / JPEG / WebP 与 EXIF、XMP。 | extract_metadata, parse_metadata, parse_metadata_with_origi… | 618 | `test_artist_metadata.py`、`test_media_manager.py` |
| `atri_x.artist.metadata_formats` | 生成参数的方言解析：A1111 / NovelAI / ComfyUI / InvokeAI。 | ComfyNode, JSONNumber, MetadataText, NEGATIVE_MARKER, apply… | 597 | `test_artist_metadata.py` |
| `atri_x.artist.metadata_store` | 入库转码前留档的图片生成参数（按 media id 索引）。 | Generation, GenerationStore, generation_from_json, generati… | 468 | — |
| `atri_x.artist.parser` | 画师串的确定性解析：分段、权重解包、画师识别与还原。 | artist_name, classify, escape, extract_artists, extract_art… | 397 | — |
| `atri_x.artist.render` | 把 preset 渲染成某个方言下的最终画师串。 | Render, WeightedTag, describe_style, format_weight, neutral… | 231 | — |
| `atri_x.artist.search` | 画师串库的检索与打分（含"只要沾边就返回"的推荐排序）。 | Match, field_score, query_terms, rank, search | 240 | `test_artist_service.py` |
| `atri_x.artist.service` | 画师串服务：身份视图、可见性、检索/推荐、保存/删除与方言编译。 | Compiled, Service, Viewer, compile_raw, group_scope_key, ne… | 464 | — |
| `atri_x.artist.store` | 画师串库的落盘存储、归一化校验与并发写序。 | ArtistConflictError, ReferencedError, Store, artist_from_js… | 821 | — |
| `atri_x.artist.types` | 画师串领域类型、方言/来源枚举与各类上限。 | Artist, ArtistRef, Dialect, Extraction, GenerationMetadata,… | 474 | `test_artist_metadata.py` |
| `atri_x.asr` | OpenAI 兼容语音转写（自包含子系统）。 | ASRTranscriptionText, AsrError, BoundedMessage, BudgetLedge… | 123 | `test_asr.py` |
| `atri_x.asr.asr` | OpenAI 兼容的语音转写（``/audio/transcriptions``）。 | BudgetLedger, DEFAULT_MAX_AUDIO_BYTES, DEFAULT_MAX_CONCURRE… | 617 | — |
| `atri_x.asr.cache` | ASR 成功缓存与负缓存。 | DEFAULT_CACHE_MAX_ENTRIES, DEFAULT_CACHE_TTL, DEFAULT_MAX_C… | 179 | — |
| `atri_x.asr.error` | ASR 结构化错误分类。 | AsrError, HTTPError, bounded_message, deterministic_error, … | 168 | — |
| `atri_x.atriena` | Atriena：ATRI-X 的事件信封协议（版本、模式、状态、平台调用与目标换算）。 | Atriwisp, BuildDirectOutputs, Bus, CAPABILITY_CHAT_LEAVE, C… | 183 | `_atrinis_support.py`、`test_atriena.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py` |
| `atri_x.atriena.calls` | Atriena 平台调用（capability 与 raw 两种形态）。 | CAPABILITY_CHAT_LEAVE, CAPABILITY_MEMBER_MUTE, CAPABILITY_M… | 352 | — |
| `atri_x.atriena.delivery` | Atriena 段/目标的投递层换算。 | BuildDirectOutputs, DirectText, PathForSegment, SegmentKey,… | 153 | — |
| `atri_x.atriena.dispatcher` | Atriena 分发总线。 | Bus, Dispatcher, DispatcherNotConfiguredMessage, new_bus | 75 | — |
| `atri_x.atriena.origin` | Atriena 事件来源。 | ORIGIN_HOOK, ORIGIN_HTTP_TOKEN, ORIGIN_INTERNAL, Origin, Or… | 82 | — |
| `atri_x.atriena.types` | Atriena 事件协议的领域类型与版本/模式/状态常量。 | Atriwisp, Call, CallKind, CallKindCapability, CallKindRaw, … | 490 | — |
| `atri_x.atrinis` | Atrinis：ATRI-X 的对内/对外事件入口（Atrwisp 后台任务服务）。 | ATRIWISP_NAME_PATTERN, Atriwisp, AuditFunc, AuthMixin, Call… | 212 | `_atrinis_support.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py` |
| `atri_x.atrinis.auth` | Atrinis 鉴权：令牌、atriwisp 策略、内部工具白名单与外部工具声明校验。 | AuthMixin, MAX_EXTERNAL_TOOL_TIMEOUT_SECONDS | 153 | — |
| `atri_x.atrinis.background` | Atrinis 的通用小工具：模型槽位/会话模式白名单、Actor 构造、平台与沙盒子目录。 | ATRINIS_MODEL_SLOTS, ATRINIS_SESSION_MODES, atrinis_actor, … | 142 | — |
| `atri_x.atrinis.direct` | Atrinis ``direct`` 模式：执行平台调用、投递给目标、回填消息映射。 | DirectMixin | 180 | — |
| `atri_x.atrinis.http` | Atrinis 的 HTTP 入口与后台运行时。 | DEFAULT_ADDR, DEFAULT_IDLE_TIMEOUT, DEFAULT_MAX_BODY_BYTES,… | 458 | — |
| `atri_x.atrinis.llm` | Atrinis ``llm`` 模式：后台跑一次任务、解析 JSON 结果、准备并触发汇报投递。 | LLMMixin | 338 | — |
| `atri_x.atrinis.media` | Atrinis 媒体：入站段物化、报告段导入，以及宿主侧 workspace 导入/导出 API。 | MediaMixin, OWNER_TYPE_ATRINIS_EVENT, media_filename | 378 | — |
| `atri_x.atrinis.outbox` | Atrinis 汇报出箱：准备投递行、认领、逐批发送、失败回退与重启恢复。 | OutboxMixin, output_from_json, output_to_json, receipt_from… | 457 | — |
| `atri_x.atrinis.prepare` | Atrinis 事件预处理：字段归一化、校验、去重键与各派生 JSON 列。 | ATRIWISP_NAME_PATTERN, PrepareMixin, normalize_targets, nor… | 211 | — |
| `atri_x.atrinis.segments` | Atrinis 段校验与段换算（纯函数）。 | segments_content_text, segments_llm, validate_media_segment… | 109 | — |
| `atri_x.atrinis.service` | Atrinis 服务：装配、鉴权入口、去重与三种模式（record / direct / llm）的分派。 | AuditFunc, EnqueueLLMFunc, ModelResolverFunc, Options, Queu… | 349 | — |
| `atri_x.atrinis.store` | Atrinis 的事件状态收尾、审计/日志与哈希工具。 | StoreMixin, background_tool_names, cleanup_token, content_h… | 223 | — |
| `atri_x.atrinis.targets` | Atrinis 投递目标解析：``all`` 展开、禁用规则、去重排序与落库 JSON 还原。 | TargetsMixin, config_targets_to_atrinis, decode_resolved_ta… | 113 | — |
| `atri_x.atrinis.types` | Atrinis 对外类型别名与常量。 | Atriwisp, Call, Event, ModeDirect, ModeLLM, ModeRecord, Req… | 63 | `test_atrinis_http.py` |
| `atri_x.atriyph` | Atriyph 语言层：``SKILL.atriyph`` 的解析、校验与规则卡。 | AtriyphError, DIAGNOSTIC_ERROR, DIAGNOSTIC_WARNING, Diagnos… | 78 | `_skill_fixtures.py`、`test_atriyph.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py` |
| `atri_x.atriyph.ast` | 对应 Go：``internal/atriyph/ast.go`` —— Atriyph 文档的 AST 与诊断类型。 | AtriyphError, DIAGNOSTIC_ERROR, DIAGNOSTIC_WARNING, Diagnos… | 76 | — |
| `atri_x.atriyph.parser` | 对应 Go：``internal/atriyph/parser.go`` —— Atriyph 解析与校验。 | error_diagnostics, format_diagnostics, lint, lint_warnings,… | 597 | — |
| `atri_x.atriyph.rules` | 对应 Go：``internal/atriyph/rules.go`` —— Atriyph 的固定常量与规则卡。 | FORMAT, SKILL_FILE_NAME, rule_card | 54 | — |
| `atri_x.background` | cron / Atrinis 共用的后台 LLM 类型与结果 helper。 | BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, BACKGROUND_SHELL_AUD… | 73 | `_atrinis_support.py`、`test_agent_admission.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_llm.py`、`test_background.py`、`test_cron.py` |
| `atri_x.background.background` | cron 与 Atrinis 共用的后台 LLM 类型。对应 Go：``internal/background/background.go``。 | BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, BACKGROUND_SHELL_AUD… | 311 | — |
| `atri_x.background.report` | 后台报告的投递输出构造。对应 Go：``internal/background/report.go``。 | BuildReportOutputs, BuildReportSegmentOutput, PathInstructi… | 106 | — |
| `atri_x.background.result` | 后台任务 JSON 结果的解析与格式重试提示。对应 Go：``internal/background/result.go``。 | DefaultJSONRetryPrompt, ParseJSONResult, default_json_retry… | 65 | — |
| `atri_x.character` | 基于目录的角色库（自包含子系统）。 | ARCHIVE_FORMAT, ARCHIVE_VERSION, ActiveIDs, AssetManifest, … | 213 | `agent_commands_fakes.py`、`test_agent_assembly.py`、`test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_character.py`、`test_character_archive.py`、`test_member_persona.py` |
| `atri_x.character.activation` | 把人设卡激活成全局系统人设（写 ``SOUL.md``），以及备份 / 回滚。 | SOUL_BACKUP_SUFFIX, SOUL_RESTORE_BACKUP_SUFFIX, SoulActivat… | 172 | `test_character_archive.py` |
| `atri_x.character.archive` | 角色卡导入 / 导出（JSON / ZIP / Markdown / TOML / 目录）。 | ARCHIVE_FORMAT, ARCHIVE_VERSION, CharacterPayload, ImportRe… | 614 | `test_character_archive.py` |
| `atri_x.character.manifest` | 角色库的 sha256 备份清单。 | go_json_indent, manifest, write_manifest | 172 | — |
| `atri_x.character.search` | 角色库检索：内存索引上的轻量匹配。 | DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, SNIPPET_RUNES, has_… | 175 | `test_character.py` |
| `atri_x.character.store` | 基于目录的角色库：读取、可见性、备份清单。 | AssetManifest, AssetManifestEntry, Character, CharacterErro… | 852 | — |
| `atri_x.character.toml_codec` | ``character.toml`` 的读写映射。 | read_character, read_doc, to_file, write_toml | 297 | — |
| `atri_x.character.write` | 角色写入：创建 / 更新 / 删除、图片索引与版本来源记录。 | add_image, add_image_with_meta, delete, remove_image, write | 384 | `test_character.py` |
| `atri_x.command` | 通用命令框架：``Info`` / ``Request`` / ``Result`` / ``Router``。 | — | 44 | `agent_commands_fakes.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_builtin_runtime_resources.py`、`test_command_router.py`、`test_completion.py`、`test_security_grants.py` |
| `atri_x.command.handler` | 函数式命令处理器。 | FuncHandler, new_func | 39 | — |
| `atri_x.command.router` | 命令路由：注册、解析、派发、按 actor 过滤与整行补全。 | Parsed, Router, new_router, normalize_name, normalize_prefi… | 272 | — |
| `atri_x.command.types` | 通用命令框架的类型：``Info``、``Request``、``Result``、``SessionEffect`` 与接口。 | Completer, Completion, CompletionRequest, Continuation, Han… | 158 | — |
| `atri_x.completion` | 平台补全服务：命令名/参数、``@tool:``、``@skill:``、``@char:``、风险确认、Fork。 | — | 64 | `test_completion.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py` |
| `atri_x.completion.character` | ``@char:`` / ``@c:`` 角色指令补全来源。 | CharacterDirectiveSource, CharacterOption | 77 | — |
| `atri_x.completion.completion` | 平台补全服务：候选条目、请求与"首个非空来源胜出"的合并规则。 | Item, Request, Service, Source, new_service, texts | 81 | `test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py` |
| `atri_x.completion.fork` | ``/fork <message_id>`` 的助手消息补全来源。 | ForkMessageSource, has_fork_args | 104 | — |
| `atri_x.completion.risk` | 风险确认阶段的命令补全来源。 | RiskConfirmationSource, has_any_command_args | 89 | — |
| `atri_x.completion.router` | 命令补全来源：整行命令名与命令参数。 | RouterSource | 101 | — |
| `atri_x.completion.tool` | ``@tool:`` / ``@skill:`` 指令补全来源。 | ToolDirectiveSource, fuzzy_match, is_allowed_skill, is_plai… | 287 | — |
| `atri_x.config` | 配置层：``app.toml`` / ``services.toml``（旧 ``providers.toml``）/ ``state.toml`` / | — | 83 | `_atrinis_support.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_commands_integration.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_directives.py`、`test_agent_group_policy.py`、`test_agent_model.py`、`test_agent_participation.py`、`test_agent_prompt.py`、`test_agent_reactions.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_llm_adapters.py`、`test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_tools4.py`、`test_config.py`、`test_config_assets.py`、`test_config_loader.py`、`test_config_state.py`、`test_config_web_search.py`、`test_daily_summary.py`、`test_deploy_harness_cli.py`、`test_dsh_route.py`、`test_groupkb.py`、`test_maintenance.py`、`test_maintenance_dsh.py`、`test_maintenance_report.py`、`test_member_profile.py`、`test_request_manager.py`、`test_safety_core.py`、`test_time_config.py`、`test_time_service.py`、`test_timesync_https.py`、`test_timesync_sntp.py`、`test_timesync_verifier.py`、`test_tool_runtimeinfo.py`、`test_toolrun.py` |
| `atri_x.config.app` | ``app.toml`` 根结构 ``AppConfig``（Go ``Config``）与默认值应用 / 校验 / 合并。 | APP_DIR_NAME, AppConfig, ConfigFilesConfig, ToolProfileConf… | 786 | `test_config.py`、`test_config_loader.py`、`test_config_web_search.py`、`test_time_config.py` |
| `atri_x.config.app_sections` | ``app.toml`` 静态行为段的 dataclass。 | AgentConfig, AgentDshConfig, AngelMemoryConfig, ArtistLibra… | 1225 | `test_agent_admission.py`、`test_agent_core.py`、`test_agent_participation.py`、`test_agent_reactions.py`、`test_config.py`、`test_config_state.py`、`test_config_web_search.py`、`test_request_manager.py`、`test_safety_core.py`、`test_toolrun.py` |
| `atri_x.config.assets` | 内置默认配置资产。**本文件由 ``scripts/extract_assets.py`` 生成，不要手改。** | DefaultAsset, copy_asset_tree, default_config_asset, ensure… | 100 | `test_config.py`、`test_config_assets.py` |
| `atri_x.config.inspect` | 严格解码框架与配置自检（未知键、内置 Skill 漂移）。 | ConfigDecodeError, ConfigError, MissingConfigFileError, OFF… | 423 | `test_config.py`、`test_time_config.py` |
| `atri_x.config.loader` | 配置文件的查找、读取与合并（``app.toml`` 为入口）。 | ENV_CONFIG_FILE, PLUGIN_CONFIG_DIR_NAME, config_env, ensure… | 373 | `test_config.py`、`test_config_loader.py`、`test_deploy_harness_cli.py` |
| `atri_x.config.services` | 共享只读服务配置：``[providers.*]`` / ``[model_metadata]`` / ``[model_profiles]`` / | APIProtocol, ASRConfig, AudioSupport, DEFAULT_CONTEXT_WINDO… | 710 | `test_config.py`、`test_config_loader.py`、`test_config_web_search.py` |
| `atri_x.config.state` | ``state.toml`` 运行态：结构、读写、以及**热加载**（mtime 判定 + 分区合并）。 | ALLOWED_LEARNING_MODERATION_ACTIONS, ContextOverflowConfig,… | 748 | `test_config_state.py`、`test_groupkb.py` |
| `atri_x.config.time_sections` | ``[time]`` / ``[time.verification]`` / ``[[time.sources]]`` 配置段。 | DEFAULT_TIMEZONE, TIME_MODES, TIME_PROTOCOLS, TimeConfig, T… | 322 | `test_time_config.py`、`test_time_service.py`、`test_timesync_https.py`、`test_timesync_sntp.py`、`test_timesync_verifier.py` |
| `atri_x.config.tool_tags` | ``tool_tags.toml`` 解码。 | ToolTagConfig, ToolTagsConfig, load_tool_tags | 49 | — |
| `atri_x.contextmgr` | 上下文基础能力：加载、窗口、用量、预算、压缩与摘要。 | COMPACT_SYSTEM_PROMPT, ClientProvider, CompactMessage, Comp… | 110 | `test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_contextmgr.py`、`test_contextmgr_window_metadata.py` |
| `atri_x.contextmgr.budget` | Prompt 预算、token 估算与文本截断。 | DEFAULT_MAX_PROMPT_RATIO, DEFAULT_RESERVE_OUTPUT_TOKENS, DE… | 261 | `test_contextmgr.py`、`test_contextmgr_window_metadata.py` |
| `atri_x.contextmgr.compact_prompt` | 压缩与摘要的系统提示词、prompt 组装与摘要拼装。 | COMPACT_SYSTEM_PROMPT, TEXT_SUMMARIZER_SYSTEM_PROMPT, assem… | 129 | `test_contextmgr.py` |
| `atri_x.contextmgr.compressor` | 客户端摘要压缩后端。 | CapCompactRequest, CompactMessage, CompactRequest, CompactR… | 392 | `test_contextmgr.py` |
| `atri_x.contextmgr.loader` | 上下文加载与 Fork 回溯。 | LoadedContext, Loader, MAX_FORK_DEPTH, STORE_REQUIRED | 172 | `test_contextmgr.py` |
| `atri_x.contextmgr.usage` | 用量状态与厂商 usage 的展示格式。 | FormatTokens, UsageState, format_tokens | 54 | `test_contextmgr.py` |
| `atri_x.contextmgr.window` | 上下文窗口解析与缓存。 | ClientProvider, WindowResolver, new_window_resolver | 259 | `test_contextmgr.py`、`test_contextmgr_window_metadata.py` |
| `atri_x.cron` | 中央 Cron Runtime：持久化 job 的调度、注册、upsert、禁用、删除、运行状态与执行日志。 | AuditFunc, CronActor, CronDeliveryOutputState, CronDelivery… | 230 | `test_agent_admission.py`、`test_agent_cron.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_cron.py`、`test_cron.py`、`test_cron_delivery.py`、`test_cron_dst.py`、`test_maintenance.py` |
| `atri_x.cron.delivery` | 逐目标逐输出的投递、状态持久化、降级与 receipt 映射。对应 Go：``internal/cron/delivery.go``。 | DeliveryMixin, ResolvedCronTarget, cron_sandbox, cron_targe… | 688 | — |
| `atri_x.cron.delivery_state` | 一轮投递的状态模型与编解码。 | CronDeliveryOutputState, CronDeliveryState, CronDeliveryTar… | 240 | — |
| `atri_x.cron.errors` | cron 包内部的错误聚合 helper（Python 侧新增件）。 | join_errors | 25 | — |
| `atri_x.cron.execution` | Direct / LLM 执行、报告生成与 JSON 格式重试。对应 Go：``internal/cron/execution.go``。 | ExecutionMixin, cron_format_retry_prompt, cron_parse_failed… | 387 | `test_agent_admission.py` |
| `atri_x.cron.jsonview` | cron 领域对象的 **Go ``json.Marshal`` 形态**（Python 侧新增件）。 | GO_ZERO_TIME_TEXT, cron_job_json, job_view_json | 94 | — |
| `atri_x.cron.manager` | 中央 Cron Manager：注册 handler、调度启用的 job、维护运行状态。对应 Go：``internal/cron/manager.go``。 | Handler, Manager, Registry, UpsertJobRequest, compute_next_… | 543 | `test_cron.py` |
| `atri_x.cron.model` | 定时任务的领域模型、JSON 编解码与校验。对应 Go：``internal/cron/model.go``。 | CronActor, CronDeliveryOutputState, CronDeliveryState, Cron… | 650 | `test_cron_dst.py` |
| `atri_x.cron.recovery` | 平台连接跟踪、过期 once 扫描与补发入口。对应 Go：``internal/cron/recovery.go``。 | RecoveryMixin | 233 | — |
| `atri_x.cron.service` | Cron Service：装配、CRUD 与公开入口。对应 Go：``internal/cron/service.go``。 | AuditFunc, LLMRunner, METADATA_KIND, Options, PlatformTarge… | 611 | `test_agent_cron.py` |
| `atri_x.delivery` | 平台无关的输出意图与发送管理。 | — | 65 | `_atrinis_support.py`、`agent_fakes.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_core.py`、`test_agent_group_policy.py`、`test_agent_reactions.py`、`test_app_platforms.py`、`test_atriena.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_background.py`、`test_builtin_tools3.py`、`test_cron.py`、`test_cron_delivery.py`、`test_delivery.py`、`test_hook_manager.py`、`test_hook_output.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_media_manager.py`、`test_media_platform.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_request_manager.py`、`test_safety_integration.py` |
| `atri_x.delivery.delivery` | 平台无关的输出意图、发送凭据与投递规则。 | DeliveryError, Kind, Manager, MessageSender, MessageStream,… | 676 | `test_background.py`、`test_cron.py`、`test_cron_delivery.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_media_manager.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py` |
| `atri_x.devtools` | ``atri_x.devtools``：把仓库事实**算出来**的开发期索引工具。 | ARTIFACTS, ModuleInfo, RepoIndex, TestFile, check_artifacts… | 58 | — |
| `atri_x.devtools.__main__` | ``python -m atri_x.devtools`` 的入口。 | — | 12 | — |
| `atri_x.devtools.astindex` | AST 索引：把 ``atri_x/`` 的模块事实与 import 边解析成结构化数据。 | ModuleInfo, RepoIndex, TestFile, collect_metrics, go_path_f… | 413 | — |
| `atri_x.devtools.boundaries` | 枢纽模块的公开面声明 + "跨域绕开 facade"的判定。 | BoundaryViolation, HUB_FACADES, PUBLIC_SUBMODULES, find_vio… | 100 | — |
| `atri_x.devtools.cli` | ``python -m atri_x.devtools <cmd>``：生成索引的命令行入口。 | main | 156 | — |
| `atri_x.devtools.domaindocs` | 域级文档的生成器：每个高价值域一个 ``README.md``，外加术语表。 | DOMAIN_NOTES, DomainNote, GLOSSARY_PATH, domain_readme_path… | 321 | — |
| `atri_x.devtools.entrypoints` | 入口索引的渲染：进程入口 / 装配层 / 命令 / 工具 / 平台 / Hook / GUI / 任务入口。 | TASK_ENTRIES, TaskEntry, locator_definitions, render_entryp… | 268 | — |
| `atri_x.devtools.markdown` | 生成物用到的 Markdown 小工具（无模板引擎）。 | HEADER, artifact, cell, code | 41 | — |
| `atri_x.devtools.render` | 把 :class:`atri_x.devtools.astindex.RepoIndex` 渲染成可提交的索引产物。 | ARTIFACTS, HEADER, TASK_ENTRIES, TaskEntry, all_artifacts, … | 399 | — |
| `atri_x.directive` | 指令前缀识别与补全 token 解析。 | — | 11 | `test_completion.py`、`test_directive.py` |
| `atri_x.directive.tool` | 指令前缀识别与补全 token 解析。 | ToolCompletionToken, ToolMatch, character_matches, image_pr… | 210 | `test_completion.py` |
| `atri_x.dist` | ``atri_x.dist``：Windows 分发件（安装器 + 快捷方式）。 | APP_NAME, INSTALL_MARKER, InstallError, InstallReport, UNIN… | 70 | — |
| `atri_x.dist.install` | 安装/卸载（用户级，不写系统目录、不需要管理员权限）。 | APP_NAME, INSTALL_MARKER, InstallError, InstallReport, UNIN… | 583 | — |
| `atri_x.dist.marker` | 安装标记 ``install.json`` 的读写（安装器与 ``status`` 共用）。 | INSTALL_MARKER, read_marker, write_marker | 77 | — |
| `atri_x.dist.paths` | 安装器的路径判定（程序目录、安装目录、数据目录、快捷方式目录）。 | APP_NAME, DATA_DIR_NAME, InstallError, data_dir, default_ta… | 91 | — |
| `atri_x.dist.registry` | 安装器的注册表后端（只写 ``HKCU``，不需要管理员权限）。 | UNINSTALL_KEY, WindowsRegistry, update_user_path | 87 | — |
| `atri_x.dist.shell_link` | 创建/读取 Windows 快捷方式（``.lnk``），纯 ``ctypes`` 调 COM。 | ShortcutError, create_shortcut, read_shortcut_target, short… | 229 | — |
| `atri_x.errors` | 全仓哨兵异常。 | AmbiguousIDError, AtriXError, BackgroundTakenOverError, CLI… | 279 | `_atrinis_support.py`、`agent_commands_fakes.py`、`agent_fakes.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_session.py`、`test_agent_context_policy.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_group_policy.py`、`test_agent_inbound_media.py`、`test_agent_model.py`、`test_agent_prompt.py`、`test_angelmemory.py`、`test_app_bootstrap.py`、`test_app_health.py`、`test_app_llm_adapters.py`、`test_app_platforms.py`、`test_app_runner.py`、`test_artist_service.py`、`test_atriena.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_cron.py`、`test_builtin_files.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_character.py`、`test_character_archive.py`、`test_contextmgr.py`、`test_cron.py`、`test_cron_dst.py`、`test_dsh_route.py`、`test_fileops.py`、`test_groupanalysis.py`、`test_historygate.py`、`test_launcher_cli.py`、`test_llm_breaker.py`、`test_logging.py`、`test_long_memory_scope.py`、`test_main.py`、`test_maintenance.py`、`test_maintenance_report.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_media_resolver.py`、`test_memory_notes.py`、`test_ops.py`、`test_platform_builtin.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py`、`test_platform_headless.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py`、`test_request_manager.py`、`test_resident_memory.py`、`test_selflearning.py`、`test_session.py`、`test_session_lifecycle.py`、`test_signal.py`、`test_sqlite_media.py`、`test_sqlite_migrations.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_time_service.py`、`test_timesync_sntp.py`、`test_timezones.py`、`test_tool_discover.py`、`test_tool_media_runtime.py`、`test_tool_runtimeinfo.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_toolrun_denied.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.groupanalysis` | 群聊天统计与可选摘要。对应 Go：``internal/groupanalysis/``。 | DEFAULT_RANGE_LIMIT, DEFAULT_SUMMARY_INPUT_RUNES, DEFAULT_S… | 60 | `test_groupanalysis.py` |
| `atri_x.groupanalysis.service` | 群聊统计（clean-room），只依赖 ``chat_history`` / ``outbound_messages``。 | DEFAULT_RANGE_LIMIT, MAX_RANGE_LIMIT, MAX_SENDER_NAME_RUNES… | 446 | — |
| `atri_x.groupanalysis.summarizer` | 群分析的可选摘要器。 | DEFAULT_SUMMARY_INPUT_RUNES, DEFAULT_SUMMARY_MAX_RUNES, DEF… | 160 | `test_groupanalysis.py` |
| `atri_x.groupkb` | 群知识库（确定性 FAQ 匹配）。对应 Go：``internal/groupkb/groupkb.go`` 的包声明。 | Match, MatchContains, MatchExact, MatchKeywords, Normalize,… | 44 | `test_groupkb.py` |
| `atri_x.groupkb.groupkb` | 确定性 FAQ 匹配引擎（纯本地、无模型依赖）。 | MatchContains, MatchExact, MatchKeywords, Normalize, Normal… | 250 | — |
| `atri_x.gui` | ``atri_x.gui``：Windows 桌面端（Tk 前端 + 托盘 + 后台服务管理）。 | GuiApp, GuiConfigError, GuiSettings, RemoteClient, Transcri… | 76 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_configedit.py`、`test_gui_diagnostics.py`、`test_gui_extractconfig.py`、`test_gui_icon.py`、`test_gui_imagemount.py`、`test_gui_link.py`、`test_gui_maintenance.py`、`test_gui_members.py`、`test_gui_modelcatalog.py`、`test_gui_proto_manager.py`、`test_gui_protocol.py`、`test_gui_qqproto.py`、`test_gui_searchconfig.py`、`test_gui_service.py`、`test_gui_tabs.py`、`test_gui_timepanel.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.__main__` | ``python -m atri_x.gui``：桌面端进程入口。 | build_settings, main, parse_args | 217 | `test_gui_diagnostics.py` |
| `atri_x.gui.about` | 「关于 ATRI-X」窗口：版本、项目简介、仓库地址、推荐语与署名。 | DESCRIPTION, POWERED_BY, RECOMMEND, REPO_URL, TAGLINE, abou… | 139 | `test_gui_tabs.py` |
| `atri_x.gui.actions` | 主窗口的「服务」动作：启动 / 重启 / 停止后台服务、打开目录/日志、关于。 | handle_restart, open_config_dir, open_service_log, restart_… | 170 | `test_gui_diagnostics.py` |
| `atri_x.gui.app` | Tk 主窗口：选项卡（对话/模型/人设卡/平台/群友/知识库/进阶/日志/功能）+ 状态栏 + 托盘常驻。 | GuiApp, WINDOW_TITLE | 857 | `test_gui_diagnostics.py`、`test_gui_tabs.py` |
| `atri_x.gui.autostart` | 开机自启：写用户注册表 ``Run`` 项（``HKCU\…\CurrentVersion\Run``）。 | AutostartState, RUN_KEY, VALUE_NAME, command_line, disable,… | 104 | — |
| `atri_x.gui.avatars` | 群友头像：拼平台头像 URL、下载、解码缩放成统一的本地 PNG 缓存。 | AVATAR_DIR, AVATAR_SIZE, AvatarError, avatar_file, avatar_u… | 164 | `test_gui_members.py`、`test_gui_tabs.py` |
| `atri_x.gui.client` | GUI 的 WS 客户端：连 ``ws://…/cli/v1/ws``、握手、收发帧、断线重连。 | REMOTE_MSG_COMPLETE, RemoteClient, STATE_AUTH_FAILED, STATE… | 301 | `test_gui_client.py` |
| `atri_x.gui.configedit` | 桌面端的配置读写底座：路径解析、TOML 行级补丁、``.env``、文本文件。 | ConfigEditError, ConfigPaths, default_config_path, load_pat… | 672 | `test_gui_configedit.py`、`test_gui_extractconfig.py`、`test_gui_link.py`、`test_gui_maintenance.py`、`test_gui_modelcatalog.py`、`test_gui_proto_manager.py`、`test_gui_qqproto.py`、`test_gui_searchconfig.py`、`test_gui_tabs.py`、`test_gui_timepanel.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.configmodel` | 桌面端「模型」页的数据模型：LLM Provider 与模型分配。 | KeyState, MODEL_SLOTS, ModelSlot, ProviderView, REQUIRED_MO… | 461 | `test_gui_configedit.py`、`test_gui_modelcatalog.py` |
| `atri_x.gui.cronschedule` | 界面上的 cron 小工具：「每天 / 每周 / 每月」结构 ↔ 5 段表达式。 | KIND_CUSTOM, KIND_DAILY, KIND_LABELS, KIND_MONTHLY, KIND_WE… | 227 | `test_gui_maintenance.py` |
| `atri_x.gui.dialogs` | 「进阶」页的二级设置窗口（自学习 / HOOK 插件 / 定时维护任务）。 | Dialog, HooksDialog, MaintenanceDialog, SelfLearningDialog,… | 55 | `test_gui_maintenance.py`、`test_gui_tabs.py` |
| `atri_x.gui.dialogs.common` | 子设置弹窗的公共骨架：独立窗口 + 底部状态行 + 保存/重载/关闭按钮。 | Dialog | 122 | — |
| `atri_x.gui.dialogs.hooks` | 「HOOK 插件配置」子窗口：直接编辑 ``plugins/hooks.toml``。 | HooksDialog | 162 | — |
| `atri_x.gui.dialogs.maintenance` | 「定时维护任务」子窗口：编辑 ``app.toml`` 的 ``[maintenance.*]``。 | MaintenanceDialog | 521 | — |
| `atri_x.gui.dialogs.selflearning` | 「自学习设置」子窗口：编辑 ``app.toml`` 的 ``[self_learning]``。 | SelfLearningDialog | 116 | — |
| `atri_x.gui.dialogs.tasks` | 维护任务清单 + 「界面状态 ↔ cron」翻译（无 Tk，便于单独测试）。 | CRON_PRESETS, MAINTENANCE_TASKS, MaintenanceTask, TASK_BY_K… | 180 | `test_gui_maintenance.py` |
| `atri_x.gui.extractconfig` | 桌面端「模型 → 抓取服务」页的数据层：读写 ``services.toml`` 的 ``[web_extract]``。 | DEFAULT_EXTRACT_ENGINES, ExtractEngineView, PROTOCOL_HINTS,… | 222 | `test_gui_extractconfig.py`、`test_gui_tabs.py` |
| `atri_x.gui.featureview` | 「功能」页的数据来源：把配置文件里能看出来的功能开关汇总成一张表。 | FeatureRow, collect_features | 594 | `test_gui_configedit.py`、`test_gui_tabs.py`、`test_gui_timepanel.py` |
| `atri_x.gui.icon` | 品牌图标素材的运行时解析。 | BRAND_DIR, app_icon, app_icon_png, available | 45 | `test_gui_icon.py` |
| `atri_x.gui.imagemount` | 本地生图服务的「挂载」：把端口映射 / SSL 链接翻译成 OpenAI 兼容 Base URL。 | DEFAULT_COMFY_PORT, DEFAULT_HOST, DEFAULT_PREFIX, ImageMoun… | 357 | `test_gui_imagemount.py`、`test_gui_tabs.py` |
| `atri_x.gui.journal` | 界面操作日志：把桌面端"点了什么、改了哪个文件"记进 ``<配置目录>/gui/gui.log``。 | JOURNAL_NAME, MAX_JOURNAL_BYTES, journal_path, read_lines, … | 91 | `test_gui_diagnostics.py`、`test_gui_tabs.py` |
| `atri_x.gui.linkstate` | 「在线」选项卡的数据层：平台配置、协议端探测、后台服务健康快照。 | DEFAULT_PLATFORM, EndpointProbe, HEALTH_PATH, HealthReport,… | 573 | `test_gui_link.py`、`test_gui_qqproto.py` |
| `atri_x.gui.memberdata` | 群友角色数据：成员档案的 GUI 适配层 + 群友发现。 | GroupScope, MAX_PROFILE_RUNES, MAX_TAGS, MEMBERS_FILE, Memb… | 222 | `test_gui_members.py`、`test_gui_tabs.py` |
| `atri_x.gui.modelcatalog` | 模型目录：从上游 ``/models`` 拉可用模型，并把「备注名」缓存在本地。 | CATALOG_FILE, CATALOG_VERSION, Catalog, CatalogEntry, Catal… | 418 | `test_gui_modelcatalog.py`、`test_gui_tabs.py` |
| `atri_x.gui.process` | 进程与路径助手：程序目录、状态目录、后台服务的 pid 记录与终止、单实例。 | app_dir, application_paths, cleanup_service_pid, focus_exis… | 363 | `test_gui_diagnostics.py` |
| `atri_x.gui.proto_manager` | 协议端管理层：按配置选后端（NapCat / SnowLuma），统一 prepare/launch/stop/status。 | apply_connection, configured_backend, launch, prepare, stat… | 190 | `test_gui_proto_manager.py` |
| `atri_x.gui.proto_process` | 协议端进程托管：启动、停止、PID 归属校验。 | STATE_NAME, clear_state, launch_console, managed_status, re… | 259 | `test_gui_proto_manager.py` |
| `atri_x.gui.proto_setup` | 协议端环境描述（NapCat / SnowLuma 共用）。 | ProtoSetup, QqProtoSetup | 76 | `test_gui_proto_manager.py`、`test_gui_qqproto.py` |
| `atri_x.gui.protocol` | GUI 侧的协议解析与对话模型（纯逻辑，不依赖 tkinter，可离线测试）。 | ChatLine, FRAME_HANDLERS, FRAME_SPECIAL, HistoryItem, ROLE_… | 454 | `test_cli_sessions.py`、`test_gui_diagnostics.py`、`test_gui_protocol.py` |
| `atri_x.gui.qqproto` | 内置 QQ 协议端环境（NapCat + 便携 QQ）的首次运行准备。 | BUNDLE_ENV_VAR, DEFAULT_ONEBOT_PORT, DEFAULT_WEBUI_PORT, EN… | 567 | `test_gui_proto_manager.py`、`test_gui_qqproto.py` |
| `atri_x.gui.qqproto_cli` | ``atri-x.exe -m atri_x.gui.qqproto`` 的命令行入口。 | main | 86 | — |
| `atri_x.gui.reactionpage` | 「人设卡 → 表情图库」子选项卡（Python 侧新增，无 Go 对应）。 | CATALOG_FILENAME, REACTION_TAB_TITLE, ReactionPanel, galler… | 553 | — |
| `atri_x.gui.searchconfig` | 桌面端「模型 → 搜索服务」页的数据层：读写 ``services.toml`` 的 ``[web_search]``。 | DEFAULT_SEARCH_ENGINES, PROTOCOL_HINTS, PROTOCOL_LABELS, Se… | 249 | `test_gui_searchconfig.py`、`test_gui_tabs.py` |
| `atri_x.gui.selftest` | 桌面端自检：不连服务、不建托盘，只验证 Tk 侧能构建与渲染。 | run_selftest, safe_print | 115 | — |
| `atri_x.gui.service` | GUI 的「服务侧」准备：配置、token、后台服务的探测与拉起。 | DEFAULT_CLIENT_ID, DEFAULT_HEALTH_ADDR, DEFAULT_TOKEN_ENV, … | 638 | `test_gui_link.py`、`test_gui_qqproto.py`、`test_gui_service.py`、`test_gui_tabs.py` |
| `atri_x.gui.sessions` | 会话侧边栏：会话列表 + 新建/刷新（GUI 的"会话列表/历史"入口）。 | SessionSidebar, load_history_into | 156 | `test_cli_sessions.py` |
| `atri_x.gui.snowluma_proto` | SnowLuma 协议端环境的探测与托管（可选的备用协议端，外置，**不内置分发**）。 | CONSENT_NAME, DEFAULT_WEBUI_PORT, ENV_VAR, ONEBOT_CONFIG_PR… | 261 | `test_gui_proto_manager.py` |
| `atri_x.gui.tabs` | 桌面端的选项卡集合。 | AdvancedTab, CharacterTab, ChatTab, ConfigTab, FeaturesTab,… | 56 | `test_gui_link.py`、`test_gui_qqproto.py`、`test_gui_tabs.py`、`test_gui_timepanel.py` |
| `atri_x.gui.tabs.advanced` | 「进阶」选项卡：生图/视觉/语音、记忆与群功能开关、日志级别。 | AdvancedTab | 530 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.base` | 配置选项卡的公共骨架：底部状态行、错误弹窗、"需要重启"提示。 | ConfigTab, confirm, message | 136 | — |
| `atri_x.gui.tabs.character` | 「人设卡」选项卡：角色库编辑 + 系统人设（SOUL.md）。 | CharacterTab | 769 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.chat` | 「对话」选项卡：会话列表 + 聊天记录 + 输入框。 | ChatTab | 165 | — |
| `atri_x.gui.tabs.extractpanel` | 「模型 → 抓取服务」子页：``web_extract`` 的通道列表、表单、顺序与密钥。 | ExtractServicePanel | 418 | — |
| `atri_x.gui.tabs.features` | 「功能」选项卡：功能状态一览 + 服务/日志面板。 | FeaturesTab | 219 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.knowledge` | 「知识库」选项卡：群知识库 / FAQ 的全局配置 + 现有条目查看。 | KnowledgeTab, load_entries | 296 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.link` | 「在线」选项卡：一条线看住「后台服务 → 协议端 → QQ 平台」三段，并提供上下线开关。 | LinkTab, REFRESH_INTERVAL_MS | 822 | `test_gui_link.py`、`test_gui_qqproto.py` |
| `atri_x.gui.tabs.logs` | 「日志」选项卡：把后台服务写的日志和界面操作日志放到一个可筛选的窗口里。 | LogsTab | 478 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.members` | 「群友」选项卡：按群管理群友的角色数据（备注名 / 标签 / 角色数据 / 头像）。 | MembersTab | 787 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.model` | 「模型」选项卡：LLM Provider（含 API Key）、模型分配、搜索服务与抓取服务。 | ModelTab | 820 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.mountpanel` | 「进阶」页生图区的本地挂载面板（:class:`MountPanelMixin`）。 | MountPanelMixin | 223 | — |
| `atri_x.gui.tabs.permission` | 「权限」选项卡：超级管理员名单 + 风险等级 / 工具轮次 / 成员建卡开关。 | PermissionTab | 227 | — |
| `atri_x.gui.tabs.platform` | 「平台」选项卡：QQ / Telegram / 本地 CLI 服务端 / 超级管理员。 | PlatformTab | 500 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.research` | 「研究」选项卡：查看科研助理的运行与产物（只读）。 | ResearchTab | 326 | — |
| `atri_x.gui.tabs.searchpanel` | 「模型 → 搜索服务」子页：``web_search`` 的通道列表、表单、顺序与密钥。 | SearchServicePanel | 429 | — |
| `atri_x.gui.tabs.timepanel` | 「时间」选项卡：统一时间服务（``[time]`` / ``[time.verification]`` / ``[[time.sources]]``）。 | TICK_MS, TimeTab | 500 | `test_gui_timepanel.py` |
| `atri_x.gui.tabs.toolperm` | 「工具权限」选项卡：按群、按会话模式管理工具授权（``state.toml`` 的 ``[group_policy]``）。 | DISCOVER_TOOL, MODE_ORDER, MODE_SAFETY_GROUPS, PARAMS_TAB_T… | 583 | `test_gui_tabs.py` |
| `atri_x.gui.timediagnostics` | 后台 ``/diagnostics`` 时间段的面板（只读，不发网络校验、不改任何配置）。 | DiagnosticsPanel, OBSERVATION_COLUMNS | 185 | — |
| `atri_x.gui.timesources` | ``[[time.sources]]`` 的表格 + 表单编辑器（只操作内存里的列表，不碰配置文件）。 | SOURCE_COLUMNS, SourceEditor | 309 | — |
| `atri_x.gui.timeview` | 「时间」页的数据层（无 Tk）：读 ``[time]``、算实时快照、跑时区探针、拉后台 ``/diagnostics``。 | COMMON_TIMEZONES, DIAGNOSTICS_PATH, HEALTH_ADDR_ENV, HEALTH… | 499 | `test_gui_timepanel.py` |
| `atri_x.gui.toolcatalog` | 桌面端「工具权限」页的工具目录：中文名、一句话功能解释、分组与风险等级。 | CATEGORY_ORDER, OTHER_CATEGORY, RISK_COLORS, RISK_LABELS, T… | 530 | `test_gui_tabs.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.toolmenu` | 「工具权限」页的纵向工具菜单控件：一行一个工具（激活按钮 + 状态 + 功能解释）。 | MenuResize, ToolRow, category_header | 242 | — |
| `atri_x.gui.toolmode` | 「工具权限」页的分模式工具编辑器（chat / work 各一个）。 | MODE_HINTS, MODE_LABELS, MODE_SHORT, ModeEditor, active_nam… | 321 | — |
| `atri_x.gui.toolparams` | 「工具参数」子选项卡的控件：每个工具一组可展开 / 收起的参数面板。 | SettingPanel, ToolParamsEditor | 530 | `test_gui_tabs.py` |
| `atri_x.gui.toolpolicy` | 「工具权限」页的策略读写层：群工具白名单（``state.toml`` 的 ``[group_policy]``）。 | DISCOVER_TOOL, FALLBACK_TOOLS, MODE_ALLOWLIST_KEYS, PRESET_… | 424 | `test_gui_tabs.py` |
| `atri_x.gui.toolpreview` | 「工具权限 → 工具进度预览」子选项卡（Python 侧新增，无 Go 对应）。 | PLATFORM_PREVIEW_SECTIONS, PREVIEW_TAB_TITLE, ToolPreviewPa… | 264 | — |
| `atri_x.gui.toolsettings` | 「工具参数」子选项卡的注册表与读写（无 Tk）。 | GROUPS, KINDS, SettingField, SettingGroup, TRILABELS, coerc… | 261 | `test_gui_tabs.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.toolsettings_base` | 工具参数的字段类型与「界面值 → TOML 值」转换。 | FILES, KINDS, SettingField, SettingGroup, TRILABELS, coerce… | 255 | `test_gui_tabs.py` |
| `atri_x.gui.toolsettings_general` | 工具参数定义：「文本 / 联网 / 文件 / 记忆 / 群聊」类工具。 | GENERAL_GROUPS | 696 | — |
| `atri_x.gui.toolsettings_media` | 工具参数定义：「图片 / 画师串 / 视觉」类工具。 | MEDIA_GROUPS | 367 | — |
| `atri_x.gui.tray` | Windows 托盘图标（纯 ``ctypes``，零第三方依赖）。 | MENU_SEPARATOR, TrayIcon, available, command_line | 523 | — |
| `atri_x.gui.widgets` | 桌面端小部件：密钥输入框、可滚动下拉框、可滚动表单区、后台任务回主线程的小工具。 | MASK_CHAR, ScrollFrame, SecretEntry, dropdown, run_async | 282 | `test_gui_tabs.py` |
| `atri_x.health` | 健康状态、readiness 检查与独立健康 HTTP 服务端。 | Checker, CheckerFunc, DEFAULT_LIVE_STALE_SECONDS, DEFAULT_R… | 57 | `test_app_ops_plugins.py`、`test_health.py` |
| `atri_x.health.checker` | 外部 readiness 检查。 | Checker, CheckerFunc, DirWritable | 102 | `test_health.py` |
| `atri_x.health.server` | 独立的健康 HTTP 服务端。 | DEFAULT_READ_HEADER_TIMEOUT_SECONDS, GO_ZERO_TIME, HTTPResp… | 624 | `test_app_ops_plugins.py`、`test_health.py` |
| `atri_x.health.state` | 健康状态：进程存活、readiness、平台连通性与模型健康。 | ModelStatus, Options, PlatformStatus, Snapshot, State, new_… | 440 | `test_app_ops_plugins.py`、`test_health.py` |
| `atri_x.historygate` | 群聊历史写入闸门：策略 + 仓储包装。 | ChatRepository, OutboundRepository, Policy, RangeUnsupporte… | 35 | `test_builtin_tools3.py`、`test_historygate.py` |
| `atri_x.historygate.gate` | 群聊历史写入闸门。 | ChatRepository, OutboundRepository, Policy, RangeUnsupporte… | 236 | — |
| `atri_x.hook` | Hook 与插件。 | — | 20 | `agent_commands_fakes.py`、`test_agent_chat.py`、`test_app_platforms.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_output.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_safety_integration.py` |
| `atri_x.hook.builtin` | 内置 Hook 插件：规则引擎、天使记忆、自我学习、出站历史。 | Options, RegisterAll, record_outbound_message, register_ang… | 30 | `test_hook_builtin_control.py` |
| `atri_x.hook.builtin.angel_memory` | 内置 Hook：按平台/会话召回长期记忆，追加为临时 system 上下文。 | register_angel_memory | 56 | — |
| `atri_x.hook.builtin.register` | 内置 Hook 插件的注册入口。 | Options, RegisterAll, record_outbound_message, register_mod… | 153 | — |
| `atri_x.hook.builtin.self_learning` | 内置 Hook：群语料观察与表达/黑话上下文注入。 | register_self_learning | 90 | — |
| `atri_x.hook.control` | Hook 控制面：给命令层用的列表 / 热重载 / 启停。 | ControlError, Loader, New, Runtime, Service | 185 | `test_hook_builtin_control.py` |
| `atri_x.hook.event` | Hook 事件点、事件 payload 与 Handler 基础类型。 | ActorContext, Control, ErrorPayload, Event, Handler, Handle… | 347 | `test_app_platforms.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py` |
| `atri_x.hook.manager` | 普通 Hook 的注册、排序、执行与原子快照替换。 | DefaultManager, HookError, HookLogger, Info, ManagerProtoco… | 733 | `agent_commands_fakes.py`、`test_agent_chat.py`、`test_hook_builtin_control.py`、`test_hook_manager.py`、`test_hook_rules.py` |
| `atri_x.hook.match` | Hook 条件匹配、字段取值与模板值。 | BlockPolicy, Condition, Match, MatchContext, MatchResult, R… | 560 | `test_hook_builtin_control.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py` |
| `atri_x.hook.media` | 宿主 Media 能力（Hook 进程内的 Go 插件用）。 | MediaAPI | 57 | — |
| `atri_x.hook.output` | Hook 输出的线格式。 | — | 21 | `test_hook_output.py`、`test_hook_protocol.py`、`test_hook_rules.py` |
| `atri_x.hook.output.jsonstrict` | 严格 JSON 解码：拒绝未知字段、拒绝尾随值。 | JSONDecodeError, decode_json, loads_strict, optional_bool, … | 217 | `test_hook_output.py` |
| `atri_x.hook.output.message` | 进程 Hook 替换消息内容的线格式（``message.segments``）。 | MessageSegment, build_message_image, build_message_segment,… | 229 | — |
| `atri_x.hook.output.output` | Hook 输出的线格式：``outputs`` 段的解码与构造。 | BuildOptions, Group, JSONDecodeError, MAX_BASE64_BYTES, Max… | 428 | — |
| `atri_x.hook.process` | 进程 Hook 继承的环境。 | ProcessEnvironment, new_process_environment | 29 | — |
| `atri_x.hook.protocol` | ``hook.v2`` 进程间协议。 | — | 19 | `test_hook_protocol.py`、`test_hook_runtime.py` |
| `atri_x.hook.protocol.eventjson` | ``hook.Event`` 与其负载的 JSON 投影。 | RawJSON, ZERO_TIME_JSON, dumps, event_from_json, event_to_j… | 659 | — |
| `atri_x.hook.protocol.frames` | ``hook.v2`` JSON Lines 协议帧。 | EventHandleParams, EventResultBase, Frame, LLMResult, Messa… | 330 | `test_hook_runtime.py` |
| `atri_x.hook.protocol.stream` | ``hook.v2`` 的按行读取（带帧上限）。 | LineReader, MAX_PROTOCOL_FRAME_BYTES, ProtocolFrameError, a… | 116 | — |
| `atri_x.hook.rules` | ``hooks.toml`` 规则引擎。 | Action, ActionResult, CONFIG_FILE, Config, DEFAULT_PRIORITY… | 88 | `test_hook_rules.py` |
| `atri_x.hook.rules.action` | 规则动作的执行：改文本、发消息、跑工具、调插件。 | ActionResult, State, allow_field, apply_text_action, byte_s… | 439 | — |
| `atri_x.hook.rules.config` | ``hooks.toml`` 与插件 ``hook.toml`` 的装载。 | decode_config, decode_plugin_config, load_config, normalize… | 686 | `test_hook_rules.py` |
| `atri_x.hook.rules.detail` | ``/hooks`` 列表里展示的规则详情文本。 | format_rule_detail, rule_description, strconv_quote, target… | 124 | `test_hook_rules.py` |
| `atri_x.hook.rules.exec` | ``exec`` 动作：跑一次 hook.v2 一次性插件。 | byte_size, plugin_stdin_write_error, read_protocol_line, ru… | 746 | — |
| `atri_x.hook.rules.exec_stderr` | ``exec`` 动作的进程辅助：stderr 尾巴、错误包装、cwd 解析。 | ExecError, ExecStderrLogger, ExecStderrTail, exec_context_e… | 261 | — |
| `atri_x.hook.rules.rule` | 规则的校验、角色条件展开与执行。 | RuleError, contains_string, matches_roles, matches_unified_… | 256 | `test_hook_rules.py` |
| `atri_x.hook.rules.rules` | 规则模块：装载 ``hooks.toml`` 并把每条规则注册成 Hook。 | CONFIG_FILE, DEFAULT_PRIORITY, Module, NewModule, new_module | 190 | `test_hook_rules.py` |
| `atri_x.hook.rules.toml_error` | ``hooks.toml`` 的读取与详细错误信息。 | ConfigParseError, StrictDecodeError, conflicting_toml_key, … | 514 | `test_hook_rules.py` |
| `atri_x.hook.rules.types` | ``hooks.toml`` 的配置类型。 | Action, CONFIG_FILE, Config, DEFAULT_PRIORITY, Options, Pla… | 239 | `test_hook_rules.py` |
| `atri_x.hook.runtime` | 有状态 Hook 运行时（hook.v2 进程）。 | Config, DISPATCHED_METADATA_KEY, Info, Invocation, Lease, M… | 113 | `test_hook_builtin_control.py`、`test_hook_runtime.py` |
| `atri_x.hook.runtime.config` | 有状态 Hook 进程的配置、状态与运行期选项。 | Config, Info, Mode, Options, RestartConfig, RuntimeConfigEr… | 253 | — |
| `atri_x.hook.runtime.manager` | 有状态 Hook 运行时的协调者：应用配置、启停 worker、路由事件。 | Manager, ManagerError, new_manager | 528 | — |
| `atri_x.hook.runtime.media` | ``media.*`` RPC：把宿主 MediaAPI 暴露给进程 Hook。 | MEDIA_INLINE_BYTES, MEDIA_LEASE_TTL, MediaBridge, MediaBrid… | 356 | — |
| `atri_x.hook.runtime.protocol` | 有状态 Hook 进程的协议负载（``system.init`` / ``event.handle``）。 | EVENT_RESULT_BASE_KEYS, EventHandle, EventResult, PROTOCOL_… | 209 | — |
| `atri_x.hook.runtime.routes` | Hook 路由：租约、等待态、单飞调用与工具上下文令牌。 | DISPATCHED_METADATA_KEY, Invocation, Lease, MAX_TOOL_CALLS_… | 355 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.shared` | Hook 共享的有界 JSON 协调存储。 | DEFAULT_SHARED_TTL, MAX_SHARED_KEY_BYTES, MAX_SHARED_STATE_… | 382 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.tool_bridge` | 进程 Hook 发来的请求：``tool.call`` / ``hooks.reload`` / ``media.*`` / ``shared.*``。 | ToolBridgeError, call_tool, plugin_request, schemas | 307 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.transport` | worker 的协议传输层：读写帧、请求/响应配对、进程强杀。 | Pending, deliver_response, handle_plugin_event, kill, notif… | 279 | — |
| `atri_x.hook.runtime.worker` | 一个有状态 Hook 进程（hook.v2 worker）。 | Worker, WorkerError, WorkerProcessError, WorkerTimeoutError | 714 | — |
| `atri_x.imagegen` | OpenAI 兼容生图客户端与提示词组装（自包含子系统）。 | Client, ComposeBlocks, ComposeResult, Config, DEFAULT_MAX_P… | 186 | `test_builtin_imagegen.py`、`test_builtin_tools4.py`、`test_imagegen.py`、`test_safety_integration.py` |
| `atri_x.imagegen._extract_library` | 从上游机械抽取提示词库 ``prompts/library.json``。 | CHECK_DRIFT, CHECK_OK, CHECK_UNUSABLE, LibraryCheck, check,… | 217 | `test_imagegen.py` |
| `atri_x.imagegen.client` | OpenAI 兼容生图客户端。 | Client, Config, DEFAULT_MAX_PROMPT, DEFAULT_MODEL, DEFAULT_… | 609 | `test_imagegen.py` |
| `atri_x.imagegen.context` | 每轮生图的 profile 传递（``@image:<name>``）。 | profile_from, with_profile | 53 | — |
| `atri_x.imagegen.library` | 内嵌提示词库：数据类型与加载。 | NegativeGroup, PromptEntry, PromptLibrary, PromptTemplate, … | 288 | — |
| `atri_x.imagegen.optimize` | 提示词库规则优化器（不调用模型）。 | EDIT_GUARD, OptimizeOptions, OptimizeResult, clean_ratio, c… | 630 | `test_imagegen.py` |
| `atri_x.imagegen.prompt` | 提示词片段组装：按优先级整体丢弃，绝不切开权重表达式。 | ComposeResult, PromptBlock, PromptPriorityCore, PromptPrior… | 112 | — |
| `atri_x.imagegen.search` | 内嵌提示词库检索。 | LibraryMatch, LibraryScopes, library_hints, search | 156 | `test_imagegen.py` |
| `atri_x.launcher` | ``internal/launcher`` 的 Python 移植：进程启动器的命令行层。 | — | 61 | `test_launcher_cli.py` |
| `atri_x.launcher.cli` | 进程启动器的命令行解析、用法文本与 shell 补全脚本。 | BASH_COMPLETION, COMMAND_COMPLETION, COMMAND_CONFIG_CHECK, … | 517 | `test_launcher_cli.py` |
| `atri_x.llm` | LLM 抽象层。 | — | 11 | `_vision_fakes.py`、`agent_fakes.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_chat_anomaly.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_segments.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_app_llm_adapters.py`、`test_app_platforms.py`、`test_background.py`、`test_builtin_tools3.py`、`test_completion.py`、`test_contextmgr.py`、`test_contextmgr_window_metadata.py`、`test_cron.py`、`test_cron_delivery.py`、`test_dsh_route.py`、`test_groupanalysis.py`、`test_gui_protocol.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_llm_breaker.py`、`test_llm_errors.py`、`test_llm_headers.py`、`test_llm_protocol.py`、`test_llm_tooltext.py`、`test_media_resolver.py`、`test_memory_notes.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_cli.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_time_context.py`、`test_tool_core.py`、`test_toolrun.py`、`test_toolrun_atriwisp_args.py`、`test_turn_manager.py`、`test_usage_attribution.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.llm.breaker` | Provider 级熔断器。 | Breaker, Config, ErrOpen, OpenError, ProviderBreakers, STAT… | 270 | `test_app_llm_adapters.py`、`test_llm_breaker.py` |
| `atri_x.llm.errors` | 结构化上游 API 失败与确定性失败分类。 | APIError, ErrorCategory, ErrorCategoryAuth, ErrorCategoryIn… | 348 | `test_agent_chat.py`、`test_llm_errors.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision.py`、`test_vision_privacy.py` |
| `atri_x.llm.llm` | LLM 抽象层类型。 | ChatRequest, ChatResponse, LLM, LLMMessage, MessageSegment,… | 559 | `_vision_fakes.py`、`test_agent_budget.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_title.py`、`test_background.py`、`test_builtin_tools3.py`、`test_completion.py`、`test_contextmgr.py`、`test_contextmgr_window_metadata.py`、`test_cron.py`、`test_cron_delivery.py`、`test_groupanalysis.py`、`test_gui_protocol.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_media_resolver.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_cli.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_tool_core.py`、`test_toolrun.py`、`test_toolrun_atriwisp_args.py`、`test_turn_manager.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.llm.openai` | OpenAI-compatible 适配器包。 | Adapter, AwaitableStream, CHAT_RESERVED_REQUEST_FIELDS, DAT… | 80 | `test_app_llm_adapters.py`、`test_llm_headers.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.openai.errors` | 上游失败响应 → ``APIError`` 的错误分类。 | APIError, ErrorCategory, HTML_RESPONSE_HINT, classify, look… | 133 | — |
| `atri_x.llm.openai.openai` | OpenAI-compatible 适配器（Chat Completions）。 | Adapter, CHAT_RESERVED_REQUEST_FIELDS, DEFAULT_FIRST_CHUNK_… | 636 | `test_llm_headers.py`、`test_openai_adapter.py` |
| `atri_x.llm.openai.responses` | OpenAI Responses 协议适配器。 | OpenAIResponsesClient, RESPONSES_RESERVED_REQUEST_FIELDS, R… | 511 | `test_llm_headers.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.openai.stream` | SSE 行扫描与首包/空闲超时。 | AwaitableStream, StreamLineReader, duration_text, first_chu… | 212 | `test_openai_adapter.py` |
| `atri_x.llm.openai.wire` | OpenAI-compatible 线上格式：请求信封构造、响应字段解析与日志摘要。 | CHAT_RESERVED_REQUEST_FIELDS, DATA_DONE, DATA_PREFIX, DEFAU… | 606 | `test_llm_headers.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.origin` | 「这段已有历史能不能交给目标模型」的唯一入口。 | Origin, OriginError, can_switch | 81 | `test_llm_protocol.py` |
| `atri_x.llm.protocol` | 协议能力矩阵的唯一入口。 | Protocol, ProtocolCapabilities, ProtocolCapabilityReporter,… | 91 | `test_agent_context_runtime.py`、`test_llm_protocol.py`、`test_openai_adapter.py` |
| `atri_x.llm.segment` | 消息段 helper。 | — | 41 | — |
| `atri_x.llm.tooltext` | 把「以正文形式泄漏的工具调用」还原成结构化调用。 | TEXT_TOOL_CALL_NOTICE, looks_like_text_tool_call, parse_tex… | 329 | `test_agent_chat_anomaly.py`、`test_llm_tooltext.py` |
| `atri_x.llm.usage_sink` | 旁路 LLM 用量的统一出口。**Python 版新增**。 | UsageSink, current_usage_sink, record_side_usage, set_usage… | 62 | `test_usage_attribution.py` |
| `atri_x.logging` | 日志包。对应 Go：``internal/logging``。 | DAMAGED_SAMPLE_LIMIT, DEFAULT_BLOCK_SIZE, DEFAULT_LOG_DAYS,… | 152 | `test_agent_commands_services.py`、`test_agent_directives.py`、`test_app_runtime_agent.py`、`test_delivery.py`、`test_log_reader.py`、`test_logging.py`、`test_maintenance_report.py`、`test_usage_attribution.py` |
| `atri_x.logging.contract` | 日志来源/结果/运行来源最小契约。对应 Go：``internal/logging/contract.go``。 | FieldRunSource, LOG_MODULES, LOG_RESULTS, ModuleAgent, Modu… | 137 | `test_logging.py` |
| `atri_x.logging.logging` | 日志中心：文本行格式、按日文件轮转、三个日志通道。对应 Go：``internal/logging/logging.go``。 | DEFAULT_RETENTION_DAYS, DailyFileWriter, Logger, MANAGED_LO… | 596 | `test_app_runtime_agent.py`、`test_delivery.py`、`test_logging.py` |
| `atri_x.logging.reader` | 日志查询：反向分块读取 + 文本键值过滤。对应 Go：``internal/logging/reader.go``。 | CONTAINS_FIELDS, DAMAGED_SAMPLE_LIMIT, DEFAULT_BLOCK_SIZE, … | 509 | `test_app_runtime_agent.py`、`test_log_reader.py`、`test_logging.py` |
| `atri_x.logging.redact_fallback` | 脱敏模块不可用时的正则兜底（``atri_x/logging/logging.py`` 的降级实现）。 | FALLBACK_RULES, announce_once, fallback_redact, status | 106 | — |
| `atri_x.maintenance` | 维护任务：定时清理与定时报告/总结。 | DAILY_SUMMARY_DIR, DEFAULT_RETENTION_DAYS, DSH_CACHE_DIRS, … | 110 | `test_daily_summary.py`、`test_maintenance.py`、`test_maintenance_dsh.py`、`test_maintenance_report.py` |
| `atri_x.maintenance.dsh` | 内置 DSH 内核状态目录（``DSH_HOME``）的保留清理（Python 版新增件）。 | DSH_CACHE_DIRS, DSH_SESSIONS_DIR, DshCleanupResult, RunDshC… | 256 | `test_maintenance_dsh.py` |
| `atri_x.maintenance.maintenance` | 维护任务：日志/会话/沙盒/聊天历史/DSH 内核/隐私数据清理，以及 cron 装配。 | DEFAULT_RETENTION_DAYS, RetentionCleanup, Service, cleanup_… | 626 | — |
| `atri_x.maintenance.report` | 定时资源报告：从审计日志聚合生图/Token/费用，附磁盘与内存快照后发出。 | ImageReportStats, ModelUsageReport, RunDailyReportMixin, co… | 402 | `test_maintenance_report.py` |
| `atri_x.maintenance.summary` | 每日会话总结：把一段时间内的会话信息汇总成一段可读文本并落盘（Python 版新增件）。 | DAILY_SUMMARY_DIR, DailySummary, RunDailySummaryMixin, coll… | 338 | `test_daily_summary.py` |
| `atri_x.maintenance.summary_cli` | ``python -m atri_x.maintenance.summary_cli``：查看/立即生成每日会话总结。 | build_now, main | 165 | — |
| `atri_x.media` | 媒体中心（Media Center）。 | ArgumentMediaIDs, AssociateHistory, Backend, BackendName, C… | 364 | `test_app_tool_runtime_wiring.py`、`test_atriena.py`、`test_builtin_tools3.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_imagecodec.py`、`test_imagecodec_jpeg_precision.py`、`test_media_image.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_media_platform.py`、`test_media_resolver.py`、`test_media_worker.py`、`test_ops_health.py`、`test_tool_media_runtime.py`、`test_toolrun.py`、`test_vision_privacy.py` |
| `atri_x.media.config` | 媒体中心配置。 | Config, FileDeliveryConfig, MediaConfig, PlatformFilesConfi… | 198 | — |
| `atri_x.media.generation` | 导入期的生成参数采集接缝。 | GenerationCapture, GenerationSink, RecordingGenerationSink,… | 134 | — |
| `atri_x.media.history` | 跨库历史 owner 对账。 | PAGE_SIZE, reconcile_history | 48 | — |
| `atri_x.media.image` | 导入期图片压缩与视觉预处理。 | MEDIA_IMAGE_MAX_DECODE_PIXELS, VISION_IMAGE_DEFAULT_BYTE_BU… | 352 | — |
| `atri_x.media.imagecodec` | 图像编解码包：纯 Python 实现 PNG / baseline JPEG，Pillow 为可选加速后端。 | DEFAULT_MAX_DECODE_BYTES, DEFAULT_MAX_PIXELS, Image, ImageC… | 59 | `test_gui_members.py`、`test_gui_tabs.py`、`test_imagecodec.py`、`test_imagecodec_jpeg_precision.py`、`test_media_image.py`、`test_media_manager.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.backend_probe` | 图像后端探测：当前生效的是 Pillow 加速后端还是纯标准库后端。 | image_backend, image_backend_info, main, pillow_module, pil… | 129 | `test_imagecodec_jpeg_precision.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.image` | 图像编解码公共契约：Image 容器、格式嗅探、后端分发与统一上限。 | DEFAULT_MAX_DECODE_BYTES, DEFAULT_MAX_PIXELS, Image, ImageC… | 734 | `test_imagecodec_jpeg_precision.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.jpeg_decode` | 纯 Python baseline JPEG 解码（对应 Go 的 ``image/jpeg`` 解码侧）。 | decode_jpeg | 768 | `test_imagecodec.py`、`test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.jpeg_encode` | 纯 Python baseline JPEG 编码（对应 Go 的 ``image/jpeg.Encode``）。 | encode_jpeg | 486 | `test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.ops` | 图像后处理：白底合成、等比缩放、阈值压缩。 | compress, resize_to_fit, to_rgb | 208 | — |
| `atri_x.media.imagecodec.png` | 纯 Python PNG 编解码（对应 Go 的 ``image/png``）。 | PNG_SIGNATURE, decode_png, encode_png | 433 | `test_imagecodec.py` |
| `atri_x.media.imagecodec.resample` | 纯 Python 重采样：最近邻 / 双线性 / Lanczos3（默认）。 | resize_image | 184 | — |
| `atri_x.media.lifecycle` | 可重试的后端清理与孤儿宽限期。 | CLEANUP_CONCURRENCY, ORPHAN_GRACE, cleanup, delete_object | 136 | — |
| `atri_x.media.local` | 本地内容寻址媒体后端。 | LocalBackend | 96 | — |
| `atri_x.media.locks` | 按内容 ID 的对象锁。 | ObjectLocks, object_locks | 117 | — |
| `atri_x.media.manager` | 媒体中心 Manager：导入、读取、导出、签名与后端选择。 | Manager, backend_name, new_configured, new_manager, read_al… | 728 | `test_toolrun.py` |
| `atri_x.media.mimetype` | 媒体 MIME 判定：内容嗅探与扩展名映射。 | detect_content_type, mime_type_by_extension | 153 | — |
| `atri_x.media.output` | 元数据读取与发送用临时导出。 | metadata, resolve_for_output | 90 | — |
| `atri_x.media.platform` | 共享平台导入、历史媒体位置关联与本地 ID 查询。 | associate_history, get_history_media, history_ids, history_… | 161 | `test_media_platform.py` |
| `atri_x.media.reference` | 引用持有与工具参数引用。 | ReferencedReader, argument_media_ids, hold, retain_session_… | 175 | — |
| `atri_x.media.remote` | 按需初始化的远端（S3）后端包装。 | LazyBackend | 49 | — |
| `atri_x.media.resolver` | LLM 媒体解析与传输选择。 | materialize, materialize_with_limits, resolve_for_llm, unav… | 275 | — |
| `atri_x.media.s3` | S3 兼容对象存储后端。 | S3Backend, new_s3_backend, object_key, resolve_credentials,… | 163 | — |
| `atri_x.media.sanitize` | 名称与来源规范化。 | sanitize_input, sanitize_media_metadata, sanitize_media_nam… | 155 | — |
| `atri_x.media.types` | 媒体中心的核心类型、常量与 ctx 适配。 | Backend, IDPrefix, ID_HASH_LENGTH, ID_PREFIX, ImportLimits,… | 202 | `test_media_platform.py`、`test_toolrun.py` |
| `atri_x.media.worker` | 媒体图片工作进程（隔离压缩的执行器）。 | IMAGE_WORKER_ARG, IMAGE_WORKER_QUEUE_LIMIT, IMAGE_WORKER_QU… | 565 | `test_media_image.py`、`test_media_worker.py` |
| `atri_x.memberprofile` | 群友档案：数据模型、JSON 存储与提示词渲染的共享层。 | GUI_SUBDIR, MAX_PROFILE_RUNES, MAX_TAGS, MEMBERS_FILE, MEMB… | 111 | `test_member_persona.py`、`test_member_profile.py` |
| `atri_x.memberprofile.model` | 群友档案的数据模型与序列化（GUI 与 Agent 的唯一出处）。 | MAX_PROFILE_RUNES, MAX_TAGS, MEMBERS_FILE, MEMBERS_VERSION,… | 161 | — |
| `atri_x.memberprofile.persona` | 成员「专属人设」：按 ``平台 + 用户 id`` 把人设卡绑到某个成员身上。 | MEMBER_PERSONAS_FILE, MEMBER_PERSONAS_VERSION, MemberPerson… | 395 | `test_member_persona.py` |
| `atri_x.memberprofile.prompt` | 把群友档案渲染成系统提示词片段（GUI 预览与 Agent 注入共用）。 | MEMBER_PROFILE_PROMPT_HEADER, MEMBER_PROFILE_TRUST_NOTE, es… | 71 | — |
| `atri_x.memberprofile.store` | 群友档案的 JSON 存储：按文件指纹缓存、读写互斥、损坏文件不静默覆盖。 | MemberProfileStore, load_profiles, members_path, remove_pro… | 265 | — |
| `atri_x.memory` | 记忆子系统。对应 Go 包：``internal/memory``（当前只有 ``resident`` 子包）。 | resident | 10 | `test_app_runtime_resident.py`、`test_builtin_tools2.py`、`test_builtin_tools4.py`、`test_memory_notes.py`、`test_resident_memory.py` |
| `atri_x.memory.notes` | 常驻记忆条目的会话可见性。**Python 版新增**（Go 侧没有对应文件）。 | MemoryNote, NoteSelection, PEOPLE_VIEW_ENABLED, PRIVATE_SCO… | 222 | `test_builtin_tools2.py`、`test_memory_notes.py` |
| `atri_x.memory.resident` | 常驻记忆（``memories.toml``）读写。对应 Go 包：``internal/memory/resident``（``store.go`` / ``atomic.go… | — | 57 | `test_app_runtime_resident.py`、`test_builtin_tools2.py`、`test_builtin_tools4.py`、`test_memory_notes.py`、`test_resident_memory.py` |
| `atri_x.memory.resident.atomic` | 常驻记忆文件的原子落盘。对应 Go：``internal/memory/resident/atomic.go``（``writeFileAtomic`` / | write_file_atomic | 40 | `test_resident_memory.py` |
| `atri_x.memory.resident.entries` | 常驻记忆条目：结果类型、条目规范化、单位计数与注入渲染。对应 Go：``internal/memory/resident/store.go``（**拆分说明**：Go 的 ``s… | Memory, NORMAL_INSTRUCTION_PATTERNS, RESIDENT_MEMORY_PROMPT… | 263 | `test_builtin_tools2.py` |
| `atri_x.memory.resident.store` | 常驻记忆存储：``memories.toml`` 的读写、上限与写入频率策略。对应 Go：``internal/memory/resident/store.go``（**拆分说明… | DEFAULT_CORE_MAX_UNITS, DEFAULT_NORMAL_MAX_UNITS, Limits, M… | 809 | `test_app_runtime_resident.py` |
| `atri_x.net` | 网络层。对应 Go 的标准库 ``net/http`` 与 ``github.com/coder/websocket``。 | — | 14 | `test_atrinis_http.py`、`test_config_state.py`、`test_cron_dst.py`、`test_cronspec.py`、`test_gui_client.py`、`test_gui_service.py`、`test_httpclient.py`、`test_media_resolver.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_cli_remote.py`、`test_platform_qqofficial.py`、`test_qq_onebot_smoke.py`、`test_s3.py`、`test_tomlw.py`、`test_web_extract_engines.py`、`test_web_search_engines.py`、`test_websocket.py` |
| `atri_x.net.cronspec` | cron 表达式解析与下次触发时间：对应 Go 的 ``robfig/cron/v3``。 | ConstantDelaySchedule, CronParseError, Schedule, SpecSchedu… | 615 | `test_cron_dst.py`、`test_cronspec.py` |
| `atri_x.net.httpclient` | HTTP 客户端：对应 Go 的 ``net/http`` 用法。 | DEFAULT_MAX_REDIRECTS, DEFAULT_TIMEOUT, HTTPClient, HTTPErr… | 999 | `test_atrinis_http.py`、`test_httpclient.py`、`test_media_resolver.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_qqofficial.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.net.s3` | 最小 S3 客户端（SigV4 签名 + REST）：对应 Go 的 ``aws-sdk-go-v2/service/s3``。 | EMPTY_SHA256, S3Client, S3Config, S3Error, S3NoSuchKeyError… | 594 | `test_s3.py` |
| `atri_x.net.tomlw` | TOML 写入：对应 Go 的 ``pelletier/go-toml/v2``（写入侧）。 | TomlWriteError, dump, dumps, dumps_table | 321 | `test_config_state.py`、`test_tomlw.py` |
| `atri_x.net.websocket` | WebSocket（RFC 6455）客户端与服务端：对应 Go 的 ``github.com/coder/websocket``。 | CLOSE_GOING_AWAY, CLOSE_INTERNAL_ERROR, CLOSE_NORMAL, CLOSE… | 1034 | `test_gui_client.py`、`test_gui_service.py`、`test_platform_cli_remote.py`、`test_platform_qqofficial.py`、`test_qq_onebot_smoke.py`、`test_websocket.py` |
| `atri_x.ops` | ops 子包：并发限制、磁盘守卫与限流。 | — | 18 | `test_agent_bootstrap.py`、`test_agent_directives.py`、`test_builtin_imagegen.py`、`test_media_manager.py`、`test_ops.py`、`test_ops_executors.py` |
| `atri_x.ops.concurrency` | 有界并发限制器（带可选等待队列）。 | Config, ErrFull, ErrTimeout, Limiter, new | 147 | `test_builtin_imagegen.py`、`test_ops.py` |
| `atri_x.ops.diskguard` | 分级磁盘用量守卫（用于非必需媒体写入）。 | Config, Guard, Level, LevelCritical, LevelOK, LevelWarn, ne… | 176 | `test_media_manager.py`、`test_ops.py` |
| `atri_x.ops.executors` | CPU / 阻塞 IO 的专用线程池。 | cpu_pool, io_pool, named_pool, run_cpu, run_io, shutdown_po… | 133 | `test_ops_executors.py` |
| `atri_x.ops.ratelimit` | 按键的令牌桶限流器。 | Decision, Limit, Limiter, new | 123 | `test_agent_bootstrap.py`、`test_agent_directives.py`、`test_ops.py` |
| `atri_x.platform` | 平台适配层：适配器契约、连接信号、配置与媒体编解码。 | — | 75 | `test_agent_chat.py`、`test_agent_commands_integration.py`、`test_agent_core.py`、`test_agent_group_policy.py`、`test_agent_inbound_media.py`、`test_agent_participation.py`、`test_agent_wakeup.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_long_memory_scope.py`、`test_media_manager.py`、`test_media_platform.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_platform_builtin.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py`、`test_platform_core.py`、`test_platform_headless.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py`、`test_refcontext.py` |
| `atri_x.platform.backoff` | 平台重连用的指数退避。 | Backoff, new_backoff | 62 | — |
| `atri_x.platform.builtin` | ``internal/platform/builtin`` 的 Python 移植：把配置变成平台适配器集合。 | Bundle, MODE_CLI_ONLY, MODE_FULL, MODE_SERVICE, Mode, ModeC… | 429 | `test_platform_builtin.py` |
| `atri_x.platform.cli` | CLI 平台适配器（对应 Go 包 ``internal/platform/cli``）。 | — | 90 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py` |
| `atri_x.platform.cli.adapter` | CLI 平台适配器主体：stdin 读取循环、TUI 交互与出站发送。 | Adapter, CliMessageStream, DEFAULT_ASSISTANT_NAME, DEFAULT_… | 386 | `test_platform_cli.py` |
| `atri_x.platform.cli.clipboard` | TUI 剪贴板读写：系统剪贴板 + OSC52 转义序列。 | AutoClipboardWriter, ClipboardError, ClipboardWriter, OSC52… | 304 | — |
| `atri_x.platform.cli.config` | CLI 平台适配器的配置解析与装配入口。 | ClientConfig, Config, DEFAULT_CLIENT, DEFAULT_REMOTE_ADDR, … | 224 | `test_cli_sessions.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.local_file` | TUI 的 ``#文件引用`` 补全与展开。 | DEFAULT_LOCAL_FILE_MAX_SIZE, DEFAULT_LOCAL_FILE_MAX_TOTAL_S… | 432 | `test_platform_cli.py`、`test_platform_cli_tui.py` |
| `atri_x.platform.cli.protocol` | 远程 CLI 的线上消息类型与 JSON 编码。 | HistoryEntry, LEVEL_DEBUG, LEVEL_ERROR, LEVEL_INFO, LEVEL_W… | 551 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_client` | 远程 CLI 客户端：把本地终端接到远端（或本机）的远程 CLI 服务端。 | COMPLETE_TIMEOUT, OUTPUT_QUEUE_SIZE, PLATFORM_NAME, RemoteC… | 335 | `test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_server` | 远程 CLI 服务端：让远程终端经 WebSocket 接入本机的 CLI 适配器。 | CompletionProvider, HELLO_READ_TIMEOUT, MAX_CONCURRENT_INPU… | 732 | `test_cli_sessions.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.session_browse` | 远端 CLI 的会话浏览扩展（Python 版新增）。 | SessionBrowseMixin | 101 | — |
| `atri_x.platform.cli.tui` | 本地 CLI 的终端界面（Tarui）——对应 Go 包 ``internal/platform/cli`` 的 TUI 部分。 | — | 92 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.content` | TUI 的聊天区内容与通知面板维护。 | ContentMixin, EMPTY_CONTENT_HINT, EMPTY_NOTICES_HINT, MAX_N… | 284 | — |
| `atri_x.platform.cli.tui.copy` | TUI 的 copy mode：vi 风格的区域选择、词移动、搜索与复制。 | CopyCursor, CopyModeMixin, CopyModeState, CopyRange, MODE_C… | 806 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.input` | 多行输入框：``bubbles/textarea`` 的最小等价物。 | DEFAULT_CHAR_LIMIT, LineInfo, TextArea | 228 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.keys` | TUI 的按键与鼠标事件：``bubbletea`` 的 ``KeyMsg`` / ``MouseMsg`` 等价物。 | KEY_BACKSPACE, KEY_BACKTAB, KEY_CTRL_C, KEY_CTRL_D, KEY_CTR… | 172 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.messages` | TUI 的消息类型与小状态对象。 | CompletionState, PASTE_BURST_CHAR_INTERVAL, PASTE_BURST_IDL… | 188 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.model` | TUI 主模型：状态、Update 主流程、输入提交、历史与补全。 | ACTION_COMPLETE, ACTION_DISPATCH, ACTION_QUIT, ACTION_READ_… | 623 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.mouse` | TUI 的鼠标处理：分区判定、分隔线拖拽、滚轮与鼠标定位光标。 | MIN_CHAT_PANEL_WIDTH, MIN_NOTICE_PANEL_WIDTH, MouseMixin, N… | 168 | — |
| `atri_x.platform.cli.tui.program` | TUI 主循环与键盘事件源：``run_tui`` 的落地。 | CLEAR_SCREEN, CTX_POLL_INTERVAL, CallbackKeySource, EOF_MAR… | 603 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.style` | TUI 的 ANSI 样式与布局助手：``lipgloss`` 的最小等价物。 | RESET, Style, fit_width, join_horizontal, place_vertical, s… | 202 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.theme` | TUI 主题：Go 里 ``lipgloss.NewStyle()...`` 常量块的等价物。 | — | 82 | — |
| `atri_x.platform.cli.tui.view` | TUI 的整帧渲染与布局。 | MAX_COMPLETION_POPUP_ITEMS, MAX_TUI_INPUT_HEIGHT, MIN_NOTIC… | 238 | — |
| `atri_x.platform.cli.tui.viewport` | 行缓冲视口：``bubbles/viewport`` 的最小等价物。 | DEFAULT_MOUSE_WHEEL_DELTA, Viewport | 78 | `test_platform_cli_tui.py` |
| `atri_x.platform.config` | 平台配置解码与命令前缀/唤醒词处理。 | command_name, decode_config, has_command_prefix, primary_co… | 93 | — |
| `atri_x.platform.headless` | ``internal/platform/headless`` 的 Python 移植。 | Adapter, new, raise_context_error | 82 | `test_platform_headless.py` |
| `atri_x.platform.media` | Chat History 原始有序 segments 的编解码与敏感来源清洗。 | marshal_chat_segments, unmarshal_chat_segments | 103 | `test_media_manager.py` |
| `atri_x.platform.platform` | 平台抽象：适配器、入站/出站类型、可选能力接口与"平台已连接"信号。 | ConnectNotifier, ConnectedEvent, ConversationKind, EventKin… | 454 | `test_agent_inbound_media.py`、`test_media_manager.py`、`test_media_platform.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py`、`test_refcontext.py` |
| `atri_x.platform.qq_onebot` | QQ OneBot v11 平台适配器（对应 Go 包 ``internal/platform/qq-onebot``）。 | — | 194 | `test_platform_qq_onebot.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.adapter` | qq-onebot 适配器主体：正向 WebSocket 读循环与出站发送。 | Adapter, new_adapter | 719 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.attachment` | 入站媒体解析（``get_image`` / ``get_file`` / 群文件直链兜底）。 | resolve_chat_file, resolve_media | 105 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.backend` | qqonebot 协议端（后端）差异描述。 | BACKEND_LABELS, BackendProfile, LEGACY_DEFAULT_BACKEND, LEG… | 213 | `test_qq_onebot_backend.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.capabilities` | OneBot 的可选平台能力：群历史、群资料、群成员、头像 URL。 | fetch_group_history, get_group_info, get_group_member_list,… | 195 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.config` | qq-onebot 适配器配置与装配入口。 | Config, apply_defaults, new_from_platform_config, resolve_a… | 258 | `test_platform_qq_onebot.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.forward` | 合并转发消息的展开渲染，以及 ``message.go`` 里的段/文本自由函数。 | ForwardParseState, at_text, clean_text, clip_forward_text, … | 492 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.inbound` | 入站预处理的去重、限流与事件分发。 | EventDispatcher, InboundDedupEntry, InboundDeduper, Inbound… | 397 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.message` | 入站消息归一化：段 → 文本 / @提及 / 媒体段 / 待展开的转发 id。 | display_name, file_segment, image_segment, is_direct_image_… | 263 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.output` | 出站输出 → OneBot 消息段，以及文本分页 / 合并转发节点 / 回执。 | local_path_file_uri, one_bot_media_receipt, one_bot_source_… | 192 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.preprocess` | 入站的网络型预处理：@ 解析、引用上下文、合并转发展开与 Chat History 落库。 | append_non_text_segments, apply_reference, attach_media_sco… | 659 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.slowfetch` | 引用 / 转发的慢取：预算等待、单飞、成功 / 失败分级缓存。**Python 版新增** | DEFAULT_BUDGET_SECONDS, DEFAULT_FAILURE_TTL_SECONDS, DEFAUL… | 257 | `test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.smoke` | ``python -m atri_x.platform.qq_onebot.smoke``：OneBot 协议端连通性冒烟。 | format_event, main, parse_args, resolve_access_token, run, … | 356 | `test_qq_onebot_smoke.py` |
| `atri_x.platform.qq_onebot.target` | 发送落点、ctx 传递与 scope 计算（Go ``adapter.go`` 里的 ``target`` 系列）。 | Target, event_scope_id, is_group_context, one_bot_target_sc… | 118 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.transport` | OneBot v11 正向 WebSocket 传输：连接、事件读取、动作调用。 | GetFileData, GetImageData, GetMessageData, LoginInfo, Reque… | 688 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.types` | OneBot v11 线上事件类型与转发展开上限。 | Event, ForwardLimits, NormalizedMessage, Segment, Sender, d… | 267 | `test_platform_qq_onebot.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qqofficial` | QQ 官方机器人平台适配器（对应 Go 包 ``internal/platform/qqofficial``）。 | — | 201 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.adapter` | qqofficial 适配器主体：网关生命周期与出站发送。 | Adapter, new_adapter, raise_context_error, sleep_context | 328 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.attachment` | 入站附件 → 平台中立消息段，以及附件文件名的清洗。 | attachment_declared_mime_type, content_disposition_filename… | 190 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.client` | QQ 官方 HTTP API 客户端：网关地址、发消息、上传媒体。 | ApiClient, PreparedSource, prepare_source, target_api_path | 222 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.config` | qqofficial 适配器配置与装配入口。 | Config, apply_defaults, new_from_platform_config, resolve_s… | 182 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.gateway` | QQ 官方网关：握手、心跳、事件分发与重连判定。 | GatewayState, ReconnectReason, classify_gateway_error, hand… | 314 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.keyboard` | 风险确认内联键盘。 | command_button, risk_keyboard, should_attach_risk_keyboard | 78 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.message` | QQ 官方入站消息 → 平台中立 ``MessageContext``，以及 Chat History 落库。 | append_non_text_segments, final_message_segments, handle_c2… | 386 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.send` | QQ 官方出站发送：文本分页、Markdown 降级、媒体上传、键盘、Ark。 | base_message, context_target, meta_string, qq_official_medi… | 284 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.target` | qqofficial 的发送落点与 ctx 传递。 | SendTarget, is_group_context, target_from_context, target_s… | 69 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.token` | QQ 官方 access token 的获取与缓存。 | TokenSource, parse_expires_in, parse_expires_text | 166 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.types` | QQ 官方机器人网关与消息 API 的线上类型。 | AccessTokenRequest, AccessTokenResponse, ApiErrorResponse, … | 684 | `test_platform_qqofficial.py` |
| `atri_x.platform.refcontext` | 引用的恢复与续聊/Fork 判定。 | Apply, BOT_REFERENCE_LABEL, ChatMessageReply, DEFAULT_REFER… | 458 | `test_refcontext.py` |
| `atri_x.platform.telegram` | Telegram 平台适配器（对应 Go 包 ``internal/platform/telegram``）。 | — | 112 | — |
| `atri_x.platform.telegram.adapter` | Telegram 适配器主体：long polling 入站循环与出站发送。 | Adapter, new_adapter, parse_int64, raise_context_error, rec… | 799 | — |
| `atri_x.platform.telegram.capabilities` | Telegram 的可选群目录能力（群信息 + 管理员列表）。 | get_group_info, get_group_member_list, telegram_chat_id | 116 | — |
| `atri_x.platform.telegram.client` | Telegram Bot API 客户端（long polling、发送、媒体上传与下载）。 | ApiClient, MediaSource, RedactedTelegramError, TelegramAPIE… | 463 | — |
| `atri_x.platform.telegram.config` | Telegram 适配器配置与装配入口。 | Config, apply_defaults, new_from_platform_config, normalize… | 235 | — |
| `atri_x.platform.telegram.format` | Markdown → Telegram 实体文本（HTML）渲染。 | code_fence_start, escape_html, first_regex_group, format_in… | 218 | — |
| `atri_x.platform.telegram.keyboard` | 高风险工具确认的内联键盘。 | command_button, risk_keyboard, should_attach_risk_keyboard | 48 | — |
| `atri_x.platform.telegram.message` | 入站消息归一化：文本、@提及、媒体段、Chat History 落库与媒体解析。 | NormalizedMessage, append_non_text_segments, audio_extensio… | 421 | — |
| `atri_x.platform.telegram.stream` | 流式助手消息：增量预览 + 最终替换。 | MessageStream, start_stream, stream_preview_payload, stream… | 270 | — |
| `atri_x.platform.telegram.target` | 发送落点与 ctx 传递（Python 侧拆出的最小模块）。 | Target, target_from_context, with_target | 42 | — |
| `atri_x.platform.telegram.types` | Telegram Bot API 的线上类型与请求体。 | AnswerCallbackQueryRequest, ApiResponse, Audio, BotCommand,… | 579 | — |
| `atri_x.processenv` | 子进程环境分层与凭据过滤。 | — | 19 | `test_app_tool_runtime_wiring.py`、`test_builtin_shell.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.processenv.environment` | 子进程环境的分层快照、PATH 补充与可执行文件解析。 | Command, Environment, SensitiveEnvName, executable_candidat… | 337 | — |
| `atri_x.ratelimit` | 按键的滑动窗口限流器（进程内）。 | Window, new, new_with_clock | 99 | `test_builtin_tools2.py`、`test_ops.py` |
| `atri_x.reaction` | 本地表情图库与情绪选择器（Python 侧新增，无 Go 对应）。 | CATALOG_FILENAME, Catalog, CatalogItem, DEFAULT_ASSETS_DIR,… | 59 | `test_reaction.py` |
| `atri_x.reaction.catalog` | 本地表情图库目录（``reactions/catalog.json``）的读取与校验。 | CATALOG_FILENAME, Catalog, CatalogItem, DEFAULT_ASSETS_DIR,… | 315 | `test_reaction.py` |
| `atri_x.reaction.marker` | 表情意图标记的解析与清除（Python 侧新增，无 Go 对应）。 | MAX_EMOTIONS_PER_REPLY, MAX_EMOTION_RUNES, MarkerParse, ext… | 121 | — |
| `atri_x.reaction.policy` | 表情发送策略：启用范围、场景抑制、冷却、去重与概率（Python 侧新增，无 Go 对应）。 | MAX_SUPPRESS_KEYWORDS, ReactionSettings, normalize_suppress… | 221 | — |
| `atri_x.reaction.service` | 表情反应的决策与状态：要不要发、发哪张、冷却与去重。 | Decision, ReactionPick, ReactionService | 323 | — |
| `atri_x.redact` | 凭据脱敏与错误 ID。 | — | 21 | `test_redact.py`、`test_redact_idempotent.py` |
| `atri_x.redact.redact` | 凭据脱敏与错误 ID。 | Error, NewErrorID, Secrets, Summarize, error, new_error_id,… | 165 | — |
| `atri_x.request` | active request 的并发限制、有界队列与公平调度。 | — | 27 | `agent_commands_fakes.py`、`test_agent_commands_session.py`、`test_agent_context_runtime.py`、`test_request_manager.py` |
| `atri_x.request.manager` | active request 的并发限制、有界等待队列与公平调度。 | KindCompress, KindHook, KindLLM, KindSubAgent, KindTool, Ki… | 727 | `test_agent_context_runtime.py` |
| `atri_x.research` | 科研研究助理：随包引擎、技能与装配引导。 | default_config_dir, research_home | 30 | `test_research_bootstrap.py` |
| `atri_x.research.bootstrap` | 把随包研究引擎装进用户配置目录（首次启动自动执行，幂等、可升级、失败不阻断主程序）。 | BootstrapResult, INSTALL_SCHEMA, MODE_BUNDLED, ensure_insta… | 539 | `test_research_bootstrap.py` |
| `atri_x.research.pack` | 随包研究引擎资源（``research_pack/``）的定位与元数据读取。 | PACK_DIR_NAME, PACK_META_FILE, PACK_SCHEMA, is_frozen, load… | 102 | `test_research_bootstrap.py` |
| `atri_x.research.paths` | 研究数据目录的解析规则（命令层 / GUI / 引擎子进程共用唯一出处）。 | APP_DIR_NAME, ENV_HOME, XDG_APP_DIR_NAME, default_config_di… | 55 | `test_research_bootstrap.py` |
| `atri_x.research.runner` | 研究引擎的子进程入口（技能 ``ATRIX_SKILL.toml`` 的 ``command`` 指向这里）。 | main, parse_target | 119 | `test_research_bootstrap.py` |
| `atri_x.runtime` | 运行状态快照与展示格式化。 | — | 27 | `test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_core.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py`、`test_turn_manager.py` |
| `atri_x.runtime.status` | 运行状态快照与展示格式化。 | Phase, PhaseDone, PhaseError, PhaseIdle, PhaseLLM, PhasePre… | 200 | `test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py` |
| `atri_x.safecontext` | 上下文块渲染与取消令牌。 | CancelToken, CancelledError, DeadlineExceededError, ErrCanc… | 476 | `test_agent_chat.py`、`test_agent_core.py`、`test_atrinis_http.py`、`test_fileops.py`、`test_groupanalysis.py`、`test_maintenance.py`、`test_maintenance_dsh.py`、`test_media_image.py`、`test_media_manager.py`、`test_media_worker.py`、`test_ops.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py`、`test_request_manager.py`、`test_selflearning.py`、`test_signal.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_turn_manager.py`、`test_vision.py`、`test_vision_privacy.py` |
| `atri_x.safety` | 内置安全闸门：道德 / 合规词表过滤 + 敏感信息（PII / 凭据）脱敏。 | ACTION_BLOCK, ACTION_FLAG, ACTION_PASS, ACTION_REDACT, Audi… | 119 | `test_safety_core.py`、`test_safety_integration.py` |
| `atri_x.safety.audit` | 审计日志：``safety-audit.jsonl`` 的追加写入。 | AuditLog, DEFAULT_AUDIT_MAX_BYTES, utc_now | 142 | — |
| `atri_x.safety.guard` | 安全闸门：文本审核 / 脱敏、媒体出口判定与审计的唯一入口。 | DEFAULT_BLOCK_REPLACEMENT, MEDIA_BLOCK_TEXT, ModeBoundGuard… | 635 | — |
| `atri_x.safety.policy` | 安全策略：``[safety]`` 段 → :class:`SafetyPolicy`，以及内置类别的默认值。 | ACTION_BLOCK, ACTION_FLAG, ACTION_PASS, ACTION_REDACT, DEFA… | 379 | — |
| `atri_x.safety.rules` | 规则与词表的唯一出处：词表数据文件 + 词表匹配 + 敏感信息正则。 | CATEGORY_IDS, CATEGORY_LABELS, CATEGORY_SEVERITIES, RuleHit… | 425 | — |
| `atri_x.security` | 工具权限与风险策略。 | ACTOR_CONTEXT_KEY, Actor, ActorFromContext, ActorID, Compar… | 121 | `agent_commands_fakes.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_group_policy.py`、`test_agent_participation.py`、`test_agent_risk.py`、`test_app_runtime_agent.py`、`test_background.py`、`test_builtin_cron.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_cron.py`、`test_hook_runtime.py`、`test_long_memory_scope.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_platform_qq_onebot.py`、`test_resident_memory.py`、`test_security_grants.py`、`test_tool_discover.py`、`test_tool_skill.py`、`test_toolrun.py` |
| `atri_x.security.context` | Actor / Policy 在取消令牌上的传播。 | ACTOR_CONTEXT_KEY, POLICY_CONTEXT_KEY, actor_from_context, … | 125 | — |
| `atri_x.security.grants` | 授权登记表（grant registry）：把"这次 / 这个会话后续"的放行变成有生命周期的凭据。 | Grant, GrantRegistry | 326 | — |
| `atri_x.security.security` | 工具权限与风险策略。 | Actor, GroupRole, GroupRoleAdmin, GroupRoleMember, GroupRol… | 244 | — |
| `atri_x.selflearning` | ATRI-X 的 clean-room 自学习层（观察 / 候选挖掘 / 先审后应用）。 | Candidate, DEFAULT_CONTEXT_LIMIT, DEFAULT_MAX_CONTEXT_RUNES… | 123 | `test_app_ops_plugins.py`、`test_selflearning.py` |
| `atri_x.selflearning.db` | 自学习库的建表、迁移与行映射。 | DEFAULT_MAX_MINE_CHARS, DEFAULT_MAX_OBSERVATIONS_PER_SCOPE,… | 188 | — |
| `atri_x.selflearning.mining` | 候选挖掘：观察扫描 → token 统计 → 候选 upsert。 | DEFAULT_MIN_COUNT, DEFAULT_MIN_USERS, MAX_MINE_ROWS, mine, … | 186 | — |
| `atri_x.selflearning.models` | 自学习层的领域模型与状态常量。 | Candidate, DecideRequest, KindExpression, KindJargon, MineS… | 142 | — |
| `atri_x.selflearning.patterns` | 候选词抽取与判定 helper。 | STOP_CANDIDATES, contains_cjk, extract_candidates, is_ascii… | 172 | — |
| `atri_x.selflearning.service` | 自学习服务层：观察限额、生命周期策略闸门与已批准上下文构造。 | DEFAULT_CONTEXT_LIMIT, DEFAULT_MAX_CONTEXT_RUNES, DEFAULT_M… | 350 | `test_app_ops_plugins.py` |
| `atri_x.selflearning.store` | 观察写入、候选挖掘、review-before-apply 与上下文构造的 SQLite store。 | Open, Store, open_store | 448 | — |
| `atri_x.session` | 会话生命周期服务。对应 Go 包：``internal/session``（``service.go`` / ``types.go`` / ``shared.go`` / | — | 97 | `agent_commands_fakes.py`、`agent_fakes.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_context_policy.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_risk.py`、`test_cli_sessions.py`、`test_memory_notes.py`、`test_resident_memory.py`、`test_session.py`、`test_session_lifecycle.py`、`test_session_naming_contract.py` |
| `atri_x.session.browse` | 会话浏览：给远程前端（桌面端）提供"列出会话 / 读历史"的只读查询。 | DEFAULT_HISTORY_LIMIT, DEFAULT_SESSION_LIMIT, MessageView, … | 186 | `test_cli_sessions.py` |
| `atri_x.session.expiration` | 闲置过期策略：按「群聊/私聊」×「普通用户/超管」选 TTL。对应 Go：``internal/session/expiration.go``。 | ExpirationMixin, ExpireIdleRequest, ExpireIdleResult, IdleE… | 124 | — |
| `atri_x.session.fork` | 从 assistant 消息 Fork 出新的当前分支。对应 Go：``internal/session/fork.go``。 | ForkMixin | 47 | — |
| `atri_x.session.lifecycle` | 会话生命周期：重命名、归档、置顶、删除与过期清理。对应 Go：``internal/session/lifecycle.go``。 | LifecycleMixin, rename_metadata | 146 | — |
| `atri_x.session.mode` | 模式激活与模式校验。对应 Go：``internal/session/mode.go``。 | ModeMixin, validate_mode | 51 | — |
| `atri_x.session.naming` | 异步 Session 命名。对应 Go：``internal/session/naming.go``。 | MAX_NAMING_FAILURES, NamingMixin, NamingState, NoopNamingNo… | 418 | `test_session_naming_contract.py` |
| `atri_x.session.promotion` | 后台 Session 的前台接管判定与提升。对应 Go：``internal/session/promotion.go``。 | BACKGROUND_KIND_KEY, FOREGROUND_ORIGIN_KEY, ForegroundOrigi… | 209 | `test_session_lifecycle.py` |
| `atri_x.session.query` | 会话查询：分页列表、可恢复列表与当前会话状态摘要。对应 Go：``internal/session/query.go``。 | QueryMixin | 97 | — |
| `atri_x.session.service` | Session 服务主体：当前会话、创建、恢复、活跃时间与访问判定。对应 Go：``internal/session/service.go``（:class:`Service` … | Service, default_title, fork_title, new_service, new_servic… | 257 | — |
| `atri_x.session.shared` | 群共享线程的 Session 所有者、Scope.Shared key 与元数据标记。对应 Go：``internal/session/shared.go``。 | SHARED_THREAD_OWNER_PREFIX, is_shared_thread_session, metad… | 85 | — |
| `atri_x.session.text` | 标题/预览文本 helper。对应 Go：``internal/session/service.go`` 末尾的 ``forkTitle`` / ``defaultTitle``… | MAX_PREVIEW_RUNES, MAX_TITLE_RUNES, PLACEHOLDER_TITLE, defa… | 52 | — |
| `atri_x.session.types` | 会话领域类型与异步命名事件契约。对应 Go：``internal/session/types.go``。 | ActivateModeRequest, ActivateModeResult, Config, CreateRequ… | 173 | `test_agent_context_policy.py`、`test_resident_memory.py` |
| `atri_x.shellparse` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：shell 词法/语法分析 + 安全裁决查询。 | AnsiCQuoted, ArithCommand, ArithExpansion, Assignment, Bloc… | 181 | `test_shellparse.py` |
| `atri_x.shellparse.ast` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：AST 节点与位置信息。 | AnsiCQuoted, ArithCommand, ArithExpansion, Assignment, Bloc… | 577 | `test_shellparse.py` |
| `atri_x.shellparse.clauses` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：复合命令语法。 | ClauseParserMixin | 327 | — |
| `atri_x.shellparse.lexer` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：记号流与 here-document 装配。 | KIND_EOF, KIND_IO_NUMBER, KIND_NEWLINE, KIND_OP, KIND_WORD,… | 245 | — |
| `atri_x.shellparse.parser` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：递归下降语法分析。 | MAX_NESTING_DEPTH, Parser, ShellParseError, parse, parse_ra… | 369 | — |
| `atri_x.shellparse.scanner` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：单词/展开扫描层。 | MAX_NESTING_DEPTH, SourceIndex, WordScanner, scan_arith, sc… | 596 | — |
| `atri_x.shellparse.walk` | clean-room 替代 Go 的 ``mvdan.cc/sh/v3``：遍历、查询与回写。 | command_base, command_names, commands, format, has_arithmet… | 481 | `test_shellparse.py` |
| `atri_x.sidecar` | ``atri_x.sidecar``：随发行包一起安装的本地运行时（sidecar）。 | DshKernel, RUNTIME_DIR_ENV, RUNTIME_MANIFEST, Runtime, bund… | 57 | `test_sidecar_kernel.py` |
| `atri_x.sidecar.__main__` | ``python -m atri_x.sidecar``：查看/核验随包安装的运行时。 | command_status, command_verify, main | 261 | — |
| `atri_x.sidecar.kernel` | 内置 DSH 内核的定位与启动命令组装。 | DSH_KERNEL_ENV, DSH_KERNEL_ID, DshKernel, describe, dsh_com… | 153 | `test_sidecar_kernel.py` |
| `atri_x.sidecar.registry` | 运行时目录发现与清单解析。 | RUNTIME_DIR_ENV, RUNTIME_MANIFEST, Runtime, bundle_root, co… | 200 | `test_sidecar_kernel.py` |
| `atri_x.signal` | 通用类型化信号。 | — | 48 | `test_signal.py` |
| `atri_x.signal.connection` | 接收者连接句柄。 | Connection | 105 | — |
| `atri_x.signal.diagnostics` | signal 内部诊断输出。 | diagnostics_written, dropped_diagnostics, flush_diagnostics… | 171 | — |
| `atri_x.signal.errors` | signal 包的哨兵异常与 ``errors.Join`` 等价物。 | ErrClosed, ErrNoReceiver, ErrQueueFull, ErrRecursive, Joine… | 124 | — |
| `atri_x.signal.executor` | signal 的选项与观察者占位类型。 | ConnectOptions, Empty, Options | 51 | — |
| `atri_x.signal.queue` | 按 key 分道的调度队列。 | Queue, QueueOptions, new_queue | 379 | — |
| `atri_x.signal.result` | 注册结果与一次 emit 的完成句柄。 | Emission, Resolved, Result, map_results | 193 | — |
| `atri_x.signal.shutdown` | 关闭策略与任务描述。 | CancelPending, Drain, SHUTDOWN_POLICIES, ShutdownPolicy, Ta… | 56 | — |
| `atri_x.signal.signal` | 类型化信号：每个信号独立调度、独立限额。 | Signal, new | 365 | — |
| `atri_x.storage` | 存储包。对应 Go：``internal/storage``。 | — | 18 | `_atrinis_support.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_commands_session.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_angelmemory.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_cron.py`、`test_builtin_imagegen.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_contextmgr.py`、`test_cron.py`、`test_cron_delivery.py`、`test_cron_dst.py`、`test_daily_summary.py`、`test_dsh_route.py`、`test_groupanalysis.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_historygate.py`、`test_hook_runtime.py`、`test_maintenance.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_member_profile.py`、`test_platform_qq_onebot.py`、`test_refcontext.py`、`test_selflearning.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_migrations.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_tool_media_runtime.py`、`test_toolrun.py` |
| `atri_x.storage.ids` | 通用 ID helper。对应 Go：``internal/storage/id.go``。 | is_valid_id, new_id, parse_id | 41 | `test_builtin_cron.py`、`test_cron.py`、`test_media_manager.py`、`test_storage_models.py` |
| `atri_x.storage.models` | 存储领域模型。对应 Go：``internal/storage/storage.go``（领域模型部分；repository | AtrinisEvent, AtrinisReportDelivery, AtrinisReportDeliveryS… | 602 | `test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_title.py`、`test_builtin_cron.py`、`test_cli_sessions.py`、`test_contextmgr.py`、`test_cron.py`、`test_cron_delivery.py`、`test_cron_dst.py`、`test_groupanalysis.py`、`test_hook_runtime.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_member_profile.py`、`test_refcontext.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_toolrun.py` |
| `atri_x.storage.repositories` | 存储 repository 抽象。对应 Go：``internal/storage/storage.go``（repository | AtrinisEventRepository, ChatHistoryRangeRepository, ChatHis… | 506 | `test_historygate.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py` |
| `atri_x.storage.sqlite` | SQLite 存储实现包。对应 Go：``internal/storage/sqlite``。 | AtrinisEventRepository, ChatHistoryRepository, ChatHistoryS… | 91 | `_atrinis_support.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_daily_summary.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_historygate.py`、`test_maintenance.py`、`test_session.py`、`test_sqlite_media.py`、`test_sqlite_migrations.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py` |
| `atri_x.storage.sqlite.atrinis_event_repository` | Atrinis 事件与报告投递（outbox）仓储。对应 Go：``internal/storage/sqlite/atrinis_event_repository.go``。 | AtrinisEventRepository | 420 | — |
| `atri_x.storage.sqlite.chat_history_repository` | 入站聊天历史与出站消息仓储。对应 Go：``internal/storage/sqlite/chat_history_repository.go``。 | ChatHistoryRepository, ChatHistoryStore, OutboundMessageRep… | 448 | `test_app_tool_runtime_wiring.py`、`test_gui_members.py`、`test_gui_tabs.py` |
| `atri_x.storage.sqlite.context_summary_repository` | 上下文摘要仓储。对应 Go：``internal/storage/sqlite/context_summary_repository.go``。 | ContextSummaryRepository | 138 | — |
| `atri_x.storage.sqlite.cron_job_repository` | 定时任务仓储。对应 Go：``internal/storage/sqlite/cron_job_repository.go``。 | CronJobRepository | 271 | — |
| `atri_x.storage.sqlite.media_history` | 历史消息媒体位置。对应 Go：``internal/storage/sqlite/media_history.go``。 | MediaHistoryMixin | 111 | — |
| `atri_x.storage.sqlite.media_lifecycle` | 媒体清理认领、输出索引、启动恢复与只读审计。对应 Go：``internal/storage/sqlite/media_lifecycle.go``。 | MediaLifecycleMixin | 200 | — |
| `atri_x.storage.sqlite.media_repository` | 媒体与媒体引用仓储。对应 Go：``internal/storage/sqlite/media_repository.go``。 | MediaReferenceRepository, MediaRepository | 226 | — |
| `atri_x.storage.sqlite.message_repository` | 会话消息仓储。对应 Go：``internal/storage/sqlite/message_repository.go``。 | MessageRepository | 257 | — |
| `atri_x.storage.sqlite.migrations` | SQLite schema migration。对应 Go：``internal/storage/sqlite/migrations.go``。 | MIGRATIONS, Migration, MigrationError, SCHEMA_MIGRATIONS_DD… | 614 | `test_sqlite_migrations.py` |
| `atri_x.storage.sqlite.session_repository` | 会话仓储。对应 Go：``internal/storage/sqlite/session_repository.go``。 | SessionRepository | 317 | — |
| `atri_x.storage.sqlite.store` | SQLite store：连接、事务与门面。对应 Go：``internal/storage/sqlite/store.go``。 | SQLiteDatabase, SQLiteError, SQLiteTransaction, Store, bool… | 397 | `test_app_tool_runtime_wiring.py` |
| `atri_x.storage.sqlite.tool_call_repository` | 工具调用审计仓储。对应 Go：``internal/storage/sqlite/tool_call_repository.go``。 | ToolCallRepository | 102 | — |
| `atri_x.storage.storage` | 存储领域模型与 repository 抽象（对外统一出口）。对应 Go：``internal/storage/storage.go``。 | AtrinisEvent, AtrinisEventRepository, AtrinisReportDelivery… | 161 | `test_storage_models.py` |
| `atri_x.storage.times` | 通用时间 helper。对应 Go：``internal/storage/time.go``。 | ZERO_TIME, ensure_utc, format_time, is_zero_time, now, orde… | 156 | `test_angelmemory.py`、`test_builtin_cron.py`、`test_cron.py`、`test_cron_delivery.py`、`test_groupanalysis.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_platform_qq_onebot.py`、`test_selflearning.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py` |
| `atri_x.sysinfo` | 可移植的运行时资源快照。 | ProcessResources, ProcessSample, ResourceSnapshot, Snapshot… | 919 | `test_builtin_runtime_resources.py`、`test_maintenance_report.py`、`test_ops.py` |
| `atri_x.textmatch` | 确定性的关键词抽取与 LIKE 转义助手。 | Keywords, LikePattern, keywords, like_pattern | 228 | `test_redact.py` |
| `atri_x.time_service` | 统一时间服务：业务时间的**唯一**读取点（Python 侧新增模块）。 | CLOCK_JUMP_THRESHOLD_SECONDS, DEFAULT_TIME_SOURCE, TimeServ… | 354 | `test_time_context.py`、`test_time_service.py`、`test_timesync_verifier.py` |
| `atri_x.timesync` | 网络时间校验子系统（首版 ``verify_only``）。 | OutboundPolicy, ROUND_TRIGGER_MANUAL, ROUND_TRIGGER_SCHEDUL… | 45 | `test_timesync_https.py`、`test_timesync_sntp.py`、`test_timesync_verifier.py` |
| `atri_x.timesync.diagnostics` | 时间相关诊断的只读聚合（``/diagnostics`` 的 ``time`` 段）。 | time_diagnostics | 86 | — |
| `atri_x.timesync.https_time` | HTTPS 时间 API 适配器（JSON 契约 + TLS + 新鲜度/大小校验）。 | DEFAULT_MAX_BODY_BYTES, FetchResult, FetchTransport, MAX_AC… | 284 | `test_timesync_https.py` |
| `atri_x.timesync.model` | 网络时间观测与一轮校验的判定结果（纯数据 + 纯函数，无 I/O）。 | ROUND_TRIGGER_MANUAL, ROUND_TRIGGER_SCHEDULED, RoundResult,… | 283 | `test_timesync_https.py`、`test_timesync_sntp.py` |
| `atri_x.timesync.policy` | 网络时间来源的出站校验：协议白名单、私网例外、大小/超时上限、日志脱敏。 | ADDRESS_PRIVATE, ADDRESS_PUBLIC, EndpointCheck, OutboundPol… | 231 | `test_timesync_sntp.py` |
| `atri_x.timesync.sntp` | 受限 SNTP 客户端（只做单播查询，不做服务端/转发/NTS）。 | MAX_ROUND_TRIP_MS, NTP_PACKET_SIZE, NTP_UNIX_EPOCH_DELTA, N… | 351 | `test_timesync_sntp.py`、`test_timesync_verifier.py` |
| `atri_x.timesync.verifier` | 间歇性多来源时间校验器（首版 ``verify_only``：只观测，不接管业务时间）。 | ALERT_HISTORY, MAX_SAMPLE_HISTORY, Verifier, VerifierOptions | 637 | `test_timesync_verifier.py` |
| `atri_x.timezones` | 时区解析与 tzdata 数据探测（Python 侧新增模块）。 | DEFAULT_TIMEZONE, FIXED_PREFIX, SYSTEM_TIMEZONE_NAME, Timez… | 264 | `test_timezones.py` |
| `atri_x.tool` | Tool Runtime：工具契约、注册表、发现、风险准入与执行适配。 | — | 191 | `agent_fakes.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_background.py`、`test_builtin_character_artist.py`、`test_builtin_cron.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_command_router.py`、`test_completion.py`、`test_hook_runtime.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_qq_slowfetch.py`、`test_research_bootstrap.py`、`test_safety_integration.py`、`test_tool_core.py`、`test_tool_discover.py`、`test_tool_media_runtime.py`、`test_tool_runtimeinfo.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_toolrun.py`、`test_usage_attribution.py`、`test_vision_privacy.py`、`test_vision_threading.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.availability` | 工具"为什么没被采用"的机器可读原因，以及产出它的唯一判定入口。 | registry_tool_availability_reason, tool_access_reason, tool… | 114 | — |
| `atri_x.tool.builder` | 工具 schema 与元信息的声明式构造器。 | Builder, ParamConfig, enum, items, new_builder, required | 249 | `test_tool_discover.py` |
| `atri_x.tool.builtin` | 内置工具包：ATRI-X 自带的全部工具实现与注册入口。 | — | 748 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_character_artist.py`、`test_builtin_cron.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_toolrun.py`、`test_vision_privacy.py`、`test_vision_threading.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.builtin.angel_memory` | Angel 长期记忆工具族：写入、召回，以及可选的删除（angel_forget）。 | AngelForgetArgs, AngelForgetResolved, AngelForgetTarget, An… | 457 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.artist_extract` | ``artist_extract``：从提示词文本或图片自身的生成参数里提取画师串。 | ArtistExtractArgs, ArtistExtractOutput, ArtistExtractTool, … | 456 | `test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_library` | ``artist_library_*``：画师串组合的检索、读取、保存与删除。 | ArtistLibraryDeleteArgs, ArtistLibraryDeleteTool, ArtistLib… | 817 | `test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_tools` | 画师串工具的共享依赖、身份视图与错误文案。 | ArtistTools, ArtistViewer, artist_builder, artist_error_mes… | 206 | `test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_builtin_tools4.py` |
| `atri_x.tool.builtin.atriwisp_creator` | ``atriwisp_creator``：给出一份创建 Atriwisp 监听器的精简任务卡。 | AtriwispCreatorConfigSnapshot, AtriwispCreatorTool, build_a… | 343 | `test_builtin_tools4.py` |
| `atri_x.tool.builtin.character` | ``character_*``：角色素材库的列表、读取、检索、管理与删除。 | CharacterActivateArgs, CharacterActivateTool, CharacterDele… | 1117 | `test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_member_persona.py` |
| `atri_x.tool.builtin.chat_history` | 聊天历史工具族：关键词检索、上下文窗口、引用回复。 | AroundChatHistoryArgs, ChatHistoryContext, GetChatHistoryAr… | 598 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.common` | 内置工具的公共环境读取与共享小助手。 | abs_clean, append_warnings, builtin_env, cancel_token_from_… | 249 | `test_builtin_character_artist.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_tools3.py` |
| `atri_x.tool.builtin.cron` | ``cron`` / ``cron_query`` / ``cron_write`` 三个定时任务工具。 | CronCreateArgs, CronQueryArgs, CronQueryTool, CronTool, Cro… | 594 | `test_builtin_cron.py`、`test_builtin_tools3.py` |
| `atri_x.tool.builtin.fetch_message` | ``fetch_message`` 工具：按消息 id 取回引用 / 转发的完整正文。**Python 版新增**。 | FETCH_MESSAGE_TOOL_NAME, FetchMessageArgs, FetchMessageTool… | 109 | `test_qq_slowfetch.py` |
| `atri_x.tool.builtin.file_guard` | 文件读写导流守卫：按路径把 read_file / edit_file 引向专用工具。 | FileGuard, FileGuardRule, new_atrix_skill_file_guard_rule, … | 158 | `test_builtin_files.py`、`test_builtin_shell.py` |
| `atri_x.tool.builtin.file_manager` | 出站文件交付的准备与体积校验。 | FileManager, PreparedFile, new_file_manager, new_file_manag… | 105 | `test_app_runtime_agent.py`、`test_builtin_tools4.py`、`test_member_profile.py` |
| `atri_x.tool.builtin.file_rollback` | ``rollback_file``：撤销 ATRI-X 最近一次 ``edit_file`` 对某个文件的修改。 | RollbackFileArgs, RollbackFileTool, format_rollback_records… | 175 | `test_builtin_files.py` |
| `atri_x.tool.builtin.file_tools` | ``read_file`` 与 ``edit_file``：带 revision 校验的文本读写。 | EditFileArgs, EditFileTool, ReadFileArgs, ReadFileTool, cap… | 605 | `test_builtin_files.py` |
| `atri_x.tool.builtin.file_tools_ast` | ``read_file`` 的 AST 搜索模式：Go / Shell 源码的标识符与函数名检索。 | ASTFunctionMatch, ASTMatch, detect_ast_language, find_go_as… | 633 | — |
| `atri_x.tool.builtin.file_tools_detail` | ``edit_file`` 的人读确认详情渲染。 | edit_line_range_text, edit_operation_title, file_tool_bool_… | 123 | — |
| `atri_x.tool.builtin.file_tools_directory` | ``read_file`` 的目录搜索模式：递归 grep（走 ripgrep）与递归 AST 检索。 | DirectoryASTCache, DirectoryASTCacheEntry, DirectoryASTCach… | 507 | — |
| `atri_x.tool.builtin.file_tools_read` | ``read_file`` 的声明与结果渲染。 | ReadFileArgs, is_sensitive_read_file, read_file_builder, re… | 216 | — |
| `atri_x.tool.builtin.get_media` | ``get_media``：按当前聊天的消息 ID 与媒体序号取媒体 ID（不返回内容）。 | GetMediaArgs, GetMediaTool, get_media_builder, get_media_hi… | 226 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.group_analysis` | ``group_analysis``：统计当前群聊的消息量、活跃成员与活跃时段。 | GroupAnalysisArgs, GroupAnalysisTool, group_analysis_builde… | 145 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.image_generate` | ``image_generate``：调用 OpenAI 兼容的生图端点并组装最终提示词。 | ImageGenerateArgs, ImageGenerateTool, ImageProfile, generat… | 754 | `test_builtin_imagegen.py`、`test_builtin_tools4.py`、`test_safety_integration.py` |
| `atri_x.tool.builtin.image_generate_compose` | ``image_generate`` 的提示词编译与参考图收集。 | ImageArtistBlock, ImageComposition, ImageReferenceSource, c… | 554 | `test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_generate_context` | ``image_generate`` 的上下文与角色解析部分。 | ImagePromptRewriteRequest, ImagePromptRewriter, ImageToolMe… | 464 | `test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_to_prompt` | ``image_to_prompt``：把参考图片反推成可直接用于绘图的提示词。 | ImageArtistJSON, ImageMetadataRecord, ImagePromptService, I… | 498 | `test_builtin_imagegen.py`、`test_vision_privacy.py` |
| `atri_x.tool.builtin.long_memory` | ``long_memory`` / ``long_memory_search`` / ``long_memory_write`` 三个工具。 | LongMemorySearchTool, LongMemoryTool, LongMemoryWriteTool, … | 315 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_model` | 长期记忆的记录模型、front matter 读写与文本处理助手。 | LongMemoryFrontMatter, LongMemoryInvalidFile, LongMemoryRec… | 528 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_scope` | Long Memory 的会话作用域：归属记录、检索隔离与跨会话操作授权。 | check_record_access, current_actor_id, current_scope_key, i… | 148 | `test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_store` | 长期记忆的 SQLite 索引、文件回填与检索。 | LongMemoryStore, append_long_memory_change, long_memory_inl… | 980 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.persona` | ``persona_control``：成员自己开关「专属人设」（按人长期生效）。 | PERSONA_CONTROL_NAME, PersonaControlArgs, PersonaControlToo… | 264 | `test_member_persona.py` |
| `atri_x.tool.builtin.prompt_library` | ``prompt_library_search``：检索内置的 GPT Image 提示词库。 | PromptLibrarySearchArgs, PromptLibrarySearchTool, parse_pro… | 132 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.register` | 把所有已落地的内置工具装进 :class:`atri_x.tool.Registry`。 | RegisterOptions, new_registry_with_builtin, register_all, w… | 458 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_imagegen.py`、`test_builtin_tools2.py`、`test_builtin_tools4.py`、`test_member_profile.py`、`test_qq_slowfetch.py` |
| `atri_x.tool.builtin.resident_memory` | 常驻记忆工具族：入口 + 读取 + 普通记忆 + 核心记忆。 | MemoryCoreArgs, MemoryNormalArgs, MemoryReadArgs, MemoryRea… | 388 | `test_builtin_tools2.py`、`test_memory_notes.py` |
| `atri_x.tool.builtin.runtime` | ``runtime.go`` 对应的入口名与配置换算。 | ImageGenConfig, ImagePromptRewriteRequest, ImagePromptRewri… | 151 | `test_builtin_tools4.py` |
| `atri_x.tool.builtin.runtime_resources` | ``runtime_resources``：超管专属的只读硬件资源占用查询。 | RuntimeResourcesArgs, RuntimeResourcesTool, new_runtime_res… | 182 | `test_builtin_runtime_resources.py` |
| `atri_x.tool.builtin.runtimeinfo` | 内置工具共享的运行时信息 —— 现在只是 :mod:`atri_x.tool.runtimeinfo` 的再导出。 | Info, RuntimeInfo, atriyph_rule_card, first | 32 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_member_profile.py` |
| `atri_x.tool.builtin.self_learning` | ``self_learning_review``：管理 clean-room 表达/黑话候选。 | SelfLearningReviewArgs, SelfLearningReviewTool, new_self_le… | 218 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.send_file` | ``send_file``：把本地文件 / file URI / HTTP(S) URL / 媒体 ID 作为产物发出。 | SendFileArgs, SendFileTool, args_source, file_name_from_url… | 264 | `test_builtin_tools3.py`、`test_safety_integration.py` |
| `atri_x.tool.builtin.shell` | ``shell`` 工具：带风险分级、目录约束与后台沙盒的执行器。 | ShellArgs, ShellData, ShellTool, background_execution_denia… | 468 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_env` | Shell 解释器探测、子进程配置与进程树清理。 | WindowsShell, configure_shell_process, current_shell_desc, … | 139 | — |
| `atri_x.tool.builtin.shell_risk` | Shell 命令的静态风险分级。 | ShellRiskClassifier, classify_bash_shell_command, classify_… | 566 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_sandbox` | 后台沙盒下的 shell 命令风险加固。 | BackgroundShellPolicy, ShellSandboxChecker, append_unique_r… | 484 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_syntax` | Shell 命令的字面词解析与参数判定助手。 | command_base, has_arg, has_recursive, has_recursive_force, … | 138 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_warnings` | Shell 命令的执行建议：把「该用专用工具」的纠偏提示回喂给模型。 | ShellAdvice, analyze_bash_shell_advice, analyze_shell_advic… | 191 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.time_tool` | ``get_current_time`` 工具：给模型一个**只读**的当前时间来源。 | GET_CURRENT_TIME_TOOL_NAME, GetCurrentTimeArgs, GetCurrentT… | 200 | — |
| `atri_x.tool.builtin.view_image` | ``view_image``：把一张图片作为真实图片段交给模型。 | ViewImageArgs, ViewImageTool, first_image_index, image_inde… | 424 | `test_builtin_tools3.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.tool.builtin.web_extract` | ``web_extract``：网页正文提取（可配置的抓取通道链）。 | CachedExtract, ExtractCache, ExtractProxyConfig, WebExtract… | 483 | `test_builtin_tools2.py`、`test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_extract_engines` | ``web_extract`` 的抓取通道。 | ExtractEngineFailure, ExtractEngineSpec, ExtractPage, JinaD… | 402 | `test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_search` | ``web_search``：网页搜索（可配置的通道链）。 | TavilyRequest, TavilyResponse, TavilyResult, WebSearchArgs,… | 610 | `test_builtin_tools2.py`、`test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_cli` | ``web_search`` 的外部 CLI 通道：Antigravity（``agy``）与 Grok Build（``grok``）。 | CLI_SEARCH_TIMEOUT_SECONDS, DEFAULT_ANTIGRAVITY_BIN, DEFAUL… | 376 | `test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_engines` | ``web_search`` 的引擎通道。 | EngineFailure, SearchEngineSpec, SearchHit, SearchOutcome, … | 556 | `test_web_search_engines.py` |
| `atri_x.tool.builtin.workspace` | ``workspace`` 工具：设置当前 Session 的共享工作目录。 | WorkspaceArgs, WorkspaceTool, default_workspace_dir, find_w… | 238 | `test_builtin_files.py`、`test_builtin_tools2.py`、`test_toolrun.py` |
| `atri_x.tool.capability` | 当前请求所用模型的能力声明，并充当本包共享的零依赖底座。 | Capabilities, capabilities_from_context, ctx_error, ctx_rai… | 170 | `test_builtin_tools3.py`、`test_command_router.py` |
| `atri_x.tool.detail` | 工具详情的结构化块、发现结果、发现文案提供者与规则卡去重。 | ContextDiscoveryContentProvider, DetailBlock, DetailProvide… | 395 | `agent_fakes.py`、`test_agent_cron_background.py`、`test_tool_discover.py` |
| `atri_x.tool.discover` | ``discover_tool``：把可用工具/技能的简介或完整 schema 交给模型。 | DISCOVER_TOOL_NAME, DiscoverArgs, DiscoverFilter, DiscoverT… | 346 | `test_tool_discover.py` |
| `atri_x.tool.executor` | Tool Runtime 的执行适配：风险裁决、策略准入与结果回灌消息。 | ExecutionResult, Executor | 124 | — |
| `atri_x.tool.media_runtime` | 宿主侧媒体访问：显式输入声明、临时引用与沙盒内导出入。 | CLOSE_TIMEOUT_SECONDS, MEDIA_CALL_DIR_PREFIX, MEDIA_INPUTS_… | 389 | `test_app_tool_runtime_wiring.py`、`test_builtin_tools4.py`、`test_tool_media_runtime.py` |
| `atri_x.tool.provider` | Agent 提示用的工具/skill 名单与 schema 提供者。 | PromptNames, SchemaProvider, actor_from_scope, prompt_names… | 145 | — |
| `atri_x.tool.runtimeinfo` | ``internal/tool/runtimeinfo`` 的 Python 移植。 | Info, atriyph_rule_card, first | 105 | `test_builtin_cron.py`、`test_tool_runtimeinfo.py` |
| `atri_x.tool.sandbox` | 工具执行的轻量沙盒运行态、后台上下文与相对路径解析。 | BackgroundKind, SandboxContext, background_context, backgro… | 150 | `test_agent_cron.py`、`test_agent_cron_background.py`、`test_background.py`、`test_builtin_files.py`、`test_builtin_shell.py`、`test_builtin_tools3.py`、`test_toolrun.py`、`test_usage_attribution.py` |
| `atri_x.tool.skill` | Skill 工具包：AgentSkill / Go skill 的发现、运行与创建。 | AGENT_SKILL_CONFIG_FILE, AgentSkillManagerName, AgentSkillM… | 186 | `test_app_tool_runtime_wiring.py`、`test_builtin_tools4.py`、`test_research_bootstrap.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py` |
| `atri_x.tool.skill.agent_manifest` | AgentSkill 清单（``ATRIX_SKILL.toml``）的读取、校验与 schema 投影。 | AGENT_SKILL_CONFIG_FILE, AgentSkillManifest, AgentSkillMani… | 349 | `test_research_bootstrap.py` |
| `atri_x.tool.skill.agent_skill_tool` | ``agent_skill`` 工具：读写 AgentSkill 的 ``ATRIX_SKILL.toml``。 | AgentSkillArgs, AgentSkillPreview, AgentSkillTool, agent_sk… | 274 | — |
| `atri_x.tool.skill.atriyph_warnings` | 把 Atriyph 诊断拼进工具输出。 | append_atriyph_warnings | 23 | — |
| `atri_x.tool.skill.catalog` | Skill 目录：种类、记录与线程安全目录。 | Catalog, Kind, KindAgent, KindGo, Record, SourceForKind, ne… | 128 | — |
| `atri_x.tool.skill.command_tool` | 可调用的 AgentSkill 工具（执行清单里的 command）。 | CommandTool, JsonNumber, argument_text, command_arguments, … | 207 | — |
| `atri_x.tool.skill.creator` | ``create_atrix_skill``：创建 ATRI-X 原生 Atriyph skill。 | CREATE_ATRIX_SKILL_NAME, CreateAtrixSkillArgs, CreateAtrixS… | 339 | `test_tool_skill_editor.py`、`test_tool_skill_tools.py` |
| `atri_x.tool.skill.descriptor` | 文档型 skill 的工具描述符。 | AgentSkillManagerName, Descriptor, GoRunnerName, agent_skil… | 165 | — |
| `atri_x.tool.skill.finalize_atrix_skill` | ``finalize_atrix_skill``：校验、gofmt、编译并重载。 | FINALIZE_ATRIX_SKILL_NAME, FinalizeAtrixSkillArgs, Finalize… | 204 | — |
| `atri_x.tool.skill.go_source` | Go skill 源码的规范化、校验与编译。 | DEFAULT_BUILD_TIMEOUT, GO_BINARY_ENV, GoBuildResult, build_… | 294 | `test_tool_skill.py` |
| `atri_x.tool.skill.manager` | Skill 管理器：装载、重载、删除与 AgentSkill 清单写入。 | FileSnapshot, Manager, new_manager | 294 | — |
| `atri_x.tool.skill.media` | Skill 子进程的媒体交接。 | MAX_INLINE_MEDIA_BYTES, prepare_command_media, prepare_go_m… | 320 | — |
| `atri_x.tool.skill.modify_atrix_skill` | ``read_atrix_skill`` / ``modify_atrix_skill``：按行读、按编辑协议改原生 skill。 | ATRIX_SKILL_TARGET_ATRIYPH, ATRIX_SKILL_TARGET_CODE_SOURCE,… | 508 | — |
| `atri_x.tool.skill.parser` | SKILL.md 的 front matter 解析与风险等级解析。 | Definition, first_markdown_paragraph, join_sentences, parse… | 175 | — |
| `atri_x.tool.skill.process` | 子进程启动、超时/取消与进程树终止。 | CommandOutput, DEFAULT_WAIT_DELAY, IS_WINDOWS, MAX_RUNNER_O… | 244 | — |
| `atri_x.tool.skill.runner` | Go skill 运行器：把 payload 写进子进程 stdin 并解析 stdout。 | DEFAULT_RUNNER_TIMEOUT, GoRunner, GoRunnerArgs, GoRunnerNam… | 333 | — |
| `atri_x.tool.skill.scanner` | 文件系统 skill 扫描器。 | FilesystemScanner, atriyph_risk, default_root, dir_name_for… | 399 | — |
| `atri_x.tool.tool` | Tool Runtime 核心类型：工具契约、风险等级、注册表与工具发现。 | CallRequest, ConfirmationPreflightProvider, Info, Registry,… | 502 | `test_agent_assembly.py`、`test_agent_cron.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_character_artist.py`、`test_builtin_cron.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_hook_runtime.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_tool_discover.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_toolrun.py`、`test_vision_privacy.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.workspace` | 工具 workspace 上下文与路径解析。 | PathResolveOptions, ResolvedPath, WorkspaceAgentNoticeStore… | 229 | `test_builtin_files.py`、`test_toolrun.py` |
| `atri_x.toolrun` | 工具解析、风险裁决、执行与批量编排。 | AtriwispInjection, AtriwispToolDeclaration, BACKGROUND_DISA… | 97 | `_atrinis_support.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_runtime_agent.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_llm.py`、`test_toolrun.py`、`test_toolrun_atriwisp_args.py`、`test_toolrun_denied.py` |
| `atri_x.toolrun.atriwisp` | Atriwisp 注入的工具声明 → ``CachedTool``。 | ATRIWISP_TOOL_NAME_REPLACER, AtriwispInjection, AtriwispToo… | 101 | `test_toolrun.py` |
| `atri_x.toolrun.atriwisp_exec` | Atriwisp 工具的 HTTP 执行。 | ATRIWISP_MAX_RESPONSE_BYTES, DEFAULT_ATRIWISP_TIMEOUT_SECON… | 261 | `test_toolrun.py`、`test_toolrun_atriwisp_args.py` |
| `atri_x.toolrun.availability` | 工具在上下文中的可用性与不可用原因。 | AvailableInContext, REASON_FOREGROUND_ONLY, REASON_UNAVAILA… | 45 | `test_toolrun.py` |
| `atri_x.toolrun.cache` | 工具发现的缓存（``CachedTool`` 列表的编解码与合并）。 | Cache, CachedTool, DecodeCache, EncodeCache, MergeCachedToo… | 205 | `test_toolrun.py` |
| `atri_x.toolrun.native` | 原生工具来源：把 ``tool.Registry`` 适配成 toolrun 的来源。 | DISCOVER_TOOL_NAME, NativeSource, new_native_source | 76 | `test_toolrun.py` |
| `atri_x.toolrun.preview` | 工具调用进度预览的默认文案与渲染（Python 侧新增，无 Go 对应）。 | DEFAULT_FAILURE_TEMPLATE, DEFAULT_PREVIEW_TEXT, DEFAULT_SEA… | 189 | `test_toolrun.py` |
| `atri_x.toolrun.runner` | 一个 assistant 回合里整批工具调用的编排。 | ConfirmResult, MAX_PREVIEW_RUNES, Run, RunRequest, RunResul… | 782 | `test_agent_cron.py`、`test_toolrun.py`、`test_toolrun_denied.py` |
| `atri_x.toolrun.toolrun` | 工具解析、风险裁决与执行的统一入口。 | Context, ExecutionResult, Manager, ResolvedTool, actor_for_… | 469 | `test_agent_cron.py`、`test_agent_risk.py`、`test_app_runtime_agent.py`、`test_toolrun.py` |
| `atri_x.turn` | 单个 Session 的 turn 阶段、pending 追加与确认状态。 | — | 31 | `agent_commands_fakes.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_risk.py`、`test_turn_manager.py` |
| `atri_x.turn.manager` | 单个 Session 的 turn 阶段、原始输入、pending 追加与风险确认。 | Input, Manager, Part, Phase, PhaseAwaitAppendConfirm, Phase… | 688 | `test_agent_context_runtime.py` |
| `atri_x.utils` | 通用工具包。 | fileops, runes | 14 | `agent_commands_fakes.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_files.py`、`test_fileops.py` |
| `atri_x.utils.fileops` | 文件生命周期、编码、原子编辑、匹配、diff 与按 Session 的回滚。 | — | 98 | `agent_commands_fakes.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_files.py`、`test_fileops.py` |
| `atri_x.utils.fileops.atomic_write` | 原子写文件。 | atomic_write_file, replace_file_atomic | 106 | — |
| `atri_x.utils.fileops.diff` | unified diff 渲染（LCS + hunk 切分）。 | DiffHunk, MAX_DIFF_CELLS, MAX_DIFF_OUTPUT_BYTES, build_diff… | 209 | — |
| `atri_x.utils.fileops.edit` | 原子编辑：解析操作、在**原文**上定位目标、按位置从后往前套用。 | EDIT_OPERATIONS, Edit, SourceLine, apply_edits, edit_operat… | 406 | — |
| `atri_x.utils.fileops.encoding` | 字节与文本之间的编码转换。 | Codec, decode_bytes, decode_with_encoding, encode_text, is_… | 152 | — |
| `atri_x.utils.fileops.file` | 文件读取、按 revision 守卫的编辑与写回。 | EditFileOptions, EditResult, File, MAX_FILE_SIZE, edit_file… | 288 | — |
| `atri_x.utils.fileops.match` | 编辑目标的匹配与候选展示。 | MATCH_PREVIEW_LIMIT, MATCH_PREVIEW_WIDTH, MatchLocation, So… | 182 | — |
| `atri_x.utils.fileops.rollback` | 进程内、按 Session 的编辑前内容备份（rollback）。 | ROLLBACK_MAX_BYTES, ROLLBACK_MAX_RECORDS, RollbackInfo, Rol… | 239 | — |
| `atri_x.utils.fileops.text` | 文本规范化、行号与内容 revision。 | CONTENT_REVISION_BYTES, DEFAULT_READ_LINE_LIMIT, LineNumber… | 186 | — |
| `atri_x.utils.runes` | rune（码点）语义与终端显示宽度。 | Condition, fill_left, fill_right, is_ambiguous, is_wide, ru… | 226 | — |
| `atri_x.vision` | 视觉（图片转文本）引擎。 | AttributeDescriptionTemplateVersion, CacheIdentity, CacheSc… | 269 | `test_app_tool_runtime_wiring.py`、`test_builtin_imagegen.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_identity.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.vision.analysis` | 结构化图像分析结果的解析与限幅。 | IDENTITY_CONFIDENCE_HIGH, IDENTITY_CONFIDENCE_LOW, IDENTITY… | 384 | — |
| `atri_x.vision.cache` | 进程内缓存与在途任务合并。 | DefaultCacheMaxEntries, DefaultCacheTTL, DefaultMaxCacheVal… | 463 | — |
| `atri_x.vision.counters` | 进程内计数器。 | Counters, Snapshot | 90 | — |
| `atri_x.vision.failures` | 识图失败的安全类别。 | FAILURE_CANCELLED, FAILURE_DOWNLOAD_FAILED, FAILURE_EMPTY_R… | 159 | `test_vision_privacy.py` |
| `atri_x.vision.fingerprint` | 缓存身份与请求指纹。 | CacheIdentity, CacheSchemaVersion, RequestFingerprint, hash… | 184 | — |
| `atri_x.vision.identity` | 以图搜图兜底（SauceNAO）：只在「画面属性确定不了身份」时补一条外部参考。 | DEFAULT_ENDPOINT, DEFAULT_MAX_CANDIDATES, DailyQuota, IDENT… | 506 | — |
| `atri_x.vision.metrics` | 视觉服务的轻量观测接口。 | Metrics, NoopMetrics, Snapshotter | 68 | — |
| `atri_x.vision.prechat` | 两段式识图的前置描述：把图片段换成「画面属性」文本。 | DEFAULT_PRECHAT_MAX_IMAGES, DEFAULT_PRECHAT_PARALLELISM, DE… | 422 | — |
| `atri_x.vision.privacy` | 识图隐私开关：图片**要不要外发**给视觉/上游模型。 | IMAGES_NOT_SENT_TEXT, PrivacySettings, blocked_reason, conf… | 116 | — |
| `atri_x.vision.prompt` | 视觉 prompt 模板与文本清洗。 | AttributeDescriptionTemplateVersion, ChatDescriptionTemplat… | 223 | — |
| `atri_x.vision.service` | 视觉服务：用配置好的视觉 LLM 把图片转成文本。 | ChatDescriptionTemplateVersion, DefaultMaxConcurrentJobs, D… | 540 | — |
| `atri_x.vision.service_types` | 视觉服务的输入/输出/配置类型。 | DefaultDetail, DefaultIdentityApiKeyEnv, DefaultIdentityDai… | 231 | — |
