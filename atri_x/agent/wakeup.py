"""唤醒判定：群聊里这条消息要不要叫醒模型，以及唤醒词的剥离。

对应 Go：``internal/agent/wakeup.go``。

``strip_trigger_keyword`` 属于 :mod:`atri_x.platform`（唯一出处），本模块只做
"配置里的唤醒词 + 平台给的唤醒词合并"和"@机器人 / 引用机器人算不算唤醒"。
"""

from __future__ import annotations

from typing import Any

from atri_x.delivery import ctx_value, ctx_with_value
from atri_x.llm import segments_text_only
from atri_x.platform import (
    ConversationPrivate,
    ConversationUnknown,
    message_context_from,
    strip_trigger_keyword,
)
from atri_x.security import RoleSuperadmin
from atri_x.storage import RoleAssistant, now as storage_now

__all__ = [
    "MessageWakeup",
    "first_token_bounds",
    "hook_wakeup",
    "inbound_raw_text",
    "is_reply_to_bot",
    "is_space_byte",
    "mentions_bot",
    "message_wakeup",
    "message_wakeup_from_context",
    "starts_with_command_prefix",
    "strip_bot_mention",
    "strip_wakeup_prefix",
    "strip_wakeup_prefix_from_text",
    "with_message_wakeup",
]

#: Go 的 ``messageWakeupContextKey{}``。
MessageWakeup = object()


def with_message_wakeup(ctx: Any, woken: bool) -> Any:
    """对应 Go ``withMessageWakeup``。"""
    return ctx_with_value(ctx, MessageWakeup, bool(woken))


def message_wakeup_from_context(ctx: Any) -> tuple[bool, bool]:
    """对应 Go ``messageWakeupFromContext``。"""
    value = ctx_value(ctx, MessageWakeup)
    if isinstance(value, bool):
        return value, True
    return False, False


def hook_wakeup(agent: Any, ctx: Any, event: Any) -> bool:
    """Hook 侧的唤醒判定入口。对应 Go ``Agent.hookWakeup``。"""
    woken, ok = message_wakeup_from_context(ctx)
    if ok:
        return woken
    text = (segments_text_only(event.message.segments) or "").strip()
    if not text:
        text = inbound_raw_text(ctx)
    return message_wakeup(agent, ctx, text)


def message_wakeup(agent: Any, ctx: Any, text: str) -> bool:
    """这条入站消息是否唤醒模型。对应 Go ``Agent.messageWakeup``。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return True
    if msg.conversation_kind in ("", ConversationUnknown, ConversationPrivate):
        return True
    commands = getattr(agent, "commands", None)
    if commands is not None and commands.is_command(text):
        return True
    match_text = text
    if msg.match_text_set:
        match_text = msg.match_text
    policy = agent.group_policy_for_scope(agent.scope(ctx))
    keywords = list(msg.trigger_keywords) + list(policy.wake_keywords_value())
    # 免打扰时段压掉普通群聊与 @，但不压 slash 命令（上面已放行）与超级管理员的
    # 运维消息。静默来源有两个：群策略 ``quiet_hours`` 与全局 ``[quiet]``（任一生效
    # 即静默，见 :mod:`atri_x.agent.admission`）。
    from atri_x.agent.admission import quiet_window_active

    if quiet_window_active(agent, ctx):
        allow_superadmin = bool(
            getattr(getattr(agent, "quiet_config", None), "allow_superadmin", True)
        )
        if not (allow_superadmin and agent.actor(ctx).role == RoleSuperadmin):
            return False
    mode = policy.response_mode_value()
    keyword_hit = strip_trigger_keyword(match_text, keywords)[1]
    mention_hit = mentions_bot(msg)
    reply_hit = is_reply_to_bot(agent, ctx, msg)
    if mode == "all":
        woken = bool((text or "").strip()) or len(msg.segments) > 0
        direct = keyword_hit or mention_hit or reply_hit
    elif mode == "keyword":
        woken = keyword_hit
        direct = keyword_hit
    elif mode == "reply":
        woken = reply_hit
        direct = reply_hit
    elif mode == "off":
        return False
    else:
        woken = keyword_hit or mention_hit or reply_hit
        direct = woken
    if not woken:
        return False
    # 参与状态机（评估 §D）：只在开启后生效，默认 no-op。只压普通闲聊，不压直接互动。
    from atri_x.agent.participation import note_inbound, note_wakeup, suppress_ambient_wakeup

    note_inbound(agent, ctx, direct=direct)
    if suppress_ambient_wakeup(agent, ctx, direct=direct):
        return False
    note_wakeup(agent, ctx)
    return True


def strip_wakeup_prefix(agent: Any, ctx: Any, text: str) -> str:
    """剥掉唤醒词/@机器人。对应 Go ``Agent.stripWakeupPrefix``。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return text
    keywords = list(msg.trigger_keywords) + list(
        agent.group_policy_for_scope(agent.scope(ctx)).wake_keywords_value()
    )
    msg.trigger_keywords = keywords
    return strip_wakeup_prefix_from_text(text, msg, agent.command_prefixes())


def strip_wakeup_prefix_from_text(text: str, msg: Any, prefixes: list[str]) -> str:
    """纯函数版前缀剥离。对应 Go ``stripWakeupPrefixFromText``。"""
    stripped, found = strip_trigger_keyword(text, msg.trigger_keywords)
    if found:
        return stripped
    before, separator, after = (text or "").partition("\n\n")
    if separator:
        inner = strip_wakeup_prefix_from_text(after, msg, prefixes)
        if inner != after:
            return before + "\n\n" + inner
    return strip_bot_mention(text, msg, prefixes)


def inbound_raw_text(ctx: Any) -> str:
    """入站原始文本。对应 Go ``inboundRawText``。"""
    msg, ok = message_context_from(ctx)
    if ok:
        return (msg.raw_text or "").strip()
    return ""


def mentions_bot(msg: Any) -> bool:
    """消息里是否 @ 了机器人。对应 Go ``mentionsBot``。"""
    bot_user_id = (msg.bot.user_id or "").strip()
    bot_username = (msg.bot.username or "").strip().lstrip("@")
    for mention in msg.mentions:
        if bot_user_id and (mention.user_id or "").strip() == bot_user_id:
            return True
        if bot_username and (mention.username or "").strip().lstrip("@").lower() == bot_username.lower():
            return True
    return False


def strip_bot_mention(text: str, msg: Any, prefixes: list[str]) -> str:
    """去掉 ``@机器人`` 这个 token，保留其余空白与换行。对应 Go ``stripBotMention``。"""
    bot_username = (msg.bot.username or "").strip().lstrip("@")
    if not bot_username or not text:
        return text
    start, end, ok = first_token_bounds(text)
    if ok:
        token = text[start:end]
        name, at_sign, mention = token.partition("@")
        if at_sign and mention.lower() == bot_username.lower() and starts_with_command_prefix(name, prefixes):
            return text[:start] + name + text[end:]
    needle = "@" + bot_username
    lower_needle = needle.lower()
    search_from = 0
    while search_from <= len(text):
        lower_text = text.lower()
        if search_from > len(lower_text):
            break
        rel = lower_text.find(lower_needle, search_from)
        if rel < 0:
            break
        index = rel
        before_ok = index == 0 or is_space_byte(text[index - 1])
        end_index = index + len(needle)
        after_ok = end_index >= len(text) or is_space_byte(text[end_index])
        if not before_ok or not after_ok:
            # 文本命中属于更大的 token（例如邮箱或 URL）：跳过，不改写这个 token。
            search_from = index + 1
            continue
        text = text[:index] + text[end_index:]
        search_from = index
    return text.strip()


def first_token_bounds(text: str) -> tuple[int, int, bool]:
    """第一个非空白 token 的边界。对应 Go ``firstTokenBounds``。"""
    start = 0
    while start < len(text) and is_space_byte(text[start]):
        start += 1
    if start >= len(text):
        return 0, 0, False
    end = start
    while end < len(text) and not is_space_byte(text[end]):
        end += 1
    return start, end, True


def is_space_byte(value: str) -> bool:
    """Go 按字节判断的四种空白。对应 Go ``isSpaceByte``。"""
    return value in (" ", "\t", "\n", "\r")


def starts_with_command_prefix(token: str, prefixes: list[str]) -> bool:
    """对应 Go ``startsWithCommandPrefix``。"""
    for prefix in prefixes:
        if prefix and token.startswith(prefix):
            return True
    return False


def is_reply_to_bot(agent: Any, ctx: Any, msg: Any) -> bool:
    """这条消息是否在回复机器人。对应 Go ``Agent.isReplyToBot``。"""
    if (msg.reply_to_sender_id or "").strip() and (msg.reply_to_sender_id or "").strip() == (
        msg.bot.user_id or ""
    ).strip():
        return True
    reply_id = (msg.reply_to_message_id or "").strip()
    store = getattr(agent, "store", None)
    if not reply_id or store is None or store.messages() is None:
        return False
    try:
        mapped = store.messages().find_by_platform_message(
            msg.platform, msg.scope_id, reply_id
        )
    except Exception:  # noqa: BLE001 - 找不到映射就是"不是回复机器人"
        mapped = None
    if mapped is not None and mapped.role == RoleAssistant:
        return True
    if store.media() is not None:
        try:
            outputs = store.media().find_outputs(
                msg.platform, msg.scope_id, reply_id, storage_now()
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(outputs)
    return False
