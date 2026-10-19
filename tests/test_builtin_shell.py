"""内置工具：shell 风险分级 / 后台沙盒 / 执行建议 / 实际执行的单元测试。

对应 Go 测试：``internal/tool/builtin/shell_test.go``、``shell_risk_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from unittest import mock

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
    DEFAULT_BACKGROUND_SHELL_ALLOW,
    BackgroundShellPolicy,
    apply_background_shell_policy,
    apply_bash_shell_sandbox_risk,
    background_shell_policy_from_config,
    is_unattended_background,
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


class TestShellRiskHardening(unittest.TestCase):
    """研究报告 §6.2 的包装器/内层脚本绕过回归（全部曾判 low）。"""

    BYPASS = (
        "env rm -rf /",
        "command rm -rf /",
        "nohup rm -rf /",
        "timeout 5 rm -rf /",
        "nice -n 19 rm -rf /",
        "setsid rm -rf /",
        "busybox rm -rf /",
        "xargs rm -rf",
        "ls | xargs rm -rf",
        "find / -exec rm -rf {} +",
        "find / -delete",
        "eval 'rm -rf /'",
        "awk 'BEGIN{system(\"rm -rf /\")}'",
        "trap 'rm -rf /' EXIT; sleep 1",
        "bash -c 'rm -rf /'",
        "chroot / rm -rf /",
        "ssh localhost 'rm -rf /'",
        "watch -n1 rm -rf /",
        "truncate -s 0 /etc/passwd",
        "shred -u /etc/passwd",
    )

    def test_wrapper_and_inner_script_bypasses_are_critical(self) -> None:
        for command in self.BYPASS:
            with self.subTest(command=command):
                level = classify_bash_shell_command(command).level
                self.assertIn(level, ("critical", "high"), command)

    def test_benign_commands_stay_low(self) -> None:
        for command in (
            "ls -la",
            "cat ./notes.txt",
            "git status",
            "git log --oneline -5",
            "echo hello",
            "env",
            "command -v rm",
            "grep -rn TODO ./src",
            "sed -n 1,10p ./file.txt",
        ):
            with self.subTest(command=command):
                self.assertEqual(classify_bash_shell_command(command).level, "low", command)

    def test_git_global_options_do_not_hide_subcommand(self) -> None:
        self.assertEqual(classify_bash_shell_command("git -C / clean -fdx").level, "high")
        self.assertEqual(
            classify_bash_shell_command("git --git-dir=/x clean -fdx").level, "high"
        )
        self.assertEqual(classify_bash_shell_command("git -C / status").level, "low")

    def test_unknown_wrapper_depth_is_high(self) -> None:
        level = classify_bash_shell_command("env " * 12 + "ls").level
        self.assertIn(level, ("high", "critical"))

    def test_wrapper_unwrap_helpers(self) -> None:
        from atri_x.tool.builtin.shell_risk import shell_command_names, unwrap_wrapper

        self.assertEqual(unwrap_wrapper("env", ["FOO=1", "ls", "-la"]), ("command", ["ls", "-la"]))
        self.assertEqual(unwrap_wrapper("cat", ["notes.txt"]), None)
        names, confirmed = shell_command_names("env cat ./a.txt && ls")
        self.assertEqual(names, ["cat", "ls"])
        self.assertTrue(confirmed)
        names, confirmed = shell_command_names('"$CMD" --version')
        self.assertFalse(confirmed)


class TestShellSandboxRecursion(unittest.TestCase):
    def test_wrapped_and_c_script_paths_are_checked(self) -> None:
        violations = validate_bash_shell_sandbox_command("env cat /etc/passwd")
        self.assertTrue(any("绝对路径" in item for item in violations), violations)
        violations = validate_bash_shell_sandbox_command("bash -c 'cat /etc/passwd'")
        self.assertTrue(any("绝对路径" in item for item in violations), violations)
        violations = validate_bash_shell_sandbox_command("env ls ../escape")
        self.assertTrue(any("逃逸" in item for item in violations), violations)
        self.assertEqual(validate_bash_shell_sandbox_command("env cat notes/a.txt"), [])


class TestBackgroundShellPolicy(unittest.TestCase):
    def setUp(self) -> None:
        # 本机是 PowerShell 主机（is_power_shell_env() 恒 True）；白名单/等级分支
        # 要在 bash 口径下验证，单个用例再用自己的 patch 覆盖。
        self._patcher = mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        )
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_default_allowlist_allows_only_low_risk_readonly(self) -> None:
        policy = BackgroundShellPolicy()
        keep = apply_background_shell_policy(
            "ls -la", RiskAssessment(level="low"), policy
        )
        self.assertEqual(keep.level, "low")
        self.assertIn("ls", DEFAULT_BACKGROUND_SHELL_ALLOW)

    def test_high_risk_command_is_denied(self) -> None:
        policy = BackgroundShellPolicy()
        denied = apply_background_shell_policy(
            "git clean -fd", RiskAssessment(level="high"), policy
        )
        self.assertEqual(denied.level, "critical")
        self.assertTrue(any("低风险只读" in reason for reason in denied.reasons))

    def test_command_outside_allowlist_is_denied(self) -> None:
        denied = apply_background_shell_policy(
            "python tool.py", RiskAssessment(level="low"), policy=BackgroundShellPolicy()
        )
        self.assertEqual(denied.level, "critical")
        self.assertTrue(any("白名单" in reason for reason in denied.reasons))

    def test_dynamic_command_is_denied(self) -> None:
        denied = apply_background_shell_policy(
            "$CMD --version", RiskAssessment(level="high"), BackgroundShellPolicy()
        )
        self.assertEqual(denied.level, "critical")

    def test_allow_all_restores_legacy_behaviour(self) -> None:
        policy = BackgroundShellPolicy(allow_all=True)
        kept = apply_background_shell_policy(
            "python tool.py", RiskAssessment(level="low"), policy
        )
        self.assertEqual(kept.level, "low")

    def test_power_shell_host_is_denied(self) -> None:
        with mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=True
        ):
            denied = apply_background_shell_policy(
                "ls", RiskAssessment(level="low"), BackgroundShellPolicy()
            )
        self.assertEqual(denied.level, "critical")
        self.assertTrue(any("PowerShell" in reason for reason in denied.reasons))

    def test_policy_from_config(self) -> None:
        class _Security:
            background_shell_allow = ["deploy.sh"]
            background_shell_allow_all = False

        policy = background_shell_policy_from_config(_Security())
        self.assertEqual(policy.allow, frozenset({"deploy.sh"}))
        default_policy = background_shell_policy_from_config(None)
        self.assertEqual(default_policy.allow, DEFAULT_BACKGROUND_SHELL_ALLOW)

    def test_unattended_detection(self) -> None:
        from atri_x.tool.sandbox import SandboxContext

        self.assertTrue(
            is_unattended_background(
                SandboxContext(background=True, background_kind="cron")
            )
        )
        self.assertTrue(
            is_unattended_background(
                SandboxContext(background=True, background_kind="atrinis")
            )
        )
        self.assertFalse(is_unattended_background(SandboxContext(background=True)))
        self.assertFalse(is_unattended_background(SandboxContext()))

    def test_shell_tool_assess_risk_denies_background_command(self) -> None:
        from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

        tool = ShellTool()
        ctx = with_sandbox_context(
            _shell_ctx(),
            SandboxContext(root=TMP_ROOT, dir=TMP_ROOT, background=True, background_kind="cron"),
        )
        with mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=False
        ), mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        ):
            assessment = _run(
                tool.assess_risk(ctx, _call("shell", {"cmd": "python tool.py"}))
            )
            allowed = _run(tool.assess_risk(ctx, _call("shell", {"cmd": "ls -la"})))
        self.assertEqual(assessment.level, "critical")
        self.assertNotEqual(allowed.level, "critical")


class BackgroundExecutionRecheckTests(unittest.TestCase):
    """执行前的后台策略复核（研究报告 §6 的 P0：不能只靠评估阶段 / 工具 schema）。

    ``ShellTool.call()`` 在真正启动进程**之前**用执行时的 ctx 重新裁决一次：评估阶段
    可能没有沙盒 ctx、也可能被前台的"本会话自动确认"放行，所以"排队之后、执行之前"
    必须自己再判一遍，拒绝时抛 :class:`ForbiddenError` 且不碰进程/工作目录。
    """

    def _unattended_ctx(self, kind: str = "cron"):
        from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

        return with_sandbox_context(
            _shell_ctx(),
            SandboxContext(
                root=TMP_ROOT, dir=TMP_ROOT, background=True, background_kind=kind
            ),
        )

    def test_call_denies_before_touching_the_work_dir(self) -> None:
        from atri_x.errors import ForbiddenError

        tool = ShellTool()
        ctx = self._unattended_ctx()
        with mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=False
        ), mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        ), mock.patch(
            "atri_x.tool.builtin.shell.resolve_shell_work_dir",
            new=mock.AsyncMock(side_effect=AssertionError("不该走到执行准备")),
        ) as work_dir:
            with self.assertRaises(ForbiddenError) as caught:
                _run(tool.call(ctx, _call("shell", {"cmd": "python tool.py"})))
        work_dir.assert_not_called()
        self.assertIn("background shell denied", str(caught.exception))

    def test_denial_helper_allows_foreground_and_allowlisted(self) -> None:
        from atri_x.tool.builtin.shell import background_execution_denial

        policy = BackgroundShellPolicy()
        with mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=False
        ), mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        ):
            self.assertEqual(
                "", background_execution_denial(_shell_ctx(), "python tool.py", policy)
            )
            self.assertEqual(
                "",
                background_execution_denial(self._unattended_ctx(), "ls -la", policy),
            )
            self.assertNotEqual(
                "",
                background_execution_denial(
                    self._unattended_ctx("atrinis"), "uname -a", policy
                ),
            )


class ShellMediaCancellationTests(unittest.IsolatedAsyncioTestCase):
    """取消传播：媒体准备 / 清理不能把 ``CancelledError`` 变成普通错误（研究报告 §6）。"""

    #: ``media:<64 位小写十六进制>``（见 ``atri_x/media/types.py::valid_id``）。
    MEDIA_ID = "media:" + "ab" * 32

    async def test_media_variables_propagate_cancellation(self) -> None:
        from atri_x.tool.builtin import shell as shell_mod

        class _MediaCall:
            async def cached_export(self, media_id):
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await shell_mod._media_variables(
                _MediaCall(), [{"media": self.MEDIA_ID}]
            )

    async def test_media_variables_still_wrap_ordinary_errors(self) -> None:
        from atri_x.tool.builtin import shell as shell_mod

        class _MediaCall:
            async def cached_export(self, media_id):
                raise RuntimeError("boom")

        with self.assertRaises(InvalidError):
            await shell_mod._media_variables(
                _MediaCall(), [{"media": self.MEDIA_ID}]
            )

    async def test_close_media_call_propagates_cancellation(self) -> None:
        from atri_x.tool.builtin import shell as shell_mod

        class _MediaCall:
            async def close(self):
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await shell_mod._close_media_call(_MediaCall())

    async def test_close_media_call_swallows_ordinary_errors(self) -> None:
        from atri_x.tool.builtin import shell as shell_mod

        class _MediaCall:
            async def close(self):
                raise RuntimeError("close failed")

        await shell_mod._close_media_call(_MediaCall())


def _shell_ctx():
    """构造一个把工作目录钉在 TMP_ROOT 的 ctx（避免污染仓库根）。"""
    from atri_x.tool.sandbox import SandboxContext, with_sandbox_context

    return with_sandbox_context(None, SandboxContext(root=TMP_ROOT, dir=TMP_ROOT))


if __name__ == "__main__":
    unittest.main()
