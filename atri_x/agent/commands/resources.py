"""``/resources``：超管直接查看本机与 ATRI-X 进程的硬件资源占用。

Python 侧新增（1.1.4）。与模型工具 ``runtime_resources`` 的区别只在于**入口**：
命令是确定性入口，不需要等模型排到那一轮、也不会有模型转述误差；渲染实现
（:func:`atri_x.sysinfo_format.format_resources`）与工具共用同一份，不会漂移。

1.1.4 修订（隐私）
------------------
此前磁盘段会打印"配置目录所在卷"的**真实路径**（如 ``C:\\Users\\<用户名>\\AppData\\...``），
等于把用户名与目录结构一起报给了群聊。现在：

* 磁盘段只报**卷标签**（``系统盘`` / ``数据盘 N``）与容量，不报盘符与路径；
* CPU / GPU 只报型号与占用；不报主机名、用户名、安装目录；
* 采集失败的描述只保留异常类型名（异常原文可能带路径）。

``Info.MinRole`` 留空（与 ``/doctor`` / ``/rollback`` 一致），路由层把空值当"仅超管"。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.command import Info, Request, Result, new_func

__all__ = ["ResourcesModule", "new_resources", "render_resources"]


def render_resources(paths: list[str] | None = None) -> str:
    """采集并渲染一次资源快照（同步；含采样窗口与硬件采集）。

    ``paths`` 为空时自动枚举本机**固定磁盘**（只出卷标签，不出路径）；显式传
    路径只会被渲染成"指定卷 N"，同样不回显路径。
    """
    from atri_x.sysinfo import collect_resources, format_resources

    return format_resources(collect_resources(paths))


def new_resources(deps: Deps) -> Any:
    """暴露只读资源占用快照。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        renderer = deps.resources if deps.resources is not None else render_resources
        try:
            # 采集含 time.sleep(采样窗口)：必须离开事件循环，否则整条流水线卡住。
            text = await asyncio.to_thread(renderer)
            if hasattr(text, "__await__"):
                text = await text
        except Exception as exc:  # noqa: BLE001 - 采集失败回文本，不炸命令流水线
            return Result(content=f"资源采集失败：{type(exc).__name__}")
        return Result(content=str(text))

    return new_func(
        Info(
            name="resources",
            usage="/resources",
            description=(
                "Show this machine's hardware usage right now: CPU / memory / disk "
                "occupancy, GPU usage and VRAM, plus the ATRI-X process status. "
                "Reports models and percentages only — never local file paths. "
                "Read-only; superadmins only."
            ),
        ),
        handle,
    )


class ResourcesModule:
    """``/resources`` 的命令模块。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_resources)
