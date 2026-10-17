"""进程启动器的命令行解析、用法文本与 shell 补全脚本。

对应 Go：``internal/launcher/cli.go``。

职责（逐条对齐 Go）：

* :func:`parse_args` —— 把 ``argv`` 解析成 :class:`Options`；未知选项/命令抛
  :class:`atri_x.errors.InvalidError`（Go 侧返回 ``fmt.Errorf``）。
* :func:`write_usage` —— 打印用法（**纯 ASCII**，Windows GBK 控制台也安全）。
* :func:`write_completion` —— 生成 bash/zsh/fish/nushell/powershell 补全脚本。

与 Go 的三处刻意差异（都是形式差别，不是行为差别）：

1. Go 的 ``ParseArgs`` 直接引用 ``app.RunMode``；本模块**不**在顶层 import
   :mod:`atri_x.app`（那个包的 ``__init__`` 很大，且并行移植期会牵进尚未交付的
   兄弟模块）。运行模式按字面值复刻成 ``MODE_*``，唯一出处仍是
   :mod:`atri_x.app.app` 的 ``RUN_MODE_*``；``RunMode`` 本质就是 ``str``，
   比较/相等语义不受影响。
2. Go 把输出写进 ``io.Writer``；Python 用任何有 ``write`` 的文本流，默认 ``sys.stdout``。
3. **Python 版新增** ``--health-probe``（Go 上游没有）：容器 HEALTHCHECK 用它跑一次
   ``/live`` 探针并按结果给退出码。待 Lead 确认，见模块末尾「Python 版新增」一节。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Sequence, TextIO

from atri_x.errors import InvalidError

__all__ = [
    "Command",
    "COMMAND_RUN",
    "COMMAND_COMPLETION",
    "COMMAND_CONFIG_CHECK",
    "COMMAND_DOCTOR",
    "CommandRun",
    "CommandCompletion",
    "CommandConfigCheck",
    "CommandDoctor",
    "MODE_AUTO",
    "MODE_FULL",
    "MODE_CLI_ONLY",
    "MODE_SERVICE",
    "Options",
    "ParseArgs",
    "parse_args",
    "SupportedCompletionShell",
    "supported_completion_shell",
    "WriteUsage",
    "write_usage",
    "WriteCompletion",
    "write_completion",
    "DetectCompletionShell",
    "detect_completion_shell",
    "WriteBashCompletion",
    "write_bash_completion",
    "WriteZshCompletion",
    "write_zsh_completion",
    "WriteFishCompletion",
    "write_fish_completion",
    "WriteNushellCompletion",
    "write_nushell_completion",
    "WritePowerShellCompletion",
    "write_powershell_completion",
    "USAGE_TEXT",
    "BASH_COMPLETION",
    "ZSH_COMPLETION",
    "FISH_COMPLETION",
    "NUSHELL_COMPLETION",
    "POWERSHELL_COMPLETION",
    "DEFAULT_HEALTH_PROBE_ADDR",
    "HEALTH_ADDR_ENV",
    "health_probe_address",
    "health_probe_default_address",
]


class Command(str):
    """Go ``Command``：``str`` 子类，便于直接当字符串比较/打印。"""

    __slots__ = ()

    def __new__(cls, value: str = "") -> "Command":
        return super().__new__(cls, value)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return f"Command({str(self)!r})"


#: 与 Go 常量逐字对应。
COMMAND_RUN = Command("run")
COMMAND_COMPLETION = Command("completion")
COMMAND_CONFIG_CHECK = Command("config-check")
COMMAND_DOCTOR = Command("doctor")

#: Go 原名（``CommandRun`` 等），与上面的 snake_case 常量指向同一对象。
CommandRun = COMMAND_RUN
CommandCompletion = COMMAND_COMPLETION
CommandConfigCheck = COMMAND_CONFIG_CHECK
CommandDoctor = COMMAND_DOCTOR

#: 运行模式常量。唯一出处是 :mod:`atri_x.app.app` 的 ``RUN_MODE_*``；这里按字面值
#: 复刻以避免 launcher 依赖整个 ``atri_x.app`` 包（见模块 docstring 第 1 条）。
MODE_AUTO = "auto"
MODE_FULL = "full"
MODE_CLI_ONLY = "cli"
MODE_SERVICE = "service"

#: ``--health-probe`` 的默认探测地址（与 ``deploy/Dockerfile`` 的
#: ``ATRIX_HEALTH_ADDR=0.0.0.0:32171`` 对应：0.0.0.0 在探测侧归一为回环）。
DEFAULT_HEALTH_PROBE_ADDR = "127.0.0.1:32171"

#: 健康探针的管理环境变量名；与 :data:`atri_x.app.health.HEALTH_ADDR_ENV` 同名。
HEALTH_ADDR_ENV = "ATRIX_HEALTH_ADDR"


@dataclass
class Options:
    """对应 Go ``Options``。

    ``health_probe`` / ``health_probe_url`` 是 **Python 版新增**字段（见模块
    docstring）；Go 的 ``ParseArgs`` 见到 ``--health-probe`` 会报未知选项。
    """

    config_path: str = ""
    client_name: str = ""
    mode: str = MODE_AUTO
    command: Command = COMMAND_RUN
    completion: str = ""
    doctor_e2e: bool = False
    doctor_json: bool = False
    doctor_no_model: bool = False
    doctor_require_platform: bool = False
    help: bool = False
    version: bool = False
    #: Python 版新增：``--health-probe[=URL|host:port]``。
    health_probe: bool = False
    health_probe_url: str = ""


def parse_args(args: Sequence[str]) -> Options:
    """解析命令行。对应 Go ``ParseArgs``。

    语法刻意与 Go 逐字一致，包括：

    * ``--`` 只是被**跳过**，不终止选项解析（Go 侧就是 ``continue``）；
    * ``-config``（单横线）与 ``--config`` 等价，但 ``-config=x`` **不**等价
      （Go 只对 ``--config=`` 做前缀匹配）；
    * ``--help`` / ``--version`` 一旦出现就提前返回，忽略位置参数里的命令。
    """
    opts = Options(mode=MODE_AUTO, command=COMMAND_RUN)
    positionals: list[str] = []
    index = 0
    total = len(args)
    while index < total:
        arg = args[index]
        if arg == "--":
            index += 1
            continue
        if arg == "--help" or arg == "-h":
            opts.help = True
        elif arg == "--version":
            opts.version = True
        elif arg == "--client" or arg == "-c":
            if index + 1 >= total:
                raise InvalidError(f"{arg} requires a value")
            index += 1
            opts.client_name = args[index]
        elif arg.startswith("--client="):
            opts.client_name = arg[len("--client=") :]
        elif arg == "--config" or arg == "-config":
            if index + 1 >= total:
                raise InvalidError(f"{arg} requires a value")
            index += 1
            opts.config_path = args[index]
        elif arg.startswith("--config="):
            opts.config_path = arg[len("--config=") :]
        elif arg == "--e2e":
            opts.doctor_e2e = True
        elif arg == "--json":
            opts.doctor_json = True
        elif arg == "--no-model":
            opts.doctor_no_model = True
        elif arg == "--require-platform":
            opts.doctor_require_platform = True
        elif arg == "--health-probe":
            opts.health_probe = True
        elif arg.startswith("--health-probe="):
            # Python 版新增：值可以是完整 URL，也可以是 host:port。
            opts.health_probe = True
            opts.health_probe_url = arg[len("--health-probe=") :]
        elif arg.startswith("-"):
            raise InvalidError(f"unknown option: {arg}")
        else:
            positionals.append(arg)
        index += 1

    if opts.help or opts.version:
        return opts
    if not positionals:
        return opts

    head = positionals[0]
    if head == "run":
        if len(positionals) != 1:
            raise InvalidError("usage: atri-x run [--config path]")
        opts.mode = MODE_FULL
    elif head == "cli":
        if len(positionals) != 1:
            raise InvalidError("usage: atri-x cli [--config path]")
        opts.mode = MODE_CLI_ONLY
    elif head == "service":
        if len(positionals) != 2 or positionals[1] != "run":
            raise InvalidError("usage: atri-x service run [--config path]")
        opts.mode = MODE_SERVICE
    elif head == "config":
        if len(positionals) != 2 or positionals[1] != "check":
            raise InvalidError("usage: atri-x config check [--config path]")
        opts.command = COMMAND_CONFIG_CHECK
    elif head == "doctor":
        if len(positionals) != 1:
            raise InvalidError(
                "usage: atri-x doctor [--config path] [--e2e] [--json] [--no-model] "
                "[--require-platform]"
            )
        opts.command = COMMAND_DOCTOR
    elif head == "completion":
        if len(positionals) != 2 or not supported_completion_shell(positionals[1]):
            raise InvalidError(
                "usage: atri-x completion [auto|bash|zsh|fish|nushell|powershell]"
            )
        opts.command = COMMAND_COMPLETION
        opts.completion = positionals[1]
    else:
        raise InvalidError(f"unknown command: {head}")
    return opts


def supported_completion_shell(shell: str) -> bool:
    """对应 Go ``SupportedCompletionShell``。"""
    return shell in (
        "auto",
        "bash",
        "zsh",
        "fish",
        "nu",
        "nushell",
        "powershell",
        "pwsh",
    )


#: 用法文本，逐字复制 Go ``WriteUsage`` 的原始字符串（纯 ASCII）。
USAGE_TEXT = """Usage:
  atri-x [--config path] [-c client]
  atri-x run [--config path]
  atri-x cli [--config path] [-c client]
  atri-x service run [--config path]
  atri-x config check [--config path]
  atri-x doctor [--config path] [--e2e] [--json] [--no-model] [--require-platform]
  atri-x completion [auto|bash|zsh|fish|nushell|powershell]

Commands:
  run           Run full foreground mode: CLI plus enabled platforms and cron.
  cli           Run local CLI-only mode: CLI without background platforms or cron.
  service run   Run headless service mode for Linux service managers.
  config check  Load app.toml, print a summary and warnings, exit non-zero on error.
  doctor        Run deployment acceptance checks. Adds --e2e for a real CLI round-trip;
                --require-platform makes disconnected/missing platforms fail the run.
  completion    Generate shell completion scripts.

Options:
  -c, --client name  CLI client profile name.
  --config path       Path to TOML config file.
  --help         Show this help.
  --version      Show version.
"""


def write_usage(w: TextIO | None = None) -> None:
    """写用法文本。对应 Go ``WriteUsage``。"""
    _write(w, USAGE_TEXT)


def write_completion(w: TextIO | None, shell: str) -> None:
    """写补全脚本；无法识别的 shell 抛 :class:`atri_x.errors.InvalidError`。

    对应 Go ``WriteCompletion``（``(error)`` → 抛异常）。
    """
    if shell == "auto":
        shell = detect_completion_shell()
    writers = {
        "bash": write_bash_completion,
        "zsh": write_zsh_completion,
        "fish": write_fish_completion,
        "nu": write_nushell_completion,
        "nushell": write_nushell_completion,
        "powershell": write_powershell_completion,
        "pwsh": write_powershell_completion,
    }
    writer = writers.get(shell)
    if writer is None:
        raise InvalidError(
            "cannot detect shell; specify bash, zsh, fish, nushell, or powershell"
        )
    writer(w)


def detect_completion_shell() -> str:
    """按 ``SHELL`` / ``PSModulePath`` 猜当前 shell。对应 Go ``detectCompletionShell``。

    判定顺序与 Go 完全一致（``nu`` 在 ``zsh`` / ``bash`` 之前）。
    """
    shell = (os.environ.get("SHELL") or "").lower()
    if "fish" in shell:
        return "fish"
    if "nu" in shell:
        return "nushell"
    if "zsh" in shell:
        return "zsh"
    if "bash" in shell:
        return "bash"
    if "powershell" in (os.environ.get("PSModulePath") or "").lower():
        return "powershell"
    return ""


BASH_COMPLETION = """# bash completion for atri-x
_atrix_completion() {
  local cur prev
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  case "$prev" in
    --config)
      COMPREPLY=( $(compgen -f -- "$cur") )
      return 0
      ;;
    completion)
      COMPREPLY=( $(compgen -W "auto bash zsh fish nushell powershell" -- "$cur") )
      return 0
      ;;
    service)
      COMPREPLY=( $(compgen -W "run" -- "$cur") )
      return 0
      ;;
  esac

  COMPREPLY=( $(compgen -W "run cli service completion --config --help --version" -- "$cur") )
}
complete -F _atrix_completion atri-x
"""

ZSH_COMPLETION = """#compdef atri-x
_arguments -C \\
  '--config[Path to TOML config file]:config file:_files' \\
  '--help[Show help]' \\
  '--version[Show version]' \\
  '1:command:(run cli service completion)' \\
  '2:subcommand:->subcmd'
case $words[2] in
  service)
    _values 'service command' run
    ;;
  completion)
    _values 'shell' auto bash zsh fish nushell powershell
    ;;
esac
"""

FISH_COMPLETION = """complete -c atri-x -f
complete -c atri-x -n "__fish_use_subcommand" -a "run" -d "Run full foreground mode"
complete -c atri-x -n "__fish_use_subcommand" -a "cli" -d "Run local CLI-only mode"
complete -c atri-x -n "__fish_use_subcommand" -a "service" -d "Service commands"
complete -c atri-x -n "__fish_use_subcommand" -a "completion" -d "Generate shell completions"
complete -c atri-x -n "__fish_seen_subcommand_from service" -a "run" -d "Run headless service mode"
complete -c atri-x -n "__fish_seen_subcommand_from completion" -a "auto bash zsh fish nushell powershell"
complete -c atri-x -l config -r -d "Path to TOML config file"
complete -c atri-x -s h -l help -d "Show help"
complete -c atri-x -l version -d "Show version"
"""

NUSHELL_COMPLETION = """def "nu-complete atri-x commands" [] {
  [
    {value: "run", description: "Run full foreground mode"}
    {value: "cli", description: "Run local CLI-only mode"}
    {value: "service", description: "Service commands"}
    {value: "completion", description: "Generate shell completions"}
  ]
}

def "nu-complete atri-x service" [] {
  [{value: "run", description: "Run headless service mode"}]
}

def "nu-complete atri-x completion" [] {
  [auto bash zsh fish nushell powershell]
}

export extern "atri-x" [
  command?: string@"nu-complete atri-x commands"
  subcommand?: string
  --config: path
  --help(-h)
  --version
]

export extern "atri-x service" [
  command?: string@"nu-complete atri-x service"
  --config: path
  --help(-h)
  --version
]

export extern "atri-x completion" [
  shell?: string@"nu-complete atri-x completion"
]
"""

POWERSHELL_COMPLETION = """Register-ArgumentCompleter -Native -CommandName atri-x -ScriptBlock {
  param($wordToComplete, $commandAst, $cursorPosition)
  $words = $commandAst.CommandElements | ForEach-Object { $_.ToString() }
  $candidates = @('run', 'cli', 'service', 'completion', '--config', '--help', '--version')
  if ($words -contains 'service') { $candidates = @('run') }
  if ($words -contains 'completion') { $candidates = @('auto', 'bash', 'zsh', 'fish', 'powershell') }
  $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
  }
}
"""


def write_bash_completion(w: TextIO | None = None) -> None:
    """对应 Go ``writeBashCompletion``。"""
    _write(w, BASH_COMPLETION)


def write_zsh_completion(w: TextIO | None = None) -> None:
    """对应 Go ``writeZshCompletion``。"""
    _write(w, ZSH_COMPLETION)


def write_fish_completion(w: TextIO | None = None) -> None:
    """对应 Go ``writeFishCompletion``。"""
    _write(w, FISH_COMPLETION)


def write_nushell_completion(w: TextIO | None = None) -> None:
    """对应 Go ``writeNushellCompletion``。"""
    _write(w, NUSHELL_COMPLETION)


def write_powershell_completion(w: TextIO | None = None) -> None:
    """对应 Go ``writePowerShellCompletion``。"""
    _write(w, POWERSHELL_COMPLETION)


def _write(w: TextIO | None, text: str) -> None:
    """写文本；``w`` 为 ``None`` 时写 ``sys.stdout``（此时才解析，便于重定向测试）。"""
    stream: TextIO = w if w is not None else sys.stdout
    stream.write(text)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def health_probe_default_address(environ: Any = None) -> str:
    """只看 ``ATRIX_HEALTH_ADDR`` 与默认值，忽略 ``--health-probe=`` 的显式值。

    供"``--health-probe=/ready`` 这种只给路径"的写法当基地址用。
    """
    env = os.environ if environ is None else environ
    return str(env.get(HEALTH_ADDR_ENV) or "").strip() or DEFAULT_HEALTH_PROBE_ADDR


def health_probe_address(opts: Options, environ: Any = None) -> str:
    """解析 ``--health-probe`` 要探测的地址。

    优先级：``--health-probe=<host:port|URL>`` > ``ATRIX_HEALTH_ADDR`` > 默认
    ``127.0.0.1:32171``。**不读配置文件**，所以没有配置也能用（Dockerfile 的
    HEALTHCHECK 就是这种场景）。

    值以 ``/`` 开头时它是**路径**而不是地址（如 ``/ready``），此时回落到环境变量 /
    默认地址；把路径拼到基地址上是 :func:`atri_x.__main__.health_probe_url` 的事。
    """
    explicit = (opts.health_probe_url or "").strip()
    if explicit and not explicit.startswith("/"):
        return explicit
    return health_probe_default_address(environ)


# Go 命名别名（PascalCase 导出名），与 snake_case 主名指向同一对象。
ParseArgs = parse_args
SupportedCompletionShell = supported_completion_shell
WriteUsage = write_usage
WriteCompletion = write_completion
DetectCompletionShell = detect_completion_shell
WriteBashCompletion = write_bash_completion
WriteZshCompletion = write_zsh_completion
WriteFishCompletion = write_fish_completion
WriteNushellCompletion = write_nushell_completion
WritePowerShellCompletion = write_powershell_completion


# ---------------------------------------------------------------------------
# Python 版新增：``--health-probe``（Go 上游无对应物，待 Lead 确认）
# ---------------------------------------------------------------------------
#
# 约定来源：``deploy/Dockerfile`` 的
#   HEALTHCHECK CMD python -m atri_x --health-probe
# 契约：跑一次 ``GET /live``；2xx → 退出码 0，其余（非 2xx / 连不上 / 超时）→ 1。
# 语义边界与 ``/live`` 一致：只看进程是否还能响应，不判断调度心跳、模型 API
# 与平台连通性；要判断“就绪”请探 ``/ready``（``--health-probe=http://.../ready``）。
