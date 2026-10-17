# 第三轮对抗性验证总结（复核本轮修复 + 真实路径找漏）

- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（Go，module `github.com/fujiwarashigure/ATRI-X`）
- 本仓（验证对象）：本仓库工作树（Python 3.14.7，纯标准库）
- 基线：`fc9b47c`（本轮所有修复在收尾提交前都只在工作区，用 `git diff fc9b47c` 可见）
- 方法论：**故障注入 + 对照**（先构造「修复前会失败」的形状，证明判据能区分前后）→ **真实进程 + 真实 HTTP**
  （不是只读代码、不是只调模块级函数）→ 符号级与调用点机器化审计
- 材料：本文件；探针脚本 `tmp_verify3_*` / `tmp_r3q*` / `tmp_r3v*` / `tmp_r3s*`（一律未入库、被 `.gitignore` 忽略）；
  验收报告 `deploy/reports/*.json`

## 0. 结论速览

| 项 | 结果 |
|---|---|
| 第一轮复核（R1-1…R1-9，「已宣称修好」的 9 条） | **9/9 PASS**（每条都有故障注入对照；其中 R1-9 的 `bare async` 正是被杀子代理记成 FAIL 的那条，现为 PASS） |
| 第三轮新发现 | 1×P1、3×P2、1×P2/P3、1×P3、1×P2（生产路径丢 4 个工具）、1×文档口径 |
| 上述发现处置 | **全部修复或定性**（见 §3），并全部补了回归用例 |
| 静态审计（调用点 / async 误用 / 死字段） | 目标 7 条路径 42 个文件、3376 个调用点、两套独立实现交叉验证：**实参错配 0、async 当同步 0**；157 个配置字段里 2 个无读点（P3）、65 个 agent `Options` 字段全部有读点 |
| 真实验收（本轮复跑） | 四道门禁全绿 + 全量 `unittest` OK + 进程冒烟（含边界）+ Docker 冒烟 13/13 + 300s 容器稳定性 |
| 仍然未验证的 | 见 §5（真实平台/真实模型/TUI 真实 TTY/Go 时代台账逐条结论） |

> 关于「第三轮验证子代理被杀」：它在被杀前已经跑完第一轮 9 条复核、并抓到本文件 §3 的 P1/P2，
> 但**没有写成报告**。本文件由 Lead 依据它的探针脚本、原始输出与运行日志补写，并对它留下的
> 现场做了**独立复跑**（不是复述它的结论）。
>
> 复跑由 4 个**独立子代理**分头完成、各自留原始输出：真实进程 + HTTP 边界（`tmp_r3v2_probe/`，
> 含单变量故障注入）、9 条复核重跑（`tmp_r3q2_*`，含"修复前形状"控制组）、静态审计与调用点核对
> （`tmp_r3s2_*`）、文档口径核对（`devdocs/porting-status.md` §4 改写）。验证者与实现者分离：
> 这些子代理不改源码，只报告；改动由 Lead 落地后由它们复验。

## 1. 第一轮：9 条「已宣称修好」的复核（全部用运行证据）

判据形式统一为「先构造修复前形状 → 必须失败；当前代码 → 必须通过」。全部探针与日志：

| 条目 | 断言 | 判定 | 探针 / 证据 |
|---|---|---|---|
| R1-1 | `@char:` / `@turn:` 走真实 `Agent.handle_session_input`：角色块进请求、`@c:` 被剥离、notice 上报 | PASS | `tmp_r3q_p1_directives.py` / `tmp_r3q_p1.log`；对照组解绑方法后 `AttributeError: 'Agent' object has no attribute 'apply_character_directives'` 且不发 LLM 请求 |
| R1-2 | `deps.group_policy is agent` 时 `group_*` 三方法不自递归 | PASS | `tmp_r3q_p2_group_toolrt.py` / `tmp_r3q_p2.log`；三个方法都返回而非 `RecursionError` |
| R1-3 | `Options.tool_registry` 真的成为 `tool_runtime.registry`（同一对象），会话工具不再恒空 | PASS | 同上 + `tmp_r3q2_p8.log`：真实注册表下 `tools_for_session=['discover_tool']` |
| R1-4 | `integrations.attach(ctx, req)` / `runtime.build(ctx, req)` 两实参 | PASS | `tmp_r3q_p4_diag.py` / `tmp_r3q_p4.log`；对照组一实参调用两个工厂都 `TypeError: missing a required argument: 'ctx'` |
| R1-5 | `build_agent` 不再永久挂起（群服务 `Task` 不被 `await`） | PASS | `tmp_r3q2_p5_build_agent.log`：`build_agent returned in 230.5ms`、`startup completed in 259.2ms`、`AgentServicesReport installed=9 skipped=0 failed=0 warnings=0`（用隔离的 `XDG_RUNTIME_DIR` 重跑；前人那次"挂起"其实是被同机另一个真实 service 的服务标记挡住，不是产品缺陷） |
| R1-6 | `cron_write` 的 create/update/disable/delete 真落存储，失败路径不假报 `ok:true` | PASS | `tmp_r3q_p6_cron.py`；`disable` 后读回 `enabled=False`、`delete` 后 `NotFoundError`；对照组「不 await 的旧形状」确实出现 `ok:true` 但没落库 |
| R1-7 | `start_stream` / `set_runtime_status` 是**同步**，且经 `start_message_stream` 真拿到 `MessageStream` | PASS | `tmp_r3q_p7_stream.py` / `tmp_r3q_p7b_remote.py`；远程侧帧序 `['status','notice']` 证明 status 帧真的上线 |
| R1-8 | ①依赖齐全的真实装配注册表 == `REGISTERED_TOOLS`（49）；②裸 options 只注册 `ALWAYS_REGISTERED_TOOLS` | ① PASS ② 见 §2 | `tmp_r3q_p8b_full_deps.py`：`count=49`、missing/extra 都是空、14 个条件组逐组命中 |
| R1-9 | `dispatch` 同时支持 Go 风格 `(w, r)` 与函数式 `handler(ctx)`，**裸 async 处理器也能被驱动** | PASS | `tmp_r3q_p9_health.py`：`bare async -> 200 b'{"bare": true}'`（修复前：`200 b''` + `coroutine ... was never awaited`） |

### 1.1 探针自身的两个「假 FAIL」（必须记录，否则会读成产品缺陷）

1. **`tools_for_session` 恒空**：早期探针注册的注册表里没有 `discover_tool`，而 Go 的
   `Registry.SchemasForContext` **只暴露 `discover_tool`**（`internal/tool/tool.go:385-392`，Python
   `atri_x/tool/tool.py:361-368` 逐字一致）——空列表是**正确行为**。真实注册表下
   `tools_for_session=['discover_tool']` 非空。
2. **「full dependency」只喂了 7 个字段**：早期探针拿到 22/30 个工具就断言 `== 49` 失败；把 14 个条件组
   的守卫全部喂满后（`tmp_r3q_p8b_full_deps.py`）正好 49，两者不矛盾。

## 2. R1-8②：`build_tool_runtime` 与 `register_all` 对「裸 options」的答案不同

| 装配路径 | 注册工具数 | 说明 |
|---|---|---|
| `register_all(Registry(), RegisterOptions())`（全空选项） | **13** == `ALWAYS_REGISTERED_TOOLS` | 这条路径 Go 里**不存在**（Go 从 `RegisterOptions` 起就需要依赖） |
| `build_tool_runtime(ToolRuntimeOptions(config_dir=...))`（最小真实装配） | **25** | `build_tool_runtime` 恒造 `resident_memory_store` / `long_memory_dir` / `file_manager` / `skill_manager`，多出 4 resident + 3 long_memory + `send_file` + 4 skill |
| 依赖喂满的真实装配 | **49** == `REGISTERED_TOOLS` | 14 个条件组全触发（`tmp_r3q2_p8b.log`） |
| 真实 `service run`（默认/冒烟配置，N8 修复前 → 后） | 38 → **42** | 差的 4 个就是 N8 里被吞掉的 `resident_memory*` |

Go 对照：`internal/tool/builtin/runtime.go:78` 的 `NewRuntime` 在 `:98/194/195/219` 恒构造上述四组依赖，
所以 Go 的最小真实装配同样是 **25**。结论：**代码对，注释错** —— 已改
`atri_x/tool/builtin/register.py:165-171`。

## 3. 第三轮新发现与处置

| # | 级别 | 现象 | 根因 | Go 出处 | 处置 |
|---|---|---|---|---|---|
| N1 | **P1** | `GET /plugins/memory` / `GET /plugins/learning` **恒回 500** `{"error":"Service.count() takes 3 positional arguments but 4 were given"}` | `atri_x/agent/memory_learning_admin.py` 照 Go 把 `ctx` 传给了**同步存储**服务（本仓签名 `count(platform, scope_id)`） | `internal/app/runner.go:148,163` 调 `Agent.AngelMemoryCount(ctx, ...)`；本仓同步存储层有意不带 ctx | 已修（两个调用点改 2 参）+ `tests/test_app_ops_plugins.py` 回归 |
| N2 | **P2** | `/plugins/*` 的 `platform` / `scope_id` 过滤**静默失效**：端点照回 200，但永远是空作用域统计 | `Server.dispatch` 把查询串丢掉（HTTP 层 `self.path.split("?")[0]`），`r.URL.Query()` 等价物恒空 | `internal/app/runner.go:146-147` `r.URL.Query().Get(...)` | 已修（解析查询串进 `_RequestView.query`）+ 用例（含 `%20`/`%2F` 解码、重复键取首值） |
| N3 | **P2** | 裸 `async` 处理器注册进 `extra_handlers` 时协程被丢弃：Go 风格 `(w, r)` 分支响应体**恒空**（`200` + `b""`），函数式 `handler(ctx)` 分支回 `200` + `null`；进程里只剩 `RuntimeWarning: coroutine ... was never awaited` | `dispatch` 只按同步形状调用，返回值里的协程没人驱动；生产装配被 `LazyHTTPHandler` 包住所以真实启动路径看不出来 | Go 的 `http.Handler` 没有协程概念，端口径是"处理函数必须写完响应" | 已修（新增 `_consume_awaitable()`，两个分支都支持）+ **单变量故障注入反证**：只 monkeypatch `_consume_awaitable = lambda r: None` 就精确复现旧症状 |
| N4 | **P2/P3** | HTTP 语义偏差：`HEAD`/`OPTIONS` 回 **501 + HTML**（Go 回 405 JSON + `Allow: GET`）；`POST /nope` 回 405（Go 回 404）；`POST /healthz` 无 token 回 405（Go 回 401）；`Server` 头泄漏 `Python/3.14.0` | `dispatch` 先判方法后路由；HTTP 层只注册了 `do_GET/POST/PUT/DELETE`；`BaseHTTPRequestHandler.version_string()` 默认带解释器版本 | `internal/health/server.go:69-98`（`ServeMux` 按路径路由 + `requireExtraHandlerToken` 中间件）、`:290-297`（`allowGet`） | 已修（判定顺序改为**路由 → 鉴权 → 方法**；任意动词都进 `dispatch`；`HEAD` 不写实体；`version_string()` 去掉版本号）+ 5 条用例 |
| N5 | **P2** | `/plugins/*` 的 `count: 0` / `pending: 0` / `approved: 0` **整键消失** | `atri_x.app.ops_health.marshal` 对**字典**也套用了 dataclass 的 `omitempty` 规则，而 Go 的 `map[string]any` 没有 omitempty | `internal/app/runner.go:153,168` 用 `map[string]any` + `json.Marshal` | 已修（字典分支不再丢键；dataclass 分支保持原语义）+ `tests/test_app_health.py::test_map_keys_are_never_dropped` |
| N6 | **P3** | `/metrics` 的 `resources.disk_err` 在干净实例上非空（`[WinError 3] 系统找不到指定的路径。`） | `cfg.Sandbox.Root` 默认指向 `<data>/sandbox`，**该目录在首次使用沙箱前不存在**，`disk_usage(root)` 因此失败 | `internal/app/ops_health.go:270-279` 也取 `cfg.Sandbox.Root`；`internal/sysinfo/sysinfo.go:30-35` 在 `DiskUsage` 失败时**无条件**写 `DiskErr`（Go/Linux 上 `statfs` 同样 ENOENT，Go/Windows 上 `disk_other.go` 直接返回"不支持"） | **定性：忠实移植**，不改代码（**单变量实验**：对同一个未重启的进程 `os.makedirs(root)` 后，`/metrics` 立刻给出真实 `disk_total_bytes` / `disk_free_bytes`、`disk_err` 变空 —— 直接原因唯一就是目录不存在。Go 侧同样没有任何一处建 `cfg.Sandbox.Root`（`internal/app/` 唯一 `MkdirAll` 在 `doctor.go:229`，建的是 storage 目录），只在 `tool/builtin/shell.go:204` 懒建。要消除这条噪音只能"启动期建目录"，那会**偏离** Go，故不做） |
| N7 | 文档口径 | 「依赖缺失时只注册 `ALWAYS_REGISTERED_TOOLS`」对全空 `RegisterOptions()` 的 `register_all` 成立（13），对裸 `ToolRuntimeOptions` 走 `build_tool_runtime` 不成立（**25**） | 两条路径的默认层不同：`build_tool_runtime` 自己补齐 `resident_memory_store` / `long_memory_dir` / `file_manager` / `skill_manager` 四组守卫对象 | Go 的真实入口是 `internal/tool/builtin/runtime.go:78` `NewRuntime`（恒填 `:98/194/195/219`）⇒ Go 的最小真实装配同样是 **25**（`internal/app/tool_runtime.go` 在 Go 仓里**不存在**） | 已改 `atri_x/tool/builtin/register.py:165-171` 的注释为准确表述；代码无改动 |
| N8 | **P1**（生产路径静默降级） | 真实启动日志只有一行 `WARN builtin runtime degraded: open resident memory store: '>' not supported between instances of 'float' and 'datetime.timedelta'`；**4 个 `resident_memory*` 工具永远注册不上**（真实启动 38 个，应为 42） | `atri_x/app/runtime.py` 用 `float(...)` 把"秒"直接填进 `NormalWritePolicy.min_interval` / `window`，而它们是 `timedelta` → `normalized()` 抛 TypeError → `_build_resident_store` 吞成 warning | Go `runtime.go:140-145`：`time.Duration(cfg...SecondsValue()) * time.Second` | 已修（新增 `resident_normal_write_policy()` 做秒→`timedelta` 换算）+ `tests/test_app_runtime_resident.py`（6 例，含旧 float 形状必须复现 TypeError 的反证）。端到端复验：真实启动 `warnings=[]`、`resident_memory` / `_read` / `_normal` / `_core` 四个工具全在、注册表 42 |

### 3.1 已知偏差（本轮登记，不修）

- **路径规范化**：Go 的 `ServeMux` 会先 `cleanPath`，`//live` / `/live/` / `/live/../live` 回 `301 + Location`；
  Python 侧没有这一层（实测 `/live/` → 404、`//live` → 200）。两者都不会把请求错误地当成另一个端点处理，
  属**形状差异**，登记不修。
- **404 响应体形状**：Go 的 `ServeMux` 未命中走默认 `NotFoundHandler`，回 `text/plain` 的
  `404 page not found`；Python 侧统一回 JSON `{"error": "not found"}`（与本仓其它端点的 JSON 契约一致，
  也更利于 watchdog 解析）。登记不修。
- **`Server` 头**：Go 的 `net/http` 默认**不写** `Server` 头；Python 的 `BaseHTTPRequestHandler` 必写。
  本轮把值收敛成 `atri-x-health`（去掉解释器版本），无法做到"完全不写"。
- **`disk_err` 的 Windows 分叉**：`internal/sysinfo/disk_other.go` 在非 Linux 上是桩函数，恒回
  "不支持"且完全不碰路径；`atri_x/sysinfo.py` 走 `shutil.disk_usage` 是**有意增强**（模块 docstring 已自述），
  Linux 上两者同义。

## 4. 真实进程验收（本轮复跑）

| 项目 | 结果 | 证据 |
|---|---|---|
| 进程级冒烟（`python -m atri_x service run`） | 启动 ~0.3s；`/live` 200、`/ready` 200（4 项检查 ok）、`/healthz` 无 token 401 / 带 token 200、`/metrics`、`/tasks`、`/diagnostics` 均 200 | 见 §4.1 |
| 工具注册（真实启动） | 冒烟配置 **38 → 42**（N8 修复后 4 个 `resident_memory*` 全部在列）、`warnings=[]` | §3 N8 |
| `/plugins/memory` / `/plugins/learning` | 200 且 `count` / `pending` / `approved` 都在；`platform` / `scope_id` 与请求一致（含外部注入真实数据行的计数实验） | §4.1 |
| HTTP 边界 | `POST /nope` 404、`POST /live` 405+`Allow: GET`、`POST /healthz` 无 token 401、`HEAD /live` 405 且实体 0 字节、`OPTIONS/DELETE/PUT/PATCH/TRACE /live` 405 JSON、`GET /nope` 404、`Server: atri-x-health` | §4.1 |
| 四道门禁 | `check_syntax`（765 文件 0 错）/ `check_imports` 612/612 / `check_integration` 74 项 0 失败 / `check_coverage --strict` 72/72、100% | §4.2 |
| 全量单元测试 | 4923 例 OK（skipped 2） | §4.2 |
| Docker 冒烟 | 13/13 | `deploy/reports/smoke-*.json` |
| 300s 容器稳定性 | 见 §4.3 | `deploy/reports/stability-*.json` |

### 4.1 真实 HTTP 证据（节选）

```
GET /plugins/memory?platform=cli&scope_id=local   -> 200 {"platform":"cli","scope_id":"local","count":0}
GET /plugins/learning?platform=cli&scope_id=local -> 200 {"platform":"cli","scope_id":"local","pending":0,"approved":0}
POST /nope        -> 404 {"error": "not found"}
POST /live        -> 405 Allow: GET {"error": "method not allowed"}
POST /healthz     -> 401（无 token；带 token 时 405 + Allow: GET）
HEAD /live        -> 405 Allow: GET Content-Length: 32，实体 0 字节
OPTIONS /live     -> 405 Allow: GET {"error": "method not allowed"}
Server 头          -> atri-x-health（不含 Python/3.14）
```

### 4.2 门禁与全量测试

```
python scripts/check_syntax.py        → scanned=765  0 错（含 BOM/CRLF/U+FFFD/乱码）
python scripts/check_imports.py       → modules=612 imported=612 import_failures=0 export_failures=0
python scripts/check_integration.py   → 74 项，失败 0 项
python scripts/check_coverage.py --strict --summary → packages=72 covered=72 weighted=100.0%
python -m unittest discover -s tests -t . → Ran 4923 tests ... OK (skipped=2)
```

### 4.3 容器稳定性（含一次启动瞬态误判与验证器修复）

- 最终树安静环境复跑：**0/50 失败（0.00%）**、RSS 增长比 1.017、0 重启、0 Traceback、延迟峰值 27ms
  （`deploy/reports/stability-*.json`）。
- **一次失败运行已定性并修掉验证器缺陷**：`stability-*.json` 的 1/50 失败**全部来自第 1 次
  采样的 `/ready` 503**（同一时刻 `/live` 200）—— harness 的 `healthy` 前置只判断三层探测（最后一层是
  TCP 连通），而冷启动数据卷首次初始化期间 `/ready` 在容器启动后 8~14 秒仍是 503，**启动瞬态被算进了
  稳定性失败率**。`deploy/stability.py` 现在在采样窗口前多等一次 `/ready`（预算复用
  `--startup-timeout`），并由 `tests/test_deploy_harness.py::TestStabilityReadyGate` 三条用例钉住。
- 另一次更早的失败运行（`stability-*.json`，11/41）是**宿主资源争用**：采样窗口与
  全量 4913 例测试套件重叠、容器中途退出（日志尾部 400 行 0 个 `Traceback`、RSS 峰值仅 66MiB）。
  安静环境复跑通过，不改变代码。

### 4.3 静态审计（两套独立实现交叉验证）

| 检查 | 结果 |
|---|---|
| 调用点实参个数 vs 形参形状（`atri_x/app/**`、`health/**`、`agent/core.py`、`agent/chat_llm.py`、`tool/builtin/register.py`、`tool/builtin/cron.py`、`cron/jsonview.py`） | 42 个文件 / **3376 个调用点** / 724 个可机器比对 → **0 问题**（第二套实现不 import 第一套；第一套自带 12 条负控） |
| 「async 函数被当同步语句调用」全仓扫描 | **0 处净问题**（8 类疑似逐个排除并列表存档；`scripts/check_integration.py` 的既有规则在更宽口径下无漏报） |
| 「传了参数但没人读」AST 检查（157 个字段：`ToolRuntimeOptions` 27 / `RegisterOptions` 23 / `ToolRuntime` 15 / agent `Options` 65 / `RuntimeRequest` 4 / `IntegrationRequest` 6 / `RuntimeComponents` 9 / `Dependencies` 8） | 2 个无读点：`RegisterOptions.config_dir`（P3，本轮 diff 恰好删掉最后一处传参）、`ToolRuntimeOptions.logger`（P3，既有，Go `RuntimeOptions` 也没有 Logger）——**保留并登记**，不在收尾提交里做 API 删除；`ToolRuntime.angel_memory_store` / `self_learning_store` 无读点但 **Go 同样不读**，判为一致；agent `Options` **65/65 全部有读点** |
| 覆盖率口径说明 | 第一套实现全仓解析率约 30%（`app/` 24%），所以"0 问题"的准确含义是"**能静态解析的 724 个点 0 问题**"，不是"全仓无错" |

### 4.4 本轮 diff 的结论

**无 P0/P1**（N8 是既有缺陷、被第三轮验证新发现，已修）；另有 3 条 P3 观察：
`status.py` 的 `create_task` 曾只挂局部引用（已改为模块级强引用集合）、
`status.py` docstring 与"CLI 已改回同步"矛盾（已更正）、
`ops_health.format_time` 是秒级 RFC3339 而 `storage.times` 是纳秒级裁剪版（既有实现，不在本轮 diff，登记不修）。

## 5. 仍然未验证的（诚实披露）

1. 没有连过真实平台服务端（QQ 官方 / OneBot / Telegram 全部只有进程内假连接用例）。
2. 没有调过真实模型 API（LLM 全部走假 client / 假 SSE 流）。
3. TUI 没有在真实 TTY 里逐项体验（headless 用例 97 例）。
4. `devdocs/` 里三份 Go 时代台账的逐条结论没有在 Python 版重核（文首已声明）。
5. 上游原生 Responses 内核能力（`previous_response_id` 续链、原生压缩、reasoning 加密载荷、
   `additional_tools`）属**有意不做**，不是"未验证"。
6. 稳定性长跑只覆盖最小配置（零平台、无模型、无持续消息）。
7. `_consume_awaitable` 的"已有运行中事件循环时抛错"这一支只做了代码级推演（与
   `LazyHTTPHandler` 的既有行为一致），**没有真实触发过**。
8. Go 侧**没有编译运行过**（本机无 Go 工具链），所有 Go 结论都来自源码行号级对照；
   `/plugins/*` 的计数过滤沿用前一轮子代理的"向 sqlite 外部注入真实行"实验，本轮未重复注入。

## 6. 复现命令

```powershell
cd <仓库根目录>

# 门禁 + 全量
python scripts/check_syntax.py
python scripts/check_imports.py
python scripts/check_integration.py
python scripts/check_coverage.py --strict --summary
python -m unittest discover -s tests -t .

# 真实进程冒烟（另开终端探测端点）
$env:APPDATA="$PWD\tmp_smoke_appdata"; $env:ATRIX_HEALTH_ADDR="127.0.0.1:33341"; $env:ATRIX_OPS_TOKEN="smoke-token"
python -m atri_x service run

# 关键回归（无需起进程）
python -m unittest tests.test_app_ops_plugins tests.test_health tests.test_app_health -v

# Docker 冒烟与稳定性（需 Docker 引擎；安静环境）
python deploy/smoke.py
python deploy/stability.py --duration 300 --interval 5
```
