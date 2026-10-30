"""聊天漫画的素材收集（P0-2）：只从**当前会话**取消息，并留下可追溯的来源。

与 ``image_generate`` 的 ``format_image_context`` 的区别：

* 那里产出的是"给提示词用的紧凑摘要"（单条约 200 字符、总量 2400）；这里要的是
  **证据包**：保留消息稳定 ID、发言人身份、时间顺序，供计划的 ``source`` 与每格
  的 ``source_message_ids`` 引用。压缩只发生在渲染提示词时。
* 取值范围永远被 ``MessageContext.platform / scope_id`` 钉死，不接受调用方传平台
  或会话参数，因此漫画不会跨聊天取材。
* 锚点（引用消息）不存在时**报错**，不静默退回"最近几条"——那会让用户以为画的是
  他引用的那段。

**不写库**：只读聊天历史仓储；仓储缺失或异常一律翻成面向用户的
:class:`~atri_x.comic.models.ComicContextError`。
"""

from __future__ import annotations

from typing import Any

from atri_x.comic.models import (
    ComicContext,
    ComicContextError,
    ComicContextMessage,
)
from atri_x.platform import message_context_from
from atri_x.storage import (
    ChatHistoryAroundRequest,
    ChatHistorySearchRequest,
    format_time,
)

__all__ = [
    "DEFAULT_CONTEXT_LIMIT",
    "MAX_CONTEXT_MESSAGES",
    "MAX_SOURCE_TEXT_RUNES",
    "collect_comic_context",
    "context_message_from_row",
    "format_context_material",
    "message_key",
    "recent_messages",
    "split_context_terms",
]

#: 默认取多少条聊天素材。
DEFAULT_CONTEXT_LIMIT = 12

#: 一次漫画最多取多少条（再多对一页漫画没有增益，只会挤占提示词）。
MAX_CONTEXT_MESSAGES = 30

#: 单条素材在**规划阶段**保留的字符上限；台词进入最终提示词时另有上限。
MAX_SOURCE_TEXT_RUNES = 500

#: 检索时丢掉的停用词（与 image_generate 的停用词表保持同一取向）。
_STOP_TERMS = frozenset(
    {
        "刚才",
        "那张",
        "上一",
        "一条",
        "群里",
        "之前",
        "上面",
        "聊天",
        "记录",
        "大家",
        "说的",
        "图里",
        "原图",
        "这个",
        "那个",
        "一下",
        "帮我",
        "画一",
        "生成",
        "图片",
        "漫画",
        "六格",
        "四格",
        "分镜",
    }
)


def message_key(row: Any) -> str:
    """一条历史消息的稳定 ID（优先平台消息 ID，退回 ``seq:<n>``）。"""
    platform_message_id = str(getattr(row, "platform_message_id", "") or "").strip()
    if platform_message_id:
        return platform_message_id
    seq = int(getattr(row, "seq", 0) or 0)
    if seq > 0:
        return f"seq:{seq}"
    message_id = str(getattr(row, "id", "") or "").strip()
    if message_id:
        return message_id
    raise ComicContextError("聊天历史里有一条消息没有任何可用 ID，无法追溯来源")


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def context_message_from_row(row: Any) -> ComicContextMessage:
    """把 ``ChatMessage`` 投影成漫画素材（保留原文，不摘要）。"""
    text = _clean(getattr(row, "text", "") or "", MAX_SOURCE_TEXT_RUNES)
    segments = str(getattr(row, "segments", "") or "").strip()
    has_media = segments not in ("", "[]", "null")
    if text == "" and has_media:
        text = "[图片消息]"
    created = getattr(row, "created_at", None)
    return ComicContextMessage(
        message_id=message_key(row),
        seq=int(getattr(row, "seq", 0) or 0),
        sender_id=str(getattr(row, "sender_id", "") or ""),
        sender_name=_clean(getattr(row, "sender_name", "") or "", 40)
        or str(getattr(row, "sender_id", "") or ""),
        text=text,
        created_at=format_time(created) if created is not None else "",
        has_media=has_media,
    )


def split_context_terms(query: str) -> list[str]:
    """把用户的取材提示拆成 LIKE 友好的检索词（中文拆 bigram）。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = (term or "").strip().lower()
        if len(term) < 2 or term in seen or term in _STOP_TERMS:
            return
        seen.add(term)
        out.append(term)

    for field in (query or "").lower().replace("\u3000", " ").split():
        field = field.strip()
        if not field:
            continue
        if not _has_cjk(field):
            add(field)
            continue
        for index in range(len(field) - 1):
            if _has_cjk(field[index]) and _has_cjk(field[index + 1]):
                add(field[index : index + 2])
                if len(out) >= 8:
                    return out[:8]
    return out[:8]


def _has_cjk(text: str) -> bool:
    for ch in text or "":
        code = ord(ch)
        if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF:
            return True
    return False


def recent_messages(
    history: Any, platform: str, scope_id: str, limit: int
) -> list[Any]:
    """最近 ``limit`` 条消息（沿用检索仓储的 ``ORDER BY seq DESC`` 再反转）。

    不用 ``list_range``：那个接口按 ``seq ASC LIMIT`` 返回**最早**的 N 条，
    拿来当"最近聊天"会画成几天前的对话。
    """
    return history.search(
        ChatHistorySearchRequest(
            platform=platform,
            platform_scope_id=scope_id,
            limit=limit,
        )
    )


def collect_comic_context(
    history: Any,
    ctx: Any,
    *,
    anchor_message_id: str = "",
    query: str = "",
    limit: int = 0,
) -> ComicContext:
    """从当前会话收集漫画素材。

    * ``anchor_message_id`` 非空 → 以该消息为中心取窗口；不存在则报错。
    * ``query`` 非空 → 关键词检索；搜不到则报错（不编造来源）。
    * 都为空 → 取最近 ``limit`` 条。
    """
    if history is None:
        raise ComicContextError(
            "聊天历史未启用，漫画需要真实的群聊素材；请先在配置里启用历史存储。"
        )
    msg, ok = message_context_from(ctx)
    if not ok:
        raise ComicContextError("当前上下文没有消息路由信息，漫画只能在真实聊天会话里使用。")
    platform = (msg.platform or "").strip()
    scope_id = (msg.scope_id or "").strip()
    if platform == "" or scope_id == "":
        raise ComicContextError("当前会话缺少平台或会话 ID，无法定位聊天历史。")

    keep = int(limit or 0) or DEFAULT_CONTEXT_LIMIT
    keep = max(1, min(keep, MAX_CONTEXT_MESSAGES))
    # 多取一条用来判断"是否被裁剪"，避免把"刚好取满"误报成截断。
    fetch = min(keep + 1, MAX_CONTEXT_MESSAGES + 1)

    warnings: list[str] = []
    anchor = (anchor_message_id or "").strip()
    if anchor == "current" or anchor == "this":
        anchor = (msg.platform_message_id or "").strip()
    if anchor:
        try:
            rows = history.around(
                ChatHistoryAroundRequest(
                    platform=platform,
                    platform_scope_id=scope_id,
                    platform_message_id=anchor,
                    before=max(1, keep // 2),
                    after=max(0, keep - keep // 2 - 1),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻成面向用户的消息
            raise ComicContextError(
                f"没有找到锚点消息 {anchor}，无法确定要画的聊天范围。"
                "请改用关键词或最近的聊天，不要凭空编造来源。"
            ) from exc
    elif (query or "").strip():
        terms = split_context_terms(query)
        if not terms:
            raise ComicContextError("取材关键词太短或全是停用词，请说得更具体一点。")
        try:
            rows = history.search(
                ChatHistorySearchRequest(
                    platform=platform,
                    platform_scope_id=scope_id,
                    query_terms=terms,
                    query_mode="or",
                    limit=fetch,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ComicContextError(f"检索聊天历史失败：{exc}") from exc
        if not rows:
            raise ComicContextError(
                f"当前聊天里没有检索到与「{query}」相关的消息；请换个关键词，"
                "或直接用最近的聊天。"
            )
    else:
        try:
            rows = recent_messages(history, platform, scope_id, fetch)
        except Exception as exc:  # noqa: BLE001
            raise ComicContextError(f"读取最近聊天失败：{exc}") from exc

    messages: list[ComicContextMessage] = []
    seen: set[str] = set()
    for row in rows or []:
        try:
            item = context_message_from_row(row)
        except ComicContextError:
            continue
        if item.message_id in seen:
            continue
        seen.add(item.message_id)
        if item.text.strip() == "":
            continue
        messages.append(item)

    # 检索结果可能带同一发送者的重复片段；按 seq（缺失时按时间）稳定排序。
    messages.sort(key=lambda item: (item.seq, item.created_at))
    truncated = False
    if len(messages) > keep:
        messages = messages[-keep:]
        truncated = True
    if not messages:
        raise ComicContextError(
            "当前聊天没有可用于漫画的文本消息；请先聊几句，或换一个聊天范围。"
        )

    return ComicContext(
        platform=platform,
        scope_id=scope_id,
        scope_type=str(getattr(msg, "conversation_kind", "") or ""),
        messages=messages,
        anchor_message_id=anchor,
        truncated=truncated,
        warnings=warnings,
    )


def format_context_material(context: ComicContext, *, max_runes: int = 0) -> str:
    """把素材渲染成给规划模型看的文本块（每行带稳定 ID，便于引用）。"""
    lines: list[str] = ["[聊天素材]"]
    total = 0
    for index, item in enumerate(context.messages):
        name = item.sender_name or item.sender_id or "未知"
        line = f"- [{item.message_id}] {name}（{item.sender_id or '无ID'}）：{item.text}"
        total += len(line)
        if max_runes > 0 and total > max_runes:
            remaining = len(context.messages) - index
            lines.append(f"- …（素材过长，后面还有 {remaining} 条未展开）")
            break
        lines.append(line)
    return "\n".join(lines)

