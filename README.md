# ATRI-X（Python 版）

**ATRI-X 的 Python 全量重写**：一个轻量、可扩展、多平台的 LLM Agent / Chatbot 守护进程。

上游是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本仓库对应 v0.9.0）。本仓库把上游 71 个生产包逐包重写为 Python（包名 `atri_x`），**纯标准库、零运行时第三方依赖**，在仓库根目录直接 `python -m atri_x` 运行——不需要安装，也没有构建产物。

支持普通聊天、工具调用、Hook 扩展、Cron 调度、Skill 沉淀、长期记忆、外部事件触发、多平台接入与容器化部署，适合个人助理、平台机器人和可编排自动化助手等场景。

| 项 | 值 |
| --- | --- |
| 版本 | `0.9.1`（与上游 v0.9.0 对应；Windows 桌面端/安装包自 0.9.1 起提供） |
| 许可 | [Apache-2.0](LICENSE)（与上游一致） |
| Python | ≥ 3.11（开发与验证在 3.14.7 上完成） |
| 入口 | `python -m atri_x` |

## 项目速览

| 项 | 说明 |
| --- | --- |
| 上游 | Go 版 `ATRI-X` v0.9.0：446 个生产 `.go` 文件 / 90,203 行（含测试共 754 个文件 / 146,153 行） |
| 本仓库 | 平铺布局的 `atri_x/`：648 个 `.py` 模块，约 18.1 万行（含注释与 docstring） |
| 包级映射 | 上游 71 个生产包 + `cmd/atri-x`，共 72 条映射，全部有对应实现（[PORTING.md](PORTING.md) §4） |
| 平台 | CLI（本地 TUI / 远程客户端）、Telegram、QQ OneBot、QQ Official，另有 headless 平台适配 |
| 存储 | SQLite（标准库 `sqlite3`），会话、消息、工具调用、媒体引用与审计数据均持久化 |
| 测试 | `tests/` 157 个 Python 文件（其中 148 个 `test_*.py`）、5098 个用例；仓库自带四道质量门禁（见「开发与验证」） |
| 规模 | 932 个受版本控制文件（不含 `.git`） |

## 功能特性

### 对话与工具

- **Chat / Work 双模式**：两种模式可分别配置模型，让低成本模型负责闲聊，强模型专注工具任务。
- **按需工具发现**：work 模式不会每轮注入全部工具 schema，默认只暴露 `discover_tool` 与可用工具名；模型按需发现后再注入具体 schema，减少无效上下文开销。另可用 `@tool:` / `@skill:` 内联预载。
- **内置工具 49 个**：文件、Shell、Web、Cron、Skill、媒体与记忆等；其中 13 个无条件注册，其余按依赖条件注册。
- **风险与确认**：工具带风险等级，按角色/权限判断可用范围；高风险调用需要逐次确认。
- **单轮声明与命令前缀**：`@model:` / `@image:` / `@use:`（也支持 `#模型:` / `#生图:` / `#工具:`）只对当前这一轮生效，仅超级管理员可用；slash 命令前缀可通过 `[commands].prefixes` 追加。

细节见 [核心概念](docs/concepts.md) 与 [命令速查](docs/commands.md)。

### 平台与前端

- **CLI**：本地 TUI（聊天区、通知区、滚动、copy mode、文件引用补全）；也支持把进程作为服务端、任意电脑用 CLI 客户端远程连接（默认端口 `32172`）。
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

### 事件触发：Atrinis

传统 Agent 只等待用户输入，Cron 只能响应时间；**Atrinis** 让外部事件也能触发 Agent：

- **Atrinis** 是监听枢纽，**Atriwisp** 是部署在各处的外部监听器，**Atriena** 是统一的 JSON over HTTP 事件协议。
- 服务器告警、RSS 更新、Webhook、游戏事件等信号都可以送入 Atrinis，再交由 Agent 处理并回发。
- 入口默认关闭；启用后监听 `32170`。

见 [Atrinis 监听枢纽](docs/atrinis.md) 与 [Atrinis 配置与使用](docs/atrinis-usage.md)。

### 角色、生图与多模态

- **角色素材库**：用文件夹管理角色文本（人设/世界观/开场白/示例）与图片素材，支持索引、检索、`@char:<id>` 临时启用与 `character_*` 工具。见 [角色素材库](docs/character-library.md)。
- **图像生成**：对接 OpenAI 兼容的 `/images/generations`，按「全局预设 + 角色预设 + 场景描述」组装提示词，支持规则优化、画师串、多角色同框、并发与排队控制。见 [生图服务](docs/image-generation.md)。
- **图片反推提示词**：内置 `image_to_prompt`，把参考图交给视觉模型反推绘图 prompt，带指纹缓存与并发合并。
- **视觉兜底**：主模型为纯文本、上游拒绝图片时，先用视觉模型转写图片再重试；结构化判定触发条件，不会把普通错误误判成「不支持视觉」。
- **语音转写（可选）**：用已有 Provider 的 `/audio/transcriptions` 把被唤醒的语音消息转成文本，再进入聊天与工具流程。
- **定时报告**：向超级管理员推送生图量、Token、费用、磁盘与内存统计。见 [定时报告](docs/reports.md)。

### 群聊能力

面向群机器人场景的确定性能力，与 LLM 调用分离：

- 群级权限与群策略、工具白名单、模型别名授权。
- 群知识库 / FAQ：命中后服务端直接答复，不创建 Session、不调用 LLM。
- 群内提醒 / 投票 / 报名：本地调度，到点通过正常输出层发送。
- 群会话线程模式与连续消息合并窗口；机器人被禁言/踢出后的群运行状态机。
- 入站消息去重与有界预处理、额度账本、限速与请求排队。

配置与命令见 [配置说明](docs/configuration.md) 与 [命令速查](docs/commands.md)。

### 安全与可观测

- **工具安全**：风险分级、角色权限、高风险确认；Shell 执行受 clean-room 自研的 AST 级解析约束（`atri_x/shellparse/`）。
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

| 产物 | 位置 |
| --- | --- |
| Windows 安装包 | [`ATRI-X-<版本>-setup.exe`](https://github.com/fujiwarashigure/ATRI-X2/releases)：用户级安装（不弹 UAC），含桌面端、托盘、开机自启与内置 DSH 内核 |
| 容器镜像 | [`ghcr.io/fujiwarashigure/atri-x2`](https://github.com/fujiwarashigure/ATRI-X2/pkgs/container/atri-x2)：`:<版本>` 与 `:latest`，由 [`.github/workflows/release-image.yml`](.github/workflows/release-image.yml) 在打 tag 时构建并回拉冒烟 |
| 源码归档 | Release 里另有 `atri-x-<版本>-repo.tar.gz`（完整仓库）、`atri-x-<版本>-src.tar.gz`（离线源码包）与 `SHA256SUMS` |

```bash
docker pull ghcr.io/fujiwarashigure/atri-x2:0.9.1
# 包若为 private，先 docker login ghcr.io（用户名 + PAT，至少 read:packages）

# 用 compose 时把镜像覆盖成 ghcr 地址；纯 docker run 的参数见 deploy/README.md
cd deploy
ATRIX_IMAGE=ghcr.io/fujiwarashigure/atri-x2:0.9.1 docker compose up -d
```

> Windows 包内已内置 DSH 内核（Node 与 `@deepseek-ai/dsh`），目标机不需要另装 Node；容器镜像同样在
> 构建期装好 DSH。安装/卸载、接 QQ 的完整步骤见 [Windows 桌面端](docs/windows-app.md)。

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
| [docs/getting-started.md](docs/getting-started.md) | 从配置 API Key 到第一次对话 |
| [docs/configuration.md](docs/configuration.md) | 配置文件、路径规则、Provider、环境变量 |
| [docs/commands.md](docs/commands.md) | 全部 slash 命令速查 |
| [docs/concepts.md](docs/concepts.md) | Chat / Work、工具发现、Session、Hook、Cron、安全策略 |
| [docs/hooks.md](docs/hooks.md) | Hook 配置、规则/进程/Worker 插件与编程接口 |
| [docs/atrinis.md](docs/atrinis.md)、[docs/atrinis-usage.md](docs/atrinis-usage.md) | 事件监听枢纽与接入 |
| [docs/atriyph.md](docs/atriyph.md) | Atriyph 任务表示法 |
| [docs/character-library.md](docs/character-library.md) | 角色素材库 |
| [docs/image-generation.md](docs/image-generation.md) | 生图服务与提示词优化 |
| [docs/reports.md](docs/reports.md) | 定时报告 |
| [docs/frontend-api.md](docs/frontend-api.md) | 远程 CLI / 自定义前端协议 |
| [docs/windows-app.md](docs/windows-app.md) | Windows 桌面端、托盘/自启、安装包与接 QQ |
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
  health/ ops/ security/ redact/ logging/ signal/  健康、限流熔断、安全、脱敏、日志与信号
  net/ shellparse/ media/imagecodec/ utils/runes.py   补齐 Python 标准库缺口
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
python scripts/check_coverage.py --strict --summary   # 按 PORTING.md §4 映射表算加权覆盖
python -m unittest discover -s tests -t .             # 全量测试
```

最近一次在 `Windows 10 + Python 3.14.7` 上的结果：

| 门禁 | 结果 |
| --- | --- |
| `check_syntax` | 812 个文件，0 错（`bom=0`、`crlf=0`、`damaged=0`） |
| `check_imports` | `modules=648 imported=648`，0 失败 |
| `check_integration` | 74 项检查，0 失败 |
| `check_coverage --strict` | `packages=72 covered=72 missing=0 weighted=100.0%` |
| 全量 `unittest` | 5098 个用例通过（`skipped=2`） |

### 对抗性验证

[verification/](verification/) 记录了独立验证者以上游 Go 源码为唯一真相做的逐包核对，以及真实进程 + 真实 HTTP 请求的边界验证：四道门禁全绿、全量测试通过、Docker 冒烟 13/13、300 秒容器稳定性长跑 0/50 采样失败。验证同时登记了**仍然未验证**的部分，例如：

- 未连接真实平台服务端（QQ 官方 / OneBot / Telegram 只有进程内假连接用例）；
- 未调用真实模型 API（LLM 路径使用假 client / 假 SSE 流）；
- TUI 未在真实 TTY 中逐项体验（有 headless 用例）；
- 稳定性长跑只覆盖最小配置（无平台、无模型、无持续消息）；
- 开发机没有 Go 工具链，Go 侧结论均来自源码行号级对照。

### 性能数据说明

本仓库**不提供官方性能基准**。旧文档中的「N5105 上 <10 ms 启动、约 30 MB 常驻内存」是 Go 版 v0.5.0 的历史参考数据，无法代表当前 Python 版（版本已到 0.9.1，且新增了健康接口、诊断、素材清单、`doctor` 等组件），因此不在本 README 中保留。启动与资源占用取决于配置、模型调用、生图与工具子进程、并发规模，需要时请在自己的目标机器上实测，例如：

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

上游 Go 源码不含 copyleft 依赖；本重写同样没有引入 copyleft 依赖——这也是 `atri_x/shellparse/` 选择 clean-room 自研、而不是引入 GPLv3 的 `bashlex` 的原因。唯一可能用到的第三方包是**可选的** Pillow（图像编解码加速，宽松许可），不安装时自动回退到纯 Python 实现。

## 开发状态

ATRI-X 仍在快速开发中，接口、配置和内部实现可能继续调整。当前版本更适合作为个人 Agent / 机器人框架探索使用；行为基准以上游 Go 版为准，跨语言细节差异以本仓库文档与验证报告为补充。
