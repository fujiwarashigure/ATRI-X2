"""``/fork <message_id>`` 的助手消息补全来源。

对应 Go：``internal/completion/fork.go``。

``session.Service`` / ``storage.Store`` 属于并行任务的模块，这里按方法鸭子类型
消费。``contextmgr.Loader`` 尚未在 Python 侧落地（Go 侧是
``(contextmgr.Loader{Store: s.Store}).Load(ctx, sessionID)``），因此加载器做成可
注入的 ``loader_factory``；不注入时在调用点惰性 import
:mod:`atri_x.contextmgr`，import 失败会原样抛出而不是静默返回空候选。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atri_x.command import Router
from atri_x.completion.completion import Item, Request
from atri_x.storage.models import RoleAssistant

KIND_FORK_MESSAGE = "fork_message"
KindForkMessage = KIND_FORK_MESSAGE

# ScopeFunc 取当前会话的 scope（Go ``func(context.Context) session.Scope``）。
ScopeFunc = Callable[[Any], Any]
# LoaderFactory 用 store 造一个能 ``await load(ctx, session_id)`` 的加载器。
LoaderFactory = Callable[[Any], Any]


@dataclass
class ForkMessageSource:
    """补全可 Fork 的助手消息 ID。对应 Go ``ForkMessageSource``。"""

    router: Router | None = None
    sessions: Any = None
    store: Any = None
    scope: ScopeFunc | None = None
    loader_factory: LoaderFactory | None = None

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``ForkMessageSource.Complete``。"""
        if (
            self.router is None
            or self.sessions is None
            or self.store is None
            or self.scope is None
        ):
            return []
        parsed = self.router.parse(req.text)
        if not parsed.ok or parsed.name != "fork" or not has_fork_args(req.text, parsed.prefix):
            return []
        try:
            session = await self.sessions.current(ctx, self.scope(ctx))
        except BaseException:
            return []
        if session is None:
            return []
        try:
            loader = self._loader()
            loaded = await loader.load(ctx, session.id)
        except BaseException:
            return []
        prefix = (parsed.args or "").strip()
        out: list[Item] = []
        for message in getattr(loaded, "messages", None) or []:
            if message.role != RoleAssistant or not (message.content or "").strip():
                continue
            if (message.id or "").startswith(prefix):
                out.append(
                    Item(
                        text=f"{parsed.prefix}fork {message.id}",
                        label=message.id,
                        kind=KIND_FORK_MESSAGE,
                    )
                )
        return out

    def _loader(self) -> Any:
        if self.loader_factory is not None:
            return self.loader_factory(self.store)
        from atri_x.contextmgr import Loader  # 惰性 import：并行任务尚未落地时不影响导入

        return Loader(store=self.store)


def has_fork_args(text: str, prefix: str) -> bool:
    """对应 Go ``hasForkArgs``：``/fork`` 之后是否处于参数区。

    Go 的 ``strings.Cut`` 返回 ``(before, after, found)``；Python 的
    ``str.partition`` 返回 ``(before, sep, after)``，因此顺序不同。
    """
    text = (text or "").lstrip(" \t")
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):]
    _, sep, args = rest.partition(" ")
    return (
        sep != ""
        and rest.lstrip(" \t").startswith("fork")
        and (args == "" or args.strip() != "")
    )


__all__ = [name for name in dir() if not name.startswith("_")]
