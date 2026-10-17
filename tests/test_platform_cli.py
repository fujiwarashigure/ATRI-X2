"""``atri_x.platform.cli`` 的配置 / 适配器 / 本地文件引用 / 线上协议测试。

对应 Go 测试：``internal/platform/cli/config_test.go``、
``internal/platform/cli/cli_output_test.go``（适配器出站与 stdin 循环），
以及 ``tui_file_reference.go`` 的补全与展开行为。

标准库 ``unittest``；临时目录一律建在仓库根下并在 ``tearDown`` 里删掉
（PORTING §7.2：不要用 ``tempfile.mkdtemp``）。
"""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.agent.status import publish_runtime_status
from atri_x.agent.turn_output import ForegroundTurnOutput, start_message_stream
from atri_x.completion.completion import Item, Request as CompletionRequest
from atri_x.delivery.delivery import (
    Kind,
    MessageStream,
    Notice,
    Output,
    Source,
    Target,
    text as text_output,
)
from atri_x.errors import InvalidError
from atri_x.platform.cli.adapter import Adapter, CliMessageStream, outputs_text
from atri_x.platform.cli.config import (
    DEFAULT_REMOTE_ADDR,
    DEFAULT_REMOTE_PATH,
    ClientConfig,
    Config,
    ServerConfig,
    apply_config_defaults,
    default_config,
    new_config_from_platform_config,
)
from atri_x.platform.cli.local_file import (
    KIND_LOCAL_FILE_REFERENCE,
    LocalFileResolver,
    format_local_file_expansion,
    format_local_file_reference,
    local_file_fuzzy_score,
    new_local_file_resolver,
    parse_local_file_completion_token,
    parse_local_file_reference_at,
    subsequence_score,
    to_slash,
)
from atri_x.platform.cli.protocol import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    ZERO_TIME_TEXT,
    RemoteMessage,
    format_rfc3339_nano,
    item_from_json,
    item_to_json,
    level_from_text,
    level_text,
    output_message,
    parse_rfc3339_nano,
    remote_notice_level,
    remote_notice_message,
    set_from_strings,
    snapshot_from_json,
    snapshot_to_json,
    usage_from_json,
    usage_to_json,
)
from atri_x.runtime.status import Snapshot
from atri_x.safecontext import CancelToken

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_cli_test")


class FakeCtx:
    """鸭子类型 ctx：``error`` + ``add_callback``（对应 :class:`CancelToken` 的子集）。"""

    def __init__(self) -> None:
        self.err: BaseException | None = None
        self.callbacks: list = []

    def error(self) -> BaseException | None:
        return self.err

    def add_callback(self, callback) -> object:
        self.callbacks.append(callback)

        def remove() -> None:
            if callback in self.callbacks:
                self.callbacks.remove(callback)

        return remove

    def cancel(self, err: BaseException) -> None:
        self.err = err
        for callback in list(self.callbacks):
            callback()


class RecordingHandler:
    """记录 ``handle_message`` 的入参。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error

    async def handle_message(self, ctx, value: str) -> None:
        self.calls.append((ctx, value))
        if self.error is not None:
            raise self.error


class RecordingProgram:
    """记录 ``send`` 的 program 桩。"""

    def __init__(self) -> None:
        self.messages: list = []

    def send(self, msg) -> None:
        self.messages.append(msg)


class FakeAgent:
    """只提供 ``platform`` 的 agent 桩（agent 侧同步调用点的取值路径）。"""

    def __init__(self, platform) -> None:
        self.platform = platform


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestCliConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = new_config_from_platform_config(None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.default_client, "local")
        self.assertEqual(cfg.server.listen, DEFAULT_REMOTE_ADDR)
        self.assertFalse(cfg.server.enabled)
        self.assertEqual(cfg.default_url, f"ws://{DEFAULT_REMOTE_ADDR}{DEFAULT_REMOTE_PATH}")
        self.assertEqual(cfg.server.tokens, {})
        self.assertEqual(cfg.clients, {})

    def test_nested_mappings_become_dataclasses(self) -> None:
        cfg = new_config_from_platform_config(
            {
                "enabled": False,
                "default_client": "box",
                "server": {
                    "enabled": True,
                    "listen": "0.0.0.0:9000",
                    "tokens": {"box": ["CLI_TOKEN", "CLI_TOKEN_ALT"]},
                },
                "clients": {"box": {"url": "ws://example.test/cli/v1/ws", "token_env": "CLI_TOKEN"}},
            }
        )
        self.assertFalse(cfg.enabled)
        self.assertIsInstance(cfg.server, ServerConfig)
        self.assertEqual(cfg.server.tokens, {"box": ["CLI_TOKEN", "CLI_TOKEN_ALT"]})
        self.assertIsInstance(cfg.clients["box"], ClientConfig)
        # token_env 是 []string，单值字符串按 Go 的解码失败处理不了；Python 侧归一成单元素列表。
        self.assertEqual(cfg.clients["box"].token_env, ["CLI_TOKEN"])
        # 档案名兜底成 id。
        self.assertEqual(cfg.clients["box"].id, "box")
        # default_url 未显式给出时保持默认（Go ``applyConfigDefaults`` 只在为空时覆盖），
        # 档案自己的 url 缺省时才取 default_url。
        self.assertEqual(cfg.default_url, f"ws://{DEFAULT_REMOTE_ADDR}{DEFAULT_REMOTE_PATH}")
        self.assertEqual(cfg.clients["box"].url, "ws://example.test/cli/v1/ws")

    def test_client_lookup(self) -> None:
        cfg = new_config_from_platform_config({"clients": {"local": {"id": "local"}}})
        name, client = cfg.client("")
        self.assertEqual(name, "local")
        self.assertEqual(client.id, "local")
        # url 缺省时取 default_url。
        self.assertEqual(client.url, cfg.default_url)

    def test_client_lookup_single_profile_fallback(self) -> None:
        cfg = Config(clients={"only": ClientConfig()})
        cfg.default_client = ""
        name, client = cfg.client("")
        self.assertEqual(name, "only")
        self.assertEqual(client.id, "only")

    def test_client_lookup_errors(self) -> None:
        cfg = new_config_from_platform_config(None)
        # 空名字落到 default_client（"local"），配置里没有该档案。
        with self.assertRaises(InvalidError) as ctx:
            cfg.client("")
        self.assertIn('cli client "local" is not configured', str(ctx.exception))
        # 显式指定不存在的档案。
        cfg.default_client = ""
        with self.assertRaises(InvalidError) as ctx:
            cfg.client("")
        self.assertIn("cli client is not configured", str(ctx.exception))
        with self.assertRaises(InvalidError) as ctx:
            cfg.client("missing")
        self.assertIn("is not configured", str(ctx.exception))

    def test_apply_defaults_keeps_explicit_values(self) -> None:
        cfg = Config(
            default_client="",
            default_url="",
            server=ServerConfig(listen="", tokens=None),
            clients=None,
        )
        apply_config_defaults(cfg)
        self.assertEqual(cfg.default_client, "local")
        self.assertEqual(cfg.server.listen, DEFAULT_REMOTE_ADDR)
        self.assertEqual(cfg.server.tokens, {})
        self.assertEqual(cfg.clients, {})

    def test_default_config_is_a_fresh_object(self) -> None:
        first = default_config()
        second = default_config()
        first.server.tokens["x"] = ["Y"]
        self.assertEqual(second.server.tokens, {})


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------

class TestCliAdapter(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self, stdin: str = "", interactive: bool = False) -> tuple[Adapter, io.StringIO, io.StringIO]:
        out, err = io.StringIO(), io.StringIO()
        adapter = Adapter(
            input_stream=io.StringIO(stdin),
            output_stream=out,
            error_stream=err,
            interactive=interactive,
        )
        return adapter, out, err

    def test_name_and_flags(self) -> None:
        adapter, _, _ = self.make_adapter()
        self.assertEqual(adapter.name(), "cli")
        self.assertTrue(adapter.stop_app_on_exit())
        self.assertFalse(adapter.is_interactive())

    async def test_run_scanner_forwards_and_stops_on_exit(self) -> None:
        adapter, out, _ = self.make_adapter("hello\n/exit\nnever\n")
        handler = RecordingHandler()
        await adapter.run(None, handler)
        self.assertEqual([call[1] for call in handler.calls], ["hello"])
        self.assertIn("> ", out.getvalue())

    async def test_run_scanner_keeps_going_at_eof(self) -> None:
        adapter, _, _ = self.make_adapter("a\nb")
        handler = RecordingHandler()
        await adapter.run(None, handler)
        self.assertEqual([call[1] for call in handler.calls], ["a", "b"])

    async def test_run_scanner_reports_handler_error_on_stderr(self) -> None:
        adapter, _, err = self.make_adapter("boom\n")
        await adapter.run(None, RecordingHandler(error=RuntimeError("nope")))
        self.assertIn("error: nope", err.getvalue())

    async def test_run_scanner_raises_when_ctx_cancelled(self) -> None:
        adapter, _, _ = self.make_adapter("x\n")
        ctx = FakeCtx()
        ctx.cancel(InvalidError("cancelled"))
        with self.assertRaises(InvalidError):
            await adapter.run(ctx, RecordingHandler())

    async def test_send_chat_prints_fallback_text(self) -> None:
        adapter, out, _ = self.make_adapter()
        receipt = await adapter.send_chat(None, [text_output("hi"), text_output("there")])
        self.assertEqual(receipt.platform_message_ids, [])
        self.assertIn("hi\nthere", out.getvalue())

    async def test_send_chat_to_program(self) -> None:
        adapter, out, _ = self.make_adapter(interactive=True)
        program = RecordingProgram()
        adapter.set_program(program)
        await adapter.send_chat(None, [text_output("hi")])
        self.assertEqual(len(program.messages), 1)
        self.assertEqual(program.messages[0].text, "hi")
        self.assertEqual(out.getvalue(), "")

    async def test_send_notice_rejects_other_platform(self) -> None:
        adapter, _, _ = self.make_adapter()
        notice = Notice(target=Target(platform="telegram"), outputs=[text_output("x")])
        with self.assertRaises(InvalidError) as ctx:
            await adapter.send_notice(None, notice)
        self.assertIn("cli cannot send to platform", str(ctx.exception))

    async def test_send_notice_prefixes_fallback(self) -> None:
        adapter, out, _ = self.make_adapter()
        await adapter.send_notice(
            None, Notice(outputs=[text_output("careful")], level=LEVEL_WARN)
        )
        self.assertIn("[notice] careful", out.getvalue())

    async def test_send_tool_notice(self) -> None:
        adapter, out, _ = self.make_adapter()
        await adapter.send_tool_notice("  running tool  ")
        self.assertIn("[tool] running tool", out.getvalue())
        await adapter.send_tool_notice("   ")
        self.assertEqual(out.getvalue().count("[tool]"), 1)

    async def test_send_reasoning_and_status(self) -> None:
        adapter, out, _ = self.make_adapter()
        await adapter.send_reasoning(None, "")
        self.assertEqual(out.getvalue(), "")
        await adapter.send_reasoning(None, "thinking hard")
        self.assertIn("thinking hard", out.getvalue())

        snapshot = Snapshot(
            session_id="s1",
            phase="llm",
            provider="openai",
            model="gpt",
            stage_started_at=datetime.now(timezone.utc),
        )
        # 同步方法（Go ``SetRuntimeStatus`` 也是同步的）：不能 await。
        adapter.set_runtime_status(None, snapshot)
        self.assertIn("openai/gpt", out.getvalue())

    async def test_start_stream_requires_interactive_tui(self) -> None:
        adapter, _, _ = self.make_adapter()
        with self.assertRaises(InvalidError) as ctx:
            adapter.start_stream(None)
        self.assertIn("requires interactive TUI", str(ctx.exception))

        interactive, _, _ = self.make_adapter(interactive=True)
        with self.assertRaises(InvalidError) as ctx:
            interactive.start_stream(None)
        self.assertIn("cli TUI is not running", str(ctx.exception))

    async def test_message_stream_uses_program(self) -> None:
        adapter, _, _ = self.make_adapter(interactive=True)
        program = RecordingProgram()
        adapter.set_program(program)
        stream = adapter.start_stream(None)
        await stream.append(None, "")
        await stream.append(None, "partial")
        await stream.replace(None, "final")
        await stream.finish(None)
        kinds = [type(msg).__name__ for msg in program.messages]
        self.assertEqual(
            kinds,
            ["TuiOutputMsg", "TuiReplaceAssistantMsg", "TuiFinishAssistantMsg"],
        )

    async def test_send_tui_message_falls_back_when_queue_full(self) -> None:
        adapter, out, _ = self.make_adapter(interactive=True)
        for _ in range(adapter.output.maxsize):
            adapter.output.put_nowait(object())
        adapter.send_tui_message(object(), "fallback line")
        self.assertIn("fallback line", out.getvalue())

    async def test_connect_notifier_is_awaited(self) -> None:
        adapter, _, _ = self.make_adapter("")
        seen: list[str] = []

        async def notify(ctx, platform) -> None:
            seen.append(platform)

        adapter.set_connect_notifier(notify)
        await adapter.run(None, RecordingHandler())
        self.assertEqual(seen, ["cli"])

    def test_outputs_text(self) -> None:
        outputs = [
            Output(kind=Kind.TEXT, text="a"),
            Output(kind=Kind.IMAGE, name="pic.png", source=Source(path="/tmp/pic.png")),
        ]
        value = outputs_text(outputs)
        self.assertIn("a", value)
        self.assertIn("pic.png", value)

    def test_adapter_satisfies_platform_runtime_protocol(self) -> None:
        from atri_x.platform.platform import ConnectNotifier, Runtime

        adapter, _, _ = self.make_adapter()
        self.assertIsInstance(adapter, Runtime)
        self.assertIsInstance(adapter, ConnectNotifier)

    def test_builtin_local_contract(self) -> None:
        """``atri_x/platform/builtin.py`` 装配本地适配器时用到的那组入口。"""
        from atri_x.platform.cli import new_adapter

        adapter = new_adapter()
        self.assertEqual(adapter.name(), "cli")
        self.assertTrue(adapter.stop_app_on_exit())
        adapter.set_connect_notifier(lambda ctx, platform: None)
        adapter.set_completer(None)
        self.assertIsNone(adapter.program)

    def test_write_survives_unicode_encode_error(self) -> None:
        class GbkStream:
            encoding = "gbk"

            def __init__(self) -> None:
                self.buffer = ""

            def write(self, value: str) -> None:
                value.encode("gbk")
                self.buffer += value

            def flush(self) -> None:
                pass

        stream = GbkStream()
        adapter = Adapter(output_stream=stream, error_stream=stream, interactive=False)
        adapter.write(stream, "price € 10")
        self.assertIn("\\u20ac", stream.buffer)


# ---------------------------------------------------------------------------
# 同步契约：CLI 侧实现必须是同步的（Go 的 ``StartStream`` / ``SetRuntimeStatus``）
# ---------------------------------------------------------------------------

class TestCliSynchronousContract(unittest.IsolatedAsyncioTestCase):
    """锁住「CLI 侧返回真正的 ``MessageStream``，而不是协程」。

    真实调用路径是 :func:`atri_x.agent.turn_output.start_message_stream`（同步）与
    :func:`atri_x.agent.status.publish_runtime_status`（同步）——回归必须经由它们，
    否则「实现被写成 ``async def``、调用点没 await」这类缺陷会被模块级用例放过。
    """

    def make_agent(self, adapter: Adapter) -> FakeAgent:
        return FakeAgent(adapter)

    async def test_start_stream_is_sync_and_returns_message_stream(self) -> None:
        adapter = Adapter(interactive=True)
        program = RecordingProgram()
        adapter.set_program(program)
        self.assertFalse(inspect.iscoroutinefunction(adapter.start_stream))

        stream = adapter.start_stream(None)
        self.assertFalse(inspect.iscoroutine(stream))
        self.assertIsInstance(stream, CliMessageStream)
        self.assertIsInstance(stream, MessageStream)

    async def test_start_message_stream_path_returns_real_stream(self) -> None:
        adapter = Adapter(interactive=True)
        program = RecordingProgram()
        adapter.set_program(program)
        stream = start_message_stream(self.make_agent(adapter), None)
        self.assertIsNotNone(stream)
        self.assertFalse(inspect.iscoroutine(stream))
        self.assertIsInstance(stream, MessageStream)

        # 拿到真 stream 之后，三段异步调用（``replace`` / ``finish`` 就是 turn_output
        # 里的 await 形状）都能落到 TUI。
        await stream.append(None, "part")
        await stream.replace(None, "final")
        await stream.finish(None)
        self.assertEqual(
            [type(msg).__name__ for msg in program.messages],
            ["TuiOutputMsg", "TuiReplaceAssistantMsg", "TuiFinishAssistantMsg"],
        )

    async def test_turn_output_falls_back_when_stream_unsupported(self) -> None:
        """非交互 TUI 下 ``start_stream`` 抛异常，调用方退回整条发送（Go 的 nil）。"""
        adapter = Adapter(interactive=False)
        self.assertIsNone(start_message_stream(self.make_agent(adapter), None))
        output = ForegroundTurnOutput(self.make_agent(adapter))
        self.assertIsNone(output.start_stream(None))

    async def test_set_runtime_status_is_sync_and_reaches_tui(self) -> None:
        adapter = Adapter(interactive=True)
        program = RecordingProgram()
        adapter.set_program(program)
        self.assertFalse(inspect.iscoroutinefunction(adapter.set_runtime_status))

        result = adapter.set_runtime_status(None, Snapshot(session_id="s1", phase="llm"))
        self.assertIsNone(result)
        self.assertEqual([type(msg).__name__ for msg in program.messages], ["TuiStatusMsg"])

        # 经由 agent 侧的同步调用点：状态必须真的推送出去（不是协程被丢弃）。
        program.messages.clear()
        publish_runtime_status(
            self.make_agent(adapter),
            None,
            Snapshot(session_id="s1", phase="llm", provider="openai", model="gpt"),
        )
        self.assertEqual([type(msg).__name__ for msg in program.messages], ["TuiStatusMsg"])
        self.assertEqual(program.messages[0].snapshot.model, "gpt")


# ---------------------------------------------------------------------------
# local file references
# ---------------------------------------------------------------------------

class TestLocalFileResolver(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(os.path.join(TMP_DIR, "sub"), exist_ok=True)
        os.makedirs(os.path.join(TMP_DIR, ".git"), exist_ok=True)
        self.write("main.py", "print('hi')\n")
        self.write("sub/notes.txt", "hello notes\n")
        self.write("sub/big.txt", "x" * (300 * 1024))
        self.write("bin.dat", "abc\x00def")
        self.resolver = new_local_file_resolver(TMP_DIR)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def write(self, relative: str, content: str) -> None:
        path = os.path.join(TMP_DIR, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    # -- 片段解析 -----------------------------------------------------------
    def test_parse_unquoted_token(self) -> None:
        value = "look at #ma"
        token, ok = parse_local_file_completion_token(value, len(value))
        self.assertTrue(ok)
        self.assertEqual((token.start, token.end, token.query, token.quoted), (8, 11, "ma", False))

    def test_parse_quoted_token(self) -> None:
        value = 'open #"sub/no'
        token, ok = parse_local_file_completion_token(value, len(value))
        self.assertTrue(ok)
        self.assertTrue(token.quoted)
        self.assertEqual(token.query, "sub/no")

    def test_parse_rejects_non_boundary_and_missing_hash(self) -> None:
        _, ok = parse_local_file_completion_token("abc#de", 6)
        self.assertFalse(ok)
        _, ok = parse_local_file_completion_token("plain text", 10)
        self.assertFalse(ok)

    def test_format_reference_quotes_whitespace(self) -> None:
        self.assertEqual(format_local_file_reference("a/b.py", False), "#a/b.py")
        self.assertEqual(format_local_file_reference("a b.py", False), '#"a b.py"')
        self.assertEqual(format_local_file_reference("a/b.py", True), '#"a/b.py"')

    # -- 评分 ---------------------------------------------------------------
    def test_fuzzy_scores_are_ordered(self) -> None:
        exact, _ = local_file_fuzzy_score("main.py", "main.py")
        prefix, _ = local_file_fuzzy_score("main.py", "mai")
        sub, ok = local_file_fuzzy_score("sub/main.py", "mnpy")
        self.assertGreater(exact, prefix)
        self.assertTrue(ok)
        self.assertLess(sub, prefix)

    def test_fuzzy_score_empty_query(self) -> None:
        score, ok = local_file_fuzzy_score("main.py", "  ")
        self.assertTrue(ok)
        self.assertEqual(score, 1000 - len("main.py"))

    def test_subsequence_score(self) -> None:
        score, ok = subsequence_score("main.py", "mnp")
        self.assertTrue(ok)
        self.assertGreater(score, 0)
        _, ok = subsequence_score("main.py", "zzz")
        self.assertFalse(ok)

    # -- 补全 ---------------------------------------------------------------
    def test_complete_returns_items_with_replace_range(self) -> None:
        value = "#ma"
        items = self.resolver.complete_sync(None, CompletionRequest(text=value, cursor=len(value)))
        self.assertTrue(items)
        first = items[0]
        self.assertEqual(first.kind, KIND_LOCAL_FILE_REFERENCE)
        self.assertEqual(first.label, "main.py")
        self.assertEqual(first.text, "#main.py")
        self.assertEqual((first.replace_start, first.replace_end), (0, 3))

    def test_complete_skips_ignored_dirs(self) -> None:
        self.write(".git/config", "secret\n")
        items = self.resolver.complete_sync(None, CompletionRequest(text="#conf", cursor=5))
        self.assertEqual([item.label for item in items], [])

    def test_complete_coroutine_matches_sync(self) -> None:
        request = CompletionRequest(text="#su", cursor=3)
        items = asyncio.run(self.resolver.complete(None, request))
        self.assertEqual([item.label for item in items], ["sub/big.txt", "sub/notes.txt"])
    def test_complete_without_token_returns_empty(self) -> None:
        self.assertEqual(
            self.resolver.complete_sync(None, CompletionRequest(text="hello", cursor=5)), []
        )

    # -- 展开 ---------------------------------------------------------------
    def test_expand_existing_reference(self) -> None:
        expanded = self.resolver.expand_references("see #main.py now")
        self.assertIn("[file: main.py]", expanded)
        self.assertIn("```text\nprint('hi')\n```", expanded)
        self.assertTrue(expanded.startswith("see "))
        self.assertTrue(expanded.endswith("now"))

    def test_expand_missing_reference_keeps_original(self) -> None:
        text = "see #nope.txt now"
        self.assertEqual(self.resolver.expand_references(text), text)

    def test_expand_rejects_directory_and_escape(self) -> None:
        with self.assertRaises(InvalidError):
            self.resolver.expand_references("look #sub")
        with self.assertRaises(InvalidError):
            self.resolver.expand_references("look #../outside.txt")

    def test_expand_rejects_binary_and_oversize(self) -> None:
        with self.assertRaises(InvalidError) as ctx:
            self.resolver.expand_references("#bin.dat")
        self.assertIn("binary", str(ctx.exception))
        with self.assertRaises(InvalidError) as ctx:
            self.resolver.expand_references("#sub/big.txt")
        self.assertIn("too large", str(ctx.exception))

    def test_expand_rejects_unterminated_quote(self) -> None:
        with self.assertRaises(InvalidError) as ctx:
            self.resolver.expand_references('#"main.py')
        self.assertIn("unterminated", str(ctx.exception))

    def test_expand_multiple_references_tracks_total(self) -> None:
        resolver = LocalFileResolver(root=TMP_DIR, max_total=10)
        with self.assertRaises(InvalidError) as ctx:
            resolver.expand_references("#main.py\n#sub/notes.txt")
        self.assertIn("too large", str(ctx.exception))

    def test_parse_reference_at(self) -> None:
        path, end, ok = parse_local_file_reference_at("#main.py rest", 0)
        self.assertTrue(ok)
        self.assertEqual((path, end), ("main.py", 8))
        path, end, ok = parse_local_file_reference_at('#"a b" x', 0)
        self.assertTrue(ok)
        self.assertEqual((path, end), ("a b", 6))
        _, _, ok = parse_local_file_reference_at("x#main", 1)
        self.assertFalse(ok)

    def test_format_expansion_grows_fence(self) -> None:
        text = format_local_file_expansion("a.md", "before ``` after")
        self.assertIn("````text", text)

    def test_to_slash(self) -> None:
        self.assertEqual(to_slash("a/b"), "a/b")

    def test_resolver_without_root_uses_cwd(self) -> None:
        resolver = new_local_file_resolver("")
        self.assertTrue(os.path.isabs(resolver.root))


# ---------------------------------------------------------------------------
# remote protocol
# ---------------------------------------------------------------------------

class TestRemoteProtocol(unittest.TestCase):
    def test_message_round_trip_and_omitempty(self) -> None:
        msg = RemoteMessage(type="complete", id="c-1", text="he", cursor=2)
        raw = msg.to_json()
        self.assertEqual(raw["type"], "complete")
        self.assertEqual(raw["id"], "c-1")
        self.assertEqual(raw["cursor"], 2)
        # omitempty：零值字段不出现。
        for key in ("client_id", "token", "level", "items"):
            self.assertNotIn(key, raw)
        # omitempty 对 struct 无效：snapshot 永远出现。
        self.assertIn("snapshot", raw)

        back = RemoteMessage.from_json(raw)
        self.assertEqual(back.type, "complete")
        self.assertEqual(back.id, "c-1")
        self.assertEqual(back.cursor, 2)
        self.assertEqual(back.text, "he")

    def test_zero_cursor_and_empty_text_are_dropped(self) -> None:
        raw = RemoteMessage(type="input").to_json()
        self.assertNotIn("cursor", raw)
        self.assertNotIn("text", raw)

    def test_type_is_never_omitted(self) -> None:
        self.assertEqual(RemoteMessage().to_json(), {"type": "", "snapshot": snapshot_to_json(Snapshot())})

    def test_items_use_go_field_names(self) -> None:
        item = Item(
            text="#a", label="a", description="local file", kind="local_file",
            replace_start=1, replace_end=3,
        )
        raw = item_to_json(item)
        self.assertEqual(
            list(raw.keys()),
            ["Text", "Label", "Description", "Kind", "ReplaceStart", "ReplaceEnd"],
        )
        self.assertEqual(item_from_json(raw), item)

    def test_snapshot_json_keys_and_zero_time(self) -> None:
        snapshot = Snapshot(session_id="s", phase="llm", usage=None)
        raw = snapshot_to_json(snapshot)
        self.assertEqual(
            list(raw.keys()),
            [
                "SessionID", "Phase", "Provider", "Model", "Mode", "RequestID", "Kind",
                "Label", "ToolName", "TurnStartedAt", "StageStartedAt", "FinishedAt",
                "Usage", "Error",
            ],
        )
        self.assertEqual(raw["TurnStartedAt"], ZERO_TIME_TEXT)
        self.assertIsNone(raw["Usage"])
        # 空快照往返时零值时间还原成 None。
        back = snapshot_from_json(raw)
        self.assertIsNone(back.turn_started_at)
        self.assertEqual(back.session_id, "s")

    def test_usage_json(self) -> None:
        from atri_x.llm.llm import Usage

        usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cache_hit_tokens=4)
        raw = usage_to_json(usage)
        self.assertEqual(
            raw,
            {"PromptTokens": 1, "CompletionTokens": 2, "TotalTokens": 3, "CacheHitTokens": 4},
        )
        self.assertEqual(usage_from_json(raw), usage)
        self.assertIsNone(usage_from_json(None))

    def test_rfc3339_nano_round_trip(self) -> None:
        moment = datetime(2026, 10, 13, 12, 0, 0, 500000, tzinfo=timezone.utc)
        text = format_rfc3339_nano(moment)
        self.assertEqual(text, "2026-10-13T12:00:00.5Z")
        self.assertEqual(parse_rfc3339_nano(text), moment)
        self.assertIsNone(parse_rfc3339_nano(ZERO_TIME_TEXT))
        self.assertIsNone(parse_rfc3339_nano("not-a-time"))

        offset = datetime(2026, 10, 13, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(format_rfc3339_nano(offset), "2026-10-13T12:00:00Z")
        parsed = parse_rfc3339_nano("2026-10-13T20:00:00+08:00")
        self.assertEqual(parsed, datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc))

    def test_levels(self) -> None:
        self.assertEqual(level_text(LEVEL_INFO), "INFO")
        self.assertEqual(level_text(LEVEL_WARN), "WARN")
        self.assertEqual(level_text(LEVEL_ERROR), "ERROR")
        self.assertEqual(level_from_text("warn"), LEVEL_WARN)
        self.assertEqual(level_from_text(""), LEVEL_INFO)
        self.assertEqual(level_from_text("nonsense"), LEVEL_INFO)
        self.assertEqual(remote_notice_level(""), LEVEL_INFO)
        self.assertEqual(remote_notice_level("ERROR"), LEVEL_ERROR)

    def test_output_and_notice_messages(self) -> None:
        msg = output_message("chat", text_output("hello"))
        self.assertEqual((msg.type, msg.text), ("chat", "hello"))
        image = Output(kind=Kind.IMAGE, name="p.png", source=Source(path="/x/p.png"))
        self.assertIn("p.png", output_message("chat", image).text)

        notice = Notice(outputs=[text_output("careful")], level=LEVEL_WARN)
        level_msg = remote_notice_message(notice)
        self.assertEqual(level_msg.type, "notice")
        self.assertEqual(level_msg.level, "WARN")
        self.assertIn("careful", level_msg.text)

    def test_set_from_strings(self) -> None:
        self.assertEqual(set_from_strings(["cli:alice", " bob ", "", "cli:"]), {"alice", "bob"})
        self.assertEqual(set_from_strings(None), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
