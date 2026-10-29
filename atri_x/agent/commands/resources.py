"""``/resources``：超管直接查看本机与 ATRI-X 进程的硬件资源占用。

Python 侧新增（1.1.4）。与模型工具 ``runtime_resources`` 的区别只在于**入口**：
命令是确定性入口，不需要等模型排到那一轮、也不会有模型转述误差；渲染实现
（:func:`atri_x.sysinfo.format_resources`）与工具共用同一份，不会漂移。

``Info.MinRole`` 留空（与 ``/doctor`` / ``/rollback`` 一致），路由层把空值当"仅超管"。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.command import Info, Request, Result, new_func

__all__ = ["ResourcesModule", "default_resource_paths", "new_resources", "render_resources"]


def default_resource_paths() -> list[str]:
    """默认统计哪个卷：平台默认配置目录（``%APPDATA%\\ATRI-X`` 或 XDG 等价物）。

    取不到时返回空列表 —— 磁盘段会缺席，但进程 / 系统段仍然可用，不报错。
    """
    from atri_x.config.loader import platform_default_config_path

    config_path = platform_default_config_path()
    if not config_path:
        return []
    directory = os.path.dirname(config_path)
    return [directory] if directory else []


def render_resources(paths: list[str] | None = None) -> str:
    """采集并渲染一次资源快照（同步；含 500ms 采样窗口）。"""
    from atri_x.sysinfo import collect_resources, format_resources

    targets = default_resource_paths() if paths is None else paths
    return format_resources(collect_resources(targets))


def new_resources(deps: Deps) -> Any:
    """暴露只读资源占用快照。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        renderer = deps.resources if deps.resources is not None else render_resources
        try:
            # 采集含 time.sleep(采样窗口)：必须离开事件循环，否则整条流水线卡 500ms。
            text = await asyncio.to_thread(renderer)
            if hasattr(text, "__await__"):
                text = await text
        except Exception as exc:  # noqa: BLE001 - 采集失败回文本，不炸命令流水线
            return Result(content=f"资源采集失败：{exc}")
        return Result(content=str(text))

    return new_func(
        Info(
            name="resources",
            usage="/resources",
            description=(
                "Show the hardware resources the ATRI-X process and this machine are "
                "using right now: process memory / CPU / threads / handles, system "
                "memory and CPU, and free disk space. Read-only; superadmins only."
            ),
        ),
        handle,
    )


class ResourcesModule:
    """``/resources`` 的命令模块。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_resources)
