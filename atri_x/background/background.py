"""cron 与 Atrinis 共用的后台 LLM 类型。对应 Go：``internal/background/background.go``。

本模块是 cron / Atrinis 两条后台链路的**共享契约**：后台 runner 接口、一次后台运行
的请求与结果、以及 LLM 最终回复的 JSON 结构。字段与 Go 的 ``RunRequest`` /
``RunResult`` / ``JSONResult`` 逐一对齐（Go 的 ``PascalCase`` → 本仓 ``snake_case``）。

与 Go 的差异（都是 Python 侧的等价物，不是简化）：

* ``Runner`` 在 Go 里是 ``internal/agent`` 实现的接口；本仓用 :class:`Runner`
  ``Protocol`` 表达，**可注入**（不 import ``atri_x.agent``，避免并行开发期循环依赖）。
* ``RunRequest.CachedTools`` 的类型是 ``toolrun.CachedTool``。``atri_x.toolrun``
  尚未落地，本模块按鸭子类型把它声明为 ``list[Any]``：集成期应改为
  ``from atri_x.toolrun import CachedTool`` 并收紧标注，字段定义唯一出处仍是 toolrun。
* ``BackgroundToolAllowed`` 的宿主是 ``internal/toolrun/toolrun.go``；
  ``atri_x.toolrun`` 未落地前用下面的本地兜底白名单，集成期改为 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from atri_x.llm.llm import MessageSegment
from atri_x.security import Actor, RiskCritical, compare_risk
from atri_x.tool.sandbox import BackgroundKind, SandboxContext
from atri_x.tool.tool import RiskAssessment

__all__ = [
    "BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED",
    "BACKGROUND_SHELL_AUDIT_REJECTED",
    "BACKGROUND_SHELL_CRITICAL_MESSAGE",
    "BACKGROUND_TOOL_DENIED",
    "JSONResult",
    "Kind",
    "KindAtrinis",
    "KindCron",
    "OutcomeTakenOver",
    "RunRequest",
    "RunResult",
    "Runner",
    "ShellRiskDecision",
    "background_tool_allowed",
    "confirm_background_sandbox_shell",
]


class Kind:
    """后台任务种类。对应 Go ``KindCron`` / ``KindAtrinis``。"""

    CRON = "cron"
    ATRINIS = "atrinis"


KindCron = Kind.CRON
KindAtrinis = Kind.ATRINIS

#: 后台任务在运行中被前台接管而提前结束：既不成功也不失败，不产生汇报也不重复投递。
#: 对应 Go ``OutcomeTakenOver``。
OutcomeTakenOver = "taken_over"

#: 后台上下文里被禁用的工具名（小写比较）。对应 Go ``toolrun.BackgroundToolAllowed``
#: 的排除项：``discover_tool`` 会把全量工具重新暴露出来，``workspace`` 是纯前台关注点。
#:
#: 兜底常量：``atri_x.toolrun`` 落地后应改为 ``from atri_x.toolrun import
#: BackgroundToolAllowed`` 并删除本常量。
BACKGROUND_TOOL_DENIED = ("discover_tool", "workspace")


def background_tool_allowed(name: str) -> bool:
    """后台任务是否允许使用该工具。

    对应 Go ``toolrun.BackgroundToolAllowed``。后台 Session 只拿到任务预载的工具。
    """
    return (name or "").strip().lower() not in BACKGROUND_TOOL_DENIED


@runtime_checkable
class Runner(Protocol):
    """后台 runner（Go ``Runner`` 接口）。由 ``atri_x.agent`` 的 ``RunBackground`` 实现。

    ``run_background`` 可以是同步函数，也可以返回 awaitable（本仓调用点统一用
    ``await`` 兼容两者）。
    """

    def run_background(self, ctx: Any, req: "RunRequest") -> Any:
        ...


@dataclass
class RunRequest:
    """一次后台运行请求。对应 Go ``RunRequest``。"""

    kind: str = ""
    name: str = ""
    title: str = ""
    platform: str = ""
    actor: Actor = field(default_factory=Actor)
    scope_id: str = ""
    session_id: str = ""
    model_provider: str = ""
    model: str = ""
    session_mode: str = ""
    prompt_segments: list[MessageSegment] = field(default_factory=list)
    prompt: str = ""
    retry_prompt: str = ""
    tool_list_names: list[str] = field(default_factory=list)
    #: ``atri_x.toolrun.CachedTool`` 列表（该模块尚未落地，故按鸭子类型声明）。
    cached_tools: list[Any] = field(default_factory=list)
    sandbox_subdir: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class JSONResult:
    """后台任务最终回复的 JSON 结构。对应 Go ``JSONResult``。

    json tag：``completed`` / ``need_report`` / ``report_segments,omitempty`` / ``report``。
    """

    completed: bool = False
    need_report: bool = False
    report_segments: list[MessageSegment] = field(default_factory=list)
    report: str = ""

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 json tag 序列化（``report_segments`` 为空时省略）。"""
        out: dict[str, Any] = {
            "completed": self.completed,
            "need_report": self.need_report,
        }
        if self.report_segments:
            out["report_segments"] = [segment.to_json() for segment in self.report_segments]
        out["report"] = self.report
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "JSONResult":
        """按 Go ``json.Unmarshal`` 的口径解析：未知键忽略，类型不符即报错。

        解析失败抛 ``ValueError``（Go 返回 ``error``）。
        """
        if not isinstance(raw, dict):
            raise ValueError("json result must be an object")
        result = cls()
        if "completed" in raw:
            result.completed = _bool_field(raw["completed"], "completed")
        if "need_report" in raw:
            result.need_report = _bool_field(raw["need_report"], "need_report")
        if "report" in raw:
            value = raw["report"]
            if not isinstance(value, str):
                raise ValueError("json: cannot unmarshal non-string into report")
            result.report = value
        segments = raw.get("report_segments")
        if segments is not None:
            if not isinstance(segments, list):
                raise ValueError("json: cannot unmarshal non-array into report_segments")
            result.report_segments = [MessageSegment.from_json(item) for item in segments]
        return result


def _bool_field(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"json: cannot unmarshal non-bool into {name}")


@dataclass
class RunResult:
    """一次后台运行的结果。对应 Go ``RunResult``。

    ``parsed`` / ``parse_error`` 对应 Go 的 ``Parsed JSONResult`` / ``ParseErr error``：
    Go 用零值 + error 表达"没解析出来"，本仓同样不抛异常（结果对象要能承载失败）。
    """

    session_id: str = ""
    message_id: str = ""
    text: str = ""
    parsed: JSONResult = field(default_factory=JSONResult)
    parse_error: BaseException | None = None
    #: ``True`` 表示该 Session 已被前台接管，本次后台运行在安全点自行停止，
    #: ``text`` 为空且不应当作报告解析。
    taken_over: bool = False
    outcome: str = ""


# ---------------------------------------------------------------------------
# cron / Atrinis 后台 shell 的风险裁决
# ---------------------------------------------------------------------------
#: 后台沙盒拒绝 critical shell 时写进工具结果提醒的文案。
#: 对应 Go ``confirmBackgroundSandboxShell`` 里的 ``message.Segments`` 文本。
BACKGROUND_SHELL_CRITICAL_MESSAGE = (
    "后台沙盒禁止执行 critical 风险 shell 命令。请改用相对路径，把文件限制在当前 sandbox 目录内，"
    "避免绝对路径、.. 逃逸和危险命令；若仍无法完成，请在最终报告中说明阻塞原因。"
)

#: 审计事件名（与 Go 逐字一致，供 Agent 侧直接使用）。
BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED = "background_shell_auto_confirmed"
BACKGROUND_SHELL_AUDIT_REJECTED = "background_shell_rejected"


@dataclass
class ShellRiskDecision:
    """后台 shell 的风险裁决结果。

    对应 Go ``confirmBackgroundSandboxShell`` 的 ``(bool, bool)`` 返回值：
    ``confirm`` = 是否放行本次调用，``handled`` = 本次调用是否已由后台特例处理
    （``handled`` 为真时调用方**不要**再去等用户确认）。

    ``reason`` 是本仓新增的裁决原因（写进审计，必须能区分"为什么拒绝/为什么放行"）。
    """

    confirm: bool = False
    handled: bool = False
    reason: str = ""

    def __iter__(self):
        """允许 ``confirm, handled = decision`` 的元组式解包（对齐 Go 的双返回值）。"""
        return iter((self.confirm, self.handled))


def confirm_background_sandbox_shell(
    sandbox: SandboxContext | None,
    tool_name: str,
    risk: str,
    *,
    cmd_text: str = "",
    policy: Any = None,
) -> ShellRiskDecision:
    """cron / Atrinis 后台任务的 shell 风险裁决（fail-closed）。

    对应 Go：``internal/agent/cron_tools.go`` 的 ``confirmBackgroundSandboxShell``
    （Agent 的方法）。本仓把"纯决策"部分放在共享的 background 包里，Agent 侧只需按
    :class:`ShellRiskDecision` 记审计（``BACKGROUND_SHELL_AUDIT_*``）并在拒绝时把
    :data:`BACKGROUND_SHELL_CRITICAL_MESSAGE` 写进工具结果——**不等待用户确认**。

    与 Go 的差异（研究报告 §6 的 P0）：Go 只看评估阶段传进来的 ``risk``，一旦评估
    阶段没有沙盒 ctx（或评估失败），确认阶段就只剩"自动确认"。本仓在确认阶段用
    ``cmd_text`` **重新**做一遍静态裁决，任何一步确认不了都拒绝：

    * 非后台、非 shell、或后台种类不是 cron / Atrinis → ``handled=False``（走既有流程）；
    * 没有命令文本 / 静态确认不出来（解析失败、命令名是变量） → 拒绝；
    * 后台策略拒绝（PowerShell 主机 / 结构违规 / 风险非 low / 白名单未命中） → 拒绝；
    * 其余 → 自动确认（不等用户）。

    ``policy.allow_all=True`` 时恢复 Go 的旧口径（critical 才拒绝、其余自动确认），
    但"没有命令文本 / 静态确认不出来"仍然拒绝。
    """
    if sandbox is None or not sandbox.background:
        return ShellRiskDecision()
    if tool_name != "shell":
        return ShellRiskDecision()
    if sandbox.background_kind not in (KindCron, KindAtrinis):
        return ShellRiskDecision()

    # 延迟 import：``atri_x.tool.builtin`` 会拉起整个内置工具包，本模块是共享契约，
    # 不在 import 期引入这层依赖（同一个包，重复 import 由 sys.modules 兜住）。
    from atri_x.tool.builtin.shell_risk import classify_shell_command, shell_command_names
    from atri_x.tool.builtin.shell_sandbox import (
        BackgroundShellPolicy,
        apply_background_shell_policy,
        apply_shell_sandbox_risk,
    )

    text = (cmd_text or "").strip()
    if text == "":
        return ShellRiskDecision(
            confirm=False,
            handled=True,
            reason="后台 shell 调用缺少命令文本，无法静态确认",
        )
    _, confirmed = shell_command_names(text)
    if not confirmed:
        return ShellRiskDecision(
            confirm=False,
            handled=True,
            reason="命令内容无法静态确认（含动态命令名或无法解析），后台无人值守拒绝",
        )
    if policy is None:
        policy = BackgroundShellPolicy()
    assessment = classify_shell_command(text)
    assessment = apply_shell_sandbox_risk(text, assessment)
    before = list(assessment.reasons)
    assessment = apply_background_shell_policy(text, assessment, policy)
    if compare_risk(RiskCritical, assessment.level) <= 0:
        return ShellRiskDecision(
            confirm=False, handled=True, reason=_decision_reason(assessment, before)
        )
    if risk == RiskCritical:
        # 评估阶段（可能带着确认阶段拿不到的上下文）已经判过 critical：不允许复核
        # 把它降级成自动确认。
        return ShellRiskDecision(
            confirm=False,
            handled=True,
            reason="评估阶段已判为 critical 风险，后台无人值守拒绝",
        )
    return ShellRiskDecision(
        confirm=True,
        handled=True,
        reason="低风险且命中后台只读白名单，自动确认",
    )


def _decision_reason(assessment: RiskAssessment, before: list[str]) -> str:
    """取裁决原因：优先用后台策略/沙盒刚加上去的那一条，否则用评估阶段已有的。"""
    added = [reason for reason in assessment.reasons if reason not in before]
    if added:
        return added[-1]
    if assessment.reasons:
        return assessment.reasons[0]
    return "后台无人值守风险超限，默认拒绝"
