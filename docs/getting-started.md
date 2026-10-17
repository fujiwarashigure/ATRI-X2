# 快速开始

本文档用于把 ATRI-X 跑起来，并完成第一次 CLI 对话。

## 环境要求

- Python 3.11 或更新版本（开发与验证在 3.14 上完成）。
- 一个 OpenAI-compatible LLM 服务，例如 DeepSeek、OpenAI 或其他兼容 `/chat/completions` 的服务。
- 至少一个可用 API Key。

不需要安装任何第三方包：本项目**零第三方依赖，纯标准库**，在仓库根目录直接运行即可。

## 获取代码

```bash
git clone <本仓库地址> ATRI-X2
cd ATRI-X2
```

如果已经在本仓库内，可以直接继续下一步。

## 配置 Provider

ATRI-X 默认按配置查找顺序读取主配置。首次运行且平台配置目录没有 `app.toml` 时，会在平台配置目录自动生成 `app.toml`、`services.toml`、`state.toml`、`SOUL.md`、`atrinis.toml` 和 `.env.example`；已有配置文件不会被覆盖。

Provider、生图和模型别名默认写在 `services.toml`。默认配置已经包含 DeepSeek 和 OpenAI Provider：

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
models = ["gpt-4o-mini"]
```

推荐把密钥放在系统环境变量，或把自动生成的 `.env.example` 复制为配置目录下的 `.env` 后填写，不要直接写进 `services.toml`（或旧 `providers.toml`）。

示例 `.env`：

```dotenv
DEEPSEEK_API_KEY=your-api-key
OPENAI_API_KEY=your-api-key
```

## 选择默认模型

默认运行态模型写在配置目录的 `state.toml`：

```toml
[session]
default_mode = "work"

[mode_models.work]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.chat]
provider = "deepseek"
model = "deepseek-chat"
```

如果使用其他 Provider，需要同步修改 `provider` 和 `model`。
default_mode 手动设置，其他设置可以在cli中使用命令设置。

## 启动 ATRI-X

无需构建、无需安装，在仓库根目录直接运行：

```bash
python -m atri_x
```

如需使用临时配置文件，可以显式指定：

```bash
python -m atri_x --config path/to/app.toml
```

常用运行模式：

```bash
python -m atri_x              # 自动模式
python -m atri_x run          # 完整前台：CLI + 已启用平台 + Cron
python -m atri_x cli          # 本地 CLI-only：只启动 CLI，不启动平台和 Cron
python -m atri_x service run  # Linux/headless 服务模式：不启动 CLI，启动已启用平台和 Cron
```

> 移植状态：见 devdocs/porting-status.md 待核对

本节的标注只针对两件事：**自动模式的 service 探测判定**与下面的 **CLI TUI 按键**。前者本机两种分支都实测过（已有 service 在跑 → CLI-only；没有 → 完整前台）；后者有 headless 用例（`tests/test_platform_cli_tui.py`），但**没有在真实 TTY 里逐项体验**过 copy mode、拖拽调宽和 `#<文件名>` 补全。

自动模式下，Linux 如果检测到当前用户已有 `python -m atri_x service run` 在运行，会进入本地 CLI-only，避免重复连接平台或重复运行 Cron；否则进入完整前台模式。Windows 不做 service 检测，默认完整前台启动。

服务模式下进程的生命周期由外部信号决定：所有已启用平台运行时都退出后进程仍然存活（`/live`、`/ready`、`/tasks` 继续可用），停止请发送 `SIGTERM` / `Ctrl+C`。某个平台运行时**非预期**退出时，`/healthz` 会把它标成断开，并给其他仍在运行平台的配置超管（`security.superadmins`）发一条错误通知；正常关停与应用取消不会通知，没有配置超管的平台只留日志。

`python -m atri_x cli` 是独立本地进程，会使用同一套配置和 SQLite 数据，但不会接管 service 进程中的当前请求、确认状态或内存里的当前 Session。需要继续历史会话时，可用 `/list` 和 `/resume`。

CLI TUI 常用按键：

- `PgUp` / `PgDn` / `Home` / `End`：滚动聊天输出。
- `Ctrl+K` / `Ctrl+J`：小幅滚动聊天输出；`Ctrl+U` / `Ctrl+D` 滚动通知区。
- `Alt+h` / `Alt+l`：进入聊天区 / 通知区 copy mode。
- copy mode 中可用 `h/j/k/l` 与 `w/e/b` 移动，`v` / `V` 选择，`y` 复制，`/` 搜索，`i` 或 `Esc` 返回输入。
- `#<文件名>`：补全并引用本地文件；文件不存在时保留原文，按普通 `#` 文本发送。
- 宽屏模式下可按住聊天区与通知区之间的竖线拖动，调整两侧宽度；本次运行期间会保留调整结果。

Python 版**没有构建步骤**：`atri_x/` 是等价于 Go 侧 `cmd/atri-x` 的解释执行入口，仓库根目录下 `python -m atri_x ...` 就是最终形态，不存在 `go build` 那样的产物。要在别的目录调用，把该目录加入 `PYTHONPATH` 或先 `cd` 到仓库根目录即可。

## Shell 补全

ATRI-X 可以生成常见 shell 的补全脚本：

```bash
python -m atri_x completion bash
python -m atri_x completion zsh
python -m atri_x completion fish
python -m atri_x completion nushell
python -m atri_x completion powershell
```

也可以用 `auto` 按当前环境变量猜测 shell：

```bash
python -m atri_x completion auto
```

示例：fish

```bash
python -m atri_x completion fish > ~/.config/fish/completions/atri-x.fish
```

示例：nushell

```bash
python -m atri_x completion nushell > ~/.config/nushell/completions/atri-x.nu
```

然后在 Nushell 配置中 source 该文件。

## 第一次对话

启动后直接输入消息即可。默认示例中 `state.toml` 的 `default_mode` 是 `work`，会启用工具发现能力。

常用起步命令：

```text
/help
/status
/models
/model 1
/chat
/work
```

- `/help` 查看可用命令。
- `/status` 查看当前 Session、模型、上下文和请求状态。
- `/models` 查看可用模型列表。
- `/model <编号或名称>` 切换模型。
- `/chat` 切到低成本聊天模式。
- `/work` 切到可使用工具的工作模式。

更多命令见 [命令速查](commands.md)。

## 配置目录和数据目录

默认配置查找顺序：

1. `--config` 指定的路径。
2. `ATRIX_CONFIG_FILE` 环境变量。
3. 平台配置目录，例如 Windows `%APPDATA%/ATRI-X/app.toml`，Linux XDG 配置目录。
4. 如果平台配置不存在，则自动生成平台默认配置。

运行数据默认进入平台数据目录，例如 SQLite、日志和 sandbox。具体规则见 [配置说明](configuration.md)。


## 常见问题

### 启动时报 API Key 缺失

检查：

- `services.toml`（旧部署为 `providers.toml`）中的 `api_key_env` 是否写对。
- 系统环境变量或配置目录 `.env` 是否包含对应 Key。
- 当前 shell 是否能读取这些环境变量。

### 模型不可用或名称不对

检查：

- `state.toml` 中的 `provider` 是否存在于 `services.toml`（旧部署为 `providers.toml`）。
- `model` 名称是否被 Provider 支持。
- 可以启动后用 `/models --fresh` 或 `/models --refresh` 刷新模型列表。

### 不想默认启用工具

把 `state.toml` 中的默认模式改为 chat：

```toml
[session]
default_mode = "chat"
```

需要工具时再输入 `/work`。
