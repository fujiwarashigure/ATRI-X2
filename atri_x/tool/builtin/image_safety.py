"""生图链路上的内容安全接线（从 :mod:`atri_x.tool.builtin.image_generate` 抽出）。

拆出来的原因：``image_generate.py`` 已超 600 行并被行数棘轮冻结（只降不升），
1.1.8 要给图片出口加"远程图片审核"，先在 P1 做纯搬迁。

两个职责：

* :func:`filter_image_prompt` —— 真正请求 provider 之前审核拼接好的正/负提示词；
* :func:`gate_image_output` —— 结果出口按策略丢弃图片并补文本说明。

**不做什么**：不做图片内容识别（那是 1.1.8 引擎在 :mod:`atri_x.safety.engine` 里的事，
本模块只负责"问 guard 要不要拦"）。``comic`` 服务有同名逻辑但走自己的 ctx 路径，
不在本模块范围内。
"""

from __future__ import annotations

from typing import Any

from atri_x.llm.llm import MessageSegment, SegmentText
from atri_x.safety import guard_from_context, scope_from_context
from atri_x.tool.builtin.common import append_warnings

__all__ = ["filter_image_prompt", "gate_image_output"]

#: 提示词被拦时返回给模型的说明。
PROMPT_BLOCKED_TEXT = "内容安全策略拒绝：生图提示词不合规，本次未发起生成请求。"

#: 图片出口被拦时的兜底说明（与 :data:`atri_x.safety.MEDIA_BLOCK_TEXT` 同义）。
IMAGE_BLOCKED_FALLBACK = "（这张图片因内容安全策略被拦截，未发送。）"


def filter_image_prompt(
    ctx: Any, positive: str, negative: str, *, tool_name: str
) -> tuple[str, str] | None:
    """审核生图提示词。

    返回 ``(positive, negative)``（可能已被脱敏）；返回 ``None`` 表示命中词表类且
    处于 ``enforce``，调用方应**直接失败、不发起请求**。``guard`` 未装配时原样返回。
    """
    guard = guard_from_context(ctx)
    if guard is None:
        return positive, negative
    scope = scope_from_context(ctx)
    positive_text, positive_decision = guard.filter_text(
        positive, stage="image_prompt", scope=scope, meta={"tool": tool_name}
    )
    negative_text, negative_decision = guard.filter_text(
        negative, stage="image_prompt", scope=scope, meta={"tool": tool_name}
    )
    blocked = [
        decision
        for decision in (positive_decision, negative_decision)
        if decision.action == "block" and guard.enforcing
    ]
    if blocked:
        return None
    return positive_text, negative_text


async def gate_image_output(ctx: Any, result: Any, *, tool_name: str) -> None:
    """图片输出边界：``block_image_output`` 生效时清空输出并补一条文本说明。

    **不做内容识别**，只按策略在出口丢弃；发送边界还会再兜一次（幂等）。
    """
    guard = guard_from_context(ctx)
    if guard is None or not getattr(result, "outputs", None):
        return
    decision = await guard.aallow_media_output(
        "image", stage="tool", scope=scope_from_context(ctx), meta={"tool": tool_name}
    )
    if decision.allowed:
        return
    note = decision.text or IMAGE_BLOCKED_FALLBACK
    result.outputs = []
    append_warnings(result, note)
    result.content = (result.content or "") + "\n" + note
    result.segments.append(MessageSegment(type=SegmentText, text=note))
