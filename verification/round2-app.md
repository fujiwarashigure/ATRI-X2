# 第二轮对抗性验证：`atri_x/app/**`

- 验证对象：`atri_x/app/**`（33 个 .py / 8,260 行）←→ 上游 `internal/app/**`（27 个生产 .go）
- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)；本仓：本仓库工作树（Python 3.14.7）

## 覆盖表

工具：`python tmp_verify2_cover.py internal/app atri_x/app`（按 docstring 声明的 Go 出处建表）

```
Go 生产文件 27 个；未在 docstring 里声明对应的: 2 / 27
   - internal/app/service_marker_unix.go
   - internal/app/service_marker_windows.go
```

| Go 文件 | Python 模块 | 说明 |
|---|---|---|
| `app.go` | `app.py` | |
| `asr.go` | `asr.py` | |
| `breaker_llm.go` | `breaker_llm.py` | |
| `cli_client.go` | `cli_client.py` | |
| `config_check.go` | `config_check.py` | |
| `cron.go` | `cron.py` | |
| `dependencies.go` | `dependencies.py` | |
| `doctor.go` | `doctor.py` + `doctor_checks.py` | 542 → 273+425，按检查项拆 |
| `environment.go` | `environment.py` | |
| `foundation.go` | `foundation.py` | |
| `health.go` | `health.py` | |
| `health_handler.go` | `health_handler.py` | |
| `health_llm.go` | `health_llm.py` | |
| `image_rewriter.go` | `image_rewriter.py` | |
| `image_to_prompt.go` | `image_to_prompt.py` | |
| `integrations.go` | `integrations.py` | |
| `models.go` | `models.py` | |
| `naming.go` | `naming.py` | |
| `ops_health.go` | `ops_health.py` | |
| `platforms.go` | `platforms.py` | |
| `platform_caller.go` | `platform_caller.py` | |
| `runner.go` | `runner.py` | |
| `runtime.go` | `runtime.py` + `runtime_agent.py` + `runtime_hooks.py` + `runtime_support.py` + `tool_runtime.py` + `service_types.py` | 583 → 428+233+87+233+361+69，按职责拆 |
| `service_marker_common.go` | `service_marker.py` | **未声明**，但 `service_marker.py` 的 `__all__` 里有 `ServiceMarker`/`marker_name` 等同名符号 |
| `service_marker_unix.go` + `service_marker_windows.go` | `service_marker.py` | **docstring 未声明**；Go 侧是 `//go:build` 分平台两文件，Python 用 `sys.platform` 单文件合并 —— 属已公开的「平台文件合并」形式差异，功能有对应物（`service_marker.py:1-168`） |
| `vision.go` | `vision.py` | |

Python 侧多出（Go 无独立对应文件）：`support.py`（40 行，通用助手）、`__init__.py`（387 行，包级再导出）。

**结论：文件级覆盖无缺口**，唯一的 2 处「未声明」是分平台文件合并，不是缺件。

## 符号级核对

（本集群以接线正确性为主，符号完整性用 `python scripts/check_imports.py` 的 `__all__` 校验兜底，见文末。）

重点核对的接线点：

| Go | Python | 结论 |
|---|---|---|
| `internal/app/dependencies.go` `DefaultDependencies()` | `dependencies.py:237-259` | 六个槽位 `environment/foundation/models/platforms/runtime/integrations/executor` 与 Go 一致；`_maybe()`（`:262-276`）在模块缺失/构造失败时静默返回 `None`，交由 `runner.py` 的必填校验报错 —— 设计上可接受 |
| `Dependencies` 字段 | `dependencies.py:224-234` | 与 Go 同名同序 |
| `IntegrationRequest` / `PlatformComponents` / `RuntimeComponents` | `dependencies.py:180-222` 区域 | 字段可交叉检索 |
| `defaultIntegrationFactory.Attach` | `integrations.py:49` | **签名不匹配，见 P0-1** |
| `runtime.go` 的 `buildAgent` | `runtime_agent.py:105-212` | 与 `internal/app/runtime.go:505-545` 的 `agent.Options{...}` 字面量逐字段核对：字段名一一对应（含 `ToolRegistry` → `tool_registry`、`ContextOverflow` → `context_overflow`、`NamingNotifier` → `naming_notifier`）；**但这三个字段在 agent 侧无人读取，见 P0-2** |

## 发现

### P0-1 `Runner.run` 调用 `integrations.attach` 少传一个参数 → 应用启动在集成阶段必然失败

**Go 位置**
- `internal/app/integrations.go:16` `func (defaultIntegrationFactory) Attach(ctx context.Context, req IntegrationRequest) (PlatformComponents, error)` —— **两个参数**
- `internal/app/runner.go` 里以 `Attach(ctx, req)` 形式调用

**Python 位置**
- 实现：`atri_x/app/integrations.py:49` `async def attach(self, ctx: Any, req: IntegrationRequest) -> PlatformComponents:`
  （`integrations.py:92` 另有 `Attach = attach` 的 Go 命名别名，指向同一函数）
- 唯一调用点：`atri_x/app/runner.py:192-203`
  ```python
  platforms = await maybe_await(
      self.deps.integrations.attach(          # ← 只传了 1 个位置参数
          IntegrationRequest(
              foundation=foundation, runtime=runtime, platforms=platforms,
              mode=mode, profiler=profiler, health=health_state,
          )
      )
  )
  ```

**证据（实跑，`tmp_verify2_app_attach.py`）**
```
deps.integrations = <atri_x.app.integrations.DefaultIntegrationFactory object at 0x000002C48F23ACF0>
type = <class 'atri_x.app.integrations.DefaultIntegrationFactory'>
signature(attach) = (ctx: 'Any', req: 'IntegrationRequest') -> 'PlatformComponents'
is coroutine function = True

== 复刻 runner.py:193 的调用形状（只传 1 个位置参数）==
   !!! TypeError: DefaultIntegrationFactory.attach() missing 1 required positional argument: 'req'

== 对照：传 2 个参数（签名要求的形状）==
   返回 coroutine: <coroutine object DefaultIntegrationFactory.attach at 0x000002C48F299380>
```

`self.deps.integrations` 的来源已核对：`dependencies.py:249` `_maybe("atri_x.app.integrations", "DefaultIntegrationFactory")` → `DefaultDependencies()` 返回的就是上面这个类（探针第一行输出）。

**为什么测试全绿**：`tests/test_app_runner.py` 用的是自己的假实现 `class Integrations: async def attach(self, req)`（`:240-246`，**一个参数**），与生产实现差一个形参；`tests/test_app_platforms.py:435` 虽然用真实类、但正确地传了两个参数 `attach(None, request)`。两条测试路径互不覆盖对方，与上一轮 session 的 P0 是同一种漏洞形态（「测试用同步/少参假实现，生产实现多一个参数」）。

**影响**：`Runner.run` 是应用启动的唯一入口（`app/runner.py` 的装配顺序 `config → logging → health → models → platforms → runtime → integrations → executor`）。第 193 行的 `TypeError` 在任何 `Deps` 为真实默认实现（`DefaultDependencies()`）的启动里都会发生，应用**永远走不到 executor 阶段**。异常被 runner 的清理/汇总逻辑收成 `ExceptionGroup`（参见 `tests/test_app_runner.py:452-464` 的同类断言），用户只看到启动失败。

**修复建议**：`runner.py:193` 改成 `self.deps.integrations.attach(ctx, IntegrationRequest(...))`（`runner.run(ctx, opts)` 本来就有 `ctx`）。同时把 `tests/test_app_runner.py:240-246` 的假实现改成 `async def attach(self, ctx, req)`，避免回归。

**建议 Lead 立刻修：是。**

---

### P0-2 `Options.tool_registry`（以及 `tool_tags_path` / `tool_tags`）从未被消费 → 工具注册表不进 Agent，模型看不到任何工具

> 跨集群（`app` ↔ `agent` ↔ `tool/builtin`）；本报告记录**触发条件**（app 侧调用形状），根因（`Agent` 构造期未按 Go `core.go:428-434` 装配）由 `round2-agent.md` 覆盖，**总计数只算 1 条**。

**Go 位置**
- `internal/agent/core.go:428-434`
  ```go
  if opts.ToolRegistry != nil || opts.Skills != nil {
      a.SetToolRuntime(opts.ToolRegistry, opts.Skills)
  } else if opts.ToolProvider != nil {
      a.SetToolProvider(opts.ToolProvider)
  }
  a.SetToolConfig(opts.ToolsConfig)
  a.SetToolTagConfig(opts.ToolTagsPath, opts.ToolTags)
  a.rebuildSystemPrompt()
  ```
  —— 全部在**构造函数里同步完成**
- `internal/app/runtime.go:544` `ToolRegistry: toolRuntime.Registry`

**Python 位置**
- app 侧传参正确：`atri_x/app/runtime_agent.py:148` `tool_registry=tool_runtime.registry`（还有 `:151 skills=`、`:173 tools_config=`、`:174 tool_tags_path=`、`:175 tool_tags=`、`:174`）
- `Agent` 侧：`atri_x/agent/core.py:244` `self.tool_runtime: Any = _ToolRuntime()` —— 全新对象，**从不读 `opts.tool_registry`**；`Agent.__init__` 里没有 `set_tool_runtime` / `set_tool_provider` / `set_tool_config` / `set_tool_tag_config` 任何一次调用
- 唯一另一条路径：`atri_x/agent/bootstrap.py:493-505`
  ```python
  registry = options.get("registry")          # ← 只认 opts 字典里的 "registry" 键
  tools = (_services_dict(agent) or {}).get("tools")
  if registry is None or tools is None:
      return                                  # ← 直接返回，set_tool_runtime 不执行
  setter = getattr(tools, "set_tool_runtime", None)
  ```
  而 app 的调用点是 `atri_x/app/runtime_agent.py:192` `report = install_agent_services(agent)` —— **没有传 `opts=`**，所以 `options == {}`，`registry is None`，第 496 行直接返回。

**证据（实跑，`tmp_verify2_toolreg.py`，复刻 `runtime_agent.py` 的接线形状）**
```
== 构造后（未装配）==
   tool_runtime.registry = None
   tool_runtime.manager  = None
   tool_runtime.provider = None

== install_agent_services(agent)（与 app/runtime_agent.py:192 完全相同的调用）==
== 装配后 tool_runtime ==
   registry = None
   manager  = None
   provider = None
   default_provider = True
   config   = ToolsConfig(max_rounds_per_turn=2)
   agent.tools_config = ToolsConfig(max_rounds_per_turn=2)
   funcs.schemas（registry 里的工具数）= 1      ← 传进去的 FakeRegistry 里有 1 个工具
```
（该探针后半段触发了 `group_tool_allowlist` 的 RecursionError，见 `round2-agent.md` P0-2，与此条无关。）

配套 grep（全仓）：
```
$ grep -rn "set_tool_runtime|set_tool_provider|set_tool_config" atri_x
atri_x/agent/bootstrap.py:497   setter = getattr(tools, "set_tool_runtime", None)   # 需要 opts["registry"]
atri_x/agent/tools.py:234       def set_tool_runtime(...)                            # 定义
（生产调用点：0 处；tests/test_agent_tools.py:124/139/148 有 3 处）

$ grep -rn "tool_registry" atri_x
atri_x/app/runtime_agent.py:148    tool_registry=tool_runtime.registry   # 传
atri_x/agent/options.py:75         tool_registry: Any = None             # 收
（读取点：0 处）
```

`tmp_verify2_options.py` 的机器化结论：
```
Options 字段数 = 65
=== 在 atri_x/agent/** 里完全没有出现的 Options 字段 ===
  - context_overflow
  - naming_notifier
  - tool_registry
=== 其中在 atri_x/app/** 里真的被赋值的（说明 app 传了但 agent 不读）===
  ! context_overflow            被 app 传给 Options: atri_x/app/runtime_agent.py
  ! naming_notifier             被 app 传给 Options: atri_x/app/runtime_agent.py
  ! tool_registry               被 app 传给 Options: atri_x/app/runtime_agent.py
```

**影响**：`agent.tool_runtime.registry` 恒为 `None`，于是
- `atri_x/agent/tool_schemas.py:75-87`：`provider is None` → 走 `manager = agent.tool_run_manager()` → `toolrun_adapter.py:57 Manager(None, policy)`；
- `atri_x/agent/toolrun_adapter.py:86-87`：`if state.registry is None: continue` —— 发现过的工具全部丢弃；
- 最终 `tools_for_session` 交给模型的 schema 为空 → **模型永远拿不到任何工具**（`shell` / `read_file` / `web_search` / `cron_write` …全部不可见），且没有任何报错，`register_all` 那边看起来一切正常。

**修复建议**（任选其一，建议 1+2 都做）：
1. `atri_x/agent/core.py` 的 `Agent.__init__` 按 Go `core.go:428-434` 补上构造期装配：
   ```python
   if opts.tool_registry is not None or opts.skills is not None:
       self.set_tool_runtime(opts.tool_registry, opts.skills)
   elif opts.tool_provider is not None:
       self.set_tool_provider(opts.tool_provider)
   self.set_tool_config(opts.tools_config)
   self.set_tool_tag_config(opts.tool_tags_path, opts.tool_tags)
   ```
2. `atri_x/app/runtime_agent.py:192` 改成 `install_agent_services(agent, opts={"registry": tool_runtime.registry, "skills": tool_runtime.skill_manager, "context_config": ..., "model_metadata": ..., "providers": ...})`，把 `bootstrap._wire_extra_host_fields` / `_configure_context` 真正要用的键传进去。
3. 加一条断言「装配后 `agent.tool_runtime.registry is not None`」的回归用例（用真实 `register_all` + 假文件管理器）。

**建议 Lead 立刻修：是。**

---

### P2-3 `_maybe()` 把工厂构造异常整体吞掉，缺件与「构造抛错」不可区分
- `atri_x/app/dependencies.py:262-276`：`except Exception: return None`（两处）
- 影响：某个工厂类存在但构造失败（例如 `DefaultPlatformFactory()` 需要配置）时，`Deps` 拿到 `None`，错误信息退化为 `runner.py` 的「必填校验」文案，真正原因（构造异常的 traceback）被丢弃，排障成本高
- 建议：至少把异常记进 `Dependencies` 的一个 `_degraded: list[tuple[str,str]]`，由 `runner` 打进启动日志；**建议排期修**

### P3-4（一行一条）
- `atri_x/app/service_marker.py` 未在 docstring 里声明 `service_marker_common.go` / `_unix.go` / `_windows.go` 三个出处，交叉检索会漏。
- `atri_x/app/doctor_checks.py` / `runtime_agent.py` / `runtime_hooks.py` / `runtime_support.py` / `tool_runtime.py` / `service_types.py` / `support.py` 的 docstring 出处声明不完整（多声明到 `doctor.go` / `runtime.go` 就停了），反向按 Go 文件检索时定位不到具体模块。

## 已核对无问题的高风险点

| 高风险点 | 怎么验的 | 结论 |
|---|---|---|
| 装配顺序 | 读 `atri_x/app/runner.py:120-240` 全流程 + `tests/test_app_runner.py:349` 的 `events` 断言（`["foundation","models","platforms","runtime","integrations"]`） | 与 Go `internal/app/runner.go` 的七段顺序一致；清理步骤 `CleanupStep` 逐段登记，`runtime` / `health server` / `health state` 都在 body 之后统一跑 |
| 假实现的签名是否与生产一致 | 逐个人工比对 `tests/test_app_runner.py:205-265` 的 `Foundation/Models/Platforms/Runtime/Integrations/Executor` 六个假实现与 `dependencies.py` 里对应真实工厂 | 只有 `Integrations.attach` 一处差参（已记为 P0-1）；其余五个假实现与真实工厂的 `build`/`run` 形参一致；`Executor.run(self, ctx, req)` 与 `platforms.py` 的 `DefaultPlatformExecutor.run` 一致 |
| `Runner.run` 的错误汇总 | 读 `runner.py:320-411` + `tests/test_app_runner.py:452-464` | body 异常与 cleanup 异常分别收集、合并成 `ExceptionGroup`，`KeyboardInterrupt/SystemExit` 不受影响 —— 与 Go 的 `errors.Join` 语义等价 |
| `platform_failure` 与心跳回调的 async 性 | `dependencies.py:211-213` 声明「`platform_failure` 是 async 可调用」；`runner.py:255-258` 用 `maybe_await` 调用 | 一致，无漏 await（`tmp_verify2_await.py atri_x/app` 的语句级丢弃数 = **0**） |
| 死 await / 漏 await | `python tmp_verify2_await.py atri_x/app` | `total stmt-level = 0`；`tmp_verify2_discard.py atri_x/app` 报的 `record_failure` / `success` / `record_circuit` / `send_header` / `run_ctx.cancel` / `task.cancel` 全是同步方法（逐条读过） |
| 默认值分歧（`None` vs 空容器） | 读 `dependencies.py:224-259`（`Dependencies` 全字段默认 `None`）+ `runner.py` 的 `_require_*` 校验 | `None` 一律被当作「未装配」并在 `_require_*` 处显式报错，没有「悄悄用空列表当未配置」的写法 |
| 三个 `Options` 死字段 | `tmp_verify2_options.py` + grep | 已记为 P0-2（`tool_registry`）与 `round2-agent.md` P1-5 / P2-6 |

## 收尾体检

```
python scripts/check_syntax.py   → 见 round2-summary.md
python scripts/check_imports.py  → modules=610 imported=610 import_failures=0 export_failures=0
```
（上一轮遗留的 `atri_x.tool.builtin.artist_extract` 导入失败**已修复**。）

## 本次用到的探针（均已删除）

`tmp_verify2_cover.py`、`tmp_verify2_app_attach.py`、`tmp_verify2_options.py`、`tmp_verify2_toolreg.py`、`tmp_verify2_await.py`、`tmp_verify2_discard.py`
