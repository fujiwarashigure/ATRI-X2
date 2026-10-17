"""``group_analysis``：统计当前群聊的消息量、活跃成员与活跃时段。

对应 Go：``internal/tool/builtin/group_analysis.go``。

与 Go 的差异：

* :meth:`atri_x.groupanalysis.Service.analyze` 在 Python 侧是同步方法（Go 传 ctx），
  :meth:`Service.summarize` 是 async，因此 ``call`` 只 await 摘要那一步。
* ``t.info.CurrentTime().IsZero()`` → Python 侧 ``now`` 永不为零值，这里按
  "取不到就退到本机当前时间" 的口径处理（与 Go 的 ``if now.IsZero()`` 等价）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from atri_x.errors import InvalidError
from atri_x.groupanalysis import Request as GroupAnalysisRequest
from atri_x.groupanalysis import Service as GroupAnalysisService
from atri_x.llm.llm import ToolSchema
from atri_x.platform import message_context_from
from atri_x.tool.builder import Builder
from atri_x.tool.builtin.common import decode_args
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo, first as first_runtime_info
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

#: 工具名。对应 Go ``groupAnalysisToolName``。
GROUP_ANALYSIS_TOOL_NAME = "group_analysis"

#: ``maxMessages`` 的默认值。对应 Go ``NewGroupAnalysisTool`` 的兜底。
GROUP_ANALYSIS_DEFAULT_MAX_MESSAGES = 5000


@dataclass
class GroupAnalysisArgs:
    """对应 Go ``groupAnalysisArgs``。"""

    days: int = 0
    limit: int = 0


def new_group_analysis_tool(
    service: GroupAnalysisService | None, max_messages: int, *infos: RuntimeInfo
) -> "GroupAnalysisTool":
    """对应 Go ``NewGroupAnalysisTool``。"""
    if max_messages <= 0:
        max_messages = GROUP_ANALYSIS_DEFAULT_MAX_MESSAGES
    return GroupAnalysisTool(service, max_messages, first_runtime_info(*infos))


def group_analysis_builder() -> Builder:
    """对应 Go ``groupAnalysisBuilder``。"""
    return (
        Builder(GROUP_ANALYSIS_TOOL_NAME)
        .description(
            "统计当前群聊在指定天数内的消息量、活跃成员和活跃时段，"
            "输出平台无关的文本报告。"
        )
        .risk(RiskLow)
        .tags("chat", "group")
        .integer("days", "统计最近多少天，默认 1，最大 30。")
        .integer("limit", "最多读取的历史消息条数，默认 5000。")
    )


class GroupAnalysisTool:
    """对应 Go ``GroupAnalysisTool``。"""

    def __init__(
        self,
        service: GroupAnalysisService | None = None,
        max_messages: int = GROUP_ANALYSIS_DEFAULT_MAX_MESSAGES,
        info: RuntimeInfo | None = None,
    ) -> None:
        self.service = service
        self.max_messages = max_messages
        # Go 的私有字段 ``info runtimeinfo.Info``；Python 里 ``info`` 是
        # ``Tool.info()`` 协议方法，故改名为 ``runtime_info``。
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return GROUP_ANALYSIS_TOOL_NAME

    def info_value(self) -> Info:
        return group_analysis_builder().build_info()

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return group_analysis_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if self.service is None:
            return Result(content="群分析服务未配置。")
        msg_ctx, ok = message_context_from(ctx)
        if (
            not ok
            or (msg_ctx.platform or "").strip() == ""
            or (msg_ctx.scope_id or "").strip() == ""
        ):
            return Result(content="当前上下文没有平台聊天信息，无法确定要分析的群。")
        args = decode_args(req.arguments, GroupAnalysisArgs, self.name())
        days = _as_int(args.days)
        if days <= 0:
            days = 1
        if days > 30:
            days = 30
        now = self.runtime_info.current_time() or _local_now()
        limit = _as_int(args.limit)
        if limit <= 0 or limit > self.max_messages:
            limit = self.max_messages
        report = self.service.analyze(
            GroupAnalysisRequest(
                platform=msg_ctx.platform,
                scope_id=msg_ctx.scope_id,
                since=now - timedelta(days=days),
                until=now,
                limit=limit,
            )
        )
        content = report.format_text()
        summary = await self.service.summarize(ctx, report)
        if summary is not None and (summary or "").strip() != "":
            content = "摘要：" + summary.strip() + "\n\n" + content
        return Result(content=content)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _local_now() -> datetime:
    return datetime.now().astimezone()


__all__ = [name for name in dir() if not name.startswith("_")]
