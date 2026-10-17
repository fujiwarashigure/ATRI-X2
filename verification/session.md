# session 集群对抗性验证报告

- 范围：`atri_x/session/**` ←→ 上游 `internal/session/**`
- 上游基准：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本机无 Go 工具链，Go 侧结论均来自读源码）；Python 侧结论均来自实跑探针/测试。
- 复现探针：`tmp_verify_session_naming.py`（仓库根，跑完已删）、`tmp_verify_symbols.py`。

## 覆盖表

| Go 生产文件 | 行数 | Python 对应 | 结论 |
|---|---|---|---|
| `session/types.go` | 97 | `session/types.py` | 一一对应（`Scope/CreateRequest/ActivateMode*/Status/NamingConfig/Config/TitleResult/TitleGenerator/三个 Naming 事件/NamingNotifier`） |
| `session/service.go` | 229 | `session/service.py`（`Service` + 三个构造）+ `session/text.py` | `forkTitle/defaultTitle/preview` 被拆到 `text.py`（`service.py:36` 再导出），属已声明拆分 |
| `session/lifecycle.go` | 172 | `session/lifecycle.py` | 一一对应 |
| `session/mode.go` | 56 | `session/mode.py` | 一一对应 |
| `session/naming.go` | 263 | `session/naming.py` | 一一对应，但 **SE-1（P0）** |
| `session/query.go` | 88 | `session/query.py` | 一一对应（另抽 `list_owner` 复用） |
| `session/expiration.go` | 90 | `session/expiration.py` | 一一对应 |
| `session/fork.go` | 47 | `session/fork.py` | 一一对应 |
| `session/promotion.go` | 114 | `session/promotion.py` | 一一对应（见 SE-2） |
| `session/shared.go` | 57 | `session/shared.py` | 一一对应（另加 `metadata_object` helper） |
| （无） | — | `session/__init__.py` | Python 侧再导出 |

**没有 Go 文件整体缺失**；`text.py` 是唯一"Go 无此文件"的新增模块，承载 `service.go` 末尾三个 helper（docstring 说明是为避免 `service↔naming/query` 循环 import）。

## 符号级核对

| Go 导出符号 | Python | 备注 |
|---|---|---|
| `Scope`（5 字段，含 `Shared`） | `Scope`（`shared`） | 字段语义逐条核对一致 |
| `Service` + `NewService/NewServiceWithNaming/NewServiceWithConfig` | `Service` + `new_service/new_service_with_naming/new_service_with_config` | 默认值链一致：`TriggerStep<=0→1`、`DefaultMode==""→work`、非法 DefaultMode→work、`notifier==nil→noop` |
| `GetOrCreateCurrent/GetOrCreateCurrentWithMode/Create/DefaultMode/Resume/Current/Touch/ResetCurrent` | 同名 snake_case | ✓ |
| `setCurrent/clearCurrentIf/scopeKey/canAccess` | `set_current/clear_current_if/scope_key/can_access` | `scope_key` 的 `\x00shared\x00` 前缀逐字一致 |
| `forkTitle/defaultTitle(40)/preview(80)` | `text.py` 同名 | 截断长度与 `...` 一致 |
| `Rename/renameMetadata/Archive/Unarchive/Pin/Unpin/Delete/CleanupExpired/targetSession` | 同名 snake_case | ✓ |
| `ActivateMode/validateMode` | `activate_mode/validate_mode` | 非法模式错误文本 `invalid session mode "x"` 一致 |
| `Fork` | `fork` | ✓ |
| `MaybeScheduleNaming/titleRenamed/titleRenamedInMetadata/generateTitle/markNamingInFlight/markNamingDone/markNamingFailed/handleNamingFailure/filterConversationMessages/normalizeTitle/isPlaceholderTitle/llmCallStatus/invalidTitleReason/fallbackTitle` | 同名 snake_case（挂在 `NamingMixin`） | 逻辑一致；**调用方式不一致 → SE-1** |
| `IdleExpirationConfig/TTLMinutes/ExpireIdleRequest/ExpireIdleResult/ExpireIdleCurrent/isGroupScope/errSessionNotIdle` | 同名 + `ErrSessionNotIdle` 哨兵 | TTL 选择、事务内二次判定、`ttl<=0` 早退、`ErrNotFound` 当"没会话"都一致 |
| `WasPromoted/IsBackground/backgroundKind/promoteToForeground/ForegroundOrigin/sessionMetadataFields` | `was_promoted/is_background/background_kind/promote_to_foreground/ForegroundOrigin/session_metadata_fields/encode_metadata_fields` | 见 SE-2 |
| `sharedThreadOwnerID/sessionOwnerID/sharedThreadMetadataForScope/isSharedThreadSession` | 同名 snake_case | `group-thread:` 前缀一致；`sort_keys=True` 复刻 Go map 排序 |
| `List/ListPage/ListResumablePage/Status` | `list/list_page/list_resumable_page/status` | 请求字段逐项一致（含 `ExcludeSessionID`/`OrderByUpdatedAt`） |

## 发现

### SE-1（P0）自动会话命名（LLM 生成标题）**完全失效**，且被记成 "llm_error" 静默降级

- 严重度：**P0**（整条功能静默失效，无异常冒泡、无用户可见报错；日志/审计把它当成 LLM 调用失败）
- Go 证据（调用契约）：
  - 接口 `internal/session/types.go:57-59`：`GenerateTitle(ctx context.Context, messages []storage.Message) (TitleResult, error)`；
  - 调用点 `internal/session/naming.go:81`：`result, err := s.titleGen.GenerateTitle(ctx, messages)`（**传 ctx**）；
  - 真实实现 `internal/agent/title.go:27`：`func (g *titleGenerator) GenerateTitle(ctx context.Context, messages []storage.Message) (session.TitleResult, error)` —— 满足接口，Go 侧可正常工作。
- Python 证据（契约在两侧都被破坏）：
  1. 真实实现是**协程且要两个参数**：`atri_x/agent/title.py:48` `async def generate_title(self, ctx: Any, messages: list[Any]) -> TitleResult`；
  2. `atri_x/session/types.py:112-118` 的 `TitleGenerator` Protocol 却声明为 `def generate_title(self, messages: list[Message]) -> TitleResult`（**没有 ctx、且不是协程**）；`runtime_checkable` 的 `isinstance` 只查方法名，不查签名，所以这个问题在导入期与类型检查期都不报错；
  3. 调用点 `atri_x/session/naming.py:136` `result = self.title_gen.generate_title(messages)` —— 只传 1 个位置参数；
  4. 装配点 `atri_x/agent/core.py:218-224` 把 `agent.title.TitleGenerator` 实例直接交给 `SessionService(...)`，`atri_x/agent/model_state.py:251-256`、`model_snapshot.py:308-313`、`model_agent.py:130-132` 也只调 `set_naming/set_primary`，**没有任何适配层把协程转成同步调用**。
- 复现命令与输出（真实 `agent.title.TitleGenerator` + 真实 SQLite store + 真实 `Service`）：
  ```
  > python tmp_verify_session_naming.py
  events: ['scheduled', 'failed']
  title_after: 'Probe session'
  failure stage=llm_error reason=generate title err="TypeError: TitleGenerator.generate_title() missing 1 required positional argument: 'messages'"
  direct_call_1arg_TypeError: "TitleGenerator.generate_title() missing 1 required positional argument: 'messages'"
  direct_call_2args_returns_coroutine: True
  ```
  即：`maybe_schedule_naming` → 后台线程 `generate_title` → **立刻 TypeError** → `handle_naming_failure(..., stage="generate title", err, "llm_error", ...)`（`naming.py:135-141`）→ 通知 `NamingFailedEvent(llm_call="failed")`。
- 影响：
  1. **用户可见**：会话标题永远不会由模型生成；会话标题停留在 `Create` 时由首条消息截出来的默认值；失败累计到 3 次后（`MAX_NAMING_FAILURES`）落入 fallback 标题（首条 user 消息前 40 字）。
  2. 每次命名都发一条 `NamingFailedEvent`，`stage=generate title`、`llm_call=failed` —— 运维/审计会看到"LLM 调用失败"的假故障，掩盖真实原因（TypeError）。
  3. 即便把调用改成 `generate_title(ctx, messages)`，返回值仍是**未被 await 的协程**（探针最后一行 `direct_call_2args_returns_coroutine: True`），标题依旧不会写库，只会多个 `RuntimeWarning: coroutine ... was never awaited` —— 所以修复必须同时解决"参数"和"await/同步桥接"两件事。
  4. 现有测试发现不了：`tests/test_session.py:108-125` 与 `tests/test_session_lifecycle.py` 用的 `FakeTitleGenerator.generate_title(self, messages)` 是**同步**的，恰好匹配 Protocol 而不是真实实现（`rg -n "agent\.title" tests` 无命中）。
- 修复建议（任选其一，推荐第 1 个）：
  1. 在 `session` 层把命名调用改成"拿到 awaitable 就调度到事件循环"（例如 `naming.py` 里检测返回值有 `__await__` 时用 `asyncio.run_coroutine_threadsafe` 投递到主循环，或把 `maybe_schedule_naming` 改成 async 由调用方 await），并同步修正 `TitleGenerator` Protocol 的签名；
  2. 或在 `agent` 侧加一个**同步适配器**（`generate_title(messages)`，内部 `asyncio.run_coroutine_threadsafe(...).result()`）再交给 `SessionService`，并把 Protocol 签名改为与之一致；
  3. 无论哪种，都应补一条"用真实 `agent.title.TitleGenerator` 走一遍 `maybe_schedule_naming` 并断言标题落库"的测试（当前夹具无法覆盖这条集成路径）。
- 是否建议现在修：**必须现在修**。

### SE-2（P3）`promotion` 的 metadata 重编码与 Go 的 RawMessage 语义有三处细微差异

- 严重度：**P3**（无实际后果，记录以便交叉核对）
- (a) **值不压缩空白**：Go `internal/session/promotion.go:89-95` 用 `json.Marshal(map[string]json.RawMessage)`，编码器会对 RawMessage 调 `compact()`（去空白）；Python `atri_x/session/promotion.py:191-203` 把原始文本**逐字**嵌入 → `{"a": {"b": 1}}` 在 Go 里写成 `{"a":{"b":1}}`，Python 保留空格。两者都是合法 JSON、语义相同。
- (b) **容忍尾部垃圾**：Go 的 `json.Unmarshal` 会因对象之后的额外内容报错并返回空 map（`promotion.go:110-112`）；Python `session_metadata_fields`（`promotion.py:144-188`）手写扫描器在看到 `}` 就返回，`{"a":1} x` 会被解析成 `{"a":"1"}`。
- (c) **`metadata="null"`**：Go `Rename` 的 `renameMetadata` 会先 `json.Unmarshal("null", &map)` 把 map 置 nil，随后 `metadata["title_renamed"]=true` **panic**（"assignment to entry in nil map"）—— 这是上游缺陷；Python `lifecycle.py:132-140` 改为抛 `InvalidError("decode session metadata: not a JSON object")`。Python 行为更安全，**不建议**向 Go 对齐。
- 影响：正常路径（metadata 由本仓写、无尾部垃圾）下三条都不可达。
- 建议：只在 (a) 上做零成本对齐；不建议现在修。

### SE-3（P3）`session.TitleGenerator` Protocol 的签名与唯一实现不一致（SE-1 的契约面）

- 严重度：**P3**（本身只是契约/类型声明错误；它的后果全部算在 SE-1）
- 证据：`atri_x/session/types.py:112-118` 声明 `def generate_title(self, messages: list[Message]) -> TitleResult`；
  唯一真实实现 `atri_x/agent/title.py:48` 是 `async def generate_title(self, ctx, messages) -> TitleResult`；
  上游接口 `internal/session/types.go:57-59` 是 `GenerateTitle(ctx, messages) (TitleResult, error)` —— 即 **Python 的 Protocol 反而偏离了 Go**（丢了 ctx、丢了 async 语义）。
- 影响：`@runtime_checkable` 的 `isinstance` 只检查方法存在性，不检查签名，因此这个谎言在导入期、类型检查期、单测（用的是同步假实现）都不报错，只在真实调用时变成 SE-1 的 TypeError。
- 修复建议：把 Protocol 改成 `async def generate_title(self, ctx, messages)`（或在适配器方案下改成与适配器一致），并让假实现与真实实现同形，避免测试继续掩盖。
- 是否建议现在修：**建议与 SE-1 一起修**。

## 已核对无问题的高风险点

1. **`maybe_schedule_naming` 的准入与阈值**：`title_renamed(metadata) || title_gen==nil` 早退、消息加载失败 → `NotifyNamingFailed(reason="load messages")`、`filterConversationMessages`（只留 user/assistant 且 content 非空）、`len < TriggerStep` 早退、`namingMessages = conversation[:TriggerStep]`、`markNamingInFlight` 三重短路（in_flight/done/failures>=3）—— 与 Go `naming.go:26-46` 逐行一致。
2. **迟到自动标题让位于手动改名**：写入在 `Sessions().Mutate` 事务内**重新**检查 `title_renamed`，命中则 `mark_naming_done` 并放弃（`naming.py:158-169` ↔ Go `naming.go:97-108`）；`handle_naming_failure` 的 fallback 也做同样的二次检查（`naming.py:255-264` ↔ Go `naming.go:178-194`，含 `ErrTitleRenamed` 静默返回与"fallback title"失败再通知）。`markNamingFailed` 的失败计数、`shouldFallback = failures>=3 || isPlaceholderTitle` 一致。
3. **`titleRenamedInMetadata`**：空/空白→false、非法 JSON→false、`title_renamed` 必须**恰为 bool true**（Python `is True`，Go 反序列化到 `bool` 字段）—— 一致。
4. **生命周期幂等与事务**：`archive`（已归档直接返回）、`pin`（已置顶直接返回）、`unpin`、`unarchive`（先 `IsBackground → promoteToForeground`，再清 `archived_at`，最后 `setCurrent`）—— 与 `lifecycle.go:54-138` 顺序一致；`rename` 的 `title_required`、`renameMetadata` 写入 `title_renamed=true` + `title_source="manual"` 一致。
5. **`canAccess` 三分支**：CLI 放行一切；shared 要求同 platform+scope 且 `thread_mode=group`；否则要求同 owner+platform 且（同 scope **或** `IsBackground`）—— 与 `service.go:185-198` 逐行一致；`targetSession`/`resume`/`fork` 的越权错误文本 `session %s is not in current platform scope` 一致。
6. **命名线程与库的并发**：`naming.py:102-108` 用 `threading.Thread(daemon=True)` 对应 Go 的 `go s.generateTitle(...)`；`Service.mu` 是 `threading.RLock`，持锁范围都在小字典上；SQLite 侧是"单连接 + 一把 RLock（`check_same_thread=False`）"，后台线程的 `mutate` 与前台请求互斥但不会死锁（同一把 RLock，同线程可重入）—— 未发现阻塞事件循环的持锁长操作。
7. **过期判定**：`expire_idle_current` 的 `ttl<=0` 早退、`is_zero_time(req.now) → now()`、`!updated_at.Before(now-ttl)` 早退、事务内二次判定 + `ErrSessionNotIdle`、`ErrNotFound` 当作"无当前会话"、成功后 `clear_current_if` —— 与 `expiration.go:33-69` 一致；群聊判定 `group:`/`supergroup:` 前缀与 `TrimSpace` 一致。
8. **`Fork`/`Status`/`List*`**：只允许从 assistant 消息 fork、`forkTitle(source.Title)`、`Mode: source.Mode`、`sharedThreadMetadataForScope(scope, "")`；`status` 从后往前取最近一条 user/assistant 预览并在两者都拿到后 break —— 与 `fork.go` / `query.go` 逐行一致（`preview` 的 `\n`→空格再截 80 一致）。
9. **本集群测试全绿**：`python -m unittest tests.test_session tests.test_session_lifecycle`（含在四集群 332 个用例里）→ `OK`；`rg -n "agent\.title" tests` 无命中（这正是 SE-1 逃逸的原因，见该条）。
