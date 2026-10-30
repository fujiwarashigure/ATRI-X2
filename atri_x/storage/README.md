<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/storage/` — 存储与 SQLite

## 这个域负责什么

会话、消息、工具调用记录、各种元数据的持久化与迁移。

**不做什么**：不在这里做业务判断；存储层只保证读写与一致性。

**入口**：`atri_x/storage/models.py`（模型）、`atri_x/storage/sqlite/`（实现）、`atri_x/storage/sqlite/migrations.py`。

**配置段**：`[storage]`

## 易错点

- 改表结构必须加迁移，且要能在已有库上原地升级（用户不会删库）。
- ID 是带连字符的小写 UUIDv4；时间统一 `datetime` + RFC3339 落库。
- SQLite 走同步路径（`threading.Lock`），不要在事件循环里做长事务。

## 模块清单（自动生成）

共 **19** 个模块，被测试覆盖 **10** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.storage` | 存储包。对应 Go：``internal/storage``。 | 18 | `_atrinis_support.py`、`agent_fakes.py`、`test_agent_admission.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_commands_session.py`、`test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_core.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_title.py`、`test_agent_tools.py`、`test_angelmemory.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_cron.py`、`test_builtin_imagegen.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_contextmgr.py`、`test_cron.py`、`test_cron_delivery.py`、`test_cron_dst.py`、`test_daily_summary.py`、`test_dsh_route.py`、`test_groupanalysis.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_historygate.py`、`test_hook_runtime.py`、`test_maintenance.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_member_profile.py`、`test_platform_qq_onebot.py`、`test_refcontext.py`、`test_selflearning.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_migrations.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_tool_media_runtime.py`、`test_toolrun.py` |
| `atri_x.storage.ids` | 通用 ID helper。对应 Go：``internal/storage/id.go``。 | 41 | `test_builtin_cron.py`、`test_cron.py`、`test_media_manager.py`、`test_storage_models.py` |
| `atri_x.storage.models` | 存储领域模型。对应 Go：``internal/storage/storage.go``（领域模型部分；repository | 602 | `test_agent_context_policy.py`、`test_agent_context_runtime.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_title.py`、`test_builtin_cron.py`、`test_cli_sessions.py`、`test_contextmgr.py`、`test_cron.py`、`test_cron_delivery.py`、`test_cron_dst.py`、`test_groupanalysis.py`、`test_hook_runtime.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_member_profile.py`、`test_refcontext.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py`、`test_toolrun.py` |
| `atri_x.storage.repositories` | 存储 repository 抽象。对应 Go：``internal/storage/storage.go``（repository | 506 | `test_historygate.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py` |
| `atri_x.storage.sqlite` | SQLite 存储实现包。对应 Go：``internal/storage/sqlite``。 | 91 | `_atrinis_support.py`、`test_app_tool_runtime_wiring.py`、`test_atrinis.py`、`test_atrinis_helpers.py`、`test_atrinis_http.py`、`test_atrinis_llm.py`、`test_builtin_tools3.py`、`test_cli_sessions.py`、`test_daily_summary.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_historygate.py`、`test_maintenance.py`、`test_session.py`、`test_sqlite_media.py`、`test_sqlite_migrations.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py` |
| `atri_x.storage.sqlite.atrinis_event_repository` | Atrinis 事件与报告投递（outbox）仓储。对应 Go：``internal/storage/sqlite/atrinis_event_reposit… | 420 | — |
| `atri_x.storage.sqlite.chat_history_repository` | 入站聊天历史与出站消息仓储。对应 Go：``internal/storage/sqlite/chat_history_repository.go``。 | 448 | `test_app_tool_runtime_wiring.py`、`test_gui_members.py`、`test_gui_tabs.py` |
| `atri_x.storage.sqlite.context_summary_repository` | 上下文摘要仓储。对应 Go：``internal/storage/sqlite/context_summary_repository.go``。 | 138 | — |
| `atri_x.storage.sqlite.cron_job_repository` | 定时任务仓储。对应 Go：``internal/storage/sqlite/cron_job_repository.go``。 | 271 | — |
| `atri_x.storage.sqlite.media_history` | 历史消息媒体位置。对应 Go：``internal/storage/sqlite/media_history.go``。 | 111 | — |
| `atri_x.storage.sqlite.media_lifecycle` | 媒体清理认领、输出索引、启动恢复与只读审计。对应 Go：``internal/storage/sqlite/media_lifecycle.go``。 | 200 | — |
| `atri_x.storage.sqlite.media_repository` | 媒体与媒体引用仓储。对应 Go：``internal/storage/sqlite/media_repository.go``。 | 226 | — |
| `atri_x.storage.sqlite.message_repository` | 会话消息仓储。对应 Go：``internal/storage/sqlite/message_repository.go``。 | 257 | — |
| `atri_x.storage.sqlite.migrations` | SQLite schema migration。对应 Go：``internal/storage/sqlite/migrations.go``。 | 614 | `test_sqlite_migrations.py` |
| `atri_x.storage.sqlite.session_repository` | 会话仓储。对应 Go：``internal/storage/sqlite/session_repository.go``。 | 317 | — |
| `atri_x.storage.sqlite.store` | SQLite store：连接、事务与门面。对应 Go：``internal/storage/sqlite/store.go``。 | 397 | `test_app_tool_runtime_wiring.py` |
| `atri_x.storage.sqlite.tool_call_repository` | 工具调用审计仓储。对应 Go：``internal/storage/sqlite/tool_call_repository.go``。 | 102 | — |
| `atri_x.storage.storage` | 存储领域模型与 repository 抽象（对外统一出口）。对应 Go：``internal/storage/storage.go``。 | 161 | `test_storage_models.py` |
| `atri_x.storage.times` | 通用时间 helper。对应 Go：``internal/storage/time.go``。 | 156 | `test_angelmemory.py`、`test_builtin_cron.py`、`test_cron.py`、`test_cron_delivery.py`、`test_groupanalysis.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_platform_qq_onebot.py`、`test_selflearning.py`、`test_session.py`、`test_session_lifecycle.py`、`test_sqlite_media.py`、`test_sqlite_repositories.py`、`test_sqlite_store.py`、`test_storage_models.py` |

## 对应上游 Go（自动生成）

`internal/storage/id.go`、`internal/storage/sqlite/atrinis_event_repository.go`、`internal/storage/sqlite/chat_history_repository.go`、`internal/storage/sqlite/context_summary_repository.go`、`internal/storage/sqlite/cron_job_repository.go`、`internal/storage/sqlite/media_history.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 796 个 / 超线文件 79 个）。
