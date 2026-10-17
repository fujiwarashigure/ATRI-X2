"""群知识库（确定性 FAQ）：命中即本地回答，不进模型。

对应 Go：``internal/agent/group_knowledge.go``。

职责：``tryGroupKnowledgeAnswer`` / ``groupKnowledgeEnabledForScope`` /
``cloneGroupKnowledge`` / ``setGroupKnowledgeSnapshot`` / ``groupKnowledgeSnapshot`` /
``groupKnowledgeForScope`` / ``setGroupKnowledgeForScope`` /
``GroupKnowledgeList`` / ``GroupKnowledgeAdd`` / ``GroupKnowledgeRemove`` /
``GroupKnowledgeClear`` / ``GroupKnowledgeTest`` /
``normalizeGroupKnowledgeSnapshot`` / ``normalizeGroupKnowledgeEntry``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``sync.RWMutex`` → :class:`GroupKnowledgeState` 上的
  :class:`threading.Lock`；写路径另有一把 ``write_lock``（Go ``groupKnowledgeWriteMu``）。
* ``tryGroupKnowledgeAnswer`` 保持**同步**（``input.py`` 的调用点没有 await）：
  命中后把"发送答案"排成 asyncio task。Go 里那次发送是同步阻塞的，Python 侧
  没有同步发送原语，因此返回值语义（是否已本地回答）不变，只是发送异步落地。
* ``GroupKnowledge*`` 命令接口按 ``commands/deps.GroupKnowledgeService`` 的契约做成
  ``async``（落盘走 ``save_runtime_state``）。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.config import GroupKnowledgeConfig, GroupKnowledgeEntry
from atri_x.errors import InvalidError
from atri_x.groupkb import MatchKeywords, match, normalize_match_mode

from atri_x.agent.group_policy import context_overflow_key, group_policy_for_scope, is_group_scope
from atri_x.agent.tools import rune_len, service_host, truncate_runes

__all__ = [
    "GroupKnowledgeMixin",
    "GroupKnowledgeState",
    "clone_group_knowledge",
    "group_knowledge_enabled_for_scope",
    "group_knowledge_for_scope",
    "group_knowledge_snapshot",
    "group_knowledge_state",
    "next_knowledge_id",
    "normalize_group_knowledge_snapshot",
    "set_group_knowledge_for_scope",
    "set_group_knowledge_snapshot",
    "try_group_knowledge_answer",
]


@dataclass
class GroupKnowledgeState:
    """群知识库快照视图。对应 Go ``Agent.groupKnowledge`` + 两把锁。

    ``entries`` 直接指向宿主上的 ``agent.group_knowledge``（agent-core 已按 Go 字段名
    建好），锁用 ``group_knowledge_mu`` / ``group_knowledge_write_mu``。
    """

    entries: dict[str, list[GroupKnowledgeEntry]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)


_STATE_ATTR = "_group_knowledge_state"


def group_knowledge_state(agent: Any) -> GroupKnowledgeState:
    """取宿主上的群知识库状态视图。"""
    host = service_host(agent)
    entries = getattr(host, "group_knowledge", None)
    if not isinstance(entries, dict):
        entries = {}
        _safe_set(host, "group_knowledge", entries)
    lock = getattr(host, "group_knowledge_mu", None)
    if lock is None:
        lock = threading.Lock()
        _safe_set(host, "group_knowledge_mu", lock)
    write_lock = getattr(host, "group_knowledge_write_mu", None)
    if write_lock is None:
        write_lock = threading.Lock()
        _safe_set(host, "group_knowledge_write_mu", write_lock)
    return GroupKnowledgeState(entries=entries, lock=lock, write_lock=write_lock)


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


def clone_group_knowledge(
    input_value: dict[str, list[GroupKnowledgeEntry]] | None,
) -> dict[str, list[GroupKnowledgeEntry]]:
    """深拷贝。对应 Go ``cloneGroupKnowledge``。"""
    if not input_value:
        return {}
    out: dict[str, list[GroupKnowledgeEntry]] = {}
    for key, entries in input_value.items():
        key = str(key or "").strip()
        if not key:
            continue
        out[key] = [entry.copy() for entry in entries]
    return out


def set_group_knowledge_snapshot(
    agent: Any, snapshot: dict[str, list[GroupKnowledgeEntry]] | None
) -> None:
    """整表替换（**就地**改宿主上的字典）。对应 Go ``setGroupKnowledgeSnapshot``。"""
    if agent is None:
        return
    state = group_knowledge_state(agent)
    with state.lock:
        state.entries.clear()
        state.entries.update(clone_group_knowledge(snapshot))


def group_knowledge_snapshot(agent: Any) -> dict[str, list[GroupKnowledgeEntry]]:
    """整表副本。对应 Go ``groupKnowledgeSnapshot``。"""
    if agent is None:
        return {}
    state = group_knowledge_state(agent)
    with state.lock:
        return clone_group_knowledge(state.entries)


def group_knowledge_for_scope(agent: Any, scope: Any) -> list[GroupKnowledgeEntry]:
    """单作用域条目副本。对应 Go ``groupKnowledgeForScope``。"""
    if agent is None:
        return []
    key = context_overflow_key(scope)
    state = group_knowledge_state(agent)
    with state.lock:
        return [entry.copy() for entry in state.entries.get(key, [])]


def set_group_knowledge_for_scope(
    agent: Any, scope: Any, entries: list[GroupKnowledgeEntry] | None
) -> None:
    """写回单作用域（空列表即删除条目）。对应 Go ``setGroupKnowledgeForScope``。"""
    if agent is None:
        return
    key = context_overflow_key(scope)
    state = group_knowledge_state(agent)
    with state.lock:
        if not entries:
            state.entries.pop(key, None)
            return
        state.entries[key] = [entry.copy() for entry in entries]


def group_knowledge_enabled_for_scope(agent: Any, scope: Any) -> bool:
    """全局开关 + 群开关。对应 Go ``groupKnowledgeEnabledForScope``。"""
    if agent is None or not _knowledge_config(agent).is_enabled():
        return False
    return group_policy_for_scope(agent, scope).is_knowledge_enabled()


def try_group_knowledge_answer(agent: Any, ctx: Any, text: str) -> bool:
    """普通对话前的本地 FAQ 命中。对应 Go ``tryGroupKnowledgeAnswer``。

    命中即本地回答并"消费"这条消息，返回 ``True``；未命中返回 ``False``，
    调用方继续正常聊天路径。
    """
    if agent is None or not is_group_scope(agent, ctx):
        return False
    scope = _scope(agent, ctx)
    if not group_knowledge_enabled_for_scope(agent, scope):
        return False
    config = _knowledge_config(agent)
    entry, ok = match(
        group_knowledge_for_scope(agent, scope), text, config.max_match_runes
    )
    if not ok:
        return False
    answer = str(entry.answer or "").strip()
    if not answer:
        return False
    _audit(
        agent,
        "group_knowledge_answer",
        platform=scope.platform,
        scope=scope.platform_scope_id,
        actor_id=getattr(_actor(agent, ctx), "id", ""),
        knowledge_id=entry.id,
        result="succeeded",
    )
    _schedule_answer(agent, ctx, scope, entry, answer)
    return True


# ---------------------------------------------------------------------------
# 命令接口
# ---------------------------------------------------------------------------
async def group_knowledge_list(agent: Any, ctx: Any) -> list[GroupKnowledgeEntry]:
    """列出当前群条目副本。对应 Go ``GroupKnowledgeList``。"""
    _require_group_knowledge(agent, ctx)
    return group_knowledge_for_scope(agent, _scope(agent, ctx))


async def group_knowledge_add(
    agent: Any,
    ctx: Any,
    question: str,
    answer: str,
    match_mode: str,
    aliases: list[str] | None = None,
    keywords: list[str] | None = None,
) -> GroupKnowledgeEntry:
    """新增一条确定性条目。对应 Go ``GroupKnowledgeAdd``。"""
    _require_group_knowledge(agent, ctx)
    scope = _scope(agent, ctx)
    config = _knowledge_config(agent).normalized()
    question = str(question or "").strip()
    answer = str(answer or "").strip()
    if not question or not answer:
        raise InvalidError("问题和答案不能为空")
    if config.max_question_runes > 0 and rune_len(question) > config.max_question_runes:
        raise InvalidError("问题超过 %d 个字符" % config.max_question_runes)
    if config.max_answer_runes > 0 and rune_len(answer) > config.max_answer_runes:
        raise InvalidError("答案超过 %d 个字符" % config.max_answer_runes)
    clean_aliases = _normalize_knowledge_list(
        aliases, config.max_aliases, "别名", config.max_question_runes
    )
    clean_keywords = _normalize_knowledge_list(
        keywords, config.max_keywords, "关键词", config.max_question_runes
    )
    mode = normalize_match_mode(match_mode)
    if mode == MatchKeywords and not clean_keywords:
        raise InvalidError("keywords 模式至少需要一个关键词")
    state = group_knowledge_state(agent)
    with state.write_lock:
        previous = group_knowledge_snapshot(agent)
        entries = group_knowledge_for_scope(agent, scope)
        if len(entries) >= config.max_entries_per_scope:
            raise InvalidError(
                "当前群的群知识库已达到 %d 条上限" % config.max_entries_per_scope
            )
        entry = GroupKnowledgeEntry(
            id=next_knowledge_id(entries),
            question=question,
            answer=answer,
            aliases=clean_aliases,
            keywords=clean_keywords,
            match=mode,
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        entries.append(entry)
        set_group_knowledge_for_scope(agent, scope, entries)
        try:
            _save_runtime_state(agent)
        except BaseException:
            set_group_knowledge_snapshot(agent, previous)
            raise
    return entry


async def group_knowledge_remove(agent: Any, ctx: Any, id: str) -> bool:
    """按 ID 删除（大小写不敏感）。对应 Go ``GroupKnowledgeRemove``。"""
    _require_group_knowledge(agent, ctx)
    target = str(id or "").strip()
    if not target:
        raise InvalidError("知识 ID 不能为空")
    scope = _scope(agent, ctx)
    state = group_knowledge_state(agent)
    with state.write_lock:
        previous = group_knowledge_snapshot(agent)
        entries = group_knowledge_for_scope(agent, scope)
        kept = [entry for entry in entries if entry.id.strip().lower() != target.lower()]
        if len(kept) == len(entries):
            return False
        set_group_knowledge_for_scope(agent, scope, kept)
        try:
            _save_runtime_state(agent)
        except BaseException:
            set_group_knowledge_snapshot(agent, previous)
            raise
    return True


async def group_knowledge_clear(agent: Any, ctx: Any) -> int:
    """清空当前群条目，返回删除条数。对应 Go ``GroupKnowledgeClear``。"""
    _require_group_knowledge(agent, ctx)
    scope = _scope(agent, ctx)
    state = group_knowledge_state(agent)
    with state.write_lock:
        previous = group_knowledge_snapshot(agent)
        entries = group_knowledge_for_scope(agent, scope)
        if not entries:
            return 0
        set_group_knowledge_for_scope(agent, scope, [])
        try:
            _save_runtime_state(agent)
        except BaseException:
            set_group_knowledge_snapshot(agent, previous)
            raise
    return len(entries)


async def group_knowledge_test(
    agent: Any, ctx: Any, text: str
) -> tuple[GroupKnowledgeEntry | None, bool]:
    """只跑匹配器、不发送答案。对应 Go ``GroupKnowledgeTest``。"""
    _require_group_knowledge(agent, ctx)
    scope = _scope(agent, ctx)
    return match(
        group_knowledge_for_scope(agent, scope), text, _knowledge_config(agent).max_match_runes
    )


def _require_group_knowledge(agent: Any, ctx: Any) -> None:
    if agent is None:
        raise InvalidError("agent is nil")
    if not _knowledge_config(agent).is_enabled():
        raise InvalidError("群知识库已全局关闭")
    if not is_group_scope(agent, ctx):
        raise InvalidError("群知识库只适用于群聊")


class GroupKnowledgeMixin:
    """Go ``*Agent`` 的群知识库方法组。"""

    def try_group_knowledge_answer(self, ctx: Any, text: str) -> bool:
        return try_group_knowledge_answer(self, ctx, text)

    def group_knowledge_enabled_for_scope(self, scope: Any) -> bool:
        return group_knowledge_enabled_for_scope(self, scope)

    def set_group_knowledge_snapshot(
        self, snapshot: dict[str, list[GroupKnowledgeEntry]] | None
    ) -> None:
        set_group_knowledge_snapshot(self, snapshot)

    def group_knowledge_snapshot(self) -> dict[str, list[GroupKnowledgeEntry]]:
        return group_knowledge_snapshot(self)

    def group_knowledge_for_scope(self, scope: Any) -> list[GroupKnowledgeEntry]:
        return group_knowledge_for_scope(self, scope)

    def set_group_knowledge_for_scope(
        self, scope: Any, entries: list[GroupKnowledgeEntry] | None
    ) -> None:
        set_group_knowledge_for_scope(self, scope, entries)

    async def group_knowledge_list(self, ctx: Any) -> list[GroupKnowledgeEntry]:
        return await group_knowledge_list(self, ctx)

    async def group_knowledge_add(
        self,
        ctx: Any,
        question: str,
        answer: str,
        match_mode: str,
        aliases: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> GroupKnowledgeEntry:
        return await group_knowledge_add(
            self, ctx, question, answer, match_mode, aliases, keywords
        )

    async def group_knowledge_remove(self, ctx: Any, id: str) -> bool:
        return await group_knowledge_remove(self, ctx, id)

    async def group_knowledge_clear(self, ctx: Any) -> int:
        return await group_knowledge_clear(self, ctx)

    async def group_knowledge_test(
        self, ctx: Any, text: str
    ) -> tuple[GroupKnowledgeEntry | None, bool]:
        return await group_knowledge_test(self, ctx, text)


# ---------------------------------------------------------------------------
# 载入时的归一化（state.toml）
# ---------------------------------------------------------------------------
def normalize_group_knowledge_snapshot(
    input_value: dict[str, list[GroupKnowledgeEntry]] | None,
    config: GroupKnowledgeConfig | None,
) -> dict[str, list[GroupKnowledgeEntry]]:
    """state.toml → 内存：截断超限字段、丢无效条目。对应 Go 同名函数。"""
    config = (config or GroupKnowledgeConfig()).normalized()
    out: dict[str, list[GroupKnowledgeEntry]] = {}
    for key, entries in (input_value or {}).items():
        key = str(key or "").strip()
        if not key:
            continue
        clean: list[GroupKnowledgeEntry] = []
        for entry in entries or ():
            normalized = normalize_group_knowledge_entry(entry, config)
            if normalized is None:
                continue
            if not normalized.id:
                normalized.id = next_knowledge_id(clean)
            clean.append(normalized)
            if len(clean) >= config.max_entries_per_scope:
                break
        if clean:
            out[key] = clean
    return out


def normalize_group_knowledge_entry(
    entry: GroupKnowledgeEntry, config: GroupKnowledgeConfig
) -> GroupKnowledgeEntry | None:
    """单条归一化；问题或答案为空（或 keywords 模式无关键词）时丢弃。"""
    out = entry.copy()
    out.id = str(out.id or "").strip()
    out.question = truncate_runes(str(out.question or "").strip(), config.max_question_runes)
    out.answer = truncate_runes(str(out.answer or "").strip(), config.max_answer_runes)
    out.match = normalize_match_mode(out.match)
    out.aliases = _truncate_knowledge_list(
        out.aliases, config.max_aliases, config.max_question_runes
    )
    out.keywords = _truncate_knowledge_list(
        out.keywords, config.max_keywords, config.max_question_runes
    )
    out.updated_at = str(out.updated_at or "").strip()
    if not out.question or not out.answer:
        return None
    if out.match == MatchKeywords and not out.keywords:
        return None
    return out


def next_knowledge_id(entries: list[GroupKnowledgeEntry] | None) -> str:
    """下一个可用 ID（``k1``、``k2``…）。对应 Go ``nextKnowledgeID``。"""
    used = {str(entry.id or "").strip().lower() for entry in entries or ()}
    index = 1
    while True:
        candidate = "k%d" % index
        if candidate not in used:
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _knowledge_config(agent: Any) -> GroupKnowledgeConfig:
    host = service_host(agent)
    config = getattr(host, "group_knowledge_cfg", None)
    if config is None:
        config = getattr(host, "group_knowledge_config", None)
    if config is None:
        services = getattr(host, "services", None)
        if isinstance(services, dict):
            config = services.get("group_knowledge_config")
    if config is None:
        config = GroupKnowledgeConfig()
        _safe_set(host, "group_knowledge_cfg", config)
    return config


def _normalize_knowledge_list(
    values: list[str] | None, max_items: int, label: str, max_runes: int
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        if max_runes > 0 and rune_len(text) > max_runes:
            raise InvalidError("%s超过 %d 个字符" % (label, max_runes))
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if max_items > 0 and len(out) > max_items:
            raise InvalidError("%s超过 %d 个上限" % (label, max_items))
    return out


def _truncate_knowledge_list(
    values: list[str] | None, max_items: int, max_runes: int
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = truncate_runes(str(value or "").strip(), max_runes)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if max_items > 0 and len(out) >= max_items:
            break
    return out


def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import actor

    return actor(agent, ctx)


def _audit(agent: Any, event: str, **attributes: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in attributes.items():
        args.append(key)
        args.append(value)
    audit(*args)


def _save_runtime_state(agent: Any) -> None:
    helper = getattr(agent, "save_runtime_state", None)
    if helper is not None:
        helper()


def _schedule_answer(
    agent: Any, ctx: Any, scope: Any, entry: GroupKnowledgeEntry, answer: str
) -> None:
    """把"发送本地答案"排成 task（Go 里是同步发送，见模块 docstring）。"""
    sender = getattr(agent, "send_chat_with_receipt", None)
    if sender is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（同步调用点/测试）：不能阻塞等待，放弃这次发送。
        return
    loop.create_task(_send_answer(agent, ctx, scope, entry, answer, sender))


async def _send_answer(
    agent: Any, ctx: Any, scope: Any, entry: GroupKnowledgeEntry, answer: str, sender: Any
) -> None:
    try:
        await sender(ctx, answer)
    except BaseException as exc:  # noqa: BLE001 - 与 Go 相同：只记审计
        _audit(
            agent,
            "group_knowledge_send_error",
            platform=scope.platform,
            scope=scope.platform_scope_id,
            knowledge_id=entry.id,
            error=str(exc),
            result="failed",
        )
