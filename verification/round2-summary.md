# 第二轮对抗性验证总结（agent / app / platform / tool-builtin）

- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（Go）
- 本仓（只读验证对象）：本仓库工作树（Python 3.14.7，纯标准库）
- 契约出处：`PORTING.md` §1/§2/§3/§3.1/§4/§7/§8
- 方法论：沿用第一轮（`verification/README.md`）的文件级覆盖表 → 符号级核对 → **能跑出错误行为的最小探针**；
  范围换成**第一轮未覆盖的四个集群**：`atri_x/agent/`、`atri_x/app/`、`atri_x/platform/`、`atri_x/tool/builtin/`

## 报告索引

| 报告 | 集群 | 一句话结论 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|---|
| [round2-agent.md](round2-agent.md) | `atri_x/agent/**` ←→ `internal/agent/**`（100 个 Go 生产文件，文件级覆盖无缺口） | 并行三组写的 mixin 主体质量不错，但**两处装配缺口让"装配后的 Agent"完全不能聊天**：`character_directive`/`turn_override` 四个方法从未绑到 `Agent`（每轮 AttributeError），`group_tool_allowlist`/`filter_group_feature_schemas`/`group_default_model_selection` 三个转发方法诊断成**自递归**（每轮 RecursionError）。 | 2 | 3 | 1 | 3 |
| [round2-app.md](round2-app.md) | `atri_x/app/**` ←→ `internal/app/**`（27 个 Go 生产文件，文件级覆盖无缺口） | 装配顺序与清理/错误汇总忠实，但 `Runner` 调用集成工厂**少传一个参数**（启动必失败），且传给 Agent 的 `tool_registry` 全仓无人读取（工具注册表静默丢失）。 | 2 | 0 | 1 | 2 |
| [round2-platform.md](round2-platform.md) | `atri_x/platform/**` ←→ `internal/platform/**`（43 个 Go 生产文件，文件级覆盖无缺口） | 符号、Protocol 一致性（含 async 性）、脱敏契约、发送失败语义全部核对干净；唯一实质分歧是 **CLI/远程 CLI 把 Go 的同步 `StartStream` 写成了 `async def`**，与同步调用点冲突（与 agent 报告 P1-3 同一条，不重复计数）。 | 0 | 0 | 0 | 3 |
| [round2-builtin.md](round2-builtin.md) | `atri_x/tool/builtin/**` ←→ `internal/tool/builtin/**`（38 个 Go 生产文件，文件级覆盖无缺口） | 42 个工具名与 Go **逐字一致、零遗漏**，安全相关工具（shell_risk/shell_sandbox/file_guard/workspace）符号无缺口；但 `cron_query` / `cron_write` 两个工具**全部失效**（漏 await），其中 `delete`/`disable` 还会**假报成功**。 | 1 | 0 | 0 | 3 |
| **合计** | | | **5** | **3** | **2** | **11** |

## P0 / P1 速查

| 级别 | 标题 | Python 位置 | Go 位置 | 一句话影响 | 立即修 |
|---|---|---|---|---|---|
| P0 | `@char:` 角色声明与 `@turn:` 单回合覆盖完全失效（4 个方法从未绑到 `Agent`，第 267 行**无条件**执行） | `atri_x/agent/input.py:267,271,277,283`；实现 `character_directive.py:48,98` / `turn_override.py:93,147` | `internal/agent/character_directive.go:22,66`、`turn_override.go:54,109`、调用点 `input.go:165-176` | 每条正常聊天消息都抛 `AttributeError: 'Agent' object has no attribute 'apply_character_directives'`，被 `message.py:77` 吞成「请求失败」，用户看不到回复 | **是** |
| P0 | `group_tool_allowlist` / `filter_group_feature_schemas` / `group_default_model_selection` 自递归 | `atri_x/agent/core.py:587-627`（配合 `seams.py:57-67 _svc`、`bootstrap.py:412-417`） | `internal/agent/tool_auth.go:53,138`、`group_policy.go:865`（Go 里是直接实现，**不经** `Deps.GroupPolicy`） | 装配后 `deps.group_policy is agent` → `hasattr` 恒真 → `RecursionError`；每个回合在构建工具 schema / 选模型时崩 | **是** |
| P0 | `Runner` 调 `integrations.attach` 少传 `ctx` | `atri_x/app/runner.py:193`（唯一调用点） | `internal/app/integrations.go:16` `Attach(ctx, req)` | 应用启动在任何真实 `Deps` 下都停在集成阶段：`TypeError: attach() missing 1 required positional argument: 'req'` | **是** |
| P0 | `Options.tool_registry` 从未被消费 → 工具注册表不进 Agent | `atri_x/app/runtime_agent.py:148`（传）↔ `atri_x/agent/core.py:244`（不读）；`bootstrap.py:493-496` 需要 `opts["registry"]` 而 `runtime_agent.py:192` 未传 `opts=` | `internal/agent/core.go:428-434`（构造期 `SetToolRuntime/SetToolProvider/SetToolConfig/SetToolTagConfig`） | `tool_runtime.registry` 恒为 `None`，`tool_schemas.py:75-87` → `Manager(None, ...)`，`tools_for_session` 恒空 → **模型拿不到任何工具**，且无任何报错 | **是** |
| P0 | `cron_query` / `cron_write` 两个工具全部失效，`delete`/`disable` 假报成功 | `atri_x/tool/builtin/cron.py:234,235,351,354,389,438`（`_create`/`_update` 是同步函数） | `internal/cron/service.go:143,166,234,251,268,288`（**同步**）；工具调用点 `internal/tool/builtin/cron.go:116,122,165,170,194` | create/update/query 抛 `TypeError`；delete/disable 回 `{"ok":true}` 但**任务没被删/停**，cron 继续触发 | **是** |
| P1 | CLI / 远程 CLI 的 `start_stream` 是 `async`，与同步契约与同步调用点冲突 | `atri_x/platform/cli/adapter.py:218`、`cli/remote_server.py:362`（async）↔ `atri_x/agent/turn_output.py:245-262`（sync）；契约 `delivery.py:366-369` | `internal/platform/cli/cli.go:121`、`remote_server.go:195`、`telegram/stream.go:26`（全同步）；`internal/delivery/delivery.go:258` | `start_message_stream` 返回协程被当成 `MessageStream`；首个增量 `stream.append` → `AttributeError` → `chat_llm.py:427` 包成 `RuntimeError` → **本地 CLI 每轮失败**；Telegram（同步实现）反而正常 | **是** |
| P1 | 运行状态永不推送（`set_runtime_status` 是 async，调用点不 await 且被 `except` 吞掉） | `atri_x/agent/status.py:66-71` ↔ `atri_x/platform/cli/adapter.py:226`、`cli/remote_server.py:346` | `internal/agent/status.go:11-13,36-38`（同步 `_ = sender.SetRuntimeStatus(...)`） | CLI 下 TUI 状态栏的 provider/model/阶段/用量**永不更新**；`except Exception: pass` 看不到协程创建成功，连告警都没有 | **是** |
| P1 | `naming_notifier` 是死字段：会话命名事件永不发出 | `atri_x/agent/options.py:59`（声明）、`atri_x/app/runtime_agent.py:132`（传）、`atri_x/agent/core.py:222-224`（第 4 实参写死 `None`） | `internal/agent/core.go:287,298`；`internal/app/runtime.go:528` | `NamingScheduled/Completed/Failed` 一次都不发，审计与日志看不到命名活动（与第一轮已修的 naming P0 是**两个独立缺口**） | **是** |

## 跨集群共性（本轮的系统性风险）

1. **「Go 同步方法被移植成 `async def`，调用点没跟着改」是本轮最高产的缺陷形态。**
   本轮 8 条 P0/P1 里有 3 条同源：cron 的 6 个 Service 方法（P0）、`set_runtime_status`（P1）、`start_stream`（P1）。
   共同点：**服务侧 async 化是对的（或至少无害），调用侧照抄了 Go 的同步调用形状**；Python 不会报错，只把协程丢掉，`except Exception` 也抓不到。
   建议 Lead 做一次全仓机器化扫描（本轮探针 `tmp_verify2_await.py` 的「async 名字被当语句调用未 await」一节 + `tmp_verify2_discard.py` 的「async 函数里的裸调用」一节即可复用），再补一条 CI 规则。

2. **契约靠鸭子类型，缺件静默跳过；`hasattr` / `isinstance` 判定都不检查 async 性。**
   - `AgentSeams._svc`（`seams.py:57-67`）用 `hasattr(service, name)` 决定「服务在不在」，被 `bootstrap.py:412-417` 把 `Agent` 自己写进 `deps.group_policy` 后恒为真 → 自递归。
   - `runtime_status_sender`（`status.py:80-88`）用 `hasattr(..., "set_runtime_status")` → 看不见「实现是 async」。
   - `bootstrap._guard`（`bootstrap.py:255-263`）把 `ImportError` 与 `Exception` 统一收成 warning，`dependencies._maybe`（`dependencies.py:262-276`）把工厂构造异常吞成 `None`。
   - 统计（AST，`except Exception/BaseException` 且块体只有 `pass`/`return None`）：`agent` 185/50、`app` 92/16、`platform` 80/10、`builtin` 57/2（**宽 except 数 / 完全吞掉数**）。
   本轮 P0-1（app）、P1-4（agent）、P1-5（agent）都直接由这类静默降级掩盖。

3. **「测试用假实现 / 只测模块级函数」，接缝无人覆盖 —— 与第一轮 session 的 P0 完全同形。**
   - `tests/test_agent_directives.py` 全程用 `_DirectiveHost` 假宿主**直接调用模块级函数**（`:223/:239/:343`），从不经过 `Agent` 的方法解析 → 掩盖 agent P0-1。
   - `tests/test_app_runner.py:240-246` 的假 `Integrations.attach(self, req)` 比生产 `DefaultIntegrationFactory.attach(self, ctx, req)` **少一个形参** → 掩盖 app P0-1。
   - `atri_x/tool/builtin/cron.py` 的两个工具没有「用真实 `cron.Service` 走一遍 `call()`」的用例，而 `atri_x/cron/**` 自身的用例全绿 → 掩盖 builtin P0-1。
   建议：凡「模块级函数 + Agent/门面方法」成对存在的模块，回归用例必须至少有一条走**真实宿主类**；凡跨模块服务，用例必须用**真实服务实现 + 假存储**，不要用与服务同形的假对象。

4. **移植时新增的「转发层 / 服务分派层」引入了 Go 没有的间接。**
   `Agent.group_*` 三个方法是 Python 新增的「优先用服务」分派（Go 里是直接实现），`_svc` 的 `deps` 回退也是 Python 新增的。两者叠加产生了自递归。凡 Python 新增于 Go 的间接层，都要显式排除「服务就是宿主自己」的情形。

5. **`Options` 字段「传了但没人读」是装配层的通用暗坑。**
   机器化核对（`tmp_verify2_options.py`，`Options` 共 65 个字段）：
   ```
   === 在 atri_x/agent/** 里完全没有出现的 Options 字段 ===
     - context_overflow
     - naming_notifier
     - tool_registry
   === 其中在 atri_x/app/** 里真的被赋值的 ===
     ! context_overflow / naming_notifier / tool_registry  均在 atri_x/app/runtime_agent.py
   ```
   建议把「`Options` 的每个字段在 `atri_x/agent/**` 里至少被引用一次」做成 `scripts/check_integration.py` 的一条规则。

## 与第一轮的关系

- **不重复**：第一轮的四个集群（`contextmgr` / `toolrun` / `session` / `storage`）本轮未重验；第一轮已公开的有意偏差（统一微秒、JSON 不转义 `<>&`、`re` 替代 RE2、单文件拆分、TUI 用 ANSI 行式渲染替代 bubbletea、`--health-probe` 新增、命名风格）本轮**不计为发现**。
- **第一轮遗留的一处已在两轮之间修好**：`python scripts/check_imports.py` 从 `modules=605 imported=604 import_failures=1`（`atri_x.tool.builtin.artist_extract` 找不到 `MetadataOriginArchive`）变为 `modules=610 imported=610 import_failures=0 export_failures=0`。
- **第一轮 P0 的下游仍在**：`naming_notifier` 未接线（本轮 agent P1-5）说明即便 `TitleGenerator` 修好，命名通知链路仍是断的。

## 收尾体检（验证者跑过，确认自己没把仓库弄脏）

```
python scripts/check_syntax.py
   → scanned=758  syntax_errors=0  encoding_errors=0  bom=0 crlf=0
     syntax_warnings=0  damaged_or_suspect=0
python scripts/check_imports.py
   → modules=611  imported=611  import_failures=0  export_failures=0
python scripts/check_integration.py
   → 共 73 项，失败 0 项   （exit=0）
```

- 本轮只写了 `verification/round2-*.md` 五个文件，未改动 `atri_x/**`、`tests/**`、`scripts/**`、`deploy/**`、`PORTING.md`，未 `git commit`。
- 全部临时探针建在仓库根 `tmp_verify2_*.py` / `tmp_verify2_*.txt`（`.gitignore` 已覆盖 `tmp_*`），验证结束后已删除。

## 局限（本次没能验证的部分）

- **没有 Go 工具链**（`go version` → CommandNotFound），Go 侧结论一律来自静态读源码；无法用差分运行证明「同输入同输出」。
- **未逐行读完的控制流**：`atri_x/agent/chat_turn.py`（607 行）的回合状态机后半、`group_policy_rules.py`、`cron.py`(838)、`toolrun_adapter.py`(795)、`platform/cli/tui/model.py`(545) 与 `tui/program.py`(487) 的 TUI 事件循环 —— 这些文件只做了符号/签名层面的核对。
- **未验证的运行时效果**：QQ OneBot / QQ 官方 / Telegram 三个真实适配器没有连过真实服务端；`platform/cli/tui` 的终端行为只做了符号与调用点核对，没有在真实 TTY 下跑过。
- **回调式 async**：`asyncio.get_running_loop().create_task(...)` 传进去的协程是否都被正确消费，只对 `input.py:317` 与 `status.py` 两条路径做了核对，其余（如 `media` / `hook/runtime`）未全量追踪。
- **P1-4 的最终效果**：`set_runtime_status` 未 await 已用探针证明「协程被丢弃」，但「TUI 状态栏具体少了哪些字段」是在真实 TTY 下才能确认的，本次只从 `cli/adapter.py:226-233`（唯一实现路径 `send_tui_message(tui.TuiStatusMsg(snapshot), ...)`）推断。
- **P2-6（`context_overflow`）的严重度取舍**：构造期不再 seed 已确认；但 `state_watch` 路径（`model_state.py:258-260`）是否在启动时完成等价加载，本次未证明时序等价，故按 P2 而非 P1 记录。
