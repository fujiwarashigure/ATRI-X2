"""``fetch_message`` 工具：按消息 id 取回引用 / 转发的完整正文。**Python 版新增**。

背景：入站预处理对引用 / 转发只等一个很短的预算（见
:mod:`atri_x.platform.qq_onebot.slowfetch`），等不到就先放占位，后台继续取。
模型这一轮如果确实需要那段的完整内容，可以用这个工具按 id 再取一次 ——
内容多半已经在缓存里，不需要重新等平台。

平台覆盖：任何在 ``MessageContext.media_resolver`` 上暴露 ``fetch_message_text``
的适配器（当前是 QQ OneBot）。其它平台返回一句"不支持"，不会抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.platform import message_context_from
from atri_x.tool.builder import Builder, required
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow, Tool

__all__ = [
    "FETCH_MESSAGE_TOOL_NAME",
    "FetchMessageArgs",
    "FetchMessageTool",
    "new_fetch_message_tool",
]

#: 工具名。
FETCH_MESSAGE_TOOL_NAME = "fetch_message"

_DESCRIPTION = (
    "按消息 id 取回一条消息或合并转发的完整内容。入站里的引用 / 转发如果只显示"
    "“拉取超时”或用“引用：某人”占位，可以用它按 id 再取一次（内容通常已在缓存里）。"
    "kind：auto 先按普通消息再按转发尝试；message 只取普通消息；forward 只取合并转发。"
)


@dataclass
class FetchMessageArgs:
    """``fetch_message`` 的参数。"""

    message_id: str = ""
    kind: str = "auto"


class FetchMessageTool(Tool):
    """按 id 取消息 / 转发的工具。"""

    def name(self) -> str:
        return FETCH_MESSAGE_TOOL_NAME

    def info(self) -> Info:
        return Builder(self.name()).description(_DESCRIPTION).risk(RiskLow).build_info()

    def schema(self) -> ToolSchema:
        return (
            Builder(self.name())
            .description(_DESCRIPTION)
            .string("message_id", "消息或合并转发的 id（入站占位里给出的那个）。", required())
            .string("kind", "取法：auto（默认）、message 或 forward。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = _decode_args(req.arguments)
        message_id = (args.message_id or "").strip()
        if message_id == "":
            raise InvalidError("message_id is required")
        adapter = _platform_adapter(ctx)
        fetcher = getattr(adapter, "fetch_message_text", None) if adapter is not None else None
        if not callable(fetcher):
            return Result(content="当前平台不支持按 id 取消息（只有能回查历史消息的平台可用）。")
        outcome = fetcher(ctx, message_id, (args.kind or "auto").strip().lower())
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        return Result(content=str(outcome or ""))


def new_fetch_message_tool() -> FetchMessageTool:
    """构造工具（无依赖，注册表直接调用）。"""
    return FetchMessageTool()


def _decode_args(raw: str) -> FetchMessageArgs:
    out = FetchMessageArgs()
    text = (raw or "").strip()
    if text == "":
        return out
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InvalidError(f"parse fetch_message arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError("parse fetch_message arguments: expected a JSON object")
    for key, value in data.items():
        if key in ("message_id", "kind") and isinstance(value, str):
            setattr(out, key, value)
    return out


def _platform_adapter(ctx: Any) -> Any:
    """从 ctx 里取当前平台适配器（``MessageContext.media_resolver``）。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return None
    return getattr(msg, "media_resolver", None)
