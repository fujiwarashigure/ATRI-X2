"""``self_learning_review``：管理 clean-room 表达/黑话候选。

对应 Go：``internal/tool/builtin/self_learning.go``。

批准后的内容才会注入上下文；``toolReviewer`` 决定审核记录里记谁。

与 Go 的差异：

* ``toolReviewer`` → :func:`tool_reviewer`（取 actor 的 id / 平台用户 ID /
  昵称 / 显示名里第一个非空值，全空则回落到 ``"tool"``）。
* :class:`atri_x.selflearning.Service` 的方法在 Python 侧都是同步的（Go 传 ctx），
  因此 ``call`` 里只有工具协议本身是 async。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.llm.llm import ToolSchema
from atri_x.platform import message_context_from
from atri_x.security import actor_from_context
from atri_x.selflearning import StatusApproved, StatusPending, StatusRejected
from atri_x.selflearning import Service as SelfLearningService
from atri_x.tool.builder import Builder
from atri_x.tool.builtin.common import decode_args
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo, first as first_runtime_info
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh

#: 工具名。对应 Go ``selfLearningReviewToolName``。
SELF_LEARNING_REVIEW_TOOL_NAME = "self_learning_review"

#: history 动作固定取最近 20 条。对应 Go 里的字面量 ``20``。
SELF_LEARNING_HISTORY_LIMIT = 20

_ACTIONS = "status、mine、list、approve、reject、undo、history"


@dataclass
class SelfLearningReviewArgs:
    """对应 Go ``selfLearningReviewArgs``。"""

    action: str = ""
    status: str = ""
    id: str = ""
    meaning: str = ""
    min_count: int = 0
    limit: int = 0


def new_self_learning_review_tool(
    service: SelfLearningService | None, *infos: RuntimeInfo
) -> "SelfLearningReviewTool":
    """对应 Go ``NewSelfLearningReviewTool``。"""
    return SelfLearningReviewTool(service, first_runtime_info(*infos))


def self_learning_review_builder() -> Builder:
    """对应 Go ``selfLearningReviewBuilder``。"""
    return (
        Builder(SELF_LEARNING_REVIEW_TOOL_NAME)
        .description(
            "管理 clean-room 表达/黑话候选：查看状态、挖掘候选、列出、批准、拒绝、"
            "撤回或查看审核历史。批准后的内容才会注入上下文。"
        )
        .risk(RiskHigh)
        .superadmin_only()
        .tags("learning")
        .string("action", f"{_ACTIONS}；默认 status。")
        .string("status", "list 时过滤 pending/approved/rejected。")
        .string("id", "approve/reject/undo/history 的候选 ID。")
        .string("meaning", "approve 黑话时可选的含义。")
        .integer(
            "min_count",
            "mine 时候选最低出现次数；最低不同用户数由 self_learning.min_users 配置。",
        )
        .integer("limit", "返回/生成数量上限。")
    )


class SelfLearningReviewTool:
    """对应 Go ``SelfLearningReviewTool``。"""

    def __init__(
        self,
        service: SelfLearningService | None = None,
        info: RuntimeInfo | None = None,
    ) -> None:
        self.service = service
        # Go 的私有字段 ``info runtimeinfo.Info``；Python 里 ``info`` 是
        # ``Tool.info()`` 协议方法，故改名为 ``runtime_info``。
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return SELF_LEARNING_REVIEW_TOOL_NAME

    def info_value(self) -> Info:
        return self_learning_review_builder().build_info()

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return self_learning_review_builder().build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        service = self.service
        if service is None or not service.ready():
            return Result(content="self learning 服务未配置。")
        msg_ctx, ok = message_context_from(ctx)
        if (
            not ok
            or (msg_ctx.platform or "").strip() == ""
            or (msg_ctx.scope_id or "").strip() == ""
        ):
            return Result(content="当前上下文没有平台聊天信息，无法确定学习范围。")
        args = decode_args(req.arguments, SelfLearningReviewArgs, self.name())
        action = (args.action or "").strip().lower()
        platform = msg_ctx.platform
        scope_id = msg_ctx.scope_id

        if action in ("", "status"):
            pending, approved = service.stats(platform, scope_id)
            return Result(
                content=f"self learning：待审 {pending} 条，已批准 {approved} 条。"
            )
        if action == "mine":
            stats = service.mine(
                platform, scope_id, _as_int(args.min_count), _as_int(args.limit)
            )
            summary = (
                f"候选挖掘完成：新增 {stats.created} 条，更新 {stats.updated} 条，"
                f"跳过 {stats.skipped} 条；扫描 {stats.scanned} 条 / "
                f"{stats.total_chars} 字。"
            )
            if stats.truncated:
                summary += " 已达到挖掘预算，结果可能不完整。"
            if stats.timed_out:
                summary += " 挖掘超时，结果可能不完整。"
            return Result(content=summary)
        if action == "list":
            status = (args.status or "").strip() or StatusPending
            candidates = service.review(
                platform, scope_id, status, _as_int(args.limit)
            )
            if not candidates:
                return Result(content="没有符合条件的候选。")
            lines = [f"候选 {len(candidates)} 条："]
            for position, candidate in enumerate(candidates):
                lines.append(
                    f"{position + 1}. [{candidate.status}] id={candidate.id} "
                    f"count={candidate.count} pattern={candidate.pattern}"
                )
            return Result(content="\n".join(lines))
        if action in ("approve", "reject"):
            if (args.id or "").strip() == "":
                return Result(content="approve/reject 需要 id。")
            status = StatusRejected if action == "reject" else StatusApproved
            service.decide(
                platform,
                scope_id,
                args.id,
                status,
                args.meaning,
                tool_reviewer(ctx),
            )
            return Result(content=f"候选 {args.id} 已标记为 {status}。")
        if action == "undo":
            if (args.id or "").strip() == "":
                return Result(content="undo 需要 id。")
            service.undo(platform, scope_id, args.id, tool_reviewer(ctx))
            return Result(content=f"候选 {args.id} 已撤回为待审。")
        if action == "history":
            if (args.id or "").strip() == "":
                return Result(content="history 需要 id。")
            records = service.history(
                platform, scope_id, args.id, SELF_LEARNING_HISTORY_LIMIT
            )
            if not records:
                return Result(content="没有审核记录。")
            lines = [f"审核历史 {len(records)} 条："]
            for position, record in enumerate(records):
                lines.append(
                    f"{position + 1}. {record.from_status} → {record.to_status} "
                    f"by {record.reviewer}"
                )
            return Result(content="\n".join(lines))
        return Result(content=f"action 只支持 {_ACTIONS}。")


def tool_reviewer(ctx: Any) -> str:
    """审核记录里的操作者。对应 Go ``toolReviewer``。"""
    actor, ok = actor_from_context(ctx)
    if not ok or actor is None:
        return "tool"
    for value in (
        getattr(actor, "id", ""),
        getattr(actor, "platform_user_id", ""),
        getattr(actor, "nickname", ""),
        getattr(actor, "display_name", ""),
    ):
        value = (value or "").strip()
        if value != "":
            return value
    return "tool"


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


__all__ = [name for name in dir() if not name.startswith("_")]
