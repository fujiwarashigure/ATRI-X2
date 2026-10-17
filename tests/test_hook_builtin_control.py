"""``atri_x.hook.control`` 与 ``atri_x.hook.builtin`` 的单元测试。

对应上游 ``internal/hook/control/service.go`` 与 ``internal/hook/builtin/*.go``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from pathlib import Path

from atri_x.hook.builtin import (
    Options,
    RegisterAll,
    record_outbound_message,
    register_angel_memory,
    register_self_learning,
)
from atri_x.hook.control import ControlError, New
from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.manager import DefaultManager, ReloadReport, Registration
from atri_x.hook.match import always
from atri_x.hook.runtime import Config as RuntimeConfig
from atri_x.llm.llm import MessageSegment

TMP = Path(__file__).resolve().parent.parent / "tmp_hook_builtin_test"


def run(coro):
    return asyncio.run(coro)


class Registrar:
    def __init__(self) -> None:
        self.registrations: list[Registration] = []

    def register(self, reg: Registration) -> None:
        self.registrations.append(reg)


class FakeRuntime:
    def __init__(self) -> None:
        self.applied: list[list[object]] = []
        self.validated: list[object] = []
        self.replaced: list[object] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.restarted: list[str] = []
        self.items: list[object] = []
        self.fail_stop = False

    async def apply(self, configs) -> None:
        self.applied.append(list(configs))

    async def validate_plugin(self, config) -> None:
        self.validated.append(config)

    async def replace_plugin(self, config) -> None:
        self.replaced.append(config)

    def list(self):
        return list(self.items)

    async def start(self, id: str) -> None:
        self.started.append(id)

    async def stop(self, ctx, id: str) -> bool:
        if self.fail_stop:
            raise ControlError("runtime stop failed")
        self.stopped.append(id)
        return True

    async def restart(self, ctx, id: str) -> None:
        self.restarted.append(id)


def make_event(point: str = Point.PLATFORM_MESSAGE_SENT, text: str = "hi") -> Event:
    event = Event(id="e1", point=point)
    event.platform.name = "cli"
    event.platform.scope_id = "s1"
    event.platform.message_id = "m1"
    event.actor.id = "cli:u1"
    event.actor.user_id = "u1"
    event.message.segments = [MessageSegment(type="text", text=text)]
    return event


def hook_registration(name: str = "h1", plugin_id: str = "p1") -> Registration:
    return Registration(
        point=Point.AGENT_INPUT_PREPARED,
        name=name,
        plugin_id=plugin_id,
        match=always(),
        handler=HandlerFunc(lambda ctx, event: event),
    )


class TestControlService(unittest.TestCase):
    def test_hook_list_without_manager(self) -> None:
        self.assertEqual([], New(None, None, None).hook_list())

    def test_stateful_hooks_without_runtime(self) -> None:
        self.assertEqual([], New(DefaultManager(), None, None).stateful_hooks())

    def test_hook_reload_requires_loader(self) -> None:
        service = New(DefaultManager(), None, None)
        with self.assertRaises(ControlError):
            run(service.hook_reload())

    def test_hook_reload_replaces_registrations(self) -> None:
        manager = DefaultManager()
        manager.register(hook_registration("old"))

        def loader(registrar: DefaultManager):
            registrar.register(hook_registration("new"))
            return ReloadReport(notices=["ok"]), []

        service = New(manager, None, loader)
        report = run(service.hook_reload())
        self.assertEqual(["ok"], report.notices)
        self.assertEqual(["new"], [info.name for info in service.hook_list()])

    def test_hook_reload_requires_runtime_for_stateful(self) -> None:
        def loader(registrar: DefaultManager):
            return ReloadReport(), [RuntimeConfig(id="demo")]

        service = New(DefaultManager(), None, loader)
        with self.assertRaises(ControlError) as ctx:
            run(service.hook_reload())
        self.assertIn("stateful hook runtime is not configured", str(ctx.exception))

    def test_hook_reload_applies_runtime(self) -> None:
        runtime = FakeRuntime()
        config = RuntimeConfig(id="demo")
        service = New(DefaultManager(), runtime, lambda registrar: (ReloadReport(), [config]))
        run(service.hook_reload())
        self.assertEqual([[config]], runtime.applied)

    def test_prepare_plugin_reload(self) -> None:
        manager = DefaultManager()
        runtime = FakeRuntime()
        config = RuntimeConfig(id="demo")

        def loader(registrar: DefaultManager):
            registrar.register(hook_registration("plug", "demo"))
            return ReloadReport(), [config]

        service = New(manager, runtime, loader)
        commit = run(service.prepare_plugin_reload("demo"))
        self.assertEqual([config], runtime.validated)
        self.assertEqual([], manager.list())
        run(commit())
        self.assertEqual([config], runtime.replaced)
        self.assertEqual(["plug"], [info.name for info in manager.list()])

    def test_prepare_plugin_reload_errors(self) -> None:
        manager = DefaultManager()
        runtime = FakeRuntime()
        service = New(manager, runtime, lambda registrar: (ReloadReport(), []))
        with self.assertRaises(ControlError) as ctx:
            run(service.prepare_plugin_reload("demo"))
        self.assertIn("missing from reloaded config", str(ctx.exception))
        with self.assertRaises(ControlError):
            run(service.prepare_plugin_reload("  "))
        with self.assertRaises(ControlError):
            run(New(None, runtime, None).prepare_plugin_reload("demo"))

    def test_prepare_plugin_reload_includes_notices(self) -> None:
        service = New(
            DefaultManager(),
            FakeRuntime(),
            lambda registrar: (ReloadReport(notices=["boom"]), []),
        )
        with self.assertRaises(ControlError) as ctx:
            run(service.prepare_plugin_reload("demo"))
        self.assertIn("boom", str(ctx.exception))

    def test_stateful_start_stop_restart(self) -> None:
        runtime = FakeRuntime()
        service = New(DefaultManager(), runtime, None)
        run(service.start_stateful_hook("demo"))
        run(service.stop_stateful_hook(None, "demo"))
        run(service.restart_stateful_hook(None, "demo"))
        self.assertEqual(["demo"], runtime.started)
        self.assertEqual(["demo"], runtime.stopped)
        self.assertEqual(["demo"], runtime.restarted)

    def test_stateful_hooks_require_runtime(self) -> None:
        service = New(DefaultManager(), None, None)
        for call in (
            service.start_stateful_hook("demo"),
            service.restart_stateful_hook(None, "demo"),
        ):
            with self.assertRaises(ControlError):
                run(call)

    def test_stop_hook_matches_plain_registration(self) -> None:
        manager = DefaultManager()
        manager.register(hook_registration("h1", "demo"))
        runtime = FakeRuntime()
        service = New(manager, runtime, None)
        self.assertTrue(run(service.stop_hook(None, "h1")))
        self.assertEqual(["h1"], runtime.stopped)

    def test_stop_hook_runtime_error_only_when_unmatched(self) -> None:
        manager = DefaultManager()
        manager.register(hook_registration("h1", "demo"))
        runtime = FakeRuntime()
        runtime.fail_stop = True
        service = New(manager, runtime, None)
        # 普通 Hook 命中时，runtime 的失败被吞掉（与 Go 一致）。
        self.assertTrue(run(service.stop_hook(None, "h1")))
        with self.assertRaises(ControlError):
            run(service.stop_hook(None, "nothing"))

    def test_stop_hook_requires_manager(self) -> None:
        with self.assertRaises(ControlError):
            run(New(None, None, None).stop_hook(None, "x"))


class FakeRepo:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.fail = False

    def append(self, message) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.messages.append(message)


class TestBuiltinRegister(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        (TMP / "hooks.toml").write_text(
            '[[rules]]\nname = "r"\non = "agent.input.prepared"\nalways = true\n'
            'action = "append"\nfield = "message.text"\ntext = "!"\n',
            encoding="utf-8",
            newline="\n",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_register_all_minimal(self) -> None:
        registrar = Registrar()
        runtimes = RegisterAll(registrar, Options(config_dir=str(TMP)))
        self.assertEqual([], runtimes)
        names = [reg.name for reg in registrar.registrations]
        self.assertEqual(["r"], names)
        self.assertEqual(1000, registrar.registrations[0].priority)

    def test_register_all_none_registrar(self) -> None:
        self.assertEqual([], RegisterAll(None, Options()))

    def test_register_all_with_outbound_and_services(self) -> None:
        registrar = Registrar()
        repo = FakeRepo()
        notices: list[str] = []

        class ReadyService:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def ready(self) -> bool:
                return True

            def context(self, platform, scope_id, query, limit):
                self.calls.append((platform, scope_id, query, limit))
                return "memory"

            def observe(self, platform, scope_id, user_id, text):
                self.calls.append((platform, scope_id, user_id, text))

        angel = ReadyService()
        learning = ReadyService()
        RegisterAll(
            registrar,
            Options(
                config_dir=str(TMP),
                outbound_messages=repo,
                angel_memory=angel,
                self_learning=learning,
                notify=lambda ctx, text: notices.append(text),
            ),
        )
        names = sorted(reg.name for reg in registrar.registrations)
        self.assertEqual(
            [
                "builtin.angel_memory",
                "builtin.outbound_history",
                "builtin.self_learning_context",
                "builtin.self_learning_observe",
                "r",
            ],
            names,
        )
        self.assertEqual([], notices)
        # 有状态 Hook 的 runtime 配置随 RegisterAll 返回。
        self.assertEqual([], RegisterAll(Registrar(), Options(config_dir=str(TMP))))

    def test_register_all_reports_broken_rules(self) -> None:
        broken = TMP / "broken"
        os.makedirs(broken, exist_ok=True)
        (broken / "hooks.toml").write_text("[[rules]\n", encoding="utf-8", newline="\n")
        notices: list[str] = []
        try:
            with self.assertRaises(Exception):
                RegisterAll(
                    Registrar(),
                    Options(
                        config_dir=str(broken),
                        notify=lambda ctx, text: notices.append(text),
                    ),
                )
            self.assertTrue(any("已禁用" in note for note in notices))
        finally:
            shutil.rmtree(broken, ignore_errors=True)


class TestRecordOutboundMessage(unittest.TestCase):
    def test_records_text(self) -> None:
        repo = FakeRepo()
        handler = HandlerFunc(record_outbound_message(repo))
        run(handler.handle_hook(None, make_event()))
        self.assertEqual(1, len(repo.messages))
        message = repo.messages[0]
        self.assertEqual("cli", message.platform)
        self.assertEqual("s1", message.platform_scope_id)
        self.assertEqual("m1", message.platform_message_id)
        self.assertEqual("hi", message.text)
        self.assertIsNotNone(message.created_at)

    def test_skips_tool_preview(self) -> None:
        repo = FakeRepo()
        handler = HandlerFunc(record_outbound_message(repo))
        run(handler.handle_hook(None, make_event(text="[tool] running")))
        self.assertEqual([], repo.messages)

    def test_skips_empty_text(self) -> None:
        repo = FakeRepo()
        handler = HandlerFunc(record_outbound_message(repo))
        run(handler.handle_hook(None, make_event(text="   ")))
        self.assertEqual([], repo.messages)

    def test_skips_other_points(self) -> None:
        repo = FakeRepo()
        handler = HandlerFunc(record_outbound_message(repo))
        run(handler.handle_hook(None, make_event(point=Point.AGENT_INPUT_PREPARED)))
        self.assertEqual([], repo.messages)

    def test_skips_missing_platform(self) -> None:
        repo = FakeRepo()
        handler = HandlerFunc(record_outbound_message(repo))
        event = make_event()
        event.platform.scope_id = ""
        run(handler.handle_hook(None, event))
        self.assertEqual([], repo.messages)

    def test_wraps_repo_error(self) -> None:
        repo = FakeRepo()
        repo.fail = True
        handler = HandlerFunc(record_outbound_message(repo))
        with self.assertRaises(RuntimeError) as ctx:
            run(handler.handle_hook(None, make_event()))
        self.assertIn("record outbound message", str(ctx.exception))


class ReadyService:
    def __init__(self, context_text: str = "ctx") -> None:
        self.context_text = context_text
        self.calls: list[tuple] = []

    def ready(self) -> bool:
        return True

    def context(self, platform, scope_id, query, limit):
        self.calls.append(("context", platform, scope_id, query, limit))
        return self.context_text

    def observe(self, platform, scope_id, user_id, text):
        self.calls.append(("observe", platform, scope_id, user_id, text))


class TestAngelMemory(unittest.TestCase):
    def test_registers_and_appends_context(self) -> None:
        registrar = Registrar()
        service = ReadyService("memory")
        register_angel_memory(registrar, service)
        self.assertEqual(1, len(registrar.registrations))
        reg = registrar.registrations[0]
        self.assertEqual(Point.LLM_TURN_PREPARED, reg.point)
        self.assertEqual(500, reg.priority)
        event = make_event(Point.LLM_TURN_PREPARED)
        event.message.intent_text = "q"
        updated = run(reg.handler.handle_hook(None, event))
        self.assertEqual(["memory"], updated.llm.system_append)
        self.assertEqual(("context", "cli", "s1", "q", 5), service.calls[0])

    def test_skips_when_not_ready(self) -> None:
        class NotReady:
            def ready(self) -> bool:
                return False

        registrar = Registrar()
        register_angel_memory(registrar, NotReady())
        self.assertEqual([], registrar.registrations)
        register_angel_memory(registrar, None)
        self.assertEqual([], registrar.registrations)

    def test_empty_context_not_appended(self) -> None:
        registrar = Registrar()
        register_angel_memory(registrar, ReadyService("   "))
        event = make_event(Point.LLM_TURN_PREPARED)
        updated = run(registrar.registrations[0].handler.handle_hook(None, event))
        self.assertEqual([], updated.llm.system_append)

    def test_missing_platform_skipped(self) -> None:
        registrar = Registrar()
        service = ReadyService()
        register_angel_memory(registrar, service)
        event = make_event(Point.LLM_TURN_PREPARED)
        event.platform.name = ""
        run(registrar.registrations[0].handler.handle_hook(None, event))
        self.assertEqual([], service.calls)

    def test_uses_segments_when_intent_empty(self) -> None:
        registrar = Registrar()
        service = ReadyService()
        register_angel_memory(registrar, service)
        event = make_event(Point.LLM_TURN_PREPARED, text="from-segments")
        run(registrar.registrations[0].handler.handle_hook(None, event))
        self.assertEqual("from-segments", service.calls[0][3])


class TestSelfLearning(unittest.TestCase):
    def test_registers_two_hooks(self) -> None:
        registrar = Registrar()
        register_self_learning(registrar, ReadyService())
        names = sorted(reg.name for reg in registrar.registrations)
        self.assertEqual(
            ["builtin.self_learning_context", "builtin.self_learning_observe"], names
        )
        observe = next(
            reg for reg in registrar.registrations if reg.name.endswith("observe")
        )
        self.assertEqual("any", observe.wakeup)
        self.assertEqual(Point.PLATFORM_MESSAGE_RECEIVED, observe.point)

    def test_observe_uses_platform_text(self) -> None:
        registrar = Registrar()
        service = ReadyService()
        register_self_learning(registrar, service)
        observe = next(
            reg for reg in registrar.registrations if reg.name.endswith("observe")
        )
        event = make_event(Point.PLATFORM_MESSAGE_RECEIVED, text="seg")
        event.message.platform_text = "platform-text"
        run(observe.handler.handle_hook(None, event))
        self.assertEqual(
            ("observe", "cli", "s1", "u1", "platform-text"), service.calls[0]
        )

    def test_context_limit_is_eight(self) -> None:
        registrar = Registrar()
        service = ReadyService()
        register_self_learning(registrar, service)
        context_reg = next(
            reg for reg in registrar.registrations if reg.name.endswith("context")
        )
        event = make_event(Point.LLM_TURN_PREPARED, text="q")
        run(context_reg.handler.handle_hook(None, event))
        self.assertEqual(8, service.calls[0][4])

    def test_not_ready(self) -> None:
        class NotReady:
            def ready(self) -> bool:
                return False

        registrar = Registrar()
        register_self_learning(registrar, NotReady())
        self.assertEqual([], registrar.registrations)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
