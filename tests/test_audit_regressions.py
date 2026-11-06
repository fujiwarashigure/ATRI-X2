"""全仓审查复现：检查点时间、记忆唯一键与文档扫描边界。"""
from __future__ import annotations

import unittest
from atri_x.storage.sqlite import new
from atri_x.storage.models import Session, Message, ContextSummary
from atri_x.storage.times import parse_time
from atri_x.errors import NotFoundError
from atri_x.angelmemory import open_store, Memory

class WorkspaceCancellationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_cancel_stays_cancel(self):
        import asyncio
        from unittest.mock import patch, AsyncMock, Mock
        from atri_x.tool import workspace
        store = Mock(get_workspace_dir=AsyncMock(side_effect=asyncio.CancelledError()))
        with patch.object(workspace, "_sandbox_service", return_value=None), patch.object(workspace, "workspace_store_from_context", return_value=(store, True)):
            with self.assertRaises(asyncio.CancelledError):
                await workspace.current_workspace_dir(None)

class FileGuardAuditTests(unittest.TestCase):
    def test_dotdot_prefix_name_still_inside_protected_root(self):
        import os
        from atri_x.tool.builtin.file_guard import FileGuardRule
        root = os.path.abspath("synthetic-guard")
        rule = FileGuardRule(root=root)
        self.assertTrue(rule.matches(os.path.join(root, "..memo.md")))
        self.assertFalse(rule.matches(os.path.join(root, "..", "outside.md")))

class CronCancellationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_wall_time_invalidates_old_frozen_utc(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock, patch
        from atri_x.cron.service import Service
        from atri_x.cron.model import Metadata, CronSchedule, PatchRequest, METADATA_KIND
        meta = Metadata(kind=METADATA_KIND, version=1, schedule=CronSchedule(mode="once", run_at="2026-01-01 03:00:00",
                        run_at_utc="2026-01-01T03:00:00Z"))
        captured = []
        async def upsert(ctx, name, updated, enabled, reset):
            captured.append(updated)
            return SimpleNamespace(name=name)
        service = SimpleNamespace(cron_jobs=lambda: SimpleNamespace(get_by_name=lambda n:
            SimpleNamespace(metadata=meta.encode(), enabled=False)), audit_event=Mock(), upsert=upsert)
        with patch("atri_x.cron.service.require_superadmin"), patch("atri_x.cron.service.validate_atriyph_cron_task"):
            await Service.update(service, None, PatchRequest(name="audit", run_at="2026-01-02 03:00:00"))
        self.assertEqual(captured[0].schedule.run_at_utc, "")

    async def test_cancelled_job_releases_running_marker(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock
        from atri_x.cron.manager import Manager
        job = SimpleNamespace(name="audit", enabled=True, handler="fake")
        manager = Manager(repo=SimpleNamespace(get_by_name=lambda name: job))
        entered = asyncio.Event()
        async def handler(*args):
            entered.set()
            await asyncio.Event().wait()
        manager.handlers["fake"] = handler
        task = asyncio.create_task(manager.run_job("audit"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("audit", manager.running)

class ArtistLoadAuditTests(unittest.TestCase):
    def test_corrupt_library_never_becomes_silent_empty_library(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from atri_x.artist.store import Store
        from atri_x.errors import InvalidError
        store = Store("synthetic-library.json")
        with patch("atri_x.artist.store.fileops.read_file", return_value=SimpleNamespace(text="{broken")):
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(InvalidError):
                    store.library()

class VisionRetryPermitAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_error_releases_provider_before_recursive_retry(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, AsyncMock, patch
        from atri_x.agent import chat_llm
        held = [False]
        def release():
            held[0] = False
        async def acquire(*a):
            held[0] = True
            return release
        async def chunks():
            yield SimpleNamespace(error=ValueError("image unsupported"))
        async def retry(*a):
            self.assertFalse(held[0], "recursive retry still owns old provider permit")
            return "retried"
        agent = SimpleNamespace(begin_chat_budget=Mock(return_value=None), turn_output_allowed=lambda c: True)
        with patch.object(chat_llm, "_acquire_provider_slot", side_effect=acquire), \
                patch.object(chat_llm, "_open_stream", new=AsyncMock(return_value=chunks())), \
                patch.object(chat_llm, "should_show_cli_reasoning", return_value=False), \
                patch.object(chat_llm, "should_fallback_vision", return_value=True), \
                patch.object(chat_llm, "_can_retry_without_vision", return_value=True), \
                patch.object(chat_llm, "_notify_vision_fallback_once", new=AsyncMock()), \
                patch.object(chat_llm, "_settle"), patch.object(chat_llm, "_retry_for_vision", side_effect=retry):
            result = await chat_llm._stream_chat(agent, None, "s", False,
                SimpleNamespace(provider="fake", model="fake"), [], [], [], chat_llm.LLMRequestOptions(), None, None, 0)
        self.assertEqual(result, "retried")

class LLMHookCancellationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_hook_cancel_stays_cancellation(self):
        import asyncio
        from dataclasses import dataclass
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.agent.chat_llm import call_llm
        @dataclass
        class Selection:
            provider: str = "fake"
            model: str = "fake"
        agent = SimpleNamespace(run_hook=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await call_llm(agent, None, "s", False, Selection(), [], [], None, None, None, None)

class MissingModerationProviderAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_without_credentials_uses_fail_closed(self):
        from types import SimpleNamespace
        from atri_x.safety.engine_remote import RemoteModerationMixin, _Tally
        class FakeEngine(RemoteModerationMixin):
            scope_for = lambda self, *a: None
            stage_policy = lambda self, *a: "sync"
            fail_mode = lambda self, *a: "closed"
            _cache_get = lambda self, *a: None
            images_mode = lambda self, *a: "all"
        engine = FakeEngine()
        engine.settings = SimpleNamespace(remote=SimpleNamespace(enabled=True), media=SimpleNamespace(skip_actors=[]))
        engine._providers = {}
        engine._tally = _Tally()
        for outcome in (await engine.moderate_text("synthetic", stage="send"),
                        await engine.moderate_image(b"synthetic")):
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome.tier, "block")
            self.assertTrue(outcome.degraded)

class ServiceHeaderAuditTests(unittest.TestCase):
    def test_commented_server_header_not_duplicated(self):
        import tomllib
        from atri_x.gui.service import ensure_server_enabled
        original = "[platform.cli.server] # local server\nenabled=false\n[other] # boundary\nenabled=false\n"
        updated, changed = ensure_server_enabled(original)
        parsed = tomllib.loads(updated)
        self.assertTrue(changed)
        self.assertTrue(parsed["platform"]["cli"]["server"]["enabled"])
        self.assertFalse(parsed["other"]["enabled"])

class ConfigArrayBoundaryAuditTests(unittest.TestCase):
    def test_parent_and_top_level_patch_stop_before_array_table(self):
        import tomllib
        from unittest.mock import patch
        from atri_x.gui import configedit
        for original, section in (("[parent]\nold=1\n[[items]]\nname='one'\n", "parent"),
                                  ("old=1\n[[items]]\nname='one'\n", None)):
            captured = []
            with patch.object(configedit, "_read_text", return_value=original), \
                    patch.object(configedit, "_commit", side_effect=lambda p, old, new: captured.append(new) or True):
                if section:
                    configedit.patch_section("fake", section, {"added": 2})
                else:
                    configedit.patch_top_level("fake", {"added": 2})
            data = tomllib.loads(captured[0])
            self.assertEqual((data[section] if section else data)["added"], 2)
            self.assertNotIn("added", data["items"][0])

class RemoteReviewAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_review_preserves_local_redaction(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from atri_x.safety import guard_async
        from atri_x.safety.guard import SafetyDecision, ACTION_REDACT
        decision = SafetyDecision(text="[masked]", action=ACTION_REDACT, tier="pass")
        outcome = SimpleNamespace(tier="review", labels=[], reason="review", provider="fake",
                                  request_id="r", score=None, degraded=False, fail_mode="closed", latency_ms=0)
        engine = SimpleNamespace(moderate_text=AsyncMock(return_value=outcome),
                                 scope_for=Mock(return_value=None), resolve_tier=lambda *a, **k: "review",
                                 should_hold=lambda *a, **k: False)
        guard = SimpleNamespace(rules=SimpleNamespace(mask=lambda text: "[masked]"))
        with patch.object(guard_async, "_enqueue_review"):
            result = await guard_async._apply_remote_text(guard, decision, "sensitive original",
                policy=None, engine=engine, stage="send", platform="cli", scope_id="audit", tool="", block_text="")
        self.assertEqual(result.text, "[masked]")
        self.assertEqual(result.action, ACTION_REDACT)
        self.assertEqual(result.tier, "review")

class MediaCredentialsAuditTests(unittest.TestCase):
    def test_configured_session_token_reaches_signer_credentials(self):
        from atri_x.media.config import FileDeliveryConfig
        from atri_x.media.s3 import resolve_credentials
        cfg = FileDeliveryConfig(s3_access_key="a", s3_secret_key="s", s3_session_token="temporary")
        self.assertEqual(resolve_credentials(cfg), ("a", "s", "temporary"))
        self.assertEqual(resolve_credentials(cfg, {"access_key": "b", "secret_key": "t", "session_token": "provider"}),
                         ("b", "t", "provider"))

class MediaLockAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_outer_cancel_after_internal_acquire_releases_lock(self):
        import asyncio
        from unittest.mock import patch
        from atri_x.media import locks
        from atri_x.safecontext import CancelToken
        lock = asyncio.Lock()
        async def cancel_after_acquired(tasks, **kwargs):
            while not lock.locked():
                await asyncio.sleep(0)
            raise asyncio.CancelledError()
        with patch.object(locks.asyncio, "wait", side_effect=cancel_after_acquired):
            with self.assertRaises(asyncio.CancelledError):
                await locks._acquire(lock, CancelToken())
        self.assertFalse(lock.locked())

class RecoverySafetyAuditTests(unittest.TestCase):
    def test_recovery_never_replaces_filtered_text_with_raw(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from atri_x.agent import chat_anomaly
        result = SimpleNamespace(raw_text="untrusted tool protocol", text="blocked by policy", tool_calls=[])
        with patch.object(chat_anomaly, "looks_like_text_tool_call", side_effect=lambda s: s.startswith("untrusted")), \
                patch.object(chat_anomaly, "recover_text_tool_calls", return_value=([], "unsafe raw answer")), \
                patch.object(chat_anomaly, "_record"):
            chat_anomaly.apply_text_tool_recovery(None, "s", result, tools_enabled=True)
        self.assertEqual(result.text, "blocked by policy")
        self.assertEqual(result.tool_calls, [])

class GroupRateLimitAuditTests(unittest.TestCase):
    def test_explicit_group_kind_honored_without_scope_prefix(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from atri_x.agent import ratelimit
        host = SimpleNamespace(scope=lambda ctx: SimpleNamespace(platform_scope_id="custom-room"))
        with patch.object(ratelimit, "message_context_from", return_value=(
                SimpleNamespace(conversation_kind=ratelimit.ConversationGroup), True)):
            self.assertTrue(ratelimit.is_group_context(host, None))

class QuietAdmissionAuditTests(unittest.TestCase):
    def test_aware_time_converts_and_direct_false_skips(self):
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        from unittest.mock import patch
        from atri_x.agent import admission
        zone = timezone(timedelta(hours=8))
        with patch.object(admission, "quiet_timezone", return_value=zone):
            self.assertEqual(admission.quiet_now(None, datetime(2026, 1, 1, tzinfo=timezone.utc)).hour, 8)
        with patch.object(admission, "quiet_window_active", return_value=True), \
                patch.object(admission, "quiet_config", return_value=SimpleNamespace(allow_direct=False)), \
                patch.object(admission, "_actor_is_superadmin", return_value=False), \
                patch.object(admission, "quiet_window", return_value="00:00-23:59"):
            self.assertEqual(admission.admit(None, None, admission.DIRECT_MESSAGE).decision, admission.ADMIT_SKIP)

class InboxAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_waiter_cancel_does_not_cancel_shared_batch(self):
        import asyncio
        from types import SimpleNamespace
        from atri_x.agent.inbox import wait_inbox_batch
        shared = asyncio.get_running_loop().create_future()
        batch = SimpleNamespace(done=shared)
        first = asyncio.create_task(wait_inbox_batch(None, batch, 1))
        second = asyncio.create_task(wait_inbox_batch(None, batch, 2))
        await asyncio.sleep(0)
        first.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(shared.cancelled())
            shared.set_result(None)
            await second
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)

class ShellTimeoutAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_includes_wait_after_pipe_eof(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from atri_x.tool.builtin import shell
        from atri_x.errors import InvalidError
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
                                  stderr=SimpleNamespace(read=AsyncMock(return_value=b"")))
        async def never_exit():
            await asyncio.Event().wait()
        process.wait = never_exit
        environment = SimpleNamespace(command=lambda *a: SimpleNamespace(env_dict=lambda: {}))
        with patch.object(shell.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)), \
                patch.object(shell, "configure_shell_process"), \
                patch.object(shell, "kill_shell_process_tree") as kill:
            with self.assertRaises(InvalidError):
                await asyncio.wait_for(shell.run_shell_command(environment, "synthetic", ".", .01), .3)
            kill.assert_called_once_with(process)

class GitRiskAuditTests(unittest.TestCase):
    def test_mutating_and_unknown_git_commands_not_low(self):
        from atri_x.tool.builtin.shell_risk import ShellRiskClassifier
        for args in (["branch", "-D", "victim"], ["tag", "-d", "v1"], ["init"],
                     ["config", "demo.key", "value"], ["custom-alias"], ["diff", "--ext-diff"],
                     ["-c", "core.pager=evil", "log"]):
            with self.subTest(args=args):
                risk = ShellRiskClassifier()
                risk.classify_git(args)
                self.assertEqual(risk.level, "high")

class ImagePolicyAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_autobuild_never_removes_old_image(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.sandbox.backend_container import ContainerBackend
        from atri_x.sandbox.errors import SandboxUnavailableError
        backend = ContainerBackend(SimpleNamespace(image="audit-image", image_auto_build=False),
                                   cli=SimpleNamespace(kind="docker"))
        backend._run = AsyncMock(return_value=SimpleNamespace(ok=True, stdout="old-digest"))
        with self.assertRaises(SandboxUnavailableError):
            await backend.ensure_image()
        self.assertEqual(backend._run.await_count, 1)
        self.assertEqual(backend._run.call_args.args[0][:2], ["image", "inspect"])

class S3AuditTests(unittest.TestCase):
    def test_signature_contains_exactly_one_host_header(self):
        from atri_x.net.s3 import sign_request
        headers = sign_request(method="GET", url="https://example.invalid/bucket/key",
                               region="test", access_key="synthetic", secret_key="synthetic")
        self.assertEqual([key for key in headers if key.lower() == "host"], ["Host"])

class OneBotWorkerAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_workers_survive_single_bad_event(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from atri_x.platform.qq_onebot.inbound import EventDispatcher, OutboundEventJob
        for priority in (False, True):
            with self.subTest(priority=priority):
                received = asyncio.Event()
                calls = 0
                async def prepare(*args):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise ValueError("malformed event")
                    received.set()
                    return None, None, "", False
                adapter = SimpleNamespace(inbound=None, prepare_inbound=prepare,
                                          handle_platform_event=prepare)
                dispatcher = EventDispatcher(adapter, None, SimpleNamespace(handle_message=AsyncMock()),
                                             workers=1, high_workers=1)
                try:
                    enqueue = dispatcher.enqueue_high if priority else dispatcher.enqueue_normal
                    enqueue(OutboundEventJob(message_key="bad"))
                    enqueue(OutboundEventJob(message_key="next"))
                    await asyncio.wait_for(received.wait(), .5)
                    self.assertTrue(all(not task.done() for task in dispatcher._workers))
                finally:
                    await dispatcher.stop()

class LogReaderAuditTests(unittest.TestCase):
    def test_line_limit_independent_of_block_size(self):
        import io
        from unittest.mock import patch
        from atri_x.logging.reader import iter_lines_reversed
        for size in (4, 64):
            with self.subTest(block_size=size), patch("builtins.open", return_value=io.BytesIO(b"a\n123456789012\n")):
                with self.assertRaises(ValueError):
                    list(iter_lines_reversed("fake", block_size=size, max_line_bytes=8))

class LimiterAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_and_after_grant_does_not_leak(self):
        import asyncio
        from atri_x.ops.concurrency import Config, Limiter
        for grant_first in (False, True):
            with self.subTest(grant_first=grant_first):
                limiter = Limiter(Config(max=1, queue_size=2))
                release = await limiter.acquire()
                task = asyncio.create_task(limiter.acquire())
                await asyncio.sleep(0)
                if grant_first:
                    release()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                if not grant_first:
                    release()
                self.assertEqual(limiter.stats(), (0, 0))

class TelegramAuditTests(unittest.TestCase):
    def test_incoming_message_without_reference(self):
        from atri_x.platform.telegram.message import final_message_segments
        parts = final_message_segments("hello", [], None)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].text, "hello")

class RequestCancellationAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_task_cancel_before_and_after_grant(self):
        import asyncio
        from atri_x.request import Manager, QueueConfig, StartRequest, KindLLM
        for grant_first in (False, True):
            with self.subTest(grant_first=grant_first):
                manager = Manager(60, {KindLLM: 1}, QueueConfig(max_queue=5, wait_timeout=2, wait_kinds=[KindLLM]))
                _, _, done = await manager.start(None, StartRequest(kind=KindLLM))
                task = asyncio.create_task(manager.start(None, StartRequest(kind=KindLLM)))
                await asyncio.sleep(0)
                if grant_first:
                    done()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                if not grant_first:
                    done()
                self.assertEqual(len(manager._waiters.get(KindLLM, [])), 0)
                self.assertEqual(manager._pending.get(KindLLM, 0), 0)
                _, _, release = await asyncio.wait_for(manager.start(None, StartRequest(kind=KindLLM)), .5)
                release()

class TenantLedgerAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_destroy_does_not_prune_other_tenant_ledgers(self):
        from unittest.mock import Mock, AsyncMock, patch
        from atri_x.sandbox import lifecycle
        from atri_x.sandbox.tenant import LOCAL_TENANT
        service = Mock()
        with patch.object(lifecycle, "reset_tenant", new=AsyncMock(return_value={"ledger_kept": True})):
            result = await lifecycle.destroy_tenant(service, LOCAL_TENANT)
        self.assertTrue(result["ledger_kept"])
        service.ledger.prune.assert_not_called()

class AdmissionAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_consume_slot(self):
        import asyncio
        from atri_x.sandbox.queue import AdmissionController
        queue = AdmissionController(capacity=1)
        lease = await queue.acquire("first")
        task = asyncio.create_task(queue.acquire("second"))
        await asyncio.sleep(0)
        queue._waiters[0].future.cancel()
        lease.release()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(queue.snapshot().running, 0)
        self.assertEqual(queue.snapshot().per_tenant, {})

    async def test_cancel_after_grant_returns_lease(self):
        import asyncio
        from atri_x.sandbox.queue import AdmissionController
        queue = AdmissionController(capacity=1)
        lease = await queue.acquire("first")
        task = asyncio.create_task(queue.acquire("second"))
        await asyncio.sleep(0)
        lease.release()
        self.assertEqual(queue.snapshot().per_tenant.get("second"), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(queue.snapshot().running, 0)
        self.assertEqual(queue.snapshot().per_tenant, {})

    async def test_panic_preserves_running_tenant_accounting(self):
        import asyncio
        from atri_x.sandbox.queue import AdmissionController
        from atri_x.sandbox.errors import SandboxDeniedError
        queue = AdmissionController(capacity=1)
        lease = await queue.acquire("first")
        task = asyncio.create_task(queue.acquire("second"))
        await asyncio.sleep(0)
        queue.cancel_all()
        with self.assertRaises(SandboxDeniedError):
            await task
        self.assertEqual(queue.snapshot().per_tenant, {"first": 1})
        lease.release()

class CheckpointAuditTests(unittest.TestCase):
    def setUp(self):
        self.store = new(":memory:")
        self.session = Session(owner_id="u", platform="cli", platform_scope_id="audit")
        self.store.sessions().create(self.session)

    def tearDown(self):
        self.store.close()

    def message(self, ident, stamp):
        item = Message(id=ident, session_id=self.session.id, role="user", content=ident,
                       created_at=parse_time(stamp))
        self.store.messages().append(item)
        return item

    def test_variable_fraction_checkpoint_ranges(self):
        a = self.message("a", "2026-01-01T00:00:00.1Z")
        b = self.message("b", "2026-01-01T00:00:00.11Z")
        c = self.message("c", "2026-01-01T00:00:00.12Z")
        repo = self.store.messages()
        self.assertEqual([m.id for m in repo.list_by_session_up_to(self.session.id, a.id)], [a.id])
        self.assertEqual([m.id for m in repo.list_by_session_after(self.session.id, a.id)], [b.id, c.id])
        self.assertEqual([m.id for m in repo.list_by_session_after_up_to(self.session.id, a.id, b.id)], [b.id])

    def test_summary_does_not_cross_equal_time_checkpoint(self):
        a = self.message("z", "2026-01-01T00:00:00.1Z")
        b = self.message("y", "2026-01-01T00:00:00.1Z")
        self.store.context_summaries().create(ContextSummary(
            session_id=self.session.id, to_message_id=b.id, summary="future", provider="p", model="m"))
        with self.assertRaises(NotFoundError):
            self.store.context_summaries().latest_by_session_up_to(self.session.id, a.id)

class UninstallAuditTests(unittest.TestCase):
    def test_frozen_copy_retains_nested_runtime(self):
        import importlib
        from unittest.mock import patch
        module = importlib.import_module("atri_x.dist.install")
        with patch.object(module, "is_frozen", return_value=True), \
                patch.object(module.os, "makedirs"), patch.object(module.shutil, "copytree") as copy:
            module._copy_tree("H:/fake-source", "H:/fake-target")
            ignore = copy.call_args.kwargs["ignore"]
            self.assertEqual(ignore("H:/fake-source/_internal", ["runtime", "dist", "__pycache__"]), {"__pycache__"})


    def test_nested_data_without_purge_refuses_before_side_effects(self):
        import importlib
        from unittest.mock import patch, Mock
        module = importlib.import_module("atri_x.dist.install")
        marker = {"target": "H:/audit-fake/app", "dataDir": "H:/audit-fake/app/data",
                  "shortcuts": ["H:/audit-fake/shortcut.lnk"]}
        registry, links = Mock(), Mock()
        with patch.object(module, "_read_marker", return_value=marker), \
                patch.object(module, "_disable_autostart") as autostart, \
                patch.object(module, "_remove_install_dir") as remove, \
                patch.object(module.os.path, "isdir", return_value=True):
            report = module.uninstall(yes=True, registry=registry, shortcut_remover=links)
        self.assertFalse(report.ok)
        self.assertTrue(report.kept_data)
        remove.assert_not_called()
        registry.delete_uninstall.assert_not_called()
        links.assert_not_called()
        autostart.assert_not_called()

class DocumentationAuditTests(unittest.TestCase):
    def test_only_root_generated_directories_are_excluded(self):
        import importlib.util
        from pathlib import Path
        from unittest.mock import patch
        script = Path(__file__).parents[1] / "scripts" / "check_docs.py"
        spec = importlib.util.spec_from_file_location("audit_check_docs", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = Path("audit-fixture")
        candidates = [root / path for path in (
            "build/old.md", "dist/old.md", "runtime/vendor.md", "docs/real.md",
            "atri_x/runtime/README.md", "atri_x/dist/README.md")]
        with patch.object(Path, "rglob", return_value=candidates), patch.object(module, "_is_generated", return_value=False):
            result = module.iter_markdown(root)
        self.assertEqual({p.relative_to(root).as_posix() for p in result},
                         {"docs/real.md", "atri_x/runtime/README.md", "atri_x/dist/README.md"})

class EgressAuditTests(unittest.TestCase):
    def test_restricted_empty_hosts_fail_closed(self):
        from atri_x.config.sandbox import SandboxConfig
        from atri_x.sandbox.settings import from_config
        from atri_x.sandbox.errors import SandboxDeniedError
        with self.assertRaises(SandboxDeniedError):
            from_config(SandboxConfig(root="H:/audit-fake/sandbox", network="restricted", restricted_hosts=[]))


    def test_shared_address_space_is_not_public(self):
        from atri_x.sandbox.egress import _is_public_ip
        for ip in ("100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1"):
            with self.subTest(ip=ip):
                self.assertFalse(_is_public_ip(ip)[0])
        self.assertTrue(_is_public_ip("8.8.8.8")[0])

class TransactionAuditTests(unittest.TestCase):
    def test_commit_failure_rolls_back(self):
        import sqlite3
        from atri_x.angelmemory.store import _transaction
        from atri_x.selflearning.db import transaction
        for helper in (_transaction, transaction):
            with self.subTest(helper=helper.__module__):
                db = sqlite3.connect(":memory:", isolation_level=None)
                try:
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
                    db.execute("CREATE TABLE child(pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
                    with self.assertRaises(sqlite3.IntegrityError):
                        with helper(db):
                            db.execute("INSERT INTO child VALUES(1)")
                    self.assertFalse(db.in_transaction)
                    self.assertEqual(db.execute("SELECT count(*) FROM child").fetchone()[0], 0)
                finally:
                    db.close()

class MemoryAuditTests(unittest.TestCase):
    def test_upsert_returns_exact_persisted_identity(self):
        store = open_store(":memory:")
        try:
            store.remember(Memory(platform="cli", scope_id="audit", content="alpha extended", strength=100))
            original = store.remember(Memory(platform="cli", scope_id="audit", content="alpha", strength=50))
            repeated = store.remember(Memory(platform="cli", scope_id="audit", content="alpha", strength=50))
            self.assertEqual(repeated.id, original.id)
            self.assertEqual(store.get("cli", "audit", repeated.id).content, "alpha")
        finally:
            store.close()
