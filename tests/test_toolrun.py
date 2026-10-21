"""``atri_x.toolrun`` 的单元测试。

对应上游 ``internal/toolrun/*_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from atri_x.llm.llm import (
    SegmentType,
    ToolCallRequest,
    ToolFunctionSchema,
    ToolSchema,
    segments_content_text,
)
from atri_x.security import Actor, Policy, RoleSuperadmin, RoleUser
from atri_x.storage.models import SessionModeChat, SessionModeWork
from atri_x.toolrun import (
    BACKGROUND_DISALLOWED_TOOLS,
    Cache,
    CachedTool,
    Manager,
    RunRequest,
)

from atri_x.toolrun import atriwisp as aw
from atri_x.toolrun import atriwisp_exec as awex
from atri_x.toolrun import availability as av
from atri_x.toolrun import cache as cache_mod
from atri_x.toolrun import native as native_mod
from atri_x.toolrun import runner as runner_mod
from atri_x.toolrun import toolrun as tr


def run(coro):
    return asyncio.run(coro)


class AssessFailClosedTests(unittest.TestCase):
    """研究报告 §5.3：风险评估器抛异常时按 high 处理（旧实现是 low → 免确认执行）。"""

    def test_assessor_exception_yields_high(self) -> None:
        class BoomManager:
            async def assess_risk(self, ctx, resolved, arguments):
                raise RuntimeError("assessor exploded")

        call = ToolCallRequest(id="1", name="echo", arguments="{}")
        assessment, risk_text = run(
            runner_mod._assess_for_run(BoomManager(), None, None, call)
        )
        self.assertEqual(assessment.level, "high")
        self.assertEqual(risk_text, "unknown")
        self.assertTrue(assessment.reasons)

    def test_empty_level_yields_high(self) -> None:
        class EmptyManager:
            async def assess_risk(self, ctx, resolved, arguments):
                from atri_x.tool.tool import RiskAssessment

                return RiskAssessment(level="")

        call = ToolCallRequest(id="1", name="echo", arguments="{}")
        assessment, _ = run(runner_mod._assess_for_run(EmptyManager(), None, None, call))
        self.assertEqual(assessment.level, "high")


def _allow_authorize(ctx, call, resolved):
    """测试用的"放行"授权回调。

    ``RunRequest.authorize_tool=None`` 现在是 fail-closed（拒绝），生产路径由
    ``chat_tools._tool_hooks`` 装配真实回调；这里显式给一个放行版本，让其余用例
    继续测它们自己的分支。
    """
    return True, ""


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
class TestCache(unittest.TestCase):
    def test_decode_empty(self) -> None:
        self.assertEqual([], cache_mod.decode_cache(None).tools)
        self.assertEqual([], cache_mod.decode_cache("").tools)
        self.assertEqual([], cache_mod.decode_cache(b"").tools)

    def test_decode_invalid_json_returns_empty(self) -> None:
        self.assertEqual([], cache_mod.decode_cache("{not json").tools)

    def test_encode_empty_is_none(self) -> None:
        self.assertIsNone(cache_mod.encode_cache(Cache()))
        self.assertIsNone(cache_mod.encode_cache(Cache(tools=[CachedTool()])))

    def test_round_trip_with_omitempty(self) -> None:
        tool = CachedTool(
            name="read_file",
            description="d",
            schema=ToolSchema(
                function=ToolFunctionSchema(name="read_file", description="d", parameters={"a": 1})
            ),
        )
        encoded = cache_mod.encode_cache(Cache(tools=[tool]))
        assert encoded is not None
        raw = json.loads(encoded)
        item = raw["tools"][0]
        # omitempty：空串/0/false 字段被省略
        for absent in ("canonical_name", "atriwisp_name", "event_key", "endpoint",
                       "timeout_seconds", "foreground_only"):
            self.assertNotIn(absent, item)
        self.assertEqual("native", item["source"])
        back = cache_mod.decode_cache(encoded)
        self.assertEqual(1, len(back.tools))
        self.assertEqual("read_file", back.tools[0].name)

    def test_normalize_dedups_prefers_canonical_key(self) -> None:
        tools = [
            CachedTool(name="a"),
            CachedTool(name="b", canonical_name="a"),
            CachedTool(name=""),
            CachedTool(name="c"),
        ]
        out = cache_mod.normalize_cached_tools(tools)
        self.assertEqual(["a", "c"], [t.canonical_name or t.name for t in out])

    def test_normalize_fills_default_source(self) -> None:
        out = cache_mod.normalize_cached_tools([CachedTool(name="x", source="")])
        self.assertEqual(cache_mod.SOURCE_KIND_NATIVE, out[0].source)

    def test_normalize_sorts(self) -> None:
        out = cache_mod.normalize_cached_tools([CachedTool(name="z"), CachedTool(name="a")])
        self.assertEqual(["a", "z"], [t.name for t in out])

    def test_merge_incoming_wins(self) -> None:
        existing = [CachedTool(name="a", description="old"), CachedTool(name="b")]
        incoming = [CachedTool(name="a", description="new")]
        out = cache_mod.merge_cached_tools(existing, incoming)
        by_name = {t.name: t for t in out}
        self.assertEqual("new", by_name["a"].description)
        self.assertIn("b", by_name)


# ---------------------------------------------------------------------------
# atriwisp
# ---------------------------------------------------------------------------
class TestAtriwisp(unittest.TestCase):
    def test_canonical_name_sanitizes(self) -> None:
        self.assertEqual("atriwisp_my_hub_my_tool", aw.atriwisp_tool_name("my hub", "my.tool"))

    def test_cached_tools_from_atriwisp(self) -> None:
        injection = aw.AtriwispInjection(
            atriwisp_name="hub",
            event_key="k",
            tools=[
                aw.AtriwispToolDeclaration(name="t1", description="d", endpoint="http://x/"),
                aw.AtriwispToolDeclaration(name="  "),  # 空名跳过
            ],
        )
        out = aw.cached_tools_from_atriwisp(injection)
        self.assertEqual(1, len(out))
        tool = out[0]
        self.assertEqual("atriwisp_hub_t1", tool.canonical_name)
        self.assertEqual(cache_mod.SOURCE_KIND_ATRIWISP, tool.source)
        self.assertEqual("k", tool.event_key)
        self.assertEqual("atriwisp_hub_t1", tool.schema.function.name)

    def test_empty_hub_returns_nothing(self) -> None:
        self.assertEqual([], aw.cached_tools_from_atriwisp(aw.AtriwispInjection()))


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------
class TestAvailability(unittest.TestCase):
    def test_foreground_only_in_background(self) -> None:
        from atri_x.tool.sandbox import SandboxContext, with_sandbox_context
        from atri_x.tool.tool import Info

        # 必须是真正的 SandboxContext —— sandbox_context_from_context 用 isinstance 判定，
        # 鸭子类型的替身会取不到（曾因此让后台相关用例全部静默走错分支）。
        ctx = with_sandbox_context({}, SandboxContext(root="/tmp", background=True))
        self.assertEqual(
            av.REASON_FOREGROUND_ONLY, av.unavailable_reason(ctx, Info(foreground_only=True))
        )

    def test_other_reason(self) -> None:
        from atri_x.tool.tool import Info

        self.assertEqual(av.REASON_UNAVAILABLE, av.unavailable_reason(None, Info()))


def background_ctx() -> object:
    """带后台标记的 ctx。"""
    from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

    return with_sandbox_context({}, SandboxContext(root="/tmp", background=True))


# ---------------------------------------------------------------------------
# native source + Manager
# ---------------------------------------------------------------------------
@dataclass
class FakeToolDef:
    """最小可用的工具替身（满足 Registry 的鸭子类型接口）。"""

    name: str = "t"
    hidden: bool = False
    foreground_only: bool = False
    schema: ToolSchema | None = None
    calls: list = field(default_factory=list)
    result: object | None = None

    def info(self):
        from atri_x.tool.tool import Info

        return Info(
            name=self.name, hidden=self.hidden, foreground_only=self.foreground_only
        )

    async def call(self, ctx, request):
        self.calls.append(request)
        from atri_x.tool.tool import Result

        return self.result if self.result is not None else Result(content=f"ok:{self.name}")


class FakeRegistry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.schema_calls = 0

    def get(self, name):
        tool = self._tools.get(name)
        return (tool, True) if tool is not None else (None, False)

    def list(self):
        return [t.info() for t in self._tools.values()]

    def schemas_for_context(self, predicate):
        self.schema_calls += 1
        out = []
        for tool in self._tools.values():
            if not predicate(tool.info()):
                continue
            schema = tool.schema or ToolSchema(
                function=ToolFunctionSchema(name=tool.name, description="", parameters={})
            )
            out.append(schema)
        return out


def view(mode: str = SessionModeWork, actor: Actor | None = None, disable: bool = False):
    return tr.Context(mode=mode, actor=actor or Actor(role=RoleSuperadmin), disable_base_tools=disable)


class TestNativeSource(unittest.TestCase):
    def test_hidden_and_discover_filtered(self) -> None:
        registry = FakeRegistry(
            [
                FakeToolDef(name="visible"),
                FakeToolDef(name="hidden_one", hidden=True),
                FakeToolDef(name="discover_tool"),
            ]
        )
        source = native_mod.NativeSource(registry)
        names = source.tool_names(None, Actor(role=RoleSuperadmin), Policy())
        self.assertEqual(["visible"], names)

    def test_nil_registry_is_safe(self) -> None:
        source = native_mod.NativeSource(None)
        self.assertEqual([], source.base_schemas(None))
        self.assertEqual([], source.tool_infos(None, Actor(), Policy()))
        self.assertEqual((None, False), source.get("x"))


class TestManagerResolve(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = FakeToolDef(name="native_tool")
        self.registry = FakeRegistry([self.tool])
        self.manager = Manager(registry=self.registry)

    def test_resolve_native(self) -> None:
        resolved = self.manager.resolve(None, "native_tool", [])
        self.assertTrue(resolved.available)
        self.assertEqual(cache_mod.SOURCE_KIND_NATIVE, resolved.source)

    def test_resolve_unknown(self) -> None:
        resolved = self.manager.resolve(None, "nope", [])
        self.assertFalse(resolved.available)
        self.assertIn("not found", resolved.reason)

    def test_resolve_empty_name(self) -> None:
        self.assertFalse(self.manager.resolve(None, "  ", []).available)

    def test_resolve_from_cache_when_native_gone(self) -> None:
        cached = [CachedTool(name="gone", schema=ToolSchema(function=ToolFunctionSchema(name="gone")))]
        resolved = self.manager.resolve(None, "gone", cached)
        self.assertFalse(resolved.available)
        self.assertIn("no longer available", resolved.reason)

    def test_resolve_atriwisp_needs_endpoint(self) -> None:
        cached = [
            CachedTool(
                name="t",
                canonical_name="atriwisp_h_t",
                source=cache_mod.SOURCE_KIND_ATRIWISP,
                endpoint="",
            )
        ]
        resolved = self.manager.resolve(None, "atriwisp_h_t", cached)
        self.assertFalse(resolved.available)
        self.assertIn("endpoint", resolved.reason)

    def test_resolve_atriwisp_ok(self) -> None:
        cached = [
            CachedTool(
                name="t",
                canonical_name="atriwisp_h_t",
                source=cache_mod.SOURCE_KIND_ATRIWISP,
                endpoint="http://127.0.0.1/x",
            )
        ]
        resolved = self.manager.resolve(None, "atriwisp_h_t", cached)
        self.assertTrue(resolved.available)
        self.assertEqual("atriwisp_h_t", resolved.name)

    def test_resolve_blocks_unlisted_native_in_background(self) -> None:
        resolved = self.manager.resolve(background_ctx(), "native_tool", [])
        self.assertFalse(resolved.available)
        self.assertIn("not allowed in this background task", resolved.reason)

    def test_background_tool_allowed(self) -> None:
        for name in BACKGROUND_DISALLOWED_TOOLS:
            self.assertFalse(tr.background_tool_allowed(name))
            self.assertFalse(tr.background_tool_allowed(name.upper()))
        self.assertTrue(tr.background_tool_allowed("read_file"))


class TestManagerSchemas(unittest.TestCase):
    def test_chat_mode_yields_nothing(self) -> None:
        manager = Manager(registry=FakeRegistry([FakeToolDef(name="t")]))
        self.assertEqual([], run(manager.base_schemas(None, view(SessionModeChat))))
        self.assertEqual([], run(manager.tool_infos(None, view(SessionModeChat))))
        self.assertEqual([], run(manager.tool_names(None, view(SessionModeChat))))

    def test_disable_base_tools(self) -> None:
        manager = Manager(registry=FakeRegistry([FakeToolDef(name="t")]))
        self.assertEqual([], run(manager.schemas(None, view(disable=True), [])))

    def test_schemas_dedup_and_order(self) -> None:
        manager = Manager(registry=FakeRegistry([FakeToolDef(name="a")]))
        cached = [
            CachedTool(
                name="a",
                schema=ToolSchema(function=ToolFunctionSchema(name="a", parameters={})),
            ),
            CachedTool(
                name="b",
                schema=ToolSchema(function=ToolFunctionSchema(name="b", parameters={})),
            ),
        ]
        schemas = run(manager.schemas(None, view(), cached))
        self.assertEqual(["a", "b"], [s.function.name for s in schemas])

    def test_background_skips_disallowed_cached_tool(self) -> None:
        manager = Manager(registry=FakeRegistry([]))
        cached = [
            CachedTool(
                name="discover_tool",
                schema=ToolSchema(function=ToolFunctionSchema(name="discover_tool")),
            ),
            CachedTool(
                name="read_file",
                schema=ToolSchema(function=ToolFunctionSchema(name="read_file")),
            ),
        ]
        schemas = run(manager.schemas(background_ctx(), view(disable=True), cached))
        self.assertEqual(["read_file"], [s.function.name for s in schemas])


class TestSchemaForContext(unittest.TestCase):
    def test_no_sandbox_returns_same(self) -> None:
        schema = ToolSchema(function=ToolFunctionSchema(name="read_file", description="d"))
        self.assertIs(schema, tr.schema_for_context(None, schema))

    def test_background_appends_instruction_once(self) -> None:
        ctx = background_ctx()
        schema = ToolSchema(function=ToolFunctionSchema(name="read_file", description="d"))
        out = tr.schema_for_context(ctx, schema)
        self.assertNotEqual(schema.function.description, out.function.description)
        self.assertTrue(out.function.description.startswith("d"))
        # 非路径类工具不动
        other = ToolSchema(function=ToolFunctionSchema(name="other", description="d"))
        self.assertIs(other, tr.schema_for_context(ctx, other))

    def test_foreground_sandbox_does_not_append(self) -> None:
        from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

        ctx = with_sandbox_context({}, SandboxContext(root="/tmp", background=False))
        schema = ToolSchema(function=ToolFunctionSchema(name="read_file", description="d"))
        self.assertIs(schema, tr.schema_for_context(ctx, schema))


# ---------------------------------------------------------------------------
# atriwisp_exec（真起 HTTP 服务端）
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    response = ("200 OK", {"content": "from-content"})
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).seen.append(json.loads(body.decode("utf-8")))
        status, payload = type(self).response
        code = int(status.split()[0])
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # noqa: A002
        return


class TestAtriwispExec(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}/tool"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.seen = []
        _Handler.response = ("200 OK", {"content": "from-content"})

    def _resolved(self, timeout: int = 0):
        @dataclass
        class Resolved:
            cached: CachedTool | None = None
            name: str = ""

        return Resolved(
            cached=CachedTool(
                name="t", canonical_name="atriwisp_h_t", endpoint=self.endpoint,
                event_key="ev", timeout_seconds=timeout,
            ),
            name="atriwisp_h_t",
        )

    def test_missing_metadata(self) -> None:
        @dataclass
        class Resolved:
            cached: object = None

        message, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), Resolved())
        self.assertIsNone(result)
        self.assertIn("metadata is missing", str(err))
        self.assertIn("failed", segments_content_text(message.segments))

    def test_content_field_wins(self) -> None:
        _Handler.response = ("200 OK", {"content": "c", "text": "t", "result": "r"})
        message, result, err = awex.execute_atriwisp_tool(
            None, ToolCallRequest(name="t", id="1", arguments='{"a":1}'), self._resolved()
        )
        self.assertIsNone(err)
        self.assertEqual("c", result.content)
        self.assertEqual("c", segments_content_text(message.segments))
        # 请求体形状
        self.assertEqual({"tool": "t", "event_key": "ev", "arguments": {"a": 1}}, _Handler.seen[0])

    def test_text_field_second(self) -> None:
        _Handler.response = ("200 OK", {"text": "t", "result": "r"})
        _, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), self._resolved())
        self.assertIsNone(err)
        self.assertEqual("t", result.content)

    def test_empty_arguments_becomes_object(self) -> None:
        awex.execute_atriwisp_tool(None, ToolCallRequest(name="t", arguments="  "), self._resolved())
        self.assertEqual({}, _Handler.seen[0]["arguments"])

    def test_error_field_is_failure(self) -> None:
        _Handler.response = ("200 OK", {"error": "boom"})
        _, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), self._resolved())
        self.assertIsNone(result)
        self.assertIn("Atriwisp tool error: boom", str(err))

    def test_non_2xx_is_failure_with_body(self) -> None:
        _Handler.response = ("500 Internal Server Error", {"oops": True})
        _, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), self._resolved())
        self.assertIsNone(result)
        self.assertIn("HTTP 500", str(err))

    def test_plain_text_body_is_content(self) -> None:
        _Handler.response = ("200 OK", b"just text")
        _, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), self._resolved())
        self.assertIsNone(err)
        self.assertEqual("just text", result.content)

    def test_first_result_helper(self) -> None:
        self.assertEqual("b", awex.first_atriwisp_result("", "  ", " b ", "c"))
        self.assertEqual("", awex.first_atriwisp_result("", "   "))

    def test_default_timeout_constant(self) -> None:
        self.assertEqual(10.0, awex.DEFAULT_ATRIWISP_TIMEOUT_SECONDS)

    def test_missing_endpoint(self) -> None:
        @dataclass
        class Resolved:
            cached: object = None

        resolved = Resolved(cached=CachedTool(name="t", canonical_name="c", endpoint=""))
        _, result, err = awex.execute_atriwisp_tool(None, ToolCallRequest(name="t"), resolved)
        self.assertIsNone(result)
        self.assertIn("endpoint", str(err))


# ---------------------------------------------------------------------------
# runner（fake deps）
# ---------------------------------------------------------------------------
class FakeDeps:
    """记录调用序列的 RunnerDeps 替身。"""

    def __init__(
        self,
        confirm=None,
        should_preview=False,
        background_handled=None,
        start_error=None,
        prepare_error=None,
        complete_error=None,
    ):
        self.calls: list[str] = []
        self.previews: list[str] = []
        self.outputs: list[object] = []
        self.audits: list[tuple] = []
        self.records: list[tuple] = []
        self.confirm = confirm or runner_mod.ConfirmResult(allowed=True)
        self.should_preview = should_preview
        self.background_handled = background_handled
        self.completed_segments = None
        self.start_error = start_error
        self.prepare_error = prepare_error
        self.complete_error = complete_error

    async def prepare_tool_call(self, ctx, session, call):
        self.calls.append("prepare_tool_call")
        if self.prepare_error is not None:
            raise self.prepare_error
        return call

    async def should_send_preview(self, ctx, session, call, text):
        return self.should_preview

    async def confirm_tool_call(self, ctx, session_id, call, assessment, detail):
        self.calls.append("confirm_tool_call")
        return self.confirm

    async def confirm_background_tool(self, ctx, session_id, call, resolved, assessment):
        if self.background_handled is None:
            return runner_mod.ConfirmResult(allowed=False), False
        return self.background_handled

    async def start_tool_request(self, ctx, session_id, tool_name):
        self.calls.append("start_tool_request")
        if self.start_error is not None:
            raise self.start_error

        def done():
            self.calls.append("done")

        # 契约：三元组 ``(ctx, started_at, done)``。这里曾经返回 4 项，和 runner
        # 当年的错误解包（同样 4 项）互相掩盖 —— 真实适配器返回 3 项，反而
        # 每次调用都在解包处失败、工具永远执行不到。见
        # StartToolRequestContractTests。
        return ctx, None, done

    async def prepare_tool_context(self, ctx, session, call):
        return ctx

    async def complete_tool_call(self, ctx, session, call, risk, segments, call_err):
        self.calls.append("complete_tool_call")
        if self.complete_error is not None:
            raise self.complete_error
        if self.completed_segments is not None:
            return self.completed_segments
        return segments

    async def send_preview(self, ctx, text):
        self.previews.append(text)

    async def send_outputs(self, ctx, outputs):
        self.outputs.extend(outputs)

    async def record_tool_call(self, ctx, session_id, call, risk, started_at, result, call_err):
        self.records.append((call.name, risk, call_err))

    async def audit_tool_denied(self, ctx, session_id, call, risk, reason):
        self.audits.append((call.name, risk, reason))

    async def remember_discovery_result(self, ctx, session, result):
        self.calls.append("remember_discovery_result")

    def add_tool_use(self, session_id, tool_name):
        self.calls.append(f"add_tool_use:{tool_name}")

    def tool_result_message(self, session_id, message):
        self.calls.append("tool_result_message")
        return ("result", message)

    def tool_call_message(self, session_id, content, raw_text, calls, reasoning=""):
        self.calls.append("tool_call_message")
        return ("call", content, raw_text, list(calls), reasoning)

    def persisted_tool_message(self, message):
        return message


def make_session(mode: str = SessionModeWork):
    from atri_x.storage.models import Session

    return Session(id="s1", mode=mode)


class TestRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = FakeToolDef(name="echo")
        self.manager = Manager(registry=FakeRegistry([self.tool]))

    def _confirm_manager(self):
        """需要走确认路径的 Manager。

        超管 + 低风险的默认策略**不会**弹确认（``needs_tool_confirmation`` 对超管用
        ``superadmin_confirm_risk``，默认 RiskHigh）。把阈值降到 RiskLow 才能走到
        ``confirm_tool_call``。
        """
        from atri_x.security import RiskLow

        return Manager(
            registry=FakeRegistry([self.tool]),
            policy=Policy(superadmin_confirm_risk=RiskLow),
        )

    def _req(self, deps, **kwargs):
        base = dict(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="echo", arguments="{}")],
            assistant_text="hi",
            actor=Actor(role=RoleSuperadmin),
            cached_tools=[],
            authorize_tool=_allow_authorize,
        )
        base.update(kwargs)
        return RunRequest(**base), deps

    def test_chat_mode_runs_nothing(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps, session=make_session(SessionModeChat))
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual([], result.messages)
        self.assertEqual([], deps.calls)

    def test_success_path(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps)
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual(1, len(result.messages))
        self.assertEqual("ok:echo", segments_content_text(result.messages[0].segments))
        self.assertIn("add_tool_use:echo", deps.calls)
        self.assertIn("done", deps.calls)
        # transcript 首条是 assistant 的 tool_calls 记录
        self.assertEqual("call", result.transcript[0][0])
        self.assertEqual("result", result.transcript[1][0])

    def test_group_allowlist_denies_before_execution(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps, allowed_tools={"other": True})
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual(1, len(result.messages))
        self.assertIn("not in this group's allowlist", segments_content_text(result.messages[0].segments))
        self.assertEqual([("echo", "denied")], [(r[0], r[1]) for r in deps.records if r[1] == "denied"])
        self.assertNotIn("start_tool_request", deps.calls)

    def test_missing_authorize_tool_denies(self) -> None:
        """没有权威授权回调时必须拒绝（旧实现等于全放行）。"""
        deps = FakeDeps()
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="echo", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
        )
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn(
            "authorization is not configured",
            segments_content_text(result.messages[0].segments),
        )
        self.assertNotIn("start_tool_request", deps.calls)

    def test_authorize_denies(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps, authorize_tool=lambda ctx, call, resolved: (False, "policy says no"))
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn("policy says no", segments_content_text(result.messages[0].segments))

    def test_authorize_called_twice(self) -> None:
        """执行前必须再授权一次（排队期间策略可能被撤销）。"""
        calls: list[str] = []

        def authorize(ctx, call, resolved):
            calls.append(call.name)
            return True, ""

        deps = FakeDeps()
        req, _ = self._req(deps, authorize_tool=authorize)
        run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual(["echo", "echo"], calls)

    def test_reauthorize_after_queue_denies(self) -> None:
        state = {"n": 0}

        def authorize(ctx, call, resolved):
            state["n"] += 1
            return (True, "") if state["n"] == 1 else (False, "revoked while queued")

        deps = FakeDeps()
        req, _ = self._req(deps, authorize_tool=authorize)
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn("revoked while queued", segments_content_text(result.messages[0].segments))
        self.assertIn("done", deps.calls)  # 必须释放队列槽位
        self.assertEqual([], self.tool.calls)  # 从未真正执行

    def test_before_execute_denies(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps, before_execute=lambda ctx, call, resolved: RuntimeError("quota"))
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn("quota", segments_content_text(result.messages[0].segments))
        self.assertEqual([], self.tool.calls)

    def test_confirmation_stop_returns_stopped(self) -> None:
        deps = FakeDeps(confirm=runner_mod.ConfirmResult(allowed=False, stopped=True))
        req, _ = self._req(deps)
        result = run(runner_mod.run(self._confirm_manager(), None, deps, req))
        self.assertTrue(result.stopped)
        self.assertEqual([], result.messages)

    def test_confirmation_denied_with_message(self) -> None:
        from atri_x.llm.llm import LLMMessage, text_segments

        denied = runner_mod.ConfirmResult(
            allowed=False,
            message=LLMMessage(name="echo", segments=text_segments("tool call echo denied: no")),
            extra="补充说明",
        )
        deps = FakeDeps(confirm=denied)
        req, _ = self._req(deps)
        result = run(runner_mod.run(self._confirm_manager(), None, deps, req))
        self.assertEqual(1, len(result.messages))
        self.assertIn("denied", segments_content_text(result.messages[0].segments))
        self.assertEqual("补充说明", result.confirmation_extra)

    def test_confirmation_extra_joins_with_blank_line(self) -> None:
        from atri_x.llm.llm import LLMMessage, text_segments

        deps = FakeDeps(
            confirm=runner_mod.ConfirmResult(
                allowed=False, extra="A", message=LLMMessage(segments=text_segments("x"))
            )
        )
        req, _ = self._req(
            deps,
            calls=[ToolCallRequest(id="1", name="echo"), ToolCallRequest(id="2", name="echo")],
        )
        result = run(runner_mod.run(self._confirm_manager(), None, deps, req))
        self.assertEqual("A\n\nA", result.confirmation_extra)

    def test_low_risk_superadmin_does_not_confirm(self) -> None:
        """默认策略下超管 + 低风险不弹确认，直接执行。"""
        deps = FakeDeps(confirm=runner_mod.ConfirmResult(allowed=False, stopped=True))
        req, _ = self._req(deps)
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertFalse(result.stopped)
        self.assertNotIn("confirm_tool_call", deps.calls)

    def test_unavailable_tool_reported(self) -> None:
        deps = FakeDeps()
        req, _ = self._req(deps, calls=[ToolCallRequest(id="1", name="ghost")])
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn("not found", segments_content_text(result.messages[0].segments))

    def test_batch_preview_sent_once_for_multiple_calls(self) -> None:
        deps = FakeDeps(should_preview=True)
        req, _ = self._req(
            deps,
            calls=[ToolCallRequest(id="1", name="echo"), ToolCallRequest(id="2", name="echo")],
        )
        run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual(1, len(deps.previews))
        self.assertEqual(2, deps.previews[0].count("正在调用"))

    def test_single_call_preview_uses_per_call_path(self) -> None:
        deps = FakeDeps(should_preview=True)
        req, _ = self._req(deps)
        run(runner_mod.run(self.manager, None, deps, req))
        self.assertEqual(1, len(deps.previews))
        self.assertIn("echo", deps.previews[0])

    def test_failure_sends_failure_preview(self) -> None:
        class BoomTool(FakeToolDef):
            async def call(self, ctx, request):
                raise RuntimeError("kaboom")

        manager = Manager(registry=FakeRegistry([BoomTool(name="boom")]))
        deps = FakeDeps()
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="boom", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=_allow_authorize,
        )
        result = run(runner_mod.run(manager, None, deps, req))
        self.assertIsNotNone(result.messages)
        self.assertTrue(any("调用失败" in p for p in deps.previews))

    def test_discovery_result_remembered(self) -> None:
        class DiscoverTool(FakeToolDef):
            async def call(self, ctx, request):
                from atri_x.tool.tool import Result

                return Result(content="found")

        manager = Manager(registry=FakeRegistry([DiscoverTool(name="discover_tool")]))
        deps = FakeDeps()
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="discover_tool", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=_allow_authorize,
        )
        run(runner_mod.run(manager, None, deps, req))
        self.assertIn("remember_discovery_result", deps.calls)

    def test_outputs_delivered(self) -> None:
        class OutTool(FakeToolDef):
            async def call(self, ctx, request):
                from atri_x.tool.tool import Result

                return Result(content="ok", outputs=["o1", "o2"])

        manager = Manager(registry=FakeRegistry([OutTool(name="out")]))
        deps = FakeDeps()
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="out", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=_allow_authorize,
        )
        run(runner_mod.run(manager, None, deps, req))
        self.assertEqual(["o1", "o2"], deps.outputs)

    def test_media_retain_failure_denies(self) -> None:
        class Media:
            def retain_session_tool_arguments(self, session_id, arguments):
                raise RuntimeError("too big")

        manager = Manager(registry=FakeRegistry([self.tool]), media=Media())
        deps = FakeDeps()
        req, _ = self._req(deps)
        result = run(runner_mod.run(manager, None, deps, req))
        self.assertIn("retain media arguments", segments_content_text(result.messages[0].segments))
        self.assertEqual([], self.tool.calls)

    def test_complete_tool_call_error_becomes_failure_text(self) -> None:
        class BadDeps(FakeDeps):
            async def complete_tool_call(self, ctx, session, call, risk, segments, call_err):
                raise RuntimeError("hook exploded")

        deps = BadDeps()
        req, _ = self._req(deps)
        result = run(runner_mod.run(self.manager, None, deps, req))
        self.assertIn("hook exploded", segments_content_text(result.messages[0].segments))


class MediaRetainContractTests(unittest.IsolatedAsyncioTestCase):
    """回归：runner 必须按**真实** ``media.Manager`` 的同步签名调用留存入口。

    历史缺陷（0.9.8 起，0.9.9-local.4 仍在发行包里）：runner 照 Go 版签名传
    ``(ctx, session_id, arguments)``，而 Python 移植把
    ``retain_session_tool_arguments`` 改成**同步两参**方法（Go 的 ctx 只服务于
    同步 SQLite 调用，PORTING §1.6 有意去掉）。真实接入后每次工具调用都在执行前
    抛 ``TypeError: ... takes 3 positional arguments but 4 were given``，又被
    宽泛的 ``except BaseException`` 记成 "retain media arguments"，于是**任何
    工具都执行不到**。测试里的假实现当年照抄了 3 参调用，单测全绿也发现不了 ——
    与 :class:`StartToolRequestContractTests` 同一类缺陷。
    """

    async def test_runner_call_matches_real_media_manager(self) -> None:
        from atri_x.media.manager import Manager as MediaManager
        from atri_x.media.types import IDPrefix

        class Refs:
            def __init__(self) -> None:
                self.added: list = []

            def add_all(self, references) -> None:
                self.added.extend(references)

        class Store:
            def __init__(self) -> None:
                self.refs = Refs()

            def media_references(self):
                return self.refs

        store = Store()
        # 真实的媒体中心：本用例只走同步引用路径，root 不需要真的存在。
        media = MediaManager(store, "tmp_toolrun_media_root")
        tool = FakeToolDef(name="echo")
        manager = Manager(registry=FakeRegistry([tool]), media=media)
        deps = FakeDeps()
        media_id = IDPrefix + "a" * 64
        req = RunRequest(
            session=make_session(),
            calls=[
                ToolCallRequest(
                    id="1", name="echo", arguments='{"source":"%s"}' % media_id
                )
            ],
            assistant_text="hi",
            actor=Actor(role=RoleSuperadmin),
            cached_tools=[],
            authorize_tool=_allow_authorize,
        )
        result = await runner_mod.run(manager, None, deps, req)
        text = segments_content_text(result.messages[0].segments)
        self.assertEqual("ok:echo", text, "工具必须真正执行，而不是被 retain 拦下")
        self.assertEqual(1, len(tool.calls))
        self.assertEqual(1, len(store.refs.added), "参数里的媒体 ID 必须留存为引用")
        reference = store.refs.added[0]
        self.assertEqual(media_id, reference.media_id)
        self.assertEqual("session_tool", reference.owner_type)
        self.assertEqual("s1", reference.owner_id)


class TestRunnerHelpers(unittest.TestCase):
    def test_tool_allowed(self) -> None:
        self.assertTrue(runner_mod._tool_allowed(None, "anything"))
        self.assertTrue(runner_mod._tool_allowed({"echo": True}, "  ECHO "))
        self.assertFalse(runner_mod._tool_allowed({"echo": True}, "other"))

    def test_preview_arguments_compacts_and_truncates(self) -> None:
        self.assertEqual("{}", runner_mod._preview_arguments("   "))
        self.assertEqual('{"a":1}', runner_mod._preview_arguments('{ "a" : 1 }'))
        self.assertEqual("not json", runner_mod._preview_arguments("not json"))
        long = "a" * 200
        out = runner_mod._preview_arguments(long)
        self.assertEqual(runner_mod.MAX_PREVIEW_RUNES + 3, len(out))

    def test_join_assistant_text(self) -> None:
        self.assertEqual("b", runner_mod._join_assistant_text("", " b "))
        self.assertEqual("a", runner_mod._join_assistant_text(" a ", ""))
        self.assertEqual("a\n\nb", runner_mod._join_assistant_text("a", "b"))


class StartToolRequestContractTests(unittest.IsolatedAsyncioTestCase):
    """二审 §二：``start_tool_request`` 的三元组契约（协议 / 实现 / runner 三方一致）。

    历史缺陷：``runner.run()`` 按 4 项解包，而 ``RunnerDeps`` 协议声明与真实适配器
    :class:`atri_x.agent.toolrun_adapter.AgentToolRunDeps` 都返回 3 项 —— 真实接入后
    每次调用都在解包处抛 ``ValueError``，被宽泛的 ``except BaseException`` 记成
    "工具启动失败"，工具**永远执行不到**，已登记的 request 也因 ``done`` 丢失而永不
    结束。测试里的假实现当年同样返回 4 项，所以单测全绿也发现不了。
    """

    async def test_adapter_returns_trio_and_done_reaches_requests(self) -> None:
        from atri_x.agent.toolrun_adapter import AgentToolRunDeps

        closed: list[str] = []

        class _Request:
            id = "req-1"
            started_at = None

        class _Requests:
            def start(self, ctx, start_request):
                self.start_request = start_request
                return _Request(), ctx, lambda: closed.append("done")

        requests = _Requests()

        class _Agent:
            tool_timeout = 5

        agent = _Agent()
        agent.requests = requests  # type: ignore[attr-defined]

        class _Output:
            snapshots: list = []

            def publish_runtime_status(self, ctx, snapshot):
                _Output.snapshots.append(snapshot)

        deps = AgentToolRunDeps(agent=agent, output=_Output())
        trio = await deps.start_tool_request("ctx", "s1", "echo")
        self.assertEqual(3, len(trio))
        run_ctx, started_at, done = trio
        self.assertEqual("ctx", run_ctx)
        self.assertIsNotNone(started_at)
        self.assertTrue(callable(done))
        done()
        self.assertEqual(["done"], closed)
        self.assertEqual("echo", requests.start_request.label)

    async def test_adapter_degrades_to_noop_without_requests(self) -> None:
        from atri_x.agent.toolrun_adapter import AgentToolRunDeps

        deps = AgentToolRunDeps(agent=object(), output=None)
        run_ctx, started_at, done = await deps.start_tool_request("ctx", "s1", "echo")
        self.assertEqual("ctx", run_ctx)
        self.assertIsNotNone(started_at)
        self.assertIsNone(done())  # 没有登记请求 → 空操作，不是失败

    async def test_runner_rejects_four_item_implementation(self) -> None:
        """旧的四元组实现必须失败（契约是三元组），不能被静默接受。"""

        class FourItemDeps(FakeDeps):
            async def start_tool_request(self, ctx, session_id, tool_name):
                self.calls.append("start_tool_request")
                return ctx, None, (lambda: self.calls.append("done")), None

        deps = FourItemDeps()
        tool = FakeToolDef(name="echo")
        manager = Manager(registry=FakeRegistry([tool]))
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="echo", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=_allow_authorize,
        )
        result = await runner_mod.run(manager, None, deps, req)
        self.assertIn("failed", segments_content_text(result.messages[0].segments))
        self.assertEqual([], tool.calls)

    def test_protocol_and_adapter_declare_exactly_three(self) -> None:
        import inspect
        import typing

        from atri_x.agent.toolrun_adapter import AgentToolRunDeps

        hints = typing.get_type_hints(runner_mod.RunnerDeps.start_tool_request)
        self.assertEqual(3, len(typing.get_args(hints["return"])))
        adapter_hints = typing.get_type_hints(AgentToolRunDeps.start_tool_request)
        self.assertEqual(3, len(typing.get_args(adapter_hints["return"])))
        self.assertTrue(inspect.iscoroutinefunction(AgentToolRunDeps.start_tool_request))


class StartToolRequestLifecycleTests(unittest.TestCase):
    """``done()`` 的调用责任：登记成功后恰好一次，任何失败/取消路径都释放。"""

    def setUp(self) -> None:
        self.tool = FakeToolDef(name="echo")
        self.manager = Manager(registry=FakeRegistry([self.tool]))

    def _run(self, deps, **kwargs):
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="echo", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=kwargs.pop("authorize_tool", _allow_authorize),
            **kwargs,
        )
        return run(runner_mod.run(self.manager, None, deps, req))

    def test_done_called_exactly_once_on_success(self) -> None:
        deps = FakeDeps()
        self._run(deps)
        self.assertEqual(1, deps.calls.count("done"))

    def test_start_failure_reports_and_does_not_release(self) -> None:
        deps = FakeDeps(start_error=RuntimeError("no slot"))
        result = self._run(deps)
        self.assertIn("no slot", segments_content_text(result.messages[0].segments))
        self.assertEqual([], self.tool.calls)
        # 没有登记成功的 request，就不该去调 done()
        self.assertEqual(0, deps.calls.count("done"))

    def test_done_called_once_when_tool_raises(self) -> None:
        class BoomTool(FakeToolDef):
            async def call(self, ctx, request):
                raise RuntimeError("kaboom")

        manager = Manager(registry=FakeRegistry([BoomTool(name="boom")]))
        deps = FakeDeps()
        req = RunRequest(
            session=make_session(),
            calls=[ToolCallRequest(id="1", name="boom", arguments="{}")],
            actor=Actor(role=RoleSuperadmin),
            authorize_tool=_allow_authorize,
        )
        run(runner_mod.run(manager, None, deps, req))
        self.assertEqual(1, deps.calls.count("done"))

    def test_done_called_once_when_reauthorize_denies(self) -> None:
        state = {"n": 0}

        def authorize(ctx, call, resolved):
            state["n"] += 1
            return (True, "") if state["n"] == 1 else (False, "revoked")

        deps = FakeDeps()
        self._run(deps, authorize_tool=authorize)
        self.assertEqual(1, deps.calls.count("done"))
        self.assertEqual([], self.tool.calls)

    def test_done_called_once_when_cancelled_mid_flight(self) -> None:
        deps = FakeDeps(complete_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            self._run(deps)
        self.assertEqual(1, deps.calls.count("done"))

    def test_cancelled_prepare_propagates_instead_of_tool_message(self) -> None:
        """取消不是"工具失败"：不能变成 tool 消息后继续跑后面的调用。"""
        deps = FakeDeps(prepare_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            self._run(deps)
        self.assertEqual([], self.tool.calls)
        self.assertEqual(0, deps.calls.count("start_tool_request"))


if __name__ == "__main__":
    unittest.main()
