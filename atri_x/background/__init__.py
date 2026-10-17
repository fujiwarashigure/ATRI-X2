"""cron / Atrinis 共用的后台 LLM 类型与结果 helper。

对应 Go：``internal/background/``（``background.go`` + ``report.go`` + ``result.go``，140 行）。

Go 侧三个文件同属 ``package background``，本包逐一对应为
:mod:`atri_x.background.background` / :mod:`atri_x.background.report` /
:mod:`atri_x.background.result`，并在此再导出，使
``from atri_x.background import ParseJSONResult`` 这类引用不受拆分影响。
"""

from __future__ import annotations

from atri_x.background.background import (
    BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED,
    BACKGROUND_SHELL_AUDIT_REJECTED,
    BACKGROUND_SHELL_CRITICAL_MESSAGE,
    BACKGROUND_TOOL_DENIED,
    JSONResult,
    Kind,
    KindAtrinis,
    KindCron,
    OutcomeTakenOver,
    RunRequest,
    RunResult,
    Runner,
    ShellRiskDecision,
    background_tool_allowed,
    confirm_background_sandbox_shell,
)
from atri_x.background.report import (
    BuildReportOutputs,
    BuildReportSegmentOutput,
    PathInstruction,
    build_report_outputs,
    build_report_segment_output,
    path_instruction,
    valid_media_id,
)
from atri_x.background.result import (
    DefaultJSONRetryPrompt,
    ParseJSONResult,
    default_json_retry_prompt,
    parse_json_result,
)

__all__ = [
    "BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED",
    "BACKGROUND_SHELL_AUDIT_REJECTED",
    "BACKGROUND_SHELL_CRITICAL_MESSAGE",
    "BACKGROUND_TOOL_DENIED",
    "BuildReportOutputs",
    "BuildReportSegmentOutput",
    "DefaultJSONRetryPrompt",
    "JSONResult",
    "Kind",
    "KindAtrinis",
    "KindCron",
    "OutcomeTakenOver",
    "ParseJSONResult",
    "PathInstruction",
    "RunRequest",
    "RunResult",
    "Runner",
    "ShellRiskDecision",
    "background_tool_allowed",
    "build_report_outputs",
    "build_report_segment_output",
    "confirm_background_sandbox_shell",
    "default_json_retry_prompt",
    "parse_json_result",
    "path_instruction",
    "valid_media_id",
]
