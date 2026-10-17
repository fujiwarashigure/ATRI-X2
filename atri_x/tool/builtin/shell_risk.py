"""Shell 命令的静态风险分级。

对应 Go：``internal/tool/builtin/shell_risk.go``。

分级口径与 Go 完全一致：``rm -rf`` / 提权 / 磁盘操作 / 下载即执行 = ``critical``，
写入、删除、解释器调用、管道进解释器 = ``high``，下载、调用解释器、构建 =
``medium``，其余 ``low``。PowerShell 环境无法用 bash AST 解析，一律 ``high``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.security import compare_risk
from atri_x.shellparse import CommandSubstitution, Redirect, SimpleCommand, iter_nodes, parse
from atri_x.shellparse import ShellParseError
from atri_x.tool.builtin.shell_env import is_power_shell_env
from atri_x.tool.builtin.shell_syntax import (
    command_base,
    has_arg,
    has_recursive,
    has_recursive_force,
    is_downloader,
    is_interpreter,
    literal_word,
    literal_words,
    touches_system_path,
)
from atri_x.tool.tool import (
    RiskAssessment,
    RiskCritical,
    RiskHigh,
    RiskLow,
    RiskMedium,
)

_GIT_READ_ONLY = frozenset({"status", "diff", "log", "show", "branch"})
_GIT_MUTATING = frozenset(
    {
        "reset",
        "clean",
        "checkout",
        "switch",
        "restore",
        "commit",
        "push",
        "add",
        "merge",
        "rebase",
        "cherry-pick",
        "stash",
    }
)


@dataclass
class ShellRiskClassifier:
    """对应 Go ``shellRiskClassifier``。"""

    level: str = RiskLow
    reasons: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    pipeline: list[str] = field(default_factory=list)
    parse_failed: bool = False

    def raise_risk(self, level: str, reason: str) -> None:
        """对应 Go ``shellRiskClassifier.raise``。"""
        if compare_risk(level, self.level) > 0:
            self.level = level
        if reason not in self.reasons:
            self.reasons.append(reason)

    def classify_call(self, call: SimpleCommand) -> None:
        """对应 Go ``classifyCall``。"""
        if not call.words:
            return
        name, ok = literal_word(call.words[0])
        if not ok or name == "":
            self.raise_risk(
                RiskHigh, "命令名包含变量、命令替换或其他动态结构，无法静态判断真实命令"
            )
            return
        name = command_base(name)
        args = literal_words(call.args())
        self.commands.append(name)
        self.pipeline.append(name)

        if name in ("rm", "rmdir", "del", "erase", "rd"):
            if has_recursive_force(args) or touches_system_path(args):
                self.raise_risk(
                    RiskCritical, f"使用删除命令 {name} 且包含递归/强制参数或系统路径"
                )
            else:
                self.raise_risk(RiskHigh, f"使用删除命令 {name}")
        elif name in ("sudo", "su", "doas", "runas"):
            self.raise_risk(RiskCritical, f"使用提权命令 {name}")
        elif name in (
            "mkfs",
            "fdisk",
            "parted",
            "diskpart",
            "format",
            "mount",
            "umount",
            "dd",
        ):
            self.raise_risk(RiskCritical, f"使用磁盘或系统级操作命令 {name}")
        elif name in ("chmod", "chown", "icacls", "takeown"):
            if has_recursive(args) or touches_system_path(args):
                self.raise_risk(RiskCritical, f"递归或系统路径权限修改命令 {name}")
            else:
                self.raise_risk(RiskHigh, f"权限或所有者修改命令 {name}")
        elif name in ("curl", "wget"):
            self.raise_risk(RiskMedium, f"使用网络下载命令 {name}")
        elif name == "git":
            self.classify_git(args)
        elif name in (
            "sh",
            "bash",
            "zsh",
            "fish",
            "python",
            "python3",
            "node",
            "perl",
            "ruby",
            "powershell",
            "pwsh",
        ):
            self.raise_risk(RiskMedium, f"调用解释器或 shell {name}")
        elif name == "sed":
            if has_arg(args, "-i"):
                self.raise_risk(RiskHigh, "sed -i 会原地修改文件")
        elif name in ("mv", "move", "cp", "copy", "install", "tee"):
            self.raise_risk(RiskHigh, f"可能写入或覆盖文件的命令 {name}")
        elif name in ("npm", "pnpm", "yarn", "pip", "pip3", "go", "cargo"):
            self.classify_package_or_build(name, args)

    def classify_git(self, args: list[str]) -> None:
        """对应 Go ``classifyGit``。"""
        if not args:
            return
        sub = args[0]
        if sub in _GIT_READ_ONLY:
            return
        if sub in _GIT_MUTATING:
            dropped = (sub == "reset" and has_arg(args[1:], "--hard")) or sub == "clean"
            if dropped:
                self.raise_risk(
                    RiskHigh, f"git {sub} 可能丢弃或删除工作区内容"
                )
                return
            self.raise_risk(RiskHigh, f"git {sub} 会修改仓库状态")

    def classify_package_or_build(self, name: str, args: list[str]) -> None:
        """对应 Go ``classifyPackageOrBuild``。"""
        if not args:
            return
        sub = args[0]
        if name == "go":
            if sub in ("test", "build", "run"):
                self.raise_risk(RiskMedium, f"go {sub} 可能执行项目代码")
            elif sub in ("install", "get"):
                self.raise_risk(RiskHigh, f"go {sub} 会安装或修改依赖")
            return
        if sub in ("install", "add", "remove", "update"):
            self.raise_risk(RiskHigh, f"{name} {sub} 会修改依赖或安装代码")
        elif sub in ("test", "run", "build"):
            self.raise_risk(RiskMedium, f"{name} {sub} 可能执行项目脚本")

    def classify_redirect(self, redir: Redirect) -> None:
        """对应 Go ``classifyRedirect``。"""
        op = redir.op or ""
        if ">" in op or "<>" in op:
            self.raise_risk(RiskHigh, f"包含写入重定向 {op}")

    def classify_pipeline(self) -> None:
        """对应 Go ``classifyPipeline``。"""
        if len(self.pipeline) < 2:
            return
        for i in range(len(self.pipeline) - 1):
            left, right = self.pipeline[i], self.pipeline[i + 1]
            if is_downloader(left) and is_interpreter(right):
                self.raise_risk(
                    RiskCritical,
                    f"网络下载命令 {left} 的输出通过管道交给 {right} 执行",
                )
            elif is_interpreter(right):
                self.raise_risk(RiskHigh, f"管道输出交给解释器或 shell {right} 执行")


def classify_shell_command(cmd_text: str) -> RiskAssessment:
    """对应 Go ``classifyShellCommand``。"""
    cmd_text = (cmd_text or "").strip()
    if cmd_text == "":
        return RiskAssessment(level=RiskHigh, reasons=["命令为空，无法判断风险"])
    if is_power_shell_env():
        return RiskAssessment(
            level=RiskHigh,
            reasons=["PowerShell 环境无法用 bash AST 解析命令，需要用户确认"],
        )
    return classify_bash_shell_command(cmd_text)


def classify_bash_shell_command(cmd_text: str) -> RiskAssessment:
    """对应 Go ``classifyBashShellCommand``。"""
    cmd_text = (cmd_text or "").strip()
    if cmd_text == "":
        return RiskAssessment(level=RiskHigh, reasons=["命令为空，无法判断风险"])
    try:
        script = parse(cmd_text)
    except ShellParseError as exc:
        return RiskAssessment(
            level=RiskHigh, reasons=[f"shell AST 解析失败，按高风险处理: {exc}"]
        )
    classifier = ShellRiskClassifier()
    for node in iter_nodes(script):
        if isinstance(node, SimpleCommand):
            classifier.classify_call(node)
        elif isinstance(node, Redirect):
            classifier.classify_redirect(node)
        elif isinstance(node, CommandSubstitution):
            classifier.raise_risk(
                RiskHigh, "包含命令替换 $() 或反引号，执行内容更难静态判断"
            )
    classifier.classify_pipeline()
    if not classifier.reasons:
        classifier.reasons.append("未发现明显写入、删除、提权或下载即执行行为")
    return RiskAssessment(level=classifier.level, reasons=classifier.reasons)


__all__ = [name for name in dir() if not name.startswith("_")]
