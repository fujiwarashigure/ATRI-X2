<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 改动请落在 `atri_x/devtools/` 的渲染实现或代码本身，然后重新生成；手改会被 `--check` 判为漂移。 -->

# Go ↔ Python 映射（自动生成）

- 有「对应 Go」声明的模块：**526** / 787。未声明的通常是 Python 侧新增能力。
- 权威映射是 `PORTING.md` §4（包级）；这里是模块级反查，用于查「这段代码对应上游哪个文件」。

## 模块 → Go 文件

| Go 文件 | Python 模块 |
|---|---|
| `cmd/atri-x/main.go` | `atri_x.__main__` |
| `internal/...go` | `atri_x.devtools.astindex` |
| `internal/agent/asr.go` | `atri_x.agent.asr` |
| `internal/agent/budget.go` | `atri_x.agent.budget`、`atri_x.agent.budget_chat`、`atri_x.agent.budget_keys`、`atri_x.agent.budget_ledger` |
| `internal/agent/character_directive.go` | `atri_x.agent.character_directive` |
| `internal/agent/chat.go` | `atri_x.agent.chat`、`atri_x.agent.chat_turn` |
| `internal/agent/chat_llm.go` | `atri_x.agent.chat_llm`、`atri_x.agent.chat_observe`、`atri_x.agent.chat_stream`、`atri_x.agent.chat_vision` |
| `internal/agent/chat_tools.go` | `atri_x.agent.chat_tools`、`atri_x.agent.tool_schemas` |
| `internal/agent/command_runtime.go` | `atri_x.agent.command_runtime` |
| `internal/agent/commands/character.go` | `atri_x.agent.commands.character` |
| `internal/agent/commands/compact.go` | `atri_x.agent.commands.compact` |
| `internal/agent/commands/completion_helpers.go` | `atri_x.agent.commands.completion` |
| `internal/agent/commands/context_policy.go` | `atri_x.agent.commands.context_policy` |
| `internal/agent/commands/doctor.go` | `atri_x.agent.commands.doctor` |
| `internal/agent/commands/group_knowledge.go` | `atri_x.agent.commands.group_knowledge` |
| `internal/agent/commands/group_policy.go` | `atri_x.agent.commands.group_policy` |
| `internal/agent/commands/group_services.go` | `atri_x.agent.commands.group_services` |
| `internal/agent/commands/help.go` | `atri_x.agent.commands.help` |
| `internal/agent/commands/hook.go` | `atri_x.agent.commands.hook` |
| `internal/agent/commands/log.go` | `atri_x.agent.commands.log`、`atri_x.agent.commands.log_args`、`atri_x.agent.commands.log_format` |
| `internal/agent/commands/member_panel.go` | `atri_x.agent.commands.member_panel` |
| `internal/agent/commands/memory_common.go` | `atri_x.agent.commands.memory_common` |
| `internal/agent/commands/memory_forget.go` | `atri_x.agent.commands.memory_forget` |
| `internal/agent/commands/memory_learning.go` | `atri_x.agent.commands.memory_learning` |
| `internal/agent/commands/model.go` | `atri_x.agent.commands.model` |
| `internal/agent/commands/register.go` | `atri_x.agent.commands.deps`、`atri_x.agent.commands.model_types` |
| `internal/agent/commands/request.go` | `atri_x.agent.commands.request` |
| `internal/agent/commands/rollback.go` | `atri_x.agent.commands.rollback` |
| `internal/agent/commands/session_core.go` | `atri_x.agent.commands.session_core` |
| `internal/agent/commands/session_format.go` | `atri_x.agent.commands.session_format` |
| `internal/agent/commands/session_lifecycle.go` | `atri_x.agent.commands.session_lifecycle` |
| `internal/agent/commands/session_module.go` | `atri_x.agent.commands.session_module` |
| `internal/agent/commands/session_navigation.go` | `atri_x.agent.commands.session_navigation` |
| `internal/agent/commands/state.go` | `atri_x.agent.commands.state` |
| `internal/agent/commands/tool.go` | `atri_x.agent.commands.tool` |
| `internal/agent/commands/usage.go` | `atri_x.agent.commands.usage` |
| `internal/agent/completion.go` | `atri_x.agent.completion` |
| `internal/agent/context_compact.go` | `atri_x.agent.context_compact` |
| `internal/agent/context_policy.go` | `atri_x.agent.context_policy` |
| `internal/agent/context_runtime.go` | `atri_x.agent.context_runtime` |
| `internal/agent/context_seed.go` | `atri_x.agent.context_seed` |
| `internal/agent/context_usage.go` | `atri_x.agent.context_usage` |
| `internal/agent/core.go` | `atri_x.agent.bootstrap`、`atri_x.agent.core`、`atri_x.agent.core_build`、`atri_x.agent.core_forward` |
| `internal/agent/cron.go` | `atri_x.agent.cron`、`atri_x.agent.cron_background` |
| `internal/agent/cron_tools.go` | `atri_x.agent.cron_tools` |
| `internal/agent/errors.go` | `atri_x.agent.errors` |
| `internal/agent/events.go` | `atri_x.agent.events` |
| `internal/agent/group_knowledge.go` | `atri_x.agent.group_knowledge` |
| `internal/agent/group_policy.go` | `atri_x.agent.group_policy`、`atri_x.agent.group_policy_rules` |
| `internal/agent/group_runtime.go` | `atri_x.agent.group_runtime` |
| `internal/agent/group_services_polls.go` | `atri_x.agent.group_services_polls` |
| `internal/agent/group_services_reminders.go` | `atri_x.agent.group_services_reminders` |
| `internal/agent/group_services_signups.go` | `atri_x.agent.group_services_signups` |
| `internal/agent/group_services_state.go` | `atri_x.agent.group_services_state` |
| `internal/agent/hooks.go` | `atri_x.agent.hooks` |
| `internal/agent/identity.go` | `atri_x.agent.identity` |
| `internal/agent/inbound_media.go` | `atri_x.agent.inbound_media` |
| `internal/agent/inbound_speaker.go` | `atri_x.agent.inbound_speaker` |
| `internal/agent/inbox.go` | `atri_x.agent.inbox` |
| `internal/agent/input.go` | `atri_x.agent.input` |
| `internal/agent/llm_request_options.go` | `atri_x.agent.chat_request_options` |
| `internal/agent/logging.go` | `atri_x.agent.logging` |
| `internal/agent/media_output.go` | `atri_x.agent.media_output` |
| `internal/agent/member_panel.go` | `atri_x.agent.member_panel` |
| `internal/agent/memory_learning_admin.go` | `atri_x.agent.memory_learning_admin` |
| `internal/agent/message.go` | `atri_x.agent.message` |
| `internal/agent/model.go` | `atri_x.agent.model`、`atri_x.agent.model_agent`、`atri_x.agent.model_list`、`atri_x.agent.model_service`、`atri_x.agent.model_state` |
| `internal/agent/model_policy.go` | `atri_x.agent.model_policy` |
| `internal/agent/model_snapshot.go` | `atri_x.agent.model_snapshot` |
| `internal/agent/notice_queue.go` | `atri_x.agent.notice_queue` |
| `internal/agent/ops.go` | `atri_x.agent.ops` |
| `internal/agent/options.go` | `atri_x.agent.options` |
| `internal/agent/output.go` | `atri_x.agent.output` |
| `internal/agent/platform_signals.go` | `atri_x.agent.platform_signals` |
| `internal/agent/prefix.go` | `atri_x.agent.prefix` |
| `internal/agent/prompt.go` | `atri_x.agent.prompt` |
| `internal/agent/ratelimit.go` | `atri_x.agent.ratelimit` |
| `internal/agent/request_context.go` | `atri_x.agent.request_context` |
| `internal/agent/risk_confirmation.go` | `atri_x.agent.risk_confirmation` |
| `internal/agent/segments.go` | `atri_x.agent.segments` |
| `internal/agent/session_metadata.go` | `atri_x.agent.session_metadata` |
| `internal/agent/state_watch.go` | `atri_x.agent.state_watch` |
| `internal/agent/status.go` | `atri_x.agent.status` |
| `internal/agent/system_prompt.go` | `atri_x.agent.system_prompt` |
| `internal/agent/system_prompt_sources.go` | `atri_x.agent.system_prompt_sources` |
| `internal/agent/title.go` | `atri_x.agent.title` |
| `internal/agent/tool_auth.go` | `atri_x.agent.tool_auth` |
| `internal/agent/tool_cache.go` | `atri_x.agent.tool_cache` |
| `internal/agent/tool_directive.go` | `atri_x.agent.tool_directive`、`atri_x.agent.tool_directive_skill` |
| `internal/agent/tool_tag_config.go` | `atri_x.agent.tool_tag_config` |
| `internal/agent/tool_transcript.go` | `atri_x.agent.tool_transcript` |
| `internal/agent/toolrun_adapter.go` | `atri_x.agent.toolrun_adapter` |
| `internal/agent/toolrun_prompt_provider.go` | `atri_x.agent.toolrun_prompt_provider` |
| `internal/agent/tools.go` | `atri_x.agent.tools`、`atri_x.agent.tools_service` |
| `internal/agent/turn_gate.go` | `atri_x.agent.turn_gate` |
| `internal/agent/turn_output.go` | `atri_x.agent.turn_output` |
| `internal/agent/turn_override.go` | `atri_x.agent.turn_override` |
| `internal/agent/vision.go` | `atri_x.agent.vision` |
| `internal/agent/wakeup.go` | `atri_x.agent.wakeup` |
| `internal/agent/workspace.go` | `atri_x.agent.workspace` |
| `internal/angelmemory/doc.go` | `atri_x.angelmemory` |
| `internal/angelmemory/service.go` | `atri_x.angelmemory.service` |
| `internal/angelmemory/store.go` | `atri_x.angelmemory.models`、`atri_x.angelmemory.schema`、`atri_x.angelmemory.store` |
| `internal/app/app.go` | `atri_x.app.app` |
| `internal/app/asr.go` | `atri_x.app.asr`、`atri_x.asr` |
| `internal/app/breaker_llm.go` | `atri_x.app.breaker_llm` |
| `internal/app/cli_client.go` | `atri_x.app.cli_client` |
| `internal/app/config_check.go` | `atri_x.app.config_check` |
| `internal/app/cron.go` | `atri_x.app.cron` |
| `internal/app/dependencies.go` | `atri_x.app.dependencies` |
| `internal/app/doctor.go` | `atri_x.app.doctor`、`atri_x.app.doctor_checks` |
| `internal/app/environment.go` | `atri_x.app.environment` |
| `internal/app/foundation.go` | `atri_x.app.foundation` |
| `internal/app/health.go` | `atri_x.app.health` |
| `internal/app/health_handler.go` | `atri_x.app.health_handler` |
| `internal/app/health_llm.go` | `atri_x.app.health_llm` |
| `internal/app/image_rewriter.go` | `atri_x.app.image_rewriter` |
| `internal/app/image_to_prompt.go` | `atri_x.app.image_to_prompt` |
| `internal/app/integrations.go` | `atri_x.app.integrations` |
| `internal/app/models.go` | `atri_x.app.models` |
| `internal/app/naming.go` | `atri_x.app.naming` |
| `internal/app/ops_health.go` | `atri_x.app.ops_health` |
| `internal/app/platform_caller.go` | `atri_x.app.platform_caller` |
| `internal/app/platforms.go` | `atri_x.app.platforms` |
| `internal/app/runner.go` | `atri_x.app.runner` |
| `internal/app/runtime.go` | `atri_x.app.runtime`、`atri_x.app.runtime_agent`、`atri_x.app.runtime_hooks`、`atri_x.app.runtime_support` |
| `internal/app/service_marker_common.go` | `atri_x.app.service_marker` |
| `internal/app/vision.go` | `atri_x.app.vision` |
| `internal/artist/metadata.go` | `atri_x.artist.metadata`、`atri_x.artist.metadata_formats` |
| `internal/artist/metadata_store.go` | `atri_x.artist.metadata_store` |
| `internal/artist/parser.go` | `atri_x.artist.parser` |
| `internal/artist/render.go` | `atri_x.artist.render` |
| `internal/artist/search.go` | `atri_x.artist.search` |
| `internal/artist/service.go` | `atri_x.artist.service` |
| `internal/artist/store.go` | `atri_x.artist.store` |
| `internal/artist/types.go` | `atri_x.artist.types` |
| `internal/asr/asr.go` | `atri_x.asr.asr` |
| `internal/asr/cache.go` | `atri_x.asr.cache` |
| `internal/asr/error.go` | `atri_x.asr.error` |
| `internal/atriena/calls.go` | `atri_x.atriena.calls` |
| `internal/atriena/delivery.go` | `atri_x.atriena.delivery` |
| `internal/atriena/dispatcher.go` | `atri_x.atriena.dispatcher` |
| `internal/atriena/origin.go` | `atri_x.atriena.origin` |
| `internal/atriena/types.go` | `atri_x.atriena.types` |
| `internal/atrinis/auth.go` | `atri_x.atrinis.auth` |
| `internal/atrinis/background.go` | `atri_x.atrinis.background` |
| `internal/atrinis/direct.go` | `atri_x.atrinis.direct` |
| `internal/atrinis/http.go` | `atri_x.atrinis.http` |
| `internal/atrinis/llm.go` | `atri_x.atrinis.llm` |
| `internal/atrinis/media.go` | `atri_x.atrinis.media` |
| `internal/atrinis/outbox.go` | `atri_x.atrinis.outbox` |
| `internal/atrinis/prepare.go` | `atri_x.atrinis.prepare` |
| `internal/atrinis/segments.go` | `atri_x.atrinis.segments` |
| `internal/atrinis/service.go` | `atri_x.atrinis.service` |
| `internal/atrinis/store.go` | `atri_x.atrinis.store` |
| `internal/atrinis/targets.go` | `atri_x.atrinis.targets` |
| `internal/atrinis/types.go` | `atri_x.atrinis.types` |
| `internal/atriyph/ast.go` | `atri_x.atriyph.ast` |
| `internal/atriyph/parser.go` | `atri_x.atriyph.parser` |
| `internal/atriyph/rules.go` | `atri_x.atriyph.rules` |
| `internal/background/background.go` | `atri_x.background.background` |
| `internal/background/report.go` | `atri_x.background.report` |
| `internal/background/result.go` | `atri_x.background.result` |
| `internal/character/search.go` | `atri_x.character.search` |
| `internal/character/store.go` | `atri_x.character.manifest`、`atri_x.character.store`、`atri_x.character.toml_codec` |
| `internal/character/write.go` | `atri_x.character.write` |
| `internal/command/handler.go` | `atri_x.command.handler` |
| `internal/command/router.go` | `atri_x.command.router` |
| `internal/command/types.go` | `atri_x.command.types` |
| `internal/completion/character.go` | `atri_x.completion.character` |
| `internal/completion/completion.go` | `atri_x.completion.completion` |
| `internal/completion/fork.go` | `atri_x.completion.fork` |
| `internal/completion/risk.go` | `atri_x.completion.risk` |
| `internal/completion/router.go` | `atri_x.completion.router` |
| `internal/completion/tool.go` | `atri_x.completion.tool` |
| `internal/config/assets.go` | `atri_x.config.assets` |
| `internal/config/config.go` | `atri_x.config.app`、`atri_x.config.app_sections`、`atri_x.config.loader`、`atri_x.config.services`、`atri_x.config.state`、`atri_x.config.tool_tags` |
| `internal/config/inspect.go` | `atri_x.config.inspect` |
| `internal/contextmgr/budget.go` | `atri_x.contextmgr.budget` |
| `internal/contextmgr/compact_prompt.go` | `atri_x.contextmgr.compact_prompt` |
| `internal/contextmgr/compressor.go` | `atri_x.contextmgr.compressor` |
| `internal/contextmgr/loader.go` | `atri_x.contextmgr.loader` |
| `internal/contextmgr/usage.go` | `atri_x.contextmgr.usage` |
| `internal/contextmgr/window.go` | `atri_x.contextmgr.window` |
| `internal/cron/delivery.go` | `atri_x.cron.delivery` |
| `internal/cron/execution.go` | `atri_x.cron.execution` |
| `internal/cron/manager.go` | `atri_x.cron.manager` |
| `internal/cron/model.go` | `atri_x.cron.delivery_state`、`atri_x.cron.jsonview`、`atri_x.cron.model` |
| `internal/cron/recovery.go` | `atri_x.cron.recovery` |
| `internal/cron/service.go` | `atri_x.cron.service` |
| `internal/delivery/delivery.go` | `atri_x.delivery.delivery` |
| `internal/directive/tool.go` | `atri_x.directive.tool` |
| `internal/groupanalysis/service.go` | `atri_x.groupanalysis.service` |
| `internal/groupanalysis/summarizer.go` | `atri_x.groupanalysis.summarizer` |
| `internal/groupkb/groupkb.go` | `atri_x.groupkb`、`atri_x.groupkb.groupkb` |
| `internal/health/checker.go` | `atri_x.health.checker` |
| `internal/health/server.go` | `atri_x.health.server` |
| `internal/health/state.go` | `atri_x.health.state` |
| `internal/historygate/gate.go` | `atri_x.historygate.gate` |
| `internal/hook/builtin/angel_memory.go` | `atri_x.hook.builtin.angel_memory` |
| `internal/hook/builtin/register.go` | `atri_x.hook.builtin.register` |
| `internal/hook/builtin/self_learning.go` | `atri_x.hook.builtin.self_learning` |
| `internal/hook/control/service.go` | `atri_x.hook.control` |
| `internal/hook/event.go` | `atri_x.hook.event`、`atri_x.hook.protocol.eventjson` |
| `internal/hook/manager.go` | `atri_x.hook.manager` |
| `internal/hook/match.go` | `atri_x.hook.match` |
| `internal/hook/media.go` | `atri_x.hook.media` |
| `internal/hook/output/message.go` | `atri_x.hook.output.message` |
| `internal/hook/output/output.go` | `atri_x.hook.output.jsonstrict`、`atri_x.hook.output.output` |
| `internal/hook/process.go` | `atri_x.hook.process` |
| `internal/hook/protocol/protocol.go` | `atri_x.hook.protocol.frames` |
| `internal/hook/rules/action.go` | `atri_x.hook.rules.action` |
| `internal/hook/rules/config.go` | `atri_x.hook.rules.config` |
| `internal/hook/rules/detail.go` | `atri_x.hook.rules.detail` |
| `internal/hook/rules/exec.go` | `atri_x.hook.protocol.stream`、`atri_x.hook.rules.exec`、`atri_x.hook.rules.exec_stderr` |
| `internal/hook/rules/rule.go` | `atri_x.hook.rules.rule` |
| `internal/hook/rules/rules.go` | `atri_x.hook.rules.rules`、`atri_x.hook.rules.types` |
| `internal/hook/rules/toml_error.go` | `atri_x.hook.rules.toml_error` |
| `internal/hook/runtime/config.go` | `atri_x.hook.runtime.config` |
| `internal/hook/runtime/manager.go` | `atri_x.hook.runtime.manager` |
| `internal/hook/runtime/media.go` | `atri_x.hook.runtime.media` |
| `internal/hook/runtime/protocol.go` | `atri_x.hook.runtime.protocol` |
| `internal/hook/runtime/routes.go` | `atri_x.hook.runtime.routes` |
| `internal/hook/runtime/shared.go` | `atri_x.hook.runtime.shared` |
| `internal/hook/runtime/tool_bridge.go` | `atri_x.hook.runtime.tool_bridge` |
| `internal/hook/runtime/worker.go` | `atri_x.hook.runtime.transport`、`atri_x.hook.runtime.worker` |
| `internal/imagegen/client.go` | `atri_x.imagegen.client` |
| `internal/imagegen/context.go` | `atri_x.imagegen.context` |
| `internal/imagegen/library.go` | `atri_x.imagegen.library` |
| `internal/imagegen/optimize.go` | `atri_x.imagegen.optimize` |
| `internal/imagegen/prompt.go` | `atri_x.imagegen.prompt` |
| `internal/imagegen/search.go` | `atri_x.imagegen.search` |
| `internal/launcher/cli.go` | `atri_x.launcher.cli` |
| `internal/llm/breaker/breaker.go` | `atri_x.llm.breaker` |
| `internal/llm/errors.go` | `atri_x.llm.errors` |
| `internal/llm/llm.go` | `atri_x.llm.llm` |
| `internal/llm/openai/openai.go` | `atri_x.llm.openai.errors`、`atri_x.llm.openai.openai`、`atri_x.llm.openai.stream`、`atri_x.llm.openai.wire` |
| `internal/llm/openai/responses.go` | `atri_x.llm.openai.responses` |
| `internal/llm/origin.go` | `atri_x.llm.origin` |
| `internal/llm/protocol.go` | `atri_x.llm.protocol` |
| `internal/llm/segment.go` | `atri_x.llm.segment` |
| `internal/logging/contract.go` | `atri_x.logging.contract` |
| `internal/logging/logging.go` | `atri_x.logging.logging` |
| `internal/logging/reader.go` | `atri_x.logging.reader` |
| `internal/maintenance/maintenance.go` | `atri_x.maintenance.maintenance` |
| `internal/maintenance/report.go` | `atri_x.maintenance.report` |
| `internal/media/config.go` | `atri_x.media.config` |
| `internal/media/generation.go` | `atri_x.media.generation` |
| `internal/media/history.go` | `atri_x.media.history` |
| `internal/media/image.go` | `atri_x.media.image`、`atri_x.media.imagecodec`、`atri_x.media.imagecodec.image`、`atri_x.media.imagecodec.ops` |
| `internal/media/lifecycle.go` | `atri_x.media.lifecycle` |
| `internal/media/local.go` | `atri_x.media.local` |
| `internal/media/locks.go` | `atri_x.media.locks` |
| `internal/media/manager.go` | `atri_x.media.manager`、`atri_x.media.sanitize` |
| `internal/media/output.go` | `atri_x.media.output` |
| `internal/media/platform.go` | `atri_x.media.platform` |
| `internal/media/reference.go` | `atri_x.media.reference` |
| `internal/media/remote.go` | `atri_x.media.remote` |
| `internal/media/resolver.go` | `atri_x.media.resolver` |
| `internal/media/s3.go` | `atri_x.media.s3` |
| `internal/media/types.go` | `atri_x.media.types` |
| `internal/media/worker.go` | `atri_x.media.worker` |
| `internal/memory/resident/atomic.go` | `atri_x.memory.resident.atomic` |
| `internal/memory/resident/store.go` | `atri_x.memory.resident.entries`、`atri_x.memory.resident.store` |
| `internal/ops/concurrency/limiter.go` | `atri_x.ops.concurrency` |
| `internal/ops/diskguard/guard.go` | `atri_x.ops.diskguard` |
| `internal/ops/ratelimit/ratelimit.go` | `atri_x.ops.ratelimit` |
| `internal/platform/backoff.go` | `atri_x.platform.backoff` |
| `internal/platform/builtin/builtin.go` | `atri_x.platform.builtin` |
| `internal/platform/cli/cli.go` | `atri_x.platform.cli.adapter` |
| `internal/platform/cli/clipboard.go` | `atri_x.platform.cli.clipboard` |
| `internal/platform/cli/config.go` | `atri_x.platform.cli.config` |
| `internal/platform/cli/remote_client.go` | `atri_x.platform.cli.remote_client` |
| `internal/platform/cli/remote_protocol.go` | `atri_x.platform.cli.protocol` |
| `internal/platform/cli/remote_server.go` | `atri_x.platform.cli.remote_server` |
| `internal/platform/cli/tui.go` | `atri_x.platform.cli.tui.content`、`atri_x.platform.cli.tui.model`、`atri_x.platform.cli.tui.view` |
| `internal/platform/cli/tui_copy.go` | `atri_x.platform.cli.tui.copy` |
| `internal/platform/cli/tui_file_reference.go` | `atri_x.platform.cli.local_file` |
| `internal/platform/cli/tui_mouse.go` | `atri_x.platform.cli.tui.mouse` |
| `internal/platform/config.go` | `atri_x.platform.config` |
| `internal/platform/media.go` | `atri_x.platform.media` |
| `internal/platform/platform.go` | `atri_x.platform.platform` |
| `internal/platform/qq-onebot/adapter.go` | `atri_x.platform.qq_onebot.adapter`、`atri_x.platform.qq_onebot.config`、`atri_x.platform.qq_onebot.output`、`atri_x.platform.qq_onebot.target` |
| `internal/platform/qq-onebot/attachment.go` | `atri_x.platform.qq_onebot.attachment` |
| `internal/platform/qq-onebot/capabilities.go` | `atri_x.platform.qq_onebot.capabilities` |
| `internal/platform/qq-onebot/inbound.go` | `atri_x.platform.qq_onebot.inbound`、`atri_x.platform.qq_onebot.preprocess` |
| `internal/platform/qq-onebot/message.go` | `atri_x.platform.qq_onebot.forward`、`atri_x.platform.qq_onebot.message`、`atri_x.platform.qq_onebot.types` |
| `internal/platform/qq-onebot/transport.go` | `atri_x.platform.qq_onebot.transport` |
| `internal/platform/qqofficial/adapter.go` | `atri_x.platform.qqofficial.adapter`、`atri_x.platform.qqofficial.target` |
| `internal/platform/qqofficial/attachment.go` | `atri_x.platform.qqofficial.attachment` |
| `internal/platform/qqofficial/client.go` | `atri_x.platform.qqofficial.client` |
| `internal/platform/qqofficial/config.go` | `atri_x.platform.qqofficial.config` |
| `internal/platform/qqofficial/gateway.go` | `atri_x.platform.qqofficial.gateway` |
| `internal/platform/qqofficial/keyboard.go` | `atri_x.platform.qqofficial.keyboard` |
| `internal/platform/qqofficial/message.go` | `atri_x.platform.qqofficial.message` |
| `internal/platform/qqofficial/send.go` | `atri_x.platform.qqofficial.send` |
| `internal/platform/qqofficial/token.go` | `atri_x.platform.qqofficial.token` |
| `internal/platform/qqofficial/types.go` | `atri_x.platform.qqofficial.types` |
| `internal/platform/refcontext/refcontext.go` | `atri_x.platform.refcontext` |
| `internal/platform/telegram/adapter.go` | `atri_x.platform.telegram.adapter`、`atri_x.platform.telegram.target` |
| `internal/platform/telegram/capabilities.go` | `atri_x.platform.telegram.capabilities` |
| `internal/platform/telegram/client.go` | `atri_x.platform.telegram.client` |
| `internal/platform/telegram/config.go` | `atri_x.platform.telegram.config` |
| `internal/platform/telegram/format.go` | `atri_x.platform.telegram.format` |
| `internal/platform/telegram/keyboard.go` | `atri_x.platform.telegram.keyboard` |
| `internal/platform/telegram/message.go` | `atri_x.platform.telegram.message` |
| `internal/platform/telegram/stream.go` | `atri_x.platform.telegram.stream` |
| `internal/platform/telegram/types.go` | `atri_x.platform.telegram.types` |
| `internal/processenv/environment.go` | `atri_x.processenv.environment` |
| `internal/ratelimit/ratelimit.go` | `atri_x.ratelimit` |
| `internal/redact/redact.go` | `atri_x.redact.redact` |
| `internal/request/manager.go` | `atri_x.request.manager` |
| `internal/runtime/status.go` | `atri_x.runtime.status` |
| `internal/safecontext/safecontext.go` | `atri_x.safecontext` |
| `internal/security/context.go` | `atri_x.security.context` |
| `internal/security/security.go` | `atri_x.security.security`、`atri_x.tool` |
| `internal/selflearning/doc.go` | `atri_x.selflearning` |
| `internal/selflearning/service.go` | `atri_x.selflearning.service` |
| `internal/selflearning/store.go` | `atri_x.selflearning.db`、`atri_x.selflearning.mining`、`atri_x.selflearning.models`、`atri_x.selflearning.patterns`、`atri_x.selflearning.store` |
| `internal/session/expiration.go` | `atri_x.session.expiration` |
| `internal/session/fork.go` | `atri_x.session.fork` |
| `internal/session/lifecycle.go` | `atri_x.session.lifecycle` |
| `internal/session/mode.go` | `atri_x.session.mode` |
| `internal/session/naming.go` | `atri_x.session.naming` |
| `internal/session/promotion.go` | `atri_x.session.promotion` |
| `internal/session/query.go` | `atri_x.session.query` |
| `internal/session/service.go` | `atri_x.session.service`、`atri_x.session.text` |
| `internal/session/shared.go` | `atri_x.session.shared` |
| `internal/session/types.go` | `atri_x.session.types` |
| `internal/signal/connection.go` | `atri_x.signal.connection` |
| `internal/signal/diagnostics.go` | `atri_x.signal.diagnostics` |
| `internal/signal/errors.go` | `atri_x.signal.errors` |
| `internal/signal/executor.go` | `atri_x.signal.executor` |
| `internal/signal/queue.go` | `atri_x.signal.queue` |
| `internal/signal/result.go` | `atri_x.signal.result` |
| `internal/signal/shutdown.go` | `atri_x.signal.shutdown` |
| `internal/signal/signal.go` | `atri_x.signal.signal` |
| `internal/storage/id.go` | `atri_x.storage.ids` |
| `internal/storage/sqlite/atrinis_event_repository.go` | `atri_x.storage.sqlite.atrinis_event_repository` |
| `internal/storage/sqlite/chat_history_repository.go` | `atri_x.storage.sqlite.chat_history_repository` |
| `internal/storage/sqlite/context_summary_repository.go` | `atri_x.storage.sqlite.context_summary_repository` |
| `internal/storage/sqlite/cron_job_repository.go` | `atri_x.storage.sqlite.cron_job_repository` |
| `internal/storage/sqlite/media_history.go` | `atri_x.storage.sqlite.media_history` |
| `internal/storage/sqlite/media_lifecycle.go` | `atri_x.storage.sqlite.media_lifecycle` |
| `internal/storage/sqlite/media_repository.go` | `atri_x.storage.sqlite.media_repository` |
| `internal/storage/sqlite/message_repository.go` | `atri_x.storage.sqlite.message_repository` |
| `internal/storage/sqlite/migrations.go` | `atri_x.storage.sqlite.migrations` |
| `internal/storage/sqlite/session_repository.go` | `atri_x.storage.sqlite.session_repository` |
| `internal/storage/sqlite/store.go` | `atri_x.storage.sqlite.store` |
| `internal/storage/sqlite/tool_call_repository.go` | `atri_x.storage.sqlite.tool_call_repository` |
| `internal/storage/storage.go` | `atri_x.storage.models`、`atri_x.storage.repositories`、`atri_x.storage.storage` |
| `internal/storage/time.go` | `atri_x.storage.times` |
| `internal/sysinfo/sysinfo.go` | `atri_x.sysinfo` |
| `internal/textmatch/textmatch.go` | `atri_x.textmatch` |
| `internal/tool/availability.go` | `atri_x.tool.availability` |
| `internal/tool/builder.go` | `atri_x.tool.builder` |
| `internal/tool/builtin/angel_memory.go` | `atri_x.tool.builtin.angel_memory` |
| `internal/tool/builtin/artist_extract.go` | `atri_x.tool.builtin.artist_extract` |
| `internal/tool/builtin/artist_library.go` | `atri_x.tool.builtin.artist_library` |
| `internal/tool/builtin/artist_tools.go` | `atri_x.tool.builtin.artist_tools` |
| `internal/tool/builtin/atriwisp_creator.go` | `atri_x.tool.builtin.atriwisp_creator` |
| `internal/tool/builtin/character.go` | `atri_x.tool.builtin.character` |
| `internal/tool/builtin/chat_history.go` | `atri_x.tool.builtin.chat_history` |
| `internal/tool/builtin/cron.go` | `atri_x.tool.builtin.cron` |
| `internal/tool/builtin/env.go` | `atri_x.tool.builtin.common` |
| `internal/tool/builtin/file_guard.go` | `atri_x.tool.builtin.file_guard` |
| `internal/tool/builtin/file_manager.go` | `atri_x.tool.builtin.file_manager` |
| `internal/tool/builtin/file_rollback.go` | `atri_x.tool.builtin.file_rollback` |
| `internal/tool/builtin/file_tools.go` | `atri_x.tool.builtin.file_tools`、`atri_x.tool.builtin.file_tools_detail`、`atri_x.tool.builtin.file_tools_read` |
| `internal/tool/builtin/file_tools_ast.go` | `atri_x.tool.builtin.file_tools_ast` |
| `internal/tool/builtin/file_tools_directory.go` | `atri_x.tool.builtin.file_tools_directory` |
| `internal/tool/builtin/get_media.go` | `atri_x.tool.builtin.get_media` |
| `internal/tool/builtin/group_analysis.go` | `atri_x.tool.builtin.group_analysis` |
| `internal/tool/builtin/image_generate.go` | `atri_x.tool.builtin.image_generate`、`atri_x.tool.builtin.image_generate_compose` |
| `internal/tool/builtin/image_generate_context.go` | `atri_x.tool.builtin.image_generate_context` |
| `internal/tool/builtin/image_to_prompt.go` | `atri_x.tool.builtin.image_to_prompt` |
| `internal/tool/builtin/long_memory.go` | `atri_x.tool.builtin.long_memory`、`atri_x.tool.builtin.long_memory_model`、`atri_x.tool.builtin.long_memory_store` |
| `internal/tool/builtin/prompt_library.go` | `atri_x.tool.builtin.prompt_library` |
| `internal/tool/builtin/register.go` | `atri_x.tool.builtin.register` |
| `internal/tool/builtin/resident_memory.go` | `atri_x.tool.builtin.resident_memory` |
| `internal/tool/builtin/runtime.go` | `atri_x.app.tool_runtime`、`atri_x.tool.builtin.runtime` |
| `internal/tool/builtin/self_learning.go` | `atri_x.tool.builtin.self_learning` |
| `internal/tool/builtin/send_file.go` | `atri_x.tool.builtin.send_file` |
| `internal/tool/builtin/shell.go` | `atri_x.tool.builtin.shell`、`atri_x.tool.builtin.shell_env` |
| `internal/tool/builtin/shell_risk.go` | `atri_x.tool.builtin.shell_risk`、`atri_x.tool.builtin.shell_syntax` |
| `internal/tool/builtin/shell_sandbox.go` | `atri_x.shellparse`、`atri_x.tool.builtin.shell_sandbox` |
| `internal/tool/builtin/shell_warnings.go` | `atri_x.tool.builtin.shell_warnings` |
| `internal/tool/builtin/view_image.go` | `atri_x.tool.builtin.view_image` |
| `internal/tool/builtin/web_extract.go` | `atri_x.tool.builtin.web_extract`、`atri_x.tool.builtin.web_extract_engines` |
| `internal/tool/builtin/web_search.go` | `atri_x.tool.builtin.web_search`、`atri_x.tool.builtin.web_search_engines` |
| `internal/tool/builtin/workspace.go` | `atri_x.tool.builtin.workspace` |
| `internal/tool/capability.go` | `atri_x.tool.capability` |
| `internal/tool/detail.go` | `atri_x.tool.detail` |
| `internal/tool/discover.go` | `atri_x.tool.discover` |
| `internal/tool/executor.go` | `atri_x.tool.executor` |
| `internal/tool/media_runtime.go` | `atri_x.tool.media_runtime` |
| `internal/tool/provider.go` | `atri_x.tool.provider` |
| `internal/tool/runtimeinfo/runtimeinfo.go` | `atri_x.tool.builtin`、`atri_x.tool.builtin.runtimeinfo` |
| `internal/tool/sandbox.go` | `atri_x.tool.sandbox` |
| `internal/tool/skill/agent_manifest.go` | `atri_x.tool.skill.agent_manifest` |
| `internal/tool/skill/agent_skill_tool.go` | `atri_x.tool.skill.agent_skill_tool` |
| `internal/tool/skill/atriyph_warnings.go` | `atri_x.tool.skill.atriyph_warnings` |
| `internal/tool/skill/catalog.go` | `atri_x.tool.skill.catalog` |
| `internal/tool/skill/command_tool.go` | `atri_x.tool.skill.command_tool` |
| `internal/tool/skill/creator.go` | `atri_x.tool.skill.creator` |
| `internal/tool/skill/descriptor.go` | `atri_x.tool.skill.descriptor` |
| `internal/tool/skill/finalize_atrix_skill.go` | `atri_x.tool.skill.finalize_atrix_skill` |
| `internal/tool/skill/go_source.go` | `atri_x.tool.skill.go_source` |
| `internal/tool/skill/manager.go` | `atri_x.tool.skill.manager` |
| `internal/tool/skill/media.go` | `atri_x.tool.skill.media` |
| `internal/tool/skill/modify_atrix_skill.go` | `atri_x.tool.skill.modify_atrix_skill` |
| `internal/tool/skill/parser.go` | `atri_x.tool.skill.parser` |
| `internal/tool/skill/process_unix.go` | `atri_x.tool.skill.process` |
| `internal/tool/skill/runner.go` | `atri_x.tool.skill.runner` |
| `internal/tool/skill/scanner.go` | `atri_x.tool.skill.scanner` |
| `internal/tool/tool.go` | `atri_x.tool.tool` |
| `internal/tool/workspace.go` | `atri_x.tool.workspace` |
| `internal/toolrun/atriwisp.go` | `atri_x.toolrun.atriwisp` |
| `internal/toolrun/atriwisp_exec.go` | `atri_x.toolrun.atriwisp_exec` |
| `internal/toolrun/availability.go` | `atri_x.toolrun.availability` |
| `internal/toolrun/cache.go` | `atri_x.toolrun.cache` |
| `internal/toolrun/native.go` | `atri_x.toolrun.native` |
| `internal/toolrun/runner.go` | `atri_x.toolrun.preview`、`atri_x.toolrun.runner` |
| `internal/toolrun/toolrun.go` | `atri_x.toolrun.toolrun` |
| `internal/turn/manager.go` | `atri_x.turn.manager` |
| `internal/utils/fileops/atomic_write.go` | `atri_x.utils.fileops.atomic_write` |
| `internal/utils/fileops/diff.go` | `atri_x.utils.fileops.diff` |
| `internal/utils/fileops/edit.go` | `atri_x.utils.fileops.edit` |
| `internal/utils/fileops/encoding.go` | `atri_x.utils.fileops.encoding` |
| `internal/utils/fileops/file.go` | `atri_x.utils.fileops.file` |
| `internal/utils/fileops/match.go` | `atri_x.utils.fileops.match` |
| `internal/utils/fileops/rollback.go` | `atri_x.utils.fileops.rollback` |
| `internal/utils/fileops/text.go` | `atri_x.utils.fileops.text` |
| `internal/vision/analysis.go` | `atri_x.vision.analysis` |
| `internal/vision/cache.go` | `atri_x.vision.cache` |
| `internal/vision/counters.go` | `atri_x.vision.counters` |
| `internal/vision/fingerprint.go` | `atri_x.vision.fingerprint` |
| `internal/vision/metrics.go` | `atri_x.vision.metrics` |
| `internal/vision/prompt.go` | `atri_x.vision.prompt` |
| `internal/vision/service.go` | `atri_x.vision.service`、`atri_x.vision.service_types` |

## PORTING.md §4 映射表体检

| Go 包 | Python 模块 | 状态 |
|---|---|---|
| `cmd/atri-x` | `atri_x/__main__.py` | OK |
| `internal/launcher` | `atri_x/launcher/cli.py` | OK |
| `internal/app` | `atri_x/app/*.py` | 缺失：atri_x/app/*.py |
| `internal/config` | `atri_x/config/{config,assets,inspect}.py` | 缺失：atri_x/config/{config,assets,inspect}.py |
| `internal/logging` | `atri_x/logging/{logging,reader,contract}.py` | 缺失：atri_x/logging/{logging,reader,contract}.py |
| `internal/redact` | `atri_x/redact/redact.py` | OK |
| `internal/processenv` | `atri_x/processenv/environment.py` | OK |
| `internal/storage` | `atri_x/storage/{storage,ids,times}.py` | 缺失：atri_x/storage/{storage,ids,times}.py |
| `internal/storage/sqlite` | `atri_x/storage/sqlite/*.py` | 缺失：atri_x/storage/sqlite/*.py |
| `internal/llm` | `atri_x/llm/{llm,protocol,errors,origin,segment}.py` | 缺失：atri_x/llm/{llm,protocol,errors,origin,segment}.py |
| `internal/llm/breaker` | `atri_x/llm/breaker.py` | OK |
| `internal/llm/openai` | `atri_x/llm/openai/{openai,responses}.py` | 缺失：atri_x/llm/openai/{openai,responses}.py |
| `internal/agent` | `atri_x/agent/*.py` | 缺失：atri_x/agent/*.py |
| `internal/agent/commands` | `atri_x/agent/commands/*.py` | 缺失：atri_x/agent/commands/*.py |
| `internal/command` | `atri_x/command/{types,router,handler}.py` | 缺失：atri_x/command/{types,router,handler}.py |
| `internal/completion` | `atri_x/completion/*.py` | 缺失：atri_x/completion/*.py |
| `internal/request` | `atri_x/request/manager.py` | OK |
| `internal/turn` | `atri_x/turn/manager.py` | OK |
| `internal/runtime` | `atri_x/runtime/status.py` | OK |
| `internal/signal` | `atri_x/signal/*.py` | 缺失：atri_x/signal/*.py |
| `internal/tool` | `atri_x/tool/*.py` | 缺失：atri_x/tool/*.py |
| `internal/tool/builtin` | `atri_x/tool/builtin/*.py` | 缺失：atri_x/tool/builtin/*.py |
| `internal/tool/skill` | `atri_x/tool/skill/*.py` | 缺失：atri_x/tool/skill/*.py |
| `internal/tool/runtimeinfo` | `atri_x/tool/runtimeinfo.py` | OK |
| `internal/toolrun` | `atri_x/toolrun/*.py` | 缺失：atri_x/toolrun/*.py |
| `internal/platform` | `atri_x/platform/{platform,config,media,backoff}.py` | 缺失：atri_x/platform/{platform,config,media,backoff}.py |
| `internal/platform/cli` | `atri_x/platform/cli/*.py` | 缺失：atri_x/platform/cli/*.py |
| `internal/platform/qq-onebot` | `atri_x/platform/qq_onebot/*.py` | 缺失：atri_x/platform/qq_onebot/*.py |
| `internal/platform/qqofficial` | `atri_x/platform/qqofficial/*.py` | 缺失：atri_x/platform/qqofficial/*.py |
| `internal/platform/telegram` | `atri_x/platform/telegram/*.py` | 缺失：atri_x/platform/telegram/*.py |
| `internal/platform/builtin` | `atri_x/platform/builtin.py` | OK |
| `internal/platform/headless` | `atri_x/platform/headless.py` | OK |
| `internal/platform/refcontext` | `atri_x/platform/refcontext.py` | OK |
| `internal/delivery` | `atri_x/delivery/delivery.py` | OK |
| `internal/hook` | `atri_x/hook/{event,match,manager,media,process}.py` | 缺失：atri_x/hook/{event,match,manager,media,process}.py |
| `internal/hook/builtin` | `atri_x/hook/builtin/*.py` | 缺失：atri_x/hook/builtin/*.py |
| `internal/hook/control` | `atri_x/hook/control.py` | OK |
| `internal/hook/output` | `atri_x/hook/output/*.py` | 缺失：atri_x/hook/output/*.py |
| `internal/hook/protocol` | `atri_x/hook/protocol/frames.py` | OK |
| `internal/hook/rules` | `atri_x/hook/rules/*.py` | 缺失：atri_x/hook/rules/*.py |
| `internal/hook/runtime` | `atri_x/hook/runtime/*.py` | 缺失：atri_x/hook/runtime/*.py |
| `internal/session` | `atri_x/session/*.py` | 缺失：atri_x/session/*.py |
| `internal/contextmgr` | `atri_x/contextmgr/*.py` | 缺失：atri_x/contextmgr/*.py |
| `internal/cron` | `atri_x/cron/*.py` | 缺失：atri_x/cron/*.py |
| `internal/maintenance` | `atri_x/maintenance/*.py` | 缺失：atri_x/maintenance/*.py |
| `internal/health` | `atri_x/health/*.py` | 缺失：atri_x/health/*.py |
| `internal/media` | `atri_x/media/*.py` | 缺失：atri_x/media/*.py |
| `internal/memory/resident` | `atri_x/memory/resident/*.py` | 缺失：atri_x/memory/resident/*.py |
| `internal/vision` | `atri_x/vision/*.py` | 缺失：atri_x/vision/*.py |
| `internal/imagegen` | `atri_x/imagegen/*.py` | 缺失：atri_x/imagegen/*.py |
| `internal/artist` | `atri_x/artist/*.py` | 缺失：atri_x/artist/*.py |
| `internal/asr` | `atri_x/asr/*.py` | 缺失：atri_x/asr/*.py |
| `internal/character` | `atri_x/character/*.py` | 缺失：atri_x/character/*.py |
| `internal/angelmemory` | `atri_x/angelmemory/*.py` | 缺失：atri_x/angelmemory/*.py |
| `internal/selflearning` | `atri_x/selflearning/*.py` | 缺失：atri_x/selflearning/*.py |
| `internal/groupanalysis` | `atri_x/groupanalysis/*.py` | 缺失：atri_x/groupanalysis/*.py |
| `internal/groupkb` | `atri_x/groupkb/*.py` | 缺失：atri_x/groupkb/*.py |
| `internal/historygate` | `atri_x/historygate/*.py` | 缺失：atri_x/historygate/*.py |
| `internal/atriena` | `atri_x/atriena/*.py` | 缺失：atri_x/atriena/*.py |
| `internal/atrinis` | `atri_x/atrinis/*.py` | 缺失：atri_x/atrinis/*.py |
| `internal/atriyph` | `atri_x/atriyph/*.py` | 缺失：atri_x/atriyph/*.py |
| `internal/background` | `atri_x/background/*.py` | 缺失：atri_x/background/*.py |
| `internal/directive` | `atri_x/directive/*.py` | 缺失：atri_x/directive/*.py |
| `internal/ops/concurrency` | `atri_x/ops/concurrency.py` | OK |
| `internal/ops/diskguard` | `atri_x/ops/diskguard.py` | OK |
| `internal/ops/ratelimit` | `atri_x/ops/ratelimit.py` | OK |
| `internal/ratelimit` | `atri_x/ratelimit.py` | OK |
| `internal/security` | `atri_x/security/*.py` | 缺失：atri_x/security/*.py |
| `internal/safecontext` | `atri_x/safecontext.py` | OK |
| `internal/textmatch` | `atri_x/textmatch.py` | OK |
| `internal/sysinfo` | `atri_x/sysinfo.py` | OK |
| `internal/utils/fileops` | `atri_x/utils/fileops/*.py` | 缺失：atri_x/utils/fileops/*.py |
| `模块` | 为什么需要 | — |
| `atri_x/net/websocket.py` | Python 标准库**没有** WebSocket | — |
| `atri_x/net/httpclient.py` | 统一的 urllib 封装：重试、超时、流式读取、multipart、重定向跟随 | — |
| `atri_x/net/tomlw.py` | `tomllib` **只读**，写 TOML 要自己实现 | 缺失：tomllib |
| `atri_x/net/s3.py` | 标准库无 S3 客户端（SigV4 + 最小 REST） | — |
| `atri_x/net/cronspec.py` | 标准库无 cron 表达式解析 | — |
| `atri_x/shellparse/` | **clean-room** 自研 shell 词法/语法分析，支撑 `shell_sandbox`/`shell_risk` 的安全裁决；不得引入 GPLv… | 缺失：shell_sandbox、shell_risk |
| `atri_x/media/imagecodec/` | 标准库无图像编解码 | — |
| `atri_x/platform/cli/tui/` | bubbletea 的 Elm 架构 + ANSI cell 渲染（可用行式渲染简化，但命令/状态栏/输入行为必须实现） | — |
| `atri_x/utils/runes.py` | Go 的 `[]rune` 语义与 `runewidth` | 缺失：[]rune、runewidth |
| `atri_x/llm/tooltext.py` | 上游网关会把模型原生工具调用以文本协议（DSML / `<tool-call>` JSON）泄漏进正文；Go 侧没有这层防御，需要判定/还原/清洗 | 缺失：<tool-call> |
| `atri_x/agent/chat_anomaly.py` | 把上面的还原结果接回 `LLMCallResult`：能还原就继续工具轮，否则清洗正文并写 WARN 日志与 `llm_tool_calls_in_text`… | 缺失：LLMCallResult、llm_tool_calls_in_text |
| `atri_x/safety/` | **1.0.3 新增能力**（Go 上游没有）：内置「道德限制 + 敏感信息过滤」——词表类别 + 敏感信息正则 + 审计（只存 hash）+ 图片/文件出口… | 缺失：re、hashlib、json、threading、docs/safety.md |
| `atri_x/vision/prechat.py` / `privacy.py` / `identity.py` / `failures.py` | **1.0.3 新增能力**（Go 上游没有）：聊天前置两段式描述、图片外发隐私开关、SauceNAO 以图搜图兜底（默认关闭）、失败原因分类；`atri_x… | 缺失：docs/vision.md |

映射行 86 条，失效 **57** 条。

