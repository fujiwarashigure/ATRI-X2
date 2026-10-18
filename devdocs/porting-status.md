# 文档移植状态与待核对清单

本页是 `README.md`、`docs/**`、`devdocs/**`、`AGENTS.md`、`CHANGELOG.md` 这些**从 Go 上游改写而来的文档**的统一状态页。
用途有三个：

1. 给写文档的人一份**改写规则速查**（Go 专属表述 → Python 对应物）。
2. 集中登记所有 `> 移植状态：见 devdocs/porting-status.md 待核对` 标注点，供 Lead 逐条核对实现。
3. 列出文档里承诺的**命令行接口**，供 Lead 与实现逐条对照。

上游仓库：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（Go，module `github.com/fujiwarashigure/ATRI-X`，v0.9.0）。
本仓库：Python 重写版（包名 `atri_x`，Python ≥ 3.11）。
包级映射的唯一出处是 [PORTING.md](../PORTING.md) §4；本页只摘录文档改写时最常用的部分。

---

## 1. 改写规则速查（Go → Python）

| Go 侧 | 文档里应写成 |
|---|---|
| `go build` / `go run .` | `python -m atri_x`（Python 版**没有构建步骤**） |
| `atri-x <子命令>` | `python -m atri_x <子命令>` |
| `go test ./...` | `python -m unittest discover -s tests -t .` |
| `gofmt` / `go vet`（指本仓库自身时） | `python scripts/check_syntax.py` |
| `go.mod` / `go.sum` / `GOPROXY` / `GO_VERSION` | 删除，或写「本项目零第三方依赖，纯标准库」 |
| `cmd/atri-x` | `atri_x/__main__.py` |
| `internal/<pkg>/...` | 查 [PORTING.md](../PORTING.md) §4 映射表 → `atri_x/<pkg>/...` |
| `*.go` 文件路径 | 对应 `*.py` 模块路径（同样查 §4；表里只给到包时按职责选文件） |
| `deploy/*.sh` | `deploy/*.py`（见 [deploy/README.md](../deploy/README.md)） |
| `slog` | `atri_x/logging/` |
| `context.Context` | ctx 对象 + `atri_x/safecontext.py` 的取消令牌 + `asyncio.CancelledError` |
| goroutine / channel | `asyncio` task / `asyncio.Queue` |
| `sync.Mutex` | `asyncio.Lock`（异步路径）或 `threading.Lock`（同步 SQLite 路径） |
| `(T, error)` 双返回 | 抛异常；哨兵异常在 `atri_x/errors.py` |
| `encoding/json` | 标准库 `json` |
| `net/http` | `atri_x/net/httpclient.py` |
| Go 运行时指标（goroutine 数、Go 堆） | Python 侧指标（线程数、RSS、解释器内存；`atri_x/sysinfo.py`） |
| `[]rune` 长度 | Python `str` 长度（显示宽度用 `atri_x/utils/runes.py`） |

**例外（必须保留 Go 字样，不要改写）**：

- **Go Skill**：ATRI-X 原生 Skill 的一种，用 Go 写 `SKILL.atriyph` + Go 源码，由 Go 工具链编译成可执行文件。Python 版**保留**该能力，实现在 `atri_x/tool/skill/go_source.py`（调 `gofmt` / `go build`）。因此 `Go Skill`、`skills/go/<name>/`、`gofmt`、`go build`（**在 Skill 编译语境下**）都是正确的。
- **`atri-x` 作为应用目录名/容器名/服务名**：`XDG_APP_DIR_NAME = "atri-x"`（`atri_x/config/app.py`、`atri_x/tool/skill/scanner.py`），所以 `data/config/atri-x/`、`$XDG_DATA_HOME/atri-x`、`/data/config/atri-x/...`、`run/atri-x/atri-x.pid`、`atri-x.*.service`、容器名 `atri-x` 都是真实路径/名字。
- **上游 Go 包名**：`internal/events`、`internal/modelmgr`、`internal/fileops` 等出现在「与上游对照」的语境里时，指的是上游 Go 仓库，保持原样。

---

## 2. 文档中承诺的命令行接口清单（供 Lead 逐条核对）

以下清单**已按 `atri_x/launcher/cli.py` 的 `USAGE_TEXT` 与 `atri_x/__main__.py` 的实际解析逐条核对过**，不是凭文档推测。
实现者：CLI 由 `atri_x/launcher/cli.py` 解析、`atri_x/__main__.py` 执行。

| # | 命令 | 说明 | 文档出处 |
|---|---|---|---|
| 1 | `python -m atri_x` | 自动模式：先试远程 CLI 客户端，本地不可达时回退完整前台 | `README.md`「使用方法」、`docs/getting-started.md` |
| 2 | `python -m atri_x run` | 完整前台：本地 CLI + 已启用平台 + Cron | `README.md`、`docs/getting-started.md` |
| 3 | `python -m atri_x cli [-c name]` | 本地 CLI-only：不启动平台与 Cron | `docs/getting-started.md`、`docs/configuration.md` |
| 4 | `python -m atri_x service run` | Linux/headless 服务模式 | `README.md`、`docs/configuration.md`、`docs/atrinis-usage.md` |
| 5 | `python -m atri_x config check [--config path]` | 配置自检，出错退出非 0 | `README.md`、`deploy/README.md` |
| 6 | `python -m atri_x doctor [--config path] [--e2e] [--json] [--no-model] [--require-platform]` | 部署验收；`--e2e` 走 CLI 真实往返探针 | `README.md`、`docs/configuration.md` |
| 7 | `python -m atri_x completion [auto\|bash\|zsh\|fish\|nushell\|powershell]` | 生成补全脚本 | `docs/getting-started.md` |
| 8 | `python -m atri_x --health-probe[=URL\|host:port\|/path]` | **Python 版新增**（Go 上游没有）；供容器 `HEALTHCHECK` / watchdog 调用 | `CHANGELOG.md`、`deploy/README.md`、`deploy/TESTING.md` |
| 9 | `--config path` / `-c, --client name` / `--help` / `--version` | 全局选项 | 各处 |
| 10 | `python scripts/check_syntax.py` | 语法 + BOM + CRLF + U+FFFD + mojibake 体检 | `AGENTS.md`、`CHANGELOG.md` |
| 11 | `python scripts/check_imports.py` | 逐模块导入校验 + `__all__` 校验 | `AGENTS.md` |
| 12 | `python scripts/check_integration.py` | 跨模块集成一致性 | `AGENTS.md`、`PORTING.md` §3 |
| 13 | `python scripts/check_coverage.py [--summary]` | 按 §4 映射表算加权覆盖率 | `AGENTS.md` |
| 14 | `python -m unittest discover -s tests -t .` | 全量测试（标准库 unittest，不用 pytest） | `docs/README.md`、`AGENTS.md` |
| 15 | `python packaging/build.py [--onefile] [--verify-only]` | **Python 版新增**（Go 上游没有打包环节）；构建 Windows 原生包并做产物级核验 | `packaging/README.md`、`docs/windows-app.md`、`CHANGELOG.md` |
| 16 | `python -m atri_x.platform.qq_onebot.smoke [--url URL] [--group ID] [--listen N]` | **Python 版新增**诊断入口；OneBot 协议层冒烟（冻结后用 `atri-x.exe -m ...` 同语义） | `docs/windows-app.md`、`CHANGELOG.md` |
| 17 | `python -m atri_x.gui [--minimized] [--no-service] [--selftest] [--enable-autostart]` | **Python 版新增**桌面端入口（Tk 前端 + 托盘；冻结后是 `atri-x-gui.exe`，参数同语义） | `docs/windows-app.md`、`packaging/README.md` |
| 18 | `python -m atri_x.dist.install install\|uninstall\|status` | **Python 版新增**安装器（用户级复制、快捷方式、HKCU 卸载项；`--dry-run`/`--yes`/`--purge-data`） | `docs/windows-app.md`、`packaging/README.md` |
| 19 | `python -m atri_x.sidecar list\|status\|dsh-command\|verify [--acp]` | **Python 版新增**内置运行时查看/核验（`runtime/` 与 `ATRIX_RUNTIME_DIR`） | `docs/windows-app.md`、`devdocs/dsh-acp.md` |
| 20 | `python packaging/assemble_kernel.py --modules <node_modules>` | **Python 版新增**组装内置 DSH 内核（原本要求外部装 `dsh`） | `packaging/README.md`、`devdocs/dsh-acp.md` |
| 21 | `python -m atri_x.agent.dsh.status [--json] [--score 文本]` | **Python 版新增**DSH 内核/深度路由状态与打分标定（冻结后 `atri-x.exe -m ...`） | `docs/windows-app.md` §8.1、`devdocs/dsh-acp.md` §6 |
| 22 | `python -m atri_x.agent.dsh.smoke [--command ...] [--json]` | **Python 版新增**DSH ACP 闭环冒烟（优先用内置内核）；命令解析与路由共用 `atri_x/agent/dsh/command.py` | `docs/windows-app.md`、`devdocs/dsh-acp.md` |
| 23 | `python packaging/build_installer.py [--verify] [--iscc PATH]` | **Python 版新增**打 Inno Setup 安装包（用户级、不弹 UAC），`--verify` 静默安装/卸载自检；也可 `build.py --with-installer` | `packaging/README.md` §7、`docs/windows-app.md` §7.1 |
| 24 | `python -m atri_x.maintenance.summary_cli [--build] [--json] [--days N]` | **Python 版新增**查看/立即生成每日会话总结（`[maintenance.daily_summary]`，冻结后 `atri-x.exe -m ...` 同语义） | `docs/reports.md`「每日会话总结」 |

补充说明：

- 第 8 条是 Python 版相对 Go 上游**新增**的开关，不是"上游有、Python 版待核对"，因此不存在等价性问题；它已实现（`atri_x/launcher/cli.py`、`atri_x/__main__.py`）并有用例覆盖（`tests/test_main.py::TestHealthProbeCommand`、`tests/test_main.py::TestHealthProbeUrl`、`tests/test_launcher_cli.py`）。若实现侧决定删减，请同步改本文档与 `deploy/Dockerfile` 的 `HEALTHCHECK`。
- 第 15、16 条同样是 Python 版**新增**：打包（`packaging/`）与 OneBot 协议层冒烟（`atri_x/platform/qq_onebot/smoke.py`）在 Go 上游都不存在，不存在等价性问题。冒烟用例在 `tests/test_qq_onebot_smoke.py`（假 OneBot 协议端，真实 WebSocket）。
- 第 17～24 条也都是 Python 版**新增**（桌面端 `atri_x/gui/`、安装器 `atri_x/dist/`、内置运行时 `atri_x/sidecar/`、内核组装 `packaging/assemble_kernel.py`、DSH 深度路由 `atri_x/agent/dsh/`、Inno 安装包 `packaging/build_installer.py`、每日会话总结 `atri_x/maintenance/summary.py` + `summary_cli.py`）。对应离线用例：`tests/test_gui_protocol.py`、`tests/test_gui_client.py`、`tests/test_gui_service.py`、`tests/test_sidecar_kernel.py`、`tests/test_dist_install.py`、`tests/test_dsh_acp_client.py`、`tests/test_dsh_route.py`、`tests/test_cli_sessions.py`、`tests/test_daily_summary.py`、`tests/test_maintenance_dsh.py`；托盘与 Tk 需要图形环境，用 `python -m atri_x.gui --selftest` 与 `python -m atri_x.gui.tray` 人工核验；安装包用 `python packaging/build_installer.py --verify` 做真实安装/卸载核验。
- 第 3、4、7 条超出了一开始的对接约定（只列了 `run` / `config check` / `doctor` / `--health-probe`），但它们**在 `launcher/cli.py` 里已实现**，因此文档按实现书写；若实现侧决定删减，请同步改文档。
- 文档里出现的 `/*`、`/` 开头的 **slash 命令**（`/*models`、`/*grouppolicy`、`/memory`…）由 Agent 命令层实现，不是 CLI 子命令，不受本表约束。
- 上游的 `atri-x service run` / `atri-x cli` / `atri-x completion` 已分别映射为上表第 4、3、7 条；文档里不再出现裸 `atri-x <子命令>`。

---

## 3. 模块映射现状（与文档里引用的模块路径有关）

`python scripts/check_coverage.py --strict` 的实测口径（Go 生产行数加权）：**72 个包，72 个已覆盖，0 个缺失，加权 96867/96867 = 100.0%。**

本页历史上登记过 1 个缺失模块与 1 个部分覆盖模块，两者现在都已全量落地，登记作废：

| 模块 | 历史登记 | 现状 | 证据 |
|---|---|---|---|
| `atri_x/platform/qqofficial/` | 「尚未实现」（对应 Go `internal/platform/qqofficial`，1,575 行） | **已全量落地**：`__init__` / `adapter` / `attachment` / `client` / `config` / `gateway` / `keyboard` / `message` / `send` / `target` / `token` / `types`，共 **12 个模块**（`check_coverage.py` 计 11 个实现文件，不含 `__init__.py`） | `check_coverage.py --strict` 报 `internal/platform/qqofficial` **OK**；用例 `tests/test_platform_qqofficial.py`（31 例） |
| `atri_x/artist/` | 「部分覆盖」（当时只有 `types.py`、`metadata.py`、`metadata_formats.py`） | **已全量落地**：上游 `internal/artist`（4,291 行）的 6 个关注点逐个有对应物 —— 解析 `parser.py`、串库 `store.py`、留档 `metadata_store.py`、检索 `search.py`、按方言渲染 `render.py`、可见性/写权限 `service.py`；连同 `types.py`、`metadata.py`、`metadata_formats.py`、`__init__.py` 共 **10 个模块**。5 个 `artist_*` 内置工具齐（`artist_extract`、`artist_library_search` / `_read` / `_save` / `_delete`） | `check_coverage.py --strict` 报 `internal/artist` **OK**；用例 `tests/test_artist_metadata.py`（57）、`tests/test_artist_service.py`（48）、`tests/test_builtin_character_artist.py`（41） |

**结论：本页不再登记任何"缺失 / 部分覆盖"模块。** 文档里凡引用这两个路径的地方，都可以当**已实现**读；
它们身上**剩下的**未验证面只有"没连过真实服务端"，见 4.1 与 4.3。

---

## 4. 待核对功能点（标注登记表）

标注形式统一为一行引用块：

```text
> 移植状态：见 devdocs/porting-status.md 待核对
```

含义是「上游 Go 版确实有这条功能，Python 版是否等价实现/是否已验证，需要按代码核对」。

**本次收尾核对后的实测总数**：`README.md` + `docs/*.md` + `devdocs/*.md` 里该字符串共出现 **11 处**
= 本页 2 处（第 7 行的行内 code 说明 + 上面的围栏代码块示例）+ **9 处真实标注**。
`AGENTS.md` 第 79 行那一处是**规则正文**（行内 code），不是标注实例，不计入。
本轮之前是 32 处字符串（= 30 处真实标注 + 本页 2 处示例），其中 **21 处已核销删除**，核销依据见 4.2。

### 4.1 仍保留的 9 处标注（标的都是"真正没验证"的部分）

| # | 文件 | 位置 | 保留的是什么没验证 |
|---|---|---|---|
| 1 | `README.md` | 「与 Go 版的关键差异」表末 | QQ 官方平台与 Telegram **没连过真实服务端**：只有进程内假连接用例，没有用真实 app_id/secret 或真实 Bot Token 做过端到端往返。表里 QQ 官方那一行已从「尚未实现」改为「已实现（12 个模块）但未连真实服务端」。 |
| 2 | `docs/getting-started.md` | 「启动 ATRI-X」 | ① 自动模式 service 探测：已实现，本机两种分支都实测过，**不是**缺口；② **CLI TUI 没在真实 TTY 里逐项体验** —— copy mode、拖拽调宽、`#<文件名>` 补全只有 headless 用例（`tests/test_platform_cli_tui.py`）。 |
| 3 | `docs/concepts.md` | 「平台适配」 | 四个适配器（CLI / QQ OneBot / QQ 官方 / Telegram）**没连真实服务端**做过端到端往返。 |
| 4 | `docs/configuration.md` | 「平台配置」 | `[platform.qqofficial]` / `[platform.qqonebot]` / `[platform.telegram]` 的配置键都有实现与用例，但**没用真实凭据连过服务端**。 |
| 5 | `devdocs/architecture.md` | 「平台适配层」 | QQ 官方的事件 URL → Media Center 导入路径**没在真实服务端验证**。 |
| 6 | `devdocs/p2-native-responses-decision.md` | 文首 | 上游的原生 Responses 内核能力（`previous_response_id` 服务端续链、原生压缩、reasoning 加密载荷存回、`additional_tools` 增量工具定义、`native_dialogue` / `native_material_roots` 迁移）Python 版**有意不做** —— 这是路线 D 下的取舍（该文 §6、`upstream-sync-todo.md` §5 `[x]`），不是"待核对"。 |
| 7 | `devdocs/upstream-sync-todo.md` | 文首 | **Go 时代台账**：逐条结论没有在 Python 版重跑过。 |
| 8 | `devdocs/upstream-changelog-cross-check.md` | 文首 | 同上。 |
| 9 | `devdocs/smoke-test-report.md` | Go 台账段（文首 `> **以下为 Go 时代的工作台账**` 声明之后） | 同上；该段（Go 时代）的验收结论**没有重跑过 Python 版**，不得当作 Python 侧结论引用。文首 `## 0. Python 版实测` 一节是重写后重新跑过的实测记录，**不在标注范围内**（该节由冒烟/稳定性负责人维护，会继续增补）。 |

### 4.2 本轮已核销删除的 21 处（每处都附代码 + 测试证据）

判定标准：**该节描述的内容能在代码里找到对应物，且有 `tests/test_*.py` 用例或验证报告覆盖**。
指不出证据的一律不删 —— 那 9 处见 4.1。

| # | 原标注位置 | 代码对应物 | 测试 / 验证证据 |
|---|---|---|---|
| 1 | `README.md`「使用方法」 | `atri_x/launcher/cli.py`、`atri_x/__main__.py`、`atri_x/app/environment.py`（`resolve_run_mode` / `platform_mode` / `should_start_cron`）、`atri_x/app/service_marker.py` | `tests/test_launcher_cli.py`（18 例）、`tests/test_main.py`（`TestRunApplication` / `TestCompletionCommand` / `TestHealthProbe*`）；`auto` 两种分支本机实测 |
| 2 | `README.md`「独立健康接口」 | `atri_x/health/{checker,server,state}.py`、`atri_x/app/health.py`、`atri_x/app/ops_health.py` | `tests/test_health.py`（59）、`tests/test_app_health.py`（40，含 `/metrics` 与 `/diagnostics` 载荷） |
| 3 | `docs/getting-started.md`「Shell 补全」 | `atri_x/launcher/cli.py` 的 `write_{bash,zsh,fish,nushell,powershell}_completion`、`supported_completion_shell`、`detect_completion_shell` | `tests/test_launcher_cli.py::TestCompletion`（7 个 shell + `auto` + 拒绝 `csh/cmd/sh` + 判定顺序）、`tests/test_main.py::TestCompletionCommand` |
| 4 | `docs/commands.md` 文首 | `atri_x/agent/commands/`（31 个 .py）、`atri_x/command/router.py` | `tests/test_agent_commands.py`（51）、`tests/test_command_router.py`（26）、`tests/test_agent_commands_integration.py`（37）、`tests/test_completion.py` |
| 5 | `docs/hooks.md` 文首 | `atri_x/hook/`（含子包，39 个 .py） | `tests/test_hook_protocol.py`（29）、`tests/test_hook_rules.py`（67）、`tests/test_hook_runtime.py`（51）、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_event.py`、`test_hook_output.py`、`test_hook_builtin_control.py` |
| 6 | `docs/configuration.md`「运维健康接口」 | `atri_x/app/health.py`（`ATRIX_HEALTH_ADDR` / `ATRIX_OPS_TOKEN` 等 5 个开关；未设 token 时不注册 `/healthz`） | `tests/test_health.py`、`tests/test_app_health.py` |
| 7 | `docs/configuration.md`「生图服务」 | `[image_generation].artist_library.root` / `artist_default_preset` / `metadata_store`（`atri_x/config/services.py:228,274-276`、`atri_x/config/loader.py:255-261`、`atri_x/config/app.py:256-258,618-622`、`atri_x/app/runtime.py:411,439-480`） | `tests/test_artist_service.py`（48）、`tests/test_artist_metadata.py`（57）、`tests/test_builtin_character_artist.py`（41） |
| 8 | `docs/atrinis.md` 文首、`docs/atrinis-usage.md` 文首、`devdocs/atrinis-atriwisp.md` 文首（**3 处**） | `atri_x/atrinis/`（13 个模块：auth / background / direct / http / llm / media / outbox / prepare / segments / service / store / targets / types）、`atri_x/atriena/`（5 个模块：calls / delivery / dispatcher / origin / types） | `tests/test_atrinis.py`（49）、`tests/test_atrinis_http.py`（28）、`tests/test_atrinis_llm.py`（23）、`tests/test_atrinis_helpers.py` |
| 9 | `docs/atriyph.md` 文首 | `atri_x/atriyph/`（3 个模块） | `tests/test_atriyph.py`（50） |
| 10 | `docs/character-library.md` 文首 | `atri_x/character/`（5 个模块） | `tests/test_character.py`（32）、`tests/test_builtin_character_artist.py`（41） |
| 11 | `docs/frontend-api.md` 文首 | `atri_x/platform/cli/remote_server.py` + 远程客户端 | `tests/test_platform_cli_remote.py`（14：`TestProtocolWire` 的 hello 帧键 / JSON 往返 / 坏 JSON 容忍 + `TestRemoteServerEndToEnd` 握手、未授权、输入回环） |
| 12 | `docs/image-generation.md`「画师串（artist string）」 | `atri_x/artist/`（10 个模块：`parser` / `store` / `metadata_store` / `search` / `render` / `service` / `types` / `metadata` / `metadata_formats` / `__init__`）+ 5 个 `artist_*` 内置工具 | `tests/test_artist_metadata.py`（57）、`tests/test_artist_service.py`（48）、`tests/test_builtin_character_artist.py`（41） |
| 13 | `docs/reports.md` 文首 | `atri_x/maintenance/report.py` | `tests/test_maintenance_report.py`（off-peak 判定、定价回退、`format_daily_report`） |
| 14 | `devdocs/architecture.md`「健康状态、限速、熔断与运维接口」 | `atri_x/health/`、`atri_x/llm/breaker.py`、`atri_x/app/breaker_llm.py`、`atri_x/ops/ratelimit.py` | `tests/test_health.py`、`tests/test_app_health.py`、`tests/test_llm_breaker.py`、`tests/test_ops.py`、`tests/test_app_platforms.py`（心跳） |
| 15 | `devdocs/code-map.md`「Slash 命令与补全」 | `atri_x/agent/commands/__init__.py` 注册入口存在，注册范围与文档一致 | 同第 4 行 |
| 16 | `devdocs/code-map.md`「Tool Runtime、工具发现与内置工具」 | `atri_x/artist/` 模块已齐（不再"缺模块"）；`image_to_prompt` 的 `include_artist` 三态（`none` / `metadata` / `recommend`，`None` = 未传）见 `atri_x/tool/builtin/image_to_prompt.py:43,158,210,248-257` | `tests/test_builtin_character_artist.py`（41） |
| 17 | `devdocs/code-map.md`「平台适配」 | `atri_x/platform/`（65 个 .py）目录职责与文档一致；QQ 官方不再标"尚未实现" | 各 `tests/test_platform_*.py` |
| 18 | `devdocs/tasks.md`「Milestone 14：QQ 适配」 | 该节条目全部 `[x]`；QQ 官方适配器已落地（`atri_x/platform/qqofficial/` 12 个模块） | `tests/test_platform_qqofficial.py`（31，含网关 `heartbeat_loop` 帧往返） |
| 19 | `devdocs/signals-and-logging.md` 文首 | `atri_x/signal/`、`atri_x/logging/` | `tests/test_signal.py`、`tests/test_logging.py` |

> 共 19 行、21 处标注（第 8 行覆盖 3 处）。
> 顺带更正一处**错误登记**：原 §4.4 曾列「`README.md` 画师串相关段落」，实测 `README.md` 整份文件里搜不到「画师」二字，该行随本节作废。

### 4.3 未验证面总清单（短期内不会变，别把它当成"还没做"）

1. **没连过真实外部服务端**：QQ 开放平台 / OneBot / Telegram 三个适配器都只有进程内假连接用例。
2. **没调过真实模型 API**：全部 LLM 用例走假 client / 假适配器。
3. **TUI 没在真实 TTY 里逐项体验**：`tests/test_platform_cli_tui.py`（97 例）是 headless 的。
4. **三份 Go 时代台账的逐条结论没在 Python 版重核**：`upstream-sync-todo.md`、`upstream-changelog-cross-check.md`、`smoke-test-report.md`（Go 台账段）。
5. **上游原生 Responses 内核能力有意不做**：见 `p2-native-responses-decision.md` §6 与 `upstream-sync-todo.md` §5。

---

## 5. 相对链接约定

- `README.md`（仓库根）：链接写 `docs/xxx.md`、`devdocs/xxx.md`、`PORTING.md`、`deploy/README.md`、`AGENTS.md`、`CHANGELOG.md`。
- `docs/*.md`：同目录直接写 `xxx.md`；跨目录写 `../devdocs/xxx.md`、`../PORTING.md`、`../deploy/*`。
- `devdocs/*.md`：同目录直接写 `xxx.md`；跨目录写 `../docs/xxx.md`、`../PORTING.md`、`../README.md`、`../deploy/*`。
- **源码模块一律用行内代码**（如 `atri_x/agent/core.py`），不做 Markdown 链接：72 个包已全量落地，模块路径按 [PORTING.md](../PORTING.md) §4 的映射表核对即可，链到某个具体文件既容易随重构失效，也不是读者需要的粒度。
- 不复制英文镜像（上游 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物），也不复制 `LICENSE`、`.github/` 等与语言无关的内容。
- 核对方式：文档里的相对链接必须指向本仓库**真实存在**的路径；行内代码里的模块路径按 [PORTING.md](../PORTING.md) §4 映射表核对（例外：部署期才创建的运行目录、`模块.属性` 写法、占位符示例）。本次收尾改写后用 `rg -n` 抽查了 locator 与链接，未引入断链。新增文档后请重新核对。

---

## 6. 已知的文档侧限制

- 文档里引用的 `deploy/data/`、`deploy/.env`、`deploy/rollback/rollback.env` 等路径**在仓库里不存在是正常的**：它们由首次部署生成（见 [deploy/README.md](../deploy/README.md)）。
- 上游 README 里的截图指向 `github.com/Elfreese/elbot-showcase`（上游展示仓库），本次移植保留了链接与图片地址，但**没有把图片复制进本仓库**。
- 上游 `LICENSE` 未复制：本仓库沿用同一许可（Apache-2.0），README「许可」一节已说明。若发布时需要独立的 `LICENSE` 文件，请从上游复制同一份文本。
