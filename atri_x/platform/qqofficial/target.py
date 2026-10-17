"""qqofficial 的发送落点与 ctx 传递。

对应 Go：``internal/platform/qqofficial/adapter.go`` 里的 ``sendTarget`` /
``sendTargetKind`` / ``targetKey``，以及 ``send.go`` 的
``qqOfficialTargetScope``。

拆出来是因为 ``adapter`` / ``message`` / ``send`` / ``gateway`` 都要读写同一个
ctx 键，而 ``adapter.py`` 会逼近单文件行数上限（PORTING §2）；Go 侧无此文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.delivery.delivery import ctx_value, ctx_with_value

#: ctx 里携带发送落点的键（对应 Go ``targetKey{}``）。
TARGET_KEY = object()

#: 落点种类。对应 Go ``sendTargetKind`` 的两个常量。
TARGET_C2C = "c2c"
TARGET_GROUP = "group"


@dataclass
class SendTarget:
    """一次发送的落点。对应 Go ``sendTarget``。"""

    kind: str = ""
    openid: str = ""
    msg_id: str = ""
    event_id: str = ""
    proactive: bool = False


def with_target(ctx: Any, target: SendTarget) -> Any:
    """对应 Go ``context.WithValue(ctx, targetKey{}, target)``。"""
    return ctx_with_value(ctx, TARGET_KEY, target)


def target_from_context(ctx: Any) -> tuple[SendTarget, bool]:
    """对应 Go ``ctx.Value(targetKey{}).(sendTarget)`` 的类型断言。"""
    value = ctx_value(ctx, TARGET_KEY)
    if isinstance(value, SendTarget):
        return value, True
    return SendTarget(), False


def is_group_context(ctx: Any) -> bool:
    """当前发送上下文是不是群聊。对应 Go ``isGroupContext``。

    群聊跳过规则本身在交付层（:func:`atri_x.delivery.delivery.should_drop_group_tool_preview`），
    这里只回答"是不是群聊"。
    """
    target, ok = target_from_context(ctx)
    return ok and target.kind == TARGET_GROUP


def target_scope(target: SendTarget) -> str:
    """落点 → 平台 scope。对应 Go ``qqOfficialTargetScope``。"""
    if target.kind == TARGET_GROUP:
        return "group:" + target.openid
    if target.kind == TARGET_C2C:
        return "c2c:" + target.openid
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
