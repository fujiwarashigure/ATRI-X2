# ATRI-X2 → Python 全量重写：移植契约

本文件是**唯一**的移植约定出处。所有实现者必须先读本文件，再读对应 Go 源码。

- 上游（只读参考）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（Go，module `github.com/fujiwarashigure/ATRI-X`）
- 本仓库：Python 重写版（包名 `atri_x`）
- 目标 Python：3.14（最低 3.11；开发与验证在 3.14 上完成）

---

## 1. 硬约束

1. **纯标准库**。运行时不得 import 任何第三方包。允许的可选加速后端必须做 `try/except ImportError` 降级（当前只允许 `Pillow` 用于图像编解码，缺失时走纯 Python 后备）。
2. **每个 Go 生产包都要有 Python 对应模块**，见 §4 映射表。不允许"留桩不实现"（可以简化内部结构，但对外行为必须实现）。
3. **包布局是平铺的**：仓库根下直接是 `atri_x/`，不安装、不做 src-layout。运行方式：
   - `python -m atri_x run`（等价 Go 的 `atri-x run`）
   - `python -m atri_x <args...>`
   - 测试：`python -m unittest discover -s tests -t .`
4. **命名**：Go 的 `PascalCase` 导出名 → Python 同名 `PascalCase`（**类型/类**）或 `snake_case`（**函数/方法**）。字段名统一 `snake_case`。Go 的 `ID` → `id`（字段）/`id`（参数）。不要自作主张改名，接口要能被交叉检索。
5. **错误处理**：Go 的 `(T, error)` → Python 抛异常。异常类型定义在 `atri_x/errors.py`，语义对齐 Go 的 sentinel error（`ErrXxx`）。需要在 Python 里用 `is` 判定哨兵时用模块级单例异常实例。
6. **并发**：Go 的 goroutine + channel → `asyncio` task + `asyncio.Queue`。Go 的 `sync.Mutex` → `asyncio.Lock`（异步路径）或 `threading.Lock`（同步 SQLite 路径）。`context.Context` → `asyncio.CancelledError` + 显式 `CancelToken` 类（见 `atri_x/safecontext.py`）。
7. **时间**：Go 的 `time.Time` → `datetime.datetime`（**必须** `timezone.utc` aware）。落库格式统一 RFC3339 字符串（`2026-10-13T12:00:00Z`），与 Go `time.RFC3339Nano` 兼容。
8. **ID**：与 `internal/storage/id.go` 一致 —— **带连字符的小写 UUIDv4**
   （`str(uuid.uuid4())`，即 `8-4-4-4-12`）。Go 侧是
   `fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", ...)`，输出正是这种形态。
   不要用 `uuid4().hex`（无连字符）。
4. **测试发现**：`tests/` 有 `__init__.py`，所以统一用
   `python -m unittest discover -s tests -t .`（缺 `tests/__init__.py` 会报
   `Start directory is not importable`）。测试文件命名为 `tests/test_<模块>.py`。

---

## 2. 代码风格

- 每个模块顶部写一段 docstring，注明「对应 Go 文件」与职责（对齐 `devdocs/code-map.md` 的粒度）。
- 单文件目标 ≤ 600 行。Go 侧超过 600 行的单文件按职责拆成 Python 包内多个模块，并在 docstring 里注明拆分来源。
- 不加投机性抽象：一次性的需求不要抽基类。与上游 `AGENTS.md` 一致。
- 中文注释/文档是默认语言。

---

## 3. 共享词汇表（core contracts）

以下模块是**跨模块契约**，由 Lead 指定，其他模块只允许消费、不允许改签名：

| 模块 | 对应 Go | 内容 |
|---|---|---|
| `atri_x/errors.py` | 各包 error 定义 | 全部哨兵异常 |
| `atri_x/llm/segment.py` | `internal/llm/segment.go` | `MessageSegment`、`SegmentType`、`text/image/file` 构造器与全部 segment helper |
| `atri_x/llm/llm.py` | `internal/llm/llm.go` | `LLMMessage`、`Role`、`StreamChunk`、`ToolCall`、`Usage`、`ChatRequest`、`LLM` 协议 |
| `atri_x/llm/protocol.py` | `internal/llm/protocol.go` | `ProtocolCapabilities`、`ProtocolCapabilitiesOf` |
| `atri_x/llm/origin.py` | `internal/llm/origin.go` | `Origin`、`can_switch` |
| `atri_x/llm/errors.py` | `internal/llm/errors.go` | `APIError`、`ErrorCategory`、`classify` |
| `atri_x/tool/tool.py` | `internal/tool/tool.go` | `Tool`、`Info`、`Result`、schema 助手 |
| **`atri_x/security/`** | **`internal/security/`** | **`Actor`、`Policy`、`Role`、`GroupRole`、`RiskLevel`、`WithActor`/`ActorFromContext`/`WithPolicy`/`PolicyFromContext`、`ParseRisk`、`CompareRisk`、`ActorID`、`DefaultPolicy`。这是全仓**唯一**出处。** |
| `atri_x/session/` | `internal/session/` | `Scope` |
| `atri_x/delivery/delivery.py` | `internal/delivery/delivery.go` | `Output`、`OutputKind`、`Target`、`Notice`、`Receipt`、`SentMessage`、`ToolPreviewPrefix` |
| `atri_x/platform/platform.py` | `internal/platform/platform.go` | `PlatformAdapter`、平台消息/事件类型、`ConnectedEvent`/`PlatformConnected`；并**再导出** delivery 的 `Receipt`/`SentMessage`/`Target`/`Output` 供两种 import 路径使用 |
| `atri_x/command/types.py` | `internal/command/types.go` | `Info`、`Result`、`Continuation`、`SessionEffect`、`Router` |

> 说明（上游校正）：`Actor` 与风险词汇属 `internal/security`，`Scope` 属
> `internal/session`，`Receipt`/`SentMessage`/`Target` 属 `internal/delivery`。
> 早期版本的本表把它们都写在 `platform.py` 下，与 Go 的落位不符；以 Go 为准。
> Go 树里**没有** `InboundMessage` / `ChatTarget` 这两个类型。
>
> 跨模块使用这些类型时必须 import 上面的**唯一出处**，不要在别的模块里复制定义：
> 复制会让 `isinstance` / `==` 跨模块失效，而各模块的单元测试仍会全绿。
> `python scripts/check_integration.py` 专门抓这一类问题。

**契约冻结规则**：实现者若发现契约不够用，**不要**改已有签名；在本文件 §7「契约变更记录」追加一行并同时改对应模块，然后继续。Lead 在集成阶段统一复核。

### 3.1 并行移植期的跨组契约（`internal/agent` 三组分工）

`atri_x/agent` 由三个并行实现者分文件完成，跨组引用**一律用函数内延迟导入 + 鸭子类型**。
组间接口在开工时冻结如下（集成阶段以本节为准核对）：

- **组合方式**：`Agent` 由若干 mixin 组成，顺序无关 ——
  `ModelMixin`(model.py)、`ModelListMixin`(model_list.py)、`RuntimeStateMixin`(model_state.py)、
  `ModelPolicyMixin`(model_policy.py)、`ModelSnapshotMixin`(model_snapshot.py)、`BudgetMixin`(budget.py)，
  以及随后的 `ContextPolicyMixin` / `ContextRuntimeMixin` / `ContextUsageMixin` / `ContextCompactMixin` /
  `ContextSeedMixin` / `RateLimitMixin` / `ToolDirectiveMixin` / `ToolTagConfigMixin`。
  每个 mixin 自带 `ensure_*` 懒建，宿主字段缺失不致命。
- **宿主状态对象**（core 侧显式初始化）：`model_runtime`、`budget`、`context_runtime`、
  `model_snapshots_store`、`context_overflow_state`、`state_store`、`naming_model`。
- **必须改名**（Go 里未导出字段与导出方法同名）：`modelProfiles`/`modelAliases` →
  `model_profiles_map` / `model_aliases_map`；`modelSnapshots` → `model_snapshots_store`
  （`model_snapshots()` 是方法名，不是字段）。
- **core 必须向上暴露**（供 model 组鸭子调用）：`scope`、`actor`、`is_group_scope`、`is_group_context`、
  `group_runtime_blocked`、`group_policy_for_scope`、`resolve_group_model_selection`、
  `group_model_selection_allowed`、`command_prefix`、`send_chat`、`send_notice`、`audit`、
  `history_enabled`、`load_session`、`mutate_session_metadata`、`model_selection_for_turn`、
  `sessions`、`store`、`pricing`、`budget_limits`、`title_gen`、`logger`；
  其中 `group_*` 一组由 tools 组实现。
- **宿主扩展钩子**：`apply_extra_runtime_sections(state)` / `publish_extra_runtime_sections(state)`
  在 `RuntimeStateMixin` 里是空实现，由 `Agent` 覆写，承载 `group_policy` / `group_knowledge` /
  `group_services` / `group_runtime` 这些不属于 model 组的 state.toml 分区。
- **模型视图类型的唯一出处**是 `atri_x/agent/model*.py`；`atri_x/agent/commands/__init__.py` 只做**再导出**，
  不另立同名字段不同的一份。

---

## 4. Go 包 → Python 模块映射

| Go 包 | Python 模块 | 生产行数（Go） |
|---|---|---|
| `cmd/atri-x` | `atri_x/__main__.py` | 116 |
| `internal/launcher` | `atri_x/launcher/cli.py` | 285 |
| `internal/app` | `atri_x/app/*.py` | 4010 |
| `internal/config` | `atri_x/config/{config,assets,inspect}.py` | 3552 |
| `internal/logging` | `atri_x/logging/{logging,reader,contract}.py` | 612 |
| `internal/redact` | `atri_x/redact/redact.py` | 70 |
| `internal/processenv` | `atri_x/processenv/environment.py` | 273 |
| `internal/storage` | `atri_x/storage/{storage,ids,times}.py` | 505 |
| `internal/storage/sqlite` | `atri_x/storage/sqlite/*.py` | 3097 |
| `internal/llm` | `atri_x/llm/{llm,protocol,errors,origin,segment}.py` | 602 |
| `internal/llm/breaker` | `atri_x/llm/breaker.py` | 118 |
| `internal/llm/openai` | `atri_x/llm/openai/{openai,responses}.py` | 1647 |
| `internal/agent` | `atri_x/agent/*.py` | 15241 |
| `internal/agent/commands` | `atri_x/agent/commands/*.py` | 5411 |
| `internal/command` | `atri_x/command/{types,router,handler}.py` | 325 |
| `internal/completion` | `atri_x/completion/*.py` | 475 |
| `internal/request` | `atri_x/request/manager.py` | 697 |
| `internal/turn` | `atri_x/turn/manager.py` | 574 |
| `internal/runtime` | `atri_x/runtime/status.py` | 152 |
| `internal/signal` | `atri_x/signal/*.py` | 857 |
| `internal/tool` | `atri_x/tool/*.py` | 1773 |
| `internal/tool/builtin` | `atri_x/tool/builtin/*.py` | 11048 |
| `internal/tool/skill` | `atri_x/tool/skill/*.py` | 2555 |
| `internal/tool/runtimeinfo` | `atri_x/tool/runtimeinfo.py` | 54 |
| `internal/toolrun` | `atri_x/toolrun/*.py` | 888 |
| `internal/platform` | `atri_x/platform/{platform,config,media,backoff}.py` | 361 |
| `internal/platform/cli` | `atri_x/platform/cli/*.py` | 3180 |
| `internal/platform/qq-onebot` | `atri_x/platform/qq_onebot/*.py` | 2695 |
| `internal/platform/qqofficial` | `atri_x/platform/qqofficial/*.py` | 1575 |
| `internal/platform/telegram` | `atri_x/platform/telegram/*.py` | 1991 |
| `internal/platform/builtin` | `atri_x/platform/builtin.py` | 87 |
| `internal/platform/headless` | `atri_x/platform/headless.py` | 21 |
| `internal/platform/refcontext` | `atri_x/platform/refcontext.py` | 316 |
| `internal/delivery` | `atri_x/delivery/delivery.py` | 472 |
| `internal/hook` | `atri_x/hook/{event,match,manager,media,process}.py` | 1180 |
| `internal/hook/builtin` | `atri_x/hook/builtin/*.py` | 224 |
| `internal/hook/control` | `atri_x/hook/control.py` | 147 |
| `internal/hook/output` | `atri_x/hook/output/*.py` | 384 |
| `internal/hook/protocol` | `atri_x/hook/protocol/frames.py` | 91 |
| `internal/hook/rules` | `atri_x/hook/rules/*.py` | 2046 |
| `internal/hook/runtime` | `atri_x/hook/runtime/*.py` | 2305 |
| `internal/session` | `atri_x/session/*.py` | 1086 |
| `internal/contextmgr` | `atri_x/contextmgr/*.py` | 699 |
| `internal/cron` | `atri_x/cron/*.py` | 1831 |
| `internal/maintenance` | `atri_x/maintenance/*.py` | 651 |
| `internal/health` | `atri_x/health/*.py` | 807 |
| `internal/media` | `atri_x/media/*.py` | 1885 |
| `internal/memory/resident` | `atri_x/memory/resident/*.py` | 629 |
| `internal/vision` | `atri_x/vision/*.py` | 1122 |
| `internal/imagegen` | `atri_x/imagegen/*.py` | 1578 |
| `internal/artist` | `atri_x/artist/*.py` | 4291 |
| `internal/asr` | `atri_x/asr/*.py` | 707 |
| `internal/character` | `atri_x/character/*.py` | 1162 |
| `internal/angelmemory` | `atri_x/angelmemory/*.py` | 1002 |
| `internal/selflearning` | `atri_x/selflearning/*.py` | 1122 |
| `internal/groupanalysis` | `atri_x/groupanalysis/*.py` | 456 |
| `internal/groupkb` | `atri_x/groupkb/*.py` | 144 |
| `internal/historygate` | `atri_x/historygate/*.py` | 137 |
| `internal/atriena` | `atri_x/atriena/*.py` | 487 |
| `internal/atrinis` | `atri_x/atrinis/*.py` | 1645 |
| `internal/atriyph` | `atri_x/atriyph/*.py` | 648 |
| `internal/background` | `atri_x/background/*.py` | 140 |
| `internal/directive` | `atri_x/directive/*.py` | 140 |
| `internal/ops/concurrency` | `atri_x/ops/concurrency.py` | 120 |
| `internal/ops/diskguard` | `atri_x/ops/diskguard.py` | 89 |
| `internal/ops/ratelimit` | `atri_x/ops/ratelimit.py` | 111 |
| `internal/ratelimit` | `atri_x/ratelimit.py` | 103 |
| `internal/security` | `atri_x/security/*.py` | 187 |
| `internal/safecontext` | `atri_x/safecontext.py` | 70 |
| `internal/textmatch` | `atri_x/textmatch.py` | 208 |
| `internal/sysinfo` | `atri_x/sysinfo.py` | 134 |
| `internal/utils/fileops` | `atri_x/utils/fileops/*.py` | 1464 |

### 4.1 无 Go 对应物、但因为 Python 缺标准库能力而必须新增的模块

| 模块 | 为什么需要 | 对应 Go 能力 |
|---|---|---|
| `atri_x/net/websocket.py` | Python 标准库**没有** WebSocket | `github.com/coder/websocket` |
| `atri_x/net/httpclient.py` | 统一的 urllib 封装：重试、超时、流式读取、multipart、重定向跟随 | `net/http` |
| `atri_x/net/tomlw.py` | `tomllib` **只读**，写 TOML 要自己实现 | `pelletier/go-toml/v2` |
| `atri_x/net/s3.py` | 标准库无 S3 客户端（SigV4 + 最小 REST） | `aws-sdk-go-v2/service/s3` |
| `atri_x/net/cronspec.py` | 标准库无 cron 表达式解析 | `robfig/cron/v3` |
| `atri_x/shellparse/` | **clean-room** 自研 shell 词法/语法分析，支撑 `shell_sandbox`/`shell_risk` 的安全裁决；不得引入 GPLv3 的 bashlex | `mvdan.cc/sh/v3` |
| `atri_x/media/imagecodec/` | 标准库无图像编解码 | `image`、`image/jpeg`、`image/png` |
| `atri_x/platform/cli/tui/` | bubbletea 的 Elm 架构 + ANSI cell 渲染（可用行式渲染简化，但命令/状态栏/输入行为必须实现） | `bubbletea`+`lipgloss`+`bubbles` |
| `atri_x/utils/runes.py` | Go 的 `[]rune` 语义与 `runewidth` | `mattn/go-runewidth` |
| `atri_x/llm/tooltext.py` | 上游网关会把模型原生工具调用以文本协议（DSML / `<tool-call>` JSON）泄漏进正文；Go 侧没有这层防御，需要判定/还原/清洗 | 无（健壮性补丁） |
| `atri_x/agent/chat_anomaly.py` | 把上面的还原结果接回 `LLMCallResult`：能还原就继续工具轮，否则清洗正文并写 WARN 日志与 `llm_tool_calls_in_text` 审计 | `internal/agent/chat.go` 的 `callLLM` 返回处 |

重定向：`atri_x/net/httpclient.py` 的三条请求路径（`request(...)`、`request_multipart(...)`、
`open_stream(...)`）已与 Go 缺省 `http.Client` 对齐——默认跟随最多 10 次
301/302/303/307/308（可用 `follow_redirects=False` / `max_redirects=N` 调整）；
301/302/303 对非 GET/HEAD 改 GET 并丢弃 body 与 `Content-Type`，307/308 保留方法与
body（body 不可重放抛 `HTTPError`）；相对 `Location` 用 `urljoin` 解析，跨源丢弃
Authorization/Cookie/Proxy-Authorization 等敏感头；自指/成环的重定向按"检测到重定向
循环"直接报 `HTTPError`，其余超过次数同样报错；整条跳转链（含重试退避与每跳 I/O）
共享同一份总超时预算，不是每跳各给一次。

---

## 5. 测试约定

- 用标准库 `unittest`（**不要** pytest，环境未安装）。
- 测试放 `tests/`，文件名 `test_<模块>.py`，类 `class TestXxx(unittest.TestCase)`。
- 每个 subagent 至少为自己负责的**顶层可测行为**写测试；纯 I/O 适配器用 fake server/fixture。
- 覆盖率口径：Go 侧 321 个测试文件 / 55,988 行；Python 侧不要求 1:1，但要求「每个模块至少 1 个测试文件覆盖其主要公开函数」。
- 运行：`python -m unittest discover -s tests -t . -v`

---

## 7. 本机环境陷阱（踩过，别再踩）

### 7.1 不要用 PowerShell 写文件

本机 `pwsh` 是 **Windows PowerShell 5.1**：

- `Set-Content -Encoding utf8` 会写 **UTF-8 BOM**；Python 的
  `read_text(encoding="utf-8")` **不剥 BOM**，`compile()`/`import` 直接报
  `invalid non-printable character U+FEFF`，整个模块无法导入。
- 更糟的是它可能**按 GBK 二次编码**：一个 UTF-8 文件经
  `Get-Content -Raw | Set-Content -Encoding utf8` 往返后，中文会变成
  `鍐欏叆` 这类乱码，甚至出现 `U+20AC` 把字符串字面量截断。
  实测毁过 `tests/test_config_state.py`（BOM + 149 处乱码，已修复）。
- **处置**：写文件一律用编辑工具，或在 Python 里以
  `path.write_text(text, encoding="utf-8", newline="\n")` 写。
  传长中文提交信息用 `git commit -F <文件>`，不要让 PowerShell 拼字符串。

### 7.2 不要用 `tempfile.TemporaryDirectory()` / `mkdtemp()`

本机 `mkdtemp` 以 `0o700` 建目录，其清理钩子在本环境下会抛
`PermissionError: [WinError 5]`，残留目录连自己都进不去。
测试统一用 `os.makedirs` 在仓库根下建 `tmp_<模块>_test/`（已进 `.gitignore`），
并在 `tearDown` 里 `shutil.rmtree(..., ignore_errors=True)`。

### 7.3 不要在仓库根部留调试产物

并行 agent 曾在仓库根留下 `.fileops-test-*/`、`.probe*/`、`.*_out.txt` 等
（且因 7.2 的 ACL 问题删不掉）。`.gitignore` 已覆盖这些模式；
需要临时文件请放进 `tests/_tmp_*/` 或仓库根的 `tmp_*_test/`。

### 7.4 报告编码损坏时注意控制台

控制台码页是 GBK，`print` 一个含 `€`(U+20AC) 的字符串会抛
`UnicodeEncodeError` —— 把"报告一次编码损坏"变成"报告器自己崩掉"。
工具脚本里的输出统一经一个 `_safe()` 包装（当前编码不可表示时退到
ASCII 转义）。另外**不要从控制台渲染结果判断文件是否损坏**（中英文都可能
显示成乱码），要用程序化计数：
`python scripts/check_syntax.py --list` 会报出每个文件的 U+FFFD 与
mojibake 序列命中数。

### 7.5 mojibake 特征不要写死在源码里

写死"乱码字面量"当检测特征会自我破坏：这些字面量本身会被其他编码修复工具
当成乱码"修好"，于是检测器退化成把正常的 `写入` / `文件` 当特征，
一次性误报 160+ 个健康文件（本仓真实发生过）。
正确做法是**运行时推导**：把正常中文词做 `encode("utf-8").decode("gbk")`
得到特征序列。见 `scripts/check_syntax.py`。

---

## 8. 验收工具

| 脚本 | 用途 |
|---|---|
| `python scripts/check_coverage.py --summary` | 按 PORTING.md §4 映射表算**加权覆盖率**（Go 生产行数口径）与缺失清单 |
| `python scripts/check_syntax.py` | 全仓语法 + BOM + CRLF + U+FFFD + mojibake 体检；`--fix-eol` / `--fix-bom` 就地修复 |
| `python scripts/check_imports.py --list` | **逐个导入** `atri_x` 下每个模块，并校验每个 `__all__` 里的名字真的存在 |
| `python scripts/check_integration.py` | 跨模块集成一致性：唯一出处、ctx 键、脱敏契约、ID 契约 |
| `python scripts/extract_assets.py --check` | 内置配置资产与上游 `assets.go` 的漂移比对 |
| `python scripts/dump_hang.py <目标>` | 挂起的 unittest 目标：10 秒后转储全部线程栈 |
| `python -m unittest discover -s tests -t .` | 全量测试 |

`docs/`、`devdocs/`、`README.md`、`CHANGELOG.md` 从上游复制后**改写为 Python 版本**：一切 Go 专属表述（`go build`、`gofmt`、`.go` 路径、`go test`）替换为 Python 对应物；架构链接改为 Python 模块路径。`docs.en/`、`README.en` 等英文镜像不复制（上游也标注为自动产物）。
