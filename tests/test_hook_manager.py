"""``atri_x.hook.manager`` 的单元测试。对应上游 ``internal/hook/manager.go``。"""

from __future__ import annotations

import asyncio
import unittest

from atri_x import delivery
from atri_x.hook import manager as mg
from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.match import BlockPolicy, Condition, Match, always
from atri_x.hook.match import MATCH_FULL

POINT = Point.AGENT_INPUT_PREPARED


def make_reg(**kw: object) -> mg.Registration:
    kw.setdefault("point", POINT)
    kw.setdefault("name", "h1")
    kw.setdefault("match", always())
    kw.setdefault("handler", HandlerFunc(lambda ctx, event: event))
    return mg.Registration(**kw)  # type: ignore[arg-type]


def run(coro):
    return asyncio.run(coro)


class TestRegistrationValidation(unittest.TestCase):
    def test_valid(self) -> None:
        m = mg.new_manager()
        m.register(make_reg())
        self.assertEqual(1, len(m.list()))

    def test_missing_point(self) -> None:
        with self.assertRaises(ValueError):
            mg.new_manager().register(make_reg(point=""))

    def test_missing_name(self) -> None:
        with self.assertRaises(ValueError):
            mg.new_manager().register(make_reg(name="   "))

    def test_missing_handler(self) -> None:
        with self.assertRaises(ValueError):
            mg.new_manager().register(make_reg(handler=None))

    def test_bad_wakeup_policy(self) -> None:
        with self.assertRaises(ValueError):
            mg.new_manager().register(make_reg(wakeup="sometimes"))

    def test_match_must_validate(self) -> None:
        with self.assertRaises(ValueError):
            mg.new_manager().register(make_reg(match=Match(conditions=[])))

    def test_empty_wakeup_normalizes_to_required(self) -> None:
        self.assertEqual(mg.WAKEUP_REQUIRED, mg.normalize_wakeup_policy(""))
        mg.validate_wakeup_policy("")
        for policy in mg.WAKEUP_POLICIES:
            mg.validate_wakeup_policy(policy)
        with self.assertRaises(ValueError):
            mg.validate_wakeup_policy("nope")


class TestPriorityOrdering(unittest.TestCase):
    def test_lower_priority_runs_first(self) -> None:
        order: list[str] = []
        m = mg.new_manager()

        def mk(name: str, priority: int) -> mg.Registration:
            def fn(ctx, event):
                order.append(name)
                return event

            return make_reg(name=name, priority=priority, handler=HandlerFunc(fn))

        m.register(mk("c", 10))
        m.register(mk("a", 1))
        m.register(mk("b", 5))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual(["a", "b", "c"], order)

    def test_same_priority_keeps_registration_order(self) -> None:
        order: list[str] = []
        m = mg.new_manager()
        for name in ("first", "second", "third"):
            def fn(ctx, event, _n=name):
                order.append(_n)
                return event

            m.register(make_reg(name=name, priority=5, handler=HandlerFunc(fn)))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual(["first", "second", "third"], order)


class TestRunBehaviour(unittest.TestCase):
    def test_returns_updated_event(self) -> None:
        m = mg.new_manager()

        def fn(ctx, event):
            event.session.title = "改过"
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        out = run(m.run(None, Event(point=POINT)))
        self.assertEqual("改过", out.session.title)

    def test_prepares_time_and_metadata(self) -> None:
        m = mg.new_manager()
        out = run(m.run(None, Event(point=POINT)))
        self.assertIsNotNone(out.time)
        self.assertEqual({}, out.metadata)

    def test_only_runs_matching_point(self) -> None:
        seen: list[str] = []
        m = mg.new_manager()

        def fn(ctx, event):
            seen.append("ran")
            return event

        m.register(make_reg(point=Point.ERROR_OCCURRED, handler=HandlerFunc(fn)))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual([], seen)

    def test_unmatched_condition_skips(self) -> None:
        ran: list[str] = []
        m = mg.new_manager()
        m.register(
            make_reg(
                name="unmatched",
                match=Match(
                    conditions=[
                        Condition(field="platform.name", op=MATCH_FULL, value="telegram")
                    ]
                ),
                handler=HandlerFunc(lambda c, e: (ran.append("unmatched"), e)[1]),
            )
        )
        m.register(
            make_reg(
                name="recorder",
                priority=9,
                handler=HandlerFunc(lambda c, e: (ran.append("recorder"), e)[1]),
            )
        )
        run(m.run(None, Event(point=POINT)))
        # 条件不匹配的那个不跑；条件为 always 的记录器正常跑
        self.assertEqual(["recorder"], ran)

    def test_block_policy_skips(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(block=BlockPolicy(platforms={"telegram"})))
        event = Event(point=POINT)
        event.platform.name = "telegram"
        seen: list[int] = []
        run(m.run(None, event))
        self.assertEqual([], seen)

    def test_match_context_is_set_and_cleared(self) -> None:
        captured: list[object] = []
        m = mg.new_manager()

        def fn(ctx, event):
            captured.append(event.metadata.get("match"))
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        event = Event(point=POINT)
        out = run(m.run(None, event))
        self.assertEqual(1, len(captured))
        self.assertIsNotNone(captured[0])
        self.assertNotIn("match", out.metadata)
        self.assertNotIn("match", event.metadata)

    def test_stop_propagation_breaks(self) -> None:
        order: list[str] = []
        m = mg.new_manager()
        for name, stop in (("a", True), ("b", False)):
            def fn(ctx, event, _stop=stop, _n=name):
                order.append(_n)
                event.control.stop_propagation = _stop
                return event

            m.register(make_reg(name=name, priority=0, handler=HandlerFunc(fn)))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual(["a"], order)

    def test_handler_error_is_wrapped(self) -> None:
        m = mg.new_manager()

        def boom(ctx, event):
            raise RuntimeError("炸")

        m.register(make_reg(name="bad", handler=HandlerFunc(boom)))
        with self.assertRaises(mg.HookError) as ctx:
            run(m.run(None, Event(point=POINT)))
        self.assertIn("hook bad", str(ctx.exception))
        self.assertIn("炸", str(ctx.exception))

    def test_handler_error_stops_remaining(self) -> None:
        order: list[str] = []
        m = mg.new_manager()

        def boom(ctx, event):
            order.append("boom")
            raise RuntimeError("x")

        def later(ctx, event):
            order.append("later")
            return event

        m.register(make_reg(name="bad", priority=0, handler=HandlerFunc(boom)))
        m.register(make_reg(name="ok", priority=1, handler=HandlerFunc(later)))
        with self.assertRaises(mg.HookError):
            run(m.run(None, Event(point=POINT)))
        self.assertEqual(["boom"], order)

    def test_active_is_cleaned_after_error(self) -> None:
        m = mg.new_manager()

        def boom(ctx, event):
            raise RuntimeError("x")

        m.register(make_reg(handler=HandlerFunc(boom)))
        with self.assertRaises(mg.HookError):
            run(m.run(None, Event(point=POINT)))
        self.assertEqual(0, m.list()[0].active)


class TestWakeupPolicy(unittest.TestCase):
    """wakeup 策略矩阵。对应 Go ``skipForWakeup``。

    ``observed`` handler 用被测策略；``recorder`` 用 ``WAKEUP_ANY``，保证它自己
    不会被策略挡掉（否则"被观察者是否运行"就观察不出来）。
    """

    def _run_case(
        self, policy: str, wakeup_result: bool = False, *, has_wakeup: bool = True
    ) -> tuple[list[int], list[int]]:
        calls: list[int] = []
        ran: list[int] = []
        m = mg.new_manager()
        if has_wakeup:
            m.set_wakeup_func(lambda ctx, event: (calls.append(1), wakeup_result)[1])
        m.register(
            make_reg(
                name="observed",
                priority=0,
                wakeup=policy,
                handler=HandlerFunc(lambda c, e: (ran.append(1), e)[1]),
            )
        )
        m.register(make_reg(name="recorder", priority=99, wakeup=mg.WAKEUP_ANY))
        run(m.run(None, Event(point=POINT)))
        return ran, calls

    def test_required_runs_when_woken(self) -> None:
        ran, calls = self._run_case(mg.WAKEUP_REQUIRED, True)
        self.assertEqual([1], ran)
        self.assertEqual(1, len(calls))

    def test_required_skips_when_unwoken(self) -> None:
        ran, calls = self._run_case(mg.WAKEUP_REQUIRED, False)
        self.assertEqual([], ran)
        self.assertEqual(1, len(calls))

    def test_any_never_consults_wakeup(self) -> None:
        ran, calls = self._run_case(mg.WAKEUP_ANY, False)
        self.assertEqual([1], ran)
        self.assertEqual(0, len(calls))

    def test_forbidden_runs_only_when_unwoken(self) -> None:
        ran, _ = self._run_case(mg.WAKEUP_FORBIDDEN, False)
        self.assertEqual([1], ran)
        ran2, _ = self._run_case(mg.WAKEUP_FORBIDDEN, True)
        self.assertEqual([], ran2)

    def test_no_wakeup_func_runs_everything(self) -> None:
        ran, calls = self._run_case(mg.WAKEUP_REQUIRED, has_wakeup=False)
        self.assertEqual([1], ran)
        self.assertEqual([], calls)

    def test_async_wakeup_func(self) -> None:
        ran: list[int] = []
        m = mg.new_manager()

        async def wakeup(ctx, event):
            return True

        m.set_wakeup_func(wakeup)
        m.register(
            make_reg(
                name="observed",
                wakeup=mg.WAKEUP_REQUIRED,
                handler=HandlerFunc(lambda c, e: (ran.append(1), e)[1]),
            )
        )
        run(m.run(None, Event(point=POINT)))
        self.assertEqual([1], ran)


class TestNotify(unittest.TestCase):
    def test_runs_all_even_after_error(self) -> None:
        order: list[str] = []
        m = mg.new_manager()

        def boom(ctx, event):
            order.append("boom")
            raise RuntimeError("x")

        def later(ctx, event):
            order.append("later")
            return event

        m.register(make_reg(name="bad", priority=0, handler=HandlerFunc(boom)))
        m.register(make_reg(name="ok", priority=1, handler=HandlerFunc(later)))
        with self.assertRaises(ExceptionGroup) as ctx:
            run(m.notify(None, Event(point=POINT)))
        self.assertEqual(["boom", "later"], order)
        self.assertEqual(1, len(ctx.exception.exceptions))
        self.assertIsInstance(ctx.exception.exceptions[0], mg.HookError)

    def test_no_error_returns_none(self) -> None:
        m = mg.new_manager()
        m.register(make_reg())
        self.assertIsNone(run(m.notify(None, Event(point=POINT))))

    def test_handler_mutation_does_not_leak(self) -> None:
        m = mg.new_manager()

        def fn(ctx, event):
            event.session.title = "hacked"
            event.metadata["extra"] = 1
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        event = Event(point=POINT)
        run(m.notify(None, event))
        self.assertEqual("", event.session.title)
        self.assertNotIn("extra", event.metadata)
        self.assertNotIn("match", event.metadata)

    def test_stop_propagation_breaks(self) -> None:
        order: list[str] = []
        m = mg.new_manager()

        def first(ctx, event):
            order.append("first")
            event.control.stop_propagation = True
            return event

        def second(ctx, event):
            order.append("second")
            return event

        m.register(make_reg(name="a", priority=0, handler=HandlerFunc(first)))
        m.register(make_reg(name="b", priority=1, handler=HandlerFunc(second)))
        run(m.notify(None, Event(point=POINT)))
        self.assertEqual(["first"], order)

    def test_existing_outputs_are_not_marked(self) -> None:
        """``notify`` 不改变调用方 —— 与 Go 的 ``_ = markHookOutputs(...)`` 一致。"""
        m = mg.new_manager()
        event = Event(point=POINT, outputs=[delivery.text("a")])
        run(m.notify(None, event))
        self.assertIsNone(event.outputs[0].meta)

    def test_outputs_appended_by_handler_do_not_reach_caller(self) -> None:
        m = mg.new_manager()

        def fn(ctx, event):
            event.outputs.append(delivery.text("new"))
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        event = Event(point=POINT, outputs=[delivery.text("a")])
        run(m.notify(None, event))
        self.assertEqual(1, len(event.outputs))

    def test_unmatched_condition_skips_notify(self) -> None:
        seen: list[int] = []
        m = mg.new_manager()
        m.register(
            make_reg(
                match=Match(
                    conditions=[
                        Condition(field="platform.name", op=MATCH_FULL, value="telegram")
                    ]
                ),
                handler=HandlerFunc(lambda c, e: (seen.append(1), e)[1]),
            )
        )
        run(m.notify(None, Event(point=POINT)))
        self.assertEqual([], seen)


class TestOutputMarking(unittest.TestCase):
    def test_marks_only_new_outputs(self) -> None:
        m = mg.new_manager()

        def fn(ctx, event):
            event.outputs.append(delivery.text("new"))
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        event = Event(point=POINT, outputs=[delivery.text("old")])
        out = run(m.run(None, event))
        self.assertEqual(2, len(out.outputs))
        self.assertIsNone(out.outputs[0].meta)
        self.assertEqual(POINT, out.outputs[1].meta.get(delivery.META_HOOK_POINT))
        self.assertEqual("h1", out.outputs[1].meta.get(delivery.META_HOOK_NAME))
        self.assertEqual("run", out.outputs[1].meta.get(delivery.META_HOOK_MODE))

    def test_does_not_overwrite_existing_meta(self) -> None:
        m = mg.new_manager()
        existing = delivery.text("x")
        existing.meta = {delivery.META_HOOK_NAME: "custom"}

        def fn(ctx, event):
            event.outputs.append(existing)
            return event

        m.register(make_reg(handler=HandlerFunc(fn)))
        out = run(m.run(None, Event(point=POINT)))
        self.assertEqual("custom", out.outputs[0].meta[delivery.META_HOOK_NAME])
        self.assertEqual(POINT, out.outputs[0].meta[delivery.META_HOOK_POINT])

    def test_clamps_start(self) -> None:
        reg = mg._Registration(name="x")
        event = Event(point=POINT, outputs=[delivery.text("a")])
        self.assertEqual(event, mg.mark_hook_outputs(event, -5, reg, "run"))
        self.assertEqual(1, len(mg.mark_hook_outputs(event, 999, reg, "run").outputs))


class TestStopAndMatches(unittest.TestCase):
    def test_matches_by_name_and_plugin(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="rule-a", plugin_id="plug"))
        self.assertTrue(m.matches("rule-a"))
        self.assertTrue(m.matches("plug"))
        self.assertFalse(m.matches("nope"))
        self.assertFalse(m.matches("  "))

    def test_stop_returns_zero_for_unknown(self) -> None:
        m = mg.new_manager()
        self.assertEqual(0, m.stop("nope"))
        self.assertEqual(0, m.stop(""))

    def test_stop_cancels_active_by_name(self) -> None:
        m = mg.new_manager()
        cancelled: list[int] = []

        class Ctx:
            def with_cancel(self):
                return self

            def cancel(self):
                cancelled.append(1)

        async def scenario() -> None:
            def fn(ctx, event):
                m.stop("h1")
                return event

            m.register(make_reg(name="h1", handler=HandlerFunc(fn)))
            await m.run(Ctx(), Event(point=POINT))

        run(scenario())
        self.assertEqual(1, len(cancelled))

    def test_active_count_visible_in_list(self) -> None:
        m = mg.new_manager()
        seen: list[int] = []

        def fn(ctx, event):
            seen.append(m.list()[0].active)
            return event

        m.register(make_reg(name="h1", handler=HandlerFunc(fn)))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual([1], seen)
        self.assertEqual(0, m.list()[0].active)


class TestList(unittest.TestCase):
    def test_sorted_by_point_then_name(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(point=Point.ERROR_OCCURRED, name="z"))
        m.register(make_reg(point=Point.AGENT_INPUT_PREPARED, name="b"))
        m.register(make_reg(point=Point.AGENT_INPUT_PREPARED, name="a"))
        got = [(i.point, i.name) for i in m.list()]
        self.assertEqual(
            [
                (Point.AGENT_INPUT_PREPARED, "a"),
                (Point.AGENT_INPUT_PREPARED, "b"),
                (Point.ERROR_OCCURRED, "z"),
            ],
            got,
        )

    def test_carries_metadata(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="n", plugin_id="p", detail="d", description="desc", priority=7))
        info = m.list()[0]
        self.assertEqual("p", info.plugin_id)
        self.assertEqual("d", info.detail)
        self.assertEqual("desc", info.description)
        self.assertEqual(7, info.priority)

    def test_plugin_id_trimmed(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(plugin_id="  p  ", description="  d  "))
        info = m.list()[0]
        self.assertEqual("p", info.plugin_id)
        self.assertEqual("d", info.description)


class TestReplace(unittest.TestCase):
    def test_replace_swaps_handlers_and_keeps_runtime_hooks(self) -> None:
        target = mg.new_manager()
        target.set_wakeup_func(lambda ctx, event: False)
        target.register(make_reg(name="old"))

        next_manager = mg.new_manager()
        next_manager.register(make_reg(name="new"))

        target.replace(next_manager)
        self.assertEqual(["new"], [i.name for i in target.list()])
        # wakeup 接线保留在接收方：新来的 handler 默认 required，wakeup 返回 False
        # → 被跳过，因此记录器（WAKEUP_ANY）也观察不到它运行。
        seen: list[int] = []
        target.register(
            make_reg(
                name="rec",
                priority=99,
                wakeup=mg.WAKEUP_ANY,
                handler=HandlerFunc(lambda c, e: (seen.append(1), e)[1]),
            )
        )
        run(target.run(None, Event(point=POINT)))
        self.assertEqual([1], seen)
        self.assertEqual(["new", "rec"], [i.name for i in target.list()])

    def test_replace_self_or_none_is_noop(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="a"))
        m.replace(m)
        m.replace(None)  # type: ignore[arg-type]
        self.assertEqual(["a"], [i.name for i in m.list()])

    def test_replace_copies_snapshot(self) -> None:
        target = mg.new_manager()
        source = mg.new_manager()
        source.register(make_reg(name="a"))
        target.replace(source)
        source.register(make_reg(name="b"))
        self.assertEqual(["a"], [i.name for i in target.list()])


class TestReplacePlugin(unittest.TestCase):
    def test_replaces_only_target_plugin(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="p1-a", plugin_id="p1", priority=0))
        m.register(make_reg(name="p2-a", plugin_id="p2", priority=5))
        m.register(make_reg(name="p1-b", plugin_id="p1", priority=10))

        source = mg.new_manager()
        source.register(make_reg(name="p1-new", plugin_id="p1", priority=99))

        m.replace_plugin("p1", source)
        names = [i.name for i in m.list()]
        self.assertIn("p2-a", names)
        self.assertIn("p1-new", names)
        self.assertNotIn("p1-a", names)
        self.assertNotIn("p1-b", names)
        # list() 按 (point, name) 排序，所以这里看到的是字母序
        self.assertEqual(["p1-new", "p2-a"], names)

    def test_execution_order_follows_priority_after_replace(self) -> None:
        order: list[str] = []
        m = mg.new_manager()
        m.register(
            make_reg(
                name="p2-a",
                plugin_id="p2",
                priority=5,
                handler=HandlerFunc(lambda c, e: (order.append("p2-a"), e)[1]),
            )
        )
        source = mg.new_manager()
        source.register(
            make_reg(
                name="p1-new",
                plugin_id="p1",
                priority=99,
                handler=HandlerFunc(lambda c, e: (order.append("p1-new"), e)[1]),
            )
        )
        m.replace_plugin("p1", source)
        run(m.run(None, Event(point=POINT)))
        # 执行顺序由 priority 决定（p2-a=5 先于 p1-new=99），与 list() 的字母序无关
        self.assertEqual(["p2-a", "p1-new"], order)

    def test_long_options_no_duplicates(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="keep", plugin_id="other"))
        source = mg.new_manager()
        source.register(make_reg(name="new", plugin_id="p1"))
        m.replace_plugin("p1", source)
        self.assertEqual(["keep", "new"], [i.name for i in m.list()])

    def test_blank_plugin_id_is_noop(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="a"))
        m.replace_plugin("  ", mg.new_manager())
        self.assertEqual(["a"], [i.name for i in m.list()])

    def test_empty_replacement_removes_plugin(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="a", plugin_id="p1"))
        m.register(make_reg(name="b"))
        m.replace_plugin("p1", mg.new_manager())
        self.assertEqual(["b"], [i.name for i in m.list()])

    def test_removing_last_registration_drops_point(self) -> None:
        m = mg.new_manager()
        m.register(make_reg(name="a", plugin_id="p1"))
        m.replace_plugin("p1", mg.new_manager())
        self.assertEqual([], m.list())
        self.assertEqual({}, m._handlers)


class TestLoggingAndText(unittest.TestCase):
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        def debug(self, msg: str, **fields: object) -> None:
            self.calls.append(("debug", msg, dict(fields)))

        def info(self, msg: str, **fields: object) -> None:
            self.calls.append(("info", msg, dict(fields)))

        def warning(self, msg: str, **fields: object) -> None:
            self.calls.append(("warning", msg, dict(fields)))

    def test_logs_success_at_info(self) -> None:
        m = mg.new_manager()
        log = self.Recorder()
        m.set_logger(log)
        m.register(make_reg(name="h1"))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual(1, len(log.calls))
        level, msg, fields = log.calls[0]
        self.assertEqual("info", level)
        self.assertEqual("hook triggered", msg)
        self.assertEqual("h1", fields["hook"])
        self.assertEqual(POINT, fields["point"])

    def test_logs_error_at_warning(self) -> None:
        m = mg.new_manager()
        log = self.Recorder()
        m.set_logger(log)
        m.register(make_reg(name="bad", handler=HandlerFunc(_boom)))
        with self.assertRaises(mg.HookError):
            run(m.run(None, Event(point=POINT)))
        self.assertEqual("warning", log.calls[0][0])
        self.assertEqual("hook error", log.calls[0][1])
        self.assertEqual("炸", log.calls[0][2]["error"])

    def test_no_logger_is_safe(self) -> None:
        m = mg.new_manager()
        m.register(make_reg())
        run(m.run(None, Event(point=POINT)))

    def test_event_text_priority(self) -> None:
        event = Event(point=POINT)
        self.assertEqual("", mg.event_text(event))
        event.tool.arguments = "args"
        self.assertEqual("args", mg.event_text(event))
        event.tool.result = "res"
        self.assertEqual("res", mg.event_text(event))
        event.message.segments = []
        event.llm.text = "llm"
        self.assertEqual("llm", mg.event_text(event))

    def test_trim_log_text(self) -> None:
        self.assertEqual("abc", mg.trim_log_text("abc"))
        long = "a" * 400
        trimmed = mg.trim_log_text(long)
        self.assertEqual(303, len(trimmed))
        self.assertTrue(trimmed.endswith("..."))

    def test_trim_log_text_counts_runes(self) -> None:
        long = "啊" * 400
        self.assertEqual(303, len(mg.trim_log_text(long)))


def _boom(ctx, event):
    raise RuntimeError("炸")


class TestNoopManager(unittest.TestCase):
    def test_noop(self) -> None:
        m = mg.NoopManager()
        self.assertIsNone(m.register(make_reg()))
        event = Event(point=POINT)
        out = run(m.run(None, event))
        self.assertIsNotNone(out.time)
        self.assertIsNone(run(m.notify(None, event)))
        self.assertEqual([], m.list())

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(mg.NoopManager(), mg.ManagerProtocol)
        self.assertIsInstance(mg.new_manager(), mg.ManagerProtocol)


class TestObserver(unittest.TestCase):
    def test_observer_wraps_execution(self) -> None:
        events: list[str] = []

        def observer(ctx, event, info):
            events.append(f"enter:{info.mode}:{info.name}:{info.point}")

            def done() -> None:
                events.append("exit")

            return ctx, done

        m = mg.new_manager()
        m.set_observer(observer)
        m.register(make_reg(name="h1"))
        run(m.run(None, Event(point=POINT)))
        self.assertEqual([f"enter:run:h1:{POINT}", "exit"], events)

    def test_none_returning_observer_is_tolerated(self) -> None:
        m = mg.new_manager()
        m.set_observer(lambda ctx, event, info: (None, None))
        m.register(make_reg())
        run(m.run(None, Event(point=POINT)))

    def test_async_observer(self) -> None:
        events: list[str] = []

        async def observer(ctx, event, info):
            events.append("enter")
            return ctx, lambda: events.append("exit")

        m = mg.new_manager()
        m.set_observer(observer)
        m.register(make_reg())
        run(m.run(None, Event(point=POINT)))
        self.assertEqual(["enter", "exit"], events)


if __name__ == "__main__":
    unittest.main()
