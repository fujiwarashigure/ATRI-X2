"""平台生命周期事件的确定性处理（不调用模型）。

对应 Go：``internal/agent/events.go``。

消息事件仍然走 :func:`atri_x.agent.message.handle_message`；本模块只处理
notice / request 这类"平台事实"：撤回按单条消息取消、退群按用户取消、入群/
管理员变动只记录不唤醒模型。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from atri_x.delivery import ctx_with_value
from atri_x.logging import (
    ResultCanceled,
    ResultFailed,
    ResultSkipped,
    ResultSucceeded,
)
from atri_x.platform import (
    ConversationGroup,
    ConversationPrivate,
    ConversationUnknown,
    EventNotice,
    EventRequest,
    message_context_from,
    with_message_context,
)
from atri_x.platform import (
    MessageContext as PlatformMessageContext,
)

from atri_x.agent.logging import audit

__all__ = [
    "MessageWorkKeys",
    "MessageWorkRef",
    "cancel_message_work",
    "cancel_user_work",
    "consume_recent_recall",
    "forget_memories_for_recalled_message",
    "handle_platform_event",
    "message_work_key",
    "message_work_keys_from_context",
    "platform_context_for_event",
    "register_message_work",
    "remember_recent_recall",
    "unregister_message_work",
    "with_message_work_keys",
    "without_message_work_keys",
]

#: Go 的 ``messageWorkKeysContextKey{}``。
MessageWorkKeys = object()


@dataclass
class MessageWorkRef:
    """一条平台消息当前绑定的回合请求。对应 Go ``messageWorkRef``。"""

    platform: str = ""
    scope_id: str = ""
    session_id: str = ""
    request_id: str = ""


def handle_platform_event(agent: Any, ctx: Any, event: Any) -> None:
    """处理平台生命周期事件。对应 Go ``Agent.HandlePlatformEvent``。"""
    if agent is None:
        return
    event_ctx = platform_context_for_event(ctx, event)
    scope_value = agent.scope(event_ctx)
    event_type = (getattr(event, "type", "") or "").strip().lower()
    # 平台生命周期事件在这里被确定性接收；事件本身没有"失败"语义，接收事实记
    # succeeded，后续处理若另有结果由各自的审计事件承担。
    audit(
        agent,
        "platform_event",
        "platform",
        event.platform,
        "kind",
        str(getattr(event, "kind", "")),
        "type",
        event_type,
        "scope",
        scope_value.platform_scope_id,
        "actor",
        scope_value.actor_id,
        "result",
        ResultSucceeded,
    )

    # 机器人自己被禁言/退群/解禁是本地运行态，不是用户任务取消。先于成员级通知消费。
    update_runtime = getattr(agent, "update_group_runtime_from_event", None)
    if callable(update_runtime) and update_runtime(event):
        return

    kind = str(getattr(event, "kind", ""))
    if kind == EventNotice:
        if "recall" in event_type:
            # 撤回绑定在一条平台消息上，不是整个群。
            cancel_message_work(agent, event_ctx, event)
            forget_memories_for_recalled_message(agent, event_ctx, event)
        elif "decrease" in event_type:
            # 成员退群/被踢：只取消该成员在本作用域内的在途工作。
            cancel_user_work(agent, event_ctx, event)
        elif "ban" in event_type:
            meta = getattr(event, "meta", None) or {}
            sub_type = str(meta.get("qq_onebot.sub_type") or "").strip().lower()
            if sub_type != "lift_ban":
                cancel_user_work(agent, event_ctx, event)
        # 其余（入群/退群通知、管理员变动、机器人禁言）都是可观测的本地事件：
        # 永不唤醒模型，也永不自动批准请求。
        return
    if kind == EventRequest:
        # 加群/加好友需要单独的超级管理员审批流程；普通群消息或通知不得批准。
        return


def message_work_key(platform_name: str, scope_id: str, message_id: str) -> str:
    """一条平台消息的工作键。对应 Go ``messageWorkKey``。"""
    return "\x00".join(
        [
            (platform_name or "").strip(),
            (scope_id or "").strip(),
            (message_id or "").strip(),
        ]
    )


def with_message_work_keys(ctx: Any, keys: list[str]) -> Any:
    """在 ctx 上追加"本轮合并进来的其他平台消息"。对应 Go ``withMessageWorkKeys``。"""
    if not keys:
        return ctx
    return ctx_with_value(ctx, MessageWorkKeys, list(keys))


def message_work_keys_from_context(ctx: Any) -> list[str]:
    """对应 Go ``messageWorkKeysFromContext``。"""
    from atri_x.delivery import ctx_value

    value = ctx_value(ctx, MessageWorkKeys)
    return list(value) if isinstance(value, list) else []


def without_message_work_keys(ctx: Any) -> Any:
    """清掉消息工作键（进入下一轮 pending 消息时）。对应 Go ``withoutMessageWorkKeys``。"""
    return ctx_with_value(ctx, MessageWorkKeys, [])


def _work_lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "message_work_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.message_work_mu = lock
    return lock


def _work_map(agent: Any) -> dict[str, MessageWorkRef]:
    work = getattr(agent, "message_work", None)
    if work is None:
        work = {}
        agent.message_work = work
    return work


def register_message_work(agent: Any, ctx: Any, session_id: str, request_id: str) -> bool:
    """把当前平台消息（以及本轮合并进来的）绑到正在处理它们的回合请求。

    对应 Go ``Agent.registerMessageWork``。返回 ``True`` 表示"这条消息在注册前
    就已经被撤回"，调用方不得继续产出输出。
    """
    ident = (request_id or "").strip()
    if agent is None or not ident:
        return False
    msg, ok = message_context_from(ctx)
    if not ok:
        return False
    keys: list[str] = []
    if (msg.platform_message_id or "").strip():
        keys.append(message_work_key(msg.platform, msg.scope_id, msg.platform_message_id))
    keys.extend(message_work_keys_from_context(ctx))
    if not keys:
        return False
    seen: dict[str, bool] = {}
    with _work_lock(agent):
        work = _work_map(agent)
        for key in keys:
            trimmed = (key or "").strip()
            if not trimmed or seen.get(trimmed):
                continue
            seen[trimmed] = True
            work[trimmed] = MessageWorkRef(
                platform=msg.platform,
                scope_id=msg.scope_id,
                session_id=session_id,
                request_id=ident,
            )
    for key in seen:
        if not consume_recent_recall(agent, key):
            continue
        # 撤回早于注册到达（例如重连之后）：取消刚绑定的请求，并告诉调用方
        # 不要再为它产出输出。
        agent.mark_turn_canceled(ident)
        if getattr(agent, "requests", None) is not None:
            agent.requests.cancel(ident)
        audit(
            agent,
            "platform_recall_cancel_late",
            "platform",
            msg.platform,
            "scope",
            msg.scope_id,
            "message_id",
            msg.platform_message_id,
            "request_id",
            ident,
            "result",
            ResultCanceled,
        )
        return True
    return False


def unregister_message_work(agent: Any, request_id: str) -> None:
    """回合结束后摘掉绑定并清掉终态标记。对应 Go ``unregisterMessageWork``。"""
    ident = (request_id or "").strip()
    if agent is None or not ident:
        return
    with _work_lock(agent):
        work = _work_map(agent)
        for key in [k for k, ref in work.items() if ref.request_id == ident]:
            work.pop(key, None)
    agent.clear_turn_state(ident)


def cancel_message_work(agent: Any, ctx: Any, event: Any) -> None:
    """撤回一条平台消息：取消排在收件箱里的、或正在跑的那个请求。

    对应 Go ``Agent.cancelMessageWork``。
    """
    key = message_work_key(event.platform, event.scope_id, event.message_id)
    if agent.cancel_inbox_message(key) > 0:
        audit(
            agent,
            "platform_recall_cancel_queued",
            "platform",
            event.platform,
            "scope",
            event.scope_id,
            "message_id",
            event.message_id,
            "result",
            ResultCanceled,
        )
        return
    with _work_lock(agent):
        work = _work_map(agent)
        ref = work.pop(key, None)
    if ref is None:
        remember_recent_recall(agent, key)
        audit(
            agent,
            "platform_recall_noop",
            "platform",
            event.platform,
            "scope",
            event.scope_id,
            "message_id",
            event.message_id,
            "result",
            ResultSkipped,
        )
        return
    agent.mark_turn_canceled(ref.request_id)
    if getattr(agent, "requests", None) is not None:
        agent.requests.cancel(ref.request_id)
    audit(
        agent,
        "platform_recall_cancel",
        "platform",
        event.platform,
        "scope",
        event.scope_id,
        "message_id",
        event.message_id,
        "session_id",
        ref.session_id,
        "request_id",
        ref.request_id,
        "result",
        ResultCanceled,
    )


def recall_tombstone_ttl(agent: Any) -> float:
    """撤回墓碑的存活秒数。对应 Go ``Agent.recallTombstoneTTL``（1~15 分钟）。"""
    ttl = float(getattr(agent, "response_timeout", 0.0) or 0.0)
    if ttl < 60.0:
        ttl = 60.0
    if ttl > 900.0:
        ttl = 900.0
    return ttl


def _recall_lock(agent: Any) -> threading.Lock:
    lock = getattr(agent, "recall_mu", None)
    if lock is None:
        lock = threading.Lock()
        agent.recall_mu = lock
    return lock


def _recall_map(agent: Any) -> dict[str, datetime]:
    recalls = getattr(agent, "recent_recalls", None)
    if recalls is None:
        recalls = {}
        agent.recent_recalls = recalls
    return recalls


def remember_recent_recall(agent: Any, key: str) -> None:
    """记住"这条消息的撤回先到了"。对应 Go ``Agent.rememberRecentRecall``。"""
    if agent is None or not (key or "").strip():
        return
    now = datetime.now(timezone.utc)
    ttl = recall_tombstone_ttl(agent)
    with _recall_lock(agent):
        recalls = _recall_map(agent)
        for existing, at in list(recalls.items()):
            if (now - at).total_seconds() > ttl:
                recalls.pop(existing, None)
        recalls[key] = now


def consume_recent_recall(agent: Any, key: str) -> bool:
    """消费一次撤回墓碑。对应 Go ``Agent.consumeRecentRecall``。"""
    if agent is None or not (key or "").strip():
        return False
    with _recall_lock(agent):
        recalls = _recall_map(agent)
        at = recalls.pop(key, None)
    if at is None:
        return False
    return (datetime.now(timezone.utc) - at).total_seconds() <= recall_tombstone_ttl(agent)


def cancel_user_work(agent: Any, ctx: Any, event: Any) -> None:
    """成员退群/被禁言：只取消该成员在本作用域内的在途工作。

    对应 Go ``Agent.cancelUserWork``。
    """
    requests = getattr(agent, "requests", None)
    if requests is None:
        return
    user_id = (getattr(event, "user_id", "") or "").strip()
    if not user_id:
        return
    fair_key = agent.request_fair_key(ctx)
    for request_id in requests.fair_key_ids(fair_key):
        agent.mark_turn_canceled(request_id)
    count = requests.cancel_fair_key(fair_key)
    count += agent.cancel_inbox_fair_key(fair_key)
    audit(
        agent,
        "platform_user_cancel",
        "platform",
        event.platform,
        "scope",
        event.scope_id,
        "user_id",
        user_id,
        "fair_key",
        fair_key,
        "count",
        count,
        "result",
        ResultCanceled,
    )


def forget_memories_for_recalled_message(agent: Any, ctx: Any, event: Any) -> None:
    """撤回消息时忘掉由它产生的长期记忆。对应 Go ``forgetMemoriesForRecalledMessage``。"""
    memory = getattr(agent, "angel_memory", None)
    if (
        agent is None
        or memory is None
        or not memory.ready()
        or not getattr(agent, "angel_memory_forget_on_recall", False)
    ):
        return
    platform_name = (getattr(event, "platform", "") or "").strip()
    scope_id = (getattr(event, "scope_id", "") or "").strip()
    message_id = (getattr(event, "message_id", "") or "").strip()
    if not platform_name or not scope_id or not message_id:
        return
    try:
        from atri_x.angelmemory import SourceFilter

        count = memory.delete_by_source(
            ctx, platform_name, scope_id, SourceFilter(message_id=message_id)
        )
    except Exception as exc:  # noqa: BLE001 - 忘记失败不能影响撤回本身
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.warn(
                "forget recalled angel memory failed",
                platform=platform_name,
                scope=scope_id,
                message_id=message_id,
                error=str(exc),
            )
        audit(
            agent,
            "angel_memory_forget_failed",
            "platform",
            platform_name,
            "scope",
            scope_id,
            "message_id",
            message_id,
            "error",
            str(exc),
            "result",
            ResultFailed,
        )
        return
    if count > 0:
        audit(
            agent,
            "angel_memory_forget_recalled",
            "platform",
            platform_name,
            "scope",
            scope_id,
            "message_id",
            message_id,
            "count",
            count,
            "result",
            ResultSucceeded,
        )


def platform_context_for_event(ctx: Any, event: Any) -> Any:
    """把平台事件投影成消息 ctx。对应 Go ``platformContextForEvent``。"""
    scope_id = getattr(event, "scope_id", "") or ""
    kind = ConversationUnknown
    if scope_id.startswith(("group:", "supergroup:")):
        kind = ConversationGroup
    elif scope_id.startswith(("private:", "c2c:")):
        kind = ConversationPrivate
    return with_message_context(
        ctx,
        PlatformMessageContext(
            platform=getattr(event, "platform", "") or "",
            platform_user_id=(getattr(event, "user_id", "") or "").strip(),
            scope_id=scope_id.strip(),
            conversation_kind=kind,
            platform_message_id=(getattr(event, "message_id", "") or "").strip(),
        ),
    )
