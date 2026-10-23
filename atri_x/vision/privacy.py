"""识图隐私开关：图片**要不要外发**给视觉/上游模型。

对应 Go：无（Go 版没有把图片外发做成可关闭的开关）。Python 侧两条独立开关：

* ``allow_external_images`` —— 总开关。关掉后任何图片都不发往上游；
* ``allow_group_images`` —— 群聊开关。总开关开着、但当前是群聊作用域时，
  群聊里的图片同样不外发。

它与 :class:`atri_x.vision.service.Options` 的关系是**两层防线**：

* 服务层（:class:`atri_x.vision.service.Service`）在打开上游连接**之前**就按
  :class:`PrivacySettings` 与 ``Options`` 拒绝，抛
  :class:`atri_x.vision.service_types.PrivacyBlockedError`；
* 工具层（``view_image`` / ``image_to_prompt``）不经服务层的那条路径也要拒绝，
  所以它们查同一个进程级配置。

配置只有一处来源（:func:`configure_privacy`，由装配层在启动时调用一次）；
没被配置时两个开关都是 ``True``，行为与改造前完全一致。这里用
``threading.Lock`` 而不是 asyncio 锁：装配发生在事件循环之外，读侧在两种
上下文里都会被调用（PORTING.md §1.6）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IMAGES_NOT_SENT_TEXT",
    "PrivacySettings",
    "blocked_reason",
    "configure_privacy",
    "current_privacy",
    "ctx_is_group_chat",
    "reset_privacy",
    "scope_is_group",
]


#: 前端聊天路径替换图片段用的占位文本。用户明确要求的就是这一句。
IMAGES_NOT_SENT_TEXT = "[图片未外发：外部识图已关闭]"

#: 群聊作用域的 ID 前缀（与 :func:`atri_x.agent.group_policy.is_group_scope` 同义）。
_GROUP_PREFIXES = ("group:", "supergroup:")


@dataclass(frozen=True)
class PrivacySettings:
    """两个开关的不可变快照。"""

    allow_external_images: bool = True
    allow_group_images: bool = True


_LOCK = threading.Lock()
_SETTINGS = PrivacySettings()


def configure_privacy(
    allow_external_images: bool = True, allow_group_images: bool = True
) -> PrivacySettings:
    """设置进程级开关并返回生效值。装配层启动时调用一次即可。"""
    global _SETTINGS
    settings = PrivacySettings(
        allow_external_images=bool(allow_external_images),
        allow_group_images=bool(allow_group_images),
    )
    with _LOCK:
        _SETTINGS = settings
    return settings


def current_privacy() -> PrivacySettings:
    """当前生效的开关快照（未配置时两个都是 True）。"""
    with _LOCK:
        return _SETTINGS


def reset_privacy() -> None:
    """恢复默认（两个开关都打开）。测试用。"""
    configure_privacy()


def scope_is_group(scope_id: str) -> bool:
    """``platform_scope_id`` 是否群聊作用域。"""
    value = str(scope_id or "").strip()
    return value.startswith(_GROUP_PREFIXES)


def ctx_is_group_chat(ctx: Any) -> bool:
    """从 ctx 的聊天历史上下文判断当前是否群聊。

    工具拿不到 agent，但都拿得到 ctx；:func:`atri_x.tool.builtin.chat_history.
    current_chat_history_context` 能给出 ``scope_id``。取不到时按「不是群聊」
    处理（宁可放行，也不因为读不到上下文而把所有私聊图片都拒掉）。
    """
    try:
        from atri_x.tool.builtin.chat_history import current_chat_history_context

        chat, err = current_chat_history_context(ctx)
    except Exception:  # noqa: BLE001 - 上下文未装配时按私聊处理
        return False
    if err:
        return False
    return scope_is_group(str(getattr(chat, "scope_id", "") or ""))


def blocked_reason(*, is_group: bool = False, settings: PrivacySettings | None = None) -> str:
    """当前是否禁止外发图片；禁止时返回一句可读原因，允许时返回空串。"""
    active = settings or current_privacy()
    if not active.allow_external_images:
        return "外部识图已关闭"
    if is_group and not active.allow_group_images:
        return "群聊外部识图已关闭"
    return ""
