<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/llm/` — LLM 适配与协议

## 这个域负责什么

统一的消息/工具 schema、OpenAI 与 Responses 两条协议适配、流式分片与错误分类。

**不做什么**：不在这里拼提示词（那在 `agent/prompt*`）、不做重试策略的业务判断（在 `llm/breaker`）。

**入口**：`atri_x/llm/llm.py`（契约）、`atri_x/llm/openai/`（适配器）。

**配置段**：`[llm]` / `[models]`

## 易错点

- 新增模型能力要落到 `ProtocolCapabilities`，不要在各适配器里各自 if。
- 流式取消要传播到 HTTP 连接；否则取消后还会继续计费。
- 思考模式（reasoning）与工具链同时用时容易触发 400，本仓已有两轮真机热修。

## 模块清单（自动生成）

共 **15** 个模块，被测试覆盖 **13** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.llm` | LLM 抽象层。 | 11 | `_vision_fakes.py`、`agent_fakes.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_chat_anomaly.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_segments.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_app_llm_adapters.py`、`test_app_platforms.py`、`test_app_runtime_agent.py`、`test_background.py`、`test_builtin_tools3.py`、`test_completion.py`、`test_contextmgr.py`、`test_contextmgr_window_metadata.py`、`test_cron.py`、`test_cron_delivery.py`、`test_dsh_route.py`、`test_groupanalysis.py`、`test_gui_protocol.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_llm_breaker.py`、`test_llm_errors.py`、`test_llm_headers.py`、`test_llm_protocol.py`、`test_llm_tooltext.py`、`test_media_resolver.py`、`test_memory_notes.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_cli.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_time_context.py`、`test_tool_core.py`、`test_toolrun.py`、`test_toolrun_atriwisp_args.py`、`test_turn_manager.py`、`test_usage_attribution.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.llm.breaker` | Provider 级熔断器。 | 270 | `test_app_llm_adapters.py`、`test_llm_breaker.py` |
| `atri_x.llm.errors` | 结构化上游 API 失败与确定性失败分类。 | 348 | `test_agent_chat.py`、`test_llm_errors.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision.py`、`test_vision_privacy.py` |
| `atri_x.llm.llm` | LLM 抽象层类型。 | 567 | `_vision_fakes.py`、`test_agent_budget.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_title.py`、`test_background.py`、`test_builtin_tools3.py`、`test_completion.py`、`test_contextmgr.py`、`test_contextmgr_window_metadata.py`、`test_cron.py`、`test_cron_delivery.py`、`test_groupanalysis.py`、`test_gui_protocol.py`、`test_hook_builtin_control.py`、`test_hook_event.py`、`test_hook_match.py`、`test_hook_protocol.py`、`test_hook_rules.py`、`test_hook_runtime.py`、`test_media_resolver.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_platform_cli.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_tool_core.py`、`test_toolrun.py`、`test_toolrun_atriwisp_args.py`、`test_turn_manager.py`、`test_vision.py`、`test_vision_attributes.py`、`test_vision_prechat.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.llm.openai` | OpenAI-compatible 适配器包。 | 80 | `test_app_llm_adapters.py`、`test_llm_headers.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.openai.errors` | 上游失败响应 → ``APIError`` 的错误分类。 | 133 | — |
| `atri_x.llm.openai.openai` | OpenAI-compatible 适配器（Chat Completions）。 | 636 | `test_llm_headers.py`、`test_openai_adapter.py` |
| `atri_x.llm.openai.responses` | OpenAI Responses 协议适配器。 | 511 | `test_llm_headers.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.openai.stream` | SSE 行扫描与首包/空闲超时。 | 212 | `test_openai_adapter.py` |
| `atri_x.llm.openai.wire` | OpenAI-compatible 线上格式：请求信封构造、响应字段解析与日志摘要。 | 606 | `test_llm_headers.py`、`test_openai_adapter.py`、`test_openai_responses.py`、`test_vision_attributes.py` |
| `atri_x.llm.origin` | 「这段已有历史能不能交给目标模型」的唯一入口。 | 81 | `test_llm_protocol.py` |
| `atri_x.llm.protocol` | 协议能力矩阵的唯一入口。 | 91 | `test_agent_context_runtime.py`、`test_llm_protocol.py`、`test_openai_adapter.py` |
| `atri_x.llm.segment` | 消息段 helper。 | 41 | — |
| `atri_x.llm.tooltext` | 把「以正文形式泄漏的工具调用」还原成结构化调用。 | 329 | `test_agent_chat_anomaly.py`、`test_llm_tooltext.py` |
| `atri_x.llm.usage_sink` | 旁路 LLM 用量的统一出口。**Python 版新增**。 | 62 | `test_app_runtime_agent.py`、`test_usage_attribution.py` |

## 对应上游 Go（自动生成）

`internal/llm/breaker/breaker.go`、`internal/llm/errors.go`、`internal/llm/llm.go`、`internal/llm/openai/openai.go`、`internal/llm/openai/responses.go`、`internal/llm/origin.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 825 个 / 超线文件 79 个）。
