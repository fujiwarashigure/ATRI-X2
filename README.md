# ATRI-X（Python 版）

**ATRI-X 的 Python 全量重写**：一个轻量、可扩展、多平台的 LLM Agent / Chatbot 守护进程。

上游是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本仓库对应 v0.9.0）。本仓库把上游 71 个生产包逐包重写为 Python（包名 `atri_x`），**纯标准库、零运行时第三方依赖**，在仓库根目录直接 `python -m atri_x` 运行——不需要安装，也没有构建产物。

支持普通聊天、工具调用、Hook 扩展、Cron 调度、Skill 沉淀、长期记忆、外部事件触发、多平台接入与容器化部署。

> **⚠️ 法务与合规提示**：本项目**仅供学习、技术研究与个人自托管实验**，**不是**腾讯 / QQ /
> Telegram / NapCat / SnowLuma / DeepSeek 等任何平台或服务商的官方产品，与其**无隶属、授权或背书
> 关系**。使用前请阅读下方 [法务与合规注意事项](#法务与合规注意事项) 与 [LEGAL.md](LEGAL.md)；按“现状”提供、风险自负。

| 项 | 值 |
| --- | --- |
| 版本 | `1.1.5`（对应上游 v0.9.0；Windows 安装包 / 便携版自 0.9.1 起提供） |
| 许可 | [Apache-2.0](LICENSE)（与上游一致） |
| Python | ≥ 3.11（开发与验证在 3.14.7 上完成） |
| 入口 | `python -m atri_x` |

## 法务与合规注意事项

> **一句话**：本项目仅供学习 / 研究 / 个人自托管实验，须遵守法律法规与平台协议；按“现状”提供，
> 风险自负。**完整条款见 [LEGAL.md](LEGAL.md)**。

- **非官方项目**：与腾讯 / QQ / Telegram / NapCat / SnowLuma / DeepSeek 及任何模型、搜索、生图、
  语音服务商**均无隶属、授权、赞助或背书关系**；接入平台用的是公开接口或非官方协议端，请自行评估
  账号风险。
- **遵守法律法规与平台协议**：包括《网络安全法》《数据安全法》《个人信息保护法》《生成式人工智能
  服务管理暂行办法》等，以及 QQ 用户协议 / 机器人规则、Telegram ToS、NapCat 许可、SnowLuma EULA、
  各 API 服务商条款。平台协议与本项目文档冲突时，**以平台协议与法律为准**。不得用于诈骗、赌博、
  色情、暴力、谣言、侵权、骚扰、人肉、群发广告、绕过风控等用途，不得侵害未成年人权益。
- **账号、凭据、费用与内容自负**：自备账号与 API Key；凭据泄露、封号、违规后果由使用者承担；测试
  请用独立小号，不要把真实 Key / 令牌 / 登录态提交到公开仓库、Issue 或群聊。AI 生成内容可能不准确
  或有偏见，发布时应按适用法律做深度合成标识；内置内容安全模块只是辅助，**不构成合规保证**。
- **第三方组件与商标**：Apache-2.0 只覆盖本项目代码，**不覆盖** NapCat、便携 QQ、Node.js / DSH 内核、
  draw.io 桌面版 / Electron / Chromium、drawio-skill、CPython、Pillow、SnowLuma 等第三方组件；
  相关名称归各自权利人，本项目仅作事实性指称。
- **免责**：按 **“现状”（AS IS）** 提供，不附带任何担保；作者与贡献者不对账号封禁、数据丢失、
  业务中断、第三方索赔、行政处罚等任何直接或间接损失负责。
- **举报**：发现违规使用或仓库内不应公开的敏感信息，请通过 GitHub Issue 联系维护者。

## 项目速览

| 项 | 说明 |
| --- | --- |
| 上游 | Go 版 `ATRI-X` v0.9.0：446 个生产 `.go` 文件 / 90,203 行 |
| 本仓库 | 平铺布局的 `atri_x/`：797 个 `.py` 模块，约 23.4 万行（物理行，含空行/注释/docstring；`tests/` 另有约 11.1 万行） |
| 研究引擎 | 随包 `research_pack/`：38 个模块、约 8.7k 行纯标准库引擎 + 15 个原创 AgentSkill；安装版 / 便携版首启自动装进配置目录 |
| 图表能力 | 随包 `drawio_pack/`（drawio-skill 3.4.2，47 个纯标准库脚本 + 20 篇参考 + shape 索引）+ 内置 draw.io 32.6.1 无头渲染器；工具 `drawio` 仅超管可用，安装版 / 便携版开箱出 PNG/SVG/PDF |
| 包级映射 | 上游 71 个生产包 + `cmd/atri-x`，共 72 条映射，全部有对应实现（[PORTING.md](PORTING.md) §4） |
| 平台 | CLI（本地 TUI / 远程客户端）、Telegram、QQ OneBot、QQ Official，另有 headless 适配 |
| 存储 | SQLite（标准库 `sqlite3`），会话、消息、工具调用、媒体引用与审计数据均持久化 |
| 测试 | `tests/` 229 个 Python 文件（其中 220 个 `test_*.py`）；用例数不硬编码（见「开发与验证」） |
| 门禁 | 代码 7 道 + 文档 1 道（`check_docs`）+ 索引 1 道（`gen_index --check`）+ 复杂度预算，全部可在 CI 跑 |
| 规模 | 1416 个受版本控制文件（不含 `runtime`/`build`/`dist`/缓存/本机私有配置） |

## 功能特性

### 对话与工具

- **Chat / Work 双模式**：两种模式可分别配置模型，让低成本模型负责闲聊、强模型专注复杂任务；两种模式都支持工具调用。
- **按需工具发现**：默认只暴露 `discover_tool` 与可用工具名，模型按需发现后再注入具体 schema；另可用 `@tool:` / `@skill:` 内联预载。
- **内置工具 55 个**（16 个无条件注册，其余按依赖条件注册）：文件、Shell、Web、Cron、Skill、媒体、记忆、时间、人设卡，以及 1.1.4 新增的**超管专属只读资源查询** `runtime_resources`（CPU / 内存 / 磁盘 / 显卡占用与 CPU/GPU 型号；只报型号与数值，不回显本机路径）。
- **联网搜索与网页抓取**：`web_search` 支持 DeepSeek 官方搜索 / Tavily / Firecrawl / Keenable / Exa / SearXNG / Antigravity / Grok 多通道自动降级；`web_extract` 支持 DeepSeek 官方抓取 / Jina Reader / 直接抓取。见 [配置说明](docs/configuration.md)。
- **风险与确认**：工具带风险等级，按角色/权限判断可用范围；高风险调用逐次确认。
- **单轮声明与命令前缀**：`@model:` / `@image:` / `@use:` 只对当前轮生效（仅超管）。

细节见 [核心概念](docs/concepts.md) 与 [命令速查](docs/commands.md)。

### 平台与前端

- **CLI**：本地 TUI（聊天区、通知区、滚动、copy mode、补全）；也可作为服务端，任意电脑用 CLI 客户端远程连接（默认 `32172`）。
- **Windows 桌面端**：Tk 窗口 + 托盘常驻 + 后台服务自动拉起，14 个选项卡覆盖模型 / 平台 / 人设卡 / 群友 / 知识库 / 时间 / 日志 / 工具权限 / 运行状态 / 研究等全部配置；密钥可显示隐藏，配置改动自动留备份，模型页保存后从磁盘回读逐字段校验。详见 [Windows 桌面端](docs/windows-app.md)。
- **Windows 两种交付形态**：安装包（开始菜单/自启/卸载项）与便携版 zip（解压即用，数据留在解压目录）；一条 `python packaging/build.py --release` 同时产出并各自自检。
- **即时通信平台**：Telegram、QQ OneBot、QQ Official，统一平台层与输出层，便于扩展。
- **前端自由定制**：远程 CLI 走 WebSocket，协议见 [前端 API](docs/frontend-api.md)。

### 会话、上下文与记忆

- **持久化 Session**：恢复、归档、置顶、Fork、删除、分页查看与平台隔离；回复历史消息可恢复或 Fork。
- **上下文压缩与预算**：长对话自动压缩；发送前做 token 预算检查；超长消息默认拒绝，群管理员可用 `/overflow` 切换 `truncate` / `summarize`。
- **记忆分层**：常驻记忆只保留每轮注入的短信息（core / normal），更长内容走长期记忆（Markdown + SQLite FTS，写入原子化）。
- **自主学习**：观察与候选先进 pending，管理员 review 通过后才注入上下文。

### 扩展机制

- **Hook**：Agent 输入、LLM 请求/响应、平台发送与连接等事件点可插逻辑；规则 / 进程 / Worker Hook，**插件可用任意语言**。见 [Hook](docs/hooks.md)。
- **Cron**：普通 Cron 发固定内容；LLM Cron 用任务描述驱动模型执行。
- **Skill**：原生 Skill（Atriyph 记法）、Go 源码 Skill、agentskills.io 风格外置 AgentSkill；内置 `create_atrix_skill` 让模型沉淀可复用经验。
- **Atrinis / Atriwisp / Atriena**：外部事件（告警、RSS、Webhook）经 Atrinis 触发 Agent。见 [Atrinis](docs/atrinis.md)。

### 科研研究助理

把「查文献 → 筛文献 → 核验引文 → 写稿 → 评审 → 导出」做成可复用硬工具：12 个确定性硬工具 + 3 个协议技能，检索去重、PRISMA 计数、DOI 反查、引用格式化、六维评分**全部由 Python 引擎完成**，换模型时流程与判定标准不变。随安装包分发、首启自动安装、普通成员可用。见 [docs/research.md](docs/research.md)。

### 图表能力（draw.io）

把代码目录、Terraform、SQL、OpenAPI、AsyncAPI、Proto、GraphQL、K8s、Compose、CI 配置，或模型手写的 Diagram IR / draw.io XML 转成**可编辑** `.drawio`，并用**随包内置的 draw.io 32.6.1** 无头渲染成 PNG / SVG / PDF 直接发到聊天。工具 `drawio`（`superadmin_only`）一个入口覆盖生成 / 校验 / 导出 / 摘要 / 查询 / 形状检索 / 参考文档 / 自检；渲染在无窗口模式下运行，用户不需要安装 draw.io，也不读写宿主机 draw.io 配置。**仅超级管理员可用**。见 [docs/diagram.md](docs/diagram.md)。

### 角色、生图与多模态

- **角色素材库**：文件夹管理角色文本与图片，支持索引、检索、`@char:<id>` 临时启用与 `character_*` 工具。见 [角色素材库](docs/character-library.md)。
- **图像生成**：对接 OpenAI 兼容 `/images/generations`，全局预设 + 角色预设 + 场景描述组装提示词，支持规则优化、画师串、多角色同框、并发排队；开启留档后入库转码前保存原图生成参数。见 [生图服务](docs/image-generation.md)。
- **图片反推提示词**：`image_to_prompt` 把参考图交给视觉模型反推 prompt，带指纹缓存与并发合并。
- **视觉兜底与语音转写**：主模型拒绝图片时先转写再重试；可选把语音消息转文本后进入聊天。见 [识图](docs/vision.md)。

### 群聊能力

群级权限与策略、工具白名单、模型别名授权；群知识库 / FAQ 命中后直接答复（不建 Session、不调 LLM）；群内提醒 / 投票 / 报名本地调度；群会话线程模式与连续消息合并；入站去重、额度账本、限速与排队。见 [配置说明](docs/configuration.md) 与 [命令速查](docs/commands.md)。

### 安全与可观测

- **统一时间服务**：业务时间只有一个来源与一个时区口径（`[time].timezone`，全量 IANA 时区随包交付）；可选的多来源网络校验只做观测、**绝不修改系统时钟**。见 [配置说明](docs/configuration.md#时间服务-time10-新增)。
- **工具安全**：风险分级、角色权限、高风险确认；Shell 执行受 clean-room 自研的 AST 级解析约束（`atri_x/shellparse/`）。
- **内容安全**：道德限制词表 + 敏感信息脱敏 + 出口策略，默认关闭，支持 chat / work 双模式覆盖。见 [docs/safety.md](docs/safety.md)。
- **密钥保护 / 审计日志 / 健康接口**：子进程剥离凭据变量；运行、审计、Atriwisp 日志分离；`/live`、`/ready`、`/healthz`、`/tasks`、`/metrics`、`/diagnostics`。
- **稳定性**：限速、有界队列、运行超时、模型熔断与备用 Provider、磁盘保护、分级 watchdog 与 `doctor` 验收。

## 快速开始

### 环境要求

- Python 3.11 或更新版本（开发与验证在 3.14.7 上完成）。
- 一个 OpenAI-compatible LLM 服务（如 DeepSeek、OpenAI 或其它兼容 `/chat/completions` 的服务）与至少一个 API Key。
- 不需要安装任何第三方包，不需要构建。

### 获取并启动

```bash
git clone <本仓库地址>
cd <仓库目录>
python -m atri_x
```

首次运行会在**平台配置目录**生成默认配置（Windows `%APPDATA%\ATRI-X`，Linux 用 XDG 配置目录；已有文件不会被覆盖）：

| 文件 | 作用 |
| --- | --- |
| `app.toml` | 主配置入口：行为、平台、工具、安全、维护等 |
| `services.toml` | 共享只读服务配置：Provider、模型元数据、生图、视觉、语音等 |
| `state.toml` | 运行态状态（默认模式与模型选择），由程序回写 |
| `atrinis.toml` | Atrinis 事件入口配置（默认不启用） |
| `SOUL.md` | Agent 的 System Prompt 来源 |
| `.env.example` | 环境变量模板；复制为 `.env` 后填密钥 |

配置 Provider 与密钥：在 `services.toml` 的 `[providers.*]` 里配 Provider（`api_key_env` 写环境变量名），把密钥放进环境变量或 `.env`，再用 `/models`、`/model` 选模型。完整步骤见 [快速开始](docs/getting-started.md)。

### 运行模式

```bash
python -m atri_x                 # 自动模式：有常驻服务则只起 CLI，否则完整前台
python -m atri_x run             # 完整前台：本地 CLI + 已启用平台 + Cron
python -m atri_x cli             # 本地 CLI-only：只起 CLI
python -m atri_x service run     # headless 服务模式：不起 CLI，起平台和 Cron
python -m atri_x --config path/to/app.toml   # 指定主配置
```

其他子命令：`config check`（加载配置并打印摘要与 warning）、`doctor [--json] [--e2e]`（部署验收）、
`completion bash|zsh|fish|nushell|powershell`（shell 补全）、`--version`。

### 第一次对话

```text
/help      # 命令列表
/status    # 当前 Session、模型、上下文与请求状态
/models    # 可用模型
/model 1   # 切换模型
/chat      # 低成本聊天模式
/work      # 可使用工具的工作模式
```

完整命令见 [命令速查](docs/commands.md)。

## 发行版、容器镜像与部署

当前版本 **`1.1.5`**（正式发行版）：新增**内置 draw.io 图表能力**——超管专属工具 `drawio`
把代码目录 / Terraform / SQL / OpenAPI / AsyncAPI / Proto / GraphQL / K8s / Compose / CI
配置或 Diagram IR 转成**可编辑** `.drawio`，并用随包内置的 draw.io 32.6.1 无头渲染成
PNG / SVG / PDF 直接发到聊天；安装版 / 便携版开箱即用，**不需要用户安装 draw.io**。
详见 [`docs/release-notes/1.1.5.md`](docs/release-notes/1.1.5.md)、[docs/diagram.md](docs/diagram.md)
与 [CHANGELOG.md](CHANGELOG.md)；上一版（超管资源查询 + 可维护性/导航体系）见
[`docs/release-notes/1.1.4.md`](docs/release-notes/1.1.4.md)。
历史版发行说明见 [`docs/release-notes/`](docs/release-notes/)。

| 产物 | 位置 |
| --- | --- |
| Windows 安装包 | `ATRI-X-<版本>-setup.exe`：用户级安装（不弹 UAC），含桌面端、托盘、开机自启、内置 DSH 内核与内置 QQ 协议端 |
| Windows 便携版 | `ATRI-X-<版本>-portable-win64.zip`：解压即用；便携模式下配置/会话/日志都留在解压目录 `data\` 里，整个文件夹可拷走排查（构建见 [`packaging/README.md`](packaging/README.md) §8） |
| 源码归档 | `ATRI-X-<版本>-repo.zip` / `.tar.gz`、`-repo-entries.txt`、`SHA256SUMS.txt`，以及发行说明 / 自检报告 / 兼容性报告 |
| 容器镜像 | `ghcr.io/fujiwarashigure/atri-x2`：`:<版本>` 与 `:latest`，由 [`.github/workflows/release-image.yml`](.github/workflows/release-image.yml) 在打 tag 时构建并回拉冒烟 |

```bash
docker pull ghcr.io/fujiwarashigure/atri-x2:latest
cd deploy && ATRIX_IMAGE=ghcr.io/fujiwarashigure/atri-x2:latest docker compose up -d
```

> Windows 包内已内置 DSH 内核（Node 与 `@deepseek-ai/dsh`），目标机不需要另装 Node；容器镜像同样在
> 构建期装好。安装 / 卸载 / 接 QQ 的完整步骤见 [Windows 桌面端](docs/windows-app.md)。
>
> **QQ 协议端可切换，默认 NapCat**（发行包内置、离线开箱即用）；**SnowLuma 是可选的备用协议端**，
> 需自行安装后外置接入（发行包不内置，其 EULA §5.4 要求"并入第三方安装包"须事先书面授权）。
> 由 `[platform.qqonebot].protocol_backend`（`napcat` / `snowluma`）决定，升级不会自动切换。
> 见 [SnowLuma 使用与配置教程](docs/snowluma.md)。

部署套件（Docker Compose、systemd、离线镜像、Portainer、备份/恢复/升级/回滚、watchdog）入口是
[deploy/README.md](deploy/README.md)：

```bash
cd deploy && cp .env.example .env    # 填 API Key / token
docker compose build && docker compose up -d
python deploy/smoke.py                        # 构建 → 起容器 → 断言 → JSON 报告 → 自动清理
python deploy/stability.py --duration 300     # 长跑采样：健康失败率、RSS 趋势、重启与 Traceback
python -m atri_x doctor [--json] [--e2e]      # 配置/存储/端口/平台/模型验收
```

| 端口 | 用途 |
| --- | --- |
| `32171` | 独立健康接口（`/live`、`/ready`、`/healthz`、`/tasks`、`/metrics`、`/diagnostics`） |
| `32172` | CLI 远程 WebSocket 服务端（启用时） |
| `32170` | Atrinis 事件 HTTP 入口（默认关闭） |

`/live` 只看进程是否还能响应；`/ready` 还要求初始化完成、数据目录可写、调度心跳未过期；`/healthz`
汇总状态并在平台/模型异常时显示 `degraded`。`/tasks`、`/metrics`、`/diagnostics`、`/healthz` 需要
配置 `ATRIX_OPS_TOKEN` 才注册。部署细节见 [deploy/README.md](deploy/README.md) 第 5～7 节。

## 文档

分三层，按"你是谁"选入口：

| 层 | 入口 | 给谁 |
| --- | --- | --- |
| 用户 | [docs/getting-started.md](docs/getting-started.md)、[docs/configuration.md](docs/configuration.md)、[docs/commands.md](docs/commands.md)、[docs/concepts.md](docs/concepts.md) | 部署与使用 |
| 维护者 | [docs/dev/START-HERE.md](docs/dev/START-HERE.md)（30 分钟上手）、[docs/dev/find-it.md](docs/dev/find-it.md)（按任务找入口）、[docs/dev/glossary.md](docs/dev/glossary.md)（术语对照） | 第一次改代码 |
| AI / 深度 | [AGENTS.md](AGENTS.md)（仓库内操作规则）、[devdocs/architecture.md](devdocs/architecture.md)（为什么这样分）、[devdocs/generated/](devdocs/generated/)（由代码生成的索引）、[PORTING.md](PORTING.md)（移植契约） | 定位与改造 |

功能专题（生图 / 识图 / Hook / 内容安全 / 研究助理 / 桌面端 / SnowLuma / Atrinis / 部署）分别在
[docs/](docs/) 与 [deploy/](deploy/) 下；变更记录见 [CHANGELOG.md](CHANGELOG.md)；
对抗性验证与已知局限见 [verification/](verification/)。

## 仓库结构

```text
atri_x/                  Python 包（平铺在仓库根下，不安装）
  __main__.py            程序入口（python -m atri_x）
  launcher/  app/        命令行解析、shell 补全；分阶段装配与生命周期
  agent/                 对话主流程、上下文、预算、模型状态与 slash 命令（commands/）
  tool/  toolrun/        工具契约与内置工具（builtin/）、Skill；执行视图、风险确认与授权复核
  llm/                   LLM 抽象与 OpenAI Chat Completions / Responses 适配器
  platform/              cli（TUI 与远程客户端）、telegram、qq_onebot、qqofficial、headless
  hook/  session/  contextmgr/   事件与插件；会话与上下文压缩
  memory/  storage/      常驻/长期记忆；领域模型 + SQLite 实现
  cron/  maintenance/  background/   定时任务、维护任务与后台任务
  atrinis/ atriena/ atriyph/        事件枢纽、事件协议、任务记法
  character/ imagegen/ vision/ artist/ asr/ media/   角色、生图、视觉、画师串、语音、媒体
  selflearning/ groupanalysis/ groupkb/              自主学习、群分析、群知识库
  research/              科研研究助理的随包引擎装配
  health/ ops/ security/ redact/ logging/ signal/    健康、限流熔断、安全、脱敏、日志、信号
  net/ shellparse/ utils/                           补齐 Python 标准库缺口
  devtools/              开发期索引工具（AST 扫描 / 渲染 / 复杂度与边界），只被 scripts/ 使用
research_pack/           随包研究引擎与 15 个 AgentSkill
drawio_pack/             随包 draw.io 技能（上游 drawio-skill 3.4.2 快照 + ATRI-X 动作适配器）与上游一致性测试
deploy/                  部署套件（Docker、systemd、Windows、离线包、备份升级、watchdog）
tests/  scripts/         unittest 用例；门禁与生成脚本
docs/                    用户文档 + 开发者入口（docs/dev/）
devdocs/                 维护与架构文档 + 生成索引（devdocs/generated/）
verification/            对抗性验证报告与已知局限
```

- 布局是**平铺**的：仓库根下直接是 `atri_x/`，不安装、不做 src-layout；在别的目录调用需先 `cd` 到仓库根或设置 `PYTHONPATH`。
- `pyproject.toml` 只承载项目元数据；推荐运行方式始终是仓库根目录下的 `python -m atri_x`。
- 内置 draw.io 渲染器是**构建材料**（`packaging/vendor/`，约 382 MB）：不入库，构建时用 `--with-drawio` 指定；`drawio_pack/` 只含技能本体（约 1.2 MB）。详见 [packaging/README.md](packaging/README.md)。
- 逐模块职责不在本文件维护——看 [`devdocs/generated/module-map.md`](devdocs/generated/module-map.md)（由代码生成）。

## 开发与验证

### 质量门禁

```bash
python scripts/check_syntax.py        # 语法 + BOM + CRLF + U+FFFD + mojibake
python scripts/check_imports.py       # 逐个导入 atri_x 下每个模块，校验 __all__
python scripts/check_integration.py   # 跨模块唯一出处、ctx 键、脱敏契约、ID 契约、async 误用
python scripts/check_coverage.py --strict --summary   # 按 PORTING.md §4 映射表算**文件存在性**加权覆盖
python scripts/check_except.py        # except BaseException 且不 re-raise 的位置基线
python scripts/check_secrets.py       # 密钥 / 令牌 / 本机身份扫描
python scripts/gen_index.py --check   # 生成索引是否漂移（模块地图 / 依赖 / 入口 / 测试地图 / Go 映射）
python scripts/check_docs.py          # 文档门禁：链接 / 内联路径 / 索引覆盖 / 锚点 / 行数棘轮 / 数字
python scripts/check_complexity.py    # 结构指标预算（模块数 / 超线文件 / 跨域边 / fan-in / 边界）
python scripts/gen_stats.py --check   # README 静态数字与工作树一致
python scripts/gen_drawio_pack.py --check   # drawio_pack/pack.json 摘要与技能文件一致（防随包脚本漂移）
python -m unittest discover -s tests -t .             # 全量测试
```

CI 会在每次 push / PR 跑完整套（[`.github/workflows/quality.yml`](.github/workflows/quality.yml)）。

最近一次在 `Windows 10 + Python 3.14.7` 上的结果（文件与模块计数由 `python scripts/gen_stats.py`
从工作树静态统计，可随时复算）：

| 门禁 | 结果 |
| --- | --- |
| `check_syntax` | 1040 个文件，0 错（`bom=0`、`crlf=0`、`damaged=0`） |
| `check_imports` | `modules=797 imported=797`，0 失败 |
| `check_integration` | 74 项检查，0 失败 |
| `check_coverage --strict` | `packages=72 covered=72 missing=0 weighted=100.0%`（**文件存在性**口径，不衡量行为正确性） |
| `check_except` | 与位置级基线一致（0 个新增位置） |
| `gen_index --check` | 生成索引与代码一致（0 漂移） |
| `check_docs` | 6 条断言全绿 |
| `check_complexity` | 结构指标在预算内 |
| `check_secrets` | 0 命中（扫描范围含未跟踪文件） |
| `gen_drawio_pack --check` | 随包 draw.io 技能摘要与清单一致（0 漂移） |
| 全量 `unittest` | 用例数不硬编码；现场跑 `python -m unittest discover -s tests -t .`，以本机输出或 CI 记录为准 |

> **数字口径**：上表的文件/模块计数由 `python scripts/gen_stats.py` 只读扫描工作树得出（与
> `check_syntax.py`、`check_imports.py` 共用同一份口径）；`--check` 在 README 数字与工作树不一致时
> 以退出码 1 打印差异，`--update` 打印可粘贴的正确数字。**用例数不在本 README 硬编码**：unittest 的
> 用例数只有真跑一遍才知道（参数化、动态生成、解释器版本、环境依赖 skip 都会影响它）。

### 对抗性验证与性能

[verification/](verification/) 记录了以上游 Go 源码为唯一真相的逐包核对与真实进程验证，并登记了
**仍然未验证**的部分：未连接真实平台服务端（QQ 官方 / OneBot / Telegram 只有进程内假连接用例）、
未调用真实模型 API（假 client / 假 SSE 流）、TUI 未在真实 TTY 逐项体验、稳定性长跑只覆盖最小配置、
开发机没有 Go 工具链（Go 侧结论来自源码行号级对照）。

本仓库**不提供官方性能基准**：启动与资源占用取决于配置、模型调用、生图与工具子进程、并发规模。
需要时在自己的目标机器上实测，例如：

```bash
python -m atri_x --version
python -m atri_x config check                 # 配置加载耗时
python -m atri_x doctor --json                # 部署自检
# Windows 上随时问超管：/resources（CPU / 内存 / 磁盘 / 显卡 + 型号，不报本机路径）
```

### 开发约定

- 改动 Python 代码后至少跑 `check_syntax` / `check_imports` / `check_integration`；只改 Markdown 也要跑
  `check_docs.py`（它同时检查路径、锚点与编码问题）。
- 单文件目标 ≤ 600 行（现状见 `scripts/complexity_budget.json`，只降不升）；模块 docstring 注明对应 Go 文件；
  中文注释与文档是默认语言。
- 新增/删除/移动模块后跑 `python scripts/gen_index.py` 并提交生成物；**不要手改** `devdocs/generated/`。
- 完整约定与易错点见 [AGENTS.md](AGENTS.md)、[docs/dev/START-HERE.md](docs/dev/START-HERE.md)、
  [PORTING.md](PORTING.md) §7。

## 与上游 Go 版的关系

本仓库是**逐包重写**而不是包装调用：上游每个生产包在 `atri_x/` 下都有对应模块（映射以
[PORTING.md](PORTING.md) §4 为唯一出处，模块级反查见 [`devdocs/generated/go-map.md`](devdocs/generated/go-map.md)）。
哪些行为尚未逐条核对，登记在 [devdocs/porting-status.md](devdocs/porting-status.md)。以下差异是设计决定：

| 项 | Go 版 | Python 版 |
| --- | --- | --- |
| 分发形态 | 单静态二进制，交叉编译多平台 | 需要 Python 解释器；仓库根直接运行，无构建产物 |
| shell AST | `mvdan.cc/sh/v3` | `atri_x/shellparse/`，**clean-room 自研**（候选 `bashlex` 是 GPLv3+ 且停更，与 Apache-2.0 冲突） |
| SQLite | `modernc.org/sqlite`（纯 Go） | 标准库 `sqlite3`（随 CPython 分发的 C 扩展） |
| WebSocket | `coder/websocket` | `atri_x/net/websocket.py`，在 `socket` / `ssl` 上实现 RFC 6455 |
| TOML | `pelletier/go-toml/v2` | 读用 `tomllib`；写用 `atri_x/net/tomlw.py` |
| S3 | `aws-sdk-go-v2` | `atri_x/net/s3.py`，SigV4 签名的最小 REST 客户端 |
| cron 表达式 | `robfig/cron/v3` | `atri_x/net/cronspec.py`，语义对齐 |
| TUI | `bubbletea` + `lipgloss` | `atri_x/platform/cli/tui/`，ANSI + 原始模式输入；渲染保真度低于上游 |
| 图像编解码 | 标准库 `image` | `atri_x/media/imagecodec/`，纯 Python + 可选 Pillow 加速；**不支持渐进式 JPEG** |
| 并发模型 | goroutine + channel | `asyncio` task + `Queue`；同步路径（SQLite、文件）用 `threading` |
| 资源采样 | `runtime.MemStats` + `/proc` | `atri_x/sysinfo.py`：Linux 读 `/proc`，Windows 用 `ctypes` 调 kernel32（零依赖） |

另需如实说明：QQ 官方平台与 Telegram 目前只有进程内假连接用例，**没有用真实凭据与真实服务端做过
端到端验证**；这部分与其他未验证项见 [verification/README.md](verification/README.md)。

## 许可

本项目使用 **Apache-2.0** 许可，与上游一致，见 [LICENSE](LICENSE)。许可证只授予版权层面的使用 /
修改 / 分发许可，**不能免除或替代**你应遵守的法律法规、平台协议与第三方权利。上游 Go 源码不含
copyleft 依赖，本重写同样没有引入 copyleft 依赖——这也是 `atri_x/shellparse/` 选择 clean-room 自研、
而不是引入 GPLv3 的 `bashlex` 的原因。唯一可能用到的第三方包是**可选的** Pillow（宽松许可），
不安装时自动回退到纯 Python 实现。

## 开发状态

ATRI-X 仍在快速开发中，接口、配置和内部实现可能继续调整。当前版本更适合作为个人 Agent / 机器人框架
探索使用；行为基准以上游 Go 版为准，跨语言细节差异以本仓库文档与验证报告为补充。
