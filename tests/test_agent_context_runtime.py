"""``atri_x.agent.context_runtime`` / ``context_compact`` / ``context_seed`` 的测试。

对应 Go：``internal/agent/context_runtime_test.go`` / ``context_compact_test.go``。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from atri_x.agent.context_compact import (
    ContextCompactMixin,
    ContextCompactState,
    build_compact_messages,
    compact_user_inputs,
    decode_metadata,
    encode_metadata_fields,
    format_compacted_title,
    next_compacted_title,
    request_scope_key,
)
from atri_x.agent.context_runtime import (
    ContextRuntimeState,
    compact_selection_for_session,
    ensure_context_runtime,
    install_context_services,
    new_context_runtime_state,
)
from atri_x.agent.context_seed import pending_context_compact
from atri_x.agent.model import new_model_runtime_state
from atri_x.agent.model_agent import ModelMixin
from atri_x.config import (
    ContextConfig,
    ModelMetadataConfig,
    ModelSelection,
    ProviderConfig,
)
from atri_x.contextmgr import (
    CompactMessage,
    CompactResult,
    CompactionChoice,
    LoadedContext,
    PromptBudget,
    SummarizeRequest,
    SummarizeResult,
)
from atri_x.llm.llm import Usage
from atri_x.request.manager import KindCompress
from atri_x.storage.models import (
    ContextSummary,
    Message,
    RoleAssistant,
    RoleUser,
    Session,
)
from atri_x.turn.manager import PhaseCompact


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class TestRuntimeState(unittest.IsolatedAsyncioTestCase):
    def _state(self, window: int = 1000, **cfg) -> ContextRuntimeState:
        state = new_context_runtime_state()
        state.configure(
            ContextConfig(**cfg),
            ModelMetadataConfig(default_context_window=window),
            {"p": ProviderConfig(models=["m"])},
            ModelSelection(),
            None,
        )
        return state

    async def test_prompt_budget_sync_and_async_agree(self) -> None:
        state = self._state(1000)
        selection = ModelSelection("p", "m")
        budget = await state.prompt_budget(None, selection)
        self.assertEqual(budget.window, 1000)
        self.assertEqual(budget.input_limit, 800)
        self.assertEqual(budget.single_message_limit, 500)
        self.assertEqual(state.prompt_budget_sync(selection).input_limit, 800)

    async def test_window_fallback_chain(self) -> None:
        state = self._state(0, )
        state.model_metadata = ModelMetadataConfig(default_context_window=0)
        self.assertEqual(await state.window_for(None, ModelSelection("p", "m")), 256000)
        state = self._state(300)
        self.assertEqual(state.window_for_sync(ModelSelection("p", "m")), 300)

    async def test_compactor_for_without_backend(self) -> None:
        state = self._state()
        state.compactor = None
        self.assertEqual(state.compactor_for("p", "m"), (None, CompactionChoice()))

    async def test_compactor_for_default_client_backend(self) -> None:
        state = self._state()
        compactor, choice = state.compactor_for("p", "m")
        self.assertIsNotNone(compactor)
        self.assertEqual(choice.backend, "client_summary")
        self.assertEqual(choice.fallback_reason, "")

    async def test_compactor_for_flags_fallback(self) -> None:
        class _Client:
            def protocol_capabilities_for(self, model):
                from atri_x.llm.protocol import ProtocolCapabilities

                return ProtocolCapabilities(protocol="responses", native_compaction=True)

        state = self._state()
        state.compactor = _FakeCompactor()
        state.client_for = lambda provider: _Client()
        compactor, choice = state.compactor_for("p", "m")
        self.assertIsNotNone(compactor)
        self.assertEqual(choice.backend, "fake")
        self.assertEqual(choice.fallback_reason, "protocol_advertises_native_compaction_without_backend")

    async def test_summarize_text_uses_chunk_limit(self) -> None:
        state = self._state(1000)
        compactor = _FakeCompactor()
        state.compactor = compactor
        summary = await state.summarize_text(None, ModelSelection("p", "m"), " long ", 50)
        self.assertEqual(summary, "summary")
        self.assertEqual(compactor.last.provider, "p")
        self.assertEqual(compactor.last.max_input_tokens, 50)
        self.assertEqual(compactor.last.chunk_input_tokens, 800)

    async def test_summarize_text_without_backend_raises(self) -> None:
        state = self._state()
        with self.assertRaises(ValueError):
            await state.summarize_text(None, ModelSelection("p", "m"), "x", 10)

    def test_compact_selection_fallbacks(self) -> None:
        state = self._state()
        fallback = ModelSelection("p", "work")
        self.assertEqual(state.compact_selection(fallback).model, "work")
        state.set_compact_model(ModelSelection("q", "c"))
        self.assertEqual(state.compact_selection(fallback).model, "c")
        self.assertEqual(state.configured_compact_model().model, "c")

    def test_overflow_and_runes(self) -> None:
        state = self._state(overflow_mode="truncate", user_original_max_runes=99)
        self.assertEqual(state.overflow_mode(), "truncate")
        self.assertEqual(state.user_original_max_runes(), 99)

    def test_record_and_read_usage(self) -> None:
        state = self._state()
        self.assertIsNone(state.usage("s"))
        state.record_usage("s", Usage(total_tokens=5))
        self.assertEqual(state.usage("s").total_tokens, 5)
        state.record_usage("s", None)
        self.assertEqual(state.usage("s").total_tokens, 5)

    async def test_reached_threshold_sync_and_async(self) -> None:
        state = self._state(1000, compact_enabled=True, compact_trigger_ratio=0.8)
        usage = Usage(total_tokens=900)
        self.assertTrue(state.reached_compact_threshold(None, usage, ModelSelection("p", "m")))
        self.assertTrue(
            await state.reached_compact_threshold_async(None, usage, ModelSelection("p", "m"))
        )
        self.assertFalse(
            state.reached_compact_threshold(None, Usage(total_tokens=10), ModelSelection("p", "m"))
        )
        state = self._state(1000, compact_enabled=False)
        self.assertFalse(
            state.reached_compact_threshold(None, usage, ModelSelection("p", "m"))
        )
        self.assertFalse(state.reached_compact_threshold(None, None, ModelSelection("p", "m")))

    def test_status_and_compact_active(self) -> None:
        state = self._state(1000, compact_enabled=True)
        text = state.status(None, "s", Usage(total_tokens=850, cache_hit_tokens=0), ModelSelection("p", "m"))
        self.assertIn("context window: 1000", text)
        self.assertIn("will compact before next request", text)

        class _Turns:
            def __init__(self, phase: str) -> None:
                self.phase = phase

            def snapshot(self, session_id: str):
                return type("S", (), {"phase": self.phase})()

        class _Requests:
            def __init__(self, kinds: list[str]) -> None:
                self.kinds = kinds

            def list_by_session(self, session_id: str):
                return [type("R", (), {"kind": kind})() for kind in self.kinds]

        state.turns = _Turns(PhaseCompact)
        self.assertTrue(state.compact_active("s"))
        state.turns = _Turns("idle")
        state.requests = _Requests([KindCompress])
        self.assertTrue(state.compact_active("s"))
        state.requests = _Requests(["turn"])
        self.assertFalse(state.compact_active("s"))

    async def test_load_delegates_to_loader(self) -> None:
        state = self._state()

        class _Loader:
            async def load(self, ctx, session_id):
                return LoadedContext(messages=[Message(id="m1")])

            async def load_raw_messages(self, ctx, session_id):
                return [Message(id="m1")]

        state.loader = _Loader()
        loaded = await state.load(None, "s")
        self.assertEqual(loaded.messages[0].id, "m1")
        self.assertEqual((await state.load_raw_messages(None, "s"))[0].id, "m1")


class _FakeCompactor:
    def __init__(self, summary: str = "summary", result: CompactResult | None = None) -> None:
        self.summary = summary
        self.result = result
        self.last: SummarizeRequest | None = None
        self.compact_request = None

    def name(self) -> str:
        return "fake"

    async def summarize_text(self, ctx, req: SummarizeRequest) -> SummarizeResult:
        self.last = req
        return SummarizeResult(summary=self.summary)

    async def compact(self, ctx, req) -> CompactResult:
        self.compact_request = req
        return self.result or CompactResult(
            summary="s", assembled_summary="assembled", usage=Usage(prompt_tokens=3, completion_tokens=1)
        )


class _Host(ModelMixin, ContextCompactMixin):
    def __init__(self, runtime: ContextRuntimeState, **extra) -> None:
        self.context_runtime = runtime
        for key, value in extra.items():
            setattr(self, key, value)
        self.providers = {"p": ProviderConfig(models=["m"])}
        self.clients = {"p": object()}
        self.model_runtime = new_model_runtime_state(
            None, "", "", None, self.providers,
            {"work": ModelSelection("p", "m")}, self.clients,
        )
        self.audit_events: list[tuple[str, dict]] = []
        self.services: dict[str, object] = {}

    def scope(self, ctx):
        return type("S", (), {"platform": "qq", "platform_scope_id": "group:1"})()

    def audit(self, event: str, **attrs) -> None:
        self.audit_events.append((event, attrs))

    def client_for_provider(self, provider):
        return self.clients.get(provider)


class TestCompactOrchestration(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, compactor: _FakeCompactor) -> ContextRuntimeState:
        state = ContextRuntimeState()
        state.configure(
            ContextConfig(),
            ModelMetadataConfig(default_context_window=1000),
            {"p": ProviderConfig(models=["m"])},
            ModelSelection(),
            None,
        )
        state.compactor = compactor
        state.loader = _Loader()
        return state

    async def test_compact_session_creates_new_session(self) -> None:
        compactor = _FakeCompactor()
        runtime = self._runtime(compactor)
        sessions = _Sessions()
        requests = _Requests()
        turns = _Turns()
        host = _Host(runtime, sessions=sessions, requests=requests, turns=turns, store=_Store())
        current = Session(id="s1", mode="work", title="Title")
        from atri_x.agent.context_compact import run_session_compaction

        next_session, choice = await run_session_compaction(
            runtime=runtime,
            requests=requests,
            turns=turns,
            sessions=sessions,
            store=host.store,
            ctx=None,
            current=current,
            scope=host.scope(None),
            trigger_reason="manual",
            selection=ModelSelection("p", "m"),
        )
        self.assertEqual(next_session.title, "Title compacted-1")
        self.assertEqual(choice.backend, "fake")
        metadata = decode_metadata(next_session.metadata)
        self.assertTrue(metadata.context_compact.pending)
        self.assertEqual(metadata.context_compact.trigger_reason, "manual")
        self.assertEqual(metadata.title_source, "compact")
        self.assertTrue(metadata.title_renamed)
        self.assertTrue(turns.completed)
        self.assertTrue(requests.done_called)
        self.assertEqual(compactor.compact_request.model, "m")

    async def test_compact_session_rejects_active_request(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        requests = _Requests(active=["req"])
        host = _Host(runtime, sessions=_Sessions(), requests=requests, turns=_Turns(), store=_Store())
        from atri_x.agent.context_compact import run_session_compaction

        with self.assertRaises(Exception) as caught:
            await run_session_compaction(
                runtime=runtime,
                requests=requests,
                turns=_Turns(),
                sessions=_Sessions(),
                store=host.store,
                ctx=None,
                current=Session(id="s1", mode="work"),
                scope=host.scope(None),
                trigger_reason="manual",
                selection=ModelSelection("p", "m"),
            )
        self.assertIn("正在运行的请求", str(caught.exception))

    async def test_compact_session_rejects_busy_turn(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        turns = _Turns(start=False)
        requests = _Requests()
        host = _Host(runtime, sessions=_Sessions(), requests=requests, turns=turns, store=_Store())
        from atri_x.agent.context_compact import run_session_compaction

        with self.assertRaises(Exception) as caught:
            await run_session_compaction(
                runtime=runtime,
                requests=requests,
                turns=turns,
                sessions=_Sessions(),
                store=host.store,
                ctx=None,
                current=Session(id="s1", mode="work"),
                scope=host.scope(None),
                trigger_reason="manual",
                selection=ModelSelection("p", "m"),
            )
        self.assertIn("正在处理其他任务", str(caught.exception))
        self.assertTrue(requests.done_called)

    async def test_compact_session_requires_messages(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        runtime.loader = _Loader(messages=[])
        requests = _Requests()
        host = _Host(runtime, sessions=_Sessions(), requests=requests, turns=_Turns(), store=_Store())
        from atri_x.agent.context_compact import run_session_compaction

        with self.assertRaises(Exception) as caught:
            await run_session_compaction(
                runtime=runtime,
                requests=requests,
                turns=host.turns,
                sessions=_Sessions(),
                store=host.store,
                ctx=None,
                current=Session(id="s1", mode="work"),
                scope=host.scope(None),
                trigger_reason="manual",
                selection=ModelSelection("p", "m"),
            )
        self.assertIn("没有可压缩的历史消息", str(caught.exception))

    async def test_compact_current_checks_history(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        host = _Host(
            runtime, sessions=_Sessions(), requests=_Requests(), turns=_Turns(), store=_Store()
        )
        host.history_enabled = lambda ctx: False
        with self.assertRaises(Exception) as caught:
            await host.compact_current(None, "manual")
        self.assertIn("已关闭历史记录", str(caught.exception))

    async def test_compact_session_audits_backend(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        host = _Host(
            runtime, sessions=_Sessions(), requests=_Requests(), turns=_Turns(), store=_Store()
        )
        host.authorize_execution_model_selection = lambda ctx, selection: None
        host.model_selection_for_turn = lambda ctx, session: ModelSelection("p", "m")
        _, content = await host.compact_session(
            None, Session(id="s1", mode="work"), "auto", ModelSelection("p", "m")
        )
        self.assertIn("上下文压缩完成", content)
        self.assertEqual(host.audit_events[0][0], "context_compaction_backend")

    def test_compact_active_mixin(self) -> None:
        runtime = self._runtime(_FakeCompactor())
        host = _Host(runtime, turns=_Turns(), requests=_Requests([KindCompress]))
        self.assertTrue(host.compact_active("s1"))


class TestTitlesAndMessages(unittest.IsolatedAsyncioTestCase):
    def test_title_generation(self) -> None:
        self.assertEqual(format_compacted_title("T", 2), "T compacted-2")
        title, generation, base = next_compacted_title(Session(id="s", title="T"))
        self.assertEqual((title, generation, base), ("T compacted-1", 1, "T"))
        session = Session(
            id="s",
            title="T compacted-1",
            metadata=encode_metadata_fields(
                "", {"context_compact": ContextCompactState(generation=1, base_title="T")}
            ),
        )
        title, generation, base = next_compacted_title(session)
        self.assertEqual((title, generation, base), ("T compacted-2", 2, "T"))
        # 标题不是本进程生成的（format 不匹配）时以当前标题为基准
        session = Session(
            id="s",
            title="用户改过的标题",
            metadata=encode_metadata_fields(
                "", {"context_compact": ContextCompactState(generation=1, base_title="T")}
            ),
        )
        title, generation, base = next_compacted_title(session)
        self.assertEqual(base, "用户改过的标题")
        self.assertEqual(next_compacted_title(Session(id="s"))[2], "New session")

    def test_compact_user_inputs(self) -> None:
        messages = [
            Message(role=RoleUser, content=" a "),
            Message(role=RoleAssistant, content="b"),
            Message(role=RoleUser, content="   "),
        ]
        self.assertEqual(compact_user_inputs(messages), [" a "])

    async def test_build_compact_messages(self) -> None:
        class _ToolCalls:
            def successful_ids(self, ids):
                return {"1": True, "2": False}

        class _Store:
            def tool_calls(self):
                return _ToolCalls()

        loaded = LoadedContext(
            summary=ContextSummary(summary="S", from_message_id="x"),
            messages=[
                Message(role=RoleUser, content="u1"),
                Message(
                    role=RoleAssistant,
                    content="a1",
                    metadata='{"tool_calls":[{"id":"1","name":"ok","arguments":"{}"},'
                    '{"id":"2","name":"bad","arguments":"{}"}],"raw_text":"raw"}',
                ),
                Message(role=RoleUser, content="   "),
            ],
        )
        out = await build_compact_messages(None, loaded, _Store())
        self.assertEqual([m.role for m in out], [RoleUser, RoleAssistant])
        self.assertTrue(out[0].content.startswith("S\n\n当前用户输入：\nu1"))
        self.assertEqual(out[1].content, "raw")
        self.assertEqual([call.name for call in out[1].tool_calls], ["ok"])

    async def test_build_compact_messages_requires_repository(self) -> None:
        class _Store:
            def tool_calls(self):
                return None

        loaded = LoadedContext(
            messages=[
                Message(
                    role=RoleAssistant,
                    metadata='{"tool_calls":[{"id":"1","name":"n","arguments":"{}"}]}',
                )
            ]
        )
        with self.assertRaises(Exception) as caught:
            await build_compact_messages(None, loaded, _Store())
        self.assertIn("tool call repository is not configured", str(caught.exception))

    def test_request_scope_key(self) -> None:
        class _S:
            platform = "qq"
            platform_scope_id = "group:1"

        self.assertEqual(request_scope_key(_S()), "qq:group:1")

        class _Empty:
            platform = ""
            platform_scope_id = ""

        self.assertEqual(request_scope_key(_Empty()), "unknown:unknown")

    def test_metadata_helpers(self) -> None:
        raw = encode_metadata_fields(
            '{"other":1}', {"context_compact": ContextCompactState(pending=True, summary="s")}
        )
        metadata = decode_metadata(raw)
        self.assertTrue(metadata.context_compact.pending)
        # 空值删键（与 Go setMetadataField 一致）
        stripped = encode_metadata_fields(raw, {"title_source": ""})
        self.assertNotIn("title_source", stripped)
        self.assertEqual(encode_metadata_fields("", {"title_source": ""}), "")
        self.assertIsNone(decode_metadata(""))
        # 直接读 JSON 时未知键保留（兜底视图语义）
        view = decode_metadata('{"tool_tags":["a"]}')
        self.assertEqual(list(view.tool_tags), ["a"])

    def test_pending_context_compact(self) -> None:
        session = Session(
            id="s",
            metadata=encode_metadata_fields(
                "",
                {"context_compact": ContextCompactState(pending=True, summary="S")},
            ),
        )
        compact = pending_context_compact(session)
        self.assertIsNotNone(compact)
        self.assertEqual(compact.summary, "S")
        self.assertIsNone(pending_context_compact(None))
        self.assertIsNone(
            pending_context_compact(
                Session(
                    id="s",
                    metadata=encode_metadata_fields(
                        "", {"context_compact": ContextCompactState(pending=False, summary="S")}
                    ),
                )
            )
        )


class TestEnsureAndInstall(unittest.TestCase):
    def test_ensure_context_runtime_builds_and_reuses(self) -> None:
        class _Host(ModelMixin):
            def __init__(self) -> None:
                self.store = None
                self.sessions = None
                self.requests = None
                self.turns = None
                self.context_config = ContextConfig(compact_enabled=True)
                self.compress_timeout = 3.0
                self.services: dict[str, object] = {}
                self.providers = {"p": ProviderConfig()}
                self.model_runtime = new_model_runtime_state(
                    None, "", "", None, self.providers, {}, {}
                )

        host = _Host()
        state = ensure_context_runtime(host)
        self.assertIsInstance(state, ContextRuntimeState)
        self.assertTrue(state.config.compact_enabled)
        self.assertEqual(state.compress_timeout, 3.0)
        self.assertIs(ensure_context_runtime(host), state)

        service = install_context_services(host)
        self.assertIs(host.services["context_runtime"], service)
        # 门面转发：方法来自 mixin，字段读写落在宿主上
        self.assertTrue(hasattr(service, "context_status"))
        self.assertIsInstance(service.compact_selection_for_session(None), ModelSelection)

    def test_compact_selection_for_session_uses_mode(self) -> None:
        class _Host(ModelMixin):
            def __init__(self) -> None:
                self.providers = {"p": ProviderConfig()}
                self.model_runtime = new_model_runtime_state(
                    None, "", "", None, self.providers,
                    {"work": ModelSelection("p", "w"), "chat": ModelSelection("p", "c")}, {},
                )

        host = _Host()
        self.assertEqual(
            compact_selection_for_session(host, Session(id="s", mode="chat")).model, "c"
        )
        self.assertEqual(
            compact_selection_for_session(host, None).model, "w"
        )


class _Loader:
    def __init__(self, messages: list[Message] | None = None) -> None:
        self.messages = messages if messages is not None else [Message(id="m1", role=RoleUser, content="hi")]

    async def load(self, ctx, session_id):
        return LoadedContext(messages=list(self.messages))

    async def load_raw_messages(self, ctx, session_id):
        return list(self.messages)


class _Sessions:
    def __init__(self) -> None:
        self.created: list[Session] = []

    def create(self, scope, req):
        session = Session(
            id="new-session",
            mode=req.mode,
            title=req.title,
            metadata=req.metadata,
            created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        )
        self.created.append(session)
        return session


class _Requests:
    def __init__(self, active: list[str] | None = None) -> None:
        self.active = list(active or [])
        self.done_called = False

    def list_by_session(self, session_id: str):
        return [type("R", (), {"kind": kind})() for kind in self.active]

    async def start(self, ctx, request):
        self.start_request = request

        def done() -> None:
            self.done_called = True

        return object(), _Token(), done


class _Token:
    def raise_if_cancelled(self) -> None:
        return None

    def error(self):
        return None


class _Turns:
    def __init__(self, start: bool = True) -> None:
        self.start = start
        self.completed = False

    def start_compact(self, session_id: str) -> bool:
        return self.start

    def complete_compact(self, session_id: str) -> bool:
        self.completed = True
        return True

    def snapshot(self, session_id: str):
        return type("S", (), {"phase": "idle"})()


class _ToolCalls:
    def successful_ids(self, ids):
        return {}


class _Store:
    def tool_calls(self):
        return _ToolCalls()

    def sessions(self):
        return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
