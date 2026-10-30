<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/app/` — 装配层

## 这个域负责什么

把配置、存储、平台、工具、Agent 装到一起，并管生命周期（启动/健康/退出）。

**不做什么**：不含业务逻辑；装配层只做「接线」，不替各域做决定。

**入口**：`atri_x/app/app.py`（入口与 Run）、`atri_x/app/runtime.py`、`atri_x/app/tool_runtime.py`。

**配置段**：`[app]` 与各服务装配段

## 易错点

- 装配顺序有依赖：工具运行时依赖平台适配器与存储，改顺序容易拿到半成品。
- 降级要显式：缺依赖时宁可少注册（模型看不到）也不要注册一个只会失败的工具。
- `doctor` 是端到端自检入口，新增服务顺手加一条检查。

## 模块清单（自动生成）

共 **34** 个模块，被测试覆盖 **29** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.app` | ``atri_x.app`` —— 装配层。 | 418 | `test_app_bootstrap.py`、`test_app_doctor_capabilities.py`、`test_app_health.py`、`test_app_llm_adapters.py`、`test_app_ops_plugins.py`、`test_app_platforms.py`、`test_app_runner.py`、`test_app_runtime_agent.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py`、`test_deploy_harness.py`、`test_llm_headers.py`、`test_main.py`、`test_member_profile.py`、`test_ops_health.py`、`test_packaging_tzdata.py`、`test_safety_integration.py`、`test_session_lifecycle.py`、`test_time_config.py` |
| `atri_x.app.app` | 应用入口：运行模式、启动选项与 ``Run``。 | 87 | `test_app_bootstrap.py`、`test_app_runner.py` |
| `atri_x.app.asr` | ASR（语音转写）服务装配。 | 175 | — |
| `atri_x.app.breaker_llm` | 熔断 / 兜底包装的 LLM 客户端。 | 374 | `test_app_llm_adapters.py` |
| `atri_x.app.cli_client` | CLI 远程客户端入口：把 CLI 平台配置成"连远端服务"的瘦客户端。 | 130 | `test_app_platforms.py` |
| `atri_x.app.config_check` | ``atri-x config check``：加载 app.toml 并给出人类可读的摘要与告警。 | 250 | `test_app_platforms.py`、`test_time_config.py` |
| `atri_x.app.cron` | Cron 启动编排：异步准备 cron 任务表。 | 115 | `test_app_bootstrap.py` |
| `atri_x.app.dependencies` | 装配层依赖注入点：各阶段请求/组件与 :class:`Dependencies`。 | 333 | `test_app_platforms.py`、`test_app_runner.py` |
| `atri_x.app.doctor` | 部署验收（doctor）：配置、本地依赖、平台连通性与端到端往返。 | 696 | `test_app_doctor_capabilities.py`、`test_app_platforms.py` |
| `atri_x.app.doctor_checks` | Doctor 的单项检查实现。 | 502 | `test_app_platforms.py` |
| `atri_x.app.environment` | 启动环境：运行模式解析、启动剖析器、平台模式映射。 | 226 | `test_app_bootstrap.py` |
| `atri_x.app.foundation` | 装配根：配置、日志、主存储、聊天历史、维护服务与 cron 管理器。 | 395 | `test_app_bootstrap.py` |
| `atri_x.app.health` | 健康与运维 HTTP 端点的启动装配。 | 274 | `test_app_health.py` |
| `atri_x.app.health_handler` | 平台消息处理器包装：把每条消息的结果记进健康状态。 | 56 | `test_app_health.py` |
| `atri_x.app.health_llm` | 健康统计包装的 LLM 客户端。 | 131 | `test_app_llm_adapters.py` |
| `atri_x.app.image_rewriter` | ``image_generate`` 的可选 LLM 改写通道。 | 267 | `test_app_platforms.py` |
| `atri_x.app.image_to_prompt` | ``image_to_prompt`` 工具共用的视觉引擎装配。 | 102 | — |
| `atri_x.app.integrations` | 集成阶段：把平台运行时接到 Agent 上，并按需拉起 atrinis。 | 186 | `test_app_platforms.py`、`test_app_runner.py` |
| `atri_x.app.models` | LLM 客户端装配：provider → 适配器（含协议路由、健康包装、熔断包装）。 | 323 | `test_app_llm_adapters.py`、`test_llm_headers.py` |
| `atri_x.app.naming` | 会话命名事件的日志/审计记录器。 | 110 | `test_session_lifecycle.py` |
| `atri_x.app.ops_health` | ``/metrics`` 与 ``/diagnostics`` 的载荷收集，以及两个延迟接线的 HTTP 组件。 | 619 | `test_app_health.py`、`test_app_ops_plugins.py`、`test_app_runtime_agent.py`、`test_member_profile.py`、`test_ops_health.py` |
| `atri_x.app.platform_caller` | 按平台名把运行时解析成"平台 API 调用器"。 | 71 | `test_app_platforms.py` |
| `atri_x.app.platforms` | 平台装配与运行循环。 | 692 | `test_app_platforms.py`、`test_deploy_harness.py` |
| `atri_x.app.runner` | 装配运行器：按固定顺序装配各阶段并保证逆序清理。 | 543 | `test_app_ops_plugins.py`、`test_app_runner.py` |
| `atri_x.app.runtime` | 运行期装配主链：媒体中心 → 工具运行时 → Hook → Agent → 生命周期。 | 620 | `test_app_runner.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py` |
| `atri_x.app.runtime_agent` | Agent 装配。 | 711 | `test_app_runtime_agent.py`、`test_member_profile.py`、`test_safety_integration.py` |
| `atri_x.app.runtime_hooks` | Hook 控制面装配。 | 120 | — |
| `atri_x.app.runtime_support` | 运行期装配的共享助手：审计入口、凭据解析、cron 服务、Hook 生命周期。 | 319 | — |
| `atri_x.app.service_marker` | 服务实例标记：防止同一台机器上跑起第二个 ``atri-x`` service。 | 226 | `test_app_bootstrap.py` |
| `atri_x.app.service_types` | app 内部使用的服务侧数据类型。 | 94 | `test_app_platforms.py` |
| `atri_x.app.support` | 装配层的叶子级共享助手。 | 71 | `test_app_runner.py` |
| `atri_x.app.time_check` | ``python -m atri_x.app.time_check``：时区数据与解析探针。 | 113 | `test_packaging_tzdata.py` |
| `atri_x.app.tool_runtime` | 内置工具运行时的 app 侧装配。 | 560 | `test_app_runtime_agent.py`、`test_app_runtime_resident.py`、`test_app_tool_runtime_wiring.py`、`test_member_profile.py` |
| `atri_x.app.vision` | 视觉服务装配：把共享 ``vision.Service`` 接到 Agent 的聊天视觉兜底接口。 | 261 | — |

## 对应上游 Go（自动生成）

`internal/app/app.go`、`internal/app/asr.go`、`internal/app/breaker_llm.go`、`internal/app/cli_client.go`、`internal/app/config_check.go`、`internal/app/cron.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 796 个 / 超线文件 79 个）。
