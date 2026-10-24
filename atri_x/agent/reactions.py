"""表情反应层与 Agent 回合输出的接线（Python 侧新增，无 Go 对应）。

本模块把三样东西拼起来，是"表情图库"在 Agent 侧的唯一入口：

* ``[reactions]`` 配置（:class:`atri_x.config.app_sections.ReactionsConfig`）
  → :class:`atri_x.reaction.ReactionSettings`；
* 当前回合的作用域（平台 + 群/私聊、会话）→ 冷却/去重键；
* 一个 :class:`atri_x.reaction.ReactionPick` → 一条 ``timing = "after_assistant"``
  的图片输出（正文先发、表情后发，由交付层统一投递）。

调用点在 :func:`atri_x.agent.chat_turn._run_chat_body` 的回合收尾处：**只处理最终
回答**（工具轮次的中间正文不配图），且整轮最多一张。
"""

from __future__ import annotations

import logging as std_logging
from typing import Any, Sequence

from atri_x.delivery import (
    DELIVERY_AFTER_ASSISTANT,
    Output,
    image_path,
    with_delivery_timing,
)
from atri_x.reaction import ReactionService, ReactionSettings

__all__ = [
    "build_reaction_service",
    "conversation_kind",
    "finalize_turn_reaction",
    "reaction_settings_from_config",
    "reaction_scope",
]

#: ``ReactionsConfig`` → :class:`ReactionSettings` 需要拷贝的字段名。
_SETTINGS_FIELDS = (
    "enabled",
    "gallery_dir",
    "show_in_group",
    "show_in_private",
    "cooldown_text_replies",
    "session_cooldown_seconds",
    "group_cooldown_seconds",
    "dedupe_recent",
    "probability",
    "max_text_chars",
    "suppress_keywords",
)

#: 被视为"群聊作用域"的 scope 前缀（QQ / Telegram 的群与超群）。
_GROUP_PREFIXES = ("group:", "supergroup:", "channel:", "guild:", "thread:")

#: 被视为"私聊作用域"的 scope 前缀。
_PRIVATE_PREFIXES = ("private:", "friend:", "user:", "dm:")


def reaction_settings_from_config(config: Any) -> ReactionSettings:
    """把 ``[reactions]`` 配置对象转成运行期设置（缺字段回落默认值）。"""
    if config is None:
        return ReactionSettings()
    data: dict[str, Any] = {}
    for name in _SETTINGS_FIELDS:
        if hasattr(config, name):
            data[name] = getattr(config, name)
    return ReactionSettings.from_config(data)


def build_reaction_service(config: Any) -> ReactionService | None:
    """按配置构造进程内单例；没有图库目录时返回 ``None``（功能不启用）。"""
    if config is None:
        return None
    gallery = str(getattr(config, "gallery_dir", "") or "").strip()
    if not gallery:
        return None
    return ReactionService(reaction_settings_from_config(config), gallery)


def conversation_kind(scope_id: str) -> str:
    """按作用域前缀判断会话种类：``group`` / ``private`` / ``channel`` / ``unknown``。"""
    value = (scope_id or "").strip().lower()
    if value.startswith(_GROUP_PREFIXES):
        return "channel" if value.startswith(("channel:", "guild:")) else "group"
    if value.startswith(_PRIVATE_PREFIXES):
        return "private"
    return "unknown"


def reaction_scope(agent: Any, ctx: Any) -> tuple[str, str]:
    """返回 ``(platform, scope_id)``：优先取当前 ctx 推导的 Scope。"""
    try:
        current = agent.scope(ctx)
        platform = str(getattr(current, "platform", "") or "").strip()
        scope_id = str(getattr(current, "platform_scope_id", "") or "").strip()
    except Exception:  # noqa: BLE001 - 作用域推导失败只是"不发图"
        return "", ""
    return platform, scope_id


def finalize_turn_reaction(
    agent: Any,
    ctx: Any,
    session: Any,
    *,
    text: str,
    emotions: Sequence[str],
) -> list[Output]:
    """回合收尾：按策略决定要不要补一张表情图，返回待投递的输出。

    这是一次**纯决策 + 状态更新**：不发送、不 await。调用方把返回的输出追加进
    ``deferred_outputs``（``timing = "after_assistant"``），由 Output Manager 在正文
    之后统一投递。

    发送失败的处理口径：表情是"锦上添花"，图库/路径问题一律静默降级为不发图；
    图片投递失败时正文已经在前面正常发出，因此这里不感知回执（冷却在决策通过时
    即提交，属于有意的乐观提交：宁可少发一张，也不要因为重复尝试而刷屏）。
    """
    service = getattr(agent, "reaction_service", None)
    if service is None:
        return []
    platform, scope_id = reaction_scope(agent, ctx)
    if not platform or not scope_id:
        return []
    kind = conversation_kind(scope_id)
    if kind == "unknown":
        # CLI / 本地会话等拿不到群聊语义的上下文不发图。
        return []
    is_group = kind in ("group", "channel")
    scope_key = platform + ":" + scope_id
    group_key = scope_key if is_group else ""
    decision = service.decide(
        scope_key=scope_key,
        group_key=group_key,
        is_group=is_group,
        text=text,
        emotions=emotions,
    )
    if decision.pick is None:
        service.note_text_reply(scope_key=scope_key, group_key=group_key)
        _log(agent, "reaction skipped", reason=decision.reason, platform=platform,
             scope=scope_id)
        return []
    service.commit_image(decision.pick, scope_key=scope_key, group_key=group_key)
    output = image_path(decision.pick.path)
    output.name = decision.pick.name
    output.alt_text = decision.pick.name
    _log(agent, "reaction picked", emotion=decision.pick.emotion,
         item=decision.pick.item_id, platform=platform, scope=scope_id)
    return [with_delivery_timing(output, DELIVERY_AFTER_ASSISTANT)]


def _log(agent: Any, message: str, **fields: Any) -> None:
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    try:
        logger.debug(message, **fields, level=std_logging.DEBUG)
    except TypeError:
        try:
            logger.debug(message)
        except Exception:  # noqa: BLE001 - 日志失败不影响回合
            return
    except Exception:  # noqa: BLE001
        return
