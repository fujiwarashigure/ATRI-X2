"""后台沙盒下的 shell 命令风险加固。

对应 Go：``internal/tool/builtin/shell_sandbox.go``。

background sandbox 是**轻量路径约束**，不是完整 OS 级沙箱。后台任务无人值守，
因此禁止绝对路径、``..`` 逃逸、动态路径和 ``cd``，尽量把 shell 影响限制在
sandbox cwd 内。命中违规时把风险抬到 ``critical`` 并附上逐条原因。

本仓在 Go 之上补了两层（研究报告 §6 的 P0）：

* **包装器/内层脚本递归检查**：``env cat /etc/passwd``、``bash -c 'cat /etc/passwd'``
  这类被包装器或 ``-c`` 遮住的路径参数同样会被检查；
* **后台默认拒绝 + 白名单**（:class:`BackgroundShellPolicy`）：cron / Atrinis 这类
  无人值守场景只自动放行"整条命令低风险 + 真实命令名都在白名单里"的调用，
  其余一律拒绝。需要别的命令时在 ``security.background_shell_allow`` 里放行，
  或（明确知情）用 ``security.background_shell_allow_all`` 恢复 Go 的旧行为。
* **白名单的结构约束**（:func:`background_shell_structure_violation`）：白名单只比
  命令名不够 —— ``ls > out.txt`` / ``ls | sh`` / ``sh -c '…'`` 的"名字"也许都在白名单
  里，真正的行为却由结构决定。管道、重定向、命令或进程替换、算术展开、复合/控制
  结构、``-c`` 内联脚本、``eval``/``trap``/``source`` 一律拒绝。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from atri_x.security import compare_risk
from atri_x.shellparse import (
    CompoundCommand,
    Redirect,
    ShellParseError,
    SimpleCommand,
    has_arithmetic,
    has_background,
    has_command_substitution,
    has_pipe,
    has_process_substitution,
    has_redirect,
    iter_nodes,
    parse,
)
from atri_x.tool.builtin.shell_env import is_power_shell_env
from atri_x.tool.builtin.shell_risk import (
    shell_command_names,
    shell_script_arg,
    unwrap_wrapper,
)
from atri_x.tool.builtin.shell_syntax import command_base, literal_word, literal_words
from atri_x.tool.tool import RiskAssessment, RiskCritical, RiskLow

WINDOWS_ABS_PATH_PATTERN = re.compile(r"^[A-Za-z]:/")

#: 递归检查包装器 / ``-c`` 内层脚本的深度上限。
_MAX_SANDBOX_DEPTH = 8

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

#: 后台无人值守时默认允许自动执行的命令名（只读检查类）。
#: 只有"整条命令低风险"时才会命中白名单，所以 ``git`` 这类既能读也能改的命令
#: 不会因为 ``git clean`` 被放行（``git clean`` 本身判 high）。
DEFAULT_BACKGROUND_SHELL_ALLOW = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "pwd",
        "echo",
        "printf",
        "grep",
        "find",
        "cut",
        "sort",
        "uniq",
        "tr",
        "basename",
        "dirname",
        "realpath",
        "stat",
        "file",
        "du",
        "df",
        "date",
        "whoami",
        "hostname",
        "git",
        "sed",
        "awk",
        "diff",
        "cmp",
        "true",
        "false",
        "sleep",
        "test",
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
    """对应 Go ``validateBashShellSandboxCommand``（含包装器/``-c`` 递归）。"""
    try:
        script = parse(cmd_text)
    except ShellParseError as exc:
        return [f"background sandbox 无法解析 shell AST，拒绝后台自动执行: {exc}"]
    checker = ShellSandboxChecker()
    _check_script(checker, script, 0)
    return checker.violations


def _check_script(checker: "ShellSandboxChecker", script: Any, depth: int) -> None:
    for node in iter_nodes(script):
        if isinstance(node, Redirect):
            checker.check_redirect(node)
        elif isinstance(node, SimpleCommand):
            checker.check_call(node)
            if depth >= _MAX_SANDBOX_DEPTH or not node.words:
                continue
            name, ok = literal_word(node.words[0])
            if not ok or name == "":
                continue
            base = command_base(name)
            args = literal_words(node.args())
            _check_inner(checker, base, args, depth + 1)


def _check_inner(
    checker: "ShellSandboxChecker", name: str, args: list[str], depth: int
) -> None:
    """检查包装器内层命令与 ``-c`` 脚本文本里的路径参数。"""
    unwrapped = unwrap_wrapper(name, args)
    if unwrapped is not None:
        mode, payload = unwrapped
        if mode == "script":
            _check_text(checker, payload[0], depth)
        elif payload:
            checker.check_words(command_base(payload[0]), payload[1:], depth)
        return
    script = shell_script_arg(name, args)
    if script:
        _check_text(checker, script, depth)


def _check_text(checker: "ShellSandboxChecker", text: str, depth: int) -> None:
    if depth > _MAX_SANDBOX_DEPTH:
        checker.add("内层脚本文本嵌套过深，background sandbox 无法确认")
        return
    try:
        script = parse(text)
    except ShellParseError as exc:
        checker.add(f"内层脚本文本无法解析，background sandbox 拒绝: {exc}")
        return
    _check_script(checker, script, depth)


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
        """对应 Go ``checkCall``（逐词判字面量，动态词必须报出来）。"""
        if call is None or not call.words:
            return
        name, ok = literal_word(call.words[0])
        if not ok or name == "":
            self.add("命令名包含动态结构，background sandbox 无法静态确认")
            return
        base = command_base(name)
        if base == "cd":
            self.add("后台 shell 不允许 cd")
            return
        if base not in _SANDBOX_PATH_COMMANDS:
            return
        for word in call.args():
            arg, literal = literal_word(word)
            if not literal:
                self.add(f"{base} 的路径参数包含变量、命令替换或其他动态结构")
                continue
            if arg == "" or arg.startswith("-"):
                continue
            self.check_path(arg, base + " 参数")

    def check_words(self, name: str, args: list[str], depth: int = 0) -> None:
        """检查一条已解包命令的字面参数（包装器内层，无动态词信息）。"""
        if depth > _MAX_SANDBOX_DEPTH:
            self.add("命令嵌套过深，background sandbox 无法确认")
            return
        if name == "cd":
            self.add("后台 shell 不允许 cd")
            return
        if name not in _SANDBOX_PATH_COMMANDS:
            return
        for arg in args:
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


# ---------------------------------------------------------------------------
# 后台（cron / Atrinis）默认拒绝 + 白名单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackgroundShellPolicy:
    """无人值守后台 shell 的放行策略。

    ``allow``：允许自动执行的命令名（只在整条命令低风险时才生效）。
    ``allow_all``：显式恢复 Go 的旧行为（后台只拦 critical，其余自动放行）。
    """

    allow: frozenset[str] = DEFAULT_BACKGROUND_SHELL_ALLOW
    allow_all: bool = False


#: 即使在白名单里也一律拒绝的命令：它们本身就是"再执行一段脚本文本"。
_BACKGROUND_SCRIPT_COMMANDS = frozenset({"eval", "source", ".", "trap"})


def background_shell_structure_violation(cmd_text: str) -> str:
    """后台白名单的**结构约束**：只放行单条、静态可确认的简单命令。

    只比较命令名会漏掉 ``ls > out.txt``、``cat f | sh``、``sh -c '…'`` 这类
    "名字可能都在白名单里、实际行为由结构决定"的调用。这里复用
    :mod:`atri_x.shellparse` 的 AST 查询与 :mod:`atri_x.tool.builtin.shell_risk`
    的包装器解包（``unwrap_wrapper`` / ``shell_script_arg``），不另写解析器。

    返回空串表示结构通过，否则返回拒绝原因。
    """
    text = (cmd_text or "").strip()
    if text == "":
        return "后台无人值守缺少命令文本，无法静态确认"
    try:
        script = parse(text)
    except ShellParseError as exc:
        return f"后台无人值守无法静态解析命令: {exc}"
    if has_command_substitution(script) or has_process_substitution(script):
        return "命令包含命令替换或进程替换，执行内容无法静态确认"
    if has_arithmetic(script):
        return "命令包含算术展开，参数无法静态确认"
    if has_pipe(script):
        return "命令包含管道，后台无人值守只放行单条命令"
    if has_redirect(script):
        return "命令包含重定向，后台无人值守只放行无重定向命令"
    if has_background(script):
        return "命令包含后台执行符 &，后台无人值守不允许"
    return _background_script_violation(script, 0)


def _background_script_violation(script: Any, depth: int) -> str:
    for node in iter_nodes(script):
        if isinstance(node, CompoundCommand):
            return (
                "命令包含复合/控制结构（if/for/while/case/函数/子 shell），"
                "后台无人值守只放行单条命令"
            )
        if isinstance(node, SimpleCommand):
            violation = _background_call_violation(node, depth)
            if violation != "":
                return violation
    return ""


def _background_call_violation(call: SimpleCommand, depth: int) -> str:
    """按 argv 形态检查一条命令（含包装器内层）。"""
    if not call.words:
        return ""
    name, ok = literal_word(call.words[0])
    if not ok or name == "":
        return "命令名包含变量、命令替换或其他动态结构，无法静态确认"
    base = command_base(name)
    args = literal_words(call.args())
    if len(args) != len(call.args()):
        return f"{base} 的参数包含变量、命令替换或其他动态结构，无法静态确认"
    violation = _background_args_violation(base, args)
    if violation != "":
        return violation
    unwrapped = unwrap_wrapper(base, args)
    if unwrapped is None:
        return ""
    if depth >= _MAX_SANDBOX_DEPTH:
        return "包装器嵌套过深，background sandbox 无法确认"
    mode, payload = unwrapped
    if mode == "script":
        return f"{base} 后面是需要二次解析的脚本文本，后台无人值守不允许"
    if not payload:
        return ""
    return _background_args_violation(command_base(payload[0]), payload[1:])


def _background_args_violation(name: str, args: list[str]) -> str:
    """单个命令的 argv 形态与路径约束（复用沙盒路径检查）。"""
    if name in _BACKGROUND_SCRIPT_COMMANDS:
        return f"{name} 会执行脚本文本，后台无人值守不允许"
    if shell_script_arg(name, args):
        return f"{name} -c 内联脚本文本，后台无人值守不允许"
    for arg in args:
        if arg == "" or arg.startswith("-"):
            continue
        reason = sandbox_path_violation(arg)
        if reason != "":
            return f"{name} 参数{reason}"
    return ""


def background_shell_policy_from_config(security_cfg: Any) -> BackgroundShellPolicy:
    """从 ``[security]`` 段构造策略；字段缺失时用内置默认（默认拒绝 + 只读白名单）。"""
    raw_allow = getattr(security_cfg, "background_shell_allow", None)
    allow_all = bool(getattr(security_cfg, "background_shell_allow_all", False))
    if raw_allow is None:
        allow = DEFAULT_BACKGROUND_SHELL_ALLOW
    else:
        allow = frozenset(
            str(item).strip().lower() for item in raw_allow if str(item).strip()
        )
    return BackgroundShellPolicy(allow=allow, allow_all=allow_all)


def is_unattended_background(sandbox: Any) -> bool:
    """cron / Atrinis 这类"没人看着"的后台种类。"""
    if sandbox is None or not getattr(sandbox, "background", False):
        return False
    kind = str(getattr(sandbox, "background_kind", "") or "")
    return kind in _unattended_kinds()


def _unattended_kinds() -> frozenset[str]:
    # 延迟 import：atri_x.background 会 import 本包，模块级 import 会成环。
    from atri_x.background import Kind

    return frozenset({Kind.CRON, Kind.ATRINIS})


def apply_background_shell_policy(
    cmd_text: str, assessment: RiskAssessment, policy: BackgroundShellPolicy
) -> RiskAssessment:
    """无人值守后台的放行裁决：默认拒绝，只在"低风险 + 白名单 + 结构合规"时放行。

    已经 critical 的命令不改（调用方本来就拒绝）；PowerShell 主机无法用 bash AST
    约束，直接按 critical 拒绝（除非显式 ``allow_all``）。放行面按顺序收紧：
    PowerShell 主机 → 结构约束（管道/重定向/替换/内联脚本…）→ 风险等级 → 白名单。
    """
    if policy.allow_all:
        return assessment
    if compare_risk(RiskCritical, assessment.level) <= 0:
        return assessment
    if is_power_shell_env():
        return _deny(
            assessment,
            "后台 PowerShell 命令无法静态约束，默认拒绝；"
            "确需放行请把 security.background_shell_allow_all 设为 true",
        )
    structure = background_shell_structure_violation(cmd_text)
    if structure != "":
        return _deny(assessment, structure)
    if assessment.level != RiskLow:
        return _deny(
            assessment,
            f"后台无人值守只自动执行低风险只读命令，该命令被判为 {assessment.level}",
        )
    names, confirmed = shell_command_names(cmd_text)
    if not confirmed or not names:
        return _deny(assessment, "后台无人值守无法静态确认命令内容，默认拒绝")
    disallowed = sorted({name for name in names if name not in policy.allow})
    if disallowed:
        return _deny(
            assessment,
            "后台自动执行白名单不含：" + ", ".join(disallowed)
            + "（可在 security.background_shell_allow 里放行）",
        )
    return assessment


def _deny(assessment: RiskAssessment, reason: str) -> RiskAssessment:
    assessment.level = RiskCritical
    assessment.reasons = append_unique_reasons(assessment.reasons, [reason])
    return assessment


__all__ = [name for name in dir() if not name.startswith("_")]
