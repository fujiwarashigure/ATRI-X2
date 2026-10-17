# 上游同步与重构待办（本 fork 要改的任务）

> **本文档是 Go 时代的工作台账**：它记录的是本 fork 仍在 Go 语言下时，与上游 Go 仓库（[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)）逐条对照的结论。文中的 `internal/**` 包路径、`*_test.go` 用例名、`go build` / `gofmt` 都是当时的产物；本仓库已全量重写为 Python，包级对应关系见 [PORTING.md](../PORTING.md) §4，Python 侧的执行/验收命令见 [porting-status.md](porting-status.md) §2。台账里的每条结论在 Python 版是否仍然成立，需要按代码复核。
> 移植状态：见 devdocs/porting-status.md 待核对


本文把 **fork 相对上游（`Elflare/elbot`）还需落地的改动**拆成可开工的任务，放在仓库内维护，
方便直接按项执行、按项验收、按项提交。上游盘点与方案论证在仓库外的 `<仓库外目录>\atri-x-upstream-sync-plan.md`
（该文件不在本机，因此完成台账改由本文维护），
**已完成的提交台账仍记在那份方案的 `§5.3` 与 `表 5.4`**；本文只维护"未完成 / 进行中"的工作。

维护约定：

- 每完成一项（提交后）把该任务勾成 `[x]` 并写上提交号；能访问仓库外方案文件时再回写那里的台账。
- 每次 `git fetch` 上游后做一次增量盘点，把新增的可移植项追加到文末「待盘点」。
- 状态图例：`[ ]` 未开工 · `[~]` 进行中（含未提交改动）· `[x]` 已完成 · `[-]` 已判定不做（附原因）。

## 0. 现状快照

- 基线：fork `main` @ `aaeb5911`；上游 `refs/remotes/upstream/main` @ `a0304ad4`；共同祖先 `d812628`。
- 已落地：命名对齐（`api` → `api_mode`）、P0 全部 8 项、P1#3 工具发现/预载事务化、P1#5 会话整行快照 → 事务内字段更新、P1#4 后台接管（fork 原生最小版，`345a1c1`）、P1#2 模型/会话服务化（`state.toml` 写回串行化，`7e5ab0e`）。
- 已落地（P1#1 最小版 A）：契约与事实丢失修复 `5964838`、结果契约两批 `c61a670` / `dde3253`、收尾事件 `8f5c52e`、app 来源标识 `2862ea3`、端到端验证 `9a8a1f4` → 见任务 2。
- 已落地（P1#6）：工具预览识别规则集中到交付层 `59d7234`、群聊跳过规则整体集中 `e141bdd` → 见任务 4。
- 已落地（P2 决策后按路线 D 补的接缝）：`d531763` → 见任务 5。
- 已落地（P1#2 之后的追加能力）：`/model --profiles` 命名模型选择列表 `82c18d4`、命名模型快照 `/model --save|--snapshots|--apply|--delete` `a293eed` → 见任务 3。
- 其他台账：Windows 部署文档修正 `d449da0`、待办哈希回填 `48680d9`、进程级冒烟记录 `4247d5f`（见 [`smoke-test-report.md`](smoke-test-report.md) §6）。
- 已落地（上游借鉴项，§8）：模型切换兼容性裁决 `6b0a5823`；移植 `atri_x/signal` 并接管平台连接扇出 `d656f689` / `0247c90f`；上游 CHANGELOG × fork 现状核对单（[`upstream-changelog-cross-check.md`](upstream-changelog-cross-check.md)）；信号/日志边界表（[`signals-and-logging.md`](signals-and-logging.md)）+ `AGENTS.md` mermaid 规则；通知队列与背压 `6e023db1`。**§8 五项全部完成。**
- 已落地（§9 上游查询层搬运）：`--raw` 解耦与移除 `/log -t` `803de2ab`、`/usage` null 容错 `25cf1bad`。
- 已研究未落地：上游最新 5 个提交（`1aa10fd9..a0304ad4`）与 §9 剩余项的**逐项判定（直接移植 / 改造 / 不搬）**见 §9.1 与 §10。
- 已落地（本轮按 §10.5 顺序执行，6 项 6 个提交）：
  1. `/rollback` 新建文件冲突校验 `9ae9c404`（§10.6 E，数据丢失级缺陷）；
  2. 平台失败隔离 + service 模式保活 + 跨平台超管通知 + health 断开 + `errors.Is` 口径 `fe98bba`（§10.1 / §10.4.1）；
  3. 后台接管检查失败保守停下 + 两处漏检窗口 `a85f330`（§10.2）；
  4. 启动阶段 Hook 加载告警改到平台发送器就绪后投递 `bbc3a185`（§9.1）；
  5. `shell` 输出上限 256 KiB `40930f3`、工具审计正文按类别限长 `d85cad7`、`/audit --run_source` `74b3275`（§9.1）；
  6. 日志查询跳过损坏行并计数 `04265c28`（§9.1）。
  研究结论本身记录在 `bcef580`。
- **仍留在 §10.6 的独立小项**：`/rollback` 的符号链接目标复核（F）、有界读取（G）、编辑备份 TOCTOU（B）、`Forget` 文案与实现不一致（A）、同目标锁（H）；§10.4.2 的 cron 取消分类。这些都在 §10.5 的"可选"档，未开工。
- **状态**：任务 1–5（P1#4 / P1#1 / P1#2 / P1#6 / P2 决策）在 fork 选定范围内均已完成，无未勾选项。以下三项是**仍留给用户的决策/建议**，不做也不影响上述任务的完成度：
  1. P1#1 是否要完整对齐上游九字段契约（含 JSONL 落盘与 Reader 改写）——会改日志文件格式并连带重写 `/log`、`/audit`、`/usage`、`/atriwisp`、维护报告的解析端。**建议不做**：最小版 A 已让来源与结果可筛选，额外收益主要是内部整洁，代价是破坏现有日志查询。
  2. Telegram 群聊是否也跳过工具调用进度预览——OneBot 与 QQ 官方都跳过（`delivery.ShouldDropGroupToolPreview`），Telegram 从来没有这条规则。无法从代码断定是"漏移植"还是"Telegram 群聊就是要看进度"，因此未擅自改；若要一致，加一行该调用 + "目标是否群聊"的判断即可（见任务 4 的盘点表）。
  3. P2 换基路线 B / C 是否启动（本轮已决策为**不做**，只补接缝）——决策材料见 [`p2-native-responses-decision.md`](p2-native-responses-decision.md)、结论见任务 5。

---

## 1. [x] P1#4 后台会话被前台接管（fork 原生最小版）— 提交 `345a1c1`

**目标**：前台用户 `/resume`（或 `/unarchive`）进入一个正在跑的后台 Session（cron / Atrinis）时，
该后台 Session 被提升为前台会话，在途后台 turn 在最近的安全点自行取消并返回"已被接管"，
不再继续烧 token、不写迟到助手消息、不投递 cron/Atrinis 报告。

**范围（已与用户确认，取 fork 原生最小版）**：

- 提升与前台激活在 **同一个 SQLite 事务**内完成：把后台事实写进 metadata `foreground_origin`，删除 `background_kind`，改 `OwnerID` / `Platform` / `PlatformScopeID` 并置 `Mode = work`。
- 在途后台 turn 用 **安全点轮询** 检测接管（模型调用前、工具批次落库后、最终输出前），命中即返回哨兵错误，由 `RunBackground` 转成 `RunResult{TakenOver: true, Outcome: "taken_over"}`（`err == nil`）。
- **不引入**上游的 `session.Binding`、`executionCoordinator`、`dialogue.ForegroundNotice`（请求尾部合成 user 消息）；语义与上游不同，属 fork 自定义。
- cron 侧把被接管的运行记为 `ReportReady=true, TaskCompleted=true, Report=""`，`deliverPrepared` 因此不发任何东西；Atrinis 侧把事件收成 `StatusCompleted` 且无报告。

**已完成改动（工作区未提交）**：

| 文件 | 改动 |
| --- | --- |
| `atri_x/session/promotion.py`（新） | `WasPromoted` / `IsBackground` / `promoteToForeground` / `ForegroundOrigin` / `ErrForegroundSession`；metadata 键 `foreground_origin`、`background_kind` |
| `atri_x/session/service.py` | `Resume` 的 `Mutate` 回调在后台会话上先 `promoteToForeground`；`canAccess` 改用导出的 `IsBackground` |
| `atri_x/session/lifecycle.py` | `Unarchive` 同样在事务内提升后台会话 |
| `atri_x/agent/cron.py` | 哨兵 `errBackgroundTakenOver`；`RunBackground` / `RunCronMessage` 映射 `TakenOver`；模型调用前的接管检查；已提升 Session 直接短路 |
| `atri_x/agent/chat.py` | `runChat` 在模型调用前、工具批次落库后、最终输出前检查接管并返回哨兵；`runChatTurnWithOutput` 对哨兵不发布 PhaseError |
| `atri_x/background/background.py` | `OutcomeTakenOver`、`RunResult.TakenOver` / `Outcome` |
| `atri_x/cron/service.py`、`atri_x/cron/execution.py` | `RunCronMessageResult.TakenOver`；`takenOverDeliveryState` + 审计 `cron.background_taken_over` |
| `atri_x/atrinis/llm.py` | 被接管时以 `StatusCompleted` 收尾 + 审计 `atrinis.background_taken_over` |
| `internal/session/promotion_test.go`（新） | 已在跑：`ok atri-x/atri_x/session`（提升换 scope、幂等且受 scope 约束、保留未知 metadata、`Unarchive` 提升） |
| `internal/agent/background_takeover_test.go`（新） | 已在跑：`ok atri-x/atri_x/agent`（探针工具在工具批次内接管 → `TakenOver`/`Outcome`/`err == nil`/`Text == ""`/仅 1 次模型请求/无迟到助手回答/`WasPromoted`；已提升 Session 直接短路且 0 次模型请求；接管后前台 `HandleMessage` 仍正常出话） |

**剩余步骤（按序执行）**：

- [x] 补 agent 侧测试 `internal/agent/background_takeover_test.go`：
  - 探针工具在后台 turn 内调用 `session.Service.Resume` 触发接管 → 断言 `TakenOver == true`、`Outcome == OutcomeTakenOver`、`err == nil`、`Text == ""`、只有 1 次模型请求（没有第二次模型调用）、`session.WasPromoted` 为真、没有迟到助手回答落库（工具调用转录行保留，这是安全点设计）；
  - `RunBackground` 传入一个"已是前台（已提升）"的 Session → 直接 `TakenOver`，0 次模型请求；
  - 负向：接管之后在同一 Session 上 `HandleMessage` 仍能正常跑一轮前台对话（前台输出不受接管门槛影响）。
- [x] `python scripts/check_syntax.py -l` 干净（新增/改动文件全部，`python scripts/check_syntax.py -l .` 全仓库无输出）。
- [x] 跑相关包测试：`go test -count=1 -vet=off -p 1 ./atri_x/session ./atri_x/storage/sqlite ./atri_x/agent ./atri_x/cron ./atri_x/atrinis ./atri_x/app`
      （结果：session / storage/sqlite / cron / atrinis / app 全绿；agent 仅剩 §6 已登记的本机环境失败 `TestEmoticonHook*`，2 例，均为 `exec: "sh"` 缺失。）
- [x] 全量构建：`go build -p 1 ./...` 通过（exit 0）。
- [x] `CHANGELOG.md` 按补丁式（旧状态 → 新状态）追加"后台会话可被前台接管"条目。
- [x] `devdocs/code-map.md` 的 Session 段补 `atri_x/session/promotion.py`（提升/接管判定）与 agent 侧接管检查点；Agent 对话流程段补 `RunBackground` 的 `TakenOver` 出口。
- [x] `docs/commands.md` 的 `/resume` 行为说明补一句：恢复后台（cron/Atrinis）Session 会中断该 Session 在途的后台任务；并同步 `docs/concepts.md` 第 194 行附近的 cron 后台 Session 说明（`docs/`、`README.md`、`CHANGELOG.en.md` 未手改）。
- [x] 提交 `345a1c1`：中文 `feat:` 前缀 + 说明"提升事务化 + 安全点取消 + cron/Atrinis 不投递"的正文，提交信息写无 BOM 文件；提交前核对 `git status` 只包含本任务改动（提交后工作区干净）。
- [!] 回写 `<仓库外目录>\atri-x-upstream-sync-plan.md` 台账与表 5.4 第 4 行：**本机没有该盘符**，该仓库外方案文件在此环境中不存在，无法回写。已改为在本文件内维护完成状态；若需要台账，请在持有该文件的机器上同步，或在仓库内新建台账文件。

**验收标准**：

1. 相关包测试全绿（本机既有的环境失败除外，见 §6）；`go build -p 1 ./...` 通过。
2. 接管后：无第二次模型调用、无迟到助手消息、cron/Atrinis 不产生任何投递，且只留一条审计事件。
3. 普通 Session 的 `/resume` 行为零变化（元数据、scope、`UpdatedAt` 语义不变）。

---

## 2. [x] P1#1 日志与信号整改（fork 最小版 A 已完成）— 提交 `5964838` / `c61a670` / `dde3253` / `8f5c52e` / `2862ea3`

**范围已与用户确认（取 fork 最小版 A）**：只做"最小契约 + 修事实丢失缺陷"，**不改日志文件格式、不改 `LogEntry.Fields` 查询契约**；上游的九字段 `LogRecord`、JSONL 落盘、`internal/events` / `atri_x/signal` 全量迁移列入"不做/待决策"，不在本项范围内。

**盘点结论（本次实测）**：fork 的审计事件共 **62 个名字散在 7 个包**，触发方式有 3 套并行写法——`atri_x/agent` 的 `a.audit("event", ...)`（消息固定 `audit event`）、`atri_x/hook/rules` 的 `hook_tool_call` / `hook_tool_error`、`hook/runtime` 的 `hook.tool_call` 与 `atri_x/hook/rules/exec.py` 的 `hook.platform_call`（带点号前缀，与前者不是同一套命名）。

**已完成（本轮）**：

| 文件 | 改动 |
| --- | --- |
| `atri_x/logging/contract.py`（新） | fork 版来源标识 + 操作结果契约：`ModuleApp`…`ModuleTool` 共 13 个已登记来源、`ModuleHook` 等常量、`Result{Succeeded,Failed,Canceled,Rejected,Skipped}`、`LogModules()` / `ValidLogModule()` / `ValidLogResult()`；文件头写明与上游九字段契约的边界（正文长度、JSON 正文、JSONL 落盘本轮不实施） |
| `atri_x/tool/availability.py`（新） | 工具"为什么没被采用"的互斥机器可读原因与唯一判定入口：`ToolAvailabilityReason` / `ToolAccessReason` / `RegistryToolAvailabilityReason`，原因常量 `tool_not_found` / `tool_context_unavailable` / `tool_hidden` / `tool_requires_superadmin` / `tool_risk_above_allowed_level` / `tool_no_schema` |
| `atri_x/agent/cron.py`、`tool_directive.go` | `background_preload_skipped` / `tool_preload_skipped` / `skill_wrapper_preload_skipped` 不再输出 `not_found_or_not_allowed`，改为新原因 + `result`（策略/角色拒绝 `rejected`，其余 `skipped`）；`canPreloadToolRoot` / `canPreloadSkill` 改为委托给原因函数，`@skill:` 根与包装工具分成 `preloadSkillRootReason` / `preloadSkillWrapperReason`（包装工具**不**按 Hidden 拒绝——隐藏是包装工具常态） |
| `atri_x/agent/logging.py` | 所有 Agent 审计记录带 `module=agent`；新增 `normalizeAuditAttrs` 把属性里的 `error` 统一转成脱敏字符串；`auditError` 统一补 `result=failed`（调用方显式给出时不覆盖） |
| `atri_x/app/runtime.py` | app 层审计统一入口 `auditFunc` 补 `module=app`——此前 app 记录是唯一没有来源标识的一类（命名失败、cron、Atriwisp、hook 接线都经它） |
| `atri_x/agent/toolrun_adapter.py`、`command_runtime.go` | `permission_denied` 带 `result=rejected` |
| `atri_x/hook/runtime/tool_bridge.py`、`rules/action.go`、`rules/exec.go` | `hook.tool_call` → `hook_tool_call`、`hook.platform_call` → `hook_platform_call`；`hook_platform_call` 改为按真实调用结果记录（成功/失败带 `error` 与 `result`）；错误对象统一字符串化 |
| 测试（新） | `internal/logging/contract_test.go`、`internal/logging/contract_source_test.go`（源码级 result 契约校验）、`internal/tool/availability_test.go`（含"新原因入口与 `CanAccessTool`+`InfoAvailableInContext` 判定等价"的固定测试）、`internal/agent/logging_test.go`（`module` 来源、无 logger 时 noop、错误属性转换）、`internal/app/audit_test.go`（`auditFunc` 带 `module=app` 且不挤掉调用方属性） |

**来源标识覆盖度（收官核对）**：全仓库 175 处审计入口调用——`atri_x/agent` 152 处经 `auditLog` 统一补 `module=agent`，`atri_x/app` 2 处经 `auditFunc` 统一补 `module=app`，`atri_x/hook` 21 处显式传 `module=hook`。**没有任何审计记录缺少来源标识。**

**操作结果覆盖度**：`atri_x/agent` 与 Hook 侧共 80 处契约取值，源码级校验覆盖其中写死的 69 处；其余 11 处把结果交给函数判定（`tool_call` 按 `success` 映射、`risk_confirmation_result` 按动作区分、预载跳过复用 `preloadSkipResult`），由各自单元测试覆盖。

**信号侧现状（本次核对）**：fork 用 `hook.PointPlatformConnected`（`platform.connected`）+ `platform.ConnectNotifier` 回调承载"平台已连接"这一进程级事实，`atri_x/app/platforms.py` 的 `registerPlatformHooks` 注册回调、`registerCronPlatformHook` 与 Agent 的 `NotifyPlatformConnected` 各自订阅（cron 补投递 missed-once、hook 通知），与上游"全局信号 + 各自订阅"的语义一致，只是载体不同（hook 点而非 `atri_x/signal`）。**无需迁移 `atri_x/signal`**（fork 没有这个包）。

**验收**：`/log`、`/audit` 的筛选参数在新来源下仍可用（`--hook` 继续按 `module=hook` 过滤，事件名只在 `platform_recall_*` 之外保持不变；`hook.tool_call` → `hook_tool_call` 是本次唯一的事件名变更，已在 CHANGELOG 记录）；每批迁移后跑受影响包测试 + `atri_x/app`。

**未做且需用户决策**：上游九字段 `LogRecord`（含 JSONL 落盘与 Reader 兼读新旧格式）。它超出已确认的最小版 A 范围，会改日志文件格式并连带重写 `/log`、`/audit`、`/usage`、`/atriwisp` 与维护报告的解析端。

## 3. [x] P1#2 模型/会话服务化（四项能力全部落实，含后续追加的命名模型快照）— 提交 `7e5ab0e` / `a293eed`

> 四项能力中一项（`state.toml` 并发写回）已修复，另三项（写盘原子性、运行中会话拒绝切离、会话绑定统一失效）经核对 fork 原本就具备。表中"命名模型快照"一行原本单列等用户决策，**用户已批准后已实施**（见本节的"命名快照（后续追加）"）。

- **上游来源**：`internal/modelmgr/*`、`atri_x/session/*`、`atri_x/app/{signals,lifecycle,services,agent_status}.go`、`internal/turn/execution.go`。
- **要拿到的能力**：`state.toml` 原子保存、命名模型快照、会话绑定统一失效、运行中会话拒绝切离。

**本轮实测（逐项核对后只做确有缺陷的一项）**：

| 要拿到的能力 | 现状 | 处置 |
| --- | --- | --- |
| `state.toml` 原子保存 | 写盘本身已是原子替换（临时文件 + `Sync` + 备份换名 + 崩溃后从 `.bak` 恢复，见 `config.SaveState` / `LoadState`）。**缺陷在并发写回**：`saveRuntimeState` 只用保护 `stateModTime` 的 `stateMu`，"重新读文件合并 → 取内存快照 → 替换文件"整段是并发的，两个写入者会互相覆盖分片；mtime 门在同一时间戳精度内也看不出差别。 | **已修**：新增 `stateWriteMu` 串行化整个合并-快照-落盘过程；22 处调用点（8 个文件）全部经过它。验证方式：手工复现（8 个并发写入者各自只把一份分片放进内存再保存）——修前只剩 1 个分片落盘，修后稳定保留全部 8 个；该缺陷没有做成单元测试，因为确定性地暴露它需要在锁内部插桩，而并发冒烟测试会与其它测试的 `state.toml` 清理互相干扰（反而制造无关的 flake）。 |
| 运行中会话拒绝切离 | 已有：`commandExecutor.Handle` 按 `turn.Snapshot` 的 phase 拒绝 `SessionEffect` 会切走当前会话的命令（`activeTurnCommandBlockedText`），压缩中另有 `compactCommandBlockedText`。 | 无需改动；`/new`、`/resume`、`/fork`、`/chat`、`/work`、`/delete`、`/compact` 均在拒绝集合内，已有测试覆盖。 |
| 会话绑定统一失效 | 已有：`session.Service` 的当前会话映射只在 `setCurrent` / `clearCurrentIf` 两处写，删除、归档、空闲过期分别经 `clearCurrentIf` 失效；读取侧 `Current` 每次都回 store 取行，进程内不缓存会话内容。 | 无需改动；未发现绕过这两个入口的写点。 |
| 命名模型快照 | fork 的 `model_profiles` 是 **services.toml 里的静态单模型选择**（用于 `@model:<profile>` 解析），没有"把一个名字绑定到一组按模式/用途保存的模型选择"的快照能力。 | **已实施（用户批准后追加）**：见下。 |

**命名快照（后续追加，用户批准后实施）**：

| 文件 | 改动 |
| --- | --- |
| `internal/config/config.go` | `StateConfig.ModelSnapshots map[string]StateModelSnapshot`（`[model_snapshots]`）+ `StateModelSnapshot{ModeModels, CompactModel, NamingModel}`；注释写明它为什么在 `state.toml` 而不是只读共享的 `app.toml` / `services.toml` |
| `atri_x/agent/model_snapshot.py`（新） | `ModelSnapshots` / `SaveModelSnapshot` / `ApplyModelSnapshot` / `DeleteModelSnapshot`；名字校验（字母/数字/`_`/`-`，≤32）、"不与静态 profile/alias 同名"的拒绝、应用前整体校验 provider（缺一个就整条拒绝、不做部分切换）、应用顺序固定（`work` 最后，因为 `applyModeModelSelection` 会把最后一次的 provider 记为进程主 provider） |
| `atri_x/agent/core.py` | `modelSnapshotsMu` + `modelSnapshots`（运行态内存副本） |
| `atri_x/agent/model.py` | 快照接入 `applyRuntimeState` / `saveRuntimeState` / `runtimeStateDigest` / `runtimeStateSections`（因此参与外部编辑热加载并出现在 `/state` 的变更摘要里）；新增 `mergeExternalRuntimeState`，并让 `SelectModelForMode` / `SelectCompactModel` / `SelectNamingModel` 在改内存**之前**先合并外部编辑 |
| `atri_x/agent/commands/model.py`、`register.go` | `/model --snapshots` / `--save <名字>` / `--apply <名字>` / `--delete <名字>`；`--apply` / `--delete` 后面补全已保存的快照名（`Kind: "model_snapshot"`），`optionOnlyModelArgs` 覆盖新选项；`ModelService` 增加四个方法 |
| `internal/agent/model_snapshot_test.go`（新） | 落盘回读、槽位顺序、静态 profile 同名拒绝、非法名字、整体应用（含"work 最后应用"断言）、未知 provider 不做部分应用、缺名报错区分 profile 与快照、外部编辑先合并（apply 用过期快照必须失败、save 不被外部编辑吞掉）、删除落盘、跨 Agent 重载后可应用 |
| `internal/agent/commands/model_test.go` | 6 个新命令测试：save/apply/delete/snapshots 的输出与不切换模型、失败透传、`--apply` 后补全快照名而不是模型名、`--sna` 补全选项 |

- **前置**：无（但会与 P1#1 抢同一批 app 层文件，建议两项顺序做，不要并行改 `atri_x/app`）。
- **注意**：fork 没有上游的 `session.Binding`；本项是"忠实移植上游后台接管"（P1#4 的完整版）的前置，若将来要把接管语义对齐上游，先做本项。
- **验收**：并发切换模型/新建会话不再出现半写 `state.toml`（写盘原子性已有，并发写回已串行化并手工复现验证：修前 8 个并发写入者只剩 1 个分片，修后稳定保留全部分片；该缺陷无法做成确定性单元测试，因为要暴露它需要在锁内部插桩）；运行中会话切模型/切模式给出明确拒绝而非静默生效（已有行为，测试覆盖）；命名快照的保存/应用/删除有落盘级测试，且"外部编辑后紧接着的内部改动不被吞掉"有专门测试；相关包测试 + `atri_x/app` 全绿。

## 4. [x] P1#6 通知规则集中化（两轮已完成：`59d7234` + `e141bdd`）

- **上游来源**：`internal/notification/*`。
- **前置**：P1#1 日志与信号整改（通知规则建立在统一来源/事件契约之上）。

**盘点结论（本次实测）**：三条投递路径（cron、Atrinis、hook）**已经**汇聚到同一个裁决入口 —— `atri_x/app/runtime.py` 的 `sendNotice` 闭包 → `Agent.SendNotice`（先过 `turnOutputAllowed` 与 `noticeTargetBlocked`，再经 `delivery.Manager` 路由到平台适配器），cron 的 `buildCronService`、Atrinis 的 `Send`、hook service 全部接的是这个闭包。真正分散的是**平台适配器各自的跳过规则**，其中"工具调用进度预览"这条规则在 OneBot、QQ 官方、hook 出站记录三处各写一遍。

**已完成（本轮）**：

| 文件 | 改动 |
| --- | --- |
| `atri_x/delivery/delivery.py` | 新增唯一的预览判定入口：`ToolPreviewPrefix`、`Output.IsToolPreview()`（要求完整前缀 `"[tool] "`，只有 `[tool]` 没有正文不算）、`IsToolPreviewNotice()`（单条文本输出 + 预览前缀） |
| `atri_x/platform/qq_onebot/adapter.py`、`internal/platform/qqofficial/adapter.go` | `isGroupToolPreviewNotice` 改为调用 `delivery.IsToolPreviewNotice`，各自只保留"目标是不是群聊"的判断 |
| `atri_x/agent/turn_output.py` | `formatToolPreview` 用 `delivery.ToolPreviewPrefix` 写前缀 |
| `atri_x/hook/builtin/register.py` | 出站历史跳过预览改用同一个前缀常量 |
| `internal/delivery/tool_preview_test.go`（新） | 覆盖前缀识别、非预览拒绝（普通回答、`[tools]`、空正文、非文本 Kind、只有前缀、正文里提到 `[tool]`）与"必须恰好一条文本输出" |

**剩余步骤（已按用户给定的边界做完第二轮，见下）**：

- "通知被禁用时不投递"的负向测试已有：`internal/agent/group_runtime_test.go` 的 `TestSelfMutePausesGroupCallsOutputAndPersists` 断言被静音群里显式群通知被丢弃、只有状态变更通知发出一次。没有再做"某个通知目标被禁用"矩阵——禁用维度（群运行状态 / 通知目标开关 / 平台能力）里的目标开关与平台能力目前各自只有一处实现，做成矩阵只是把同一判定抄成表，不增加保护。
- 其余平台级跳过规则已逐个盘点（第二轮），结果见下面的表。

**第二轮：平台级跳过规则盘点与合并（提交 `e141bdd`）**：

| 规则 | 出现位置 | 语义 | 处置 |
| --- | --- | --- | --- |
| 群聊里不投递工具调用进度预览 | `qq-onebot`、`qqofficial` | **完全相同**：通知无显式目标 + 当前上下文是群聊 + 该通知就是预览（单条文本 + `"[tool] "` 前缀）⇒ 静默丢弃、不算失败 | **已合并**：规则整体收敛为 `delivery.ShouldDropGroupToolPreview(target, outputs, groupContext)`，两个适配器只保留 `isGroupContext`（各自的上下文键类型不同：OneBot 看 `target.MessageType`，QQ 官方看 `sendTarget.Kind`） |
| 空白文本不发 | `qq-onebot`、`qqofficial`、`telegram` 各自的 `sendText` / `sendContextText` | 语义相同（`strings.TrimSpace(text) == ""` ⇒ 空回执），但只有一行、无共享状态 | **不合并**：抽成公共函数不增加任何保护，只是把 `TrimSpace` 换个名字；三处已用同一写法 |
| 主动消息被禁用时报错 | `qqofficial`（`Proactive && !allowProactive()`） | 平台配置特有 | **保持平台内**：只有 QQ 官方有"主动消息"概念 |
| 本地 CLI / headless 不过滤 | `cli`、`headless` | CLI 的预览就是终端输出；headless 无投递 | **不改** |
| Telegram 不过滤群聊预览 | `telegram` | **与 QQ 两处不同**：Telegram 从未有这条规则（QQ 侧来自 `356141b "qqonebot will not send notify in group"`） | **未统一**：无法从代码断定是"漏移植"还是"Telegram 群聊就是要看进度"，按"不同语义不强行统一"保留原行为，**留给用户决策**（若确认要一致，加一行 `delivery.ShouldDropGroupToolPreview(...)` + 目标是否群聊的判断即可） |

**验收**：通知目标的裁决逻辑只有一处入口（`Agent.SendNotice`，已确认）；cron / Atrinis / Hook 三条投递路径都走它（已确认）；平台级跳过规则中"语义相同"的那条已只有一份实现，且边界（群聊普通通知照发、私聊预览照发、显式目标预览照发、多输出不算预览）都有测试固定。

## 5. [x] P2 决策：**不换基，走路线 D（按需补接口）**——三个接缝已实施

**决策材料：[`p2-native-responses-decision.md`](p2-native-responses-decision.md)**（提交 `462091d`）。**用户已决策：本轮按路线 D 补接口，不做换基（路线 B/C 暂不启动）。**

- **目标收益**：服务端续链（`previous_response_id`）、原生压缩、reasoning 加密内容、`additional_tools` 增量工具定义、原生 checkpoint。
- **现实结论**：这不是"再写一个客户端"，而是换内核——LLM 分层（fork 的 `atri_x/llm/openai` 在上游已删除）、agent 路由（会替换 fork 的 `chat.go` / `chat_llm.go` / `chat_tools.go` / `core.go`，正是 P0/P1 刚加固过的地方）、压缩、存储（3 个新迁移：`session_llm_origin`、`native_dialogue`、`native_material_roots`）、模型来源、工具可用性。
- **取证结论（本轮再次确认）**：fork 的 Responses 支持是协议翻译层——`store=false` 明确写在 `responses.go` 注释里（重放完整历史、从不读回已存响应），`previous_response_id` 与 `additional_tools` 在全仓库出现 **0** 次，压缩仍是客户端的。因此新增的协议能力查询接口对 Responses 适配器**如实报告"服务端能力全 false"**，而不是照抄协议文档。
- **为什么不做 B**：会替换的 `chat*.go` / `core.go` 正是前几轮刚加固的地方（后台接管安全点、工具白名单、事务化字段更新、预载事实区分、预算预占），在同一个文件上换内核等于把刚建立的回归保护推倒重来；20–35 人日的成本也远超短期省下的 token。

**路线 D 已实施（三个接缝，全部不改变现有行为）**：

| 接缝 | 实现 | 换基时省掉什么 |
| --- | --- | --- |
| 协议能力查询 | `atri_x/llm/protocol.py`：`Protocol`（`chat_completions` / `responses`）、`ProtocolCapabilities`（`ServerSideConversation` / `NativeCompaction` / `IncrementalTools` / `ServerSideStore`）、可选接口 `ProtocolCapabilityReporter`、保守入口 `ProtocolCapabilitiesOf(client, model)`。Chat 适配器报告 chat、Responses 适配器报告 responses（服务端能力全 false），`atri_x/app/models.py` 的 `protocolRouter` **按模型**回答（同一 provider 混用两种协议时以实际适配器为准） | 不必在 agent 各处写 `if protocol == responses` 式判断；换基只需改这里的返回值 |
| 压缩分派点 | `atri_x/contextmgr`：`Compactor` 接口（`Name` / `Compact` / `SummarizeText`）+ `CompactionChoice`；`contextRuntimeState.compactorFor(provider, model)` 按协议能力选后端，目前恒定选中客户端后端 `client_summary`；协议声称支持服务端原生压缩而 fork 没有实现时，**显式回退**并在压缩后写一条审计（`event=context_compaction_backend`，含 `backend` / `reason`），而不是静默失效或静默假装走了服务端路径 | 新增服务端压缩时只加一个 `Compactor` 实现并在分派点登记，不动压缩流程 |
| 存储协议来源 | 会话 metadata 的 `llm_origin`（`{protocol, provider, model}`，`atri_x/agent/session_metadata.py`）：每轮模型调用后由 `recordLLMOrigin` 记录，值未变时**不写事务**。上游对应的是 `session_llm_origin` 迁移；fork 用加法式 metadata 字段承载同一事实，避免为一个诊断字段引入 schema 迁移 | 换基时"哪些会话能续链/需要迁移"有据可查，不必考古 |

**验收**：`python scripts/check_syntax.py` 干净、`go build -p 1 ./...` 通过；`atri_x/llm`、`atri_x/app`、`atri_x/contextmgr`、`atri_x/agent` 相关测试通过；新增测试覆盖"未实现查询接口时保守默认值""按模型回答协议能力""Responses 适配器不虚报服务端能力""协议声称原生压缩但没有后端时必须回退且留下审计""`llm_origin` 只在变化时写入"。

**仍未做（明确不在本轮范围）**：服务端续链、原生压缩实现、reasoning 加密载荷、`additional_tools`、`native_dialogue` / `native_material_roots` 迁移；`atri_x/llm/openai` 的协议翻译与会话语义拆分（路线 C 的第 2 步）也未启动。

---

## 6. 通用收尾流程（每项都要做）

1. `python scripts/check_syntax.py -l` 干净；跑改动包 + `atri_x/app`，用 `go test -count=1 -vet=off -p 1`。
2. 涉及存储/迁移：在既有库副本上跑一次启动迁移，新增迁移必须加法式。
3. 涉及权限：补"普通用户不能操作他人资源"的负向测试。
4. 文档：`docs/*.md`（中文）+ `CHANGELOG.md`（补丁式：旧 → 新）；`docs/`、`README.md`、`CHANGELOG.en.md` 不手改；`devdocs/{architecture,code-map}.md` 同步职责。
5. 提交：中文前缀（`feat:` / `fix:` / `docs:` / `change:`），一个任务一次提交，正文说明改动与验证命令。

**本机既有环境失败（与上述改动无关，不要计入本次回归）**：

- `atri_x/agent`：2 个 `TestEmoticonHook*`（`exec: "sh": executable file not found`）。
- `atri_x/agent`：`TestMemberPanelShowsOwnTasksAndQuota` / `TestGroupServicesCommandIntegration` / `TestModelsGroupsProvidersAndSwitchPersistsState` / `TestGroupKnowledge*` / `TestReminderCreateListRemoveAndDispatch` / `TestLearningModerationActionsAreGranularAndRevocable` 等**偶发**失败，报错是 `TempDir RemoveAll cleanup: unlinkat ...: The directory is not empty` —— 测试返回后仍有迟到的 `state.toml` 写入落在那次 `t.TempDir()` 里，属于测试夹具的清理竞态，与 `stateWriteMu`（只串行化同一实例的写回）无关；单独跑这些测试稳定通过，全包连跑约每次命中 1 例。
  - **归属已实测**：用 `git worktree add` 检出改动前的 `82c18d4`，在同样的并行负载下（`./atri_x/agent/ ./atri_x/tool/builtin/ ./atri_x/platform/refcontext.py/`）跑 4 轮，同样出现 `TestLearningModerationActionsAreGranularAndRevocable` / `TestModelsGroupsProvidersAndSwitchPersistsState` 等偶发失败；单独跑 `./atri_x/agent/` 3 轮则稳定只有 2 个 `TestEmoticonHook*`。因此这些 flake 是既有的负载敏感问题，不是本次改动引入。
  - **本轮（§8.2 / §8.5 之后）追加观察**：连跑 8 轮 `./atri_x/agent/`，每轮固定只有 2 个 `TestEmoticonHook*`，另外每轮随机命中 **1 例** `TempDir RemoveAll cleanup: ... The directory is not empty`，命中的用例在三轮里分别是 `TestBudgetLedgerPersistsAndIsIdempotent`、`TestLearningModerationActionsAreGranularAndRevocable`、`TestModelsGroupsProvidersAndSwitchPersistsState`；另有一次扫描命中 `TestSaveModelSnapshotKeepsSnapshotWithConcurrentExternalEdit`（该用例随后单独跑 3/3、全包再跑 8 轮均通过）。这些用例都往 `t.TempDir()` 里写 `state.toml`，命中对象随机 → 与上面的"迟到写入落在已删除临时目录"是同一类，不是本次改动引入。
  - **本轮（§10.5 六项落地后）追加观察**：完整 `go test -count=1 -vet=off -p 1 ./...` 过一遍，全仓没有新失败；`atri_x/agent` 除 2 个 `TestEmoticonHook*` 外仍随机命中 1 例 `state.toml` 临时目录清理竞态（本轮命中 `TestApplyModelSnapshotMergesExternalEditFirst`，单独跑 1/1 通过），`atri_x/character` 与 `atri_x/request` 仍因写测试二进制 `Access is denied` 报 `[build failed]`。
- `atri_x/tool/builtin`：4 个 `TestAnalyzeBashShellAdvice*`，以及 `TestShellToolUsesConfiguredEnvironment` / `TestResolveWindowsShellCachedAndValid` / `TestShellMediaInputsAndCleanup`（本机 `bash` 来自 WSL 但 `sh` 不存在、且 shell 解析结果随 PATH 变化）。
- `atri_x/tool/skill`：`TestFinalizeAtrixSkillReturnsBuildFailure`（测试内部跑 `go build`）。
- `atri_x/hook/rules`：`TestExecCancellationKillsDescendantProcesses`（依赖 TCP 回连，本机 accept 超时）。
- `atri_x/llm/openai`：`TestChatStream_ActiveStreamCompletesWithoutResponseTimeout` 等基于真实计时的用例在并行跑全量时偶发失败（单独跑 3 轮均通过）。
- `atri_x/character` / `atri_x/request`：偶发 `[build failed]`，原因是 `go test` 写测试二进制时被拒（`...\eac-beta\tmp\go-build*\b001\*.test.exe: Access is denied`），属本机 Go 构建缓存/临时目录权限问题；`go vet` 这两个包通过，说明代码本身可编译。

**本机 Go 工具链事实（本次验证环境，供后续复核）**：

- Go 不在 `PATH` 上，工具链装在自定义目录（例如 `go-sdk\go\bin`）；直接用绝对路径或把它加进 `PATH`。
- `proxy.golang.org` 在本机不通（连接超时），需要 `GOPROXY=https://goproxy.cn,direct`；缺失的模块正是靠它补齐的。
- dpx 环境把 `GOPATH`/`GOCACHE` 收容到环境目录，`go` 默认写不进去（`Access is denied`）；用 `danger-full-access` 运行，或把 `GOPATH`/`GOCACHE` 指到可写目录。

## 7. 待盘点（增量）

- [x] 上游是否有新的 `### Added` / `### Fixed` 需要挑拣：`git log --oneline HEAD..upstream/main` + 上游 `CHANGELOG.md` 的 Unreleased 段。
  - **上一轮（无新提交）**：本地 `upstream/main` 仍是 `8457fdd`（"update dev docs"），未移动；`git ls-remote` 返回同一个 `8457fdd`，`HEAD..upstream/main` 为空。
  - **本轮盘点（2026-10-11 之后）**：上游已推进到 **`1aa10fd`**（"update log format"），自上次盘点新增 **25 个提交**（`8457fdd..1aa10fd`，510 文件 / +27580 / −12222）。上一轮之后 `refs/remotes/upstream/main` 一度被 prune 掉，已按下面的命令重新取回。
    - 主题分布：信号与日志重做 11（`546408a2` `12266e17` `8faa9d34` step1、`e476f72c` `3540fc35` step2、`7d32594b` `e5bfc27a` `9549975d` `1aa10fd9` log format、`1dcb61c7` `32e22cdb` fix）、结构重构 3（`9234f85f` app、`d03af071` notify、`1ecaf4f2` cli ui）、其他修复 1、devdocs/AGENTS 4、自动翻译镜像 6。
    - **用户可见的新功能只有 8 项**：`/log`、`/audit`、`/atriwisp` 新增 `--raw`（并从 `-d` 解耦）；移除 `/log -t`（`-t` 只留在 `/audit`，工具调用只进 audit）；`/audit --run_source foreground|cron|atrinis`；`/atriwisp --tag`（fork 早已有，见下）；损坏日志行不再中断查询并提示数量；`/usage` 未知/非法/溢出显示 `null`；工具日志正文按类别限长（读/查/说明 ≤128、执行/编辑/写入/媒体 ≤256）；模型信息超时 5s→30s（上游 `internal/modelmgr`，fork 无对应实现）。`### Added` 只有 `--raw` 一条，其余全在 Changed / Fixed。
    - **可挑拣性判断**：这批新功能**没有一项能低成本单挑**——它们都长在 `atri_x/signal` + `internal/events` + `*/module` 这套新骨架上，而 fork 走 hook 路线、没有 `atri_x/signal`。但 `/log` 与 `/audit` 的**查询层**改动是可分离的：fork 的 `atri_x/agent/commands/log.py`（774 行）与上游 `internal/command/builtin/log.go`（780 行）是**兄弟版本**（同名同形的 `formatRawLogEntries` / `previousLogToken` / `completionOption` / `jsonQuotedLogValue`），上游本轮该文件只 +208/−153，因此按"对齐行为、不对齐实现"移植成本很低。已登记的待办见 §9。
    - 另：上游本轮 `AGENTS.md` 两条改动建议采纳（见 §8.4）。
  - **再一轮盘点（本轮实测）**：`git fetch` 重新取回 ref 后，上游从 `1aa10fd9` 又推进到 **`a0304ad4`**，新增 **5 个提交**（`1aa10fd9..a0304ad4`，38+2 文件 / +826 / −125）。逐条内容：
    - `6fd1d1c` **`fix: sync foreground takeover via local signals and isolate platform failures with cross-platform admin notifications`**（38 文件 / +770 / −89）：前台接管由"注入回调 `SetForegroundActivation`"改为**局部信号** `session.ForegroundActivated`；工具边界刷新由**重读数据库**改为**只合并内存快照**；平台异常退出改为**隔离 + 跨平台超管通知**；服务模式不再随全部消息平台退出而结束；`delivery` 抽出 `LogSendResult` 并让失败日志用 `context.WithoutCancel`。
    - `a0304ad` **`fix bug`**：把 `runCtx.Err() == err` 修成 `errors.Is(err, runCtx.Err())`（`internal/platform/module/module.go` 的 `runPlatforms`，1 行）。
    - `8ae5f6f` / `549287e` / `132e930`：`docs: auto-translate …`（`CHANGELOG.md`、`docs/getting-started.md`、`docs/commands.md` 的英文镜像）——本 fork 不手改英文镜像，**不跟**。
    - **判定**：这 5 个提交里没有"可整段照抄"的东西（都长在上游 `atri_x/session` + `atri_x/turn` + `atri_x/platform/module` 骨架上），但**有两项行为值得在 fork 的等价落点改造**（平台失败隔离 / 服务模式保活 / 超管通知，以及启动插件告警的投递时机）。逐项结论见 §10，§9 剩余项的结论见 §9.1。
  - **注意**：本仓库**没有配置 `upstream` remote**（只有 `origin`）。`refs/remotes/upstream/main` 是手工 fetch 留下的 ref（会被 `git fetch --prune` 清掉）；每次增量盘点前先 `git fetch https://github.com/Elflare/elbot.git main:refs/remotes/upstream/main`，否则会拿到过期结论。

## 8. 上游借鉴项（本轮计划，按序落地）

除"挑拣上游新功能"之外，本轮还把上游整库盘了一遍，找出 4 类值得借鉴的东西。执行顺序与状态：

### 8.1 [x] 模型切换兼容性裁决（借鉴上游 `modelmgr.CanSwitch`）— 提交 `6b0a5823`

**为什么值得借鉴**：上游 `modelmgr/compatibility.go` 的 `CanSwitch` 只有 30 行，却把"这段历史能不能交给目标模型"做成一个明确的裁决入口。fork 在 P2 路线 D 已经记下同样的事实（会话 metadata 的 `llm_origin`，`d531763`），却**只记不用**：`/model` 能把一个 Responses 会话切到别的厂商的 Responses 模型、或直接切到 Chat Completions，切完才发现上下文续不上。这是"借鉴裁决而不是搬实现"的低成本样例。

| 改动 | 位置 |
| --- | --- |
| `Origin{Protocol, Provider}` + `CanSwitch`（与上游同名同形，规则逐条对齐；错误文案改英文与 fork 现有 Go 错误统一） | `atri_x/llm/origin.py`（新） |
| 切当前会话模式前裁决；`llmOriginFor` 归约协议（与 `recordLLMOrigin` 共用同一次判定） | `atri_x/agent/model.py` |
| `/model --apply <快照>` 走同一个裁决（否则是现成的绕过口） | `atri_x/agent/model_snapshot.py` |
| `SelectModelForMode` / `ApplyModelSnapshot` 补 `ctx` | `atri_x/agent/commands/{register.go,model.go}` |

**行为**：目标缺协议/厂商 → 拒绝；来源没记协议（旧会话）→ 放行；同协议且（chat_completions 或同厂商）→ 放行；其余 → 拒绝，且内存与 `state.toml` 都不改动。只拦**当前会话正在用的模式**；切其他模式槽位（如 work 会话里 `/model --chat`）不拦。

**故意不做的**：不搬 `internal/modelmgr`（580 行；fork 的模型目录与选择在 `atri_x/agent/model.py` + `atri_x/app/models.py`，搬过来是纯搬家）；不引入上游的 `PrepareModel` / `CommitModelForMode` 两段式（fork 的 `SelectModelForMode` 一次完成，裁决插在中间即可）。

**验证**：`python scripts/check_syntax.py -l` 干净；`python -m unittest discover -s tests -t .` 通过；`atri_x/llm`、`atri_x/agent/commands`、`atri_x/app` 测试全绿；`atri_x/agent` 仅剩 §6 既有环境失败。新用例见 `internal/agent/model_switch_test.go`（跨协议拒绝、responses 跨厂商拒绝、同厂商同协议放行、chat 跨厂商放行、无来源放行、其他模式槽位不拦、快照拒绝与放行）与 `internal/llm/origin_test.go`（规则表，对齐上游 `compatibility_test.go` 用例）。

### 8.2 [x] 移植 `atri_x/signal`，替换 hook 侧裸扇出

上游 `atri_x/signal`（857 行源码 + 933 行测试 + 10 个 benchmark）**零 `internal/*` 依赖**，只用标准库，是这次盘出来的唯一"可以整包搬"的东西。

- [x] **1a 只落包** — 提交 `d656f689`：8 个源文件 + 7 个测试文件原样移植（`git checkout refs/remotes/upstream/main -- atri_x/signal`，字节级一致），不改任何现有行为。验证：`python scripts/check_syntax.py -l atri_x/signal` 无输出、`go vet ./atri_x/signal/...` 通过、`python -m unittest discover -s tests -t .` 通过、`go test -count=1 -p 1 ./atri_x/signal/...` 通过。`devdocs/code-map.md` 的 Hook 与插件段已登记。
- [x] **1b 接管平台连接扇出** — 提交 `0247c90f`（+ 断言测试与冒烟记录 `014e2fc2`）。
  - **改动前的问题**：`atri_x/app/platforms.py` 的 connect notifier 直接把平台连接事实同步转给 `Agent.NotifyPlatformConnected`，由 hook 管理器在**同一条 asyncio task** 上跑完所有 `platform.connected` 处理器，其中 `builtin.cron.missed_once` 会调模型并发送消息——等于把一次 LLM 补投递压在平台连接回调上，而且没有队列、超时和关闭预算。
  - **已确认的两个决策**（用户选定）：全局信号放 `atri_x/platform`（不新开 `internal/events`，避免与"不引入上游九字段日志契约"的既有决策混淆）；范围只改 hook 侧，cron 继续挂在 hook 点 `builtin.cron.missed_once` 上。
  - **落地**：`atri_x/platform/platform.py` 新增 `ConnectedEvent{Platform}` + `PlatformConnected`；`atri_x/agent/platform_signals.py`（新）里 `StartPlatformEvents` 订阅并按平台建 `signal.Queue`（默认 256 等待 / 1 并发），worker 上跑 `handlePlatformConnected`，`ClosePlatformEvents` 先停止接纳再排空；`atri_x/app/integrations.py` 在适配器启动前订阅（信号不缓存、不重放）；`atri_x/app/runtime.py` 的 `hookRuntimeLifecycle` 先关平台连接订阅、再关 hook 运行时。
  - **验证**：`python scripts/check_syntax.py` 干净；`python -m unittest discover -s tests -t .` 通过；`./atri_x/agent/`、`./atri_x/app/`、`./atri_x/platform/...`、`./atri_x/cron/...`、`./atri_x/hook/...`、`./atri_x/signal/` 全绿（`atri_x/agent` 仅剩 §6 既有环境失败）。新用例：`internal/agent/platform_signals_test.go`（信号驱动 hook、发布方不等 hook 业务、同平台串行 max 并发 = 1、关闭后不再接纳、幂等且不可重启）、`internal/app/platforms_signal_test.go`（连接回调确实发布信号、适配器不报名字时用适配器名）。进程级冒烟见 [`smoke-test-report.md`](smoke-test-report.md) §6.1。
  - **与上游的差异（有意）**：上游让**每个平台适配器**直接 `Emit`，并让 Hook 与 Cron 各自订阅；fork 保留 App 里的单一发布点（`registerPlatformHooks` 的 notifier），只让 hook 侧订阅。

### 8.3 [x] 上游 CHANGELOG × fork 现状核对单

产出独立文档 [`upstream-changelog-cross-check.md`](upstream-changelog-cross-check.md)：把上游 `## Unreleased`（`v0.5.0` → `1aa10fd`，与本 fork 共同祖先同日，共 44 条）逐条判成 `已跟 / 部分 / 未跟 / 未跟（有意） / 不搬 / 不适用 / 相反 / 待复现`，并给出证据（`CHANGELOG.md` 行号或 `file:line`）。

**结论**：已跟 19、部分 5、未跟 12、未跟（有意）2、不搬 3、不适用 1、相反 1、待复现 1。**真正算"fork 漏了且值得补"的只有 6～7 条**，集中在日志查询/写入侧与几个一行常量级的改动上；其余未跟项要么依赖上游那套 `atri_x/signal` + `internal/events` + `*/module` + `atri_x/turn` 骨架（结构级，已判"不搬"），要么是 fork 的有意差异。

已确认的具体缺口（按优先级）：日志查询 `--raw` 解耦 / 移除 `/log -t` / 损坏行计数（§9.1～§9.3）、工具日志正文按类别限长（§9.5）、`shell` stdout/stderr 上限仍是 16 KiB（`atri_x/tool/builtin/shell.py:24`）、`/usage` 把非法值当 0（`atri_x/agent/commands/usage.py:279`）、`internal/fileops/rollback.go` 的上游增量（见 8.1 项 A3 说明）、启动插件告警仍在 Agent 创建后补发（`atri_x/app/runtime.py:260`）。

已确认的**有意差异**（不再当"漏跟"）：`additional_tools` / `Session.Binding` / 九字段日志契约 / 按 API 类型限制工具 / 压缩的服务端实现 / 日志脱敏（fork 保留并加强，与上游删除相反）。

**本项为纯 devdocs 交付，没有用户可见行为变化，因此按仓库规则不写 `CHANGELOG.md`。**

### 8.4 [x] fork 版信号/日志边界表 + `AGENTS.md` 采纳 mermaid 规则

- **边界表**：新增 [`signals-and-logging.md`](signals-and-logging.md)。它不采用上游那套实现，只写 fork 现在的真实边界：§1 两条独立设施（`atri_x/signal` 的进程级广播 vs `atri_x/hook` 的可改写边界，同一事实只允许走一条）；§2 mermaid 处理链路并把每个文件职责标在节点上；**§3 表一"信号所有权与依赖边界"**（`atri_x/signal` / 定义方 / 发布方 / 订阅方 / hook / App / logging 各自做什么、不做什么）；**§4 表二"必须同步完成 vs 可以广播"**（权限裁决、可改写 Hook、确认等待、Usage 记账、关键持久化必须同步；日志异步；平台连接广播；通知在 fork 里是同步直达；媒体清理后台并发）；§5 日志侧边界（`module`/`result` 契约、等级与结果独立、审计独立下限、文本行 + `body_json` 的正文形态、fork 保留脱敏）；§6 新增信号时的检查单；§7 有意差异清单。
- **`AGENTS.md`**：采纳上游 `73b81e90` 的"在架构规划时，写 Mermaid，并在其中注明每个文件职责"，并在 locator 表补 `locator:signals` 入口。
- **验证**：`locator:signals` 在文档与 `AGENTS.md` 两端都在；新文档里的相对链接逐个 `Test-Path` 存在；未触碰 `docs/`、`README.md`、`CHANGELOG.en.md`。
- **本项为纯 devdocs/规则文档交付，没有用户可见行为变化，因此按仓库规则不写 `CHANGELOG.md`。**
- **遗留待确认**：本次会话收到的 `AGENTS.md` 指令里还有两条本仓库文件里**没有**的规则——"所有包、文件位置、包名、文件名、函数名、变量名不能自作主张，必须和用户讨论"与"对于比较底层的定义型字段，需在代码的注释中说明字段作用"（这两条在上游 `AGENTS.md` 里存在）。按约定只采纳了 mermaid 规则，是否把这两条也补进本仓库请用户决定。

### 8.5 [x] 通知队列与背压 — 提交 `6e023db1`

**改动前的问题**：`agentOutputSender.SendNotice`（`atri_x/agent/output.py`）同步直达平台适配器，这一层没有队列与容量上限（只有 OneBot 适配器内部另有 normal/high 队列）；一个发不动的平台会让所有通知无限堆在它上面。上游 `notification.Manager` 的做法是每平台 8 个发送槽 / 256 等待、同目标串行、队列满返回 `ErrQueueFull` 且不自动重试。

**已确认的两个决策**（用户选定）：队列放 `atri_x/agent/notice_queue.py`（复用已移植的 `atri_x/signal.Queue`，与 `platform_signals.go` 同一套写法）；**保持调用方同步语义**（提交后等真实 `Receipt` / error），只加并发上限、同目标串行与队满快速失败。

**落地**：每个平台一条 `signal.Queue`（默认 256 等待 / 8 并发），串行键 `noticeTargetKey` = 平台 + scope + 收件人；`noticeDispatch` 先解析落点（当前会话 sender 还是某平台 sender）再入队，解析失败不进队列；队满记一条 `notice queue full` WARN 并原样返回错误；`Agent.CloseNoticeQueues` 先停止接纳再排空，由 `atri_x/app` 的运行时关闭步骤在 30 秒预算内调用（顺序：平台连接订阅 → 通知队列 → hook 运行时）。

**与上游的差异（有意）**：不做异步化（fork 的调用方靠返回的 `Receipt`/error 决定后续：命令回复、continuation、Hook 失败通知）；不做"发送前重查 binding"（fork 没有上游的 `session.Binding`）。

**验证**：`python scripts/check_syntax.py` 干净；`python -m unittest discover -s tests -t .` 通过；`./atri_x/app/`、`./atri_x/agent/...`、`./atri_x/delivery/...` 全绿（`atri_x/agent` 仅剩 §6 既有环境失败）。新用例 `internal/agent/notice_queue_test.go`：同目标串行（最大并发 = 1）、不同目标可并发、队满返回 `ErrQueueFull`、关闭后 `ErrClosed`、关闭预算耗尽时在途与等待的发送都会拿到结果而不挂住、`noticeTargetKey` 的同/异目标区分。

**风险说明**：这一项动的是对话主链路上的通知发送，因此刻意只加"上限"、不改"契约"；`SendChat`（对话正文）与流式输出**没有**进队列，行为与改动前一致。

**明确不搬**（已逐个核对，写在这里避免下一轮重新论证）：`internal/events`（九字段契约的载体）、`internal/modelmgr`、`internal/contextinfo`（只借"按域快照"的模式）、`*/module` 五个装配包、`atri_x/app` 拆成 12 个文件。`internal/fileops` / `internal/workspace` / `internal/sandbox` / `internal/doctor` / `internal/notification` 在 fork 都已有等价物（分别是 `atri_x/utils/fileops`、`atri_x/agent/workspace.py` + `atri_x/tool/builtin/workspace.py`、`tool.SandboxContext`、`atri_x/app/doctor.py`、`atri_x/delivery` + `Agent.SendNotice`），上游只是把它们提成了独立包——纯搬家，零收益、大冲突面。

## 9. 待办：上游查询层新功能（可分离搬运）

按 §7 的结论，这 8 项可以只动查询/展示层与写入侧的少量字段，不需要吞下上游的信号/日志骨架。按成本从低到高：

| # | 功能 | 落点 | 状态 |
| --- | --- | --- | --- |
| 1 | `--raw` 与 `-d` 解耦（`/log` `/audit` `/atriwisp`）+ 补全项 | `atri_x/agent/commands/log.py` | `[x]` 提交 `803de2ab`（与第 2 项同一提交） |
| 2 | 移除 `/log -t`（保留 `/audit -t`）+ 补全表更新 | 同上 | `[x]` 提交 `803de2ab`；`--event` 补全的事件改名**未采纳**（fork 的事件名与命名流程不同），改为补上一直缺失的 `--tag` 补全 |
| 3 | 损坏行计数与提示（`Reader.Query` 返回 `{Entries, DamagedCount, DamagedLines}`） | `atri_x/logging/reader.py` + `log.go` + `usage.go` + `maintenance/report.go` + `agent/logging.go` | `[ ]` |
| 4 | `/audit --run_source foreground\|cron\|atrinis`（需先在 `tool_call` 审计写入侧加稳定字段） | `atri_x/agent/chat_tools.py` 等写入点 + `log.go` | `[ ]` |
| 5 | 工具日志正文按类别限长（读/查/说明 ≤128、执行/编辑/写入/媒体 ≤256） | 工具日志写入侧 | `[ ]` |
| 6 | `/usage` 未知/非法/溢出显示 `null`（原来 `atoiSafe` 把非法值当 0） | `atri_x/agent/commands/usage.py` | `[x]` 提交 `25cf1bad` |
| 7 | `/audit` 不再显示 JSON 转义 | 需先复现：fork 存的是 `body_json` 文本字段并在 raw 视图里缩进，可能不存在该缺陷 | `[ ]` |
| 8 | `/atriwisp --tag` | — | `[x]` fork 早已有（`e94f1adb`），上游只是换了实现；本次补上了它的选项补全 |

**不搬**：上游的 `ContentFields` / `ContentFieldContains`（那是"字段被塞进 JSON content 里"的补偿机制；fork 的 `key=value` 文本属性本来就能直接按字段筛 `risk` / `tool` / `actor_id`）、九字段 `LogRecord`、`ContentLimit` 强约束。**若要做，只搬查询层与写入侧的加法式字段**。

除本表之外，§8.3 的核对单还确认了几个**不在查询层**的缺口（`shell` stdout/stderr 上限 16 KiB → 256 KiB，`atri_x/tool/builtin/shell.py:24`；启动插件告警的投递时机，`atri_x/app/runtime.py:260`；`internal/fileops/rollback.go` 的上游增量）。这三项与 §9 的剩余项一起在本轮做了逐项判定，见 §9.1（结论：前两项要做，第三项不搬）。

### 9.1 逐项判定（本轮研究结论）

**本轮执行结果（6 项 6 个提交）**：第 3 项损坏行 `04265c28`；第 4 项 `/audit --run_source` `74b3275`；第 5 项工具正文分类限长 `d85cad7`；第 7 项判定"不搬"（fork 无该缺陷）；`shell` 上限 `40930f3`；启动插件告警 `bbc3a185`；`internal/fileops/rollback.go` 整文件不搬，但其中核对出的真缺陷 E 已修 `9ae9c404`。

判定分三档：**直接移植**（可整段照抄）· **改造**（只搬行为，在 fork 的等价落点重写）· **不搬**（依赖上游骨架，或 fork 不存在该缺陷）。

| # | 项目 | 判定 | 依据 |
| --- | --- | --- | --- |
| 3 | 损坏行处理（跳过 + 计数提示） | **改造（推荐；换理由，不是"移植上游的修复"）** | 上游修的是**上游 JSONL 读取器**把坏行当致命错误（`atri_x/logging/reader.py` 的判定全部建立在 `category/type/result_status/content` 等 fork 没有的字段上，整段搬会把 fork 自己的日志全判成损坏）。fork 没有"坏行导致查询失败"这个失败模式：`atri_x/logging/reader.py:46` 的 `Query` 不会因坏行失败，`parseLogLine`（`:125`）对任何一行都返回条目。**但实测发现 fork 的真实问题在别处**（污染行 = 非 key=value 行 + 全 JSON 行，用临时探针实测）：坏行被当成**零字段记录返回并占掉 `--limit` 配额**，`/audit` 会把它渲染成一条"未知事件"的空记录（`atri_x/agent/commands/log.py:598` 的 `fieldOr`）。所以要做的是"**坏行跳过 + 计数提示**"：`Reader.Query` 需返回 `{Entries, DamagedCount, DamagedLines}`，判定规则要**按 fork 的文本格式另写**（保守：非空行且既无 `time=` 也无 `msg=`、解析不出任何合法 key/value 才算损坏），样本上限沿用上游语义（10 条 / 256 KiB，且不参与匹配）。波及 `atri_x/agent/commands/log.py:254`、`usage.go:62`、`internal/agent/commands/register.go:199`（接口签名）与 `atri_x/maintenance/report.py`，另有大量测试要跟着改签名。属**用户可见变化**（正常结果里不再出现零字段坏行；扫描到坏行时末尾多一句提示）。 |
| 4 | `/audit --run_source foreground\|cron\|atrinis` | **改造（建议做，比上游简单）** | 上游把来源塞进工具调用正文的 JSON（`atri_x/toolrun/runner.py` 的 `recordCall`：结构体字段 `RunSource`，值在调用前由 `sandboxctx.SandboxContextFromContext(ctx)` 的 `Background`/`BackgroundKind` 固定，读取侧还要在 `atri_x/logging/reader.py:464` 特判 `ContentFields`，命令行侧在 `internal/command/builtin/log.go:301` 校验取值）。fork 的审计记录本来就是 `key=value` 属性（`atri_x/agent/logging.py:73`），写侧多一个属性、查询侧用现成的 `query.Fields`（与 `--tool`/`--risk` 同一套）即可，**读取侧零改动**（`parseTextAttrs` 自动解析新 key）。来源事实 fork 已经有：`tool.SandboxContextFromContext(ctx).BackgroundKind`（`atri_x/tool/sandbox.py:13-40`；后台运行在 `atri_x/agent/cron.py:90` 写入 `Background: true` + kind）。**不要走"查询层按 `session_id` 前缀猜来源"这条路**：会话 ID 由存储层生成（`atri_x/agent/cron.py:155-156`），且 `background_kind` 会被前台接管清掉（`atri_x/session/promotion.py:90`），猜出来的结果不可解释。落点：`atri_x/agent/chat_tools.py:167`/`:182` 加 `run_source`、`atri_x/logging/contract.py` 登记三个取值常量、`atri_x/agent/commands/log.py` 的选项/帮助/补全/展示。缺字段的历史记录按上游语义**不补造**，查不到就是查不到。 |
| 5 | 工具日志正文按类别限长 | **改造（可选，收益有限）** | 上游是每个工具自己 `defer func() { logContent = tool.TruncateLogContent(logContent, N) }()`（读/查/说明 128、执行/编辑/写入/媒体 256，例：上游 `atri_x/tool/builtin/{file_tools,shell,cron,long_memory}.go`），并且**按工具逐个自报预算**而非按类别枚举（同一个 `cron.go` 里 128 与 256 都有）。fork 的工具接口**没有独立 logContent**（`atri_x/tool/tool.py:61` 是 `Call(ctx, req) (*Result, error)`），审计正文是 Agent 侧从业务结果生成的预览，且**已经有统一限长**：`resultPreview` 120 rune、`argumentsPreview` 160 rune（`atri_x/agent/chat_tools.py` 的 `maxPreviewRunes`）。所以要做的是**只把限长改成分类取值**（读/查/说明 128、执行/编辑/写入/媒体 256）+ 一张按工具名的分类表，落点只有 `recordToolCall`（`atri_x/agent/chat_tools.py:141-194`）与两个预览函数，不需要动工具接口。注意 fork 的审计是单行 atri_x/logging/ 文本，`result` 里的真实换行会被引号转义；提到 256 时要么保留短预览，要么把换行归一到 `\n` 字面，别把单行结构撑坏。属整洁性收益，不是缺陷修复。 |
| 7 | `/audit` 不再显示 JSON 转义 | **不搬**（fork 无此缺陷） | 上游的病灶是审计正文存成 JSON `body`、非 raw 视图直接打印该 JSON 字符串（`\n` 显示为字面量），修法是展示层再解码一次（上游 `internal/command/builtin/log.go` 的 `formatLogContent`）。fork 的文本行在**读取时**就 `strconv.Unquote` 还原引号内的值（`atri_x/logging/reader.py:159-190` 的 `parseTextValue`），展示层 `appendField`（`atri_x/agent/commands/log.py:768`）直接把值写出去，本来就是真实换行；raw 视图里唯一单独排版的是 `body_json`（`log.go:730` 的 `formatDebugLogEntry`），也是按纯文本缩进，不做二次转义。**已有回归保护**：`internal/agent/commands/log_test.go:403` 的 `TestLogCommandDebugShowsParsedBodyJSON` 断言输出必须含解码后的 `body_json:` 且不得含 `body_json="{\"`。另：fork 现在连 `body_json` 都不再写（`atri_x/llm/openai/openai.py:345` 是注释行，测试 `openai_test.go:170` 明确禁止该字段），链路两端都是闭合的。 |
| — | `shell` stdout/stderr 上限 16 KiB → 256 KiB | **直接移植（1 行常量）** | 上游 `atri_x/tool/builtin/shell.py:27` 的 `maxShellOutput = 256 * 1024` 与 fork `atri_x/tool/builtin/shell.py:24` 的 `16 * 1024` 是同一常量，两侧 `shellOutputBuffer`（fork `:347-374` ↔ 上游 `:377-396`）逐行同构（同样的 `min` 截断、同样的"仍返回 len(p) 让进程排空"、同样的 `... output too long; truncated to first N KiB ...` 文案），stdout/stderr 各自独立计数。所以这是本清单里唯一可以整段照抄的项。已核查连带影响：`internal/tool/builtin/shell_test.go:441-463` 全程用 `maxShellOutput` 符号、全目录无写死 16 KiB 的断言（改常量自动跟随）；该缓冲不回写日志（审计正文是 120 rune 预览）、不直接发平台；代价是单次 shell 结果变大（内存 ~32 KiB → ~512 KiB，给模型的 token 变多）。注意别顺手改 `atri_x/tool/skill/runner.py:21` 的 `maxRunnerOutput`（同名数值，与 shell 无关）。 |
| — | 启动插件告警的投递时机 | **改造（建议做；影响面比初判小）** | 加载告警在 Agent 存在前先攒起来（`atri_x/app/runtime.py:215` 的 `startupHookNotices`，写入点 `:385`/`:389`），`buildAgent` 成功后回放（`runtime.go:260-262`，`context.Background()`）；真实平台发送器要到 `Integrations.Attach` 才注册（`atri_x/app/integrations.py:62` → `atri_x/app/platforms.py:118`，Attach 在 `atri_x/app/runner.py:120`、即 `Runtime.Build` 之后）。**回放落到哪里取决于主平台**：Agent 构造时已把 `platforms.Primary` 注册成发送器（`atri_x/agent/core.py:439-441`），所以 `noticeDispatch`（`atri_x/agent/output.py:82-91`）在启动期总能解析到它——full/CLI 模式的 primary 是 cli 适配器，告警进 `sendTUIMessage`（TUI 未起来时压在 `a.output` 缓冲里，界面起来才显示；非 TTY 直接打印），**属于"能到但被推迟"**；service 模式的 primary 是 headless（名字 `service`，`atri_x/platform/builtin.py:42-43`），`headless.SendNotice` 直接报 `service platform cannot send notice without a target platform`，只剩 `delivery.Manager` 一条 WARN（`atri_x/delivery/delivery.py:341-348`），**用户侧完全看不到**——而 hook 配置写错恰恰是 service 部署最常见的运维问题。改造方向与上游一致（上游把首次 Reload 从构造期移到模块 `Start()`、接线完成后才跑）：把 `hookService.HookReload()` 与告警处理移出 `buildHookService`（`runtime.go:383-390`），改在 `Integrations.Attach` 成功之后调用；告警送到"已启用且有超管配置"的平台（`Target{Platform: name, Superadmins: true}`，超管由适配器内部解析），一个可用通道都没有时至少落一条明确的 WARN。顺带删掉 `startupHookNotices` 与补发循环，避免两套通道。 |
| — | `internal/fileops/rollback.go` 的上游增量 | **不搬整文件**；顺带核对出 fork 自己的缺陷，见 §10.6 | 两侧不是同一件东西：上游 `RollbackManager` 以 `Binding` 为键（`Forget(binding)`/`Session(binding)`/`validLocked`），fork `atri_x/utils/fileops/rollback.py` 是以 `sessionID` 为键的备份存储（`Record`/`List`/`Restore`/`Forget`）；绑定租约在 fork 无对应概念（`session.Binding` 已列为有意差异），"编码保持"fork 早已具备（`internal/utils/fileops/encoding_test.go:83` 的 `TestEditFilePreservesEncodingAndBOM`）。**但逐条对上游 diff 时发现三条 fork 真缺陷（新建文件回滚不校验内容=会删掉用户后来的修改、符号链接目标不复核、无界读取），已单列 §10.6。** |

---

## 10. 上游最新提交评估（`1aa10fd9..a0304ad4`）：平台失败隔离与接管同步

`6fd1d1c` 一个提交里塞了三件互相独立的事，且都落在"fork 有对应物但结构不同"的位置。

| 项 | 上游落点 | 判定 | fork 落点 |
| --- | --- | --- | --- |
| 平台异常退出隔离 + 服务模式保活 + 跨平台超管通知 | `atri_x/platform/module/{module.go,notifications.go}` | **改造（本轮第一优先）** | `atri_x/app/platforms.py` + `PlatformRunRequest` |
| 前台接管局部信号 / `Execution` 内存快照 / 发送时改道 | `atri_x/session` + `atri_x/turn` + `atri_x/agent/dialogue` | **不搬机制**；但**要跟**"查不到就停下"的口径与跑完复核 | `atri_x/agent/{cron.go,chat.go}` |
| `delivery.LogSendResult` + 失败日志 `context.WithoutCancel` | `atri_x/delivery/delivery.py` | **不搬** | — |
| `a0304ad` 的 `errors.Is(err, runCtx.Err())` 口径 | `internal/platform/module/module.go` | **随平台项一起对齐（1 行）** | `atri_x/app/platforms.py:149` |

### 10.1 平台失败隔离、服务模式保活、超管通知 —— 建议**改造**（本轮价值最高）

**上游做法**：`runPlatforms` 里每个适配器退出后从 `running` 集合里删掉；退出过滤从 `!errors.Is(err, context.Canceled)` 改成 `runCtx.Err() != nil && errors.Is(err, runCtx.Err())`（`a0304ad` 把 `==` 修成 `errors.Is`）；**非预期**退出（`runCtx.Err() == nil`）时调 `notifyPlatformFailure`——从 `m.superadmins[其他仍在运行的平台]` 收集**去重后**的私聊目标，用 `events.NotificationRequested.EmitAsync` 异步发一条 `平台 %s 异常退出：%v`（Error 级）；`platformStopsAppOnExit` 只在**非** service 模式生效，service 模式下即使全部平台循环退出也继续 `<-ctx.Done()` 等外部停止。上游 `CHANGELOG.md` 对应条目："服务模式不再随全部消息平台退出而结束。"

**fork 现状**（`atri_x/app/platforms.py:139` 的 `runPlatforms`）：
- 适配器退出只写一条 WARN（`:149`），且只过滤 `errors.Is(err, context.Canceled)`；只有 `StopAppOnExit()` 为真才 `cancel()`（`:152`）——全仓库只有 CLI 适配器实现它（`internal/platform/cli/cli.go:61`），而 CLI 适配器只在 CLI-only 模式加入 `Runtimes`（`atri_x/platform/builtin.py:45-47`，service 模式的 primary 是 headless）；
- `wg` 全部结束就 `return nil`（`:199-200`），进程随之退出：service 模式下"最后一个平台异常退出"会**静默停服**。只有"一个平台都没启用"才有保活分支（`:168-185`，注释里已经写明 Docker/systemd 的 restart policy 考虑）——也就是说上游这次补的正是**其余情况**；
- 没有任何"平台死了"的通知，而且 health 侧也**不会**反映：`health.State.MarkPlatformDisconnected`（`atri_x/health/state.py:251`）在全仓库**只有测试调用**（`grep` 命中 4 处全是 `_test.go`），`MarkPlatformConnected` 之后平台状态永远停在 connected，`/ready` 不会因平台死亡转 not-ready；
- 适配器本身已内含重连退避（`qq-onebot`/`telegram` 只在 ctx 结束时返回；`qqofficial` 只在 `reason.fatal` 时返回），所以"全部退出"主要来自 qqofficial 致命错误或多个适配器同时被取消。

**建议（三件事一起做，都落在 `runPlatforms` 的退出分支）**：
1. 把模式带进 `runPlatforms`（`PlatformRunRequest` 现在只有 `Handler/Logger/Runtimes/AfterStart/Heartbeat`，见 `atri_x/app/dependencies.py:145`；`runner.go:37` 已经有解析好的 `mode`，加一个字段即可），service 模式下全部平台退出后继续等 `ctx.Done()`；**做法上要把 `:168-185` 的"无平台保活"与 `:186-206` 的等待合并成一个循环**（保留 10 秒心跳与"no platform is enabled"那条 warn 只在真的没有平台时打印），不要并列两套等待逻辑；
2. 非预期退出时调 `healthState.MarkPlatformDisconnected(name, err)`——**这个 API 已经存在但没人用**，顺手让 `/ready` 说真话；
3. 通知其他平台的配置超管：`cfg.Security.Superadmins` 就是 `map[平台名][]id`（键与运行时名一致：`cli`/`qqofficial`/`qqonebot`/`telegram`，见 `atri_x/platform/qq_onebot/adapter.py:262`、`internal/platform/qqofficial/adapter.go:43`），发送走现成的 `delivery.Target{Platform: 名字, Superadmins: true}`（超管由适配器内部解析，见 `internal/platform/qqofficial/adapter.go:121-131`）+ `Agent.SendNotice`；注入点在 `atri_x/app/runner.py:183`（那里同时有 `runtime.Agent`、`foundation.Config` 与 `platforms.Runtimes`，且已在 `Attach` 之后、发送器已注册），只需给 `PlatformRunRequest` 加一个回调字段，不引入新包或全局状态。**"只通知仍在运行的平台"要靠本项第 1 条的 running 集合**（fork 现在没有这个集合）；目标 ID 若要与上游一样**去重**，需要在 app 层按 `cfg.Security.Superadmins[name]` 展开并逐条 `PrivateUserID` 发送（适配器的 `Superadmins: true` 分支自己不去重）。

**风险与边界**：只在 service 模式改退出语义，CLI-only 的 `/exit` 仍要退出；`StopAppOnExit` 的"service 下不认"保护在 fork **今天是空转**（service 模式的 `Runtimes` 里没有交互 CLI，只有 remote CLI server），属于未来保险；`atrinisRuntimeAdapter` 也在 `Runtimes` 里（`atri_x/app/integrations.py:56`，上游把 Atrinis 放在 background 模块、天然不在平台列表里），要明确它的退出**不影响保活**、也**不作为通知对象**（fork 别处已经显式把它当"非平台"，见 `integration.go` 的 `runtimeNames`）；通知必须用未被取消的 ctx 或 `context.WithoutCancel(runCtx)`（`Agent.SendNotice` 会先在 `turnOutputAllowed` 里检查 `ctx.Err()`，见 `atri_x/agent/turn_gate.py:41-49`，fork 已有同款写法 `atri_x/agent/platform_signals.py:84`），且只在 `runCtx.Err() == nil` 时发；非 service 模式下 `case <-done: return nil` 之后进程就会退出，**异步通知会被截断**，所以建议在失败适配器的 asyncio task 内同步发（受通知队列 256 等待 / 8 并发与适配器自身超时约束），或至少在返回前等一次结果。

**现实触发面**（决定这一项的优先级）：qq/telegram 的 `Run` 是无限重连循环，只在 ctx 取消时返回；交互 CLI 只在 `/exit` 或 stdin EOF 返回（CLI-only 模式，本就应该退出）。真正会"非预期退出"的是 remote CLI server（端口被占，`atri_x/platform/cli/remote_server.py`）与 Atrinis 的 HTTP server 启动失败——都是**启动期配置错误**，而且此刻日志里已经有一条 WARN。因此第 1 条的价值在于"最后一个 runtime 退出后 health 接口不再消失、SIGTERM 仍是干净的退出路径"，第 2/3 条的价值在于"某平台死了但其它平台还在服务、进程不会重启"这个场景。

### 10.2 前台接管改局部信号 / 内存快照 / 发送时改道 —— 机制**不搬**，但有一条**要跟**

**上游做法**（`6fd1d1c`）：`internal/session/signals.go` 新增 `ForegroundActivatedEvent{Session, Binding}` 与 `Service.ForegroundActivated()`，`promotion.go` 在接管提交后、准入释放前用 `context.WithoutCancel(ctx)` 同步 `Emit`（原来注入的是回调 `SetForegroundActivation`），`internal/agent/module/signals.go` 把信号连到 `ExecutionCoordinator.AdoptForeground`；`internal/turn/execution.go` 的 `Adopt` 同时保存前台 ctx 与会话快照并新增 `ForegroundState()`（同一把锁返回两者）；工具边界刷新从**重读数据库**的 `RefreshSession` 换成**只合并内存快照**的 `RefreshExecution`；`atri_x/toolrun/runner.py` 的刷新失败从静默 `Stopped: true` 改成返回 `Err`。

**为什么 fork 不该搬**：
- fork 的接管是**安全点中止**而不是"接管继续"：`atri_x/session/promotion.py` 在同一事务里写 `foreground_origin`，在途后台 turn 在三个安全点（`atri_x/agent/chat.py:242`、`:354`、`:368`，另有 `atri_x/agent/cron.py:119`）判定后返回哨兵，由 `RunBackground`/cron/Atrinis 收敛成 `TakenOver`。上游那两条"接管后继续跑"才有的缺陷——上下文与路由来自不同次接管、重读整行覆盖已提交工具状态——**在 fork 不存在**（全仓 `RefreshExecution`/`RefreshSession` 0 命中；工具边界只用内存 session，工具状态写入走事务内合并，`atri_x/agent/tool_cache.py:114-136`）。
- fork **没有可注入的对象**：`atri_x/turn/` 只有 `manager.go`（没有上游的 `Execution`），信号订阅方拿到事实后只能写一张 Agent 内存 map，再由现有轮询读取——行为增益≈0（少一次主键 SELECT），却要新增跨包信号、发布点、订阅与关闭顺序（约 60–100 行）。而且信号**不能替换**轮询：`/resume` 可能在订阅建立之前发生（启动恢复、工具内触发），按 fork 自己的规范"订阅必须早于第一次 Emit、信号不缓存不重放"（`devdocs/signals-and-logging.md`），必须保留读库兜底。
- 硬约束：`Emit` 是**同步等待**（默认 30 秒超时），而 fork 的 SQLite 是**单连接**（`atri_x/storage/sqlite/store.py:39` 的 `db.SetMaxOpenConns(1)`）——订阅方一旦在派发里碰数据库就是硬死锁，所以"订阅方只更新内存"在 fork 是强制条件而不是优化建议。

**要跟的那一条（真实缺陷，成本很小）**：fork 的 `backgroundSessionTakenOver`（`atri_x/agent/cron.py:194-203`）在 `Sessions().Get` 失败时 `return false`，把"读不出来"当成"没被接管"——在途安全点会继续烧 token 跑到自然结束，随后 cron/Atrinis 照常解析报告并**按 job 目标投递**（上游的策略相反：取不到就保守当作已接管并停下，见上游 `internal/background/takeover.go`、`atri_x/cron/execution.py`、`atri_x/atrinis/llm.py`）。上游同一次修复里"静默吞掉刷新错误"针对的正是这类问题。建议最小改造：
- `backgroundSessionTakenOver` 改成 `(bool, error)`：`storage.ErrNotFound` 视为未接管返回 `nil`，其他错误原样返回；
- 四个采样点（`chat.go:242`/`:354`/`:368`、`cron.go:119`）在 `err != nil` 时按"已接管"中止，并留一条可查的审计/日志说明原因；
- `atri_x/agent/cron.py:122-136`（`startBackgroundChat` 正常返回之后）**补一次跑完复核**，对齐上游的兜底；这一条同时关掉另一个窗口：安全点 3（`chat.go:368`）与助手消息落库（`chat.go:423-428`）之间还夹着 `prepareAssistantOutput`（hook，可能很慢），这期间提交的接管会让**迟到的后台回答写进已被提升的前台会话历史**，cron/Atrinis 也可能据此投递报告。
- 合计约 25–30 行源码 + 测试；不动 `atri_x/session`、不动 `atri_x/signal`、不动装配顺序、不引入 `session.Binding`。

### 10.3 `delivery.LogSendResult` 与失败日志 `context.WithoutCancel` —— 不搬

上游这么做是因为他们的发送失败日志走 `events.EmitLog`（会随请求取消而丢），且 CLI 这类"原生通知发送方"绕过了 Delivery。fork 的失败日志是普通 `atri_x/logging/` 调用（`atri_x/delivery/delivery.py:345` 的 `Logger.WarnContext`，TextHandler 不看 ctx），审计记录走 `a.audit`（内部固定 `context.Background()`，`atri_x/agent/logging.py:83`），并且**所有**通知发送都经过 `delivery.Manager`（`agentOutputSender` 就是 `Manager.Sender`，`atri_x/agent/output.py:27`/`:246`），没有"绕过 Delivery"的第二条路。没有可修的缺陷，抽一个 `LogSendResult` 只是搬家。

### 10.4 取消口径的两处小差异（低优先，可选）

1. `atri_x/app/platforms.py:149` 只过滤 `errors.Is(err, context.Canceled)`；上游用 `runCtx.Err() != nil && errors.Is(err, runCtx.Err())`。差异表现为 `DeadlineExceeded` 或带自定义 cause 的取消会多打一条"platform stopped with error"。做 10.1 时顺手对齐（1 行）。
2. `atri_x/cron` 全包没有取消分类（`context.Canceled`/`errors.Is` 0 命中）：`atri_x/cron/service.py:133-137` 对任何运行错误都发 `cron.trigger_failed` WARN；上游把它记成 `ResultCanceled`。关机时在途 cron 运行会在 fork 留下一条"失败"审计 + WARN。建议单独立项（约 6 行），不要与 10.1/10.2 混在一个提交里。

### 10.5 本轮建议的执行顺序（供决策）

| 顺序 | 项 | 性质 | 成本 |
| --- | --- | --- | --- |
| 1 | §10.6 `/rollback` 对"新建文件"分支补冲突校验 | **缺陷修复（数据丢失级：会删掉用户后来的修改）** | 很小（~10 行 + 测试） |
| 2 | §10.1 平台失败隔离 + service 保活 + 超管通知（含 §10.4.1 的 `errors.Is`） | 用户可见（不再静默停服 / 超管收到告警 / `/ready` 说真话） | 中 |
| 3 | §10.2 接管检查失败"保守停下" + 跑完复核 | 缺陷修复（不再向已接管会话投递后台报告） | 小 |
| 4 | §9.1 启动插件告警的投递时机 | 缺陷修复（service 模式下这类告警用户看不到） | 小-中 |
| 5 | §9.1 `/audit --run_source`（写入侧加字段） | 新功能（对齐上游查询能力） | 小-中 |
| 6 | §10.6 的 symlink 目标复核与有界读取（F/G） | 安全性与内存边界 | 小 |
| 7 | §9.1 `shell` 上限 1 行 + 工具日志分类限长 | 行为对齐 / 整洁性 | 很小 |
| 8 | §9.1 损坏行跳过与计数 | 行为对齐（中成本，涉及 `Reader.Query` 签名与 4 个消费点） | 中 |

**执行结果**：1 → `9ae9c404`；2 → `fe98bba`（含 §10.4.1 的 `errors.Is` 口径）；3 → `a85f330`；4 → `bbc3a185`；5 → `74b3275`；6 → `d85cad7`；7（与 5 同项）→ `40930f3`；8 → `04265c28`。未开工：§10.4.2（cron 取消分类）与 §10.6 的 F / G / B / A / H。

**明确不做**：§9.1 的 `/audit` JSON 转义（fork 无此缺陷，已有回归测试）、`internal/fileops/rollback.go` 的整文件替换（结构不同；只搬 §10.6 列出的几条独立缺陷）、§10.3（无对应结构）、§10.2 的局部信号与 `Execution` 快照（依赖 fork 没有的骨架）。

### 10.6 顺带发现：`/rollback` 的几条真缺陷（不由 `6fd1d1c` 引入，核对上游实现时发现）

逐条对着上游 `internal/fileops/rollback.go` 看 fork `atri_x/utils/fileops/rollback.py`，结论如下（**A/D/I/J 不搬**：绑定租约、`encoding` 字段、返回值形状、错误文案，都是结构差异或 fork 已更好）：

| # | 上游行为 | fork 现状 | 判定 |
| --- | --- | --- | --- |
| **E** | 提交前对**新建**路径同样复核 `RevisionAfter`（上游 `inspectFileState`，覆盖新建与覆盖两条路径） | 已在删除前先 `os.ReadFile` 并比对 `ContentRevision(current) != record.RevisionAfter`（不一致返回 `ErrRollbackConflict`） | **已修 `9ae9c404`（数据丢失）**：此前 `!existed` 分支只看文件是否存在就删除，用户改过 ATRI-X 新建的文件后再回滚会静默删掉那些改动；也与工具自述"不覆盖 Shell 或外部程序在编辑之后做的修改"矛盾。新增 `TestRollbackStoreKeepsCreatedFileChangedAfterTheEdit` 与 `TestRollbackStoreReportsMissingCreatedFile`。 |
| **F** | 记录"解析后的真实目标"并在回滚前复核（`ResolveFileTarget` / `checkFileTarget` / "file target changed; rollback refused"） | 直接对 `record.Path` 做 `os.ReadFile` / `os.Remove` / `AtomicWriteFile`（`:187/205/211/201`），而 `AtomicWriteFile` 明确**会写穿符号链接**（`atri_x/utils/fileops/atomic_write.py:10-12`） | **建议做**：记录后该路径的符号链接被改指别处时，回滚会读/写新目标，可能覆盖用户没编辑过的文件。需要在 `rollbackRecord` 里存下解析后的目标（会改 `RollbackInfo` 结构与 `Record` 签名，调用点 4 处），约 20–30 行。 |
| **G** | 有界读取 `readRevisionBytes`（先 stat 判普通文件与大小，再 `io.LimitReader`） | `os.ReadFile` 整文件入内存，无类型/大小前置（`rollback.go:187`） | **建议做**：外部把该路径换成超大文件时 `/rollback` 会读满内存。约 20 行（需在记录里存 after 大小）。 |
| **B** | 备份取自**编辑自己那次读取**的字节（`options.beforeWrite`，上游 `:189-231`） | 编辑**之前另做一次** `captureEditBackup`（`atri_x/tool/builtin/file_tools.py:530`、`:542-554`）再调 `EditFileWithOptions`（`:531`）：两次读取之间有 TOCTOU 窗口，期间被并发改写时记下的"编辑前内容"不是紧邻编辑前的版本 | **单独排期**（要动编辑核心，给 `fileops.EditFileOptions` 加钩子，约 50–70 行）。今天的两次读取用同一个 100 MiB 上限，所以不存在"采集失败被当成新建"的问题。 |
| **A** | `Binding` 租约 + 失效回收 | fork 以 `sessionID` 为键，`Forget` **全仓库没有生产调用点**（只有 `rollback_test.go:106`） | **不搬语义**，但**文案与实现不一致**：`/rollback` 帮助写着"切换 Session、重启或容量淘汰后记录失效"（`atri_x/agent/commands/rollback.py:81`），实际同一 sessionID 切回来记录仍在。要么把 `Forget` 接进会话切离/删除（app 层已有接线点），要么把文案改成与实现一致。 |
| **H** | 目标锁 `lockTarget`（同一目标的读→写串行） | `Restore` 取完记录就释放锁（`rollback.go:179-181`），读文件→写文件之间没有串行保护，与并发的 `edit_file`/`rollback` 可能交错 | **可选（低）**：概率低，但顺着 E/F 一起改成本很小。 |

**装配/发送路径的一条通用结论**（§10.1 与 §9.1 的启动告警都要用）：任何"启动期要发通知"的代码都必须放在 `atri_x/app/runner.py:120` 的 `Integrations.Attach` **之后**，因为 `RegisterPlatformSender` 只有一条链路（`runner.go:120 → integrations.go:62 → platforms.go:118`），在此之前 `agent.platformSenders` 里只有主平台（`atri_x/agent/core.py:439-441`）。用窄回调注入（`PlatformRunRequest` 加字段）而不是把 `*agent.Agent` 传进 `runPlatforms`：不引入循环依赖（`atri_x/app` 已经 import `agent`/`delivery`），测试面也更窄。
