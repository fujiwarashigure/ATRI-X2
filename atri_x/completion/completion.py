"""平台补全服务：候选条目、请求与"首个非空来源胜出"的合并规则。

对应 Go：``internal/completion/completion.go``。

``Source.Complete`` 在 Go 里不返回 error，但风险/分叉两个来源要查库或读 Session，
因此 Python 侧统一是异步方法（纯字符串来源只是不 await 任何东西）；
``context.Context`` → 鸭子类型 ``ctx``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable


@dataclass
class Item:
    """一个结构化补全候选。对应 Go ``Item``。

    UI 层不需要知道候选来自哪里就能渲染它。
    """

    text: str = ""
    label: str = ""
    description: str = ""
    kind: str = ""
    replace_start: int = 0
    replace_end: int = 0


@dataclass
class Request:
    """一次补全请求。对应 Go ``Request``。``cursor`` 为字符偏移。"""

    text: str = ""
    cursor: int = 0

    def cursor_or_end(self) -> int:
        """对应 Go ``Request.CursorOrEnd``：越界或未给定时取文本末尾。"""
        if self.cursor <= 0 or self.cursor > len(self.text):
            return len(self.text)
        return self.cursor


@runtime_checkable
class Source(Protocol):
    """一类补全来源。对应 Go ``Source``。"""

    async def complete(self, ctx: Any, req: Request) -> list[Item]: ...


class Service:
    """按来源顺序取第一个非空结果的补全服务。对应 Go ``Service``。"""

    def __init__(self, *sources: Source | None) -> None:
        self._sources = [source for source in sources if source is not None]

    def sources(self) -> list[Source]:
        """当前来源列表（用于测试与诊断）。"""
        return list(self._sources)

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``Service.Complete``：第一个非空来源胜出。"""
        for source in self._sources:
            items = await source.complete(ctx, req)
            if items:
                return items
        return []


def new_service(*sources: Source | None) -> Service:
    """对应 Go ``NewService``（nil 来源被丢弃）。"""
    return Service(*sources)


def texts(items: Sequence[Item] | None) -> list[str]:
    """对应 Go ``Texts``：只取非空 ``text``。"""
    return [item.text for item in items or [] if item.text]


__all__ = [name for name in dir() if not name.startswith("_")]
