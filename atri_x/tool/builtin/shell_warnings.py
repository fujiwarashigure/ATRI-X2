"""Shell 命令的执行建议：把「该用专用工具」的纠偏提示回喂给模型。

对应 Go：``internal/tool/builtin/shell_warnings.go``。

除了 warning，这里还会在命中 FileGuard 写规则时返回 ``block_err``：
``shell`` 工具的 ``preflight_confirmation`` 与 ``call`` 都会据此直接拒绝。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from atri_x.shellparse import Redirect, ShellParseError, SimpleCommand, iter_nodes, parse
from atri_x.tool.builtin.file_guard import (
    WARN_USE_EDIT_FILE,
    WARN_USE_READ_FILE,
    FileGuard,
)
from atri_x.tool.builtin.shell_env import is_power_shell_env
from atri_x.tool.builtin.shell_syntax import (
    WARN_USE_WORKSPACE,
    command_base,
    has_arg,
    literal_word,
    literal_words,
)


@dataclass
class ShellAdvice:
    """对应 Go ``shellAdvice``。"""

    warnings: list[str] = field(default_factory=list)
    block_err: BaseException | None = None

    def add_warning(self, warning: str) -> None:
        """对应 Go ``shellAdvice.addWarning``：去空白、去重、丢空。"""
        warning = (warning or "").strip()
        if warning == "" or warning in self.warnings:
            return
        self.warnings.append(warning)

    def inspect_shell_call(
        self, call: SimpleCommand, work_dir: str, file_guard: FileGuard | None
    ) -> None:
        """对应 Go ``shellAdvice.inspectShellCall``。"""
        if not call.words:
            return
        name, ok = literal_word(call.words[0])
        if not ok:
            return
        name = command_base(name)
        args = literal_words(call.args())
        if name in ("cat", "less", "more", "head", "tail", "grep", "rg"):
            self.add_warning(WARN_USE_READ_FILE)
            self.add_path_arg_warning(work_dir, args)
            self.add_read_warnings(file_guard, work_dir, args)
        elif name == "sed":
            if has_arg(args, "-i"):
                if self.check_write_args(file_guard, work_dir, args):
                    return
                self.add_warning(WARN_USE_EDIT_FILE)
                self.add_path_arg_warning(work_dir, args)
        elif name == "perl":
            if has_arg(args, "-pi") or has_arg(args, "-pI"):
                if self.check_write_args(file_guard, work_dir, args):
                    return
                self.add_warning(WARN_USE_EDIT_FILE)
                self.add_path_arg_warning(work_dir, args)
        elif name == "tee":
            if self.check_write_args(file_guard, work_dir, args):
                return
            self.add_warning(WARN_USE_EDIT_FILE)
            self.add_path_arg_warning(work_dir, args)

    def inspect_shell_redirect(
        self, redir: Redirect, work_dir: str, file_guard: FileGuard | None
    ) -> None:
        """对应 Go ``shellAdvice.inspectShellRedirect``。"""
        op = redir.op or ""
        if ">" not in op and "<>" not in op:
            return
        path, ok = literal_word(redir.word)
        if ok:
            resolved, hit = resolve_shell_literal_path(work_dir, path)
            if hit:
                self.add_warning(WARN_USE_WORKSPACE)
                if file_guard is not None:
                    try:
                        file_guard.check_write(resolved)
                    except BaseException as exc:  # noqa: BLE001 - 原样上抛给调用方
                        self.block_err = exc
                        return
        self.add_warning(WARN_USE_EDIT_FILE)

    def add_path_arg_warning(self, work_dir: str, args: list[str]) -> None:
        """对应 Go ``addPathArgWarning``。"""
        for arg in args:
            _, hit = resolve_shell_literal_path(work_dir, arg)
            if hit:
                self.add_warning(WARN_USE_WORKSPACE)
                return

    def add_read_warnings(
        self, file_guard: FileGuard | None, work_dir: str, args: list[str]
    ) -> None:
        """对应 Go ``addReadWarnings``。"""
        if file_guard is None:
            return
        for arg in args:
            path, ok = resolve_shell_literal_path(work_dir, arg)
            if not ok:
                continue
            for warning in file_guard.read_warnings(path):
                self.add_warning(warning)

    def check_write_args(
        self, file_guard: FileGuard | None, work_dir: str, args: list[str]
    ) -> bool:
        """对应 Go ``checkWriteArgs``：命中写规则则记录 block_err 并返回 True。"""
        if file_guard is None:
            return False
        for arg in args:
            path, ok = resolve_shell_literal_path(work_dir, arg)
            if not ok:
                continue
            try:
                file_guard.check_write(path)
            except BaseException as exc:  # noqa: BLE001 - 原样上抛给调用方
                self.block_err = exc
                return True
        return False


def analyze_shell_advice(
    cmd_text: str, work_dir: str, file_guard: FileGuard | None
) -> ShellAdvice:
    """对应 Go ``analyzeShellAdvice``。"""
    if is_power_shell_env():
        return ShellAdvice()
    return analyze_bash_shell_advice(cmd_text, work_dir, file_guard)


def analyze_bash_shell_advice(
    cmd_text: str, work_dir: str, file_guard: FileGuard | None
) -> ShellAdvice:
    """对应 Go ``analyzeBashShellAdvice``。"""
    try:
        script = parse(cmd_text)
    except ShellParseError:
        return ShellAdvice()
    advice = ShellAdvice()
    for node in iter_nodes(script):
        if advice.block_err is not None:
            break
        if isinstance(node, SimpleCommand):
            advice.inspect_shell_call(node, work_dir, file_guard)
        elif isinstance(node, Redirect):
            advice.inspect_shell_redirect(node, work_dir, file_guard)
    return advice


def resolve_shell_literal_path(work_dir: str, raw: str) -> tuple[str, bool]:
    """对应 Go ``resolveShellLiteralPath``。"""
    raw = (raw or "").strip()
    if raw == "" or raw.startswith("-"):
        return "", False
    if "://" in raw:
        return "", False
    expanded = msys_path_to_windows(raw)
    if expanded is not None:
        raw = expanded
    if os.path.isabs(raw):
        return os.path.normpath(raw), True
    if (work_dir or "").strip() == "":
        return "", False
    return os.path.normpath(os.path.join(work_dir, raw)), True


def msys_path_to_windows(path: str) -> str | None:
    """对应 Go ``msysPathToWindows``：``/c/foo`` → ``c:/foo``。"""
    if len(path) < 3 or path[0] != "/" or path[2] != "/":
        return None
    drive = path[1]
    if not (("a" <= drive <= "z") or ("A" <= drive <= "Z")):
        return None
    return drive + ":/" + path[3:]


__all__ = [name for name in dir() if not name.startswith("_")]
