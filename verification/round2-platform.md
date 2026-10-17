# 第二轮对抗性验证：`atri_x/platform/**`

- 验证对象：`atri_x/platform/**`（65 个 .py）←→ 上游 `internal/platform/**`（43 个生产 .go）
- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)；本仓：本仓库工作树（Python 3.14.7）

## 覆盖表

工具：`python tmp_verify2_cover.py internal/platform atri_x/platform`

```
Go 生产文件 43 个；未在 docstring 里声明对应的: 1 / 43
   - internal/platform/headless/headless.go
```

`atri_x/platform/headless.py` 存在（59 行）且 `tmp_verify2_symdiff.py` 报 `internal/platform/headless/headless.go -> atri_x/platform/headless.py: go=6 missing=0`，只是 docstring 没写 Go 出处 —— **不是缺件**。

| Go 目录/文件 | Python | 说明 |
|---|---|---|
| `platform.go`(194) | `platform.py`(308) | Protocol 化 + `PlatformConnectedSignal` 取代 `signal.New`（`platform.py:11-13` 已注明） |
| `config.go`(62) / `media.go`(64) / `backoff.go`(41) | `config.py`(65) / `media.py`(85) / `backoff.py`(43) | 逐符号对齐 |
| `builtin/builtin.go`(87) | `builtin.py`(300) | 平台分支展开 + `Bundle`/`Options` |
| `headless/headless.go`(21) | `headless.py`(59) | |
| `refcontext/refcontext.go`(316) | `refcontext.py`(375) | PORTING §4 单列一行 |
| `cli/cli.go`(196) | `cli/adapter.py`(295) | |
| `cli/clipboard.go`(88) / `cli/config.go`(97) | `cli/clipboard.py`(227) / `cli/config.py`(163) | |
| `cli/remote_client.go`(173) / `remote_server.go`(350) / `remote_protocol.go`(33) | `cli/remote_client.py`(265) / `remote_server.py`(459) / `protocol.py`(328) | `remote_protocol.go` → `protocol.py`，多出帧编解码细节 |
| `cli/tui.go`(1023) | `cli/tui/{model,program,view,content,style,theme,viewport,messages,keys,input,mouse,copy}.py` | bubbletea → ANSI 行式渲染（已公开偏差）；`View` → `TuiModel.view()` |
| `cli/tui_copy.go`(739) | `cli/tui/copy.py`(688) | |
| `cli/tui_file_reference.go`(369) | `cli/local_file.py`(349) | 符号 diff `go=1 missing=0` |
| `cli/tui_mouse.go`(112) | `cli/tui/mouse.py`(139) | |
| `qq-onebot/*.go`(6 文件) | `qq_onebot/{adapter,attachment,capabilities,config,inbound,message,transport,types,forward,output,preprocess,target,__init__}.py` | 单文件拆分（`message.go` 657 → `message.py` 207 + `forward.py` 401 + `preprocess.py` 460 + `output.py` 156 + `target.py` 83） |
| `qqofficial/*.go`(9 文件) | `qqofficial/{adapter,attachment,client,config,gateway,keyboard,message,send,token,types,target,__init__}.py` | |
| `telegram/*.go`(9 文件) | `telegram/{adapter,capabilities,client,config,format,keyboard,message,stream,types,target,__init__}.py` | |

**结论：文件级覆盖无缺口**，唯一的「未声明」有对应模块；所有拆分都在各自 docstring 注明。

## 符号级核对

工具：`python tmp_verify2_symdiff.py <Go文件> <Python文件> ...`（Go 导出 func/type/const/var → Python 的 PascalCase 原名或 snake_case 化名）

```
OK internal/platform/platform.go -> atri_x/platform/platform.py: go=37 missing=0
OK internal/platform/config.go -> atri_x/platform/config.py: go=5 missing=0
OK internal/platform/media.go -> atri_x/platform/media.py: go=2 missing=0
OK internal/platform/backoff.go -> atri_x/platform/backoff.py: go=5 missing=0
OK internal/platform/builtin/builtin.go -> atri_x/platform/builtin.py: go=7 missing=0
OK internal/platform/headless/headless.go -> atri_x/platform/headless.py: go=6 missing=0
OK internal/platform/cli/cli.go -> atri_x/platform/cli/adapter.py: go=16 missing=1     (New → 已按规范改成 new)
OK internal/platform/cli/config.go -> atri_x/platform/cli/config.py: go=5 missing=0
OK internal/platform/cli/clipboard.go -> atri_x/platform/cli/clipboard.py: go=1 missing=0
OK internal/platform/cli/remote_client.go -> atri_x/platform/cli/remote_client.py: go=7 missing=0
OK internal/platform/cli/remote_server.go -> atri_x/platform/cli/remote_server.py: go=16 missing=1  (ConnectedClientIDs → connected_client_ids，探针 snake 转换误报)
OK internal/platform/cli/remote_protocol.go -> atri_x/platform/cli/protocol.py: go=0 missing=0
OK internal/platform/cli/tui_file_reference.go -> atri_x/platform/cli/local_file.py: go=1 missing=0
OK internal/platform/cli/tui_copy.go -> atri_x/platform/cli/tui/copy.py: go=0 missing=0
OK internal/platform/cli/tui_mouse.go -> atri_x/platform/cli/tui/mouse.py: go=0 missing=0
OK internal/platform/telegram/message.go / format.go / capabilities.go / stream.go / keyboard.go / types.go -> 对应 .py: missing=0
!! internal/platform/telegram/config.go -> config.py: go=3 missing=1   (Logger 接口 → Python 用鸭子类型 logger，P3)
!! internal/platform/telegram/client.go -> client.py: go=2 missing=2   (redactedTelegramError.Error()/Unwrap() → Python 的 RedactedTelegramError 类，见下)
```

`redactTelegramError` 的脱敏契约已逐行核对（`internal/platform/telegram/client.go:235-249` ↔ `atri_x/platform/telegram/client.py:384-400`）：替换顺序（先 `/bot<token>/` 再裸 token）、`token` 为空/文本未变时**原样返回原异常**、`RedactedTelegramError` 保留原异常 —— **完全一致**，未发现凭据泄漏面。

`internal/platform/media.go` 的脱敏也逐行核对（`chatMediaURL` ↔ `atri_x/platform/media.py:73-100`）：`x-amz-*` / `x-goog-*` / 含 `token` / 含 `signature` / `sig` / `rkey` / `auth_key` 七类 query、`/bot` 路径、`u.User != nil`、非 http(s)、空 host —— **判定集合与顺序一致**。

## 发现

### P1-1 CLI / 远程 CLI 的 `start_stream` 是 `async`，与同步契约和同步调用点不符 → 流式输出泄漏 coroutine

> 与 `round2-agent.md` P1-3 是**同一条缺陷**，此处给出平台侧证据，**总计数只算一次**。

**Go 位置**（三个平台的 `StartStream` 全是同步）
- `internal/platform/cli/cli.go:121` `func (a *Adapter) StartStream(ctx context.Context) (delivery.MessageStream, error)`
- `internal/platform/cli/remote_server.go:195` `func (s *RemoteServer) StartStream(ctx) (delivery.MessageStream, error)`
- `internal/platform/telegram/stream.go:26` `func (a *Adapter) StartStream(ctx) (delivery.MessageStream, error)`
- `internal/delivery/delivery.go:258` `type StreamingMessageSender interface { StartStream(ctx) (MessageStream, error) }`

**Python 位置**
- 契约：`atri_x/delivery/delivery.py:366-369` `@runtime_checkable class StreamingMessageSender(Protocol): async def start_stream(self, ctx) -> MessageStream`
- 实现：
  - `atri_x/platform/cli/adapter.py:218` `async def start_stream(self, ctx)` ← **异步**
  - `atri_x/platform/cli/remote_server.py:362` `async def start_stream(self, ctx)` ← **异步**
  - `atri_x/platform/telegram/adapter.py:549` `def start_stream(self, ctx)` ← 同步，**因此反而不满足 Protocol 声明的 async**

**证据（实跑）**
```
$ python tmp_verify2_stream.py
CliAdapter.start_stream is coroutine function: True
start_message_stream(...) -> <coroutine object Adapter.start_stream at 0x00000244B180EA80>
is coroutine object: True
== 对照：telegram 适配器（同步 start_stream）==
telegram.Adapter.start_stream is coroutine function: False
== 对照：remote_server（异步）==
RemoteServer.start_stream is coroutine function: True
```
`@runtime_checkable` 只查属性名、不查 async 性，所以 `isinstance(adapter, StreamingMessageSender)` 对三者**全部为 True**（`tmp_verify2_platform_proto.py` 的 `Runtime/PlatformAdapter` 一栏），调用方 `atri_x/agent/turn_output.py:250/253` 的类型判定因此看不出问题。

**生产可达性**：`atri_x/platform/builtin.py:156-159`（默认 `full` 模式）`bundle.primary = cli_new()` 即 CLI `Adapter`；`app/runtime_agent.py:107 platform=platforms.primary` → `agent.platform`。本地 CLI 的 `msg.sender` 为 `None`（只有 telegram / qqofficial / qq_onebot / remote_server 设 `sender=self`），走 `turn_output.py:252-254` 的 platform 分支。

**影响**：本地 CLI 与 `service` 模式的 RemoteServer 下，`start_message_stream` 返回协程对象；`atri_x/agent/chat_llm.py:422-428` 在第一个增量上 `stream.append(...)` → `AttributeError: 'coroutine' object has no attribute 'append'` → `RuntimeError("stream append: ...")` → **整轮失败**；Telegram 走同步实现正常。Go 的语义是「`err != nil` 就返回 nil，退回整条发送」。

**修复建议**：把 `cli/adapter.py:218`、`cli/remote_server.py:362` 改回**同步**工厂（`CliMessageStream` / `RemoteMessageStream` 本身同步构造），并把 `atri_x/delivery/delivery.py:369` 的 Protocol 声明改为 `def start_stream(...)`；这样三处实现与调用方一致，也还原 Go 的「失败即回退 nil」。

**建议 Lead 立刻修：是。**

### P3-2 `internal/platform/telegram/config.go:27` 的 `Logger` 接口在 Python 侧没有对应类型
- Go：`type Logger interface { Debug/Info/Warn/Error(...) }`，`Config` 结构体持有一个 `Logger` 字段
- Python：`atri_x/platform/telegram/config.py` 里 `Logger` 出现 0 次、`logger` 出现 2 次，靠鸭子类型传 logger
- 影响：无功能影响，交叉检索 Go 的 `Logger` 类型时定位不到 Python 侧落点
- 建议：在 `config.py` 的 docstring 里注明「Go `Logger` 接口 → Python 鸭子类型 logger」，或补一个 `@runtime_checkable Protocol`；**不必现在修**

### P3-3（一行一条）
- `atri_x/platform/cli/tui/model.py` 未提供 `View`（bubbletea 的 `View() string`）同名入口，实际渲染在 `view.py` + `program.py:445 _draw`；属已公开的 ANSI 行式渲染偏差，仅记录交叉检索断点。
- `internal/platform/headless/headless.go`、`internal/platform/cli/tui_copy.go` 等文件在 Python 侧无 docstring 反向声明，按 Go 路径检索会漏。

## 已核对无问题的高风险点

| 高风险点 | 怎么验的 | 结论 |
|---|---|---|
| **适配器对 `PlatformAdapter`/可选能力 Protocol 的一致性（含 async 性）** | `tmp_verify2_platform_proto.py`：对 7 个具体类 × 9 个 Protocol 做 `isinstance` + **逐方法比对 async/sync** | 除 P1-1 的 `start_stream` 外**无 async/sync 不一致**；`name/run/send_chat/send_notice` 四个必需方法在 7 个类上全部齐全且 async 性一致；`MediaResolver` / `GroupHistoryProvider` / `GroupDirectoryProvider` / `UserAvatarProvider` / `GroupHistoryProvider` 的 6 个方法也全部一致 |
| **可选能力的类型断言是否与 Go 一致** | grep `isinstance(...Protocol)`：全仓只有 `atri_x/platform/qq_onebot/adapter.py:273` 一处 | 该处与 `internal/platform/qq-onebot/adapter.go:709` 的 `handler.(platform.PlatformEventHandler)` 逐行对应；其余能力检查都是 `hasattr`，且都能在 Go 里找到同名类型断言 |
| **发送失败被吞成「已发送」** | `tmp_verify2_swallow.py`：AST 扫 `atri_x/platform/**` 所有 `except` 块里 `return Receipt()` / `return None` / `return []` / `return {}` | 12 处命中全部是**解码/读取助手**（`_decode` / `_read_line` / `parse_rfc3339_nano` / `unmarshal_chat_segments` / `complete` 的补全结果），**没有一处**在发送路径上用空 `Receipt()` 掩盖失败 |
| **`UnmarshalChatSegments` 的失败语义** | 读 `internal/platform/media.go:41-47` 与 `atri_x/platform/media.py:59-70` | 一致：解析失败返回空（Go 返回 `nil`，Python 返回 `[]`），不抛异常 |
| **Telegram bot token 脱敏** | 逐行比对 `client.go:235-249` ↔ `client.py:384-400` | 一致（替换顺序、空 token 短路、文本未变时返回原异常对象） |
| **round2-agent P1-4 的 sender 来源** | grep `sender=`：`telegram/adapter.py:276`、`cli/remote_server.py:379`、`qqofficial/message.py:139`、`qq_onebot/preprocess.py:108` | 与 Go 的 `msg.Sender = a` 落点一致；本地 CLI 不设 `sender`，与 Go 相同（Go 侧 CLI 也只在 remote server 里设） |
| **`builtin.py` 的平台分支与 Go 逐条对应** | 读 `internal/platform/builtin/builtin.go` 全文 ↔ `atri_x/platform/builtin.py:118-177` + 各 `_append_*` | 分支条件一致（`mode == service` → headless 主平台；`cli_new is None` 的降级分支是 Python 新增但显式记入 `bundle.skipped`，不会静默） |
| **CLI/TUI 的行为面是否有缺件** | `tmp_verify2_symdiff.py` 对 `cli/*.py` 与 `tui/*.py`，以及 `internal/platform/cli/tui_file_reference.go` ↔ `local_file.py` | 无缺件；`localFileResolver`（含 `maxFileSize=256KiB` / `maxTotal=1MiB` / `.git,node_modules,vendor,dist,build` 忽略目录）在 `local_file.py` 里全有对应物 |

## 本次用到的探针（均已删除）

`tmp_verify2_cover.py`、`tmp_verify2_symdiff.py`、`tmp_verify2_platform_proto.py`、`tmp_verify2_swallow.py`、`tmp_verify2_await.py`、`tmp_verify2_discard.py`、`tmp_verify2_stream.py`
