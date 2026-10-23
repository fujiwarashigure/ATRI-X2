# 配置说明

ATRI-X 使用一个主配置入口加载应用配置和运行态状态；LLM Provider、生图等稳定服务配置默认集中到只读的 `services.toml`。默认配置由程序内置 assets 生成到平台配置目录；已有配置文件不会被覆盖。

## 配置文件职责

所有配置文件均由程序内置 assets 首次运行时自动生成到平台配置目录，已有文件不会被覆盖；源码中不再保留 `config/` 目录。

| 文件或目录 | 职责 |
| --- | --- |
| `app.toml` | 主配置入口，保存行为、平台、工具、安全、维护等应用配置，并记录各独立配置文件的相对路径。 |
| `services.toml` | 共享只读服务配置：`[providers.*]`、`[model_metadata]`、`[model_profiles]`、`[image_generation]`、`[image_to_prompt]`、`[vision]`、`[asr]`。多个服务可以挂载同一个文件；密钥仍只放在 `.env`。 |
| `providers.toml` | 旧版 LLM Provider 配置；仍兼容，只有 `[config_files].providers` 指定时才读取。新部署建议统一使用 `services.toml`。 |
| `atrinis.toml` | Atrinis 监听枢纽配置，保存 HTTP、token、delivery、allowed_tools 和 Atriwisp 策略。 |
| `state.toml` | 运行态状态，例如默认 Session 模式、chat/work/compact/naming 模型选择。 **该文件会被 ATRI-X 运行时回写，不能和 `services.toml` 共用。** |
| `tool_tags.toml` | 给工具添加 tag 和 prompt 的配置文件。 |
| `SOUL.md` | Agent 的 System Prompt 来源文件。 |
| `.env` | 可选，本地密钥文件，不建议提交；首次自动生成的是 `.env.example`，不会直接生成 `.env`。 |
| `plugins/.env` | 自动生成的 Hook 公共环境文件；默认只有注释，可补充公共变量和额外 `PATH`。 |
| `plugins/` | Hook 和插件配置目录。 |
| `skills/` | 用户侧 Skill 目录，默认位于配置目录下；当前子目录为 `skills/agent/` 和 `skills/go/`。AgentSkill 可在根目录放置 `ATRIX_SKILL.toml` 配置可见性或注册为普通工具。 |
| `memories.toml` | 常驻记忆文件，默认位于配置目录下。 |
| `long_memory/` | 长期记忆 Markdown 源数据目录，默认位于配置目录下。 |

## 主配置查找顺序

启动时主配置按以下顺序查找：

1. 命令行 `--config`。
2. 环境变量 `ATRIX_CONFIG_FILE`。
3. 平台配置目录：Windows `%APPDATA%/ATRI-X/app.toml`；Linux 使用 XDG 配置目录。
4. 若平台配置不存在，则自动在平台配置目录生成默认配置文件。已有配置不会被覆盖。自动生成只在没有显式 `--config` 和 `ATRIX_CONFIG_FILE` 时触发。若显式指定的配置路径不存在，ATRI-X 会报错而不是偷偷生成，避免掩盖路径拼写错误。

开发期直接运行即可使用平台配置目录：

```bash
python -m atri_x
```

如需使用临时配置文件，也可以显式指定 `--config`。

## 相对路径规则

相对路径默认基于主配置文件所在目录解析。

例如平台配置目录下的 `app.toml` 写入：

```toml
[config_files]
services = "services.toml"
# providers 仅用于兼容旧部署；services 存在时不再读取 providers.toml。
# providers = "providers.toml"
state = "state.toml"
atrinis = "atrinis.toml"

[soul]
path = "SOUL.md"
```

这些路径都会解析到主配置文件所在目录下；默认情况下就是平台配置目录。

## 环境变量与进程环境继承

ATRI-X 使用以下环境来源：

- ATRI-X 进程环境：由终端、服务管理器或容器在启动时提供。
- 配置根 `.env`：与主配置文件同目录，用于 Provider Key、平台 Secret、CLI/Atrinis token、内置工具变量和 LLM Shell。
- `plugins/.env`：所有 Hook 共用的环境层。
- `plugins/<plugin-id>/.env`：单个插件 exec 和 Worker 的环境层。

### 配置变量与密钥

`api_key_env`、`token_env`、`client_secret_env`、`access_token_env`、`bot_token_env`、`proxy_url_env` 等字段保存的是环境变量名。读取对应值时，优先级统一为：

1. ATRI-X 进程环境。
2. 配置根 `.env`。

Docker Compose 的 `env_file`、`docker run -e`、systemd `EnvironmentFile` 注入的变量都属于 ATRI-X 进程环境。ATRI-X 会把名字含独立 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`PRIVATE` 词段的变量从 Shell / Go Skill 子进程环境中移除；内置 DSH 子进程同样只拿到 `[agent.dsh]` 显式注入的那一个模型凭据（见 [`docs/windows-app.md`](windows-app.md) §8.1），其余敏感变量一并过滤。但父进程和 Web 搜索、生图、媒体下载等工具仍可读取这些密钥；如果向群聊用户开放工具，不能只依赖“容器不是 root”来保护其中的 API Key。

推荐把真实密钥放在系统环境或配置根 `.env`，不要直接写入 TOML 或提交到仓库。例如：

```dotenv
DEEPSEEK_API_KEY=your-api-key
OPENAI_API_KEY=your-api-key
QQOFFICIAL_CLIENT_SECRET=your-client-secret
QQONEBOT_ACCESS_TOKEN=your-access-token
TELEGRAM_BOT_TOKEN=your-bot-token
ATRIX_CLI_LOCAL_TOKEN=your-cli-token
ATRINIS_HOME_TOKEN=your-atrinis-token
```

配置根 `.env` 中的变量会作为“凭据环境”提供给 Web 搜索、生图、媒体下载等父进程工具，但不会整体注入 LLM Shell：Shell 与 Go Skill 只继承其中的非凭据变量。

### Shell 与 Hook 环境

| 进程入口 | 环境来源与优先级 |
| --- | --- |
| 内置工具与 Go Skill（含 LLM Shell） | ATRI-X 进程环境与配置根 `.env` 的普通变量；名字像密钥的变量会被移除。 |
| Web 搜索 / 生图 / 媒体下载 | ATRI-X 进程环境为基础，配置根 `.env` 补充凭据变量。 |
| 根 Hook 规则 | ATRI-X 进程环境为基础，`plugins/.env` 覆盖同名普通变量。 |
| 插件 exec 与 Worker | 根 Hook 环境为基础，插件配置文件同目录的 `.env` 再覆盖同名普通变量。 |

ATRI-X 自行读取配置文件时，配置根 `.env` 不会注入 Hook。所有 Hook 共用的代理、工具路径或变量应放在 `plugins/.env`，仅供单个插件使用的变量应放在该插件的 `.env`。一次性 exec、Persistent Worker 和 Transient Worker 使用相同规则。

需要代理时，可在对应环境层设置标准变量；子进程是否使用仍取决于具体程序：

```dotenv
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

### PATH 规则与 systemd

ATRI-X 自行合并上述环境层时，`PATH` 不按普通变量覆盖，而是按照层级依次追加并去重。这里的基础 PATH 是 ATRI-X 启动时已经获得的进程 PATH；后续 `.env` 只需填写要增加的绝对目录：

```dotenv
PATH=/home/atri-x/.local/bin:/usr/local/go/bin
```

不要填写 `$PATH`、`%PATH%` 或 `~`，dotenv 不执行变量替换或路径展开。没有额外目录时可以省略 `PATH`。

systemd 用户服务没有显式设置 PATH 时，ATRI-X 使用服务管理器提供的 PATH。`EnvironmentFile` 本身不会影响 PATH，但文件中一旦包含 `PATH=`，systemd 会在启动 ATRI-X 前完整覆盖原值；ATRI-X 只能在这个结果上继续追加，无法恢复已经丢失的系统目录。systemd 的 `EnvironmentFile` 同样不会展开 `$PATH`。

由 ATRI-X 自行读取配置根 `.env` 时，systemd unit 不需要重复加载该文件；其中的 PATH 按上面的追加规则处理。若 unit 使用 `EnvironmentFile` 加载同一文件，则其中的 PATH 必须写成完整值

通过 `EnvironmentFile` 加载后，该文件中的全部变量都已经属于 ATRI-X 进程环境，因而也会被 Shell 和 Hook 继承，不再具有“配置根 `.env` 不注入 Hook”的隔离效果。

### 生效时机

配置根 `.env` 或 systemd 环境在重启 ATRI-X 后生效。`plugins/.env` 和插件 `.env` 在启动及 `/hooks reload` 时重新读取，reload 会按新环境重建 Worker；文件不存在时视为空配置。


## 共享服务配置 services.toml

`services.toml` 是只读的集中服务配置，适合 ATRI-X 与其他服务读取同一份端点定义。`app.toml` 通过 `[config_files].services` 指向它：

```toml
[config_files]
services = "services.toml"
state = "state.toml"
```

文件内容按 section 划分，当前支持：

```toml
# LLM Provider：与旧 providers.toml 的 [providers.*] 完全兼容
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
models = ["gpt-4o-mini"]

[model_metadata]
default_context_window = 256000

# 可选：@model: 命名 profile 也可以集中在这里
[model_profiles.fast]
provider = "deepseek"
model = "deepseek-v4-flash"
aliases = ["快"]

# 生图基础配置与命名 profile
[image_generation]
enabled = false
base_url = "https://your-relay.example.com/v1"
api_key_env = "IMAGE_API_KEY"
model = "gpt-image-2.5"

[image_generation.profiles.fast]
quality = "medium"

# 可选：image_to_prompt 复用上面的 [providers.*] 作为视觉后端
[image_to_prompt]
provider = "openai"
model = "gpt-4o-mini"

# 可选：语音转写，复用上面的 [providers.*]
# [asr]
# enabled = false
# provider = "openai"
# model = "whisper-1"
```

说明：

- 读取优先级：`services.toml` 中存在该 section 时覆盖旧 `providers.toml` / `app.toml` 中的同名配置；不存在时保留旧文件的值，便于逐步迁移。
- `state.toml` 不能和 `services.toml` 共用。`state.toml` 会被 ATRI-X 运行时回写，共用会导致 `SaveState` 把其他静态配置覆盖掉；ATRI-X 加载时也会直接拒绝这种配置。
- 密钥不要写入 `services.toml`。继续使用 `api_key_env` 指向进程环境或配置根 `.env`。
- 其他服务可以只读挂载同一个文件，只读取自己需要的 section；建议使用 `:ro`。例如：

```yaml
services:
  atri-x:
    volumes:
      - ./data/config/atri-x/services.toml:/data/config/atri-x/services.toml:ro
      - ./data/config/atri-x/state.toml:/data/config/atri-x/state.toml

  image-adapter:
    volumes:
      - ./data/config/atri-x/services.toml:/etc/atri-x/services.toml:ro
```

- 修改 `services.toml` 后需要重启或 recreate ATRI-X 容器；当前不热加载该文件（`state.toml` 相反：它会被运行时回写，并支持外部编辑热加载，见[外部修改 `state.toml` 的热加载](#外部修改-statetoml-的热加载)）。
- 旧部署不配置 `[config_files].services`、继续使用 `[config_files].providers = "providers.toml"` 时，行为与旧版本一致。


## 运维健康接口

设置 `ATRIX_HEALTH_ADDR`（进程环境或配置根 `.env`）后，ATRI-X 会启动一个**不依赖 Atrinis** 的独立 HTTP 健康接口：

```dotenv
# 原生部署默认只监听回环；容器内需要监听 0.0.0.0，宿主机端口仍只映射到 127.0.0.1。
ATRIX_HEALTH_ADDR=127.0.0.1:32171
ATRIX_HEALTH_LIVE_STALE_SECONDS=90
# /tasks、/metrics、/diagnostics、/plugins/* 和 /healthz 的访问 token。
# 未设置时这些敏感接口默认不注册；只有显式开启不安全模式才会无鉴权暴露：
# ATRIX_OPS_TOKEN=请替换为随机长字符串
# ATRIX_OPS_ALLOW_UNAUTHENTICATED=1
```

`ATRIX_HEALTH_LIVE_STALE_SECONDS` 建议明显大于调度心跳间隔；当前实现约每 10 秒更新一次心跳，未启用任何平台时也会持续发送心跳。

- `GET /live`：只表示进程仍在运行；进程存活时 HTTP 返回 200，不判断模型、平台或调度心跳。
- `GET /ready`：进程已初始化、SQLite / 数据目录可写，且调度心跳已开始且未过期。平台未连接、模型 API 故障不会让它失败。
- `GET /healthz`：汇总状态。设置 `ATRIX_OPS_TOKEN` 后，`/healthz` 需要 `Authorization: Bearer <token>` 或 `X-Atrix-Ops-Token: <token>`；未设置且未显式允许无鉴权时该接口不注册（只保留 `/live`、`/ready`）。平台或模型故障显示为 `degraded`，调度心跳过期时 `/ready` 与 `/healthz` 返回 `not_ready`。HTTP 状态不应单独作为自动重启依据。
- `GET /tasks`：活跃任务、阶段、开始时间、最近进展和 `queued_by_kind` 排队积压。
- `GET /metrics`：任务/资源/平台/模型状态、熔断状态、限速阈值与拒绝计数、生图队列状态。
- `GET /diagnostics`：面向“机器人没回复”的聚合诊断，包含排队/超时、限速命中、熔断状态和最近一次重启原因。

`/tasks`、`/metrics`、`/diagnostics`、`/plugins/*` 是敏感运维接口。默认安全策略是：**未设置 `ATRIX_OPS_TOKEN` 时不注册这些接口**，只保留 `/live`、`/ready`。本机排障需要临时无鉴权访问时，显式设置 `ATRIX_OPS_ALLOW_UNAUTHENTICATED=1`；这应当只用于回环或完全可信的临时环境。接口只应监听容器内部或宿主机回环地址，不要在 Nginx 中无鉴权暴露到公网。

平台和模型的 `last_error` 在写入健康快照前会先做凭据脱敏（`sk-` / `Bearer` / `api_key=` / JSON 凭据 / Telegram bot token / URL userinfo 等），以免上游错误里的 token 通过 `/healthz`、`/metrics`、`/diagnostics` 泄露。这是第二道防线，不改变"这些接口必须限制在回环或可信内网"的前提。

## Workspace 工具

在 work / chat 模式中（本仓两种模式都支持工具），超级管理员可以让 LLM 调用 `workspace` 工具切换当前 Session 的共享工作目录。切换后，`read_file`、`edit_file`、`send_file` 和前台 `shell` 等路径类工具会基于该目录解析相对路径，避免每次都传完整路径。

某个目录的 `workspace` 工具首次被发现或注入，或者首次切换、重置到该目录时，如果目录根部存在 `AGENTS.md` 或 `AGENT.md`，系统会自动把文件内容附带给 LLM，供其读取当前目录的工作约定。文件名主体必须是大写 `AGENTS` 或 `AGENT`，`.md` 后缀大小写不限；`AGENTS.md` 优先于 `AGENT.md`。

自动附带的说明文件最大为 64 KiB。超过限制时不会读取内容，也不会标记为已附带，工具结果会提示需要缩短或拆分该文件后再切换 workspace。

## 常驻记忆配置

常驻记忆数据默认保存在配置目录的 `memories.toml`，文件由程序首次运行时生成。主配置中可以设置 core/normal 两段常驻记忆的长度上限：

```toml
[resident_memory]
core_max_units = 200
normal_max_units = 300

# P1/P2 normal 写入保护；显式写 0 可关闭对应限制。
normal_write_min_interval_seconds = 5
normal_write_window_seconds = 60
normal_write_max_per_window = 6
normal_max_lines = 20
normal_max_units_per_entry = 80
normal_block_instruction_patterns = true
```

长度单位 `units` 可以近似理解为“中文按字数、英文按单词”：中日韩字符按单字计数，英文/数字连续片段按一个词计数。core 是高风险核心记忆，普通用户修改自己的 core 时也必须由本人确认。normal 是可直接整理的低风险普通记忆，不需要确认；注入 Prompt 时 core 与 normal 会分行展示，normal 的每条记忆渲染为一条 `-` 列表项，并包在 `<resident_memory>` 边界内，同时声明为用户数据而非系统指令。记忆内容中的尖括号会被转义，防止内容提前结束或伪造边界标签。

normal 以“每条一行、一行一件事”的结构化方式保存：写入时会去掉 `-` / `*` / `1.` 等列表前缀、去掉空行、合并多余空白、按大小写不敏感去重，再用换行连接。`resident_memory_read` 返回的 normal 就是这种一行一条的形式。

normal 写入还有服务端保护：

- `normal_write_min_interval_seconds`：同一平台、同一 actor 两次 normal 写入的最小间隔；0 关闭。
- `normal_write_window_seconds` + `normal_write_max_per_window`：窗口内最多写入次数；任一项为 0 关闭。
- 校验失败、内容被拒绝的写入不会消耗写入次数。
- `normal_max_lines`：normal 最大条目数（每条一行）；0 关闭。
- `normal_max_units_per_entry`：单条 normal 的最大长度；0 关闭。
- `normal_block_instruction_patterns`：逐条拒绝明显的指令类内容（默认 true）；false 只关闭模式匹配，控制字符、条目数和单条长度检查仍生效。
- 这些限制只作用于 normal；core 仍由 high risk + 确认流程控制。

`memories.toml` 采用原子写入：先写同目录临时文件并 `fsync`，再 `rename` 覆盖目标文件，最后尽力 `fsync` 目录。崩溃或断电时只会留下完整的旧文件或完整的新文件，不会出现被截断的记忆文件。写入前还会比对文件状态；如果检测到外部（例如手工编辑或恢复脚本）在读取和写入之间修改了 `memories.toml`，会重新加载后再应用本次修改，避免覆盖外部改动。

## Provider 配置

Provider 写在 `services.toml`（旧部署仍可写在 `providers.toml`，由 `[config_files].providers` 指定）：

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy = ""                          # 可选，HTTP/SOCKS5 代理地址
extra_payload = { provider_field = "xxx" }  # 可选，Provider 级 extra payload

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
models = ["gpt-4o-mini"]             # 手动补充模型列表（API 获取不到时使用）
api_mode = "chat"                    # chat（默认）/ response，见下方说明
reasoning_effort = "medium"          # 推理思考强度：none/minimal/low/medium/high；留空 = 不发送

# 可选：网关要求的自定义请求头（保留头不能覆盖）
[providers.openai.extra_headers]
x-opencode-session = "atri-x-local"

# 可选：为特定模型配置 context_window 或 extra_payload
# [providers.openai.model_configs."gpt-4o-mini"]
# context_window = 128000
# extra_payload = { }
# api_mode = "response"              # 按模型覆盖 Provider 级协议
# reasoning_effort = "high"          # 按模型覆盖 Provider 级思考强度

[model_metadata]
default_context_window = 256000
```

说明：

- `base_url` 使用 Provider 的 OpenAI-compatible API 地址。
- `api_mode` 选择上游协议：`chat`（默认）请求 `POST {base_url}/chat/completions`，`response` 请求 `POST {base_url}/responses`；也兼容 `chat_completions`、`responses` 等写法，其他值在启动阶段直接报错，不会静默回退。协议与会话模式（`chat` / `work` / `background`）相互独立。`[providers.<name>.model_configs."<model>"].api_mode` 可按模型覆盖 Provider 级设置，所以同一个 Provider 能混用两种协议（例如默认 `response`，个别只有 chat 的模型单独写 `chat`）。选 `response` 时 `base_url` 要写到包含 `/v1` 的前缀，因为端点只拼 `/responses`；请求会把 system 段合并为 `instructions`、把工具调用/结果转成 `function_call` / `function_call_output` item、把工具定义扁平化，并固定带 `store = false`（`store` 不在保留字段里，可用 `extra_payload` 覆盖）。Responses 的流式事件会归一化成与 chat 相同的文本 / reasoning / 工具调用 / usage 输出，熔断、健康状态、视觉兜底和用量统计无需额外配置。
- `api_key_env` 指向环境变量名，推荐用这种方式保存密钥。
- `proxy` 可选，支持 `http://` 和 `socks5://` 代理地址；省略或留空时直连，不继承 ATRI-X 进程的 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理。该设置同时作用于模型列表和聊天请求。
- `models` 是手动补充的模型名列表，当 Provider 的模型列表接口获取不到某些模型时使用。
- `[providers.<name>.model_configs."<model>"]` 为特定模型配置 `context_window`、`extra_payload`、`vision` 和 `audio`，都是可选的。
- `reasoning_effort` 是**推理思考强度**，取值 `none` / `minimal` / `low` / `medium` / `high`（大小写不敏感），写进 LLM 请求 JSON 的 `reasoning_effort` 字段；留空表示**不发送**该字段。只对支持这个参数的模型/网关有效（OpenAI o 系、部分中转站），不支持的模型请保持留空——它不会自动降级，发错会由上游返回 400。Provider 级写在 `[providers.<name>]`，`[providers.<name>.model_configs."<model>"]` 可按模型覆盖（空串表示回落到 Provider 级）。桌面端「模型」页的 Provider 表单可以直接选这一项；启动期与桌面端保存前用同一个白名单校验，写错会直接报错而不是静默发出去。个别网关的私有档位（例如 `xhigh`、`auto`）请改用 `extra_payload` 透传。
- `vision` / `audio` 是可选的三态能力声明：`true` 表示声明支持，`false` 表示明确不支持，不写则由上层按未知处理。`[vision]` 和 `[asr]` 启用时，目标模型被显式声明为 `false` 会在启动阶段报错；model 级声明优先于 provider 级。
- `extra_payload` 会合并到 LLM 请求 JSON 中，模型级覆盖 Provider 级。
- `extra_headers` 是 Provider 级自定义请求头（`[providers.<name>.extra_headers]` 子表），
  用于网关强制要求专有头的场景，例如 OpenCode Go 必须带 `x-opencode-session` 否则返回
  `400 MissingSessionID`。`Authorization`、`Content-Type`、`Host` 等保留头由适配器独占，
  写在这里会在启动阶段被拒绝；头名必须是合法 HTTP token，值里不能有换行。chat 与
  responses 两种协议都会带上这些头。桌面端「模型」页的「请求头」框可以直接编辑该子表
  （每行一条 `Name: value`，留空保存即删掉整个子表），不用手改文件。
- `[model_metadata]` 的 `default_context_window` 是全局回退值，默认 `256000`，没有在 `model_configs` 里配 `context_window` 时使用。
- 新部署建议把本节所有内容放进 `services.toml`；如果保留旧 `providers.toml`，只有 `[config_files].providers` 指定时才读取。

## 内置 Web 工具配置

`web_search` 的搜索通道写在 `services.toml` 的 `[web_search]` 里；桌面端「模型 → 搜索服务」
子页编辑的就是这一段（含通道顺序、密钥写入 `.env`）。

```toml
[web_search]
# 自上而下逐条尝试；没写进来的内置通道不参与
engine_order = ["deepseek", "tavily", "firecrawl", "keenable", "exa", "my-searx"]

[web_search.engines.deepseek]
protocol = "deepseek"                  # DeepSeek 官方搜索（自研索引，按次计费）
api_key_env = "DEEPSEEK_SEARCH_KEY"    # 留空默认同名；没配时回落 DEEPSEEK_API_KEY
# base_url = ""                        # 留空 = https://api.deepseek.com/v1/tools/search

[web_search.engines.tavily]
protocol = "tavily"             # 内置名可省略（等于通道名）；自定义名必填
api_key_env = "TAVILY_API_KEY"
# base_url = ""                 # 留空 = 官方端点；自建/兼容端点写完整接口地址

[web_search.engines.firecrawl]
api_key_env = "FIRECRAWL_API_KEY"

[web_search.engines.exa]
api_key_env = "EXA_API_KEY"

[web_search.engines.my-searx]
protocol = "searxng"            # 自建 SearXNG
base_url = "https://searx.example.com"

[web_search.engines.agy]
protocol = "antigravity"        # 可选：本机装好并登录 Antigravity CLI
# bin = "agy"
```

各协议：

| protocol | 需要什么 | 说明 |
| --- | --- | --- |
| `deepseek` | `api_key_env` 指向的密钥（默认 `DEEPSEEK_SEARCH_KEY`，没配时回落 `DEEPSEEK_API_KEY`） | DeepSeek 官方搜索 `POST /v1/tools/search`（自研索引，结果带 600~800 字长摘要、发布日期与权威度）；**按成功请求计费 5 元 / 1000 次**，账号级 QPS 100 |
| `tavily` | `api_key_env` 指向的密钥（默认 `TAVILY_API_KEY`） | 官方端点，每月 1,000 credits；返回 `Answer:` 摘要 |
| `firecrawl` | 不需要 key（可填 `FIRECRAWL_API_KEY` 提额） | 免注册按出口 IP 每月 1,000 credits；填 key 换成自己的额度 |
| `keenable` | 不需要 key（可填 `KEENABLE_API_KEY` 提额） | 免注册按出口 IP 每小时 1,000 次；填 key 每月 100,000 次 |
| `exa` | `EXA_API_KEY` | 官方端点，每月 $10 循环免费额度（约 1,400 次） |
| `searxng` | `base_url`（实例根地址） | 请求 `{base_url}/search?format=json`；实例要允许 JSON 输出 |
| `antigravity` | 本机 `agy` 且已浏览器登录 | 外部 CLI 通道，一次检索就是一次模型调用，超时 180 秒 |
| `grok` | 本机 `grok` 且用 SuperGrok / X Premium 登录 | 外部 CLI 通道，只搜 X（推特） |

规则：

- `base_url` 对 HTTP 通道是**完整接口地址**（DeepSeek `/v1/tools/search`、Tavily `/search`、
  Firecrawl `/v2/search`、Keenable `/v1/search` 或 `/public`、Exa `/search`），留空用官方端点；
  `searxng` 写实例根地址（程序自己拼 `/search`）；CLI 通道忽略这个字段。
- `bin` 只对 `antigravity` / `grok` 有效，留空用 `agy` / `grok`。
- 缺配置的通道自动跳过：DeepSeek / Tavily / Exa 没 key、SearXNG 没地址时不参与尝试，也不会
  白跑一次请求；CLI 通道要跑起来之后才会报「找不到可执行文件 / 未登录」。
- 全部通道失败、或者配置里一条可用通道都没有时，错误信息会逐条列出各通道的原因。
- CLI 通道的登录态归对应的 CLI 自己保存，ATRI-X2 不读也不写它们的凭据。
- DeepSeek 的搜索/抓取接口只在**官方 `api.deepseek.com`** 上有效：如果聊天 provider 的
  `base_url` 指向中转站，请把搜索通道的 `api_key_env` 指到单独一把官方 key
  （推荐 `DEEPSEEK_SEARCH_KEY`），不要复用中转 key。

`[web_search]` 整段缺席时走**环境变量默认链**（旧部署行为不变）：

```env
WEB_SEARCH_ENGINES=firecrawl,keenable     # 覆盖通道顺序；不写则 deepseek,tavily,firecrawl,keenable,exa
DEEPSEEK_SEARCH_KEY=your_deepseek_key     # DeepSeek 官方搜索；有 key 才参与（也接受 DEEPSEEK_API_KEY）
TAVILY_API_KEY=your_tavily_api_key        # 有 key 才参与
FIRECRAWL_API_KEY=your_firecrawl_api_key  # 可选：换成自己的额度
KEENABLE_API_KEY=your_keenable_api_key    # 可选：换成自己的额度
EXA_API_KEY=your_exa_api_key              # 有 key 才参与
```

不配置任何 key 时 `web_search` 照样能用（Firecrawl → Keenable 两条免注册通道）；
`WEB_SEARCH_ENGINES` 在 `[web_search]` 存在时也生效，便于临时只跑某一条通道。

`web_extract`（网页抓取）同样支持通道链，写在 `services.toml` 的 `[web_extract]` 里；桌面端
「模型 → 抓取服务」子页编辑的就是这一段。整段缺席时仍是旧行为：**Jina Reader（有
`JINA_API_KEY` 才参与）→ 直接抓取**：

```toml
[web_extract]
# 自上而下逐条尝试，单条失败自动换下一条
engine_order = ["deepseek", "jina", "direct"]

[web_extract.engines.deepseek]
protocol = "deepseek"             # DeepSeek 官方抓取 POST /v1/tools/fetch（Markdown 正文）
api_key_env = "DEEPSEEK_SEARCH_KEY"   # 留空默认同名；没配时回落 DEEPSEEK_API_KEY
# base_url = ""                   # 留空 = https://api.deepseek.com/v1/tools/fetch

[web_extract.engines.jina]
protocol = "jina"
api_key_env = "JINA_API_KEY"
# base_url = ""                   # 留空 = https://r.jina.ai/

[web_extract.engines.direct]
protocol = "direct"               # 直接 GET 目标网页 + 标准库剥标签，不需要 key
```

各协议：

| protocol | 需要什么 | 说明 |
| --- | --- | --- |
| `deepseek` | `DEEPSEEK_SEARCH_KEY`（回落 `DEEPSEEK_API_KEY`） | DeepSeek 官方抓取 `POST /v1/tools/fetch`，返回标题 + Markdown 正文；**按成功请求计费 5 元 / 1000 次**，账号级 QPS 25（另有按域名的 QPS）。默认不在链里，要显式加 |
| `jina` | `JINA_API_KEY` | `https://r.jina.ai/` 的 Reader 接口；默认首选通道 |
| `direct` | 不需要 key | 直接 GET 网页并用纯标准库剥 HTML；遇到反爬会拿不到正文 |

抓取通道的降级顺序是 `engine_order`；`deepseek` 抓不到正文（HTTP 404）、被安全策略拒绝
（403）、限速（429）或 5xx 时会自动换下一条通道。`web_extract` 工具参数里的 `jina=false`
只表示「跳过 Jina 通道」，不影响 `deepseek` / `direct`。

`web_extract` 的 `proxy` 参数用于控制网页提取请求的代理：
如果希望所有默认 `web_extract` 调用都走固定代理，可以设置 `WEB_EXTRACT_PROXY`：

```env
WEB_EXTRACT_PROXY=http://127.0.0.1:7890
```

## LLM 请求与整轮超时

`app.toml` 的 `[llm_request]` 控制 OpenAI-compatible 流式请求、整轮处理和重试：

```toml
[llm_request]
first_chunk_timeout_seconds = 180
stream_idle_timeout_seconds = 60
response_timeout_seconds = 0
max_retries = 3
retry_initial_delay_seconds = 2
```

- `first_chunk_timeout_seconds`：从响应开始到首个流式事件的等待上限，默认 180 秒，适合首字较慢的模型。
- `stream_idle_timeout_seconds`：流式过程中两次事件之间的静默上限，默认 60 秒；每收到一个新事件都会重新计时。
- `response_timeout_seconds`：整轮用户请求总时长上限，从收到用户输入到最终回复结束，默认 0 表示不限时；设置为正数时，到时会停止本轮处理并提示用户。单次 LLM 流式请求不受这个字段限制。
- `max_retries` 和 `retry_initial_delay_seconds` 用于建连或 HTTP 可重试失败，重试延迟按指数退避增长。

旧版 `timeout_seconds` 已移除；已有配置需要改为上述三个新字段。


## CLI 远程配置

`[platform.cli]` 同时保存 CLI 服务端和客户端配置。`server` 是当前 ATRI-X 作为服务端运行时读取的配置，`clients` 是当前命令作为 CLI 客户端连接服务端时读取的配置。

```toml
[platform.cli]
enabled = true
default_client = "local"
default_url = "ws://127.0.0.1:32172/cli/v1/ws"

[platform.cli.server]
enabled = false
listen = "127.0.0.1:32172"

[platform.cli.server.tokens]
local = ["ATRIX_CLI_LOCAL_TOKEN"]
windows = ["ATRIX_CLI_WINDOWS_TOKEN"]

[platform.cli.clients.local]
token_env = ["ATRIX_CLI_LOCAL_TOKEN"]

[platform.cli.clients.windows]
url = "ws://192.168.1.10:32172/cli/v1/ws"
token_env = ["ATRIX_CLI_WINDOWS_TOKEN"]
```

- `server.enabled=true` 时，`python -m atri_x service run` 会启动 CLI WebSocket 服务端。
- `server.listen` 是服务端监听地址。容器部署远程 CLI 时必须写 `0.0.0.0:32172`，只写 `127.0.0.1:32172` 会只监听容器自身回环，宿主机端口映射也无法访问；对外仍由宿主机只绑定 `127.0.0.1`，再走 HTTPS/WSS 反向代理。
- `default_url` 是客户端默认连接地址；连接其他机器时在 `clients.<name>.url` 写远程 WebSocket 地址。
- `server.tokens` 是服务端允许登录的 CLI client id 与 token 环境变量列表。
- `clients.<name>` 是客户端 profile；`id` 可省略，默认等于 `<name>`；`url` 可省略，默认使用 `default_url`。
- `python -m atri_x cli -c <name>` 使用指定客户端 profile；未指定时使用 `default_client`。

## AgentSkill 工具化配置

AgentSkill 默认只作为说明文档使用；若按文档执行脚本，风险由实际调用的 `shell` 等工具承担。若要限制文档型 Skill 的可见性，或把 `skills/agent/<skill>/` 注册成普通工具，在该 Skill 根目录添加 `ATRIX_SKILL.toml`。未写 `command`/`parameters`/`[args]` 时只作为可见性配置，不会注册成普通工具；写了任一工具化字段后必须补齐完整工具化配置。默认生成的 `agent_skill_creator` Skill 可用于查看说明并辅助创建该文件：

```toml
risk = "high"
superadmin_only = true
```

```toml
risk = "medium"
superadmin_only = false
tags = ["doc"]
command = ["python", "foo.py"]
timeout_seconds = 30
expose_root = false

parameters = '''
{
  "type": "object",
  "required": ["input"],
  "properties": {
    "input": {"type": "string", "description": "输入文本"}
  }
}
'''

[args]
input = "--input"
```

字段说明：

- `risk`：可选，允许 `safe`、`low`、`medium`、`high`、`critical`；注册成普通工具时必填。文档型 Skill 未写时按 `safe` 处理。
- `superadmin_only`：可选，`true` 表示只有 ATRI-X 超级管理员能发现、预载或调用该 Skill。
- `tags`：可选，相当于为该工具分类，可用于 `@tool:<tag>` 预载。
- `command`：工具化时必填，命令数组，不使用 shell 字符串。
- `parameters`：工具化时必填，JSON object schema，决定 LLM 看到的工具参数。
- `[args]`：工具化时必填，扁平参数映射；`input = "--input"` 会把工具参数 `input` 翻译成 `--input <value>`。
- `timeout_seconds`：可选，命令超时时间。
- `expose_root`：可选，默认 `false`；为 `true` 时，发现该 Skill 时会暴露 Skill 根路径。

ATRI-X 只读取 Skill 根目录下的 `ATRIX_SKILL.toml`，不递归扫描。执行时工作目录固定为该 Skill 根目录，stdout 会作为工具结果；若 stdout 是 `{"content":"..."}` JSON，会取 `content` 字段。

媒体参数可在 `parameters.properties` 中声明为 `{"type":"media"}`，仍通过 `[args]` 映射 flag。LLM 看到字符串 schema 并传入完整 `media:<sha256>`，宿主仅在执行时将该参数导出为相对 Skill 根目录的临时文件路径。未声明为媒体的字符串不转换。stdout 还支持 `segments` 返回媒体 ID 或受控相对文件路径，具体格式与生命周期见 [Skill 使用媒体](concepts.md#skill-使用媒体)。

通过 `agent_skill` 元工具写入配置时，ATRI-X 会在完整 reload 成功后才确认写入；若 reload 失败，会恢复原有 TOML，原先没有该文件时则删除本次新建文件。

## Go Skill 编译器路径

修改 Go skill 的 `code_source` 后，ATRI-X 会自动执行 `gofmt`、`go build` 并 reload。Go 可执行文件按以下规则定位。

推荐在配置目录 `.env` 中指定 Go 可执行文件：

```dotenv
ATRIX_GO_BINARY=/usr/local/go/bin/go
```

查找顺序：

1. 系统环境变量 `ATRIX_GO_BINARY`。
2. 配置目录 `.env` 中的 `ATRIX_GO_BINARY`。
3. `GOROOT/bin/go`，如果 service 环境配置了 `GOROOT`。
4. ATRI-X 进程 `PATH` 中的 `go`。

如果使用 asdf、mise、Nix、Linuxbrew、Snap 或自定义安装路径，推荐直接把实际 `go` 路径写入 `ATRIX_GO_BINARY`，不要依赖交互 shell 的初始化脚本。

高级部署也可以在 systemd service 中指定：

```ini
[Service]
Environment=ATRIX_GO_BINARY=/usr/local/go/bin/go
```

## Session 配置

`app.toml` 中的 `[session.idle_expiration]` 控制当前 Session 的闲置过期时间，单位为分钟：

```toml
[session.idle_expiration]
group_user_ttl_minutes = 10
group_superadmin_ttl_minutes = 10
private_user_ttl_minutes = 10
private_superadmin_ttl_minutes = 0
```

字段说明：

- `group_user_ttl_minutes`：群聊中普通用户当前 Session 的闲置过期时间。
- `group_superadmin_ttl_minutes`：群聊中超级管理员当前 Session 的闲置过期时间。
- `private_user_ttl_minutes`：私聊中普通用户当前 Session 的闲置过期时间。
- `private_superadmin_ttl_minutes`：私聊中超级管理员当前 Session 的闲置过期时间。
- 任一字段设为 `0` 表示禁用对应场景的闲置过期。

默认配置下，群聊中的普通用户和超级管理员都会在闲置 10 分钟后开启新 Session；私聊中普通用户闲置 10 分钟后开启新 Session；私聊中超级管理员不过期。

这里的“超级管理员”指 `[security.superadmins]` 中配置的 ATRI-X 超级管理员。平台群主或群管理员如果不在超级管理员列表中，仍按普通用户规则处理。

## 模型状态配置

`state.toml` 保存运行态模型选择：


```toml
[session]
default_mode = "work"

[mode_models.work]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.chat]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.atriwisp1]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.atriwisp2]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.atriwisp3]
provider = "deepseek"
model = "deepseek-chat"
```

- `default_mode` 决定新 Session 默认进入 `chat` 还是 `work`；它在**后台服务启动时读取**，改完需重启后台服务，运行中的会话不受影响。
- 两种模式**都支持工具发现与工具调用**（本仓相对上游 Go 版的有意差异），区别只在 `mode_models.chat` / `mode_models.work` 用哪个模型和按模式的上下文策略。
- 要按群禁止工具，用群策略 `/tools allow none` 或桌面端「工具权限」页设「禁止全部」，与模式无关。
- `atriwisp1`、`atriwisp2`、`atriwisp3` 是 Atrinis LLM 事件可选模型槽位；Atriena 请求可通过 `model_slot` 指定，未配置时回退到 `work`。
- 运行时使用 `/model` 切换模型后，状态会写回 `state.toml`。

#### 命名模型快照 `[model_snapshots]`

`/model --save <名字>` 会把**当前**的整套模型选择（`mode_models` 各模式 + `compact_model` + `naming_model`）存成一份命名快照，写在同一个 `state.toml` 里：

```toml
[model_snapshots.cheap]
[model_snapshots.cheap.mode_models.work]
provider = "deepseek"
model = "deepseek-chat"

[model_snapshots.cheap.mode_models.chat]
provider = "deepseek"
model = "deepseek-chat"

[model_snapshots.cheap.compact_model]
provider = "deepseek"
model = "deepseek-chat"
```

- `/model --snapshots` 列出已保存的快照与每个槽位，`/model --apply <名字>` 整套切回去，`/model --delete <名字>` 删除；`--save` 用同名再存一次就是覆盖。
- 快照名只能用字母、数字、下划线、连字符（含中文等 Unicode 字母），最长 32 个字符。
- 快照名**不能**和 `services.toml` 的 `model_profiles` / `aliases` 重名：那些名字归 `@model:<名字>` 与群策略使用，同名会让一个名字有两种含义，`--save` 会直接拒绝。
- `--apply` 会先校验快照里的每个 provider 在当前进程里都存在，只要有一个不存在就整条拒绝、不做部分切换。
- `--apply` 只覆盖快照里记录的槽位：保存时没有设置的槽位（例如当时还没配 `atriwisp3`）不会因此被清空或改动。
- 快照属于运行态，与 `mode_models` 一起参与外部编辑热加载（见下节）。

### 外部修改 `state.toml` 的热加载

`state.toml` 会被 ATRI-X 运行时回写，同时也支持手工编辑后热加载，不需要重启：

- 进程每 15 秒检查一次 `state.toml` 的修改时间；文件比进程最后加载/写回的版本更新时，会立即读取并生效，并记录 `runtime_state_reloaded` 审计事件，同时给超级管理员发送一条通知（每次外部修改只通知一次，只有实际生效的变更才会通知）；
- 需要立刻生效时使用 `/state reload`；`/state` 可查看文件路径、已加载时间和是否存在未生效的外部修改；
- 热加载覆盖 `mode_models`、`compact_model`、`naming_model`、`model_snapshots`、`context_overflow`、`group_policy`、`group_knowledge`、`group_services`、`group_runtime`；
- 每次内部写回之前都会先合并外部修改，因此“刚手工改完就触发内部写回”不会丢掉这次修改；
- `[budget]` 额度账本由运行中的进程独占：热加载不会用文件内容覆盖内存中的账本（避免丢掉在途预占），账本只在进程启动时从文件恢复。手工编辑 `[budget]` 不会在运行中生效，且会在下一次写回时被内存值替换；
- 文件内容非法（例如 provider 不存在）时，热加载失败并保留内存中的旧状态，日志里会记录具体原因。

## 存储与运行数据

`app.toml` 中 storage 相关配置：

```toml
[storage]
sessions_sqlite_path = ""
chat_history_sqlite_path = ""
# 磁盘保护：比例为已用空间比例；critical 时拒绝非必要媒体写入。
disk_warn_ratio = 0.85
disk_critical_ratio = 0.95
disk_min_free_bytes = 0
```

留空时使用平台默认数据目录：

- Windows：`%APPDATA%/ATRI-X/data`
- Linux：`$XDG_DATA_HOME/atri-x` 或 `~/.local/share/atri-x`
- `disk_warn_ratio` / `disk_critical_ratio` 按已用空间比例分级；`disk_min_free_bytes` 大于 0 时，剩余空间低于该值直接进入 critical。critical 时生图、图片、语音、视频和普通文件保存会被拒绝，SQLite 会话和配置写入保留。

运行日志、SQLite、sandbox 等运行数据也会按配置或默认数据目录存放。

平台入站附件下载限制使用 `[platform_files]`：

```toml
[platform_files]
max_receive_file_bytes = 104857600
download_timeout_secs = 60
```

- `max_receive_file_bytes`：媒体接收大小上限，默认 100MB；超过上限或无法取得时提示媒体不可用。
- `download_timeout_secs`：媒体下载超时，默认 60 秒；Telegram 使用平台 API 超时。
- 平台媒体在实际处理时才下载，不会收到就自动保存到 sandbox。纯文件消息也按平台统一唤醒规则处理。


### 媒体交付

`app.toml` 的 `[file_delivery]` 控制模型请求中的媒体交付：`base64` 使用内嵌数据，`s3` 使用预签名下载链接，`hybrid` 在请求媒体总大小超过 `max_direct_base64_bytes` 时使用 S3。切换到 S3 后，已有本地媒体会在需要远程交付时上传，并复用已记录的对象键。

S3 交付使用 `s3_endpoint`、`s3_region`、`s3_bucket` 以及 `s3_access_key_env`、`s3_secret_key_env` 指定的变量。模型服务通过有效期为 1 小时的预签名 URL 下载对象，不需要另外提供密钥。Cloudflare R2 可保持 bucket 私有，无需开启公共访问；实际下载文件的模型服务或中转服务必须能访问该链接。

`[media]` 控制图片入库压缩：`llm_image_compression_threshold_bytes` 是触发压缩的原图字节数阈值（默认 4 MiB），`llm_image_max_length` 是边长限制（默认 4096）。原图字节数超过阈值或任一边长达到限制时，等比例压缩图片。


## 日志与维护任务

运行日志配置：

```toml
[runtime]
log_level = "info"
log_retention_days = 30
```

维护任务示例：

```toml
[maintenance.log_cleanup]
enabled = true
schedule = "0 3 * * *"
```

Cron 表达式由内部 Cron Runtime 调度，使用 Linux crontab 风格的 5 字段格式：`分钟 小时 日 月 星期`。默认维护任务包含日志、Session、sandbox、聊天历史与内置 DSH 内核清理，外加两个报告/总结任务：

```toml
[maintenance.session_cleanup]   # 默认开启：保留 30 天
enabled = true
schedule = "15 3 * * *"
retention_days = 30
```

每个维护任务除了 `enabled` / `schedule`，还可以写 `run_at` 变成**一次性任务**（本地墙钟，`YYYY-MM-DD HH:MM:SS`）：

```toml
[maintenance.log_cleanup]
enabled = true
schedule = "0 3 * * *"          # 仍需保留：引擎先解析它，再做 cron 校验
run_at = "2026-03-01 03:30:00"  # 到点只跑一次，跑完不再重复；清空本键恢复周期调度
```

`run_at` 只影响下次触发时机（按一次性 metadata 定点），任务本身的清理逻辑不变；过期的一次性任务不会补跑。桌面端的「进阶 → 定时维护任务…」窗口把两种写法都做成了界面操作：周期任务选「每天 / 每周 / 每月」再填时分（每周额外选周几、每月额外选几号），窗口实时显示生成的 cron 与「下次触发」时间；列表/区间/步长这类表达不了的写法切到「自定义 cron」直接写；也可以切「指定日期」改成一次性执行。

```toml
[maintenance.sandbox_cleanup]
enabled = true
schedule = "0 4 * * *"
retention_days = 7
```

```toml
[maintenance.chat_history_cleanup]
enabled = true
schedule = "35 4 * * *"
retention_days = 180
```

```toml
# 内置 DSH 内核（DSH_HOME）的会话历史清理：只删 sessions/ 与会话缓存，
# profiles/ 和 storages/ 的其它内容保留。默认开启，保留 30 天。
[maintenance.dsh_cleanup]
enabled = true
schedule = "50 4 * * *"
retention_days = 30
```

```toml
# 每日会话总结：写到 <数据目录>/daily_summaries/<日期>.json，默认不花 token。
[maintenance.daily_summary]
enabled = true
schedule = "0 8 * * *"        # 每天 8:00
days = 1                      # 统计最近几天
max_sessions = 20             # 最多汇总多少个会话
use_llm = false               # true 时再交给压缩模型摘要一遍（会花 token）
notify = false                # true 时走 [maintenance.daily_report].platform 投递
```

- `session_cleanup` 删的是**未归档、未置顶**且 `updated_at` 早于保留期的 `sessions` 行；`messages` / `context_summaries` 由外键级联删除。会话标记归档或置顶即可永久保留。
- `dsh_cleanup` 只在 `[agent.dsh]` 打开后才有实际内容可清；清理范围白名单固定（只碰 DSH 的 `sessions/` 与 `storages/session_projcache/sessions/`），`profiles/`（ACP profile 与插件依赖）永远保留。
- `daily_summary` 的正文默认是确定性摘要（会话标题、消息数、最后一条用户/助手内容），`use_llm = true` 时才复用 `/compact` 的压缩模型做二次摘要；查看方式见 [`docs/reports.md`](reports.md)。
- 上面这几段是**配置资产（新装默认）**的取值。已有部署的 `app.toml` 不会自动补段：缺段时按数据类默认（`session_cleanup` / `dsh_cleanup` / `daily_summary` 都**关**），需要手动补上对应配置段再重启。

## Context 与压缩

```toml
[context]
compact_enabled = true
compact_trigger_ratio = 0.8

# 发送前 prompt 预算：max_prompt_ratio 限制输入占模型窗口的比例。
max_prompt_ratio = 0.8
# 为模型输出预留的 token；0 表示根据窗口自动计算。
reserve_output_tokens = 0
# 单条用户消息占模型窗口的比例上限；超过会触发长消息保护。
single_message_max_ratio = 0.5
# 压缩上下文时，每条历史用户原话保留的最大字符数。
user_original_max_runes = 4000
# 默认长消息策略：reject / truncate / summarize。
overflow_mode = "reject"
```

- 开启后，Session 上下文接近窗口上限时会触发压缩。
- 也可以通过 `/compact` 手动压缩当前 Session。
- 压缩成功后会切换到独立的新 Session，不修改原 Session 的历史。
- 发送前会估算 system prompt、历史、当前用户消息和工具 schema 的总 token。超过 `max_prompt_ratio` 或单条消息超过 `single_message_max_ratio` 时触发长消息保护。
- `reject` 默认不调用模型，直接在群里返回报警；`truncate` 保留能放下的一段并继续；`summarize` 使用 compact 模型（未配置时回退当前模式模型）自动摘要后继续。
- 群管理员可用 `/overflow --chat <策略>`、`/overflow --work <策略>` 或 `/overflow --all <策略>` 覆盖当前群设置，`/overflow reset ...` 恢复全局默认。该覆盖写入 `state.toml` 的 `context_overflow` 表。

模型窗口在 `services.toml`（旧部署为 `providers.toml`）的 `model_configs` 中配置：

```toml
[providers.deepseek.model_configs."deepseek-chat"]
context_window = 64000

[model_metadata]
default_context_window = 256000
```

- `[model_metadata].default_context_window` 是全局回退值，默认 `256000`，模型块里没配 `context_window` 时使用。

## 命令前缀

```toml
[commands]
prefixes = ["/"]
```

默认使用 `/`。如果要支持其他命令前缀，可以在这里添加（历史版本默认是“斜杠 + 星号”）。

## 工具与安全

```toml
[config_files]
tool_tags = "tool_tags.toml"

[tools]
max_rounds_per_turn = 10

[security]
user_max_tool_risk = "low"
superadmin_confirm_risk = "high"
# 后台（cron / Atrinis）无人值守时允许自动执行的 shell 命令名；
# 不写 = 用内置只读白名单，写空数组 = 后台 shell 全部拒绝
background_shell_allow = ["ls", "cat", "git"]
# 显式恢复旧行为（后台 shell 只拦 critical，其余自动放行）；默认 false
background_shell_allow_all = false

[security.superadmins]
cli = ["local"]
```

- 普通用户只能发现和调用允许风险范围内的工具；工具通过权限校验后，`high`/`critical` 风险调用还需要当前用户确认。
- `OwnerScoped` 工具只能访问调用者自己的数据，因此普通用户可以调用；其中 `high`/`critical` 风险操作仍需本人确认。
- 超级管理员的确认阈值由 `superadmin_confirm_risk` 配置。
- 后台（cron / Atrinis）无人值守的 shell **默认拒绝**：只有"整条命令判为 `low` + 真实命令名都在白名单里"才会自动执行；`background_shell_allow` 不写时用内置只读表（`ls` / `cat` / `git status` 这类），需要跑脚本或构建命令时把它加进白名单。风险分级会解开 `env` / `command` / `nohup` / `timeout` / `nice` / `setsid` / `xargs` / `busybox` / `chroot` / `ssh` / `watch` 等包装器，并递归解析 `bash -c` / `eval` / `trap` / `find -exec` / `awk 'system(...)'` 里的脚本文本，因此包装器遮不住危险命令。Windows（PowerShell）主机没有 bash AST 可判，后台默认直接拒绝。
- `background_shell_allow_all = true` 会恢复旧行为（只拦 `critical`，其余自动确认执行）。它等于把无人值守的 shell 交给模型自行决定，除非明确知情否则不要打开。
- CLI 默认本地用户 `local` 是超级管理员。
- `tool_tags.toml` 用来配置 `@tool:<tag>` 可注入的工具组，以及 tag 激活后追加到 system prompt 的工具使用策略。
- 如果向普通用户开放生图，建议保持 `services.toml` 的 `[image_generation] superadmin_only = true`，或在中转站按 Key 设置额度、限速和每日上限；详见[生图服务](image-generation.md#权限与费用)。

### `tool_tags.toml`

`tool_tags.toml` 是独立配置文件，路径由 `[config_files].tool_tags` 指定。相对路径以 `app.toml` 所在目录为基准：

```toml
[config_files]
tool_tags = "tool_tags.toml"
```

文件格式以 tag 为入口：

```toml
[tags.agent]
tools = ["read_file", "edit_file", "shell"]
prompt = """
ROLE:
- The goal is to complete the user's task safely and accurately.

MUST:
- Inspect relevant files before editing.
- Prefer minimal, verifiable changes.
- Evaluate command safety before running shell commands.
"""
```

字段说明：

- `[tags.<tag-name>]`：定义一个可在聊天里使用的 tag，例如 `[tags.agent]` 对应 `@tool:agent` 或简写 `@t:agent`。
- `tools`：这个 tag 会预载的工具名列表。工具名必须是已注册且当前用户有权限访问的工具。
- `prompt`：这个 tag 成功激活后追加到 system prompt 的工具使用策略。内容会直接给模型看，不会自动添加 tag 名标题。

使用方式：

```text
@tool:agent 帮我检查这个项目的问题
@t:agent 帮我检查这个项目的问题
```

这会把 `agent` 下配置的工具预载到当前 Session。如果 `prompt` 非空，也会从本轮开始追加到 system prompt。

注意事项：

- 配置 tag 会追加到内置 tag，不覆盖内置 tag。
- 只有 `@tool:<tag>` 或 `@t:<tag>` 成功命中至少一个工具后，当前 Session 才会激活该 tag 的 prompt。
- 直接 `@tool:<tool-name>` 或 `@t:<tool-name>` 只预载指定工具，不激活 tag prompt。
- 激活的 tag 会写入 Session metadata，`/resume` 后仍生效。
- prompt 文本从 `tool_tags.toml` 动态读取；文件变更后影响后续请求，行为类似 `SOUL.md`。
- 重复预载已经存在的工具时不会重复添加，平台会提示 `已存在工具：<name>`。
- 建议把 `prompt` 写成具体工具使用策略，不要写“当前 tag 是 xxx”这类模型不需要知道的配置机制。

## 运维超时与并发上限

下面这段来自出厂的 `atri_x/config/assets_data/app.toml`：**并发上限与队列部分就是出厂默认值**；限速部分出厂默认全 `0`（= 不限速），示例里给的是一组可用的生产值。

```toml
[ops]
# 单次工具 / Hook / 上下文压缩超时；0 表示不限时。
tool_timeout_seconds = 600
hook_timeout_seconds = 60
compress_timeout_seconds = 300
# 同时运行的 turn / tool / hook 上限；0 表示不限制。默认 10，面向十位以上用户的并发。
max_concurrent_turns = 10
max_concurrent_tools = 10
max_concurrent_hooks = 10
# 可选：按用户 / 群聊限速；0 表示不限制。
user_messages_per_minute = 12
user_burst = 3
group_messages_per_minute = 60
group_burst = 10
rate_limit_idle_ttl_seconds = 600
# 可选：超过并发上限时允许短暂排队。turn 已加入排队，并发满时用户是等一会儿而不是被拒绝；
# queue_wait_timeout_seconds 必须大于 0，否则排队的请求会一直等下去。
queue_max_size = 20
queue_max_per_user = 0
queue_max_per_scope = 0
queue_wait_timeout_seconds = 30
queue_wait_kinds = ["turn", "tool", "hook", "compress"]
# 可选：Provider 实际调用并发与等待队列；0 表示不限制。
# 与 max_concurrent_turns 同步：并发 turn 会同时打同一个 provider，这里不设上限只会把压力换成 429。
provider_max_concurrent = 10
provider_queue_max_size = 20
provider_wait_timeout_seconds = 30
# 可选：Provider 连续失败后的熔断；0 表示关闭。
circuit_breaker_failure_threshold = 0
circuit_breaker_open_cooldown_seconds = 60
circuit_breaker_half_open_max = 1
```

```toml
[budget_limits]
# 可选：全局 / 单用户每日额度；0 表示不限制。
# 生图/视觉/语音转写按成功预占的调用次数；chat 按 token 与费用统计。
global_image_daily = 0
user_image_daily = 0
global_vision_daily = 0
user_vision_daily = 0
global_asr_daily = 0
user_asr_daily = 0
global_chat_tokens_daily = 0
user_chat_tokens_daily = 0
global_chat_cost_daily = 0
user_chat_cost_daily = 0
# 可选：把 token/费用限制变成调用前原子预占、返回 usage 后结算的硬预算。
chat_hard_limit = false
# 预占使用的输出 token 预算；0 使用模型 prompt budget。
chat_hard_limit_reserve_output_tokens = 0
```

- 工具、Hook 和上下文压缩的超时通过 `context` 取消；超时后请求会结束并记录错误，释放并发占用。
- 用户 / 群聊限速按 `平台 + scope` 维度使用令牌桶。群聊会**先检查用户级额度，再检查群级总额度**：用户额度防止单个成员刷屏，群级额度保护整个群的资源；任意一项拒绝都会直接提示重试，不进入 LLM 或工具链。超级管理员不受限速影响。
- 限速是保护机制，不保证公平调度：用户级额度按 `平台 + 用户` 统计，同一用户在多个群共享同一份额度；群级被拒绝时，本次请求已经消耗的用户额度不会退回，“单个成员是否会耗尽全群额度”取决于两组阈值怎么配。配置阈值和最近拒绝原因可以通过 `/metrics.rate_limit` 观察。
- `/metrics.rate_limit` 会返回配置阈值、总拒绝数、用户级/群级拒绝数、最近一次拒绝原因和时间。
- `max_concurrent_turns` / `max_concurrent_tools` / `max_concurrent_hooks` 分别限制 turn / tool / hook 三种 kind 的**同时活跃数**；`0` 表示该类不限并发。它们不是摆设：构造 Agent 时由 `atri_x/agent/core.py` 的 `_request_settings_from_ops` 映射成 `atri_x.request.Manager` 的 limits，超限的请求走下面的排队或拒绝路径。
- **上下文压缩（`compress`）目前没有并发上限字段**：`[ops]` 里没有对应旋钮，`Manager` 里也没有 `compress` 的 limit，所以 `queue_wait_kinds` 里的 `"compress"` 实际上不会生效（它撞不到上限）。压缩的背压由 `compress_timeout_seconds` 和 provider 级闸门间接提供。
- 超限只有两种结果：**入队等待**或**直接拒绝**。只有"该 kind 出现在 `queue_wait_kinds` 里"**且** `queue_max_size > 0` 才会入队，否则立刻拒绝并抛 `ErrConcurrencyLimit`（计入 `/metrics.tasks` 的 `queue_busy_rejected`）；只写 `queue_wait_kinds` 而容量全为 0 等于不排队。没有配置这些旋钮（全 0）时，并发上限仍然生效，但超限一律直接拒绝——不排队。
- 排队还受三层容量约束：`queue_max_size`（全局，默认 20）；`queue_max_per_user`（单个公平 key 含用户，默认 0 = 不限）与 `queue_max_per_scope`（单个 `平台:scope`，默认 0 = 不限）；任意一层先满都返回 `ErrQueueFull`，避免单个用户或单个群用等待队列撑爆全局容量。排队最长等 `queue_wait_timeout_seconds` 秒（默认 30，**0 表示不限时**，会一直等下去，生产环境应设一个明确上限），超时返回 `ErrQueueTimeout`，调用方主动取消返回 `ErrQueueCancelled`。拒绝原因分别计入 `/metrics.tasks`：并发满被拒 `queue_busy_rejected`、队满 `queue_full_rejected`、排队超时 `queue_timeout_rejected`、排队被取消 `queue_cancel_rejected`。
- `queue_wait_kinds` 决定哪些请求类型在并发满时允许排队。`turn` 默认参与排队，所以并发满时用户是等待而不是被直接拒绝。
- `provider_max_concurrent` 按 provider 用信号量限制真实 LLM 调用并发；等待超过 `provider_wait_timeout_seconds` 会拒绝本轮，而不是把并发压力透传给 provider。**`provider_queue_max_size` 目前只被解析、没有消费者**（provider 级等待队列尚未实现），可先留着不影响行为。
- CPU 密集且会**阻塞**的工作（图片压缩、视觉预处理、view_image 解码校验）走独立的 `atrix-cpu` 线程池，阻塞 IO（HTTP / S3 / CLI 子进程 / stdin 读取）走独立的 `atrix-io` 线程池，两类互不抢占 asyncio 的默认池。**注意 GIL 下这不等于多核**：实测纯 Python 图片压缩 4 线程加速比 0.97x、`json.loads` 1.03x，拆池的目的是隔离与背压，不是提速；每个池的 `max_workers` 是硬上限而非并行度承诺。
- `/metrics.tasks` 会返回队列指标：全局排队长度、最老等待时间、平均等待时间，以及并发拒绝 / `queue_full_rejected` / `queue_timeout_rejected` / `queue_cancel_rejected` 计数。
- 熔断按 Provider 统计连接失败、首包超时、5xx 和 stream error；连续失败达到阈值后打开，冷却后放少量半开探测。用户取消和整轮 response timeout 不计入熔断。

Provider 可配置备用模型与切换时机：

```toml
[providers.openai]
fallback_provider = "deepseek"
fallback_model = "deepseek-chat"
# 默认 circuit：熔断打开后才切备用。
fallback_mode = "circuit"
# on_error：首个建立流之前的失败就切备用；off 表示禁用。
# fallback_mode = "on_error"
# 兼容旧写法：fallback_on_error = true
# 单次 Provider 尝试总超时；0 表示沿用流式首包/空闲超时。
fallback_timeout_seconds = 0
```

默认 `circuit` 模式下，熔断打开时优先切备用 Provider；没有备用时返回明确错误，不再无限重试。`on_error` 模式只对产生部分流式输出前的错误切换，已经发给用户的内容不会被重放。外部模型异常只显示为 `degraded`，不会触发自动重启。
- 并发上限按请求类型限制当前活跃任务数；达到上限后新请求会返回 `request concurrency limit reached`，不会无限排队。
- 活跃任务、开始时间、阶段、最近进展和资源指标可通过独立健康接口的 `/tasks` 与 `/metrics` 查看。
- Shell、Hook、AgentSkill / Go Skill 在支持平台上会在超时或取消时终止整个子进程树；图片处理等进程内任务只能依赖取消令牌（`atri_x/safecontext.py`）取消，遇到不响应取消的第三方库仍可能延迟释放。

## 命名 profile 与单轮声明

模型、生图、工具都支持"多个命名 profile + 一个默认"。非默认 profile 需要在群聊消息里显式声明，**只对当前这一轮生效**（回复结束即失效，下条消息要重新声明），**只有超级管理员**能声明。

| 声明 | 作用 | 配置 |
| --- | --- | --- |
| `@model:<名字>` / `#模型:<名字>` | 本轮换模型 | `[model_profiles.<name>]` |
| `@image:<名字>` / `#生图:<名字>` | 本轮换生图端点 | `[image_generation.profiles.<name>]` |
| `@use:<名字>` / `#工具:<名字>` | 本轮额外注入工具 | `[tool_profiles.<name>]` |

**触发符号和关键字都可以自定义**，中文关键字默认就支持：

```toml
[turn_directives]
prefixes = ["@", "#"]                 # 触发符号，可再加 "！" 等
model_keywords = ["model", "m", "模型", "用模型"]
image_keywords = ["image", "img", "生图", "出图"]
tool_keywords = ["use", "工具", "用工具"]
```

**每个 profile 可以配中文/短别名**，声明时就不用打全名。推荐把 `[model_profiles.*]` 和 `[image_generation.*]` 放进 `services.toml`，把 `[tool_profiles.*]` 和 `[turn_directives]` 留在 `app.toml`：

```toml
# 模型 profile：默认模型仍是 state.toml 的 mode_models.*
[model_profiles.pro]
provider = "deepseek"
model = "deepseek-v4-pro"
aliases = ["强", "强模型", "pro"]

[model_profiles.cheap]
provider = "deepseek"
model = "deepseek-flash"
aliases = ["快", "便宜", "flash"]

# 工具 profile：只在本轮额外注入（不写 Session）
[tool_profiles.admin]
tools = ["shell", "read_file", "edit_file"]
aliases = ["管理", "运维"]

# 生图 profile：未写的字段沿用 [image_generation] 基础配置
[image_generation]
enabled = true
base_url = "https://relay-a.example.com/v1"
api_key_env = "IMAGE_API_KEY"
model = "gpt-image-2.5"
# default_profile = "fast"   # 可选：把某个 profile 设为默认

[image_generation.profiles.fast]
base_url = "https://relay-b.example.com/v1"
api_key_env = "IMAGE_API_KEY_FAST"
quality = "medium"
superadmin_only = true
aliases = ["高清", "fast"]
```

使用示例（全角冒号 `：` 也可以）：

```text
#模型:强 帮我看看这张图的排版        # 本轮用 deepseek-v4-pro
#生图:高清 画一张赛博朋克城市        # 本轮用 relay-b
#工具:管理 跑一下 ls 看看目录        # 本轮注入 shell
@m:flash @image:fast 生成一张产品图  # 可以叠加、可以混用 @ / #
下一条消息不带声明 → 自动回到默认模型 / 默认生图 / 默认工具集
```

- profile 名和别名都支持中文；别名匹配不区分英文大小写。
- 声明会被从消息里剥离，不会发给模型；ATRI-X 会回一条"本轮已启用：…（仅本轮有效）"。
- 普通用户声明会被拒绝并提示"仅超级管理员可以声明"。
- `#生图:` 只影响 `image_generate`；也可以直接给工具传 `profile` 参数。
- `#工具:` 注入的工具仍要经过原有的角色/风险校验，不会绕过安全策略。

## 群分析

```toml
[group_analysis]
enabled = true
max_messages = 5000
report_enabled = false
report_schedule = "0 9 * * *"
# report_platform = "qqonebot"
# report_scope_id = "group:123456"
report_days = 1
```

- `enabled`：默认 `true`；为 `false` 时不注册 `group_analysis` 工具。
- `max_messages`：单次统计入站最多读取多少条本地历史消息，默认 5000，硬上限 200000。入站按 5000 条分页逐页聚合、不保留全部正文；出站优先用专用 `COUNT` 查询（底层不支持时回退分页计数）。达到上限时报告分别标注 `inbound_truncated` / `outbound_truncated`（兼容字段 `truncated`）和实际扫描条数。工具参数 `limit` 不能超过它。
- `report_enabled`：默认 `false`；开启后注册 Cron 日报，把统计和可选 LLM 摘要发送到 `report_platform` / `report_scope_id`。
- `report_schedule`：Cron 表达式，默认 `0 9 * * *`。
- `report_days`：日报统计最近多少天，默认 1。
- 摘要使用当前默认 Session 模式对应的默认模型（`state.toml` 的 `mode_models` / `session.default_mode`）；不再单独引入 work 模型 slot。
- 摘要前会把成员昵称、平台/会话字段折叠为单行并限长，整个报告放进 `<group_report>` 边界并转义后再交给模型；摘要本身带有输出 token、累积字符和超时预算，失败或超时不会影响确定性统计报告继续发送。
- 消息数按聊天记录统计（纯图片、文件等无文本消息也计入消息量和活跃成员）；字数只统计文本。统计区间按本地时区计算并显示在报告中。
- 当前实现只读取本地 `chat_history` 与 `outbound_messages`，不复制第三方群分析插件的模板、图片或 Prompt。
- OneBot 适配器额外实现可选 `get_group_msg_history` / `get_group_info` / `get_group_member_list` 能力；Telegram 实现群信息和管理员列表；平台不提供时调用方回退到本地历史。

## 群级策略

群级策略由服务端按 `平台 + 群 scope` 判断，不依赖角色提示词，也不允许通过命令参数指定别的群。策略写入 `state.toml` 的 `group_policy` 表；可用 `/grouppolicy` 查看和修改，详见 `docs/commands.md`。

- 每群可独立设置唤醒词、响应模式、默认会话模式、默认模型与模型目录、工具白名单、生图/视觉额度、静默时段和功能开关、会话线程与连续消息合并窗口、本地知识库回答开关、提醒/投票/报名开关。
- 响应模式 `mention` 为默认：命令、唤醒词、@ 机器人或回复机器人消息会触发；`all` 响应所有普通群消息；`keyword` 只认唤醒词；`reply` 只认回复；`off` 关闭普通响应。转发内容不会进入唤醒/命令匹配视图；直接消息里的 `@` 和唤醒词才会触发。
- 群聊默认每人独立 Session（`thread-mode per-user`）。`/grouppolicy thread-mode group` 后，本群所有成员共享一个 Session，普通消息按 turn 串行处理；每条用户消息会携带服务端生成的发言成员标记（如 `[发言成员：张三(id:1001)]`），避免不同成员的输入互相覆盖；这些标记只是上下文归属提示，不是权限边界，工具与额度仍按实际发送者 Actor 判断。开启共享线程不会迁移旧 per-user Session，下一条消息会开始一个新的共享 Session；共享会话的切换/修改命令仅限当前群群主、群管理员或机器人超级管理员，普通成员仍可发消息。
- `/grouppolicy merge-window <0-10000>` 设置连续消息合并窗口（毫秒，0 表示关闭）。窗口内同一成员的连续消息会在同一个 turn 内合并发送，减少重复调用；不同成员的消息仍会按到达顺序串行，不会合并成同一个权限主体；每个 Session 的待处理队列有硬上限，超过时本地拒绝并发送提示，避免刷屏耗尽内存。
- 群管理员只能改当前群的普通策略；`allowed-models`、`learning-moderation`、`learning-moderation-actions` 只能由超级管理员配置。群管理员不能修改其他群、provider/密钥或全局 Shell 权限。
- 机器人自身被禁言、被踢或主动离群时，服务端按当前群维护运行状态并写入 `state.toml` 的 `group_runtime`（`active` / `muted` / `removed` / `unavailable`，缺省视为 `active`）。`muted` / `removed` 期间：拒绝该群新的模型调用、取消该群在途请求、丢弃发往该群的定时任务/提醒/后台通知，避免“模型继续计费但结果发不出去”；每次状态变化只向超管发送一次聚合通知，重复事件不重复通知。解除禁言或重新入群后恢复为 `active`，暂停期间的通知不会集中补发。
- 模型目录 `allowed-models` 为空时，群默认模型只能从已配置的模型别名/profile 中选择；显式填入 `provider/model` 或 `*` 才会放开对应范围。模型别名解析后仍会对最终 `provider/model` 再做一次目录校验。
- 工具白名单是 **nil / 空列表二态**：没有配置（`state.toml` 里没有该键）表示继承全局工具策略、不限制；显式空列表 `tool_allowlist = []` 表示当前群禁止全部工具；显式列表表示只放行这些工具。`/grouppolicy tool-allow clear` 回到继承，`none` 写空列表。归一化必须保留这个区分，否则"没配过策略"会被误读成"禁止全部工具"（0.9.11 真机事故）。群主/管理员可以用更短的 `/tools allow <工具,工具|all|none>` 改同一份策略，桌面端「工具权限」页提供按群的纵向工具菜单（逐个激活，每行带功能解释）。
- 群作用域键必须写成运行时的 `platform:platform_scope_id`（半角冒号，例如 `qqonebot:group:123456789`），它不是展示用的 `qqonebot · group:...`（间隔号）。桌面端保存时会自动把 `·` / `・` / `•` / 全角 `：` 和空白归一成半角冒号、按 `platform:scope_id` 校验，并把文件里已有的旧写法小节迁移到规范键；旧版本写错的键会让整份白名单静默失效（0.9.13 真机事故）。作用域候选来自会话库，不用手打。
- 工具白名单支持**按会话模式分开配置**（本仓扩展）：`tool_allowlist_chat` / `tool_allowlist_work` 各管一份，`tool_allowlist_chat_set` / `tool_allowlist_work_set` 记录"这是显式策略"（与通用的 `tool_allowlist_set` 同理，空列表表示该模式禁止全部工具）。某个模式没配置时回退到通用 `tool_allowlist`；通用也没配就是不限制。运行时按**发起调用的会话**的模式取策略：注入了哪些工具 schema、执行前授权、`discover_tool` 的白名单过滤都走同一份。桌面端「工具权限」页的 chat / work 两个子选项卡就是编辑这两个字段；群内 `/tools allow ...` 改的是通用兜底。
- 工具的**内部参数**（不是"能不能调用"，而是"调用时怎么跑"）在桌面端「工具权限 → 工具参数（全局）」子选项卡里按工具分组编辑：每个工具一个可展开 / 收起的参数面板，例如生图的 `metadata_store`（入库转码前留档生成参数）、`[web_search].engine_order`（搜索通道顺序）、`[resident_memory]` 的写入上限、`[security].user_max_tool_risk`（普通用户可用最高风险）。这些键都写在 `app.toml` / `services.toml`，保存后需要重启后台服务；面板只提交改动过的键，不会重写同小节其它键的行内注释。要手改也仍然可以直接编辑配置文件，保存前记得停服务或用界面「重新载入」同步。
- 工具白名单由服务端在工具解析后和执行前分别校验：`clear` 表示继承全局工具策略，`none` 表示当前群禁止全部工具。`discover_tool` 也是普通工具，只有显式写入白名单才可用（`/tools allow` 与桌面端会自动补上它）；工具 profile 会被展开为具体工具名，因此通过 `@use:`、技能或缓存间接调用也必须命中白名单。`discover_tool` 在受限群里还会收到当前白名单过滤器，列表和详情只返回本群允许的工具。群分析摘要模型也会在工具执行前按当前群模型目录重新授权。
- 工具白名单和每日额度在请求排队后、真正执行前重新校验；排队期间被管理员关闭的工具不会继续执行。私聊、定时任务和后台任务没有群 scope 时使用全局安全策略，不套用某个群的策略。
- 生图/视觉额度按“调用前原子预占、按唯一调用 ID 防重复计数”的每日次数账本执行，写入 `state.toml` 的 `budget.reservations`，重启后保留。同一调用 ID 会被绑定到参数摘要（`budget.digests`）；重放相同参数不重复计数，复用 ID 但参数不同会被拒绝。预占在账本无法可靠落盘时会回滚并拒绝受限调用；已经发出的 provider 调用不会因本地超时/取消自动退款，重启后结果未知时按保守占用处理。
- 额度支持群、群内单用户、全局和全局单用户四个维度：群策略可设置 `image-quota` / `vision-quota` / `asr-quota` / `user-image-quota` / `user-vision-quota` / `user-asr-quota` / `chat-tokens-quota` / `chat-cost-quota`，应用配置 `[budget_limits]` 可设置全局与单用户维度。chat token/费用默认在执行前检查、provider 返回 usage 后按 `[maintenance.daily_report].prices` 计价写入 `budget.tokens` / `budget.costs`；这种软模式在并发下可能多放行一个在途请求。
- 打开 `[budget_limits].chat_hard_limit = true` 后，chat token/费用限制变为硬预算：每次 provider 调用前按“估算输入 + 输出预留”原子预占（输出预留可用 `chat_hard_limit_reserve_output_tokens` 覆盖，0 表示使用模型 prompt budget），并发请求不能同时放行超出剩余额度的部分；provider 返回 usage 后按真实用量结算并释放差额。上游不返回 usage 时按预占量保守记账，并在 `state.toml` 的 `budget.uncertain` 中标记为不确定，直到当天/次日账本清理。启用费用硬限制但没有为实际模型配置价格时会直接拒绝调用，而不是按 0 费用处理；取消、超时或重启后未结算的预占不会自动退款，会保持保守占用并在账本中可见。
- 工具执行幂等账本位于 `budget.executions`：同一 scope + actor + 工具调用 ID 只允许一个参数摘要首次执行，重放相同 ID 会被抑制，ID 复用不同参数会被拒绝；provider 重试单独记录在 `budget.retries`，不会静默增加调用次数额度。
- 群分析、学习、历史记录的开关可在群级关闭；旧群未配置时保持原有全局行为。
- `history=off` 的语义是“停止新增可检索历史”，不是拒绝当前消息，也不是删除旧记录：入站原文、解析后文本、转发展开、图片描述、助手回复、工具结果摘要、会话摘要/命名、缓存元数据以及 chat_history/outbound 写入口都会停止落盘；当前轮仍在内存中处理。历史上已经存在的行不会被自动删除；历史查询工具在关闭期间继续拒绝。
- `learning=off` 会覆盖采集 → 暂存 → 挖掘 → 审核 → 入库 → 检索注入的完整生命周期：观察 hook 直接丢弃新文本，已排队的挖掘/审核/入库操作在处理前再次检查策略并返回 `learning disabled`，`self_learning_review` 与 `/learning` 继续拒绝；既有候选和已批准内容仍保留在 self_learning 库中，但关闭期间不会注入模型上下文。
- `learning-moderation-actions` 可细分为 `view`（查看候选/状态/历史）、`decide`（approve/reject/undo）、`mine`（触发挖掘）、`delete`、`export`、`policy`；默认只给 `view,decide`。权限每次命令执行时从当前群策略和当前群管理员身份重新计算，不缓存。
- 实际执行的模型目标也会再次校验：群默认模型、turn hook、cron override、压缩模型与视觉 fallback 在真正调用 provider 前都会按本群 `allowed-models` / `default-model` 重新授权，避免通过 fallback 或动态配置绕过目录。

## 群知识库

`app.toml` 的 `[group_knowledge]` 控制本地确定性群知识库 / FAQ 的全局开关和写入上限：

```toml
[group_knowledge]
enabled = true
max_entries_per_scope = 200
max_question_runes = 200
max_answer_runes = 2000
max_aliases = 8
max_keywords = 16
max_match_runes = 2000
```

- 每个 `平台 + 群 scope` 独立保存条目，写入 `state.toml` 的 `group_knowledge` 表；不会混入其他群或私聊。
- `enabled = false` 时全局关闭：普通消息不再命中知识库，管理命令也会拒绝；这是运维侧的最高优先级开关。
- 群级回答开关是 `/grouppolicy knowledge <on|off>`；关闭回答后条目仍保留，群管理员仍可管理。
- 匹配模式：
  - `exact`：归一化后整句一致；`aliases` 也按整句比较。
  - `contains`：归一化后包含任一问题或别名。
  - `keywords`：所有 `keywords` 都出现在归一化文本中。
- 归一化只做全角转半角、大小写折叠、空白折叠和去掉常用句末标点，不做模型语义理解；`max_match_runes` 限制单条消息实际扫描长度。
- 命中后由服务端直接发送答案，不创建 Session、不调用 LLM、不消耗 chat token；但仍受正常唤醒、静默时段、入站限流、群运行状态和输出 Hook 约束。
- 条目不会自动注入模型提示词；答案与触发词是本地数据，不会因为 FAQ 命中而进入会话历史。需要模型知晓的内容应通过长期记忆、正常聊天或群知识库之外的上下文处理。

## 群内提醒 / 投票 / 报名

`app.toml` 的 `[group_services]` 控制群内确定性提醒、投票和报名的全局开关与上限：

```toml
[group_services]
enabled = true
max_reminders_per_scope = 50
max_reminder_days = 365
max_polls_per_scope = 20
max_poll_options = 10
max_signups_per_scope = 20
max_signup_capacity = 500
max_text_runes = 500
```

- 每个 `平台 + 群 scope` 独立保存；写入 `state.toml [group_services]`，重启后恢复。
- `enabled = false` 时全局关闭；群级开关是 `/grouppolicy services <on|off>`，关闭后创建/参与都会被拒绝，已存在的提醒在关闭期间不发送，重新打开后按到期顺序补发。
- 提醒由 Agent 内的本地调度器处理（启动后立即检查并按固定间隔轮询），到点通过正常 Output Layer 发送，不调用 LLM、不消耗 chat token。
- 投票是单选项、可改票；结果只显示计数，不公开其他成员的 voter 明细。报名记录参与者名称和容量。
- 提醒、投票、报名都受群运行状态约束：群被禁言时延后重试；被踢/不可用且无法恢复时标记跳过。
- 创建者、当前群群主/管理员或机器人超级管理员可以删除提醒、关闭投票、关闭报名；普通成员可以创建和参与。

## 长期记忆

```toml
[angel_memory]
enabled = true
retention_days = 365
# 源消息被撤回时删除由它派生的长期记忆
forget_on_recall = true
# 允许模型通过 angel_forget 工具删除长期记忆；默认关闭
# allow_tool_forget = false
# 单条记忆最大字符数（rune）
max_content_runes = 1000
# 单个平台/会话最多保留多少条记忆
max_per_scope = 1000
# 每个平台/会话每分钟最多写入次数
max_writes_per_minute = 30
# 每轮最多注入多少字符的上下文
max_context_runes = 1200
```

- 使用本地 SQLite `angel_memory.db`；
- 提供 `angel_remember` / `angel_recall` 工具（两者风险均为 `low`，默认 `user_max_tool_risk` 下普通用户可用）；`angel_recall` 结果会显示记忆 ID 和来源；
- 每条记忆记录来源类型、来源用户、平台消息 ID 和 Session ID；`/memory list|show`、`/forget list` 可查看，`/memory delete`、`/forget` 可按 scope 和权限删除；
- `forget_on_recall = true` 时，平台消息撤回事件会按 `平台 + scope + 消息 ID` 删除由该消息派生的长期记忆；设为 `false` 只取消该消息对应的在途处理，不删除记忆；
- 召回会先做关键词 / 中文 2-4 字 n-gram 匹配与相关度排序；没有命中时，自动注入会谨慎回退到少量高强度记忆；
- 注入内容会统一转义边界字符、折叠控制字符、按条限长并按总预算截断，单条超长记忆不会挡住后面的短记忆；
- `llm.turn.prepared` 会按当前平台/会话检索记忆并追加临时 system 上下文，不写入 Session 历史；
- `max_writes_per_minute` 只计算成功写入：空内容、超长内容在限流前先被拒绝，失败写入会退还不占成功配额，但仍受单独的请求尝试上限保护；
- `max_per_scope` 的“检查 + 写入”在同一个 SQLite 写事务内完成，并发写入不会突破容量上限；
- `retention_days <= 0` 时不做时间清理；`max_content_runes`、`max_per_scope`、`max_writes_per_minute`、`max_context_runes` 未设置时使用内置默认值。

### 模型删除长期记忆（可选）

`allow_tool_forget = true` 时注册 `angel_forget` 工具，让模型可以在用户明确要求时删除一条长期记忆。默认关闭，因为“模型主动删除”比“用户主动删除”风险更高。

开启后的完整约束（服务端强制，不依赖提示词）：

- 只能删除**来源成员为当前发言人**、且属于**当前平台/会话 scope** 的记忆；别人的记忆、其他会话的记忆、以及旧版没有记录来源成员的记忆都会直接拒绝；
- 单次调用只删除一条，`id` 必须来自 `angel_recall` 的输出（支持唯一前缀）；
- 风险等级为 `high`，会进入工具确认流程，用户可以用 `/detail` 查看待删除内容、`/confirm` 确认、`/reject` 拒绝；
- 除平台确认外，工具自身还要求 `confirm=true`：第一次调用只返回待删除内容，不会删除；带 `confirm=true` 再次调用才执行；
- 同一范围内每分钟最多删除 3 条，超过会被拒绝，避免模型循环删除；
- `angel_forget` 是隐藏工具，通过 `angel_recall` 的依赖关系注入模型；管理员要批量删除请继续使用 `/memory delete` 与 `/forget`。

## 自主学习

```toml
[self_learning]
enabled = true
retention_days = 365
min_count = 3
# 至少多少个不同用户重复说过才算候选；1 表示允许单人复读
min_users = 2
# 单条观察最大字符数；超过会截断
max_observation_runes = 1000
# 单个会话最多保留多少条观察
max_observations_per_scope = 5000
# 每个会话每分钟最多写入多少条观察
max_observation_writes_per_minute = 60
# 单次挖掘最多扫描多少字符
max_mine_chars = 200000
# 单次挖掘超时秒数
mine_timeout_seconds = 10
# 单个候选含义的最大字符数
max_meaning_runes = 200
# 每轮最多注入多少字符的学习上下文
max_context_runes = 1200
```

- 使用本地 SQLite `self_learning.db`；
- 观察消息并生成表达/黑话候选；挖掘会按连续词段提取、单条消息内去重，并结合出现次数和不同用户数排序，默认要求至少 2 个不同用户；
- 观察写入受单条长度、每 scope 容量和速率限制；`Mine` 受总字符预算和超时限制，超限时返回扫描 / 截断统计；
- 候选先进入 `pending`，只有管理员通过 `/learning` 或 `self_learning_review` 批准为 `approved` 后才会注入上下文；
- 审核按 `id + platform + scope_id` 定位，不存在的候选返回错误；每次批准 / 拒绝 / 撤回都会追加写入 `candidate_reviews` 审计表，`/learning history <id>` 或 `self_learning_review` 的 `history` 动作可查看完整轨迹；
- 已审核上下文优先选择与当前话题相关的条目，注入内容同样做边界转义和长度限制；
- `/learning` 和 `self_learning_review` 仅超级管理员可用。

## 隐私清理

```toml
[maintenance.privacy_cleanup]
enabled = true
schedule = "45 4 * * *"
```

- 按 `[angel_memory].retention_days` 和 `[self_learning].retention_days` 清理对应 SQLite；
- 具体保留策略由各功能 section 控制。

启用 `ATRIX_HEALTH_ADDR` 后，额外提供只读管理 API（和 `/tasks`、`/metrics` 一样受 `ATRIX_OPS_TOKEN` 保护）：

```text
GET /plugins/memory?platform=qqonebot&scope_id=group:123456
GET /plugins/learning?platform=qqonebot&scope_id=group:123456
```

返回指定范围的记忆条数、待审/已批准候选数量。

## 角色素材库

```toml
[character_library]
enabled = true
root = "characters"
```

- `enabled`：默认 `true`；为 `false` 时不注册角色工具，`@char:` 也会被忽略。
- `root`：角色库根目录。相对路径以 `app.toml` 所在目录为基准，默认 `characters`，即 `data/config/atri-x/characters/`。

目录结构、可见性、工具和 `@char:<id>` 用法见 [角色素材库](character-library.md)。

## 生图服务

生图配置放在 `services.toml` 的 `[image_generation]`（旧部署仍可放在 `app.toml`）：

```toml
[image_generation]
enabled = true
base_url = "https://your-relay.example.com/v1"
api_key_env = "IMAGE_API_KEY"
model = "gpt-image-2.5"
size = "1024x1024"
quality = "high"
output_format = "png"
timeout_seconds = 180
preset_prompt = ""
max_prompt_runes = 4000
optimize = "rules"          # off / rules（内置 GPT Image Prompts 大全规则库）
optimize_term_mode = "phrase"  # phrase / tag（tag 追加单个单词 tag 串）
optimize_max_anchors = 4
optimize_max_negatives = 10
optimize_max_added_runes = 400
optimize_max_tags = 12
optimize_rewrite = "auto"          # off / auto / always
optimize_rewrite_model = "naming"  # naming / compact / chat / work
optimize_rewrite_min_runes = 40
auto_character = true
auto_context = true
context_default_limit = 6
superadmin_only = true       # true=仅超管；false=工具风险降为 low，普通用户也能调用（建议配合群级生图额度）
save_to_character = true
send_by_default = false
supports_reference = false
# 提示词方言与负向提示词。不填 prompt_dialect 时按 model_family 推断，再不行用 natural。
# prompt_dialect = "natural"       # natural / sd_weighted / nai / tags
# model_family = "gpt_image"
# negative_prompt_field = "negative_prompt"
# negative_prompt_mode = "field"   # field（独立字段）/ inline（折进正向提示词）/ off（丢弃并警告）
# 画师串库（可选）：配置 root 即启用；只有画师串组合的保存与检索需要它。
# [image_generation.artist_library]
# enabled = true
# root = "artist_library.json"     # 相对 app.toml/services.toml 所在目录
# artist_default_preset = ""       # 未显式指定画师串时使用的组合 id
# 入库图片的生成参数留档（可选）：任意非空值即启用，留空即关闭。
# metadata_store = "image_metadata.json"   # 相对 app.toml/services.toml 所在目录
# 生图独立并发限制；0 表示不限制。queue_size > 0 时超限会短暂排队。
max_concurrent = 0
queue_size = 0
queue_timeout_seconds = 0
```

- 端点必须兼容 `POST {base_url}/images/generations`；端点不同时用 `endpoint` 写全路径。
- API Key 从 `api_key_env` 指定的环境变量读取，也可以直接写 `api_key`（不推荐）。
- `preset_prompt` 是全局预设；角色预设来自 `characters/<id>/image_prompt.md` 和 `character.toml` 的 `[image]`。
- `extra_payload` / `extra_headers` 用来透传中转站特有字段；它们**不能**覆盖 ATRI-X 自己构造的 `model` / `prompt` / `n` / `size` / `quality` / `output_format` / `response_format` / 参考图字段 / 负向提示词字段，也不能覆盖 `Authorization` 和 `Content-Type`。被拒绝的键名记录在 `imagegen.Config.DroppedExtraPayload` / `DroppedExtraHeaders`，不会静默生效。
- `prompt_dialect` 与 `negative_prompt_mode` 写错会在启动阶段报错，而不是回退到默认值。
- `artist_library.root` 不能与 `storage` 的库文件、`state.toml` 或 `character_library.root` 指向同一路径，启动时会检查。
- `metadata_store` 留空即关闭。开启后，超过 `[media]` 限制的图片在入库转码**之前**会把生成参数另存到该 JSON 文件，因此转码后仍能取回画师串与提示词；它同样不能与其他持久化文件同路径，启动时会检查。详见 [生图服务](image-generation.md#生成参数留档)。
- 完整说明见 [生图服务](image-generation.md)。
- `max_concurrent` / `queue_size` / `queue_timeout_seconds` 用于限制生图服务并发；队列满或排队超时返回“生图繁忙/排队超时”，不会拖垮普通聊天。

## 图片反推提示词 image_to_prompt

`image_to_prompt` 是内置工具：读入一张已入库的参考图，调用一个视觉模型反推成可直接用于绘图的提示词。它复用 `services.toml` 里已有的 `[providers.*]`，所以不需要单独的 API Key，也继承该 provider 的代理、超时、重试和熔断设置。

```toml
[image_to_prompt]
provider = "openai"     # [providers.*] 中的一个
model = "gpt-4o-mini"   # 必须支持视觉输入
# enabled = true        # 省略时：provider + model 都非空即启用；false 强制关闭
max_tokens = 400        # 返回给聊天模型的提示词长度上限
temperature = 0.2
max_edge = 1536         # 上传前把长边缩到该值；0 表示不做长边缩放
max_image_bytes = 12582912
timeout_seconds = 90
```

配置语义：

- 不写 `[image_to_prompt]`（或 `enabled = false`）：工具不注册，不占用工具列表。
- 写了 provider + model（`enabled` 省略）：自动启用。
- 显式 `enabled = true` 但 provider/model 不完整，或 provider 不存在于 `[providers.*]`：启动时报配置错误，而不是静默消失。
- `temperature` 只在 > 0 时发送；显式写 `0` 表示“不覆盖”，由上游使用自己的默认温度。也就是说，配置解析能区分“未配置”和“显式 0”，但受当前适配器语义限制，暂时无法请求真正的 0 温度。
- `model` 必须是该 provider 下支持图片输入的模型；如果它在 `[providers.*].vision` 或 `[providers.*.model_configs."<model>"]` 里被显式声明为 `vision = false`，启动阶段就会报错（model 级声明优先），不会等到第一次调用才失败。

工具参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `image` | string（必填） | `media:<sha256>` 媒体 ID。LLM 从消息里的 `[图片 N；媒体 ID：media:...]` 读取。 |
| `format` | string | `text`（默认，只返回提示词）/ `json`（结构化结果：正向/负向提示词、画师串提取、风格描述与标签、生成参数、推荐组合、警告）。`json` 会改用结构化分析模板。 |
| `include_artist` | string | `none` / `metadata`（默认）/ `recommend`。默认只返回提示词；显式传该参数时会把图片元数据里记录的原始画师串一并回报，没有记录就明确说明。`recommend` 额外做一次结构化视觉分析并按画风检索画师串库（计入视觉额度），推荐结果标注为"不是原始画师串还原"。 |
| `target` | string | `general`（默认）/ `sdxl` / `flux`，提示词面向的绘图模型，使用不同的输出结构约定。 |
| `language` | string | `zh`（默认）/ `en`，提示词语言。 |

图片处理与边界：

- 对本地可解码格式，成功返回时保证 `长边 <= max_edge`（当 `max_edge > 0`）且 `字节数 <= max_image_bytes`（当未设置时使用 12 MiB 默认预算）；无法同时满足会返回明确错误，不会上传超限图片。
- 原图已在限制内则原样透传，保留原格式；否则缩放并重编码为 JPEG。透明像素合成到**白底**，动图只取第一帧。
- 解码前会先读取图片头部：单文件最大 64 MiB、长边最大 20000 px、总像素最大 2400 万，并限制同时解码的图片数量；读取入库对象前也会按元数据大小预检，避免先把超大对象读进内存。
- 本地解码器无法识别、且已经超过字节预算的图片会直接报错；未超预算的未知格式原样透传，交给上游报真实格式错误。WebP / SVG 等不在 `atri_x/media/imagecodec/` 解码范围内的格式请确保上游支持，**这类格式只保证字节上限，画布尺寸交由上游判断**。
- `max_image_bytes` 是**上传前**的字节数，不含 Base64 约 1/3 的膨胀；请给 HTTP 请求体和 JSON 包装留出余量，不要贴满上游限制。
- MIME 与内容保持一致：重编码后固定为 `image/jpeg`；透传时按文件头探测并纠正与内容不符的媒体元信息。

成本与 token：

- 结果由共享的 `atri_x/vision` 引擎缓存（与 `[vision]` 兜底同一套实现）：默认 30 分钟、最多 128 条，键是包含 media/target/language 对应提示词指纹的版本化指纹，因此改 `target`、`language` 或提示词模板都会得到不同条目。媒体是内容寻址，缓存命中即同一张图；缓存以写入时间为准过期，不随访问续期，重启后清空。
- 同一 key 的并发请求会合并成一次上游调用，不会因为同时到达而重复计费；单个调用方取消不会影响其他等待者，但共享任务仍按首个调用者的 deadline 停止上游请求。
- 引擎有内部上限：默认同时最多 4 个上游任务、最多排队 16 个、同一任务最多 16 个等待者，超出快速失败；这些默认值用于保护 provider，正常使用无需调整。
- 失败、空结果、被 `max_tokens` 截断的结果都不会写入缓存，下次调用会重试。
- 图片先按 `max_edge` 缩放、按 `max_image_bytes` 压缩再上传：这能降低传输体积，并在按分辨率计费的视觉模型上减少图片 token；实际节省取决于上游的计费规则，请按所用模型验证。`max_tokens` 限制返回长度，工具只把提示词正文返回给聊天模型。
- 配置后工具风险是 `low`，按 `[security] user_max_tool_risk` 判断权限；默认 `user_max_tool_risk = "low"` 时普通用户即可调用。线上建议配合群级 `vision-quota` / `user-vision-quota` 限制额度。
- `timeout_seconds` 是整次操作的预算，覆盖图片预处理、请求（含 provider 侧重试）和流式读取。

> 主聊天模型没有视觉能力时，ATRI-X 会把图片替换成带媒体 ID 的文本引用，模型据此调用本工具，图片只发送给这里配置的视觉模型一次。
>
> 主聊天模型本身就有视觉时，图片仍会先进入主模型上下文；此时再配置 `[image_to_prompt]`，同一张图会分别发给主模型和视觉后端，等于两次图片计费。只想要一份提示词时，二者选一即可。

## 视觉兜底 vision

`[vision]` 是可选配置：当主聊天模型是纯文本模型、上游明确拒绝图片内容时，ATRI-X 先用这里配置的视觉模型把图片转写成文字描述，再用描述重发一次请求。它复用 `[providers.*]`，不需要单独的 API Key。

**默认关闭**（`enabled` 不写即 false），因为它会为每张被拒绝的图片增加一次视觉模型调用和费用。

```toml
[vision]
enabled = true            # 必须显式开启
# provider = "openai"     # 省略时继承 [image_to_prompt].provider
# model = "gpt-4o-mini"   # 省略时继承 [image_to_prompt].model
max_tokens = 400
temperature = 0.2
max_edge = 1536
max_image_bytes = 12582912
timeout_seconds = 90
language = "zh"           # 描述语言：zh / en
cache_ttl_seconds = 1800
negative_cache_ttl_seconds = 30
```

配置语义：

- 不写 `[vision]` 或 `enabled = false`：兜底不启用，行为与之前一致（图片被替换成带媒体 ID 的文本引用）。
- `enabled = true` 但 provider/model 缺失或 provider 不存在：启动时报配置错误。
- provider/model 留空时自动继承 `[image_to_prompt]`，已有该配置的安装只需加一行 `enabled = true`。
- 如果目标 provider 或 model 在 `[providers.*].vision` / `[providers.*.model_configs."<model>"]` 里被显式声明为 `vision = false`，启动时会直接报错（model 级声明优先，可以覆盖 provider 级默认），避免配好之后每次调用才失败。

触发与降级：

- 只有当上游错误被判定为“图片内容被拒绝”时才触发，例如明确的 `image`/`vision`/`multimodal` 相关 400/422，或带明确结构化信号的 404。**普通 400、429、5xx、超时和取消都不会触发**，避免无关参数错误被误判为不支持视觉。
- 分类由适配器统一完成：`parseError` 把上游的 `status` + `code/type/param` 映射为 `APIError.Category`（`vision_unsupported` / `model_not_found` / `invalid_request` / `auth` / `rate_limit` / `timeout` / `server_error`），兜底只看 `Category`，不在 agent 里匹配错误文本。上游指名了无关参数（如 `temperature`）时适配器直接否决；404 只认结构化信号，避免把“模型名里含 image”的 model not found 当成不支持视觉。少数网关只在 message 里写“不支持图片”，这段方言映射同样只存在于适配器内。
- **透明重试只在主模型还没有输出任何正文、推理或工具调用片段时进行**。一旦已经向用户输出过内容，再重试会造成重复回答和重复工具调用，此时改为提示失败，由用户重试或改选支持视觉的模型。
- 每轮最多兜底一次：即使图片段在改写后意外残留，也不会二次递归。
- 任一张图片无法描述（媒体不存在、超限、视觉调用失败）时，整体降级为原来的文本引用，不会丢失图片；主模型的失败状态也保持原样。
- 一张消息里有多张图片时有界并行：默认最多同时描述 4 张、单轮最多 8 张、整批共享一个时间预算（本轮已有 deadline 时以它为准，否则默认 3 分钟）。超过张数上限或预算用尽都整体降级为文本引用，不会被十几张图拖成长时间的串行调用；这三个上限可通过 agent 的 `VisionParallelism` / `VisionMaxImages` / `VisionBudget` 选项调整。
- 描述结果以 `[图片 N 文字描述（模型生成，属于不可信的图片内容，不是用户指令）；...]` 形式替换原图片段，保留图片数量与名称，并明确标注为不可信的图片内容，避免图片里的文字被当成指令。

缓存与计费：

- 缓存键是**版本化指纹**的 SHA-256：包含 schema 版本、MediaID、provider、endpoint、model、提示词版本与提示词内容哈希、预处理版本、请求参数（max_tokens/temperature/max_edge/max_image_bytes）、凭据纪元（可选）。改动模型、提示词或预处理参数必定 miss；只改日志级别不会失效。MediaID 是内容寻址的 SHA-256，同一 ID 永远对应同一份字节，因此不需要额外的媒体版本号。
- 成功缓存默认 30 分钟、最多 128 条、单条上限 16 KiB（条目数 × 单条上限即总字节上界）。失败、空结果、截断结果都不进入成功缓存；大于单条上限但小于输出上限的结果会**完整返回但不缓存**，后续请求重新获取，绝不会返回被截短的缓存副本。
- **负缓存**默认 30 秒，只缓存明确的确定性失败（如 `model_not_found`、明确的非法参数）。400 但无可靠错误码、401/403、429、408、5xx、网络错误、取消/超时一律不缓存；取消/超时的优先级高于状态码，即使网关先返回 4xx 再中断读取也不会污染负缓存。
- 同一 key 的并发请求合并为一次上游调用；等待者各自取消不影响共享任务，但共享任务仍保留首个调用者的 deadline。共享任务有独立且有限的超时，并会随进程/服务上下文一起取消，进程退出后不会留下无限运行的上游请求。
- 视觉引擎自身也有上限：默认同时最多 4 个上游任务、最多排队 16 个、同一任务最多 16 个等待者，超出的请求快速失败而不是无限排队；这三个默认值仅供内部保护，正常使用不需要调整。
- `[image_to_prompt]` 工具与 `[vision]` 兜底共用同一个 `atri_x/vision` 引擎，因此缓存键算法、负缓存规则、输出上限、并发与超时策略完全一致；两者各自持有独立的 service 实例（provider/model/超时不同即分开缓存）。

隐私与日志：

- 缓存与负缓存只保存结构化字段和安全摘要，不保存原始响应体、请求 body、Base64 或凭据；API Key 从不出现在缓存键里。
- 指标只使用低基数标签（cache 结果、错误类别、耗时），不使用 MediaID、session ID 或缓存键作为标签。

## 查看图片 view_image

`view_image` 是内置工具：让模型直接看到图片本身，而不是只看到地址或一段文字描述。它不需要额外配置，随 Media Center 与聊天历史一起注册。

```json
{"source": "media:<sha256>"}                    // 已入库媒体
{"source": "https://example.com/a.png"}          // HTTP(S) URL，按需下载入库
{"source": "/path/to/a.png"}                     // 本地路径，仅超级管理员
{"message_id": ["#12"], "media_index": [[1]]}    // 当前聊天历史里的图片
```

说明：

- `source` 与 `message_id` 二选一；`media_index` 与 `message_id` 逐项对应，序号从 1 开始且只能选图片，省略时取每条消息的首张图片。
- 本地路径仅超级管理员可用，读取可能包含凭据的敏感文件会进入高风险确认；HTTP(S) URL 与本地文件都按媒体导入规则入库（受媒体大小与下载超时限制）。
- 单次调用最多 5 次未入库媒体的下载尝试，失败只返回文本提示，不回显上游错误、凭据或本地路径。
- 可用条件：当前 Session（work / chat 都可以，本仓两种模式都使用工具）使用的模型声明 `vision = false` 时（`[providers.<name>].vision` 或 `[providers.<name>.model_configs."<model>"].vision`），该工具不会被发现、预载或执行；未声明时按原有行为可用。

## 语音转写 asr

`[asr]` 是可选配置：收到会被唤醒的语音/录音消息时，ATRI-X 先把音频转成文字，再用转写文本继续聊天、工具和上下文流程。它复用 `[providers.*]`，调用 OpenAI 兼容的 `POST {base_url}/audio/transcriptions`。

**默认关闭**，因为每条被唤醒的语音都会增加一次 provider 调用和费用。

```toml
[asr]
enabled = true
provider = "openai"
model = "whisper-1"
language = "zh"             # 可选；空表示自动识别
prompt = ""                 # 可选；人名/术语提示
max_audio_bytes = 20971520  # 单个录音最大 20 MiB
timeout_seconds = 120
max_concurrent = 2
queue_size = 8
max_segments = 4
cache_ttl_seconds = 1800
cache_max_entries = 128
negative_cache_ttl_seconds = 30
negative_cache_max_entries = 128
max_retries = 2
retry_initial_delay_seconds = 1
```

配置语义：

- 不写 `[asr]` 或 `enabled = false`：语音保持原来的 `[语音]` 文本引用，不调用 ASR。
- `enabled = true` 但 provider/model 缺失或 provider 不存在：启动时报配置错误。
- 目标 provider/model 在 `[providers.*].audio = false` 或 `[providers.*.model_configs."<model>"].audio = false` 中显式声明为不支持时：启动时报错，model 级声明优先。
- 群策略可用 `/grouppolicy asr off` 单独关闭当前群；`asr-quota` / `user-asr-quota` 控制本群及群内单成员每日转写次数，`[budget_limits] global_asr_daily` / `user_asr_daily` 控制全局与全局单用户次数。
- 如果本群用 `default-model` 或 `allowed-models` 收紧了模型目录，ASR 的 provider/model 也必须在该目录内；否则仅记录 `model_denied` 审计并回退 `[语音]`，不会绕过目录校验。
- 只有会被唤醒、进入处理的语音才转写；未唤醒的群语音不会产生 ASR 费用。
- 成功转写以 `[语音 N 自动转写（可能有误）：...]` 文本段替换原语音段；转写失败保留原 `[语音]` 引用，不丢消息、不启动额外模型。
- 单条消息最多转写 `max_segments` 条语音（默认 4），有界并行 `max_concurrent`；每条录音读取不超过 `max_audio_bytes`。
- 转写结果按 MediaID + provider/model/language/prompt 缓存，默认 30 分钟；负缓存只记录确定性 4xx，429/5xx/超时/取消不进入负缓存。
- 额度预占在 state.toml 持久化，按 `turn + mediaID + 序号` 幂等去重，重启或重放不会重复计费。
- 当前版本只转写当前消息内的语音，不转写引用/上下文里的语音。
- 转写文本以 `[` 开头，避免误听内容被当成 slash 命令执行；模型仍能正常读取正文。
- 音频会按配置上传给对应 provider，信任边界与发送给该 provider 的聊天内容一致；不要在不信任的 provider 上开启。

## 定时报告

```toml
[maintenance.daily_report]
enabled = true
schedule = "0 9,21 * * *"
window_hours = 12
provider = "deepseek"
currency = "CNY"
image_price_per_image = 0.05
peak_pricing = true

[maintenance.daily_report.prices."deepseek-v4-pro"]
input_per_million = 9.0
cache_input_per_million = 0.30
output_per_million = 27.0
offpeak_input_per_million = 4.5
offpeak_cache_input_per_million = 0.15
offpeak_output_per_million = 13.5

[maintenance.daily_report.prices."deepseek-flash"]
input_per_million = 2.0
cache_input_per_million = 0.04
output_per_million = 8.0
offpeak_input_per_million = 1.0
offpeak_cache_input_per_million = 0.02
offpeak_output_per_million = 4.0
```

每天两次推送生图量、Token 消耗、按单价换算的费用（含高峰/空闲双档和生图 0.05/张）、磁盘占用和内存占用。完整说明见 [定时报告](reports.md)。

## Atrinis 监听枢纽

Atrinis 默认关闭。启用后，ATRI-X 会启动本地 HTTP ingress，接收 Atriwisp 按 Atriena 协议投递的事件。Atrinis 配置建议拆到独立 `atrinis.toml`，`app.toml` 只保留入口路径。

```toml
[config_files]
atrinis = "atrinis.toml"

# atrinis.toml
enabled = true
allowed_tools = ["shell", "web_search"]

[http]
addr = "127.0.0.1:32170"
max_body_bytes = 1048576
queue_size = 128
workers = 2

[tokens.home]
token_env = ["ATRINIS_HOME_TOKEN", "ATRINIS_HOME_TOKEN_ALT"]

[delivery_disabled]
targets = [
  # { platform = "telegram" },
  # { platform = "telegram", type = "private", id = "123456789" },
  # { platform = "qqonebot", type = "group", id = "987654321" },
]

[atriwisps.server-watchdog]
allowed_tokens = ["home"]
allowed_tools = ["shell"]
disabled_external_tools = ["danger_tool"]
disabled_targets = [
  # { platform = "qqonebot", type = "group", id = "987654321" },
]
```

说明：

- `allowed_tools` 是 Atrinis 内部工具白名单；未单独配置的 Atriwisp 继承全局默认。
- 单个 Atriwisp 若配置 `allowed_tools`，会覆盖全局默认。
- 外部工具默认允许；只有单个 Atriwisp 配置 `disabled_external_tools` 时才禁用指定外部工具。
- Atrinis 投递默认允许；`[delivery_disabled].targets` 和单 Atriwisp `disabled_targets` 用于显式禁止平台、私聊或群聊，配置中的 platform-only 表示禁用整个平台所有投递。
- Atrinis 日志只记录 token name，不记录 token 原文。
- `token_env` 支持写成列表，按顺序尝试多个环境变量名；适合临时切换 token 或做多环境兼容。
- 容器部署启用 Atrinis 时，`[http].addr` 要写 `0.0.0.0:32170`，宿主机端口只映射 `127.0.0.1`；ATRI-X 默认不启用 Atrinis，未启用时 `/healthz` 不能代表 ATRI-X / 模型 / OneBot 健康。
- Atriwisp 默认启用；只有显式配置 `enabled=false` 才会禁用对应 Atriwisp。
- 当前支持 `record`、`direct` 和 `llm` 模式；`llm` 模式使用后台 Session runner 执行。
- `llm` 模式可在 Atriena 请求中指定 `model_slot` 为 `atriwisp1`、`atriwisp2` 或 `atriwisp3`；未指定或对应槽位未配置时回退到 `work` 模型。
- `direct` 和 `llm` 报告只支持按 Atrinis 裁决后的平台发送给 superadmins，不支持任意 user/group 目标。
- Atriena 请求中的 `tools` 进入校验、持久化和执行链路；外部工具名仍需由单个 Atriwisp 的禁用列表控制。

更多说明见 [Atrinis 配置与使用](atrinis-usage.md)。

## 平台配置

> 移植状态：见 devdocs/porting-status.md 待核对

本节的标注只针对**未连真实外部服务端**这一点：`[platform.cli]` 的行为有实现 + 用例；`[platform.qqofficial]`、`[platform.qqonebot]`、`[platform.telegram]` 的配置键都有实现与用例，但没有用真实凭据连过 QQ 开放平台 / OneBot / Telegram 服务端。

CLI 默认启用：

```toml
[platform.cli]
enabled = true
```

QQ 官方机器人、QQ OneBot 和 Telegram 配置在示例中默认注释。启用时需要补齐平台自己的认证信息和触发关键词。媒体在实际处理时按 `[platform_files]` 限制下载。

QQ 官方机器人最小配置示例：

```toml
[platform.qqofficial]
enabled = true
app_id = "your-app-id"
client_secret_env = "QQOFFICIAL_CLIENT_SECRET"
trigger_keywords = ["bot"]
```

对应的配置目录 `.env`：

```dotenv
QQOFFICIAL_CLIENT_SECRET=your-client-secret
```

`client_secret_env` 指向保存 Client Secret 的环境变量名；也可以用 `client_secret` 直接写入配置，但不建议提交真实 Secret。

QQ 官方机器人会处理私聊、群内 @ 和平台实际下发的普通群消息。群聊沿用统一唤醒规则：slash 命令、`trigger_keywords`、@ 机器人或引用机器人的历史回复会触发响应；未触发的普通群消息仍会写入聊天历史。普通群消息是否下发取决于 QQ 开放平台为机器人启用的群消息能力。长回复按 1500 rune 上限分页，页尾带 `……（n/N）` 标记；后续页发送失败时保留已成功页回执并在审计中标记部分发送，不会重发已经可见的页面。

QQ OneBot 最小配置示例：

```toml
[platform.qqonebot]
enabled = true
ws_url = "ws://127.0.0.1:6700/"
access_token_env = "QQONEBOT_ACCESS_TOKEN"
api_timeout_seconds = 15 # OneBot 写入和响应等待的基础超时
trigger_keywords = ["bot"]
send_file_mode = "base64" # 本地图片、文件、语音默认用 base64；共享文件系统可改为 file_uri
forward_max_nodes = 50 # 合并转发最多展开节点数（同一条消息内多个转发 id 共享）
forward_max_runes = 20000 # 合并转发展开后的总字符预算（整条消息共享）
forward_max_depth = 4 # 合并转发最大嵌套深度
forward_max_fetches = 8 # get_forward_msg 最多拉取次数
forward_max_result_bytes = 1048576 # 单次 get_forward_msg 返回体大小上限
forward_max_non_text = 20 # 转发中图片/文件/语音等非文本节点上限
forward_fetch_timeout_seconds = 5 # 单次 get_forward_msg 超时
inbound_dedup_enabled = true # 按 平台+机器人+scope+消息ID 去重，不依赖历史记录开关
inbound_dedup_ttl_seconds = 1800 # processing/completed/failed 状态保留时长
inbound_dedup_max_entries = 4096 # 去重表硬上限，超出按最旧记录淘汰
preprocess_workers = 4 # 解析 @、引用、合并转发的有界 worker 数
preprocess_queue_size = 256 # 普通消息预处理队列；满时本地丢弃，不产生无界 asyncio task
high_priority_workers = 1 # 撤回/成员/管理事件专用 worker，不排在普通消息后面
high_priority_queue_size = 128 # 高优先级事件队列；满时 readLoop 内联执行而不是丢弃
show_tool_preview_in_group = false # 群聊里是否投递"正在调用某工具"的进度预览（默认关闭）
```

> **容器部署注意**：`ws_url` 是 **ATRI-X 容器内**要访问的地址。`ws://127.0.0.1:6700/` 在容器内指向 ATRI-X 自身。OneBot 在同一个 Compose 项目（或同一 Docker 网络）的服务中用服务名，例如 `ws://onebot:6700/`；OneBot 在宿主机时，需要配置容器可访问的宿主机地址（Linux 可加 `extra_hosts: ["host.docker.internal:host-gateway"]` 后写 `ws://host.docker.internal:6700/`）；OneBot 在另一台机器时写其 IP / 域名。

`access_token_env` 指向保存 Access Token 的环境变量名；原有 `access_token` 仍然兼容且优先于 `access_token_env`。OneBot 不要求鉴权时，两项都可省略。

> **内置协议端**：便携版 / 安装版可以内置一套 NapCat + 便携 QQ（构建参数
> `--with-qqproto`，见 [packaging/README.md](../packaging/README.md) §3.5）。此时上面这段
> `[platform.qqonebot]` 由桌面端首次启动时自动补齐（`ws_url` 指向本机空闲端口、
> `access_token_env = "QQONEBOT_ACCESS_TOKEN"`、`enabled = false`），并同步生成
> `%APPDATA%\ATRI-X\.env` 里的 OneBot 令牌与 NapCat 的 `onebot11.json` / `webui.json`；
> **已有值一律不动**，没有内置环境的构建或自备 NapCat 的配置完全不受影响。之后在「在线」页
> 点「启动协议端」→「打开 WebUI」扫码 → 「连接（上线）」即可。

`show_tool_preview_in_group` 控制群聊里是否投递工具调用进度预览（例如"正在调用 web_search"）。默认 `false`，与上游 Go 一致——群里的进度提示通常被当成噪声；想在群里看到"现在在用什么工具"就打开它（`[platform.qqofficial]` 有同名字段）。桌面端「工具权限」页提供了这个开关。

`send_file_mode` 同时控制 QQ OneBot 本地图片、文件和 `record` 语音的发送方式。`base64` 适用于 ATRI-X 与 OneBot 不共享文件系统的部署；`file_uri` 仅适用于双方能访问同一本地路径的场景。

OneBot 入站文本会保留原始换行、缩进和连续空白；唤醒判断使用单独规范化后的匹配视图，不会为了匹配关键词而重排模型输入，也不会让转发里的 `@`、唤醒词或命令进入匹配视图。合并转发消息会展开为带发送者、时间、消息 ID 的引用文本，并通过 `forward_max_nodes`、`forward_max_runes`、`forward_max_depth` 限制节点数、总字符数和嵌套深度；`forward_max_fetches`、`forward_max_result_bytes`、`forward_max_non_text`、`forward_fetch_timeout_seconds` 进一步限制拉取次数、单次返回体、非文本节点和拉取超时。同一条消息内所有转发 id 共享节点/字符预算；循环引用会被拒绝，重复引用会标记省略，拉取失败或节点缺字段会稳定降级为文本标记。转发内容始终作为不可信用户数据传入，不会构造成 system/developer 消息。

OneBot 入站消息会先按 `平台 + 机器人 self_id + scope + message_id` 去重，状态分为 `processing` / `completed` / `failed`，在 TTL 内重连重放不会再次唤醒模型或计费；去重表有 `inbound_dedup_max_entries` 硬上限，且不依赖 `history` 开关。`@` 解析、引用拉取和合并转发展开都在 `preprocess_workers` / `preprocess_queue_size` 限定的预处理池里执行，队列满时该条普通消息在本地丢弃并记录告警，而不是无限创建 asyncio task。撤回、成员退群/被踢/禁言和管理类事件走独立的高优先级通道 `high_priority_workers` / `high_priority_queue_size`，不会排在普通聊天消息后面；高优先级队列也满时会在读循环中内联处理，保证撤回和取消不会因为流量洪峰丢失。

`api_timeout_seconds` 分别作为 OneBot 帧写入和 API 响应等待的基础超时。大帧写入会按编码后大小每完整 1 MiB 增加 1 秒，最多增加到基础超时本身；写入失败或超时后 ATRI-X 会断开并重连 OneBot，避免一个慢发送长期占住后续消息。发送仍会同步等待平台回执。

> 桌面端「在线」页把上下线做成了开关：只改 `[platform.qqonebot].enabled` 再重启后台服务，
> 状态（连接/断开、连接次数、最近错误）从后台 `/healthz` 的 `platforms[]` 读。桌面端首次启动
> 还会往 `.env` 补 `ATRIX_HEALTH_ADDR=127.0.0.1:32171`（被占则顺延）与随机 `ATRIX_OPS_TOKEN`，
> 已有值一律不动；手动部署请自己设这两个变量后重启。发行包内置协议端时，同一页还能启动
> 协议端并打开带令牌的 WebUI（见上面「内置协议端」）。详见
> [Windows 桌面端](windows-app.md) §4.8。

Telegram 使用 Bot API long polling。最小配置示例：

```toml
[security.superadmins]
cli = ["local"]
telegram = ["123456789"]

[platform.telegram]
enabled = true
bot_token_env = "TELEGRAM_BOT_TOKEN"
proxy_url_env = "TELEGRAM_PROXY_URL" # 可选
trigger_keywords = ["bot"]
format = "html" # html/plain/rich
stream_edit_interval_milliseconds = 250
```

说明：

- `bot_token_env` 指向保存 Bot Token 的环境变量名；也可以用 `bot_token` 直接写入配置，但不建议提交真实 token。
- `proxy_url_env` 指向保存代理地址的环境变量名；也可以用 `proxy_url` 直接写入配置。代理地址示例：`http://127.0.0.1:7890`、`socks5://127.0.0.1:1080`。
- `format="html"` 是默认值：使用普通 `sendMessage` + `parse_mode="HTML"`，并把常见 Markdown 轻量转换成 Telegram HTML；支持标题、引用、分割线、代码块和表格的可读渲染，失败时自动纯文本重试。
- `format="plain"` 关闭格式化，只发送纯文本。
- `format="rich"` 是实验模式：使用 `sendRichMessage` / 私聊 `sendRichMessageDraft`，Rich Message 失败时会自动退回 HTML；部分客户端可能无法查看 Rich Message。
- `stream_edit_interval_milliseconds` 控制流式刷新节流间隔，默认 250ms，避免触发平台限频。
- 启动连接成功后，ATRI-X 会把内置 slash 命令同步到 Telegram bot 命令菜单；只同步主命令名，不同步 alias。
- 群聊/超级群组中，命令前缀、触发关键词、`@bot_username` 或回复 bot 消息都会触发处理。私聊默认处理。
- 高风险工具确认消息会附带 Telegram inline keyboard，点击按钮会转换为 `/confirm`、`/reject` 等现有确认命令。
- `security.superadmins.telegram` 填 Telegram 用户 ID 或可直接发送的私聊 chat ID，用于超级管理员权限与通知投递。


## 插件和 Hook 配置

插件配置固定放在配置目录的 `plugins/` 下：

- `plugins/hooks.toml`：规则 Hook 配置。
- `plugins/<plugin-id>/hook.toml`：被 `hooks.toml` 引用的插件 Hook；可包含 `[plugin.runtime]` 持久运行配置。
- `plugins/_shared/`：ATRI-X 创建的跨 Hook 文件协作目录，不作为插件扫描。

Hook 不要直接发平台消息，应返回输出意图，由 Agent 统一交给 Output Manager 发送。

规则与持久 Hook 的完整配置说明见 [Hook](hooks.md)。

## 建议的维护方式


- 用户可编辑配置集中放在平台配置目录，避免直接改源码示例。
- 新增配置项时同步更新本文档。
- 改变默认路径或启动行为时同步更新 [快速开始](getting-started.md) 和 README。
