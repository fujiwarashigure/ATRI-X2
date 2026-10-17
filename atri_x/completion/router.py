"""命令补全来源：整行命令名与命令参数。

对应 Go：``internal/completion/router.go``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atri_x.command import (
    Actor,
    Completer,
    CompletionRequest,
    Router,
    can_access,
)
from atri_x.completion.completion import Item, Request

# KindCommand 是命令名补全的 kind。
KIND_COMMAND = "command"
KindCommand = KIND_COMMAND


@dataclass
class RouterSource:
    """按 Router 补全命令名与命令参数。对应 Go ``RouterSource``。

    ``actor`` 是取当前 actor 的回调（Go ``func(context.Context) security.Actor``）；
    为 None 时退回 ctx 里的 actor（``security.ActorFromContext``）。
    """

    router: Router | None = None
    actor: Callable[[Any], Actor] | None = None

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``RouterSource.Complete``。"""
        if self.router is None:
            return []
        actor = self._actor(ctx)
        items = await self.complete_args(ctx, req, actor)
        if items:
            return items
        texts = self.router.complete_for_actor(req.text, actor)
        if not texts:
            return []
        return [
            Item(text=text, label=text.lstrip("/-"), kind=KIND_COMMAND) for text in texts
        ]

    async def complete_args(self, ctx: Any, req: Request, actor: Actor) -> list[Item]:
        """对应 Go ``RouterSource.completeArgs``：只在已进入参数区时才补参数。"""
        router = self.router
        if router is None:
            return []
        parsed = router.parse(req.text)
        rest = req.text.lstrip(" \t")
        if rest.startswith(parsed.prefix):
            rest = rest[len(parsed.prefix):]
        if not parsed.ok or parsed.name == "" or (" " not in rest and "\t" not in rest):
            return []
        handler, ok = router.handler(parsed.name)
        if not ok or handler is None:
            return []
        if not can_access(handler.info(), actor):
            return []
        if not isinstance(handler, Completer):
            return []
        completions = await handler.complete(
            ctx,
            CompletionRequest(
                raw=req.text,
                prefix=parsed.prefix,
                name=parsed.name,
                args=parsed.args,
                cursor=req.cursor_or_end(),
            ),
        )
        return [
            Item(
                text=item.text,
                label=item.label,
                description=item.description,
                kind=item.kind,
                replace_start=item.replace_start,
                replace_end=item.replace_end,
            )
            for item in completions
        ]

    def _actor(self, ctx: Any) -> Actor:
        """对应 Go ``RouterSource.actor``。"""
        if self.actor is not None:
            return self.actor(ctx)
        from atri_x.tool.capability import ActorFromContext

        actor, ok = ActorFromContext(ctx)
        return actor if ok and actor is not None else Actor()


__all__ = [name for name in dir() if not name.startswith("_")]
