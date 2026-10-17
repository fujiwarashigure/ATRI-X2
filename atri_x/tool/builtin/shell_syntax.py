"""Shell 命令的字面词解析与参数判定助手。

对应 Go：``internal/tool/builtin/shell_risk.go`` 的助手部分（``literalWord`` /
``literalArgs`` / ``commandBase`` / ``hasArg`` / ``hasRecursive`` /
``hasRecursiveForce`` / ``touchesSystemPath`` / ``isDownloader`` /
``isInterpreter``）与 ``shell.go`` 的 ``warnUseWorkspace``。

拆分原因：这些助手被 ``shell_risk`` / ``shell_sandbox`` / ``shell_warnings``
三者共享，直接放在 ``shell_risk.py`` 会造成 import 环，故按职责单列一个模块。
词级拆解复用 :mod:`atri_x.shellparse`（mvdan/sh 的 clean-room 替代物）。
"""

from __future__ import annotations

from atri_x.shellparse import Word, literal_args

# Go 定义在 shell.go；这里集中声明以便三个 shell_* 模块共同引用。
WARN_USE_WORKSPACE = (
    "需要切换工作目录时请使用 workspace 工具，不要在 cmd 中切换目录或夹带目录切换。"
)


def literal_word(word: Word | None) -> tuple[str, bool]:
    """对应 Go ``literalWord``：整词可静态求值时才返回 ``(值, True)``。"""
    if word is None:
        return "", False
    value = word.literal()
    if value is None:
        return "", False
    return value, True


def literal_words(words: list[Word] | tuple[Word, ...]) -> list[str]:
    """对应 Go ``literalArgs``：只保留可静态求值的词。"""
    return literal_args(list(words))


def command_base(name: str) -> str:
    """对应 Go ``commandBase``（与 :func:`atri_x.shellparse.command_base` 等价）。"""
    from atri_x.shellparse import command_base as _command_base

    return _command_base(name)


def has_arg(args: list[str], target: str) -> bool:
    """对应 Go ``hasArg``：相等或包含都算命中。"""
    return any(arg == target or target in arg for arg in args)


def has_recursive(args: list[str]) -> bool:
    """对应 Go ``hasRecursive``。"""
    for arg in args:
        if arg in ("-r", "-R", "--recursive"):
            return True
        if "r" in arg and arg.startswith("-"):
            return True
    return False


def has_recursive_force(args: list[str]) -> bool:
    """对应 Go ``hasRecursiveForce``：同时含递归与强制开关。"""
    recursive = False
    force = False
    for arg in args:
        if not arg.startswith("-"):
            continue
        recursive = recursive or "r" in arg or "R" in arg or arg == "--recursive"
        force = force or "f" in arg or arg == "--force"
    return recursive and force


_SYSTEM_PATHS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
        "c:/",
        "c:/windows",
        "c:/program files",
        "c:/users",
    }
)


def touches_system_path(args: list[str]) -> bool:
    """对应 Go ``touchesSystemPath``。"""
    for arg in args:
        path = arg.replace("\\", "/").strip("'\"").lower()
        if path in _SYSTEM_PATHS:
            return True
        if (
            path.startswith("/etc/")
            or path.startswith("/usr/")
            or path.startswith("c:/windows/")
        ):
            return True
    return False


def is_downloader(name: str) -> bool:
    """对应 Go ``isDownloader``。"""
    return name in ("curl", "wget")


_INTERPRETERS = frozenset(
    {
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
    }
)


def is_interpreter(name: str) -> bool:
    """对应 Go ``isInterpreter``。"""
    return name in _INTERPRETERS


__all__ = [name for name in dir() if not name.startswith("_")]
