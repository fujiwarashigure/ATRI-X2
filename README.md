# ATRI-X（Python 版）

**ATRI-X 的 Python 全量重写**：一个轻量、可扩展、多平台的 LLM Agent / Chatbot 守护进程。

上游是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本仓库对应 v0.9.0）。本仓库把上游 71 个生产包逐包重写为 Python（包名 `atri_x`），**纯标准库、零运行时第三方依赖**，在仓库根目录直接 `python -m atri_x` 运行——不需要安装，也没有构建产物。

支持普通聊天、工具调用、Hook 扩展、Cron 调度、Skill 沉淀、长期记忆、外部事件触发、多平台接入与容器化部署，适合个人助理、平台机器人和可编排自动化助手等场景。

> **⚠️ 法务与合规提示**：本项目**仅供学习、技术研究与个人自托管实验**，**不是**腾讯 / QQ /
> Telegram / NapCat / SnowLuma / DeepSeek 等任何平台或服务商的官方产品，与其**无隶属、授权或背书
> 关系**。使用前请阅读下方的 [法务与合规注意事项](#法务与合规注意事项) 与 [LEGAL.md](LEGAL.md)；
> 按“现状”提供、风险自负。

| 项 | 值 |
| --- | --- |
| 版本 | `1.1.3`（与上游 v0.9.0 对应；Windows 桌面端/安装包自 0.9.1 起提供，便携版 zip 自 0.9.3 起提供） |
| 许可 | [Apache-2.0](LICENSE)（与上游一致） |
| Python | ≥ 3.11（开发与验证在 3.14.7 上完成） |
| 入口 | `python -m atri_x` |

## 法务与合规注意事项

> **一句话**：本项目仅供学习 / 研究 / 个人自托管实验，须遵守法律法规与平台协议；按“现状”提供，
> 风险自负。**完整条款见 [LEGAL.md](LEGAL.md)**。

- **非官方项目**：与腾讯 / QQ / Telegram / NapCat / SnowLuma / DeepSeek 及任何模型、搜索、生图、语音
  服务商**均无隶属、授权、赞助或背书关系**；接入平台用的是公开接口或非官方协议端，使用前请自行评估
  账号风险。
- **遵守法律法规**：包括《网络安全法》《数据安全法》《个人信息保护法》《生成式人工智能服务管理暂行
  办法》《互联网信息服务深度合成管理规定》《网络信息内容生态治理规定》《民法典》《广告法》
  《反电信网络诈骗法》《未成年人保护法》等，以及你所在司法辖区的法律。**不得**用于诈骗、赌博、色情、
  暴力恐怖、谣言、侵权、骚扰、人肉、爬取隐私、群发广告、养号、绕过风控 / 实名 / 审核 / 付费等用途。
- **遵守平台与第三方协议**：《QQ 用户协议》《腾讯服务条款》与 QQ 机器人 / 开放平台规则；Telegram ToS；
  NapCat 的许可；SnowLuma 的 EULA（本仓库与发行包**不内置、不再分发**，须自行安装并自行同意）；
  DeepSeek 及各 API 服务商的条款与计费规则。平台协议与本项目文档冲突时，**以平台协议与法律为准**。
- **账号、凭据与费用自负**：自备账号与 API Key，自行承担费用与配额；凭据泄露、账号封禁 / 被盗、违规
  后果由使用者承担；测试请用独立小号，不要把真实 Key / 令牌 / 登录态提交到公开仓库、Issue 或群聊。
- **内容与数据合规**：你对输入、生成、存储、发布的一切内容负责；AI 生成内容可能不准确或有偏见，
  适用法律要求时应做**深度合成标识**；处理他人个人信息须有合法性基础并履行告知义务，不得采集他人
  敏感个人信息；会话 / 记忆 / 日志默认存本机，请自行备份、加密与脱敏；内置内容安全模块只是辅助，
  **不构成合规保证**。
- **第三方组件与商标**：Apache-2.0 只覆盖本项目代码，**不覆盖** NapCat、便携 QQ、Node.js / DSH 内核、
  CPython、Pillow、SnowLuma 等第三方组件；相关名称与标识归各自权利人，本项目仅作事实性指称，
  不代表授权或关联。
- **免责与责任限制**：按 **“现状”（AS IS）** 提供，不附带任何担保；在法律允许的最大范围内，作者与
  贡献者不对账号封禁 / 被盗、数据丢失或泄露、业务中断、利润损失、第三方索赔、行政处罚等任何直接或
  间接损失负责；本项目可能随时变更或停止维护。
- **与许可证的关系**：[LICENSE](LICENSE)（Apache-2.0）授予版权许可，但**不能免除**你应遵守的法律法规、
  平台协议与第三方权利；本注意事项不是对许可证的额外限制，如有冲突，版权许可事项以 LICENSE 为准。
- **未成年人**：不得用于侵害未成年人权益；未满 18 周岁应在监护人同意与指导下使用。
- **举报**：发现违规使用，或发现仓库内存在不应公开的敏感信息（凭据、个人信息等），请通过 GitHub
  Issue 联系维护者。

详见 **[LEGAL.md](LEGAL.md)**：完整的禁止用途清单、平台 / 第三方协议逐项说明、内容与数据合规要求、
AS IS 免责条款、责任与赔偿、生效与更新机制，以及英文摘要。

## 项目速览

| 项 | 说明 |
| --- | --- |
| 上游 | Go 版 `ATRI-X` v0.9.0：446 个生产 `.go` 文件 / 90,203 行（含测试共 754 个文件 / 146,153 行） |
| 本仓库 | 平铺布局的 `atri_x/`：777 个 `.py` 模块，约 22.8 万行（物理行，含空行/注释/docstring；`tests/` 另有约 11.0 万行） |
| 研究引擎 | 随包 `research_pack/`：38 个模块、约 8.7k 行的纯标准库引擎 + 15 个原创 AgentSkill（12 个硬工具 + 3 个协议技能，1.1.3 新增）；安装版 / 便携版首启自动装进配置目录，源码树用 `atri-research/install.py` 装 venv 版 |
| 包级映射 | 上游 71 个生产包 + `cmd/atri-x`，共 72 条映射，全部有对应实现（[PORTING.md](PORTING.md) §4） |
| 平台 | CLI（本地 TUI / 远程客户端）、Telegram、QQ OneBot、QQ Official，另有 headless 平台适配 |
| 存储 | SQLite（标准库 `sqlite3`），会话、消息、工具调用、媒体引用与审计数据均持久化 |
| 测试 | `tests/` 225 个 Python 文件（其中 216 个 `test_*.py`）；用例数不硬编码在这里（会随改动漂移，见「开发与验证」）；仓库自带七道质量门禁 |
| 规模 | 1229 个受版本控制文件（工作树文件清单；不含 `runtime`/`build`/`dist`/缓存/本机私有配置；1.1.3 发行快照按当时清单打包为 1229 条） |

## 功能特性

### 对话与工具

- **Chat / Work 双模式**：两种模式可分别配置模型，让低成本模型负责闲聊、强模型专注复杂任务；**两种模式都支持工具调用**（本仓相对上游 Go 版的有意差异）。
- **按需工具发现**：不会每轮注入全部工具 schema，默认只暴露 `discover_tool` 与可用工具名；模型按需发现后再注入具体 schema，减少无效上下文开销。另可用 `@tool:` / `@skill:` 内联预载。
- **内置工具 54 个**：文件、Shell、Web、Cron、Skill、媒体、记忆、时间与人设卡导入/启用等；其中 15 个无条件注册，其余按依赖条件注册。
- **联网搜索与网页抓取**：`web_search` 支持 DeepSeek 官方搜索 / Tavily / Firecrawl / Keenable / Exa / SearXNG / Antigravity / Grok 多种通道并按顺序自动降级（没配 key 的通道自动跳过）；`web_extract` 支持 DeepSeek 官方抓取 / Jina Reader / 直接抓取。两条链路都在桌面端「模型 → 搜索服务 / 抓取服务」里配置，DeepSeek 官方通道需要 `api.deepseek.com` 的官方 Key（按成功请求计费），承载聊天的中转 Key 不能用于搜索/抓取。见 [配置说明](docs/configuration.md)。
- **风险与确认**：工具带风险等级，按角色/权限判断可用范围；高风险调用需要逐次确认。
- **单轮声明与命令前缀**：`@model:` / `@image:` / `@use:`（也支持 `#模型:` / `#生图:` / `#工具:`）只对当前这一轮生效，仅超级管理员可用；slash 命令前缀可通过 `[commands].prefixes` 追加。

细节见 [核心概念](docs/concepts.md) 与 [命令速查](docs/commands.md)。

### 平台与前端

- **CLI**：本地 TUI（聊天区、通知区、滚动、copy mode、文件引用补全）；也支持把进程作为服务端、任意电脑用 CLI 客户端远程连接（默认端口 `32172`）。
- **Windows 桌面端（选项卡式）**：Tk 窗口 + 托盘常驻 + 后台服务自动拉起；14 个选项卡（对话 / 模型 / 人设卡 / 平台 / 在线 / 群友 / 知识库 / 进阶 / 时间 / 日志 / 功能 / 工具权限 / 研究 / 权限）把模型 API 与密钥、角色人设卡与 `SOUL.md`、QQ/Telegram 接入、群知识库、生图/视觉/语音与记忆开关、维护任务和功能状态都做成界面操作，配置改动自动留备份。密钥输入框可一键显示/隐藏；模型页与生图区能直接拉取上游 `/models` 清单并给模型起本地备注名，Provider 表单还可选**推理思考强度（`reasoning_effort`：none / minimal / low / medium / high，留空 = 不发送）**并按模型覆盖；**模型页保存后会从磁盘回读逐字段校验**，界面值与配置文件不一致时直接报错，模型槽位清空/半填也有明确规则，不会出现"界面改了、文件没变"；生图区支持**本地生图服务挂载**（端口映射 / AUTODL 等 SSL 链接 → OpenAI 兼容 Base URL，可一键检测服务类型）；「在线」页并排显示后台服务 / OneBot 协议端 / QQ 平台三段状态，并提供连接/断开开关（不用手改 TOML），发行包可内置 NapCat + 便携 QQ，在「在线」页一键启动协议端并扫码登录；「日志」页统一查看运行/审计/Atrinis/后台输出/界面操作五类日志并可导出当前视图；「工具权限」页按群在**纵向可滚动的工具菜单**里逐个「激活」工具（每行带中文名、风险、一句话功能解释与激活状态；模式为继承全局 / 仅白名单 / 禁止全部，另有搜索 / 只看已激活 / 常用与只读预设）并开关"群聊里显示工具调用进度"，第三个子选项卡**「工具参数（全局）」**还给每个工具一组可展开 / 收起的参数面板（生图留档 `metadata_store`、搜索通道顺序、记忆写入上限、风险阈值、生图提示词优化……），不用再手改 `services.toml` 再重启；「人设卡」页可导入/导出角色卡、粘贴一段文本一步建卡，并能把某张卡载入全局 `SOUL.md`（自动备份、可回滚）；「群友」页按群管理每个人的备注名、标签、角色数据与头像，并可勾选「参与对话」——保存后下一轮对话就把**当前发言人**的档案注入系统提示词（按平台+群+用户精确匹配，界面下方有注入预览），还能给单个成员绑定**专属人设**并随时开关（只影响他本人的对话，关掉恢复默认 `SOUL.md`，不干扰其他人）；「权限」页登记各平台**超级管理员**（QQ 填 QQ 号）并调普通成员工具风险、单轮工具轮次与"允许成员建卡"开关；**「时间」页**把统一时间服务全摆到界面上：每秒刷新的当前时间/时区/偏移/tzdata 读数、不触网的时区探针（含 DST 冬夏断言）、`[time]` / `[time.verification]` / `[[time.sources]]` 的全部可调项（含按表格增删来源），以及从后台 `/diagnostics` 读回的校验状态、每条来源最近观测与告警；「进阶」页里的自学习设置、HOOK 插件配置与定时维护任务是按钮打开的二级窗口（维护任务用「每天 / 每周 / 每月 / 自定义 cron」或「指定日期一次性」调度，并实时显示下次触发时间）；「进阶」页整页可竖向滚动，窗口再小也不会把下方控件挤出可视区。见 [Windows 桌面端](docs/windows-app.md)。
- **Windows 两种交付形态**：安装包（开始菜单/自启/卸载项）与**便携版 zip**（解压即用，数据可留在解压目录，整包拷走即可复现问题）；一条 `python packaging/build.py --release` 同时产出并各自自检。
- **即时通信平台**：Telegram、QQ OneBot、QQ Official，统一抽象平台层与输出层，便于扩展新平台。
- **前端自由定制**：远程 CLI 使用 WebSocket 协议，附带补全接口，可以自己写前端界面；协议见 [前端 API](docs/frontend-api.md)。

### 会话、上下文与记忆

- **持久化 Session**：恢复、归档、置顶、Fork、删除、分页查看与平台隔离；回复历史消息可恢复或 Fork 会话。
- **上下文压缩与预算**：长对话自动压缩；发送前做 prompt token 预算检查；超长消息默认拒绝，群管理员可用 `/overflow` 在 `truncate` / `summarize` 间切换。
- **常驻记忆与长期记忆分层**：常驻记忆只保留每轮都要注入的短信息（内部区分 core / normal），更长内容由模型按需查询长期记忆；长期记忆使用 Markdown 源数据 + SQLite FTS，写入原子化，外部改动会重新加载。
- **自主学习**：观察与候选先进入 pending，管理员 review 通过后才注入上下文。

细节见 [核心概念](docs/concepts.md) 与 [命令速查](docs/commands.md)。

### 扩展机制

- **Hook 系统**：可在 Agent 输入、LLM 请求/响应、平台发送、平台连接等事件点插入逻辑；支持规则 Hook、进程 Hook 与 Worker Hook，**插件可用任意语言**编写，协议见 [Hook](docs/hooks.md)。
- **Cron**：普通 Cron 按计划发送固定内容；LLM Cron 用任务描述驱动模型执行，适合需要分析、归纳或使用工具的定时任务。
- **Skill**：原生 Skill 用 Atriyph 记法描述；也支持 Go 源码 Skill（需要 Go 工具链）与 agentskills.io 风格的外置 AgentSkill。内置 `create_atrix_skill` 元工具，可让模型把可复用经验沉淀为 Skill。
- **Atriyph 任务表示法**：用结构化记法表达输入、输出、步骤、条件和约束，减少自然语言任务描述的歧义，便于 lint、审计与复用。见 [Atriyph](docs/atriyph.md)。

### 科研研究助理（1.1.3）

把「查文献 → 筛文献 → 核验引文 → 写稿 → 评审 → 导出」做成**可复用硬工具**的科研子功能：

- **12 个确定性硬工具 + 3 个协议技能**：`research_plan` / `search` / `screen` / `verify` / `cite` / `write` / `panel` / `export` / `doctor` / `flow` / `compose` / `audit`。检索去重、PRISMA 计数、DOI 反查、引用格式化、正文结构、六维评分全部由 Python 引擎完成，**不依赖模型自评**——换模型时流程顺序、格式与判定标准不变；技能文档里的参数表由引擎生成并校验同源，不与实现漂移。
- **随包交付、开箱即用**：研究引擎与 15 个 AgentSkill 随安装包/便携版一起分发（仓库里是 `research_pack/`），**首次启动自动装进配置目录**（`%APPDATA%\ATRI-X\research` 与 `skills\agent\research_*`），不需要系统 Python、不需要另跑安装器；升级安装包时按内容摘要自动升级。源码树用户按 `atri_x/research/` 的说明用 `atri-research/install.py` 装 venv 版。
- **按需调用、普通成员可用**：12 个工具全部 `risk="low"`、`superadmin_only=false`，低于默认 `user_max_tool_risk="low"`；AgentSkill 渐进披露（系统提示只列技能名，发现后才注入正文），不常驻上下文。
- **可观测**：`/research` 命令（`status` / `env` / `open` / `<运行id>`，普通成员只读）与桌面端「研究」选项卡（运行列表 + 产物树 + 打开报告/导出目录）看的是同一份研究目录；`research_doctor` 实测网络、缓存与依赖，`degraded` 字段显式声明降级（缺 PDF 库时导出仍给 md/html/docx）。
- **离线与依赖**：引擎零第三方代码依赖；PDF 导出/全文抽取用可选 `reportlab` / `pypdf`，发行版把它们打进冻结包，源码树缺库时自动降级并在结果里说明。原创实现与许可说明见 [LEGAL.md](LEGAL.md)。

### 事件触发：Atrinis

传统 Agent 只等待用户输入，Cron 只能响应时间；**Atrinis** 让外部事件也能触发 Agent：

- **Atrinis** 是监听枢纽，**Atriwisp** 是部署在各处的外部监听器，**Atriena** 是统一的 JSON over HTTP 事件协议。
- 服务器告警、RSS 更新、Webhook、游戏事件等信号都可以送入 Atrinis，再交由 Agent 处理并回发。
- 入口默认关闭；启用后监听 `32170`。

见 [Atrinis 监听枢纽](docs/atrinis.md) 与 [Atrinis 配置与使用](docs/atrinis-usage.md)。

### 角色、生图与多模态

- **角色素材库**：用文件夹管理角色文本（人设/世界观/开场白/示例）与图片素材，支持索引、检索、`@char:<id>` 临时启用与 `character_*` 工具。见 [角色素材库](docs/character-library.md)。
- **图像生成**：对接 OpenAI 兼容的 `/images/generations`，按「全局预设 + 角色预设 + 场景描述」组装提示词，支持规则优化、画师串、多角色同框、并发与排队控制。开启 `[image_generation].metadata_store`（桌面端「工具权限 → 工具参数（全局）」）后，入库转码**之前**会留档原图的生成参数（A1111 / NovelAI / ComfyUI 等），因此超过体积/尺寸限制被转成 JPEG 的图仍能取回提示词与画师串（留档只对开启之后入库的图生效）。见 [生图服务](docs/image-generation.md#生成参数留档)。
- **图片反推提示词**：内置 `image_to_prompt`，把参考图交给视觉模型反推绘图 prompt，带指纹缓存与并发合并；provider + model 在「工具权限 → 工具参数（全局）→ 图片反推提示词」配置（「功能」页显示 未配置 / 已启用 / 已停用 三态，双击直达该面板）。见 [配置说明](docs/configuration.md#图片反推提示词-image_to_prompt)。
- **视觉兜底**：主模型为纯文本、上游拒绝图片时，先用视觉模型转写图片再重试；结构化判定触发条件，不会把普通错误误判成「不支持视觉」。
- **语音转写（可选）**：用已有 Provider 的 `/audio/transcriptions` 把被唤醒的语音消息转成文本，再进入聊天与工具流程。
- **定时报告与会话总结**：向超级管理员推送生图量、Token、费用、磁盘与内存统计；每天把当天会话汇总成 `<数据目录>/daily_summaries/<日期>.json`（可选 LLM 二次摘要）。见 [定时报告](docs/reports.md)。
- **定时维护清理**：日志、会话、沙盒、聊天历史、隐私数据与内置 DSH 内核会话历史的保留期清理，cron 表达式可配，也支持 `run_at` 指定日期一次性执行（见 [配置说明](docs/configuration.md)）。

### 群聊能力

面向群机器人场景的确定性能力，与 LLM 调用分离：

- 群级权限与群策略、工具白名单、模型别名授权。
- 群知识库 / FAQ：命中后服务端直接答复，不创建 Session、不调用 LLM。
- 群内提醒 / 投票 / 报名：本地调度，到点通过正常输出层发送。
- 群会话线程模式与连续消息合并窗口；机器人被禁言/踢出后的群运行状态机。
- 入站消息去重与有界预处理、额度账本、限速与请求排队。

配置与命令见 [配置说明](docs/configuration.md) 与 [命令速查](docs/commands.md)。

### 安全与可观测

- **统一时间服务（1.0.10）**：业务时间只有一个来源（系统时钟）与一个时区口径（`[time].timezone`，默认 `Asia/Shanghai`，**全量 IANA 时区随包交付**）。每轮请求把"当前时间"注入系统提示（工具续轮只有一份），并区分「收到消息的时刻」与「处理时刻」——跨午夜、`23:59 收到 / 00:01 处理` 不再需要猜。可选的多来源网络校验（SNTP / HTTPS 时间 API，**默认关闭**）只做观测、只写诊断，**绝不修改系统时钟**，也不会把结果说成"已校准"；状态只进 `/diagnostics`，不进 `/ready`。超级管理员可用 `/time` / `/time verify` / `/time zones`；桌面端「时间」选项卡是同一套配置与状态的图形入口（实时读数、时区探针、全部可调项、来源表格、后台校验状态）。详见 [配置说明](docs/configuration.md#时间服务-time10-新增)。
- **工具安全**：风险分级、角色权限、高风险确认；Shell 执行受 clean-room 自研的 AST 级解析约束（`atri_x/shellparse/`）。
- **内容安全**：内置「道德限制 + 敏感信息过滤」（8 类词表拦截、凭据/PII 脱敏、只存 sha256 的审计日志、图片/文件出口策略），默认关闭；支持 **chat / work 两种会话模式各自覆盖**（`[safety.chat]` / `[safety.work]`，没写的键继承全局）。GUI 入口：**「工具权限」→「工具参数（全局）」→「全局参数」/「内容安全：chat/work 模式覆盖」面板**，chat / work 两个子选项卡里也各有一份覆盖面板；也可直接改 `app.toml` 的 `[safety]` 段，详见 [docs/safety.md](docs/safety.md)。
- **密钥保护**：Shell / Skill 子进程会移除名字含 `KEY`、`TOKEN`、`SECRET` 等词段的凭据变量。
- **审计与日志**：运行日志、Atriwisp 日志与审计日志分离，支持结构化字段与查询。
- **健康接口**：`/live`、`/ready`、`/healthz`、`/tasks`、`/metrics`、`/diagnostics`，语义边界明确（见「部署与运维」）。
- **稳定性机制**：限速、有界队列、运行超时、模型熔断与备用 Provider、磁盘保护。
- **自愈与验收**：分级 watchdog、`python -m atri_x doctor` 部署验收、备份/恢复验证/升级/回滚脚本。

## 快速开始

### 环境要求

- Python 3.11 或更新版本（开发与验证在 3.14.7 上完成）。
- 一个 OpenAI-compatible LLM 服务（如 DeepSeek、OpenAI 或其他兼容 `/chat/completions` 的服务）与至少一个 API Key。
- 不需要安装任何第三方包，不需要构建。

### 获取并启动

```bash
git clone <本仓库地址>
cd <仓库目录>
python -m atri_x
```

首次运行会在**平台配置目录**生成默认配置（Windows `%APPDATA%\ATRI-X`，Linux 使用 XDG 配置目录；已有文件不会被覆盖），包括但不限于：

| 文件 | 作用 |
| --- | --- |
| `app.toml` | 主配置入口：行为、平台、工具、安全、维护等 |
| `services.toml` | 共享只读服务配置：`[providers.*]`、模型元数据、生图、视觉、语音等 |
| `state.toml` | 运行态状态（默认模式与模型选择），由程序回写 |
| `atrinis.toml` | Atrinis 事件入口配置（默认不启用） |
| `SOUL.md` | Agent 的 System Prompt 来源 |
| `.env.example` | 环境变量模板；复制为 `.env` 后填密钥 |

配置 Provider 与密钥：

1. 在 `services.toml` 的 `[providers.*]` 中配置 Provider（默认已含 DeepSeek / OpenAI 示例），`api_key_env` 写环境变量名。
2. 把密钥放进系统环境变量，或复制 `.env.example` 为同目录 `.env` 后填写。
3. 在 `state.toml` 选择默认模式和模型，或启动后用 `/models`、`/model` 设置。

完整步骤见 [快速开始](docs/getting-started.md)，配置项见 [配置说明](docs/configuration.md)。

### 运行模式

```bash
python -m atri_x                 # 自动模式：有常驻服务则只起 CLI，否则完整前台
python -m atri_x run             # 完整前台：本地 CLI + 已启用平台 + Cron
python -m atri_x cli             # 本地 CLI-only：只起 CLI，不启动平台和 Cron
python -m atri_x service run     # headless 服务模式：不启动 CLI，启动平台和 Cron
python -m atri_x --config path/to/app.toml   # 使用指定主配置
```

其他子命令：

```bash
python -m atri_x config check              # 加载配置并打印摘要与 warning，错误时非零退出
python -m atri_x doctor [--json] [--e2e]   # 部署验收（见「部署与运维」）
python -m atri_x completion bash|zsh|fish|nushell|powershell|auto   # 生成 shell 补全
python -m atri_x --version                 # 版本
```

### 第一次对话

启动后直接输入消息即可。常用起步命令：

```text
/help      # 命令列表
/status    # 当前 Session、模型、上下文与请求状态
/models    # 可用模型
/model 1   # 切换模型
/chat      # 低成本聊天模式
/work      # 可使用工具的工作模式
```

完整命令见 [命令速查](docs/commands.md)。

## 发行版与容器镜像

当前最新版本：**`1.1.3`**（正式发行版）：新增**科研研究助理**——随包分发的研究引擎（`research_pack/`，38 个模块约 8.7k 行、纯标准库）与 15 个原创 AgentSkill（12 个 `risk="low"` 硬工具 + 3 个协议技能），把「检索 → 筛选 → 核验 → 大纲 → 引用 → 写作 → 质检 → 评审 → 导出」做成确定性流水线：检索去重、PRISMA 计数、DOI 反查（Crossref / DataCite）、APA / GB-T 7714 引用、正文结构契约、六维固定公式评分全部由引擎代码完成，模型只负责判断与表达，**换 LLM 时流程与判定标准不变**。首次启动自动把引擎与技能装进配置目录（幂等、按内容摘要升级、尊重 `atri-research/install.py` 的 venv 版外部安装），**安装版 / 便携版开箱即用、不需要系统 Python**；源码树用户按 [docs/research.md](docs/research.md) 安装。新增 `/research` 只读命令（`status` / `<运行id>` / `env` / `open`，普通成员可用）与桌面端第 13 个选项卡「研究」（运行列表 + 产物树 + 打开报告/导出目录）。**1.1.3 修订（同日）**补齐人设卡「导入 / 启用」：聊天里 `character_import` 一步把粘贴文本建卡、`persona_control` 让成员自助开关**专属人设**（按人持久、只影响本人、关闭恢复默认 `SOUL.md`）、`character_activate` 把角色卡设为全局人设并可回滚（仅超管）；桌面端「人设卡」页新增导入文件 / 粘贴导入 / 导出（ZIP 含图片）与 SOUL 从卡载入，新增第 14 页「权限」登记各平台超级管理员（QQ 填 QQ 号）并调工具轮次 / 成员建卡开关；另加**私聊回声保护**：`[自动回复]` 等客户端自动应答与同一人连续复读完全相同文本不再唤醒模型，避免与对方的自动回复互相刷屏；**上线通知**从 `hooks.toml` 搬到 `app.toml [connect_notice]`，私聊（超管）与群聊（`groups` 里的 QQ 群号）变成两个独立开关，桌面端「在线」页可直接勾选。新增 `docs/research.md` 与 `research_pack/README.md`；引擎与技能为原创实现，不含上游 `academic-research-skills` 的任何文本或代码（原创性核查见发行说明与 `LEGAL.md`）。1.1.2 的群友人设接入与 1.1.1 的两处修复全部保留；**不改现有配置格式 / 工具 schema / 会话与数据库格式**，配置与会话数据**向后兼容**；完整变更见 [CHANGELOG.md](CHANGELOG.md)；
交付物：`ATRI-X-1.1.3-setup.exe` 455.5 MB、`ATRI-X-1.1.3-portable-win64.zip` 671.6 MB、源码归档 `ATRI-X-1.1.3-repo.zip` / `.tar.gz`（1229 条目）；尺寸与 SHA-256 见发行库 `SHA256SUMS-1.1.3.txt`（安装包 / 便携版 / 源码归档）、发行说明与自检报告；本版已发布：[GitHub Release v1.1.3](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.1.3)（tag `v1.1.3`，提交 `139aaf2`；11 个附件含四件产物、`SHA256SUMS.txt` 与文档）。

已线上发布的历史版本：[**GitHub Release v1.1.1**](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.1.1)（当前线上版，tag `v1.1.1` 指向 1.1.1 发行提交）；上一版 [v1.1.0](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.1.0)（tag `v1.1.0`）；更早的 [v1.0.10](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.0.10)、[v1.0.9](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.0.9)、[v1.0.8](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.0.8)。各版本的尺寸与 SHA-256 见对应 Release 的 `SHA256SUMS.txt` 与兼容性报告（`ATRI-X-<版本>-compatibility-report.md`）。

| 产物 | 位置 |
| --- | --- |
| Windows 安装包 | [`ATRI-X-<版本>-setup.exe`](https://github.com/fujiwarashigure/ATRI-X2/releases)：用户级安装（不弹 UAC），含桌面端、托盘、开机自启、内置 DSH 内核与内置 QQ 协议端 |
| Windows 便携版 | `ATRI-X-<版本>-portable-win64.zip`：解压即用（绿色版），内置启动器与 `README.txt`；便携模式下配置/会话/日志都留在解压目录的 `data\` 里，整个文件夹可整体拷走排查问题（构建见 [`packaging/README.md`](packaging/README.md) §8） |
| 容器镜像 | [`ghcr.io/fujiwarashigure/atri-x2`](https://github.com/fujiwarashigure/ATRI-X2/pkgs/container/atri-x2)：`:<版本>` 与 `:latest`，由 [`.github/workflows/release-image.yml`](.github/workflows/release-image.yml) 在打 tag 时构建并回拉冒烟 |
| 源码归档 | Release 里另有 `ATRI-X-<版本>-repo.zip` / `ATRI-X-<版本>-repo.tar.gz`（完整仓库，含文档与测试）、`ATRI-X-<版本>-repo-entries.txt`（条目清单）、`SHA256SUMS.txt`，以及发行说明 / 自检报告 / 第三方组件说明 / 兼容性报告 |

```bash
docker pull ghcr.io/fujiwarashigure/atri-x2:latest
# 具体版本 tag（:<版本>）与 Release 一一对应；包若为 private，先 docker login ghcr.io（用户名 + PAT，至少 read:packages）

# 用 compose 时把镜像覆盖成 ghcr 地址；纯 docker run 的参数见 deploy/README.md
cd deploy
ATRIX_IMAGE=ghcr.io/fujiwarashigure/atri-x2:latest docker compose up -d
```

> Windows 包内已内置 DSH 内核（Node 与 `@deepseek-ai/dsh`），目标机不需要另装 Node；容器镜像同样在
> 构建期装好 DSH。安装/卸载、接 QQ 的完整步骤见 [Windows 桌面端](docs/windows-app.md)。
>
> **QQ 协议端可切换，默认 NapCat**：`qqonebot` 平台默认使用**发行包内置的 NapCat**
> （离线/开箱即用，装好即可连）；**SnowLuma 是可选的备用协议端**，需自行安装后外置接入
> （发行包不内置，因为它的 EULA §5.4 要求"并入第三方安装包"须事先书面授权）。
> 用哪个由 `[platform.qqonebot].protocol_backend`（`napcat` / `snowluma`）决定；**整个
> `[platform.qqonebot]` 段不存在 = 新装 = NapCat**，段存在但键缺失 = 1.0.x 老配置 =
> NapCat——升级不会自动切换协议端，只有你在桌面端显式选择 `snowluma` 才会切过去。
> 切换也不改 `ws_url` 与令牌，但要重启桌面端让协议端环境重新准备。桌面端「平台」页和
> 「在线」页把"进程运行 / OneBot 连通 / QQ 已登录 / ATRI 接入"分成四个状态显示，端口在听
> 不会被当成"QQ 已登录"；SnowLuma 没装时「在线」页会提示"本程序包内置 NapCat，可在
> 「平台」页切回去用"。SnowLuma 的完整安装/首启/注入/OneBot 端点配置与
> `ATRIX_SNOWLUMA_DIR` 探测规则见 [SnowLuma 使用与配置教程](docs/snowluma.md)（独立成篇，可单独转发）。

## 部署与运维

仓库自带完整的单机部署套件（全部为纯标准库 Python），入口文档是 [deploy/README.md](deploy/README.md)。

| 方式 | 入口 |
| --- | --- |
| Docker Compose | [`deploy/docker-compose.yml`](deploy/docker-compose.yml)、[`deploy/Dockerfile`](deploy/Dockerfile)（单阶段 `python:3.14-slim`，非 root `10001:10001`） |
| 离线 / 预构建镜像 | [`deploy/pack/README-OFFLINE.md`](deploy/pack/README-OFFLINE.md) |
| systemd（原生部署） | [`deploy/atri-x.service`](deploy/atri-x.service)、[`deploy/atri-x-compose.service`](deploy/atri-x-compose.service) |
| Windows 本地 Docker Desktop | [`deploy/windows/README.md`](deploy/windows/README.md) |
| Portainer 浏览器运维 | [`deploy/portainer/portainer-compose.yml`](deploy/portainer/portainer-compose.yml) |

快速起容器：

```bash
cd deploy
cp .env.example .env    # 填 API Key / token
docker compose build
docker compose up -d
```

镜像校验、冒烟与稳定性长跑：

```bash
python deploy/smoke.py                        # 构建 → 起容器 → 断言 → JSON 报告 → 自动清理
python deploy/stability.py --duration 300     # 长跑采样：健康失败率、RSS 趋势、重启与 Traceback
```

说明见 [deploy/TESTING.md](deploy/TESTING.md)。

### 端口与健康接口

| 端口 | 用途 |
| --- | --- |
| `32171` | 独立健康接口（`/live`、`/ready`、`/healthz`、`/tasks`、`/metrics`、`/diagnostics`） |
| `32172` | CLI 远程 WebSocket 服务端（`app.toml` 中启用时） |
| `32170` | Atrinis 事件 HTTP 入口（`atrinis.toml` 中启用时；默认关闭） |

`/live` 只看进程是否还能响应；`/ready` 还要求初始化完成、数据目录可写、调度心跳未过期；`/healthz` 汇总状态并在平台/模型异常时显示 `degraded`。`/tasks`、`/metrics`、`/diagnostics`、`/healthz` 需要配置 `ATRIX_OPS_TOKEN` 才注册，未配置时只保留 `/live` 与 `/ready`。容器镜像的 `HEALTHCHECK` 优先用应用内置的健康探测（`python -m atri_x --health-probe`），不依赖 `curl`。

### 验收、备份与自愈

```bash
python -m atri_x doctor [--json] [--e2e] [--require-platform]   # 配置/存储/端口/平台/模型验收
python deploy/health_probe.py --ready                           # 独立健康探针
python deploy/backup.py                                         # 一致性备份 + 恢复验证
python deploy/restore_verify.py backups/<归档>.tar.gz           # 隔离目录恢复演练
python deploy/upgrade.py                                        # 升级（含数据快照与回滚准备）
python deploy/rollback.py                                       # 回滚到上一版
python deploy/atri_x_watchdog.py                                # 外部 watchdog（配合 systemd timer）
```

- watchdog 只依据 `/live` 判断是否需要处置，按阈值、冷却与窗口限制重启次数，超限后暂停并等待人工恢复；诊断包会就地脱敏。
- 备份默认走 SQLite 在线一致性快照，不中断服务；升级/回滚的每一步失败都会中止，不留「半升级」状态。

细节见 [deploy/README.md](deploy/README.md) 的第 5～7 节。

## 文档

| 文档 | 内容 |
| --- | --- |
| [LEGAL.md](LEGAL.md) | **法务与合规注意事项**：使用边界、法律法规与平台 / 第三方协议义务、第三方组件与商标、AS IS 免责与责任限制（**使用前请先读**） |
| [docs/getting-started.md](docs/getting-started.md) | 从配置 API Key 到第一次对话 |
| [docs/configuration.md](docs/configuration.md) | 配置文件、路径规则、Provider、环境变量 |
| [docs/commands.md](docs/commands.md) | 全部 slash 命令速查 |
| [docs/concepts.md](docs/concepts.md) | Chat / Work、工具发现、Session、Hook、Cron、安全策略 |
| [docs/hooks.md](docs/hooks.md) | Hook 配置、规则/进程/Worker 插件与编程接口 |
| [docs/atrinis.md](docs/atrinis.md)、[docs/atrinis-usage.md](docs/atrinis-usage.md) | 事件监听枢纽与接入 |
| [docs/atriyph.md](docs/atriyph.md) | Atriyph 任务表示法 |
| [docs/character-library.md](docs/character-library.md) | 角色素材库 |
| [docs/image-generation.md](docs/image-generation.md) | 生图服务与提示词优化 |
| [docs/vision.md](docs/vision.md) | 识图能力：图片精度、两段式描述、以图搜图兜底与隐私开关 |
| [docs/safety.md](docs/safety.md) | 内容安全：道德限制词表、敏感信息脱敏、审计与出口策略 |
| [docs/reports.md](docs/reports.md) | 定时报告 |
| [docs/research.md](docs/research.md) | 科研研究助理：12 个硬工具、`/research` 命令、「研究」选项卡、数据布局与排障（1.1.3） |
| [docs/frontend-api.md](docs/frontend-api.md) | 远程 CLI / 自定义前端协议 |
| [docs/windows-app.md](docs/windows-app.md) | Windows 桌面端、托盘/自启、安装包与接 QQ |
| [docs/snowluma.md](docs/snowluma.md) | SnowLuma（**可选的备用 QQ 协议端**，默认协议端是内置 NapCat）安装、WebUI 首启、进程注入、OneBot 端点与 ATRI-X 接入教程 |
| [deploy/README.md](deploy/README.md) | 部署与运维总览 |
| [deploy/TESTING.md](deploy/TESTING.md) | 镜像冒烟与稳定性测试 |
| [deploy/windows/README.md](deploy/windows/README.md) | Windows 本地容器部署 |
| [deploy/pack/README-OFFLINE.md](deploy/pack/README-OFFLINE.md) | 离线 / 预构建部署 |
| [PORTING.md](PORTING.md) | Go → Python 移植契约与包级映射（§4） |
| [AGENTS.md](AGENTS.md) | Agent / 开发者在仓库内的操作手册 |
| [devdocs/](devdocs/) | 架构、代码地图、维护任务、移植状态 |
| [verification/](verification/) | 独立对抗性验证报告与已知局限 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |

## 仓库结构

```text
atri_x/                  Python 包（平铺在仓库根下，不安装）
  __main__.py            程序入口（python -m atri_x）
  launcher/              命令行解析与 shell 补全
  app/                   分阶段装配（Environment → Foundation → Models → Platforms → Runtime → Integrations）
  agent/                 对话主流程、上下文、预算、模型状态与 slash 命令（commands/）
  tool/                  工具运行时、内置工具（builtin/）、Skill（skill/）
  toolrun/               工具视图、命名解析、风险确认与执行前授权复核
  llm/                   LLM 抽象与 OpenAI Chat Completions / Responses 适配器
  platform/              cli（TUI 与远程客户端）、telegram、qq_onebot、qqofficial、headless
  hook/                  事件、规则 Hook、持久 Hook 进程与 hook.v2 协议
  session/ contextmgr/   会话与上下文压缩
  memory/ angelmemory/   常驻记忆与长期记忆
  storage/               领域模型 + SQLite 实现（migration、repository、媒体引用与清理）
  cron/ maintenance/ background/   定时任务、维护任务与后台任务
  atrinis/ atriena/ atriyph/       事件监听枢纽、事件协议、任务记法
  character/ imagegen/ vision/ artist/ asr/   角色、生图、视觉、画师串、语音转写
  selflearning/ groupanalysis/ groupkb/       自主学习与群分析、群知识库
  research/              科研研究助理的随包引擎装配（pack 定位 / 首次启动引导 / 子进程入口）
  health/ ops/ security/ redact/ logging/ signal/  健康、限流熔断、安全、脱敏、日志与信号
  net/ shellparse/ media/imagecodec/ utils/runes.py   补齐 Python 标准库缺口
research_pack/           随包研究引擎（engine/）、15 个 AgentSkill（skills/）与清单数据（pack.json）
deploy/                  部署套件（Docker、systemd、Windows、离线包、备份升级、watchdog）
tests/                   unittest 用例
docs/                    用户文档        devdocs/  维护与架构文档
scripts/                 验收与体检脚本  verification/  对抗性验证报告
PORTING.md  AGENTS.md  CHANGELOG.md  LICENSE  pyproject.toml
```

- 布局是**平铺**的：仓库根下直接是 `atri_x/`，不安装、不做 src-layout；在别的目录调用需先 `cd` 到仓库根或设置 `PYTHONPATH`。
- `pyproject.toml` 只承载项目元数据（名称、版本、`requires-python`、可选的 `atri-x` 入口声明）；本仓库的推荐运行方式始终是仓库根目录下的 `python -m atri_x`。

## 开发与验证

### 质量门禁

```bash
python scripts/check_syntax.py        # 语法 + BOM + CRLF + U+FFFD + mojibake
python scripts/check_imports.py       # 逐个导入 atri_x 下每个模块，校验 __all__
python scripts/check_integration.py   # 跨模块唯一出处、ctx 键、脱敏契约、ID 契约、async 误用
python scripts/check_coverage.py --strict --summary   # 按 PORTING.md §4 映射表算**文件存在性**加权覆盖
python scripts/check_except.py        # except BaseException 且不 re-raise 的位置基线（文件 + 函数 + 指纹）
python scripts/gen_stats.py --check   # README 静态数字与工作树一致
python scripts/check_secrets.py       # 密钥 / 令牌 / 本机身份扫描
python -m unittest discover -s tests -t .             # 全量测试
```

最近一次在 `Windows 10 + Python 3.14.7` 上的结果（文件计数与模块计数由 `python scripts/gen_stats.py` 从工作树静态统计，可随时复算）：

| 门禁 | 结果 |
| --- | --- |
| `check_syntax` | 1012 个文件，0 错（`bom=0`、`crlf=0`、`damaged=0`） |
| `check_imports` | `modules=777 imported=777`，0 失败 |
| `check_integration` | 74 项检查，0 失败 |
| `check_coverage --strict` | `packages=72 covered=72 missing=0 weighted=100.0%`（**文件存在性**口径：只验 [PORTING.md](PORTING.md) §4 映射表里的 Python 模块是否存在，不衡量行为正确性） |
| `check_except` | `except_baseexception_no_raise=119`，与基线一致（0 个新增位置） |
| `gen_stats --check` | README 里的静态数字与工作树一致 |
| `check_secrets` | 0 命中（扫描范围含未跟踪文件） |
| 全量 `unittest` | 用例数不硬编码（见下）；现场跑 `python -m unittest discover -s tests -t .`，以本机输出或 CI 记录为准 |

> **数字口径与复算方式**：上表的文件/模块计数由 `python scripts/gen_stats.py` 只读扫描工作树得出
> （扫描口径与 `check_syntax.py`、`check_imports.py` 共用同一份实现）；`python scripts/gen_stats.py --check`
> 会在 README 数字与工作树不一致时以退出码 1 打印差异，`--update` 打印可粘贴的正确数字。
> **用例数不在本 README 硬编码**：unittest 的用例数只有真跑一遍才知道（参数化、动态生成、解释器版本都会影响它），
> 而且测试夹具依赖宿主机条件（有 Docker、上游 Go 仓库在同级目录等）时会 skip，
> 所以 README 不把它当成可复现的事实；需要时由 CI 记录或现场运行。

### 对抗性验证

[verification/](verification/) 记录了独立验证者以上游 Go 源码为唯一真相做的逐包核对，以及真实进程 + 真实 HTTP 请求的边界验证：四道门禁全绿、全量测试通过、Docker 冒烟 13/13、300 秒容器稳定性长跑 0/50 采样失败。验证同时登记了**仍然未验证**的部分，例如：

- 未连接真实平台服务端（QQ 官方 / OneBot / Telegram 只有进程内假连接用例）；
- 未调用真实模型 API（LLM 路径使用假 client / 假 SSE 流）；
- TUI 未在真实 TTY 中逐项体验（有 headless 用例）；
- 稳定性长跑只覆盖最小配置（无平台、无模型、无持续消息）；
- 开发机没有 Go 工具链，Go 侧结论均来自源码行号级对照。

> **「全绿」是绑定环境的事实**：那几轮验证跑在做者机器上（装了 Docker、上游 Go 仓库在同级目录），
> 而当时的夹具对这两者有隐式假设——缺 Docker 时 `deploy/` 的升级/回滚用例会因为
> `compose_command()` 探测不到 docker 而失败，缺上游时提示词库比对会失败；README 却把这种「绿」
> 当成了可移植的事实。现在这两处已改成不依赖环境：`deploy/` 用例用**空壳 docker/git + 伪造命令表**
> 驱动（真正要跑的命令先命中伪造表，不会被真的执行），提示词库比对在上游不可用时明确 `skip` 并写出原因——
> 「没法验证」不再被当成「通过」。跨环境结论请以你自己机器上的 `python -m unittest discover -s tests -t .` 输出为准。

### 性能数据说明

本仓库**不提供官方性能基准**。旧文档中的「N5105 上 <10 ms 启动、约 30 MB 常驻内存」是 Go 版 v0.5.0 的历史参考数据，无法代表当前 Python 版（版本已到 1.0.2，且新增了健康接口、诊断、素材清单、`doctor` 等组件），因此不在本 README 中保留。启动与资源占用取决于配置、模型调用、生图与工具子进程、并发规模，需要时请在自己的目标机器上实测，例如：

```bash
python -m atri_x --version
# Linux：估算配置加载的耗时与峰值内存
/usr/bin/time -v python -m atri_x config check 2>&1 | grep -E 'Elapsed|Maximum resident'
# Docker：观察容器常驻内存
docker stats --no-stream atri-x
```

### 开发约定

- 改动 Python 代码后至少跑一遍 `check_syntax.py` / `check_imports.py` / `check_integration.py`；只改 Markdown 时没有自动门禁，至少确认文件仍是 UTF-8（无 BOM、无 CRLF 混杂）且相对链接有效。
- 单文件目标 ≤ 600 行；每个模块在 docstring 中注明对应的 Go 文件；中文注释与文档是默认语言。
- 不要用 PowerShell 写文件（本机 PowerShell 5.1 会写 UTF-8 BOM 或按 GBK 二次编码），不要用 `tempfile.mkdtemp()`，不要在仓库根留调试产物。完整约定见 [AGENTS.md](AGENTS.md) 与 [PORTING.md](PORTING.md) §7。

## 与上游 Go 版的关系

本仓库是**逐包重写**而不是包装调用：上游每个生产包在 `atri_x/` 下都有对应模块，映射关系以 [PORTING.md](PORTING.md) §4 为唯一出处；哪些行为尚未逐条核对，登记在 [devdocs/porting-status.md](devdocs/porting-status.md)。以下差异是设计决定，不是遗漏：

| 项 | Go 版 | Python 版 |
| --- | --- | --- |
| 分发形态 | 单静态二进制，交叉编译多平台 | 需要 Python 解释器；仓库根目录直接运行，无构建产物 |
| shell AST | `mvdan.cc/sh/v3` | `atri_x/shellparse/`，**clean-room 自研**（唯一可信候选 `bashlex` 是 GPLv3+ 且已停更，与 Apache-2.0 冲突）；覆盖安全裁决所需语法，不是完整 POSIX shell |
| SQLite | `modernc.org/sqlite`（纯 Go） | 标准库 `sqlite3`（随 CPython 分发的 C 扩展） |
| WebSocket | `coder/websocket` | `atri_x/net/websocket.py`，在标准库 `socket` / `ssl` 上实现 RFC 6455 |
| TOML | `pelletier/go-toml/v2` | 读用标准库 `tomllib`；写用 `atri_x/net/tomlw.py` |
| S3 | `aws-sdk-go-v2` | `atri_x/net/s3.py`，SigV4 签名的最小 REST 客户端 |
| cron 表达式 | `robfig/cron/v3` | `atri_x/net/cronspec.py`，语义对齐 |
| TUI | `bubbletea` + `lipgloss` + `bubbles` | `atri_x/platform/cli/tui/`，ANSI + 原始模式输入；命令/状态栏/输入行为保留，cell 级渲染保真度低于上游 |
| 图像编解码 | 标准库 `image` / `jpeg` / `png` | `atri_x/media/imagecodec/`，纯 Python 编解码 + 可选 Pillow 加速；**不支持渐进式 JPEG**（明确抛异常） |
| 并发模型 | goroutine + channel | `asyncio` task + `asyncio.Queue`；同步路径（SQLite、文件操作）用 `threading` |
| Go Skill | 原生 | 保留支持，但编译仍需 Go 工具链（仅这类 Skill 需要；本仓库自身不需要 `go build`） |

另需如实说明：QQ 官方平台与 Telegram 目前只有进程内假连接用例，**没有用真实凭据与真实服务端做过端到端验证**；这部分与其他未验证项见 [verification/README.md](verification/README.md)。

## 许可

本项目使用 **Apache-2.0** 许可，与上游一致，见 [LICENSE](LICENSE)。

许可证只授予版权层面的使用 / 修改 / 分发许可，**不能免除或替代**你应遵守的法律法规、平台协议与第三方权利；使用前请一并阅读 [法务与合规注意事项](#法务与合规注意事项) 与 [LEGAL.md](LEGAL.md)。

上游 Go 源码不含 copyleft 依赖；本重写同样没有引入 copyleft 依赖——这也是 `atri_x/shellparse/` 选择 clean-room 自研、而不是引入 GPLv3 的 `bashlex` 的原因。唯一可能用到的第三方包是**可选的** Pillow（图像编解码加速，宽松许可），不安装时自动回退到纯 Python 实现。

## 开发状态

ATRI-X 仍在快速开发中，接口、配置和内部实现可能继续调整。当前版本更适合作为个人 Agent / 机器人框架探索使用；行为基准以上游 Go 版为准，跨语言细节差异以本仓库文档与验证报告为补充。
