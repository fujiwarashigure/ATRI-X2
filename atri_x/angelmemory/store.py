"""scope-local 长期记忆的 SQLite store。

对应 Go：``internal/angelmemory/store.go``（建表/迁移见
:mod:`atri_x.angelmemory.schema`，模型见 :mod:`atri_x.angelmemory.models`，
上层限流与上下文构造见 :mod:`atri_x.angelmemory.service`）。

关键语义（与 Go 逐条一致，不能简化）：

1. **来源**：写入时记录 ``source_kind`` / ``source_actor_id`` / ``source_message_id``
   / ``source_session_id``；命令与工具都能读到记忆 ID 与来源。
2. **唯一键**是 ``(platform, scope_id, content)``：同一条内容再次写入走
   ``ON CONFLICT DO UPDATE``（强度取 ``MAX``，``created_at`` 与 ``id`` 保留）。
3. **scope 隔离**：``get`` / ``resolve_id`` / ``delete_scoped`` 都带
   ``platform + scope_id`` 条件，前缀解析永不越出 scope。
4. **按来源删除**：``delete_by_source`` 要求至少一个过滤字段，命中不到任何旧行
   （旧行没有结构化来源）——这正是"绝不误删"的实现方式。
5. **旧版回填**：``backfill_legacy_source_kind`` 只回填"写入方固定"的 ``source_kind``，
   绝不发明成员/消息/Session。
6. **平台撤回**：按「平台 + scope + source_message_id」删除派生记忆，即
   ``delete_by_source(platform, scope_id, SourceFilter(message_id=...))``。

并发：Go 的 ``db.SetMaxOpenConns(1)`` + 显式事务在本仓对应
:class:`threading.RLock` + 单连接 + ``BEGIN IMMEDIATE``，因此
「查存在 → 数条数 → 插入」整段是原子的（见 Go 的
``TestRememberPerScopeLimitIsAtomicUnderConcurrency``）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from atri_x.angelmemory.models import (
    LegacySourceBackfill,
    Memory,
    RecallQuery,
    SourceFilter,
    StoreLimits,
)
from atri_x.angelmemory.schema import (
    DEFAULT_MAX_CONTENT_RUNES,
    DEFAULT_MAX_PER_SCOPE,
    DEFAULT_RECALL_LIMIT,
    LEGACY_TOOL_SOURCE_LABEL,
    SELECT_MEMORY_COLUMNS,
    like_prefix_pattern,
    migrate,
    normalize_tags,
    row_to_memory,
    source_filter_clause,
    truncate_source_text,
)
from atri_x.errors import (
    ContentTooLongError,
    ErrAmbiguousID,
    ErrNotFound,
    ForbiddenError,
    InvalidError,
    ScopeFullError,
)
from atri_x.storage.ids import new_id
from atri_x.storage.times import format_time, now as storage_now
from atri_x.textmatch import keywords, like_pattern

__all__ = ["Store", "open_store", "Open"]

#: 来源标签的 rune 上限（Go ``truncateSourceText(memory.Source, 200)``）。
_SOURCE_LABEL_MAX_RUNES = 200
#: ``source_kind`` 的 rune 上限（Go 用 40）。
_SOURCE_KIND_MAX_RUNES = 40


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：异常一律回滚。对应 Go 的 ``BeginTx`` + ``defer Rollback``。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 回滚失败时保留原始异常
            pass
        raise
    connection.execute("COMMIT")


class Store:
    """长期记忆的 SQLite store。对应 Go ``Store``。

    所有公开方法都持有同一把可重入锁：SQLite 连接是单条的，语句级串行化不足以
    保护「读-判断-写」序列。
    """

    def __init__(self, connection: sqlite3.Connection, limits: StoreLimits | None = None) -> None:
        self._db = connection
        self._lock = threading.RLock()
        self.limits = limits or StoreLimits(
            max_content_runes=DEFAULT_MAX_CONTENT_RUNES,
            max_per_scope=DEFAULT_MAX_PER_SCOPE,
        )

    # -- 生命周期 -----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接。对应 Go 的 ``Store.db``（测试夹具与诊断用）。"""
        return self._db

    def set_limits(self, limits: StoreLimits) -> None:
        """应用写入上限。对应 Go ``Store.SetLimits``。

        设计上只在 store 构造期、并发使用之前调用一次。零值字段回落默认值。
        """
        with self._lock:
            if limits.max_content_runes <= 0:
                limits.max_content_runes = DEFAULT_MAX_CONTENT_RUNES
            if limits.max_per_scope <= 0:
                limits.max_per_scope = DEFAULT_MAX_PER_SCOPE
            self.limits = limits

    def close(self) -> None:
        """关闭连接（幂等）。对应 Go ``Store.Close``。"""
        with self._lock:
            if self._db is None:
                return
            connection, self._db = self._db, None
        try:
            connection.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败不应阻塞调用方
            pass

    # -- 写入 ---------------------------------------------------------------

    def remember(self, memory: Memory | None) -> Memory:
        """写入（或更新）一条记忆并返回落库后的行。对应 Go ``Store.Remember``。

        与 Go 一样**原地规范化**传入的 :class:`Memory`（trim、标签归一、来源截断、
        强度/ID/时间填充），随后重新读回并返回库里的那一行（``id`` / ``created_at``
        以库为准）。
        """
        with self._lock:
            self._require_open()
            if memory is None:
                raise InvalidError("angel memory is required")
            memory.platform = (memory.platform or "").strip()
            memory.scope_id = (memory.scope_id or "").strip()
            memory.content = (memory.content or "").strip()
            memory.tags = normalize_tags(memory.tags)
            memory.source = truncate_source_text(memory.source, _SOURCE_LABEL_MAX_RUNES)
            memory.source_kind = truncate_source_text(memory.source_kind, _SOURCE_KIND_MAX_RUNES)
            memory.source_actor_id = truncate_source_text(memory.source_actor_id, _SOURCE_LABEL_MAX_RUNES)
            memory.source_message_id = truncate_source_text(
                memory.source_message_id, _SOURCE_LABEL_MAX_RUNES
            )
            memory.source_session_id = truncate_source_text(
                memory.source_session_id, _SOURCE_LABEL_MAX_RUNES
            )
            if memory.platform == "" or memory.scope_id == "" or memory.content == "":
                raise InvalidError("angel memory platform, scope and content are required")
            max_content = self.limits.max_content_runes
            if max_content > 0 and len(memory.content) > max_content:
                raise ContentTooLongError(
                    f"angel memory content is too long: {len(memory.content)} runes"
                    f" (max {max_content})"
                )
            # 「查存在 → 数条数 → 插入」必须是一个原子单元：单连接只串行化单条语句，
            # 没有事务时两个并发写入方会同时看到"还剩一个名额"。
            with _transaction(self._db):
                max_per_scope = self.limits.max_per_scope
                if max_per_scope > 0:
                    existing = self._db.execute(
                        "SELECT id FROM memories WHERE platform = ? AND scope_id = ? AND content = ?",
                        (memory.platform, memory.scope_id, memory.content),
                    ).fetchone()
                    if existing is None:
                        count = self._db.execute(
                            "SELECT COUNT(*) FROM memories WHERE platform = ? AND scope_id = ?",
                            (memory.platform, memory.scope_id),
                        ).fetchone()[0]
                        if count >= max_per_scope:
                            raise ScopeFullError(
                                f"angel memory scope limit reached: {count} entries"
                                f" (max {max_per_scope})"
                            )
                if memory.strength <= 0:
                    memory.strength = 50
                if memory.strength > 100:
                    memory.strength = 100
                if memory.id == "":
                    memory.id = new_id()
                moment = storage_now()
                if memory.created_at is None:
                    memory.created_at = moment
                if memory.updated_at is None:
                    memory.updated_at = moment
                if memory.last_accessed_at is None:
                    memory.last_accessed_at = moment
                self._db.execute(
                    """
INSERT INTO memories (id, platform, scope_id, content, tags, strength, source, source_kind, source_actor_id, source_message_id, source_session_id, created_at, updated_at, last_accessed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(platform, scope_id, content) DO UPDATE SET
    tags = excluded.tags,
    strength = MAX(memories.strength, excluded.strength),
    source = excluded.source,
    source_kind = excluded.source_kind,
    source_actor_id = excluded.source_actor_id,
    source_message_id = excluded.source_message_id,
    source_session_id = excluded.source_session_id,
    updated_at = excluded.updated_at,
    last_accessed_at = excluded.last_accessed_at""",
                    (
                        memory.id,
                        memory.platform,
                        memory.scope_id,
                        memory.content,
                        memory.tags,
                        memory.strength,
                        memory.source,
                        memory.source_kind,
                        memory.source_actor_id,
                        memory.source_message_id,
                        memory.source_session_id,
                        format_time(memory.created_at),
                        format_time(memory.updated_at),
                        format_time(memory.last_accessed_at),
                    ),
                )
            try:
                rows = self.recall(
                    RecallQuery(
                        platform=memory.platform,
                        scope_id=memory.scope_id,
                        text=memory.content,
                        limit=1,
                    )
                )
            except Exception:  # Go: `if err != nil { return memory, nil }`
                return memory
            for row in rows:
                if row.content == memory.content:
                    return row
            return memory

    # -- 读取 ---------------------------------------------------------------

    def recall(self, query: RecallQuery) -> list[Memory]:
        """按关键词召回，空查询退化为「强度 + 更新时间」排序。对应 Go ``Store.Recall``。

        召回命中的行会刷新 ``last_accessed_at``。
        """
        with self._lock:
            self._require_open()
            platform = (query.platform or "").strip()
            scope_id = (query.scope_id or "").strip()
            if platform == "" or scope_id == "":
                raise InvalidError("angel memory recall platform and scope are required")
            limit = query.limit
            if limit <= 0:
                limit = DEFAULT_RECALL_LIMIT
            if limit > 50:
                limit = 50
            text = (query.text or "").strip()
            terms = keywords(text)
            memories: list[Memory]
            if not terms:
                rows = self._db.execute(
                    SELECT_MEMORY_COLUMNS
                    + " WHERE platform = ? AND scope_id = ?"
                    " ORDER BY strength DESC, updated_at DESC LIMIT ?",
                    (platform, scope_id, limit),
                ).fetchall()
                memories = [row_to_memory(row) for row in rows]
            else:
                where_parts: list[str] = []
                score_parts: list[str] = []
                where_params: list[object] = []
                score_params: list[object] = []
                for term in terms:
                    pattern = like_pattern(term)
                    where_parts.append(
                        "(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
                    )
                    where_params.extend((pattern, pattern))
                    score_parts.append(
                        "(CASE WHEN content LIKE ? ESCAPE '\\' THEN 2 ELSE 0 END"
                        " + CASE WHEN tags LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
                    )
                    score_params.extend((pattern, pattern))
                params: list[object] = [platform, scope_id]
                params.extend(where_params)
                params.extend(score_params)
                params.append(limit)
                statement = (
                    SELECT_MEMORY_COLUMNS
                    + " WHERE platform = ? AND scope_id = ? AND ("
                    + " OR ".join(where_parts)
                    + ") ORDER BY ("
                    + " + ".join(score_parts)
                    + ") DESC, strength DESC, updated_at DESC LIMIT ?"
                )
                rows = self._db.execute(statement, params).fetchall()
                memories = [row_to_memory(row) for row in rows]

            if not memories and query.allow_fallback and text != "" and terms:
                fallback_limit = limit
                if fallback_limit > 3:
                    fallback_limit = 3
                rows = self._db.execute(
                    SELECT_MEMORY_COLUMNS
                    + " WHERE platform = ? AND scope_id = ? AND strength >= 60"
                    " ORDER BY strength DESC, updated_at DESC LIMIT ?",
                    (platform, scope_id, fallback_limit),
                ).fetchall()
                memories = [row_to_memory(row) for row in rows]

            if memories:
                self._touch(memories)
            return memories

    def _touch(self, memories: list[Memory]) -> None:
        """刷新 ``last_accessed_at``；失败时静默（Go 里返回 memories 而不是错误）。"""
        try:
            stamp = format_time(storage_now())
            for memory in memories:
                self._db.execute(
                    "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                    (stamp, memory.id),
                )
        except sqlite3.Error:  # pragma: no cover - 与 Go 的 `return memories, nil` 对应
            return

    def get(self, platform: str, scope_id: str, id: str) -> Memory:
        """按精确 ID 取一条 scope 内记忆。对应 Go ``Store.Get``（不存在抛 ``ErrNotFound``）。"""
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            id = (id or "").strip()
            if platform == "" or scope_id == "" or id == "":
                raise InvalidError("angel memory platform, scope and id are required")
            row = self._db.execute(
                SELECT_MEMORY_COLUMNS + " WHERE platform = ? AND scope_id = ? AND id = ?",
                (platform, scope_id, id),
            ).fetchone()
            if row is None:
                raise ErrNotFound
            return row_to_memory(row)

    def list(self, platform: str, scope_id: str, filter: SourceFilter | None, limit: int) -> list[Memory]:
        """列出 scope 内记忆，可按结构化来源过滤。对应 Go ``Store.List``。"""
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            if platform == "" or scope_id == "":
                raise InvalidError("angel memory list platform and scope are required")
            if limit <= 0:
                limit = 20
            if limit > 1000:
                limit = 1000
            clause, params = source_filter_clause(filter)
            args: list[object] = [platform, scope_id, *params, limit]
            rows = self._db.execute(
                SELECT_MEMORY_COLUMNS
                + " WHERE platform = ? AND scope_id = ?"
                + clause
                + " ORDER BY strength DESC, updated_at DESC LIMIT ?",
                args,
            ).fetchall()
            return [row_to_memory(row) for row in rows]

    def resolve_id(
        self, platform: str, scope_id: str, prefix: str, filter: SourceFilter | None
    ) -> str:
        """把唯一 ID 前缀解析成完整 ID，永不越出 scope。对应 Go ``Store.ResolveID``。

        没有匹配抛 ``ErrNotFound``；匹配多于一条抛 ``ErrAmbiguousID``。
        """
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            prefix = (prefix or "").strip()
            if platform == "" or scope_id == "" or prefix == "":
                raise InvalidError("angel memory platform, scope and id prefix are required")
            clause, params = source_filter_clause(filter)
            args: list[object] = [platform, scope_id, *params, like_prefix_pattern(prefix)]
            rows = self._db.execute(
                "SELECT id FROM memories WHERE platform = ? AND scope_id = ?"
                + clause
                + " AND id LIKE ? ESCAPE '\\' ORDER BY id LIMIT 20",
                args,
            ).fetchall()
            matches = [row[0] for row in rows]
            if not matches:
                raise ErrNotFound
            if len(matches) > 1:
                raise ErrAmbiguousID
            return matches[0]

    def count(self, platform: str, scope_id: str) -> int:
        """统计 scope 内记忆条数。对应 Go ``Store.Count``。"""
        with self._lock:
            if self._db is None:
                return 0
            row = self._db.execute(
                "SELECT COUNT(*) FROM memories WHERE platform = ? AND scope_id = ?",
                ((platform or "").strip(), (scope_id or "").strip()),
            ).fetchone()
            return int(row[0])

    # -- 删除 ---------------------------------------------------------------

    def delete_scoped(self, platform: str, scope_id: str, id: str) -> bool:
        """只在该 scope 内删除一条记忆。对应 Go ``Store.DeleteScoped``。"""
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            id = (id or "").strip()
            if platform == "" or scope_id == "" or id == "":
                raise InvalidError("angel memory platform, scope and id are required")
            cursor = self._db.execute(
                "DELETE FROM memories WHERE id = ? AND platform = ? AND scope_id = ?",
                (id, platform, scope_id),
            )
            return cursor.rowcount > 0

    def delete_by_source(
        self, platform: str, scope_id: str, filter: SourceFilter | None
    ) -> int:
        """按结构化来源删除 scope 内记忆，返回删除行数。对应 Go ``Store.DeleteBySource``。

        至少需要一个过滤字段，否则抛 :class:`InvalidError` —— 畸形调用不能清空整个
        scope。旧行（没有结构化来源）永远匹配不到。

        平台撤回事件即调用本方法：
        ``delete_by_source(platform, scope_id, SourceFilter(message_id=...))``。
        """
        with self._lock:
            self._require_open()
            platform = (platform or "").strip()
            scope_id = (scope_id or "").strip()
            if platform == "" or scope_id == "":
                raise InvalidError("angel memory delete platform and scope are required")
            clause, params = source_filter_clause(filter)
            if clause == "":
                raise InvalidError("angel memory source filter is required")
            args: list[object] = [platform, scope_id, *params]
            cursor = self._db.execute(
                "DELETE FROM memories WHERE platform = ? AND scope_id = ?" + clause, args
            )
            return int(cursor.rowcount or 0)

    def delete(self, id: str) -> None:
        """按 ID 全局删除（无 scope 限制）。对应 Go ``Store.Delete``。"""
        with self._lock:
            self._require_open()
            self._db.execute("DELETE FROM memories WHERE id = ?", ((id or "").strip(),))

    def delete_before(self, cutoff) -> int:
        """删除 ``updated_at`` 早于 cutoff 的记忆，返回行数。对应 Go ``Store.DeleteBefore``。"""
        with self._lock:
            if self._db is None:
                return 0
            cursor = self._db.execute(
                "DELETE FROM memories WHERE updated_at < ?", (format_time(cutoff),)
            )
            return int(cursor.rowcount or 0)

    # -- 旧版来源回填 -------------------------------------------------------

    def legacy_source_backfill_stats(self) -> LegacySourceBackfill:
        """统计库级来源覆盖率。对应 Go ``Store.LegacySourceBackfillStats``。"""
        with self._lock:
            self._require_open()
            row = self._db.execute(
                """
SELECT COUNT(*),
       SUM(CASE WHEN source_actor_id <> '' OR source_message_id <> '' OR source_session_id <> '' THEN 1 ELSE 0 END),
       SUM(CASE WHEN source_kind = '' AND source = ? THEN 1 ELSE 0 END)
FROM memories""",
                (LEGACY_TOOL_SOURCE_LABEL,),
            ).fetchone()
            stats = LegacySourceBackfill()
            stats.total = int(row[0] or 0)
            stats.linked = int(row[1] or 0)
            stats.backfillable = int(row[2] or 0)
            return stats

    def backfill_legacy_source_kind(self) -> int:
        """回填旧行的 ``source_kind``，返回更新行数（幂等）。对应 Go
        ``Store.BackfillLegacySourceKind``。

        只搬运"写入方已固定其含义"的标签（旧版唯一生产写入方写的是 ``"tool"``），
        **绝不**发明成员 / 消息 / Session id，因此这些行依旧被按来源删除跳过。
        """
        with self._lock:
            self._require_open()
            cursor = self._db.execute(
                "UPDATE memories SET source_kind = ? WHERE source_kind = '' AND source = ?",
                (LEGACY_TOOL_SOURCE_LABEL, LEGACY_TOOL_SOURCE_LABEL),
            )
            return int(cursor.rowcount or 0)

    # -- 工具层 helper（供 angel_forget 使用） ------------------------------

    def resolve_own(
        self, platform: str, scope_id: str, prefix: str, actor_id: str
    ) -> Memory:
        """解析"来源成员 = 当前发言人"的单条记忆。对应 Go ``angel_forget`` 的
        ``AngelForgetTool.resolve`` 里的库层部分。

        过滤器就是权限边界：只有记录到的 ``source_actor_id`` 等于当前发言人的记忆
        才可能被解析出来；结构化来源出现之前的旧行（``source_actor_id`` 为空）
        永远不匹配。解析到之后**再核对一次**来源成员，不一致抛
        :data:`atri_x.errors.ErrForbidden`。
        """
        actor_id = (actor_id or "").strip()
        if actor_id == "":
            raise InvalidError("angel memory forget requires an actor id")
        memory_id = self.resolve_id(
            platform, scope_id, prefix, SourceFilter(actor_id=actor_id)
        )
        memory = self.get(platform, scope_id, memory_id)
        if memory.source_actor_id.strip() == "" or memory.source_actor_id != actor_id:
            raise ForbiddenError("这条记忆的来源成员不是当前用户，已拒绝删除。")
        return memory

    def _require_open(self) -> None:
        if self._db is None:
            raise InvalidError("angel memory store is not open")


def open_store(path: str) -> Store:
    """打开（并按需建库）长期记忆 SQLite。对应 Go ``angelmemory.Open``。"""
    path = (path or "").strip()
    if path == "":
        raise InvalidError("angel memory sqlite path is empty")
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o755, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        migrate(connection)
    except BaseException:
        connection.close()
        raise
    return Store(
        connection,
        StoreLimits(
            max_content_runes=DEFAULT_MAX_CONTENT_RUNES,
            max_per_scope=DEFAULT_MAX_PER_SCOPE,
        ),
    )


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
Open = open_store
