# 命令速查

ATRI-X 的 slash 命令由 Agent Core 统一处理，CLI、QQ、后续平台共享同一套语义。默认命令前缀是 `/`，可在 `app.toml` 的 `[commands]` 中配置。

> 说明：当前默认命令前缀是 `/`（历史版本默认是“斜杠 + 星号”，那个星号已经去掉）。本文示例已按 `/` 书写。

## 帮助

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看可用命令列表。 |
| `/help <command>` | 查看某个命令的详细帮助。 |

示例：

```text
/help
/help model
/help log
```

## 模型

| 命令 | 作用 |
| --- | --- |
| `/models` | 查看模型列表。 |
| `/models --fresh` 或 `/models --refresh` | 强制刷新模型列表缓存。 |
| `/model <编号或名称>` | 切换当前 Session 模式使用的模型。 |
| `/model --chat <模型>` | 切换 chat 模式模型。 |
| `/model --work <模型>` | 切换 work 模式模型。 |
| `/model --atriwisp1 <模型>` | 切换 Atrinis atriwisp1 模型槽位。 |
| `/model --atriwisp2 <模型>` | 切换 Atrinis atriwisp2 模型槽位。 |
| `/model --atriwisp3 <模型>` | 切换 Atrinis atriwisp3 模型槽位。 |
| `/model --compact <模型>` | 切换上下文压缩模型。 |
| `/model --naming <模型>` | 切换 Session 自动命名模型。 |
| `/model --profiles` | 列出 `services.toml` 里的命名模型选择（`model_profiles` / `model_aliases`），并标注当前进程是否可用。 |
| `/model --snapshots` | 列出已保存的命名模型快照与每个槽位。 |
| `/model --save <名字>` | 把当前的整套模型选择（各模式 + 压缩 + 命名）存成命名快照，写在 `state.toml` 的 `[model_snapshots]`。 |
| `/model --apply <名字>` | 按快照整套切回模型。 |
| `/model --delete <名字>` | 删除命名快照。 |
| `/checkmodel [关键词]` | 查看或搜索模型。 |

模型参数可以是列表编号、模型名或 `provider/model`。

切换**当前会话正在用的**模式时会先做协议/厂商兼容性裁决：会话历史由 Responses 产生时只允许切到同厂商的
Responses 模型，由 Chat Completions 产生时允许跨厂商切换。不兼容时命令直接失败且不改动任何选择，按提示
新建会话或切回兼容模型即可；切换其他模式槽位（当前会话用不到，例如在 work 会话里 `/model --chat`）不做检查。

`/model --profiles` 列出的名字可以直接用于 `@model:<名字>` 和群策略的 `default-model`；
标注 `unavailable` 的名字表示它指向的 provider 在当前进程里没有可用客户端，配置存在但用不了。

`/model --save` / `--apply` 操作的是**快照**，它记录的是"按模式/用途的一组模型"，和只固定一个模型的
`model_profiles` 不是一回事，因此两者同名会被拒绝；在快照名后面可以直接用补全（`--apply` / `--delete`），
不需要先去 `--snapshots` 里抄名字。快照的详细约束见 `configuration.md` 的"命名模型快照"。

示例：

```text
/models
/models --fresh
/models --refresh
/model 2
/model --profiles
/model --snapshots
/model --save cheap
/model --apply cheap
/model --delete cheap
/model --work deepseek/deepseek-chat
/model --chat openai/gpt-4o-mini
/model --atriwisp2 openai/gpt-4.1
/checkmodel deepseek
```

## 模式

| 命令 | 作用 |
| --- | --- |
| `/chat [消息]` | 切换到 chat 模式，或创建新的 chat Session；提供消息时会切换后立即发送。 |
| `/work [消息]` | 切换到 work 模式，或创建新的 work Session；提供消息时会切换后立即发送。 |

说明：

- 两个模式**都支持工具调用**：都注入工具 schema、跑工具轮、允许 `@tool:` / `@skill:` 预载；
  区别只在 `state.toml` 的 `mode_models.chat` / `mode_models.work` 用哪个模型，以及
  `[context]` 的按模式溢出策略。这是本仓相对上游 Go 版的**有意差异**（上游 chat 不注入工具）。
- 要禁止某个群用工具，用群策略 `/tools allow none`（桌面端「工具权限」页也有同样开关），
  与 `chat` / `work` 模式无关。
- 已有历史的 work Session 不能直接切到 chat；需要先 `/new`，再 `/chat`。
- 例如 `/chat 随便聊聊` 会先切换到 chat 模式，再把“随便聊聊”作为同一条用户消息发给模型；若模式切换被拒绝，消息不会发送。

## Session

| 命令 | 作用 |
| --- | --- |
| `/new` | 创建并切换到新 Session。 |
| `/status` | 查看当前 Session 状态。 |
| `/sessions [关键词]` | 列出或搜索可见 Session。 |
| `/resume [最近编号或session_id]` | 恢复历史 Session；编号 `1` 表示最近更新的非当前 Session；恢复后台（cron / Atrinis）Session 会中断该 Session 在途的后台任务（见下）。 |
| `/archives [页码] [关键词]` | 查看已归档 Session。 |
| `/archive [编号或session_id] --confirm` | 归档 Session，默认当前 Session。 |
| `/unarchive [编号或session_id]` | 取消归档 Session，默认当前 Session。 |
| `/pin [编号或session_id]` | 置顶 Session，默认当前 Session。 |
| `/unpin [编号或session_id]` | 取消置顶 Session，默认当前 Session。 |
| `/rename [编号或session_id|当前标题] <新标题>` | 重命名当前或指定 Session。 |
| `/delete <编号或session_id> --confirm` | 永久删除 Session。 |
| `/clean --confirm` | 删除过期且未归档、未置顶的 Session。 |

示例：

```text
/new
/status
/sessions
/sessions project
/resume 1
/rename 我的新会话标题
/archive --confirm
/delete 2 --confirm
```

说明：

- `/resume 1` 可以直接恢复最近更新的非当前 Session，无需先执行裸 `/resume`；编号按更新时间排列，不受置顶影响，翻页时编号连续。
- 恢复一个正在跑的后台 Session（cron / Atrinis）会**中断该 Session 在途的后台任务**：它当场被提升为前台会话，后台不再发起后续模型调用，也不再写入后台回答或发送本轮汇报；已经执行过的工具调用保留在会话历史里。`/unarchive` 把一个后台 Session 取消归档同样会中断它。
- 当前会话处理中时，不支持执行 `/new`、`/resume`、`/fork`、`/chat`、`/work` 等 Session 切换命令；如有必要，请先使用 `/stop` 结束当前处理。
- 各群可用 `/grouppolicy thread-mode group` 开启共享会话；此时 Session 命令面向全群共享当前会话，上面列出的切换/修改命令仅群主、群管理员或超管可用；开启后不迁移旧 per-user 会话。
- `/sessions` 展示的编号可被 `/archive`、`/pin`、`/delete` 等 Session 操作命令复用。
- CLI 作为本地高权限入口，可以跨平台查看 Session；非 CLI 平台默认只查看当前平台和作用域下的 Session。
- 删除是永久操作，需要显式 `--confirm`。

## Fork

| 命令 | 作用 |
| --- | --- |
| `/messages [页码]` | 列出当前 Session 中可用于 fork 的 assistant message ID。 |
| `/fork <message_id>` | 从指定 assistant 消息创建分支 Session。 |

示例：

```text
/messages
/fork msg_xxx
```

Fork 会保留原会话，并从指定 assistant 消息位置创建新的上下文分支。

## 请求管理

| 命令 | 作用 |
| --- | --- |
| `/requests` | 查看当前进程中的 active request，包括 turn、LLM、tool、hook 等请求；turn 会显示运行阶段和阶段耗时。 |
| `/me [tasks\|quota\|all]` | 普通成员查看自己的进行中任务、排队消息和今日生图/视觉/语音转写/聊天额度；只读，不调用模型。 |
| `/stop [request_id]` | 停止指定请求；也可使用 `/requests` 显示的编号；不传参数时停止当前 Session 的请求。普通用户只能停止自己当前 Session 的请求，编号与补全同样只列出这些请求；只有超级管理员可以按 ID/编号停止任意会话的请求。停止一次性 exec Hook 时会结束该 Hook 的完整进程树；持久 Worker 只取消当前调用。 |
| `/stopall` | 停止当前进程中的所有 active request。 |

示例：

```text
/requests
/stop
/stop 1.1
/stop req_xxx
/stopall
```

`/me` 只显示当前 actor 在当前平台/作用域内自己的任务和额度，按 FairKey 过滤；不会暴露其他成员的活动。`/requests` 仍保留全局/管理视角（仅超级管理员可用），`/stopall` 同样仅超级管理员可用。

## 配置检查

| 命令 | 作用 |
| --- | --- |
| `/doctor` | 只读检查当前配置：加载错误/缺失项、主配置未知字段、内置 Skill 文件与默认版本不一致等，逐条列出；没有问题回复 `Everything is OK`。仅超级管理员可用，不会修改任何文件、不调用模型。 |

## 文件回滚
`edit_file` 每次成功写入前会在内存里保留该文件上一版内容（每个文件只保留最近一次），仅超级管理员可用 `/rollback` 与内置工具 `rollback_file` 撤销。

| 命令 | 作用 |
| --- | --- |
| `/rollback` | 列出当前 Session 可回滚的文件编辑：编号、路径、编辑/新建、时间与字节数。 |
| `/rollback <编号>` | 把该文件恢复成编辑前内容；如果这次编辑新建了文件，则删除该文件（前提是它仍保持这次编辑写入的内容）。 |

说明：

- 记录只存在于当前进程、按 Session 隔离：切换 Session 后不再列出，重启进程或超过容量上限（1024 条 / 256 MiB，超出按最旧淘汰）后失效。
- 编辑之后文件又被 Shell、外部编辑器或其它程序改过时，回滚会拒绝执行并提示「未回滚」，不会覆盖别人的修改；这次编辑**新建**的文件同样如此——它只有在内容仍等于这次编辑写入的内容时才会被删除，被改过就拒绝删除。
- `rollback_file` 工具需要切到 `work` 模式，普通用户不可见；发现 `read_file` / `edit_file` 时会自动展开，也可用 `@tool:rollback_file` 预载。

## 上下文压缩

| 命令 | 作用 |
| --- | --- |
| `/compact` | 手动压缩当前 Session 上下文。 |

说明：

- 自动压缩由 `[context] compact_enabled` 和 `compact_trigger_ratio` 控制。
- 压缩会保留历史用户原话、忽略工具返回值，成功后创建并切换到独立的 `原标题 compacted-N` Session；旧 Session 不修改。
- 压缩时每条历史用户原话最多保留 `[context] user_original_max_runes` 个字符，避免超长消息撑爆摘要。

## 长消息保护

发送前会估算 prompt token；如果总输入超过模型窗口预算，或单条用户消息超过单条上限，会触发长消息保护。

| 命令 | 作用 |
| --- | --- |
| `/overflow` | 查看当前群的 chat/work 长消息策略。 |
| `/overflow --chat <策略>` | 设置 chat 模式策略。 |
| `/overflow --work <策略>` | 设置 work 模式策略。 |
| `/overflow --all <策略>` | 同时设置 chat/work。 |
| `/overflow reset [--chat|--work|--all]` | 恢复对应模式的全局默认。 |

策略：

- `reject`：默认。不调用模型，在群里返回报警，原文不写入 Session。
- `truncate`：保留能放下的一段，追加截断标记后继续。
- `summarize`：使用 compact 模型（未配置时回退当前模式模型）摘要后继续。

权限：Bot 超级管理员和当前群群主/管理员可用。策略覆盖持久化在 `state.toml` 的 `context_overflow` 表。

## 群级策略

| 命令 | 作用 |
| --- | --- |
| `/grouppolicy` | 查看当前群的唤醒、响应、会话线程、连续消息合并、语音转写、工具与额度策略。 |
| `/grouppolicy wake <词1,词2>` | 增加本群唤醒词；只影响当前群。 |
| `/grouppolicy response <mention\|all\|keyword\|reply\|off>` | 设置普通群消息的响应模式。 |
| `/grouppolicy thread-mode <per-user\|group>` | 设置本群会话线程模式：`per-user` 每人独立，`group` 全群共享并按 turn 串行。共享会话的切换/修改命令仅群主/群管理员/超管可用。 |
| `/grouppolicy merge-window <0-10000>` | 设置连续消息合并窗口（毫秒）；窗口内同一成员的连续消息合并为一轮。 |
| `/grouppolicy default-mode <work\|chat\|inherit>` | 设置新 Session 的默认模式。 |
| `/grouppolicy default-model <别名\|provider/model\|clear>` | 设置本群默认模型；别名或 `provider/model` 解析后会再次校验本群模型目录。 |
| `/grouppolicy allowed-models <别名或provider/model\|*>` | 超级管理员设置本群可选模型目录；空目录只允许已配置的模型别名/profile。 |
| `/grouppolicy tool-allow <工具名,工具名\|none\|clear>` | 设置工具白名单；`none` 表示禁止全部工具，`clear` 表示继承全局。群主/管理员也可以用更短的 `/tools allow`（见下文）。 |
| `/grouppolicy image-quota <次数>` / `/grouppolicy vision-quota <次数>` / `/grouppolicy asr-quota <次数>` | 设置本群每日生图/视觉/语音转写调用额度；执行前会原子预占，重启不丢账。 |
| `/grouppolicy user-image-quota <次数>` / `/grouppolicy user-vision-quota <次数>` / `/grouppolicy user-asr-quota <次数>` | 设置本群内单用户每日生图/视觉/语音转写调用额度。 |
| `/grouppolicy chat-tokens-quota <token 数>` | 设置本群每日聊天 token 额度；provider 返回 usage 后记账。 |
| `/grouppolicy chat-cost-quota <金额>` | 设置本群每日聊天费用额度，金额使用 `[maintenance.daily_report].currency` 对应的价格表计算。 |
| `/grouppolicy quiet <HH:MM-HH:MM\|clear>` | 设置静默时段；命令与超级管理员不受影响。 |
| `/grouppolicy analysis/learning/history <on\|off>` | 启停本群群分析、学习观察和历史记录。`history=off` 只停止新写入，不删除旧记录；`learning=off` 停止采集、挖掘、审核入库和上下文注入。 |
| `/grouppolicy asr <on\|off>` | 启停本群语音消息转写；全局 `[asr].enabled=false` 时全部关闭。 |
| `/grouppolicy knowledge <on\|off>` | 启停当前群的本地确定性知识库回答；关闭后普通消息不再命中知识库，但管理命令仍可用（全局 `[group_knowledge].enabled=false` 时全部关闭）。 |
| `/grouppolicy services <on\|off>` | 启停当前群的提醒 / 投票 / 报名；全局 `[group_services].enabled=false` 时全部关闭。 |
| `/grouppolicy learning-moderation <on\|off>` | 超级管理员显式授权本群群主/管理员审核本群 learning 候选。 |
| `/grouppolicy learning-moderation-actions <view,decide,mine>` | 超级管理员细分授权；默认 `view,decide`，每次命令都会重新校验。 |
| `/grouppolicy reset [field]` | 重置当前群策略。 |

权限与边界：

- 群管理员只能修改自己当前群的普通策略；命令不接受“目标群”参数，不能跨群修改。
- provider、API Key、全局 Shell 权限不通过群策略暴露，仍由服务端全局配置和 `security` 判断。
- `learning-moderation` 只能由机器人超级管理员设置；群管理员默认没有 `/learning` 管理权限，被显式授权后也只能审核本群候选，不能获得系统级超管权限。

## 群知识库（FAQ）

群知识库按 `平台 + 群 scope` 保存，命中后由服务端直接发送答案，不创建 Session、不调用 LLM，也不消耗 chat token。它适合固定口径的群规、绑定方式、客服问答等确定性内容。

| 命令 | 作用 |
| --- | --- |
| `/faq` / `/faq list [关键词]` | 列出或搜索当前群知识库。 |
| `/faq add <问题> => <答案>` | 添加精确匹配条目；左侧可用 `\|` 分隔别名。 |
| `/faq add-contains <文本> => <答案>` | 添加包含匹配条目。 |
| `/faq add-keyword <词1,词2> => <答案>` | 添加关键词条目；所有关键词都出现才命中。 |
| `/faq remove <id>` | 删除指定条目。 |
| `/faq clear --confirm` | 清空当前群知识库。 |
| `/faq test <文本>` | 只测试匹配结果，不发送答案。 |
| `/faq on` / `/faq off` | 开关当前群的知识库回答。 |

说明：

- 匹配前会做确定性归一化：全角转半角、大小写折叠、空白折叠、去掉常用句末标点；`exact` 需要整句一致，`contains` 只要求包含，`keywords` 要求所有关键词都出现。
- 命中后仍受正常唤醒、静默时段、限流和群运行状态约束；不是“无条件自动回复”。
- 知识条目和答案不会注入模型提示词；关闭开关后普通消息会继续走正常 LLM 流程。
- 管理命令仅当前群群主、群管理员或机器人超级管理员可用；不能跨群操作。

## 群内提醒 / 投票 / 报名

这些服务按 `平台 + 群 scope` 保存在 `state.toml [group_services]`，由本地调度和确定性逻辑执行，不调用模型。普通成员可创建/参与；创建者、当前群群主/管理员或超级管理员可关闭/删除。

### 提醒

| 命令 | 作用 |
| --- | --- |
| `/remind <时间> <内容>` | 创建群提醒。 |
| `/remind list` | 查看当前群待发送提醒。 |
| `/remind remove <id>` | 删除提醒。 |

时间格式：`10m`、`1h30m`、`2d`、`2d3h`、`15:04`、`15:04:05`、`YYYY-MM-DD HH:MM`、`MM-DD HH:MM`。提醒到点后由调度器发送到创建时的群；群被禁言/删除等不可用状态会延后重试，已移除/不可用状态会标记跳过。

### 投票

| 命令 | 作用 |
| --- | --- |
| `/poll <问题> \| <选项1> \| <选项2>` | 创建投票。 |
| `/poll list` | 查看当前群进行中的投票。 |
| `/poll show <id>` | 查看投票结果和自己的选择。 |
| `/vote <id> <选项序号>` | 投票；可改票。 |
| `/poll close <id>` | 关闭投票并展示结果。 |

### 报名

| 命令 | 作用 |
| --- | --- |
| `/signup <标题> [人数]` | 创建报名；人数省略或 0 表示不限。 |
| `/signup list` | 查看进行中的报名。 |
| `/signup show <id>` | 查看报名成员。 |
| `/join <id>` / `/signup join <id>` | 加入报名。 |
| `/signup leave <id>` | 退出报名。 |
| `/signup close <id>` | 关闭报名。 |

示例：

```text
/remind 10m 开会
/poll 周末团建去哪 | 爬山 | 桌游 | 聚餐
/vote p1 2
/signup 团建 20
/join s1
```

## 工具与 Skill

| 命令 | 作用 |
| --- | --- |
| `/tools` / `/tools list` | 列出已注册工具和外置 Skill，并附带**当前群的工具授权状态**（普通成员也能用）。 |
| `/tools perm` | 只看当前群的工具授权状态（继承全局 / 仅白名单 / 禁止全部）。 |
| `/tools allow <工具名,工具名\|all\|none>` | 群主 / 群管理员 / 超级管理员授权本群工具：列出白名单、`all` 继承全局、`none` 禁止全部。白名单会自动带上 `discover_tool`。 |
| `/tools reload` | 重新扫描并加载 Skill（超级管理员）。 |
| `/tools remove <name> --confirm` | 删除外置 Skill 及其目录（超级管理员）。 |
| `/tools uninstall <name> --confirm` | 等同于 remove。 |

示例：

```text
/tools
/tools perm
/tools allow web_search,web_extract
/tools allow all
/tools allow none
/tools reload
/tools remove my_skill --confirm
```

权限：列出工具与查看授权状态对所有用户开放（否则群成员连"为什么某工具用不了"都看不到）；
`allow` 需要当前群群主 / 群管理员 / 机器人超级管理员；`reload` / `remove` 仍然只允许超级管理员。

`/tools allow` 与 `/grouppolicy tool-allow` 写的是同一份群策略（`state.toml` 的
`[group_policy."<平台:scope>"]`），命令写入的是**通用白名单**，作为 chat / work 两个模式的
兜底；桌面端「工具权限」页的 chat / work 两个子选项卡可以分别给两个模式配更细的白名单
（`tool_allowlist_chat` / `tool_allowlist_work`），配置了就用分模式那份，没配置才回退到通用。
后台服务 15 秒内热加载，不需要重启。白名单里没写 `discover_tool` 时命令会自动补上——
缺了它，模型连"本群还有哪些工具可用"都发现不了。
桌面端还有「工具权限」选项卡：按群在 chat / work 两个子选项卡里逐个「激活」工具（每行带中文名、功能解释与激活状态，另有「常用」「只读」预设）。

`/tools reload` 会先完整扫描并验证候选 Skill，再一次性替换当前集合。若存在重名 Skill、与内置工具重名或读取失败，reload 会返回错误并保留原有工具集合。

LLM 在 work / chat 模式下都可以通过 `discover_tool` 按需发现工具详情。聊天中也可以用 `@tool:<name-or-tag>`（简写 `@t:<name-or-tag>`）预载工具，或用 `@skill:<name>`（简写 `@s:<name>`）把 Skill 文档加入本轮消息并预载对应运行 wrapper。冒号也可以写成中文全角冒号 `：`。

`@model:` / `@image:` / `@use:`（也支持 `#模型:`、`#生图:`、`#工具:` 和中文别名）是"单轮声明"：只对当前这一轮生效，下条消息要重新声明，且仅超级管理员可用。触发符号、关键字和别名都可配置，详见 [配置说明](configuration.md#命名-profile-与单轮声明)。

`@tool:` / `@skill:` 预载在 work / chat 模式都生效（本仓 chat 也支持工具）。后台沙盒会话不使用工具，发送时会被剥离并提示。

`discover_tool` 的 `name` / `names` 除了工具名和 Skill 名，也可以传 tag（例如 `chat`、`web`），会展开成该 tag 下的全部工具；`discover_tool` 列表中的 `tags` 字段可以用来发现可用的 tag。

## 运行时状态文件

| 命令 | 作用 |
| --- | --- |
| `/state` | 查看 `state.toml` 路径、已加载时间和是否有未生效的外部修改。 |
| `/state reload` | 立即重新读取 `state.toml` 并应用。 |

示例：

```text
/state
/state reload
```

`state.toml` 由 ATRI-X 运行时回写，也支持手工编辑后热加载：进程每 15 秒检测一次外部修改并自动生效，`/state reload` 用于立刻生效。热加载覆盖模型选择、长消息策略、群策略、群知识库、群服务和群运行状态；`[budget]` 额度账本由进程独占，只在启动时从文件恢复，详见 [配置说明](configuration.md#外部修改-statetoml-的热加载)。

## 角色素材库

| 命令 | 作用 |
| --- | --- |
| `/chars` | 列出当前可见角色。 |
| `/chars <关键词>` | 按 id / 名称 / 别名 / tags / 简介过滤。 |
| `/chars reload` | 超级管理员重建角色索引。 |

在消息里写 `@char:<id>`（简写 `@c:<id>`，支持全角冒号 `：`）可以把该角色的设定临时注入当轮，仅影响这一轮：

```text
@char:catgirl 你好呀
```

角色不存在或无权访问会提示“未找到或不可用的角色”。完整说明见 [角色素材库](character-library.md)。

## Hook


| 命令 | 作用 |
| --- | --- |
| `/hooks` | 列出所有已注册 Hook。 |
| `/hooks <name>` | 查看某个 Hook 的详细配置。 |
| `/hooks start <id>` | 启动一个持久 Hook。 |
| `/hooks stop <id>` | 优雅停止一个 Hook。 |
| `/hooks restart <id>` | 停止并重新启动一个持久 Hook。 |
| `/hooks reload` | 重读规则和持久 Hook 配置，并重新协调进程生命周期。 |

示例：

```text
/hooks
/hooks greet
/hooks restart weather
/hooks reload
```

说明：

- 规则 Hook 直接使用配置里的 `name`；内置 Hook 使用 `builtin.*` 名称，例如 `builtin.cron.missed_once`。
- `Description` 会显示在列表和详情里；规则细节只在详情里显示。
- `reload` 会重新读取 `hooks.toml` 和各插件 `hook.toml`，并重建 Hook 注册、替换受影响的持久进程。
- `/hooks` 为超级管理员命令。

## 长期记忆与自主学习

| 命令 | 作用 |
| --- | --- |
| `/memory status` | 查看当前平台/会话的长期记忆条数。 |
| `/memory list [n]` | 列出当前范围的长期记忆及其 ID、来源、强度和内容。 |
| `/memory recall [关键词]` | 按关键词检索当前范围的长期记忆，并显示记忆 ID 与来源。 |
| `/memory show <id>` | 查看一条记忆的完整来源和内容。 |
| `/memory delete <id>` | 删除一条当前 scope 的长期记忆。 |
| `/memory source <消息id>` | 删除由指定平台消息派生的当前 scope 长期记忆。 |
| `/memory backfill` | 预检旧记忆来源回填：统计全库来源覆盖和可确定性回填的条数，不修改数据。 |
| `/memory backfill --confirm` | 为旧记忆回填可确定的 `source_kind`。 |
| `/forget list [n]` | 列出当前用户可删除的长期记忆。 |
| `/forget <id>` | 删除当前用户可删除的一条长期记忆；id 支持唯一前缀。 |
| `/forget source <消息id>` | 删除由指定消息派生的、当前用户可删除的长期记忆。 |
| `/forget resident normal\|core\|all [--confirm]` | 清空自己的普通 / 核心 / 全部常驻记忆。 |
| `/learning status` | 查看待审和已批准的表达/黑话候选数量。 |
| `/learning mine` | 从最近消息中挖掘待审候选。 |
| `/learning review [pending\|approved\|rejected]` | 列出候选。 |
| `/learning approve <id> [含义]` | 批准候选；批准后才会注入上下文。 |
| `/learning reject <id>` | 拒绝候选。 |

说明：

- `/memory` 对应 `angel_memory` SQLite，仅超级管理员可用；`/memory delete` 和 `/memory source` 只作用于当前平台/会话 scope。
- `/forget` 面向普通成员：私聊 scope 可删除该 scope 内的长期记忆；群聊中普通成员只能删除来源成员为自己的条目，群主、群管理员和超级管理员可以删除当前群 scope 的任意条目。
- 长期记忆在写入时记录来源用户、平台消息 ID 和 Session ID；源消息撤回后默认会删除由该消息派生的长期记忆，可通过 `[angel_memory].forget_on_recall` 关闭。`/delete` 永久删除 Session 时也会删除来源 Session ID 匹配的长期记忆；旧版本未记录结构化来源的记忆不会被匹配，避免误删。
- `/memory backfill` 是给旧数据的一次性迁移入口：旧版只把写入方写成自由文本 `source = "tool"`，所以可以确定地回填 `source_kind`；旧数据里没有来源成员、消息 ID 和 Session ID，`/memory backfill` 不会猜测它们，这些旧记忆仍然不会被 `/forget source`、撤回清理和 `/delete` Session 命中。
- `/forget resident core` 和 `/forget resident all` 需要 `--confirm`；`/forget resident normal` 不需要确认。
- `/learning` 对应 `self_learning` SQLite；默认仅超级管理员可用。超级管理员可在本群执行 `/grouppolicy learning-moderation on` 并通过 `learning-moderation-actions` 细分授权（`view` / `decide` / `mine` / …）。群管理员审核结果只作用于当前群 scope，不会进入其他群或全局知识域；撤权或群管理员身份变化后，下一次命令立即失效，不依赖旧缓存。
- 只有 `approved` 的表达/黑话会通过 `llm.turn.prepared` 注入当前请求。

## 日志和审计

| 命令 | 作用 |
| --- | --- |
| `/log [options]` | 查询运行日志。 |
| `/audit [options]` | 查询审计日志。 |
| `/atriwisp [name] [options]` | 查询 Atrinis/Atriwisp 事件日志。 |

常用选项：

| 选项 | 作用 |
| --- | --- |
| `-n, --limit <n>` | 返回条数，默认 5。 |
| `--days <n>` | 读取最近 n 天日志，默认 1。 |
| `--level <level>` | 最低等级：`debug`、`info`、`warn`、`error`。 |
| `-d, -i, -w, -e` | 最低等级快捷方式，**不改变展示方式**。 |
| `--raw` | 显示文件里的完整原始记录，与等级筛选互相独立。 |
| `--since <time>` | 只看某时间之后，例如 `2h`、`30m`、`2026-06-03`。 |
| `--until <time>` | 只看某时间之前。 |
| `--msg <text>` | 按 msg 字段过滤。 |
| `--contains <text>` | 按文本、参数、结果或 raw 内容过滤。 |

`--level debug` 与 `-d` 只降低门槛，**不会**顺带显示原始记录；要看原始记录显式加 `--raw`
（例：`/log -d --raw`）。`--raw` 在 `/log`、`/audit`、`/atriwisp` 上都可用。

解析不出 `time`／`level`／`msg` 的行（被截断的半行、早期格式、非本系统的内容）会被跳过并单独计数，不会占用 `--limit` 的条数，也不会在结果里冒充一条记录。一旦本次扫描跳过了这类损坏行，输出末尾会提示「本次扫描跳过 N 条损坏记录，结果可能不完整」；加 `--raw` 时另外列出最多 10 条原始样本与被省略的条数。`/usage` 同样会提示用量统计可能不完整。

`/log` 额外支持：

| 选项 | 作用 |
| --- | --- |
| `-u`、`-a` | 分别筛选用户、助手事件。 |
| `-s, --system` | 筛选并显示 `system prompt` 日志。 |
| `--hook` | 筛选 Hook 模块记录（`module=hook`）；可与 `--event` 等条件同时使用，不会互相覆盖。 |

工具调用**只记录在 audit**，因此 `/log` 不再接受 `-t`；`/audit -t` 仍然可用。

`/audit` 额外支持：

| 选项 | 作用 |
| --- | --- |
| `-t` | 筛选 `event=tool_call` 的工具审计。 |
| `--event <name>` | 按审计事件过滤，例如 `tool_call`、`llm_usage`、`permission_denied`。 |
| `--risk <level>` | 按风险等级过滤。 |
| `--actor <id>` | 按 actor ID 过滤。 |
| `--session <id>` | 按 Session ID 过滤。 |
| `--tool <name>` | 按工具名过滤。 |
| `--run_source <foreground\|cron\|atrinis>` | 按工具调用的来源过滤。来源在调用现场固定：前台会话发起的调用记 `foreground`，Cron／Atrinis 后台任务分别记 `cron`、`atrinis`；没有该字段的历史记录不会被补造，也查不到。 |

`/atriwisp` 查询 `atrinis-YYYY-MM-DD.log`，额外支持：

| 选项 | 作用 |
| --- | --- |
| `[name]` | 按 Atriwisp 名称过滤，等同于 `--name`。 |
| `--name <name>` 或 `--atriwisp <name>` | 按 Atriwisp 名称过滤。 |
| `--source <source>` | 按事件来源过滤。 |
| `--id <id>` 或 `--source-id <id>` | 按外部事件 ID 过滤。 |
| `--mode <record|direct|llm>` | 按事件模式过滤。 |
| `--event-key <key>` | 按 Atrinis event key 过滤。 |
| `--event-id <id>` | 按内部 Atrinis event ID 过滤。 |
| `--token <name>` | 按 token name 过滤，不包含 token 原文。 |
| `--tag <tag>` | 按 Atriwisp tag 过滤，可重复，要求全部匹配。 |

示例：

```text
/log
/log -w -n 10
/log --system
/log -d --raw
/log --msg startup --days 3
/audit --event tool_call --risk high -n 10
/audit -t --raw -n 20
/audit -t --run_source cron -n 20
/audit --actor cli:local --since 24h
/atriwisp
/atriwisp server-watchdog -n 20
/atriwisp --tag watchdog --raw
/atriwisp --source minecraft-main --mode llm --since 2h
```

## Token 消耗统计

| 命令 | 作用 |
| --- | --- |
| `/usage [options]` | 汇总审计日志中的 token 消耗数据。 |

选项：

| 选项 | 作用 |
| --- | --- |
| `-d, --days <n>` | 查看最近 n 天，默认 1。 |
| `-m, --model <name>` | 按模型名过滤。 |
| `-s, --session <id>` | 按 Session ID 过滤。 |
| `--by <key>` | 按维度汇总：`model`（默认）、`day`、`session`。 |
| `--since <time>` | 只看某时间之后，例如 `2h`、`30m`、`2026-06-03`。 |
| `--until <time>` | 只看某时间之前。 |

示例：

```text
/usage
/usage -d 7
/usage -m gpt-4o
/usage -s sess-xxx
/usage --by day -d 30
/usage --by trigger
/usage --since 2h
```

说明：

- `/usage` 从审计日志的 `llm_usage` 事件聚合 token 用量，按模型/天/会话/原因分组统计 prompt、completion、total、cache 和耗时。
- `--by trigger` 按调用原因分组：用户直聊（`direct_message`）、后台任务（`cron`）、记忆学习、重试，以及 5 条旁路调用（`side:compaction` 压缩摘要、`side:group_analysis` 群分析、`side:vision` 视觉理解、`side:title` 会话命名、`side:image_rewrite` 图片提示词改写）。旁路调用现在也计入用量。
- 字段缺失、非法或溢出的值显示 `null`，并向上传递到受影响的分组与总计项；显式的 `0` 照常统计，其他完全已知的分组和字段不受影响。用量未知的调用仍计入 `calls`。这样"没有记录"不会被当成"用了 0 token"。
- 费用未知不等于 0：模型价格表里查不到价格的调用、以及只统计 token 的旁路调用，报表末尾会单独提示"费用未计入 / 只统计 token"，不会把未知当成免费。
- 仅超级管理员可用。

## 高风险工具确认

当工具调用触发高风险确认时，Agent 会提示可用确认命令，例如：

| 命令 | 作用 |
| --- | --- |
| `/detail` | 查看待确认工具调用详情；支持工具自定义纯文本详情，未自定义时显示格式化后的参数。 |
| `/confirm` | 确认当前待确认工具调用。 |
| `/confirmtool` | 确认当前工具。 |
| `/confirmall` | 确认当前批次全部待确认工具。**只在私聊与 CLI 可用**。 |
| `/reject` | 拒绝当前待确认工具调用。 |
| `/stop` | 停止当前请求。 |

确认交互的三条边界：

- **只能由发起人处理**：这条高风险调用属于谁，就只有谁能 `/confirm` / `/confirmtool` / `/detail` / `/reject` / `/stop`。共享线程群（`thread_mode = "group"`）里所有人共用一个 Session，没有这道校验时任何群成员都能替发起人放行；超级管理员是唯一例外（运维解锁，审计记 `foreign_actor`）。
- **群聊里不展示参数**：群聊 / 频道的确认提示只给工具名与风险等级，参数显示为「已隐藏」；`/detail` 同样不展示，因为提示本身发在群里，所有成员都能看到。
- **群聊里没有 `/confirmall`**：一次放行「本会话后续所有高风险工具」在群里风险太大，默认不提供（私聊 / CLI 保留）。群里需要减少重复确认时用 `/confirmtool`（只放行同一个工具）。

普通用户的确认等待默认在 10 分钟无有效操作后过期；若当前群聊或私聊的 Session TTL 更短，则使用更短的时限。超级管理员不受额外的 10 分钟限制，但仍遵守大于 0 的 Session TTL。执行 `/detail` 会重新计时；过期后工具不会执行，当前处理会停止。

### 授权凭据（`/grant` / `/panic`）

`/confirmall` 与 `/confirmtool` 产生的是**有生命周期的放行凭据**，不是永久开关：

- 默认有效期 10 分钟（会话级与工具级相同），过期后重新需要确认；
- 绑定产生时的**会话作用域 + 主体**：换个人（共享线程群的其他成员）或换个会话都不能复用；
- 群策略 / 权限配置改变、Session 被删除时，对应凭据立即失效；
- 凭据只存在进程内存里，重启即清空。

| 命令 | 作用 |
| --- | --- |
| `/grant` / `/grant list` | 列出当前生效的放行凭据（id、工具、会话、作用域、主体、已用次数、剩余时间）。 |
| `/grant revoke <id>` | 按 id（或唯一前缀）立即撤销一条放行。 |
| `/panic` | 紧急停止：撤销**全部**放行、停止所有进行中的 turn、取消排队请求、丢弃排队消息。群里执行需要写 `/panic confirm` 二次确认；私聊与 CLI 直接执行。 |

两个命令都仅超级管理员可用；每次发放 / 使用 / 撤销 / panic 都会写审计（`grant_created`、`grant_used`、`grant_revoked`、`panic`）。

具体提示以运行时输出为准。
