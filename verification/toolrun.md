# toolrun 集群对抗性验证报告

- 范围：`atri_x/toolrun/**` ←→ 上游 `internal/toolrun/**`
- 上游基准：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本机无 Go 工具链，Go 侧结论均来自读源码）；Python 侧结论均来自实跑探针/测试。
- 复现探针：`tmp_verify_toolrun_denied.py`、`tmp_verify_symbols.py`、`tmp_verify_sql_diff.py`（仓库根，跑完已删）。

## 覆盖表

| Go 生产文件 | 行数 | Python 对应 | 结论 |
|---|---|---|---|
| `toolrun/toolrun.go` | 304/332 | `toolrun/toolrun.py` | 一一对应（含 `Context/ResolvedTool/ExecutionResult/Manager`） |
| `toolrun/runner.go` | 322/338 | `toolrun/runner.py` | 一一对应（`RunnerDeps/RunRequest/RunResult/ConfirmResult` + `Run`） |
| `toolrun/native.go` | 47 | `toolrun/native.py` | 一一对应 |
| `toolrun/cache.go` | 67 | `toolrun/cache.py` | 一一对应 |
| `toolrun/atriwisp.go` | 53 | `toolrun/atriwisp.py` | 一一对应 |
| `toolrun/atriwisp_exec.go` | 81 | `toolrun/atriwisp_exec.py` | 一一对应（HTTP 用 `http.client` 复刻） |
| `toolrun/availability.go` | 14 | `toolrun/availability.py` | 一一对应（另加两条原因常量） |
| （无） | — | `toolrun/__init__.py` | Python 侧额外再导出文件 |

**文件级无缺失。** 符号级核对（自动比对 Go `func` 名 → snake_case 与 Python `def` 名，探针 `tmp_verify_symbols.py`）后，真实缺口只有一处：

- `NewManager`（`internal/toolrun/toolrun.go:68-70`）在 Python 侧**没有对应的 `new_manager` 符号**（只有 `Manager.__init__(registry, policy, media=None)`）。
  全仓 `rg -n "new_manager"` 命中的都是其它包（delivery/hook/skill/media/request）的同名工厂，**没有人调用 `toolrun.new_manager`**，因此只是"名字不可交叉检索"的 P3（见 TR-4）。

其余 diff 均为：① Python 把 async 方法写在同一缩进层级被我粗糙的正则漏掉（`resolve/execute/tool_infos/...` 实际都在）；② Go 未导出 helper → Python `_xxx` 私有函数（`executionError→_execution_error`、`messagePosition→_message_position` 等）；③ 已公开的拆分（`sortCachedTools` 落在 `cache.py`，`toolrun.py` 提供 `SortCachedTools` 别名）。

## 符号级核对（要点）

| Go 导出符号 | Python | 备注 |
|---|---|---|
| `SourceKind/SourceKindNative/SourceKindAtriwisp` | `SOURCE_KIND_NATIVE/ATRIWISP` + 别名 | ✓ |
| `CachedTool`（11 字段 + json tag） | `CachedTool`（同字段 snake_case） | JSON 键名逐字一致；键**顺序**不同，见 TR-3 |
| `Cache/DecodeCache/EncodeCache/NormalizeCachedTools/MergeCachedTools/SortCachedTools` | 同名 snake_case + 驼峰别名 | ✓ |
| `Manager.ToolInfos/ToolNames/BaseSchemas/Schemas/Resolve/AssessRisk/PreflightConfirmation/RiskDetail/Execute` | `tool_infos/tool_names/base_schemas/schemas/resolve/assess_risk/preflight_confirmation/risk_detail/execute`（+`Resolve/Execute` 别名） | ✓ |
| `Manager.Run` | `runner.run`（模块级函数） | Go 是方法，Python 是函数；调用点统一 `run(manager, ctx, deps, req)`，签名等价 |
| `RunnerDeps`（12 个方法） | `RunnerDeps` Protocol（12 个方法） | 逐方法对应；Python 侧全 async（Go 同步），见下 |
| `RunRequest.AllowedTools/AuthorizeTool/BeforeExecute` | `allowed_tools/authorize_tool/before_execute` | ✓ |
| `BackgroundToolAllowed` | `background_tool_allowed` + 别名 | 白名单 `discover_tool/workspace` 一致 |
| `NativeCachedToolsFromDiscovery` | `native_cached_tools_from_discovery` + 别名 | ✓ |
| `AtriwispToolDeclaration/AtriwispInjection` | 同名 | ✓ |
| `CachedToolsFromAtriwisp` | `cached_tools_from_atriwisp` + 别名 | canonical 名替换正则 `[^a-zA-Z0-9_-]+` 一致 |
| `FirstAtriwispResult` | `first_atriwisp_result` + 别名 | ✓ |

## 发现

### TR-1（P1）确认阶段被拒绝的工具调用，被记成"成功"（`call_err=None` → `success=1`）

- 严重度：**P1**（落库的审计字段错误 + 影响"成功过的工具调用"判定）
- Go 证据：
  - `internal/toolrun/runner.go:140` `confirm, err := m.confirm(...)`；
    `:141-155` 拒绝分支里 `deps.RecordToolCall(ctx, sessionID, call, riskText, startedAt, messageText, err)` —— **`err` 非 nil**；
  - `internal/toolrun/runner.go:251-283` `confirm` 的每个拒绝分支都 `return ..., fmt.Errorf(...)`：
    `resolved.Reason`（`:255`）、`"native tool unavailable"`（`:263`）、`"tool requires superadmin"`（`:269`）、`"tool risk above allowed level"`（`:274`）。
- Python 证据：
  - `atri_x/toolrun/runner.py:283-306`：`error` 只在 `_confirm` **抛异常**时才被赋值（`:284-288`），而 `_confirm`（`:458-519`）在四个拒绝分支上都是 `return ConfirmResult(allowed=False, message=message)`，**从不抛异常**；
  - 于是 `:304-306` 传给 `record_tool_call(..., call_err=None)`；
  - 下游 `atri_x/agent/chat_tools.py:305` `success=call_err is None`、`:310-311` `if call_err is not None: record.error = str(call_err)`
    → 被拒绝的调用写入 `tool_call_records.success = 1`、`error = NULL`。
- 复现命令与输出（真实 `Manager` + 假 deps，工具 `superadmin_only=True`、actor 为普通用户）：
  ```
  > python tmp_verify_toolrun_denied.py
  denials: [{'risk': 'low', 'reason': 'tool_requires_superadmin'}]
  records: 1
    risk='low' call_err=None
    success_would_be=True
  result.messages count: 1
  message text: 'tool call danger denied: requires superadmin role; ask an administrator to add you to security.superadmins'
  ```
  即：拒绝理由进了模型可见的消息，但**审计行被标成成功**。
- 影响：
  1. `tool_call_records` 的成功率/审计数据对"超管门槛""风险门槛"两类拒绝全部记反；
  2. `SuccessfulIDs`（`storage/sqlite/tool_call_repository.py:65-89`）被
     `atri_x/agent/context_compact.py:230` 用于"只保留成功过的工具调用" —— 被安全策略拒绝的调用会被当成成功调用进入压缩后的上下文/新会话种子，模型会以为工具真的执行过；
  3. 与 Go 的**差异只出现在 confirm 拒绝这一条路径**：其余拒绝/失败路径 Python 传的都是真实异常（allowlist `ValueError` `:237`、authorize `ValueError` `:252`/`:341`、preflight `exc` `:270`、start_tool_request `start_err` `:324`、before_execute `before` `:360`、媒体参数 `exc` `:382`、执行 `result.err`、hook `hook_err`），与 Go 的 `fmt.Errorf` 一一对应。
- 修复建议：让 `_confirm` 像 Go 一样把"拒绝原因"作为错误一起返回（例如返回 `(ConfirmResult, BaseException|None)`，或在四个拒绝分支构造 `ValueError(...)` 由 `run` 取出），再把该对象传给 `record_tool_call`。
- 是否建议现在修：**强烈建议现在修**（改动局限在 `runner.py::_confirm` 与一处调用，影响审计与压缩上下文正确性）。

### TR-2（P2）Atriwisp 工具的参数**不是合法 JSON** 时，Go 直接失败，Python 会把错误参数当字符串发给远端

- 严重度：**P2**（错误路径分歧：本地报错 vs 发出一个语义不同的请求）
- Go 证据：`internal/toolrun/atriwisp_exec.go:43-50` —— `arguments` 是 `json.RawMessage`，
  `json.Marshal(atriwispToolRequest{... Arguments: arguments})` 对非法 JSON 会**返回 error**，于是 `executionError` 直接结束这次调用（不发出 HTTP 请求）。
- Python 证据：`atri_x/toolrun/atriwisp_exec.py:49-57` + `:129-134` —— `_as_raw()` 在 `json.loads` 失败时**回退为原始字符串**，payload 变成 `{"tool": ..., "arguments": "not json"}`，请求照发。
- 附带差异（同一函数）：Go 用 RawMessage **原样**嵌入模型给的 JSON 文本；Python 走 `json.loads`→`json.dumps(..., separators)`，数字会被重写（`1e2`→`100.0`、`1.50`→`1.5`）、字符串里的 `\uXXXX` 转义会被解码。远端若对参数做严格比对/展示，会看到不同的字节。
- 复现（只在 Python 侧可跑）：读 `atriwisp_exec.py:49-57,129-134`；Go 侧由 `atriwisp_exec.go:47-50` 的 `if err != nil { return executionError(call, message, err) }` 直接判定。
- 影响：模型偶尔产出畸形工具参数时，Go 得到"本地报错"，Python 得到"远端 HTTP 400/异常语义"，错误文本与失败位置都不同；hub 侧可能把字符串参数当成合法输入处理。
- 修复建议：`_as_raw` 失败时不要回退为字符串，改为返回错误（或在发送前 `json.loads` 校验并返回 `_execution_error`）。
- 是否建议现在修：建议（低成本，收敛错误语义）。

### TR-3（P3）`CachedTool.to_json()` 的键顺序与 Go 的 struct 字段顺序不同

- Go 证据：`internal/toolrun/toolrun.go:39-50` —— 字段顺序 `name, canonical_name, source, description, schema, atriwisp_name, event_key, endpoint, timeout_seconds, foreground_only`；有 `omitempty` 的字段语义与 Python 一致（`Source`/`Name`/`Schema` 恒输出）。
- Python 证据：`atri_x/toolrun/cache.py:46-63` —— dict 插入顺序为 `name, source, schema, canonical_name, description, ...`。
- 影响：JSON 是对象，**读取方不受影响**（Go/Python 都按 key 取值），只有做整串字节比对/哈希的场合才会分叉。这份缓存会落进 Session metadata，因此记录在案。
- 修复建议：按 Go 的字段顺序构造 dict，成本为零。
- 是否建议现在修：可延后。

### TR-4（P3）`new_manager` 符号缺失

- Go：`internal/toolrun/toolrun.go:68-70` `func NewManager(registry *tool.Registry, policy *security.Policy) *Manager`。
- Python：只有 `Manager(registry, policy, media=None)`，无 `new_manager`/`NewManager` 符号（`atri_x/toolrun/__init__.py` 也只导出 `Manager`）。
- 影响：交叉检索/逐符号对照会缺一个名字；无调用方，无行为影响。
- 建议：加一个 `new_manager = Manager` 别名或工厂函数。不建议现在修。

### TR-5（P3）`SortCachedTools` 的稳定性与 Go 不同（无实际后果）

- Go：`internal/toolrun/toolrun.go:289-303` 用 `sort.Slice`（**不稳定**）。
- Python：`atri_x/toolrun/cache.py:176-178` 用 `sorted`（稳定）；
- 结论：`NormalizeCachedTools` 先按同一 key 去重，排序键（`canonical_name or name`）在结果里**唯一**，因此不存在平局，两种排序结果必然相同。**不构成缺陷**，仅记录。

## 已核对无问题的高风险点

1. **背景任务工具白名单（安全边界）**：`discover_tool` / `workspace` 在 `schemas()`（`toolrun.py:153-158`）、
   `resolve()` 的缓存分支（`:171-178`）与非缓存分支（`:220-231`）三处都被挡，与
   Go `BackgroundToolAllowed` + `Resolve` 的 `background` 判定逐行一致；`schemaForContext` 追加
   `BackgroundPathInstruction()` 的四个工具名（`shell/read_file/edit_file/send_file`）一致。
2. **`Schemas` 的去重顺序**：先基础 schema 再缓存工具，`name=="" || seen[name]` 跳过，与 `toolrun.go:104-132` 一致（Python 未预分配容量，无行为差异）。
3. **`Resolve` 的分支顺序与返回值**：空名 → 缓存的 background 拒绝 → 缓存不可用 → 缺失 endpoint → Atriwisp（返回 **canonical_name**）→ 原生命中 → "native tool is no longer available" → 非缓存原生（含 background 拒绝与 `unavailableReason`）→ `tool "x" not found`，逐分支一致（`toolrun.py:162-244` ↔ `toolrun.go:169-217`）。
4. **`Run` 的整批编排顺序**：chat 模式早退 → 逐个调用 prepare→群白名单→authorize→prepareToolContext→assess→AddToolUse→preflight→（批量预览只发一次 / 单条预览）→confirm（含 stopped 早退、`confirmationExtra` 拼接）→StartToolRequest→**再次 authorize**→before_execute→媒体参数保留→Execute→ctx.Err 判定（外层 ctx 取消则 `Stopped`）→SendOutputs→CompleteToolCall→RecordToolCall→discover_tool 记忆→失败预览，最后 `transcript` 头部插入 ToolCallMessage —— 与 `runner.go:82-237` 顺序逐条对齐（含 `preparedCalls` 在 prepare 失败时回填 `original`）。
5. **预览文本**：`previewArguments` 的 160 rune 截断 + `...`、空参数 `{}`、`joinAssistantText` 的 `"\n\n"` 拼接、批量预览 `"\n".join` —— 一致（`runner.py:541-598` ↔ `runner.go:239-337`）。
6. **`toolAllowed`**：`None` 表示不限制；`lower().strip()` 后查表，未命中即拒绝（Python `bool(...)`）—— 与 Go map 查询一致。
7. **Atriwisp HTTP 细节**：默认 10s 超时、响应体 1 MiB 上限（`ATRIWISP_MAX_RESPONSE_BYTES = 1<<20`）、非 2xx 报 `Atriwisp tool HTTP %d: %s`、按 `content→text→result→原始 body` 取第一个非空、`error` 字段非空即失败、`Result.llm_segments()` 作为消息段 —— 与 `atriwisp_exec.go:30-88` 一致（`first_atriwisp_result` 的 strip 语义也一致）。
8. **`_as_raw` 之外的参数路径**：`arguments.strip() or "{}"` 与 Go 的 `strings.TrimSpace` + `len==0 → {}` 一致；`event_key` 仅在非空时输出（对应 `omitempty`）。
9. **`NativeSource`**：`discover_tool` 恒不出现在 `tool_infos`、`hidden`/上下文不可用/无权限三重过滤、`base_schemas` 用 `available_in_context` 过滤 —— 与 `native.go:19-55` 一致。
10. **本集群测试全绿**：`python -m unittest tests.test_toolrun`（含在四集群 332 个用例里）→ `OK`。

## 修复记录（Lead 指派）

**TR-2（P2）已修**，并把同一函数的"附带差异"（RawMessage 被重新序列化）一并收敛。

### Go 的确切行为（读源码）

- `internal/toolrun/atriwisp_exec.go:43-50`：`arguments` 是 `json.RawMessage`，
  `json.Marshal(atriwispToolRequest{...})` 的返回值**先于建请求**被检查，
  `err != nil` 直接 `return executionError(call, message, err)` —— **不发 HTTP 请求**。
- 那个 `err` 是 `*json.MarshalerError`（不是 scanner 错误直接冒出来）：
  `encoding/json/encode.go` 的 `marshalerEncoder` 在 `m.MarshalJSON()` 之后调
  `appendCompact(out, b, opts.escapeHTML)`，失败即 `e.error(&MarshalerError{v.Type(), err, "MarshalJSON"})`；
  `MarshalerError.Error()` 是
  `"json: error calling " + srcFunc + " for type " + e.Type.String() + ": " + e.Err.Error()`。
  对 `json.RawMessage` 字段得到的就是
  `json: error calling MarshalJSON for type json.RawMessage: <语法错误>`。
- 合法 JSON 时 `appendCompact`（`escapeHTML=true`）**原样嵌入字节**：只去掉字符串字面量
  之外的空白、把 `<` / `>` / `&` / U+2028 / U+2029 转义，**不重排数字、不解码 `\uXXXX`**。
  字段顺序是结构体声明顺序 `tool` → `event_key`（`omitempty`）→ `arguments`。

### Python 改动

- `atri_x/toolrun/atriwisp_exec.py`：
  - 新增 `_raw_arguments_error()`：先用 `json.loads(..., parse_constant=...)` 校验
    （Go 的 scanner 不接受 `NaN` / `Infinity`，Python 的 `json` 默认接受，用 `parse_constant` 拒掉），
    非法即返回 `ValueError("json: error calling MarshalJSON for type json.RawMessage: " + <解码器文案>)`
    → `_execution_error()` 本地结束，**不发请求**；校验位置在 endpoint 判空之前，
    与 Go「先 Marshal 再建请求」的顺序一致。
  - 删掉 `_as_raw()`（那条"解析失败回退成原始字符串"正是缺陷本体）。
  - 新增 `_encode_tool_request()` + `_compact_raw_arguments()`：等价复刻 `appendCompact`
    的字节形态（压空白 + `escapeHTML` 转义），`arguments` 作为原文嵌入，字段顺序对齐 Go。
- 内层语法细节文案取自 Python 解码器（Go 是 scanner 的 `SyntaxError` 文本），
  外层前缀与 Go 逐字一致 —— 这是本机没有 Go 工具链时能做到的最大对齐度。

### 新增测试

`tests/test_toolrun_atriwisp_args.py`（16 例，真起 `ThreadingHTTPServer` 并断言**原始请求体字节**）：
非法 JSON 本地失败且请求数为 0、错误文本前缀、`NaN`/`Infinity` 被拒、非法参数优先于 endpoint 判空、
`1.50`/`1e2`/`\u0041`/大整数/`1e400` 的字节保真（旧实现会把 `1e400` 写成非法的 `Infinity`）、
字符串外空白压缩、`<&>` 转义、U+2028 转义、转义引号不结束字符串、字段顺序、`{}` 兜底、标量参数原样嵌入。

### 仍存在的差异（有意保留）

- 键名 / `tool` / `event_key` 三个字符串不做 HTML 转义（Go 会写 `\u003c` 等）：
  两者是等价 JSON 文本，且工具名受 `[^a-zA-Z0-9_-]+` 规范化约束。

