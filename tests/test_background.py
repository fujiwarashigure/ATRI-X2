"""``atri_x.background`` 的单元测试。

对应 Go 测试：``internal/background/report_test.go``（1 例）与
``internal/background/result_test.go``（3 例）—— 4 例全部逐条移植；其余用例覆盖
Go 侧没有测试但被 cron/Atrinis 依赖的行为（媒体 ID 校验、沙盒路径策略、JSON 结构对齐、
后台工具白名单、``RunRequest``/``RunResult`` 字段）。

临时目录在仓库根下用 ``os.makedirs`` 建、``tearDown`` 里 ``shutil.rmtree(ignore_errors=True)``
（不用 ``tempfile.TemporaryDirectory``：本机沙箱下它的 0o700 ACL 会让清理抛
``PermissionError``）。
"""

from __future__ import annotations

import os
import shutil
import unittest
import uuid

from atri_x.background import (
    BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED,
    BACKGROUND_SHELL_AUDIT_REJECTED,
    BACKGROUND_SHELL_CRITICAL_MESSAGE,
    BACKGROUND_TOOL_DENIED,
    BuildReportOutputs,
    BuildReportSegmentOutput,
    DefaultJSONRetryPrompt,
    JSONResult,
    KindAtrinis,
    KindCron,
    OutcomeTakenOver,
    ParseJSONResult,
    PathInstruction,
    RunRequest,
    RunResult,
    ShellRiskDecision,
    background_tool_allowed,
    confirm_background_sandbox_shell,
    valid_media_id,
)
from atri_x.delivery.delivery import KindFile, KindImage, KindText
from atri_x.llm.llm import MessageSegment, SegmentType
from atri_x.security import Actor, RiskCritical, RiskHigh, RiskLow
from atri_x.tool.sandbox import BackgroundKindCron, SandboxContext

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BackgroundTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_REPO_ROOT, f".background-test-{uuid.uuid4().hex[:8]}")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def sandbox(self) -> SandboxContext:
        directory = os.path.join(self.root, "cron", "test")
        os.makedirs(directory, exist_ok=True)
        return SandboxContext(
            root=self.root, dir=directory, background=True, background_kind=BackgroundKindCron
        )


# --- report_test.go（1/1 移植） --------------------------------------------
class TestBuildReportOutputs(BackgroundTestCase):
    def test_build_report_outputs_keeps_http_media_url(self) -> None:
        """移植：``TestBuildReportOutputsKeepsHTTPMediaURL``。"""
        sandbox = SandboxContext(dir=self.root)
        outputs = BuildReportOutputs(
            "报告",
            [
                MessageSegment(
                    type=SegmentType.IMAGE,
                    url="https://example.com/chart.png",
                    name="chart.png",
                )
            ],
            sandbox,
        )
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0].kind, KindText)
        self.assertEqual(outputs[1].kind, KindImage)
        self.assertEqual(outputs[1].source.url, "https://example.com/chart.png")
        self.assertEqual(outputs[1].source.path, "")
        self.assertEqual(outputs[1].name, "chart.png")

    def test_build_report_outputs_skips_blank_report(self) -> None:
        outputs = BuildReportOutputs("   ", [], SandboxContext(dir=self.root))
        self.assertEqual(outputs, [])

    def test_build_report_outputs_resolves_relative_path_inside_sandbox(self) -> None:
        sandbox = self.sandbox()
        with open(os.path.join(sandbox.dir, "chart.png"), "wb") as handle:
            handle.write(b"png")
        outputs = BuildReportOutputs(
            "见图",
            [MessageSegment(type=SegmentType.IMAGE, url="chart.png")],
            sandbox,
        )
        self.assertEqual(outputs[1].source.path, os.path.join(sandbox.dir, "chart.png"))
        self.assertEqual(outputs[1].source.url, "")

    def test_build_report_segment_output_rejects_unknown_type(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BuildReportSegmentOutput(
                MessageSegment(type=SegmentType.TEXT, text="x"), SandboxContext(dir=self.root)
            )
        self.assertIn("unsupported type", str(ctx.exception))

    def test_build_report_outputs_prefixes_segment_index(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BuildReportOutputs(
                "报告",
                [MessageSegment(type=SegmentType.TEXT, text="x")],
                SandboxContext(dir=self.root),
            )
        self.assertIn("report segment 0:", str(ctx.exception))

    def test_build_report_segment_output_requires_existing_file(self) -> None:
        sandbox = self.sandbox()
        with self.assertRaises(ValueError) as ctx:
            BuildReportSegmentOutput(
                MessageSegment(type=SegmentType.FILE, url="missing.txt"), sandbox
            )
        self.assertIn("stat:", str(ctx.exception))

    def test_build_report_segment_output_rejects_directory(self) -> None:
        sandbox = self.sandbox()
        os.makedirs(os.path.join(sandbox.dir, "subdir"), exist_ok=True)
        with self.assertRaises(ValueError) as ctx:
            BuildReportSegmentOutput(
                MessageSegment(type=SegmentType.FILE, url="subdir"), sandbox
            )
        self.assertIn("path is a directory", str(ctx.exception))

    def test_build_report_segment_output_rejects_escaping_path(self) -> None:
        with self.assertRaises(Exception):
            BuildReportSegmentOutput(
                MessageSegment(type=SegmentType.FILE, url="../outside.txt"), self.sandbox()
            )

    def test_build_report_segment_output_accepts_media_id(self) -> None:
        media_id = "media:" + "a" * 64
        out = BuildReportSegmentOutput(
            MessageSegment(type=SegmentType.IMAGE, media=media_id), SandboxContext(dir=self.root)
        )
        self.assertEqual(out.source.media_id, media_id)
        self.assertEqual(out.kind, KindImage)

    def test_build_report_segment_output_rejects_mixed_media_source(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            BuildReportSegmentOutput(
                MessageSegment(
                    type=SegmentType.IMAGE,
                    media="media:" + "a" * 64,
                    url="https://example.com/x.png",
                ),
                SandboxContext(dir=self.root),
            )
        self.assertIn("invalid or mixed report media source", str(ctx.exception))

    def test_build_report_segment_output_rejects_invalid_media_id(self) -> None:
        with self.assertRaises(ValueError):
            BuildReportSegmentOutput(
                MessageSegment(type=SegmentType.IMAGE, media="media:short"),
                SandboxContext(dir=self.root),
            )
        self.assertFalse(valid_media_id("media:" + "A" * 64))
        self.assertFalse(valid_media_id("media:" + "a" * 63))
        self.assertTrue(valid_media_id("media:" + "0" * 64))

    def test_build_report_segment_output_file_kind(self) -> None:
        sandbox = self.sandbox()
        with open(os.path.join(sandbox.dir, "report.txt"), "wb") as handle:
            handle.write(b"x")
        out = BuildReportSegmentOutput(
            MessageSegment(type=SegmentType.FILE, url="report.txt"), sandbox
        )
        self.assertEqual(out.kind, KindFile)

    def test_path_instruction_matches_tool_package(self) -> None:
        """路径约定说明只有 ``atri_x.tool.sandbox`` 一个出处。"""
        from atri_x.tool.sandbox import background_path_instruction

        self.assertEqual(PathInstruction(), background_path_instruction())
        self.assertIn("相对路径", PathInstruction())


# --- result_test.go（3/3 移植） --------------------------------------------
class TestParseJSONResult(unittest.TestCase):
    def test_parse_json_result_accepts_plain_json(self) -> None:
        """移植：``TestParseJSONResultAcceptsPlainJSON``。"""
        result = ParseJSONResult('{"completed":true,"need_report":false,"report":"ok"}')
        self.assertTrue(result.completed)
        self.assertFalse(result.need_report)
        self.assertEqual(result.report, "ok")

    def test_parse_json_result_accepts_code_fence_and_extra_text(self) -> None:
        """移植：``TestParseJSONResultAcceptsCodeFenceAndExtraText``。"""
        result = ParseJSONResult(
            "前缀```json\n{\"completed\":true,\"need_report\":true,\"report\":\"ok\"}\n```后缀"
        )
        self.assertTrue(result.completed)
        self.assertTrue(result.need_report)
        self.assertEqual(result.report, "ok")

    def test_parse_json_result_rejects_invalid_json(self) -> None:
        """移植：``TestParseJSONResultRejectsInvalidJSON``。"""
        with self.assertRaises(ValueError):
            ParseJSONResult("not json")

    def test_parse_json_result_parses_report_segments(self) -> None:
        result = ParseJSONResult(
            '{"completed":false,"need_report":true,"report":"见图",'
            '"report_segments":[{"type":"image","url":"chart.png","name":"chart.png"}]}'
        )
        self.assertEqual(len(result.report_segments), 1)
        self.assertEqual(result.report_segments[0].type, SegmentType.IMAGE)
        self.assertEqual(result.report_segments[0].url, "chart.png")

    def test_parse_json_result_ignores_unknown_keys(self) -> None:
        result = ParseJSONResult('{"completed":true,"report":"ok","extra":123}')
        self.assertTrue(result.completed)
        self.assertEqual(result.report, "ok")

    def test_parse_json_result_rejects_wrong_field_types(self) -> None:
        with self.assertRaises(ValueError):
            ParseJSONResult('{"completed":"yes","report":"ok"}')
        with self.assertRaises(ValueError):
            ParseJSONResult('{"report":123}')

    def test_parse_json_result_rejects_trailing_text_between_objects(self) -> None:
        # 与 Go 一致：取第一个 { 到最后一个 } 之后，中间夹着的额外文本会让
        # json.Unmarshal 报 "invalid character ... after top-level value"。
        with self.assertRaises(ValueError):
            ParseJSONResult('a{"report":"1"}b{"report":"2"}c')

    def test_json_result_round_trip_omits_empty_segments(self) -> None:
        payload = JSONResult(completed=True, need_report=False, report="ok").to_json()
        self.assertEqual(payload, {"completed": True, "need_report": False, "report": "ok"})
        restored = JSONResult.from_json(payload)
        self.assertTrue(restored.completed)
        self.assertEqual(restored.report_segments, [])

    def test_default_json_retry_prompt_mentions_path_instruction(self) -> None:
        prompt = DefaultJSONRetryPrompt()
        self.assertIn("你返回的格式有误", prompt)
        self.assertIn("严格使用 JSON 格式", prompt)
        self.assertIn(PathInstruction(), prompt)


# --- background.go（Go 侧无测试，但 cron/Atrinis 依赖） ---------------------
class TestBackgroundTypes(unittest.TestCase):
    def test_kind_constants(self) -> None:
        self.assertEqual(KindCron, "cron")
        self.assertEqual(KindAtrinis, "atrinis")
        self.assertEqual(OutcomeTakenOver, "taken_over")

    def test_run_request_defaults_align_with_go(self) -> None:
        req = RunRequest()
        self.assertEqual(req.kind, "")
        self.assertEqual(req.session_id, "")
        self.assertEqual(req.prompt_segments, [])
        self.assertEqual(req.cached_tools, [])
        self.assertEqual(req.metadata, {})
        self.assertIsInstance(req.actor, Actor)
        req.actor = Actor(id="cli:local", platform="cli", platform_user_id="local")
        self.assertEqual(req.actor.id, "cli:local")

    def test_run_result_carries_taken_over_and_parse_error(self) -> None:
        result = RunResult()
        self.assertFalse(result.taken_over)
        self.assertIsNone(result.parse_error)
        self.assertFalse(result.parsed.completed)
        result = RunResult(
            session_id="s",
            message_id="m",
            text="t",
            parsed=JSONResult(completed=True, report="ok"),
            parse_error=ValueError("boom"),
            taken_over=True,
            outcome=OutcomeTakenOver,
        )
        self.assertEqual(result.parsed.report, "ok")
        self.assertEqual(result.outcome, OutcomeTakenOver)

    def test_background_tool_whitelist(self) -> None:
        self.assertEqual(BACKGROUND_TOOL_DENIED, ("discover_tool", "workspace"))
        self.assertFalse(background_tool_allowed("discover_tool"))
        self.assertFalse(background_tool_allowed(" Discover_Tool "))
        self.assertFalse(background_tool_allowed("workspace"))
        self.assertTrue(background_tool_allowed("shell"))
        self.assertTrue(background_tool_allowed(""))
        self.assertTrue(background_tool_allowed("read_file"))


class TestBackgroundShellRisk(unittest.TestCase):
    """cron / Atrinis 后台 shell 的风险裁决（对应 Go ``confirmBackgroundSandboxShell``）。

    裁决是 fail-closed：没有命令文本、静态确认不出来、PowerShell 主机、结构违规、
    风险非 low、白名单未命中 —— 全部拒绝并带可区分的原因。
    """

    def setUp(self) -> None:
        # 本机 ``is_power_shell_env()`` 恒为 True（真实环境就是 PowerShell）：白名单 /
        # 结构 / 等级这些 bash 口径的分支要在显式构造的 bash 主机下验证。
        from unittest import mock

        self._risk_patch = mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=False
        )
        self._sandbox_patch = mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=False
        )
        self._risk_patch.start()
        self._sandbox_patch.start()

    def tearDown(self) -> None:
        self._risk_patch.stop()
        self._sandbox_patch.stop()

    def _sandbox(self, kind: str = "cron", background: bool = True) -> SandboxContext:
        return SandboxContext(root="r", dir="r/cron/x", background=background, background_kind=kind)

    def test_reject_without_cmd_text(self) -> None:
        decision = confirm_background_sandbox_shell(self._sandbox(), "shell", RiskLow)
        self.assertTrue(decision.handled, "后台特例必须自己处理，不得转去等用户确认")
        self.assertFalse(decision.confirm)
        self.assertIn("命令文本", decision.reason)

    def test_critical_is_rejected_without_waiting_for_user(self) -> None:
        decision = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskCritical, cmd_text="rm -rf /"
        )
        self.assertTrue(decision.handled, "后台特例必须自己处理，不得转去等用户确认")
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.reason, "拒绝必须带原因（写进审计）")
        self.assertIn("critical", BACKGROUND_SHELL_CRITICAL_MESSAGE)

    def test_allowlisted_low_risk_is_auto_confirmed(self) -> None:
        decision = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskLow, cmd_text="ls -la"
        )
        self.assertTrue(decision.handled)
        self.assertTrue(decision.confirm, "低风险 + 白名单应自动确认")
        confirm, handled = decision
        self.assertTrue(confirm and handled)
        self.assertIn("白名单", decision.reason)

    def test_high_risk_is_rejected_even_without_critical(self) -> None:
        decision = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskHigh, cmd_text="git clean -fd"
        )
        self.assertTrue(decision.handled)
        self.assertFalse(decision.confirm, "high 不再自动确认（Go 旧口径的缺口）")
        self.assertIn("低风险", decision.reason)

    def test_command_outside_allowlist_is_rejected_with_reason(self) -> None:
        # ``uname`` 低风险但不在白名单里：命中"白名单未命中"分支（``python tool.py``
        # 会先被判 medium，拿到的原因是风险超限）。
        decision = confirm_background_sandbox_shell(
            self._sandbox("atrinis"), "shell", RiskLow, cmd_text="uname -a"
        )
        self.assertTrue(decision.confirm is False and decision.handled is True)
        self.assertIn("白名单", decision.reason)
        self.assertIn("uname", decision.reason)

    def test_structural_violations_are_rejected(self) -> None:
        for command, needle in (
            ("ls | grep TODO", "管道"),
            ("ls > out.txt", "重定向"),
            ("cat $(ls)", "替换"),
            ("ls &", "后台执行符"),
        ):
            with self.subTest(command=command):
                decision = confirm_background_sandbox_shell(
                    self._sandbox(), "shell", RiskLow, cmd_text=command
                )
                self.assertFalse(decision.confirm, command)
                self.assertIn(needle, decision.reason)

    def test_power_shell_host_is_rejected(self) -> None:
        from unittest import mock

        with mock.patch(
            "atri_x.tool.builtin.shell_risk.is_power_shell_env", return_value=True
        ), mock.patch(
            "atri_x.tool.builtin.shell_sandbox.is_power_shell_env", return_value=True
        ):
            decision = confirm_background_sandbox_shell(
                self._sandbox(), "shell", RiskLow, cmd_text="ls -la"
            )
        self.assertFalse(decision.confirm)
        self.assertIn("PowerShell", decision.reason)

    def test_dynamic_command_is_rejected(self) -> None:
        decision = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskLow, cmd_text='"$CMD" --version'
        )
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.reason)

    def test_unparsable_command_is_rejected(self) -> None:
        decision = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskLow, cmd_text="if true; then"
        )
        self.assertFalse(decision.confirm)
        self.assertTrue(decision.reason)

    def test_allow_all_restores_go_behaviour_but_still_needs_static_input(self) -> None:
        class _Policy:
            allow: frozenset[str] = frozenset()
            allow_all = True

        # Go 旧口径：非 critical 自动确认（白名单/结构都不再看）
        high = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskHigh, cmd_text="git clean -fd", policy=_Policy()
        )
        self.assertTrue(high.confirm and high.handled)
        # 但"没有命令文本 / 静态确认不出来"仍然拒绝
        empty = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskHigh, cmd_text="  ", policy=_Policy()
        )
        self.assertFalse(empty.confirm)
        self.assertIn("命令文本", empty.reason)
        dynamic = confirm_background_sandbox_shell(
            self._sandbox(), "shell", RiskHigh, cmd_text='"$CMD"', policy=_Policy()
        )
        self.assertFalse(dynamic.confirm)
        self.assertIn("静态确认", dynamic.reason)

    def test_only_cron_and_atrinis_background_shell_is_special_cased(self) -> None:
        self.assertEqual(
            confirm_background_sandbox_shell(
                self._sandbox(), "read_file", RiskCritical, cmd_text="rm -rf /"
            ),
            ShellRiskDecision(),
        )
        self.assertEqual(
            confirm_background_sandbox_shell(
                self._sandbox("other"), "shell", RiskHigh, cmd_text="ls -la"
            ),
            ShellRiskDecision(),
        )
        self.assertEqual(
            confirm_background_sandbox_shell(
                self._sandbox(background=False), "shell", RiskHigh, cmd_text="ls -la"
            ),
            ShellRiskDecision(),
        )
        self.assertEqual(
            confirm_background_sandbox_shell(None, "shell", RiskCritical, cmd_text="ls"),
            ShellRiskDecision(),
        )

    def test_audit_event_names_match_go(self) -> None:
        self.assertEqual(BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, "background_shell_auto_confirmed")
        self.assertEqual(BACKGROUND_SHELL_AUDIT_REJECTED, "background_shell_rejected")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
