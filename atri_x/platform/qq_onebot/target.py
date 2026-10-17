"""发送落点、ctx 传递与 scope 计算（Go ``adapter.go`` 里的 ``target`` 系列）。

对应 Go：``internal/platform/qq-onebot/adapter.go`` 的 ``target`` / ``targetKey`` /
``targetToQQ`` / ``oneBotTargetScope`` / ``isGroupContext`` / ``scopeID`` /
``eventScopeID``。

拆出来是因为 ``adapter`` / ``preprocess`` / ``output`` 都要读写同一个 ctx 键，
而 ``adapter.py`` 已接近单文件行数上限（PORTING §2）；Go 侧无此文件。

形式差异：Go 的 ``(target, error)`` → 抛 :class:`atri_x.errors.InvalidError`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.delivery.delivery import Target as DeliveryTarget
from atri_x.delivery.delivery import ctx_value, ctx_with_value
from atri_x.errors import InvalidError
from atri_x.platform.qq_onebot.types import parse_int64

#: ctx 里携带发送落点的键（对应 Go ``targetKey{}``）。
TARGET_KEY = object()

#: 会话种类字面量。对应 Go 里对 ``event.MessageType`` 的直接比较。
MESSAGE_TYPE_PRIVATE = "private"
MESSAGE_TYPE_GROUP = "group"


@dataclass
class Target:
    """一次发送的落点。对应 Go ``target``。"""

    message_type: str = ""
    user_id: int = 0
    group_id: int = 0


def with_target(ctx: Any, target: Target) -> Any:
    """对应 Go ``context.WithValue(ctx, targetKey{}, target)``。"""
    return ctx_with_value(ctx, TARGET_KEY, target)


def target_from_context(ctx: Any) -> tuple[Target, bool]:
    """对应 Go ``ctx.Value(targetKey{}).(target)`` 的类型断言。"""
    value = ctx_value(ctx, TARGET_KEY)
    if isinstance(value, Target):
        return value, True
    return Target(), False


def target_to_qq(out_target: DeliveryTarget) -> Target:
    """把交付层目标解析成 OneBot 落点。对应 Go ``targetToQQ``。

    只给 ``scope_id`` 时按 ``private:`` / ``group:`` 前缀回填；两者都缺时抛
    :class:`atri_x.errors.InvalidError`。
    """
    private_user_id = (out_target.private_user_id or "").strip()
    group_id = (out_target.group_id or "").strip()
    if private_user_id == "" and group_id == "":
        scope = (out_target.scope_id or "").strip()
        if scope.startswith("private:"):
            private_user_id = scope.removeprefix("private:")
        elif scope.startswith("group:"):
            group_id = scope.removeprefix("group:")
    if private_user_id != "":
        user_id = parse_int64(private_user_id)
        if user_id is None:
            raise InvalidError(f"parse qqonebot private user id: invalid syntax {private_user_id!r}")
        return Target(message_type=MESSAGE_TYPE_PRIVATE, user_id=user_id)
    if group_id != "":
        value = parse_int64(group_id)
        if value is None:
            raise InvalidError(f"parse qqonebot group id: invalid syntax {group_id!r}")
        return Target(message_type=MESSAGE_TYPE_GROUP, group_id=value)
    raise InvalidError("qqonebot target missing private_user_id, group_id or scope_id")


def one_bot_target_scope(target: Target) -> str:
    """落点 → 平台 scope。对应 Go ``oneBotTargetScope``。"""
    if target.message_type == MESSAGE_TYPE_GROUP:
        return "group:" + str(target.group_id)
    if target.message_type == MESSAGE_TYPE_PRIVATE:
        return "private:" + str(target.user_id)
    return ""


def is_group_context(ctx: Any) -> bool:
    """当前发送上下文是不是群聊。对应 Go ``isGroupContext``。

    群聊跳过规则本身在交付层（:func:`atri_x.delivery.delivery.should_drop_group_tool_preview`），
    这里只回答"是不是群聊"。
    """
    target, ok = target_from_context(ctx)
    return ok and target.message_type == MESSAGE_TYPE_GROUP


def scope_id(event: Any) -> str:
    """会话 scope。对应 Go ``scopeID``（群聊优先按 ``group:``）。"""
    message_type = getattr(event, "message_type", "")
    if message_type == MESSAGE_TYPE_GROUP:
        return "group:" + str(getattr(event, "group_id", 0))
    return "private:" + str(getattr(event, "user_id", 0))


def event_scope_id(event: Any) -> str:
    """notice/request 事件用的 scope。对应 Go ``eventScopeID``。"""
    group_id = getattr(event, "group_id", 0)
    if group_id:
        return "group:" + str(group_id)
    user_id = getattr(event, "user_id", 0)
    if user_id:
        return "private:" + str(user_id)
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
