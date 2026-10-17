# ATRI-X 架构说明

本文写系统如何流转、模块如何协作。只想找代码入口时，优先看 `devdocs/code-map.md`。

定位方法：

```bash
rg -n "locator:tool-flow" devdocs/architecture.md
```

<!-- locator:startup -->
## 启动与装配链路

简化链路：

1. `atri_x/__main__.py` 创建根 context，并把命令行交给 launcher。
2. `atri_x/launcher/cli.py` 解析 `run`、`cli`、`service run`、补全和远程 CLI 参数。
3. 普通运行进入 `atri_x/app.Run`，由默认 `Runner` 执行；远程 CLI 进入 `atri_x/app` 的 CLI client 入口。
4. Runner 按 Environment、Foundation、Models、Platforms、Runtime、Integrations 阶段装配配置、日志、SQLite、LLM、Agent、Tool、Platform、Hook、Output、Cron 和 Atrinis。
5. app 层按运行模式启动平台 runtime，并在平台启动后异步启动 Cron runtime。

设计边界：

- app 层负责装配，不承载业务逻辑。
- launcher 只做命令行解析，不直接初始化复杂依赖。
- 平台 adapter 只处理平台输入输出，不直接驱动 LLM。
- `app.Run` 保持默认生产入口；需要替换启动阶段或做隔离测试时，使用 `NewRunner(Dependencies)` 注入分组工厂。
- Runner 逆序释放已完成阶段：Hook runtime 先于 Cron，Cron 先于 SQLite 和日志；阶段失败不得启动后续阶段。

<!-- locator:config -->
## 配置与运行数据

配置约定：

- 应用/行为静态配置：`app.toml`。
- 共享只读服务配置：同目录 `services.toml`（`[providers.*]`、`[model_metadata]`、`[model_profiles]`、`[image_generation]`）；旧部署仍支持 `providers.toml`。
- 运行时模型状态：同目录 `state.toml`；由 ATRI-X 回写，必须与只读配置分离。
- `state.toml` 支持外部编辑热加载：`Agent.refreshRuntimeState` 按 mtime 判定外部修改并合并到内存，`StartRuntimeStateWatch` 每 15 秒轮询，`/state reload` 强制立即生效；`saveRuntimeState` 在写回前先执行一次合并，避免内部写回覆盖手工修改。热加载覆盖模型选择、`context_overflow`、`group_policy`、`group_knowledge`、`group_services`、`group_runtime`；`[budget]` 账本由运行中进程独占，只在启动时从文件恢复。
- 工具 tag 配置：同目录 `tool_tags.toml`。
- 用户可编辑资产：配置目录下的 `memories.toml`、`long_memory/`、`skills/`、`plugins/`。
- Hook 配置：入口为配置目录 `plugins/hooks.toml`；被引用插件使用 `plugins/<plugin-id>/hook.toml`，持久 Hook 在其中声明 `[plugin.runtime]`。
- Provider key 推荐用 `api_key_env`，读取优先级是系统环境变量高于配置目录 `.env`。

默认配置查找顺序：

1. `--config`
2. `ATRIX_CONFIG_FILE`
3. 平台配置目录

<!-- locator:health-ops -->
## 健康状态、限速、熔断与运维接口

健康状态由 `atri_x/health` 统一维护，语义边界如下：

- `/live` 只由进程存活决定：HTTP 服务可响应且未进入 shutdown 即返回 live；心跳、平台连接和模型健康都不影响该接口。
- `/ready` 由进程初始化、数据目录/SQLite 目录可写、以及调度心跳“已知且未过期”决定；平台未连接、模型 API 故障不会让 `/ready` 失败。
- `/healthz` 汇总上述状态；平台或模型异常显示为 `degraded`，调度心跳过期时因 readiness 检查失败返回 `not_ready`；平台与模型状态数组始终保留。
- `runPlatforms` 无论是否有已启用平台都约每 10 秒发送一次心跳，避免空平台部署被误判为卡死。
- 平台运行时的退出分两类：运行上下文未结束的**非预期退出**会记 WARN、把该平台在 health 里标成断开（`MarkPlatformDisconnected`），并通知其他仍在运行平台的配置超管；`StopAppOnExit`（交互式 CLI）只在**非** service 模式拥有应用退出职责。service 模式下平台循环不拥有进程生命周期——全部运行时退出后仍等待外部停止，`/live`、`/ready`、`/tasks` 保持可用。

运维接口：

- `/tasks` 返回活跃请求、`pending_by_kind`、`queued_by_kind` 和累计超时数；`/metrics` 返回任务/资源/平台/模型状态、Provider 熔断状态、限速阈值和拒绝原因、生图队列等。
- `/diagnostics` 是面向排障的聚合视图：排队/超时、限速命中、熔断状态和最近一次重启原因。watchdog 会把重启/暂停原因写入 `data/run/atri-x/last_restart_reason`，进程启动时读取并写入 health snapshot。
- 配置 `ATRIX_OPS_TOKEN` 后，extra handlers 需要 `Authorization: Bearer <token>` 或 `X-Atrix-Ops-Token`；健康三接口保持无鉴权但只应监听回环或可信内网。
- 平台 / 模型的 `last_error` 和最近重启原因在写入 `State` 前会经过 `health.RedactSecrets`（URL、`sk-`、Bearer、`key=value` / JSON 凭据、Telegram bot token），避免上游错误里的 token 随 `/healthz`、`/metrics`、`/diagnostics` 外泄；这是第二道防线，不替代接口访问控制。
- `python -m atri_x doctor` 是部署验收命令：第一阶段检查配置、存储目录、健康端口、平台状态和模型调用（`config_ok`）；加 `--e2e` 后通过 CLI 远程 WebSocket 完成一次真实消息往返（`e2e_ok`）。
- `deploy/upgrade.py` 升级前做配置检查、数据快照和上一版镜像快照，并用 `deploy/VERSION` 守卫源码版本（`ATRIX_GIT_REF` 可自动切标签）；重建后等待 healthcheck 并通过 `python -m atri_x doctor` 验收才判定成功。`deploy/rollback.py` 先用 `restore-verify.sh` 校验数据快照，再恢复 data 和镜像。
- `deploy/backup.py` 的清单从归档内容生成；`deploy/restore_verify.py` 默认严格模式（manifest + sha256、sqlite3 integrity_check + 表结构、必需配置与 TOML 解析、本地媒体精确路径），`RESTORE_VERIFY_STRICT=0` 才降级。
- `deploy/atri_x_watchdog.py` 只依据 `/live` 决定重启；`/ready` 连续失败只发 `not_ready` 告警（需 webhook）。诊断包按 `REDACT_SED` 规则逐文件脱敏并复检，失败写 `REDACTION-FAILED`；`--redact-dir` 可单独重脱敏，`tests/test_deploy_watchdog.py` 是 fixture 自测。

Provider 熔断与备用：

- `breakerLLM` 按 Provider 统计失败；默认 `fallback_mode = "circuit"`，仅在熔断打开后切备用 Provider；`fallback_mode = "on_error"`（兼容 `fallback_on_error = true`）对预流式失败立即切换。
- `fallback_timeout_seconds` 作为单次 Provider 尝试的总超时；已经输出部分流式内容后不重放备用响应。
- 熔断状态写入 health model status，供 `/healthz` 与 `/metrics` 展示。

限速与子进程环境边界：

- 群聊入站先检查用户级令牌桶，再检查群级令牌桶；超级管理员绕过。拒绝计数和最近原因进入 `/metrics.rate_limit`。
- Shell / Go Skill 子进程使用进程环境和配置根 `.env` 中的非凭据变量；名字按词段包含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`PRIVATE` 的变量会被移除。Web 搜索、生图、媒体下载等父进程工具仍使用完整凭据环境。

角色素材在 `character.toml` 中记录 `version` / `source`，图片索引也带版本和来源；`Store.Manifest` / `WriteManifest` 生成 sha256 备份清单，备份脚本会对 `characters/` 和 `media/` 生成 manifest，恢复时校验。临时 `@char:` 指令仍是单轮注入，不写入 Session，回复结束即失效，天然不会串到其他会话。

<!-- locator:agent-chat -->
## Agent 对话链路

普通输入简化链路：

1. 平台 adapter 收到消息并交给 Agent。
2. Agent core 判断 slash 命令、普通输入、工具 pending 输入和风险确认命令。
3. 普通对话加载 Session 上下文、构建 Prompt、选择模型并调用 LLM。
4. LLM 返回文本、reasoning 或 tool call。
5. 如果有 tool call，Agent 进入工具执行链路；工具结果写入 transcript 后继续 LLM 循环。
6. 生成最终 assistant 输出前，先跑输出预处理 Hook。
7. Output Manager 负责实际发送，成功后保存最终 assistant 消息。

关键约定：

- user 与已完成工具 transcript 会阶段性落库；前置 Hook 绑定的当前消息在调用 LLM 前以最终 segments 落库。
- 多模态消息的 `segments` 保存原始结构；`content` 由 segments 生成可读文本投影。请求 OpenAI-compatible 模型时，再按每条消息的图片顺序临时插入对应文本标签，不向 segment JSON 增加派生字段。
- 流式输出最终由对话主流程用最终文本 replace。
- 发送前会发布 `sending` phase，便于 `/requests` 区分 LLM 慢还是平台发送慢。
- 普通输入在工具阶段不会打断工具，会以 text/image segments 进入 pending；下一次 LLM 调用前已有的 pending 会合并注入当前轮，最终 LLM 调用期间新到达的 pending 则在当前轮正常结束后作为新用户消息自动开启下一轮。
- 群可用 `thread-mode = group` 切换为共享 Session：普通输入先进入 `atri_x/agent/inbox.py` 的有序队列，同一成员在 `merge-window` 内的连续消息合并为一轮，不同成员按到达顺序串行；队列项保留各自的 Actor 与发言成员元数据，撤回/退群会同步清理尚未运行的队列项。
- 群普通输入在进入 Session 前会先经过本地确定性 FAQ 匹配：命中时由服务端直接发送答案并结束本轮，不创建 Session、不调用 LLM；未命中才走正常对话链路。FAQ 仍受唤醒、静默时段、入站限流和群运行状态约束。
- 普通成员可用 `/me` 查看自己的 FairKey 任务、排队消息和本群/本人额度；面板隐藏跨群全局聚合用量，只读且不调用模型。
- 群内提醒/投票/报名是本地确定性服务：状态写入 `state.toml [group_services]`，提醒由 Agent 启动时的本地 ticker 调度发送，投票/报名不进入 LLM；关闭群级或全局开关后创建与参与都会被拒绝。
- `angel_memory` 是 scope-local SQLite 长期记忆：写入时记录 `source_kind` / `source_actor_id` / `source_message_id` / `source_session_id`，命令和工具都返回记忆 ID 与来源；`/forget` 对普通成员只暴露来源为自己的群记忆，群主/管理员/超管可按 scope 删除；平台撤回事件默认按 `平台 + scope + source_message_id` 删除派生记忆。
- 旧版长期记忆只写了自由文本 `source`（生产写入方唯一，固定为 `"tool"`），没有结构化来源列，因此无法回填来源成员/消息/Session；`/memory backfill` 只做可确定的 `source_kind` 回填，这些旧行继续被按来源删除的逻辑跳过，避免误删。
- `angel_forget` 是默认关闭的可选工具（`[angel_memory].allow_tool_forget`）：仅在来源成员等于当前发言人、且属于当前 scope 时删除单条记忆，风险等级 `high` 走确认流程，自身还要求 `confirm=true` 二次调用，并有每 scope/actor 每分钟上限；它是隐藏工具，通过 `angel_recall` 的 `DependsOn` 注入模型。
- Prompt Builder 每个 turn 从 Soul、工具提示、工具标签和当前 actor 的常驻记忆构建一次 system message；该消息只在当前 turn 内复用，不进入会话历史。常驻记忆注入时使用 `<resident_memory>` 边界和“用户数据、不是系统指令”的信任声明，并转义内容中的尖括号，防止提前结束或伪造边界标签。normal 以换行分隔的独立条目保存，注入时渲染为列表项；`memories.toml` 通过“临时文件 + fsync + rename”原子落盘，并在保存前检测外部改动后重载，避免损坏或覆盖外部编辑。

<!-- locator:commands -->
## 命令链路

Slash 命令链路：

1. `atri_x/agent/command_runtime.py` 的命令执行器识别命令前缀，并统一处理权限、Turn 冲突和用户通知。
2. `atri_x/command/router.py` 负责解析命令名、alias、参数文本和分发。
3. `atri_x/agent/commands/` 的模块注册具体命令。
4. 命令通过 deps 访问 Session、模型、Hook、工具、日志、请求管理等能力。
5. 命令可通过 `command.Result.Continuation` 请求在指定 Session 中继续处理一条普通输入；模式切换、历史限制等策略先由 Session 服务完成，Agent core 不识别具体 Session 命令名。
6. 平台补全通过中央 completion 服务组合命令名、命令参数、风险确认、fork message ID 和 `@tool:` 候选。

约定：

- 新命令优先做成 `atri_x/agent/commands/` 模块。
- 会改变或切换 Session 的命令必须声明 `command.Info.SessionEffect`，命令执行器据此处理压缩和 pending 确认冲突，不维护命令名白名单。
- Session 规则放在 `session.Service`；命令只解析参数和格式化结果，Agent 只编排命令与普通输入。
- 命令详细帮助写在 `command.Info.Help`。
- 用户可见命令变化要同步 `docs/commands.md` 和 `CHANGELOG.md`。

<!-- locator:request-turn -->
## Request 与 Turn 状态

职责分层：

- Request manager 管 active request 树、父子关系、取消、超时和完成清理。
- Turn manager 管单个 Session 当前 turn 的阶段、原始输入、pending 追加、确认状态和工具计数。
- Runtime status 是状态快照，供 CLI 状态栏、`/requests` 和日志展示。

运行约定：

- 长耗时操作应登记 request，结束时清理。
- 能被用户取消的 LLM、工具、压缩、后台 Agent 请求应挂入 request 树。
- 当前阶段变化要更新 runtime status，避免 `/requests` 只能看到“卡住”。

<!-- locator:tool-flow -->
## 工具调用链路

简化链路：

1. LLM 返回 tool call。
2. Agent 进入工具执行阶段并记录工具调用请求。
3. prepared Hook 只能改写 arguments；ToolRun 用最终参数做工具视图、命名解析、foreground-only 过滤、权限和风险确认，并把同一参数回灌当前 assistant tool call。
4. Tool Runtime 执行具体工具，并按 Actor/Policy 做风险兜底校验。
5. 已进入实际执行阶段的工具结果以 text/image segments 通过完成 Hook，随后写入 transcript；纯文本只存 `content`，多模态结果额外存 `segments`。执行前失败或拒绝不触发完成 Hook。
6. 如果工具有输出意图，交给 Output Manager 发送，而不是工具直接发平台消息。

关键约定：

- 风险等级用于内部权限和确认，不暴露给 LLM。
- `Result.Content` 或 typed `Result.Segments` 回灌 LLM。
- `Result.Data` 只供内部结构化消费，不进入 tool message。
- 图片和文件必须显式返回 segment。
- OpenAI Chat Completions 的 tool message 只发送文本；同一批工具图片在所有 tool message 后派生为一条带 `tool.name/tool_call_id` 说明的 user 多模态消息，且每张图片前临时插入含序号和可复用 URL 的文本标签；这些派生内容都不持久化。
- Hook、Tool、插件都不要直接发平台消息，统一返回输出意图。

<!-- locator:tool -->
## Tool Runtime 与工具发现

Tool Runtime 负责注册、schema、权限、风险、确认详情、用户侧 tags 和工具结果。

工具视图由 ToolRun 提供：

- 管理 session 工具缓存。
- 合并 native/Atriwisp 工具。
- 按前台/后台过滤 foreground-only 工具。
- 处理工具名解析、风险确认和批量工具预览。

`discover_tool` 的特殊约定：

- 查询普通工具时，返回“已发现工具”文本，并把完整 schema 放在结构化 Data 供 Agent 注入 top-level tools。
- 查询说明型 AgentSkill 会激活 `agent_skill` 元工具。
- 查询工具化 AgentSkill 会注入其 top-level schema。
- 查询 Go skill 会按需激活 `go_skill_run`。

### 媒体引用与清理

媒体本体按 SHA-256 去重，`media_references` 是引用事实来源，不维护整数计数。消息追加/替换、Session 删除、fork、工具参数、Cron 报告状态、Atrinis 排队 ID/outbox、输出关联与引用通过事务及 SQLite 触发器同步。工具通过确认并即将执行时，ToolRun 递归检查最终参数的 JSON 值，将完整且有效的媒体 ID 作为 `session_tool` owner 原子关联到当前 Session；同一 Session/媒体幂等，失败执行仍保留，执行前拒绝或跳过不关联。新 fork 按检查点边界继承父消息、祖先 fork 及此前的工具参数媒体；Chat History 的原始平台 URL/file ID 不构成中心引用。读取、LLM 请求和发送期间另有临时引用。

媒体中心在持久化及对外返回副本时统一规范化名称和来源：名称只保留跨平台 basename，平台文件 ID 只保留不透明 ID，来源 URL 不保留用户信息、query、fragment 或 Telegram token 路径。实际下载仍使用清洗前的调用参数；旧记录按需清洗返回副本，不批量回写或重算媒体 ID。

`ImportReader` 在导入硬上限校验后、内容哈希和后端写入前统一检查图片；字节数超过 `[media]` 阈值或边长达到限制时，压缩为白底 JPEG，只保存压缩内容并返回对应 ID、名称、MIME 和大小。入站消息与历史关联直接保存该 ID。`ResolveForLLM` 物化请求副本，按持久化媒体大小选择 base64/S3/hybrid。S3 后端按需初始化，配置不可用只告警并使实际远端操作失败，不阻止应用启动。

聊天历史查询不下载媒体：`search_chat_history` / `get_chat_history_around` 按媒体顺序展示编号和 `[图片 media:未下载]` 或已有媒体 ID。`get_media(message_id=[...], media_index=[[...],...])` 限定当前平台/scope；序号从 1 开始，省略索引时每消息取首个媒体。只有显式获取才下载，单次最多尝试 5 个未入库媒体，失败计数，缓存和同次重复位置不额外占额度；结果为纯文本，不返回图片内容。

Chat History 库只保存清洗后的原始来源。主库 `media_history` 保存历史记录内部 ID、平台/scope/消息 ID、媒体位置及媒体 ID，并以触发器同事务维护 `chat_history` owner 引用。回复历史消息时按非文本媒体位置和当前历史内部 ID 恢复仍有效的媒体 ID，不下载或重新解析来源；缺失位置继续使用原有来源。入站实际消费和 get_media 共用导入与关联规则；文本工具结果中的 ID 本身不建立 Transcript 媒体引用。删除历史成功后释放关联；启动和媒体清理前按主库已有关联分页核对历史 owner，消息不存在或内部 ID 已替换才释放，历史库故障保守停止。关联替换使用新的 owner ID，避免延迟对账删掉新关联。最后引用释放后继续遵循 1 小时孤儿宽限期，不扫描/下载全部历史。

Atrinis direct 实际投递才导入中心，不生成 sandbox 下载副本；LLM 输入在执行时物化。workspace 普通文件不入库，报告附件准备投递时安全导入，outbox 只保存稳定 ID。Agent 通用发送边界以结构化回执建立平台/scope/消息 ID/segment index 到 kind/media ID 的有限期有序映射，重复媒体位置独立保留；Atrinis 不再单独缓存发送关联。

输出缓存到期释放引用，待重试和 Session 引用独立保留。最后引用释放后记录 orphaned_at，无引用资源再次使用会刷新时间；宽限期固定 1 小时。清理认领与引用添加在 SQLite 写事务中互斥，认领后禁止新引用，并按记录中的实际存储位置选择后端；双副本先删除远端再删除本地，全部成功后才移除记录。所需后端不可用或任一删除失败时保留 deleting 状态供幂等重试。共享 Manager 的后端导入/清理由同一锁串行化，防止删除和内容寻址重建交叉。

媒体维护任务复用 sandbox 清理时间表，独立于按文件年龄清理的 workspace 任务；已发送缓存复用 retention_days，非正值不缓存。启动时在任务运行前恢复 Hook/Skill/request 临时引用，并将无法恢复的内存队列事件标记失败；持久化 outbox 保留。清理前只读检查消息、outbox、输出、Cron 报告及 fork 历史的缺失引用和悬空 owner，一致性异常时保守停止并报告。Atrinis 终态写入使用独立于调用取消的有界清理 context，使失败状态和事件引用释放在同一事务内完成。

<!-- locator:skill -->
## Skill 架构

Skill 分三类：

- AgentSkill：`skills/agent/<name>/SKILL.md`，可选 `ATRIX_SKILL.toml` 做文档可见性限制或工具化。
- 原生 ATRI-X Skill：使用 `SKILL.atriyph` 描述任务和规则，可选 Go 源码。
- Go skill：`skills/go/<name>/SKILL.atriyph`，可选编译产物，通过隐藏 wrapper 执行。

关键链路：

1. Skill scanner 扫描配置目录下 `skills/`。
2. Catalog 记录名称、详情格式、风险、根目录、binary 和工具化状态。
3. `discover_tool` 暴露 Skill 详情或激活对应 wrapper。
4. 原生 ATRI-X Skill 创建/修改后需要 finalize，执行 lint、python scripts/check_syntax.py、build 和 reload。

Reload 由 Skill Manager 串行执行：scanner 先构建并验证完整候选集，registry 在单次写锁内替换 Agent/Go Skill 快照，成功后再替换 catalog；任一步失败均保留旧运行快照。`agent_skill` 写入 `ATRIX_SKILL.toml` 后若 reload 失败，会在同一管理事务内恢复原文件。

Skill 媒体处理发生在具体工具的执行阶段，权限/风险评估不导出文件。`tool.MediaRuntime` 复用 Media Center API 管理显式输入、调用期引用和临时导出；ToolRun 会递归识别参数 JSON 中完整的媒体 ID 以建立 Session 引用，但不扫描自由文本，也不递归替换任意字符串参数。shell 解析 `media_inputs` 并注入调用级 `ATRIX_MEDIA_N`；Go runner 处理 `payload.media_inputs`；TOML 工具只处理 `type=media` 的顶层参数，对 LLM 投影为字符串 schema。

shell 导出缓存位于 sandbox 的 `media-inputs/`，按内容 ID 命名，首次导出原子发布，复用时刷新 ModTime，直接沿用 sandbox 时间清理。Go/TOML 保持 Skill 根目录为 cwd，媒体输入使用调用专属子目录和相对路径。stdout 媒体段通过 `os.Root` 校验、导入并生成稳定 ID，随后清理调用目录和引用；落库沿用 message/tool_result 引用事务。普通文本中的 ID 不触发转换。

<!-- locator:hook -->
## Hook 链路

普通 Hook Manager 按事件点和优先级串行执行 Handler，`hook/control.Service` 作为 `/hooks` 的独立管理入口，组合普通 Manager、持久 Runtime 和配置 loader。

平台连接成功是**进程级事实**，不再由连接回调直接转给 Hook：适配器（`platform.ConnectNotifier`）只 `Emit` 全局信号 `platform.PlatformConnected`（`atri_x/platform` 的 `ConnectedEvent{Platform}`），Agent 在 `StartPlatformEvents` 里 `Observe` 它，并按平台各自排进一条 `signal.Queue`（`atri_x/agent/platform_signals.py`；默认 256 等待 / 1 并发，同一平台串行、平台之间互不排队），再在队列 worker 上跑 `platform.connected` 的规则 Hook 与 `builtin.cron.missed_once` 补投递。因此连接回调不会被 Hook 规则或 cron 的模型补投递阻塞。关闭时 `ClosePlatformEvents` 先停止接纳、再排空，超出 `atri_x/app` 的 30 秒关闭预算则记为关闭错误。

常见来源：

- 规则 Hook：读取 `plugins/hooks.toml`。
- exec action：按 `hook.v2` 一次性 Pipe 协议执行，默认在 `plugins/` 目录直接启动 argv 命令，不隐式经过 shell；取消或超时时终止该次调用的完整进程树。
- 持久 Hook：在插件 `hook.toml` 的 `[plugin.runtime]` 声明；Hook runtime 管进程生命周期、双向 RPC、waiting 路由和进程内 SharedState。

约定：

- Hook 配置先加载到候选 Manager，并完成持久 Runtime 配置校验；候选构建或校验失败时保留当前活动 Hook。提交时先一次性替换 Runtime worker 索引，再原子替换普通 Hook handler 快照。
- 持久进程启动仍是异步生命周期，reload 提交后可短暂处于 `starting`，进程后续失败由既有状态和重启策略处理。
- 所有进程 Hook 共用启动时构建的环境快照：进程环境优先补充配置 `.env`，PATH 按进程目录在前、`.env` 目录在后合并；argv 首项也用该 PATH 解析。
- Hook 可返回控制字段和输出意图。
- Go Hook 通过事件提供宿主 `MediaAPI`；进程 Hook 通过 `media.import`、`media.read`、`media.export` 和 `media.metadata` 使用媒体。稳定 `media` 引用可跨消息传递，Host 仅在发送边界导出为临时文件；临时 Hook 引用在过期或 runtime 关闭时释放，外部 Hook 不接触 SQLite、媒体根目录或 S3 凭据。
- 入站消息的唤起状态在 Agent 消息入口计算一次并随 context 贯穿处理链；后续 Hook 不根据已改写的 user 文本或 assistant 输出重新推断。
- `llm.messages` 对普通 Hook 只读并以深拷贝提供；turn Hook 只能修改当前初始 user，request Hook 只能修改本次请求前新 drain 的 pending。
- 进程 Hook 可用 `message.segments` 替换当前绑定消息；用户/pending 修改在请求前落库，工具完成 Hook 的修改进入 transcript 和后续 LLM 请求。
- Hook 不直接发平台消息。
- 输出预处理 Hook 运行在 assistant 最终发送前。
- 命中 waiting 租约的消息在常规平台 Hook 后、命令和主 LLM 前交给持久 Hook；`/cancel` 只取消该路由执行，不停止进程。
- Hook 用户文档优先看 `docs/hooks.md`。

<!-- locator:output -->
## Output 与发送链路

输出层把 Agent、Hook、Tool、Atrinis 等来源的输出意图统一发送到平台。

职责：

- 定义 text/image/file/record/at/reply/emoticon 等平台无关输出类型。
- 提供媒体源前缀、fallback 文本、delivery timing 元数据。
- 统一处理发送回执、流式发送和普通发送。

约定：

- 业务层返回输出意图，不直接调用平台 adapter。
- 平台 adapter 负责把平台无关输出转换成平台 API。
- Agent 在发送前统一把 URL/Path/Data 归一为 MediaID，发送副本经 `ResolveForOutput` 临时解析，回执按实际成功的输出索引建立关联。多目标 scope 由 adapter 明确提供；缓存期限复用 sandbox retention，非正值不缓存。
- QQ OneBot 把 record 输出转换为原生语音段；暂不支持 record 的平台使用统一文字 fallback。
- 流式输出、notice、reasoning、runtime status 由 Agent turn 输出适配层区分前后台发送。
- 通知（`Agent.SendNotice`）按平台排队后再发：每个平台一条 `atri_x/signal.Queue`（默认 256 等待 / 8 并发），串行键是"平台 + scope + 收件人"，所以同一目标的发送不交叉、平台之间互不排队；调用方仍然同步等回执，队列满时立刻拿到 `ErrQueueFull`（背压，不是故障）。关闭时先停止接纳再排空，纳入 `atri_x/app` 的 30 秒关闭预算。实现见 `atri_x/agent/notice_queue.py`。

<!-- locator:platform -->
## 平台适配层

> 移植状态：见 devdocs/porting-status.md 待核对

标注只针对**未连真实外部服务端**：适配器注册与能力矩阵已实现（`atri_x/platform/`，含 `qqofficial` 12 个模块），但 QQ 官方的事件 URL → Media Center 导入路径没有在真实服务端上验证过。

平台层负责输入归一化和输出落地。

输入侧：

- 解析 Actor、Scope、发送目标、群身份、引用、多模态消息段和平台 metadata。
- 原始有序 segments 写入 Chat History，包括纯媒体消息；过滤 base64、临时本地路径和 token/签名 URL，不保证来源永久有效。
- Agent 统一判断 wakeup，并只读检查 waiting Hook route；仅唤起或 waiting continuation 时物化媒体，普通观察 Hook 不下载。
- Telegram resolver 内使用 token URL和代理，OneBot 按需 get_image/get_file；QQ Official 的事件 URL直接由 Media Center 导入，不引入额外 resolver 层。
- 可选 ASR 在媒体物化后、Hook/命令前处理当前消息的语音段：复用 provider 的音频转写端点，成功后替换为带“自动转写（可能有误）”标记的文本段，失败保留 `[语音]`；受全局 `[asr]`、群策略 `asr on|off` 和四级每日额度约束。
- 引用按输出索引 → Chat History → 平台能力恢复有序媒体，图片进入视觉输入，Session 仅保存稳定媒体 ID 与文本投影。同一 actor、平台和 scope 的最后一条 assistant 显式设置 `ResumeSessionID`，使 TTL 清理或 `/new` 清除 current 后仍恢复来源 Session，且不重复注入引用内容；较早 assistant 设置 `ForkFromMessageID` 并保留引用媒体。后台 Resume 和其他用户或 scope 的普通引用规则保持独立。

输出侧：

- 实现统一 `SendChat` / `SendNotice`。
- receipt 同时返回平台消息 ID 和结构化 `SentMessages`（platform/scope/message ID/output indexes），部分成功保留成功项，文本降级不关联媒体。
- 平台发送保持同步回执语义；不得把需要平台消息 ID 或错误的调用改成只入队即成功。
- 支持平台能力差异下的 fallback。

常见平台：

- CLI 本地/TUI 与远程 CLI。
- QQ OneBot v11。
- QQ 官方机器人。
- Telegram。
- headless service 模式。

<!-- locator:session -->
## Session 生命周期

Session 服务管理：

- 当前 session。
- 创建、恢复、Fork。
- 分页列表、置顶、归档、删除、过期清理。
- 模式切换和手动重命名。
- 平台隔离。
- cron session 可见性和 CLI 全平台列表可见性。

Session 命令的分页选择记录和维护配置由 `SessionCommandState` 按 Scope 保存，不使用跨用户的包级状态。

约定：

- Agent 入口需要从平台上下文解析 Actor/Scope，缺失时走 fallback。
- Fork 上下文由 Session/Storage 支持，不在平台层拼接。
- 闲置过期策略按群聊/私聊和普通用户/超管选择 TTL；过期与 `/new` 都只清除内存中的 current，首条普通消息才创建并持久化新 Session，恢复历史 Session 时重新刷新活跃时间。

<!-- locator:context -->
## 上下文管理

上下文管理负责：

- 加载历史消息和 Fork 上下文。
- 解析 context window。
- 按当前模型的 context window 动态判断压缩阈值。
- 格式化厂商 usage 状态。

约定：

- Prompt Builder 只生成单条 system prompt，并组合历史、工具 transcript、多模态 metadata 和摘要。
- 压缩以可取消 request 保护生命周期，仅总结有效对话与成功工具调用。成功后创建并切换到无 Parent/Fork 关系的新 Session，旧 Session 保持不变。
- 新 Session metadata 暂存一次性 compact seed；首条用户输入时，Prompt Builder 将“压缩结果 + 历史用户原话 + 当前输入”物化为单条 user message，成功持久化后消耗 seed。
- 模型选择在 turn 开始时快照；进行中的 `/model` 不改变当前 LLM/工具循环，下一轮按新模型重新解析窗口与阈值。
- System Prompt Manager 按优先级收集 Soul、工具名称、tag prompt 等片段。
- 最近 usage 会写入 Session metadata，恢复会话后可展示。

<!-- locator:storage -->
## Storage 与 SQLite

Storage 抽象定义领域模型和 repository interfaces。

SQLite 实现负责：

- migration。
- Session、Message、ContextSummary。
- 平台聊天历史。
- Cron job。
- Atrinis event。
- Tool call record。

约定：

- 新持久化能力先扩展 storage interface，再落 SQLite repository。
- Message 的 `segments` 是多模态消息的完整结构来源；`content` 是由 segments 生成的纯文本快速路径。仅多模态内容保存 segments，读取时非空 segments 优先，否则直接使用 content。
- migration 需要可重复检测已应用版本。
- 查询条件要保留平台隔离、归档过滤、Fork 范围等业务约束。

<!-- locator:atrinis -->
## Atrinis / Atriena / Atriwisp

Atrinis 是监听枢纽，Atriena 是公共协议层，Atriwisp 是外部事件/能力接入形态。

链路：

1. HTTP runtime 接收 `POST /atriena/v2/events`。
2. auth 校验 token、Atriwisp 和工具授权。
3. prepare 校验 v2/v3 request，规范化 target/tool/calls，生成事件 key/hash。
4. service 去重后分发 record/direct/llm。
5. direct 发送 content/segments 或执行 raw/capability calls。
6. llm 后台任务按 session_mode 运行，并投递结果报告。

LLM 报告使用 SQLite outbox：result 与逐目标、逐 output 的投递项原子落库，投递期间状态为 `result_ready/delivering`，所有 receipt 持久化后才进入 `completed`。Runtime 定时重试未完成项，并在启动时恢复被中断的投递；语义为至少一次。

约定：

- 公共协议类型放在 `atri_x/atriena`，Atrinis 复用别名。
- segment 下载和 URL/data URI 校验集中处理。
- 背景 LLM 要使用后台 actor、sandbox subdir 和对应 session_mode。
- 报告发送不能先写 `completed`；外部平台未提供幂等能力时允许恢复产生重复消息，但不能静默丢失待投递项。

<!-- locator:cron -->
## Cron 与后台任务

中央 Cron Runtime 负责持久化 job 的调度、注册、upsert、禁用、删除、运行状态和执行日志。

约定：

- 维护类任务集中注册在 maintenance 包。
- LLM cron 每次实际调度触发都通过 Agent 后台 runner 创建新 Session；同一轮 JSON 格式重试才复用 Session。
- 一次性 cron 的任务配置与 Delivery 状态分列持久化；Delivery 以 RunID/报告 Session ID 做条件更新，只写仍启用的当前轮次，不能覆盖配置或重新启用任务。
- 已完整投递的任务条件禁用，未完整投递的任务沿用旧报告继续补发，不能重新执行 LLM。
- 正常触发和平台连接补发共用逐实际收件目标、逐输出状态；`ReportReady` 表示报告可复用，LLM 的 `TaskCompleted` 只记录任务结论。
- 补发读取最新任务配置且只由平台连接触发；附件失败在同次补发中降级为路径或 URL 文字，降级文字失败则等待下次连接，不做周期重试。
- LLM cron 可预注入工具或 Skill。
- cron/Atrinis 后台 shell 的非 critical 风险可自动确认，critical 直接返回提醒，不等待用户。
