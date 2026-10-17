"""``internal/launcher`` 的 Python 移植：进程启动器的命令行层。

对外只暴露 :mod:`atri_x.launcher.cli` 的公开名（:func:`parse_args` /
:class:`Options` / :func:`write_usage` / :func:`write_completion` 等）。
``atri_x/__main__.py``（对应 Go ``cmd/atri-x``）从这里取参数与用法文本。

上游只有一个 ``cli.go``，Python 侧同样只有一个 ``cli.py``；本 ``__init__``
只做再导出，不另立第二份定义。
"""

from __future__ import annotations

from atri_x.launcher.cli import (  # noqa: F401
    BASH_COMPLETION,
    COMMAND_COMPLETION,
    COMMAND_CONFIG_CHECK,
    COMMAND_DOCTOR,
    COMMAND_RUN,
    DEFAULT_HEALTH_PROBE_ADDR,
    FISH_COMPLETION,
    HEALTH_ADDR_ENV,
    MODE_AUTO,
    MODE_CLI_ONLY,
    MODE_FULL,
    MODE_SERVICE,
    NUSHELL_COMPLETION,
    POWERSHELL_COMPLETION,
    USAGE_TEXT,
    ZSH_COMPLETION,
    Command,
    CommandCompletion,
    CommandConfigCheck,
    CommandDoctor,
    CommandRun,
    DetectCompletionShell,
    Options,
    ParseArgs,
    SupportedCompletionShell,
    WriteBashCompletion,
    WriteCompletion,
    WriteFishCompletion,
    WriteNushellCompletion,
    WritePowerShellCompletion,
    WriteUsage,
    WriteZshCompletion,
    detect_completion_shell,
    health_probe_address,
    health_probe_default_address,
    parse_args,
    supported_completion_shell,
    write_bash_completion,
    write_completion,
    write_fish_completion,
    write_nushell_completion,
    write_powershell_completion,
    write_usage,
    write_zsh_completion,
)
from atri_x.launcher.cli import __all__ as _CLI_ALL

__all__ = list(_CLI_ALL)
