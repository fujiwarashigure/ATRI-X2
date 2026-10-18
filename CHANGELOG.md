# 变更记录

本文件**只做补丁式写作**：描述从旧状态怎样变化到新状态。其它文档（`README.md`、`docs/`、`devdocs/`）只描述「现在正确的状态」，不写历史。

本仓库 `ATRI-X2-Py` 是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（v0.9.0，781 个 `.go` 文件 / 156,773 行生产代码）的 **Python 全量重写**。因此本文件的「旧状态」有两层含义：一是**跨语言**（Go → Python），二是 Python 仓库内部的后续改动。跨语言那一层集中在下面「未发布」的第一节，之后按正常节奏追加。

---

## [0.9.1] - 2026-10-18

> 本版是仓库的**首个 Windows 发行版**：Windows 桌面端（Tk GUI + 托盘 + 安装包）、内置 DSH 深度路由、
> GUI 会话浏览、GitHub Actions 容器镜像与发行流程都在这一版落地。与上游 Go 版的整体对应关系见
> [`PORTING.md`](PORTING.md) 与 [`devdocs/porting-status.md`](devdocs/porting-status.md)。

### 本版摘要（v0.9.1）

- **Windows 桌面端**：`atri-x-gui.exe`（Tk 窗口 + 托盘常驻 + 单实例 + 后台服务自动拉起），关窗口不退出，QQ 机器人保持在线；首次启动自动打通 `[platform.cli.server]` 与本地 token。
- **安装/分发**：Inno Setup 单文件安装包（用户级、不弹 UAC、默认开机自启）；`atri_x.dist.install` 安装/卸载/状态；内置 DSH 内核随包分发。
- **DSH 深度路由**：`call_llm` 前按任务打分 + 成本闸门 + 熔断决定是否交给内置 DSH，ACP 用量并入既有预算账本（见 [`devdocs/dsh-acp.md`](devdocs/dsh-acp.md) §6）。
- **GUI 会话浏览**：前端协议新增只读 `sessions` / `history` 帧，左侧会话栏切换复用 `/resume`。
- **发行与 CI**：`.github/workflows/release-image.yml` 在 tag 推送时构建并冒烟 `ghcr.io/fujiwarashigure/atri-x2` 镜像；容器镜像与 Windows 安装包一起随 GitHub Release 发布。
- **仓库清理与脱敏**：修正 `.gitignore` 把 `atri_x/dist/` 误当构建产物忽略的问题；删除测试/构建残留；文档里的本机绝对路径改为通用占位符。

### 从 Go 版到 Python 版（本次重写）

- **逐包重写，不留桩**：上游 71 个生产包各有 Python 对应模块，包级映射表是 [`PORTING.md`](PORTING.md) §4（唯一出处）。`cmd/atri-x` → `atri_x/__main__.py`；`internal/<pkg>/...` → `atri_x/<pkg>/...`；`deploy/*.sh` → `deploy/*.py`。
- **运行方式变了**：不再有 `go build` 产物，也不再有单静态二进制。入口是 `python -m atri_x`，在仓库根目录直接运行，解释执行、无需安装、无需构建。
- **纯标准库**：运行时零第三方依赖。Go 侧那几个第三方库由 Python 侧自研模块顶替：
  | Go 依赖 | Python 对应 |
  |---|---|
  | `github.com/coder/websocket` | `atri_x/net/websocket.py`（标准库 `socket` / `ssl` 上的 RFC 6455） |
  | `pelletier/go-toml/v2` | 读用标准库 `tomllib`，写用 `atri_x/net/tomlw.py`（`tomllib` 只读） |
  | `aws-sdk-go-v2/service/s3` | `atri_x/net/s3.py`（SigV4 最小 REST 客户端） |
  | `robfig/cron/v3` | `atri_x/net/cronspec.py` |
  | `mvdan.cc/sh/v3`（shell AST） | `atri_x/shellparse/`（**clean-room 自研**；`bashlex` 是 GPLv3+ 且已停更，与 Apache-2.0 冲突，不引入） |
  | `bubbletea` + `lipgloss` + `bubbles` | `atri_x/platform/cli/tui/`（ANSI + 原始模式输入） |
  | Go 标准库 `image` / `image/jpeg` / `image/png` | `atri_x/media/imagecodec/`（纯 Python 编解码，可选 `Pillow` 加速后端） |
  | `modernc.org/sqlite` | 标准库 `sqlite3`（随 CPython 分发的 C 扩展） |
  | `mattn/go-runewidth` | `atri_x/utils/runes.py` |
- **并发模型变了**：goroutine + channel → `asyncio` task + `asyncio.Queue`；`sync.Mutex` → `asyncio.Lock`（异步路径）或 `threading.Lock`（同步 SQLite 路径）；`context.Context` → ctx 对象 + `atri_x/safecontext.py` 的取消令牌 + `asyncio.CancelledError`。
- **错误模型变了**：Go 的 `(T, error)` 双返回 → Python 抛异常；哨兵异常（对齐 Go 的 `ErrXxx`）统一在 `atri_x/errors.py`，需要 `is` 判定的用模块级单例异常实例。
- **时间与 ID 口径统一**：`time.Time` → 带 `timezone.utc` 的 `datetime`，落库仍用 RFC3339（与 Go `time.RFC3339Nano` 兼容）；ID 仍是**带连字符的小写 UUIDv4**，与 `internal/storage/id.go` 的输出形态一致。
- **文档全量本地化**：`README.md`、`docs/**`、`devdocs/**`、`AGENTS.md` 从上游对应文件改写而来，一切 Go 专属表述（`go build`、`go test`、`gofmt`、`go.mod`、`.go` 路径、`internal/...`）替换为 Python 对应物；不复制英文镜像（上游 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物）。
- **文档里承诺的命令行接口**（与实现对接的约定，逐条见 [`devdocs/porting-status.md`](devdocs/porting-status.md) §2）：

  ```bash
  python -m atri_x                      # 自动模式
  python -m atri_x run                  # 完整前台
  python -m atri_x config check         # 配置自检
  python -m atri_x doctor               # 部署验收（--no-model / --e2e / --json / --require-platform）
  python -m atri_x --health-probe       # 健康探针（Python 版新增）
  python scripts/check_syntax.py        # 语法 + 编码体检
  python scripts/check_imports.py       # 逐模块导入校验
  python scripts/check_integration.py   # 跨模块集成一致性
  python scripts/check_coverage.py      # 按映射表算加权覆盖率
  ```

### Added

- 新增 Python 侧必需、Go 侧由标准库或第三方库直接提供的模块：`atri_x/net/`（websocket / httpclient / tomlw / s3 / cronspec）、`atri_x/shellparse/`、`atri_x/media/imagecodec/`、`atri_x/platform/cli/tui/`、`atri_x/utils/runes.py`。
- 新增 `atri_x/__main__.py` 的 `--health-probe[=URL|host:port|/path]` 开关与 `deploy/health_probe.py`：Go 上游的健康检查依赖外部 `curl`，Python 版把它做成应用内置开关，容器 `HEALTHCHECK` 与 watchdog 可直接调用。
- 新增 `atri_x/launcher/cli.py`：命令行解析与 shell 补全生成（`bash` / `zsh` / `fish` / `nushell` / `powershell` / `auto`）。
- 新增验收与体检脚本：`scripts/check_syntax.py`（语法 + BOM + CRLF + U+FFFD + mojibake）、`scripts/check_imports.py`、`scripts/check_integration.py`、`scripts/check_coverage.py`、`scripts/extract_assets.py`、`scripts/dump_hang.py`。
- 新增文档：[`devdocs/porting-status.md`](devdocs/porting-status.md)（移植规则速查、命令行接口清单、缺失模块、待核对功能点清单）与 [`AGENTS.md`](AGENTS.md)（Python 版 Agent 操作手册）。
- 新增配置资产漂移比对：`scripts/extract_assets.py --check` 对比内置配置资产与上游 `assets.go` 的差异。

### 内置 DSH（DeepSeek Harness）最小闭环

- 镜像新增 DSH 阶段（`node:24-bookworm-slim` + `@deepseek-ai/dsh@0.2.0-rc.2`）：DSH 以 `dsh --profile acp` 的 stdio 子进程形式内置，不开端口；状态根 `/data/dsh`、会话工作区 `/data/dsh-ws` 与 ATRI-X 数据同卷。运行阶段因此固定安装 `libstdc++6` 与 `ca-certificates`（node 二进制依赖），其余 apt 包仍默认不装。
- 新增 `atri_x/agent/dsh/`（纯标准库、导入无副作用）：`types`（ACP 数据类型与解析）、`transport`（NDJSON JSON-RPC 子进程传输）、`client`（`initialize` / `session/*` / `prompt` / `cancel` / `close`）、`smoke`（`python -m atri_x.agent.dsh.smoke` 闭环冒烟，打印真实 token 用量）。客户端默认**拒绝**一切权限请求——自动放行会绕过 ATRI-X 自己的风险确认。
- 新增构建参数 `NODE_BASE_IMAGE` / `DSH_VERSION` / `DSH_NPM_REGISTRY`，`.env`、compose 与 `deploy/build_push.py` 均可覆盖。
- 新增离线测试 `tests/test_dsh_acp_client.py` + `tests/_dsh_fake_acp_server.py`：不依赖 Node/dsh，覆盖握手、会话生命周期、流式事件、token 用量、权限默认拒绝、未知服务端请求、超时取消、进程崩溃与冒烟入口。
- 本阶段**只落闭环**：路由阈值、上下文交接与预算记账尚未接入对话链路，设计见 [`devdocs/dsh-acp.md`](devdocs/dsh-acp.md)。

### Windows 原生应用与 QQ OneBot 连通性

- 新增 `packaging/`（此前只有 Docker 形态的 [`deploy/windows/`](deploy/windows/README.md)，没有任何冻结/打包脚本）：`atri-x.spec`（收集 `atri_x` 全部子模块、打入 `config/assets_data` 与 `imagegen/prompts`、默认排除 Pillow/numpy/PyYAML/tkinter）、`build.py`（构建 + **产物级核验**）、`entry.py`（冻结入口，转发到 `atri_x.__main__:main`，并支持 `-m <模块>` 复刻 `python -m`）、`README.md`。产物为 onedir 目录（约 27 MB / 44 文件）或单文件 exe，运行期仍是纯标准库。
- 新增 `atri_x/platform/qq_onebot/smoke.py`（`python -m atri_x.platform.qq_onebot.smoke`，冻结后用 `atri-x.exe -m ...`）：一次跑完"连上协议端 → `get_login_info` → 可选发群/私聊 → 可选监听 N 秒上报事件"，把 QQ 链路故障压到不依赖模型的一层。它不读 `app.toml`，`--url` / `--access-token-env` 显式给；`--json` 输出 `ok` / `self_id` / `sent` / `event_count`。
- 新增离线端到端用例 `tests/test_qq_onebot_smoke.py`：用 `atri_x/net/websocket.py` 的服务端实现起一个**真的走 WebSocket** 的假 OneBot v11 协议端，覆盖动作通道、发送通道（`message_id` 回读）、事件监听、Bearer token 透传与连不上时的退出码；不依赖 QQ/NapCat，可进 CI。
- 修复 `pyproject.toml` 的 `[project.scripts]`：`atri-x` 原来指向 `atri_x.launcher.cli:main`，而该模块并没有 `main`（直接 `ImportError`），`pip install` 出来的命令是坏的；现指向真正的入口 `atri_x.__main__:main`。
- 文档新增 [`docs/windows-app.md`](docs/windows-app.md)（原生 exe 的构建、首启、接 QQ、冒烟与排障）与 [`packaging/README.md`](packaging/README.md)（构建细节、冻结限制）；`docs/README.md` 阅读顺序补一条。三种部署形态（Linux/Docker、Docker Desktop、原生 exe）共用同一份配置格式。

### Windows 桌面端（Tk GUI + 托盘 + 安装器 + 内置 DSH 内核）

- 新增 `atri_x/gui/`（纯标准库，Tk 9.0 实测）：`app.py` 主窗口（聊天气泡式记录、流式渲染、状态栏、菜单、补全）、`client.py`（WS 客户端：`hello` 握手、断线重连、鉴权失败单列状态）、`protocol.py`（帧 → 界面状态的纯逻辑，复用 `RemoteMessage`/`format_compact`，不抄第二份字段映射）、`service.py`（准备配置与 token、探测/拉起后台服务）、`process.py`（程序/状态目录、pid 记录、强制停服务、单实例）、`tray.py`（`ctypes` 调 `Shell_NotifyIconW` 的托盘，独立线程 + 隐藏窗口 + 消息循环）、`autostart.py`（`HKCU\…\Run` 开机自启）、`__main__.py`（单实例、`--minimized`、`--selftest`、`--no-service`）。
- **关窗口 ≠ 退出**：默认隐藏到托盘，Agent 继续跑在独立的 `atri-x service run` 进程里（GUI 只是 `docs/frontend-api.md` 那套 WS 协议的前端），QQ 机器人保持在线；要彻底退出用托盘/「文件」菜单，要停服务走「服务 → 停止后台服务（强制）」。
- 首次启动自动打通前后端：`app.toml` 的 `[platform.cli.server].enabled` 置 `true`（只改这一行、写前留 `.bak`、写完用 `tomllib` 重新解析校验），token 写入配置目录 `.env` 的 `ATRIX_CLI_LOCAL_TOKEN`（已有/环境变量优先生效，幂等）。这两处是 GUI 唯一会写的用户配置。
- 新增 `atri_x/dist/`：`shell_link.py`（`ctypes` 走 COM `IShellLinkW` + `IPersistFile` 建/读 `.lnk`）、`install.py`（`python -m atri_x.dist.install install|uninstall|status`）：复制发行包到 `%LOCALAPPDATA%\ATRI-X\app`、建开始菜单/桌面快捷方式、写 `HKCU` 「应用和功能」卸载项、可选 `--autostart`/`--add-path`；卸载默认**保留** `%APPDATA%\ATRI-X` 用户数据，`--purge-data` 才连数据一起删。
- 新增 `atri_x/sidecar/`（内置运行时的通用发现层）：`registry.py`（`ATRIX_RUNTIME_DIR` → 发行包 `runtime/` → `<配置目录>/runtime` 的查找顺序，`kernel.json` 清单解析）、`kernel.py`（DSH 内核定位与 `[node.exe, bin.js, --profile acp]` 命令组装，找不到就回退 PATH 上的 `dsh`）、`__main__.py`（`python -m atri_x.sidecar list|status|dsh-command|verify [--acp]`）。
- 新增 `packaging/assemble_kernel.py`：从**本机已装好的 DSH** 组装自包含内核到 `runtime/dsh-kernel/`（node.exe + 依赖闭包 + `kernel.json`）。做法是先跑 `--dump-config` 取 acp profile 引用的包、按 `dependencies` 求闭包、剪掉 Web/桌面前端包，再**真启动 ACP 服务端发 `initialize`** 核验；启动期报缺包就从源目录补拷后重试（实测 2 轮补拷 23 个包通过）。实测产物 384 包 / 189.1 MB + node.exe 92.8 MB，握手返回 `deepseek-harness-acp/0.0.1`。
- `atri_x/agent/dsh/smoke.py` 启动命令改为「显式 > `DSH_ACP_COMMAND` > 内置内核 > PATH 上的 `dsh`」，内置内核优先，因此不再需要外部 `dsh` 安装。
- `packaging/` 升级：onedir 现在出**两个 exe**（`atri-x.exe` 控制台 + `atri-x-gui.exe` 窗口版，共用 `_internal`），`entry.py` 按 exe 文件名分流；`tkinter` 不再被排除；`--with-kernel/--no-kernel`（默认按 `runtime/dsh-kernel/kernel.json` 自动判断）、`--no-gui`；产物核验新增 `-m atri_x.sidecar status` 与窗口版 `--selftest` 两项。
- 新增离线用例 66 个：`tests/test_gui_protocol.py`（记录/流式/补全/状态栏）、`tests/test_gui_client.py`（真 WebSocket 假 CLI 服务端：握手、收发、鉴权失败、断线）、`tests/test_gui_service.py`（配置定向修改、token 幂等、WS 探测）、`tests/test_sidecar_kernel.py`（发现顺序、清单校验、命令组装）、`tests/test_dist_install.py`（假注册表/假快捷方式的安装/卸载/状态）。
- 文档重写 [`docs/windows-app.md`](docs/windows-app.md)（新增桌面端、托盘/自启/安装、内置 DSH 内核三节，并更新构建产物、命令表与限制）与 [`packaging/README.md`](packaging/README.md)（双 exe、内核组装、安装器、核验清单）；`devdocs/code-map.md`、`devdocs/dsh-acp.md`、`AGENTS.md` locator 表同步。

### Windows 桌面端（第二轮）：会话列表、Inno 安装包、DSH 深度路由

- **会话列表与历史**：前端协议新增两对**只读**帧（Python 版扩展，Go 上游没有）：
  客户端 `sessions` → 服务端 `sessions_result`（id/标题/模式/最近活跃/是否当前）、
  客户端 `history` → 服务端 `history_result`（用户/助手消息 + 时间）。服务端实现复用
  `atri_x.session.Service.list_page` 与 `can_access`（新模块 `atri_x/session/browse.py`），
  不碰 Agent 上下文装载；GUI 侧新增 `atri_x/gui/sessions.py` 会话侧边栏（双击切换 =
  发 `/resume <id>` + 拉历史，`Ctrl+N`/`新建` = `/new`）。切换/鉴权/审计仍走既有命令链路。
  `docs/frontend-api.md` 增补「扩展帧：会话浏览」一节。
- **Inno Setup 安装包**：新增 `packaging/atri-x.iss` + `packaging/build_installer.py`，
  把 onedir 打成单个用户级安装包（`PrivilegesRequired=lowest`，不弹 UAC）：
  `%LOCALAPPDATA%\ATRI-X\app`、开始菜单快捷方式、默认勾选开机自启（HKCU Run `ATRI-X`，
  与 `autostart.py` 同值名）、`install.json` 标记 `"installer":"inno"`、静默安装/卸载参数、
  `--verify` 自检（本地盘 + 短路径，实测 317.9 MB onedir → 56.0 MB 安装包；安装 40.7 s /
  23865 文件、卸载 13.6 s 且清空目录）。`packaging/build.py` 新增
  `--with-installer` / `--installer-verify` / `--installer-output`。
  `atri_x.dist.install` 检测到 Inno 安装箱时会拒绝脚本卸载并提示 `unins000.exe`
  （新增 `--force` 可强删），`status` 输出新增 `installer` 字段。
- **DSH 深度路由（§6 落地）**：新增 `atri_x/agent/dsh/{command,router,handoff,runtime,route_call,status}.py`，
  在 `call_llm` 的原生调用前问一次路由：任务特征打分（代码痕迹/路径/任务动词/多步…）+
  成本闸门（`score − λ·cost_pressure`）+ 连续失败熔断；命中时把「系统设定 + 最近 N 轮 +
  当前消息」拼成一次性 prompt 交给内置内核（每次新开 DSH 会话，无状态交接），流式文本写回
  同一个 `MessageStream`，返回普通 `LLMCallResult`，因此落库/发送/状态发布全部沿用原生路径；
  ACP 真实 `usage` 走既有预算服务（`begin_chat_budget`/`settle_chat_budget`/`record_chat_usage`），
  与原生用量共享按用户/群/日额度。失败时：无输出→静默回退原生，有部分输出→保留并提示。
  新增配置段 `[agent.dsh]`（`enabled`/`min_score`/成本参数/模型/超时/历史预算/权限/熔断，
  默认值写入 `atri_x/config/assets_data/app.toml`）；新增
  `python -m atri_x.agent.dsh.status [--json] [--score 文本]` 查看内核/熔断/打分。
- 新增离线用例 68 个：`tests/test_dsh_route.py`（打分/成本闸门/熔断/交接/运行时/预算/失败回退，
  用假 ACP 服务端真跑子进程）、`tests/test_cli_sessions.py`（新帧协议往返、查询层、服务端分发、
  GUI 解码与侧边栏）；`tests/test_gui_client.py` 扩展会话帧收发；`tests/test_dist_install.py`
  扩展 Inno 标记与拒绝卸载。
- 修复 `SyncWebSocket.close` 的非幂等：重复关闭会对已停事件循环再造一个没人 await 的
  `connection.close()` 协程（`RuntimeWarning`）。
- 修复 `atri_x/sidecar/__main__.py` 的核验目录：冻结时不再写 `_internal/build/sidecar-verify`
  （会被下一次打包/安装包带进发行物），改为写到 onedir 之外的同级目录。

### 发行与持续集成（GitHub Release / ghcr.io）

- 新增 `.github/workflows/release-image.yml`：tag `v*` 推送（也支持 Release 发布与手动触发）时构建
  `deploy/Dockerfile`，推送 `ghcr.io/fujiwarashigure/atri-x2:<版本>` 与 `:latest`，随后**回拉镜像跑
  `python -m atri_x --version` 冒烟**；构建附带 provenance 与 SBOM，并尝试把包可见性设为 public。
- 新增仓库根 `.dockerignore`：排除 `.git`、`tests`、`docs`、`runtime/`（内置 DSH 内核约 277 MB）、
  `dist/`、`build/` 等，构建上下文只保留 Dockerfile 需要的 `atri_x/`、`pyproject.toml`、`README.md`。
- 发行产物：`ATRI-X-<版本>-setup.exe`（Windows 安装包）、`atri-x-<版本>-repo.tar.gz`（完整仓库归档）、
  `atri-x-<版本>-src.tar.gz`（离线源码包）、`SHA256SUMS`；后两者由
  `deploy/pack/prepare_offline.py --skip-images` 与 `git archive` 生成。

### Changed

- `go test ./...` → `python -m unittest discover -s tests -t .`；测试放 `tests/test_<模块>.py`，用标准库 `unittest`（不引入 pytest）。
- `gofmt` / `go vet` → `python scripts/check_syntax.py`（编码与语法体检）；`go build` 这一环整体消失。
- `go.mod` / `go.sum` / `GOPROXY` / `GO_VERSION` 不再存在：本项目零第三方依赖，没有模块代理与版本守卫。
- 部署脚本全部由 shell 改为 Python：`deploy/{backup,restore_verify,upgrade,rollback,init_host,build_push,health_probe,atri_x_watchdog}.py`；Windows 侧不再经过 PowerShell 调度。
- 文档里的「单个静态二进制 / 6 平台交叉编译」口径改为「需要 Python 解释器，仓库根目录直接运行」；`deploy/pack/` 的离线包提供预构建镜像 tar 而不是静态二进制。
- Go Skill（用 Go 编写的原生 Skill）**保留**：仍由 `atri_x/tool/skill/go_source.py` 调用 `gofmt` 与 Go 工具链完成格式化和编译，Skill 的 `skills/go/<name>/SKILL.atriyph` 目录约定不变。上文「不再有 `go build`」指的是**本仓库自身的构建**，不是 Skill 编译。
- `devdocs/` 里三份 Go 时代的工作台账（`upstream-sync-todo.md`、`upstream-changelog-cross-check.md`、`smoke-test-report.md`）保留原文语义与历史结论，但在文首标注「这是 Go 时代台账」，其中的 `internal/**` 包路径、`*_test.go` 用例名、`go build` / `gofmt` 都属于当时的产物。

### 收尾修复（两轮对抗性验证 + 进程级冒烟之后）

跨语言重写完成后又做了两轮对抗性验证（`verification/round2-*.md`、`verification/round3-*.md`）与一次
Python 侧进程级冒烟（真实 `python -m atri_x service run` + 运维端点探测）。下面这批缺陷的共同点是
**单元测试全绿、真实装配或真实启动才炸**，因此逐条记录修法与识别方式。

**装配层（"传了参数但没人读"/"假实现比生产少一个形参"掩盖）**

- `atri_x/app/runner.py`：`integrations.attach(ctx, req)` 与 `runtime.build(ctx, req)` 都补回 `ctx`
  （Go `integrations.go:16` / `runtime.go:42`）。此前真实启动第一行就 `TypeError`；测试里的假工件形参比
  生产少一个，所以全绿。回归用例改为直接使用生产的 `DefaultIntegrationFactory` / `RuntimeFactory` 签名。
- `atri_x/app/runtime_agent.py`：群服务常驻 `Task` 不再被 `await`（Go 是 `StartGroupServices(ctx)` 同步不等待）。
  此前 `build_agent` 会**永久挂起**，任何真实 `Deps` 都起不来。
- `atri_x/app/runtime_agent.py` + `atri_x/agent/core.py`：`Options.tool_registry` / `naming_notifier` /
  `context_overflow` / `group_services` 四个「在 app 侧被赋值、agent 侧从未读取」的字段全部接线；
  工具注册表此前静默丢弃（模型一个工具都拿不到且不报错），会话命名事件永不发出，`[group_services] enabled = false` 无效。
- `atri_x/app/tool_runtime.py`：真实装配此前只把 9 个依赖传给 `RegisterOptions`，**43 个内置工具只注册 23 个**；
  现按 Go `register.go` 逐字段接全，并补 `internal/tool/skill` 一族 6 个工具的接线（Go 顺序：先建 SkillManager 再 RegisterAll）。
  真实装配注册表 == `REGISTERED_TOOLS`（49 个）。
- `atri_x/hook/process.py`：`Environment.new(...)` → 包级 `atri_x.processenv.new(...)`（Go 里 `New` 是包级函数，不是方法）。
- `atri_x/app/runtime_support.py`：补 `ModelSelection` 导入；`_call_bool` 收敛到 `atri_x.app.support.call_bool`
  （此前裸用了一个只定义在同层 `runtime` 模块的私有名，函数体里的未定义名 import 门禁查不出来）。
- `atri_x/app/runtime.py`：`ParseRisk(raw)` → `ParseRisk(raw, fallback)`，两处 fallback 与 Go
  `security.go:50-51`（`RiskLow` / `RiskHigh`）一致。

**Agent 核心**

- `atri_x/agent/character_directive.py` / `turn_override.py`：`@char:` 角色声明与 `@turn:` 单回合覆盖的四个
  Go `Agent` 方法此前只写成模块级函数，从未绑到 `Agent`，每条正常消息都 `AttributeError` 被吞成"请求失败"；
  现以 mixin 形式挂进 `Agent`。
- `atri_x/agent/seams.py` / `core.py`：`group_tool_allowlist` 等三个方法在 `deps.group_policy is agent`
  时自递归（`RecursionError`）；现按 Go 直接实现，并让服务分派显式排除"服务就是宿主自己"。
- `atri_x/agent/chat_llm.py`：`stream.append(ctx, delta)` 补 `await`。此前增量协程被静默丢弃，
  CLI/TUI/Telegram 的流式增量永不显示、只剩终态 `replace`。

**工具与平台**

- `atri_x/tool/builtin/cron.py` + 新增 `atri_x/cron/jsonview.py`：cron CRUD 服务是 `async` 而工具层照抄 Go 的
  同步形状 → 协程被丢弃，`delete`/`disable` 什么都没做却返回 `{"ok":true}`（cron 继续触发）；现全部 `await`，
  并按 Go `json.Marshal` 形态序列化（`storage.CronJob` 没有 json tag，键即字段名）。
- `atri_x/platform/cli/adapter.py` / `remote_server.py`：`start_stream` / `set_runtime_status` 改回**同步**
  （Go 三个平台的实现都是同步的），此前本地 CLI 每轮失败、TUI 状态栏永不更新。
- `atri_x/platform/telegram/stream.py`：`replace` 里补上 `telegram_text_pages` 的延迟导入（原先是未定义名）。

**门禁与测试**

- `scripts/check_integration.py` 新增「async 函数被当同步语句调用」检查（本轮最高产的缺陷形态），
  检查项 73 → 74。
- 新增回归用例覆盖上述每条（`tests/test_agent_assembly.py`、`tests/test_app_runtime_agent.py`、
  `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_cron.py` 等），并把多个"假实现比生产少一个形参"
  的测试替身换成生产实现。
- 验收：`check_syntax` 763 文件 0 错、`check_imports` 612/612、`check_integration` 74 项 0 失败、
  `check_coverage --strict` 72/72 包 100%、全量 `unittest` 4894 例 OK。

### 第三轮对抗性验证之后的修复（运维端点与 HTTP 语义）

第三轮验证报告（`verification/round3-summary.md`）在**真实进程 + 真实 HTTP 请求**上抓到一批
"单测全绿、真实路径才炸/才静默失效"的问题，逐条记录改法：

- `atri_x/agent/memory_learning_admin.py`：`angel_memory_count` / `self_learning_stats` 此前照 Go 把
  `ctx` 传给**同步存储**服务，而本仓签名是 `Service.count(platform, scope_id)` →
  `GET /plugins/memory` 与 `GET /plugins/learning` **恒回 500**
  （`Service.count() takes 3 positional arguments but 4 were given`）。现按本仓约定改回 2 参，
  两个端点恢复 200；方法仍保留 `ctx` 形参（调用方按 Go 形状传）。
- `atri_x/health/server.py::dispatch`：此前把查询串丢掉，`r.URL.Query()` 等价物恒空 →
  `/plugins/*` 的 `platform` / `scope_id` 过滤**静默失效**（端点照回 200，但永远是空作用域统计）。
  现解析查询串放进 `_RequestView.query`。
- 同一个 `dispatch`：新增 `_consume_awaitable()`，`extra_handlers` 里直接注册 `async def handler(w, r)`
  时协程会被驱动 —— 此前 Go 风格分支的响应体恒空（`200` + `b""`）、函数式 `async def handler(ctx)`
  分支回 `200` + `null`，进程里只留一条 `RuntimeWarning: coroutine ... was never awaited`
  （生产装配被 `LazyHTTPHandler` 包住，所以真实启动路径看不出来；两个分支的症状由故障注入逐一复现过）。
- `atri_x/health/server.py`：`dispatch` 判定顺序改为**路由 → 鉴权 → 方法**，逐字对齐 Go 的
  `ServeMux` + `requireExtraHandlerToken` + `allowGet`：`POST /nope` 由 405 改回 **404**，
  `POST /healthz`（无 token）由 405 改回 **401**；HTTP 层任意动词都进 `dispatch`
  （`HEAD` / `OPTIONS` 此前回 **501 + HTML**，Go 回 405 JSON + `Allow: GET`），`HEAD` 不写实体，
  `Server` 响应头去掉解释器版本（`atri-x-health Python/3.14.0` → `atri-x-health`）。
- `atri_x/app/ops_health.py::marshal`：**字典**不再按 `omitempty` 丢键 —— Go 的 `map` 没有 omitempty，
  `map[string]any{"count": 0}` 必须输出 `"count":0`。此前 `/plugins/*` 的 `count` / `pending` / `approved`
  在值为 0 时整键消失，与 Go 形状不符（dataclass 分支的 omitempty 语义保持不变）。
- `atri_x/app/runtime.py`：`[resident_memory]` 的 normal 写入策略改由
  `resident_normal_write_policy()` 构造，配置里的**秒**换算成 `timedelta`（Go 是
  `time.Duration(...) * time.Second`，`runtime.go:140-145`）。此前直接填 `float(...)`，而
  `NormalWritePolicy.min_interval` / `window` 是 `timedelta` → `normalized()` 抛
  `TypeError: '>' not supported between instances of 'float' and 'datetime.timedelta'`，被
  `_build_resident_store` 吞成一条 `WARN builtin runtime degraded: open resident memory store` →
  **4 个 `resident_memory*` 工具在生产路径永远注册不上**（真实启动只注册 38 个；修复后 42 个、
  `warnings=[]`，`resident_memory` / `resident_memory_read` / `resident_memory_normal` /
  `resident_memory_core` 全部在列）。回归：`tests/test_app_runtime_resident.py`（6 例，含
  "旧 float 形状必须复现 TypeError" 与"生产入口必须不留 warning"的正反两面）。
- `atri_x/agent/status.py`：排进事件循环的状态推送 Task 现在保留强引用（事件循环只持弱引用，
  官方文档要求调用方保存引用；完成即从集合移除）；同时更正该模块 docstring —— 两个 CLI
  `set_runtime_status` 本轮已改回同步，不再举它们当"async 实现"的例子。
- `atri_x/tool/builtin/register.py`：`REGISTERED_TOOLS` 的注释改准 —— "只注册
  `ALWAYS_REGISTERED_TOOLS`" 只对**全空** `RegisterOptions()` 的 `register_all` 成立（13 个）；
  最小**真实**装配 `build_tool_runtime(ToolRuntimeOptions(config_dir=...))` 是 **25** 个
  （与 Go `builtin.NewRuntime` 同义），依赖喂满才是 49。
- 回归用例：新增 `tests/test_app_ops_plugins.py`（真实 handler + 真实服务 + 真实 `dispatch`，含裸 async
  处理器与零值计数）、`tests/test_app_runtime_resident.py`（写入策略形状 + 生产入口无 warning），
  `tests/test_health.py` 新增 5 条（`POST /nope`→404、token 先于方法判定、`HEAD` 无实体、
  `OPTIONS` 405 JSON、`Server` 头无版本号），`tests/test_app_bootstrap.py` 把
  `auto` 模式用例隔离到独立运行目录并补"已有实例持标记 → CLI-only"一条（此前它会因为同机跑着
  真实 service 而变红，属测试自身依赖全局状态）。
- `deploy/stability.py`：采样窗口开启前多等一次 `/ready`（预算复用 `--startup-timeout`）。
  此前 harness 的 `healthy` 前置与 `Dockerfile` 的 `HEALTHCHECK` 一样只看三层探测（最后一层是
  TCP 连通），而冷启动的数据卷首次初始化期间 `/ready` 在容器启动后 8~14 秒仍是 503 ——
  **启动瞬态被算进了"稳定性失败率"**（真实事故：1/50 = 2% > 1% 阈值，逐条取证后全部来自第 1 次
  采样；加了等待后同一棵树复跑 0/50）。新增
  `tests/test_deploy_harness.py::TestStabilityReadyGate` 三条用例（等到 ready、预算耗尽即放弃、
  `duration=0` 的干跑不做真实 HTTP 轮询）；`deploy/TESTING.md` §3 同步说明。

### 已知差异与未完成（诚实披露）

- `atri_x/platform/qqofficial/`、`atri_x/platform/qq_onebot/`、`atri_x/platform/cli/`（含 TUI 与远程 CLI）
  均已落地；它们**没有连接过真实服务端**，端到端行为只由单元测试与本地进程冒烟覆盖。
- 画师串链路（`atri_x/artist/`）已全量落地（解析、串库、渲染、按方言搜索与 `artist_*` 工具），
  但仍未接真实画师串数据做人工核对。
- TUI 的 cell 级渲染保真度低于上游 `bubbletea`；渐进式 JPEG 不支持（明确抛异常）；`atri_x/shellparse/` 覆盖安全裁决所需的语法面，**不是**完整 POSIX shell。
- `GroupPolicyConfig.normalize()` 与上游一样**非幂等**：群作用域没有显式工具策略时
  `group_tool_allowlist` 返回空映射（等价于禁用该群全部工具）。这是上游 Go 同款行为，已在
  `tests/test_agent_bootstrap.py::test_group_allowlist_empty_policy_matches_go` 里钉住并注明出处。
- 文档中所有「上游有、Python 版待核对」的功能点集中登记在 [`devdocs/porting-status.md`](devdocs/porting-status.md) §4，未删除任何功能描述。

### Security

- 上游 Go 源码不含 copyleft 依赖；本重写同样不引入任何 copyleft 依赖 —— 这正是自研 `atri_x/shellparse/` 而不用 `bashlex`、自研 `atri_x/net/*` 而不用第三方客户端的原因。
- Shell / Go Skill / Hook 子进程的凭据变量过滤（名字含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`PRIVATE` 词段）与上游保持同一套规则，由 `atri_x/processenv/environment.py` 统一实现。
