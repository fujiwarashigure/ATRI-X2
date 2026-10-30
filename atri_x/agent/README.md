<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/agent/` — Agent 对话编排

## 这个域负责什么

一轮对话的全部编排：取上下文 → 调模型 → 跑工具 → 落库 → 生成输出。

**不做什么**：不放平台协议细节（那在 `platform/`）、不直接发消息（那在 `delivery/`）。

**入口**：`atri_x/agent/core.py`（Agent 装配与主流程）、`atri_x/agent/chat.py`。

**配置段**：`[agent]`、`[agent.dsh]`

## 易错点

- 改系统提示词装配时注意来源覆盖顺序（群友人设 > 角色卡 > SOUL.md），1.1.2 修过一个「装配期覆盖」的真 bug。
- 工具轮次用 `limits.max_rounds_per_turn`；调大只会更慢更贵。
- 命令层（`agent/commands/`）只做参数解析与回执，业务逻辑应在服务层。

## 模块清单（自动生成）

共 **148** 个模块，被测试覆盖 **89** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.agent` | ``atri_x.agent``：Agent 核心、回合流程、事件、收件箱与外围能力。 | 389 | `agent_commands_fakes.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_chat_anomaly.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_group_policy.py`、`test_agent_inbound_media.py`、`test_agent_model.py`、`test_agent_participation.py`、`test_agent_prompt.py`、`test_agent_reactions.py`、`test_agent_risk.py`、`test_agent_segments.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_agent_wakeup.py`、`test_app_runtime_agent.py`、`test_builtin_runtime_resources.py`、`test_dsh_acp_client.py`、`test_dsh_route.py`、`test_hook_runtime.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_promptlib.py`、`test_request_manager.py`、`test_safety_integration.py`、`test_security_grants.py`、`test_session_naming_contract.py`、`test_time_context.py`、`test_toolrun.py`、`test_usage_attribution.py`、`test_vision_prechat.py`、`test_vision_privacy.py` |
| `atri_x.agent.admission` | 计算静默：静默时段里"哪些模型调用允许发生"的唯一判定入口。 | 224 | `test_agent_admission.py` |
| `atri_x.agent.asr` | 入站语音转写（ASR）。 | 355 | — |
| `atri_x.agent.bootstrap` | Agent 的可选服务装配入口（幂等）。 | 576 | `test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_commands_integration.py`、`test_app_runtime_agent.py` |
| `atri_x.agent.budget` | 每日计量账本（对外统一入口）。 | 79 | `test_agent_budget.py` |
| `atri_x.agent.budget_chat` | 聊天预算的 Agent 方法 + 次数限额规格 + 预算服务装配。 | 459 | — |
| `atri_x.agent.budget_keys` | 每日计量账本的键、裁剪、计数与请求结构。 | 354 | — |
| `atri_x.agent.budget_ledger` | 每日计量账本本体：原子预留、执行幂等、用量与状态桥。 | 701 | — |
| `atri_x.agent.call_reason` | 一次 LLM 调用的**原因**标签（Python 版新增）。 | 89 | `test_agent_admission.py`、`test_usage_attribution.py` |
| `atri_x.agent.character_directive` | ``@char:`` / ``@c:`` 单回合角色声明的解析与应用。 | 145 | `test_agent_assembly.py` |
| `atri_x.agent.character_guide_prompt` | 人设卡操作指引（系统提示词来源）。 | 48 | — |
| `atri_x.agent.chat` | 回合主流程：从入站文本到助手消息落库。 | 412 | `test_agent_chat.py`、`test_agent_core.py` |
| `atri_x.agent.chat_anomaly` | 上游把工具调用写成正文时的补救与清洗。 | 157 | `test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_llm` | 单次 LLM 调用：请求构造、流式读取、用量与错误审计。 | 611 | `test_agent_chat.py`、`test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_observe` | 模型调用的日志、审计与上下文助手。 | 260 | `test_usage_attribution.py` |
| `atri_x.agent.chat_request_options` | LLM 请求的临时参数（``LLMRequestOptions``）与其合并/解析。 | 79 | — |
| `atri_x.agent.chat_stream` | 模型流式调用的叶子助手（打开流、并发槽、记账结算、媒体补全）。 | 154 | — |
| `atri_x.agent.chat_tools` | 回合里的工具批次：待发用户消息、工具执行、自动确认与 schema 过滤。 | 747 | `test_agent_chat.py`、`test_agent_core.py`、`test_security_grants.py` |
| `atri_x.agent.chat_turn` | 一轮对话的主体循环。 | 625 | `test_agent_chat.py` |
| `atri_x.agent.chat_vision` | 图片不受支持时的透明兜底。 | 86 | — |
| `atri_x.agent.command_runtime` | Slash 命令的执行与权限裁决。 | 243 | — |
| `atri_x.agent.commands` | ``internal/agent/commands`` 的 Python 移植：slash 命令族与注册入口。 | 539 | `agent_commands_fakes.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_commands.py`、`test_agent_commands_integration.py`、`test_agent_commands_services.py`、`test_builtin_runtime_resources.py`、`test_security_grants.py`、`test_usage_attribution.py` |
| `atri_x.agent.commands.approval` | ``/grant`` 与 ``/panic``：授权生命周期的手动入口。 | 186 | `test_security_grants.py` |
| `atri_x.agent.commands.character` | ``/chars``：列出角色素材库，或重建角色索引。 | 118 | — |
| `atri_x.agent.commands.compact` | ``/compact``：手动压缩当前会话上下文。 | 50 | — |
| `atri_x.agent.commands.completion` | 命令参数补全的公共助手。 | 212 | — |
| `atri_x.agent.commands.context_policy` | ``/overflow``（别名 ``/ctxpolicy``、``/longmsg``）：长消息溢出策略。 | 103 | — |
| `atri_x.agent.commands.deps` | 命令注册表、依赖容器与服务协议。 | 442 | `test_agent_assembly.py`、`test_agent_commands_integration.py`、`test_builtin_runtime_resources.py`、`test_security_grants.py` |
| `atri_x.agent.commands.doctor` | ``/doctor``：只读配置检查。 | 55 | — |
| `atri_x.agent.commands.group_knowledge` | ``/faq``（别名 ``/knowledge``、``/kb``、``/groupkb``）：群确定性知识库。 | 293 | — |
| `atri_x.agent.commands.group_policy` | ``/grouppolicy``（别名 ``/gpolicy``、``/grouppolicies``）：当前群的策略配置。 | 109 | — |
| `atri_x.agent.commands.group_services` | ``/remind``、``/poll``、``/vote``、``/signup``、``/join``：群确定性服务。 | 323 | — |
| `atri_x.agent.commands.help` | ``/help`` 命令与帮助文本渲染。 | 175 | — |
| `atri_x.agent.commands.hook` | ``/hooks``：列出/查看 hook、重载配置、控制有状态 hook。 | 355 | — |
| `atri_x.agent.commands.log` | ``/log``、``/audit``、``/atriwisp``、``/usage`` 的注册与派发。 | 374 | — |
| `atri_x.agent.commands.log_args` | 日志类命令的参数解析（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 | 422 | — |
| `atri_x.agent.commands.log_format` | 日志类命令的输出渲染（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 | 248 | — |
| `atri_x.agent.commands.member_panel` | ``/me``（别名 ``/mytasks``、``/my``、``/mypanel``）：普通成员自助面板。 | 63 | — |
| `atri_x.agent.commands.memory_common` | 长期记忆命令的共享助手。 | 195 | — |
| `atri_x.agent.commands.memory_forget` | ``/forget``（别名 ``/forget_memory``、``/delete_memory``）：删除长期/常驻记忆。 | 220 | — |
| `atri_x.agent.commands.memory_learning` | ``/memory``（别名 ``/angel_memory``）与 ``/learning``（别名 ``/selflearning``）。 | 443 | — |
| `atri_x.agent.commands.model` | ``/model`` 与 ``/checkmodel``（别名 ``/models``）。 | 560 | — |
| `atri_x.agent.commands.model_types` | 模型服务的共享数据类型与形状。 | 147 | `test_agent_bootstrap.py`、`test_agent_commands_integration.py` |
| `atri_x.agent.commands.request` | ``/requests``、``/stop``、``/stopall`` 与请求树渲染。 | 375 | — |
| `atri_x.agent.commands.research` | ``/research``：科研研究助理的只读操作面板命令。 | 308 | — |
| `atri_x.agent.commands.resources` | ``/resources``：超管直接查看本机与 ATRI-X 进程的硬件资源占用。 | 74 | `test_builtin_runtime_resources.py` |
| `atri_x.agent.commands.rollback` | ``/rollback``：列出并撤销 ATRI-X 自己对文件的最新一次编辑。 | 109 | — |
| `atri_x.agent.commands.session_core` | ``/new``、``/status``、``/work``、``/chat``。 | 266 | — |
| `atri_x.agent.commands.session_format` | Session 列表/消息的查询与文本渲染。 | 366 | — |
| `atri_x.agent.commands.session_lifecycle` | Session 生命周期命令：``/archive`` 系列、``/rename``、``/delete``、``/clean``。 | 523 | — |
| `atri_x.agent.commands.session_module` | ``SessionCommandState`` 与 ``SessionModule``。 | 138 | — |
| `atri_x.agent.commands.session_navigation` | ``/sessions``、``/archives``、``/resume``、``/messages``、``/fork``。 | 270 | — |
| `atri_x.agent.commands.state` | ``/state``：``state.toml`` 热加载状态与手动重载。 | 164 | — |
| `atri_x.agent.commands.time` | ``/time``：查看统一时间服务状态，手动触发一次网络校验，列出可用时区。 | 221 | — |
| `atri_x.agent.commands.tool` | ``/tools``：查看工具与当前群的工具授权，群主/管理员可在群里直接授权。 | 342 | — |
| `atri_x.agent.commands.usage` | ``/usage``：从审计日志聚合 token 用量。 | 451 | `test_agent_commands_services.py`、`test_usage_attribution.py` |
| `atri_x.agent.completion` | 结构化补全服务对外的两个门面。 | 28 | — |
| `atri_x.agent.context_compact` | 上下文压缩：``/compact`` 的会话切换、摘要 metadata 与状态。 | 524 | `test_agent_context_runtime.py` |
| `atri_x.agent.context_policy` | 长消息保护（上下文溢出策略）：判定、截断/摘要/拒绝与群级覆盖。 | 532 | `test_agent_context_policy.py`、`test_agent_group_policy.py` |
| `atri_x.agent.context_runtime` | 上下文运行态：窗口/预算解析、压缩后端选择、加载器与最近用量。 | 557 | `test_agent_context_policy.py`、`test_agent_context_runtime.py` |
| `atri_x.agent.context_seed` | 压缩种子（``context_compact``）的消费。 | 113 | `test_agent_context_runtime.py` |
| `atri_x.agent.context_usage` | 上下文用量：``/context`` 状态文本、最近用量持久化、协议归属记录、压缩触发判定。 | 340 | `test_agent_context_policy.py` |
| `atri_x.agent.core` | Agent 核心：状态、构造与跨模块方法的薄转发。 | 995 | `test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_core.py`、`test_request_manager.py` |
| `atri_x.agent.core_build` | Agent 构造期的支撑件：默认配置、模型运行态、构造入口。 | 225 | — |
| `atri_x.agent.core_forward` | ``Agent`` 的薄转发方法（唤醒 / 消息 / 聊天 / 模型 / 工具 / 完成）。 | 283 | — |
| `atri_x.agent.cron` | 后台运行（cron / Atrinis 共用的 ``RunBackground``）与会话预处理。 | 605 | `test_agent_admission.py`、`test_agent_cron.py`、`test_agent_cron_background.py` |
| `atri_x.agent.cron_background` | 后台任务的工具 / Skill 预载与 Session 元数据（Agent 侧）。 | 563 | — |
| `atri_x.agent.cron_tools` | 后台任务里 shell 工具的风险裁决（Agent 侧）。对应 Go：``internal/agent/cron_tools.go``。 | 163 | `test_agent_cron.py` |
| `atri_x.agent.dsh` | ``atri_x.agent.dsh``：ATRI-X 编排 DSH 的纯标准库 ACP 客户端。 | 51 | `test_dsh_acp_client.py`、`test_dsh_route.py` |
| `atri_x.agent.dsh.client` | ``atri_x.agent.dsh.client``：DSH ACP 客户端（会话语义与 prompt 生命周期）。 | 423 | — |
| `atri_x.agent.dsh.command` | DSH ACP 启动命令的解析（冒烟与路由共用，**唯一出处**）。 | 61 | — |
| `atri_x.agent.dsh.handoff` | 上下文交接：把 ATRI-X 的对话拼成 DSH 的一次性 prompt，并把 ACP 用量折算成账本口径。 | 163 | `test_dsh_route.py` |
| `atri_x.agent.dsh.route_call` | 把「路由判定」接到对话链路上：预算、执行、流式回传、熔断与失败回退。 | 596 | `test_dsh_route.py` |
| `atri_x.agent.dsh.router` | DSH 深度路由：任务特征打分 + 成本闸门 + 熔断。 | 483 | `test_dsh_route.py` |
| `atri_x.agent.dsh.runtime` | DSH 运行时的进程与并发管理：长驻 AcpClient + 每次路由一个一次性会话。 | 425 | `test_dsh_route.py` |
| `atri_x.agent.dsh.smoke` | ``python -m atri_x.agent.dsh.smoke``：DSH ACP 最小闭环冒烟。 | 196 | `test_dsh_acp_client.py` |
| `atri_x.agent.dsh.status` | ``python -m atri_x.agent.dsh.status``：DSH 内核与深度路由的状态自检。 | 172 | `test_dsh_route.py` |
| `atri_x.agent.dsh.transport` | ``atri_x.agent.dsh.transport``：ACP 子进程的 NDJSON stdio JSON-RPC 传输。 | 376 | — |
| `atri_x.agent.dsh.types` | ``atri_x.agent.dsh`` 的协议类型、常量与解析函数。 | 295 | `test_dsh_acp_client.py`、`test_dsh_route.py` |
| `atri_x.agent.errors` | 面向用户的错误呈现与"已通知用户"标记。 | 145 | `test_agent_chat.py` |
| `atri_x.agent.events` | 平台生命周期事件的确定性处理（不调用模型）。 | 466 | — |
| `atri_x.agent.group_knowledge` | 群知识库（确定性 FAQ）：命中即本地回答，不进模型。 | 563 | `test_agent_group_policy.py` |
| `atri_x.agent.group_policy` | 群级策略：状态快照、视图、模型目录与授权闸门。 | 834 | `agent_fakes.py`、`test_agent_group_policy.py` |
| `atri_x.agent.group_policy_rules` | 群策略字段的规范化与校验规则。 | 536 | `test_agent_group_policy.py` |
| `atri_x.agent.group_runtime` | 群运行态状态机：禁言 / 退群 / 不可用。 | 533 | `test_agent_bootstrap.py`、`test_agent_group_policy.py` |
| `atri_x.agent.group_services_polls` | 群投票：创建 / 列表 / 查看 / 投票 / 关闭。 | 316 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_reminders` | 群提醒：创建 / 列表 / 删除，以及到点派发。 | 479 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_signups` | 群报名：创建 / 列表 / 查看 / 加入 / 退出 / 关闭。 | 345 | `test_agent_group_policy.py` |
| `atri_x.agent.group_services_state` | 群服务（提醒 / 投票 / 报名）的快照、开关与落库。 | 661 | `test_agent_core.py`、`test_agent_group_policy.py` |
| `atri_x.agent.hooks` | Hook 运行、路由与失败通知。 | 389 | `test_hook_runtime.py` |
| `atri_x.agent.identity` | 身份、作用域与对话元数据推导。 | 191 | — |
| `atri_x.agent.inbound_media` | 入站平台媒体的本地化。 | 214 | `test_agent_inbound_media.py` |
| `atri_x.agent.inbound_speaker` | 发言人身份：回合输入的快照、显示名与群共享线程标记。 | 179 | — |
| `atri_x.agent.inbox` | 会话收件箱：串行化一个 Session 的普通回合，并合并静默窗口内的连续消息。 | 507 | — |
| `atri_x.agent.input` | 入站输入的准入、确认与回合切换。 | 587 | `test_agent_risk.py` |
| `atri_x.agent.logging` | Agent 的日志与审计通道。 | 141 | — |
| `atri_x.agent.media_output` | 媒体输出的准备、解析与回执缓存。 | 218 | — |
| `atri_x.agent.member_panel` | 成员自助面板：任务与额度视图。 | 494 | — |
| `atri_x.agent.member_persona_prompt` | 成员专属人设的系统提示词来源。 | 73 | `test_member_persona.py` |
| `atri_x.agent.member_profile_prompt` | 群友档案提示词来源：把**当前发言人**的档案注入系统提示词。 | 72 | `test_member_profile.py` |
| `atri_x.agent.memory_learning_admin` | 后台记忆/学习的管理查询门面。 | 47 | — |
| `atri_x.agent.message` | 入站消息总入口：命令派发与聊天。 | 257 | `test_safety_integration.py` |
| `atri_x.agent.model` | 模型运行时状态、模式模型选择、客户端查找与重试通知。 | 484 | `test_agent_bootstrap.py`、`test_agent_context_runtime.py`、`test_agent_model.py` |
| `atri_x.agent.model_agent` | ``ModelMixin``：``internal/agent/model.go`` 里挂在 ``*Agent`` 上的模型方法。 | 428 | `test_agent_context_runtime.py`、`test_agent_model.py` |
| `atri_x.agent.model_list` | 模型列表：缓存、并发合并、按 provider 聚合与命名模型档。 | 448 | `test_agent_model.py` |
| `atri_x.agent.model_policy` | 执行前的模型授权（群模型目录）。 | 102 | `test_agent_model.py` |
| `atri_x.agent.model_service` | 模型服务的门面与装配（从 ``model.py`` 尾部拆出）。 | 189 | `test_agent_bootstrap.py`、`test_agent_model.py` |
| `atri_x.agent.model_snapshot` | 命名模型快照（``/model --save`` / ``--apply`` / ``--delete``）。 | 337 | `test_agent_model.py` |
| `atri_x.agent.model_state` | 运行态（``state.toml``）的启动恢复、热加载与回写。 | 323 | `test_agent_bootstrap.py`、`test_agent_model.py` |
| `atri_x.agent.notice_queue` | 按平台的通知发送队列与背压。 | 232 | `test_agent_core.py` |
| `atri_x.agent.ops` | 运维可见的请求快照。 | 22 | — |
| `atri_x.agent.options` | 构造期依赖与配置容器，以及若干 setter。 | 239 | `test_agent_core.py` |
| `atri_x.agent.output` | 出站：发送管理器的装配、部分失败记账与平台消息映射。 | 408 | `test_safety_integration.py` |
| `atri_x.agent.participation` | 群聊参与状态机 MVP（评估 §D）：只做**抑制**，不主动找话题。 | 345 | `test_agent_participation.py` |
| `atri_x.agent.platform_signals` | 平台连接信号的订阅与按平台执行队列。 | 249 | — |
| `atri_x.agent.prefix` | 命令前缀的读取与取消命令识别。 | 47 | — |
| `atri_x.agent.prompt` | 提示词构造：SOUL 提供者、工具名/schema 提供者、``PromptBuilder``。 | 425 | `test_agent_prompt.py`、`test_member_profile.py` |
| `atri_x.agent.ratelimit` | 入站限流：用户桶 + 群桶、计数与可观测状态。 | 275 | `test_agent_bootstrap.py`、`test_agent_directives.py` |
| `atri_x.agent.reactions` | 表情反应层与 Agent 回合输出的接线（Python 侧新增，无 Go 对应）。 | 164 | `test_agent_chat.py`、`test_agent_reactions.py` |
| `atri_x.agent.request_context` | 回合请求 ID 在 ctx 上的读写。 | 32 | `test_agent_core.py` |
| `atri_x.agent.risk_confirmation` | 高风险工具确认的文案与指令识别。对应 Go：``internal/agent/risk_confirmation.go``。 | 366 | `test_agent_risk.py` |
| `atri_x.agent.seams` | Agent 的可选能力接缝（seam）。 | 595 | `test_agent_bootstrap.py` |
| `atri_x.agent.segments` | 入站消息段在 ctx / LLM / 平台三种表示之间的搬运。 | 299 | `test_agent_segments.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.session_metadata` | Session metadata 的编解码。 | 339 | `test_agent_core.py` |
| `atri_x.agent.state_watch` | ``state.toml`` 的外部编辑热加载与状态查询。 | 223 | — |
| `atri_x.agent.status` | 运行状态快照的记录与发布。 | 192 | `test_agent_assembly.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.system_prompt` | 系统提示词管理器：按来源收集分段内容并拼接。 | 112 | `test_agent_prompt.py`、`test_member_persona.py`、`test_member_profile.py` |
| `atri_x.agent.system_prompt_sources` | 系统提示词的具体来源：对话元信息、SOUL、常驻记忆、工具名清单。 | 246 | `test_agent_prompt.py`、`test_agent_reactions.py`、`test_memory_notes.py` |
| `atri_x.agent.time_context` | 请求级时间上下文：入站消息时间基准 + 系统提示里的时间片段。 | 357 | `test_time_context.py` |
| `atri_x.agent.title` | 会话标题生成器。 | 164 | `test_agent_title.py`、`test_session_naming_contract.py` |
| `atri_x.agent.tool_auth` | 群级工具授权与执行闸门。对应 Go：``internal/agent/tool_auth.go``。 | 379 | `test_agent_group_policy.py`、`test_agent_risk.py`、`test_agent_tools.py` |
| `atri_x.agent.tool_cache` | 会话级工具缓存（发现结果的持久化）。对应 Go：``internal/agent/tool_cache.go``。 | 341 | `test_agent_tools.py` |
| `atri_x.agent.tool_directive` | ``@tool:`` / ``@skill:`` 指令：预载工具与 Skill。 | 691 | `test_agent_directives.py` |
| `atri_x.agent.tool_directive_skill` | ``@skill:`` 指令：Skill 详情注入与包装工具预载。 | 329 | `test_agent_directives.py` |
| `atri_x.agent.tool_schemas` | 工具 schema 装配与日志正文预览。 | 244 | `test_agent_assembly.py` |
| `atri_x.agent.tool_tag_config` | ``tool_tags.toml``：标签 → 工具集合 + 提示词，以及按标签取工具名。 | 372 | `test_agent_directives.py`、`test_agent_prompt.py` |
| `atri_x.agent.tool_transcript` | 工具调用在会话记录里的落库形态。对应 Go：``internal/agent/tool_transcript.go``。 | 476 | `test_agent_chat.py`、`test_agent_tools.py` |
| `atri_x.agent.toolrun_adapter` | 工具运行适配层：把 ``Agent`` 接到 ``atri_x.toolrun`` 的 Runner 上。 | 980 | `test_agent_risk.py`、`test_safety_integration.py`、`test_toolrun.py` |
| `atri_x.agent.toolrun_prompt_provider` | 默认的工具 schema / 名单提供者（Agent 侧适配）。对应 Go：``internal/agent/toolrun_prompt_provider.… | 88 | `test_agent_tools.py` |
| `atri_x.agent.tools` | 工具运行时状态装配。对应 Go：``internal/agent/tools.go``。 | 525 | `test_agent_assembly.py`、`test_agent_risk.py`、`test_agent_tools.py` |
| `atri_x.agent.tools_service` | ``agent.services["tools"]`` / ``command_deps.tools`` 的装配对象。 | 60 | — |
| `atri_x.agent.turn_gate` | 回合输出的"最后一公里"闸门。 | 75 | — |
| `atri_x.agent.turn_output` | 回合输出端口：前台流式输出与后台静默输出。 | 273 | `test_agent_chat.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.agent.turn_override` | 单回合指令：``@model:`` / ``@image:`` / ``@use:`` 的解析、裁决与剥离。 | 337 | `test_agent_assembly.py`、`test_agent_chat.py` |
| `atri_x.agent.user_prompt_source` | 用户自建提示词的系统提示词来源。 | 42 | `test_promptlib.py` |
| `atri_x.agent.vision` | 多图兜底：把图片段换成模型生成的文字描述。 | 518 | `test_vision_prechat.py`、`test_vision_privacy.py` |
| `atri_x.agent.wakeup` | 唤醒判定：群聊里这条消息要不要叫醒模型，以及唤醒词的剥离。 | 262 | `test_agent_participation.py`、`test_agent_wakeup.py` |
| `atri_x.agent.workspace` | 会话工作区目录的持久化适配（``tool.WorkspaceStore`` 的实现）。 | 154 | `test_agent_directives.py`、`test_toolrun.py` |

## 对应上游 Go（自动生成）

`internal/agent/asr.go`、`internal/agent/budget.go`、`internal/agent/character_directive.go`、`internal/agent/chat.go`、`internal/agent/chat_llm.go`、`internal/agent/chat_tools.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 825 个 / 超线文件 79 个）。
