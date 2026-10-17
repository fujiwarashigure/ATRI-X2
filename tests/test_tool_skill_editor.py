"""``create_atrix_skill`` / ``read_atrix_skill`` / ``modify_atrix_skill`` /
``finalize_atrix_skill`` 四个编辑器工具的测试。

对应 Go：``creator_test.go``、``modify_atrix_skill_test.go``、
``finalize_atrix_skill_test.go``。子进程与命令执行、管理器、``agent_skill`` 与
``go_skill_run`` 在 ``tests/test_tool_skill_tools.py``；解析层在
``tests/test_tool_skill.py``；夹具见 ``tests/_skill_fixtures.py``。
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
# create / read / modify / finalize
# ---------------------------------------------------------------------------

class TestCreateAtrixSkill(SkillTestBase):
    def _manager(self) -> Manager:
        from atri_x.tool.tool import new_registry

        os.makedirs(os.path.join(self.root, "go"), exist_ok=True)
        return new_manager(self.root, new_registry())

    def test_creates_text_only_skill(self) -> None:
        from atri_x.tool.tool import CallRequest

        manager = self._manager()
        tool = CreateAtrixSkillTool(manager)
        payload = json.dumps(
            {
                "name": "demo",
                "description": "演示能力",
                "risk": "low",
                "atriyph": VALID_ATRIYPH,
            }
        )
        result = asyncio.run(tool.call(None, CallRequest(arguments=payload)))
        self.assertIn("created Atriyph skill demo", result.content)
        path = os.path.join(self.root, "go", "demo", SKILL_FILE_NAME)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("#skill demo - 演示能力"))
        self.assertIn("** risk low", text)
        _, ok = manager.registry.get("demo")
        self.assertTrue(ok)

    def test_rejects_duplicates_and_bad_names(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = CreateAtrixSkillTool(self._manager())
        base = {"description": "d", "risk": "low", "atriyph": VALID_ATRIYPH}

        def call(**overrides: Any) -> Any:
            payload = dict(base)
            payload.update(overrides)
            return asyncio.run(tool.call(None, CallRequest(arguments=json.dumps(payload))))

        with self.assertRaises(ValueError):
            call(name="Bad Name")
        with self.assertRaises(ValueError):
            call(name="con")
        with self.assertRaises(ValueError):
            call(name="demo", description=" ")
        with self.assertRaises(ValueError):
            call(name="demo", risk="extreme")
        call(name="demo")
        with self.assertRaises(ValueError):
            call(name="demo")

    def test_rejects_go_source_without_package_main(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = CreateAtrixSkillTool(self._manager())
        payload = json.dumps(
            {
                "name": "demo",
                "description": "d",
                "risk": "low",
                "atriyph": VALID_ATRIYPH,
                "go_source": "func main() {}",
            }
        )
        with self.assertRaises(ValueError):
            asyncio.run(tool.call(None, CallRequest(arguments=payload)))

    def test_rejects_invalid_atriyph(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = CreateAtrixSkillTool(self._manager())
        payload = json.dumps(
            {"name": "demo", "description": "d", "risk": "low", "atriyph": "#task x\n"}
        )
        with self.assertRaises(AtriXError):
            asyncio.run(tool.call(None, CallRequest(arguments=payload)))

    def test_helpers(self) -> None:
        # Go 的 parseGoModVersion 返回去掉 ``go`` 前缀的 ``1.23``；go.mod 写 ``go 1.23``。
        self.assertEqual(parse_go_mod_version("go1.23.4\n"), "1.23")
        self.assertEqual(parse_go_mod_version("go1.21rc1"), "1.21")
        with self.assertRaises(ValueError):
            parse_go_mod_version("1.23")
        with self.assertRaises(ValueError):
            parse_go_mod_version("go")
        self.assertEqual(
            go_mod_content("demo", "1.23"),
            "module github.com/fujiwarashigure/ATRI-X-skill/demo\n\ngo 1.23\n",
        )
        updated = with_atriyph_metadata(VALID_ATRIYPH, "新描述", "critical")
        self.assertEqual(updated.splitlines()[0], "#skill demo - 新描述")
        self.assertEqual(updated.splitlines()[1], "** risk critical")
        validate_skill_name("ok_name-1")
        for bad in ("NUL", "9bad", "Upper", "a" * 65):
            with self.assertRaises(ValueError):
                validate_skill_name(bad)

    def test_info_depends_on_sibling_tools(self) -> None:
        info = CreateAtrixSkillTool().info()
        self.assertEqual(
            info.depends_on,
            ["read_atrix_skill", "modify_atrix_skill", "finalize_atrix_skill"],
        )
        self.assertEqual(info.risk, "high")


class TestReadAndModifySkill(SkillTestBase):
    def _manager(self) -> Manager:
        from atri_x.tool.tool import new_registry

        make_go_skill(self.root, "demo")
        manager = new_manager(self.root, new_registry())
        asyncio.run(manager.ensure_loaded(None))
        return manager

    def test_read_returns_revision_and_lines(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ReadAtrixSkillTool(self._manager())
        result = asyncio.run(
            tool.call(
                None,
                CallRequest(arguments='{"name": "demo", "target": "skill_atriyph"}'),
            )
        )
        self.assertIn("revision: ", result.content)
        self.assertIn("1: #skill demo - demo", result.content)

    def test_read_line_range(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ReadAtrixSkillTool(self._manager())
        result = asyncio.run(
            tool.call(
                None,
                CallRequest(
                    arguments='{"name": "demo", "start_line": 2, "end_line": 2}'
                ),
            )
        )
        self.assertIn("2: ** risk medium", result.content)
        self.assertNotIn("1: #skill", result.content)

    def test_read_rejects_bad_target(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ReadAtrixSkillTool(self._manager())
        with self.assertRaises(ValueError):
            asyncio.run(
                tool.call(None, CallRequest(arguments='{"name": "demo", "target": "nope"}'))
            )
        with self.assertRaises(Exception):
            asyncio.run(
                tool.call(
                    None,
                    CallRequest(arguments='{"name": "demo", "target": "code_source"}'),
                )
            )

    def test_modify_writes_and_returns_diff(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ModifyAtrixSkillTool(self._manager())
        path = os.path.join(self.root, "go", "demo", "main.go")
        write_text(path, "package main\n\nfunc main() {}\n")
        request = CallRequest(
            arguments=json.dumps(
                {
                    "name": "demo",
                    "target": "code_source",
                    "edits": [
                        {
                            "operation": "replace_text",
                            "old_text": "func main() {}",
                            "new_text": "func main() { /*x*/ }",
                        }
                    ],
                }
            )
        )
        asyncio.run(tool.preflight_confirmation(None, request))
        self.assertIn("预检 diff:", asyncio.run(tool.risk_detail(None, request)))
        result = asyncio.run(tool.call(None, request))
        self.assertIn("modified ATRI-X skill demo target code_source", result.content)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("/*x*/", handle.read())

    def test_modify_rejects_unknown_fields(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ModifyAtrixSkillTool(self._manager())
        with self.assertRaises(ValueError):
            asyncio.run(
                tool.call(
                    None,
                    CallRequest(arguments=json.dumps({"name": "demo", "edits": [], "bogus": 1})),
                )
            )

    def test_modify_requires_edits(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ModifyAtrixSkillTool(self._manager())
        with self.assertRaises(ValueError):
            asyncio.run(
                tool.call(None, CallRequest(arguments='{"name": "demo", "edits": []}'))
            )

    def test_modify_validates_atriyph_target(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = ModifyAtrixSkillTool(self._manager())
        request = CallRequest(
            arguments=json.dumps(
                {
                    "name": "demo",
                    "target": "skill_atriyph",
                    "edits": [
                        {
                            "operation": "replace_text",
                            "old_text": "#skill demo - demo",
                            "new_text": "#task nope",
                        }
                    ],
                }
            )
        )
        with self.assertRaises(AtriXError):
            asyncio.run(tool.call(None, request))


class TestFinalizeAtrixSkill(SkillTestBase):
    def _manager(self) -> Manager:
        from atri_x.tool.tool import new_registry

        make_go_skill(self.root, "demo")
        manager = new_manager(self.root, new_registry())
        asyncio.run(manager.ensure_loaded(None))
        return manager

    def test_text_only_skill_reloads(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = FinalizeAtrixSkillTool(self._manager())
        result = asyncio.run(tool.call(None, CallRequest(arguments='{"name": "demo"}')))
        self.assertIn("Atriyph: ok", result.content)
        self.assertIn("Go source: not found, text-only skill", result.content)
        self.assertIn("reload: ok", result.content)

    def test_invalid_atriyph_is_reported_not_raised(self) -> None:
        from atri_x.tool.tool import CallRequest

        manager = self._manager()
        write_text(os.path.join(self.root, "go", "demo", SKILL_FILE_NAME), "#task nope\n")
        result = asyncio.run(
            FinalizeAtrixSkillTool(manager).call(
                None, CallRequest(arguments='{"name": "demo"}')
            )
        )
        self.assertIn("Atriyph: failed", result.content)

    def test_missing_atriyph_file_is_an_error(self) -> None:
        from atri_x.tool.tool import CallRequest

        manager = self._manager()
        os.remove(os.path.join(self.root, "go", "demo", SKILL_FILE_NAME))
        with self.assertRaises(OSError):
            asyncio.run(
                FinalizeAtrixSkillTool(manager).call(
                    None, CallRequest(arguments='{"name": "demo"}')
                )
            )

    def test_rejects_unknown_skill_name(self) -> None:
        from atri_x.tool.tool import CallRequest

        tool = FinalizeAtrixSkillTool(self._manager())
        with self.assertRaises(ValueError):
            asyncio.run(tool.call(None, CallRequest(arguments='{"name": "Bad"}')))
        with self.assertRaises(OSError):
            asyncio.run(tool.call(None, CallRequest(arguments='{"name": "ghost"}')))

    def test_reports_go_source_validation_failure(self) -> None:
        from atri_x.tool.tool import CallRequest

        manager = self._manager()
        write_text(
            os.path.join(self.root, "go", "demo", "main.go"),
            "package other\n\nfunc main() {}\n",
        )
        result = asyncio.run(
            FinalizeAtrixSkillTool(manager).call(
                None, CallRequest(arguments='{"name": "demo"}')
            )
        )
        # 没有 gofmt（本机可能没装 Go）时是 gofmt 失败，装了就轮到 package 校验。
        self.assertTrue(
            "gofmt: failed" in result.content
            or "Go source validation: failed" in result.content
            or "go build: failed" in result.content,
            result.content,
        )
if __name__ == "__main__":
    unittest.main()
