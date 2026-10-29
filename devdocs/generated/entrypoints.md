<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 改动请落在 `atri_x/devtools/` 的渲染实现或代码本身，然后重新生成；手改会被 `--check` 判为漂移。 -->

# 入口索引（自动生成）

上游/架构说明见 `devdocs/architecture.md`，逐文件职责见 `devdocs/generated/module-map.md`，按任务找入口见 `docs/dev/find-it.md`。

## 进程入口

| 入口 | 模块 | 说明 |
|---|---|---|
| CLI | `atri_x.__main__` | `python -m atri_x`（与 `atri-x` console script 同一实现） |
| 桌面端 GUI | `atri_x.gui.__main__` | `atri-x-gui` / 托盘 |
| 启动器参数解析 | `atri_x.launcher.cli` | 命令行参数与用法文本 |

## 装配层（`atri_x/app/`）

| 模块 | 职责 |
|---|---|
| `atri_x.app` | ``atri_x.app`` —— 装配层。 |
| `atri_x.app.app` | 应用入口：运行模式、启动选项与 ``Run``。 |
| `atri_x.app.asr` | ASR（语音转写）服务装配。 |
| `atri_x.app.breaker_llm` | 熔断 / 兜底包装的 LLM 客户端。 |
| `atri_x.app.cli_client` | CLI 远程客户端入口：把 CLI 平台配置成"连远端服务"的瘦客户端。 |
| `atri_x.app.config_check` | ``atri-x config check``：加载 app.toml 并给出人类可读的摘要与告警。 |
| `atri_x.app.cron` | Cron 启动编排：异步准备 cron 任务表。 |
| `atri_x.app.dependencies` | 装配层依赖注入点：各阶段请求/组件与 :class:`Dependencies`。 |
| `atri_x.app.doctor` | 部署验收（doctor）：配置、本地依赖、平台连通性与端到端往返。 |
| `atri_x.app.doctor_checks` | Doctor 的单项检查实现。 |
| `atri_x.app.environment` | 启动环境：运行模式解析、启动剖析器、平台模式映射。 |
| `atri_x.app.foundation` | 装配根：配置、日志、主存储、聊天历史、维护服务与 cron 管理器。 |
| `atri_x.app.health` | 健康与运维 HTTP 端点的启动装配。 |
| `atri_x.app.health_handler` | 平台消息处理器包装：把每条消息的结果记进健康状态。 |
| `atri_x.app.health_llm` | 健康统计包装的 LLM 客户端。 |
| `atri_x.app.image_rewriter` | ``image_generate`` 的可选 LLM 改写通道。 |
| `atri_x.app.image_to_prompt` | ``image_to_prompt`` 工具共用的视觉引擎装配。 |
| `atri_x.app.integrations` | 集成阶段：把平台运行时接到 Agent 上，并按需拉起 atrinis。 |
| `atri_x.app.models` | LLM 客户端装配：provider → 适配器（含协议路由、健康包装、熔断包装）。 |
| `atri_x.app.naming` | 会话命名事件的日志/审计记录器。 |
| `atri_x.app.ops_health` | ``/metrics`` 与 ``/diagnostics`` 的载荷收集，以及两个延迟接线的 HTTP 组件。 |
| `atri_x.app.platform_caller` | 按平台名把运行时解析成"平台 API 调用器"。 |
| `atri_x.app.platforms` | 平台装配与运行循环。 |
| `atri_x.app.runner` | 装配运行器：按固定顺序装配各阶段并保证逆序清理。 |
| `atri_x.app.runtime` | 运行期装配主链：媒体中心 → 工具运行时 → Hook → Agent → 生命周期。 |
| `atri_x.app.runtime_agent` | Agent 装配。 |
| `atri_x.app.runtime_hooks` | Hook 控制面装配。 |
| `atri_x.app.runtime_support` | 运行期装配的共享助手：审计入口、凭据解析、cron 服务、Hook 生命周期。 |
| `atri_x.app.service_marker` | 服务实例标记：防止同一台机器上跑起第二个 ``atri-x`` service。 |
| `atri_x.app.service_types` | app 内部使用的服务侧数据类型。 |
| `atri_x.app.support` | 装配层的叶子级共享助手。 |
| `atri_x.app.time_check` | ``python -m atri_x.app.time_check``：时区数据与解析探针。 |
| `atri_x.app.tool_runtime` | 内置工具运行时的 app 侧装配。 |
| `atri_x.app.vision` | 视觉服务装配：把共享 ``vision.Service`` 接到 Agent 的聊天视觉兜底接口。 |

## Slash 命令模块（`atri_x/agent/commands/`）

| 模块 | 职责 |
|---|---|
| `atri_x.agent.commands` | ``internal/agent/commands`` 的 Python 移植：slash 命令族与注册入口。 |
| `atri_x.agent.commands.approval` | ``/grant`` 与 ``/panic``：授权生命周期的手动入口。 |
| `atri_x.agent.commands.character` | ``/chars``：列出角色素材库，或重建角色索引。 |
| `atri_x.agent.commands.compact` | ``/compact``：手动压缩当前会话上下文。 |
| `atri_x.agent.commands.completion` | 命令参数补全的公共助手。 |
| `atri_x.agent.commands.context_policy` | ``/overflow``（别名 ``/ctxpolicy``、``/longmsg``）：长消息溢出策略。 |
| `atri_x.agent.commands.deps` | 命令注册表、依赖容器与服务协议。 |
| `atri_x.agent.commands.doctor` | ``/doctor``：只读配置检查。 |
| `atri_x.agent.commands.group_knowledge` | ``/faq``（别名 ``/knowledge``、``/kb``、``/groupkb``）：群确定性知识库。 |
| `atri_x.agent.commands.group_policy` | ``/grouppolicy``（别名 ``/gpolicy``、``/grouppolicies``）：当前群的策略配置。 |
| `atri_x.agent.commands.group_services` | ``/remind``、``/poll``、``/vote``、``/signup``、``/join``：群确定性服务。 |
| `atri_x.agent.commands.help` | ``/help`` 命令与帮助文本渲染。 |
| `atri_x.agent.commands.hook` | ``/hooks``：列出/查看 hook、重载配置、控制有状态 hook。 |
| `atri_x.agent.commands.log` | ``/log``、``/audit``、``/atriwisp``、``/usage`` 的注册与派发。 |
| `atri_x.agent.commands.log_args` | 日志类命令的参数解析（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 |
| `atri_x.agent.commands.log_format` | 日志类命令的输出渲染（``/log`` / ``/audit`` / ``/atriwisp`` 共用）。 |
| `atri_x.agent.commands.member_panel` | ``/me``（别名 ``/mytasks``、``/my``、``/mypanel``）：普通成员自助面板。 |
| `atri_x.agent.commands.memory_common` | 长期记忆命令的共享助手。 |
| `atri_x.agent.commands.memory_forget` | ``/forget``（别名 ``/forget_memory``、``/delete_memory``）：删除长期/常驻记忆。 |
| `atri_x.agent.commands.memory_learning` | ``/memory``（别名 ``/angel_memory``）与 ``/learning``（别名 ``/selflearning``）。 |
| `atri_x.agent.commands.model` | ``/model`` 与 ``/checkmodel``（别名 ``/models``）。 |
| `atri_x.agent.commands.model_types` | 模型服务的共享数据类型与形状。 |
| `atri_x.agent.commands.request` | ``/requests``、``/stop``、``/stopall`` 与请求树渲染。 |
| `atri_x.agent.commands.research` | ``/research``：科研研究助理的只读操作面板命令。 |
| `atri_x.agent.commands.resources` | ``/resources``：超管直接查看本机与 ATRI-X 进程的硬件资源占用。 |
| `atri_x.agent.commands.rollback` | ``/rollback``：列出并撤销 ATRI-X 自己对文件的最新一次编辑。 |
| `atri_x.agent.commands.session_core` | ``/new``、``/status``、``/work``、``/chat``。 |
| `atri_x.agent.commands.session_format` | Session 列表/消息的查询与文本渲染。 |
| `atri_x.agent.commands.session_lifecycle` | Session 生命周期命令：``/archive`` 系列、``/rename``、``/delete``、``/clean``。 |
| `atri_x.agent.commands.session_module` | ``SessionCommandState`` 与 ``SessionModule``。 |
| `atri_x.agent.commands.session_navigation` | ``/sessions``、``/archives``、``/resume``、``/messages``、``/fork``。 |
| `atri_x.agent.commands.state` | ``/state``：``state.toml`` 热加载状态与手动重载。 |
| `atri_x.agent.commands.time` | ``/time``：查看统一时间服务状态，手动触发一次网络校验，列出可用时区。 |
| `atri_x.agent.commands.tool` | ``/tools``：查看工具与当前群的工具授权，群主/管理员可在群里直接授权。 |
| `atri_x.agent.commands.usage` | ``/usage``：从审计日志聚合 token 用量。 |

## 内置工具（`atri_x/tool/builtin/` + 注册表字面量）


工具实现模块（含 builder 的模块）：

| 模块 | 职责 |
|---|---|
| `atri_x.tool.builtin.angel_memory` | Angel 长期记忆工具族：写入、召回，以及可选的删除（angel_forget）。 |
| `atri_x.tool.builtin.artist_tools` | 画师串工具的共享依赖、身份视图与错误文案。 |
| `atri_x.tool.builtin.chat_history` | 聊天历史工具族：关键词检索、上下文窗口、引用回复。 |
| `atri_x.tool.builtin.cron` | ``cron`` / ``cron_query`` / ``cron_write`` 三个定时任务工具。 |
| `atri_x.tool.builtin.file_rollback` | ``rollback_file``：撤销 ATRI-X 最近一次 ``edit_file`` 对某个文件的修改。 |
| `atri_x.tool.builtin.file_tools` | ``read_file`` 与 ``edit_file``：带 revision 校验的文本读写。 |
| `atri_x.tool.builtin.file_tools_read` | ``read_file`` 的声明与结果渲染。 |
| `atri_x.tool.builtin.get_media` | ``get_media``：按当前聊天的消息 ID 与媒体序号取媒体 ID（不返回内容）。 |
| `atri_x.tool.builtin.group_analysis` | ``group_analysis``：统计当前群聊的消息量、活跃成员与活跃时段。 |
| `atri_x.tool.builtin.image_to_prompt` | ``image_to_prompt``：把参考图片反推成可直接用于绘图的提示词。 |
| `atri_x.tool.builtin.long_memory` | ``long_memory`` / ``long_memory_search`` / ``long_memory_write`` 三个工具。 |
| `atri_x.tool.builtin.resident_memory` | 常驻记忆工具族：入口 + 读取 + 普通记忆 + 核心记忆。 |
| `atri_x.tool.builtin.runtime_resources` | ``runtime_resources``：超管专属的只读硬件资源占用查询。 |
| `atri_x.tool.builtin.self_learning` | ``self_learning_review``：管理 clean-room 表达/黑话候选。 |
| `atri_x.tool.builtin.send_file` | ``send_file``：把本地文件 / file URI / HTTP(S) URL / 媒体 ID 作为产物发出。 |
| `atri_x.tool.builtin.shell` | ``shell`` 工具：带风险分级、目录约束与后台沙盒的执行器。 |
| `atri_x.tool.builtin.view_image` | ``view_image``：把一张图片作为真实图片段交给模型。 |
| `atri_x.tool.builtin.web_extract` | ``web_extract``：网页正文提取（可配置的抓取通道链）。 |
| `atri_x.tool.builtin.web_search` | ``web_search``：网页搜索（可配置的通道链）。 |
| `atri_x.tool.builtin.workspace` | ``workspace`` 工具：设置当前 Session 的共享工作目录。 |

## 平台适配器（`atri_x/platform/`）

| 模块 | 职责 |
|---|---|
| `atri_x.platform` | 平台适配层：适配器契约、连接信号、配置与媒体编解码。 |
| `atri_x.platform.backoff` | 平台重连用的指数退避。 |
| `atri_x.platform.builtin` | ``internal/platform/builtin`` 的 Python 移植：把配置变成平台适配器集合。 |
| `atri_x.platform.cli` | CLI 平台适配器（对应 Go 包 ``internal/platform/cli``）。 |
| `atri_x.platform.cli.adapter` | CLI 平台适配器主体：stdin 读取循环、TUI 交互与出站发送。 |
| `atri_x.platform.cli.clipboard` | TUI 剪贴板读写：系统剪贴板 + OSC52 转义序列。 |
| `atri_x.platform.cli.config` | CLI 平台适配器的配置解析与装配入口。 |
| `atri_x.platform.cli.local_file` | TUI 的 ``#文件引用`` 补全与展开。 |
| `atri_x.platform.cli.protocol` | 远程 CLI 的线上消息类型与 JSON 编码。 |
| `atri_x.platform.cli.remote_client` | 远程 CLI 客户端：把本地终端接到远端（或本机）的远程 CLI 服务端。 |
| `atri_x.platform.cli.remote_server` | 远程 CLI 服务端：让远程终端经 WebSocket 接入本机的 CLI 适配器。 |
| `atri_x.platform.cli.session_browse` | 远端 CLI 的会话浏览扩展（Python 版新增）。 |
| `atri_x.platform.cli.tui` | 本地 CLI 的终端界面（Tarui）——对应 Go 包 ``internal/platform/cli`` 的 TUI 部分。 |
| `atri_x.platform.cli.tui.content` | TUI 的聊天区内容与通知面板维护。 |
| `atri_x.platform.cli.tui.copy` | TUI 的 copy mode：vi 风格的区域选择、词移动、搜索与复制。 |
| `atri_x.platform.cli.tui.input` | 多行输入框：``bubbles/textarea`` 的最小等价物。 |
| `atri_x.platform.cli.tui.keys` | TUI 的按键与鼠标事件：``bubbletea`` 的 ``KeyMsg`` / ``MouseMsg`` 等价物。 |
| `atri_x.platform.cli.tui.messages` | TUI 的消息类型与小状态对象。 |
| `atri_x.platform.cli.tui.model` | TUI 主模型：状态、Update 主流程、输入提交、历史与补全。 |
| `atri_x.platform.cli.tui.mouse` | TUI 的鼠标处理：分区判定、分隔线拖拽、滚轮与鼠标定位光标。 |
| `atri_x.platform.cli.tui.program` | TUI 主循环与键盘事件源：``run_tui`` 的落地。 |
| `atri_x.platform.cli.tui.style` | TUI 的 ANSI 样式与布局助手：``lipgloss`` 的最小等价物。 |
| `atri_x.platform.cli.tui.theme` | TUI 主题：Go 里 ``lipgloss.NewStyle()...`` 常量块的等价物。 |
| `atri_x.platform.cli.tui.view` | TUI 的整帧渲染与布局。 |
| `atri_x.platform.cli.tui.viewport` | 行缓冲视口：``bubbles/viewport`` 的最小等价物。 |
| `atri_x.platform.config` | 平台配置解码与命令前缀/唤醒词处理。 |
| `atri_x.platform.headless` | ``internal/platform/headless`` 的 Python 移植。 |
| `atri_x.platform.media` | Chat History 原始有序 segments 的编解码与敏感来源清洗。 |
| `atri_x.platform.platform` | 平台抽象：适配器、入站/出站类型、可选能力接口与"平台已连接"信号。 |
| `atri_x.platform.qq_onebot` | QQ OneBot v11 平台适配器（对应 Go 包 ``internal/platform/qq-onebot``）。 |
| `atri_x.platform.qq_onebot.adapter` | qq-onebot 适配器主体：正向 WebSocket 读循环与出站发送。 |
| `atri_x.platform.qq_onebot.attachment` | 入站媒体解析（``get_image`` / ``get_file`` / 群文件直链兜底）。 |
| `atri_x.platform.qq_onebot.backend` | qqonebot 协议端（后端）差异描述。 |
| `atri_x.platform.qq_onebot.capabilities` | OneBot 的可选平台能力：群历史、群资料、群成员、头像 URL。 |
| `atri_x.platform.qq_onebot.config` | qq-onebot 适配器配置与装配入口。 |
| `atri_x.platform.qq_onebot.forward` | 合并转发消息的展开渲染，以及 ``message.go`` 里的段/文本自由函数。 |
| `atri_x.platform.qq_onebot.inbound` | 入站预处理的去重、限流与事件分发。 |
| `atri_x.platform.qq_onebot.message` | 入站消息归一化：段 → 文本 / @提及 / 媒体段 / 待展开的转发 id。 |
| `atri_x.platform.qq_onebot.output` | 出站输出 → OneBot 消息段，以及文本分页 / 合并转发节点 / 回执。 |
| `atri_x.platform.qq_onebot.preprocess` | 入站的网络型预处理：@ 解析、引用上下文、合并转发展开与 Chat History 落库。 |
| `atri_x.platform.qq_onebot.slowfetch` | 引用 / 转发的慢取：预算等待、单飞、成功 / 失败分级缓存。**Python 版新增** |
| `atri_x.platform.qq_onebot.smoke` | ``python -m atri_x.platform.qq_onebot.smoke``：OneBot 协议端连通性冒烟。 |
| `atri_x.platform.qq_onebot.target` | 发送落点、ctx 传递与 scope 计算（Go ``adapter.go`` 里的 ``target`` 系列）。 |
| `atri_x.platform.qq_onebot.transport` | OneBot v11 正向 WebSocket 传输：连接、事件读取、动作调用。 |
| `atri_x.platform.qq_onebot.types` | OneBot v11 线上事件类型与转发展开上限。 |
| `atri_x.platform.qqofficial` | QQ 官方机器人平台适配器（对应 Go 包 ``internal/platform/qqofficial``）。 |
| `atri_x.platform.qqofficial.adapter` | qqofficial 适配器主体：网关生命周期与出站发送。 |
| `atri_x.platform.qqofficial.attachment` | 入站附件 → 平台中立消息段，以及附件文件名的清洗。 |
| `atri_x.platform.qqofficial.client` | QQ 官方 HTTP API 客户端：网关地址、发消息、上传媒体。 |
| `atri_x.platform.qqofficial.config` | qqofficial 适配器配置与装配入口。 |
| `atri_x.platform.qqofficial.gateway` | QQ 官方网关：握手、心跳、事件分发与重连判定。 |
| `atri_x.platform.qqofficial.keyboard` | 风险确认内联键盘。 |
| `atri_x.platform.qqofficial.message` | QQ 官方入站消息 → 平台中立 ``MessageContext``，以及 Chat History 落库。 |
| `atri_x.platform.qqofficial.send` | QQ 官方出站发送：文本分页、Markdown 降级、媒体上传、键盘、Ark。 |
| `atri_x.platform.qqofficial.target` | qqofficial 的发送落点与 ctx 传递。 |
| `atri_x.platform.qqofficial.token` | QQ 官方 access token 的获取与缓存。 |
| `atri_x.platform.qqofficial.types` | QQ 官方机器人网关与消息 API 的线上类型。 |
| `atri_x.platform.refcontext` | 引用的恢复与续聊/Fork 判定。 |
| `atri_x.platform.telegram` | Telegram 平台适配器（对应 Go 包 ``internal/platform/telegram``）。 |
| `atri_x.platform.telegram.adapter` | Telegram 适配器主体：long polling 入站循环与出站发送。 |
| `atri_x.platform.telegram.capabilities` | Telegram 的可选群目录能力（群信息 + 管理员列表）。 |
| `atri_x.platform.telegram.client` | Telegram Bot API 客户端（long polling、发送、媒体上传与下载）。 |
| `atri_x.platform.telegram.config` | Telegram 适配器配置与装配入口。 |
| `atri_x.platform.telegram.format` | Markdown → Telegram 实体文本（HTML）渲染。 |
| `atri_x.platform.telegram.keyboard` | 高风险工具确认的内联键盘。 |
| `atri_x.platform.telegram.message` | 入站消息归一化：文本、@提及、媒体段、Chat History 落库与媒体解析。 |
| `atri_x.platform.telegram.stream` | 流式助手消息：增量预览 + 最终替换。 |
| `atri_x.platform.telegram.target` | 发送落点与 ctx 传递（Python 侧拆出的最小模块）。 |
| `atri_x.platform.telegram.types` | Telegram Bot API 的线上类型与请求体。 |

## Hook（`atri_x/hook/`）

| 模块 | 职责 |
|---|---|
| `atri_x.hook` | Hook 与插件。 |
| `atri_x.hook.builtin` | 内置 Hook 插件：规则引擎、天使记忆、自我学习、出站历史。 |
| `atri_x.hook.builtin.angel_memory` | 内置 Hook：按平台/会话召回长期记忆，追加为临时 system 上下文。 |
| `atri_x.hook.builtin.register` | 内置 Hook 插件的注册入口。 |
| `atri_x.hook.builtin.self_learning` | 内置 Hook：群语料观察与表达/黑话上下文注入。 |
| `atri_x.hook.control` | Hook 控制面：给命令层用的列表 / 热重载 / 启停。 |
| `atri_x.hook.event` | Hook 事件点、事件 payload 与 Handler 基础类型。 |
| `atri_x.hook.manager` | 普通 Hook 的注册、排序、执行与原子快照替换。 |
| `atri_x.hook.match` | Hook 条件匹配、字段取值与模板值。 |
| `atri_x.hook.media` | 宿主 Media 能力（Hook 进程内的 Go 插件用）。 |
| `atri_x.hook.output` | Hook 输出的线格式。 |
| `atri_x.hook.output.jsonstrict` | 严格 JSON 解码：拒绝未知字段、拒绝尾随值。 |
| `atri_x.hook.output.message` | 进程 Hook 替换消息内容的线格式（``message.segments``）。 |
| `atri_x.hook.output.output` | Hook 输出的线格式：``outputs`` 段的解码与构造。 |
| `atri_x.hook.process` | 进程 Hook 继承的环境。 |
| `atri_x.hook.protocol` | ``hook.v2`` 进程间协议。 |
| `atri_x.hook.protocol.eventjson` | ``hook.Event`` 与其负载的 JSON 投影。 |
| `atri_x.hook.protocol.frames` | ``hook.v2`` JSON Lines 协议帧。 |
| `atri_x.hook.protocol.stream` | ``hook.v2`` 的按行读取（带帧上限）。 |
| `atri_x.hook.rules` | ``hooks.toml`` 规则引擎。 |
| `atri_x.hook.rules.action` | 规则动作的执行：改文本、发消息、跑工具、调插件。 |
| `atri_x.hook.rules.config` | ``hooks.toml`` 与插件 ``hook.toml`` 的装载。 |
| `atri_x.hook.rules.detail` | ``/hooks`` 列表里展示的规则详情文本。 |
| `atri_x.hook.rules.exec` | ``exec`` 动作：跑一次 hook.v2 一次性插件。 |
| `atri_x.hook.rules.exec_stderr` | ``exec`` 动作的进程辅助：stderr 尾巴、错误包装、cwd 解析。 |
| `atri_x.hook.rules.rule` | 规则的校验、角色条件展开与执行。 |
| `atri_x.hook.rules.rules` | 规则模块：装载 ``hooks.toml`` 并把每条规则注册成 Hook。 |
| `atri_x.hook.rules.toml_error` | ``hooks.toml`` 的读取与详细错误信息。 |
| `atri_x.hook.rules.types` | ``hooks.toml`` 的配置类型。 |
| `atri_x.hook.runtime` | 有状态 Hook 运行时（hook.v2 进程）。 |
| `atri_x.hook.runtime.config` | 有状态 Hook 进程的配置、状态与运行期选项。 |
| `atri_x.hook.runtime.manager` | 有状态 Hook 运行时的协调者：应用配置、启停 worker、路由事件。 |
| `atri_x.hook.runtime.media` | ``media.*`` RPC：把宿主 MediaAPI 暴露给进程 Hook。 |
| `atri_x.hook.runtime.protocol` | 有状态 Hook 进程的协议负载（``system.init`` / ``event.handle``）。 |
| `atri_x.hook.runtime.routes` | Hook 路由：租约、等待态、单飞调用与工具上下文令牌。 |
| `atri_x.hook.runtime.shared` | Hook 共享的有界 JSON 协调存储。 |
| `atri_x.hook.runtime.tool_bridge` | 进程 Hook 发来的请求：``tool.call`` / ``hooks.reload`` / ``media.*`` / ``shared.*``。 |
| `atri_x.hook.runtime.transport` | worker 的协议传输层：读写帧、请求/响应配对、进程强杀。 |
| `atri_x.hook.runtime.worker` | 一个有状态 Hook 进程（hook.v2 worker）。 |

## GUI 页（`atri_x/gui/tabs/`）

| 模块 | 职责 |
|---|---|
| `atri_x.gui.tabs` | 桌面端的选项卡集合。 |
| `atri_x.gui.tabs.advanced` | 「进阶」选项卡：生图/视觉/语音、记忆与群功能开关、日志级别。 |
| `atri_x.gui.tabs.base` | 配置选项卡的公共骨架：底部状态行、错误弹窗、"需要重启"提示。 |
| `atri_x.gui.tabs.character` | 「人设卡」选项卡：角色库编辑 + 系统人设（SOUL.md）。 |
| `atri_x.gui.tabs.chat` | 「对话」选项卡：会话列表 + 聊天记录 + 输入框。 |
| `atri_x.gui.tabs.extractpanel` | 「模型 → 抓取服务」子页：``web_extract`` 的通道列表、表单、顺序与密钥。 |
| `atri_x.gui.tabs.features` | 「功能」选项卡：功能状态一览 + 服务/日志面板。 |
| `atri_x.gui.tabs.knowledge` | 「知识库」选项卡：群知识库 / FAQ 的全局配置 + 现有条目查看。 |
| `atri_x.gui.tabs.link` | 「在线」选项卡：一条线看住「后台服务 → 协议端 → QQ 平台」三段，并提供上下线开关。 |
| `atri_x.gui.tabs.logs` | 「日志」选项卡：把后台服务写的日志和界面操作日志放到一个可筛选的窗口里。 |
| `atri_x.gui.tabs.members` | 「群友」选项卡：按群管理群友的角色数据（备注名 / 标签 / 角色数据 / 头像）。 |
| `atri_x.gui.tabs.model` | 「模型」选项卡：LLM Provider（含 API Key）、模型分配、搜索服务与抓取服务。 |
| `atri_x.gui.tabs.mountpanel` | 「进阶」页生图区的本地挂载面板（:class:`MountPanelMixin`）。 |
| `atri_x.gui.tabs.permission` | 「权限」选项卡：超级管理员名单 + 风险等级 / 工具轮次 / 成员建卡开关。 |
| `atri_x.gui.tabs.platform` | 「平台」选项卡：QQ / Telegram / 本地 CLI 服务端 / 超级管理员。 |
| `atri_x.gui.tabs.research` | 「研究」选项卡：查看科研助理的运行与产物（只读）。 |
| `atri_x.gui.tabs.searchpanel` | 「模型 → 搜索服务」子页：``web_search`` 的通道列表、表单、顺序与密钥。 |
| `atri_x.gui.tabs.timepanel` | 「时间」选项卡：统一时间服务（``[time]`` / ``[time.verification]`` / ``[[time.sources]]``）。 |
| `atri_x.gui.tabs.toolperm` | 「工具权限」选项卡：按群、按会话模式管理工具授权（``state.toml`` 的 ``[group_policy]``）。 |

## 存储与网络

| 模块 | 职责 |
|---|---|
| `atri_x.llm.breaker` | Provider 级熔断器。 |
| `atri_x.llm.errors` | 结构化上游 API 失败与确定性失败分类。 |
| `atri_x.llm.llm` | LLM 抽象层类型。 |
| `atri_x.llm.openai` | OpenAI-compatible 适配器包。 |
| `atri_x.llm.openai.errors` | 上游失败响应 → ``APIError`` 的错误分类。 |
| `atri_x.llm.openai.openai` | OpenAI-compatible 适配器（Chat Completions）。 |
| `atri_x.llm.openai.responses` | OpenAI Responses 协议适配器。 |
| `atri_x.llm.openai.stream` | SSE 行扫描与首包/空闲超时。 |
| `atri_x.llm.openai.wire` | OpenAI-compatible 线上格式：请求信封构造、响应字段解析与日志摘要。 |
| `atri_x.llm.origin` | 「这段已有历史能不能交给目标模型」的唯一入口。 |
| `atri_x.llm.protocol` | 协议能力矩阵的唯一入口。 |
| `atri_x.llm.segment` | 消息段 helper。 |
| `atri_x.llm.tooltext` | 把「以正文形式泄漏的工具调用」还原成结构化调用。 |
| `atri_x.llm.usage_sink` | 旁路 LLM 用量的统一出口。**Python 版新增**。 |
| `atri_x.net` | 网络层。对应 Go 的标准库 ``net/http`` 与 ``github.com/coder/websocket``。 |
| `atri_x.net.cronspec` | cron 表达式解析与下次触发时间：对应 Go 的 ``robfig/cron/v3``。 |
| `atri_x.net.httpclient` | HTTP 客户端：对应 Go 的 ``net/http`` 用法。 |
| `atri_x.net.s3` | 最小 S3 客户端（SigV4 签名 + REST）：对应 Go 的 ``aws-sdk-go-v2/service/s3``。 |
| `atri_x.net.tomlw` | TOML 写入：对应 Go 的 ``pelletier/go-toml/v2``（写入侧）。 |
| `atri_x.net.websocket` | WebSocket（RFC 6455）客户端与服务端：对应 Go 的 ``github.com/coder/websocket``。 |
| `atri_x.storage` | 存储包。对应 Go：``internal/storage``。 |
| `atri_x.storage.ids` | 通用 ID helper。对应 Go：``internal/storage/id.go``。 |
| `atri_x.storage.models` | 存储领域模型。对应 Go：``internal/storage/storage.go``（领域模型部分；repository |
| `atri_x.storage.repositories` | 存储 repository 抽象。对应 Go：``internal/storage/storage.go``（repository |
| `atri_x.storage.sqlite` | SQLite 存储实现包。对应 Go：``internal/storage/sqlite``。 |
| `atri_x.storage.sqlite.atrinis_event_repository` | Atrinis 事件与报告投递（outbox）仓储。对应 Go：``internal/storage/sqlite/atrinis_event_repository.go``。 |
| `atri_x.storage.sqlite.chat_history_repository` | 入站聊天历史与出站消息仓储。对应 Go：``internal/storage/sqlite/chat_history_repository.go``。 |
| `atri_x.storage.sqlite.context_summary_repository` | 上下文摘要仓储。对应 Go：``internal/storage/sqlite/context_summary_repository.go``。 |
| `atri_x.storage.sqlite.cron_job_repository` | 定时任务仓储。对应 Go：``internal/storage/sqlite/cron_job_repository.go``。 |
| `atri_x.storage.sqlite.media_history` | 历史消息媒体位置。对应 Go：``internal/storage/sqlite/media_history.go``。 |
| `atri_x.storage.sqlite.media_lifecycle` | 媒体清理认领、输出索引、启动恢复与只读审计。对应 Go：``internal/storage/sqlite/media_lifecycle.go``。 |
| `atri_x.storage.sqlite.media_repository` | 媒体与媒体引用仓储。对应 Go：``internal/storage/sqlite/media_repository.go``。 |
| `atri_x.storage.sqlite.message_repository` | 会话消息仓储。对应 Go：``internal/storage/sqlite/message_repository.go``。 |
| `atri_x.storage.sqlite.migrations` | SQLite schema migration。对应 Go：``internal/storage/sqlite/migrations.go``。 |
| `atri_x.storage.sqlite.session_repository` | 会话仓储。对应 Go：``internal/storage/sqlite/session_repository.go``。 |
| `atri_x.storage.sqlite.store` | SQLite store：连接、事务与门面。对应 Go：``internal/storage/sqlite/store.go``。 |
| `atri_x.storage.sqlite.tool_call_repository` | 工具调用审计仓储。对应 Go：``internal/storage/sqlite/tool_call_repository.go``。 |
| `atri_x.storage.storage` | 存储领域模型与 repository 抽象（对外统一出口）。对应 Go：``internal/storage/storage.go``。 |
| `atri_x.storage.times` | 通用时间 helper。对应 Go：``internal/storage/time.go``。 |

## 任务入口（task → locator → 代码）

| 任务 | 文档锚点 | 先看代码 | 相关文档 |
|---|---|---|---|
| 启动、装配、配置 | `locator:startup`、`locator:config` | `atri_x/app/app.py`、`atri_x/config` | `devdocs/architecture.md`、`docs/configuration.md` |
| Agent 对话流程 | `locator:agent-chat` | `atri_x/agent/core.py`、`atri_x/agent/chat.py` | `devdocs/architecture.md` |
| Slash 命令与补全 | `locator:commands` | `atri_x/agent/commands`、`atri_x/command` | `docs/commands.md` |
| Request / Turn 状态 | `locator:request-turn` | `atri_x/request/manager.py`、`atri_x/turn/manager.py` | — |
| 工具调用链路 | `locator:tool`、`locator:tool-flow` | `atri_x/tool`、`atri_x/toolrun` | `devdocs/code-map.md` |
| 内置工具实现 | `locator:tool` | `atri_x/tool/builtin` | `devdocs/code-map.md`、`docs/dev/find-it.md` |
| 内容安全（道德限制 / 敏感信息） | `locator:safety` | `atri_x/safety` | `docs/safety.md` |
| 识图（两段式 / 隐私开关） | `locator:vision` | `atri_x/vision`、`atri_x/agent/vision.py` | `docs/vision.md` |
| Skill / Atriyph | `locator:skill` | `atri_x/tool/skill`、`atri_x/atriyph` | — |
| Hook / 插件 | `locator:hook` | `atri_x/hook` | `docs/hooks.md` |
| 信号与日志边界 | `locator:signals` | `atri_x/signal` | `devdocs/signals-and-logging.md` |
| 健康状态与运维接口 | `locator:health-ops` | `atri_x/health`、`atri_x/app/ops_health.py` | — |
| Output / 发送 | `locator:output` | `atri_x/delivery`、`atri_x/agent/output.py` | — |
| 平台适配 | `locator:platform` | `atri_x/platform` | `docs/snowluma.md` |
| Session / 上下文 | `locator:session`、`locator:context` | `atri_x/session`、`atri_x/contextmgr` | — |
| SQLite / 存储 | `locator:storage` | `atri_x/storage`、`atri_x/storage/sqlite` | — |
| Atrinis / Atriena / Atriwisp | `locator:atrinis` | `atri_x/atrinis`、`atri_x/atriena` | `docs/atrinis.md` |
| Cron / 维护任务 | `locator:cron` | `atri_x/cron`、`atri_x/maintenance` | — |
| 时间服务 / 时区 / 网络校验 | `locator:time` | `atri_x/time_service.py`、`atri_x/timezones.py`、`atri_x/timesync` | — |
| Go → Python 移植状态 | `locator:porting` | `atri_x` | `PORTING.md`、`devdocs/porting-status.md` |
| Windows 打包 / 桌面端 / 安装器 | `locator:packaging` | `packaging`、`atri_x/gui`、`atri_x/sidecar` | `packaging/README.md`、`docs/windows-app.md` |
| 文档维护 | `locator:docs` | `docs`、`devdocs` | `docs/dev/START-HERE.md` |
| 测试与门禁 | `locator:testing` | `scripts`、`tests` | `docs/dev/START-HERE.md` |

## 文档锚点（locator 定义全量）

定义 = 文档里的 `<!-- locator:NAME -->` 注释；用 `rg -n "locator:NAME" .` 跳到对应章节。

| locator | 定义位置 |
|---|---|
| `locator:NAME` | `AGENTS.md:33` |
| `locator:agent-chat` | `devdocs/architecture.md:85` |
| `locator:atrinis` | `devdocs/architecture.md:370` |
| `locator:commands` | `devdocs/architecture.md:114` |
| `locator:config` | `devdocs/architecture.md:30` |
| `locator:context` | `devdocs/architecture.md:330` |
| `locator:cron` | `devdocs/architecture.md:393` |
| `locator:health-ops` | `devdocs/architecture.md:50` |
| `locator:hook` | `devdocs/architecture.md:229` |
| `locator:output` | `devdocs/architecture.md:257` |
| `locator:platform` | `devdocs/architecture.md:277` |
| `locator:request-turn` | `devdocs/architecture.md:134` |
| `locator:session` | `devdocs/architecture.md:310` |
| `locator:signals` | `devdocs/signals-and-logging.md:1` |
| `locator:skill` | `devdocs/architecture.md:207` |
| `locator:startup` | `devdocs/architecture.md:11` |
| `locator:storage` | `devdocs/architecture.md:349` |
| `locator:tool` | `devdocs/architecture.md:170` |
| `locator:tool-flow` | `devdocs/architecture.md:149` |
| `locator:vision` | `docs/vision.md:10` |

