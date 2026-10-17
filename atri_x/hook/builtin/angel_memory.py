"""内置 Hook：按平台/会话召回长期记忆，追加为临时 system 上下文。

对应 Go：``internal/hook/builtin/angel_memory.go``。
"""

from __future__ import annotations

import inspect
from typing import Any

from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.manager import Registration
from atri_x.hook.match import always
from atri_x.llm.segment import segments_text_only

__all__ = ["register_angel_memory"]

#: 召回的条数上限。对应 Go 里的字面量 ``5``。
ANGEL_MEMORY_LIMIT = 5


def register_angel_memory(registrar: Any, service: Any) -> None:
    """注册天使记忆 Hook。对应 Go ``registerAngelMemory``。"""
    if registrar is None or service is None:
        return
    ready = getattr(service, "ready", None)
    if callable(ready) and not ready():
        return

    async def handle(ctx: Any, event: Event) -> Event:
        platform_name = (event.platform.name or "").strip()
        scope_id = (event.platform.scope_id or "").strip()
        if not platform_name or not scope_id:
            return event
        query = (event.message.intent_text or "").strip()
        if not query:
            query = segments_text_only(event.message.segments).strip()
        context_text = service.context(platform_name, scope_id, query, ANGEL_MEMORY_LIMIT)
        if inspect.isawaitable(context_text):
            context_text = await context_text
        if not (context_text or "").strip():
            return event
        event.llm.system_append.append(context_text)
        return event

    registrar.register(
        Registration(
            point=Point.LLM_TURN_PREPARED,
            priority=500,
            plugin_id="builtin",
            name="builtin.angel_memory",
            description="按当前平台/会话召回长期记忆，追加为临时 system 上下文。",
            match=always(),
            handler=HandlerFunc(handle),
        )
    )
