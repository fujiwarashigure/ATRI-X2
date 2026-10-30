<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 改动请落在 `atri_x/devtools/` 的渲染实现或代码本身，然后重新生成；手改会被 `--check` 判为漂移。 -->

# 测试地图（自动生成）

- 模块 **825** 个，其中被测试直接/间接覆盖 **518** 个（62.8%）；无覆盖 **307** 个。
- 测试文件 **239** 个。

口径：测试文件 import 了某模块（或其子模块）就算覆盖该模块；「无覆盖」不代表没测过，但**改那个模块时没有同名测试可跑**，只能靠调用方测试。

## 模块 → 测试

| 模块 | 测试文件 |
|---|---|
| `atri_x.__main__` | `tests/test_main.py` |
| `atri_x.agent` | `tests/agent_commands_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_admission.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_budget.py`、`tests/test_agent_chat.py`、`tests/test_agent_chat_anomaly.py`、`tests/test_agent_commands.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_commands_services.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_directives.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_inbound_media.py`、`tests/test_agent_model.py`、`tests/test_agent_participation.py`、`tests/test_agent_prompt.py`、`tests/test_agent_reactions.py`、`tests/test_agent_risk.py`、`tests/test_agent_segments.py`、`tests/test_agent_title.py`、`tests/test_agent_tools.py`、`tests/test_agent_wakeup.py`、`tests/test_app_runtime_agent.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_dsh_acp_client.py`、`tests/test_dsh_route.py`、`tests/test_hook_runtime.py`、`tests/test_member_persona.py`、`tests/test_member_profile.py`、`tests/test_memory_notes.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_promptlib.py`、`tests/test_request_manager.py`、`tests/test_safety_integration.py`、`tests/test_security_grants.py`、`tests/test_session_naming_contract.py`、`tests/test_time_context.py`、`tests/test_toolrun.py`、`tests/test_usage_attribution.py`、`tests/test_vision_prechat.py`、`tests/test_vision_privacy.py` |
| `atri_x.agent.admission` | `tests/test_agent_admission.py` |
| `atri_x.agent.bootstrap` | `tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_commands_integration.py`、`tests/test_app_runtime_agent.py` |
| `atri_x.agent.budget` | `tests/test_agent_budget.py` |
| `atri_x.agent.call_reason` | `tests/test_agent_admission.py`、`tests/test_usage_attribution.py` |
| `atri_x.agent.character_directive` | `tests/test_agent_assembly.py` |
| `atri_x.agent.chat` | `tests/test_agent_chat.py`、`tests/test_agent_core.py` |
| `atri_x.agent.chat_anomaly` | `tests/test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_llm` | `tests/test_agent_chat.py`、`tests/test_agent_chat_anomaly.py` |
| `atri_x.agent.chat_observe` | `tests/test_usage_attribution.py` |
| `atri_x.agent.chat_tools` | `tests/test_agent_chat.py`、`tests/test_agent_core.py`、`tests/test_security_grants.py` |
| `atri_x.agent.chat_turn` | `tests/test_agent_chat.py` |
| `atri_x.agent.commands` | `tests/agent_commands_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_commands.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_commands_services.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_security_grants.py`、`tests/test_usage_attribution.py` |
| `atri_x.agent.commands.approval` | `tests/test_security_grants.py` |
| `atri_x.agent.commands.deps` | `tests/test_agent_assembly.py`、`tests/test_agent_commands_integration.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_security_grants.py` |
| `atri_x.agent.commands.model_types` | `tests/test_agent_bootstrap.py`、`tests/test_agent_commands_integration.py` |
| `atri_x.agent.commands.resources` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.agent.commands.usage` | `tests/test_agent_commands_services.py`、`tests/test_usage_attribution.py` |
| `atri_x.agent.context_compact` | `tests/test_agent_context_runtime.py` |
| `atri_x.agent.context_policy` | `tests/test_agent_context_policy.py`、`tests/test_agent_group_policy.py` |
| `atri_x.agent.context_runtime` | `tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py` |
| `atri_x.agent.context_seed` | `tests/test_agent_context_runtime.py` |
| `atri_x.agent.context_usage` | `tests/test_agent_context_policy.py` |
| `atri_x.agent.core` | `tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_core.py`、`tests/test_request_manager.py` |
| `atri_x.agent.cron` | `tests/test_agent_admission.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py` |
| `atri_x.agent.cron_tools` | `tests/test_agent_cron.py` |
| `atri_x.agent.dsh` | `tests/test_dsh_acp_client.py`、`tests/test_dsh_route.py` |
| `atri_x.agent.dsh.handoff` | `tests/test_dsh_route.py` |
| `atri_x.agent.dsh.route_call` | `tests/test_dsh_route.py` |
| `atri_x.agent.dsh.router` | `tests/test_dsh_route.py` |
| `atri_x.agent.dsh.runtime` | `tests/test_dsh_route.py` |
| `atri_x.agent.dsh.smoke` | `tests/test_dsh_acp_client.py` |
| `atri_x.agent.dsh.status` | `tests/test_dsh_route.py` |
| `atri_x.agent.dsh.types` | `tests/test_dsh_acp_client.py`、`tests/test_dsh_route.py` |
| `atri_x.agent.errors` | `tests/test_agent_chat.py` |
| `atri_x.agent.group_knowledge` | `tests/test_agent_group_policy.py` |
| `atri_x.agent.group_policy` | `tests/agent_fakes.py`、`tests/test_agent_group_policy.py` |
| `atri_x.agent.group_policy_rules` | `tests/test_agent_group_policy.py` |
| `atri_x.agent.group_runtime` | `tests/test_agent_bootstrap.py`、`tests/test_agent_group_policy.py` |
| `atri_x.agent.group_services_polls` | `tests/test_agent_group_policy.py` |
| `atri_x.agent.group_services_reminders` | `tests/test_agent_group_policy.py` |
| `atri_x.agent.group_services_signups` | `tests/test_agent_group_policy.py` |
| `atri_x.agent.group_services_state` | `tests/test_agent_core.py`、`tests/test_agent_group_policy.py` |
| `atri_x.agent.hooks` | `tests/test_hook_runtime.py` |
| `atri_x.agent.inbound_media` | `tests/test_agent_inbound_media.py` |
| `atri_x.agent.input` | `tests/test_agent_risk.py` |
| `atri_x.agent.member_persona_prompt` | `tests/test_member_persona.py` |
| `atri_x.agent.member_profile_prompt` | `tests/test_member_profile.py` |
| `atri_x.agent.message` | `tests/test_safety_integration.py` |
| `atri_x.agent.model` | `tests/test_agent_bootstrap.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_model.py` |
| `atri_x.agent.model_agent` | `tests/test_agent_context_runtime.py`、`tests/test_agent_model.py` |
| `atri_x.agent.model_list` | `tests/test_agent_model.py` |
| `atri_x.agent.model_policy` | `tests/test_agent_model.py` |
| `atri_x.agent.model_service` | `tests/test_agent_bootstrap.py`、`tests/test_agent_model.py` |
| `atri_x.agent.model_snapshot` | `tests/test_agent_model.py` |
| `atri_x.agent.model_state` | `tests/test_agent_bootstrap.py`、`tests/test_agent_model.py` |
| `atri_x.agent.notice_queue` | `tests/test_agent_core.py` |
| `atri_x.agent.options` | `tests/test_agent_core.py` |
| `atri_x.agent.output` | `tests/test_safety_integration.py` |
| `atri_x.agent.participation` | `tests/test_agent_participation.py` |
| `atri_x.agent.prompt` | `tests/test_agent_prompt.py`、`tests/test_member_profile.py` |
| `atri_x.agent.ratelimit` | `tests/test_agent_bootstrap.py`、`tests/test_agent_directives.py` |
| `atri_x.agent.reactions` | `tests/test_agent_chat.py`、`tests/test_agent_reactions.py` |
| `atri_x.agent.request_context` | `tests/test_agent_core.py` |
| `atri_x.agent.risk_confirmation` | `tests/test_agent_risk.py` |
| `atri_x.agent.seams` | `tests/test_agent_bootstrap.py` |
| `atri_x.agent.segments` | `tests/test_agent_segments.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.agent.session_metadata` | `tests/test_agent_core.py` |
| `atri_x.agent.status` | `tests/test_agent_assembly.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.agent.system_prompt` | `tests/test_agent_prompt.py`、`tests/test_member_persona.py`、`tests/test_member_profile.py` |
| `atri_x.agent.system_prompt_sources` | `tests/test_agent_prompt.py`、`tests/test_agent_reactions.py`、`tests/test_memory_notes.py` |
| `atri_x.agent.time_context` | `tests/test_time_context.py` |
| `atri_x.agent.title` | `tests/test_agent_title.py`、`tests/test_session_naming_contract.py` |
| `atri_x.agent.tool_auth` | `tests/test_agent_group_policy.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py` |
| `atri_x.agent.tool_cache` | `tests/test_agent_tools.py` |
| `atri_x.agent.tool_directive` | `tests/test_agent_directives.py` |
| `atri_x.agent.tool_directive_skill` | `tests/test_agent_directives.py` |
| `atri_x.agent.tool_schemas` | `tests/test_agent_assembly.py` |
| `atri_x.agent.tool_tag_config` | `tests/test_agent_directives.py`、`tests/test_agent_prompt.py` |
| `atri_x.agent.tool_transcript` | `tests/test_agent_chat.py`、`tests/test_agent_tools.py` |
| `atri_x.agent.toolrun_adapter` | `tests/test_agent_risk.py`、`tests/test_safety_integration.py`、`tests/test_toolrun.py` |
| `atri_x.agent.toolrun_prompt_provider` | `tests/test_agent_tools.py` |
| `atri_x.agent.tools` | `tests/test_agent_assembly.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py` |
| `atri_x.agent.turn_output` | `tests/test_agent_chat.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.agent.turn_override` | `tests/test_agent_assembly.py`、`tests/test_agent_chat.py` |
| `atri_x.agent.user_prompt_source` | `tests/test_promptlib.py` |
| `atri_x.agent.vision` | `tests/test_vision_prechat.py`、`tests/test_vision_privacy.py` |
| `atri_x.agent.wakeup` | `tests/test_agent_participation.py`、`tests/test_agent_wakeup.py` |
| `atri_x.agent.workspace` | `tests/test_agent_directives.py`、`tests/test_toolrun.py` |
| `atri_x.angelmemory` | `tests/agent_commands_fakes.py`、`tests/test_agent_commands_services.py`、`tests/test_agent_commands_session.py`、`tests/test_angelmemory.py`、`tests/test_app_ops_plugins.py`、`tests/test_builtin_tools2.py` |
| `atri_x.angelmemory.service` | `tests/test_app_ops_plugins.py` |
| `atri_x.app` | `tests/test_app_bootstrap.py`、`tests/test_app_doctor_capabilities.py`、`tests/test_app_health.py`、`tests/test_app_llm_adapters.py`、`tests/test_app_ops_plugins.py`、`tests/test_app_platforms.py`、`tests/test_app_runner.py`、`tests/test_app_runtime_agent.py`、`tests/test_app_runtime_resident.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_comic_service.py`、`tests/test_deploy_harness.py`、`tests/test_llm_headers.py`、`tests/test_main.py`、`tests/test_member_profile.py`、`tests/test_ops_health.py`、`tests/test_packaging_tzdata.py`、`tests/test_promptlib.py`、`tests/test_safety_integration.py`、`tests/test_session_lifecycle.py`、`tests/test_time_config.py` |
| `atri_x.app.app` | `tests/test_app_bootstrap.py`、`tests/test_app_runner.py` |
| `atri_x.app.breaker_llm` | `tests/test_app_llm_adapters.py` |
| `atri_x.app.cli_client` | `tests/test_app_platforms.py` |
| `atri_x.app.comic` | `tests/test_comic_service.py` |
| `atri_x.app.config_check` | `tests/test_app_platforms.py`、`tests/test_time_config.py` |
| `atri_x.app.cron` | `tests/test_app_bootstrap.py` |
| `atri_x.app.dependencies` | `tests/test_app_platforms.py`、`tests/test_app_runner.py`、`tests/test_comic_service.py` |
| `atri_x.app.doctor` | `tests/test_app_doctor_capabilities.py`、`tests/test_app_platforms.py` |
| `atri_x.app.doctor_checks` | `tests/test_app_platforms.py` |
| `atri_x.app.environment` | `tests/test_app_bootstrap.py` |
| `atri_x.app.foundation` | `tests/test_app_bootstrap.py` |
| `atri_x.app.health` | `tests/test_app_health.py` |
| `atri_x.app.health_handler` | `tests/test_app_health.py` |
| `atri_x.app.health_llm` | `tests/test_app_llm_adapters.py` |
| `atri_x.app.image_rewriter` | `tests/test_app_platforms.py`、`tests/test_promptlib.py` |
| `atri_x.app.integrations` | `tests/test_app_platforms.py`、`tests/test_app_runner.py` |
| `atri_x.app.models` | `tests/test_app_llm_adapters.py`、`tests/test_llm_headers.py` |
| `atri_x.app.naming` | `tests/test_session_lifecycle.py` |
| `atri_x.app.ops_health` | `tests/test_app_health.py`、`tests/test_app_ops_plugins.py`、`tests/test_app_runtime_agent.py`、`tests/test_member_profile.py`、`tests/test_ops_health.py` |
| `atri_x.app.platform_caller` | `tests/test_app_platforms.py` |
| `atri_x.app.platforms` | `tests/test_app_platforms.py`、`tests/test_deploy_harness.py` |
| `atri_x.app.runner` | `tests/test_app_ops_plugins.py`、`tests/test_app_runner.py` |
| `atri_x.app.runtime` | `tests/test_app_runner.py`、`tests/test_app_runtime_resident.py`、`tests/test_app_tool_runtime_wiring.py` |
| `atri_x.app.runtime_agent` | `tests/test_app_runtime_agent.py`、`tests/test_member_profile.py`、`tests/test_safety_integration.py` |
| `atri_x.app.service_marker` | `tests/test_app_bootstrap.py` |
| `atri_x.app.service_types` | `tests/test_app_platforms.py` |
| `atri_x.app.support` | `tests/test_app_runner.py` |
| `atri_x.app.time_check` | `tests/test_packaging_tzdata.py` |
| `atri_x.app.tool_runtime` | `tests/test_app_runtime_agent.py`、`tests/test_app_runtime_resident.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_member_profile.py` |
| `atri_x.app.tool_runtime_comic` | `tests/test_comic_service.py` |
| `atri_x.artist` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_artist_metadata.py`、`tests/test_artist_service.py`、`tests/test_builtin_character_artist.py`、`tests/test_builtin_imagegen.py`、`tests/test_media_manager.py` |
| `atri_x.artist.metadata` | `tests/test_artist_metadata.py`、`tests/test_media_manager.py` |
| `atri_x.artist.metadata_formats` | `tests/test_artist_metadata.py` |
| `atri_x.artist.search` | `tests/test_artist_service.py` |
| `atri_x.artist.types` | `tests/test_artist_metadata.py` |
| `atri_x.asr` | `tests/test_asr.py` |
| `atri_x.atriena` | `tests/_atrinis_support.py`、`tests/test_atriena.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py` |
| `atri_x.atrinis` | `tests/_atrinis_support.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py` |
| `atri_x.atrinis.types` | `tests/test_atrinis_http.py` |
| `atri_x.atriyph` | `tests/_skill_fixtures.py`、`tests/test_atriyph.py`、`tests/test_tool_skill.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py` |
| `atri_x.background` | `tests/_atrinis_support.py`、`tests/test_agent_admission.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_llm.py`、`tests/test_background.py`、`tests/test_cron.py` |
| `atri_x.character` | `tests/_comic_fakes.py`、`tests/agent_commands_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_builtin_character_artist.py`、`tests/test_builtin_imagegen.py`、`tests/test_character.py`、`tests/test_character_archive.py`、`tests/test_comic_characters.py`、`tests/test_comic_service.py`、`tests/test_member_persona.py` |
| `atri_x.character.activation` | `tests/test_character_archive.py` |
| `atri_x.character.archive` | `tests/test_character_archive.py` |
| `atri_x.character.search` | `tests/test_character.py` |
| `atri_x.character.write` | `tests/test_character.py` |
| `atri_x.comic` | `tests/_comic_fakes.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_tools4.py`、`tests/test_comic_characters.py`、`tests/test_comic_context.py`、`tests/test_comic_models.py`、`tests/test_comic_planner.py`、`tests/test_comic_prompt.py`、`tests/test_comic_service.py` |
| `atri_x.comic.characters` | `tests/test_comic_models.py` |
| `atri_x.comic.context` | `tests/test_comic_models.py` |
| `atri_x.comic.errors` | `tests/test_comic_models.py` |
| `atri_x.comic.models` | `tests/test_comic_models.py` |
| `atri_x.comic.options` | `tests/test_comic_models.py` |
| `atri_x.comic.planner` | `tests/test_comic_models.py`、`tests/test_comic_service.py` |
| `atri_x.comic.prompt` | `tests/test_comic_models.py` |
| `atri_x.comic.records` | `tests/test_comic_models.py` |
| `atri_x.comic.references` | `tests/test_comic_models.py`、`tests/test_comic_service.py` |
| `atri_x.comic.service` | `tests/test_comic_models.py` |
| `atri_x.comic.store` | `tests/test_comic_models.py` |
| `atri_x.command` | `tests/agent_commands_fakes.py`、`tests/test_agent_commands.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_commands_services.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_command_router.py`、`tests/test_completion.py`、`tests/test_security_grants.py` |
| `atri_x.completion` | `tests/test_completion.py`、`tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py` |
| `atri_x.completion.completion` | `tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py` |
| `atri_x.config` | `tests/_atrinis_support.py`、`tests/agent_fakes.py`、`tests/test_agent_admission.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_budget.py`、`tests/test_agent_chat.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_core.py`、`tests/test_agent_directives.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_model.py`、`tests/test_agent_participation.py`、`tests/test_agent_prompt.py`、`tests/test_agent_reactions.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py`、`tests/test_app_llm_adapters.py`、`tests/test_app_runtime_agent.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py`、`tests/test_builtin_tools4.py`、`tests/test_comic_service.py`、`tests/test_config.py`、`tests/test_config_assets.py`、`tests/test_config_loader.py`、`tests/test_config_state.py`、`tests/test_config_web_search.py`、`tests/test_daily_summary.py`、`tests/test_deploy_harness_cli.py`、`tests/test_dsh_route.py`、`tests/test_groupkb.py`、`tests/test_maintenance.py`、`tests/test_maintenance_dsh.py`、`tests/test_maintenance_report.py`、`tests/test_member_profile.py`、`tests/test_request_manager.py`、`tests/test_safety_core.py`、`tests/test_time_config.py`、`tests/test_time_service.py`、`tests/test_timesync_https.py`、`tests/test_timesync_sntp.py`、`tests/test_timesync_verifier.py`、`tests/test_tool_runtimeinfo.py`、`tests/test_toolrun.py` |
| `atri_x.config.app` | `tests/test_config.py`、`tests/test_config_loader.py`、`tests/test_config_web_search.py`、`tests/test_time_config.py` |
| `atri_x.config.app_sections` | `tests/test_agent_admission.py`、`tests/test_agent_core.py`、`tests/test_agent_participation.py`、`tests/test_agent_reactions.py`、`tests/test_config.py`、`tests/test_config_state.py`、`tests/test_config_web_search.py`、`tests/test_request_manager.py`、`tests/test_safety_core.py`、`tests/test_toolrun.py` |
| `atri_x.config.assets` | `tests/test_config.py`、`tests/test_config_assets.py` |
| `atri_x.config.comic` | `tests/test_config.py` |
| `atri_x.config.inspect` | `tests/test_config.py`、`tests/test_time_config.py` |
| `atri_x.config.loader` | `tests/test_config.py`、`tests/test_config_loader.py`、`tests/test_deploy_harness_cli.py` |
| `atri_x.config.services` | `tests/test_config.py`、`tests/test_config_loader.py`、`tests/test_config_web_search.py` |
| `atri_x.config.state` | `tests/test_config_state.py`、`tests/test_groupkb.py` |
| `atri_x.config.time_sections` | `tests/test_time_config.py`、`tests/test_time_service.py`、`tests/test_timesync_https.py`、`tests/test_timesync_sntp.py`、`tests/test_timesync_verifier.py` |
| `atri_x.contextmgr` | `tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_contextmgr.py`、`tests/test_contextmgr_window_metadata.py` |
| `atri_x.contextmgr.budget` | `tests/test_contextmgr.py`、`tests/test_contextmgr_window_metadata.py` |
| `atri_x.contextmgr.compact_prompt` | `tests/test_contextmgr.py` |
| `atri_x.contextmgr.compressor` | `tests/test_contextmgr.py` |
| `atri_x.contextmgr.loader` | `tests/test_contextmgr.py` |
| `atri_x.contextmgr.usage` | `tests/test_contextmgr.py` |
| `atri_x.contextmgr.window` | `tests/test_contextmgr.py`、`tests/test_contextmgr_window_metadata.py` |
| `atri_x.cron` | `tests/test_agent_admission.py`、`tests/test_agent_cron.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_cron.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_cron_dst.py`、`tests/test_maintenance.py` |
| `atri_x.cron.execution` | `tests/test_agent_admission.py` |
| `atri_x.cron.manager` | `tests/test_cron.py` |
| `atri_x.cron.model` | `tests/test_cron_dst.py` |
| `atri_x.cron.service` | `tests/test_agent_cron.py` |
| `atri_x.delivery` | `tests/_atrinis_support.py`、`tests/agent_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_core.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_reactions.py`、`tests/test_app_platforms.py`、`tests/test_atriena.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py`、`tests/test_background.py`、`tests/test_builtin_tools3.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_delivery.py`、`tests/test_hook_manager.py`、`tests/test_hook_output.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py`、`tests/test_media_manager.py`、`tests/test_media_platform.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_qq_onebot.py`、`tests/test_platform_qqofficial.py`、`tests/test_request_manager.py`、`tests/test_safety_integration.py` |
| `atri_x.delivery.delivery` | `tests/test_background.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py`、`tests/test_media_manager.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_qq_onebot.py`、`tests/test_platform_qqofficial.py` |
| `atri_x.diagram` | `tests/test_diagram_pack.py`、`tests/test_diagram_skill.py` |
| `atri_x.diagram.bootstrap` | `tests/test_diagram_pack.py`、`tests/test_diagram_skill.py` |
| `atri_x.diagram.pack` | `tests/test_diagram_pack.py` |
| `atri_x.diagram.paths` | `tests/test_diagram_pack.py` |
| `atri_x.diagram.runner` | `tests/test_diagram_pack.py` |
| `atri_x.directive` | `tests/test_completion.py`、`tests/test_directive.py` |
| `atri_x.directive.tool` | `tests/test_completion.py` |
| `atri_x.errors` | `tests/_atrinis_support.py`、`tests/_comic_fakes.py`、`tests/agent_commands_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_budget.py`、`tests/test_agent_chat.py`、`tests/test_agent_commands.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_commands_session.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_inbound_media.py`、`tests/test_agent_model.py`、`tests/test_agent_prompt.py`、`tests/test_angelmemory.py`、`tests/test_app_bootstrap.py`、`tests/test_app_health.py`、`tests/test_app_llm_adapters.py`、`tests/test_app_platforms.py`、`tests/test_app_runner.py`、`tests/test_artist_service.py`、`tests/test_atriena.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_files.py`、`tests/test_builtin_long_memory.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_builtin_shell.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_character.py`、`tests/test_character_archive.py`、`tests/test_comic_characters.py`、`tests/test_contextmgr.py`、`tests/test_cron.py`、`tests/test_cron_dst.py`、`tests/test_dsh_route.py`、`tests/test_fileops.py`、`tests/test_groupanalysis.py`、`tests/test_historygate.py`、`tests/test_launcher_cli.py`、`tests/test_llm_breaker.py`、`tests/test_logging.py`、`tests/test_long_memory_scope.py`、`tests/test_main.py`、`tests/test_maintenance.py`、`tests/test_maintenance_report.py`、`tests/test_media_lifecycle.py`、`tests/test_media_manager.py`、`tests/test_media_resolver.py`、`tests/test_memory_notes.py`、`tests/test_ops.py`、`tests/test_platform_builtin.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py`、`tests/test_platform_headless.py`、`tests/test_platform_qq_onebot.py`、`tests/test_platform_qqofficial.py`、`tests/test_qq_onebot_backend.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py`、`tests/test_request_manager.py`、`tests/test_resident_memory.py`、`tests/test_selflearning.py`、`tests/test_session.py`、`tests/test_session_lifecycle.py`、`tests/test_signal.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_migrations.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py`、`tests/test_time_service.py`、`tests/test_timesync_sntp.py`、`tests/test_timezones.py`、`tests/test_tool_discover.py`、`tests/test_tool_media_runtime.py`、`tests/test_tool_runtimeinfo.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py`、`tests/test_toolrun_denied.py`、`tests/test_vision.py`、`tests/test_vision_attributes.py`、`tests/test_vision_prechat.py`、`tests/test_vision_privacy.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.gpu_usage` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.groupanalysis` | `tests/test_groupanalysis.py` |
| `atri_x.groupanalysis.summarizer` | `tests/test_groupanalysis.py` |
| `atri_x.groupkb` | `tests/test_groupkb.py` |
| `atri_x.gui` | `tests/test_cli_sessions.py`、`tests/test_gui_client.py`、`tests/test_gui_configedit.py`、`tests/test_gui_diagnostics.py`、`tests/test_gui_extractconfig.py`、`tests/test_gui_icon.py`、`tests/test_gui_imagemount.py`、`tests/test_gui_link.py`、`tests/test_gui_maintenance.py`、`tests/test_gui_members.py`、`tests/test_gui_modelcatalog.py`、`tests/test_gui_promptlib.py`、`tests/test_gui_proto_manager.py`、`tests/test_gui_protocol.py`、`tests/test_gui_qqproto.py`、`tests/test_gui_searchconfig.py`、`tests/test_gui_service.py`、`tests/test_gui_tabs.py`、`tests/test_gui_timepanel.py`、`tests/test_gui_toolsettings.py` |
| `atri_x.gui.__main__` | `tests/test_gui_diagnostics.py` |
| `atri_x.gui.about` | `tests/test_gui_tabs.py` |
| `atri_x.gui.actions` | `tests/test_gui_diagnostics.py` |
| `atri_x.gui.app` | `tests/test_gui_diagnostics.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.avatars` | `tests/test_gui_members.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.client` | `tests/test_gui_client.py` |
| `atri_x.gui.configedit` | `tests/test_gui_configedit.py`、`tests/test_gui_extractconfig.py`、`tests/test_gui_link.py`、`tests/test_gui_maintenance.py`、`tests/test_gui_modelcatalog.py`、`tests/test_gui_promptlib.py`、`tests/test_gui_proto_manager.py`、`tests/test_gui_qqproto.py`、`tests/test_gui_searchconfig.py`、`tests/test_gui_tabs.py`、`tests/test_gui_timepanel.py`、`tests/test_gui_toolsettings.py` |
| `atri_x.gui.configmodel` | `tests/test_gui_configedit.py`、`tests/test_gui_modelcatalog.py` |
| `atri_x.gui.cronschedule` | `tests/test_gui_maintenance.py` |
| `atri_x.gui.dialogs` | `tests/test_gui_maintenance.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.dialogs.tasks` | `tests/test_gui_maintenance.py` |
| `atri_x.gui.extractconfig` | `tests/test_gui_extractconfig.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.featureview` | `tests/test_gui_configedit.py`、`tests/test_gui_tabs.py`、`tests/test_gui_timepanel.py` |
| `atri_x.gui.icon` | `tests/test_gui_icon.py` |
| `atri_x.gui.imagemount` | `tests/test_gui_imagemount.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.imageprofileedit` | `tests/test_gui_promptlib.py` |
| `atri_x.gui.journal` | `tests/test_gui_diagnostics.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.linkstate` | `tests/test_gui_link.py`、`tests/test_gui_qqproto.py` |
| `atri_x.gui.memberdata` | `tests/test_gui_members.py`、`tests/test_gui_promptlib.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.modelcatalog` | `tests/test_gui_modelcatalog.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.process` | `tests/test_gui_diagnostics.py` |
| `atri_x.gui.promptedit` | `tests/test_gui_promptlib.py` |
| `atri_x.gui.proto_manager` | `tests/test_gui_proto_manager.py` |
| `atri_x.gui.proto_process` | `tests/test_gui_proto_manager.py` |
| `atri_x.gui.proto_setup` | `tests/test_gui_proto_manager.py`、`tests/test_gui_qqproto.py` |
| `atri_x.gui.protocol` | `tests/test_cli_sessions.py`、`tests/test_gui_diagnostics.py`、`tests/test_gui_protocol.py` |
| `atri_x.gui.qqproto` | `tests/test_gui_proto_manager.py`、`tests/test_gui_qqproto.py` |
| `atri_x.gui.searchconfig` | `tests/test_gui_searchconfig.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.service` | `tests/test_gui_link.py`、`tests/test_gui_qqproto.py`、`tests/test_gui_service.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.sessions` | `tests/test_cli_sessions.py` |
| `atri_x.gui.snowluma_proto` | `tests/test_gui_proto_manager.py` |
| `atri_x.gui.tabs` | `tests/test_gui_link.py`、`tests/test_gui_promptlib.py`、`tests/test_gui_qqproto.py`、`tests/test_gui_tabs.py`、`tests/test_gui_timepanel.py` |
| `atri_x.gui.tabs.advanced` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.character` | `tests/test_gui_promptlib.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.features` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.knowledge` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.link` | `tests/test_gui_link.py`、`tests/test_gui_qqproto.py` |
| `atri_x.gui.tabs.logs` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.members` | `tests/test_gui_promptlib.py`、`tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.model` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.platform` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tabs.promptlib` | `tests/test_gui_promptlib.py` |
| `atri_x.gui.tabs.timepanel` | `tests/test_gui_timepanel.py` |
| `atri_x.gui.tabs.toolperm` | `tests/test_gui_tabs.py` |
| `atri_x.gui.taglibedit` | `tests/test_gui_promptlib.py` |
| `atri_x.gui.timeview` | `tests/test_gui_timepanel.py` |
| `atri_x.gui.toolcatalog` | `tests/test_gui_tabs.py`、`tests/test_gui_toolsettings.py` |
| `atri_x.gui.toolparams` | `tests/test_gui_tabs.py` |
| `atri_x.gui.toolpolicy` | `tests/test_gui_tabs.py` |
| `atri_x.gui.toolsettings` | `tests/test_gui_tabs.py`、`tests/test_gui_toolsettings.py` |
| `atri_x.gui.toolsettings_base` | `tests/test_gui_tabs.py` |
| `atri_x.gui.tooltagedit` | `tests/test_gui_promptlib.py` |
| `atri_x.gui.widgets` | `tests/test_gui_tabs.py` |
| `atri_x.hardware` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.health` | `tests/test_app_ops_plugins.py`、`tests/test_health.py` |
| `atri_x.health.checker` | `tests/test_health.py` |
| `atri_x.health.server` | `tests/test_app_ops_plugins.py`、`tests/test_health.py` |
| `atri_x.health.state` | `tests/test_app_ops_plugins.py`、`tests/test_health.py` |
| `atri_x.historygate` | `tests/test_builtin_tools3.py`、`tests/test_historygate.py` |
| `atri_x.hook` | `tests/agent_commands_fakes.py`、`tests/test_agent_chat.py`、`tests/test_app_platforms.py`、`tests/test_hook_builtin_control.py`、`tests/test_hook_event.py`、`tests/test_hook_manager.py`、`tests/test_hook_match.py`、`tests/test_hook_output.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py`、`tests/test_safety_integration.py` |
| `atri_x.hook.builtin` | `tests/test_hook_builtin_control.py` |
| `atri_x.hook.control` | `tests/test_hook_builtin_control.py` |
| `atri_x.hook.event` | `tests/test_app_platforms.py`、`tests/test_hook_builtin_control.py`、`tests/test_hook_event.py`、`tests/test_hook_manager.py`、`tests/test_hook_match.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py` |
| `atri_x.hook.manager` | `tests/agent_commands_fakes.py`、`tests/test_agent_chat.py`、`tests/test_hook_builtin_control.py`、`tests/test_hook_manager.py`、`tests/test_hook_rules.py` |
| `atri_x.hook.match` | `tests/test_hook_builtin_control.py`、`tests/test_hook_manager.py`、`tests/test_hook_match.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py` |
| `atri_x.hook.output` | `tests/test_hook_output.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py` |
| `atri_x.hook.output.jsonstrict` | `tests/test_hook_output.py` |
| `atri_x.hook.protocol` | `tests/test_hook_protocol.py`、`tests/test_hook_runtime.py` |
| `atri_x.hook.protocol.frames` | `tests/test_hook_runtime.py` |
| `atri_x.hook.rules` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.config` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.detail` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.rule` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.rules` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.toml_error` | `tests/test_hook_rules.py` |
| `atri_x.hook.rules.types` | `tests/test_hook_rules.py` |
| `atri_x.hook.runtime` | `tests/test_hook_builtin_control.py`、`tests/test_hook_runtime.py` |
| `atri_x.hook.runtime.routes` | `tests/test_hook_runtime.py` |
| `atri_x.hook.runtime.shared` | `tests/test_hook_runtime.py` |
| `atri_x.hook.runtime.tool_bridge` | `tests/test_hook_runtime.py` |
| `atri_x.imagegen` | `tests/_comic_fakes.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_tools4.py`、`tests/test_comic_service.py`、`tests/test_imagegen.py`、`tests/test_safety_integration.py` |
| `atri_x.imagegen._extract_library` | `tests/test_imagegen.py` |
| `atri_x.imagegen.client` | `tests/test_imagegen.py` |
| `atri_x.imagegen.optimize` | `tests/test_imagegen.py` |
| `atri_x.imagegen.search` | `tests/test_imagegen.py` |
| `atri_x.launcher` | `tests/test_launcher_cli.py` |
| `atri_x.launcher.cli` | `tests/test_launcher_cli.py` |
| `atri_x.llm` | `tests/_vision_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_budget.py`、`tests/test_agent_chat.py`、`tests/test_agent_chat_anomaly.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_directives.py`、`tests/test_agent_prompt.py`、`tests/test_agent_risk.py`、`tests/test_agent_segments.py`、`tests/test_agent_title.py`、`tests/test_agent_tools.py`、`tests/test_app_llm_adapters.py`、`tests/test_app_platforms.py`、`tests/test_app_runtime_agent.py`、`tests/test_background.py`、`tests/test_builtin_tools3.py`、`tests/test_completion.py`、`tests/test_contextmgr.py`、`tests/test_contextmgr_window_metadata.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_dsh_route.py`、`tests/test_groupanalysis.py`、`tests/test_gui_protocol.py`、`tests/test_hook_builtin_control.py`、`tests/test_hook_event.py`、`tests/test_hook_match.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py`、`tests/test_llm_breaker.py`、`tests/test_llm_errors.py`、`tests/test_llm_headers.py`、`tests/test_llm_protocol.py`、`tests/test_llm_tooltext.py`、`tests/test_media_resolver.py`、`tests/test_memory_notes.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_platform_cli.py`、`tests/test_qq_slowfetch.py`、`tests/test_safety_integration.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py`、`tests/test_time_context.py`、`tests/test_tool_core.py`、`tests/test_toolrun.py`、`tests/test_toolrun_atriwisp_args.py`、`tests/test_turn_manager.py`、`tests/test_usage_attribution.py`、`tests/test_vision.py`、`tests/test_vision_attributes.py`、`tests/test_vision_prechat.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py` |
| `atri_x.llm.breaker` | `tests/test_app_llm_adapters.py`、`tests/test_llm_breaker.py` |
| `atri_x.llm.errors` | `tests/test_agent_chat.py`、`tests/test_llm_errors.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_vision.py`、`tests/test_vision_privacy.py` |
| `atri_x.llm.llm` | `tests/_vision_fakes.py`、`tests/test_agent_budget.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_directives.py`、`tests/test_agent_prompt.py`、`tests/test_agent_title.py`、`tests/test_background.py`、`tests/test_builtin_tools3.py`、`tests/test_completion.py`、`tests/test_contextmgr.py`、`tests/test_contextmgr_window_metadata.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_groupanalysis.py`、`tests/test_gui_protocol.py`、`tests/test_hook_builtin_control.py`、`tests/test_hook_event.py`、`tests/test_hook_match.py`、`tests/test_hook_protocol.py`、`tests/test_hook_rules.py`、`tests/test_hook_runtime.py`、`tests/test_media_resolver.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_platform_cli.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py`、`tests/test_tool_core.py`、`tests/test_toolrun.py`、`tests/test_toolrun_atriwisp_args.py`、`tests/test_turn_manager.py`、`tests/test_vision.py`、`tests/test_vision_attributes.py`、`tests/test_vision_prechat.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py` |
| `atri_x.llm.openai` | `tests/test_app_llm_adapters.py`、`tests/test_llm_headers.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_vision_attributes.py` |
| `atri_x.llm.openai.openai` | `tests/test_llm_headers.py`、`tests/test_openai_adapter.py` |
| `atri_x.llm.openai.responses` | `tests/test_llm_headers.py`、`tests/test_openai_responses.py`、`tests/test_vision_attributes.py` |
| `atri_x.llm.openai.stream` | `tests/test_openai_adapter.py` |
| `atri_x.llm.openai.wire` | `tests/test_llm_headers.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_vision_attributes.py` |
| `atri_x.llm.origin` | `tests/test_llm_protocol.py` |
| `atri_x.llm.protocol` | `tests/test_agent_context_runtime.py`、`tests/test_llm_protocol.py`、`tests/test_openai_adapter.py` |
| `atri_x.llm.tooltext` | `tests/test_agent_chat_anomaly.py`、`tests/test_llm_tooltext.py` |
| `atri_x.llm.usage_sink` | `tests/test_app_runtime_agent.py`、`tests/test_usage_attribution.py` |
| `atri_x.logging` | `tests/test_agent_commands_services.py`、`tests/test_agent_directives.py`、`tests/test_app_runtime_agent.py`、`tests/test_delivery.py`、`tests/test_log_reader.py`、`tests/test_logging.py`、`tests/test_maintenance_report.py`、`tests/test_usage_attribution.py` |
| `atri_x.logging.contract` | `tests/test_logging.py` |
| `atri_x.logging.logging` | `tests/test_app_runtime_agent.py`、`tests/test_delivery.py`、`tests/test_logging.py` |
| `atri_x.logging.reader` | `tests/test_app_runtime_agent.py`、`tests/test_log_reader.py`、`tests/test_logging.py` |
| `atri_x.maintenance` | `tests/test_daily_summary.py`、`tests/test_maintenance.py`、`tests/test_maintenance_dsh.py`、`tests/test_maintenance_report.py` |
| `atri_x.maintenance.dsh` | `tests/test_maintenance_dsh.py` |
| `atri_x.maintenance.report` | `tests/test_maintenance_report.py` |
| `atri_x.maintenance.summary` | `tests/test_daily_summary.py` |
| `atri_x.media` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_atriena.py`、`tests/test_builtin_tools3.py`、`tests/test_gui_members.py`、`tests/test_gui_tabs.py`、`tests/test_imagecodec.py`、`tests/test_imagecodec_jpeg_precision.py`、`tests/test_media_image.py`、`tests/test_media_lifecycle.py`、`tests/test_media_manager.py`、`tests/test_media_platform.py`、`tests/test_media_resolver.py`、`tests/test_media_worker.py`、`tests/test_ops_health.py`、`tests/test_tool_media_runtime.py`、`tests/test_toolrun.py`、`tests/test_vision_privacy.py` |
| `atri_x.media.imagecodec` | `tests/test_gui_members.py`、`tests/test_gui_tabs.py`、`tests/test_imagecodec.py`、`tests/test_imagecodec_jpeg_precision.py`、`tests/test_media_image.py`、`tests/test_media_manager.py`、`tests/test_ops_health.py` |
| `atri_x.media.imagecodec.backend_probe` | `tests/test_imagecodec_jpeg_precision.py`、`tests/test_ops_health.py` |
| `atri_x.media.imagecodec.image` | `tests/test_imagecodec_jpeg_precision.py`、`tests/test_ops_health.py` |
| `atri_x.media.imagecodec.jpeg_decode` | `tests/test_imagecodec.py`、`tests/test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.jpeg_encode` | `tests/test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.png` | `tests/test_imagecodec.py` |
| `atri_x.media.manager` | `tests/test_toolrun.py` |
| `atri_x.media.platform` | `tests/test_media_platform.py` |
| `atri_x.media.types` | `tests/test_media_platform.py`、`tests/test_toolrun.py` |
| `atri_x.media.worker` | `tests/test_media_image.py`、`tests/test_media_worker.py` |
| `atri_x.memberprofile` | `tests/test_member_persona.py`、`tests/test_member_profile.py`、`tests/test_promptlib.py` |
| `atri_x.memberprofile.model` | `tests/test_promptlib.py` |
| `atri_x.memberprofile.persona` | `tests/test_member_persona.py` |
| `atri_x.memberprofile.prompt` | `tests/test_promptlib.py` |
| `atri_x.memory` | `tests/test_app_runtime_resident.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools4.py`、`tests/test_memory_notes.py`、`tests/test_resident_memory.py` |
| `atri_x.memory.notes` | `tests/test_builtin_tools2.py`、`tests/test_memory_notes.py` |
| `atri_x.memory.resident` | `tests/test_app_runtime_resident.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools4.py`、`tests/test_memory_notes.py`、`tests/test_resident_memory.py` |
| `atri_x.memory.resident.atomic` | `tests/test_resident_memory.py` |
| `atri_x.memory.resident.entries` | `tests/test_builtin_tools2.py` |
| `atri_x.memory.resident.store` | `tests/test_app_runtime_resident.py` |
| `atri_x.net` | `tests/test_atrinis_http.py`、`tests/test_config_state.py`、`tests/test_cron_dst.py`、`tests/test_cronspec.py`、`tests/test_gui_client.py`、`tests/test_gui_service.py`、`tests/test_httpclient.py`、`tests/test_media_resolver.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_qqofficial.py`、`tests/test_qq_onebot_smoke.py`、`tests/test_s3.py`、`tests/test_tomlw.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py`、`tests/test_websocket.py` |
| `atri_x.net.cronspec` | `tests/test_cron_dst.py`、`tests/test_cronspec.py` |
| `atri_x.net.httpclient` | `tests/test_atrinis_http.py`、`tests/test_httpclient.py`、`tests/test_media_resolver.py`、`tests/test_openai_adapter.py`、`tests/test_openai_responses.py`、`tests/test_platform_qqofficial.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.net.s3` | `tests/test_s3.py` |
| `atri_x.net.tomlw` | `tests/test_config_state.py`、`tests/test_tomlw.py` |
| `atri_x.net.websocket` | `tests/test_gui_client.py`、`tests/test_gui_service.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_qqofficial.py`、`tests/test_qq_onebot_smoke.py`、`tests/test_websocket.py` |
| `atri_x.ops` | `tests/test_agent_bootstrap.py`、`tests/test_agent_directives.py`、`tests/test_builtin_imagegen.py`、`tests/test_media_manager.py`、`tests/test_ops.py`、`tests/test_ops_executors.py` |
| `atri_x.ops.concurrency` | `tests/test_builtin_imagegen.py`、`tests/test_ops.py` |
| `atri_x.ops.diskguard` | `tests/test_media_manager.py`、`tests/test_ops.py` |
| `atri_x.ops.executors` | `tests/test_ops_executors.py` |
| `atri_x.ops.ratelimit` | `tests/test_agent_bootstrap.py`、`tests/test_agent_directives.py`、`tests/test_ops.py` |
| `atri_x.platform` | `tests/_comic_fakes.py`、`tests/test_agent_chat.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_core.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_inbound_media.py`、`tests/test_agent_participation.py`、`tests/test_agent_wakeup.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_cli_sessions.py`、`tests/test_gui_client.py`、`tests/test_gui_protocol.py`、`tests/test_long_memory_scope.py`、`tests/test_media_manager.py`、`tests/test_media_platform.py`、`tests/test_member_profile.py`、`tests/test_memory_notes.py`、`tests/test_platform_builtin.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py`、`tests/test_platform_core.py`、`tests/test_platform_headless.py`、`tests/test_platform_qq_onebot.py`、`tests/test_platform_qqofficial.py`、`tests/test_qq_onebot_backend.py`、`tests/test_qq_onebot_smoke.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py`、`tests/test_refcontext.py` |
| `atri_x.platform.builtin` | `tests/test_platform_builtin.py` |
| `atri_x.platform.cli` | `tests/test_cli_sessions.py`、`tests/test_gui_client.py`、`tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.adapter` | `tests/test_platform_cli.py` |
| `atri_x.platform.cli.config` | `tests/test_cli_sessions.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.platform.cli.local_file` | `tests/test_platform_cli.py`、`tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.protocol` | `tests/test_cli_sessions.py`、`tests/test_gui_client.py`、`tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_client` | `tests/test_platform_cli_remote.py` |
| `atri_x.platform.cli.remote_server` | `tests/test_cli_sessions.py`、`tests/test_platform_cli_remote.py` |
| `atri_x.platform.cli.tui` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.copy` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.input` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.keys` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.messages` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.model` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.program` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.style` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.cli.tui.viewport` | `tests/test_platform_cli_tui.py` |
| `atri_x.platform.headless` | `tests/test_platform_headless.py` |
| `atri_x.platform.media` | `tests/test_media_manager.py` |
| `atri_x.platform.platform` | `tests/test_agent_inbound_media.py`、`tests/test_media_manager.py`、`tests/test_media_platform.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_qq_onebot.py`、`tests/test_platform_qqofficial.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py`、`tests/test_refcontext.py` |
| `atri_x.platform.qq_onebot` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_backend.py`、`tests/test_qq_onebot_smoke.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.adapter` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.attachment` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.backend` | `tests/test_qq_onebot_backend.py`、`tests/test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.capabilities` | `tests/test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.config` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_backend.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.forward` | `tests/test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.inbound` | `tests/test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.message` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_snowluma.py` |
| `atri_x.platform.qq_onebot.output` | `tests/test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.preprocess` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.slowfetch` | `tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.smoke` | `tests/test_qq_onebot_smoke.py` |
| `atri_x.platform.qq_onebot.target` | `tests/test_platform_qq_onebot.py` |
| `atri_x.platform.qq_onebot.transport` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qq_onebot.types` | `tests/test_platform_qq_onebot.py`、`tests/test_qq_onebot_smoke.py`、`tests/test_qq_onebot_snowluma.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.platform.qqofficial` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.adapter` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.attachment` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.client` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.config` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.gateway` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.keyboard` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.message` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.send` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.target` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.token` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.qqofficial.types` | `tests/test_platform_qqofficial.py` |
| `atri_x.platform.refcontext` | `tests/test_refcontext.py` |
| `atri_x.processenv` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_shell.py`、`tests/test_tool_skill.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.promptlib` | `tests/test_gui_promptlib.py`、`tests/test_promptlib.py` |
| `atri_x.promptlib.defaults` | `tests/test_promptlib.py` |
| `atri_x.promptlib.library` | `tests/test_promptlib.py` |
| `atri_x.promptlib.model` | `tests/test_promptlib.py` |
| `atri_x.promptlib.render` | `tests/test_promptlib.py` |
| `atri_x.promptlib.store` | `tests/test_promptlib.py` |
| `atri_x.promptlib.tags` | `tests/test_promptlib.py` |
| `atri_x.ratelimit` | `tests/test_builtin_tools2.py`、`tests/test_ops.py` |
| `atri_x.reaction` | `tests/test_reaction.py` |
| `atri_x.reaction.catalog` | `tests/test_reaction.py` |
| `atri_x.redact` | `tests/test_redact.py`、`tests/test_redact_idempotent.py` |
| `atri_x.request` | `tests/agent_commands_fakes.py`、`tests/test_agent_commands_session.py`、`tests/test_agent_context_runtime.py`、`tests/test_request_manager.py` |
| `atri_x.request.manager` | `tests/test_agent_context_runtime.py` |
| `atri_x.research` | `tests/test_research_bootstrap.py` |
| `atri_x.research.bootstrap` | `tests/test_research_bootstrap.py` |
| `atri_x.research.pack` | `tests/test_research_bootstrap.py` |
| `atri_x.research.paths` | `tests/test_research_bootstrap.py` |
| `atri_x.research.runner` | `tests/test_research_bootstrap.py` |
| `atri_x.runtime` | `tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_core.py`、`tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py`、`tests/test_turn_manager.py` |
| `atri_x.runtime.status` | `tests/test_gui_protocol.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py` |
| `atri_x.safecontext` | `tests/test_agent_chat.py`、`tests/test_agent_core.py`、`tests/test_atrinis_http.py`、`tests/test_fileops.py`、`tests/test_groupanalysis.py`、`tests/test_maintenance.py`、`tests/test_maintenance_dsh.py`、`tests/test_media_image.py`、`tests/test_media_manager.py`、`tests/test_media_worker.py`、`tests/test_ops.py`、`tests/test_platform_cli.py`、`tests/test_platform_cli_remote.py`、`tests/test_platform_cli_tui.py`、`tests/test_request_manager.py`、`tests/test_selflearning.py`、`tests/test_signal.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py`、`tests/test_turn_manager.py`、`tests/test_vision.py`、`tests/test_vision_privacy.py` |
| `atri_x.safety` | `tests/test_safety_core.py`、`tests/test_safety_integration.py` |
| `atri_x.security` | `tests/agent_commands_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_admission.py`、`tests/test_agent_assembly.py`、`tests/test_agent_bootstrap.py`、`tests/test_agent_commands.py`、`tests/test_agent_commands_integration.py`、`tests/test_agent_commands_services.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_directives.py`、`tests/test_agent_group_policy.py`、`tests/test_agent_participation.py`、`tests/test_agent_risk.py`、`tests/test_app_runtime_agent.py`、`tests/test_background.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_cron.py`、`tests/test_hook_runtime.py`、`tests/test_long_memory_scope.py`、`tests/test_member_profile.py`、`tests/test_memory_notes.py`、`tests/test_platform_qq_onebot.py`、`tests/test_resident_memory.py`、`tests/test_security_grants.py`、`tests/test_tool_discover.py`、`tests/test_tool_skill.py`、`tests/test_toolrun.py` |
| `atri_x.selflearning` | `tests/test_app_ops_plugins.py`、`tests/test_selflearning.py` |
| `atri_x.selflearning.service` | `tests/test_app_ops_plugins.py` |
| `atri_x.session` | `tests/agent_commands_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_risk.py`、`tests/test_cli_sessions.py`、`tests/test_memory_notes.py`、`tests/test_resident_memory.py`、`tests/test_session.py`、`tests/test_session_lifecycle.py`、`tests/test_session_naming_contract.py` |
| `atri_x.session.browse` | `tests/test_cli_sessions.py` |
| `atri_x.session.naming` | `tests/test_session_naming_contract.py` |
| `atri_x.session.promotion` | `tests/test_session_lifecycle.py` |
| `atri_x.session.types` | `tests/test_agent_context_policy.py`、`tests/test_resident_memory.py` |
| `atri_x.shellparse` | `tests/test_shellparse.py` |
| `atri_x.shellparse.ast` | `tests/test_shellparse.py` |
| `atri_x.shellparse.walk` | `tests/test_shellparse.py` |
| `atri_x.sidecar` | `tests/test_sidecar_kernel.py` |
| `atri_x.sidecar.kernel` | `tests/test_sidecar_kernel.py` |
| `atri_x.sidecar.registry` | `tests/test_sidecar_kernel.py` |
| `atri_x.signal` | `tests/test_signal.py` |
| `atri_x.storage` | `tests/_atrinis_support.py`、`tests/_comic_fakes.py`、`tests/agent_fakes.py`、`tests/test_agent_admission.py`、`tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_commands_session.py`、`tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_core.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_directives.py`、`tests/test_agent_prompt.py`、`tests/test_agent_risk.py`、`tests/test_agent_title.py`、`tests/test_agent_tools.py`、`tests/test_angelmemory.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_cli_sessions.py`、`tests/test_comic_service.py`、`tests/test_contextmgr.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_cron_dst.py`、`tests/test_daily_summary.py`、`tests/test_dsh_route.py`、`tests/test_groupanalysis.py`、`tests/test_gui_members.py`、`tests/test_gui_tabs.py`、`tests/test_historygate.py`、`tests/test_hook_runtime.py`、`tests/test_maintenance.py`、`tests/test_media_lifecycle.py`、`tests/test_media_manager.py`、`tests/test_member_profile.py`、`tests/test_platform_qq_onebot.py`、`tests/test_refcontext.py`、`tests/test_selflearning.py`、`tests/test_session.py`、`tests/test_session_lifecycle.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_migrations.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py`、`tests/test_tool_media_runtime.py`、`tests/test_toolrun.py` |
| `atri_x.storage.ids` | `tests/test_builtin_cron.py`、`tests/test_cron.py`、`tests/test_media_manager.py`、`tests/test_storage_models.py` |
| `atri_x.storage.models` | `tests/test_agent_context_policy.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_directives.py`、`tests/test_agent_prompt.py`、`tests/test_agent_title.py`、`tests/test_builtin_cron.py`、`tests/test_cli_sessions.py`、`tests/test_contextmgr.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_cron_dst.py`、`tests/test_groupanalysis.py`、`tests/test_hook_runtime.py`、`tests/test_media_lifecycle.py`、`tests/test_media_manager.py`、`tests/test_member_profile.py`、`tests/test_refcontext.py`、`tests/test_session.py`、`tests/test_session_lifecycle.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py`、`tests/test_toolrun.py` |
| `atri_x.storage.repositories` | `tests/test_historygate.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py` |
| `atri_x.storage.sqlite` | `tests/_atrinis_support.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_http.py`、`tests/test_atrinis_llm.py`、`tests/test_builtin_tools3.py`、`tests/test_cli_sessions.py`、`tests/test_daily_summary.py`、`tests/test_gui_members.py`、`tests/test_gui_tabs.py`、`tests/test_historygate.py`、`tests/test_maintenance.py`、`tests/test_session.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_migrations.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py` |
| `atri_x.storage.sqlite.chat_history_repository` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_gui_members.py`、`tests/test_gui_tabs.py` |
| `atri_x.storage.sqlite.migrations` | `tests/test_sqlite_migrations.py` |
| `atri_x.storage.sqlite.store` | `tests/test_app_tool_runtime_wiring.py` |
| `atri_x.storage.storage` | `tests/test_storage_models.py` |
| `atri_x.storage.times` | `tests/test_angelmemory.py`、`tests/test_builtin_cron.py`、`tests/test_cron.py`、`tests/test_cron_delivery.py`、`tests/test_groupanalysis.py`、`tests/test_media_lifecycle.py`、`tests/test_media_manager.py`、`tests/test_platform_qq_onebot.py`、`tests/test_selflearning.py`、`tests/test_session.py`、`tests/test_session_lifecycle.py`、`tests/test_sqlite_media.py`、`tests/test_sqlite_repositories.py`、`tests/test_sqlite_store.py`、`tests/test_storage_models.py` |
| `atri_x.sysinfo` | `tests/test_builtin_runtime_resources.py`、`tests/test_maintenance_report.py`、`tests/test_ops.py` |
| `atri_x.sysinfo_format` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.sysinfo_platform` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.textmatch` | `tests/test_redact.py` |
| `atri_x.time_service` | `tests/test_time_context.py`、`tests/test_time_service.py`、`tests/test_timesync_verifier.py` |
| `atri_x.timesync` | `tests/test_timesync_https.py`、`tests/test_timesync_sntp.py`、`tests/test_timesync_verifier.py` |
| `atri_x.timesync.https_time` | `tests/test_timesync_https.py` |
| `atri_x.timesync.model` | `tests/test_timesync_https.py`、`tests/test_timesync_sntp.py` |
| `atri_x.timesync.policy` | `tests/test_timesync_sntp.py` |
| `atri_x.timesync.sntp` | `tests/test_timesync_sntp.py`、`tests/test_timesync_verifier.py` |
| `atri_x.timesync.verifier` | `tests/test_timesync_verifier.py` |
| `atri_x.timezones` | `tests/test_timezones.py` |
| `atri_x.tool` | `tests/agent_fakes.py`、`tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_directives.py`、`tests/test_agent_prompt.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py`、`tests/test_app_runtime_agent.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_background.py`、`tests/test_builtin_character_artist.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_files.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_long_memory.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_builtin_shell.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_builtin_tools4.py`、`tests/test_command_router.py`、`tests/test_completion.py`、`tests/test_diagram_pack.py`、`tests/test_hook_runtime.py`、`tests/test_long_memory_scope.py`、`tests/test_member_persona.py`、`tests/test_member_profile.py`、`tests/test_memory_notes.py`、`tests/test_qq_slowfetch.py`、`tests/test_research_bootstrap.py`、`tests/test_safety_integration.py`、`tests/test_tool_core.py`、`tests/test_tool_discover.py`、`tests/test_tool_media_runtime.py`、`tests/test_tool_runtimeinfo.py`、`tests/test_tool_skill.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py`、`tests/test_toolrun.py`、`tests/test_usage_attribution.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.tool.builder` | `tests/test_tool_discover.py` |
| `atri_x.tool.builtin` | `tests/test_app_runtime_agent.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_character_artist.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_files.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_long_memory.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_builtin_shell.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools3.py`、`tests/test_builtin_tools4.py`、`tests/test_long_memory_scope.py`、`tests/test_member_persona.py`、`tests/test_member_profile.py`、`tests/test_memory_notes.py`、`tests/test_qq_slowfetch.py`、`tests/test_safety_integration.py`、`tests/test_toolrun.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.tool.builtin.angel_memory` | `tests/test_builtin_tools2.py` |
| `atri_x.tool.builtin.artist_extract` | `tests/test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_library` | `tests/test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_tools` | `tests/test_builtin_character_artist.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_tools4.py` |
| `atri_x.tool.builtin.atriwisp_creator` | `tests/test_builtin_tools4.py` |
| `atri_x.tool.builtin.character` | `tests/test_builtin_character_artist.py`、`tests/test_builtin_imagegen.py`、`tests/test_member_persona.py` |
| `atri_x.tool.builtin.chat_history` | `tests/test_builtin_tools2.py` |
| `atri_x.tool.builtin.comic` | `tests/test_builtin_comic.py` |
| `atri_x.tool.builtin.common` | `tests/test_builtin_character_artist.py`、`tests/test_builtin_files.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_tools3.py` |
| `atri_x.tool.builtin.cron` | `tests/test_builtin_cron.py`、`tests/test_builtin_tools3.py` |
| `atri_x.tool.builtin.fetch_message` | `tests/test_qq_slowfetch.py` |
| `atri_x.tool.builtin.file_guard` | `tests/test_builtin_files.py`、`tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.file_manager` | `tests/test_app_runtime_agent.py`、`tests/test_builtin_tools4.py`、`tests/test_member_profile.py` |
| `atri_x.tool.builtin.file_rollback` | `tests/test_builtin_files.py` |
| `atri_x.tool.builtin.file_tools` | `tests/test_builtin_files.py` |
| `atri_x.tool.builtin.get_media` | `tests/test_builtin_tools3.py` |
| `atri_x.tool.builtin.group_analysis` | `tests/test_builtin_tools3.py` |
| `atri_x.tool.builtin.image_generate` | `tests/test_builtin_imagegen.py`、`tests/test_builtin_tools4.py`、`tests/test_safety_integration.py` |
| `atri_x.tool.builtin.image_generate_compose` | `tests/test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_generate_context` | `tests/test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_to_prompt` | `tests/test_builtin_imagegen.py`、`tests/test_vision_privacy.py` |
| `atri_x.tool.builtin.long_memory` | `tests/test_builtin_long_memory.py`、`tests/test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_model` | `tests/test_builtin_long_memory.py`、`tests/test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_scope` | `tests/test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_store` | `tests/test_builtin_long_memory.py`、`tests/test_long_memory_scope.py` |
| `atri_x.tool.builtin.persona` | `tests/test_member_persona.py` |
| `atri_x.tool.builtin.prompt_library` | `tests/test_builtin_tools2.py` |
| `atri_x.tool.builtin.register` | `tests/test_app_runtime_agent.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_tools2.py`、`tests/test_builtin_tools4.py`、`tests/test_member_profile.py`、`tests/test_qq_slowfetch.py` |
| `atri_x.tool.builtin.resident_memory` | `tests/test_builtin_tools2.py`、`tests/test_memory_notes.py` |
| `atri_x.tool.builtin.runtime` | `tests/test_builtin_tools4.py` |
| `atri_x.tool.builtin.runtime_resources` | `tests/test_builtin_runtime_resources.py` |
| `atri_x.tool.builtin.runtimeinfo` | `tests/test_app_runtime_agent.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_member_profile.py` |
| `atri_x.tool.builtin.self_learning` | `tests/test_builtin_tools3.py` |
| `atri_x.tool.builtin.send_file` | `tests/test_builtin_tools3.py`、`tests/test_safety_integration.py` |
| `atri_x.tool.builtin.shell` | `tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_risk` | `tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_sandbox` | `tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_syntax` | `tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_warnings` | `tests/test_builtin_shell.py` |
| `atri_x.tool.builtin.view_image` | `tests/test_builtin_tools3.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py` |
| `atri_x.tool.builtin.web_extract` | `tests/test_builtin_tools2.py`、`tests/test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_extract_engines` | `tests/test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_search` | `tests/test_builtin_tools2.py`、`tests/test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_cli` | `tests/test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_engines` | `tests/test_web_search_engines.py` |
| `atri_x.tool.builtin.workspace` | `tests/test_builtin_files.py`、`tests/test_builtin_tools2.py`、`tests/test_toolrun.py` |
| `atri_x.tool.capability` | `tests/test_builtin_tools3.py`、`tests/test_command_router.py` |
| `atri_x.tool.detail` | `tests/agent_fakes.py`、`tests/test_agent_cron_background.py`、`tests/test_tool_discover.py` |
| `atri_x.tool.discover` | `tests/test_tool_discover.py` |
| `atri_x.tool.media_runtime` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_tools4.py`、`tests/test_tool_media_runtime.py` |
| `atri_x.tool.runtimeinfo` | `tests/test_builtin_cron.py`、`tests/test_tool_runtimeinfo.py` |
| `atri_x.tool.sandbox` | `tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_background.py`、`tests/test_builtin_files.py`、`tests/test_builtin_shell.py`、`tests/test_builtin_tools3.py`、`tests/test_toolrun.py`、`tests/test_usage_attribution.py` |
| `atri_x.tool.skill` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_tools4.py`、`tests/test_diagram_pack.py`、`tests/test_research_bootstrap.py`、`tests/test_tool_skill.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py` |
| `atri_x.tool.skill.agent_manifest` | `tests/test_diagram_pack.py`、`tests/test_research_bootstrap.py` |
| `atri_x.tool.skill.creator` | `tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py` |
| `atri_x.tool.skill.go_source` | `tests/test_tool_skill.py` |
| `atri_x.tool.tool` | `tests/test_agent_assembly.py`、`tests/test_agent_cron.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_character_artist.py`、`tests/test_builtin_comic.py`、`tests/test_builtin_cron.py`、`tests/test_builtin_imagegen.py`、`tests/test_builtin_long_memory.py`、`tests/test_builtin_runtime_resources.py`、`tests/test_builtin_shell.py`、`tests/test_builtin_tools3.py`、`tests/test_builtin_tools4.py`、`tests/test_hook_runtime.py`、`tests/test_long_memory_scope.py`、`tests/test_member_persona.py`、`tests/test_qq_slowfetch.py`、`tests/test_safety_integration.py`、`tests/test_tool_discover.py`、`tests/test_tool_skill_editor.py`、`tests/test_tool_skill_tools.py`、`tests/test_toolrun.py`、`tests/test_vision_privacy.py`、`tests/test_web_extract_engines.py`、`tests/test_web_search_engines.py` |
| `atri_x.tool.workspace` | `tests/test_builtin_files.py`、`tests/test_toolrun.py` |
| `atri_x.toolrun` | `tests/_atrinis_support.py`、`tests/test_agent_assembly.py`、`tests/test_agent_chat.py`、`tests/test_agent_cron.py`、`tests/test_agent_cron_background.py`、`tests/test_agent_risk.py`、`tests/test_agent_tools.py`、`tests/test_app_runtime_agent.py`、`tests/test_atrinis.py`、`tests/test_atrinis_helpers.py`、`tests/test_atrinis_llm.py`、`tests/test_toolrun.py`、`tests/test_toolrun_atriwisp_args.py`、`tests/test_toolrun_denied.py` |
| `atri_x.toolrun.atriwisp` | `tests/test_toolrun.py` |
| `atri_x.toolrun.atriwisp_exec` | `tests/test_toolrun.py`、`tests/test_toolrun_atriwisp_args.py` |
| `atri_x.toolrun.availability` | `tests/test_toolrun.py` |
| `atri_x.toolrun.cache` | `tests/test_toolrun.py` |
| `atri_x.toolrun.native` | `tests/test_toolrun.py` |
| `atri_x.toolrun.preview` | `tests/test_toolrun.py` |
| `atri_x.toolrun.runner` | `tests/test_agent_cron.py`、`tests/test_toolrun.py`、`tests/test_toolrun_denied.py` |
| `atri_x.toolrun.toolrun` | `tests/test_agent_cron.py`、`tests/test_agent_risk.py`、`tests/test_app_runtime_agent.py`、`tests/test_toolrun.py` |
| `atri_x.turn` | `tests/agent_commands_fakes.py`、`tests/test_agent_context_runtime.py`、`tests/test_agent_core.py`、`tests/test_agent_risk.py`、`tests/test_turn_manager.py` |
| `atri_x.turn.manager` | `tests/test_agent_context_runtime.py` |
| `atri_x.utils` | `tests/agent_commands_fakes.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_files.py`、`tests/test_fileops.py` |
| `atri_x.utils.fileops` | `tests/agent_commands_fakes.py`、`tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_files.py`、`tests/test_fileops.py` |
| `atri_x.vision` | `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_imagegen.py`、`tests/test_promptlib.py`、`tests/test_vision.py`、`tests/test_vision_attributes.py`、`tests/test_vision_identity.py`、`tests/test_vision_prechat.py`、`tests/test_vision_privacy.py`、`tests/test_vision_threading.py` |
| `atri_x.vision.failures` | `tests/test_vision_privacy.py` |
| `atri_x.vision.prompt` | `tests/test_promptlib.py` |

## 无直接测试覆盖的模块（按一级域）

### agent（59）

`atri_x.agent.asr`、`atri_x.agent.budget_chat`、`atri_x.agent.budget_keys`、`atri_x.agent.budget_ledger`、`atri_x.agent.character_guide_prompt`、`atri_x.agent.chat_request_options`、`atri_x.agent.chat_stream`、`atri_x.agent.chat_vision`、`atri_x.agent.command_runtime`、`atri_x.agent.commands.character`、`atri_x.agent.commands.compact`、`atri_x.agent.commands.completion`、`atri_x.agent.commands.context_policy`、`atri_x.agent.commands.doctor`、`atri_x.agent.commands.group_knowledge`、`atri_x.agent.commands.group_policy`、`atri_x.agent.commands.group_services`、`atri_x.agent.commands.help`、`atri_x.agent.commands.hook`、`atri_x.agent.commands.log`、`atri_x.agent.commands.log_args`、`atri_x.agent.commands.log_format`、`atri_x.agent.commands.member_panel`、`atri_x.agent.commands.memory_common`、`atri_x.agent.commands.memory_forget`、`atri_x.agent.commands.memory_learning`、`atri_x.agent.commands.model`、`atri_x.agent.commands.request`、`atri_x.agent.commands.research`、`atri_x.agent.commands.rollback`、`atri_x.agent.commands.session_core`、`atri_x.agent.commands.session_format`、`atri_x.agent.commands.session_lifecycle`、`atri_x.agent.commands.session_module`、`atri_x.agent.commands.session_navigation`、`atri_x.agent.commands.state`、`atri_x.agent.commands.time`、`atri_x.agent.commands.tool`、`atri_x.agent.completion`、`atri_x.agent.core_build`、`atri_x.agent.core_forward`、`atri_x.agent.cron_background`、`atri_x.agent.dsh.client`、`atri_x.agent.dsh.command`、`atri_x.agent.dsh.transport`、`atri_x.agent.events`、`atri_x.agent.identity`、`atri_x.agent.inbound_speaker`、`atri_x.agent.inbox`、`atri_x.agent.logging`、`atri_x.agent.media_output`、`atri_x.agent.member_panel`、`atri_x.agent.memory_learning_admin`、`atri_x.agent.ops`、`atri_x.agent.platform_signals`、`atri_x.agent.prefix`、`atri_x.agent.state_watch`、`atri_x.agent.tools_service`、`atri_x.agent.turn_gate`

### angelmemory（3）

`atri_x.angelmemory.models`、`atri_x.angelmemory.schema`、`atri_x.angelmemory.store`

### app（5）

`atri_x.app.asr`、`atri_x.app.image_to_prompt`、`atri_x.app.runtime_hooks`、`atri_x.app.runtime_support`、`atri_x.app.vision`

### artist（5）

`atri_x.artist.metadata_store`、`atri_x.artist.parser`、`atri_x.artist.render`、`atri_x.artist.service`、`atri_x.artist.store`

### asr（3）

`atri_x.asr.asr`、`atri_x.asr.cache`、`atri_x.asr.error`

### atriena（5）

`atri_x.atriena.calls`、`atri_x.atriena.delivery`、`atri_x.atriena.dispatcher`、`atri_x.atriena.origin`、`atri_x.atriena.types`

### atrinis（12）

`atri_x.atrinis.auth`、`atri_x.atrinis.background`、`atri_x.atrinis.direct`、`atri_x.atrinis.http`、`atri_x.atrinis.llm`、`atri_x.atrinis.media`、`atri_x.atrinis.outbox`、`atri_x.atrinis.prepare`、`atri_x.atrinis.segments`、`atri_x.atrinis.service`、`atri_x.atrinis.store`、`atri_x.atrinis.targets`

### atriyph（3）

`atri_x.atriyph.ast`、`atri_x.atriyph.parser`、`atri_x.atriyph.rules`

### background（3）

`atri_x.background.background`、`atri_x.background.report`、`atri_x.background.result`

### character（3）

`atri_x.character.manifest`、`atri_x.character.store`、`atri_x.character.toml_codec`

### command（3）

`atri_x.command.handler`、`atri_x.command.router`、`atri_x.command.types`

### completion（5）

`atri_x.completion.character`、`atri_x.completion.fork`、`atri_x.completion.risk`、`atri_x.completion.router`、`atri_x.completion.tool`

### config（1）

`atri_x.config.tool_tags`

### cron（5）

`atri_x.cron.delivery`、`atri_x.cron.delivery_state`、`atri_x.cron.errors`、`atri_x.cron.jsonview`、`atri_x.cron.recovery`

### devtools（9）

`atri_x.devtools`、`atri_x.devtools.__main__`、`atri_x.devtools.astindex`、`atri_x.devtools.boundaries`、`atri_x.devtools.cli`、`atri_x.devtools.domaindocs`、`atri_x.devtools.entrypoints`、`atri_x.devtools.markdown`、`atri_x.devtools.render`

### dist（6）

`atri_x.dist`、`atri_x.dist.install`、`atri_x.dist.marker`、`atri_x.dist.paths`、`atri_x.dist.registry`、`atri_x.dist.shell_link`

### groupanalysis（1）

`atri_x.groupanalysis.service`

### groupkb（1）

`atri_x.groupkb.groupkb`

### gui（23）

`atri_x.gui.autostart`、`atri_x.gui.dialogs.common`、`atri_x.gui.dialogs.hooks`、`atri_x.gui.dialogs.maintenance`、`atri_x.gui.dialogs.selflearning`、`atri_x.gui.qqproto_cli`、`atri_x.gui.reactionpage`、`atri_x.gui.selftest`、`atri_x.gui.tabs.base`、`atri_x.gui.tabs.chat`、`atri_x.gui.tabs.extractpanel`、`atri_x.gui.tabs.mountpanel`、`atri_x.gui.tabs.permission`、`atri_x.gui.tabs.research`、`atri_x.gui.tabs.searchpanel`、`atri_x.gui.timediagnostics`、`atri_x.gui.timesources`、`atri_x.gui.toolmenu`、`atri_x.gui.toolmode`、`atri_x.gui.toolpreview`、`atri_x.gui.toolsettings_general`、`atri_x.gui.toolsettings_media`、`atri_x.gui.tray`

### historygate（1）

`atri_x.historygate.gate`

### hook（18）

`atri_x.hook.builtin.angel_memory`、`atri_x.hook.builtin.register`、`atri_x.hook.builtin.self_learning`、`atri_x.hook.media`、`atri_x.hook.output.message`、`atri_x.hook.output.output`、`atri_x.hook.process`、`atri_x.hook.protocol.eventjson`、`atri_x.hook.protocol.stream`、`atri_x.hook.rules.action`、`atri_x.hook.rules.exec`、`atri_x.hook.rules.exec_stderr`、`atri_x.hook.runtime.config`、`atri_x.hook.runtime.manager`、`atri_x.hook.runtime.media`、`atri_x.hook.runtime.protocol`、`atri_x.hook.runtime.transport`、`atri_x.hook.runtime.worker`

### imagegen（3）

`atri_x.imagegen.context`、`atri_x.imagegen.library`、`atri_x.imagegen.prompt`

### llm（2）

`atri_x.llm.openai.errors`、`atri_x.llm.segment`

### logging（1）

`atri_x.logging.redact_fallback`

### maintenance（2）

`atri_x.maintenance.maintenance`、`atri_x.maintenance.summary_cli`

### media（16）

`atri_x.media.config`、`atri_x.media.generation`、`atri_x.media.history`、`atri_x.media.image`、`atri_x.media.imagecodec.ops`、`atri_x.media.imagecodec.resample`、`atri_x.media.lifecycle`、`atri_x.media.local`、`atri_x.media.locks`、`atri_x.media.mimetype`、`atri_x.media.output`、`atri_x.media.reference`、`atri_x.media.remote`、`atri_x.media.resolver`、`atri_x.media.s3`、`atri_x.media.sanitize`

### memberprofile（1）

`atri_x.memberprofile.store`

### platform（19）

`atri_x.platform.backoff`、`atri_x.platform.cli.clipboard`、`atri_x.platform.cli.session_browse`、`atri_x.platform.cli.tui.content`、`atri_x.platform.cli.tui.mouse`、`atri_x.platform.cli.tui.theme`、`atri_x.platform.cli.tui.view`、`atri_x.platform.config`、`atri_x.platform.telegram`、`atri_x.platform.telegram.adapter`、`atri_x.platform.telegram.capabilities`、`atri_x.platform.telegram.client`、`atri_x.platform.telegram.config`、`atri_x.platform.telegram.format`、`atri_x.platform.telegram.keyboard`、`atri_x.platform.telegram.message`、`atri_x.platform.telegram.stream`、`atri_x.platform.telegram.target`、`atri_x.platform.telegram.types`

### processenv（1）

`atri_x.processenv.environment`

### reaction（3）

`atri_x.reaction.marker`、`atri_x.reaction.policy`、`atri_x.reaction.service`

### redact（1）

`atri_x.redact.redact`

### safety（4）

`atri_x.safety.audit`、`atri_x.safety.guard`、`atri_x.safety.policy`、`atri_x.safety.rules`

### security（3）

`atri_x.security.context`、`atri_x.security.grants`、`atri_x.security.security`

### selflearning（5）

`atri_x.selflearning.db`、`atri_x.selflearning.mining`、`atri_x.selflearning.models`、`atri_x.selflearning.patterns`、`atri_x.selflearning.store`

### session（8）

`atri_x.session.expiration`、`atri_x.session.fork`、`atri_x.session.lifecycle`、`atri_x.session.mode`、`atri_x.session.query`、`atri_x.session.service`、`atri_x.session.shared`、`atri_x.session.text`

### shellparse（4）

`atri_x.shellparse.clauses`、`atri_x.shellparse.lexer`、`atri_x.shellparse.parser`、`atri_x.shellparse.scanner`

### sidecar（1）

`atri_x.sidecar.__main__`

### signal（8）

`atri_x.signal.connection`、`atri_x.signal.diagnostics`、`atri_x.signal.errors`、`atri_x.signal.executor`、`atri_x.signal.queue`、`atri_x.signal.result`、`atri_x.signal.shutdown`、`atri_x.signal.signal`

### storage（9）

`atri_x.storage.sqlite.atrinis_event_repository`、`atri_x.storage.sqlite.context_summary_repository`、`atri_x.storage.sqlite.cron_job_repository`、`atri_x.storage.sqlite.media_history`、`atri_x.storage.sqlite.media_lifecycle`、`atri_x.storage.sqlite.media_repository`、`atri_x.storage.sqlite.message_repository`、`atri_x.storage.sqlite.session_repository`、`atri_x.storage.sqlite.tool_call_repository`

### timesync（1）

`atri_x.timesync.diagnostics`

### tool（22）

`atri_x.tool.availability`、`atri_x.tool.builtin.file_tools_ast`、`atri_x.tool.builtin.file_tools_detail`、`atri_x.tool.builtin.file_tools_directory`、`atri_x.tool.builtin.file_tools_read`、`atri_x.tool.builtin.shell_env`、`atri_x.tool.builtin.time_tool`、`atri_x.tool.executor`、`atri_x.tool.provider`、`atri_x.tool.skill.agent_skill_tool`、`atri_x.tool.skill.atriyph_warnings`、`atri_x.tool.skill.catalog`、`atri_x.tool.skill.command_tool`、`atri_x.tool.skill.descriptor`、`atri_x.tool.skill.finalize_atrix_skill`、`atri_x.tool.skill.manager`、`atri_x.tool.skill.media`、`atri_x.tool.skill.modify_atrix_skill`、`atri_x.tool.skill.parser`、`atri_x.tool.skill.process`、`atri_x.tool.skill.runner`、`atri_x.tool.skill.scanner`

### utils（9）

`atri_x.utils.fileops.atomic_write`、`atri_x.utils.fileops.diff`、`atri_x.utils.fileops.edit`、`atri_x.utils.fileops.encoding`、`atri_x.utils.fileops.file`、`atri_x.utils.fileops.match`、`atri_x.utils.fileops.rollback`、`atri_x.utils.fileops.text`、`atri_x.utils.runes`

### vision（10）

`atri_x.vision.analysis`、`atri_x.vision.cache`、`atri_x.vision.counters`、`atri_x.vision.fingerprint`、`atri_x.vision.identity`、`atri_x.vision.metrics`、`atri_x.vision.prechat`、`atri_x.vision.privacy`、`atri_x.vision.service`、`atri_x.vision.service_types`

