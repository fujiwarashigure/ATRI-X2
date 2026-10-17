"""``image_generate`` 的上下文与角色解析部分。

对应 Go：``internal/tool/builtin/image_generate_context.go``。

这里是"自动模式"的全部逻辑：从 prompt 里认出角色、决定要不要拉当前群聊上下文、
决定要不要走低成本模型的语义改写。它们都不碰生图接口，因此可以单独测试。

与 Go 的差异：

* Go 的 ``imageToolMessage`` 是一个实现了 ``error`` 的私有类型；Python 侧对应
  :class:`ImageToolMessage`，继承自 :class:`atri_x.errors.AtriXError`，``call``
  捕获后把 ``str(exc)`` 放进结果正文（与 Go 一致：失败不抛给执行器）。
* Go 的 ``imageContextStopTerms`` 是 ``map[string]bool``；Python 侧是 ``frozenset``，
  语义相同。
* ``storage.ChatHistorySearchRequest`` 的字段名按本仓的 snake_case 版本构造。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from atri_x.character import Character, ErrForbidden, ErrNotFound, Store, Viewer
from atri_x.character import active_ids as character_active_ids
from atri_x.errors import AtriXError
from atri_x.platform import message_context_from
from atri_x.storage import ChatHistorySearchRequest, ChatMessage

#: 上下文块的总字符上限。对应 Go ``imageContextMaxRunes``。
IMAGE_CONTEXT_MAX_RUNES = 2400

#: 单条消息的字符上限。对应 Go ``imageContextMsgRunes``。
IMAGE_CONTEXT_MSG_RUNES = 200

#: 单张图最多放几个角色。对应 Go ``maxImageCharacters``。
MAX_IMAGE_CHARACTERS = 4


@dataclass
class ImagePromptRewriteRequest:
    """可选 LLM 改写器的输入。对应 Go ``ImagePromptRewriteRequest``。

    目标字段走请求而不是改写器自己的配置：一个改写器实例被所有 profile 共享，
    而选中的 profile 每次调用都可能不同。
    """

    scene: str = ""
    character_name: str = ""
    character_prompt: str = ""
    context_text: str = ""
    library_hints: str = ""
    max_runes: int = 0
    #: TargetModel 是这条提示词面向的生图模型。
    target_model: str = ""
    #: TargetDialect 是该模型理解的提示词语法。
    target_dialect: str = ""
    #: ModelFamily 是方言为 auto 时使用的粗粒度模型族名。
    model_family: str = ""
    #: ProtectedBlocks 是改写器不许碰的提示词片段。它们只被**点名**，让模型知道
    #: 它们存在；真正的文本在改写之后由程序拼进去，从不作为待改写文本发送。
    protected_blocks: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.protected_blocks is None:
            self.protected_blocks = []


class ImagePromptRewriter:
    """把一个场景描述改写成更强的生图提示词。对应 Go ``ImagePromptRewriter``。

    本仓的 ``internal/llm`` 改写器由 app 层注入；这里是协议声明（鸭子类型），
    实现方只需提供 ``async def rewrite_image_prompt(ctx, req) -> str``。
    """

    async def rewrite_image_prompt(
        self, ctx: Any, req: ImagePromptRewriteRequest
    ) -> str:  # pragma: no cover - 协议声明
        raise NotImplementedError


class ImageToolMessage(AtriXError):
    """把面向用户的消息包成正常错误。对应 Go ``imageToolMessage``。

    调用方可以把它显示出来而不必让整次工具调用失败。
    """


def resolve_image_characters(
    tool: Any,
    ctx: Any,
    args: Any,
    prompt: str,
    viewer: Viewer,
    auto: bool,
) -> list[Character]:
    """挑出本次生图要用的全部角色。对应 Go ``resolveImageCharacters``。

    优先级：显式 ``character_id`` / ``character_ids`` > 本轮 ``@char`` 激活的 id >
    显式 ``character_query`` > prompt 里出现的名称/别名。
    """
    store: Store | None = getattr(tool, "characters", None)
    if store is None or not _enabled(store):
        return []
    explicit = explicit_character_ids(args)
    if explicit:
        out: list[Character] = []
        seen: set[str] = set()
        for id_value in explicit:
            if id_value in seen:
                continue
            seen.add(id_value)
            try:
                item = store.get_visible(ctx, id_value, viewer)
            except AtriXError as exc:
                raise image_character_error(id_value, exc) from exc
            out.append(item)
            if len(out) >= MAX_IMAGE_CHARACTERS:
                break
        return out
    active = character_active_ids(ctx)
    if active:
        out = []
        seen = set()
        for id_value in active:
            if id_value in seen:
                continue
            seen.add(id_value)
            try:
                item = store.get_visible(ctx, id_value, viewer)
            except AtriXError:
                continue
            out.append(item)
            if len(out) >= MAX_IMAGE_CHARACTERS:
                break
        if out:
            return out
    query = (getattr(args, "character_query", "") or "").strip()
    explicit_auto = (getattr(args, "character_id", "") or "").strip().lower() == "auto"
    if not auto and not explicit_auto and query == "":
        return []
    items = match_characters_by_name(tool, ctx, prompt, viewer, MAX_IMAGE_CHARACTERS)
    if items:
        return items
    if query == "":
        query = prompt
    if query.strip() == "":
        return []
    results = store.search(ctx, query, "", "", MAX_IMAGE_CHARACTERS, viewer)
    if not results:
        return []
    out = []
    seen = set()
    for result in results:
        if result.id in seen:
            continue
        seen.add(result.id)
        try:
            item = store.get_visible(ctx, result.id, viewer)
        except AtriXError:
            continue
        out.append(item)
    return out


def explicit_character_ids(args: Any) -> list[str]:
    """对应 Go ``explicitCharacterIDs``。"""
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = (value or "").strip()
        if value == "" or value.lower() == "auto" or value in seen:
            return
        seen.add(value)
        ids.append(value)

    add(getattr(args, "character_id", "") or "")
    for value in getattr(args, "character_ids", None) or []:
        add(value)
    return ids


def match_characters_by_name(
    tool: Any, ctx: Any, prompt: str, viewer: Viewer, limit: int
) -> list[Character]:
    """找出名称或别名出现在 prompt 里的可见角色。对应 Go ``matchCharactersByName``。

    长名字先匹配，这样短别名不会遮住完整名字；已经匹配过的片段在测试更短的关键词
    之前会被抹掉。
    """
    store: Store | None = getattr(tool, "characters", None)
    if store is None:
        return []
    if limit <= 0:
        limit = MAX_IMAGE_CHARACTERS
    items = store.list(ctx, viewer)
    candidates: list[tuple[str, Character]] = []
    seen_keys: set[str] = set()
    for item in items or []:
        for name in [item.name, *(item.aliases or [])]:
            key = (name or "").strip().lower()
            if len(key) < 2 or key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append((key, item))
    # Go 的 sort.SliceStable：先按 rune 长度降序，同长度按 key 升序。
    candidates.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    folded = prompt.lower()
    out: list[Character] = []
    seen_ids: set[str] = set()
    for key, item in candidates:
        if item.id in seen_ids or key not in folded:
            continue
        seen_ids.add(item.id)
        out.append(item)
        if len(out) >= limit:
            break
        folded = folded.replace(key, " ")
    return out


def image_character_names(items: list[Character]) -> str:
    """对应 Go ``imageCharacterNames``。"""
    names = [item.name for item in items if item is not None and (item.name or "").strip()]
    return "、".join(names)


def image_character_prompts(items: list[Character]) -> str:
    """对应 Go ``imageCharacterPrompts``。"""
    parts: list[str] = []
    for item in items:
        if item is None:
            continue
        preset = (item.image_prompt() or "").strip()
        if preset == "":
            continue
        if len(items) > 1:
            parts.append(f"角色 {item.name}（{item.id}）：\n{preset}")
            continue
        parts.append(preset)
    return "\n\n".join(parts)


def image_character_summary(items: list[Character]) -> str:
    """对应 Go ``imageCharacterSummary``。"""
    parts = [f"{item.name}({item.id})" for item in items if item is not None]
    return "、".join(parts)


def image_character_error(id_value: str, exc: BaseException) -> BaseException:
    """对应 Go ``imageCharacterError``。"""
    if exc is ErrNotFound:
        return ImageToolMessage(f"没有找到角色 {id_value}，可先用 character_list 查询。")
    if exc is ErrForbidden:
        return ImageToolMessage(f"角色 {id_value} 不可访问。")
    return exc


def image_context_query(tool: Any, args: Any, prompt: str, auto: bool) -> tuple[str, bool]:
    """决定要不要拉群聊上下文。对应 Go ``imageContextQuery``。"""
    raw = (getattr(args, "context_query", "") or "").strip()
    cfg = getattr(tool, "cfg", None)
    if raw == "":
        if not auto or not getattr(cfg, "auto_context", False) or not looks_like_image_context(prompt):
            return "", False
        raw = "auto"
    folded = raw.lower()
    if folded in ("off", "none", "false", "0"):
        return "", False
    if folded == "auto":
        return prompt, True
    return raw, True


#: 触发"拉上下文"的词。对应 Go ``imageContextTriggers``。
IMAGE_CONTEXT_TRIGGERS = (
    "刚才",
    "上一条",
    "那条",
    "那张",
    "上一张",
    "之前",
    "上面",
    "群里",
    "大家",
    "聊天",
    "记录",
    "他说的",
    "她说的",
    "图里",
    "原图",
    "this image",
    "the previous",
    "earlier",
    "above",
    "group chat",
)


def looks_like_image_context(prompt: str) -> bool:
    """对应 Go ``looksLikeImageContext``。"""
    folded = (prompt or "").lower()
    return any(trigger.lower() in folded for trigger in IMAGE_CONTEXT_TRIGGERS)


def search_image_context(tool: Any, ctx: Any, query: str, limit: int) -> list[ChatMessage]:
    """查当前聊天历史。对应 Go ``searchImageContext``。"""
    history = getattr(tool, "history", None)
    if history is None:
        return []
    msg, ok = message_context_from(ctx)
    if not ok or (msg.platform or "").strip() == "" or (msg.scope_id or "").strip() == "":
        return []
    cfg = getattr(tool, "cfg", None)
    if limit <= 0:
        limit = int(getattr(cfg, "context_default_limit", 0) or 0)
    try:
        rows = history.search(
            ChatHistorySearchRequest(
                platform=msg.platform,
                platform_scope_id=msg.scope_id,
                query_terms=split_image_context_terms(query),
                query_mode="or",
                limit=limit,
            )
        )
    except BaseException:  # noqa: BLE001 - Go 只吞掉错误并返回空
        return []
    return rows or []


#: 检索时丢掉的停用词。对应 Go ``imageContextStopTerms``。
IMAGE_CONTEXT_STOP_TERMS = frozenset(
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
        "我这",
        "我们",
    }
)

#: ``strings.FieldsFunc`` 的分隔符集合。对应 Go ``splitImageContextTerms`` 里的 switch。
_IMAGE_CONTEXT_SEPARATORS = re.compile(
    r"[\s,\uff0c\u3002!\uff01?\uff1f;\uff1b|\u3001\"'()\uff08\uff09]+"
)


def split_image_context_terms(query: str) -> list[str]:
    """构造 LIKE 友好的检索词。对应 Go ``splitImageContextTerms``。

    中文查询会拆成 bigram，因为历史检索用的是纯子串匹配。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = (term or "").strip().lower()
        if len(term) < 2 or term in seen or term in IMAGE_CONTEXT_STOP_TERMS:
            return
        seen.add(term)
        out.append(term)

    fields = _IMAGE_CONTEXT_SEPARATORS.split((query or "").lower())
    for field in fields:
        if not has_cjk(field):
            add(field)
            continue
        for index in range(len(field) - 1):
            if not is_cjk_rune(field[index]) or not is_cjk_rune(field[index + 1]):
                continue
            add(field[index : index + 2])
            if len(out) >= 8:
                return out
    if len(out) > 8:
        out = out[:8]
    return out


def has_cjk(text: str) -> bool:
    """对应 Go ``hasCJK``。"""
    return any(is_cjk_rune(ch) for ch in text or "")


def is_cjk_rune(ch: str) -> bool:
    """对应 Go ``isCJKRune``。"""
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def format_image_context(rows: list[ChatMessage]) -> str:
    """把检索到的消息渲染成紧凑的块。对应 Go ``formatImageContext``。"""
    if not rows:
        return ""
    lines = ["[参考对话]"]
    total = 0
    for row in rows:
        text = " ".join((row.text or "").strip().split())
        if text == "":
            continue
        if len(text) > IMAGE_CONTEXT_MSG_RUNES:
            text = text[:IMAGE_CONTEXT_MSG_RUNES] + "..."
        name = (row.sender_name or "").strip() or row.sender_id
        line = "- " + _format_created_at(row.created_at) + " " + name + ": " + text
        total += len(line)
        if total > IMAGE_CONTEXT_MAX_RUNES:
            break
        lines.append(line)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _format_created_at(value: datetime | None) -> str:
    """Go 的 ``CreatedAt.Format("01-02 15:04")``。"""
    if value is None:
        return ""
    return value.strftime("%m-%d %H:%M")


def should_rewrite_image_prompt(tool: Any, args: Any, prompt: str, context_text: str) -> bool:
    """要不要跑 LLM 语义改写。对应 Go ``shouldRewriteImagePrompt``。"""
    cfg = getattr(tool, "cfg", None)
    mode = (str(getattr(cfg, "optimize_rewrite", "") or "")).strip().lower()
    rewrite = getattr(args, "rewrite", None)
    if rewrite is not None:
        mode = "always" if rewrite else "off"
    if mode == "always":
        return True
    if mode == "auto":
        min_runes = int(getattr(cfg, "optimize_rewrite_min_runes", 0) or 0)
        return len((prompt or "").strip()) < min_runes or (context_text or "").strip() != ""
    return False


def _enabled(store: Any) -> bool:
    checker = getattr(store, "enabled", None)
    return bool(checker()) if callable(checker) else store is not None


__all__ = [name for name in dir() if not name.startswith("_")]
