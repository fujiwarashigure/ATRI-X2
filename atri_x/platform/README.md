<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/platform/` — 平台适配器

## 这个域负责什么

把 QQ / 官方 / Telegram / CLI 等外部协议翻译成 `platform.PlatformAdapter` 契约。

**不做什么**：不写业务逻辑、不做内容决策；适配器只负责传输与消息形态。

**入口**：`atri_x/platform/platform.py`（契约）、`atri_x/platform/qq_onebot/`（NapCat/SnowLuma）。

**配置段**：`[platform.*]`

## 易错点

- 能力矩阵（capability）要对齐真实协议；不支持就用能力位声明，不要静默降级。
- 适配器里的失败要隔离：一个平台挂掉不能带走整个进程（见 `app/platforms.py`）。
- QQ 官方与 Telegram **没有连过真实服务端**，只有进程内假连接用例（见 porting-status）。

## 模块清单（自动生成）

共 **69** 个模块，被测试覆盖 **50** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.platform` | 平台适配层：适配器契约、连接信号、配置与媒体编解码。 | 75 | `test_agent_chat.py`、`test_agent_commands_integration.py`、`test_agent_core.py`、`test_agent_group_policy.py`、`test_agent_inbound_media.py`、`test_agent_participation.py`、`test_agent_wakeup.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_long_memory_scope.py`、`test_media_manager.py`、`test_media_platform.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_platform_builtin.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py`、`test_platform_core.py`、`test_platform_headless.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py`、`test_refcontext.py` |
| `atri_x.platform.backoff` | 平台重连用的指数退避。 | 62 | — |
| `atri_x.platform.builtin` | ``internal/platform/builtin`` 的 Python 移植：把配置变成平台适配器集合。 | 429 | `test_platform_builtin.py` |
| `atri_x.platform.cli` | CLI 平台适配器（对应 Go 包 ``internal/platform/cli``）。 | 90 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_cli_tui.py` |
| `atri_x.platform.cli.adapter` | CLI 平台适配器主体：stdin 读取循环、TUI 交互与出站发送。 | 386 | `test_platform_cli.py` |
| `atri_x.platform.cli.clipboard` | TUI 剪贴板读写：系统剪贴板 + OSC52 转义序列。 | 304 | — |
| `atri_x.platform.cli.config` | CLI 平台适配器的配置解析与装配入口。 | 224 | `test_cli_sessions.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.local_file` | TUI 的 ``#文件引用`` 补全与展开。 | 432 | `test_platform_cli.py`、`test_platform_cli_tui.py` |
| `atri_x.platform.cli.protocol` | 远程 CLI 的线上消息类型与 JSON 编码。 | 551 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_protocol.py`、`test_platform_cli.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_client` | 远程 CLI 客户端：把本地终端接到远端（或本机）的远程 CLI 服务端。 | 335 | `test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_server` | 远程 CLI 服务端：让远程终端经 WebSocket 接入本机的 CLI 适配器。 | 732 | `test_cli_sessions.py`、`test_platform_cli_remote.py` |
| `atri_x.platform.cli.session_browse` | 远端 CLI 的会话浏览扩展（Python 版新增）。 | 101 | — |
| `atri_x.platform.cli.tui` | 本地 CLI 的终端界面（Tarui）——对应 Go 包 ``internal/platform/cli`` 的 TUI 部分。 | 92 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.content` | TUI 的聊天区内容与通知面板维护。 | 284 | — |
| `atri_x.platform.cli.tui.copy` | TUI 的 copy mode：vi 风格的区域选择、词移动、搜索与复制。 | 806 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.input` | 多行输入框：``bubbles/textarea`` 的最小等价物。 | 228 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.keys` | TUI 的按键与鼠标事件：``bubbletea`` 的 ``KeyMsg`` / ``MouseMsg`` 等价物。 | 172 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.messages` | TUI 的消息类型与小状态对象。 | 188 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.model` | TUI 主模型：状态、Update 主流程、输入提交、历史与补全。 | 623 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.mouse` | TUI 的鼠标处理：分区判定、分隔线拖拽、滚轮与鼠标定位光标。 | 168 | — |
| `atri_x.platform.cli.tui.program` | TUI 主循环与键盘事件源：``run_tui`` 的落地。 | 603 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.style` | TUI 的 ANSI 样式与布局助手：``lipgloss`` 的最小等价物。 | 202 | `test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.theme` | TUI 主题：Go 里 ``lipgloss.NewStyle()...`` 常量块的等价物。 | 82 | — |
| `atri_x.platform.cli.tui.view` | TUI 的整帧渲染与布局。 | 238 | — |
| `atri_x.platform.cli.tui.viewport` | 行缓冲视口：``bubbles/viewport`` 的最小等价物。 | 78 | `test_platform_cli_tui.py` |
| `atri_x.platform.config` | 平台配置解码与命令前缀/唤醒词处理。 | 93 | — |
| `atri_x.platform.headless` | ``internal/platform/headless`` 的 Python 移植。 | 82 | `test_platform_headless.py` |
| `atri_x.platform.media` | Chat History 原始有序 segments 的编解码与敏感来源清洗。 | 103 | `test_media_manager.py` |
| `atri_x.platform.platform` | 平台抽象：适配器、入站/出站类型、可选能力接口与"平台已连接"信号。 | 454 | `test_agent_inbound_media.py`、`test_media_manager.py`、`test_media_platform.py`、`test_platform_cli.py`、`test_platform_cli_remote.py`、`test_platform_qq_onebot.py`、`test_platform_qqofficial.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py`、`test_refcontext.py` |
| `atri_x.platform.qq_onebot` | QQ OneBot v11 平台适配器（对应 Go 包 ``internal/platform/qq-onebot``）。 | 194 | `test_platform_qq_onebot.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.adapter` | qq-onebot 适配器主体：正向 WebSocket 读循环与出站发送。 | 719 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.attachment` | 入站媒体解析（``get_image`` / ``get_file`` / 群文件直链兜底）。 | 105 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.backend` | qqonebot 协议端（后端）差异描述。 | 213 | `test_qq_onebot_backend.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.capabilities` | OneBot 的可选平台能力：群历史、群资料、群成员、头像 URL。 | 195 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.config` | qq-onebot 适配器配置与装配入口。 | 258 | `test_platform_qq_onebot.py`、`test_qq_onebot_backend.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.forward` | 合并转发消息的展开渲染，以及 ``message.go`` 里的段/文本自由函数。 | 492 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.inbound` | 入站预处理的去重、限流与事件分发。 | 397 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.message` | 入站消息归一化：段 → 文本 / @提及 / 媒体段 / 待展开的转发 id。 | 263 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.output` | 出站输出 → OneBot 消息段，以及文本分页 / 合并转发节点 / 回执。 | 192 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.preprocess` | 入站的网络型预处理：@ 解析、引用上下文、合并转发展开与 Chat History 落库。 | 659 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.slowfetch` | 引用 / 转发的慢取：预算等待、单飞、成功 / 失败分级缓存。**Python 版新增** | 257 | `test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.smoke` | ``python -m atri_x.platform.qq_onebot.smoke``：OneBot 协议端连通性冒烟。 | 356 | `test_qq_onebot_smoke.py` |
| `atri_x.platform.qq_onebot.target` | 发送落点、ctx 传递与 scope 计算（Go ``adapter.go`` 里的 ``target`` 系列）。 | 118 | `test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.transport` | OneBot v11 正向 WebSocket 传输：连接、事件读取、动作调用。 | 688 | `test_platform_qq_onebot.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.types` | OneBot v11 线上事件类型与转发展开上限。 | 267 | `test_platform_qq_onebot.py`、`test_qq_onebot_smoke.py`、`test_qq_onebot_snowluma.py`、`test_qq_slowfetch.py` |
| `atri_x.platform.qqofficial` | QQ 官方机器人平台适配器（对应 Go 包 ``internal/platform/qqofficial``）。 | 201 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.adapter` | qqofficial 适配器主体：网关生命周期与出站发送。 | 328 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.attachment` | 入站附件 → 平台中立消息段，以及附件文件名的清洗。 | 190 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.client` | QQ 官方 HTTP API 客户端：网关地址、发消息、上传媒体。 | 222 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.config` | qqofficial 适配器配置与装配入口。 | 182 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.gateway` | QQ 官方网关：握手、心跳、事件分发与重连判定。 | 314 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.keyboard` | 风险确认内联键盘。 | 78 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.message` | QQ 官方入站消息 → 平台中立 ``MessageContext``，以及 Chat History 落库。 | 386 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.send` | QQ 官方出站发送：文本分页、Markdown 降级、媒体上传、键盘、Ark。 | 284 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.target` | qqofficial 的发送落点与 ctx 传递。 | 69 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.token` | QQ 官方 access token 的获取与缓存。 | 166 | `test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.types` | QQ 官方机器人网关与消息 API 的线上类型。 | 684 | `test_platform_qqofficial.py` |
| `atri_x.platform.refcontext` | 引用的恢复与续聊/Fork 判定。 | 458 | `test_refcontext.py` |
| `atri_x.platform.telegram` | Telegram 平台适配器（对应 Go 包 ``internal/platform/telegram``）。 | 112 | — |
| `atri_x.platform.telegram.adapter` | Telegram 适配器主体：long polling 入站循环与出站发送。 | 799 | — |
| `atri_x.platform.telegram.capabilities` | Telegram 的可选群目录能力（群信息 + 管理员列表）。 | 116 | — |
| `atri_x.platform.telegram.client` | Telegram Bot API 客户端（long polling、发送、媒体上传与下载）。 | 463 | — |
| `atri_x.platform.telegram.config` | Telegram 适配器配置与装配入口。 | 235 | — |
| `atri_x.platform.telegram.format` | Markdown → Telegram 实体文本（HTML）渲染。 | 218 | — |
| `atri_x.platform.telegram.keyboard` | 高风险工具确认的内联键盘。 | 48 | — |
| `atri_x.platform.telegram.message` | 入站消息归一化：文本、@提及、媒体段、Chat History 落库与媒体解析。 | 421 | — |
| `atri_x.platform.telegram.stream` | 流式助手消息：增量预览 + 最终替换。 | 270 | — |
| `atri_x.platform.telegram.target` | 发送落点与 ctx 传递（Python 侧拆出的最小模块）。 | 42 | — |
| `atri_x.platform.telegram.types` | Telegram Bot API 的线上类型与请求体。 | 579 | — |

## 对应上游 Go（自动生成）

`internal/platform/backoff.go`、`internal/platform/builtin/builtin.go`、`internal/platform/cli/cli.go`、`internal/platform/cli/clipboard.go`、`internal/platform/cli/config.go`、`internal/platform/cli/remote_client.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 796 个 / 超线文件 79 个）。
