<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/hook/` — Hook 与插件

## 这个域负责什么

事件钩子：注册、匹配规则、超时与失败策略。

**不做什么**：Hook 不直接改 Agent 状态；它通过返回值影响流程。

**入口**：`atri_x/hook/manager.py`、`atri_x/hook/match.py`、`atri_x/hook/process.py`。

**配置段**：`hooks.toml`

## 易错点

- Hook 失败默认不阻断主流程，除非规则显式声明（否则一个坏插件会让机器人静默失聪）。
- `hooks.toml` 的热加载边界与 `state.toml` 不同，改完要重启。

## 模块清单（自动生成）

共 **39** 个模块，被测试覆盖 **21** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.hook` | Hook 与插件。 | 20 | `agent_commands_fakes.py`、`test_agent_chat.py`、`test_app_platforms.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_output.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_safety_integration.py` |
| `atri_x.hook.builtin` | 内置 Hook 插件：规则引擎、天使记忆、自我学习、出站历史。 | 30 | `test_hook_builtin_control.py` |
| `atri_x.hook.builtin.angel_memory` | 内置 Hook：按平台/会话召回长期记忆，追加为临时 system 上下文。 | 56 | — |
| `atri_x.hook.builtin.register` | 内置 Hook 插件的注册入口。 | 153 | — |
| `atri_x.hook.builtin.self_learning` | 内置 Hook：群语料观察与表达/黑话上下文注入。 | 90 | — |
| `atri_x.hook.control` | Hook 控制面：给命令层用的列表 / 热重载 / 启停。 | 185 | `test_hook_builtin_control.py` |
| `atri_x.hook.event` | Hook 事件点、事件 payload 与 Handler 基础类型。 | 347 | `test_app_platforms.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py` |
| `atri_x.hook.manager` | 普通 Hook 的注册、排序、执行与原子快照替换。 | 733 | `agent_commands_fakes.py`、`test_agent_chat.py`、`test_hook_builtin_control.py`、`test_hook_manager.py`、`test_hook_rules.py` |
| `atri_x.hook.match` | Hook 条件匹配、字段取值与模板值。 | 560 | `test_hook_builtin_control.py`、`test_hook_manager.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py` |
| `atri_x.hook.media` | 宿主 Media 能力（Hook 进程内的 Go 插件用）。 | 57 | — |
| `atri_x.hook.output` | Hook 输出的线格式。 | 21 | `test_hook_output.py`、`test_hook_protocol.py`、`test_hook_rules.py` |
| `atri_x.hook.output.jsonstrict` | 严格 JSON 解码：拒绝未知字段、拒绝尾随值。 | 217 | `test_hook_output.py` |
| `atri_x.hook.output.message` | 进程 Hook 替换消息内容的线格式（``message.segments``）。 | 229 | — |
| `atri_x.hook.output.output` | Hook 输出的线格式：``outputs`` 段的解码与构造。 | 428 | — |
| `atri_x.hook.process` | 进程 Hook 继承的环境。 | 29 | — |
| `atri_x.hook.protocol` | ``hook.v2`` 进程间协议。 | 19 | `test_hook_protocol.py`、`test_hook_runtime.py` |
| `atri_x.hook.protocol.eventjson` | ``hook.Event`` 与其负载的 JSON 投影。 | 659 | — |
| `atri_x.hook.protocol.frames` | ``hook.v2`` JSON Lines 协议帧。 | 330 | `test_hook_runtime.py` |
| `atri_x.hook.protocol.stream` | ``hook.v2`` 的按行读取（带帧上限）。 | 116 | — |
| `atri_x.hook.rules` | ``hooks.toml`` 规则引擎。 | 88 | `test_hook_rules.py` |
| `atri_x.hook.rules.action` | 规则动作的执行：改文本、发消息、跑工具、调插件。 | 439 | — |
| `atri_x.hook.rules.config` | ``hooks.toml`` 与插件 ``hook.toml`` 的装载。 | 686 | `test_hook_rules.py` |
| `atri_x.hook.rules.detail` | ``/hooks`` 列表里展示的规则详情文本。 | 124 | `test_hook_rules.py` |
| `atri_x.hook.rules.exec` | ``exec`` 动作：跑一次 hook.v2 一次性插件。 | 746 | — |
| `atri_x.hook.rules.exec_stderr` | ``exec`` 动作的进程辅助：stderr 尾巴、错误包装、cwd 解析。 | 261 | — |
| `atri_x.hook.rules.rule` | 规则的校验、角色条件展开与执行。 | 256 | `test_hook_rules.py` |
| `atri_x.hook.rules.rules` | 规则模块：装载 ``hooks.toml`` 并把每条规则注册成 Hook。 | 190 | `test_hook_rules.py` |
| `atri_x.hook.rules.toml_error` | ``hooks.toml`` 的读取与详细错误信息。 | 514 | `test_hook_rules.py` |
| `atri_x.hook.rules.types` | ``hooks.toml`` 的配置类型。 | 239 | `test_hook_rules.py` |
| `atri_x.hook.runtime` | 有状态 Hook 运行时（hook.v2 进程）。 | 113 | `test_hook_builtin_control.py`、`test_hook_runtime.py` |
| `atri_x.hook.runtime.config` | 有状态 Hook 进程的配置、状态与运行期选项。 | 253 | — |
| `atri_x.hook.runtime.manager` | 有状态 Hook 运行时的协调者：应用配置、启停 worker、路由事件。 | 528 | — |
| `atri_x.hook.runtime.media` | ``media.*`` RPC：把宿主 MediaAPI 暴露给进程 Hook。 | 356 | — |
| `atri_x.hook.runtime.protocol` | 有状态 Hook 进程的协议负载（``system.init`` / ``event.handle``）。 | 209 | — |
| `atri_x.hook.runtime.routes` | Hook 路由：租约、等待态、单飞调用与工具上下文令牌。 | 355 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.shared` | Hook 共享的有界 JSON 协调存储。 | 382 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.tool_bridge` | 进程 Hook 发来的请求：``tool.call`` / ``hooks.reload`` / ``media.*`` / ``shared.*``。 | 307 | `test_hook_runtime.py` |
| `atri_x.hook.runtime.transport` | worker 的协议传输层：读写帧、请求/响应配对、进程强杀。 | 279 | — |
| `atri_x.hook.runtime.worker` | 一个有状态 Hook 进程（hook.v2 worker）。 | 714 | — |

## 对应上游 Go（自动生成）

`internal/hook/builtin/angel_memory.go`、`internal/hook/builtin/register.go`、`internal/hook/builtin/self_learning.go`、`internal/hook/control/service.go`、`internal/hook/event.go`、`internal/hook/manager.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 796 个 / 超线文件 79 个）。
