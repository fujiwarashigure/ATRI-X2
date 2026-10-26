"""内置工具：文件读写 / 回滚 / 守卫 / workspace 的单元测试。

对应 Go 测试：``internal/tool/builtin/file_tools_test.go`` /
``file_rollback_test.go`` / ``workspace_test.go``（覆盖核心路径，不逐函数照搬）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.errors import InvalidError
from atri_x.tool import CallRequest
from atri_x.tool.builtin.common import go_quote
from atri_x.tool.builtin.file_guard import (
    WARN_USE_MODIFY_LONG_MEMORY,
    WARN_USE_READ_LONG_MEMORY,
    FileGuard,
    new_file_guard,
    new_long_memory_file_guard_rule,
)
from atri_x.tool.builtin.file_rollback import (
    ROLLBACK_FILE_NAME,
    format_rollback_records,
    new_rollback_file_tool,
)
from atri_x.tool.builtin.file_tools import (
    EditFileTool,
    ReadFileTool,
    is_sensitive_read_file,
    parse_edit_file_args,
    parse_read_file_args,
    truncate_utf8_bytes,
)
from atri_x.tool.builtin.workspace import (
    WorkspaceTool,
    find_workspace_agent_file,
)
from atri_x.tool.sandbox import with_session_id
from atri_x.utils.fileops import new_rollback_store

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_files_test")


def _call(name: str, arguments: dict) -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


def _run(coro):
    return asyncio.run(coro)


class TestFileReadWrite(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.path = os.path.join(TMP_ROOT, "sample.txt")
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("alpha\nbeta\ngamma\ndelta\n")

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_read_file_plain(self) -> None:
        tool = ReadFileTool()
        result = _run(tool.call(None, _call("read_file", {"path": self.path})))
        self.assertIn("file: ", result.content)
        self.assertIn("lines: 1-4/4", result.content)
        self.assertIn("1 | alpha", result.content)
        self.assertIn("revision: ", result.content)

    def test_read_file_range_and_end(self) -> None:
        tool = ReadFileTool()
        result = _run(
            tool.call(
                None,
                _call(
                    "read_file",
                    {"path": self.path, "start_line": 2, "end_line": "end"},
                ),
            )
        )
        self.assertIn("lines: 2-4/4", result.content)

    def test_read_file_start_line_accepts_string(self) -> None:
        parsed = parse_read_file_args(json.dumps({"path": "x", "start_line": "3"}))
        self.assertEqual(parsed.start_line, 3)
        with self.assertRaises(InvalidError):
            parse_read_file_args(json.dumps({"path": "x", "start_line": "abc"}))

    def test_read_file_grep(self) -> None:
        tool = ReadFileTool()
        result = _run(
            tool.call(
                None,
                _call("read_file", {"path": self.path, "mode": "grep", "query": "delta"}),
            )
        )
        self.assertIn('grep: "delta"', result.content)
        self.assertIn("1. match: 4", result.content)

    def test_read_file_grep_requires_query(self) -> None:
        tool = ReadFileTool()
        with self.assertRaises(InvalidError):
            _run(
                tool.call(
                    None, _call("read_file", {"path": self.path, "mode": "grep"})
                )
            )

    def test_read_file_ast_and_ast_function(self) -> None:
        source = os.path.join(TMP_ROOT, "demo.go")
        with open(source, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "package demo\n\n"
                "func Greet(name string) string {\n"
                "\treturn name\n"
                "}\n"
            )
        tool = ReadFileTool()
        ident = _run(
            tool.call(
                None,
                _call("read_file", {"path": source, "mode": "ast", "query": "name"}),
            )
        )
        self.assertIn("language: go", ident.content)
        self.assertIn("[identifier] in function Greet", ident.content)
        func = _run(
            tool.call(
                None,
                _call(
                    "read_file",
                    {"path": source, "mode": "ast_function", "query": "Greet"},
                ),
            )
        )
        self.assertIn("match: 3-5 [function] Greet", func.content)

    def test_read_file_ast_rejects_unknown_language(self) -> None:
        other = os.path.join(TMP_ROOT, "note.txt")
        with open(other, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("hello\n")
        tool = ReadFileTool()
        with self.assertRaises(InvalidError):
            _run(
                tool.call(
                    None,
                    _call("read_file", {"path": other, "mode": "ast", "query": "x"}),
                )
            )

    def test_read_file_rejects_unknown_mode_and_field(self) -> None:
        # 模式合法性在 normalize_read_file_mode 里判定（同 Go），不在 JSON 解析期。
        self.assertEqual(
            parse_read_file_args(json.dumps({"path": "x", "mode": "nope"})).mode, "nope"
        )
        tool = ReadFileTool()
        with self.assertRaises(InvalidError):
            _run(
                tool.call(
                    None, _call("read_file", {"path": self.path, "mode": "nope"})
                )
            )
        with self.assertRaises(InvalidError):
            parse_read_file_args(json.dumps({"path": "x", "bogus": 1}))

    def test_read_file_risk_marks_sensitive(self) -> None:
        secret = os.path.join(TMP_ROOT, ".env")
        with open(secret, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("KEY=1\n")
        tool = ReadFileTool()
        assessment = _run(
            tool.assess_risk(None, _call("read_file", {"path": secret}))
        )
        self.assertEqual(assessment.level, "high")
        self.assertTrue(is_sensitive_read_file(secret))
        self.assertFalse(is_sensitive_read_file(self.path))

    def test_edit_file_replace_text(self) -> None:
        tool = EditFileTool()
        args = parse_edit_file_args(
            json.dumps(
                {
                    "path": self.path,
                    "edits": [{"operation": "replace_text", "old_text": "beta", "new_text": "BETA"}],
                }
            )
        )
        self.assertEqual(len(args.edits), 1)
        result = _run(
            tool.call(
                None,
                _call(
                    "edit_file",
                    {
                        "path": self.path,
                        "edits": [
                            {
                                "operation": "replace_text",
                                "old_text": "beta",
                                "new_text": "BETA",
                            }
                        ],
                    },
                ),
            )
        )
        self.assertIn("edited: ", result.content)
        self.assertIn("created: false", result.content)
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("BETA", handle.read())

    def test_edit_file_rejects_unknown_field(self) -> None:
        with self.assertRaises(InvalidError):
            parse_edit_file_args(json.dumps({"path": "x", "nope": 1}))
        with self.assertRaises(InvalidError):
            parse_edit_file_args(
                json.dumps({"path": "x", "edits": [{"operation": "replace", "bogus": 1}]})
            )

    def test_edit_file_create_and_preflight(self) -> None:
        target = os.path.join(TMP_ROOT, "new.txt")
        tool = EditFileTool()
        request = _call(
            "edit_file",
            {
                "path": target,
                "create": True,
                "edits": [{"operation": "overwrite", "new_text": "hello"}],
            },
        )
        _run(tool.preflight_confirmation(None, request))
        result = _run(tool.call(None, request))
        self.assertIn("created: true", result.content)
        self.assertTrue(os.path.exists(target))

    def test_edit_file_guard_blocks_write(self) -> None:
        guard = new_file_guard(
            new_long_memory_file_guard_rule(TMP_ROOT)
        )
        memories = os.path.join(TMP_ROOT, "memories")
        os.makedirs(memories, exist_ok=True)
        target = os.path.join(memories, "note.md")
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("old\n")
        tool = EditFileTool(guard)
        with self.assertRaises(InvalidError) as ctx:
            _run(
                tool.call(
                    None,
                    _call(
                        "edit_file",
                        {
                            "path": target,
                            "edits": [
                                {
                                    "operation": "replace_text",
                                    "old_text": "old",
                                    "new_text": "new",
                                }
                            ],
                        },
                    ),
                )
            )
        self.assertEqual(str(ctx.exception), WARN_USE_MODIFY_LONG_MEMORY)

    def test_guard_read_warnings(self) -> None:
        guard = FileGuard([new_long_memory_file_guard_rule(TMP_ROOT)])
        target = os.path.join(TMP_ROOT, "memories", "note.md")
        self.assertEqual(guard.read_warnings(target), [WARN_USE_READ_LONG_MEMORY])
        self.assertEqual(guard.read_warnings(self.path), [])

    def test_rollback_file_restores_previous_content(self) -> None:
        store = new_rollback_store()
        tool = EditFileTool(backups=store)
        ctx = with_session_id(None, "session-1")
        _run(
            tool.call(
                ctx,
                _call(
                    "edit_file",
                    {
                        "path": self.path,
                        "edits": [
                            {
                                "operation": "replace_text",
                                "old_text": "alpha",
                                "new_text": "ALPHA",
                            }
                        ],
                    },
                ),
            )
        )
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("ALPHA", handle.read())
        records = store.list("session-1")
        self.assertEqual(len(records), 1)
        self.assertIn("可回滚的文件编辑", format_rollback_records(records))

        rollback = new_rollback_file_tool(store)
        listed = _run(rollback.call(ctx, _call(ROLLBACK_FILE_NAME, {})))
        self.assertIn(self.path, listed.content)
        restored = _run(
            rollback.call(ctx, _call(ROLLBACK_FILE_NAME, {"path": self.path}))
        )
        self.assertIn("已回滚", restored.content)
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("alpha", handle.read())

    def test_rollback_without_session(self) -> None:
        rollback = new_rollback_file_tool(new_rollback_store())
        result = _run(rollback.call(None, _call(ROLLBACK_FILE_NAME, {})))
        self.assertEqual(result.content, "无法确定当前会话，未回滚。")

    def test_rollback_disabled(self) -> None:
        rollback = new_rollback_file_tool(None)
        result = _run(rollback.call(None, _call(ROLLBACK_FILE_NAME, {})))
        self.assertEqual(result.content, "文件回滚未启用。")

    def test_truncate_and_quote_helpers(self) -> None:
        text, truncated = truncate_utf8_bytes("中文内容", 4)
        self.assertTrue(truncated)
        self.assertEqual(text, "中")
        self.assertEqual(truncate_utf8_bytes("abc", 8), ("abc", False))
        self.assertEqual(go_quote('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(go_quote("中"), '"\\u4e2d"')


class TestFileToolsMetadata(unittest.TestCase):
    def test_tool_names_and_schema(self) -> None:
        read = ReadFileTool()
        edit = EditFileTool()
        rollback = new_rollback_file_tool(None)
        self.assertEqual(read.name(), "read_file")
        self.assertEqual(edit.name(), "edit_file")
        self.assertEqual(rollback.name(), "rollback_file")
        self.assertEqual(read.info().risk, "low")
        self.assertEqual(edit.info().risk, "high")
        self.assertTrue(read.info().superadmin_only)
        self.assertTrue(edit.info().depends_on[0] == "workspace")
        schema = edit.schema()
        self.assertEqual(schema.function.name, "edit_file")
        self.assertIn("edits", schema.function.parameters["properties"])
        self.assertIn("edits", schema.function.parameters["required"])


class TestWorkspaceTool(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_find_workspace_agent_file(self) -> None:
        path = os.path.join(TMP_ROOT, "AGENTS.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("hi\n")
        found, name, ok = find_workspace_agent_file(TMP_ROOT)
        self.assertTrue(ok)
        self.assertEqual(name, "AGENTS.md")
        self.assertEqual(found, path)
        with self.assertRaises(InvalidError):
            find_workspace_agent_file(os.path.join(TMP_ROOT, "nope"))

    def test_workspace_without_store(self) -> None:
        tool = WorkspaceTool()
        current = _run(tool.call(None, _call("workspace", {})))
        self.assertTrue(current.content.startswith("current workspace: "))
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _call("workspace", {"path": TMP_ROOT})))

    def test_workspace_set_and_reset(self) -> None:
        store = _FakeWorkspaceStore()
        from atri_x.tool.workspace import with_workspace_store

        ctx = with_workspace_store(None, store)
        tool = WorkspaceTool()
        result = _run(tool.call(ctx, _call("workspace", {"path": TMP_ROOT})))
        self.assertIn("workspace set.", result.content)
        self.assertEqual(store.directory, TMP_ROOT)

        reset = _run(tool.call(ctx, _call("workspace", {"reset": True})))
        self.assertIn("workspace reset.", reset.content)
        self.assertEqual(store.directory, "")

    def test_workspace_instructions_notice(self) -> None:
        path = os.path.join(TMP_ROOT, "AGENTS.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("规则\n")
        store = _FakeWorkspaceStore()
        from atri_x.tool.workspace import with_workspace_store

        ctx = with_workspace_store(None, store)
        tool = WorkspaceTool()
        result = _run(tool.call(ctx, _call("workspace", {"path": TMP_ROOT})))
        self.assertIn("Loaded workspace instructions from AGENTS.md", result.content)
        self.assertTrue(store.notice_dirs)
        again = _run(tool.call(ctx, _call("workspace", {"path": TMP_ROOT})))
        self.assertNotIn("Loaded workspace instructions", again.content)

    def test_validate_workspace_dir_resolves_relative_paths(self) -> None:
        """1.0.9：相对路径按 cwd 解析；`"."` 不再被当成空路径报错。"""
        from atri_x.tool.workspace import validate_workspace_dir

        self.assertEqual(os.path.normpath(os.getcwd()), validate_workspace_dir("."))
        self.assertEqual(
            os.path.normpath(os.getcwd()),
            validate_workspace_dir(os.path.join(".", "")),
        )
        self.assertEqual(TMP_ROOT, validate_workspace_dir(os.path.join(TMP_ROOT, ".")))

    def test_validate_workspace_dir_messages(self) -> None:
        """空路径与非目录的报错要能区分（1.0.8 两者都报 required，误导模型）。"""
        from atri_x.tool.workspace import validate_workspace_dir

        with self.assertRaises(InvalidError) as blank:
            validate_workspace_dir("   ")
        self.assertIn("workspace path is required", str(blank.exception))

        path = os.path.join(TMP_ROOT, "not-a-dir.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("x\n")
        with self.assertRaises(InvalidError) as file_error:
            validate_workspace_dir(path)
        self.assertIn("not a directory", str(file_error.exception))

    def test_workspace_tool_accepts_dot(self) -> None:
        """workspace 工具支持 `{"path": "."}`（模型最自然的"就用当前目录"）。"""
        store = _FakeWorkspaceStore()
        from atri_x.tool.workspace import with_workspace_store

        ctx = with_workspace_store(None, store)
        result = _run(WorkspaceTool().call(ctx, _call("workspace", {"path": "."})))
        self.assertIn("workspace set.", result.content)
        self.assertEqual(os.path.normpath(os.getcwd()), store.directory)

    def test_workspace_tool_metadata(self) -> None:
        info = WorkspaceTool().info()
        self.assertEqual(info.name, "workspace")
        self.assertEqual(info.tags, ["agent", "files"])
        self.assertTrue(info.superadmin_only)
        self.assertTrue(info.foreground_only)
        schema = WorkspaceTool().schema()
        self.assertEqual(schema.function.name, "workspace")
        self.assertIn("相对路径", str(schema.function.parameters["properties"]["path"]))


class _FakeWorkspaceStore:
    """带已提示标记的 workspace 存储替身。"""

    def __init__(self) -> None:
        self.directory = ""
        self.notice_dirs: set[str] = set()

    async def get_workspace_dir(self, ctx):
        return self.directory

    async def set_workspace_dir(self, ctx, directory):
        self.directory = directory

    async def clear_workspace_dir(self, ctx):
        self.directory = ""

    async def has_workspace_agent_notice_dir(self, ctx, directory):
        return directory in self.notice_dirs

    async def mark_workspace_agent_notice_dir(self, ctx, directory):
        self.notice_dirs.add(directory)

    async def set_workspace_dir_with_agent_notice(self, ctx, directory, mark_notice):
        self.directory = directory
        if mark_notice:
            self.notice_dirs.add(directory)

    async def clear_workspace_dir_with_agent_notice(self, ctx, mark_notice):
        self.directory = ""


if __name__ == "__main__":
    unittest.main()
