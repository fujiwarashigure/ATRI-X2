"""``runtime_resources``：超管专属的只读硬件资源占用查询。

Python 侧新增（上游 Go 没有对应工具）。数据来源见 :mod:`atri_x.sysinfo` 与
:mod:`atri_x.hardware` / :mod:`atri_x.gpu_usage`：

- 硬件：CPU 型号、逻辑核数、系统版本、架构、显卡型号与显存容量；
- 进程：RSS / 峰值 RSS / 私有提交 / CPU%（单核基准）/ 线程 / 句柄 / 读写字节 /
  已运行时长（"软件进程状态"）；
- 系统：内存总量与可用量、全机 CPU%、逻辑核数、开机时长；
- 显卡：每卡利用率 / 显存 / 温度 / 功耗（``nvidia-smi``），或整机 3D 占用（PDH）；
- 磁盘：本机固定磁盘或指定路径所在卷的容量（**只报卷标签，不报真实路径**）。

三条硬约束：

1. **只读、无副作用** —— 因此 ``risk(RiskSafe)``，不需要二次确认；
2. **仅超级管理员** —— ``superadmin_only()`` 同时在"发现"与"执行"两道闸门上生效
   （非超管连 schema 都拿不到，见 :mod:`atri_x.tool.discover` 与
   :mod:`atri_x.toolrun.runner`）；
3. **不阻塞事件循环** —— :func:`atri_x.sysinfo.collect_resources` 为了算 CPU% 会
   ``time.sleep(采样窗口)``，所以 ``call`` 里必须走 ``asyncio.to_thread``。

隐私（1.1.4 修订）
------------------
工具结果会被模型转述到群聊，因此与 ``/resources`` 同一条边界：**不返回任何本机
真实路径、用户名、主机名**。``paths`` 参数只用于"选哪个卷统计容量"，渲染成
``指定卷 N``；磁盘采集失败也只写异常类型名。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
# format_resources / format_uptime 的**实现**在 sysinfo_format（/resources 命令也用它），
# sysinfo 只是再导出；这里从 sysinfo 取，保证工具与命令共用同一份渲染。
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
        # 保留构造参数以兼容既有装配（``opts.runtime_info``）；1.1.4 修订后
        # 不再需要"配置目录"来选卷 —— 默认自动枚举本机固定磁盘。
        self.runtime_info = runtime_info

    def name(self) -> str:
        return RUNTIME_RESOURCES_NAME

    def info(self) -> Info:
        return runtime_resources_builder().build_info()

    def schema(self):
        return runtime_resources_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = parse_runtime_resources_args(req.arguments)
        try:
            # 同步采集 + time.sleep：必须离开事件循环，否则整条消息流水线卡住。
            snapshot = await asyncio.to_thread(
                collect_resources,
                args.paths,
                sample_interval=args.sample_ms / 1000.0,
                include_dir_size=args.include_dir_size,
            )
        except Exception as exc:  # noqa: BLE001 - 采集失败要回文本，不炸 turn
            return Result(content=f"资源采集失败：{type(exc).__name__}")
        return Result(
            content=format_resources(snapshot),
            data=json.dumps(resources_to_dict(snapshot), ensure_ascii=False),
        )


def runtime_resources_builder() -> Builder:
    """``runtime_resources`` 的声明式元信息与 schema。"""
    return (
        Builder(RUNTIME_RESOURCES_NAME)
        .description(
            "查看当前 ATRI-X 进程与所在机器的硬件资源占用：CPU / 内存 / 磁盘容量、"
            "显卡占用与显存、CPU/GPU 型号、软件进程状态（内存 / CPU / 线程 / 句柄 / 运行时长）。"
            "只读、无副作用、仅超级管理员可用；只返回型号与占用，不返回本机文件路径。"
            "用户问“现在占多少内存 / CPU / 显卡 / 磁盘还剩多少”时调用。"
        )
        .superadmin_only()
        .risk(RiskSafe)
        .tags("agent", "runtime")
        .integer(
            "sample_ms",
            f"CPU 采样窗口毫秒数，默认 {DEFAULT_SAMPLE_MS}，"
            f"范围 {MIN_SAMPLE_MS}-{MAX_SAMPLE_MS}。",
        )
        .string_array(
            "paths",
            "仅用于选择要统计容量的卷；缺省自动枚举本机固定磁盘。"
            "结果只回卷标签（系统盘 / 指定卷 N），不回显路径。",
        )
        .boolean(
            "include_dir_size",
            "是否递归统计 paths 的目录大小（较慢，默认 false；未给 paths 时忽略）。",
        )
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
