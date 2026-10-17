"""后台沙盒下的 shell 命令风险加固。

对应 Go：``internal/tool/builtin/shell_sandbox.go``。

background sandbox 是**轻量路径约束**，不是完整 OS 级沙箱。后台任务无人值守，
因此禁止绝对路径、``..`` 逃逸、动态路径和 ``cd``，尽量把 shell 影响限制在
sandbox cwd 内。命中违规时把风险抬到 ``critical`` 并附上逐条原因。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from atri_x.security import compare_risk
from atri_x.shellparse import Redirect, ShellParseError, SimpleCommand, iter_nodes, parse
from atri_x.tool.builtin.shell_env import is_power_shell_env
from atri_x.tool.builtin.shell_syntax import command_base, literal_word
from atri_x.tool.tool import RiskAssessment, RiskCritical

WINDOWS_ABS_PATH_PATTERN = re.compile(r"^[A-Za-z]:/")

_SANDBOX_PATH_COMMANDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "ls",
        "touch",
        "mkdir",
        "rm",
        "rmdir",
        "cp",
        "mv",
        "tee",
        "find",
        "grep",
        "sed",
    }
)


def apply_shell_sandbox_risk(
    cmd_text: str, assessment: RiskAssessment
) -> RiskAssessment:
    """对应 Go ``applyShellSandboxRisk``。"""
    if is_power_shell_env():
        return assessment
    return apply_bash_shell_sandbox_risk(cmd_text, assessment)


def apply_bash_shell_sandbox_risk(
    cmd_text: str, assessment: RiskAssessment
) -> RiskAssessment:
    """对应 Go ``applyBashShellSandboxRisk``。"""
    violations = validate_bash_shell_sandbox_command(cmd_text)
    if not violations:
        return assessment
    if compare_risk(RiskCritical, assessment.level) > 0:
        assessment.level = RiskCritical
    assessment.reasons = append_unique_reasons(assessment.reasons, violations)
    return assessment


def validate_bash_shell_sandbox_command(cmd_text: str) -> list[str]:
    """对应 Go ``validateBashShellSandboxCommand``。"""
    try:
        script = parse(cmd_text)
    except ShellParseError as exc:
        return [f"background sandbox 无法解析 shell AST，拒绝后台自动执行: {exc}"]
    checker = ShellSandboxChecker()
    for node in iter_nodes(script):
        if isinstance(node, Redirect):
            checker.check_redirect(node)
        elif isinstance(node, SimpleCommand):
            checker.check_call(node)
    return checker.violations


@dataclass
class ShellSandboxChecker:
    """对应 Go ``shellSandboxChecker``。"""

    violations: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        """对应 Go ``shellSandboxChecker.add``。"""
        reason = (reason or "").strip()
        if reason == "" or reason in self.violations:
            return
        self.violations.append(reason)

    def check_redirect(self, redir: Redirect) -> None:
        """对应 Go ``checkRedirect``。"""
        if redir is None or redir.word is None:
            return
        op = redir.op or ""
        if ">" not in op and "<>" not in op:
            return
        self.check_path_word(redir.word, "重定向目标")

    def check_call(self, call: SimpleCommand) -> None:
        """对应 Go ``checkCall``。"""
        if call is None or not call.words:
            return
        name, ok = literal_word(call.words[0])
        if not ok or name == "":
            self.add("命令名包含动态结构，background sandbox 无法静态确认")
            return
        name = command_base(name)
        if name == "cd":
            self.add("后台 shell 不允许 cd")
            return
        if name not in _SANDBOX_PATH_COMMANDS:
            return
        for word in call.args():
            arg, ok = literal_word(word)
            if not ok:
                self.add(f"{name} 的路径参数包含变量、命令替换或其他动态结构")
                continue
            if arg == "" or arg.startswith("-"):
                continue
            self.check_path(arg, name + " 参数")

    def check_path_word(self, word: object, label: str) -> None:
        """对应 Go ``checkPathWord``。"""
        value, ok = literal_word(word)  # type: ignore[arg-type]
        if not ok:
            self.add(label + "包含变量、命令替换或其他动态结构")
            return
        self.check_path(value, label)

    def check_path(self, value: str, label: str) -> None:
        """对应 Go ``checkPath``。"""
        reason = sandbox_path_violation(value)
        if reason != "":
            self.add(label + reason)


def sandbox_path_violation(value: str) -> str:
    """对应 Go ``sandboxPathViolation``。"""
    path = (value or "").strip().strip("'\"").replace("\\", "/")
    if path == "" or path.startswith("-"):
        return ""
    if path.startswith("~"):
        return "使用 home 路径"
    if path.startswith("//"):
        return "使用 UNC 或网络绝对路径"
    if path.startswith("/") or WINDOWS_ABS_PATH_PATTERN.match(path):
        return "使用绝对路径"
    clean = posixpath.normpath(path)
    if clean == ".." or clean.startswith("../"):
        return "逃逸 background sandbox"
    return ""


def append_unique_reasons(base: list[str], extra: list[str]) -> list[str]:
    """对应 Go ``appendUniqueReasons``。"""
    out = list(base)
    for reason in extra:
        reason = (reason or "").strip()
        if reason == "" or reason in out:
            continue
        out.append(reason)
    return out


__all__ = [name for name in dir() if not name.startswith("_")]
