"""Shell 命令的静态风险分级。

对应 Go：``internal/tool/builtin/shell_risk.go``。

分级口径以 Go 为基线，并在同一层做了三处加固（研究报告 §6 的 P0）：

* **包装器解包**：``env`` / ``command`` / ``nohup`` / ``timeout`` / ``nice`` /
  ``setsid`` / ``stdbuf`` / ``ionice`` / ``chroot`` / ``ssh`` / ``watch`` /
  ``busybox`` / ``xargs`` / ``time`` / ``exec`` / ``coproc`` 不再遮住内层命令；
* **内层脚本文本递归解析**：``sh|bash|zsh -c '…'``、``eval '…'``、
  ``trap '…' EXIT``、``find -exec … ;``、``awk 'system("…")'`` 里的脚本文本交给
  同一个解析器，解析失败按 ``high``（fail-closed）；
* ``git`` 的全局选项（``-C`` / ``--git-dir=`` …）不再把子命令挤出视野，
  并补上 ``truncate`` / ``shred`` / ``ln`` / ``find -delete`` 等破坏性命令。

分级口径：``rm -rf`` / 提权 / 磁盘操作 / 下载即执行 = ``critical``，
写入、删除、解释器调用、管道进解释器 = ``high``，下载、调用解释器、构建 =
``medium``，其余 ``low``。PowerShell 环境无法用 bash AST 解析，一律 ``high``
（后台无人值守时由后台策略直接拒绝，见 :mod:`shell_sandbox`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
#: ``git`` 里"后面还跟一个值"的全局选项；不解包它们会把子命令挤到视野外
#: （``git -C / clean -fdx`` 曾被判 low）。
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--namespace", "--config-env", "--super-prefix"}
)
#: 只能解一层包装器：深度上限同时挡住 ``env env env …`` 这类递归构造。
_MAX_UNWRAP_DEPTH = 8
#: 真正会把字符串当脚本执行的 shell；``python -c`` 等不做文本递归（保持 Go 口径）。
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
#: 系统管理类命令（Windows/Linux）：无人值守场景下按 high 处理。
_ADMIN_COMMANDS = frozenset(
    {"reg", "schtasks", "sc", "net", "wmic", "bcdedit", "vssadmin", "cipher", "taskkill", "netsh"}
)


@dataclass(frozen=True)
class _Wrapper:
    """一个包装器的解包规则。

    ``mode``：``command`` = 余下词就是内层命令；``applet`` = 第一个词是 applet；
    ``script`` = 余下词拼成脚本文本，走二次解析。
    """

    flags: frozenset[str] = frozenset()
    value_flags: frozenset[str] = frozenset()
    positional_skip: int = 0
    skip_assignments: bool = False
    mode: str = "command"


_WRAPPERS: dict[str, _Wrapper] = {
    "env": _Wrapper(
        flags=frozenset({"-i", "-0", "--null", "--ignore-environment"}),
        value_flags=frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
        skip_assignments=True,
    ),
    "command": _Wrapper(flags=frozenset({"-p", "-v", "-V"})),
    "nohup": _Wrapper(),
    "timeout": _Wrapper(
        flags=frozenset({"--foreground", "--preserve-status", "-v", "--verbose"}),
        value_flags=frozenset({"-k", "--kill-after", "-s", "--signal"}),
        positional_skip=1,
    ),
    "nice": _Wrapper(value_flags=frozenset({"-n", "--adjustment"})),
    "setsid": _Wrapper(flags=frozenset({"-c", "--ctty", "-f", "--fork", "-w", "--wait"})),
    "stdbuf": _Wrapper(
        value_flags=frozenset({"-i", "-o", "-e", "--input", "--output", "--error"})
    ),
    "ionice": _Wrapper(
        flags=frozenset({"-t", "--ignore"}),
        value_flags=frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    ),
    "chroot": _Wrapper(value_flags=frozenset({"--userspec", "--groups"}), positional_skip=1),
    "ssh": _Wrapper(
        flags=frozenset(
            {
                "-4", "-6", "-A", "-a", "-C", "-f", "-G", "-g", "-K", "-k", "-M", "-N",
                "-n", "-q", "-s", "-T", "-t", "-V", "-v", "-X", "-x", "-Y", "-y",
            }
        ),
        value_flags=frozenset(
            {
                "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
                "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
            }
        ),
        positional_skip=1,
        mode="script",
    ),
    "watch": _Wrapper(
        flags=frozenset(
            {"-d", "--differences", "-g", "--chgexit", "-t", "--no-title", "-b", "--beep", "-e", "--errexit", "-x", "--exec"}
        ),
        value_flags=frozenset({"-n", "--interval", "-p", "--precise"}),
    ),
    "busybox": _Wrapper(mode="applet"),
    "xargs": _Wrapper(
        flags=frozenset(
            {"-0", "--null", "-o", "--open-tty", "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit", "-p", "--interactive"}
        ),
        value_flags=frozenset(
            {"-a", "--arg-file", "-d", "--delimiter", "-E", "--eof", "-I", "-i", "--replace", "-L", "-l", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars", "--process-slot-var"}
        ),
    ),
    "time": _Wrapper(
        flags=frozenset({"-p", "--portability", "-v", "--verbose", "-a", "--append"}),
        value_flags=frozenset({"-o", "--output", "-f", "--format"}),
    ),
    "builtin": _Wrapper(),
    "exec": _Wrapper(flags=frozenset({"-c", "-l"}), value_flags=frozenset({"-a"})),
    "coproc": _Wrapper(),
}

#: ``awk 'BEGIN{system("rm -rf /")}'`` 里的外部命令文本。
_AWK_SYSTEM = re.compile(r"system\s*\(\s*['\"]([^'\"]*)['\"]")


def _is_assignment(word: str) -> bool:
    """``FOO=bar`` 形式的临时环境变量赋值。"""
    name, sep, _value = word.partition("=")
    return bool(sep) and bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _skip_options(args: list[str], rule: _Wrapper) -> int:
    """跳过包装器的开关与赋值，返回第一个"内层命令"位置。"""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return index + 1
        if arg.startswith("-") and arg != "-":
            base = arg.split("=", 1)[0]
            if base in rule.value_flags:
                index += 1 if "=" in arg else 2
                continue
            # 未知开关（含 -n1 / -rf 这类粘连写法）按无值开关处理
            index += 1
            continue
        if rule.skip_assignments and _is_assignment(arg):
            index += 1
            continue
        break
    return index


def _unwrap(name: str, args: list[str]) -> tuple[str, list[str]] | None:
    """把包装器拆成内层命令；没有内层命令（如单独的 ``env``）时返回 ``None``。"""
    rule = _WRAPPERS[name]
    if name == "command" and any(arg in ("-v", "-V") for arg in args):
        return None  # command -v 只查询路径，不执行
    index = _skip_options(args, rule)
    index = min(len(args), index + rule.positional_skip)
    rest = args[index:]
    if name == "busybox":
        return ("applet", rest) if rest else None
    if not rest:
        return None
    if rule.mode == "script":
        return "script", [" ".join(rest)]
    return "command", rest


def _script_arg(name: str, args: list[str]) -> str:
    """取 ``sh -c '<脚本>'`` / ``pwsh -Command '<脚本>'`` 的脚本文本。"""
    if name in ("powershell", "pwsh"):
        keys = ("-c", "-Command", "-command", "--command")
    elif name in _SHELL_INTERPRETERS:
        keys = ("-c",)
    else:
        return ""
    for index, arg in enumerate(args):
        if arg in keys:
            return args[index + 1] if index + 1 < len(args) else ""
        if (
            name in _SHELL_INTERPRETERS
            and arg.startswith("-")
            and not arg.startswith("--")
            and "c" in arg[1:]
        ):
            return args[index + 1] if index + 1 < len(args) else ""
    return ""


@dataclass
class ShellRiskClassifier:
    """对应 Go ``shellRiskClassifier``。"""

    level: str = RiskLow
    reasons: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    pipeline: list[str] = field(default_factory=list)
    parse_failed: bool = False
    #: 出现"命令名/路径无法静态求值"的动态结构：用于后台白名单的 fail-closed 判定。
    dynamic: bool = False

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
            self.dynamic = True
            self.raise_risk(
                RiskHigh, "命令名包含变量、命令替换或其他动态结构，无法静态判断真实命令"
            )
            return
        self.classify_command(command_base(name), literal_words(call.args()), 0)

    # -- 解包与递归 --------------------------------------------------------

    def classify_command(self, name: str, args: list[str], depth: int = 0) -> None:
        """判级一个具体命令；包装器会被解开后递归到内层命令。"""
        name = command_base(name)
        self.commands.append(name)
        if name in _WRAPPERS and depth < _MAX_UNWRAP_DEPTH:
            unwrapped = _unwrap(name, args)
            if unwrapped is not None:
                mode, payload = unwrapped
                if mode == "script":
                    self.raise_risk(RiskHigh, f"{name} 后面是需要二次解析的脚本文本")
                    self.classify_text(payload[0], depth + 1)
                    return
                self.classify_command(payload[0], payload[1:], depth + 1)
                return
        if name in _WRAPPERS:
            self.pipeline.append(name)
            if depth >= _MAX_UNWRAP_DEPTH:
                self.raise_risk(RiskHigh, f"包装器 {name} 嵌套过深，无法静态判断真实命令")
                return
            # 包装器后面没有内层命令（单独的 env / xargs / timeout…）：按本身判级
            self.classify_plain(name, args, depth)
            return
        self.pipeline.append(name)
        self.classify_plain(name, args, depth)

    def classify_text(self, text: str, depth: int = 0) -> None:
        """解析并判级一段脚本文本（``-c`` / ``eval`` / ``trap`` / ``-exec`` 内层）。"""
        text = (text or "").strip()
        if text == "":
            return
        if depth > _MAX_UNWRAP_DEPTH:
            self.raise_risk(RiskHigh, "内层脚本文本嵌套过深，无法静态判断")
            return
        try:
            script = parse(text)
        except ShellParseError as exc:
            self.raise_risk(RiskHigh, f"内层脚本文本解析失败，按高风险处理: {exc}")
            return
        _classify_script(self, script, depth)

    # -- 具体规则 ----------------------------------------------------------

    def classify_plain(self, name: str, args: list[str], depth: int = 0) -> None:
        """对应 Go ``classifyCall`` 的命令分支（已解包到真实命令）。"""
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
        elif name in ("shutdown", "reboot", "halt", "poweroff"):
            self.raise_risk(RiskCritical, f"使用关机/重启命令 {name}")
        elif name in ("truncate", "shred", "wipe"):
            if touches_system_path(args):
                self.raise_risk(RiskCritical, f"{name} 会破坏系统路径下的文件内容")
            else:
                self.raise_risk(RiskHigh, f"{name} 会破坏或擦除文件内容")
        elif name in ("chmod", "chown", "icacls", "takeown"):
            if has_recursive(args) or touches_system_path(args):
                self.raise_risk(RiskCritical, f"递归或系统路径权限修改命令 {name}")
            else:
                self.raise_risk(RiskHigh, f"权限或所有者修改命令 {name}")
        elif name in ("curl", "wget"):
            self.raise_risk(RiskMedium, f"使用网络下载命令 {name}")
        elif name == "git":
            self.classify_git(args)
        elif name in _SHELL_INTERPRETERS or is_interpreter(name):
            self.raise_risk(RiskMedium, f"调用解释器或 shell {name}")
            script = _script_arg(name, args)
            if script:
                self.classify_text(script, depth + 1)
        elif name == "sed":
            if has_arg(args, "-i"):
                self.raise_risk(RiskHigh, "sed -i 会原地修改文件")
        elif name in ("mv", "move", "cp", "copy", "install", "tee"):
            self.raise_risk(RiskHigh, f"可能写入或覆盖文件的命令 {name}")
        elif name == "ln":
            if touches_system_path(args):
                self.raise_risk(RiskHigh, "创建指向系统路径的链接")
            else:
                self.raise_risk(RiskMedium, "使用链接命令 ln")
        elif name in ("find", "gfind"):
            self.classify_find(args, depth)
        elif name == "trap":
            if args and args[0] not in ("-", "-l", "-p", "--"):
                self.raise_risk(RiskHigh, "trap 注册的钩子里带脚本文本，会在信号/退出时执行")
                self.classify_text(args[0], depth + 1)
        elif name == "eval":
            script = " ".join(args).strip()
            self.raise_risk(RiskHigh, "eval 会执行动态拼接的脚本文本")
            self.classify_text(script, depth + 1)
        elif name in ("awk", "gawk", "mawk"):
            self.raise_risk(RiskMedium, f"调用文本处理解释器 {name}")
            self.classify_awk(args, depth)
        elif name in ("npm", "pnpm", "yarn", "pip", "pip3", "go", "cargo"):
            self.classify_package_or_build(name, args)
        elif name in _ADMIN_COMMANDS:
            self.raise_risk(RiskHigh, f"使用系统管理命令 {name}")
        elif name in ("nc", "ncat", "netcat", "socat", "telnet"):
            self.raise_risk(RiskMedium, f"使用网络连接命令 {name}")
        elif name == "ssh":
            self.raise_risk(RiskMedium, "使用远程登录/执行命令 ssh")

    def classify_find(self, args: list[str], depth: int = 0) -> None:
        """``find`` 的执行/删除/写文件分支（Go 只按路径参数判级，会漏掉这些）。"""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "-delete":
                self.raise_risk(RiskCritical, "find -delete 会删除匹配到的文件")
                index += 1
                continue
            if arg in ("-exec", "-execdir", "-ok", "-okdir"):
                index += 1
                inner: list[str] = []
                while index < len(args) and args[index] not in (";", "+", "\\;"):
                    inner.append(args[index])
                    index += 1
                index += 1  # 跳过终止符
                if inner:
                    self.classify_command(inner[0], inner[1:], depth + 1)
                continue
            if arg.startswith("-fprint") or arg in ("-fls", "-fprintf"):
                self.raise_risk(RiskHigh, "find 会把结果写入文件")
            index += 1

    def classify_awk(self, args: list[str], depth: int = 0) -> None:
        """``awk`` 里的 ``system()`` / 重定向会把命令交给 shell 执行或写文件。"""
        program = " ".join(args)
        if "system" in program:
            self.raise_risk(RiskHigh, "awk 调用了 system()，会执行外部命令")
            for script in _AWK_SYSTEM.findall(program):
                self.classify_text(script, depth + 1)
        if ">" in program:
            self.raise_risk(RiskHigh, "awk 程序里有输出重定向，可能写入文件")

    def classify_git(self, args: list[str]) -> None:
        """对应 Go ``classifyGit``：先剥掉全局选项，再看子命令。"""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--":
                index += 1
                break
            if arg in _GIT_GLOBAL_VALUE_FLAGS:
                index += 2
                continue
            if any(arg.startswith(flag + "=") for flag in _GIT_GLOBAL_VALUE_FLAGS):
                index += 1
                continue
            if arg.startswith("-"):
                index += 1
                continue
            break
        rest = args[index:]
        if not rest:
            return
        sub = rest[0]
        rest = rest[1:]
        if sub in _GIT_READ_ONLY:
            return
        if sub in _GIT_MUTATING:
            dropped = (sub == "reset" and has_arg(rest, "--hard")) or sub == "clean"
            if dropped:
                self.raise_risk(RiskHigh, f"git {sub} 可能丢弃或删除工作区内容")
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


def _classify_script(classifier: ShellRiskClassifier, script: Any, depth: int) -> None:
    """遍历一棵已解析的脚本树；内层文本递归时复用同一套规则。"""
    for node in iter_nodes(script):
        if isinstance(node, SimpleCommand):
            if not node.words:
                continue
            name, ok = literal_word(node.words[0])
            if not ok or name == "":
                classifier.dynamic = True
                classifier.raise_risk(
                    RiskHigh, "命令名包含变量、命令替换或其他动态结构，无法静态判断真实命令"
                )
                continue
            classifier.classify_command(command_base(name), literal_words(node.args()), depth)
        elif isinstance(node, Redirect):
            classifier.classify_redirect(node)
        elif isinstance(node, CommandSubstitution):
            classifier.raise_risk(
                RiskHigh, "包含命令替换 $() 或反引号，执行内容更难静态判断"
            )


def unwrap_wrapper(name: str, args: list[str]) -> tuple[str, list[str]] | None:
    """公开的包装器解包（``shell_sandbox`` 复用同一套规则，避免两处各写一份）。"""
    if name not in _WRAPPERS:
        return None
    return _unwrap(name, args)


def shell_script_arg(name: str, args: list[str]) -> str:
    """公开的 ``-c`` / ``-Command`` 脚本文本提取（``shell_sandbox`` 复用）。"""
    return _script_arg(name, args)


def shell_command_names(cmd_text: str) -> tuple[list[str], bool]:
    """脚本里的真实命令名（已解包包装器）与"是否全部可静态确认"。

    后台白名单用它做 fail-closed 判定：解析失败或出现动态命令名时第二个返回值为
    ``False``，调用方必须拒绝而不是放行。
    """
    try:
        script = parse(cmd_text)
    except ShellParseError:
        return [], False
    classifier = ShellRiskClassifier()
    _classify_script(classifier, script, 0)
    names = list(dict.fromkeys(classifier.pipeline))
    return names, not classifier.dynamic


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
    _classify_script(classifier, script, 0)
    classifier.classify_pipeline()
    if not classifier.reasons:
        classifier.reasons.append("未发现明显写入、删除、提权或下载即执行行为")
    return RiskAssessment(level=classifier.level, reasons=classifier.reasons)


__all__ = [name for name in dir() if not name.startswith("_")]
