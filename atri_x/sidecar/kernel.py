"""内置 DSH 内核的定位与启动命令组装。

DSH 内核由 ``packaging/assemble_kernel.py`` 生成，布局见 :mod:`atri_x.sidecar`。
本模块只做三件事：

1. :func:`find_kernel` —— 找到一份可用的内核（清单齐全、``node.exe`` 与入口都在）；
2. :func:`dsh_command` —— 组装 ``[node.exe, bin.js, "--profile", "acp"]``，
   直接喂给 :class:`atri_x.agent.dsh.AcpClient`（它接受序列）；
3. :func:`describe` —— 把结论做成可打印/可测试的字典（``doctor``、``smoke``、CLI 用）。

**回退语义**：找不到内核时 :func:`dsh_command` 返回 ``None``，调用方继续用
``dsh --profile acp``（PATH 上的外部安装）。是否内置内核是发行包的选择，
不是代码路径的分叉。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.sidecar.registry import Runtime, find, load_runtime

__all__ = [
    "DSH_KERNEL_ENV",
    "DSH_KERNEL_ID",
    "DshKernel",
    "dsh_command",
    "describe",
    "find_kernel",
    "kernel_from_dir",
]

#: 内核 id（清单里的 ``id``）。
DSH_KERNEL_ID = "dsh-kernel"

#: 直接指向内核目录的环境变量（优先于根目录搜索，便于调试/替换）。
DSH_KERNEL_ENV = "ATRIX_DSH_KERNEL"

#: ACP profile 名。
DEFAULT_PROFILE = "acp"


@dataclass(frozen=True)
class DshKernel:
    """一份可用的 DSH 内核。"""

    root: str
    node: str
    entry: str
    dsh_version: str
    node_version: str
    verified: bool
    profile: str
    source: str

    def command(self, profile: str = "") -> tuple[str, ...]:
        """启动命令（``node.exe`` + ``bin.js`` + ``--profile <name>``）。"""
        return (self.node, self.entry, "--profile", (profile or self.profile or DEFAULT_PROFILE))

    def missing(self) -> list[str]:
        """缺失的关键文件（空列表表示完整）。"""
        out: list[str] = []
        for label, path in (("node", self.node), ("entry", self.entry)):
            if not path or not os.path.isfile(path):
                out.append(f"{label}: {path or '<manifest 缺字段>'}")
        return out


def find_kernel() -> DshKernel | None:
    """找到一份可用的 DSH 内核；没有则返回 ``None``。

    优先看 ``ATRIX_DSH_KERNEL`` 指向的目录，其次按 :mod:`atri_x.sidecar.registry`
    的根目录顺序找 ``id == dsh-kernel`` 的清单。
    """
    explicit = (os.environ.get(DSH_KERNEL_ENV) or "").strip()
    if explicit:
        kernel = kernel_from_dir(os.path.abspath(os.path.expanduser(explicit)))
        if kernel is not None:
            return kernel
    runtime = find(DSH_KERNEL_ID)
    if runtime is None:
        return None
    return _kernel_from_runtime(runtime)


def kernel_from_dir(directory: str) -> DshKernel | None:
    """从一个具体目录读内核（不做根目录搜索）。"""
    runtime = load_runtime(directory)
    if runtime is None or runtime.id != DSH_KERNEL_ID:
        return None
    return _kernel_from_runtime(runtime)


def dsh_command(profile: str = "") -> tuple[str, ...] | None:
    """内置内核的启动命令；没有可用内核时返回 ``None``（调用方回退外部 ``dsh``）。"""
    kernel = find_kernel()
    if kernel is None or kernel.missing():
        return None
    return kernel.command(profile)


def describe() -> dict[str, Any]:
    """内核状态快照（``doctor``、``python -m atri_x.sidecar`` 与测试共用）。"""
    kernel = find_kernel()
    if kernel is None:
        return {
            "found": False,
            "id": DSH_KERNEL_ID,
            "searched": _searched_dirs(),
            "detail": "未找到内置 DSH 内核（回退 PATH 上的 dsh）",
        }
    return {
        "found": True,
        "id": DSH_KERNEL_ID,
        "root": kernel.root,
        "node": kernel.node,
        "entry": kernel.entry,
        "dshVersion": kernel.dsh_version,
        "nodeVersion": kernel.node_version,
        "verified": kernel.verified,
        "profile": kernel.profile,
        "source": kernel.source,
        "missing": kernel.missing(),
        "command": list(kernel.command()),
    }


def _kernel_from_runtime(runtime: Runtime) -> DshKernel:
    manifest: Mapping[str, Any] = runtime.manifest
    return DshKernel(
        root=runtime.root,
        node=runtime.path("node") or "",
        entry=runtime.path("entry") or "",
        dsh_version=str(manifest.get("dshVersion") or ""),
        node_version=str(manifest.get("nodeVersion") or ""),
        verified=bool(manifest.get("verified")),
        profile=str(manifest.get("profile") or DEFAULT_PROFILE),
        source=runtime.source,
    )


def _searched_dirs() -> list[str]:
    from atri_x.sidecar.registry import RUNTIME_DIR_ENV, bundle_root, config_runtime_dir  # noqa: PLC0415

    out = [os.path.join(bundle_root(), "runtime")]
    user_dir = config_runtime_dir()
    if user_dir:
        out.append(user_dir)
    explicit = (os.environ.get(RUNTIME_DIR_ENV) or "").strip()
    if explicit:
        out.insert(0, os.path.abspath(os.path.expanduser(explicit)))
    return out
