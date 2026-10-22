# 上游 CHANGELOG × fork 现状核对单

> **本文档是 Go 时代的工作台账**：它记录的是本 fork 仍在 Go 语言下时，与上游 Go 仓库（[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)）逐条对照的结论。文中的 `internal/**` 包路径、`*_test.go` 用例名、`go build` / `gofmt` 都是当时的产物；本仓库已全量重写为 Python，包级对应关系见 [PORTING.md](../PORTING.md) §4，Python 侧的执行/验收命令见 [porting-status.md](porting-status.md) §2。台账里的每条结论在 Python 版是否仍然成立，需要按代码复核。
> 移植状态：见 devdocs/porting-status.md 待核对


本文回答一件事：**上游 `## Unreleased` 里的每一条，fork 到底跟了没有。**

- **口径**：上游取 `refs/remotes/upstream/main`（核对时 @ `1aa10fd`）的 `CHANGELOG.md` `## Unreleased` 段。
  上游最后一次发版是 `v0.5.0`（2026-09-28），与本 fork 的共同祖先 `d812628` 同日，因此这一段
  基本等于"分歧以来上游的全部条目"（4 类：Added 5 条、Changed 16 条、Fixed 23 条，共 44 条）。
- **判定方式**：先在本仓库 `CHANGELOG.md` 里找对应条目，再去代码里核实。只写有证据的结论；
  静态看不出的一律标"待复现"，不猜。
- **行号**：文中 `CHANGELOG.md:NN` 是本次核对时的行号（新条目会移位），引用时以原文关键词为准。
- **状态图例**：`已跟` · `部分` · `未跟` · `未跟（有意）` · `不搬`（纯结构重构） · `不适用` · `相反` · `待复现`

## 0. 速览

> **本轮（按台账 §10.5 执行）之后的增量**：原"未跟"里已有 6 条落地——`--raw` 解耦与移除
> `/log -t` `803de2ab`、损坏行处理 `04265c28`、工具正文分类限长 `d85cad7`、`shell` 上限
> `40930f3`、`/usage` null `25cf1bad`、启动插件告警 `bbc3a185`。下表是首次核对时的口径，
> 逐条的最新状态以各表行的"已跟/已完成"标注为准。

| 状态 | 条数 | 说明 |
| --- | --- | --- |
| 已跟 | 19 | 上游改了、fork 也有等价实现 |
| 部分 | 5 | 行为只对上一部分，或只落了接缝 |
| 未跟 | 12 | fork 确实还没有（其中 10 条值得单独评估）；本轮已落地 6 条 |
| 未跟（有意） | 2 | 是 fork 明确选定不做的差异，已记在 §1/§5 与台账 |
| 不搬 | 3 | 纯包/文件结构重构，零行为收益、冲突面大 |
| 不适用 | 1 | 上游条目依赖 fork 没有的包 |
| 相反 | 1 | 两边取向相反（日志脱敏） |
| 待复现 | 1 | 需要先复现才能判定（`/audit` JSON 转义；本轮判定为**不存在该缺陷**，见台账 §9.1 第 7 项） |
| **合计** | **44** | |

## 1. `### Added`（5 条）

| 上游条目 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| Responses 专用 `view_image`（媒体 ID / URL / 本地图片 / 当前聊天历史图片；本地路径仅超管） | **已跟** | `CHANGELOG.md:37`；`atri_x/tool/builtin/view_image.py` 支持 `source`（媒体 ID / HTTP(S) / 本地路径）与 `message_id` + `media_index` |
| Provider 新增 `api_mode="chat"/"response"` | **已跟** | `CHANGELOG.md:53`；`atri_x/app/models.py` 的 `newProviderLLM` 按 `[providers.*].api_mode` / `model_configs.<model>.api_mode` 选择适配器，混用时返回按模型分发的 `protocolRouter` |
| `edit_file` 内存备份 + `/rollback` + `rollback_file` | **部分** | `CHANGELOG.md:36` 有；但实现规模差一倍：上游 `internal/fileops/rollback.go` 566 行 vs fork `atri_x/utils/fileops/rollback.py` 243 行。两者共享同一批常量（`256*1024*1024` / `1024`）和逐字相同的错误文案，说明 fork 拿到的是早期版本；上游后来加了**会话绑定有效性与过期**（`Binding` / `ErrRollbackExpired`）、`Target`、编码保持。→ 台账 §8.3 |
| 仅超管可用的 `/doctor` | **已跟** | 台账 §0；`atri_x/app/doctor.py` + `atri_x/agent/commands/doctor.py` |
| `/log` `/audit` `/atriwisp` 新增独立 `--raw`；损坏日志行不再中断查询并提示数量 | **已跟** | `--raw` 与 `-d` 解耦 `803de2ab`；损坏行处理 `04265c28`（fork 的病灶不是"中断"而是坏行占 `--limit` 配额并冒充记录，见台账 §9.1 第 3 项）。 |

## 2. `### Changed`（16 条）

| 上游条目 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| 大幅重构底层，优化各包职责与边界 | **不搬** | 对应上游 `9234f85f` refactor app 与 `*/module` 五个装配包、`atri_x/app` 拆 12 个文件。纯搬家，零行为收益。→ 台账 §8「明确不搬」 |
| 重构日志系统 | **不搬** | 九字段 `LogRecord` + 全局 `LogSubmitted` 信号（`internal/events`）。台账 P1#1 已判定不做。 |
| 重构通知系统 | **部分** | 行为侧：群聊跳过工具预览规则已集中到 `atri_x/delivery`（`e141bdd`）；结构侧：fork 仍是 `agentOutputSender.SendNotice` 同步直达适配器，没有上游 `notification.Manager` 的队列/背压/同目标串行。→ 台账 §8.5 |
| 优化 CLI UI 逻辑 | **不搬** | 上游 `1ecaf4f2`，纯结构。 |
| 工具调用仅记录 audit；移除 `/log -t`；日志精简工具内容记录 | **已跟** | 移除 `/log -t` `803de2ab`；工具正文按类别限长 `d85cad7`（读取／查询／说明 128，执行／编辑／写入／媒体 256，参数统一 256）。→ 台账 §9.1 |
| 平台连接事件改为发布全局信号，Hook 与 Cron 各自订阅、按平台排队并管理关闭 | **部分** | hook 侧已跟（`0247c90f`：`platform.PlatformConnected` + `atri_x/agent/platform_signals.py` 按平台队列 + `ClosePlatformEvents` 排空，纳入 `atri_x/app` 的 30 秒关闭预算）；**cron 侧仍挂在 hook 点 `builtin.cron.missed_once` 上**，没有独立订阅与独立队列（台账 §8.2 已记为有意差异）。另外 fork 保留 App 里的单一发布点，没有让每个适配器各自 `Emit`。 |
| 工具支持按 API 类型限制发现、补全、预载和执行 | **未跟** | 代码里没有"协议类型 → 工具可用性"的联结（`git grep -E 'APIType\|apiType' internal` 无结果）。这是 P2 路线 D 里明确未实施的部分（台账 §5「仍未做」）。 |
| `shell` 的 stdout／stderr 返回上限从各 16 KiB 提高到各 256 KiB | **已跟** | `atri_x/tool/builtin/shell.py:24` 已改成 `maxShellOutput = 256 * 1024`（提交见台账 §9.1）。同一条里的"边收边限容"部分 fork 本来就已跟，见 Fixed 表第 2 条。 |
| Responses 工具定义改为递增 `additional_tools` | **未跟（有意）** | `atri_x/llm/openai/responses.py:59` 明确写着 "never sends previous_response_id / additional_tools"；`atri_x/llm/protocol.py:14` 也把这一点记为 fork 的 Responses 适配器定位。台账 §5「仍未做」。 |
| 后台路径说明从四个路径工具的 schema 移至系统提示 | **未跟** | `atri_x/toolrun/toolrun.py:152` 仍把 `tool.BackgroundPathInstruction()` 拼进 `schema.Function.Description`。 |
| 压缩按会话协议分派（Chat 用摘要模型、Responses 用当前模型并保存原生窗口） | **部分** | `d531763` 只落了接缝：`contextRuntimeState.compactorFor` 按 `ProtocolCapabilities` 选后端，但目前恒定选中客户端摘要后端 `client_summary`，没有 Responses 原生窗口实现（台账 §5「仍未做」）。 |
| 工具定义及 Hook 的只读 `llm.tools` 改为 `name/description/parameters` 业务结构 | **未跟** | `git grep 'llm\.tools'` 无结果。 |
| QQ OneBot 超过 3000 转为合并转发 | **已跟** | `CHANGELOG.md:35`（超长回复改为一条合并转发，每页一个 node）。 |
| Cron／Atrinis 首次在同平台私聊或 CLI 恢复时永久转为前台会话 | **已跟** | 台账 P1#4 / `345a1c1`；`CHANGELOG.md` 有"后台会话可被前台接管"。 |
| 媒体清理最多同时处理 4 个对象并按媒体 ID 互斥 | **已跟** | `CHANGELOG.md:270`。 |
| 获取模型信息超时从 5 秒放宽到 30 秒 | **不适用** | 上游改的是 `internal/modelmgr`（fork 没有这个包）。fork 的模型列表在 `atri_x/agent/model.py`，用的是 `modelListErrorTTL = 30 * time.Second`（失败缓存窗口，语义不同）。 |

## 3. `### Fixed`（23 条）

| 上游条目 | 状态 | 证据 / 说明 |
| --- | --- | --- |
| 普通 `/audit` 等日志查询直接展示 JSON 转义的 bug | **待复现** | 上游的成因是"JSONL 原生 JSON 内容被字符串化"；fork 用的是 atri_x/logging/ 文本行 + `body_json` 文本字段，并在 raw 视图里专门缩进展开（`atri_x/agent/commands/log.py` 的 `formatDebugLogEntry`），因此这个缺陷**可能不存在**。先复现再决定。→ 台账 §9.7 |
| `shell` 先缓存全部输出、结束后才截断导致内存随输出量增长 | **已跟** | `atri_x/tool/builtin/shell.py:355` `keep := min(len(p), maxShellOutput-len(b.data))`，收集期就按固定容量截断。 |
| 修复高风险确认的 bug | **未跟** | 上游 `93c98d40`（2026-10-06，**不在 fork 里**）把确认等待改成 `turns.BeginRiskConfirmation(...)` + `wait.Wait(ctx, timeout)`；fork 仍是旧 API `AwaitRiskConfirmationContext`（`atri_x/agent/toolrun_adapter.py:112`），且 `atri_x/turn` 未按上游重写。 |
| 修复追加确认等待的 bug | **未跟** | 同上一个提交：上游同时引入了 `atri_x/turn` 的 risk wait / append wait 状态机与 `internal/agent/execution_lifecycle.go`；fork 只有 `AwaitAppendExpiration`（`atri_x/agent/input.py:209`）这套旧实现。 |
| OneBot：llm 能看到引用回复的合并消息，只展开一层 | **已跟** | `CHANGELOG.md:35`（引用回复里的合并转发会展开一层内容并保留"转发内容为引用"标记）。 |
| OneBot 长文本分页只有非末页添加省略号 | **已跟** | 同条目：fork 已改为合并转发，`CHANGELOG.md:35` 记"分页不再附加 `……（N/M）` 标记"。 |
| Agent 去除唤醒词或工具指令时合并全部文字段、打乱图文位置 | **已跟** | `CHANGELOG.md:76`（只改写实际变化的文字区间，段间图片与文件段保持原位）。 |
| 后台任务工具缓存 bug | **已跟** | `CHANGELOG.md:69`（重建 `toolrun.CachedTool` 时带上 `ForegroundOnly`）。 |
| 日志无意义的脱敏操作（大量正则、浪费性能） | **相反** | fork **保留并加强了**脱敏：`atri_x/redact`（fork 自加，`7d606898`）+ `CHANGELOG.md:242`/`:281`/`:296` 等条目。两边不是同一份实现，取向相反，属有意差异。 |
| chat 模式不再能传入工具 | **有意相反** | 上游（`internal/toolrun/runner.go`）让 chat 会话永不跑工具；本仓**放开 chat 模式的工具调用**（产品决策：群聊默认可能跑在 chat 槽位，但仍需要搜索等工具）。判断收敛到 `atri_x/storage/models.py` 的 `session_mode_supports_tools`，用于 `atri_x/tool/provider.py`、`atri_x/agent/tool_schemas.py`、`atri_x/toolrun/toolrun.py`、`atri_x/toolrun/runner.py`、`atri_x/agent/tool_directive*.py`。见 `CHANGELOG.md` 的 0.9.14 节。 |
| 普通用户能 stop 其他用户的请求 | **已跟** | `CHANGELOG.md:75`（可停止集合、编号解析与补全都限定在自己当前 Session）。 |
| 压缩期间能预载工具 | **已跟** | `CHANGELOG.md:78`（压缩准入移到输入 Hook 与指令处理之前）。 |
| 启动中途失败时统一取消并清理 Hook／信号和延迟 Skill 加载；平台退出等待纳入共享 30 秒预算 | **未跟** | fork 只有 `atri_x/app/runner.py` 的 `cleanups` 逆序关闭（`defaultShutdownTimeout = 30s`），没有"启动中途失败取消已创建 Hook／信号／延迟 Skill 加载"这一步，也没有把平台退出等待并入同一预算。 |
| 统一发送链路同时保留成功结果与错误；Telegram 已发出部分分页时不再从头降级重发 | **部分** | `CHANGELOG.md:80` 记了"长回复分包与部分发送追踪覆盖主要平台"（OneBot 部分回执、QQ 官方按 rune 分页、Telegram 不再回退纯文本造成重复发送）。**仍需逐平台确认 Telegram 的"已发出部分分页不从头重发"是否完全对齐**。 |
| 启动插件告警不再等待 Agent 创建后补发 | **未跟** | `atri_x/app/runtime.py:215-218` 收集 `startupHookNotices`，到 `:260` 才在 Agent 创建后逐条 `notifyHookIssue` —— 正是上游改掉的那条路径。 |
| 工具发现、预载现在是事务写入 session | **已跟** | 台账 P1#3。 |
| 模型切换先原子保存 `state.toml` 再生效；命名/work fallback 每次从共享模型服务取快照 | **已跟** | 台账 P1#2 / `7e5ab0e`；`atri_x/agent/model.py` 的 `mergeExternalRuntimeState` + `saveRuntimeState`。 |
| Session 绑定由 Session 服务统一同步失效 | **未跟（有意）** | fork 不引入上游的 `session.Binding` / `executionCoordinator`（台账 §1「不引入」）。 |
| Session 整行快照写回改为事务内字段更新 | **已跟** | 台账 P1#5；`CHANGELOG.md:63`。 |
| 压缩交接先结束旧 Turn 再激活新会话 | **未跟** | fork 用 `contextCompactState.Pending` 承载压缩交接（`atri_x/agent/session_metadata.py`），没有"先结束旧 Turn 再激活新会话 + 校验取消与执行身份"的交接协议。 |
| 平台连接后的 Hook／Cron 处理按平台独立排队，退出时取消积压与在途处理并共享 30 秒预算 | **部分** | 与 Changed 表第 6 条同一件事：hook 侧已完成（`0247c90f`），cron 侧未独立。 |
| `/resume` 与 `/fork` 返回的历史消息预览每条最多 200 个 Unicode 字符 | **已跟** | `CHANGELOG.md:95`。 |
| 文档型 AgentSkill 的 `agent_skill_creator` 引导仅超管可见 | **已跟** | `CHANGELOG.md:74`。 |

## 4. 结论：未跟项怎么处理

### 4.1 值得做（有明确收益，且不依赖上游骨架）

| 优先级 | 条目 | 落点 | 备注 |
| --- | --- | --- | --- |
| 高 | ~~日志查询 `--raw` 解耦、移除 `/log -t`、损坏行计数~~ **已完成** | `atri_x/agent/commands/log.py`、`atri_x/logging/reader.py` | `803de2ab` / `04265c28` → 台账 §9.1 |
| 高 | ~~工具日志正文按类别限长~~ **已完成** | 工具日志写入侧 | `d85cad7` → 台账 §9.1 |
| 中 | ~~`shell` stdout/stderr 上限 16 KiB → 256 KiB~~ **已完成** | `atri_x/tool/builtin/shell.py:24` | 一行常量 + CHANGELOG；"边收边限容"本来就在 |
| 中 | ~~`/usage` 未知/非法/溢出显示 `null`~~ **已完成** | `atri_x/agent/commands/usage.py` | `25cf1bad` |
| 中 | `/audit --run_source`（写入侧 `run_source` 字段 + 查询选项） | `atri_x/agent/chat_tools.py`、`atri_x/agent/commands/log.py` | **已完成 `74b3275`** |
| 中 | `internal/fileops` rollback 的上游增量（会话绑定有效期、编码保持） | `atri_x/utils/fileops/rollback.py` | 整文件不搬（Binding 结构差异、编码保持本来就有）；核出的真缺陷 E 已修 `9ae9c404`，F/G/B/A/H 仍在 §10.6 待定 |
| 低 | ~~启动插件告警不再等 Agent 创建~~ **已完成** | `atri_x/app/runtime.py`、`atri_x/app/platforms.py` | `bbc3a185`：告警改到 `Integrations.Attach` 之后由 `deliverHookNotices` 投递 |
| 待定 | 高风险确认 / 追加确认的两个 bug、压缩交接先结束旧 Turn | `atri_x/agent`、`atri_x/turn` | 这三条都是上游"core refactor"的一部分（自建 `atri_x/turn` 状态机 + `execution_lifecycle.go`）。要吃它们就得先吃那套 `atri_x/turn` 重写，成本远高于前几条 —— **建议先单独复现"确认等待"和"压缩交接"是否真的在 fork 上出问题**，只在能复现时才考虑动 |

### 4.2 不适用 / 有意差异（不要再当"漏跟"处理）

- `获取模型信息超时 5s→30s`：上游 `internal/modelmgr` 的事，fork 没有这个包。
- `Responses additional_tools`、`Session.Binding`、`九字段日志契约`、`按 API 类型限制工具`、`压缩按协议分派的服务端实现`：都是 fork 明确选定不做/只落接缝的差异，见台账 §5 与 §8「明确不搬」。
- `日志脱敏`：**取向相反**。上游删掉了自己的脱敏正则（理由：本来就没记敏感信息）；fork 反而有 `atri_x/redact` 并在多处使用。这是 fork 的安全取舍，保留。

### 4.3 结论

- 上游这段 `Unreleased` 里，**真正算"fork 漏了、且值得补"的只有 6～7 条**，且集中在日志查询/写入侧（§4.1 的前两条）与几个一行常量级的改动上。
- 剩下的未跟项要么依赖上游那套 `atri_x/signal` + `internal/events` + `*/module` + `atri_x/turn` 骨架（结构级，台账已判"不搬"），要么是 fork 的有意差异。
- 这反过来支持既有策略：**对齐行为、不对齐实现**。

## 5. 复核方法（可重跑）

```bash
# 1. 取上游 Unreleased 段（上游最后发版 v0.5.0 与本 fork 共同祖先是同一天）
git show refs/remotes/upstream/main:CHANGELOG.md | sed -n '/^## Unreleased/,/^## \[v0\.5\.0/p'

# 2. 在 fork 的 CHANGELOG 里找对应条目
rg -n "<关键词>" CHANGELOG.md

# 3. 去代码核实（本文所有 file:line 都可用这条复核）
rg -n "<符号或文案>" internal
```

**注意**：每次盘点前先 `git fetch https://github.com/Elflare/elbot.git main:refs/remotes/upstream/main`
（本仓库没有配置 `upstream` remote，该 ref 会被 `git fetch --prune` 清掉）。
