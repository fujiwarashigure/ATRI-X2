"""内置 Hook：群语料观察与表达/黑话上下文注入。

对应 Go：``internal/hook/builtin/self_learning.go``。
"""

from __future__ import annotations

import inspect
from typing import Any

from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.manager import WAKEUP_ANY, Registration
from atri_x.hook.match import always
from atri_x.llm.segment import segments_text_only

__all__ = ["register_self_learning"]

#: 注入上下文的条数上限。对应 Go 里的字面量 ``8``。
SELF_LEARNING_LIMIT = 8


def register_self_learning(registrar: Any, service: Any) -> None:
    """注册自我学习的两个 Hook。对应 Go ``registerSelfLearning``。"""
    if registrar is None or service is None:
        return
    ready = getattr(service, "ready", None)
    if callable(ready) and not ready():
        return

    async def observe(ctx: Any, event: Event) -> Event:
        text = (event.message.platform_text or "").strip()
        if not text:
            text = segments_text_only(event.message.segments).strip()
        if (
            not text
            or not (event.platform.name or "").strip()
            or not (event.platform.scope_id or "").strip()
        ):
            return event
        result = service.observe(
            event.platform.name,
            event.platform.scope_id,
            event.actor.user_id,
            text,
        )
        if inspect.isawaitable(result):
            await result
        return event

    registrar.register(
        Registration(
            point=Point.PLATFORM_MESSAGE_RECEIVED,
            priority=950,
            plugin_id="builtin",
            name="builtin.self_learning_observe",
            description="观察群消息，写入 clean-room 学习语料。",
            match=always(),
            wakeup=WAKEUP_ANY,
            handler=HandlerFunc(observe),
        )
    )

    async def inject_context(ctx: Any, event: Event) -> Event:
        platform_name = (event.platform.name or "").strip()
        scope_id = (event.platform.scope_id or "").strip()
        if not platform_name or not scope_id:
            return event
        query = (event.message.intent_text or "").strip()
        if not query:
            query = segments_text_only(event.message.segments).strip()
        context_text = service.context(
            platform_name, scope_id, query, SELF_LEARNING_LIMIT
        )
        if inspect.isawaitable(context_text):
            context_text = await context_text
        if (context_text or "").strip():
            event.llm.system_append.append(context_text)
        return event

    registrar.register(
        Registration(
            point=Point.LLM_TURN_PREPARED,
            priority=510,
            plugin_id="builtin",
            name="builtin.self_learning_context",
            description="把审核通过的表达/黑话上下文追加为临时 system 上下文。",
            match=always(),
            handler=HandlerFunc(inject_context),
        )
    )
