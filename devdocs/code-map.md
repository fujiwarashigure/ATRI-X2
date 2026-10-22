# ATRI-X 代码地图

本文用于快速定位“某类任务应该看哪些文件”。需要理解调用链路时，读 `devdocs/architecture.md`。

定位方法：

```bash
rg -n "locator:tool" devdocs/code-map.md
```

原则：

- 同一职责集中在目录内时，只写目录。
- 只有核心入口、跨目录桥接、容易混淆或独特文件才单独列出。
- 每节只写入口和边界，不展开完整架构说明。

<!-- locator:startup -->
## 启动、运行模式与装配

适用任务：命令行入口、运行模式判断、app 层依赖装配、远程 CLI client/service 入口。

先看：

- `atri_x/__main__.py`：程序入口。
- `atri_x/launcher/cli.py`：命令行解析和补全生成。
- `atri_x/app/app.py`、`atri_x/app/runner.py`、`atri_x/app/dependencies.py`：稳定启动入口、分阶段 Runner 和可替换依赖组。
- `atri_x/app/foundation.py`、`atri_x/app/models.py`、`atri_x/app/runtime.py`：配置/存储基础设施、模型客户端，以及 Cron/Tool/Hook/Agent 核心装配。`atri_x/app/runtime.py` 在 Agent 就绪前把 Hook 加载告警攒进 `RuntimeComponents.HookNotices`（真实平台发送器要等 `Integrations.Attach` 才注册），由 `atri_x/app/runner.py` 在 Attach 之后用 `atri_x/app/platforms.py` 的 `deliverHookNotices` 发到 `hookNoticeTarget` 选出的落地平台超管，无可用平台时退回日志。
- `atri_x/app/image_rewriter.py`、`atri_x/app/image_to_prompt.py`：把 `image_generate` 的低成本改写模型和 `image_to_prompt` 的视觉模型接到已有 `ModelClients`，再以接口形式注入 builtin 工具；`image_to_prompt` 显式启用但 provider/model 有误时在启动阶段报错。改写器实例是共享的，所以目标模型与提示词方言随 `ImagePromptRewriteRequest` 逐次传入，由 `imageRewriteSystemFor` 在"自然语言长句"与"逗号分隔标签、禁止输出权重表达式"两套系统提示词之间选择。
- `atri_x/imagegen/client.py`、`atri_x/imagegen/prompt.py`、`atri_x/imagegen/optimize.py`、`atri_x/imagegen/library.py`：OpenAI 兼容生图客户端与提示词组装。`atri_x/imagegen/client.py` 的 `Config.Normalize()` 会剔除 `extra_payload` / `extra_headers` 里试图覆盖 ATRI-X 自建字段的键（结果记在 `DroppedExtraPayload` / `DroppedExtraHeaders`），负向提示词按 `NegativePromptMode` 决定走独立字段、折进正向提示词还是丢弃。
- `atri_x/app/vision.py`、`atri_x/agent/vision.py`、`atri_x/vision/`：自动视觉兜底。`[vision]` 段显式开启后，`agent.callLLM` 在 `APIError.Category == vision_unsupported` 时调用 `VisionDescriber` 把图片段替换成文字描述（有界并行 + 整批时间预算），失败则降级回文本引用；`atri_x/vision` 是 `image_to_prompt` 工具与兜底共用的唯一描述引擎（预处理 + 流式调用 + 版本化指纹缓存 + 成功/负缓存 + 同 key 合并 + 运行器/等待者上限 + `Stats()` 接入 `/metrics` + panic 隔离），不反向依赖 `atri_x/media`。`atri_x/llm/openai` 的 `parseError` 负责把上游错误映射为 `APIError.Category`，`shouldFallbackVision` 只读该字段。
- `atri_x/app/asr.py`、`atri_x/agent/asr.py`、`atri_x/asr/`：可选语音转写。`[asr]` 显式开启后，`HandleMessage` 在已唤醒的入站消息上调用 `AudioTranscriber` 把语音段替换成 `[语音 N 自动转写（可能有误）：...]` 文本段；`atri_x/asr` 负责 OpenAI 兼容 multipart 上传、有界并发/队列、录音大小与响应上限、成功/负缓存和重试；预算按 `asr` kind 预占，受全局、群和群内单用户四级每日额度约束。
- `atri_x/llm/errors.py`：结构化 `APIError`（`StatusCode`/`Code`/`Type`/`Param`/`Message`/`Cause`）与确定性失败分类；`openai` 适配器的 `parseError` 在此保留上游错误码，供视觉兜底判定和负缓存使用。
- `atri_x/app/platforms.py`、`atri_x/app/integrations.py`：平台运行、Atrinis 和平台能力接线；`runPlatforms` 维护"仍在运行的运行时"集合并区分非预期退出（记 WARN + 交给 `PlatformRunRequest.PlatformFailure` → health 标断开 + `notifyPlatformFailure` 通知其他平台超管）与交互式 CLI 退出（`StopAppOnExit`，非 service 模式才取消整轮运行），service 模式下全部运行时退出后仍等外部停止；同目录 `atri_x/app/service_marker.py` 用 PID 标记文件做服务单实例互斥，避免陈旧 PID 在容器重建后误判。

常用搜索：

```bash
rg -n "func Run|service run|completion|--client|RunCron" cmd atri_x/app atri_x/launcher/cli.py
```

<!-- locator:config -->
## 配置、资产与日志

适用任务：配置读取、默认配置资产、provider/state/tool_tags 合并、日志写入/轮转/读取。

先看：

- `atri_x/config/`
- `atri_x/logging/`：`atri_x/logging/logging.py` 是日志中心与三份按日文件（`atri-x-*` / `audit-*` / `atrinis-*`，审计下限至少 info）；`atri_x/logging/reader.py` 是 `/log`、`/audit`、`/atriwisp`、`/usage` 与维护报告共用的反向分块查询，读取 `LogEntry.Fields` 这种文本键值契约，`Query` 返回 `QueryResult{Entries, DamagedCount, DamagedLines}`——解析不出 `time`/`level`/`msg` 的行被跳过并计数（不占 `--limit`、不冒充记录，样本只在 `--raw` 查询里收集）；`atri_x/logging/contract.py` 是 fork 版"来源标识（`module`）+ 操作结果（`result`）+ 运行来源（`run_source`）"最小契约的唯一出处（`ModuleAgent` / `ModuleHook` / `ResultRejected` / `RunSourceCron` 等常量、`ValidLogModule` / `ValidLogResult` / `ValidRunSource`）；`atri_x/logging/redact_fallback.py` 是 `atri_x.redact` 取不到时的正则兜底脱敏、一次性告警与 `redact_status()` 自检快照（接 `/diagnostics.redaction` 与 doctor，研究报告 §5.2）。**日志文件格式与字段查询契约本轮不变**：上游的九字段 LogRecord 与 JSONL 落盘未实施。
- `atri_x/tool/availability.py`：`ToolAvailabilityReason` / `ToolAccessReason` / `RegistryToolAvailabilityReason` 把"工具为什么没被采用"拆成互斥的机器可读原因（`tool_not_found` / `tool_context_unavailable` / `tool_hidden` / `tool_requires_superadmin` / `tool_risk_above_allowed_level` / `tool_no_schema`），供预载与审计使用；判定顺序与 `CanAccessTool` / `InfoAvailableInContext` 一致，迁移调用点不得顺带改变准入。
- `docs/configuration.md`

常用搜索：

```bash
rg -n "ATRIX_CONFIG_FILE|services.toml|providers.toml|state.toml|tool_tags.toml|TextHandler|audit|LogModule|ResultRejected|ToolReason" atri_x/config atri_x/logging atri_x/tool docs/configuration.md
```

<!-- locator:health-ops -->
## 健康状态、限速、熔断与运维接口

适用任务：`/live`、`/ready`、`/healthz` 语义，watchdog 诊断，Provider 熔断/备用，群聊限速，`/tasks`、`/metrics` 和 ops token。

先看：

- `atri_x/health/`：健康状态、`/live`、`/ready`、`/healthz`、extra handler token；`atri_x/health/state.py` 区分进程存活、readiness、调度心跳、平台和模型状态。
- `atri_x/app/health.py`、`atri_x/app/ops_health.py`、`atri_x/app/health_llm.py`、`atri_x/app/health_handler.py`：启动健康接口、读取 `ATRIX_HEALTH_*` / `ATRIX_OPS_TOKEN` / 重启原因文件、组装 `/metrics`、`/diagnostics` 以及只读插件状态 `/plugins/memory`、`/plugins/learning`。
- `atri_x/app/doctor.py`、`atri_x/launcher/cli.py`、`atri_x/__main__.py`：`python -m atri_x doctor` 配置/端口/平台/模型验收；`platform_ok` 独立于 `config_ok`，`--require-platform` 要求平台状态存在，`--e2e` 使用唯一探测标记做 CLI 真实消息往返。
- `atri_x/character/store.py`、`atri_x/character/write.py`：角色/图片 `version`、`source` 与 `Manifest`/`WriteManifest` 备份清单。
- `atri_x/app/breaker_llm.py`、`atri_x/app/models.py`、`atri_x/llm/breaker.py`：Provider 熔断、`fallback_mode` / `fallback_on_error`、备用 Provider 和总超时；`atri_x/app/models.py` 的 `newProviderLLM` 按 `[providers.*].api_mode` / `model_configs.<model>.api_mode` 选择 chat 或 responses 适配器，混用两种协议时返回按请求模型分发的 `protocolRouter`。
- `atri_x/agent/ratelimit.py`、`atri_x/ops/ratelimit.py`：用户级/群级令牌桶叠加；阈值和拒绝原因进入 `/metrics.rate_limit`。
- `atri_x/processenv/environment.py`：Shell / Go Skill 子进程凭据变量过滤。
- `deploy/atri_x_watchdog.py`、`deploy/restore_verify.py`、`deploy/backup.py`：阈值/冷却/诊断脱敏、重启原因文件、sha256 备份清单和隔离恢复验证；在线 SQLite 模式先做数据库快照再复制媒体并按引用补齐，恢复结果区分 `passed` / `static_passed` / `static_passed_with_skips`，并在启动隔离实例前移除旧 PID 标记。
- `deploy/upgrade.py`、`deploy/rollback.py`：配置兼容性检查、源码版本守卫、按实际 image ID 保存旧镜像、升级前 stop 模式快照、隔离配置预检；回滚先载入旧镜像验证备份，重建后等待 `/ready` 并跑 `doctor`，失败时尝试恢复回滚前 data。
- `atri_x/agent/dsh/`、`deploy/Dockerfile` 的 `dsh-runtime` 阶段：ATRI-X 编排 DSH（DeepSeek Harness）的纯标准库 ACP 客户端（`types` 协议类型 / `transport` NDJSON JSON-RPC 传输 / `client` 会话与 prompt / `smoke` 闭环冒烟）；DSH 以 `dsh --profile acp` 的 stdio 子进程运行，状态根 `/data/dsh`，权限请求默认拒绝。路由阈值、上下文交接与预算记账尚未接入对话链路，设计见 `devdocs/dsh-acp.md`。
- `atri_x/redact/`：用户可见错误、Hook 失败、日志/audit 和健康快照共用的凭据脱敏与错误 ID；`atri_x/redact/redact.py` 是脱敏的唯一实现，`atri_x/health/` 只保留兼容入口；`tests/test_deploy_watchdog.py` 覆盖诊断包脱敏。

常用搜索：

```bash
rg -n "IsProcessLive|SchedulerKnown|ExtraHandlerToken|RateLimitStatus|UsesFallbackOnError|WithoutSensitiveKeys|RunDoctor|WriteManifest" internal
rg -n "ATRIX_OPS_TOKEN|WATCHDOG_OPS_TOKEN|restore-verify|fallback_mode|doctor|upgrade.py|rollback.py" docs deploy README.md
```

<!-- locator:agent-chat -->
## Agent 对话流程

适用任务：普通聊天主流程、LLM 调用、流式输出、Prompt、system prompt、工具 transcript、pending 输入、风险确认。

先看：

- `atri_x/agent/core.py`：Agent 状态和构造装配；构造参数集中在 `Options`。
- `atri_x/agent/message.py`：消息入口、slash/普通输入分发和用户错误通知。
- `atri_x/agent/command_runtime.py`：命令权限、Turn 冲突、通知和 continuation 的统一编排。
- `atri_x/agent/input.py`：普通输入预处理、命令 continuation、pending 和风险确认入口。
- `atri_x/agent/inbox.py`、`atri_x/agent/inbound_speaker.py`：群共享线程的有序 Session 队列与同一成员连续消息合并窗口；每条输入保留独立 Actor/Speaker 与平台消息钮，不同成员串行而不合并权限主体；共享会话的用户消息写入服务端生成的发言成员标记与 `speakers` 元数据。
- `atri_x/agent/segments.py`：平台入站 Segment 与 LLM Segment 转换。
- `atri_x/agent/inbound_media.py`：实际消费前的平台 resolver 与 Media Center 桥接、大小校验和不可用降级。
- `atri_x/tool/builtin/chat_history.py`：当前聊天历史查询与媒体位置/下载状态展示，查询不下载；`atri_x/tool/builtin/get_media.py`：显式选定媒体位置获取，仅返回文本 ID，单次最多 5 次未入库媒体获取尝试；`atri_x/tool/builtin/view_image.py`：把图片本身交给模型（`source` 的媒体 ID / HTTP(S) URL / 本地路径，或 `message_id` + `media_index` 取历史图片），声明 `tool.Info.VisionRequired`，本地路径限超级管理员。
- `atri_x/tool/capability.py`：请求级工具能力（当前只有 `Vision`），随 context 传播；`InfoAvailableInContext` 据此隐藏 `VisionRequired` 工具，agent 在 `runChat` / `handleSessionInput` / 后台预载入口用 `withToolCapabilities` 按当前 work 模型的 `vision` 声明注入。
- `atri_x/config/inspect.py`：只读配置体检（`UnknownAppConfigKeys` 用严格解码列出 `app.toml` 未消费的键；`BuiltinAssetDrift` 对比内置 Skill 文件与默认版本），由 `atri_x/app/config_check.py` 汇入警告；`atri_x/agent/commands/doctor.py` + `atri_x/app/doctor.py` 的 `doctorService` 提供仅超管的 `/doctor`（复用 `RunDoctor(SkipModel)`，无问题返回 `Everything is OK`）。
- `atri_x/toolrun/toolrun.py`：后台任务工具白名单 `BackgroundToolAllowed`（排除 `discover_tool`/`workspace`），后台上下文里 `Resolve` 不再回落到全局 registry、`Schemas` 按同一白名单过滤；`atri_x/agent/cron.py` 的 `backgroundCachedTools` 在写入后台 metadata 前过滤外部声明。
- `atri_x/platform/qq_onebot/adapter.py`、`atri_x/platform/qq_onebot/transport.py`：超过 `qqTextPageRunes`（3000 rune）的文本按页包成 `node`，用 `send_group_forward_msg` / `send_private_forward_msg` 发一条合并转发（不再分页加标记）；`referenceFetcher` 对引用回复里的合并转发展开一层。
- `atri_x/utils/fileops/rollback.py`：进程内、按 Session 的编辑前内容备份（每文件仅保留最近一次，1024 条 / 256 MiB 上限，超出淘汰最旧），`Restore` 用 `RevisionAfter` 校验文件自编辑后未被改动，Shell 或外部程序的修改一律拒绝覆盖（**新建文件同样先核对内容、一致才删除**）；`atri_x/tool/builtin/file_rollback.py` 与 `atri_x/agent/commands/rollback.py` 分别是仅超管的 `rollback_file` 工具与 `/rollback [编号]` 命令，`edit_file` 通过 `editTool.Backups` 写入记录，Session 身份由 `agentToolRunDeps.PrepareToolContext` 注入的 `tool.WithSessionID` 传递。
- `atri_x/media/platform.py`：共享平台导入、历史媒体位置关联与本地 ID 查询；`atri_x/media/manager.py`、`atri_x/media/image.py`：统一媒体入库和持久化前图片压缩；`atri_x/media/generation.py`：`GenerationSink` 接口与入库时的生成参数采集——采集点必须落在压缩之前（压缩会把超限图片重编码成 JPEG，销毁 PNG 文本块与 EXIF/XMP），落盘点必须落在哈希之后（媒体 ID 那时才存在），所以入库路径在转码前留下原始字节、拿到 ID 后再交出去；`atri_x/media/resolver.py`：LLM 媒体解析与传输选择；`atri_x/media/history.py`：跨库历史 owner 分页对账。
- `atri_x/media/worker.py`：隔离压缩的槽位闸门。槽位**只在底层工作真正结束后**归还（超时/取消只放弃等待，既不还槽位也不取消内部任务），因此 `_GATE_SLOTS` 恒等于"最多这么多个真实存活的工作"；排队有界（`IMAGE_WORKER_QUEUE_LIMIT` / `IMAGE_WORKER_QUEUE_TIMEOUT`，超限/超时分别抛含 `queue full` / `queue timed out` 的 `MediaError`），`worker_stats()` 暴露 active/waiting/orphaned/timed_out 等诊断字段。`python -m atri_x.media.worker` 是逐字保留的 Go 工作进程协议入口（当前 in-process 路径不经过它，线程模式是明确的降级而非硬隔离）。
- `atri_x/media/imagecodec/`：标准库图像编解码（PNG / baseline JPEG / 缩放 / 像素操作），Pillow 只作可选加速后端。`backend_probe.py` 是 `import PIL` 的**唯一出处**：`image_backend()` / `image_backend_info()` 供启动诊断与 `/diagnostics` 使用，`python -m atri_x.media.imagecodec.backend_probe --json` 是部署侧（含冻结产物）核验后端能力的入口。
- `atri_x/storage/sqlite/media_history.py`：主库历史媒体关联与引用事务，区别于机器人发送输出索引。
- `atri_x/agent/`：只读提供当前 Session ID，供平台引用续聊/fork 判定（对应 Go `reference.go`）。
- `atri_x/agent/media_output.py`：发送前归一、发送副本解析与有序媒体回执缓存。
- `atri_x/agent/options.py`、`atri_x/agent/logging.py`、`atri_x/agent/identity.py`：运行配置、日志和 Actor/Scope 解析。
- `atri_x/agent/dsh/`（**Python 版新增**：把 DSH 接进对话链路）：`router.py`（任务特征打分 + 成本闸门 `score−λ·cost_pressure` + 熔断，纯函数可离线断言）、`handoff.py`（系统设定 + 最近 N 轮 + 当前消息 → 一次性 prompt；ACP usage → 账本 `Usage`）、`runtime.py`（长驻 `AcpClient` + 每轮一个一次性会话 + asyncio/阻塞桥接）、`route_call.py`（在 `call_llm` 里接路由：预算预占/结算、流式回传、失败回退/部分输出、熔断提示）、`command.py`（启动命令解析的唯一出处）、`status.py`（`python -m atri_x.agent.dsh.status`）。配置段 `[agent.dsh]`；挂点只在**一轮的第一次** `call_llm`（`allow_route`），工具轮次与视觉重试不换后端。设计见 [`devdocs/dsh-acp.md`](dsh-acp.md) §6。
- `atri_x/agent/chat.py`：普通对话主流程；群级默认模型只在本轮没有显式 `@model:` 覆盖时生效。后台 turn（`backgroundTurnOutput`）在每次模型调用前、工具批次落库后、最终输出前与输出 hook 之后各查一次前台接管标记（`backgroundSessionMustStop` → `session.WasPromoted`），命中即返回 `errBackgroundTakenOver`，不发布 PhaseError、不写迟到助手消息；标记**查不出来**时保守停下并记 `background_takeover_check_failed`（不拿"读不到"当"没被接管"继续烧 token），上下文已结束时交给原有取消路径。`atri_x/agent/cron.py` 的 `RunBackground` 在 turn 正常结束后再复核一次接管，避免向已被接管的会话投递后台报告。
- `atri_x/agent/group_policy.py`：群级策略解析、唤醒/静默判断、模型目录、learning 审核动作授权；策略保存在 `state.toml` 的 `group_policy`。
- `atri_x/agent/group_knowledge.py`、`atri_x/groupkb/`：按群 scope 的本地确定性 FAQ 匹配与状态；`groupkb` 只做归一化和 exact/contains/keywords 匹配，不调用模型；Agent 负责权限、上限、`state.toml [group_knowledge]` 持久化和命中后的直接回答。
- `atri_x/agent/member_panel.py`、`atri_x/agent/commands/member_panel.py`：普通成员 `/me` 自助面板；按 FairKey 过滤自己的请求和排队消息，并汇总自己的生图/视觉/聊天额度，不暴露跨群全局聚合用量。
- `atri_x/agent/group_services_state.py`、`atri_x/agent/group_services_reminders.py`、`atri_x/agent/group_services_polls.py`、`atri_x/agent/group_services_signups.py`、`atri_x/agent/commands/group_services.py`：群内提醒/投票/报名；状态写入 `state.toml [group_services]`，提醒由 Agent 本地 ticker 调度，投票/报名为纯确定性状态机，全部受群运行状态与全局/群级开关约束。
- `atri_x/agent/model_policy.py`：实际执行模型目标的最后一道目录校验，覆盖 turn hook、cron override、压缩、视觉 fallback 和 group_analysis 摘要。
- `atri_x/agent/state_watch.py`、`atri_x/agent/commands/state.py`：`state.toml` 外部编辑热加载；`refreshRuntimeState`/`applyRuntimeState`（`atri_x/agent/model.py`）负责 mtime 判定、分区合并和变更摘要，`StartRuntimeStateWatch` 每 15 秒轮询，`/state` 查看状态、`/state reload` 强制生效；`saveRuntimeState` 写回前先合并外部修改，`[budget]` 账本不参与热加载。
- `atri_x/angelmemory/`、`atri_x/tool/builtin/angel_memory.py`、`atri_x/tool/builtin/angel_memory.py`：scope-local 长期记忆与工具；`angel_forget` 默认关闭（`[angel_memory].allow_tool_forget`），只删除来源为当前发言人的单条记忆，高风险 + `confirm=true` 二次确认 + 每分钟上限，作为 `angel_recall` 的依赖注入；`/memory backfill` 只回填旧数据可确定的 `source_kind`。
- `atri_x/agent/tool_auth.py`：群工具白名单统一授权入口与执行前复核；展开工具 profile，区分“继承全局”和“禁止全部”。
- `atri_x/agent/budget.py`：群 / 单用户 / 全局每日账本；生图/视觉/语音转写调用预占、chat token/费用、provider 重试、工具执行幂等分别写入 `budget`；账本落盘失败时回滚并拒绝受限调用。可选 `chat_hard_limit` 在调用前按估算输入 + 输出预留原子预占，usage 返回后结算差额；usage 缺失或重启未结算时保持保守占用并写入 `budget.uncertain`。
- `atri_x/historygate/`：统一历史写入门；适配器的 `chat_history` / `outbound_messages` 写入按可信 `平台 + scope` 策略过滤，`history=off` 不删除旧记录但停止新增。
- `atri_x/agent/turn_gate.py`：turn 终止状态和晚到输出闸门；流式 flush、工具结果回传和最终发送前检查。
- `atri_x/agent/events.py`：notice/request/meta_event 的确定性处理；撤回按消息 ID → turn request 精确取消，成员退群/禁用按 FairKey 定向取消，不调用 LLM；机器人自身被禁言/踢出时由 `atri_x/agent/group_runtime.py` 转入 `muted` / `removed`，暂停该群模型调用与输出。群运行状态持久化在 `state.toml [group_runtime]`。
- `atri_x/agent/chat_llm.py`：LLM 调用和消息转换。
- `atri_x/agent/chat_anomaly.py`：上游把工具调用写成正文（DSML / `<tool-call>` 文本）时的补救接线：还原成 `ToolCallRequest` 并继续工具轮，还原不出来就清洗正文换成提示，绝不让协议标记发到平台；判据与解析在 `atri_x/llm/tooltext.py`。
- `atri_x/agent/chat_tools.py`：工具执行与确认。
- `atri_x/agent/turn_output.py`：turn 输出适配。
- `atri_x/agent/prompt.py`：Prompt 构建。
- `atri_x/agent/system_prompt*.py`：Soul、常驻记忆、工具提示等 system prompt 来源和组合。
- `atri_x/agent/cron.py`：`RunBackground` / `RunCronMessage` 的后台入口；已提升（被前台接管）的 Session 在 `backgroundSession` / `ensureBackgroundSession` 阶段就短路，出口统一为 `RunResult{TakenOver: true, Outcome: OutcomeTakenOver}`（`err == nil`），cron 侧记为 `ReportReady=true, TaskCompleted=true, Report=""`，因此不投递任何汇报。
- `atri_x/agent/tool_transcript.py`：工具 transcript 持久化。

常用搜索：

```bash
rg -n "Handle|Run|Prompt|tool_calls|reasoning|usage|pending|prepared" atri_x/agent
```

<!-- locator:commands -->
## Slash 命令与补全

适用任务：新增/修改 slash 命令、命令帮助、命令参数补全。

先看：

- `atri_x/agent/commands/`：内置命令实现。
- `atri_x/agent/commands/__init__.py`：命令模块注册入口。
- `atri_x/agent/commands/session_*.py`：按模式、核心、导航、生命周期和格式化拆分的 Session 命令；共享状态由 `SessionCommandState` 按 Scope 隔离。
- `atri_x/agent/commands/group_policy.py`：`/grouppolicy` 群级策略命令；修改入口只接受当前群，`learning-moderation` 额外要求机器人超级管理员。
- `atri_x/command/`：通用命令框架和 Router；`Info.GroupAdminNeedsGrant` 用于把群管理员访问绑定到服务端群级授权。
- `atri_x/completion/`：平台补全服务。
- `docs/commands.md`：用户侧命令文档。

常用搜索：

```bash
rg -n "Register|Info\{|Help:|Complete|Alias|/requests|/model" atri_x/agent/commands atri_x/command atri_x/completion docs/commands.md
```

<!-- locator:request-turn -->
## Request、Turn 与运行状态

适用任务：active request 树、取消/停止/超时、阶段展示、工具 pending、确认状态、runtime status。

先看：

- `atri_x/request/`：并发限制、有界等待队列、全局及每用户/每 scope 队列上限、按 `FairKey` 的公平调度和 `CancelFairKey` 定向取消；`Snapshot` 提供排队长度、平均/最老等待和队满/超时/取消拒绝指标。
- `atri_x/turn/`
- `atri_x/runtime/`
- `atri_x/agent/status.py`：Agent runtime status 发布。
- `atri_x/agent/request_context.py`：父子 request context。
- `atri_x/agent/risk_confirmation.py`：高风险确认命令文案和识别。

常用搜索：

```bash
rg -n "Phase|Request|Cancel|pending|confirm|runtime status|sending" atri_x/request atri_x/turn atri_x/runtime atri_x/agent
```

<!-- locator:tool -->
## Tool Runtime、工具发现与内置工具

适用任务：内置工具、工具注册、schema、风险等级、确认详情、工具发现、工具缓存、工具 tag、文件/shell/web/cron/memory 工具。

先看：

- `atri_x/tool/`：Tool Runtime 核心类型、builder、discover、executor、sandbox/workspace helper。
- `atri_x/tool/media_runtime.py`：shell/Skill 的显式媒体准备、调用期引用、sandbox 导出缓存与受控结果导入；缓存复用刷新 ModTime，沿用既有 sandbox 清理。
- `atri_x/tool/runtimeinfo.py`：工具运行期常用信息入口，如配置路径、sandbox、文件发送配置、时间源和规则卡转发。
- `atri_x/toolrun/`：工具调用中间层、工具视图、命名解析、风险确认、执行前 Session 工具参数媒体引用，以及排队后的授权/额度二次校验钩子。
- `atri_x/tool/builtin/`：内置工具。
- `atri_x/tool/builtin/group_analysis.py`：`group_analysis` 工具，读取本地历史并输出群统计，可选 LLM 摘要。
- `atri_x/tool/builtin/angel_memory.py`：`angel_remember` / `angel_recall` 工具。
- `atri_x/tool/builtin/self_learning.py`：`self_learning_review` 工具。
- `atri_x/tool/builtin/web_search.py`、`atri_x/tool/builtin/web_search_engines.py`、`atri_x/tool/builtin/web_search_cli.py`：`web_search` 工具与搜索通道链——通道来自 `[web_search]` 配置（桌面端「模型 → 搜索服务」），按 `engine_order` 逐条尝试、失败降级；HTTP 通道有 DeepSeek 官方搜索 / Tavily / Firecrawl 免注册 / Keenable 免注册 / Exa / 自建 SearXNG（`web_search_engines.py`），外部 CLI 通道有 Antigravity（`agy`）与 Grok Build（`grok`，X 搜索，`web_search_cli.py`）；Tavily 的请求体与文本输出仍与 Go 版逐字一致，其余通道只回「标题 + URL + 摘要」（DeepSeek 回长摘要）。密钥口径：DeepSeek 优先 `DEEPSEEK_SEARCH_KEY`、回落 `DEEPSEEK_API_KEY`（`key_env_names`）。
- `atri_x/tool/builtin/web_extract.py`、`atri_x/tool/builtin/web_extract_engines.py`：`web_extract` 工具与抓取通道链——通道来自 `[web_extract]` 配置（桌面端「模型 → 抓取服务」），默认链是 Jina Reader（有 `JINA_API_KEY` 才参与）→ 直接抓取；可选 `deepseek` 通道调用 `POST /v1/tools/fetch` 取 Markdown 正文（按次计费，要显式配置）。`web_extract.py` 保留编排/缓存/分段输出，`web_extract_engines.py` 放各通道的请求与解析（含纯标准库 HTML 清洗）。
- `atri_x/app/image_to_prompt.py`：`image_to_prompt` 内置工具，读取 Media Center 图片并通过 `ImagePromptService` 委托给共享的 `atri_x/vision.Service`（app 层用已有 provider 配置它）；工具自身不缓存、不做 singleflight，缓存键、并发合并与失败/空/截断处理都在 `atri_x/vision`。同一份字节会先用 `atri_x/artist` 读一遍生成参数（先查入库留档、未命中再读图片自身元数据，都在视觉预处理之前），据 `include_artist` 决定回报原始画师串还是按画风检索串库；`format=json` 与 `include_artist=recommend` 才会改用 `vision.ImageAnalysisPrompt` 结构化模板，因此纯文本调用继续命中旧缓存条目。
- `atri_x/artist/`：画师串领域包，只依赖 `atri_x/media`（为实现 `media.GenerationSink`）与 `atri_x/utils/fileops`，`atri_x/media` 及其依赖不反向依赖本包。`types.go` 定义画师/组合/scope/方言与上限；`parser.go` 按括号层级与转义切分提示词、解析权重（`(x:1.2)`、`(x)`、`{x}`、`[x]`），识别不了的表达式（提示词编辑语法、分组）标为 `TokenUnparsed` 原文透传；`metadata.go` 只读解析 PNG `tEXt`/`iTXt`/`zTXt`、JPEG EXIF/XMP、WebP EXIF/XMP 里的 A1111/NovelAI/ComfyUI/InvokeAI 参数（长度先校验、解压有上限、绝不 panic）；`store.go` 用一个 JSON 文件 + 进程内互斥 + 原子替换 + 整数 revision 承载串库；`metadata_store.go` 用另一个 JSON 文件按媒体 ID 保存入库时采集的生成参数，与串库相互独立（读图片参数不需要可写串库）；写入失败时回滚内存状态，因此 `Lookup` 永远不会看见一条没落盘的记录；`search.go` 提供严格匹配的 `Search` 与允许部分匹配的 `Rank`；`render.go` 按方言渲染并显式报告权重丢失；`service.go` 是唯一决定可见性与写权限的地方，scope 身份只从调用上下文取，并且是留档与字节两条读路径的唯一切换点：图片被转码时留档是唯一来源，未被转码时先解析字节、留档只作兜底（这样旧解析器写下的记录不会一直作答），同一次调用就给出 `MetadataOriginArchive` / `MetadataOriginEmbedded`，调用方不需要再查一次去猜来源；留档读取失败降级为可见警告，而不是静默当成"没有参数"。
- `atri_x/tool/builtin/artist_tools.py`、`atri_x/artist/`：`artist_extract` 与 `artist_library_search` / `_read` / `_save` / `_delete`。`artist_extract` 与生图功能无关的解析路径不注册依赖，串库未配置时也可用；四个库工具只在串库启用时注册（`RegisterAll` 里按 `ArtistTools.Enabled()` 判断）。
- `atri_x/imagegen/prompt.py`：`PromptBlock` + `ComposeBlocks`，按优先级整体丢弃片段并由 `PromptPriorityProtected` 保护画师串与角色约束，取代原先按 rune 硬截断（会切坏 `(artist_a:1.2)`）。
- `atri_x/vision/analysis.py`：结构化图像分析模板的结果类型与容错解析（从模型回答里取出第一个 JSON 对象），与绘图提示词模板共用同一个引擎和缓存命名空间。
- `atri_x/groupanalysis/`：群分析 clean-room 统计与可选 `Summarizer`；只依赖 `chat_history` / `outbound_messages`。
- `atri_x/groupkb/`：确定性 FAQ 匹配引擎；归一化（全角/大小写/空白/标点）和 exact/contains/keywords 规则，纯本地、无模型依赖。
- `atri_x/angelmemory/`：clean-room SQLite 长期记忆、召回和上下文构造；写入来源类型/用户/消息 ID/Session ID，提供 scope 限定的查询、前缀解析和按来源删除。
- `atri_x/selflearning/`：clean-room 观察、候选挖掘、review-before-apply 和上下文构造。
- `atri_x/agent/commands/memory_learning.py`：`/memory`、`/learning` 管理命令。
- `atri_x/agent/commands/memory_forget.py`、`atri_x/agent/commands/memory_common.py`：普通成员 `/forget` 入口、可见性过滤、按来源删除和常驻记忆清空。
- `atri_x/tool/builtin/file_tools_ast.py`：`read_file` 的 Python/Shell AST 名称搜索与结果渲染。
- `atri_x/agent/tools.py`：Agent 工具运行态和命令依赖适配。
- `atri_x/agent/toolrun_*.py`：Agent 到 ToolRun 的桥接。
- `atri_x/agent/tool_cache.py`：Session 级工具 schema 缓存；工具发现与 `@tool:` / `@skill:` 预载的写入都经 `mutateSessionMetadata` 在事务内合并进最新 metadata。
- `atri_x/agent/tool_directive.py`：`@tool:` / `@skill:` 预处理。
- `atri_x/agent/tool_tag_config.py`：工具 tag 配置。
- `atri_x/security/`：工具权限和风险策略。
- `atri_x/utils/fileops/{file,encoding,text}.py`：文件生命周期、编码与通用文本处理。
- `atri_x/utils/fileops/{edit,match,diff}.py`：原子编辑解析、目标匹配与 unified diff。

常用搜索：

```bash
rg -n "discover_tool|NewBuilder|Risk|Confirm|ToolRun|Result\{|Outputs|workspace|shell" atri_x/tool atri_x/toolrun atri_x/agent
```

<!-- locator:tool-flow -->
## 工具调用链路相关文件

适用任务：LLM tool call 到工具执行、工具结果回灌 LLM、工具调用记录、工具确认、批量预览。

先看：

- `atri_x/agent/chat_tools.py`：Agent 工具执行主入口；`recordToolCall` 写 `tool_call` 审计时按工具类别限长正文（读取／查询／说明类 128 字符，其余 256，参数统一 256，见 `toolLogResultLimit`），并写入 `run_source`（`toolRunSource` 从调用现场的 `SandboxContext.BackgroundKind` 取 foreground／cron／atrinis，供 `/audit --run_source` 筛选）。
- `atri_x/toolrun/`：执行前解析、过滤、确认、排队后授权复核、额度预占和预览。
- `atri_x/tool/executor.py`：Tool Runtime 执行适配。
- `atri_x/tool/tool.py`：Tool 核心类型。
- `atri_x/agent/tool_transcript.py`：tool message/transcript 落库。
- `atri_x/storage/sqlite/tool_call_repository.py`：工具调用记录持久化。

常用搜索：

```bash
rg -n "ToolCall|tool message|transcript|ToolCallRecord|confirm|preview" atri_x/agent atri_x/tool atri_x/toolrun atri_x/storage
```

<!-- locator:skill -->
## Skill 与 Atriyph

适用任务：AgentSkill 解析或工具化、Atriyph parser/linter、原生 ATRI-X Skill 创建/修改/finalize、Go skill 扫描/编译/运行。

先看：

- `atri_x/atriyph/`：Atriyph 语言层。
- `atri_x/tool/skill/`：Skill 解析、扫描、catalog、创建、修改、finalize、runner。
- `atri_x/tool/skill/agent_manifest.py`：AgentSkill 工具化 manifest。
- `atri_x/tool/skill/media.py`：Go payload/TOML 媒体参数的执行副本转换和 stdout 媒体结果解析；`atri_x/config/assets.py` 中的 `defaultAgentSkillCreatorSkillMD` 是 Agent Skill Creator 的内置说明。
- `atri_x/tool/skill/go_source.py`：原生 Go skill 源码维护和编译。

常用搜索：

```bash
rg -n "SKILL.atriyph|ATRIX_SKILL|AgentSkill|go_skill_run|finalize|Lint|Catalog" atri_x/atriyph atri_x/tool/skill
```

<!-- locator:hook -->
## Hook 与插件

适用任务：Hook 事件、控制字段、注册、列表、热重载、规则 Hook TOML、exec action、hook.v2 协议、持久 Hook 和 SharedState。

先看：

- `atri_x/hook/event.py`：Hook 点、事件 payload 和 Handler 基础类型。
- `atri_x/hook/media.py`：Go Hook 使用的宿主 Media API；`atri_x/hook/runtime/media.py`：hook.v2 媒体 RPC、安全导出和临时引用生命周期。
- `atri_x/hook/output/`：规则、一次性 exec 与 runtime 共用的输出协议、消息图片 segment 规范化、校验和 delivery 转换。
- `atri_x/hook/protocol/`：进程 Hook 共用的 `hook.v2` 帧、ID 校验和 `event.handle` 公共结果字段。
- `atri_x/processenv/`：Shell 与进程 Hook 共用的环境分层、PATH 补充和可执行文件解析；`atri_x/hook/process.py` 保留 Hook 侧适配入口。
- `atri_x/hook/match.py`：Hook 条件匹配、字段读取和模板值。
- `atri_x/hook/manager.py`：普通 Hook 注册、排序、执行与原子 handler 快照替换。
- `atri_x/hook/control.py`：`/hooks` 的列表、重载和持久进程生命周期管理入口。
- `atri_x/hook/builtin/`：内置 Hook 注册（用户插件放配置目录的 `plugins/`）。
- `atri_x/hook/rules/`：规则 Hook；`rules.py` 提供类型和模块入口，`config.py`/`toml_error.py` 负责配置加载与诊断，`rule.py`/`action.py`/`exec.py` 负责规则及 Action 执行，`exec_stderr.py` 负责一次性 exec 的跨平台进程树终止，`detail.py` 负责列表详情。
- `atri_x/hook/runtime/`：Worker Hook 配置、进程、双向 Pipe RPC、waiting 路由、工具桥接和进程内 SharedState。
- `atri_x/agent/hooks.py`：Agent 的 Hook 执行、上下文和 continuation 接入。
- `atri_x/agent/platform_signals.py`：平台连接事实的订阅方。`StartPlatformEvents` 订阅 `platform.PlatformConnected` 并按平台建 `signal.Queue`（同平台串行），worker 上跑 `handlePlatformConnected`（`atri_x/agent/hooks.py`）执行 `platform.connected` 的 Hook；`ClosePlatformEvents` 先停止接纳再排空。发布方在 `atri_x/app/platforms.py` 的连接回调里只 `Emit`。
- `atri_x/platform/platform.py`：平台契约（适配器、消息、群资料等）与 `ConnectedEvent` / `PlatformConnected` 全局信号。
- `atri_x/agent/output.py`：Agent 的 Output Manager 与平台 sender 接入。
- `atri_x/agent/media_output.py`：平台发送前解析 Hook/Tool 输出中的 media，并清理受控临时导出。
- `atri_x/signal/`：通用类型化信号（`Signal[T,R]` / `Emit` / `EmitAsync` / `Connect` / `ConnectForward` / `Observe` / `Disconnect` / `CancelPending` / `Drain`），每个信号自带独立的调度队列、容量与并发上限、按 key 串行、超时和关闭策略；**不依赖任何 `internal/*` 包**，因此是上游唯一可以整包移植的东西（来源见 `upstream-sync-todo.md` §8.2）。目前只落包 + 自带测试，尚未接管平台连接扇出。
- `docs/hooks.md`：用户侧 Hook 文档。

常用搜索：

```bash
rg -n "Event|Handler|Control|plugins/hooks.toml|exec|hook.v2|runtime|SharedState|message.segments|llm.messages" atri_x/hook atri_x/agent docs/hooks.md
```

<!-- locator:output -->
## Output、Delivery 与发送

适用任务：输出意图结构、文本/图片/文件/语音/at/reply/emoticon 发送、流式输出、notice、reasoning、runtime status。

先看：

- `atri_x/delivery/`：平台无关输出意图和发送管理。`ToolPreviewPrefix` / `Output.IsToolPreview` / `IsToolPreviewNotice` 是"这条通知是不是工具调用进度预览"的唯一判定入口：Agent 写前缀（`formatToolPreview`）、hook 出站记录用它跳过预览；`ShouldDropGroupToolPreview(target, outputs, groupContext)` 是"群聊里不投递进度预览"这条平台跳过规则的唯一入口（OneBot 与 QQ 官方调用它，各自只提供"当前上下文是不是群聊"：`isGroupContext`）。Telegram 与本地 CLI 不跳过预览。
- `atri_x/agent/turn_output.py`：Agent turn 输出适配。
- `atri_x/agent/output.py`：Agent 的 Hook/工具输出意图和平台 sender 接入；`Agent.SendNotice` 是全部通知投递（含 cron / Atrinis / hook 经 `app` 注入的 `sendNotice` 闭包）的统一裁决点，先过 `turnOutputAllowed` 与 `noticeTargetBlocked` 再下发；`noticeDispatch` 解析这次通知的落点（当前会话 sender 还是某个平台的 sender）并把平台名交给队列。
- `atri_x/agent/notice_queue.py`：通知的按平台发送队列与背压。每个平台一条 `signal.Queue`（默认 256 等待 / 8 并发），串行键是"平台 + scope + 收件人"（`noticeTargetKey`），因此同一目标按到达顺序发送、平台之间互不排队；调用方语义不变（提交后等真实 `Receipt` / error），队满立刻返回 `signal.ErrQueueFull` 并由 `atri_x/agent/output.py` 记一条 WARN；`Agent.CloseNoticeQueues` 先停止接纳再排空，由 `atri_x/app` 的运行时关闭步骤在 30 秒预算内调用。
- `atri_x/platform/platform.py`：平台发送抽象。

常用搜索：

```bash
rg -n "Output|SendChat|SendNotice|Stream|Reasoning|emoticon|receipt|ToolPreview" atri_x/delivery atri_x/agent atri_x/platform
```

<!-- locator:platform -->
## 平台适配

适用任务：平台输入解析、平台发送、CLI/TUI、远程 CLI、OneBot、QQ 官方、Telegram。

先看：

- `atri_x/platform/platform.py`：平台抽象，以及可选的群历史/群目录/头像/群素材能力接口；`PlatformEventHandler` 提供 notice/request/meta_event 的非消息事件入口。
- `atri_x/platform/qq_onebot/capabilities.py`：OneBot 可选群历史、群信息、成员列表和头像 URL 能力。
- `atri_x/platform/qq_onebot/message.py`：入站文本保留原始空白，并生成不含转发内容的唤醒匹配视图；按共享总预算展开 `forward`/`node` 合并转发。`atri_x/platform/qq_onebot/inbound.py` 提供 `platform+bot+scope+message_id` 入站去重和有界预处理 worker / 高优先级事件通道；`atri_x/platform/qq_onebot/adapter.py` 的读取循环不再逐条无界起 asyncio task，并限制 `get_forward_msg` 拉取次数、超时、返回体和拉取失败降级。
- `atri_x/platform/qq_onebot/transport.py`、`atri_x/platform/qq_onebot/smoke.py`：OneBot v11 正向 WS 连接的读写闸、动作响应分发与发送接口；`smoke.py`（`python -m atri_x.platform.qq_onebot.smoke`）只做协议层冒烟——连上 → `get_login_info` → 可选发群/私聊 → 可选监听上报事件，不读 `app.toml`、不装配 Agent，故障定位不依赖模型。
- `atri_x/platform/telegram/capabilities.py`：Telegram 可选群信息和管理员列表能力；历史与全量成员回退本地历史。
- `atri_x/platform/media.py`：Chat History 原始有序 segments 编解码与敏感来源清洗。
- `atri_x/platform/refcontext.py`：按输出索引、Chat History、平台兜底恢复引用；自己 Session 的最后一条 assistant 自动 Resume，较早 assistant 自动 Fork。
- `atri_x/platform/config.py`：平台配置解码。
- `atri_x/platform/builtin.py`：内置平台装配。
- `atri_x/platform/cli/`：本地/远程 CLI 和 TUI。
- `atri_x/platform/cli/protocol.py`、`remote_server.py`（**Python 版扩展**）：前端协议在原有 `input`/`complete` 之外新增两对**只读**帧——`sessions`→`sessions_result`、`history`→`history_result`（会话列表/历史；不改当前会话，切换仍走 `/resume <id>`）。服务端通过 `set_session_browser(...)` 注入 `atri_x/session/browse.py` 的浏览器（装配在 `app/platforms.py` 的 `register_session_browsers`，与 `register_completion_platforms` 同时机）；没有注入时两帧返回空结果，不影响原协议。
- `atri_x/platform/cli/tui/model.py`、`atri_x/platform/cli/tui/mouse.py`、`atri_x/platform/cli/tui/copy.py`：TUI 主模型、鼠标交互与 copy mode。
- `atri_x/platform/qq_onebot/`
- `atri_x/platform/qqofficial/`
- `atri_x/platform/telegram/`
- `atri_x/platform/headless.py`

常用搜索：

```bash
rg -n "PlatformAdapter|SendChat|MessageSegment|Actor|Scope|remote|websocket|long polling" atri_x/platform
```

<!-- locator:session -->
## Session

适用任务：session 创建/恢复/列表/归档/置顶/删除、Fork、模式切换、命名、过期清理、平台隔离、cron session 可见性。

先看：

- `atri_x/session/service.py`、`atri_x/session/types.py`：Session 服务主体和领域请求/结果类型；字段写入统一走 `storage.SessionRepository.Mutate`（事务内读-改-写），`Resume` / `Touch` 的 `UpdatedAt` 也在事务内设置。
- `atri_x/session/shared.py`：群共享线程的 Session 所有者、`Scope.Shared` key、元数据标记与访问判定。
- `atri_x/session/mode.py`：模式激活和 work 历史限制。
- `atri_x/session/lifecycle.py`、`atri_x/session/query.py`、`atri_x/session/fork.py`、`atri_x/session/expiration.py`：生命周期、查询、Fork 和闲置过期策略；重命名/归档/置顶与闲置过期都在事务内基于最新行重新判定（手动改名标记、空闲判定）；`Unarchive` 对后台 Session 同样执行提升。
- `atri_x/session/promotion.py`：后台 Session 的前台接管判定与提升。`IsBackground` 按 metadata `background_kind` 或 `cron:` / `atrinis:` scope 前缀判断，`WasPromoted` 按 metadata `foreground_origin` 判断；`promoteToForeground` 必须在 `Sessions().Mutate` 事务内调用，一次性写 `foreground_origin`（记录接管前的 kind/owner/platform/scope）、删除 `background_kind`、切到当前前台 scope 并置 `Mode = work`。`Resume` / `Unarchive` 用它做原地提升，`canAccess` 用 `IsBackground` 放行后台 Session 的跨 scope 恢复。
- `atri_x/session/naming.py`：异步 Session 命名；迟到的命名写入在事务内检查 `title_renamed`，不覆盖手动改名。
- `atri_x/session/browse.py`（**Python 版新增**）：远程前端的只读浏览视图（`sessions` / `history` 帧的数据源）——复用 `Service.list_page` 与 `can_access`，只读 `store.messages().list_by_session`，返回纯 dataclass（时间由平台层编码成 RFC3339），不依赖任何平台模块。
- `atri_x/agent/session_metadata.py`：Session metadata 编解码；`encodeSessionMetadataInto` 基于原始 JSON 只改写已知字段，保留未知键。
- `atri_x/agent/workspace.py`：Agent workspace 持久化适配；`workspace_dir` 与通知目录经 `mutateMetadata` 在事务内合并。
- `atri_x/tool/workspace.py`：工具 workspace context 和路径解析。

常用搜索：

```bash
rg -n "Fork|Archive|Pinned|Expire|SessionMode|metadata|workspace|cron:" atri_x/session atri_x/agent atri_x/tool
```

<!-- locator:context -->
## Context、Prompt 与压缩

适用任务：上下文加载、压缩摘要、context window、usage 展示、system prompt source。

先看：

- `atri_x/contextmgr/`：按加载/Fork、窗口、usage、压缩器和摘要 prompt 拆分的上下文基础能力。
- `atri_x/agent/context_runtime.py`、`atri_x/agent/context_compact.py`、`atri_x/agent/context_seed.py`、`atri_x/agent/context_usage.py`：Agent 上下文运行态、独立 Session 压缩编排、首消息 seed 物化与 usage/动态阈值。
- `atri_x/agent/prompt.py`：Prompt Builder。
- `atri_x/agent/system_prompt*.py`：system prompt 管理和来源。
- `atri_x/llm/segment.py`：MessageSegment helper。

常用搜索：

```bash
rg -n "ContextLoader|Compress|Window|System Prompt|MessageSegment|usage" atri_x/contextmgr atri_x/agent atri_x/llm
```

<!-- locator:llm -->
## LLM Adapter

适用任务：LLM 抽象、OpenAI-compatible 请求（chat / responses 两种协议）、SSE、usage、reasoning、tool call delta、多模态消息转换。

先看：

- `atri_x/llm/`：LLM 抽象和 MessageSegment。`atri_x/llm/protocol.py` 是"这个适配器、这个模型实际支持哪些协议能力"的唯一入口：`ProtocolCapabilities`（服务端续链 / 原生压缩 / 增量工具 / 服务端存储）+ 可选接口 `ProtocolCapabilityReporter` + 保守的 `ProtocolCapabilitiesOf`（未实现查询接口的客户端按 Chat Completions 处理）。`atri_x/llm/origin.py` 是"这段已有历史能不能交给目标模型"的唯一入口：`Origin{Protocol, Provider}` + `CanSwitch`（目标缺协议/厂商直接拒绝；来源没记协议放行；同协议且是 chat_completions 或同厂商放行；其余拒绝）。
- `atri_x/llm/openai/`：OpenAI-compatible adapter。`atri_x/llm/openai/openai.py` 是 Chat Completions 适配器（`{base_url}/chat/completions`），`atri_x/llm/openai/responses.py` 是 Responses 适配器（`{base_url}/responses`，内嵌前者以复用重试、`/models`、SSE 行扫描与 `parseError` 错误分类，只替换请求信封和流式事件翻译）；`[providers.*].api_mode` 决定用哪个，两者对上层输出同一套 `llm.StreamChunk`。两个适配器都实现 `ProtocolCapabilitiesFor`：Responses 适配器报告 `responses` 但服务端能力全 false（它是协议翻译层，固定 `store=false`、每轮重放历史，不虚报能力）。
- `atri_x/llm/tooltext.py`：上游把原生工具调用以文本协议泄漏进 assistant 正文时的判定、还原与清洗（纯函数）。支持 DSML 文本协议（含 `<｜place▁holder▁no▁73｜>`、`parameter name=` 丢失等破损形态）与 `<tool-call>{...}</tool-call>` JSON；接线在 `atri_x/agent/chat_anomaly.py`，正常回合（结构化 `tool_calls`）不经过这条路径。
- `atri_x/agent/model.py`：模型运行态、模型切换、provider client 缓存；`Agent.ModelProfiles()` 把 `services.toml` 的 `model_profiles` / `aliases` 汇成 `/model --profiles` 的列表（按别名键列出用户实际输入的名字，`Available` 反映 provider 在当前进程里有没有 client）；`mergeExternalRuntimeState` 是所有 `state.toml` 写入者必须先调用的合并入口（先合并外部编辑、再改内存、最后 `saveRuntimeState`）。
- `atri_x/agent/model_snapshot.py`：命名模型快照（`/model --save` / `--snapshots` / `--apply` / `--delete`）。快照是 `state.toml [model_snapshots]`（`config.StateModelSnapshot`：各模式 + compact + naming），内存副本由 `modelSnapshotsMu` 保护，与 `mode_models` 一起走 `applyRuntimeState` / `saveRuntimeState` / `runtimeStateDigest`；名字校验与"不与静态 profile 同名"的约束也在这里。
- `atri_x/agent/chat_llm.py`：Agent LLM 调用适配。
- `atri_x/contextmgr/compressor.py`、`atri_x/agent/context_compact.py`：上下文压缩后端。`contextmgr.Compactor`（`Name` / `Compact` / `SummarizeText`）是后端接口，目前唯一实现是客户端摘要压缩 `Compressor`（`Name() == "client_summary"`）；`contextRuntimeState.compactorFor(provider, model)` 按 `llm.ProtocolCapabilities` 分派，协议声称支持服务端原生压缩而 fork 没有对应实现时**显式回退**并由 `Agent.compactSession` 写审计 `context_compaction_backend`（含 `backend` / `reason`）。
- 会话协议来源：`atri_x/agent/session_metadata.py` 的 `llm_origin`（`{protocol, provider, model}`）由 `Agent.recordLLMOrigin`（`atri_x/agent/context_usage.py`）在每轮模型调用后写入，值未变时不写事务；对应上游的 `session_llm_origin` 迁移，fork 用加法式 metadata 字段承载。`Agent.llmOriginFor` / `Agent.checkModelSwitch`（`atri_x/agent/model.py`）在 `/model` 切换当前会话正在用的模式前，用同一事实过一遍 `llm.CanSwitch`。

常用搜索：

```bash
rg -n "ChatCompletion|Stream|SSE|reasoning|usage|ToolCall|MessageSegment|Models" atri_x/llm atri_x/agent
```

<!-- locator:storage -->
## Storage 与 SQLite

适用任务：领域模型、repository interface、migration、session/message/context summary/chat history/cron/atrinis/tool call 持久化。

先看：

- `atri_x/storage/storage.py`：领域模型和 repository interfaces；Message 使用 `content` 作为纯文本快速路径，`segments` 保存可选多模态正文；可选 `ChatHistoryRangeRepository` 提供批量历史，`OutboundMessageRepository` 保存实际发送的 assistant 消息。`SessionRepository.Mutate` 在单个事务里读-改-写同一行并返回最新 Session，是会话字段的唯一并发安全写入口；`Update` 只用于测试夹具。
- `atri_x/storage/ids.py`、`atri_x/storage/times.py`：通用 ID/时间 helper。
- `atri_x/storage/sqlite/`：SQLite store、migration 和 repository 实现；`atri_x/storage/sqlite/chat_history_repository.py` 内含 `outbound_messages` 表；`atri_x/storage/sqlite/session_repository.py` 的 `Mutate` 用 `sessionSelectByID` / `sessionUpdateStatement` / `sessionUpdateArgs` 与 `Get` / `Update` 共享同一份行读写语句。

常用搜索：

```bash
rg -n "Migration|Repository|Upsert|List|Archive|Fork|ToolCall|CronJob|AtrinisEvent" atri_x/storage
```

<!-- locator:atrinis -->
## Atrinis / Atriena / Atriwisp

适用任务：Atriena 协议类型、Atrinis HTTP/鉴权/去重/事件准备、direct/llm 投递、segments、background、Atriwisp 文档或指南工具。

先看：

- `atri_x/atriena/`：公共协议层。
- `atri_x/atrinis/`：Atrinis HTTP、鉴权、准备、投递和后台任务；`atri_x/atrinis/outbox.py` 负责 LLM 报告持久化投递、重试与恢复。
- `atri_x/atrinis/media.py`：Atriena 媒体来源校验、按需入库、宿主 workspace 双向读写；通过 `media.Manager.MaterializeWithLimits` 传递本次接收限制，复用共享 Manager；发送回执缓存由 Agent 通用边界处理。
- `atri_x/media/lifecycle.py`、`atri_x/media/reference.py`：引用保护、1 小时孤儿宽限期和可重试后端清理；`atri_x/storage/sqlite/media_lifecycle.py`：输出关联、清理认领、启动恢复和只读一致性检查；`tests/test_sqlite_media.py` 覆盖媒体 JSON 引用事务与 fork 检查点范围。
- `atri_x/storage/sqlite/atrinis_event_repository.py`：Atrinis event 与 report outbox 的事务、claim、receipt 和完成状态持久化。
- `atri_x/background/`：cron/Atrinis 共用后台 LLM 类型与结果 helper。
- `atri_x/toolrun/atriwisp.py`：Atriwisp 工具与创建指南。
- `docs/atrinis.md`
- `docs/atrinis-usage.md`
- `devdocs/atrinis-atriwisp.md`

常用搜索：

```bash
rg -n "Atriena|Atriwisp|/atriena/v2/events|direct|segments|session_mode|background" atri_x/atriena atri_x/atrinis atri_x/background docs devdocs
```

<!-- locator:cron -->
## Cron 与维护任务

适用任务：中央 Cron Runtime、LLM 可编排 cron 服务、维护类清理任务、cron 工具。

先看：

- `atri_x/cron/service.py`：Cron Service 装配、CRUD 与公开入口。
- `atri_x/cron/model.py`：任务 Metadata、Delivery 状态类型、校验与规范化。
- `atri_x/cron/execution.py`：Direct/LLM 执行、报告生成和 JSON 格式重试。
- `atri_x/cron/delivery.py`：逐目标逐输出发送、状态持久化、降级与 receipt mapping。

- `atri_x/cron/recovery.py`：平台连接跟踪、过期 once 扫描与补发入口。
- `atri_x/cron/jsonview.py`：`JobView` / `CronJob` 回给模型的 Go `json.Marshal` 形态（Python 侧新增件，无对应 Go 文件）。
- `atri_x/cron/errors.py`：`errors.Join` 的等价物（Python 侧新增件）。
- `atri_x/maintenance/`：定时清理与报告
  - `maintenance.py`：`Service`（日志/会话/沙盒/聊天历史/DSH 内核/隐私清理）+ `setup_cron` 装配 9 个 handler。
  - `report.py`：资源日报（生图 / Token / 费用 / 磁盘 / 内存）的聚合、计价与渲染。
  - `dsh.py`（Python 版新增）：内置 DSH 内核 `DSH_HOME` 的保留清理（只碰 `sessions/` 与会话缓存）。
  - `summary.py`（Python 版新增）：每日会话总结的摘要生成、落盘与 `Service` 混入；CLI 在 `summary_cli.py`（`python -m atri_x.maintenance.summary_cli`）。
- `atri_x/agent/cron*.py`：Agent 后台 runner 和后台工具确认特例。
- `atri_x/tool/builtin/cron.py`：cron 内置工具。
- `atri_x/storage/sqlite/cron_job_repository.py`：cron job 持久化。

常用搜索：

```bash
rg -n "Cron|Job|Schedule|RunCron|maintenance|include_completed|tool_list_names" atri_x/cron atri_x/maintenance atri_x/agent atri_x/tool atri_x/storage
```

<!-- locator:packaging -->
## 打包与分发（Windows 桌面端 / 原生 exe）

适用任务：把 `atri_x` 冻结成不依赖 Python/Docker 的 Windows 可执行包、排查"打出来的 exe 起不来/首次启动没有默认配置/`-m` 模块跑不了/窗口版没 Tk/内置内核找不到"。

先看：

- `packaging/atri-x.spec`：PyInstaller 描述。三件事必须做对——`collect_submodules("atri_x")`（`--help`/`doctor`/各平台适配器大量走 `importlib.import_module` 延迟导入，静态分析看不到）、`datas` 打入 `atri_x/config/assets_data` 与 `atri_x/imagegen/prompts`（漏了会表现为"exe 能启动但生成不了默认配置"）、**不排除 `tkinter`**（窗口版要用）。onedir 下出两个 exe（`atri-x.exe` 控制台 + `atri-x-gui.exe` 窗口）共用一个 `COLLECT`；`ATRIX_PKG_RUNTIME_DIR` 非空时把 `runtime/` 打进 `_internal/runtime`。
- `packaging/entry.py`：冻结入口。`atri-x.exe` 转发到 `atri_x.__main__:main`；`atri-x-gui.exe` 按文件名分流到 `atri_x.gui.__main__`；`-m <模块>` 用 `runpy` 复刻 `python -m`（冻结包没有解释器）。
- `packaging/build.py`：构建 + **产物级核验**（跑 exe 的 `--version` / `config check` / `-m ... --help` / `-m atri_x.sidecar status` / 窗口版 `--selftest`，前几步把 `APPDATA`/`XDG_CONFIG_HOME` 指到 `build/pyinstaller-verify/`）。构建退出码为 0 不代表产物可用，核验步骤不能省。
- `packaging/assemble_kernel.py`：从本机已装好的 DSH 组装 `runtime/dsh-kernel/`。闭包根来自 `dsh --profile acp --dump-config`，剪掉 Web/桌面前端包，然后用**真启动 ACP 服务端 + `initialize` 握手**核验，缺包自动补拷重试；`packaging/kernel_manifest.py` 负责 `kernel.json` 的读写（产物契约）。
- `atri_x/sidecar/`：运行时发现层。`registry.py`（`ATRIX_RUNTIME_DIR` → `<发行包>/runtime` → `<配置目录>/runtime`；`kernel.json` 清单、`format` 版本守卫）、`kernel.py`（`find_kernel` / `dsh_command` / `describe`，找不到就回退外部 `dsh`）、`__main__.py`（`python -m atri_x.sidecar list|status|dsh-command|verify [--acp]`）。
- `atri_x/gui/`：桌面端。`app.py`（Tk 主窗口：状态栏 + `ttk.Notebook` 选项卡容器 + 菜单/托盘联动 + 跨线程事件队列）、`actions.py`（「服务」动作：启动/重启/停止后台服务、打开目录/日志、关于，重启在后台线程里跑）、`about.py`（「关于」窗口：简介/仓库地址/推荐语/`POWERED BY DAFEIYU`）、`tabs/`（各选项卡：`base.py` 公共骨架 + 操作日志、`chat.py` 对话、`model.py` 模型与 API Key + Provider 请求头（`extra_headers`）+ 模型目录/备注名/模型分配 + 「搜索服务」子页（	abs/searchpanel.py）+「抓取服务」子页（	abs/extractpanel.py）、`character.py` 人设卡与 `SOUL.md`、`platform.py` 平台接入、`link.py` 在线页（后台服务/协议端/QQ 平台三段状态 + 连接开关 + 唤醒词编辑）、`members.py` 群友角色数据（列表/头像/档案编辑）、`knowledge.py` 群知识库（`[group_knowledge]` 全局配置 + 只读 `state.toml` 条目）、`advanced.py` 生图/视觉/ASR/记忆开关 + 生图模型目录 + 本地生图挂载 + 三个二级窗口入口（整页竖向可滚动、两列布局）、`logs.py` 日志查看（五通道 + 筛选 + 自动刷新 + 导出）、`features.py` 功能总览 + 服务日志面板、`toolperm.py` 工具权限页（左栏群列表 + 作用域输入、右栏纵向滚动的工具菜单：激活按钮 + 状态徽标 + 一句话功能解释 + 搜索/只看已激活/预设；chat / work 两个子选项卡 + 第三个「工具参数（全局）」子选项卡；群聊工具进度开关）、`toolcatalog.py`（49 个内置工具的中文名/功能解释/分组/风险静态目录，回归用例钉住与 `REGISTERED_TOOLS` 集合相等）、`toolmenu.py`（工具菜单行控件：状态徽标、激活按钮、行配色）、`toolpolicy.py`（群工具白名单读写 `load_scope_policies`/`save_scope_policy` + 常用/只读预设，无 Tk）、`toolmode.py`（chat / work 分模式编辑器：三态 + 搜索 + 预设 + 纵向工具菜单 + 工具行「参数」跳转）、`toolsettings.py`（「工具参数」子选项卡的数据层：工具 → 配置键注册表、按分类分组、`load_all` / `save_group`，无 Tk；参数定义在 `toolsettings_general.py` / `toolsettings_media.py`，字段类型与「界面值 → TOML 值」在 `toolsettings_base.py`）、`toolparams.py`（「工具参数」子选项卡控件：一组参数一个可展开 / 收起面板 + 保存本组 / 保存全部 / 从工具行定位））、`dialogs/`（「进阶」页二级窗口：`common.py` 公共窗口骨架、`selflearning.py` `[self_learning]`、`hooks.py` `plugins/hooks.toml` 编辑器、`tasks.py` 维护任务清单 + 表单 ↔ cron 翻译（无 Tk）、`maintenance.py` 维护任务窗口：每天/每周/每月/自定义 cron + 一次性调度 + 下次触发预览）、`cronschedule.py`（界面用 cron 工具：「每天/每周/每月」↔ 5 段表达式、人话摘要、下次触发计算）、`widgets.py`（`SecretEntry` 掩码/明文切换、可滚动 `dropdown`、竖向滚动容器 `ScrollFrame`、`run_async` 后台 IO 回主线程）、`modelcatalog.py`（`gui/model_catalog.json`：`/models` 拉取 + 模型备注名，备注显示成 `备注（模型）` 保存时还原）、`imagemount.py`（本地生图服务挂载：端口映射/SSL 链接 → OpenAI 兼容 base_url，只读探测 OpenAI/ComfyUI）、`memberdata.py`（`gui/members.json` 群友档案 + 只读聊天历史库聚合群友/群列表）、`avatars.py`（QQ 头像下载 + 内置 imagecodec 转 96×96 PNG 缓存到 `gui/avatars/`）、`journal.py`（`gui/gui.log` 界面操作日志）、`configedit.py`（配置路径解析 + TOML 行级补丁/备份/校验 + `.env` 读写 + 文本文件写入；同时推 `[storage]` 数据目录/日志目录给只读页）、`configmodel.py`（Provider 视图（含 `extra_headers` 子表）与密钥状态、`secret_value` 明文取值、模型分配读写）、`featureview.py`（「功能」页状态汇总，只读）、`client.py`（WS 客户端，复用 `atri_x/platform/cli/protocol.RemoteMessage`）、`protocol.py`（帧 → 界面状态的纯逻辑，含 `sessions_result`/`history_result` 解码）、`sessions.py`（会话侧边栏 + 历史灌入；打开会话 = 发 `/resume <id>` + 请求 `history`）、`service.py`（**唯一会改用户配置的启动期逻辑**：`[platform.cli.server].enabled` + `.env` token + 健康接口 `ATRIX_HEALTH_ADDR`/`ATRIX_OPS_TOKEN`（已有值不动）；以及探测/拉起/停止/重启 `service run`）、`linkstate.py`（「在线」页的数据层：平台配置读写、协议端 TCP 探测、`/healthz` 客户端与解析、状态文本格式化；不依赖 Tk）、`qqproto.py`（内置 NapCat/便携 QQ 协议端：OneBot 令牌、端口选择、NapCat `onebot11.json` / `webui.json` 与 `[platform.qqonebot]` 补齐；没有内置环境时一个文件都不写）、`process.py`（程序/状态目录、窗口位置、pid 记录、强制停服务、单实例互斥量）、`tray.py`（`ctypes` Shell_NotifyIcon；图标尺寸按 `SM_CXSMICON` 取，让 Windows 从多尺寸 ico 挑档位）、`icon.py`（品牌图标素材解析：`brand/app.ico` 与 `brand/app.png`，exe / 窗口 / 托盘 / 安装包共用同一份，源码与冻结后同一套 `__file__` 相对查找）、`autostart.py`（注册表 Run 项）、`selftest.py`（`--selftest` 的 Tk 自检，含逐页切换）、`__main__.py`（单实例/`--minimized`/`--no-service`）。GUI 私有数据都在 `<配置目录>/gui/`（`model_catalog.json` / `members.json` / `avatars/` / `gui.log` / `service.out.log`），不进 `app.toml`/`services.toml`。配置写入分工见 `atri_x/gui/configedit.py` 模块 docstring：`state.toml` 热加载，`services.toml`/`.env`/`app.toml` 需重启后台服务；`plugins/hooks.toml` 可用 `/hooks reload` 热重载。
- `atri_x/gui/searchconfig.py`：`[web_search]` 的桌面端数据层（通道名/顺序/协议/密钥环境变量名/服务地址/CLI 可执行文件；删旧小节再写、自定义名校验、默认链）；`atri_x/gui/tabs/searchpanel.py`：`ModelTab` 的「搜索服务」子页（通道列表 + 表单 + 排序 + 写 `.env` + 恢复默认链）。
- `atri_x/gui/extractconfig.py`：`[web_extract]` 的桌面端数据层（通道名/顺序/协议/密钥环境变量名/服务地址；校验与密钥回落口径）；`atri_x/gui/tabs/extractpanel.py`：`ModelTab` 的「抓取服务」子页（通道列表 + 表单 + 排序 + 写 `.env` + 恢复默认链）。
- `atri_x/dist/`：分发件。`shell_link.py`（COM `IShellLinkW`/`IPersistFile`）、`registry.py`（HKCU 卸载项 + 用户 PATH）、`paths.py`（安装/数据/快捷方式目录判定）、`install.py`（`python -m atri_x.dist.install install|uninstall|status`：复制到 `%LOCALAPPDATA%\ATRI-X\app`、快捷方式、HKCU 卸载项；卸载默认保留用户数据；检测到 `install.json` 里 `installer=inno` 时拒绝脚本卸载并提示 `unins000.exe`，`--force` 可强删）。
- `packaging/atri-x.iss` + `packaging/build_installer.py`（**Python 版新增**）：Inno Setup 安装包。用户级安装（`PrivilegesRequired=lowest`，不弹 UAC）、`%LOCALAPPDATA%\ATRI-X\app`、开始菜单快捷方式、默认勾选开机自启、安装时写 `install.json`（`installer=inno`）、卸载前 `taskkill` 后台服务、`--verify` 静默安装/卸载自检（安装包先复制到本地盘、装到短路径；TEMP 不能指向映射盘，否则 Inno 子进程受限、HKCU 写入 `code 5`）。ISCC 查找顺序：`--iscc` → `ATRIX_ISCC` → `packaging/tools/ISCC.exe` → 常见安装路径。
- `packaging/qqproto.py`（**Python 版新增**）：把 NapCat/便携 QQ 协议端环境**脱敏后**嵌进发行包（`<dist>/qqproto`），跳过 `resources\app\napcat\` 的 `config`/`cache`/`logs` 与 `*.log`，写 `Start-NapCat.bat`/`Stop-NapCat.bat`/说明/`proto-info.json`，并核验必需文件、路径长度与文本脱敏；`build.py --with-qqproto <Shell 目录>` 调用它，运行期配置由 `atri_x/gui/qqproto.py` 在目标机首次启动时生成（包里不含令牌）。
- `packaging/README.md`（构建细节与冻结限制）、`docs/windows-app.md`（用户侧：桌面端/托盘/自启/安装/接 QQ/内置内核/排障）。
- 既有 Docker 形态在 `deploy/windows/`（Docker Desktop 生命周期与计划任务）与 `deploy/`（Linux 容器）；四种形态共用同一份 `app.toml` / `services.toml` / `state.toml` / `.env`。

常用搜索：

```bash
rg -n "assets_data|ATRIX_PKG_|collect_submodules|freeze_support|-m atri_x" packaging docs/windows-app.md
rg -n "sidecar|dsh-kernel|assemble_kernel|is_gui_exe|Shell_NotifyIcon|IShellLink" atri_x packaging devdocs/dsh-acp.md
```

<!-- locator:docs -->
## 文档与变更记录

适用任务：用户文档、开发文档、移植状态、changelog。

先看：

- `docs/`：中文用户文档。
- `devdocs/`：维护者/Agent 开发文档。
- `devdocs/porting-status.md`：Go → Python 文档移植状态与待核对清单。
- `CHANGELOG.md`：中文变更记录（补丁式写作）。

不要手动修改（本仓库**不保留**英文镜像，上游 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物，不随移植复制）：

- `atri_x/**`、`deploy/**`、`tests/**`、`scripts/**`、`PORTING.md`、`pyproject.toml`（并行实现者的写范围）

常用搜索：

```bash
rg -n "locator:|CHANGELOG|porting-status" AGENTS.md docs devdocs scripts
```

