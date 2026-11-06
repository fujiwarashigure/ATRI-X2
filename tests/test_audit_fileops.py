"""全仓审查：文件编辑与回滚的数据完整性回归。"""
import unittest
from atri_x.utils.fileops.edit import apply_edits, Edit


class SessionPreviewScopeAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_delete_preview_reject_foreign_scope(self):
        from atri_x.errors import ForbiddenError
        from atri_x.session import Scope, CreateRequest
        from tests.agent_commands_fakes import Harness
        harness = Harness(admin=False)
        self.addCleanup(harness.store.close)
        foreign = harness.sessions.create(
            Scope(actor_id="other", platform="qq", platform_scope_id="private:2"),
            CreateRequest(title="PRIVATE_FOREIGN_TITLE"),
        )
        for name in ("archive", "delete"):
            with self.subTest(command=name):
                with self.assertRaises(ForbiddenError):
                    await harness.router.dispatch(harness.ctx, f"/{name} {foreign.id}")
        self.assertEqual(harness.store.sessions().get(foreign.id).title, "PRIVATE_FOREIGN_TITLE")

    async def test_owned_preview_still_displays_title_without_mutation(self):
        from tests.agent_commands_fakes import Harness
        harness = Harness(admin=False)
        self.addCleanup(harness.store.close)
        owned = harness.create_session(title="OWNED_TITLE")
        for name in ("archive", "delete"):
            result = await harness.router.dispatch(harness.ctx, f"/{name} {owned.id}")
            self.assertIn("OWNED_TITLE", result.content)
            self.assertIn("--confirm", result.content)
        self.assertFalse(harness.store.sessions().get(owned.id).archived_at)


class PanicFailureIsolationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_failure_keeps_later_stop_steps_and_reports_incomplete(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock, patch
        from atri_x.agent.commands.approval import new_panic
        from atri_x.command import Request
        for failed in ("grants", "snapshot", "turns", "requests", "inbox", "sandbox", "audit"):
            with self.subTest(failed=failed):
                def step(label, value):
                    return Mock(side_effect=RuntimeError("injected")) if failed == label else Mock(return_value=value)
                inbox = step("inbox", 1)
                # A single failing inbox must not prevent cancelling the next one.
                if failed == "inbox":
                    inbox.side_effect = [RuntimeError("injected"), 1]
                deps = SimpleNamespace(
                    scope=lambda ctx: SimpleNamespace(platform_scope_id="private:test"),
                    grants=SimpleNamespace(clear=step("grants", 2)),
                    turns=SimpleNamespace(snapshot_all=step("snapshot", [SimpleNamespace(session_id="a"), SimpleNamespace(session_id="b")]),
                                          stop_all=step("turns", None)),
                    requests=SimpleNamespace(cancel_all=step("requests", 3)),
                    cancel_session_inbox=inbox, audit=step("audit", None),
                )
                sandbox = SimpleNamespace(stop_all=AsyncMock(return_value=4))
                if failed == "sandbox":
                    sandbox.stop_all.side_effect = RuntimeError("injected")
                with patch("atri_x.agent.commands.approval._sandbox_service", return_value=sandbox):
                    result = await new_panic(deps).handle(None, Request(args=""))
                deps.turns.stop_all.assert_called_once()
                deps.requests.cancel_all.assert_called_once()
                sandbox.stop_all.assert_awaited_once_with(reason="panic")
                if failed != "snapshot":
                    self.assertEqual(inbox.call_count, 2)
                self.assertIn("未完成", result.content)
                self.assertIn(failed, result.content)
                self.assertNotIn("injected", result.content)


class SkillConfigCancellationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_boundary_preserves_asyncio_cancellation(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.tool.skill.agent_skill_tool import AgentSkillTool
        preview = SimpleNamespace(action="write", name="synthetic", toml="risk='low'")
        tool = SimpleNamespace(preview=AsyncMock(return_value=preview), manager=SimpleNamespace(
            write_agent_skill_config=AsyncMock(side_effect=asyncio.CancelledError())))
        with self.assertRaises(asyncio.CancelledError):
            await AgentSkillTool.call(tool, None, None)


class SkillManagerRollbackAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_cancellation_preserved_after_restore_success_or_failure(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock, patch
        from atri_x.tool.skill.manager import Manager
        from atri_x.tool.skill.catalog import Kind
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                cancelled = asyncio.CancelledError("synthetic cancellation")
                snapshot = SimpleNamespace(restore=Mock(side_effect=OSError("restore failure") if rollback_fails else None))
                scanner = SimpleNamespace(root="synthetic", catalog=SimpleNamespace(
                    get=lambda name: (SimpleNamespace(root="synthetic/alpha", kind=Kind.AGENT), True)),
                    reload=AsyncMock(side_effect=cancelled))
                manager = Manager("synthetic", registry=object(), scanner=scanner)
                with patch("atri_x.tool.skill.manager.capture_file", return_value=snapshot), patch(
                        "atri_x.tool.skill.manager.atomic_write_file") as write, patch(
                        "atri_x.tool.skill.manager._existing_file_mode", return_value=0o600):
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await manager.write_agent_skill_config(None, "alpha", 'risk = "low"\n')
                self.assertIs(caught.exception, cancelled)
                snapshot.restore.assert_called_once()
                write.assert_called_once()
                self.assertFalse(manager.loaded)
                self.assertIsNotNone(manager.last_error)
                if rollback_fails:
                    self.assertIsInstance(cancelled.__cause__, OSError)


class SkillMediaAsyncAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_media_and_stdout_await_runtime_operations(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.tool.skill.media import prepare_command_media, result_from_stdout_with_media
        call = SimpleNamespace(export=AsyncMock(return_value=("media/input.bin", None)),
            import_=AsyncMock(return_value=SimpleNamespace(id="new-id")),
            retain=AsyncMock(return_value=SimpleNamespace(name="output.bin", mime_type="test/type")))
        manifest = SimpleNamespace(parameters={"type": "object", "properties": {
            "nested": {"type": "array", "items": {"type": "object", "properties": {
                "blob": {"type": "media"}}}}}})
        value = prepare_command_media(None, '{"nested":[{"blob":"input-id"}]}', manifest, call, "synthetic")
        if hasattr(value, "__await__"):
            value = await value
        self.assertIn("media/input.bin", value)
        call.export.assert_awaited_once()
        result = result_from_stdout_with_media(None, '{"segments":[{"type":"file","path":"out.bin"}]}', call, "synthetic")
        if hasattr(result, "__await__"):
            result = await result
        self.assertEqual(result.segments[0].media, "new-id")
        call.import_.assert_awaited_once()
        call.retain.assert_awaited_once_with(None, "new-id")


class RealMediaRuntimeAdapterAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdout_retains_and_close_releases_real_runtime_reference(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from atri_x.tool.media_runtime import MediaRuntime
        from atri_x.tool.skill.media import result_from_stdout_with_media
        media_id = "media:" + "a" * 64
        center = SimpleNamespace(metadata=AsyncMock(return_value=SimpleNamespace(
            id=media_id, name="synthetic.bin", mime_type="test/type")),
            add_reference=Mock(), remove_reference=Mock())
        call = MediaRuntime(center=center).new_call()
        try:
            result = await result_from_stdout_with_media(None, json.dumps({"segments": [
                {"type": "file", "media": media_id}, {"type": "file", "media": media_id}]}), call, "synthetic")
            self.assertEqual(result.segments[0].name, "synthetic.bin")
            center.add_reference.assert_called_once()
            self.assertIn(media_id, call.refs)
        finally:
            await call.close()
        center.remove_reference.assert_called_once()
        self.assertFalse(call.refs)


class AsyncModelCompletionAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_catalog_fuzzy_fallback_is_awaited(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.agent.commands.model import new_model
        from atri_x.command import CompletionRequest
        catalog = AsyncMock(side_effect=[SimpleNamespace(options=[]), SimpleNamespace(
            options=[SimpleNamespace(provider="fake", model="abc-model")])])
        command = new_model(SimpleNamespace(models=SimpleNamespace(model_list=catalog)))
        items = await command.complete(None, CompletionRequest(
            raw="/model acm", prefix="/", name="model", args="acm", cursor=10))
        self.assertEqual([item.text for item in items], ["fake/abc-model"])
        self.assertEqual(catalog.await_count, 2)


class ShutdownAggregationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_cleanup_error_remain_in_base_exception_group(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.app.runner import Runner
        runner = SimpleNamespace(_build_and_run=AsyncMock(side_effect=asyncio.CancelledError()),
                                 _shutdown=AsyncMock(return_value=ValueError("cleanup")))
        with self.assertRaises(BaseExceptionGroup) as caught:
            await Runner.run(runner)
        self.assertIsInstance(caught.exception.exceptions[0], asyncio.CancelledError)
        self.assertIsInstance(caught.exception.exceptions[1], ValueError)


class PlatformStartupCleanupAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_callback_failure_cancels_started_adapters(self):
        import asyncio
        from types import SimpleNamespace
        from atri_x.app.platforms import run_platforms
        entered, exited = asyncio.Event(), asyncio.Event()
        async def run(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        adapter = SimpleNamespace(name=lambda: "audit", run=run)
        async def fail(*args):
            await entered.wait()
            raise ValueError("callback failure")
        before = asyncio.all_tasks()
        try:
            with self.assertRaises(ValueError):
                await run_platforms(None, None, None, [adapter], after_start=fail)
            self.assertTrue(exited.is_set())
        finally:
            leaked = asyncio.all_tasks() - before
            for task in leaked:
                task.cancel()
            await asyncio.gather(*leaked, return_exceptions=True)


class RunnerStopAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapped_external_event_reaches_platform_contract(self):
        import asyncio
        from atri_x.app.runner import _RunContext
        from atri_x.app.platforms import _RunFlag, _wait_outer
        stop = asyncio.Event()
        wrapped = _RunContext(stop)
        flag = _RunFlag(wrapped)
        pending = asyncio.create_task(_wait_outer(wrapped))
        stop.set()
        try:
            self.assertTrue(flag.is_set())
            await asyncio.wait_for(pending, .2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


class RollbackReplacementAuditTests(unittest.TestCase):
    def test_second_edit_same_file_remains_discoverable(self):
        from atri_x.utils.fileops.rollback import RollbackStore
        store = RollbackStore()
        first, _ = store.record("audit", "synthetic", b"before", 0o600, True, "r0", "r1")
        second, available = store.record("audit", "synthetic", b"middle", 0o600, True, "r1", "r2")
        self.assertTrue(available)
        self.assertEqual([entry.id for entry in store.list("audit")], [second.id])
        self.assertTrue(store.get("audit", second.id)[1])
        self.assertFalse(store.get("audit", first.id)[1])


class ToolPolicyMigrationAuditTests(unittest.TestCase):
    def test_preserves_generic_restrictions_and_canonical_policy_pairs(self):
        from copy import deepcopy
        from unittest.mock import patch
        from atri_x.gui import toolpolicy
        data = {"group_policy": {
            "qqonebot · group:1": {"tool_allowlist": [], "tool_allowlist_set": True},
            "qqonebot · group:2": {"tool_allowlist": ["read_file"], "tool_allowlist_set": True},
            "qqonebot · group:3": {"tool_allowlist_chat": ["shell"], "tool_allowlist_chat_set": True},
            "qqonebot:group:3": {"tool_allowlist_chat_set": True},
            "qqonebot · group:4": {"tool_allowlist_work": ["shell"], "tool_allowlist_work_set": True},
            "qqonebot:group:4": {"tool_allowlist_work": []},
        }}
        def section_key(section):
            return section.split('"')[1]
        def update(path, section, fields):
            target = data["group_policy"].setdefault(section_key(section), {})
            for key, value in fields.items():
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
        def remove(path, section):
            data["group_policy"].pop(section_key(section), None)
        with patch.object(toolpolicy.os.path, "exists", return_value=True), patch.object(
                toolpolicy, "read_toml", side_effect=lambda path: deepcopy(data)), patch.object(
                toolpolicy, "patch_section", side_effect=update), patch.object(
                toolpolicy, "remove_section", side_effect=remove):
            toolpolicy.save_scope_mode_policy("synthetic", "qqonebot:group:1", "chat", "none", [])
            policies = toolpolicy.load_scope_mode_policies("synthetic")
        self.assertEqual(policies["qqonebot:group:1"]["generic"], ("none", []))
        self.assertEqual(policies["qqonebot:group:2"]["generic"], ("whitelist", ["read_file"]))
        self.assertEqual(policies["qqonebot:group:3"]["chat"], ("none", []))
        self.assertEqual(policies["qqonebot:group:4"]["work"], ("none", []))


class SafetyDeletionContractAuditTests(unittest.TestCase):
    def test_none_is_removed_before_validation_and_forwarded_to_patch(self):
        from unittest.mock import Mock, patch
        from atri_x.gui import safetyconfig
        observed = []
        def validate(value, **kwargs):
            observed.append(value)
        with patch.object(safetyconfig, "safety_path", return_value="synthetic"), patch.object(
                safetyconfig, "read_toml", return_value={"local": {"max_scan_chars": 123}}), patch.object(
                safetyconfig.SafetySettings, "from_dict", side_effect=validate), patch.object(
                safetyconfig, "patch_section", return_value=True) as write, patch.object(
                safetyconfig, "_validate_file"):
            safetyconfig.save_safety_sections(Mock(), {"local": {"max_scan_chars": None}})
        self.assertNotIn("max_scan_chars", observed[0]["local"])
        write.assert_called_once_with("synthetic", "local", {"max_scan_chars": None})


class ToolTagSerializationAuditTests(unittest.TestCase):
    def test_dotted_tag_roundtrips_as_literal_key(self):
        import tomllib
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from atri_x.gui.tooltagedit import ToolTagsPanel
        tags = {"a": {"tools": ["read_file"], "prompt": "first"},
                "a.b": {"tools": ["web_search"], "prompt": "second"}}
        panel = SimpleNamespace(paths=SimpleNamespace(tool_tags_path="synthetic"),
            _tags=tags, _capture=Mock(), _fail=Mock(), _notify=Mock(), _journal=Mock())
        with patch("atri_x.gui.tooltagedit.write_text_file") as write:
            ToolTagsPanel.save(panel)
        self.assertEqual(tomllib.loads(write.call_args.args[1])["tags"], tags)

    def test_duplicate_new_tag_does_not_replace_existing_entry(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from atri_x.gui.tooltagedit import ToolTagsPanel
        entry = {"tools": ["read_file"], "prompt": "sentinel"}
        panel = SimpleNamespace(_tags={"existing": entry}, frame=None, _capture=Mock(),
            _refill=Mock(), _select=Mock(), _notify=Mock(), _fail=Mock())
        with patch("tkinter.simpledialog.askstring", return_value="existing"):
            ToolTagsPanel._new_tag(panel)
        self.assertIs(panel._tags["existing"], entry)
        panel._fail.assert_called_once()


class ProviderHeaderDeletionAuditTests(unittest.TestCase):
    def test_removed_header_is_explicitly_deleted(self):
        from unittest.mock import patch
        from atri_x.gui import configmodel
        old = {"X-Old": "synthetic-secret", "X-Keep": "before"}
        def merge(path, section, fields, **kwargs):
            for key, value in fields.items():
                if value is None:
                    old.pop(key, None)
                else:
                    old[key] = value
        with patch.object(configmodel, "read_toml", return_value={"providers": {"fake": {
                "extra_headers": dict(old)}}}), patch.object(configmodel, "patch_section", side_effect=merge):
            configmodel._save_extra_headers("synthetic", "fake", {"X-Keep": "after"})
        self.assertEqual(old, {"X-Keep": "after"})


class WordlistCreationBoundaryAuditTests(unittest.TestCase):
    def test_panel_uses_exclusive_creation_boundary(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from atri_x.gui.safetywordedit import WordlistPanel
        from atri_x.gui.configedit import ConfigEditError
        panel = SimpleNamespace(paths=object(), frame=None, tab=SimpleNamespace(fail=Mock()), reload=Mock())
        with patch("tkinter.simpledialog.askstring", return_value="../outside"), patch(
                "atri_x.gui.safetyconfig.load_settings", return_value=(None, [])), patch(
                "atri_x.gui.safetyconfig.wordlists_dir", return_value="synthetic"), patch(
                "atri_x.gui.safetyconfig.create_list_file", side_effect=ConfigEditError("rejected")) as create, patch(
                "atri_x.gui.safetyconfig.write_list_file") as overwrite:
            WordlistPanel._create(panel, "wordlist")
        create.assert_called_once()
        overwrite.assert_not_called()
        panel.tab.fail.assert_called_once()
        panel.reload.assert_not_called()


class LocalStatusDestinationAuditTests(unittest.TestCase):
    def test_status_endpoints_reject_remote_and_malformed_addresses(self):
        from atri_x.gui import linkstate, timeview, perfconfig
        for fn in (linkstate.health_endpoint, timeview.diagnostics_endpoint, perfconfig.metrics_endpoint):
            for addr in ("example.com:32171", "192.0.2.1:80", "user@127.0.0.1:80", "127.0.0.1:99999", "127.0.0.1:abc"):
                with self.subTest(endpoint=fn.__name__, addr=addr), self.assertRaises(Exception):
                    fn(addr)

    def test_local_ipv6_wildcards_and_redirect_refusal(self):
        import urllib.request
        from atri_x.gui.timeview import local_status_endpoint, LocalStatusRedirectHandler
        for addr, host in (("localhost:32171", "127.0.0.1"), ("0.0.0.0:32171", "127.0.0.1"),
                           ("[::]:32171", "127.0.0.1"), ("[::1]:32171", "[::1]")):
            self.assertEqual(local_status_endpoint(addr, "/metrics", ValueError), f"http://{host}:32171/metrics")
        req = urllib.request.Request("http://127.0.0.1:32171/metrics", headers={"Authorization": "Bearer synthetic"})
        for target in ("https://example.com/", "http://127.0.0.1:32171/other"):
            self.assertIsNone(LocalStatusRedirectHandler().redirect_request(req, None, 302, "found", {}, target))

    def test_bad_websocket_port_returns_none(self):
        from atri_x.gui.linkstate import parse_ws_url
        for addr in ("ws://host:abc/", "ws://host:65536/", "ws://[broken/"):
            with self.subTest(addr=addr):
                self.assertIsNone(parse_ws_url(addr))


class ProtocolProcessIdentityAuditTests(unittest.TestCase):
    def test_os_creation_probe_reads_current_process_stably(self):
        import os
        from atri_x.gui.proto_process import _pid_creation_id
        if os.name != "nt":
            self.skipTest("Windows OS probe verification; Linux fallback not verified here")
        first = _pid_creation_id(os.getpid())
        self.assertTrue(first.startswith("win:"))
        self.assertEqual(first, _pid_creation_id(os.getpid()))

    def test_stop_refuses_missing_or_changed_creation_identity(self):
        from unittest.mock import patch
        from atri_x.gui import proto_process
        for recorded, actual, image in (("old", "new", "cmd.exe"), ("", "new", "cmd.exe"),
                                         ("old", "", "cmd.exe"), ("old", "old", "")):
            with self.subTest(recorded=recorded, actual=actual, image=image), patch.object(
                    proto_process, "read_state", return_value={"pid": 123, "image": "cmd", "creation_id": recorded}), patch.object(
                    proto_process, "_pid_alive", return_value=True), patch.object(
                    proto_process, "_pid_image", return_value=image), patch.object(
                    proto_process, "_pid_creation_id", return_value=actual, create=True), patch.object(
                    proto_process, "_terminate_tree") as terminate, patch.object(
                    proto_process, "clear_state"), patch.object(proto_process, "STOP_WAIT_SECONDS", 0):
                self.assertFalse(proto_process.managed_status("synthetic")["managed"])
                self.assertFalse(proto_process.stop_managed("synthetic")[0])
                terminate.assert_not_called()


class PromptImportEditorAuditTests(unittest.TestCase):
    def test_import_current_id_refreshes_editor_before_next_capture(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock, mock_open, patch
        from atri_x.gui.promptedit import PromptTemplatesPanel
        from atri_x.promptlib.model import PromptEntry
        editor = {"text": "OLD"}
        entries = {"custom.1": PromptEntry(id="custom.1", content="OLD")}
        def capture():
            entries["custom.1"].content = editor["text"]
        def select(prompt_id):
            editor["text"] = entries[prompt_id].content
        panel = SimpleNamespace(paths=object(), frame=None, _entries=entries, _order=["custom.1"],
            _current_id="custom.1", _capture=capture, _select=select, _fill_tree=Mock(), _notify=Mock(), _fail=Mock())
        payload = json.dumps({"prompts": [{"id": "custom.1", "title": "Imported", "category": "custom", "content": "NEW"}]})
        with patch("atri_x.gui.promptedit.filedialog.askopenfilename", return_value="synthetic"), patch(
                "builtins.open", mock_open(read_data=payload)):
            PromptTemplatesPanel._import(panel)
        capture()
        self.assertEqual(entries["custom.1"].content, "NEW")
        panel._fail.assert_not_called()


class ReactionWeightAuditTests(unittest.TestCase):
    def test_fractional_weights_preserved_and_nonfinite_defaults(self):
        import math
        from atri_x.gui.reactionpage import _number
        self.assertEqual(_number("0.5", "weight"), .5)
        self.assertEqual(_number("1.9", "weight"), 1.9)
        self.assertEqual(_number("0", "weight"), 0)
        for text in ("nan", "inf", "-inf", "1e999"):
            for key in ("weight", "probability", "cooldown_seconds"):
                with self.subTest(text=text, key=key):
                    self.assertTrue(math.isfinite(_number(text, key)))


class ProtocolCredentialCommitAuditTests(unittest.TestCase):
    def test_token_write_failure_never_commits_reference_and_retry_succeeds(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from atri_x.gui import proto_manager, configedit
        setup = SimpleNamespace(available=True, backend="napcat", ws_url="ws://127.0.0.1:3001", token="synthetic")
        events = []
        def commit(*args):
            events.append("config")
            return True
        def store(*args):
            events.append("token")
            return True
        with patch.object(proto_manager.os.path, "isfile", return_value=True), patch.object(
                configedit, "read_toml", return_value={"platform": {"qqonebot": {}}}), patch.object(
                configedit, "patch_section", side_effect=commit) as write, patch.object(
                configedit, "set_env_value", side_effect=configedit.ConfigEditError("synthetic failure")) as token:
            self.assertFalse(proto_manager.apply_connection(setup, "app", "env"))
            write.assert_not_called()
            token.side_effect = store
            self.assertTrue(proto_manager.apply_connection(setup, "app", "env"))
        self.assertEqual(events, ["token", "config"])

    def test_missing_env_path_does_not_create_dangling_reference(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from atri_x.gui import proto_manager, configedit
        setup = SimpleNamespace(available=True, backend="napcat", ws_url="ws://127.0.0.1:3001", token="synthetic")
        with patch.object(proto_manager.os.path, "isfile", return_value=True), patch.object(
                configedit, "read_toml", return_value={}), patch.object(configedit, "patch_section") as write:
            self.assertFalse(proto_manager.apply_connection(setup, "app", ""))
            write.assert_not_called()


class PlatformLoginLabelAuditTests(unittest.TestCase):
    def test_transport_connected_is_not_qq_logged_in(self):
        from atri_x.gui.linkstate import PlatformEntry, describe_platform
        for checked in (False, True):
            text = describe_platform(PlatformEntry(connected=True, login_checked=checked, login_ok=False),
                                     enabled=True, service_known=True)
            self.assertNotIn("QQ 在线", text)
        text = describe_platform(PlatformEntry(connected=True, login_checked=True, login_ok=True),
                                 enabled=True, service_known=True)
        self.assertIn("QQ 在线", text)


class LongMemoryExternalChangeAuditTests(unittest.TestCase):
    def test_update_rechecks_parsed_scope_and_requested_id(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from atri_x.errors import InvalidError
        from atri_x.tool.builtin.long_memory_store import LongMemoryStore
        from atri_x.tool.builtin.long_memory_model import LongMemoryRecord, LongMemoryUpdateArgs
        from tests.test_long_memory_scope import _ctx
        old = LongMemoryRecord(id=1, file_path="synthetic", source_scope="qq_onebot:group:1")
        for changed in (LongMemoryRecord(id=1, source_scope="qq_onebot:group:2"),
                        LongMemoryRecord(id=2, source_scope="qq_onebot:group:1")):
            store = SimpleNamespace(_ensure=Mock(), _record_by_id=Mock(return_value=old))
            with self.subTest(id=changed.id, scope=changed.source_scope), patch(
                    "atri_x.tool.builtin.long_memory_store.read_file", return_value=SimpleNamespace(text="synthetic")), patch(
                    "atri_x.tool.builtin.long_memory_store.parse_long_memory_text"), patch(
                    "atri_x.tool.builtin.long_memory_store.record_from_parsed_file", return_value=changed), self.assertRaises(InvalidError):
                try:
                    LongMemoryStore._preview_update(store, _ctx("group:1"), LongMemoryUpdateArgs(id=1, title="new"))
                except InvalidError as exc:
                    self.assertTrue("会话范围" in str(exc) or "ID" in str(exc), str(exc))
                    raise


class NoticeCloseCompletionAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_first_close_does_not_make_next_close_skip_drain(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock
        from atri_x.agent.notice_queue import NoticeQueues
        queues = NoticeQueues()
        done = asyncio.Event()
        queues._queues["synthetic"] = SimpleNamespace(begin_close=Mock(), done=lambda: done)
        first = asyncio.create_task(queues.close(None))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(queues.close(None))
        try:
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            done.set()
            await asyncio.wait_for(second, .2)
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


class ToolRequestHandoffAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_cleanup_without_calling_it(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock
        from atri_x.agent.toolrun_adapter import AgentToolRunDeps
        done = Mock()
        deps = SimpleNamespace(agent=SimpleNamespace(requests=SimpleNamespace(
            start=Mock(return_value=(SimpleNamespace(started_at=None), "tool-context", done)))),
            output=SimpleNamespace(publish_runtime_status=AsyncMock()))
        ctx, started, cleanup = await AgentToolRunDeps.start_tool_request(deps, None, "session", "fake")
        self.assertEqual(ctx, "tool-context")
        self.assertIs(cleanup, done)
        done.assert_not_called()
        cleanup()
        done.assert_called_once()

    async def test_publisher_failure_cleans_registered_request_before_handoff(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock
        from atri_x.agent.toolrun_adapter import AgentToolRunDeps
        for error in (RuntimeError("publish"), asyncio.CancelledError()):
            for asynchronous in (False, True):
                with self.subTest(error=type(error).__name__, asynchronous=asynchronous):
                    done = Mock()
                    publish = AsyncMock(side_effect=error) if asynchronous else Mock(side_effect=error)
                    deps = SimpleNamespace(agent=SimpleNamespace(requests=SimpleNamespace(
                        start=Mock(return_value=(SimpleNamespace(started_at=None), None, done)))),
                        output=SimpleNamespace(publish_runtime_status=publish))
                    with self.assertRaises(type(error)):
                        await AgentToolRunDeps.start_tool_request(deps, None, "session", "fake")
                    done.assert_called_once_with()


class EditBoundaryAuditTests(unittest.TestCase):
    def test_replace_unterminated_final_line_preserves_separator(self):
        self.assertEqual(apply_edits("first\nlast", [Edit(operation="replace", line=2, new_text="NEW")]), "first\nNEW")

    def test_delete_unterminated_final_line_still_removes_separator(self):
        self.assertEqual(apply_edits("first\nlast", [Edit(operation="delete", line=2)]), "first")
