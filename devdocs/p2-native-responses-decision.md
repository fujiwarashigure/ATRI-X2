# P2 换基决策材料（原生 Responses 协议）

> 移植状态：见 devdocs/porting-status.md 待核对

本文是 `upstream-sync-todo.md` §5（P2）决策点的取证材料，**不写代码、不改行为**，只为让"要不要换基、怎么换"这个决策可以基于事实做。
结论已回写到 `upstream-sync-todo.md` §5（`[x]`），决策与实施结果见本文 §6。

标注保留的是**上游有、Python 版有意不做**的那部分：服务端续链 `previous_response_id`、原生压缩、reasoning 加密载荷存回、`additional_tools` 增量工具定义，以及 `native_dialogue` / `native_material_roots` 两条迁移。这不是"待核对"，是路线 D 下的明确取舍；将来换基时的入口见 §6 末。

## 1. 现状：fork 已经有 Responses 适配器

| 事实 | 位置 / 规模 |
| --- | --- |
| Responses 协议适配器已存在 | `atri_x/llm/openai/responses.py`，540 行 |
| Chat Completions 适配器 | `atri_x/llm/openai/openai.py`，1260 行 |
| 协议选择入口 | `[providers.<name>].api_mode`（`chat` 默认 / `response`）与 `[providers.<name>.model_configs.<model>].api_mode` |
| 已实现能力 | system 段合并为 `instructions`、工具调用/结果转 `function_call` / `function_call_output` item、工具定义扁平化、`store = false`、流式事件归一化（`response.output_text.delta`、reasoning 文本、`function_call_arguments.*`、`response.completed` / `failed` / `error`）、usage 与错误分类（含 `vision_unsupported`） |
| 复用情况 | 协议翻译层 + **复用现有 chat 会话循环**（`atri_x/agent/chat*.py`），不新增会话语义 |

**逐项取证（本次核对）**：

- `atri_x/llm/openai/responses.py` 顶部注释明确写着适配器固定发送 `store=false`，理由正是"ATRI-X 重放完整历史、从不读回已存响应"——这句话直接说明**没有服务端续链**。
- 全仓库 `atri_x/llm/openai/*.py` 里 `previous_response_id` 与 `additional_tools` 出现次数为 **0**，即上游承诺的两项核心收益在 fork 完全不存在。
- fork 的上下文压缩是客户端的：`atri_x/agent/context_compact.py`（`compactSession` / `compactMessages`）+ `atri_x/agent/context_runtime.py` + `atri_x/contextmgr/` 包，与服务端原生压缩无关。

**结论**：fork 的 Responses 支持是"协议翻译 + 复用会话循环"，不是"原生 Responses 内核"。这是一条重要的分界：能用 Responses 端点，但用不到服务端的会话级能力。

## 2. 上游路线 B 承诺的收益 vs fork 现状

| 目标收益 | fork 现状 | 差距 |
| --- | --- | --- |
| 服务端续链 `previous_response_id` | 无：每轮把完整历史重发给模型 | 需要把"会话历史 → 增量引用"改造成存储 + 生命周期，并处理服务端侧状态过期/丢失的兜底 |
| 原生压缩 | 无：fork 用自家的客户端压缩（`atri_x/agent/context_compact.py` 的 `compactSession` / `compactMessages` + `atri_x/contextmgr`） | 需要分派新旧两套压缩路径，并保证压缩后 `previous_response_id` 链不断 |
| reasoning 加密内容 | 部分：reasoning 文本已归一化进 `llm.StreamChunk` | 需要存回服务端要求的加密 reasoning 载荷，属于存储schema变化 |
| `additional_tools` 增量工具定义 | 无：每轮重发完整工具定义 | 需要工具定义版本化与失效判定 |
| 原生 checkpoint | 无：fork 用自家 `turn` / `request` 状态机 | 需要把 checkpoint 语义映射到现有 turn 生命周期，或并存两套 |

## 3. 换基的真实改动面（待办 §5 已列出，此处逐项确认）

- **LLM 分层**：fork 的 `atri_x/llm/openai` 在上游已删除 → 换基意味着 fork 这套适配器要重写或删掉。
- **agent 路由**：会替换 fork 的 `atri_x/agent/chat.py` / `chat_llm.py` / `chat_tools.py` / `core.py` —— 这四个文件是 fork 定制层（后台接管安全点、工具白名单、预算预占、pending 输入合并、群策略）最集中的地方。
- **压缩**：见上表。
- **存储**：上游带 3 个新迁移（`session_llm_origin`、`native_dialogue`、`native_material_roots`）→ fork 的 SQLite schema 与迁移序列需要跟进，且迁移必须加法式（待办 §6.2）。
- **模型来源与工具可用性**：`ListModels` / `ListModelMetadata` / 视觉能力声明（`vision`）在原生模式下语义不同。

## 4. 三种路线的成本与风险对比

| 路线 | 内容 | 成本 | 风险 |
| --- | --- | --- | --- |
| **A. 保持现状** | 继续用 `responses.go` 协议翻译层；新增能力时通过接口查询协议（工具可用性、压缩分派、存储协议来源）留接缝 | 0（本文） | 拿不到服务端续链/原生压缩；上下文成本继续由客户端承担 |
| **B. 在 fork 现有 agent 上换基** | 直接把 agent 会话循环换成原生 Responses 语义 | 20–35 人日（待办估算） | **最高**：会长期维护两套会话语义（fork 定制层 vs 原生意义），且 fork 的 P1#4 后台接管、预算预占、pending 输入都要在新语义下重新实现 |
| **C. 另建 `upstream-track` 分支换基** | 先把 fork 定制层插件化/接口化，再在新分支换基 | B 的成本 + 插件化前期投入 | 中：定制层与内核解耦后，两边都能独立演进；代价是前期要把耦合点找出来 |
| **D. 按需补接口（本文推荐）** | 不换基，但把"将来可能换"的接缝显式化：协议能力查询、压缩分派点、存储协议来源字段 | 1–2 人日 | 低：不改变现有行为；只是把隐式假设变成显式接口，换基时改动面收敛 |

## 5. 结论与建议

**建议取 D（按需补接口），暂不换基。** 理由：

1. **收益与成本不对等**：路线 B 的 5 项收益里，服务端续链与原生压缩是省 token 的，但 fork 当前上下文压缩、预算账本、后台接管都已按客户端语义实现并有测试；换基要把这些重做一遍，收益却是"省下的 token"，而 20–35 人日的成本本身就远超短期省下的量。
2. **风险集中在 fork 最定制的部分**：会替换的 `atri_x/agent/chat.py` / `chat_llm.py` / `chat_tools.py` / `core.py` 正是 P0/P1 这几轮刚加固过的地方（后台接管安全点、工具白名单、事务化字段更新、预载事实区分）。在同一个文件上换内核，等于把刚建立的回归保护推倒重来。
3. **路线 C 是真正的工程解，但需要先做前提工作**：把 fork 定制层插件化/接口化本身就是一项独立、可验收、低风险的工作，而且无论最后换不换基都有价值。
4. **D 是 C 的第一步**：如果将来要走 C，从 D 开始不会浪费——接口化之后换基的改动面会收敛到适配器与内核，而不是散在 agent 各处。

**若最终决定走 B 或 C，建议的最小前置顺序**：

1. 先做 D（协议能力查询接口 + 压缩分派点 + 存储协议来源字段），把隐式假设显式化，并补测试。
2. 再评估 `atri_x/llm/openai` 的拆分边界（协议翻译 vs 会话语义），确认哪些能被原生内核替代。
3. 只有在第 2 步确认"定制层已与内核解耦"之后，才动 `atri_x/agent/chat*.py`。

**不建议**在 fork 现有 agent 上直接加协议分支（路线 B 的直接形态）：那会长期维护两套会话语义，且与本仓库"不考虑历史兼容到让代码变复杂"的 AGENTS.md 原则冲突。

## 6. 决策与实施结果（本轮）

**用户决策：取路线 D（按需补接口），不做换基。** 三个接缝已实施，均不改变现有行为：

| 接缝 | 落点 | 关键点 |
| --- | --- | --- |
| 协议能力查询 | `atri_x/llm/protocol.py` + `openai` 两个适配器 + `atri_x/app/models.py` 的 `protocolRouter` | `ProtocolCapabilitiesOf(client, model)` 是唯一入口；未实现查询接口的客户端得到最保守答案（Chat Completions、服务端能力全 false）；router **按模型**回答，因为一个 provider 可以按模型混用两种协议。Responses 适配器如实报告"服务端能力全 false"——本文件第 1 节取证说明 fork 的 Responses 只是协议翻译层，虚报能力会让上层按不存在的服务端状态设计 |
| 压缩分派点 | `atri_x/contextmgr` 的 `Compactor` / `CompactionChoice` + `contextRuntimeState.compactorFor` | 目前唯一后端是客户端 `client_summary`；协议声称支持服务端原生压缩而 fork 没有实现时显式回退，并写审计 `event=context_compaction_backend`（`backend` / `reason`），不静默 |
| 存储协议来源 | 会话 metadata 的 `llm_origin`（`{protocol, provider, model}`）+ `Agent.recordLLMOrigin` | 上游的 `session_llm_origin` 迁移在 fork 里用加法式 metadata 字段承载，避免为一个诊断字段改 schema；值未变时不写事务（每会话只在换协议/换模型时写一次） |

**本轮明确未做**：`previous_response_id` 服务端续链、原生压缩实现、reasoning 加密载荷存回、`additional_tools` 增量工具定义、`native_dialogue` / `native_material_roots` 迁移、`atri_x/llm/openai` 的"协议翻译 vs 会话语义"拆分（第 5 节建议的前置顺序第 2 步）。

**将来换基时的入口**：协议能力在 `ProtocolCapabilitiesFor` 一处声明（新增服务端能力只需让它返回 true 并补上实现），压缩后端在 `contextRuntimeState.compactorFor` 一处分派，会话来源在 `llm_origin` 一处可查。
