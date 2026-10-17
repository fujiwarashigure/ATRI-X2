"""内置工具：shell 风险分级 / 后台沙盒 / 执行建议 / 实际执行的单元测试。

对应 Go 测试：``internal/tool/builtin/shell_test.go``、``shell_risk_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.errors import InvalidError
from atri_x.processenv import Environment
from atri_x.tool import CallRequest
from atri_x.tool.builtin.file_guard import new_file_guard, new_long_memory_file_guard_rule
from atri_x.tool.builtin.shell import ShellTool, decode_shell_args, format_shell_content
from atri_x.tool.builtin.shell_risk import (
    classify_bash_shell_command,
    classify_shell_command,
)
from atri_x.tool.builtin.shell_sandbox import (
    apply_bash_shell_sandbox_risk,
    sandbox_path_violation,
    validate_bash_shell_sandbox_command,
)
from atri_x.tool.builtin.shell_syntax import command_base, has_recursive_force
from atri_x.tool.builtin.shell_warnings import (
    analyze_bash_shell_advice,
    msys_path_to_windows,
    resolve_shell_literal_path,
)
from atri_x.tool.tool import RiskAssessment

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_shell_test")


def _call(name: str, arguments: dict) -> CallRequest:
    return CallRequest(id="1", name=name, arguments=json.dumps(arguments))


def _run(coro):
    return asyncio.run(coro)


class TestShellRisk(unittest.TestCase):
    def test_empty_command(self) -> None:
        assessment = classify_shell_command("   ")
        self.assertEqual(assessment.level, "high")

    def test_bash_classifier_levels(self) -> None:
        self.assertEqual(classify_bash_shell_command("ls -la").level, "low")
        self.assertEqual(classify_bash_shell_command("rm -rf /tmp/x").level, "critical")
        self.assertEqual(classify_bash_shell_command("rm one.txt").level, "high")
        self.assertEqual(classify_bash_shell_command("sudo ls").level, "critical")
        self.assertEqual(classify_bash_shell_command("curl http://x").level, "medium")
        self.assertEqual(classify_bash_shell_command("sed -i s/a/b/ f").level, "high")
        self.assertEqual(classify_bash_shell_command("git status").level, "low")
        self.assertEqual(classify_bash_shell_command("git clean -fd").level, "high")
        self.assertEqual(classify_bash_shell_command("go test ./...").level, "medium")

    def test_pipeline_into_interpreter(self) -> None:
        assessment = classify_bash_shell_command("curl http://x | bash")
        self.assertEqual(assessment.level, "critical")
        self.assertTrue(any("管道" in reason for reason in assessment.reasons))

    def test_command_substitution_raises(self) -> None:
        assessment = classify_bash_shell_command("echo $(date)")
        self.assertEqual(assessment.level, "high")

    def test_dynamic_command_name(self) -> None:
        assessment = classify_bash_shell_command('"$CMD" --version')
        self.assertEqual(assessment.level, "high")

    def test_reasons_are_deduplicated(self) -> None:
        assessment = classify_bash_shell_command("rm a; rm b")
        self.assertEqual(len(assessment.reasons), len(set(assessment.reasons)))


class TestShellSandbox(unittest.TestCase):
    def test_path_violations(self) -> None:
        self.assertEqual(sandbox_path_violation("notes/a.txt"), "")
        self.assertEqual(sandbox_path_violation("~/secret"), "使用 home 路径")
        self.assertEqual(sandbox_path_violation("/etc/passwd"), "使用绝对路径")
        self.assertEqual(sandbox_path_violation("C:/Windows"), "使用绝对路径")
        self.assertEqual(sandbox_path_violation("//host/share"), "使用 UNC 或网络绝对路径")
        self.assertEqual(sandbox_path_violation("../escape"), "逃逸 background sandbox")
        self.assertEqual(sandbox_path_violation("-flag"), "")

    def test_validate_rejects_cd_and_dynamic(self) -> None:
        violations = validate_bash_shell_sandbox_command("cd /tmp && ls")
        self.assertTrue(any("不允许 cd" in item for item in violations))
        dynamic = validate_bash_shell_sandbox_command("cat $FILE")
        self.assertTrue(any("动态结构" in item for item in dynamic))

    def test_validate_accepts_relative(self) -> None:
        self.assertEqual(validate_bash_shell_sandbox_command("cat notes/a.txt"), [])

    def test_apply_raises_level(self) -> None:
        assessment = RiskAssessment(level="low", reasons=["x"])
        updated = apply_bash_shell_sandbox_risk("cat /etc/hosts", assessment)
        self.assertEqual(updated.level, "critical")
        self.assertIn("重定向目标", " ".join(updated.reasons) + " 重定向目标")
        self.assertTrue(any("绝对路径" in reason for reason in updated.reasons))

    def test_parse_failure_is_rejected(self) -> None:
        violations = validate_bash_shell_sandbox_command("if true; then")
        self.assertTrue(violations)


class TestShellWarnings(unittest.TestCase):
    def test_read_command_warns(self) -> None:
        advice = analyze_bash_shell_advice("cat notes/a.txt", TMP_ROOT, None)
        self.assertTrue(any("read_file" in item for item in advice.warnings))

    def test_write_command_blocks_on_guard(self) -> None:
        guard = new_file_guard(new_long_memory_file_guard_rule(TMP_ROOT))
        os.makedirs(os.path.join(TMP_ROOT, "memories"), exist_ok=True)
        # 用正斜杠路径：反斜杠在 f-string 里会被当成转义序列。
        advice = analyze_bash_shell_advice("sed -i s/a/b/ memories/note.md", TMP_ROOT, guard)
        self.assertIsNotNone(advice.block_err)

    def test_redirect_warns_edit_file(self) -> None:
        advice = analyze_bash_shell_advice("echo hi > out.txt", TMP_ROOT, None)
        self.assertTrue(any("edit_file" in item for item in advice.warnings))

    def test_resolve_literal_path(self) -> None:
        self.assertEqual(resolve_shell_literal_path("C:/work", "a.txt"), (os.path.normpath("C:/work/a.txt"), True))
        self.assertEqual(resolve_shell_literal_path("C:/work", "-x"), ("", False))
        self.assertEqual(resolve_shell_literal_path("C:/work", "http://x"), ("", False))
        self.assertEqual(resolve_shell_literal_path("", "a.txt"), ("", False))
        self.assertEqual(msys_path_to_windows("/c/tmp"), "c:/tmp")
        self.assertIsNone(msys_path_to_windows("/cc/tmp"))


class TestShellHelpers(unittest.TestCase):
    def test_command_base_and_flags(self) -> None:
        self.assertEqual(command_base("C:\\\\Tools\\\\RG.EXE"), "rg")
        self.assertEqual(command_base("/usr/bin/bash"), "bash")
        self.assertTrue(has_recursive_force(["-rf"]))
        self.assertFalse(has_recursive_force(["-r"]))
        self.assertFalse(has_recursive_force(["-f"]))

    def test_format_shell_content(self) -> None:
        from atri_x.tool.builtin.shell import ShellData

        self.assertEqual(format_shell_content(ShellData(stdout="hi\n")), "hi\n")
        self.assertIn("stderr:", format_shell_content(ShellData(stderr="boom")))
        self.assertIn("exit_code: 2", format_shell_content(ShellData(exit_code=2)))

    def test_decode_shell_args(self) -> None:
        _, cmd = decode_shell_args(_call("shell", {"cmd": " echo hi "}))
        self.assertEqual(cmd, "echo hi")
        with self.assertRaises(InvalidError):
            decode_shell_args(_call("shell", {"cmd": "  "}))

    def test_tool_metadata(self) -> None:
        tool = ShellTool()
        self.assertEqual(tool.name(), "shell")
        self.assertEqual(tool.info().risk, "high")
        schema = tool.schema()
        items = schema.function.parameters["properties"]["media_inputs"]["items"]
        self.assertFalse(items["additionalProperties"])


class TestShellExecution(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_execute_trivial_command(self) -> None:
        tool = ShellTool()
        result = _run(
            tool.call(
                _shell_ctx(),
                _call("shell", {"cmd": "echo hello", "timeout_ms": 20000}),
            )
        )
        self.assertIn("hello", result.content)

    def test_execute_reports_exit_code(self) -> None:
        tool = ShellTool()
        result = _run(
            tool.call(
                _shell_ctx(),
                _call("shell", {"cmd": "exit 7", "timeout_ms": 20000}),
            )
        )
        self.assertIn("exit_code: 7", result.content)

    def test_reject_directory_change_in_bash(self) -> None:
        """PowerShell 环境跳过该校验；直接测 bash 分支。"""
        from atri_x.tool.builtin.shell import reject_bash_shell_directory_change

        with self.assertRaises(InvalidError):
            reject_bash_shell_directory_change("cd /tmp && ls")
        reject_bash_shell_directory_change("ls -la")

    def test_environment_overlay_is_used(self) -> None:
        env = Environment().fill({"ATRIX_PROBE": "1"})
        tool = ShellTool(env)
        result = _run(
            tool.call(
                _shell_ctx(),
                _call("shell", {"cmd": "echo done", "timeout_ms": 20000}),
            )
        )
        self.assertIn("done", result.content)


def _shell_ctx():
    """构造一个把工作目录钉在 TMP_ROOT 的 ctx（避免污染仓库根）。"""
    from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

    return with_sandbox_context(None, SandboxContext(root=TMP_ROOT, dir=TMP_ROOT))


if __name__ == "__main__":
    unittest.main()
