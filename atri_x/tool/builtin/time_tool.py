"""``get_current_time`` 工具：给模型一个**只读**的当前时间来源。

Python 版新增（Go 侧没有这个工具）。存在意义有两条：

1. 模型可以不依赖系统提示里的片段，主动查询当前时间（片段在 ``inject_context=false``
   时不写具体时刻）；
2. 查询结果里带**校验状态与网络观测快照**，让模型知道"这个时间是本地读数、
   未经网络证实"，而不是把本地时钟当成事实。

安全边界：

* 只读，永不触网（网络刷新走超管命令 ``/time verify``）；
* 不接受地址/端口/文件路径/格式字符串；
* ``timezone`` 只接受本机已安装的 IANA 名（或省略 = 配置默认），非法值结构化报错；
* 系统时钟读不出来时**明确失败**，不返回编造值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.time_service import TimeService
from atri_x.timezones import available_timezones, resolve_timezone
from atri_x.tool.builder import Builder, required
from atri_x.tool.tool import CallRequest, Info, Result, RiskSafe, Tool

__all__ = [
    "GET_CURRENT_TIME_TOOL_NAME",
    "GetCurrentTimeArgs",
    "GetCurrentTimeTool",
    "new_get_current_time_tool",
]

#: 工具名（注册表 / GUI 目录 / 测试三处必须一致）。
GET_CURRENT_TIME_TOOL_NAME = "get_current_time"

_DESCRIPTION = (
    "查询当前时间（时区、本地时间、UTC、星期、unix 秒）以及这次时间的来源与校验状态。"
    "只读：不会联网刷新，也不代表系统时间一定准确——verification.state 会说明网络证据情况。"
    "需要某个具体时区时传 timezone（IANA 名，如 Asia/Shanghai、Europe/London、UTC）。"
)


@dataclass
class GetCurrentTimeArgs:
    """``get_current_time`` 的参数。"""

    timezone: str = ""
    include_observations: bool = False


class GetCurrentTimeTool(Tool):
    """只读的当前时间查询。"""

    def __init__(
        self,
        time_service: TimeService | None = None,
        verification: Any = None,
    ) -> None:
        self.time_service = time_service
        #: 网络校验状态提供者（``.snapshot()`` 返回只读 dict）；未装配时输出 disabled。
        self.verification = verification

    def name(self) -> str:
        return GET_CURRENT_TIME_TOOL_NAME

    def info(self) -> Info:
        return (
            Builder(self.name())
            .description(_DESCRIPTION)
            .risk(RiskSafe)
            .tags("runtime", "time")
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            Builder(self.name())
            .description(_DESCRIPTION)
            .string(
                "timezone",
                "要返回的 IANA 时区名（如 Asia/Shanghai、UTC）。省略 = 配置默认时区。",
            )
            .boolean(
                "include_observations",
                "是否附带最近一次网络时间观测的快照（只读缓存，不会触发联网）。",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = _decode_args(req.arguments)
        service = self._service()
        requested = (args.timezone or "").strip()
        snapshot = service.snapshot(_validate_timezone(requested) if requested else None)
        payload = snapshot.to_dict()
        payload["verification"] = self._verification_payload(service)
        payload["timezone_help"] = _timezone_help(requested, snapshot.timezone)
        if args.include_observations:
            payload["network_observations"] = self._observations()
        return Result(content=json.dumps(payload, ensure_ascii=False, sort_keys=True))

    # -- 内部 ---------------------------------------------------------------
    def _service(self) -> TimeService:
        if self.time_service is not None:
            return self.time_service
        # 未装配（旧装配路径 / 单测）：用进程级默认实例，保证工具仍可用。
        from atri_x.time_service import default_time_service

        return default_time_service()

    def _verification_payload(self, service: TimeService) -> dict[str, Any]:
        snapshot = getattr(self.verification, "snapshot", None)
        if callable(snapshot):
            try:
                data = snapshot()
            except Exception:  # noqa: BLE001 - 诊断失败不应让工具失败
                data = None
            if isinstance(data, dict):
                return {
                    "state": str(data.get("state") or service.verification_state()),
                    "sample_age_seconds": data.get("sample_age_seconds"),
                    "last_check_at": data.get("last_check_at"),
                    "last_success_at": data.get("last_success_at"),
                }
        return {
            "state": service.verification_state(),
            "sample_age_seconds": None,
            "last_check_at": None,
            "last_success_at": None,
        }

    def _observations(self) -> list[dict[str, Any]]:
        observations = getattr(self.verification, "observations", None)
        if callable(observations):
            try:
                rows = observations()
            except Exception:  # noqa: BLE001
                return []
            return [row for row in rows if isinstance(row, dict)]
        return []


def new_get_current_time_tool(
    time_service: TimeService | None = None, verification: Any = None
) -> GetCurrentTimeTool:
    """构造工具。"""
    return GetCurrentTimeTool(time_service, verification)


def _validate_timezone(name: str) -> str:
    """工具入参校验：只接受本机可解析的 IANA 名；不接受 ``system`` / 固定偏移。"""
    text = (name or "").strip()
    if not text:
        raise InvalidError("timezone must not be empty")
    lowered = text.lower()
    if lowered == "system" or lowered.startswith("fixed:"):
        raise InvalidError(
            "timezone must be an IANA name such as Asia/Shanghai or UTC; "
            "'system' and 'fixed:' are configuration-only values"
        )
    known = available_timezones()
    if text not in known:
        raise InvalidError(
            f"unknown timezone {text!r}; pass an IANA name (e.g. Asia/Shanghai, "
            f"Europe/London, UTC)"
        )
    # 双保险：解析失败（数据损坏）也要明确报错。
    resolve_timezone(text, source="argument")
    return text


def _timezone_help(requested: str, effective: str) -> str:
    if requested and requested != effective:
        return f"requested {requested!r} but returned {effective!r}"
    return ""


def _decode_args(raw: str) -> GetCurrentTimeArgs:
    out = GetCurrentTimeArgs()
    text = (raw or "").strip()
    if text == "":
        return out
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InvalidError(f"parse get_current_time arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse get_current_time arguments: expected a JSON object")
    timezone = data.get("timezone")
    if isinstance(timezone, str):
        out.timezone = timezone
    include = data.get("include_observations")
    if isinstance(include, bool):
        out.include_observations = include
    return out
