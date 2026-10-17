"""``atri_x.hook.runtime`` 的单元测试。

对应上游 ``internal/hook/runtime/*.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atri_x.delivery.delivery import Receipt, Target
from atri_x.hook.event import Control, Event, Point
from atri_x.hook.runtime import (
    Config,
    Manager,
    Mode,
    Options,
    RouteKey,
    RuntimeConfigError,
    SharedState,
    SharedStateError,
    Status,
    Worker,
    resolve_cwd,
    route_key_for,
)
from atri_x.hook.runtime.routes import (
    MAX_TOOL_CALLS_PER_INVOCATION,
    put_tool_context,
    take_tool_context,
)
from atri_x.hook.runtime.shared import compact_json
from atri_x.hook.runtime.tool_bridge import ToolBridgeError, call_tool
from atri_x.llm.llm import MessageSegment
from atri_x.storage.models import Media

TMP = Path(__file__).resolve().parent.parent / "tmp_hook_runtime_test"

PLUGIN_BASIC = """
import json
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def read_frame():
    for line in sys.stdin:
        line = line.strip()
        if line:
            return json.loads(line)
    return None


while True:
    frame = read_frame()
    if frame is None:
        break
    if frame.get("type") != "request":
        continue
    method = frame.get("method")
    if method == "system.init":
        send({"type": "response", "id": frame["id"], "ok": True, "result": {}})
    elif method == "event.handle":
        # 反向请求：写共享状态，等宿主应答（验证双向交错）。
        send({"type": "request", "id": "plugin:1", "method": "shared.set",
              "params": {"key": "seen", "value": 1}})
        reply = read_frame()
        ok = bool(reply and reply.get("ok"))
        send({"type": "response", "id": frame["id"], "ok": True,
              "result": {"status": "completed",
                         "outputs": [{"kind": "text", "text": "from-plugin"}],
                         "message": {"text": "rewritten" if ok else "no-reply"}}})
    elif method == "system.shutdown":
        send({"type": "response", "id": frame["id"], "ok": True, "result": {}})
        break
"""

PLUGIN_WAIT = """
import json
import sys
from datetime import datetime, timedelta, timezone

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def read_frame():
    for line in sys.stdin:
        line = line.strip()
        if line:
            return json.loads(line)
    return None


while True:
    frame = read_frame()
    if frame is None:
        break
    if frame.get("type") != "request":
        continue
    method = frame.get("method")
    if method == "system.init":
        send({"type": "response", "id": frame["id"], "ok": True, "result": {}})
    elif method == "event.handle":
        params = json.loads(frame["params"]) if isinstance(frame.get("params"), str) else frame.get("params")
        if params and params.get("continuation"):
            # 有状态 Hook 的结果结构体里没有 consume/stop_propagation 字段，
            # 只有 pass_through（Go 的 eventResult 同样如此）。
            send({"type": "response", "id": frame["id"], "ok": True,
                  "result": {"status": "completed", "pass_through": False}})
        else:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            send({"type": "response", "id": frame["id"], "ok": True,
                  "result": {"status": "waiting", "conversation_id": "conv-1",
                             "expires_at": expires}})
    elif method == "system.shutdown":
        send({"type": "response", "id": frame["id"], "ok": True, "result": {}})
        break
"""


def run(coro):
    return asyncio.run(coro)


def make_event(text: str = "hello") -> Event:
    event = Event(id="e1", point=Point.PLATFORM_MESSAGE_RECEIVED)
    event.platform.name = "cli"
    event.platform.scope_id = "s1"
    event.actor.id = "cli:u1"
    event.actor.user_id = "u1"
    event.message.segments = [MessageSegment(type="text", text=text)]
    return event


def worker_config(
    ident: str = "demo",
    script: str = "plugin_basic.py",
    mode: str = Mode.PERSISTENT,
    **kw: object,
) -> Config:
    config = Config(
        mode=mode,
        command=[sys.executable, str(TMP / script)],
        cwd=".",
        startup_timeout_seconds=10,
        shutdown_timeout_seconds=10,
        event_timeout_seconds=10,
        max_wait_seconds=60,
        id=ident,
        dir=str(TMP),
        config_path=str(TMP / "hook.toml"),
    )
    config.restart.strategy = str(kw.pop("strategy", "never"))
    config.restart.initial_delay_seconds = 1
    config.restart.max_delay_seconds = 2
    for key, value in kw.items():
        setattr(config, key, value)
    return config


class TestConfig(unittest.TestCase):
    def test_validate_once_mode_skips_checks(self) -> None:
        Config().validate()

    def test_validate_worker(self) -> None:
        config = worker_config()
        config.validate()

    def test_invalid_id(self) -> None:
        config = worker_config(ident="Demo")
        with self.assertRaises(RuntimeConfigError):
            config.validate()

    def test_missing_command(self) -> None:
        config = worker_config()
        config.command = []
        with self.assertRaises(RuntimeConfigError):
            config.validate()

    def test_non_positive_timeouts(self) -> None:
        config = worker_config()
        config.event_timeout_seconds = 0
        with self.assertRaises(RuntimeConfigError):
            config.validate()

    def test_bad_restart_strategy(self) -> None:
        config = worker_config()
        config.restart.strategy = "sometimes"
        with self.assertRaises(RuntimeConfigError):
            config.validate()

    def test_restart_delay_order(self) -> None:
        config = worker_config()
        config.restart.initial_delay_seconds = 10
        config.restart.max_delay_seconds = 5
        with self.assertRaises(RuntimeConfigError):
            config.validate()

    def test_mode_helpers(self) -> None:
        self.assertEqual(Mode.ONCE, Config().mode_or_once())
        self.assertFalse(Config().is_worker())
        self.assertTrue(worker_config().is_worker())

    def test_resolve_cwd(self) -> None:
        self.assertTrue(resolve_cwd("/plugin", "sub").endswith(os.path.join("plugin", "sub")))
        with self.assertRaises(RuntimeConfigError):
            resolve_cwd("/plugin", "/abs")
        with self.assertRaises(RuntimeConfigError):
            resolve_cwd("/plugin", "../escape")


class TestSharedState(unittest.TestCase):
    def make(self) -> tuple[SharedState, list[datetime]]:
        clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        state = SharedState(now=lambda: clock[0])
        return state, clock

    def test_set_get_delete(self) -> None:
        state, _clock = self.make()
        state.set("k", '{"a":1}')
        value, ok = state.get("k")
        self.assertTrue(ok)
        self.assertEqual('{"a":1}', value)
        self.assertTrue(state.delete("k"))
        self.assertFalse(state.delete("k"))
        self.assertFalse(state.get("k")[1])

    def test_value_is_compacted(self) -> None:
        state, _clock = self.make()
        state.set("k", '{ "a" : 1 }')
        self.assertEqual('{"a":1}', state.get("k")[0])

    def test_invalid_json_rejected(self) -> None:
        state, _clock = self.make()
        with self.assertRaises(SharedStateError):
            state.set("k", "not-json")

    def test_empty_key_rejected(self) -> None:
        state, _clock = self.make()
        with self.assertRaises(SharedStateError):
            state.set("  ", "1")

    def test_key_too_long(self) -> None:
        state, _clock = self.make()
        with self.assertRaises(SharedStateError):
            state.set("k" * 257, "1")

    def test_ttl_expiry(self) -> None:
        state, clock = self.make()
        state.set_with_ttl("k", "1", 60.0)
        clock[0] += timedelta(seconds=59)
        self.assertTrue(state.get("k")[1])
        # get 会刷新 last_access（与 Go 一致），所以再往后推 ttl 才过期。
        clock[0] += timedelta(seconds=61)
        self.assertFalse(state.get("k")[1])
        self.assertEqual(0, state.prune_expired())

    def test_prune_expired_counts(self) -> None:
        state, clock = self.make()
        state.set_with_ttl("k", "1", 1.0)
        clock[0] += timedelta(seconds=5)
        self.assertEqual(1, state.prune_expired())

    def test_list_prefix_sorted(self) -> None:
        state, _clock = self.make()
        state.set("b", "1")
        state.set("a1", "1")
        state.set("a2", "1")
        self.assertEqual(["a1", "a2", "b"], state.list(""))
        self.assertEqual(["a1", "a2"], state.list("a"))

    def test_compare_and_swap(self) -> None:
        state, _clock = self.make()
        self.assertTrue(state.compare_and_swap("k", None, '"v1"'))
        self.assertFalse(state.compare_and_swap("k", None, '"v2"'))
        self.assertFalse(state.compare_and_swap("k", '"other"', '"v2"'))
        self.assertTrue(state.compare_and_swap("k", '"v1"', '"v2"'))
        self.assertEqual('"v2"', state.get("k")[0])

    def test_negative_ttl(self) -> None:
        state, _clock = self.make()
        with self.assertRaises(SharedStateError):
            state.set_with_ttl("k", "1", -1)

    def test_handle_request_round_trip(self) -> None:
        state, _clock = self.make()
        self.assertEqual({"ok": True}, state.handle_request("shared.set", '{"key":"k","value":{"a":1}}'))
        got = state.handle_request("shared.get", '{"key":"k"}')
        self.assertTrue(got["found"])
        self.assertEqual('{"a":1}', str(got["value"]))
        self.assertEqual({"swapped": False}, state.handle_request(
            "shared.compare_and_swap", '{"key":"k","expected":1,"value":2}'
        ))
        self.assertEqual({"swapped": True}, state.handle_request(
            "shared.compare_and_swap", '{"key":"k","expected":{"a":1},"value":2}'
        ))
        self.assertEqual({"deleted": True}, state.handle_request("shared.delete", '{"key":"k"}'))
        self.assertEqual({"keys": []}, state.handle_request("shared.list", "{}"))
        with self.assertRaises(SharedStateError):
            state.handle_request("shared.nope", "{}")

    def test_lru_eviction(self) -> None:
        state, clock = self.make()
        state._max_entries = 2
        state.set("a", "1")
        clock[0] += timedelta(seconds=1)
        state.set("b", "1")
        clock[0] += timedelta(seconds=1)
        state.set("c", "1")
        keys = state.list("")
        self.assertEqual(2, len(keys))
        self.assertNotIn("a", keys)

    def test_entry_too_large(self) -> None:
        state, _clock = self.make()
        state._max_size = 16
        with self.assertRaises(SharedStateError):
            state.set("k", '"' + "x" * 64 + '"')

    def test_compact_json_helper(self) -> None:
        self.assertEqual('{"a":1}', compact_json('{"a": 1}'))
        self.assertIsNone(compact_json("nope"))


class TestRoutes(unittest.TestCase):
    def test_route_key(self) -> None:
        key = route_key_for(make_event())
        self.assertEqual(RouteKey(platform="cli", scope_id="s1", actor_id="cli:u1"), key)

    def test_tool_context_exhaustion(self) -> None:
        manager = Manager(Options())
        token = put_tool_context(manager, "demo", None, make_event(), 60)
        for index in range(MAX_TOOL_CALLS_PER_INVOCATION):
            value = take_tool_context(manager, token, "demo")
            self.assertIsNotNone(value)
            self.assertEqual(index + 1, value.calls)
        self.assertIsNone(take_tool_context(manager, token, "demo"))

    def test_tool_context_wrong_hook(self) -> None:
        manager = Manager(Options())
        token = put_tool_context(manager, "demo", None, make_event(), 60)
        self.assertIsNone(take_tool_context(manager, token, "other"))

    def test_tool_context_expired(self) -> None:
        manager = Manager(Options())
        token = put_tool_context(manager, "demo", None, make_event(), 0)
        token_value = manager.tokens[token]
        token_value.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIsNone(take_tool_context(manager, token, "demo"))

    def test_set_lease_requires_conversation(self) -> None:
        manager = Manager(Options())
        manager.configs["demo"] = worker_config()

        class Result:
            status = "waiting"
            conversation_id = ""
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)

        from atri_x.hook.runtime.routes import set_lease

        with self.assertRaises(Exception):
            run(set_lease(manager, "demo", make_event(), Result(), Control()))

    def test_set_lease_and_clear(self) -> None:
        manager = Manager(Options())
        manager.configs["demo"] = worker_config()

        class Result:
            status = "waiting"
            conversation_id = "conv"
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)

        from atri_x.hook.runtime.routes import clear_lease, set_lease

        run(set_lease(manager, "demo", make_event(), Result(), Control(consume=True)))
        self.assertTrue(manager.has_lease(make_event()))
        self.assertEqual("demo", manager.route_hook_id(make_event()))
        clear_lease(manager, "demo", make_event())
        self.assertFalse(manager.has_lease(make_event()))

    def test_set_lease_beyond_max_wait(self) -> None:
        manager = Manager(Options())
        config = worker_config()
        config.max_wait_seconds = 1
        manager.configs["demo"] = config

        class Result:
            status = "waiting"
            conversation_id = "conv"
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)

        from atri_x.hook.runtime.routes import set_lease

        with self.assertRaises(Exception):
            run(set_lease(manager, "demo", make_event(), Result(), Control()))


class TestManagerValidation(unittest.TestCase):
    def test_apply_duplicate_ids(self) -> None:
        manager = Manager(Options())
        with self.assertRaises(Exception):
            run(manager.apply([worker_config(), worker_config()]))

    def test_unknown_tool_rejected(self) -> None:
        class Registry:
            def get(self, name: str):
                return None, False

        config = worker_config()
        config.tools.allow = ["nope"]
        manager = Manager(Options(registry=Registry()))
        with self.assertRaises(RuntimeConfigError):
            run(manager.apply([config]))

    def test_background_allow_must_be_allowed(self) -> None:
        class Registry:
            def get(self, name: str):
                return object(), True

        config = worker_config()
        config.tools.allow = ["a"]
        config.tools.background_allow = ["b"]
        manager = Manager(Options(registry=Registry()))
        with self.assertRaises(RuntimeConfigError):
            run(manager.apply([config]))

    def test_registry_required(self) -> None:
        config = worker_config()
        config.tools.allow = ["a"]
        manager = Manager(Options())
        with self.assertRaises(RuntimeConfigError):
            run(manager.apply([config]))

    def test_start_unknown_worker(self) -> None:
        manager = Manager(Options())
        with self.assertRaises(Exception):
            run(manager.start("nope"))


class TestWorkerLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        (TMP / "plugin_basic.py").write_text(PLUGIN_BASIC, encoding="utf-8", newline="\n")
        (TMP / "plugin_wait.py").write_text(PLUGIN_WAIT, encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    async def _scenario_basic(self) -> None:
        manager = Manager(Options())
        try:
            await manager.apply([worker_config()])
            await manager.start("demo")
            infos = manager.list()
            self.assertEqual(1, len(infos))
            self.assertEqual(Status.READY, infos[0].status)
            self.assertEqual(Mode.PERSISTENT, infos[0].mode)

            event = await manager.handle(None, "demo", make_event(), Control())
            self.assertEqual("rewritten", event.message.segments[0].text)
            self.assertEqual(1, len(event.outputs))
            self.assertEqual("from-plugin", event.outputs[0].text)
            self.assertTrue(event.metadata.get("hook.runtime.dispatched"))
            # 插件通过 shared.set 写进去的状态确实落在共享存储里。
            value, ok = manager.shared.get("seen")
            self.assertTrue(ok)
            self.assertEqual("1", value)
        finally:
            await manager.close()

    def test_persistent_roundtrip(self) -> None:
        run(self._scenario_basic())

    async def _scenario_waiting(self) -> None:
        manager = Manager(Options())
        try:
            await manager.apply([worker_config(script="plugin_wait.py")])
            await manager.start("demo")
            event = await manager.handle(None, "demo", make_event(), Control())
            self.assertTrue(manager.has_lease(event))
            self.assertEqual(1, manager.list()[0].waiting)
            # 续跑用的是**新事件**（同一路由键），对应应用侧"下一条消息命中租约"。
            routed, handled = await manager.route(None, make_event("next"))
            self.assertTrue(handled)
            self.assertTrue(routed.control.consume)
            self.assertFalse(manager.has_lease(make_event()))
        finally:
            await manager.close()

    def test_waiting_lease_and_route(self) -> None:
        run(self._scenario_waiting())

    async def _scenario_transient(self) -> None:
        manager = Manager(Options())
        try:
            await manager.apply([worker_config(mode=Mode.TRANSIENT)])
            event = await manager.handle(None, "demo", make_event(), Control())
            self.assertEqual("rewritten", event.message.segments[0].text)
            infos = manager.list()
            self.assertEqual(1, len(infos))
            self.assertEqual(Mode.TRANSIENT, infos[0].mode)
            # 瞬态 worker 的回收是后台任务（Go 同样是 ``go worker.stop``）。
            await asyncio.sleep(0.2)
            self.assertEqual(0, len(manager.transient))
            self.assertTrue(await manager.stop(None, "demo"))
            with self.assertRaises(Exception):
                await manager.stop(None, "ghost")
        finally:
            await manager.close()

    def test_transient_worker(self) -> None:
        run(self._scenario_transient())

    async def _scenario_replace(self) -> None:
        manager = Manager(Options())
        try:
            await manager.apply([worker_config()])
            await manager.start("demo")
            await manager.replace_plugin(worker_config())
            await manager.stop(None, "demo")
            self.assertEqual(Status.STOPPED, manager.list()[0].status)
        finally:
            await manager.close()

    def test_replace_plugin(self) -> None:
        run(self._scenario_replace())

    async def _scenario_validate_plugin(self) -> None:
        manager = Manager(Options())
        try:
            await manager.apply([worker_config()])
            changed = worker_config()
            changed.dir = str(TMP / "other")
            with self.assertRaises(Exception):
                await manager.validate_plugin(changed)
        finally:
            await manager.close()

    def test_validate_plugin_rejects_path_change(self) -> None:
        run(self._scenario_validate_plugin())

    async def _scenario_missing_binary(self) -> None:
        manager = Manager(Options())
        try:
            config = worker_config()
            config.command = ["definitely-not-a-real-binary-xyz"]
            await manager.apply([config])
            with self.assertRaises(Exception):
                await manager.start("demo")
            self.assertEqual(Status.FAILED, manager.list()[0].status)
        finally:
            await manager.close()

    def test_missing_binary_fails_start(self) -> None:
        run(self._scenario_missing_binary())


class FakeMediaAPI:
    """只实现 bridge 用到的那几个方法。"""

    def __init__(self) -> None:
        self.data = b"hello"
        self.refs: list[object] = []
        self.removed: list[object] = []
        self.imported: list[bytes] = []
        self.exports: list[str] = []
        self.url = ""

    def _media(self, name: str = "m", mime: str = "text/plain", size: int = 0) -> Media:
        return Media(
            id="media:" + "a" * 64,
            name=name,
            mime_type=mime,
            size=size,
        )

    async def import_bytes(self, ctx, data, spec):
        self.imported.append(data)
        return self._media(spec.name or "m", spec.mime_type or "text/plain", len(data))

    async def import_url(self, ctx, url, spec):
        self.url = url
        return self._media(spec.name or "u", spec.mime_type or "text/plain", 5)

    async def import_file(self, ctx, path, spec):
        return self._media(spec.name or "f", spec.mime_type or "text/plain", 5)

    async def import_reader(self, ctx, reader, size, spec):
        return self._media(spec.name or "r", spec.mime_type or "text/plain", size)

    async def open(self, ctx, media_id):
        return None, self._media()

    async def read(self, ctx, media_id):
        return self.data, self._media(size=len(self.data))

    async def export(self, ctx, media_id, destination):
        self.exports.append(destination)
        with open(destination, "wb") as handle:
            handle.write(self.data)
        return self._media()

    async def metadata(self, ctx, media_id):
        return self._media(size=len(self.data))

    async def resolve_for_llm(self, ctx, messages):
        return messages, lambda: None

    async def resolve_for_output(self, ctx, media_id):
        return None, lambda: None

    def add_reference(self, reference) -> None:
        self.refs.append(reference)

    def remove_reference(self, reference) -> None:
        self.removed.append(reference)


class TestMediaBridge(unittest.TestCase):
    def test_import_read_export_prune(self) -> None:
        async def scenario() -> None:
            api = FakeMediaAPI()
            manager = Manager(Options(media=api))
            try:
                result = await manager.media_request(
                    None, str(TMP), "media.import", '{"base64":"aGVsbG8=","name":"a.txt"}'
                )
                self.assertEqual(b"hello", api.imported[0])
                data = result.to_json()
                self.assertEqual("media:" + "a" * 64, data["media"])
                self.assertEqual("a.txt", data["name"])

                read = await manager.media_request(
                    None, str(TMP), "media.read", '{"media":"media:' + "a" * 64 + '"}'
                )
                self.assertEqual("aGVsbG8=", read.to_json()["base64"])
                self.assertEqual(1, len(api.refs))

                meta = await manager.media_request(
                    None, str(TMP), "media.metadata", '{"media":"media:' + "a" * 64 + '"}'
                )
                self.assertEqual(5, meta.to_json()["size"])

                await manager.media.prune(None, close_all=True)
                self.assertEqual(1, len(api.removed))
                self.assertTrue(manager.media.closed)
            finally:
                await manager.close()

        run(scenario())

    def test_import_url_and_path_validation(self) -> None:
        async def scenario() -> None:
            api = FakeMediaAPI()
            manager = Manager(Options(media=api))
            try:
                await manager.media_request(
                    None, str(TMP), "media.import", '{"url":"https://a/b.png"}'
                )
                self.assertEqual("https://a/b.png", api.url)
                with self.assertRaises(Exception):
                    await manager.media_request(
                        None, str(TMP), "media.import", '{"path":"../escape"}'
                    )
                with self.assertRaises(Exception):
                    await manager.media_request(
                        None, str(TMP), "media.import", '{"url":"https://a","path":"b"}'
                    )
                with self.assertRaises(Exception):
                    await manager.media_request(None, str(TMP), "media.nope", "{}")
            finally:
                await manager.close()

        run(scenario())

    def test_media_requires_api(self) -> None:
        manager = Manager(Options())
        with self.assertRaises(Exception):
            run(manager.media_request(None, "", "media.metadata", "{}"))


class FakeTool:
    def __init__(self, foreground_only: bool = False) -> None:
        self.calls: list[object] = []
        self._foreground_only = foreground_only

    def name(self) -> str:
        return "echo"

    def info(self):
        from atri_x.tool.tool import Info

        return Info(name="echo", foreground_only=self._foreground_only)

    def schema(self):
        from atri_x.llm.llm import ToolFunctionSchema, ToolSchema

        return ToolSchema(function=ToolFunctionSchema(name="echo"))

    async def call(self, ctx, req):
        from atri_x.security import actor_from_context
        from atri_x.tool.tool import Result

        actor, ok = actor_from_context(ctx)
        self.calls.append((req.arguments, actor.id if ok else ""))
        return Result(content="ok:" + req.arguments)


class FakeRegistry:
    def __init__(self, tool: FakeTool) -> None:
        self.tool = tool

    def get(self, name: str):
        if name == "echo":
            return self.tool, True
        return None, False


class TestToolBridge(unittest.TestCase):
    def make_worker(self, tool: FakeTool, background_allow: list[str] | None = None) -> Worker:
        config = worker_config()
        config.tools.allow = ["echo"]
        config.tools.background_allow = list(background_allow or [])
        sent: list[object] = []

        async def send(ctx, target, outputs):
            sent.append((target, outputs))
            return Receipt(platform_message_ids=["m1"])

        manager = Manager(Options(registry=FakeRegistry(tool), send=send))
        manager.configs["demo"] = config
        return Worker(manager, config)

    def test_call_with_tool_context(self) -> None:
        async def scenario() -> None:
            tool = FakeTool()
            worker = self.make_worker(tool)
            event = make_event()
            token = put_tool_context(worker.manager, "demo", None, event, 60)
            result = await call_tool(
                worker,
                json.dumps(
                    {"name": "echo", "arguments": '{"a":1}', "tool_context": token}
                ),
            )
            self.assertEqual("ok:{\"a\":1}", result["content"])
            self.assertEqual("cli:u1", tool.calls[0][1])
            self.assertEqual(["m1"], result["receipts"][0]["PlatformMessageIDs"])

        run(scenario())

    def test_call_without_context_uses_superadmin(self) -> None:
        async def scenario() -> None:
            tool = FakeTool()
            worker = self.make_worker(tool)
            with self.assertRaises(ToolBridgeError) as ctx:
                await call_tool(worker, json.dumps({"name": "echo", "arguments": "{}"}))
            self.assertIn("invalid, expired or exhausted", str(ctx.exception))

        run(scenario())

    def test_not_allowed(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker(FakeTool())
            with self.assertRaises(ToolBridgeError) as ctx:
                await call_tool(worker, json.dumps({"name": "nope"}))
            self.assertIn("not allowed", str(ctx.exception))

        run(scenario())

    def test_background_requires_target(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker(FakeTool(), background_allow=["echo"])
            with self.assertRaises(ToolBridgeError) as ctx:
                await call_tool(
                    worker, json.dumps({"name": "echo", "background": True})
                )
            self.assertIn("explicit target", str(ctx.exception))
            result = await call_tool(
                worker,
                json.dumps(
                    {
                        "name": "echo",
                        "arguments": "{}",
                        "background": True,
                        "target": {"platform": "cli", "scope_id": "s9"},
                    }
                ),
            )
            self.assertEqual("ok:{}", result["content"])

        run(scenario())

    def test_foreground_only_rejected_for_background(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker(FakeTool(foreground_only=True), ["echo"])
            with self.assertRaises(ToolBridgeError) as ctx:
                await call_tool(
                    worker,
                    json.dumps(
                        {
                            "name": "echo",
                            "background": True,
                            "target": {"platform": "cli", "scope_id": "s"},
                        }
                    ),
                )
            self.assertIn("foreground-only", str(ctx.exception))

        run(scenario())

    def test_missing_name(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker(FakeTool())
            with self.assertRaises(ToolBridgeError):
                await call_tool(worker, "{}")

        run(scenario())

    def test_response_ok_false_becomes_error(self) -> None:
        async def scenario() -> None:
            worker = self.make_worker(FakeTool())
            worker.manager.configs["demo"] = worker.config
            result, error = await worker.plugin_request(
                _frame("request", "plugin:1", "nope", "{}")
            )
            self.assertIsNone(result)
            self.assertIsNotNone(error)
            self.assertIn("unsupported hook request method", str(error))

            result, error = await worker.plugin_request(
                _frame("request", "plugin:2", "hooks.reload", '{"a":1}')
            )
            self.assertIsNone(result)
            self.assertIn("does not accept parameters", str(error))

            result, error = await worker.plugin_request(
                _frame("request", "plugin:3", "shared.list", "{}")
            )
            self.assertIsNone(error)
            self.assertEqual({"keys": []}, result)

        run(scenario())


def _frame(kind: str, ident: str, method: str, params: str):
    from atri_x.hook.protocol.frames import Frame

    return Frame(type=kind, id=ident, method=method, params=params)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
