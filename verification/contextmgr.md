# contextmgr 集群对抗性验证报告

- 范围：`atri_x/contextmgr/**` ←→ 上游 `internal/contextmgr/**`
- 上游基准（唯一真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)，本机**没有 Go 工具链**（`go version` → CommandNotFoundException），因此所有 Go 侧结论来自逐行读源码；Python 侧结论全部由实际运行探针/测试得出。
- 验证者：对抗性验证 agent（非实现者）。**本报告只读代码，未修改 `atri_x/**`。**
- 复现用探针：`tmp_verify_contextmgr.py`（仓库根，跑完已删）；命令与输出见各条目。

## 覆盖表

| Go 生产文件 | 行数 | Python 对应 | 结论 |
|---|---|---|---|
| `contextmgr/budget.go` | 170 | `contextmgr/budget.py` | 一一对应 |
| `contextmgr/compact_prompt.go` | 64 | `contextmgr/compact_prompt.py` | 一一对应（含两个系统提示词常量） |
| `contextmgr/compressor.go` | 100 | `contextmgr/compressor.py` | 一一对应 |
| `contextmgr/summarize.go` | 145 | `contextmgr/compressor.py`（**无独立文件**） | 已公开的拆分；符号 `SummarizeRequest/SummarizeResult/SummarizeText/splitSummaryChunks/maxSummarizeChunks/maxSummarizeDepth` 全部落在 `compressor.py`，无遗漏 |
| `contextmgr/loader.go` | 124 | `contextmgr/loader.py` | 一一对应 |
| `contextmgr/usage.go` | 29 | `contextmgr/usage.py` | 一一对应 |
| `contextmgr/window.go` | 67 | `contextmgr/window.py` | 一一对应，但见 **CM-1** |
| （无） | — | `contextmgr/__init__.py`（99 行，再导出） | Python 侧额外文件，无 Go 对应物，属包布局需要 |

**没有 Go 文件在 Python 侧整体缺失。** 唯一"文件级不对齐"是 `summarize.go` 合并进 `compressor.py`（模块 docstring 已声明，且逐符号可查）。

## 符号级核对

| Go 导出符号 | Python | 备注 |
|---|---|---|
| `PromptBudget` / `NewPromptBudget` | `PromptBudget` / `new_prompt_budget` | 字段同名 snake_case；见 CM-2 |
| `DefaultMaxPromptRatio`=0.8 / `DefaultSingleMessageMaxRatio`=0.5 / `DefaultReserveOutputTokens`=0 / `DefaultUserOriginalMaxRunes`=4000 | 同名常量 | 逐值核对一致 |
| `MessageTokenOverhead`=4 | `MESSAGE_TOKEN_OVERHEAD`=4 | ✓ |
| `imageTokenEstimate`=1024（未导出） | `IMAGE_TOKEN_ESTIMATE`=1024 | ✓ |
| `EstimateTextTokens/EstimateMessagesTokens/EstimateMessageTokens/EstimateToolsTokens` | `estimate_text_tokens/estimate_messages_tokens/estimate_message_tokens/estimate_tools_tokens` | 见 CM-3 |
| `TruncateTextWithMarker/TruncateTextToTokens` | `truncate_text_with_marker/truncate_text_to_tokens` | 算法逐行一致（含 `maxRunes<=0` 回退 4000、二分前缀） |
| `compactPrompt/assembleSummary`（未导出） | `compact_prompt/assemble_summary` + 驼峰别名 | ✓ |
| `compactSystemPrompt/textSummarizerSystemPrompt` | `COMPACT_SYSTEM_PROMPT/TEXT_SUMMARIZER_SYSTEM_PROMPT` | **逐字节一致**（探针） |
| `CompactRequest/CompactMessage/CompactToolCall/CompactResult/Compactor/CompactionChoice/Compressor` | 同名类/Protocol | `Compactor` 的三个方法签名（`name/compact/summarize_text`）齐备 |
| `SummarizeRequest/SummarizeResult` | 同名 | ✓ |
| `capCompactRequest`（未导出） | `cap_compact_request` | 见 CM-6 |
| `splitSummaryChunks`（未导出） | `split_summary_chunks` | 整数运算 `maxInputTokens*3/4` 与下界 256 一致 |
| `LoadedContext/Loader/Load/LoadRawMessages` | `LoadedContext/Loader/load/load_raw_messages` | 见 CM-7 |
| `maxForkDepth`=32 | `MAX_FORK_DEPTH`=32 | ✓ |
| `UsageState/TokensKnown/UsageRatio/ReachedThreshold/FormatTokens` | `UsageState/tokens_known/usage_ratio/reached_threshold/format_tokens` | `FormatTokens` 文本逐字一致（含全角冒号） |
| `ClientProvider/WindowResolver/Resolve/NewWindowResolver` | `ClientProvider/WindowResolver/resolve/new_window_resolver` | 解析优先级一致；见 CM-1 |
| （Go 无） | `resolve_sync` / `clear_cache` | Python 额外 API，见 CM-1 |

## 发现

### CM-1（P2）`WindowResolver` 的"provider 元数据"分支在实际调用链上不可达，`/context` 与压缩阈值会根据冷/热缓存给出不同窗口

- 严重度：**P2**（边界/文案分歧；真实影响取决于 provider 是否上报了与配置不同的窗口）
- Go 证据：`internal/contextmgr/window.go:21-45` —— `Resolve` 的第 2 优先级是
  `ClientFor(provider).(llm.ModelMetadataProvider).ListModelMetadata(ctx)`，命中即写缓存并返回；
  `internal/agent/context_usage.go:20-29`（`status`，即 `/context`）与 `:142-159`（`reachedCompactThreshold`，即 `shouldCompact` 的判定核心）**都调用这个 `Resolve`**；`internal/agent/context_policy.go:139-145` 同样。
- Python 证据：`atri_x/contextmgr/window.py:77-88` 的 `resolve_sync` **只查缓存/手工配置/默认窗口，完全不看 `list_model_metadata`**；
  而实际调用点走的就是它：
  - `atri_x/agent/context_usage.py:209-225` `should_compact` → `reached_compact_threshold`
  - `atri_x/agent/context_runtime.py:339-359` `reached_compact_threshold` → `:354 window_for_sync(selection)`（失败即用 `:224-230 _window_fallback` 的 `default_context_window`/`DEFAULT_CONTEXT_WINDOW=256000`）
  - `atri_x/agent/context_runtime.py:288-306` `status`（`/context`）→ `:300 window_for_sync`
  - 只有 `window_for`（`:198-206`，异步）、`prompt_budget`（`:232-235`）走 `resolve()`；`contextmgr/window.py:56-68` 的元数据分支仅在 `resolve()` 内。
- 复现：`rg -n "resolve_sync\(|window_for_sync\(|resolver\.Resolve" atri_x internal` →
  Go 命中 3 处 `resolver.Resolve`（全部在判定/展示路径），Python 命中 `resolve_sync`/`window_for_sync` 5 处、`resolver.resolve` 1 处（`prompt_budget`）。
- 影响：`ListModelMetadata` 是窗口解析的**最高优先级**来源。缓存尚未被 `prompt_budget` 预热时（典型场景：进程刚启动就 `/context`，或首轮尚未发出请求就判断是否需要压缩），Python 用配置值/默认 256000，Go 用 provider 上报值 —— 两者显示与阈值计算都会不同；若 provider 上报窗口小于配置默认值，Python 会**压缩得更晚**，可能直接撞 provider 的上下文上限。
  一旦 `prompt_budget` 跑过一次，缓存写入同一 key，两条路径结果一致（`window.py:95-102` 与 `:91-93` 共用 `_cache`），所以是"冷缓存才分叉"。
- 修复建议：让 `reached_compact_threshold` / `status` 走 `window_for` 的异步版本（`context_usage.py` 的 `should_compact` 已被 `AgentSeams` 钉成同步返回 `bool`，可在更上层改成 `await` + `bool()`）；或在进程启动/模型选择变更时预热一次 `resolver.resolve()`。
- 是否建议现在修：**建议**（改动小，但需要 Lead 决定是否接受"同步契约"变动）。

### CM-2（P3）`new_prompt_budget(window<=0)` 的 ratio 语义与 Go 不同

- 严重度：**P3**（当前不可达，且两个字段没有被任何调用方消费）
- Go 证据：`internal/contextmgr/budget.go:34-37` —— `if window <= 0 { return PromptBudget{MaxPromptRatio: maxPromptRatio, SingleMessageMaxRatio: singleMessageMaxRatio} }`，**先返回、后归一化**，因此 window<=0 时返回的是调用方原样传入的值（常见为 0/0）。
- Python 证据：`atri_x/contextmgr/budget.py:58-65` —— 先 `_normalize_ratios(...)`（`budget.py:85-91`，非法值→0.8/0.5）再判 `window <= 0`，于是返回 0.8/0.5。
- 复现命令与输出：
  ```
  > python -c "... new_prompt_budget(0, 0.0, 0.0, 0) ..."
  new_prompt_budget(window=0, ratios=0) -> max_prompt_ratio=0.8 single_message_max_ratio=0.5 (Go returns 0.0/0.0)
  ```
- 影响：无实际影响 —— `max_prompt_ratio` / `single_message_max_ratio` 字段在全仓只有 `new_prompt_budget` 自己读写（`rg -n "max_prompt_ratio" atri_x` 仅命中 `budget.py` 与配置默认值），且窗口为 0 时 `input_limit`/`single_message_limit` 都是 0，两条路径的**可消费字段完全一致**。
- 修复建议：把 `_normalize_ratios` 调用挪到 `window <= 0` 分支之后，或显式注释"有意差异"。
- 是否建议现在修：不建议（纯一致性，无行为后果）。

### CM-3（P3）`estimate_text_tokens` 的 CJK 分类表比 Go 的 `unicode` 表窄，个别字符按 0.65 计

- 严重度：**P3**（只影响 token 估算的保守度）
- Go 证据：`internal/contextmgr/budget.go:73-90` —— 用 `unicode.Is(unicode.Han, r)` / `Is(unicode.Hangul)` / `Is(unicode.Hiragana)` / `Is(unicode.Katakana)` 或 `0xFF00..0xFFEF`，命中记 1.0；`unicode.Han` 包含 CJK 部首补充、康熙部首、`〇`(U+3007)、U+3005、U+3021-3029、U+3038-303B 等；`unicode.Katakana` 含 U+32D0-32FE、U+3300-3357。
- Python 证据：`atri_x/contextmgr/budget.py:129-147` `_is_cjk` 只覆盖 `0x3400-0x4DBF / 0x4E00-0x9FFF / 0xF900-0xFAFF / 0x20000-0x2FA1F` + Hangul + 假名 + 全角块，**不含** U+2E80-2FDF、U+3005/3007/3021-3029/3038-303B、U+32D0-32FE、U+3300-3357。
- 复现命令与输出：
  ```
  > python -c "... estimate_text_tokens(chr(0x2f00)*10) ..."
  U+2F00 x10 py: 7 go=10
  U+3007 x10 py: 7 go=10
  U+32D0 x10 py: 7 go=10
  U+3130 x10 py: 10 go=10      # Hangul 兼容字母：一致
  U+4E00 x10 py: 10 go=10      # 常用汉字：一致
  ```
  （单个字符时 `total<1 → 1` 的钳位会把差异掩盖成 1，必须用多字符输入才能观察到。）
- 影响：含康熙部首/圈号假名/`〇` 的文本会被**低估**约 35%/字，进而让压缩阈值判定偏晚一点点。实际文本里这类字符罕见，量级也小。
- 修复建议：把 `_is_cjk` 的区间补齐到 Go 的几张表（或直接用 `unicodedata` 名称/区块判断）。
- 是否建议现在修：可延后（P3）。

### CM-4（P3）`_summarize_chunk` 的 provider 未配置错误文案少了 `摘要 ` 前缀

- Go 证据：`internal/contextmgr/summarize.go:112-115` → `fmt.Errorf("摘要 provider %q 未配置", req.Provider)`。
- Python 证据：`atri_x/contextmgr/compressor.py:228-230` 委托给 `_collect_stream`，后者 `:263-265` 抛 `f'provider "{provider}" 未配置'`。
- 影响：只在"摘要 provider 配置缺失"这一失败路径上，错误文本与 Go 不同（少前缀），如果日志/测试按文本断言会分叉。`Compressor.Compact` 路径的文案（`compressor is not configured` / `没有可压缩的历史消息` / `压缩模型未配置`）与 Go 逐字一致。
- 修复建议：`_summarize_chunk` 自己先取 client，缺失时抛 `摘要 provider "x" 未配置`。
- 是否建议现在修：可延后。

### CM-5（P3）`cap_compact_request` 会改写空白包裹的工具名

- Go 证据：`internal/contextmgr/compact_prompt.go:19-26` —— 判定用 `strings.TrimSpace(call.Name) == ""`，但**写出的是 `call.Name` 原值**。
- Python 证据：`atri_x/contextmgr/compact_prompt.py:90-96` —— `name = str(...).strip()` 后写出 `name`（已去空白）。
- 影响：仅当工具名带首尾空白（注册表不允许，`Registry.register` 会 strip 后判空、但存的是原值）时提示词文本不同。极边缘。
- 修复建议：写出未 strip 的原值。
- 是否建议现在修：不建议。

### CM-6（P3）`Loader._is_not_found` 把 `LookupError` 也算"没找到"

- Go 证据：`internal/contextmgr/loader.go:88-92, 118-124` —— 只认 `errors.Is(err, storage.ErrNotFound)`。
- Python 证据：`atri_x/contextmgr/loader.py:159-169` —— `exc is ErrNotFound`、`isinstance(exc, ErrNotFound.__class__)`、`isinstance(exc, (LookupError,))` 或 `exc.not_found is True` 都算。
- 影响：如果某个 repository 实现用 `KeyError`/`IndexError` 表达"没有摘要"，Python 会静默把它当成"无摘要"继续走父链回溯，而 Go 会把错误抛给调用方。当前真实实现（`storage/sqlite/context_summary_repository.py:77-79,96-98`）抛的是 `ErrNotFound`，所以是**冗余容错**而非现实缺陷。
- 修复建议：收紧到哨兵/异常类判定。
- 是否建议现在修：不建议。

## 已核对无问题的高风险点

1. **两个系统提示词与 Go 逐字节一致**（模型行为的一部分，改一个字就可能变差）。
   证据：探针提取 Go 反引号常量与 Python 常量做 `==`：
   ```
   COMPACT prompt equal: True
   SUMMARIZER prompt equal: True
   ```
2. **`new_prompt_budget` 正常路径逐值一致**：`new_prompt_budget(100, .8, .5, 0)` →
   `reserve=20 input_limit=80 single=50`，与 Go 的 `window/5`、`min(4096)`、`max(8)`、
   `reserve>=window → window/5`、`int(window*ratio)`、不超过 `window-reserve` 全部对齐。
   （另一条探针 `new_prompt_budget(100000, 0.9, 0.9, 0)` → 0.9/0.9，与 Go「0<r<1 保留原值」一致。）
3. **`EstimateMessageTokens` 的分支与常数**：`MESSAGE_TOKEN_OVERHEAD=4`、图片 1024、文件 `64+`、tool_call `4+name+args`、默认段按文本 —— 逐行一致。
4. **`EstimateToolsTokens` 的 JSON 形态**：Go 的 `ToolSchema`/`ToolFunctionSchema`（`internal/llm/llm.go:52-61`）**没有 omitempty**，四个键恒输出；Python `tool_schema_to_json`（`budget.py:194-203`）同样恒输出 `type/function{name,description,parameters}`，且 `parameters or {}` 避免 `null`。
5. **压缩主流程**：`len(messages)==0`、`provider/model` 空、空摘要、`capCompactRequest` 的截断标记
   `\n...[用户原话过长，压缩时已截断]`、`assembleSummary` 的 `\n\n以下是用户原话：` + 编号列表 —— 与 Go 逐字一致（`compressor.py:140-170` / `compact_prompt.py:103-115`）。
6. **摘要分片与递归**：`maxSummarizeChunks=64`、`maxSummarizeDepth=4`、`chunkRunes=maxInputTokens*3/4`（下界 256）、
   `len(chunks)>1 && est > maxInputTokens*4/5 && depth<4` 才递归、最后 `TruncateTextToTokens` 兜底 —— 逐行一致（含整数除法口径）。
7. **`Loader` 的两条易漏语义**：①本会话有摘要就直接返回"摘要+摘要之后的消息"，**不回溯父会话**（`loader.py:71-73`）；②`list_by_session_after` 命中 `NotFoundError` 时退回"本会话全部消息"而不是报错（`loader.py:136-140`，对应 Go `loader.go` 的注释）。深度上限 32 与 `fork depth exceeds 32` 文本一致。
8. **`UsageState` / `FormatTokens`**：`tokens：unknown（命中：unknown）` 与 `tokens：%d（命中：%d）` 与 Go `usage.go:31-35` 逐字一致（含全角冒号）。
9. **`Compressor` 与调用方的签名**：`Compactor` Protocol 的 `compact(ctx, req)` / `summarize_text(ctx, req)` 与真实调用点
   `atri_x/agent/context_compact.py:342`、`atri_x/agent/context_runtime.py:275,278` 一致（**没有**出现 session 集群那种"协程没被 await"的问题；`context_runtime.py:122` 用 `Compressor(client_for=...)` 注入同步 provider 闭包，与 Go 的 `ClientProvider` 语义相同）。
10. **本集群测试全绿**：`python -m unittest tests.test_contextmgr` 与全量四集群套件
    （`tests.test_contextmgr tests.test_toolrun tests.test_session tests.test_session_lifecycle tests.test_sqlite_* tests.test_storage_models`）
    → `Ran 332 tests ... OK`。

## 修复记录（Lead 指派）

**CM-1（P2）机制已修**：同步路径现在能读到 provider 上报的窗口；冷缓存那一半需要在
**装配期的异步点**调一次 `prewarm`（见最后一节，落在本次写范围之外）。

### Go 的语义（读源码）

- `internal/contextmgr/window.go:21-45` 只有**一个** `Resolve`：
  缓存 → `ListModelMetadata`（现取，命中即 `cacheValue`）→ 手工配置（写缓存）→
  `DefaultWindow`（**不写缓存**）→ `config.DefaultContextWindow`；
  `ClientFor(provider)` 返回 nil 或不是 `ModelMetadataProvider` 时**跳过第 2 步继续往下走**。
- `/context`（`context_usage.go:20-29`）与压缩阈值（`context_usage.go:142-159`）走的都是它。

### Python 改动（`atri_x/contextmgr/window.py`）

- 新增 provider 元数据快照 `_metadata: provider -> {model: window}`：`resolve()` 每成功取到一次
  元数据就把**整份**记下来（不是只记当前那个模型）；`resolve()` 仍然每次缓存未命中都现取
  （Go 也不缓存"没找到该模型"这个否定结果）。
- `resolve_sync()` 在缓存之后、手工配置之前读这份**已解析**快照，取到正值就按 Go 的
  `cacheValue` 写缓存。这就是 CM-1 的核心：同一 provider 的**任意**模型，只要该 provider 的
  元数据在进程内被解析过一次，同步路径与异步路径取值一致。
- **同步路径不再把配置值/默认值写进缓存**。旧实现冷读时会把手工值（或默认值）写进
  `_cache`，之后异步路径命中缓存、**provider 上报值永远读不到** —— 这是比"一次性分歧"更糟的
  "冻死"路径。Go 之所以能缓存配置值，是因为它缓存前已经问过元数据。
- `_client_for()` 吞掉 `client_for(provider)` 的异常：`AgentSeams.client_for_provider`
  对未知 provider 会抛 `RuntimeError`，而 Go 的 `ClientFor` 返回 nil 接口后继续降级 ——
  旧实现会让 `/context` 直接炸。
- 新增 `prewarm(ctx, providers)`：装配期一次性把各 provider 上报的窗口记进快照（上游没有对应方法）。

### 三种情形的取值（`resolve_sync`）

| 情形 | 取值 | 与 Go 的关系 |
|---|---|---|
| **热缓存**（该 `provider/model` 命中缓存） | 缓存值（由异步路径或快照写入） | 与 Go 相同 |
| **冷缓存 · 该 provider 的元数据已解析过**（`resolve()` 或 `prewarm()` 跑过一次，key 还没缓存） | **provider 上报值**，并写缓存 | 与 Go 相同 |
| **冷缓存 · 该 provider 的元数据从未解析过** | 手工配置 → `default_context_window` → `FALLBACK_CONTEXT_WINDOW`(256000)，**不写缓存** | Go 会在这里现取一次元数据（网络调用）；同步路径不能 `await`，因此这是唯一残留差异 |
| **provider 未知 / 客户端抛错 / 不支持元数据** | 手工配置 → 默认值 → 兜底（不抛异常） | 与 Go 相同 |

### 冷缓存那一半需要的一行接线（不在本次写范围）

```python
# 装配期的异步点（app 异步启动 / 回合起点的 await 点 / /context 命令处理里）：
state = ensure_context_runtime(agent)
if state.window_resolver is not None:
    await state.window_resolver.prewarm(ctx, providers)  # providers: 本进程会用到的 provider 名
```

调用点建议放在 `atri_x/agent/chat.py:117`（`should_compact` 之前）或 `/context` 的命令处理里：
`ContextRuntimeState.configure` 与 `install_agent_services` 都是同步函数，无法 `await`。
`prewarm` 内部对每个 provider 只发一次元数据请求，失败即降级（下次 `resolve()` 会再试，与 Go 相同）；
它只写 provider 元数据快照，**不写** `_cache`，因此不会掩盖后续的变化。

### 新增测试

`tests/test_contextmgr_window_metadata.py`（21 例）：Go `window_test.go` 的优先级表逐条对齐
（provider 元数据 > 手工 > 默认 > 内置兜底，默认值不写缓存）、热缓存一致、
同一 provider 其它模型的快照读取、`prewarm`（含只发一次请求 / 失败 provider 降级 / 不缓存配置值）、
冷读不污染缓存、`window<=0` 的元数据被忽略、未知 provider 抛错时不炸、无客户端 / 无 lister / 元数据抛错降级。

