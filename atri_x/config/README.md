<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/config/` — 配置与运行时状态

## 这个域负责什么

`app.toml` / `services.toml` / `hooks.toml` / `state.toml` 的加载、校验、热加载与默认资产。

**不做什么**：不放业务默认值以外的逻辑；配置项本身不产生行为。

**入口**：`atri_x/config/loader.py`（加载）、`atri_x/config/app_sections.py`（分段定义）、`atri_x/config/assets.py`（默认资产）。

**配置段**：`[app]` / `[security]` / `[connect_notice]` 等

## 易错点

- 新增配置键要同时进默认资产与文档（`docs/configuration.md`），否则用户看不到。
- 热加载只对 `state.toml` 生效；改 `app.toml` 需要重启。
- `app_sections.py` 已经超过 600 行（棘轮冻结），新段优先放独立模块。

## 模块清单（自动生成）

共 **10** 个模块，被测试覆盖 **9** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.config` | 配置层：``app.toml`` / ``services.toml``（旧 ``providers.toml``）/ ``state.toml`` / | 83 | `_atrinis_support.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_bootstrap.py`、`test_agent_budget.py`、`test_agent_chat.py`、`test_agent_commands_integration.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_directives.py`、`test_agent_group_policy.py`、`test_agent_model.py`、`test_agent_participation.py`、`test_agent_prompt.py`、`test_agent_reactions.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_llm_adapters.py`、`test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_tools4.py`、`test_config.py`、`test_config_assets.py`、`test_config_loader.py`、`test_config_state.py`、`test_config_web_search.py`、`test_daily_summary.py`、`test_deploy_harness_cli.py`、`test_dsh_route.py`、`test_groupkb.py`、`test_maintenance.py`、`test_maintenance_dsh.py`、`test_maintenance_report.py`、`test_member_profile.py`、`test_request_manager.py`、`test_safety_core.py`、`test_time_config.py`、`test_time_service.py`、`test_timesync_https.py`、`test_timesync_sntp.py`、`test_timesync_verifier.py`、`test_tool_runtimeinfo.py`、`test_toolrun.py` |
| `atri_x.config.app` | ``app.toml`` 根结构 ``AppConfig``（Go ``Config``）与默认值应用 / 校验 / 合并。 | 786 | `test_config.py`、`test_config_loader.py`、`test_config_web_search.py`、`test_time_config.py` |
| `atri_x.config.app_sections` | ``app.toml`` 静态行为段的 dataclass。 | 1225 | `test_agent_admission.py`、`test_agent_core.py`、`test_agent_participation.py`、`test_agent_reactions.py`、`test_config.py`、`test_config_state.py`、`test_config_web_search.py`、`test_request_manager.py`、`test_safety_core.py`、`test_toolrun.py` |
| `atri_x.config.assets` | 内置默认配置资产。**本文件由 ``scripts/extract_assets.py`` 生成，不要手改。** | 100 | `test_config.py`、`test_config_assets.py` |
| `atri_x.config.inspect` | 严格解码框架与配置自检（未知键、内置 Skill 漂移）。 | 423 | `test_config.py`、`test_time_config.py` |
| `atri_x.config.loader` | 配置文件的查找、读取与合并（``app.toml`` 为入口）。 | 373 | `test_config.py`、`test_config_loader.py`、`test_deploy_harness_cli.py` |
| `atri_x.config.services` | 共享只读服务配置：``[providers.*]`` / ``[model_metadata]`` / ``[model_profiles]`` / | 710 | `test_config.py`、`test_config_loader.py`、`test_config_web_search.py` |
| `atri_x.config.state` | ``state.toml`` 运行态：结构、读写、以及**热加载**（mtime 判定 + 分区合并）。 | 748 | `test_config_state.py`、`test_groupkb.py` |
| `atri_x.config.time_sections` | ``[time]`` / ``[time.verification]`` / ``[[time.sources]]`` 配置段。 | 322 | `test_time_config.py`、`test_time_service.py`、`test_timesync_https.py`、`test_timesync_sntp.py`、`test_timesync_verifier.py` |
| `atri_x.config.tool_tags` | ``tool_tags.toml`` 解码。 | 49 | — |

## 对应上游 Go（自动生成）

`internal/config/assets.go`、`internal/config/config.go`、`internal/config/inspect.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 787 个 / 超线文件 79 个）。
