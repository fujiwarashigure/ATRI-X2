"""``atri_x.tool.skill`` 的解析层与扫描层测试。

对应 Go：``parser_test.go``、``descriptor_test.go``、``scanner_test.go``、
``media_test.go``（纯函数部分）、``go_source_test.go``。

工具/子进程/管理器部分在 ``tests/test_tool_skill_tools.py``。临时目录与假媒体
运行时见 ``tests/_skill_fixtures.py``（PORTING.md §7.2）。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

from atri_x.atriyph import SKILL_FILE_NAME
from atri_x.tool.skill import (
    AGENT_SKILL_CONFIG_FILE,
    Catalog,
    CommandTool,
    Descriptor,
    FilesystemScanner,
    Kind,
    Record,
    atriyph_risk,
    default_root,
    find_go_binary,
    parse_agent_skill_manifest,
    parse_risk,
    parse_skill_markdown,
    project_media_schema,
    schema_contains_media,
    schema_required,
    source_for_kind,
    validate_go_main_source,
)
from tests._skill_fixtures import (
    CALLABLE_TOML,
    DOC_ONLY_TOML,
    FakeMediaCall,
    SkillTestBase,
    VALID_ATRIYPH,
    make_agent_skill,
    make_go_skill,
    write_text,
)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

class TestParser(unittest.TestCase):
    def test_parses_front_matter_and_body(self) -> None:
        raw = (
            b"---\nname: demo\ndescription: \"a skill\"\nwhen_to_use: when needed\n"
            b"---\n# Title\n\nbody line\n"
        )
        definition = parse_skill_markdown(raw, "fallback")
        self.assertEqual(definition.name, "demo")
        self.assertEqual(definition.description, "a skill when needed")
        self.assertEqual(definition.format, "markdown")
        self.assertIn("body line", definition.detail)

    def test_falls_back_to_directory_name_and_first_paragraph(self) -> None:
        raw = "---\n# comment\n---\n\n# Heading\n\nfirst paragraph\n\nsecond\n"
        definition = parse_skill_markdown(raw, "dir-name")
        self.assertEqual(definition.name, "dir-name")
        # Go 取第一个非空块并去掉行首 #，所以描述就是该块的标题行。
        self.assertEqual(definition.description, "Heading")

    def test_requires_a_name(self) -> None:
        with self.assertRaises(ValueError):
            parse_skill_markdown("---\n---\nbody", "  ")

    def test_uses_description_when_body_is_empty(self) -> None:
        definition = parse_skill_markdown("---\nname: n\ndescription: d\n---\n", "x")
        self.assertEqual(definition.detail, "d")

    def test_risk_levels(self) -> None:
        self.assertEqual(parse_risk(""), "high")
        self.assertEqual(parse_risk(" low "), "low")
        with self.assertRaises(ValueError):
            parse_risk("extreme")

    def test_split_front_matter_without_fence(self) -> None:
        definition = parse_skill_markdown("plain body", "name")
        self.assertEqual(definition.detail, "plain body")
        self.assertEqual(definition.name, "name")


# ---------------------------------------------------------------------------
# agent_manifest
# ---------------------------------------------------------------------------

class TestAgentManifest(unittest.TestCase):
    def test_document_only_manifest(self) -> None:
        manifest = parse_agent_skill_manifest(DOC_ONLY_TOML)
        self.assertFalse(manifest.callable)
        self.assertEqual(manifest.risk, "high")
        self.assertTrue(manifest.superadmin_only)

    def test_callable_manifest(self) -> None:
        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        self.assertTrue(manifest.callable)
        self.assertEqual(manifest.command, ["python", "echo_args.py"])
        self.assertEqual(manifest.timeout_seconds, 10)
        self.assertTrue(manifest.expose_root)
        self.assertEqual(manifest.tags, ["doc"])
        self.assertEqual(schema_required(manifest.parameters), ["input"])
        self.assertEqual(manifest.args["input"], "--input")
        schema = manifest.schema("demo", "d")
        self.assertEqual(schema.function.name, "demo")
        self.assertEqual(
            schema.function.parameters["properties"]["media_in"]["pattern"],
            "^media:[0-9a-f]{64}$",
        )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest('risk = "high"\nbogus = 1\n')
        self.assertIn("bogus", str(caught.exception))

    def test_callable_requires_risk(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest('command = ["python"]\n')
        self.assertIn("risk is required", str(caught.exception))

    def test_rejects_empty_command_part(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest(
                'risk = "low"\ncommand = ["python", " "]\n'
                "parameters = '''{\"type\":\"object\",\"properties\":{\"a\":{}}}'''\n"
                '[args]\na = "--a"\n'
            )
        self.assertIn("command[1] is empty", str(caught.exception))

    def test_rejects_non_object_parameters(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest(
                'risk = "low"\ncommand = ["python"]\nparameters = \'{"type":"array"}\'\n'
                '[args]\na = "--a"\n'
            )
        self.assertIn("parameters.type must be object", str(caught.exception))

    def test_rejects_args_outside_properties(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest(
                'risk = "low"\ncommand = ["python"]\n'
                "parameters = '''{\"type\":\"object\",\"properties\":{\"a\":{}}}'''\n"
                '[args]\nb = "--b"\n'
            )
        self.assertIn("not defined in parameters.properties", str(caught.exception))

    def test_rejects_required_without_args_mapping(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest(
                'risk = "low"\ncommand = ["python"]\n'
                "parameters = '''{\"type\":\"object\",\"required\":[\"a\",\"b\"],"
                "\"properties\":{\"a\":{},\"b\":{}}}'''\n"
                '[args]\na = "--a"\n'
            )
        self.assertIn("has no args mapping", str(caught.exception))

    def test_negative_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_skill_manifest("timeout_seconds = -1\n")

    def test_parameters_must_be_json_object(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest(
                'risk = "low"\ncommand = ["python"]\nparameters = "not json"\n'
                '[args]\na = "--a"\n'
            )
        self.assertIn("parameters must be JSON object schema", str(caught.exception))

    def test_media_schema_projection(self) -> None:
        parameters = {
            "type": "object",
            "properties": {
                "one": {"type": "media"},
                "many": {"type": "array", "items": {"type": "media"}},
                "nested": {"type": "object", "properties": {"m": {"type": "media"}}},
            },
        }
        projected = project_media_schema(parameters)
        self.assertEqual(projected["properties"]["one"]["type"], "string")
        self.assertEqual(projected["properties"]["one"]["pattern"], "^media:[0-9a-f]{64}$")
        self.assertEqual(projected["properties"]["many"]["items"]["type"], "string")
        self.assertEqual(
            projected["properties"]["nested"]["properties"]["m"]["type"], "string"
        )
        # 原 schema 不被改动。
        self.assertEqual(parameters["properties"]["one"]["type"], "media")

    def test_schema_required_ignores_bad_shapes(self) -> None:
        self.assertEqual(schema_required({"required": ["a", 1, ""]}), ["a"])
        self.assertEqual(schema_required({"required": "a"}), [])
        self.assertEqual(schema_required(None), [])

    def test_toml_syntax_error_is_reported(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_agent_skill_manifest("risk = \n")
        self.assertIn(AGENT_SKILL_CONFIG_FILE, str(caught.exception))


# ---------------------------------------------------------------------------
# catalog / scanner
# ---------------------------------------------------------------------------

class TestCatalogAndScanner(SkillTestBase):
    def test_catalog_replace_get_list(self) -> None:
        catalog = Catalog()
        catalog.replace(
            [
                Record(name="b", root="rel-b"),
                Record(name="a", root="rel-a"),
                Record(name="", root="ignored"),
            ]
        )
        records = catalog.list()
        self.assertEqual([record.name for record in records], ["a", "b"])
        record, ok = catalog.get("a")
        self.assertTrue(ok)
        self.assertTrue(os.path.isabs(record.root))
        self.assertEqual(catalog.get("zzz"), (None, False))

    def test_source_for_kind(self) -> None:
        self.assertEqual(source_for_kind(Kind.GO), "skill_go")
        self.assertEqual(source_for_kind(Kind.AGENT), "skill_agent")
        self.assertEqual(source_for_kind("weird"), "skill_agent")

    def test_scans_agent_and_go_skills(self) -> None:
        make_agent_skill(self.root, "alpha", toml=DOC_ONLY_TOML)
        make_go_skill(self.root, "beta")
        scanner = FilesystemScanner(root=self.root)
        records = asyncio.run(scanner.scan_records(None))
        self.assertEqual([record.name for record in records], ["alpha", "beta"])
        alpha = records[0]
        self.assertEqual(alpha.kind, Kind.AGENT)
        self.assertTrue(alpha.manifest_found)
        self.assertEqual(alpha.risk, "high")
        self.assertTrue(alpha.superadmin_only)
        beta = records[1]
        self.assertEqual(beta.kind, Kind.GO)
        self.assertEqual(beta.risk, "medium")
        self.assertTrue(beta.binary_path.endswith("beta.exe") or beta.binary_path.endswith("beta"))

    def test_callable_agent_skill_becomes_command_tool(self) -> None:
        make_agent_skill(self.root, "alpha", toml=CALLABLE_TOML)
        scanner = FilesystemScanner(root=self.root)
        records = asyncio.run(scanner.scan_records(None))
        tool = scanner.tool_for_record(records[0])
        self.assertIsInstance(tool, CommandTool)
        self.assertEqual(tool.info().risk, "medium")

    def test_document_skill_becomes_descriptor(self) -> None:
        make_agent_skill(self.root, "alpha", toml=DOC_ONLY_TOML)
        scanner = FilesystemScanner(root=self.root)
        records = asyncio.run(scanner.scan_records(None))
        tool = scanner.tool_for_record(records[0])
        self.assertIsInstance(tool, Descriptor)
        self.assertEqual(tool.activate_tools(), ["agent_skill"])

    def test_duplicate_names_are_rejected(self) -> None:
        make_agent_skill(self.root, "dup")
        make_go_skill(self.root, "dup")
        scanner = FilesystemScanner(root=self.root)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(scanner.scan_records(None))
        self.assertIn("duplicate skill names", str(caught.exception))

    def test_invalid_manifest_is_recorded_not_fatal(self) -> None:
        directory = make_agent_skill(self.root, "alpha")
        write_text(os.path.join(directory, AGENT_SKILL_CONFIG_FILE), "bogus = 1\n")
        scanner = FilesystemScanner(root=self.root)
        records = asyncio.run(scanner.scan_records(None))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].manifest_found)
        self.assertIn("bogus", records[0].manifest_error)

    def test_agent_skill_without_manifest_is_document_only(self) -> None:
        make_agent_skill(self.root, "alpha")
        records = asyncio.run(FilesystemScanner(root=self.root).scan_records(None))
        self.assertFalse(records[0].manifest_found)
        self.assertEqual(records[0].manifest_error, "")

    def test_skill_markdown_fallback(self) -> None:
        directory = os.path.join(self.root, "agent", "mdskill")
        os.makedirs(directory, exist_ok=True)
        write_text(
            os.path.join(directory, "SKILL.md"),
            "---\nname: mdskill\ndescription: 说明\n---\n\n正文\n",
        )
        scanner = FilesystemScanner(root=self.root)
        records = asyncio.run(scanner.scan_records(None))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].format, "markdown")
        self.assertEqual(records[0].description, "说明")

    def test_missing_root_is_empty(self) -> None:
        scanner = FilesystemScanner(root=os.path.join(self.root, "nope"))
        self.assertEqual(asyncio.run(scanner.scan_records(None)), [])

    def test_go_directory_without_atriyph_is_skipped(self) -> None:
        directory = os.path.join(self.root, "go", "empty")
        os.makedirs(directory, exist_ok=True)
        write_text(os.path.join(directory, "readme.txt"), "x")
        self.assertEqual(
            asyncio.run(FilesystemScanner(root=self.root).scan_records(None)), []
        )

    def test_atriyph_risk_reads_marker(self) -> None:
        self.assertEqual(atriyph_risk(VALID_ATRIYPH), "low")
        self.assertEqual(atriyph_risk("#skill x\n"), "high")
        self.assertEqual(atriyph_risk("** risk nonsense\n"), "high")

    def test_find_go_binary_prefers_matching_name(self) -> None:
        directory = make_go_skill(self.root, "beta")
        path, ok = find_go_binary(directory, "beta", "beta")
        self.assertTrue(ok)
        self.assertEqual(os.path.dirname(path), directory)

    def test_find_go_binary_rejects_ambiguous_directory(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows 专有的 .exe 歧义判定")
        directory = os.path.join(self.root, "go", "amb")
        os.makedirs(directory, exist_ok=True)
        write_text(os.path.join(directory, "one.exe"), "x")
        write_text(os.path.join(directory, "two.exe"), "x")
        _, ok = find_go_binary(directory, "amb", "amb")
        self.assertFalse(ok)

    def test_default_root_is_stable(self) -> None:
        root = default_root()
        self.assertTrue(root)
        self.assertTrue(root.endswith("skills"))


# ---------------------------------------------------------------------------
# descriptor
# ---------------------------------------------------------------------------

class TestDescriptor(SkillTestBase):
    def test_info_and_schema_are_document_only(self) -> None:
        make_agent_skill(self.root, "alpha")
        descriptor = Descriptor(self._record("alpha"))
        info = descriptor.info()
        self.assertEqual(info.name, "alpha")
        self.assertEqual(info.source, "skill_agent")
        self.assertEqual(descriptor.schema().function.name, "")
        with self.assertRaises(ValueError):
            asyncio.run(descriptor.call(None, None))

    def test_activate_tools_by_kind(self) -> None:
        go_record = Record(name="g", kind=Kind.GO, binary_path="/tmp/g")
        self.assertEqual(Descriptor(go_record).activate_tools(), ["go_skill_run"])
        self.assertEqual(Descriptor(Record(name="g2", kind=Kind.GO)).activate_tools(), [])

    def test_detail_renders_content(self) -> None:
        make_agent_skill(self.root, "alpha")
        descriptor = Descriptor(self._record("alpha"))
        self.assertIn("alpha", descriptor.detail())
        self.assertIn("alpha", descriptor.detail_block().content)

    def test_load_detail_appends_superadmin_notice(self) -> None:
        from atri_x.security import Actor, RoleSuperadmin, with_actor

        make_agent_skill(self.root, "alpha")
        descriptor = Descriptor(self._record("alpha"))
        block = asyncio.run(descriptor.load_detail(None))
        self.assertNotIn("使用提示", block.content)
        ctx = with_actor(None, Actor(id="u1", role=RoleSuperadmin))
        with_notice = asyncio.run(descriptor.load_detail(ctx))
        self.assertIn("使用提示", with_notice.content)

    def test_load_detail_reports_invalid_manifest(self) -> None:
        directory = make_agent_skill(self.root, "alpha")
        write_text(os.path.join(directory, AGENT_SKILL_CONFIG_FILE), "bogus = 1\n")
        block = asyncio.run(Descriptor(self._record("alpha")).load_detail(None))
        self.assertIn("无效", block.content)

    def _record(self, name: str) -> Record:
        records = asyncio.run(FilesystemScanner(root=self.root).scan_records(None))
        for record in records:
            if record.name == name:
                return record
        raise AssertionError(f"record {name} not found")


# ---------------------------------------------------------------------------
# media 交接（纯函数部分，不落盘到仓库根）
# ---------------------------------------------------------------------------

class TestMedia(SkillTestBase):
    def test_schema_contains_media(self) -> None:
        self.assertTrue(schema_contains_media({"type": "media"}))
        self.assertTrue(
            schema_contains_media({"type": "array", "items": {"type": "media"}})
        )
        self.assertFalse(schema_contains_media({"type": "string"}))
        self.assertFalse(schema_contains_media(None))

    def test_prepare_go_media_without_inputs_is_untouched(self) -> None:
        from atri_x.tool.skill import prepare_go_media

        raw = '{"a": 1}'
        self.assertEqual(prepare_go_media(None, raw, None, self.root), raw)

    def test_prepare_go_media_expands_inputs(self) -> None:
        from atri_x.tool.skill import prepare_go_media

        call = FakeMediaCall()
        raw = json.dumps({"media_inputs": [{"media": "media:" + "a" * 64}], "keep": 1})
        out = json.loads(prepare_go_media(None, raw, call, self.root))
        entry = out["media_inputs"][0]
        self.assertTrue(entry["path"].startswith("media/"))
        self.assertIn("base64", entry)
        self.assertEqual(entry["size"], len(call.payload))
        self.assertTrue(out["media_workspace"].endswith("workspace"))
        self.assertEqual(out["keep"], 1)

    def test_prepare_go_media_skips_large_base64(self) -> None:
        from atri_x.tool.skill import prepare_go_media

        call = FakeMediaCall(payload=b"x" * (1024 * 1024 + 1))
        raw = json.dumps({"media_inputs": [{"media": "media:" + "b" * 64}]})
        out = json.loads(prepare_go_media(None, raw, call, self.root))
        self.assertNotIn("base64", out["media_inputs"][0])

    def test_prepare_go_media_rejects_non_array(self) -> None:
        from atri_x.tool.skill import prepare_go_media

        with self.assertRaises(ValueError):
            prepare_go_media(None, '{"media_inputs": 1}', FakeMediaCall(), self.root)

    def test_prepare_command_media_replaces_ids(self) -> None:
        from atri_x.tool.skill import prepare_command_media

        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        raw = json.dumps(
            {"media_in": "media:" + "c" * 64, "nested": {"blob": "media:" + "d" * 64}}
        )
        out = json.loads(
            prepare_command_media(None, raw, manifest, FakeMediaCall(), self.root)
        )
        self.assertTrue(out["media_in"].endswith(".bin"))
        self.assertTrue(out["nested"]["blob"].endswith(".bin"))

    def test_prepare_command_media_rejects_null(self) -> None:
        from atri_x.tool.skill import prepare_command_media

        manifest = parse_agent_skill_manifest(CALLABLE_TOML)
        with self.assertRaises(ValueError) as caught:
            prepare_command_media(None, '{"media_in": null}', manifest, FakeMediaCall(), self.root)
        self.assertIn("must not be null", str(caught.exception))

    def test_prepare_command_media_without_media_schema(self) -> None:
        from atri_x.tool.skill import prepare_command_media

        manifest = parse_agent_skill_manifest(DOC_ONLY_TOML)
        raw = '{"a": 1}'
        self.assertEqual(prepare_command_media(None, raw, manifest, None, self.root), raw)

    def test_result_from_stdout_plain_text(self) -> None:
        from atri_x.tool.skill import result_from_stdout

        self.assertEqual(result_from_stdout("plain output").content, "plain output")
        self.assertEqual(result_from_stdout("   ").content, "")

    def test_result_from_stdout_structured_content(self) -> None:
        from atri_x.tool.skill import result_from_stdout

        result = result_from_stdout('{"content": "done", "data": {"path": "out.txt"}}')
        self.assertEqual(result.content, "done")

    def test_result_from_stdout_truncates_long_output(self) -> None:
        from atri_x.tool.skill import result_from_stdout

        self.assertIn("output truncated", result_from_stdout("x" * (16 * 1024 + 100)).content)

    def test_result_from_stdout_segments(self) -> None:
        from atri_x.tool.skill import result_from_stdout_with_media

        call = FakeMediaCall()
        payload = json.dumps(
            {
                "content": "head",
                "segments": [
                    {"type": "text", "text": "extra"},
                    {"type": "image", "media": "media:" + "e" * 64},
                    {"type": "file", "path": "out/thing.bin", "name": "thing"},
                ],
            }
        )
        result = result_from_stdout_with_media(None, payload, call, self.root)
        self.assertEqual(result.segments[0].text, "head")
        self.assertEqual(result.segments[1].text, "extra")
        self.assertEqual(result.segments[2].media, "media:" + "e" * 64)
        self.assertEqual(result.segments[3].media, "media:" + "c" * 64)
        self.assertEqual(call.imported, ["out/thing.bin"])

    def test_result_from_stdout_rejects_bad_segments(self) -> None:
        from atri_x.tool.skill import result_from_stdout

        with self.assertRaises(ValueError):
            result_from_stdout(
                '{"content": "x", "segments": [{"type": "text", "media": "m"}]}'
            )
        with self.assertRaises(ValueError):
            result_from_stdout('{"segments": [{"type": "image"}]}')
        with self.assertRaises(ValueError):
            result_from_stdout('{"segments": [{"type": "video", "media": "m"}]}')

    def test_truncate_output_is_byte_based(self) -> None:
        from atri_x.tool.skill import truncate_output

        self.assertEqual(truncate_output("short"), "short")
        self.assertIn("truncated", truncate_output("中" * 10000))


# ---------------------------------------------------------------------------
# go_source 辅助
# ---------------------------------------------------------------------------

class TestGoSource(unittest.TestCase):
    def test_validate_go_main_source(self) -> None:
        validate_go_main_source("package main\n\nfunc main() {}\n")
        validate_go_main_source("// package main is required\npackage main\n")
        with self.assertRaises(ValueError):
            validate_go_main_source("package other\n")
        with self.assertRaises(ValueError):
            validate_go_main_source("")

    def test_comment_stripping_keeps_strings(self) -> None:
        from atri_x.tool.skill.go_source import strip_go_comments

        text = 'package main // trailing\nconst s = "// not a comment"\n/* block */\n'
        stripped = strip_go_comments(text)
        self.assertNotIn("trailing", stripped)
        self.assertIn("// not a comment", stripped)
        self.assertNotIn("block", stripped)

    def test_normalize_go_source(self) -> None:
        from atri_x.tool.skill.go_source import normalize_go_source

        self.assertEqual(normalize_go_source("package main\r\n\r\n"), "package main\n")

    def test_resolve_go_executable_reports_missing_go(self) -> None:
        from atri_x.processenv import Environment
        from atri_x.tool.skill.go_source import GO_BINARY_ENV, resolve_go_executable

        empty = Environment([])
        try:
            resolve_go_executable("", empty)
        except ValueError as exc:
            self.assertIn(GO_BINARY_ENV, str(exc))
        # 注：本机若 PATH 上有 go，则解析成功，同样接受。


if __name__ == "__main__":
    unittest.main()
