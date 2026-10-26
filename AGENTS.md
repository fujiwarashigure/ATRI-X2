# ATRI-X（Python 版）Agent 操作手册

本文件只写 Agent 在本仓库里「怎么做」（How）。架构细节、代码职责和功能说明不要塞回这里：

- 需要理解整体架构：读 [`devdocs/architecture.md`](devdocs/architecture.md)。
- 需要定位代码入口：读 [`devdocs/code-map.md`](devdocs/code-map.md)。
- 需要用户侧功能说明：从 [`docs/README.md`](docs/README.md) 开始。
- 需要维护任务：读 [`devdocs/tasks.md`](devdocs/tasks.md)。
- 需要移植契约（Go 包 → Python 模块）：读 [`PORTING.md`](PORTING.md)，尤其是 §4 映射表。
- 需要知道「哪些文档内容还没被核对」：读 [`devdocs/porting-status.md`](devdocs/porting-status.md)。

## 基本原则

- 默认使用简体中文。
- 当前项目是 **Python 项目**（包名 `atri_x`，目标解释器 Python 3.14，最低 3.11）。
- **纯标准库**：运行时不得 import 任何第三方包；允许的可选加速后端必须 `try/except ImportError` 降级（当前只有 `Pillow` 用于图像编解码）。
- 包布局是**平铺**的：仓库根下直接是 `atri_x/`，不安装、不做 src-layout；运行入口是 `python -m atri_x`。
- 先看用户给出的上下文和相关文档，再查代码；不要凭空猜。
- 只读取和任务相关的文件；不要为了一个小改全项目乱翻。
- 优先复用已有接口和模式，不为一次性需求做抽象。
- 不考虑历史兼容到让代码变复杂，除非用户明确要求。
- 暂不实现但未来可能需要的能力，可以留接口或 TODO，但不要写投机性代码。
- 单文件目标 ≤ 600 行；Go 侧超过 600 行的单文件按职责拆成包内多个模块，并在模块 docstring 里注明「对应 Go 文件」。
- 在架构规划时写 Mermaid，并在其中注明每个文件的职责（示例见 [`devdocs/signals-and-logging.md`](devdocs/signals-and-logging.md)）。
- **不要修改别人的写范围**：`atri_x/**`、`deploy/**`、`tests/**`、`scripts/**`、`PORTING.md`、`pyproject.toml` 各有归属时，先确认再动。

<!-- locator:locate -->
## 快速定位流程

1. 先判断任务类型。
2. 用下面的 locator 搜索文档位置，拿到行号后只读附近章节。
3. 如果只是找文件入口，优先查 [`devdocs/code-map.md`](devdocs/code-map.md)。
4. 如果要理解调用链路，再查 [`devdocs/architecture.md`](devdocs/architecture.md)。
5. 定位到 Python 模块后，优先用语义工具或 `python scripts/list_api.py <module>` 看导出符号；Markdown 和纯文本用 `rg -n`。
   不要用 `python -c "..."` 去探接口（本机控制台是 GBK，容易把排查变成二次损坏）。

示例：

```bash
rg -n "locator:tool" AGENTS.md devdocs/*.md docs/*.md
rg -n "locator:agent-chat" AGENTS.md devdocs/*.md
```

常见任务入口：

| 任务 | 搜索 locator | 优先阅读 |
|---|---|---|
| 启动、装配、配置 | `locator:startup`, `locator:config` | `atri_x/app/app.py`、`atri_x/config/`、[`devdocs/code-map.md`](devdocs/code-map.md) |
| 时间服务 / 时间校验 / 时区 | `locator:time` | `atri_x/time_service.py`、`atri_x/timezones.py`、`atri_x/timesync/`、[`devdocs/code-map.md`](devdocs/code-map.md) |
| Agent 对话流程 | `locator:agent-chat` | `atri_x/agent/core.py`、`atri_x/agent/chat.py`、[`devdocs/architecture.md`](devdocs/architecture.md) |
| Slash 命令 | `locator:commands` | `atri_x/agent/commands/`、`atri_x/command/` |
| Request/Turn 状态 | `locator:request-turn` | `atri_x/request/manager.py`、`atri_x/turn/manager.py` |
| Tool/工具调用 | `locator:tool`, `locator:tool-flow` | `atri_x/tool/`、`atri_x/toolrun/`、[`devdocs/code-map.md`](devdocs/code-map.md) |
| 内容安全（道德限制 / 敏感信息过滤） | `locator:safety` | `atri_x/safety/`、[`docs/safety.md`](docs/safety.md) |
| 识图（两段式 / 隐私开关 / 以图搜图） | `locator:vision` | `atri_x/vision/`、`atri_x/agent/vision.py`、[`docs/vision.md`](docs/vision.md) |
| Skill | `locator:skill` | `atri_x/tool/skill/`、`atri_x/atriyph/` |
| Hook/插件 | `locator:hook` | [`docs/hooks.md`](docs/hooks.md)、`atri_x/hook/` |
| 信号与日志边界 | `locator:signals` | [`devdocs/signals-and-logging.md`](devdocs/signals-and-logging.md)、`atri_x/signal/` |
| 健康状态与运维接口 | `locator:health-ops` | `atri_x/health/`、`atri_x/app/ops_health.py` |
| Output/发送 | `locator:output` | `atri_x/delivery/`、`atri_x/agent/output.py` |
| 平台适配 | `locator:platform` | `atri_x/platform/`、[`devdocs/code-map.md`](devdocs/code-map.md) |
| Session/上下文 | `locator:session`, `locator:context` | `atri_x/session/`、`atri_x/contextmgr/` |
| SQLite/存储 | `locator:storage` | `atri_x/storage/`、`atri_x/storage/sqlite/` |
| Atrinis/Atriena/Atriwisp | `locator:atrinis` | `atri_x/atrinis/`、`atri_x/atriena/`、[`docs/atrinis.md`](docs/atrinis.md) |
| Cron/维护任务 | `locator:cron` | `atri_x/cron/`、`atri_x/maintenance/` |
| Go → Python 移植状态 | `locator:porting` | [`PORTING.md`](PORTING.md)、[`devdocs/porting-status.md`](devdocs/porting-status.md) |
| Windows 打包/分发 | `locator:packaging` | [`packaging/README.md`](packaging/README.md)、[`docs/windows-app.md`](docs/windows-app.md) |
| 桌面端/托盘/安装器 | `locator:packaging` | `atri_x/gui/`、`atri_x/dist/`、`atri_x/sidecar/`、`packaging/build_installer.py`、[`docs/windows-app.md`](docs/windows-app.md) |
| DSH 深度路由 | `locator:packaging` | `atri_x/agent/dsh/`、`[agent.dsh]`、[`devdocs/dsh-acp.md`](devdocs/dsh-acp.md) §6 |
| 文档维护 | `locator:docs` | 本文件、[`docs/README.md`](docs/README.md) |
| 测试验证 | `locator:testing` | 本文件 |

<!-- locator:docs -->
## 文档维护规则

- 用户侧功能、命令、配置、行为变化：更新 `docs/*.md` 和 `CHANGELOG.md`。
- 内部架构、代码地图、维护说明变化：更新 `devdocs/*.md`。
- 新增、删除或明显调整重要 Python 模块职责：更新 [`devdocs/code-map.md`](devdocs/code-map.md)；必要时在本文件的 locator 表里补入口。
- 本仓库**不保留**英文镜像：上游的 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物，不随移植复制；文档里不要新增指向它们的链接。
- `devdocs/` 只给维护者和 Agent 看。
- `AGENTS.md` 只保留操作规则；不要把架构详解、逐文件说明重新写回这里。
- 在已知代码的情况下，发现 code-map 或 architecture 和代码有冲突、或内容太多，以代码为准，同时修改或精简这两个文档。
- 只有 `CHANGELOG.md` 执行**补丁式写作**：描述从旧状态怎样变化到新状态；其他文档只描述现在正确的状态。
- 文档里承诺的命令行接口必须与实现一致；清单见 [`devdocs/porting-status.md`](devdocs/porting-status.md) §2。
- 文档里凡出现「上游 Go 版有、Python 版尚未核对」的功能，加一行 `> 移植状态：见 devdocs/porting-status.md 待核对`，并在该页登记。

<!-- locator:testing -->
## 验证规则

- 改动 Python 代码后，至少跑一次全仓体检：

  ```bash
  python scripts/check_syntax.py        # 语法 + BOM + CRLF + U+FFFD + mojibake
  python scripts/check_imports.py       # 逐个导入 atri_x 下每个模块，校验 __all__
  python scripts/check_integration.py   # 跨模块唯一出处、ctx 键、脱敏契约、ID 契约
  ```

- 优先跑相关模块的用例；影响范围不明确或公共接口变化时，再跑全量：

  ```bash
  python -m unittest discover -s tests -t .          # 全量
  python -m unittest tests.test_cron -v               # 单个模块
  ```

- 覆盖率口径按 [`PORTING.md`](PORTING.md) §4 的映射表算：

  ```bash
  python scripts/check_coverage.py --summary
  ```

- 可选棘轮（改动涉及异常处理 / 取消传播时跑）：

  ```bash
  python scripts/check_except.py   # 逐「文件＋函数＋handler 指纹」比对基线：新增位置或次数变多即失败
  ```

  基线 `scripts/except_baseexception_baseline.json` 是**位置级**的（v2：`file` + `qualname` +
  归一化 handler 指纹 + `count`），所以"总数不变但旧问题被删、新问题被加进来"同样会被抓到；
  位置消失或次数减少会提示收紧基线。确实要调整基线时跑 `python scripts/check_except.py --update`
  并复查它打印的 diff 摘要；`--self-test` 可验证脚本自身五种判定。

- 修复 bug 时，优先用测试或最小复现证明问题，再验证修复。
- 修改文档时，用 `rg -n` 检查关键链接、locator 和禁改说明是否还在；链接必须指向本仓库里真实存在的路径。
- 如果本次只改 Markdown，不需要跑测试，但仍要跑 `python scripts/check_syntax.py`（它同时检查中文有没有被编码毁掉）。
- 看 diff 时发现有和自己改动无关、或和记忆中不对的地方，可能是别人在同时修改，先向用户确认。

<!-- locator:porting -->
## 移植期特别注意

- 上游 Go 仓库是**只读参考**（[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)），不要修改它；本仓库的对应模块才可写。
- `internal/<pkg>/...` ↔ `atri_x/<pkg>/...` 的对应关系**只以 [`PORTING.md`](PORTING.md) §4 为准**，不要凭直觉映射。
- Go 的 `(T, error)` 双返回 → Python 抛异常，哨兵异常统一在 `atri_x/errors.py`。
- Go 的 `context.Context` → ctx 对象 + `atri_x/safecontext.py` 的取消令牌；goroutine/channel → `asyncio` task / `asyncio.Queue`；`sync.Mutex` → `asyncio.Lock`（异步路径）或 `threading.Lock`（同步 SQLite 路径）。
- 时间统一 `datetime`（必须带 `timezone.utc`），落库格式 RFC3339；ID 是**带连字符的小写 UUIDv4**。
- 跨模块共享类型必须 import [`PORTING.md`](PORTING.md) §3 里指定的唯一出处，不要在别的模块复制定义。

## 注意：

- 所有非精确 `rg` 使用（如搜索内容中有多个 `|`、内容简短等），必须加上限制，如 `-m`，或者使用管道符过滤。
- 不要用 PowerShell 写文件（本机是 Windows PowerShell 5.1，会写 UTF-8 BOM 并可能按 GBK 二次编码毁掉中文）；写文件用编辑工具，或在 Python 里以 `encoding="utf-8", newline="\n"` 写。
- 不要用 `tempfile.mkdtemp()` / `TemporaryDirectory()`：本机 `mkdtemp` 以 `0o700` 建目录，清理钩子会抛 `PermissionError`，并留下删不掉的残留目录；临时目录统一用仓库根的 `tmp_<模块>_test/`。
- 不要在仓库根留调试产物；临时文件放 `tests/_tmp_*/` 或 `tmp_*_test/`（已进 `.gitignore`）。
- 控制台码页是 GBK：打印含 `€`(U+20AC) 之类的字符会抛 `UnicodeEncodeError`，工具脚本输出要经 ASCII 兜底包装；也不要从控制台渲染结果判断文件是否损坏，用 `python scripts/check_syntax.py --list` 的程序化计数。
