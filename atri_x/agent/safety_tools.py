"""工具链路上的内容安全接线（从 :mod:`atri_x.agent.toolrun_adapter` 抽出）。

拆出来的原因：``toolrun_adapter.py`` 已超 600 行并被行数棘轮冻结（只降不升），
1.1.8 的内容安全升级还要往这里加"工具级差异化策略"。这里先做**纯搬迁**。

两个职责：

* :func:`attach_safety_ctx` —— 把 guard（按会话模式绑定）与审计归属挂到工具 ``ctx``；
* :func:`guard_segments` —— 工具结果的文本段脱敏 / 图片段剥离。

**不做什么**：不判定策略、不决定"是否远程审"——那是 :mod:`atri_x.safety` 的事；
这里只负责"拿到 guard、按它的指示改消息段"。
"""

from __future__ import annotations

from typing import Any

from atri_x.llm import MessageSegment, SegmentType
from atri_x.safety import (
    TOOL_IMAGE_STRIPPED_TEXT,
    mode_from_host,
    scope_from_host,
    with_safety_guard,
    with_safety_scope,
)

__all__ = [
    "_safety_guard_segments",
    "attach_safety_ctx",
    "guard_segments",
]


def attach_safety_ctx(agent: Any, ctx: Any) -> Any:
    """把 guard 与审计归属挂到工具 ctx 上。

    工具（``image_generate`` / ``send_file`` / ``comic``）拿不到 ``agent``，只能从 ctx 取。
    guard 绑定当前会话模式：chat / work 的「按模式覆盖」策略在工具内部也生效。
    """
    guard = getattr(agent, "safety_guard", None)
    if guard is not None:
        guard = guard.for_mode(mode_from_host(agent, ctx))
    ctx = with_safety_guard(ctx, guard)
    return with_safety_scope(ctx, scope_from_host(agent, ctx))


def guard_segments(
    agent: Any, ctx: Any, segments: list[MessageSegment], call: Any
) -> list[MessageSegment]:
    """工具结果审核。

    文本段逐段过 ``filter_text``（``enforce`` 下脱敏、命中词表类换成拦截文案）；
    ``should_strip_tool_images()`` 为真时把图片段替换成文本占位（模型与后续上下文
    看不到图）。``guard`` 未装配或 ``mode = off`` 时原样返回。
    """
    guard = getattr(agent, "safety_guard", None)
    if guard is None:
        return segments
    scope = scope_from_host(agent, ctx)
    mode = mode_from_host(agent, ctx)
    strip_images = guard.should_strip_tool_images(mode)
    tool_name = str(getattr(call, "name", "") or "")
    out: list[MessageSegment] = []
    for segment in segments:
        if segment.type == SegmentType.TEXT:
            filtered, _decision = guard.filter_text(
                segment.text,
                stage="tool",
                scope=scope,
                meta={"tool": tool_name},
                mode=mode,
            )
            if filtered != segment.text:
                clone = segment.clone()
                clone.text = filtered
                out.append(clone)
            else:
                out.append(segment)
            continue
        if segment.type == SegmentType.IMAGE and strip_images:
            out.append(
                MessageSegment(type=SegmentType.TEXT, text=TOOL_IMAGE_STRIPPED_TEXT)
            )
            continue
        out.append(segment)
    return out


#: 兼容旧名（1.1.7 及更早的测试按 ``_safety_guard_segments`` 取）。
_safety_guard_segments = guard_segments
