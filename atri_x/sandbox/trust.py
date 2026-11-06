"""进程启动点的信任分级清单（修改清单 §13）。

「不能简单把所有子进程都搬进同一个用户盒子」：用户任务代码、构建脚本、不可信渲染
输入要进执行沙箱；管理员安装的可信控制面插件保留独立信任档案、最小权限运行；任务
新生成的 Skill/插件属于候选区，不能直接热加载到宿主。

本模块只做**登记**（静态清单 + 扫描函数），供测试与文档使用；真正的执行边界在
:mod:`atri_x.sandbox.manager`（用户任务）与各控制面模块本身（可信插件）。
新增 ``create_subprocess_exec`` / ``subprocess.run`` 等启动点时，必须在这里登记
信任类别——``tests/test_sandbox_trust.py`` 会扫描源码，漏登记的模块会让测试失败。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "CLASSES",
    "INVENTORY",
    "TrustEntry",
    "inventory_for",
    "scan_process_sites",
    "summary",
]

#: 信任类别。
CLASSES: dict[str, str] = {
    "sandboxed": "用户任务代码：在租户沙箱容器内执行（默认联网、资源有界）",
    "control_plane": "管理员安装的可信控制面插件：宿主进程、最小权限、只读配置",
    "untrusted_candidate": "任务生成物（Skill/插件/渲染输入）：候选区，不热加载到宿主",
    "host_legacy": "宿主遗留执行（显式 mode=off 或内部管理命令）；沙箱模式不向普通用户开放",
}


@dataclass(frozen=True)
class TrustEntry:
    """一个进程启动点。"""

    module: str
    function: str
    trust: str
    note: str = ""


#: 进程启动点登记表（按模块去重；同一模块多个入口只列主要入口）。
INVENTORY: tuple[TrustEntry, ...] = (
    TrustEntry(
        "atri_x.sandbox.container_cli",
        "EngineCLI.run",
        "control_plane",
        "容器引擎命令行（docker/podman），只在控制面调用，参数受控",
    ),
    TrustEntry(
        "atri_x.sandbox.manager",
        "SandboxService.run_shell",
        "sandboxed",
        "用户任务 shell：容器内执行（唯一的普通用户执行面）",
    ),
    TrustEntry(
        "atri_x.tool.builtin.shell",
        "run_shell_command",
        "host_legacy",
        "宿主 shell：仅在 [sandbox].mode=off 的显式旧模式可用",
    ),
    TrustEntry(
        "atri_x.hook.rules.exec",
        "run_exec_rule",
        "control_plane",
        "Hook 规则命令：hooks.toml 由管理员安装，按最小权限运行",
    ),
    TrustEntry(
        "atri_x.hook.rules.exec_stderr",
        "kill",
        "control_plane",
        "Hook 超时清理（taskkill/ps），只操作自己拉起的进程",
    ),
    TrustEntry(
        "atri_x.hook.runtime.worker",
        "HookWorker.start",
        "control_plane",
        "Hook worker 子进程：可信插件进程，环境收敛后启动",
    ),
    TrustEntry(
        "atri_x.hook.runtime.transport",
        "spawn_kill",
        "control_plane",
        "Hook worker 进程回收",
    ),
    TrustEntry(
        "atri_x.tool.skill.command_tool",
        "run",
        "sandboxed",
        "技能命令：经由 shell/媒体执行通道，沙箱模式下进入租户容器",
    ),
    TrustEntry(
        "atri_x.tool.skill.creator",
        "build",
        "untrusted_candidate",
        "任务生成的 Skill：先落候选目录，构建命令在沙箱内跑",
    ),
    TrustEntry(
        "atri_x.tool.skill.go_source",
        "go_build",
        "untrusted_candidate",
        "Skill 的 Go 源码构建/格式化：在沙箱内跑（go 工具链随镜像提供时）",
    ),
    TrustEntry(
        "atri_x.research.runner",
        "run",
        "untrusted_candidate",
        "研究引擎子进程：输入来自任务，沙箱模式应降级为容器内执行或禁用",
    ),
    TrustEntry(
        "atri_x.sidecar.__main__",
        "main",
        "control_plane",
        "随包 DSH 内核 sidecar（node）：控制面进程",
    ),
    TrustEntry(
        "atri_x.gui.service",
        "start",
        "control_plane",
        "桌面端启动/重启后台服务（管理员操作）",
    ),
    TrustEntry(
        "atri_x.gui.proto_process",
        "stop_process",
        "control_plane",
        "桌面端进程管理（pid 来自本机 pid 文件）",
    ),
    TrustEntry(
        "atri_x.gui.process",
        "kill",
        "control_plane",
        "桌面端按 pid 结束进程（管理员操作）",
    ),
    TrustEntry(
        "atri_x.dist.install",
        "install",
        "control_plane",
        "发行包安装器（管理员操作）",
    ),
    TrustEntry(
        "atri_x.processenv.environment",
        "command",
        "host_legacy",
        "通用子进程环境构造（调用方决定信任类别；不直接启动进程）",
    ),
    TrustEntry(
        "atri_x.sandbox.backend_container",
        "ContainerBackend._spawn",
        "control_plane",
        "沙箱容器内的 docker exec 执行（受控参数，唯一用户任务执行通道）",
    ),
    TrustEntry(
        "atri_x.agent.dsh.transport",
        "start",
        "control_plane",
        "DSH 内核（node）子进程：沙箱模式下 DSH 路由已被限制（见 dsh.service）",
    ),
    TrustEntry(
        "atri_x.agent.commands.research",
        "handle",
        "untrusted_candidate",
        "``/research`` 命令拉起研究引擎：输入来自任务，沙箱模式下应限制/降级",
    ),
    TrustEntry(
        "atri_x.gui.tabs.research",
        "run",
        "control_plane",
        "桌面端「研究」页拉起的引擎进程（管理员操作）",
    ),
    TrustEntry(
        "atri_x.gpu_usage",
        "query",
        "control_plane",
        "读取 GPU 占用（nvidia-smi 等只读查询）",
    ),
    TrustEntry(
        "atri_x.platform.cli.clipboard",
        "copy",
        "control_plane",
        "CLI 剪贴板工具（xclip/pbcopy，管理员本机操作）",
    ),
    TrustEntry(
        "atri_x.tool.builtin.file_tools_directory",
        "read_file_directory_search",
        "host_legacy",
        "目录搜索调用 ripgrep/find：沙箱模式下应改为容器内搜索",
    ),
    TrustEntry(
        "atri_x.tool.builtin.shell_env",
        "configure_shell_process",
        "host_legacy",
        "宿主 shell 进程参数（沙箱模式下不用于用户命令）",
    ),
    TrustEntry(
        "atri_x.tool.skill.process",
        "run_process",
        "untrusted_candidate",
        "Skill 子进程：候选区代码，沙箱模式下应进容器执行",
    ),
)

#: 扫描进程启动点时关心的模式（与 ``tests/test_sandbox_trust.py`` 共用）。
_SPAWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"create_subprocess_exec\("),
    re.compile(r"create_subprocess_shell\("),
    re.compile(r"subprocess\.run\("),
    re.compile(r"subprocess\.Popen\("),
    re.compile(r"os\.system\("),
    re.compile(r"os\.popen\("),
)


def inventory_for(module: str) -> list[TrustEntry]:
    """某模块的全部登记项。"""
    return [entry for entry in INVENTORY if entry.module == module]


def summary() -> dict[str, int]:
    """按信任类别统计登记项。"""
    out = {name: 0 for name in CLASSES}
    for entry in INVENTORY:
        out[entry.trust] = out.get(entry.trust, 0) + 1
    return out


def scan_process_sites(root: str) -> dict[str, int]:
    """扫描 ``atri_x`` 源码里的进程启动点，返回 ``模块名 -> 命中次数``。

    只用于测试/审计（``root`` 指向仓库根）。冻结产物没有源码，不会调用它。
    """
    package = os.path.join(root, "atri_x")
    found: dict[str, int] = {}
    if not os.path.isdir(package):
        return found
    for directory, _dirs, files in os.walk(package):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            hits = sum(len(pattern.findall(text)) for pattern in _SPAWN_PATTERNS)
            if not hits:
                continue
            rel = os.path.relpath(path, root).replace("\\", "/").replace("/", ".")
            module = rel[: -len(".py")] if rel.endswith(".py") else rel
            module = module.replace(".__init__", "")
            found[module] = hits
    return found
