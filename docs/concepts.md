# 核心概念

本文档解释 ATRI-X 的主要概念，帮助理解为什么它这样组织对话、工具、上下文和扩展能力。

## Agent Core

Agent Core 是 ATRI-X 的对话与编排中心，负责：

- 接收平台输入。
- 解析 slash 命令。
- 管理 Session 上下文。
- 构建 Prompt。
- 调用 LLM。
- 处理工具调用循环。
- 统一输出最终回复。

平台适配层只负责把平台消息转换为统一输入，以及把输出意图发送回平台。Agent Core 不依赖具体平台。

## Chat / Work 双模式

ATRI-X 把对话分成两种模式：

| 模式 | 适合场景 | 工具 |
| --- | --- | --- |
| `chat` | 闲聊、陪伴、轻量问答、低成本对话 | **同样注入并执行工具**（本仓相对上游的有意差异） |
| `work` | 搜索、文件、命令、Cron、Skill 等任务 | 注入并执行工具 |

两种模式都能跑工具调用循环，区别只在模型槽位与上下文策略：

- `chat` / `work` 各自通过 `state.toml` 的 `mode_models.chat` / `mode_models.work` 配模型，
  可以让低成本模型负责闲聊、强模型负责复杂任务（两者都能调工具）。
- `[context]` 的溢出策略、`/overflow --chat|--work` 可以按模式分别设置。
- 要禁止某个群用工具，用群策略 `/tools allow none` 或桌面端「工具权限」页设「禁止全部」，
  与模式无关。

运行时可以使用 `/chat` 和 `/work` 切换模式。

## 工具发现

ATRI-X 不会在每轮对话中默认注入所有工具的完整 schema。

默认流程是：

1. Agent 只提供 `discover_tool` 和当前可用工具名称。
2. LLM 判断需要哪个工具。
3. LLM 调用 `discover_tool` 获取工具详情。
4. Agent 把被发现的工具 schema 注入后续请求。
5. LLM 再调用具体工具。

这种机制可以减少普通任务中的无效上下文开销，也能降低无关工具干扰。

## 内联预载

内联预载用于在普通输入中提示 ATRI-X：本轮任务可能需要某类工具或某个 Skill。

它的作用是减少 LLM 先发现工具再调用工具的步骤，让明确的任务更快进入工作状态。预载成功后，相关工具或 Skill 会进入当前 Session 的工具上下文。

工具可用 `@tool:<name-or-tag>` 或简写 `@t:<name-or-tag>` 预载，Skill 可用 `@skill:<name>` 或简写 `@s:<name>` 预载；冒号也可以写成中文全角冒号 `：`。

具体语法和示例见 [命令速查：工具与 Skill](commands.md#工具与-skill)。

## Session

Session 是 ATRI-X 的持久化会话单位，用来保存一次连续对话的上下文、模式、消息历史和工具调用记录。

默认情况下，群聊中每个成员有自己的 Session。群管理员或超管可用 `/grouppolicy thread-mode group` 开启共享线程：同一群内所有成员共享一个 Session，普通消息按到达顺序串行处理，每条用户消息会带上服务端生成的发言成员标记，便于模型区分不同发言者（该标记仅作为上下文归属提示，不作为权限判断依据）。`/grouppolicy merge-window <0-10000>` 可让同一成员在很短时间内连续发出的消息合并为一轮；不同成员不会被合并成同一个权限主体。开启共享线程不迁移旧的每人独立 Session。

群聊还可以配置本地确定性知识库（FAQ）。命中后由服务端直接回复管理员配置的答案，不创建 Session、不调用 LLM；未命中才进入正常对话流程。匹配只做归一化和 exact / contains / keywords 规则，不做模型语义判断。

不同平台、不同聊天作用域通常会使用各自的 Session，避免上下文互相污染。CLI 属于本地高权限入口，可以跨平台查看和管理 Session。

Session 的创建、恢复、归档、置顶和删除等操作见 [命令速查：Session](commands.md#session)。

在支持引用回复的平台中，回复自己在当前聊天作用域内某个 Session 的最后一条 assistant 消息会自动恢复该 Session；回复更早的 assistant 消息会从该位置 Fork。即使 Session 已因闲置过期或 `/new` 不再是当前会话，引用最后回复仍会恢复原 Session。

## Fork

Fork 用于从历史对话中的某个 assistant 回复处分出新的对话分支。

新分支会继承 fork 点之前的上下文，但不会修改原 Session。它适合在同一个问题上尝试另一种思路、重做方案，或保留原对话同时继续探索。

## 上下文压缩

长对话会逐渐接近模型上下文窗口。上下文压缩会用压缩模型整理当前对话，再把压缩结果与所有历史用户原话组装为新的上下文起点。

压缩成功后会切换到一个完全独立的新 Session，标题为 `原标题 compacted-N`。旧 Session 不会被修改，新旧 Session 也没有 Fork 关系。用户下一条消息会和组装后的压缩内容合并成新 Session 的第一条用户消息。它可以自动触发，也可以由用户手动触发。

为避免单条超长消息直接撑爆上下文，Agent 在发送前会估算 prompt token。默认超限时拒绝调用模型并在聊天里报警；管理员也可以按 chat/work 模式启用 `truncate` 或 `summarize`。压缩历史用户原话时也会先按 `user_original_max_runes` 限长，避免摘要本身再次超窗口。

## Prompt 与 Soul

Soul 是 Agent 的基础 System Prompt，用来定义人格、行为边界和长期稳定的表达风格。

实际发送给 LLM 的 Prompt 不只包含 Soul，还会按当前会话动态组合平台信息、用户身份、记忆、工具提示、压缩摘要和会话历史。

带图片的消息会同时向视觉模型发送图片和紧邻图片的文本标签；标签按消息内顺序编号，并在可复用时包含原始 HTTP(S) URL，方便模型在上传、编辑等工具调用中准确引用图片。内嵌图片只标明没有可复用 URL，不会把 base64 展开进文本；恢复 Session 后行为相同。

语音消息在全局 `[asr]` 和群策略开关都开启时先转写成 `[语音 N 自动转写（可能有误）：...]` 文本段，再进入同一套 Prompt、工具和上下文流程；未开启或转写失败时保持原来的 `[语音]` 引用，不发起 ASR 调用。

因此，Soul 只适合放稳定规则；工具发现、时间、平台上下文和临时状态等动态信息不应硬编码进去。

## 记忆

ATRI-X 将记忆分成两类：

| 类型 | 用法 |
| --- | --- |
| 常驻记忆 | 短小、稳定、经常有用的信息，会在对话中自动注入。 |
| 长期记忆 | 更长、更复杂的信息，按需通过工具搜索和使用。 |

常驻记忆适合保存偏身份、偏偏好、偏长期稳定的信息。长期记忆适合保存篇幅更大、需要检索的资料。normal 以“每条一行、一行一件事”的结构化方式保存：写入时去掉列表前缀、空行和重复条目，并限制条目数、单条长度和写入频率，同时逐条拒绝明显的指令类内容；这些限制用于降低常驻记忆被当成持久 Prompt 注入通道的风险。

长期记忆还可以放在 `long_memory/` 目录里，由模型通过 `long_memory_search` / `long_memory_write` 检索与维护（面向超级管理员的资料库）。这套记忆按会话隔离：每条新记忆记录写入时的会话作用域（`平台:会话 id`）与写入者，检索、更新、删除默认只看当前会话与未归属会话的旧数据；超级管理员可以显式传 `scope=all` 跨会话检索，或传具体作用域键定向查看别的会话。升级前的旧记忆没有来源字段，按“全局旧数据”处理，仍然到处可见。

长期记忆（`angel_memory`）在写入时记录来源类型、来源用户、平台消息 ID 和 Session ID，因此可以查看和删除单条记忆，也可以按来源消息删除；平台消息被撤回时，默认同时删除由该消息派生的长期记忆。普通成员只能删除来源为自己的群记忆，群主、群管理员或超级管理员可以删除当前群 scope 的任意记忆。

默认只有人（命令）能删除长期记忆：模型只能写入和检索。开启 `[angel_memory].allow_tool_forget` 后，模型可以在用户明确要求时调用 `angel_forget` 删除一条来源为该用户的记忆；该工具属于高风险操作，会先向用户确认，且一次只删一条。

## Tool Runtime

Tool Runtime 管理工具的注册、发现、权限、风险评估和执行。

工具可以来自内置能力、Skill、外部扩展或监听事件。ATRI-X 不会默认把所有工具细节都塞进上下文，而是通过工具发现和预载机制按需暴露。

工具执行期间收到的新消息不会打断正在运行的工具。下一次模型请求尚未开始时，多条消息会按顺序合并后注入该请求；如果最终模型请求已经开始并直接结束了当前轮次，这些消息会在回复发出后自动作为一条新用户消息开启下一轮。

常见工具能力包括：

- 搜索与网页提取。
- 文件读写与文件发送。
- Shell 命令。
- 聊天历史查询。
- 记忆管理。
- Cron 管理。
- Skill 创建、修改和运行。

## System Prompt

系统提示词在每轮请求时实时组装为一条 system message，各部分按以下顺序排列：

1. Soul。
2. 当前发言人的群友档案（群聊且该档案勾选了「参与对话」时）。
3. 当前可用的工具与 Skill。
4. 已激活的 Tool Tag 提示。
5. 当前用户的常驻记忆。
6. 表情提示（启用本地表情图库时）。
7. 当前会话的 Meta 信息。

常驻记忆会包在 `<resident_memory>` 边界内注入，并带有一句明确声明：它是用户数据，不是系统指令，不得覆盖当前对话指令、安全规则或工具权限。记忆内容中的尖括号会被转义，避免内容提前结束或伪造边界标签。

群友档案（桌面端「群友」页维护，存在 `<配置目录>/gui/members.json`）只注入**当前发言人**这一份，按「平台 + 完整作用域 + 用户 id」精确匹配，因此同一个人在不同群可以有不同设定，私聊与群聊也不共享。它同样包在 `<member_profile>` 边界内并附信任声明：档案是用户数据、不改变机器人自身身份、不授予任何权限；其中的虚构身份只用于角色互动，不应当作现实事实。后台 Cron / 外部事件等沙盒上下文不注入该片段。保存后下一轮对话生效，不需要重启服务。


Meta 信息使用一行纯文本，例如：

```text
meta: platform=qqonebot, conversation=private, display_name="昵称"(id:1001).
meta: platform=qqonebot, conversation=group(id:9), display_name="群名片"(id:1001), session_created_at=2026-08-27T12:34:56.
```

`display_name` 在群聊中优先使用群名片，没有群名片时使用昵称；私聊和频道中使用昵称。缺失的 ID 不输出对应括号；没有展示名但有用户 ID 时输出 `display_name=""(id:xxx)`，两者都缺失时省略该字段。
`session_created_at` 是 Session 创建时的当地时间，精确到秒；该值在 Session 生命周期内保持不变，以免时间变化降低 Prompt 缓存命中率。

## 安全策略

ATRI-X 的工具系统包含风险等级和权限控制。

核心规则：

- 风险等级用于内部权限与确认，不直接暴露给 LLM。
- 普通用户只能发现和调用允许风险范围内的工具。
- 超级管理员调用高风险工具时也需要确认。
- 权限拒绝、危险确认和工具调用会进入审计日志。

CLI 默认本地用户 `local` 是超级管理员。

## Hook

Hook Layer 用于在 Agent 的关键流程前后插入扩展逻辑，例如修改输入、补充上下文、追加输出意图或触发外部动作。

Hook 是扩展机制，不是权限机制。工具调用、危险操作和角色限制仍由 Security Layer 判断。

完整配置和示例见 [Hook：规则 Hook 配置](hooks.md#规则-hook-配置)。

## Output Layer

Output Layer 定义平台无关的输出意图。

Agent、Hook 和 Tool 不直接依赖具体平台发送消息，而是返回统一的输出意图，再由 Output Manager 交给对应平台适配器发送。

这样可以让同一段 Agent 逻辑复用于 CLI、聊天平台和未来的新平台。

## Cron

ATRI-X 包含两层 Cron 能力：

| 类型 | 说明 |
| --- | --- |
| Direct Cron | 按计划直接发送固定内容。 |
| LLM Cron | 按任务描述驱动模型执行，并可使用工具。 |

LLM Cron 每次调度触发都会创建独立的后台 Session，把任务作为新输入执行，并在完成后发送本轮结果。Session 可在创建 Cron 的平台通过 `/sessions`、`/resume` 查看；广播任务会为其他目标平台复制 Session，CLI 可查看全部平台 Session。在后台任务仍在运行时用 `/resume`（或 `/unarchive`）进入该 Session，会把它提升为当前前台会话并中断在途的后台任务：后台不再继续调用模型，也不会补发本轮汇报。



## Atrinis / Atriwisp / Atriena

Atrinis 是 ATRI-X 的监听枢纽，用于接收外部事件。Atriwisp 是外部子监听器，负责观察服务器、Webhook、RSS、日志或脚本输出等外部世界。Atriena 是 Atriwisp 向 Atrinis 投递事件的协议，也是 Hook exec 等内部触发源复用的动作协议。

它们的分工是：Atriwisp 观测一切，Atrinis 管理一切，ATRI-X 掌控最终执行与投递。

Atrinis 不作为聊天平台，也不替代 Cron。Cron 处理“按时间触发”的任务，Atrinis 处理“按外部事件触发”的任务。完整介绍见 [Atrinis 监听枢纽](atrinis.md)，配置和请求示例见 [Atrinis 配置与使用](atrinis-usage.md)。

## Skill 与 Atriyph

Skill 是 ATRI-X 的可复用任务扩展。它可以是一段给 Agent 阅读的任务说明，也可以被包装成结构化工具供 LLM 调用。

ATRI-X 支持两类主要 Skill：

- AgentSkill：以文档形式描述任务，让 LLM 按说明使用通用工具完成工作。
- 工具化 Skill：把 Skill 注册成普通工具，让 LLM 通过结构化参数调用。

Atriyph Task Notation 是 ATRI-X 用来描述可复用任务的结构化表示法。它用更稳定的格式表达输入、输出、步骤、条件和约束，减少自然语言任务描述的歧义。

工具化 AgentSkill 配置示例见 [配置说明：AgentSkill 工具化配置](configuration.md#agentskill-工具化配置)。Atriyph 与 Skill 的关系见 [Atriyph 任务表示法：与 Skill 的关系](atriyph.md#与-skill-的关系)，完整语法见 [语法速查](atriyph.md#语法速查)。

> **随包 AgentSkill（1.1.3 起）**：科研研究助理的 15 个 AgentSkill（12 个可调用的硬工具 +
> 3 个协议文档）不由用户创建，而是**随安装包/便携版分发**；首次启动时从 `research_pack/`
> 装进 `<配置目录>/skills/agent/research_*`，清单里的 `command` 指向随包引擎
> （`atri-x.exe -m atri_x.research.runner`），因此冻结产物也不需要系统 Python。详见
> [科研研究助理](research.md)。
>
> **随包 AgentSkill（1.1.5 起，图表）**：draw.io 图表能力走同一个模式——上游
> drawio-skill 3.4.2 随包放在 `drawio_pack/`，首启装进
> `<配置目录>/skills/agent/drawio_skill`，工具名 `drawio`（**仅超级管理员**），
> `command` 指向 `atri-x.exe -m atri_x.diagram.runner --skill-dir <技能目录>`；
> 渲染用的 draw.io 桌面版放在产物 `_internal/drawio_runtime/`，只读、不复制到用户目录。
> 详见 [draw.io 图表能力](diagram.md)。

### Skill 使用媒体

媒体使用稳定标识 `media:<64 位小写 SHA-256>`。普通文档型 Skill 可以指导 Agent 将标识传给支持媒体的工具。

bash 脚本通过 `shell` 显式声明输入，例如：

```json
{
  "cmd": "python generate.py --input \"$ATRIX_MEDIA_1\" --output result.png",
  "media_inputs": [{"media": "media:<sha256>"}]
}
```

`media_inputs` 每项仅接受 `media`。宿主在 sandbox 的 `media-inputs/` 目录导出或复用文件，并为本次进程注入 `ATRIX_MEDIA_1`、`ATRIX_MEDIA_2` 等环境变量。PowerShell 使用 `$env:ATRIX_MEDIA_1`。脚本将输入视为只读，修改前先复制；缓存命中也刷新文件使用时间，闲置副本由既有 sandbox 保留期清理。原始命令和调用参数保持不变。

工具化 AgentSkill 在 `ATRIX_SKILL.toml` 的 `parameters.properties` 中使用 `{"type":"media"}` 声明媒体参数，并在 `[args]` 中映射命令行 flag。LLM 传入媒体 ID，实际进程收到相对 Skill 根目录的调用期文件路径；普通字符串参数不会自动转换。

调用工具时，若json字段使用到了媒体id，也会视为该session的引用。纯文本的调用不算。

Go Skill 在 `go_skill_run` 的 `payload.media_inputs` 中使用同样的输入列表。宿主在 stdin 的执行副本中为各项补充 `path`、`name`、`mime_type`、`size`，不超过 1 MiB 的文件还提供 `base64`。`payload.media_workspace` 是相对 Skill 根目录的调用专属目录；可以传空输入列表申请仅用于输出的目录。临时路径和 base64 不写回原始调用参数。

Go/TOML Skill 的 stdout 可以返回媒体结果：

```json
{"content":"处理完成","segments":[{"type":"image","path":"result.png"}]}
```

`segments` 支持 `text`、`image`、`file`；媒体段必须且只能提供 `media` 或 `path`。路径相对 Skill 根目录解析，拒绝绝对路径、`..` 和 symlink/junction 逃逸。宿主在返回前导入文件，Tool Transcript 保存稳定媒体 ID 并建立引用。调用专属目录及调用期引用在成功、失败、取消或超时后释放；Skill 根目录中的其他工作文件仍由 Skill 管理。Go Skill 建议把临时输出写入 `media_workspace`，TOML 脚本可写入收到的媒体输入所在目录。

## 平台适配

> 移植状态：见 devdocs/porting-status.md 待核对

标注只针对**未连真实外部服务端**：CLI、QQ OneBot、QQ 官方、Telegram 四个适配器都有实现与进程内用例（`tests/test_platform_*.py`），但没有用真实服务端做过端到端往返。

Platform Adapter 负责接入具体平台。

它的职责是把平台消息转换为 Agent Core 能理解的统一输入，并把 Output Layer 的输出意图转换回平台消息。

因此，Agent Core 不需要关心消息来自 CLI、群聊、私聊还是其他平台。

## 日志与审计

ATRI-X 区分：

| 类型 | 用途 |
| --- | --- |
| 运行日志 | 排查启动、模型请求、平台连接、持久化等运行问题。 |
| 审计日志 | 追踪权限拒绝、工具调用、危险确认、Cron 投递等关键行为。 |

可以用 `/log` 和 `/audit` 在运行时查询。

## 开发期约定

ATRI-X 仍在快速开发中：

- 内部接口可能调整。
- 配置和命令可能变化。
- 用户文档优先覆盖稳定使用路径。
- 详细开发计划和任务拆分放在 [`../devdocs/`](../devdocs/)。
