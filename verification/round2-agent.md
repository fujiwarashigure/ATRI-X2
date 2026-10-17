# 第二轮对抗性验证：`atri_x/agent/**`

- 验证对象：`atri_x/agent/**`（122 个 .py，其中生产模块含 `commands/`）←→ 上游 `internal/agent/**`（100 个生产 .go）
- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)；本仓：本仓库工作树（Python 3.14.7）
- 契约出处：`PORTING.md` §1/§2/§3/§3.1/§4/§7/§8
- 验证者不是实现者；本报告只写 `verification/`，未改动 `atri_x/**`、`tests/**`

## 覆盖表

用「Python 模块 docstring 里声明的 Go 出处」建表（工具 `tmp_verify2_cover.py`）：

```
python tmp_verify2_cover.py internal/agent atri_x/agent
-> Go 生产文件 (100) ；docstring 未声明对应的 Go 文件数: 2 / 100
   - internal/agent/commands/help_util.go
   - internal/agent/commands/session_mode.go
```

两条「未声明」经源码核对**功能已实现，只是 docstring 没写**（不是缺口）：

| Go 文件 | 实际落点 | 证据 |
|---|---|---|
| `internal/agent/commands/help_util.go` | `atri_x/agent/commands/help.py` | `wants_command_help` / `format_command_help` / `rewrite_help_prefix` / `command_usage` 四个符号全在 `help.py:37/74/48/64`，且 `help.py:20-25` 的 `__all__` 里导出 |
| `internal/agent/commands/session_mode.go` | `atri_x/agent/commands/session_core.py` | `NewWork` → `session_core.py:197 new_work`、`NewChat` → `:228 new_chat`，命令名 `"work"` / `"chat"` 在 `:218` / `:257` |

Go 单文件 → Python 多模块的拆分（PORTING.md §2 允许，已在各自 docstring 注明），主要 5 处：

| Go | Python |
|---|---|
| `core.go` | `core.py` / `core_build.py` / `core_forward.py` / `bootstrap.py` / `seams.py` |
| `model.go` | `model.py` / `model_agent.py` / `model_list.py` / `model_service.py` / `model_state.py` / `state_watch.py` |
| `budget.go` | `budget.py` / `budget_chat.py` / `budget_keys.py` / `budget_ledger.py` |
| `chat_llm.go` | `chat_llm.py` / `chat_stream.py` / `chat_observe.py` / `chat_vision.py` |
| `group_policy.go` | `group_policy.py` / `group_policy_rules.py` |

反向多出（Go 无对应、Python 侧新增的接缝文件）：`__init__.py`、`bootstrap.py`、`core_build.py`、`core_forward.py`、`seams.py`、`tools_service.py`、`model_service.py`、`tool_schemas.py`、`tool_directive_skill.py`、`group_policy_rules.py`、`commands/deps.py`、`commands/model_types.py`、`commands/log_args.py`、`commands/log_format.py`、`commands/completion.py` —— 均属「拆文件 / 服务门面 / 命令依赖协议」，不是缺失实现。

**结论：文件级覆盖无缺口。**

## 符号级核对

工具：`python tmp_verify2_bind.py agent`（把「第一个形参名为 `agent`/`host` 的模块级函数」与 `Agent` 类的 355 个属性求差，再找 `X.<name>(` 调用点）。

```
宿主类 Agent 的属性数: 355
候选宿主函数数: 460
=== 宿主函数名字不在 Agent 上、但存在 `X.name(` 调用点 ===
  _audit                 → 误报（BudgetLedger 自己的 _audit）
  _mutate_metadata       → 误报（SessionWorkspaceStore 自己的）
  apply_character_directives          ← 真缺口，见 P0-1
  apply_turn_overrides                ← 真缺口，见 P0-1
  audit_event            → 误报（注入的回调字段）
  notify_character_directive_result   ← 真缺口，见 P0-1
  notify_turn_override_result         ← 真缺口，见 P0-1
  replace_and_finish_stream → 误报（out 是 TurnOutput 实例）
  send_notice_public     → 误报（core.py 显式模块级调用）
  send_preview           → 误报（同上）
-- 命中 10 个（真缺口 4 个，全在 P0-1）
```

`python tmp_verify2_directives.py` 的类级核对（决定性）：

```
  apply_tool_directives                  True   owner=atri_x.agent.tool_directive.ToolDirectiveMixin
  notify_tool_directive_result           True   owner=atri_x.agent.tool_directive.ToolDirectiveMixin
  apply_skill_directives                 True   owner=atri_x.agent.tool_directive_skill.SkillDirectiveMixin
  notify_skill_directive_result          True   owner=atri_x.agent.tool_directive_skill.SkillDirectiveMixin
  apply_character_directives             False  owner=None  __getattr__=no
  notify_character_directive_result      False  owner=None  __getattr__=no
  apply_turn_overrides                   False  owner=None  __getattr__=no
  notify_turn_override_result            False  owner=None  __getattr__=no
```

即：**同一组四个「单回合声明」能力，`tool` / `skill` 两个走 mixin 正确挂上了，`character` / `turn_override` 两个只有模块级函数、从未绑到 `Agent`。** `Agent` 没有 `__getattr__`（`__getattr__=no`），所以不会兜底。

## 发现

### P0-1 `@char:` 单回合角色声明与 `@turn:` 单回合覆盖完全失效（每轮聊天必抛 AttributeError）

**Go 位置**
- `internal/agent/character_directive.go:22` `func (a *Agent) applyCharacterDirectives(ctx context.Context, text string) characterDirectiveResult`
- `internal/agent/character_directive.go:66` `func (a *Agent) notifyCharacterDirectiveResult(ctx, result)`
- `internal/agent/turn_override.go:54` `func (a *Agent) applyTurnOverrides(ctx, text) (context.Context, turnDirectiveResult)`
- `internal/agent/turn_override.go:109` `func (a *Agent) notifyTurnOverrideResult(ctx, result)`
- 调用点：`internal/agent/input.go:165/167/174/176`（`a.applyCharacterDirectives(...)` —— **方法调用**）

**Python 位置**
- 实现写成了模块级函数（首参是 `agent`），从未成为 `Agent` 方法：
  - `atri_x/agent/character_directive.py:48` `def apply_character_directives(agent, ctx, text)`
  - `atri_x/agent/character_directive.py:98` `async def notify_character_directive_result(agent, ctx, result)`
  - `atri_x/agent/turn_override.py:93` `def apply_turn_overrides(agent, ctx, text)`
  - `atri_x/agent/turn_override.py:147` `async def notify_turn_override_result(agent, ctx, result)`
- 调用点照抄了 Go 的**方法**写法：
  - `atri_x/agent/input.py:267` `character_directives = agent.apply_character_directives(ctx, text)`
  - `atri_x/agent/input.py:271` `await agent.notify_character_directive_result(ctx, character_directives)`
  - `atri_x/agent/input.py:277` `ctx, turn_override = agent.apply_turn_overrides(ctx, text)`
  - `atri_x/agent/input.py:283` `await agent.notify_turn_override_result(ctx, turn_override)`
- 接线缺口：`atri_x/agent/bootstrap.py:51-57` 的 `_CORE_INSTALLERS` 只有 `model_service` / `context_runtime` / `budget_chat` / `ratelimit` / `tool_directive` 五项，`character_directive` 与 `turn_override` **既没有 mixin 也没有 installer**。

**证据（实跑）**

静态探针：
```
$ python tmp_verify2_await.py atri_x/agent
atri_x/agent/input.py:267  in <module>  call apply_character_directives(2 pos)  def apply_character_directives at atri_x\agent\character_directive.py:48  expected 3..3
atri_x/agent/input.py:271  in <module>  call notify_character_directive_result(2 pos)  ... at ...character_directive.py:98  expected 3..3
atri_x/agent/input.py:277  in <module>  call apply_turn_overrides(2 pos)  ... at ...turn_override.py:93  expected 3..3
atri_x/agent/input.py:283  in <module>  call notify_turn_override_result(2 pos)  ... at ...turn_override.py:147  expected 3..3
```

最小复现（真实 `Agent` + 测试脚手架，`tmp_verify2_p0_probe.py`）：
```python
from tests.test_agent_core import make_agent, make_ctx
from atri_x.agent.core import Agent
agent = make_agent(); ctx = make_ctx()
session = Session(id="s1", platform="cli", platform_scope_id="local", mode=SessionModeWork)
agent.store.sessions().create(session)
await Agent.handle_session_input(agent, ctx, session, "你好")
```
输出：
```
== 1) 直接查符号是否挂在 Agent 上 ==
   hasattr(agent, 'apply_character_directives') = False
   hasattr(agent, 'notify_character_directive_result') = False
   hasattr(agent, 'apply_turn_overrides') = False
   hasattr(agent, 'notify_turn_override_result') = False

== 2) 走真实入口 Agent.handle_session_input(...) ==
   !!! AttributeError: 'Agent' object has no attribute 'apply_character_directives'
      File "atri_x/agent/core.py", line 555, in handle_session_input
        await input_module.handle_session_input(self, ctx, session, text)
      File "atri_x/agent/input.py", line 267, in handle_session_input
        character_directives = agent.apply_character_directives(ctx, text)
      AttributeError: 'Agent' object has no attribute 'apply_character_directives'
```

**影响**：`handle_session_input` 是普通聊天消息的主路径（`message.py:177 → core.py:551 handle_input → input.py:190 handle_input → :220 handle_session_input`），第 267 行**无条件执行**（只被 `snapshot.phase != PhaseAwaitRiskConfirm` 挡着）。因此**每一条正常聊天消息都会在这里抛 AttributeError**，被 `message.py:77 except BaseException` 兜住并呈现成「请求失败」，用户看不到任何回复；`@char:` / `@c:` 单回合角色声明与 `@model:` / `@use:` / `@image:` 单回合覆盖两个功能**完全不可用**。

**为什么测试全绿**：`tests/test_agent_directives.py` 全程用 `_DirectiveHost` 假宿主（`:132`）**直接调用模块级函数**（`:223`、`:239`、`:343`），从不经过 `Agent` 的方法解析；`install_tool_directive_services` 那一条（`:337-344`）也只覆盖了 tool/skill 两个。这与上一轮 session 的 P0（`TitleGenerator` 同步假实现掩盖协程 + 参数不匹配）是同一类漏洞。

**修复建议**：在 `atri_x/agent/seams.py` 之外新增两个 mixin（与 `ToolDirectiveMixin` 同形），或最小改动——在 `core.py` 的 `Agent` 类里加四个显式转发方法：
```python
def apply_character_directives(self, ctx, text):
    from atri_x.agent.character_directive import apply_character_directives
    return apply_character_directives(self, ctx, text)
# notify_character_directive_result / apply_turn_overrides / notify_turn_override_result 同理
```
并把 `character_directive` / `turn_override` 加进 `bootstrap._CORE_INSTALLERS`（若走 installer 路线）。修完后加一条**经过真实 `Agent`** 的用例（`Agent.handle_session_input` 用 `make_agent()`）。

**建议 Lead 立刻修：是。**

---

### P0-2 `Agent.group_tool_allowlist` / `filter_group_feature_schemas` / `group_default_model_selection` 自重入 → 每轮 RecursionError

**Go 位置**（三个方法都是**直接实现**，从不经 `Deps.GroupPolicy` 二次分派）
- `internal/agent/tool_auth.go:53` `func (a *Agent) groupToolAllowlist(ctx) map[string]bool`
- `internal/agent/tool_auth.go:138` `func (a *Agent) filterGroupFeatureSchemas(ctx, schemas) []llm.ToolSchema`
- `internal/agent/group_policy.go:865` `func (a *Agent) groupDefaultModelSelection(ctx) (config.ModelSelection, bool)`
- `internal/agent/core.go:454` `GroupPolicy: a,` —— `Deps.GroupPolicy` 接 `a` 只是**给命令层**用（`commands/group_policy.go:62-81` 才读 `c.deps.GroupPolicy`）

**Python 位置**（多了一层 Go 里不存在的「优先用服务」分派）
- `atri_x/agent/core.py:587` `group_default_model_selection`：`:596 service = self._svc("group_policy")`，`:597 if service is not None and hasattr(service, "group_default_model_selection")`，`:598 result = service.group_default_model_selection(ctx)`
- `atri_x/agent/core.py:608` `group_tool_allowlist`：`:614/:615/:616` 同上
- `atri_x/agent/core.py:619` `filter_group_feature_schemas`：`:624/:625/:626` 同上
- `atri_x/agent/seams.py:57-67` `_svc`：`services[key]` 没有就退回 `command_deps.<key>`
- `atri_x/agent/bootstrap.py:412-417`：`for attribute in ("group_policy","group_knowledge","group_services"): service = services.get(attribute); if service is None: service = agent` —— **把 Agent 自己写进了 `deps.group_policy`**

`Agent` 上就有这三个同名方法，于是 `hasattr(service, ...)` 恒为真 → 自己调自己。

**证据（实跑，`tmp_verify2_recur.py`）**
```
构造后 command_deps.group_policy = None
装配后 deps.group_policy is agent ? True
装配后 deps.group_knowledge is agent ? True
装配后 deps.group_services is agent ? True
agent._svc('group_policy') is agent ? True
report.services = ['compact', 'context_policy', 'context_status', 'group_knowledge', 'group_policy', 'group_services', 'member_panel', 'models', 'tools']
   [RecursionError] Agent.group_tool_allowlist(): maximum recursion depth exceeded
   [RecursionError] Agent.filter_group_feature_schemas(): maximum recursion depth exceeded
   [RecursionError] Agent.group_default_model_selection(): maximum recursion depth exceeded
```

**生产可达性**：`atri_x/app/runtime_agent.py:192` 的 `install_agent_services(agent)` 是**必跑**的；`atri_x/app/runtime_agent.py:113` 传的 `group_policy=getattr(cfg,"group_policy",None)` 是 **dict**（`atri_x/config/app.py:172` `group_policy: dict[str, GroupPolicyConfig]`），而 `core.py:700-701` 对 dict 显式跳过（`if key == "group_policy" and isinstance(value, dict): continue`），所以 `services["group_policy"]` 永远缺失 → `_svc` 必然拿到 Agent 自己。

命中调用链（真实回合）：
- `atri_x/agent/chat_turn.py:103` 与 `:354` `tools = await tools_for_session(agent, ctx, session)` → `atri_x/agent/tool_schemas.py:94 allowlist = agent.group_tool_allowlist(ctx)` → RecursionError
- `atri_x/agent/chat.py:273` `group_model, has_group = agent.group_default_model_selection(ctx)` → RecursionError

**影响**：装配过（=生产）之后，**每个回合都在构建工具 schema / 选模型时 RecursionError**，用户看到的又是「请求失败」。`except BaseException` 把它吃掉，日志里只有一个笼统错误。

**修复建议**：把三个方法的「服务分派」删掉，直接委托给模块级实现（与 Go 一致）：
```python
def group_tool_allowlist(self, ctx):
    return ToolAuthMixin.group_tool_allowlist(self, ctx)
def filter_group_feature_schemas(self, ctx, schemas):
    return ToolAuthMixin.filter_group_feature_schemas(self, ctx, schemas)
def group_default_model_selection(self, ctx):
    return group_policy_module.group_default_model_selection(self, ctx)
```
或退一步：在 `_svc` 里把 `service is self` 当作「没注册服务」返回 `None`（`bootstrap.py:412-417` 的 `service = agent` 也建议改成不再往 `deps` 里塞 Agent 自己，或让 `_svc` 识别这种自指）。

**建议 Lead 立刻修：是。**（与 P0-1 一起构成「装配后聊天全挂」的组合故障）

---

### P1-3 CLI / 远程 CLI 的流式输出泄漏 coroutine（`start_message_stream` 是同步的，实现是异步的）

**Go 位置**
- `internal/agent/turn_output.go:185-204` `func (a *Agent) startMessageStream(ctx) delivery.MessageStream` —— **同步**；`sender.StartStream(ctx)` 返回 `(MessageStream, error)`，`err != nil` 时回退 `nil`
- `internal/agent/turn_output.go:17` `StartStream(ctx context.Context) delivery.MessageStream`（turnOutput 契约也是同步）
- `internal/platform/cli/cli.go:121`、`internal/platform/cli/remote_server.go:195`、`internal/platform/telegram/stream.go:26` 三个 `StartStream` 全是同步 `(MessageStream, error)`

**Python 位置**
- 契约：`atri_x/delivery/delivery.py:366-369` `class StreamingMessageSender(Protocol): async def start_stream(self, ctx) -> MessageStream`
- 调用方（同步）：`atri_x/agent/turn_output.py:245-255` `def start_message_stream(...)`；`:258-262 def _try_start_stream(sender, ctx): try: return sender.start_stream(ctx) except Exception: return None`
- 实现者：
  - `atri_x/platform/cli/adapter.py:218` `async def start_stream(self, ctx)` ← **异步**
  - `atri_x/platform/cli/remote_server.py:362` `async def start_stream(self, ctx)` ← **异步**
  - `atri_x/platform/telegram/adapter.py:549` `def start_stream(self, ctx)` ← 同步（正确）

`try/except` 抓不到「协程体里稍后才抛的错」，所以 Go 的「`err != nil` 回退整条发送」语义丢失。

**证据（实跑）**
```
$ python tmp_verify2_stream.py
CliAdapter.start_stream is coroutine function: True
start_message_stream(...) -> <coroutine object Adapter.start_stream at 0x00000244B180EA80>
is coroutine object: True

$ python tmp_verify2_stream2.py
interactive=True: ForegroundTurnOutput.start_stream -> <coroutine object Adapter.start_stream at ...>
   是 coroutine 对象: True
   !! stream.append 失败: AttributeError: 'coroutine' object has no attribute 'append'
      -> chat_llm 会把它包成 RuntimeError('stream append: ...')，本轮失败
interactive=False: （同上，一样是 coroutine）
```

下游落点（照抄 `atri_x/agent/chat_llm.py:422-428`）：
```python
if stream is not None and delta:
    try:
        stream.append(ctx, delta)
    except BaseException as exc:
        raise RuntimeError(f"stream append: {exc}") from exc
```
`atri_x/agent/chat_turn.py:191 stream = out.start_stream(req_ctx)` 拿到的就是协程对象；`:220` 把它传给 `call_llm`。`turn_output_allowed`（`turn_gate.py:58`）在正常前台回合返回 `True`，所以闸门不会提前返回。

**生产可达性**：`atri_x/platform/builtin.py:156-159` —— 非 `service` 模式（默认 `full`）下 `bundle.primary = cli_new()` 即 CLI `Adapter`，`app/platforms.py:99 primary=getattr(bundle,"primary",None)` → `app/runtime_agent.py:107 platform=platforms.primary` → `agent.platform` 就是它。本地 CLI 的 `msg.sender` 是 `None`（只有 telegram / qqofficial / qq_onebot / remote_server 设了 `sender=self`），因此走 `turn_output.py:252-254` 的 `agent.platform` 分支。

**影响**：本地 CLI（以及 `service` 模式下的 RemoteServer）下，模型吐出的第一段增量就会把本轮打成 `RuntimeError("stream append: ...")`；流式输出不但不生效，还会**让整轮失败**。Telegram 走的是同步实现，反而正常。

**修复建议**：二选一，不要两边都改——
1. 把 `cli/adapter.py:218` 与 `cli/remote_server.py:362` 的 `start_stream` 改回同步工厂（`CliMessageStream` / `RemoteMessageStream` 本来就是同步构造），与 `delivery.StreamingMessageSender` 的 sync 契约、`_try_start_stream` 的 try/except、Go 的语义一致；同时把 `delivery.py:369` 的 Protocol 声明改成 `def start_stream(...)`。
2. 若坚持异步契约，则 `start_message_stream` 必须变成 `async`，`TurnOutput.start_stream` 与 `chat_turn.py:191/299` 全部 `await`，`_try_start_stream` 改成 `await` + `try/except`。

**建议 Lead 立刻修：是**（选项 1 改动最小）。

---

### P1-4 运行状态永不推送到平台（`set_runtime_status` 是 async，调用点不 await 且被 `except` 吞掉）

**Go 位置**：`internal/agent/status.go:11-13` `runtimeStatusSender interface { SetRuntimeStatus(context.Context, runtimestatus.Snapshot) error }`；`:36-38` `if sender := a.runtimeStatusSender(ctx); sender != nil { _ = sender.SetRuntimeStatus(ctx, snapshot) }` —— 同步调用，效果真实发生。

**Python 位置**
- 调用点：`atri_x/agent/status.py:66-71`
  ```python
  sender = runtime_status_sender(agent, ctx)
  if sender is not None:
      try:
          sender.set_runtime_status(ctx, merged)     # ← 没有 await
      except Exception:                              # noqa: BLE001 - 状态推送失败不能影响回合
          pass
  ```
- 唯一的两个实现都是 `async`：`atri_x/platform/cli/adapter.py:226`、`atri_x/platform/cli/remote_server.py:346`
- `runtime_status_sender`（`status.py:80-88`）的判定标准只是 `hasattr(..., "set_runtime_status")`，`runtime_checkable`/hasattr 都不检查 async 性

**影响**：CLI（`agent.platform` 或 `msg.sender` 是 CLI `Adapter`）下 `publish_runtime_status` 只创建一个协程就丢掉 —— TUI 状态栏的 provider/model/阶段/用量**永远不会更新**（Go 里 `internal/platform/cli/cli.go:152-155` 会真的往 TUI 发 `tuiStatusMsg`）。同一错误不会被 `except Exception` 看见（协程创建本身不抛），属典型静默失效。Python 只在 GC 时给一条 `RuntimeWarning: coroutine 'Adapter.set_runtime_status' was never awaited`。

**修复建议**：`publish_runtime_status` 是同步函数、被 `turn_output.py:110-111` 的 async 方法间接调用，最小改法是把两个实现改成同步（它们内部只做「塞一条 TUI 消息」/「写一个 remote frame」，其中 RemoteServer 那条需要 `client.write`，可改为 `asyncio.get_running_loop().create_task(...)` 并保留错误日志），或在 `publish_runtime_status` 里 `asyncio.get_running_loop().create_task(sender.set_runtime_status(ctx, merged))` 并给 task 挂 `add_done_callback` 记录异常（不能像现在这样静默丢弃）。

**建议 Lead 立刻修：是**（P1，功能静默失效）

---

### P1-5 `naming_notifier` 是死字段：会话命名事件永不发出

**Go 位置**：`internal/agent/core.go:287` `namingNotifier := opts.NamingNotifier`；`internal/agent/core.go:298` 附近把它交给 `titleGenerator` / session service；`internal/app/runtime.go:528` `NamingNotifier: namingLogger{...}`。

**Python 位置**
- `atri_x/agent/options.py:59` `naming_notifier: Any = None`
- `atri_x/app/runtime_agent.py:132` `naming_notifier=NamingLogger(foundation.logger, audit_func(foundation.logs))` —— app 老老实实传了
- `atri_x/agent/core.py:222-224`
  ```python
  self.sessions = SessionService(
      opts.store, opts.session_config or SessionConfig(), self.title_gen, None   # ← notifier 恒为 None
  )
  ```
- `atri_x/session/service.py:89` `self.notifier = notifier if notifier is not None else NoopNamingNotifier()`

**证据（实跑）**
```
$ python tmp_verify2_options.py
Options 字段数 = 65
=== 在 atri_x/agent/** 里完全没有出现的 Options 字段 ===
  - context_overflow
  - naming_notifier
  - tool_registry
=== 其中在 atri_x/app/** 里真的被赋值的（说明 app 传了但 agent 不读）===
  ! context_overflow   被 app 传给 Options: atri_x/app/runtime_agent.py
  ! naming_notifier    被 app 传给 Options: atri_x/app/runtime_agent.py
  ! tool_registry      被 app 传给 Options: atri_x/app/runtime_agent.py
```
（`naming_notifier` 在 `atri_x/agent/**` 里出现次数为 0 —— 连一处引用都没有。）

**影响**：`NamingScheduled` / `NamingCompleted` / `NamingFailed` 三类事件一次都不会发；审计与日志里看不到命名活动。这与上一轮已公开的 P0（`TitleGenerator` 缺参导致命名本身失败）是**两个独立的缺口**，即便上一轮修好，通知链路仍然断。

**修复建议**：`core.py:222-224` 把第 4 个实参由 `None` 改成 `opts.naming_notifier`。

**建议 Lead 立刻修：是**（一行）

---

### P2-6 `Options.context_overflow` 是死字段：构造期不再 seed 每作用域溢出覆盖

- Go：`internal/agent/core.go:336` `contextOverflow: cloneContextOverflow(opts.ContextOverflow)`；`internal/app/runtime.go:511` `ContextOverflow: cfg.ContextOverflow`
- Python：`atri_x/agent/options.py:39` `context_overflow: dict[str, Any]`，`atri_x/app/runtime_agent.py:112` 传了，`atri_x/agent/**` 里 0 引用；Agent 侧没有 `context_overflow` 字段，只有 `context_overflow_state`（`context_policy.py:156-163`）
- 缓解：`model_state.py:258-260` 的 `apply_runtime_state` 会 `.replace(state.context_overflow)`，若 `start_runtime_state_watch`（`app/runtime_agent.py:209-211`）在启动时完成一次加载，最终状态可能一致 —— 本次未能证明两条路径的时序等价
- **修复建议**：构造期补 `ensure_context_overflow_state(self).replace(opts.context_overflow)`；**建议 Lead 排期修**

### P3-7（一行一条）
- `atri_x/agent/title.py` 的 `TitleGenerator` 只接 `primary` / `primary_model`，Go 的 `titleGenerator`（`internal/agent/core.go:298`）还有 `naming` / `namingModel` / `namingSource` 三个字段 —— 命名专用模型未接线（与上一轮 P0 的 `title_gen` 装配点重叠，此处只记录未修部分）。
- `atri_x/agent/commands/help_util.go` / `session_mode.go` 的实现存在但两个 Python 模块的 docstring 没写 Go 出处，交叉检索会漏。
- `atri_x/agent/state_watch.py` / `model_snapshot.py` 等模块的 docstring 声明了多个 Go 出处（`model.go` + `state_watch.go`），反向按 Go 文件检索时无法唯一定位。

## 已核对无问题的高风险点

| 高风险点 | 怎么验的 | 结论 |
|---|---|---|
| **协程漏 await（全局扫描）** | `tmp_verify2_await.py`（AST：`async def` 名字被当语句调用且未 `await`，并扣除 `@staticmethod` / `dict.items` / `re.*` 误报）跑遍 `atri_x/agent/**`，再 `tmp_verify2_discard.py` 补扫「async 函数里的裸调用」 | 除 P1-3 / P1-4 外**没有真实漏 await**；`core_forward.py:197 record_tool_call`、`tools.py:232/246/268 _call`、`toolrun_adapter.py:675/679 _call`、`inbox.py batch.finish`、`model_state.py:221 store.save` 全是**同名不同类**的误报（逐条读过源码） |
| **调用点位置参数个数** | `tmp_verify2_await.py` 的 `POSITIONAL ARG COUNT MISMATCH` 一节 | 除 P0-1 外，`_window_fallback` / `_threshold_precondition` / `_require_provider` 三条经 `tmp_verify2_show.py` 逐行核对为探针未处理 `@staticmethod` / 闭包参数的误报；`budget_ledger.py:193 self.save_state()`、`chat_stream.py:133 semaphore.release()`、`signal/signal.py:338 self._options.key(event)` 是注入回调，正确 |
| **`install_agent_services` 是否被调用** | grep + `atri_x/app/runtime_agent.py:190-192` | 已调用（在 `build_agent` 内，幂等、异常不阻断启动）。但**未传 `opts=`**，见 `round2-app.md` P0-2 |
| **`Agent` 的方法解析是否靠 `__getattr__` 兜底** | `hasattr(Agent,"__getattr__")` → `False`（`tmp_verify2_directives.py` 输出里的 `__getattr__=no`） | `Agent` 类没有转发型门面，`AgentSeams` 只提供「未装配时的保守降级」，且它在 MRO **最后**（`core.py:184`），真实 mixin 优先 —— 这个设计本身是对的，P0-1 是「压根没提供实现」而不是被门面挡住 |
| **服务门面的 `_agent` 转发 + `isinstance` 假阴性** | 读 `tools.py:106-124` `AgentService.__getattr__`、`bootstrap.py:356-373` `_HostService.__getattr__`；搜 `isinstance(...Protocol)` | 全仓只有 `qq_onebot/adapter.py:273` 一处 Protocol 判定，且与 Go 的类型断言逐行对应；`AgentService` 的转发只影响「方法不在类上」的场景，`Agent` 侧没有对服务门面做 `isinstance` 判定，**未发现假阴性** |
| **群策略三方法的 MRO 遮蔽方向** | `Agent.__mro__`（`core.py:151-185`）：`Agent` → `CoreForwardMixin` → … → `ToolAuthMixin` → … → `AgentSeams` | 方向正确（`Agent` 覆写生效、`AgentSeams` 最后）；问题不在 MRO 而在覆写体本身（P0-2） |
| **`Deps` 槽位与 `services` 键是否对齐** | 读 `bootstrap.py:68-80 _DEPS_MAPPING` + `:294-339 _install_command_facades` + `:418-425 _wire_deps` | 12 个槽位全部有落点；`context_runtime` 一物三用（`compact`/`context_status`/`context_policy`）显式 `setdefault` 补齐；`hooks` 额外要求 `hook_list()`（`:325-337`）以避免把执行期 `HookManager` 当命令层 `Hooks` —— 这条防御是有效的 |
| **`Agent` 构造是否按 Go `core.go:429-434` 装配工具运行时** | grep `set_tool_runtime\|set_tool_provider\|set_tool_config\|set_tool_tag_config` 全仓 | **未装配**，见 `round2-app.md` P0-2（跨集群，本报告不重复计数） |
| **协程方法的 `@staticmethod` 契约** | `long_memory_store._check_cancelled`、`qq_onebot.inbound._offer` 等 9 处探针报警 | 全部是 `@staticmethod`，调用点参数个数正确 |

## 本次用到的探针（均已删除）

`tmp_verify2_await.py`（漏 await + 位置参数差）、`tmp_verify2_bind.py`（宿主函数未绑类）、`tmp_verify2_directives.py`、`tmp_verify2_p0_probe.py`、`tmp_verify2_recur.py`、`tmp_verify2_options.py`、`tmp_verify2_cover.py`、`tmp_verify2_show.py`、`tmp_verify2_discard.py`、`tmp_verify2_stream.py`、`tmp_verify2_stream2.py`、`tmp_verify2_asynccheck2.py`、`tmp_verify2_symdiff.py`
