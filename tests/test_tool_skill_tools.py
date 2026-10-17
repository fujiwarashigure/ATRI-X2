"""``atri_x.tool.skill`` 的工具层测试：子进程、管理器与六个 skill 工具。

对应 Go：``command_tool_test.go``、``runner_test.go``、``manager_test.go``、
``agent_skill_tool_test.go``、``creator_test.go``、``modify_atrix_skill_test.go``、
``finalize_atrix_skill_test.go``。

解析层与扫描层在 ``tests/test_tool_skill.py``；夹具见
``tests/_skill_fixtures.py``。所有子进程都用当前解释器（``sys.executable``）
跑真实脚本，覆盖 ``asyncio.create_subprocess_exec`` 这条路径。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any

from atri_x.atriyph import SKILL_FILE_NAME
from atri_x.errors import AtriXError
from atri_x.processenv import Environment
from atri_x.safecontext import with_timeout
from atri_x.tool.skill import (
    AGENT_SKILL_CONFIG_FILE,
    AgentSkillTool,
    Catalog,
    CommandTool,
    CreateAtrixSkillTool,
    FinalizeAtrixSkillTool,
    GoRunner,
    Manager,
    ModifyAtrixSkillTool,
    ReadAtrixSkillTool,
    Record,
    command_arguments,
    new_manager,
    parse_agent_skill_manifest,
    parse_go_runner_args,
    run_command,
    safe_relative_path,
    validate_skill_name,
)
from atri_x.tool.skill.creator import (
    go_mod_content,
    parse_go_mod_version,
    with_atriyph_metadata,
)
from tests._skill_fixtures import (
    CALLABLE_TOML,
    DOC_ONLY_TOML,
    VALID_ATRIYPH,
    SkillTestBase,
    make_agent_skill,
    make_go_skill,
    write_text,
)


def _records(root: str) -> list[Record]:
    from atri_x.tool.skill import FilesystemScanner

    return asyncio.run(FilesystemScanner(root=root).scan_records(None))


# ---------------------------------------------------------------------------
# command_tool
# ---------------------------------------------------------------------------

class TestCommandTool(SkillTestBase):
    def test_argument_ordering_and_types(self) -> None:
        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        raw = json.dumps(
            {
                "input": "hi",
                "count": 3,
                "flag": True,
                "items": ["a", "b"],
                "media_in": "media:" + "a" * 64,
                "nested": {"blob": "media:" + "b" * 64},
            }
        )
        self.assertEqual(
            command_arguments(manifest, raw),
            [
                "--count", "3",
                "--flag", "true",
                "--input", "hi",
                "--items", '["a","b"]',
                "--media", "media:" + "a" * 64,
            ],
        )

    def test_argument_number_keeps_literal(self) -> None:
        manifest = parse_agent_skill_manifest(
            'risk = "low"\ncommand = ["python"]\n'
            "parameters = '''{\"type\":\"object\",\"properties\":{\"n\":{}}}'''\n"
            '[args]\nn = "--n"\n'
        )
        self.assertEqual(command_arguments(manifest, '{"n": 1.50}'), ["--n", "1.50"])

    def test_missing_required_argument(self) -> None:
        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        with self.assertRaises(ValueError) as caught:
            command_arguments(manifest, "{}")
        self.assertIn("required argument input is missing", str(caught.exception))

    def test_null_argument_is_skipped(self) -> None:
        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        self.assertEqual(
            command_arguments(manifest, '{"input": "x", "count": null}'), ["--input", "x"]
        )

    def test_bad_arguments_payload(self) -> None:
        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        with self.assertRaises(ValueError):
            command_arguments(manifest, "[1, 2]")

    def test_call_runs_the_configured_command(self) -> None:
        from atri_x.tool.tool import CallRequest

        make_agent_skill(self.root, "alpha", toml=CALLABLE_TOML)
        record = self._record("alpha")
        tool = CommandTool(record=record, manifest=record.manifest)
        tool.manifest.command = [sys.executable, "echo_args.py"]
        result = asyncio.run(
            tool.call(None, CallRequest(name="alpha", arguments='{"input": "hello"}'))
        )
        self.assertIn("--input hello", result.content)

    def test_call_reports_non_zero_exit(self) -> None:
        from atri_x.tool.tool import CallRequest

        record = self._failing_record()
        tool = CommandTool(record=record, manifest=record.manifest)
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(tool.call(None, CallRequest(name="bad", arguments="{}")))
        self.assertIn("exit code 3", str(caught.exception))

    def test_load_detail_exposes_root(self) -> None:
        make_agent_skill(self.root, "alpha", toml=CALLABLE_TOML)
        record = self._record("alpha")
        tool = CommandTool(record=record, manifest=record.manifest)
        block = asyncio.run(tool.load_detail(None))
        self.assertIn("AgentSkill root: " + record.root, block.content)
        self.assertEqual(tool.activate_tools(), ["agent_skill", "alpha"])

    def test_info_uses_manifest_policy(self) -> None:
        make_agent_skill(self.root, "alpha", toml=CALLABLE_TOML)
        record = self._record("alpha")
        info = CommandTool(record=record, manifest=record.manifest).info()
        self.assertEqual(info.risk, "medium")
        self.assertEqual(info.tags, ["doc"])
        self.assertEqual(info.source, "skill_agent")

    def _record(self, name: str) -> Record:
        return next(record for record in _records(self.root) if record.name == name)

    def _failing_record(self) -> Record:
        directory = os.path.join(self.root, "agent", "bad")
        os.makedirs(directory, exist_ok=True)
        write_text(os.path.join(directory, SKILL_FILE_NAME), "#skill bad\n** risk low\n")
        write_text(os.path.join(directory, "fail.py"), "import sys\nsys.exit(3)\n")
        write_text(
            os.path.join(directory, AGENT_SKILL_CONFIG_FILE),
            "risk = \"low\"\ncommand = ["
            + json.dumps(sys.executable)
            + ', "fail.py"]\n'
            "parameters = '''{\"type\":\"object\",\"properties\":{\"a\":{}}}'''\n"
            '[args]\na = "--a"\n',
        )
        return self._record("bad")


# ---------------------------------------------------------------------------
# runner / process
# ---------------------------------------------------------------------------

class TestRunner(SkillTestBase):
    def test_parse_go_runner_args(self) -> None:
        args = parse_go_runner_args(
            '{"skill_name": "s", "timeout_ms": 1500, "payload": {"a": 1}}'
        )
        self.assertEqual(args.skill_name, "s")
        self.assertEqual(args.timeout_ms, 1500)
        self.assertEqual(json.loads(args.payload), {"a": 1})

    def test_parse_go_runner_args_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            parse_go_runner_args('{"skill_name": "s"}')
        with self.assertRaises(ValueError):
            parse_go_runner_args('{"payload": [1]}')
        with self.assertRaises(ValueError):
            parse_go_runner_args('{"payload": {}, "timeout_ms": "x"}')
        with self.assertRaises(ValueError):
            parse_go_runner_args("not json")

    def test_runner_info_and_schema(self) -> None:
        runner = GoRunner()
        self.assertEqual(runner.name(), "go_skill_run")
        info = runner.info()
        self.assertTrue(info.hidden)
        self.assertEqual(info.source, "builtin")
        schema = runner.schema()
        self.assertEqual(schema.function.parameters["required"], ["skill_name", "payload"])

    def test_assess_risk_uses_record(self) -> None:
        from atri_x.tool.tool import CallRequest

        make_go_skill(self.root, "beta")
        catalog = Catalog()
        catalog.replace(_records(self.root))
        runner = GoRunner(catalog=catalog)
        known = CallRequest(arguments='{"skill_name": "beta", "payload": {}}')
        self.assertEqual(asyncio.run(runner.assess_risk(None, known)).level, "medium")
        unknown = CallRequest(arguments='{"skill_name": "nope", "payload": {}}')
        self.assertEqual(asyncio.run(runner.assess_risk(None, unknown)).level, "high")
        self.assertEqual(asyncio.run(GoRunner().assess_risk(None, unknown)).level, "high")

    def test_call_reports_missing_skill(self) -> None:
        from atri_x.tool.tool import CallRequest

        request = CallRequest(arguments='{"skill_name": "x", "payload": {}}')
        with self.assertRaises(ValueError):
            asyncio.run(GoRunner(catalog=Catalog()).call(None, request))
        with self.assertRaises(ValueError):
            asyncio.run(GoRunner().call(None, request))

    def test_safe_relative_path(self) -> None:
        self.assertEqual(
            safe_relative_path("root", os.path.join("scripts", "ok.py")),
            os.path.join("scripts", "ok.py"),
        )
        for bad in ("", "../evil.py", os.path.abspath("evil.py"), "."):
            with self.assertRaises(ValueError):
                safe_relative_path("root", bad)


class TestProcess(SkillTestBase):
    def test_run_command_captures_output(self) -> None:
        script = os.path.join(self.root, "echo.py")
        write_text(script, "import sys\nprint('out'); print('err', file=sys.stderr)\n")
        command = Environment().command(sys.executable, script)
        output = asyncio.run(run_command(None, command, cwd=self.root))
        self.assertEqual(output.code, 0)
        self.assertIn("out", output.stdout_text())
        self.assertIn("err", output.stderr_text())

    def test_run_command_passes_stdin(self) -> None:
        script = os.path.join(self.root, "read_stdin.py")
        write_text(script, "import sys\nprint(sys.stdin.read().strip().upper())\n")
        command = Environment().command(sys.executable, script)
        output = asyncio.run(
            run_command(None, command, cwd=self.root, stdin_data=b"hello")
        )
        self.assertEqual(output.stdout_text().strip(), "HELLO")

    def test_non_zero_exit_is_reported(self) -> None:
        script = os.path.join(self.root, "fail.py")
        write_text(script, "import sys\nsys.exit(7)\n")
        command = Environment().command(sys.executable, script)
        output = asyncio.run(run_command(None, command, cwd=self.root))
        self.assertEqual(output.code, 7)
        self.assertIsNone(output.ctx_error)

    def test_cancelled_context_kills_the_process(self) -> None:
        script = os.path.join(self.root, "sleep.py")
        write_text(script, "import time\ntime.sleep(30)\n")

        async def _run() -> Any:
            command = Environment().command(sys.executable, script)
            return await run_command(
                with_timeout(None, 0.2), command, cwd=self.root, wait_delay=2.0
            )

        output = asyncio.run(_run())
        self.assertIsNotNone(output.ctx_error)
        self.assertNotEqual(output.code, 0)


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------

class TestManager(SkillTestBase):
    def _registry(self) -> Any:
        from atri_x.tool.tool import new_registry

        return new_registry()

    def test_ensure_loaded_registers_tools(self) -> None:
        make_agent_skill(self.root, "alpha", toml=DOC_ONLY_TOML)
        registry = self._registry()
        manager = new_manager(self.root, registry)
        asyncio.run(manager.ensure_loaded(None))
        self.assertTrue(manager.loaded)
        _, ok = registry.get("alpha")
        self.assertTrue(ok)
        _, found = manager.catalog.get("alpha")
        self.assertTrue(found)
        asyncio.run(manager.ensure_loaded(None))

    def test_ensure_loaded_deduplicates_concurrent_calls(self) -> None:
        make_agent_skill(self.root, "alpha")
        manager = new_manager(self.root, self._registry())
        calls: list[int] = []
        original = manager.scanner.reload

        async def counting(ctx: Any, registry: Any = None) -> None:
            calls.append(1)
            await asyncio.sleep(0.05)
            await original(ctx, registry)

        manager.scanner.reload = counting  # type: ignore[method-assign]

        async def _run() -> None:
            await asyncio.gather(*[manager.ensure_loaded(None) for _ in range(5)])

        asyncio.run(_run())
        self.assertEqual(len(calls), 1)

    def test_concurrent_ensure_loaded_propagates_failure(self) -> None:
        make_agent_skill(self.root, "dup")
        make_go_skill(self.root, "dup")
        manager = new_manager(self.root, self._registry())

        async def _run() -> list[BaseException]:
            results = await asyncio.gather(
                *[manager.ensure_loaded(None) for _ in range(3)], return_exceptions=True
            )
            return [item for item in results if isinstance(item, BaseException)]

        errors = asyncio.run(_run())
        self.assertEqual(len(errors), 3)
        self.assertFalse(manager.loaded)

    def test_reload_and_remove(self) -> None:
        make_agent_skill(self.root, "alpha")
        registry = self._registry()
        manager = new_manager(self.root, registry)
        asyncio.run(manager.reload(None))
        _, ok = registry.get("alpha")
        self.assertTrue(ok)
        asyncio.run(manager.remove(None, "alpha"))
        _, ok = registry.get("alpha")
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.root, "agent", "alpha")))
        with self.assertRaises(ValueError):
            asyncio.run(manager.remove(None, "ghost"))

    def test_write_agent_skill_config(self) -> None:
        make_agent_skill(self.root, "alpha")
        registry = self._registry()
        manager = new_manager(self.root, registry)
        asyncio.run(manager.ensure_loaded(None))
        asyncio.run(manager.write_agent_skill_config(None, "alpha", 'risk = "medium"\n'))
        record, _ = manager.catalog.get("alpha")
        self.assertEqual(record.risk, "medium")
        with open(
            os.path.join(self.root, "agent", "alpha", AGENT_SKILL_CONFIG_FILE),
            encoding="utf-8",
        ) as handle:
            self.assertEqual(handle.read(), 'risk = "medium"\n')

    def test_write_agent_skill_config_validates_input(self) -> None:
        make_agent_skill(self.root, "alpha")
        manager = new_manager(self.root, self._registry())
        asyncio.run(manager.ensure_loaded(None))
        for content in ("   ", "bogus = 1\n"):
            with self.assertRaises(ValueError):
                asyncio.run(manager.write_agent_skill_config(None, "alpha", content))
        with self.assertRaises(ValueError):
            asyncio.run(
                manager.write_agent_skill_config(None, "Bad Name", 'risk = "low"\n')
            )
        with self.assertRaises(ValueError):
            asyncio.run(
                manager.write_agent_skill_config(None, "ghost", 'risk = "low"\n')
            )

    def test_start_delayed_reload(self) -> None:
        make_agent_skill(self.root, "alpha")
        manager = new_manager(self.root, self._registry())

        async def _run() -> None:
            await asyncio.wait_for(manager.start_delayed_reload(None, 0.01), 2.0)

        asyncio.run(_run())
        self.assertTrue(manager.loaded)


# ---------------------------------------------------------------------------
# agent_skill 工具
# ---------------------------------------------------------------------------

class TestAgentSkillTool(SkillTestBase):
    def _manager(self) -> Manager:
        from atri_x.tool.tool import new_registry

        make_agent_skill(self.root, "alpha")
        manager = new_manager(self.root, new_registry())
        asyncio.run(manager.ensure_loaded(None))
        return manager

    def test_read_returns_status_and_format(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = AgentSkillTool(self._manager())
        result = asyncio.run(
            tool.call(None, CallRequest(arguments='{"action": "read", "name": "alpha"}'))
        )
        self.assertIn("Status: document-only", result.content)
        self.assertIn("No current TOML.", result.content)
        self.assertIn("Format:", result.content)
        self.assertIn("[args]", result.content)

    def test_read_shows_existing_toml_and_status(self) -> None:
        from atri_x.tool.tool import CallRequest

        manager = self._manager()
        asyncio.run(
            manager.write_agent_skill_config(None, "alpha", 'risk = "medium"\n')
        )
        tool = AgentSkillTool(manager)
        result = asyncio.run(
            tool.call(None, CallRequest(arguments='{"action": "read", "name": "alpha"}'))
        )
        self.assertIn("Policy: configured", result.content)
        self.assertIn('risk = "medium"', result.content)

    def test_write_then_read(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = AgentSkillTool(self._manager())
        payload = json.dumps(
            {"action": "write", "name": "alpha", "toml": "superadmin_only = true\n"}
        )
        result = asyncio.run(tool.call(None, CallRequest(arguments=payload)))
        self.assertIn("reloaded skills", result.content)
        read = asyncio.run(
            tool.call(None, CallRequest(arguments='{"action": "read", "name": "alpha"}'))
        )
        self.assertIn("superadmin_only = true", read.content)

    def test_rejects_bad_arguments(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = AgentSkillTool(self._manager())
        for arguments in (
            '{"action": "delete", "name": "alpha"}',
            '{"action": "read", "name": "ghost"}',
            '{"action": "write", "name": "alpha"}',
            "not json",
        ):
            with self.assertRaises(ValueError):
                asyncio.run(tool.call(None, CallRequest(arguments=arguments)))

    def test_preflight_and_risk_detail(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = AgentSkillTool(self._manager())
        request = CallRequest(arguments='{"action": "read", "name": "alpha"}')
        asyncio.run(tool.preflight_confirmation(None, request))
        self.assertIn("读取 AgentSkill alpha", asyncio.run(tool.risk_detail(None, request)))
        write_request = CallRequest(
            arguments=json.dumps(
                {"action": "write", "name": "alpha", "toml": 'risk = "low"\n'}
            )
        )
        self.assertIn(
            "写入 AgentSkill alpha", asyncio.run(tool.risk_detail(None, write_request))
        )

    def test_info_is_hidden_and_high_risk(self) -> None:
        info = AgentSkillTool().info()
        self.assertTrue(info.hidden)
        self.assertEqual(info.risk, "high")
        self.assertEqual(info.name, "agent_skill")
if __name__ == "__main__":
    unittest.main()
