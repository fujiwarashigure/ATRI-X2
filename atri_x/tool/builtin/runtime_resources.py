"""``runtime_resources``：超管专属的只读硬件资源占用查询。

Python 侧新增（上游 Go 没有对应工具）。数据来源见 :mod:`atri_x.sysinfo`：

- 进程：RSS / 峰值 RSS / 私有提交 / CPU%（单核基准）/ 线程 / 句柄 / 读写字节；
- 系统：内存总量与可用量、全机 CPU%、逻辑核数、运行时长；
- 磁盘：给定路径所在卷的总量与可用量（可选递归统计目录大小）。

三条硬约束：

1. **只读、无副作用** —— 因此 ``risk(RiskSafe)``，不需要二次确认；
2. **仅超级管理员** —— ``superadmin_only()`` 同时在"发现"与"执行"两道闸门上生效
   （非超管连 schema 都拿不到，见 :mod:`atri_x.tool.discover` 与
   :mod:`atri_x.toolrun.runner`）；
3. **不阻塞事件循环** —— :func:`atri_x.sysinfo.collect_resources` 为了算 CPU% 会
   ``time.sleep(采样窗口)``，所以 ``call`` 里必须走 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
# format_resources / format_uptime 的**实现**在 sysinfo（/resources 命令也用它），
# 这里只做再导出，保证工具与命令共用同一份渲染。
from atri_x.sysinfo import (
    collect_resources,
    format_resources,
    format_uptime,
    resources_to_dict,
)
from atri_x.tool.builder import Builder
from atri_x.tool.tool import CallRequest, Info, Result, RiskSafe

#: 工具名。群白名单 / ``tool_allowlist`` 里用的就是这个名字。
RUNTIME_RESOURCES_NAME = "runtime_resources"

#: 参数名白名单：与 Go 工具一致的"未知字段直接拒绝"策略。
_RUNTIME_RESOURCES_ARG_KEYS = frozenset({"sample_ms", "paths", "include_dir_size"})

#: 采样窗口的默认与边界（毫秒）。
DEFAULT_SAMPLE_MS = 500
MIN_SAMPLE_MS = 100
MAX_SAMPLE_MS = 2000


@dataclass
class RuntimeResourcesArgs:
    """``runtime_resources`` 的入参。"""

    sample_ms: int = DEFAULT_SAMPLE_MS
    paths: list[str] | None = None
    include_dir_size: bool = False


class RuntimeResourcesTool:
    """超管专属的只读资源查询工具。"""

    def __init__(self, runtime_info: Any = None) -> None:
        # 提供配置目录（默认统计哪个卷 / 哪个目录的大小）。
        self.runtime_info = runtime_info

    def name(self) -> str:
        return RUNTIME_RESOURCES_NAME

    def info(self) -> Info:
        return runtime_resources_builder().build_info()

    def schema(self):
        return runtime_resources_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = parse_runtime_resources_args(req.arguments)
        paths = list(args.paths or [])
        if not paths:
            config_dir = str(getattr(self.runtime_info, "config_dir", "") or "")
            if config_dir:
                paths = [config_dir]
        try:
            # 同步采集 + time.sleep：必须离开事件循环，否则整条消息流水线卡住。
            snapshot = await asyncio.to_thread(
                collect_resources,
                paths,
                sample_interval=args.sample_ms / 1000.0,
                include_dir_size=args.include_dir_size,
            )
        except Exception as exc:  # noqa: BLE001 - 采集失败要回文本，不炸 turn
            return Result(content=f"资源采集失败：{exc}")
        return Result(
            content=format_resources(snapshot),
            data=json.dumps(resources_to_dict(snapshot), ensure_ascii=False),
        )


def runtime_resources_builder() -> Builder:
    """``runtime_resources`` 的声明式元信息与 schema。"""
    return (
        Builder(RUNTIME_RESOURCES_NAME)
        .description(
            "查看当前 ATRI-X 进程与所在机器的硬件资源占用：进程内存 / CPU / 线程 / 句柄、"
            "系统内存与 CPU、磁盘剩余。只读、无副作用、仅超级管理员可用。"
            "用户问“现在占多少内存 / CPU / 磁盘还剩多少”时调用。"
        )
        .superadmin_only()
        .risk(RiskSafe)
        .tags("agent", "runtime")
        .integer(
            "sample_ms",
            f"CPU 采样窗口毫秒数，默认 {DEFAULT_SAMPLE_MS}，"
            f"范围 {MIN_SAMPLE_MS}-{MAX_SAMPLE_MS}。",
        )
        .string_array("paths", "要统计磁盘剩余 / 目录大小的路径列表；默认配置目录。")
        .boolean("include_dir_size", "是否递归统计目录大小（较慢，默认 false）。")
    )


def new_runtime_resources_tool(runtime_info: Any = None) -> RuntimeResourcesTool:
    """构造 ``runtime_resources``（与 Go 的 ``NewXxxTool`` 同风格）。"""
    return RuntimeResourcesTool(runtime_info)


def parse_runtime_resources_args(raw: str) -> RuntimeResourcesArgs:
    """解析入参；未知字段报错（对齐 Go 的 ``DisallowUnknownFields``）。"""
    if not raw:
        return RuntimeResourcesArgs()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse runtime_resources arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse runtime_resources arguments: expected a JSON object")
    for key in data:
        if key not in _RUNTIME_RESOURCES_ARG_KEYS:
            raise InvalidError(
                f'parse runtime_resources arguments: json: unknown field "{key}"'
            )
    return RuntimeResourcesArgs(
        sample_ms=_parse_sample_ms(data.get("sample_ms")),
        paths=_parse_paths(data.get("paths")),
        include_dir_size=_parse_bool(data.get("include_dir_size")),
    )


def _parse_sample_ms(value: Any) -> int:
    if value is None:
        return DEFAULT_SAMPLE_MS
    try:
        sample_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidError("sample_ms must be an integer") from exc
    return min(max(sample_ms, MIN_SAMPLE_MS), MAX_SAMPLE_MS)


def _parse_paths(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise InvalidError("paths must be an array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidError("paths must be an array of strings")
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def _parse_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise InvalidError("include_dir_size must be a boolean")


__all__ = [name for name in dir() if not name.startswith("_")]
