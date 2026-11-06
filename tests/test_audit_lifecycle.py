"""全仓审查：后台任务的所有权与重新启动回归。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from atri_x.agent import state_watch


class SharedArtistCatalogAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonadmin_tag_write_is_rejected_before_any_catalog_mutation(self):
        from unittest.mock import Mock
        from atri_x.tool.builtin.artist_library import ArtistLibrarySaveTool
        from atri_x.tool.tool import CallRequest
        service = Mock()
        tool = SimpleNamespace(deps=SimpleNamespace(enabled=lambda: True, service=service), name=lambda: "artist_library_save")
        with patch("atri_x.tool.builtin.artist_library.artist_viewer", return_value=SimpleNamespace(actor_id="user", superadmin=False)):
            result = await ArtistLibrarySaveTool.call(tool, None, CallRequest(arguments=
                '{"name":"fake","artists":[{"artist_id":"existing","tag":"replacement"}]}'))
        service.store.assert_not_called()
        service.save.assert_not_called()
        self.assertIn("超级管理员", result.content)


class PlatformCloseRecoveryTests(unittest.TestCase):
    def test_async_close_recovers_after_no_loop_begin_close(self):
        from unittest.mock import Mock
        from atri_x.agent import platform_signals
        subs = platform_signals.PlatformSubscriptions()
        done = asyncio.Event()
        queue = SimpleNamespace(begin_close=Mock(side_effect=done.set), done=lambda: done)
        subs.queues["fake"] = queue
        host = SimpleNamespace(platform_events=subs)
        platform_signals.begin_close_platform_events(host)
        async def scenario():
            task = asyncio.create_task(platform_signals.close_platform_events(host, None))
            try:
                await asyncio.wait_for(task, .2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())
        self.assertTrue(subs.done.is_set())


class MappingCoverageCompletenessTests(unittest.TestCase):
    def test_partial_brace_group_is_not_reported_covered(self):
        import importlib.util
        from pathlib import Path
        import contextlib
        import io
        path = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_mapping_gate", path)
        gate = importlib.util.module_from_spec(spec)
        import sys
        with patch.dict(sys.modules, {spec.name: gate}):
            spec.loader.exec_module(gate)
        mapping = SimpleNamespace(py_pattern="fake/{a,b}.py", go_package="synthetic", go_lines=1)
        with patch.object(gate, "parse_mapping", return_value=[mapping]), patch.object(
                gate, "upstream_go_files", return_value={}), patch.object(
                gate, "_resolve_one", side_effect=lambda pattern: [gate.REPO / "a.py"] if pattern.endswith("a.py") else []), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["--strict", "--summary"]), 1)


class SyntaxGateFailureTests(unittest.TestCase):
    def test_invalid_source_reports_failure_without_crashing_and_crlf_fails(self):
        import importlib.util
        import contextlib
        import io
        from pathlib import Path
        script = Path(__file__).resolve().parents[1] / "scripts" / "check_syntax.py"
        spec = importlib.util.spec_from_file_location("audit_syntax_gate", script)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        class FakePath:
            def relative_to(self, root):
                return Path("synthetic.py")
            def read_bytes(self):
                return self.raw
        for raw in (b"def broken(:\n", b"x = 1\r\n"):
            path = FakePath()
            path.raw = raw
            with self.subTest(raw=raw), patch.object(gate, "iter_py_files", return_value=[path]), patch(
                    "sys.argv", ["check_syntax.py", "--strict"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(), 1)


class FaqSendCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_send_is_not_swallowed_or_audited_as_failure(self):
        from unittest.mock import AsyncMock, Mock
        from atri_x.agent.group_knowledge import _send_answer
        host = SimpleNamespace(audit=Mock())
        with self.assertRaises(asyncio.CancelledError):
            await _send_answer(host, None, SimpleNamespace(platform="qq", platform_scope_id="group:1"),
                               SimpleNamespace(id="fake"), "synthetic", AsyncMock(side_effect=asyncio.CancelledError()))
        host.audit.assert_not_called()


class GenericStatusAwaitableTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_awaitable_is_scheduled_and_released(self):
        from atri_x.agent import status
        calls = []
        class Awaitable:
            def __await__(self):
                async def send():
                    calls.append("sent")
                return send().__await__()
        before = set(status._PENDING_STATUS_TASKS)
        status._consume_status_result(SimpleNamespace(), Awaitable())
        pending = status._PENDING_STATUS_TASKS - before
        await asyncio.gather(*pending)
        await asyncio.sleep(0)
        self.assertEqual(calls, ["sent"])
        self.assertFalse(status._PENDING_STATUS_TASKS - before)


class UnicodeMentionBoundaryTests(unittest.TestCase):
    def test_expanding_lowercase_does_not_shift_original_offsets(self):
        from atri_x.agent.wakeup import strip_bot_mention
        msg = SimpleNamespace(bot=SimpleNamespace(username="bot"))
        prefix = "\u0130" * 6
        self.assertEqual(strip_bot_mention(prefix + " @bot", msg, []), prefix)
        self.assertEqual(strip_bot_mention("a  @BOT\nrest", msg, []), "a  \nrest")
        self.assertEqual(strip_bot_mention("mail@bot.example", msg, []), "mail@bot.example")


class ModelCacheGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_inflight_cannot_repopulate_cleared_cache(self):
        from atri_x.agent.model_list import ModelListCache
        cache = ModelListCache()
        entered, release = asyncio.Event(), asyncio.Event()
        async def old(provider):
            entered.set()
            await release.wait()
            return ["old"], None
        async def new(provider):
            return ["new"], None
        first = asyncio.create_task(cache.load("fake", False, old))
        await entered.wait()
        cache.clear()
        release.set()
        second = await cache.load("fake", False, new)
        await first
        self.assertEqual(second[0], ["new"])
        self.assertEqual((await cache.load("fake", False, new))[0], ["new"])


class AsyncCancellationClassificationTests(unittest.TestCase):
    def test_task_cancellation_is_not_user_error(self):
        from atri_x.agent.errors import should_notify_user_error
        self.assertFalse(should_notify_user_error(asyncio.CancelledError()))
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = asyncio.CancelledError()
        self.assertFalse(should_notify_user_error(wrapped))
        self.assertTrue(should_notify_user_error(ValueError("ordinary")))


class MemoryConfirmationTypeTests(unittest.TestCase):
    def test_confirm_rejects_non_boolean_values(self):
        from atri_x.tool.builtin.angel_memory import _decode, AngelForgetArgs
        from atri_x.errors import InvalidError
        for raw in ('{"confirm":"false"}', '{"confirm":1}', '{"confirm":[]}', '{"confirm":null}'):
            with self.subTest(raw=raw), self.assertRaises(InvalidError):
                _decode(raw, AngelForgetArgs, "angel_forget")
        self.assertFalse(_decode('{"confirm":false}', AngelForgetArgs, "angel_forget").confirm)
        self.assertTrue(_decode('{"confirm":true}', AngelForgetArgs, "angel_forget").confirm)


class InboundMediaOrdinalTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_first_media_does_not_renumber_second_history_slot(self):
        from unittest.mock import AsyncMock, Mock
        from atri_x.agent.inbound_media import materialize_platform_media
        from atri_x.platform import MessageContext, MessageSegment, SegmentImage, with_message_context
        msg = MessageContext(platform="qq", scope_id="group:1", platform_message_id="m", segments=[
            MessageSegment(type=SegmentImage, url="https://fake/one"),
            MessageSegment(type=SegmentImage, url="https://fake/two")])
        media = SimpleNamespace(history=SimpleNamespace(get_by_platform_message=Mock(return_value=object())),
            import_platform=AsyncMock(side_effect=[ValueError("synthetic"), SimpleNamespace(
                id="media:second", name="two", mime_type="image/png", size=1)]), associate_history=AsyncMock())
        await materialize_platform_media(SimpleNamespace(media=media), with_message_context(None, msg))
        self.assertEqual(media.associate_history.await_args.args[2], 2)

    async def test_import_cancellation_is_not_degraded(self):
        from unittest.mock import AsyncMock
        from atri_x.agent.inbound_media import materialize_platform_segment
        from atri_x.platform import MessageSegment, SegmentImage
        host = SimpleNamespace(media=SimpleNamespace(import_platform=AsyncMock(side_effect=asyncio.CancelledError())))
        with self.assertRaises(asyncio.CancelledError):
            await materialize_platform_segment(host, None, SimpleNamespace(), MessageSegment(type=SegmentImage))


class GroupRuntimeNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_sender_is_executed_and_task_observed(self):
        from unittest.mock import AsyncMock
        from atri_x.agent.group_runtime import notify_group_runtime_change
        sender = AsyncMock()
        agent = SimpleNamespace(send_notice=sender)
        notify_group_runtime_change(agent, SimpleNamespace(platform="qq", platform_scope_id="group:1"), "removed", "synthetic")
        await asyncio.sleep(0)
        sender.assert_awaited_once()
        tasks = getattr(agent, "group_runtime_notice_tasks", set())
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        self.assertFalse(getattr(agent, "group_runtime_notice_tasks", set()))


class MediaOutputCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_does_not_erase_original_media_id(self):
        from unittest.mock import AsyncMock, Mock
        from atri_x.agent.media_output import resolve_media_outputs
        from atri_x.delivery import Output, Source, KindImage
        media_id = "media:" + "a" * 64
        original = Output(kind=KindImage, source=Source(media_id=media_id))
        release = Mock()
        host = SimpleNamespace(media=SimpleNamespace(resolve_for_output=AsyncMock(
            return_value=(Source(path="synthetic.png"), release))))
        resolved, cleanup = await resolve_media_outputs(host, None, [original])
        try:
            self.assertEqual(original.source.media_id, media_id)
            self.assertEqual(resolved[0].source.path, "synthetic.png")
            self.assertIsNot(original, resolved[0])
        finally:
            cleanup()
        release.assert_called_once()


class ReminderDispatchLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_runs_outside_thread_lock_and_preserves_new_entries(self):
        from datetime import datetime, timezone, timedelta
        from tests.agent_fakes import FakeAgent
        from atri_x.config import GroupReminderConfig
        from atri_x.agent import group_services_reminders as reminders, group_services_state as state
        agent = FakeAgent()
        due = state.format_group_service_time(datetime.now(timezone.utc) - timedelta(seconds=1))
        agent.group_reminders["qq:group:42"] = [GroupReminderConfig(id="r1", text="old", due_at=due, status="pending")]
        async def send(*args):
            lock = state.group_services_state(agent).write_lock
            acquired = lock.acquire(blocking=False)
            self.assertTrue(acquired, "threading lock held across await")
            try:
                agent.group_reminders["qq:group:42"].append(GroupReminderConfig(id="r2", text="new", due_at=due, status="pending"))
            finally:
                if acquired:
                    lock.release()
        agent.send_notice = send
        await reminders.dispatch_due_reminders(agent)
        entries = agent.group_reminders["qq:group:42"]
        self.assertEqual([item.id for item in entries], ["r1", "r2"])
        self.assertEqual(entries[0].status, "sent")
        self.assertEqual(entries[1].status, "pending")


class ReminderSenderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_propagates_and_missing_sender_is_failure(self):
        from unittest.mock import AsyncMock
        from atri_x.agent.group_services_reminders import _send_notice_async
        with self.assertRaises(asyncio.CancelledError):
            await _send_notice_async(SimpleNamespace(send_notice=AsyncMock(side_effect=asyncio.CancelledError())),
                                     platform="qq", scope_id="group:1", body="synthetic")
        error = await _send_notice_async(SimpleNamespace(), platform="qq", scope_id="group:1", body="synthetic")
        self.assertIsInstance(error, Exception)


class ReminderServiceStartTests(unittest.TestCase):
    def test_no_loop_start_does_not_poison_retry(self):
        from atri_x.agent import group_services_reminders as reminders
        agent = SimpleNamespace()
        with patch.object(reminders, "service_host", return_value=agent), patch.object(
                reminders, "group_services_config", return_value=SimpleNamespace(is_enabled=lambda: True)):
            self.assertIsNone(reminders.start_group_services(agent))
            self.assertFalse(getattr(agent, "group_services_started", False))

    def test_completed_task_can_be_restarted(self):
        from atri_x.agent import group_services_reminders as reminders
        async def scenario():
            agent = SimpleNamespace()
            async def run(*args):
                return
            with patch.object(reminders, "service_host", return_value=agent), patch.object(
                    reminders, "group_services_config", return_value=SimpleNamespace(is_enabled=lambda: True)), patch.object(
                    reminders, "_run_group_services", side_effect=run):
                first = reminders.start_group_services(agent)
                self.assertIs(first, reminders.start_group_services(agent))
                await first
                second = reminders.start_group_services(agent)
                self.assertIsNot(first, second)
                await second
        asyncio.run(scenario())


class ImageGenerationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_and_batch_preserve_child_cancellation(self):
        from unittest.mock import AsyncMock
        from atri_x.tool.builtin.image_generate import generate_images
        for count in (1, 3):
            with self.subTest(count=count), patch(
                    "atri_x.tool.builtin.image_generate.generate_one", new=AsyncMock(side_effect=asyncio.CancelledError())):
                with self.assertRaises(asyncio.CancelledError):
                    await generate_images(None, None, None, count)

    async def test_child_cancel_drains_all_sibling_generators_before_return(self):
        from atri_x.tool.builtin.image_generate import generate_images
        entered = asyncio.Event()
        active = set()
        started = 0
        async def generate(*args):
            nonlocal started
            index = started
            started += 1
            active.add(index)
            if started == 3:
                entered.set()
            try:
                await entered.wait()
                if index == 0:
                    raise asyncio.CancelledError()
                await asyncio.Event().wait()
            finally:
                active.remove(index)
        before = asyncio.all_tasks()
        try:
            with patch("atri_x.tool.builtin.image_generate.generate_one", side_effect=generate):
                with self.assertRaises(asyncio.CancelledError):
                    await generate_images(None, None, None, 3)
            self.assertFalse(active)
        finally:
            leaked = asyncio.all_tasks() - before
            for task in leaked:
                task.cancel()
            await asyncio.gather(*leaked, return_exceptions=True)

    async def test_ordinary_generator_failure_still_collected(self):
        from unittest.mock import AsyncMock
        from atri_x.tool.builtin.image_generate import generate_images
        for count in (1, 3):
            with patch("atri_x.tool.builtin.image_generate.generate_one", new=AsyncMock(side_effect=ValueError("synthetic"))):
                results, errors = await generate_images(None, None, None, count)
                self.assertEqual(results, [None] * count)
                self.assertTrue(all(isinstance(error, ValueError) for error in errors))


class ExtractEngineCacheIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_cache_does_not_satisfy_primary_endpoint(self):
        from unittest.mock import AsyncMock
        from atri_x.tool.builtin.web_extract import WebExtractTool
        from atri_x.tool.builtin.web_extract_engines import ExtractEngineSpec, ExtractEngineFailure, ExtractPage
        from atri_x.tool.tool import CallRequest
        tool = WebExtractTool()
        primary = ExtractEngineSpec(name="primary", protocol="jina", base_url="https://primary.invalid")
        backup = ExtractEngineSpec(name="backup", protocol="jina", base_url="https://backup.invalid")
        tool.extract_chain = lambda *args: [primary, backup]
        tool.http_client = lambda *args: None
        tool.run_engine = AsyncMock(side_effect=[ExtractEngineFailure("synthetic"),
            ExtractPage(url="https://target.invalid", source="backup", content="BACKUP"),
            ExtractPage(url="https://target.invalid", source="primary", content="PRIMARY")])
        req = CallRequest(arguments='{"url":"https://target.invalid"}')
        await tool.call(None, req)
        result = await tool.call(None, req)
        self.assertIn("PRIMARY", result.content)
        self.assertEqual(tool.run_engine.await_count, 3)


class ArtistNegativeCompositionTests(unittest.TestCase):
    def test_artist_negative_respects_all_negative_modes(self):
        from atri_x.tool.builtin.image_generate_compose import compose_image_request
        for mode in ("separate", "inline", "off"):
            with self.subTest(mode=mode):
                tool = SimpleNamespace(cfg=SimpleNamespace(negative_prompt_mode=mode))
                result = compose_image_request(tool, [], "scene", "artist", "", "", False,
                                               artist_negative="ARTIST_NEGATIVE_SENTINEL")
                if mode == "separate":
                    self.assertIn("ARTIST_NEGATIVE_SENTINEL", result.negative)
                elif mode == "inline":
                    self.assertIn("ARTIST_NEGATIVE_SENTINEL", result.positive)
                else:
                    self.assertNotIn("ARTIST_NEGATIVE_SENTINEL", result.positive + result.negative)
                    self.assertTrue(result.warnings)


class HistoryTimeFilterTests(unittest.TestCase):
    def test_explicit_invalid_or_reversed_ranges_fail_closed(self):
        from atri_x.errors import InvalidError
        from atri_x.tool.builtin.chat_history import chat_history_time_range
        for since, until in (("bad-date", ""), ("", "999999999999999999999999"),
                             ("2026-02-30", ""), ("2026-02-02", "2026-01-01")):
            with self.subTest(since=since, until=until), self.assertRaises(InvalidError):
                chat_history_time_range(since, until, 0, 0)
        self.assertEqual(chat_history_time_range("", "", 0, 0), (None, None))


class SnapshotFacadeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_facade_accepts_installed_synchronous_snapshot_method(self):
        from unittest.mock import Mock
        from atri_x.agent.model_service import ModelService
        result = object()
        host = SimpleNamespace(apply_model_snapshot=Mock(return_value=result))
        facade = SimpleNamespace(agent=host)
        self.assertIs(await ModelService.apply_model_snapshot(facade, None, "synthetic"), result)


class SnapshotDeleteRollbackTests(unittest.TestCase):
    def test_persistence_failure_restores_deleted_snapshot(self):
        from unittest.mock import Mock
        from atri_x.agent.model_snapshot import ModelSnapshotMixin, ensure_model_snapshots
        from atri_x.config import StateModelSnapshot, ModelSelection
        host = SimpleNamespace(merge_external_runtime_state=Mock(), save_runtime_state=Mock(side_effect=OSError("synthetic")))
        store = ensure_model_snapshots(host)
        store.put("safe", StateModelSnapshot(mode_models={"work": ModelSelection("fake", "old")}))
        with self.assertRaises(OSError):
            ModelSnapshotMixin.delete_model_snapshot(host, "safe")
        self.assertIsNotNone(store.get("safe"))


class SnapshotApplyRollbackTests(unittest.TestCase):
    def test_save_failure_restores_all_model_selections(self):
        import threading
        from unittest.mock import Mock
        from atri_x.agent.model_snapshot import ModelSnapshotMixin, ensure_model_snapshots
        from atri_x.config import StateModelSnapshot, ModelSelection
        old = ModelSelection("fake", "old")
        state = {"modes": {"work": old}, "compact": old, "naming": old}
        runtime = SimpleNamespace(providers={"fake": object()}, selection_mu=threading.Lock(),
            provider_name="fake", provider="old-provider", model="old", llm_client="old-client")
        runtime.set_mode_models = lambda value: state.update(modes=value)
        host = SimpleNamespace(model_runtime=runtime, merge_external_runtime_state=Mock(),
            check_model_switch=Mock(), mode_models_snapshot=lambda: dict(state["modes"]),
            current_compact_selection=lambda: state["compact"], configured_naming_model=lambda: state["naming"],
            set_compact_selection=lambda value: state.update(compact=value),
            set_naming_model=lambda value: state.update(naming=value),
            client_for_provider=lambda name: "new-client", save_runtime_state=Mock(side_effect=OSError("synthetic")))
        def apply(mode, provider, model):
            state["modes"][mode] = ModelSelection(provider, model)
            runtime.model = model
        host.apply_mode_model_selection = apply
        ensure_model_snapshots(host).put("new", StateModelSnapshot(
            mode_models={"work": ModelSelection("fake", "new")},
            compact_model=ModelSelection("fake", "new"), naming_model=ModelSelection("fake", "new")))
        with self.assertRaises(OSError):
            ModelSnapshotMixin.apply_model_snapshot(host, None, "new")
        self.assertEqual(state["modes"]["work"].model, "old")
        self.assertEqual(state["compact"].model, "old")
        self.assertEqual(state["naming"].model, "old")
        self.assertEqual(runtime.model, "old")


class CharacterPolicyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_missing_id_obeys_creation_policy(self):
        from unittest.mock import Mock
        from atri_x.tool.builtin.character import CharacterManageTool
        from atri_x.tool.tool import CallRequest
        store = SimpleNamespace(write=Mock(return_value=SimpleNamespace(name="fake", visibility="private", docs={}, images=[], id="new")))
        tool = SimpleNamespace(deps=SimpleNamespace(enabled=lambda: True, allow_user_create=False, store=store),
                               name=lambda: "character_manage")
        with patch("atri_x.tool.builtin.character.character_viewer", return_value=SimpleNamespace(superadmin=False)), patch(
                "atri_x.tool.builtin.character._character_exists", return_value=False):
            result = await CharacterManageTool.call(tool, None, CallRequest(arguments='{"operation":"update","id":"new"}'))
        store.write.assert_not_called()
        self.assertIn("allow_user_create=false", result.content)

    async def test_add_image_task_cancellation_propagates(self):
        from unittest.mock import AsyncMock
        from atri_x.tool.builtin.character import CharacterManageTool
        from atri_x.tool.tool import CallRequest
        tool = SimpleNamespace(deps=SimpleNamespace(enabled=lambda: True), name=lambda: "character_manage",
            add_image=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await CharacterManageTool.call(tool, None, CallRequest(arguments='{"operation":"add_image","id":"fake"}'))


class WorkspaceResetTypeTests(unittest.TestCase):
    def test_reset_requires_boolean_not_truthy_string(self):
        from atri_x.tool.builtin.workspace import parse_workspace_args
        from atri_x.errors import InvalidError
        for value in ('"false"', '"true"', '1', '[]', '{}'):
            with self.subTest(value=value), self.assertRaises(InvalidError):
                parse_workspace_args('{"reset":' + value + '}')
        self.assertFalse(parse_workspace_args('{"reset":false}').reset)


class GoAstByteColumnTests(unittest.TestCase):
    def test_unicode_prefix_reports_utf8_byte_column(self):
        from atri_x.tool.builtin.file_tools_ast import find_go_ast_matches
        source = 'package main\nvar 名 = target\n'
        matches = find_go_ast_matches("synthetic.go", source, "target")
        self.assertEqual(matches[0].column, len('var 名 = '.encode('utf-8')) + 1)


class GroupPolicyResetAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_admin_cannot_use_all_to_reset_protected_fields(self):
        from unittest.mock import Mock
        from atri_x.agent import group_policy
        from atri_x.errors import InvalidError
        for field in ("", "all", "ALL"):
            with self.subTest(field=field), patch.object(group_policy, "can_manage_current_group_policy", return_value=True), patch.object(
                    group_policy, "_scope", return_value=SimpleNamespace()), patch.object(
                    group_policy, "group_policy_snapshot", return_value={}), patch.object(
                    group_policy, "_actor", return_value=SimpleNamespace(id="group-admin", role="user")), patch.object(
                    group_policy, "set_group_policy_for_scope") as mutate:
                with self.assertRaises(InvalidError):
                    await group_policy.reset_group_policy(None, None, field)
                mutate.assert_not_called()


class SearchCliRiskTests(unittest.TestCase):
    def test_cli_fallback_makes_search_high_risk_without_launching(self):
        from atri_x.tool.builtin.web_search import WebSearchTool
        from atri_x.tool.builtin.web_search_engines import SearchEngineSpec
        from atri_x.tool.tool import RiskHigh, RiskLow
        for protocol in ("antigravity", "grok"):
            tool = WebSearchTool(specs=[SearchEngineSpec(name="api", protocol="tavily"),
                                       SearchEngineSpec(name="backup", protocol=protocol)])
            self.assertEqual(tool.info().risk, RiskHigh)
        self.assertEqual(WebSearchTool().info().risk, RiskLow)


class JinaTargetSchemeTests(unittest.TestCase):
    def test_preserves_https_and_rejects_nonweb_or_credentials(self):
        from atri_x.errors import InvalidError
        from atri_x.tool.builtin.web_extract_engines import jina_reader_url
        self.assertEqual(jina_reader_url("https://reader", "https://example.com/a?q=1#x"),
                         "https://reader/https://example.com/a?q=1")
        self.assertEqual(jina_reader_url("https://reader", "http://example.com/"),
                         "https://reader/http://example.com/")
        for target in ("ftp://example.com/a", "file://host/a", "https://user:secret@example.com/", "https://example.com:bad/"):
            with self.subTest(target=target), self.assertRaises(InvalidError):
                jina_reader_url("https://reader", target)


class GetMediaCancellationTests(unittest.TestCase):
    def test_cancelled_download_propagates_instead_of_error_text(self):
        from unittest.mock import AsyncMock
        from tests.test_builtin_tools3 import FakeRow, FakeHistory, FakeCenter, _chat_ctx, _req
        from atri_x.llm.llm import SegmentImage
        from atri_x.tool.builtin.get_media import new_get_media_tool
        row = FakeRow([type("S", (), {"type": SegmentImage})()])
        center = FakeCenter()
        center.get_history_media = AsyncMock(side_effect=asyncio.CancelledError())
        tool = new_get_media_tool(FakeHistory({"1": row}), center)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(tool.call(_chat_ctx(), _req("get_media", {"message_id": ["1"]})))


class QuotaFiniteValueTests(unittest.TestCase):
    def test_nonfinite_cost_quota_rejected_without_mutation(self):
        from atri_x.agent.group_policy_rules import apply_policy_field
        from atri_x.config import GroupPolicyConfig
        from atri_x.errors import InvalidError
        for text in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(text=text):
                policy = GroupPolicyConfig(chat_cost_quota=2.5)
                with self.assertRaises(InvalidError):
                    apply_policy_field(None, policy, "chat-cost-quota", text)
                self.assertEqual(policy.chat_cost_quota, 2.5)


class SkillFinalizeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_cancellation_is_not_wrapped_as_tool_error(self):
        from unittest.mock import AsyncMock
        from atri_x.tool.skill.finalize_atrix_skill import FinalizeAtrixSkillTool
        tool = SimpleNamespace(manager=SimpleNamespace(reload=AsyncMock(side_effect=asyncio.CancelledError())))
        with self.assertRaises(asyncio.CancelledError):
            await FinalizeAtrixSkillTool._reload(tool, None)


class StateWatcherLifecycleTests(unittest.TestCase):
    def test_outside_loop_does_not_poison_later_start(self):
        agent = SimpleNamespace(state_path="synthetic")
        state_watch.start_runtime_state_watch(agent)
        self.assertFalse(getattr(agent, "state_watch_started", False))

    def test_completed_task_can_restart_and_duplicate_start_is_single(self):
        async def scenario():
            agent = SimpleNamespace(state_path="synthetic")
            entered = asyncio.Event()
            async def watch(*args):
                entered.set()
                await asyncio.Event().wait()
            with patch.object(state_watch, "_watch_loop", side_effect=watch):
                state_watch.start_runtime_state_watch(agent)
                first = agent.state_watch_task
                state_watch.start_runtime_state_watch(agent)
                self.assertIs(agent.state_watch_task, first)
                await entered.wait()
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                state_watch.start_runtime_state_watch(agent)
                second = agent.state_watch_task
                try:
                    self.assertIsNot(second, first)
                    self.assertFalse(second.done())
                finally:
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
        asyncio.run(scenario())
