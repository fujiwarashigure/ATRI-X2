"""服务类命令的端到端测试：``/model``、``/log``、``/faq``、``/forget`` 等。

对应 Go：``internal/agent/commands`` 里除会话族之外的命令。

这一组全部用替身服务（``tests.agent_commands_fakes``）：断言点是"命令把参数正确翻译成
服务调用，并把返回值渲染成约定的文本"，以及"服务缺失时回退成提示而不是崩溃"。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from atri_x.angelmemory import Memory
from atri_x.logging import LogEntry
from tests.agent_commands_fakes import (
    Harness,
    KnowledgeEntry,
    LogEntryStub,
    ScanResultStub,
    StatefulHookInfo,
    ToolInfo,
)


def run(coro):
    return asyncio.run(coro)


def dispatch(harness: Harness, text: str):
    return run(harness.router.dispatch(harness.ctx, text)).content


class ModelCommandTests(unittest.TestCase):
    def test_bare_model_selects_current(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/model gpt-4o-mini")
        self.assertEqual(content, "switched to model: openai/gpt-4o-mini")
        self.assertEqual(harness.models.calls, ["select_model:gpt-4o-mini"])

    def test_mode_specific_targets(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/model --chat gpt-4o"),
            "switched chat model: openai/gpt-4o",
        )
        self.assertEqual(
            dispatch(harness, "/model --work fast"),
            "switched work model: openai/fast",
        )
        self.assertEqual(
            dispatch(harness, "/model --atriwisp2 m2"),
            "switched atriwisp2 model: openai/m2",
        )
        self.assertEqual(
            dispatch(harness, "/model --compact c1"),
            "switched compact model: openai/c1",
        )
        self.assertEqual(
            dispatch(harness, "/model -n n1"),
            "switched naming model: openai/n1",
        )
        self.assertIn("select_model_for_mode:chat:gpt-4o", harness.models.calls)
        self.assertIn("select_model_for_mode:work:fast", harness.models.calls)
        self.assertIn("select_model_for_mode:atriwisp2:m2", harness.models.calls)
        self.assertIn("select_compact_model:c1", harness.models.calls)
        self.assertIn("select_naming_model:n1", harness.models.calls)

    def test_profiles_and_snapshots(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/model --profiles"),
            "named model profiles (@model:<name>):\n"
            "  fast -> openai/gpt-4o (available)",
        )
        content = dispatch(harness, "/model --snapshots")
        self.assertTrue(content.startswith("saved model snapshots:\n  cheap\n"), content)
        self.assertIn("    chat -> openai/gpt-4o-mini", content)

    def test_empty_profiles_and_snapshots_explain_themselves(self) -> None:
        harness = Harness()
        harness.models.profiles = []
        harness.models.snapshots = []
        self.assertIn("no named model profiles configured", dispatch(harness, "/model --profiles"))
        self.assertIn("no saved model snapshots", dispatch(harness, "/model --snapshots"))

    def test_save_apply_delete_snapshot(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/model --save cheap"), "saved model snapshot: cheap"
        )
        content = dispatch(harness, "/model --apply cheap")
        self.assertEqual(
            content, "applied model snapshot:\nchat -> openai/gpt-4o-mini"
        )
        self.assertEqual(
            dispatch(harness, "/model --delete cheap"), "deleted model snapshot: cheap"
        )
        self.assertEqual(harness.models.calls, ["save:cheap", "apply:cheap", "delete:cheap"])

    def test_model_service_missing_raises(self) -> None:
        harness = Harness()
        harness.deps.models = None
        with self.assertRaises(ValueError):
            dispatch(harness, "/model --profiles")

    def test_check_model_groups_by_provider(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/checkmodel")
        self.assertTrue(content.startswith("available models:\n"), content)
        self.assertIn("openai:\n", content)
        self.assertIn("  * [1] gpt-4o (chat, work)", content)
        self.assertIn("anthropic:\n", content)
        self.assertIn("    [2] claude-3-5-sonnet", content)

    def test_check_model_alias_and_query(self) -> None:
        harness = Harness()
        harness.models.options = []
        self.assertEqual(dispatch(harness, "/models claude"), 'no models matching "claude"')
        self.assertEqual(dispatch(harness, "/checkmodel"), "no models available")

    def test_check_model_reports_provider_errors(self) -> None:
        from atri_x.agent.commands import ModelListResult, ModelProviderError

        harness = Harness()

        def model_list(query, opts):
            return ModelListResult(
                options=[],
                errors=[ModelProviderError(provider="anthropic", err=RuntimeError("boom"))],
            )

        harness.models.model_list = model_list
        content = dispatch(harness, "/checkmodel")
        self.assertIn("model provider errors:\n", content)
        self.assertIn("  - anthropic: boom", content)

    def test_check_model_completion_uses_snapshot_names(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("model")
        req = _completion("/model --apply ", name="model", args="--apply ")
        items = run(handler.complete(harness.ctx, req))
        self.assertEqual([item.text for item in items], ["cheap"])
        self.assertEqual(items[0].kind, "model_snapshot")

    def test_check_model_completion_offers_options(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("model")
        req = _completion("/model --", name="model", args="--")
        texts = [item.text for item in run(handler.complete(harness.ctx, req))]
        self.assertIn("--profiles", texts)
        self.assertIn("--atriwisp3", texts)

    def test_check_model_completion_lists_models(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("model")
        req = _completion("/model gp", name="model", args="gp")
        items = run(handler.complete(harness.ctx, req))
        self.assertIn("openai/gpt-4o", [item.text for item in items])


def _completion(raw: str, *, name: str, args: str):
    from atri_x.command import CompletionRequest

    return CompletionRequest(
        raw=raw, prefix="/", name=name, args=args, cursor=len(raw)
    )


class ToolCommandTests(unittest.TestCase):
    def test_tools_lists_registry(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/tools")
        self.assertEqual(
            content, "tools:\n  read_file [builtin] tags=fs 读文件"
        )

    def test_tools_empty(self) -> None:
        harness = Harness()
        harness.tools.infos = []
        self.assertEqual(dispatch(harness, "/tools"), "tools: none")

    def test_tools_missing_service(self) -> None:
        harness = Harness()
        harness.deps.tools = None
        self.assertEqual(dispatch(harness, "/tools"), "tool runtime is not configured")

    def test_tools_reload(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/tools reload"), "skill reload completed")
        self.assertEqual(harness.tools.calls, ["reload"])

    def test_tools_remove_requires_confirm(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/tools remove demo")
        self.assertEqual(
            content, '将删除外置 skill "demo" 及其目录。确认请执行：/tools remove demo --confirm'
        )
        self.assertEqual(harness.tools.calls, [])
        self.assertEqual(
            dispatch(harness, "/tools remove demo --confirm"), "removed skill: demo"
        )
        self.assertEqual(harness.tools.calls, ["remove:demo"])

    def test_tools_usage_error(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/tools bogus"),
            "usage: /tools [reload|uninstall|remove] [name]",
        )
        self.assertEqual(
            dispatch(harness, "/tools remove"),
            "usage: /tools remove <name> --confirm",
        )

    def test_tools_completion(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("tools")
        items = run(
            handler.complete(harness.ctx, _completion("/tools ", name="tools", args=""))
        )
        self.assertEqual(
            [item.text for item in items], ["reload", "remove", "uninstall"]
        )
        items = run(
            handler.complete(
                harness.ctx, _completion("/tools remove r", name="tools", args="remove r")
            )
        )
        self.assertEqual([item.text for item in items], ["read_file"])


class LogCommandTests(unittest.TestCase):
    def _entry(self, **fields) -> LogEntry:
        return LogEntry(
            time=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
            level="info",
            message="tool finished",
            fields=fields,
            raw="",
        )

    def test_log_renders_runtime_entries(self) -> None:
        harness = Harness()
        harness.logs.entries = [self._entry(tool="read_file", session_id="s1")]
        content = dispatch(harness, "/log")
        self.assertTrue(content.startswith("runtime logs:\n"), content)
        self.assertIn("2026-05-01 12:00:00 info tool finished", content)
        self.assertIn(" tool=read_file", content)
        self.assertIn(" session_id=s1", content)
        self.assertEqual(harness.logs.queries[0].prefix, "atri-x")

    def test_log_query_defaults_and_options(self) -> None:
        harness = Harness()
        dispatch(harness, "/log -w -n 10 --days 3 --msg startup")
        query = harness.logs.queries[0]
        self.assertEqual(query.limit, 10)
        self.assertEqual(query.days, 3)
        self.assertEqual(query.min_level, "warn")
        self.assertEqual(query.msg_contains, "startup")
        self.assertFalse(query.raw)

    def test_log_raw_flag(self) -> None:
        harness = Harness()
        harness.logs.entries = [LogEntryStub(raw="2026-05-01T12:00:00 INFO hi")]
        content = dispatch(harness, "/log --raw")
        self.assertTrue(content.startswith("runtime logs:\n"), content)
        self.assertIn("2026-05-01T12:00:00 INFO hi", content)
        self.assertTrue(harness.logs.queries[0].raw)

    def test_log_no_entries(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/log"), "no log entries found")

    def test_log_damaged_notice(self) -> None:
        harness = Harness()
        harness.logs.query_logs = lambda ctx, query: ScanResultStub(
            entries=[], damaged_count=2, damaged_lines=["line 1"]
        )
        content = dispatch(harness, "/log")
        self.assertIn("no log entries found", content)
        self.assertIn("跳过 2 条损坏记录", content)

    def test_log_reader_missing(self) -> None:
        harness = Harness()
        harness.deps.logs = None
        self.assertEqual(dispatch(harness, "/log"), "log reader is not configured")

    def test_audit_renders_fields(self) -> None:
        harness = Harness()
        harness.logs.entries = [
            self._entry(
                event="llm_usage",
                session_id="s1",
                provider="openai",
                model="gpt-4o",
                prompt_tokens="100",
                completion_tokens="20",
                total_tokens="120",
                cache_hit_tokens="50",
            )
        ]
        content = dispatch(harness, "/audit")
        self.assertTrue(content.startswith("audit events:\n"), content)
        self.assertIn("llm_usage", content)
        self.assertIn(" session=s1", content)
        self.assertIn(" provider=openai", content)
        self.assertIn(" model=gpt-4o", content)
        self.assertIn(" tokens=100/20/120", content)
        self.assertIn(" cache_hit=50/100(50.0%)", content)
        self.assertEqual(harness.logs.queries[0].prefix, "audit")

    def test_audit_filters(self) -> None:
        harness = Harness()
        dispatch(harness, "/audit --event tool_call --risk high --actor u1")
        query = harness.logs.queries[0]
        self.assertEqual(query.fields["event"], "tool_call")
        self.assertEqual(query.fields["risk"], "high")
        self.assertEqual(query.fields["actor_id"], "u1")
        self.assertEqual(query.prefix, "audit")
    def test_audit_shorthand_filters(self) -> None:
        harness = Harness()
        # Go 的 parseAuditQuery 用 logArgOptions{eventFilters: true}：
        # -t / -u / -a / --hook 可用，-s / --system 不可用。
        for flag, key, value in (
            ("-t", "event", "tool_call"),
            ("-u", "event", "user_message"),
            ("-a", "event", "assistant_message"),
            ("--hook", "module", "hook"),
        ):
            dispatch(harness, f"/audit {flag}")
            self.assertEqual(harness.logs.queries[-1].fields[key], value, flag)
        for flag in ("-s", "--system"):
            with self.assertRaises(ValueError, msg=flag):
                dispatch(harness, f"/audit {flag}")

    def test_log_accepts_system_shorthand(self) -> None:
        harness = Harness()
        dispatch(harness, "/log -s")
        self.assertEqual(harness.logs.queries[-1].fields["event"], "system_message")

    def test_atriwisp_rejects_event_shorthands(self) -> None:
        harness = Harness()
        for flag in ("-u", "-a", "-t", "-s"):
            with self.assertRaises(ValueError, msg=flag):
                dispatch(harness, f"/atriwisp {flag}")

    def test_audit_event_aliases(self) -> None:
        harness = Harness()
        dispatch(harness, "/audit --event user_input")
        self.assertEqual(harness.logs.queries[0].fields["event"], "user_message")
        dispatch(harness, "/audit --event assistant_output")
        self.assertEqual(harness.logs.queries[1].fields["event"], "assistant_message")

    def test_audit_tool_filter(self) -> None:
        harness = Harness()
        dispatch(harness, "/audit --tool read_file")
        self.assertEqual(harness.logs.queries[0].fields["tool"], "read_file")

    def test_atriwisp_filters(self) -> None:
        harness = Harness()
        harness.logs.entries = [self._entry(atriwisp_name="watchdog", mode="llm")]
        content = dispatch(harness, "/atriwisp watchdog --mode llm")
        self.assertTrue(content.startswith("atriwisp logs:\n"), content)
        self.assertIn(" atriwisp=watchdog", content)
        self.assertIn(" mode=llm", content)
        query = harness.logs.queries[0]
        self.assertEqual(query.prefix, "atrinis")
        self.assertEqual(query.fields["mode"], "llm")

    def test_log_completion_offers_options_and_levels(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("log")
        items = run(
            handler.complete(harness.ctx, _completion("/log --level ", name="log", args="--level "))
        )
        self.assertEqual(
            [item.text for item in items], ["debug", "info", "warn", "error"]
        )
        items = run(
            handler.complete(harness.ctx, _completion("/log --", name="log", args="--"))
        )
        texts = [item.text for item in items]
        self.assertIn("--limit", texts)
        self.assertIn("--system", texts)
        self.assertNotIn("--risk", texts)

    def test_audit_completion_has_audit_specific_options(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("audit")
        items = run(
            handler.complete(harness.ctx, _completion("/audit --", name="audit", args="--"))
        )
        texts = [item.text for item in items]
        self.assertIn("--risk", texts)
        self.assertIn("--run_source", texts)
        self.assertNotIn("--system", texts)


class UsageCommandTests(unittest.TestCase):
    def _usage_entry(self, **fields) -> LogEntry:
        return LogEntry(
            time=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
            level="info",
            message="llm usage",
            fields=fields,
            raw="",
        )

    def test_usage_aggregates_by_model(self) -> None:
        harness = Harness()
        harness.logs.entries = [
            self._usage_entry(
                provider="openai",
                model="gpt-4o",
                prompt_tokens="100",
                completion_tokens="20",
                total_tokens="120",
                cache_hit_tokens="50",
                elapsed_ms="1500",
            ),
            self._usage_entry(
                provider="openai",
                model="gpt-4o",
                prompt_tokens="50",
                completion_tokens="5",
                total_tokens="55",
                cache_hit_tokens="0",
                elapsed_ms="500",
            ),
        ]
        content = dispatch(harness, "/usage")
        self.assertIn("token usage (last 1 day(s), by model):", content)
        self.assertIn("[openai/gpt-4o] calls: 2\n", content)
        self.assertIn("  prompt: 150 | completion: 25 | total: 175\n", content)
        self.assertIn("  cache: 50 | elapsed: 2.0s\n", content)
        self.assertIn("─────", content)
        self.assertIn("[total] calls: 2\n", content)
        query = harness.logs.queries[0]
        self.assertEqual(query.prefix, "audit")
        self.assertEqual(query.limit, 100000)
        self.assertEqual(query.fields["event"], "llm_usage")

    def test_usage_unknown_values_render_null(self) -> None:
        harness = Harness()
        harness.logs.entries = [self._usage_entry(provider="openai", model="gpt-4o")]
        content = dispatch(harness, "/usage")
        self.assertIn("  prompt: null | completion: null | total: null\n", content)
        self.assertIn("  cache: null | elapsed: null\n", content)

    def test_usage_group_by_day_and_session(self) -> None:
        harness = Harness()
        harness.logs.entries = [
            self._usage_entry(session_id="s1", prompt_tokens="1", completion_tokens="1", total_tokens="2")
        ]
        content = dispatch(harness, "/usage --by day -d 30")
        self.assertIn("by day", content)
        self.assertIn("[2026-05-01] calls: 1", content)
        content = dispatch(harness, "/usage --by session")
        self.assertIn("[s1] calls: 1", content)

    def test_usage_without_data(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/usage"), "no usage data found")

    def test_usage_filter_options(self) -> None:
        harness = Harness()
        dispatch(harness, "/usage -m gpt-4o -s s1 --since 2026-05-01")
        query = harness.logs.queries[0]
        self.assertEqual(query.fields["model"], "gpt-4o")
        self.assertEqual(query.fields["session_id"], "s1")
        self.assertIsNotNone(query.since)

    def test_usage_helper_outputs(self) -> None:
        from atri_x.agent.commands.usage import format_thousands

        self.assertEqual(format_thousands(0), "0")
        self.assertEqual(format_thousands(999), "999")
        self.assertEqual(format_thousands(1000), "1,000")
        self.assertEqual(format_thousands(1234567), "1,234,567")
        self.assertEqual(format_thousands(-1234), "-1,234")

    def test_usage_completion(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("usage")
        items = run(
            handler.complete(harness.ctx, _completion("/usage --by ", name="usage", args="--by "))
        )
        self.assertEqual([item.text for item in items], ["model", "day", "session"])


class StateCommandTests(unittest.TestCase):
    def test_status_view(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/state")
        self.assertIn("运行时状态文件：data/config/atri-x/state.toml", content)
        self.assertIn("已加载：2026-01-01 12:00:00", content)
        self.assertIn("当前内存状态与文件一致。", content)

    def test_pending_view(self) -> None:
        harness = Harness()
        harness.runtime_state.status = type(harness.runtime_state.status)(
            path="state.toml",
            loaded_mod_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            file_mod_time=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
            pending=True,
        )
        content = dispatch(harness, "/state")
        self.assertIn("存在未生效的外部修改", content)

    def test_unconfigured_view(self) -> None:
        harness = Harness()
        harness.runtime_state.status = type(harness.runtime_state.status)()
        self.assertEqual(
            dispatch(harness, "/state"), "未配置 state.toml（运行时状态不会持久化）。"
        )
        harness.deps.runtime_state = None
        self.assertEqual(dispatch(harness, "/state"), "运行时状态热加载未配置。")

    def test_reload_reports_changed_sections(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/state reload")
        self.assertEqual(
            content,
            "state.toml 已重新加载（2026-01-01 13:00:00），生效内容：mode_models。",
        )
        self.assertEqual(harness.runtime_state.reloads, 1)
        self.assertIn("runtime_state_reload_command", harness.audit_events())

    def test_reload_failure_is_rendered(self) -> None:
        harness = Harness()

        async def boom(ctx):
            raise RuntimeError("disk on fire")

        harness.runtime_state.reload_runtime_state = boom
        self.assertEqual(
            dispatch(harness, "/state reload"), "重新加载 state.toml 失败：disk on fire"
        )

    def test_usage_error(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/state bogus"), "用法：/state | /state reload")


class HookCommandTests(unittest.TestCase):
    def test_hook_list(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/hooks")
        self.assertTrue(content.startswith("hooks:\n"), content)
        self.assertIn("  plug  [oneshot:running]  rules=1", content)
        self.assertIn(" | 1 actived", content)
        self.assertIn(" | 2 waiting", content)
        self.assertIn("  audit-log  [tool_call_prepared]  priority=10", content)
        self.assertIn(" | 3 actived", content)

    def test_hook_list_empty(self) -> None:
        harness = Harness()
        harness.hooks.infos = []
        harness.hooks.stateful = []
        self.assertEqual(dispatch(harness, "/hooks"), "hooks: none")

    def test_hook_service_missing(self) -> None:
        harness = Harness()
        harness.deps.hooks = None
        self.assertEqual(dispatch(harness, "/hooks"), "hook service is not configured")

    def test_hook_detail_by_name(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/hooks audit-log")
        self.assertEqual(
            content,
            "name: audit-log\npoint: tool_call_prepared\npriority: 10\n"
            "description: 记录审计事件",
        )

    def test_plugin_hook_detail(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/hooks plug")
        self.assertIn("name: plug\n", content)
        self.assertIn("mode: oneshot\nstatus: running", content)
        self.assertIn("active: 1", content)
        self.assertIn("waiting: 2", content)
        self.assertIn("rules: 1", content)
        self.assertIn("rule: rule-a\npoint: llm_request_prepared\npriority: 5", content)

    def test_unknown_hook(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/hooks nope"),
            'hook "nope" not found. Use /hooks to list all hooks.',
        )

    def test_hook_reload(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/hooks reload"), "hook reload completed")
        harness.hooks.notices = ["bad plugin", ""]
        self.assertEqual(
            dispatch(harness, "/hooks reload"),
            "hook reload completed with warnings:\n- bad plugin",
        )

    def test_stateful_hook_control(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/hooks start plug"), "hook start completed: plug")
        self.assertEqual(dispatch(harness, "/hooks stop plug"), "hook stop completed: plug")
        self.assertEqual(
            dispatch(harness, "/hooks restart plug"), "hook restart completed: plug"
        )
        self.assertEqual(harness.hooks.calls, ["start:plug", "stop:plug", "restart:plug"])
        self.assertEqual(
            dispatch(harness, "/hooks start"), "usage: /hooks start <id>"
        )

    def test_hook_completion(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("hooks")
        items = run(
            handler.complete(harness.ctx, _completion("/hooks ", name="hooks", args=""))
        )
        texts = [item.text for item in items]
        self.assertEqual(texts[:4], ["reload", "start", "stop", "restart"])
        self.assertIn("plug", texts)
        self.assertIn("audit-log", texts)
        items = run(
            handler.complete(
                harness.ctx, _completion("/hooks stop ", name="hooks", args="stop ")
            )
        )
        self.assertEqual([item.text for item in items], ["plug"])
        self.assertEqual(items[0].description, "running")


class GroupKnowledgeTests(unittest.TestCase):
    def test_add_and_list(self) -> None:
        harness = Harness(group=True)
        # 别名用 `|` 分隔（Go 的 splitKnowledgeAliases）。
        content = dispatch(harness, "/faq add 现在几点|几点了 => 看手机")
        self.assertEqual(content, "已添加知识 k1 [exact]：现在几点")
        listing = dispatch(harness, "/faq")
        self.assertIn("群知识库（1 条）", listing)
        self.assertIn("- k1 [exact] 现在几点", listing)
        self.assertIn("  别名：几点了", listing)
        self.assertIn("  答案：看手机", listing)

    def test_add_contains_and_keywords(self) -> None:
        harness = Harness(group=True)
        dispatch(harness, "/faq add-contains 天气 => 晴")
        entry = harness.group_knowledge.entries[0]
        self.assertEqual(entry.match, "contains")
        dispatch(harness, "/faq add-keyword 开会,例会 => 三楼")
        entry = harness.group_knowledge.entries[1]
        self.assertEqual(entry.match, "keywords")
        self.assertEqual(entry.keywords, ["开会", "例会"])
        self.assertEqual(entry.question, "开会、例会")

    def test_list_empty_and_search(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/faq"),
            "当前群还没有知识库条目。使用 /faq add 问题 => 答案 添加。",
        )
        harness.group_knowledge.entries = [
            KnowledgeEntry(id="k1", question="现在几点", answer="看手机")
        ]
        self.assertEqual(dispatch(harness, "/faq list 不存在"), '没有匹配 "不存在" 的知识条目。')

    def test_remove_clear_test(self) -> None:
        harness = Harness(group=True)
        dispatch(harness, "/faq add 问题 => 答案")
        self.assertEqual(dispatch(harness, "/faq remove k1"), "已删除知识 k1")
        self.assertEqual(dispatch(harness, "/faq remove k1"), '没有找到知识 "k1"')
        dispatch(harness, "/faq add 问题 => 答案")
        self.assertEqual(
            dispatch(harness, "/faq clear"),
            "请使用 /faq clear --confirm 确认清空当前群知识库。",
        )
        self.assertEqual(dispatch(harness, "/faq clear --confirm"), "已清空 1 条知识。")
        dispatch(harness, "/faq add 问题 => 答案")
        content = dispatch(harness, "/faq test 问题")
        self.assertIn("匹配 k1 [exact]：问题", content)
        self.assertIn("答案：答案", content)
        self.assertEqual(dispatch(harness, "/faq test 别的"), "未匹配到任何知识条目。")

    def test_enable_and_disable_go_through_group_policy(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(dispatch(harness, "/faq off"), "群策略已更新：knowledge = off")
        self.assertEqual(dispatch(harness, "/faq on"), "群策略已更新：knowledge = on")
        self.assertEqual(
            harness.group_policy.calls, ["set:knowledge:off", "set:knowledge:on"]
        )

    def test_help_subcommand(self) -> None:
        harness = Harness(group=True)
        self.assertIn("Usage:", dispatch(harness, "/faq help"))

    def test_service_missing(self) -> None:
        harness = Harness(group=True)
        harness.deps.group_knowledge = None
        with self.assertRaises(ValueError):
            dispatch(harness, "/faq")


class GroupPolicyCommandTests(unittest.TestCase):
    def test_status_and_set(self) -> None:
        harness = Harness(group=True)
        self.assertIn("群级策略", dispatch(harness, "/grouppolicy"))
        self.assertEqual(
            dispatch(harness, "/grouppolicy wake atri,助手"),
            "群策略已更新：wake = atri,助手",
        )
        self.assertEqual(
            harness.group_policy.calls, ["set:wake:atri,助手"]
        )

    def test_reset(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/grouppolicy reset wake"), "群策略字段已重置：wake"
        )

    def test_value_is_kept_verbatim(self) -> None:
        harness = Harness(group=True)
        dispatch(harness, "/grouppolicy quiet 23:00-07:30")
        self.assertEqual(harness.group_policy.calls, ["set:quiet:23:00-07:30"])

    def test_missing_value_is_usage_error(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/grouppolicy wake"),
            "用法：/grouppolicy <field> <value>；发送 /grouppolicy 查看当前策略。",
        )

    def test_service_missing(self) -> None:
        harness = Harness(group=True)
        harness.deps.group_policy = None
        with self.assertRaises(ValueError):
            dispatch(harness, "/grouppolicy")


class ContextPolicyCommandTests(unittest.TestCase):
    def test_status_set_reset(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/overflow"), "overflow policy: reject")
        self.assertEqual(
            dispatch(harness, "/overflow --work summarize"),
            "已设置 work 溢出策略为 summarize",
        )
        self.assertEqual(
            dispatch(harness, "/overflow reset --chat"), "已重置 chat 溢出策略"
        )
        self.assertEqual(
            harness.context_policy.calls, ["set:work:summarize", "reset:chat"]
        )

    def test_show_keyword(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/overflow show"), "overflow policy: reject")

    def test_alias(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/ctxpolicy"), "overflow policy: reject")


class MemberPanelTests(unittest.TestCase):
    def test_panel_views(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/me"), "my panel (all)")
        self.assertEqual(dispatch(harness, "/me tasks"), "my panel (tasks)")
        self.assertEqual(harness.member_panel.views, ["", "tasks"])

    def test_alias_and_help(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/mytasks"), "my panel (all)")
        self.assertIn("Usage:", dispatch(harness, "/me help"))

    def test_service_missing(self) -> None:
        harness = Harness()
        harness.deps.member_panel = None
        with self.assertRaises(ValueError):
            dispatch(harness, "/me")


class GroupServicesCommandTests(unittest.TestCase):
    def test_remind_create_list_remove(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/remind 10m 开会"), "提醒已创建：10m 开会"
        )
        self.assertEqual(dispatch(harness, "/remind"), "当前群提醒（0 条）：")
        self.assertEqual(dispatch(harness, "/remind remove r1"), "已删除提醒 r1")
        self.assertIn("用法：/remind", dispatch(harness, "/remind help"))

    def test_poll_create_vote_show(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/poll 吃啥 | 面 | 饭"), "[p1] 吃啥（open）"
        )
        self.assertEqual(dispatch(harness, "/poll list"), "当前群投票（0 条）：")
        self.assertEqual(dispatch(harness, "/vote p1 2"), "已投票：p1 选项 2")
        self.assertEqual(dispatch(harness, "/poll show p1"), "[p1] 问题（open）")
        self.assertEqual(dispatch(harness, "/poll close p1"), "[p1] 问题（closed）")

    def test_signup_create_join_leave(self) -> None:
        harness = Harness(group=True)
        self.assertEqual(
            dispatch(harness, "/signup 团建 3"), "[s1] 团建（open，0/3）"
        )
        self.assertEqual(dispatch(harness, "/join s1"), "已加入报名 s1")
        self.assertEqual(dispatch(harness, "/signup show s1"), "[s1] 报名（open）")
        self.assertEqual(dispatch(harness, "/signup leave s1"), "已退出报名 s1")
        self.assertEqual(dispatch(harness, "/signup close s1"), "[s1] 报名（closed）")

    def test_signup_capacity_parsing(self) -> None:
        harness = Harness(group=True)
        dispatch(harness, "/signup 团建")
        self.assertIn("signup_create:团建:0", harness.group_services.calls)

    def test_service_missing(self) -> None:
        harness = Harness(group=True)
        harness.deps.group_services = None
        with self.assertRaises(ValueError):
            dispatch(harness, "/remind 10m x")


class ForgetCommandTests(unittest.TestCase):
    def _memory(self, id_: str = "abcdef1234567890") -> Memory:
        return Memory(
            id=id_,
            platform="qq",
            scope_id="private:1",
            content="用户喜欢喝茶",
            strength=5,
            source_kind="user_message",
            source_actor_id="u1",
            source_message_id="m1",
        )

    def test_help_without_arguments(self) -> None:
        harness = Harness()
        self.assertIn("Usage:", dispatch(harness, "/forget"))

    def test_list_and_delete(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [self._memory()]
        listing = dispatch(harness, "/forget list")
        self.assertIn("可管理的长期记忆：1 条", listing)
        self.assertIn("1. [5] id=abcdef123456 用户喜欢喝茶", listing)
        self.assertIn("(kind=user_message actor=u1 message=m1)", listing)
        self.assertEqual(dispatch(harness, "/forget abcdef"), "已删除记忆 abcdef123456。")
        self.assertEqual(harness.angel_memory.deleted, ["abcdef1234567890"])
        self.assertIn("angel_memory_forget", harness.audit_events())

    def test_list_empty_and_disabled(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/forget list"), "当前没有你可删除的长期记忆。")
        harness.angel_memory.ready = lambda: False
        self.assertEqual(dispatch(harness, "/forget list"), "angel memory 未启用或未配置。")

    def test_unknown_id(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [self._memory()]
        with self.assertRaises(ValueError):
            dispatch(harness, "/forget zzz")

    def test_ambiguous_id(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [
            self._memory("abcdef1111"),
            self._memory("abcdef2222"),
        ]
        with self.assertRaises(ValueError):
            dispatch(harness, "/forget abcdef")

    def test_delete_by_message_source(self) -> None:
        harness = Harness()
        other = self._memory("other0000000")
        other.source_message_id = "m2"
        harness.angel_memory.memories = [self._memory(), other]
        content = dispatch(harness, "/forget source m1")
        self.assertEqual(content, "已删除 1 条由消息 m1 派生的记忆。")
        self.assertIn("angel_memory_forget_source", harness.audit_events())

    def test_source_requires_message_id(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/forget source"), "用法：/forget source <平台消息id>")

    def test_resident_requires_confirm_for_core(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/forget resident core")
        self.assertEqual(
            content, "清空 core 常驻记忆需要确认：/forget resident core --confirm"
        )
        self.assertEqual(
            dispatch(harness, "/forget resident core --confirm"),
            "已清空你的 core 常驻记忆。",
        )
        self.assertEqual(harness.resident_memory.core_writes, [""])

    def test_resident_normal_and_all(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/forget resident normal"), "已清空你的普通常驻记忆。"
        )
        self.assertEqual(harness.resident_memory.normal_cleared, 1)
        dispatch(harness, "/forget resident all --confirm")
        self.assertEqual(harness.resident_memory.normal_cleared, 2)
        self.assertEqual(len(harness.resident_memory.core_writes), 1)
        self.assertEqual(
            dispatch(harness, "/forget resident bogus"),
            "用法：/forget resident normal|core|all [--confirm]",
        )

    def test_resident_store_missing(self) -> None:
        harness = Harness()
        harness.deps.resident_memory = None
        self.assertEqual(
            dispatch(harness, "/forget resident normal"), "常驻记忆未启用或未配置。"
        )

    def test_cannot_delete_someone_elses_memory_in_a_group(self) -> None:
        harness = Harness(group=True, admin=False, group_role="member")
        memory = self._memory()
        memory.source_actor_id = "someone-else"
        harness.angel_memory.memories = [memory]
        self.assertEqual(dispatch(harness, "/forget abcdef"), "你没有权限删除这条记忆。")


class MemoryLearningCommandTests(unittest.TestCase):
    def _memory(self) -> Memory:
        return Memory(
            id="abcdef1234567890",
            platform="qq",
            scope_id="private:1",
            content="用户喜欢喝茶",
            tags="偏好",
            strength=5,
            source_actor_id="u1",
        )

    def test_status_and_list(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [self._memory()]
        self.assertEqual(dispatch(harness, "/memory"), "当前范围长期记忆：1 条。")
        harness.angel_memory.memories = []
        self.assertEqual(dispatch(harness, "/memory status"), "当前范围长期记忆：0 条。")
        harness.angel_memory.memories = [self._memory()]
        listing = dispatch(harness, "/memory list")
        self.assertIn("长期记忆 1 条（最多显示 20 条）：", listing)
        self.assertIn("1. [5] id=abcdef123456 用户喜欢喝茶", listing)

    def test_show_and_delete(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [self._memory()]
        content = dispatch(harness, "/memory show abcdef")
        self.assertIn("id：abcdef1234567890", content)
        self.assertIn("强度：5", content)
        self.assertIn("标签：偏好", content)
        self.assertIn("内容：用户喜欢喝茶", content)
        self.assertIn("来源：actor=u1", content)
        self.assertEqual(dispatch(harness, "/memory delete abcdef"), "已删除记忆 abcdef123456。")

    def test_show_includes_source_kind_when_recorded(self) -> None:
        harness = Harness()
        memory = self._memory()
        memory.source_kind = "user_message"
        harness.angel_memory.memories = [memory]
        content = dispatch(harness, "/memory show abcdef")
        self.assertIn("来源：kind=user_message actor=u1", content)

    def test_recall_and_source(self) -> None:
        harness = Harness()
        harness.angel_memory.memories = [self._memory()]
        self.assertIn("记忆 1 条：", dispatch(harness, "/memory recall 喝茶"))
        self.assertEqual(
            dispatch(harness, "/memory source m1"), "已删除 0 条由消息 m1 派生的记忆。"
        )

    def test_backfill_preview_and_confirm(self) -> None:
        harness = Harness()
        preview = dispatch(harness, "/memory backfill")
        self.assertIn("记忆总数：10 条", preview)
        self.assertIn("已有结构化来源：4 条", preview)
        self.assertIn("执行回填：/memory backfill --confirm", preview)
        content = dispatch(harness, "/memory backfill --confirm")
        self.assertIn("已为 3 条旧记忆回填 source_kind", content)
        self.assertIn("angel_memory_backfill_legacy_source", harness.audit_events())

    def test_usage_line_when_action_unknown(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/memory bogus"),
            "用法：/memory status|list|recall|show|delete|source|backfill",
        )

    def test_service_missing(self) -> None:
        harness = Harness()
        harness.deps.angel_memory = None
        self.assertEqual(dispatch(harness, "/memory"), "angel memory 未启用或未配置。")

    def test_learning_status_and_review(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/learning"), "self learning：待审 2 条，已批准 5 条。"
        )
        content = dispatch(harness, "/learning review")
        self.assertIn("候选 1 条：", content)
        self.assertIn("1. [pending] id=c1 count=4 pattern=yyds", content)

    def test_learning_mine(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/learning mine"),
            "候选挖掘完成：新增 1 条，更新 2 条，跳过 3 条；扫描 40 条 / 800 字。",
        )

    def test_learning_approve_reject_undo_history(self) -> None:
        harness = Harness()
        self.assertEqual(
            dispatch(harness, "/learning approve c1 永远的神"), "候选 c1 已标记为 approved。"
        )
        self.assertEqual(
            dispatch(harness, "/learning reject c1"), "候选 c1 已标记为 rejected。"
        )
        self.assertEqual(dispatch(harness, "/learning undo c1"), "候选 c1 已撤回为待审。")
        self.assertEqual(
            harness.self_learning.decisions,
            [
                ("c1", "approved", "u1"),
                ("c1", "rejected", "u1"),
                ("c1", "undo", "u1"),
            ],
        )
        self.assertIn("审核历史 1 条：", dispatch(harness, "/learning history c1"))

    def test_learning_requires_ids(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/learning approve"), "approve/reject 需要候选 ID。")
        self.assertEqual(dispatch(harness, "/learning undo"), "undo 需要候选 ID。")
        self.assertEqual(dispatch(harness, "/learning history"), "history 需要候选 ID。")

    def test_learning_disabled_by_group_policy(self) -> None:
        harness = Harness(group=True)
        harness.group_policy.learning = False
        self.assertIn("本群已关闭学习观察", dispatch(harness, "/learning"))

    def test_learning_group_admin_needs_grant(self) -> None:
        harness = Harness(group=True, admin=False, group_role="admin")
        harness.group_policy.authorized = False
        self.assertIn("当前群管理员未获授权", dispatch(harness, "/learning"))

    def test_learning_group_admin_with_grant_is_allowed(self) -> None:
        harness = Harness(group=True, admin=False, group_role="admin")
        self.assertEqual(
            dispatch(harness, "/learning"), "self learning：待审 2 条，已批准 5 条。"
        )

    def test_learning_disabled_service(self) -> None:
        harness = Harness()
        harness.deps.self_learning = None
        self.assertEqual(dispatch(harness, "/learning"), "self learning 未启用或未配置。")

    def test_learning_alias(self) -> None:
        harness = Harness()
        self.assertIn("self learning：", dispatch(harness, "/selflearning"))

    def test_learning_action_permission_table(self) -> None:
        from atri_x.agent.commands import learning_action_permission

        self.assertEqual(learning_action_permission("status"), "view")
        self.assertEqual(learning_action_permission("Review"), "view")
        self.assertEqual(learning_action_permission("history"), "view")
        self.assertEqual(learning_action_permission("approve"), "decide")
        self.assertEqual(learning_action_permission("reject"), "decide")
        self.assertEqual(learning_action_permission("undo"), "decide")
        self.assertEqual(learning_action_permission("mine"), "mine")
        self.assertEqual(learning_action_permission("delete"), "delete")
        self.assertEqual(learning_action_permission("export"), "export")
        self.assertEqual(learning_action_permission("bogus"), "")


class DoctorAndRollbackTests(unittest.TestCase):
    def test_doctor(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/doctor"), "配置检查：一切正常")

    def test_doctor_missing(self) -> None:
        harness = Harness()
        harness.deps.doctor = None
        self.assertEqual(dispatch(harness, "/doctor"), "配置检查未启用。")

    def test_rollback_lists_records(self) -> None:
        harness = Harness()
        harness.create_session()
        content = dispatch(harness, "/rollback")
        self.assertIn("可回滚的文件编辑：", content)
        self.assertIn("1. a.txt（编辑，2026-01-01 12:00:00，12 字节）", content)
        self.assertIn("使用 /rollback <编号> 回滚其中一个文件", content)

    def test_rollback_empty_and_missing(self) -> None:
        harness = Harness()
        harness.create_session()
        harness.file_backups.records = []
        self.assertEqual(dispatch(harness, "/rollback"), "当前会话没有可回滚的文件编辑。")
        harness.deps.file_backups = None
        self.assertEqual(dispatch(harness, "/rollback"), "文件回滚未启用。")

    def test_rollback_restores(self) -> None:
        harness = Harness()
        harness.create_session()
        content = dispatch(harness, "/rollback 1")
        self.assertIn("已回滚 a.txt：恢复编辑前内容（编辑前 revision r1）。", content)

    def test_rollback_usage_and_unknown_id(self) -> None:
        harness = Harness()
        harness.create_session()
        self.assertEqual(
            dispatch(harness, "/rollback abc"),
            "用法：/rollback [编号]；编号来自不带参数的 /rollback 列表。",
        )
        self.assertIn("没有编号 9 的回滚记录", dispatch(harness, "/rollback 9"))


class CharacterCommandTests(unittest.TestCase):
    def test_list_characters(self) -> None:
        harness = Harness()
        content = dispatch(harness, "/chars")
        self.assertIn("角色：共 1 个可见。", content)
        self.assertIn("- alice | Alice | aliases=小爱 | tags=demo | public | 测试角色", content)
        self.assertIn("发送 @char:<id> 可在本轮启用某个角色。", content)

    def test_search_and_empty(self) -> None:
        harness = Harness()
        self.assertIn("alice", dispatch(harness, "/chars 小爱"))
        self.assertIn("把角色目录放到", dispatch(harness, "/chars nope"))

    def test_reload_requires_superadmin(self) -> None:
        harness = Harness(admin=False)
        self.assertEqual(dispatch(harness, "/chars reload"), "只有超级管理员可以重建角色索引。")
        harness.actor.role = "superadmin"
        self.assertEqual(dispatch(harness, "/chars reload"), "角色索引已重建。")
        self.assertEqual(harness.characters.refreshes, 1)

    def test_disabled_store(self) -> None:
        harness = Harness()
        harness.characters.enabled = lambda: False
        self.assertEqual(dispatch(harness, "/chars"), "角色素材库未配置。")
        harness.deps.characters = None
        self.assertEqual(dispatch(harness, "/chars"), "角色素材库未配置。")


class CompactCommandTests(unittest.TestCase):
    def test_compact(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/compact"), "context compacted")
        self.assertEqual(harness.compact.reasons, ["manual"])


class SyncServiceImplementationTests(unittest.TestCase):
    """并行期约定：服务实现可能是协程，也可能是同步返回值，命令侧两种都要能用。"""

    def test_sync_implementations_are_accepted(self) -> None:
        harness = Harness()

        class SyncCompact:
            def compact_current(self, ctx, trigger_reason):
                return f"compacted:{trigger_reason}"

        class SyncDoctor:
            def doctor(self, ctx):
                return "sync doctor"

        class SyncPanel:
            def member_panel(self, ctx, view):
                return f"sync panel {view}"

        class SyncContextPolicy:
            def context_policy_status(self, ctx):
                return "sync policy"

            def set_context_policy(self, ctx, target, value):
                return f"sync set {target}={value}"

            def reset_context_policy(self, ctx, target):
                return f"sync reset {target}"

        class SyncTools:
            def list(self):
                return []

            def unregister(self, name):
                pass

            def remove(self, ctx, name):
                pass

            def reload(self, ctx):
                pass

        class SyncHooks:
            def hook_list(self):
                return []

            def hook_reload(self):
                return type("R", (), {"notices": []})()

            def stateful_hooks(self):
                return []

            def start_stateful_hook(self, id):
                pass

            def stop_hook(self, ctx, id):
                return True

            def restart_stateful_hook(self, ctx, id):
                pass

        class SyncState:
            def reload_runtime_state(self, ctx):
                return type(
                    "R",
                    (),
                    {"path": "state.toml", "applied": True, "changed": [], "mod_time": None},
                )()

            def runtime_state_status(self):
                return type(
                    "S",
                    (),
                    {"path": "", "loaded_mod_time": None, "file_mod_time": None, "pending": False},
                )()

        class SyncLogs:
            def query_logs(self, ctx, query):
                return ScanResultStub(entries=[], damaged_count=0)

        harness.deps.compact = SyncCompact()
        harness.deps.doctor = SyncDoctor()
        harness.deps.member_panel = SyncPanel()
        harness.deps.context_policy = SyncContextPolicy()
        harness.deps.tools = SyncTools()
        harness.deps.hooks = SyncHooks()
        harness.deps.runtime_state = SyncState()
        harness.deps.logs = SyncLogs()

        self.assertEqual(dispatch(harness, "/compact"), "compacted:manual")
        self.assertEqual(dispatch(harness, "/doctor"), "sync doctor")
        self.assertEqual(dispatch(harness, "/me quota"), "sync panel quota")
        self.assertEqual(dispatch(harness, "/overflow"), "sync policy")
        self.assertEqual(dispatch(harness, "/overflow truncate"), "sync set all=truncate")
        self.assertEqual(dispatch(harness, "/overflow reset"), "sync reset all")
        self.assertEqual(dispatch(harness, "/tools"), "tools: none")
        self.assertEqual(dispatch(harness, "/tools reload"), "skill reload completed")
        self.assertEqual(dispatch(harness, "/hooks"), "hooks: none")
        self.assertEqual(dispatch(harness, "/hooks reload"), "hook reload completed")
        self.assertEqual(dispatch(harness, "/hooks stop x"), "hook stop completed: x")
        self.assertEqual(dispatch(harness, "/state reload"), "state.toml 已重新加载，运行状态无变化。")
        self.assertEqual(dispatch(harness, "/log"), "no log entries found")

    def test_async_implementations_are_accepted(self) -> None:
        harness = Harness()
        self.assertEqual(dispatch(harness, "/compact"), "context compacted")
        self.assertEqual(dispatch(harness, "/doctor"), "配置检查：一切正常")
        self.assertEqual(dispatch(harness, "/me"), "my panel (all)")
        self.assertEqual(dispatch(harness, "/tools reload"), "skill reload completed")
        self.assertEqual(dispatch(harness, "/hooks stop plug"), "hook stop completed: plug")
        self.assertEqual(dispatch(harness, "/state reload").startswith("state.toml 已重新加载"), True)


class DependencyFallbackTests(unittest.TestCase):
    """``Deps`` 全空时，可选服务缺失必须回退成提示而不是崩溃。"""

    def test_optional_services_report_cleanly(self) -> None:
        harness = Harness(with_services=False)
        harness.sessions, harness.store = harness.sessions, harness.store
        cases = {
            "/tools": "tool runtime is not configured",
            "/hooks": "hook service is not configured",
            "/doctor": "配置检查未启用。",
            "/state": "运行时状态热加载未配置。",
            "/chars": "角色素材库未配置。",
            "/memory": "angel memory 未启用或未配置。",
            "/learning": "self learning 未启用或未配置。",
            "/forget list": "angel memory 未启用或未配置。",
            "/log": "log reader is not configured",
        }
        for text, expected in cases.items():
            self.assertEqual(dispatch(harness, text), expected, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
