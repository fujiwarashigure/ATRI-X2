"""``atri_x.hook.rules`` 的单元测试。

对应上游 ``internal/hook/rules/*.go``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import unittest
from pathlib import Path

from atri_x.delivery import delivery
from atri_x.hook.event import Event, Point
from atri_x.hook.manager import DefaultManager
from atri_x.hook.match import Condition
from atri_x.hook.rules import (
    Action,
    Module,
    Options,
    Rule,
    RuleError,
    format_rule_detail,
    load_config,
    rule_registrations,
    rule_role_conditions,
    validate_rule,
)
from atri_x.hook.rules.config import (
    clear_block_config,
    config_normalize,
    decode_config,
    decode_plugin_config,
    has_block_config,
    plugin_config_path,
    safe_relative_path,
)
from atri_x.hook.rules.detail import rule_description, target_string
from atri_x.hook.rules.rule import rule_conditions
from atri_x.hook.rules.toml_error import (
    ConfigParseError,
    StrictDecodeError,
    decode_toml_file,
    decode_toml_text,
    find_toml_array_table_conflict,
    parse_toml_string_assignment,
    toml_context_at_line,
)
from atri_x.hook.rules.types import Action as ActionType
from atri_x.hook.rules.types import CONFIG_FILE, DEFAULT_PRIORITY

TMP = Path(__file__).resolve().parent.parent / "tmp_hook_rules_test"

PLUGIN_OK = """
import json
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    frame = json.loads(line)
    if frame.get("type") != "request":
        continue
    method = frame.get("method")
    if method == "system.init":
        send({"type": "response", "id": frame["id"], "ok": True, "result": {"ok": True}})
    elif method == "event.handle":
        # 顺带发一个插件反向请求与一条日志事件，验证宿主的双向处理。
        send({"type": "event", "method": "hook.log", "params": {"hello": "world"}})
        send({"type": "request", "id": "plugin:1", "method": "message.get_reply", "params": {}})
        send({"type": "response", "id": frame["id"], "ok": True,
              "result": {"status": "completed", "result": "plugin-result",
                         "message": {"text": "rewritten-by-plugin"}}})
        break
"""

PLUGIN_FAIL = """
import sys

sys.stdin.readline()
sys.stderr.write("boom from plugin\\n")
sys.exit(3)
"""

PLUGIN_HANG = """
import time
import sys

sys.stdin.readline()
time.sleep(30)
"""


def run(coro):
    return asyncio.run(coro)


def make_event(point: str = Point.AGENT_INPUT_PREPARED, text: str = "hello") -> Event:
    event = Event(id="e1", point=point)
    event.platform.name = "cli"
    event.platform.scope_id = "s1"
    event.actor.id = "cli:u1"
    event.actor.user_id = "u1"
    event.message.id = "m1"
    from atri_x.llm.llm import MessageSegment

    event.message.segments = [MessageSegment(type="text", text=text)]
    return event


class TestStrictToml(unittest.TestCase):
    GOOD = """
[[rules]]
name = "a"
on = "agent.input.prepared"
always = true
action = "append"
field = "message.text"
text = "!"
"""

    def test_decode_ok(self) -> None:
        cfg = decode_toml_text("hooks.toml", self.GOOD, decode_config)
        self.assertEqual(1, len(cfg.rules))
        self.assertEqual("a", cfg.rules[0].name)
        self.assertTrue(cfg.rules[0].always)

    def test_unknown_field_reports_line(self) -> None:
        text = self.GOOD + 'bogus = "x"\n'
        with self.assertRaises(ConfigParseError) as ctx:
            decode_toml_text("hooks.toml", text, decode_config)
        message = str(ctx.exception)
        self.assertIn("unknown field", message)
        self.assertIn('field "bogus"', message)
        self.assertIn("line 9", message)

    def test_invalid_toml(self) -> None:
        with self.assertRaises(ConfigParseError) as ctx:
            decode_toml_text("hooks.toml", "[[rules]\n", decode_config)
        self.assertIn("parse hook rule config", str(ctx.exception))

    def test_wrong_type(self) -> None:
        text = self.GOOD.replace('always = true', 'always = "yes"')
        with self.assertRaises(ConfigParseError) as ctx:
            decode_toml_text("hooks.toml", text, decode_config)
        self.assertIn("期望布尔值", str(ctx.exception))

    def test_type_error_reports_key(self) -> None:
        text = '[[rules]]\nname = 1\n'
        with self.assertRaises(ConfigParseError) as ctx:
            decode_toml_text("hooks.toml", text, decode_config)
        self.assertIn("期望字符串", str(ctx.exception))

    def test_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            decode_toml_file(str(TMP / "nope.toml"), decode_config)

    def test_nested_tables(self) -> None:
        text = """
[plugin]
name = "demo"
description = "d"
blocked_platform = ["qq"]

[plugin.runtime]
mode = "persistent"
command = ["python", "main.py"]
cwd = "."
startup_timeout_seconds = 5
shutdown_timeout_seconds = 5
event_timeout_seconds = 5
max_wait_seconds = 60

[plugin.runtime.restart]
strategy = "on_failure"
initial_delay_seconds = 1
max_delay_seconds = 30

[plugin.runtime.tools]
allow = ["read_file"]
background_allow = []

[[rules]]
name = "r"
on = "agent.input.prepared"
action = "append"
field = "message.text"
text = "x"
match = [{ field = "message.text", op = "contains", value = "y" }]
"""
        cfg = decode_toml_text("hook.toml", text, decode_plugin_config)
        self.assertEqual("demo", cfg.plugin.name)
        self.assertEqual(["qq"], cfg.plugin.blocked_platforms)
        self.assertEqual("persistent", cfg.plugin.runtime.mode)
        self.assertEqual(1, cfg.plugin.runtime.restart.initial_delay_seconds)
        self.assertEqual(["read_file"], cfg.plugin.runtime.tools.allow)
        self.assertEqual("contains", cfg.rules[0].match[0].op)

    def test_unknown_nested_field(self) -> None:
        text = "[plugin.runtime]\nmode = \"once\"\nbogus = 1\n"
        with self.assertRaises(ConfigParseError) as ctx:
            decode_toml_text("hook.toml", text, decode_plugin_config)
        self.assertIn("unknown field", str(ctx.exception))

    def test_if_keyword_maps_to_if_(self) -> None:
        text = """
[[rules]]
name = "r"
on = "agent.input.prepared"
if = "message.text"
op = "contains"
value = "y"
action = "append"
field = "message.text"
text = "x"
"""
        cfg = decode_toml_text("hooks.toml", text, decode_config)
        self.assertEqual("message.text", cfg.rules[0].if_)

    def test_toml_text_helpers(self) -> None:
        self.assertEqual(("name", "a", True), parse_toml_string_assignment('name = "a"'))
        self.assertEqual(("name", "", False), parse_toml_string_assignment("name = 1"))
        self.assertEqual(
            (4, 1, 3, 'rule "a"'),
            find_toml_array_table_conflict(
                '[[rules]]\nname = "a"\naction = "x"\n[[action]]\n', "action"
            ),
        )
        self.assertEqual('rule "b"', toml_context_at_line('[[rules]]\nname = "b"\n', 2))
        self.assertEqual("plugin metadata", toml_context_at_line('[plugin]\nname = "p"\n', 2))

    def test_safe_relative_path(self) -> None:
        self.assertEqual(
            os.path.join("base", "demo", "hook.toml"),
            safe_relative_path("base", "demo/hook.toml"),
        )
        for bad in ("../x.toml", "/abs/x.toml", ".", ".."):
            with self.assertRaises(ValueError):
                safe_relative_path("base", bad)

    def test_plugin_config_path_defaults(self) -> None:
        from atri_x.hook.rules.types import PluginRef

        self.assertEqual(
            os.path.join("cfg", "demo", "hook.toml"),
            plugin_config_path("cfg", PluginRef(name="demo")),
        )
        with self.assertRaises(ValueError):
            plugin_config_path("cfg", PluginRef(name=""))


class TestLoadConfig(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        self.notices: list[str] = []

    def tearDown(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def opts(self) -> Options:
        return Options(
            config_dir=str(TMP),
            notify=lambda ctx, text: self.notices.append(text),
        )

    def write(self, relative: str, text: str) -> None:
        path = TMP / relative
        os.makedirs(path.parent, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def test_missing_config_returns_empty(self) -> None:
        cfg, path = load_config(self.opts())
        self.assertEqual([], cfg.rules)
        self.assertEqual(str(TMP / CONFIG_FILE), path)

    def test_empty_config_dir(self) -> None:
        cfg, path = load_config(Options())
        self.assertEqual([], cfg.rules)
        self.assertEqual("", path)

    def test_root_rules_and_env(self) -> None:
        self.write(
            CONFIG_FILE,
            """
[[rules]]
name = "root"
on = "agent.input.prepared"
always = true
action = "append"
field = "message.text"
text = "!"
""",
        )
        self.write(".env", "EXTRA=1\n")
        cfg, _path = load_config(self.opts())
        self.assertEqual(1, len(cfg.rules))
        rule = cfg.rules[0]
        self.assertEqual("root", rule.source.original_name)
        self.assertEqual("root", rule.source.final_name)
        self.assertEqual(str(TMP / CONFIG_FILE), rule.source.config_path)
        self.assertEqual(str(TMP), rule.source.base_dir)
        self.assertTrue(rule.source.process_env.configured())
        value, ok = rule.source.process_env.lookup("EXTRA")
        self.assertTrue(ok)
        self.assertEqual("1", value)

    def test_plugin_rules_and_runtime(self) -> None:
        self.write(
            CONFIG_FILE,
            """
[[plugins]]
name = "demo"

[[rules]]
name = "root"
on = "agent.input.prepared"
always = true
action = "append"
field = "message.text"
text = "!"
""",
        )
        self.write(
            "demo/hook.toml",
            """
[plugin]
name = "demo"
description = "demo plugin"
blocked_platform = ["legacy"]

[plugin.runtime]
mode = "persistent"
command = ["python", "main.py"]
cwd = "."
startup_timeout_seconds = 5
shutdown_timeout_seconds = 5
event_timeout_seconds = 5
max_wait_seconds = 60

[plugin.runtime.restart]
strategy = "never"
initial_delay_seconds = 1
max_delay_seconds = 2

[[rules]]
name = "plug"
on = "llm.turn.prepared"
always = true
""",
        )
        cfg, _path = load_config(self.opts())
        self.assertEqual(2, len(cfg.rules))
        plugin_rule = cfg.rules[1]
        self.assertEqual("demo", plugin_rule.source.plugin_name)
        self.assertEqual("demo plugin", plugin_rule.source.plugin_description)
        self.assertEqual(str(TMP / "demo"), plugin_rule.source.base_dir)
        self.assertEqual(str(TMP / "demo"), plugin_rule.source.strict_dir)
        self.assertEqual("demo", plugin_rule.source.runtime_id)
        # 有状态插件的规则只能是事件触发器（Go 同样拒绝 "actions"）。
        self.assertEqual([], plugin_rule.actions)
        self.assertTrue(plugin_rule.source.block.blocks(make_event_with_platform("legacy")))
        self.assertEqual(1, len(cfg.runtimes))
        self.assertEqual("demo", cfg.runtimes[0].id)
        self.assertEqual("demo plugin", cfg.runtimes[0].description)

    def test_worker_plugin_rule_with_actions_is_skipped(self) -> None:
        self.write(CONFIG_FILE, '[[plugins]]\nname = "demo"\n')
        self.write(
            "demo/hook.toml",
            """
[plugin.runtime]
mode = "persistent"
command = ["python", "main.py"]
cwd = "."
startup_timeout_seconds = 5
shutdown_timeout_seconds = 5
event_timeout_seconds = 5
max_wait_seconds = 60

[plugin.runtime.restart]
strategy = "never"
initial_delay_seconds = 1
max_delay_seconds = 2

[[rules]]
name = "plug"
on = "llm.turn.prepared"
always = true
action = "append"
field = "llm.latest_user_text"
text = "?"
""",
        )
        cfg, _path = load_config(self.opts())
        self.assertEqual([], cfg.rules)
        self.assertTrue(any("cannot set actions" in note for note in self.notices))

    def test_duplicate_rule_names_get_suffix(self) -> None:
        rule = """
[[rules]]
name = "dup"
on = "agent.input.prepared"
always = true
action = "append"
field = "message.text"
text = "!"
"""
        self.write(CONFIG_FILE, rule + rule)
        cfg, _path = load_config(self.opts())
        self.assertEqual("dup", cfg.rules[0].source.final_name)
        self.assertEqual("dup.1", cfg.rules[1].source.final_name)

    def test_plugin_rule_blocks_warn_and_are_cleared(self) -> None:
        self.write(
            CONFIG_FILE,
            """
[[plugins]]
name = "demo"
""",
        )
        self.write(
            "demo/hook.toml",
            """
[[rules]]
name = "blk"
on = "agent.input.prepared"
always = true
blocked_platform = ["qq"]
action = "append"
field = "message.text"
text = "!"
""",
        )
        cfg, _path = load_config(self.opts())
        self.assertTrue(any("blocked_platform" in note for note in self.notices))
        self.assertFalse(has_block_config(cfg.rules[0]))

    def test_plugin_name_mismatch_warns(self) -> None:
        self.write(CONFIG_FILE, '[[plugins]]\nname = "demo"\n')
        self.write(
            "demo/hook.toml",
            '[plugin]\nname = "other"\n\n[[rules]]\nname = "r"\non = "agent.input.prepared"\nalways = true\naction = "append"\nfield = "message.text"\ntext = "!"\n',
        )
        _cfg, _path = load_config(self.opts())
        self.assertTrue(any("differs" in note for note in self.notices))

    def test_broken_plugin_is_skipped(self) -> None:
        self.write(
            CONFIG_FILE,
            """
[[plugins]]
name = "demo"

[[rules]]
name = "root"
on = "agent.input.prepared"
always = true
action = "append"
field = "message.text"
text = "!"
""",
        )
        self.write("demo/hook.toml", "[[rules]\n")
        cfg, _path = load_config(self.opts())
        self.assertEqual(1, len(cfg.rules))
        self.assertTrue(any("已跳过" in note for note in self.notices))

    def test_plugin_missing_file_is_skipped(self) -> None:
        self.write(CONFIG_FILE, '[[plugins]]\nname = "ghost"\n')
        cfg, _path = load_config(self.opts())
        self.assertEqual([], cfg.rules)
        self.assertTrue(self.notices)

    def test_disabled_plugin_skipped_silently(self) -> None:
        self.write(CONFIG_FILE, '[[plugins]]\nname = "ghost"\nenabled = false\n')
        cfg, _path = load_config(self.opts())
        self.assertEqual([], cfg.rules)
        self.assertEqual([], self.notices)


def make_event_with_platform(platform: str) -> Event:
    event = make_event()
    event.platform.name = platform
    return event


class TestRuleConditions(unittest.TestCase):
    def test_always(self) -> None:
        rule = Rule(always=True)
        self.assertEqual("always", rule_conditions(rule)[0].op)

    def test_always_conflicts(self) -> None:
        with self.assertRaises(ValueError):
            rule_conditions(Rule(always=True, match=[Condition(op="exists")]))
        with self.assertRaises(ValueError):
            rule_conditions(Rule(always=True, op="contains"))

    def test_flat_condition(self) -> None:
        conditions = rule_conditions(
            Rule(if_="message.text", op="contains", value="x")
        )
        self.assertEqual("message.text", conditions[0].field)
        self.assertEqual("x", conditions[0].value)

    def test_flat_conflicts_with_match(self) -> None:
        with self.assertRaises(ValueError):
            rule_conditions(Rule(op="contains", match=[Condition(op="exists")]))

    def test_match_passthrough(self) -> None:
        conditions = rule_conditions(Rule(match=[Condition(op="exists", field="tool.name")]))
        self.assertEqual("tool.name", conditions[0].field)

    def test_normalize_inline_action(self) -> None:
        rule = Rule(
            if_="message.text",
            op="contains",
            value="x",
            action="append",
            field="message.text",
            pattern="a",
            text="!",
        )
        config_normalize_rule(rule)
        self.assertEqual(1, len(rule.actions))
        action = rule.actions[0]
        self.assertEqual("append", action.type)
        self.assertEqual("a", action.match)

    def test_normalize_action_plus_actions(self) -> None:
        rule = Rule(action="append", actions=[ActionType(type="append")])
        with self.assertRaises(ValueError):
            config_normalize_rule(rule)


def config_normalize_rule(rule: Rule) -> None:
    from atri_x.hook.rules.config import rule_normalize

    rule_normalize(rule)


class TestRuleRoles(unittest.TestCase):
    def test_role_conditions(self) -> None:
        conditions = rule_role_conditions(
            Rule(roles=["superadmin", "owner"], actor_roles=["user"], group_roles=["admin"])
        )
        seen = {(c.field, c.value) for c in conditions}
        self.assertIn(("actor.role", "superadmin"), seen)
        self.assertIn(("actor.group_role", "owner"), seen)
        self.assertIn(("actor.role", "user"), seen)
        self.assertIn(("actor.group_role", "admin"), seen)

    def test_role_conditions_dedupe(self) -> None:
        conditions = rule_role_conditions(Rule(actor_roles=["user", "user"]))
        self.assertEqual(1, len(conditions))

    def test_registrations_partition(self) -> None:
        rule = Rule(
            match=[Condition(field="message.text", op="contains", value="x")],
            roles=["superadmin", "user"],
        )
        registrations = rule_registrations(rule)
        self.assertEqual(2, len(registrations))
        for match in registrations:
            self.assertEqual(2, len(match.conditions))

    def test_registrations_without_roles(self) -> None:
        rule = Rule(match=[Condition(field="message.text", op="contains", value="x")])
        self.assertEqual(1, len(rule_registrations(rule)))

    def test_unknown_role_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(
                Rule(
                    name="r",
                    on=Point.AGENT_INPUT_PREPARED,
                    always=True,
                    roles=["nobody"],
                    actions=[ActionType(type="append", field="message.text", text="x")],
                )
            )


class TestValidateRule(unittest.TestCase):
    def base(self, **kw: object) -> Rule:
        values: dict[str, object] = {
            "name": "r",
            "on": Point.AGENT_INPUT_PREPARED,
            "always": True,
            "match": [Condition(op="always")],
            "actions": [ActionType(type="append", field="message.text", text="!")],
        }
        values.update(kw)
        return Rule(**values)  # type: ignore[arg-type]

    def test_ok(self) -> None:
        validate_rule(self.base())

    def test_missing_on(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_rule(self.base(on=""))
        self.assertIn("missing on", str(ctx.exception))

    def test_unknown_point(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_rule(self.base(on="nope.point"))
        self.assertIn("unknown hook point", str(ctx.exception))

    def test_no_actions(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(self.base(actions=[]))

    def test_no_match(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(self.base(match=[]))

    def test_worker_rule_cannot_have_actions(self) -> None:
        rule = self.base()
        rule.source.runtime_id = "demo"
        with self.assertRaises(ValueError) as ctx:
            validate_rule(rule)
        self.assertIn("cannot set actions", str(ctx.exception))

    def test_bad_wakeup(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(self.base(wakeup="sometimes"))

    def test_exec_requires_command(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_rule(self.base(actions=[ActionType(type="exec")]))
        self.assertIn("command is required", str(ctx.exception))

    def test_exec_negative_timeout(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(
                self.base(
                    actions=[
                        ActionType(type="exec", command=["x"], timeout_seconds=-1)
                    ]
                )
            )

    def test_bad_timing(self) -> None:
        with self.assertRaises(ValueError):
            validate_rule(
                self.base(
                    actions=[ActionType(type="append", field="message.text", timing="later")]
                )
            )


class TestRunRule(unittest.TestCase):
    def module(self) -> Module:
        return Module(opts=Options())

    def test_append_text(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[ActionType(type="append", field="message.text", text=" world")],
        )
        event = run(self.module().run_rule(None, rule, make_event()))
        self.assertEqual("hello world", event.message.segments[0].text)

    def test_input_event_not_mutated(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[ActionType(type="append", field="message.text", text=" world")],
        )
        original = make_event()
        run(self.module().run_rule(None, rule, original))
        self.assertEqual("hello", original.message.segments[0].text)

    def test_replace_and_delete(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[
                ActionType(type="replace", field="message.text", match="l+", replace="L"),
                ActionType(type="delete", field="message.text", match="o"),
            ],
        )
        event = run(self.module().run_rule(None, rule, make_event()))
        self.assertEqual("heL", event.message.segments[0].text)

    def test_field_not_allowed_at_point(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[ActionType(type="append", field="llm.text", text="x")],
        )
        with self.assertRaises(RuleError) as ctx:
            run(self.module().run_rule(None, rule, make_event()))
        self.assertIn("cannot be edited", str(ctx.exception))

    def test_template_rendering(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[
                ActionType(type="append", field="message.text", text="-{{session.id}}"),
                ActionType(
                    type="append", field="message.text", text="-{{tool.name}}"
                ),
            ],
        )
        event = make_event()
        event.session.id = "sess1"
        event.tool.name = "shell"
        updated = run(self.module().run_rule(None, rule, event))
        self.assertEqual("hello-sess1-shell", updated.message.segments[0].text)

    def test_send_action_appends_outputs(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[
                ActionType(
                    type="send",
                    kind="text",
                    text="ping",
                    timing="after_assistant",
                    target=_target("cli", "s9"),
                )
            ],
        )
        event = run(self.module().run_rule(None, rule, make_event()))
        self.assertEqual(1, len(event.outputs))
        self.assertEqual("ping", event.outputs[0].text)
        self.assertEqual("s9", event.outputs[0].target.scope_id)
        self.assertEqual(
            delivery.DELIVERY_AFTER_ASSISTANT, delivery.delivery_timing(event.outputs[0])
        )

    def test_send_action_rejects_quick_plus_outputs(self) -> None:
        action = ActionType(
            type="send",
            kind="text",
            text="a",
            outputs=[__import__("atri_x.hook.output", fromlist=["Segment"]).Segment(kind="text", text="b")],
        )
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[action],
        )
        with self.assertRaises(RuleError):
            run(self.module().run_rule(None, rule, make_event()))

    def test_unsupported_action(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[ActionType(type="dance")],
        )
        with self.assertRaises(RuleError) as ctx:
            run(self.module().run_rule(None, rule, make_event()))
        self.assertIn("unsupported action type", str(ctx.exception))

    def test_consume_and_stop_propagation(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            consume=True,
            stop_propagation=True,
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        event = run(self.module().run_rule(None, rule, make_event()))
        self.assertTrue(event.control.consume)
        self.assertTrue(event.control.stop_propagation)

    def test_roles_gate(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            roles=["superadmin"],
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        event = run(self.module().run_rule(None, rule, make_event()))
        self.assertEqual("hello", event.message.segments[0].text)

    def test_matches_roles_by_group_role(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            group_roles=["owner"],
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        event = make_event()
        event.actor.group_role = "owner"
        updated = run(self.module().run_rule(None, rule, event))
        self.assertEqual("hello!", updated.message.segments[0].text)


def _target(platform: str, scope_id: str):
    from atri_x.hook.output import Target

    return Target(platform=platform, scope_id=scope_id)


class TestModuleRegistration(unittest.TestCase):
    def test_register_and_run_through_manager(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        module = Module(rules=[rule], opts=Options())
        manager = DefaultManager()
        module.register_hooks(manager)
        infos = manager.list()
        self.assertEqual(1, len(infos))
        self.assertEqual("r", infos[0].name)
        self.assertEqual(DEFAULT_PRIORITY, infos[0].priority)
        self.assertIn("on: agent.input.prepared", infos[0].detail)
        updated = run(manager.run(None, make_event()))
        self.assertEqual("hello!", updated.message.segments[0].text)

    def test_role_partition_names(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            roles=["superadmin", "user"],
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        module = Module(rules=[rule], opts=Options())
        manager = DefaultManager()
        module.register_hooks(manager)
        names = sorted(info.name for info in manager.list())
        self.assertEqual(["r.role.1", "r.role.2"], names)
        self.assertIn("role partition 1/2", manager.list()[0].detail)

    def test_disabled_rule_not_registered(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            enabled=False,
            match=[Condition(op="always")],
            actions=[ActionType(type="append", field="message.text", text="!")],
        )
        module = Module(rules=[rule], opts=Options())
        manager = DefaultManager()
        module.register_hooks(manager)
        self.assertEqual([], manager.list())

    def test_worker_rule_routes_to_runtime(self) -> None:
        class FakeRuntime:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def handle(self, ctx, id, event, defaults):
                self.calls.append((id, defaults.consume, defaults.stop_propagation))
                event.metadata["routed"] = True
                return event

        runtime = FakeRuntime()
        rule = Rule(
            name="r",
            on=Point.LLM_TURN_PREPARED,
            match=[Condition(op="always")],
            consume=True,
            stop_propagation=False,
        )
        rule.source.runtime_id = "demo"
        module = Module(rules=[rule], opts=Options(runtime=runtime))
        manager = DefaultManager()
        module.register_hooks(manager)
        event = run(manager.run(None, make_event(Point.LLM_TURN_PREPARED)))
        self.assertEqual([("demo", True, False)], runtime.calls)
        self.assertTrue(event.metadata.get("routed"))

    def test_new_module_from_config_dir(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        try:
            (TMP / CONFIG_FILE).write_text(
                '[[rules]]\nname = "r"\non = "agent.input.prepared"\nalways = true\naction = "append"\nfield = "message.text"\ntext = "!"\n',
                encoding="utf-8",
                newline="\n",
            )
            module = Module(
                **{}
            ) if False else None
            from atri_x.hook.rules.rules import NewModule

            module = NewModule(Options(config_dir=str(TMP)))
            self.assertEqual(1, len(module.rules))
            manager = DefaultManager()
            module.register_hooks(manager)
            updated = run(manager.run(None, make_event()))
            self.assertEqual("hello!", updated.message.segments[0].text)
        finally:
            shutil.rmtree(TMP, ignore_errors=True)


class TestDetail(unittest.TestCase):
    def test_format(self) -> None:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            priority=5,
            roles=["user"],
            consume=True,
            always=True,
            actions=[
                ActionType(
                    type="exec",
                    action_name="run",
                    command=["python", "x.py"],
                    timeout_seconds=3,
                )
            ],
        )
        text = format_rule_detail(rule)
        self.assertIn("on: agent.input.prepared", text)
        self.assertIn("match: always", text)
        self.assertIn("roles: user", text)
        self.assertIn('action[1]: exec (run)', text)
        self.assertIn('command=["python" "x.py"]', text)
        self.assertIn("consume: true", text)
        self.assertIn("priority: 5", text)

    def test_description_fallback(self) -> None:
        rule = Rule(name="r")
        rule.source.plugin_description = "from plugin"
        self.assertEqual("from plugin", rule_description(rule))

    def test_target_string(self) -> None:
        self.assertEqual(
            "platform=cli,scope_id=s,superadmins=true",
            target_string(_target("cli", "s").__class__(platform="cli", scope_id="s", superadmins=True)),
        )


class TestExecAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(TMP, exist_ok=True)
        (TMP / "plugin_ok.py").write_text(PLUGIN_OK, encoding="utf-8", newline="\n")
        (TMP / "plugin_fail.py").write_text(PLUGIN_FAIL, encoding="utf-8", newline="\n")
        (TMP / "plugin_hang.py").write_text(PLUGIN_HANG, encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def module(self) -> Module:
        return Module(opts=Options(config_dir=str(TMP)))

    def rule(self, script: str, timeout: int = 10) -> Rule:
        rule = Rule(
            name="r",
            on=Point.AGENT_INPUT_PREPARED,
            match=[Condition(op="always")],
            actions=[
                ActionType(
                    type="exec",
                    action_name="go",
                    command=[sys.executable, str(TMP / script)],
                    timeout_seconds=timeout,
                )
            ],
        )
        rule.source.base_dir = str(TMP)
        rule.source.config_path = str(TMP / CONFIG_FILE)
        return rule

    def test_successful_one_shot(self) -> None:
        event = run(self.module().run_rule(None, self.rule("plugin_ok.py"), make_event()))
        self.assertEqual("rewritten-by-plugin", event.message.segments[0].text)

    def test_failure_reports_stderr(self) -> None:
        with self.assertRaises(RuleError) as ctx:
            run(self.module().run_rule(None, self.rule("plugin_fail.py"), make_event()))
        message = str(ctx.exception)
        # 插件在读 system.init 之后就退出：宿主先看到 stdout 关闭，再把 stderr 尾巴接上。
        self.assertIn("closed stdout", message)
        self.assertIn("boom from plugin", message)

    def test_timeout(self) -> None:
        with self.assertRaises(RuleError) as ctx:
            run(self.module().run_rule(None, self.rule("plugin_hang.py", 1), make_event()))
        self.assertIn("timed out after 1s", str(ctx.exception))


class TestHookProcessGroup(unittest.TestCase):
    """POSIX 上 hook 插件必须自成进程组，否则超时清理会杀到宿主自己。

    真实故障：Linux 上 ``test_timeout``（插件故意挂住）触发超时清理时，
    ``os.killpg(os.getpgid(child))`` 命中的是**宿主自己所在的进程组**——CI 分片
    runner（乃至整个 job）被 SIGKILL，job 一直 in_progress、日志都拿不到。
    修复两件事：子进程 ``start_new_session=True``；杀进程组前拒绝朝宿主自己的组开枪。
    """

    def test_spawn_kwargs_only_detach_on_posix(self) -> None:
        from atri_x.hook.protocol.stream import detached_process_kwargs

        self.assertEqual(detached_process_kwargs("nt"), {})
        self.assertEqual(detached_process_kwargs("posix"), {"start_new_session": True})

    def test_kill_process_group_never_targets_host_group(self) -> None:
        from atri_x.hook.protocol.stream import kill_process_group

        if os.name == "nt":
            self.skipTest("killpg 是 POSIX 语义")
        # 传宿主自己的 pid：它与宿主同组，守卫必须什么都不做（否则这里把测试进程打死）。
        kill_process_group(os.getpid())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
