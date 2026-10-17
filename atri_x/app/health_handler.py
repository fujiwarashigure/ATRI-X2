"""平台消息处理器包装：把每条消息的结果记进健康状态。

对应 Go：``internal/app/health_handler.go``。

Go 的 ``healthHandler`` 只在 ``HandleMessage`` 上做一层统计；Python 侧的平台
处理器协议是 ``async def handle_message(ctx, text) -> None``，所以本类同样只实现
``handle_message``，其余属性/方法用 ``__getattr__`` 透传给内层处理器 ——
Agent 作为 ``PlatformHandler`` 还带别的成员，透传才不会把它阉掉。
"""

from __future__ import annotations

from typing import Any

__all__ = ["HealthHandler", "healthHandler"]


class HealthHandler:
    """对应 Go ``healthHandler``。"""

    def __init__(self, inner: Any = None, state: Any = None) -> None:
        self.inner = inner
        self.state = state

    async def handle_message(self, ctx: Any, text: str) -> Any:
        """对应 Go ``HandleMessage``。

        Go 侧 ``err != nil`` 时记 ``RecordMessageError``；Python 侧错误是异常，
        因此这里捕获后先计数再原样重抛（不能让统计改变错误语义）。
        """
        if self.inner is None:
            return None
        try:
            result = self.inner.handle_message(ctx, text)
            if hasattr(result, "__await__"):
                result = await result
        except BaseException:
            if self.state is not None:
                self.state.record_message_error()
            raise
        if self.state is not None:
            self.state.record_message_handled()
        return result

    def __getattr__(self, name: str) -> Any:
        # 只在内层存在时透传；避免 ``inner`` 自身尚未赋值时无限递归。
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    # Go 命名别名。
    HandleMessage = handle_message


healthHandler = HealthHandler
