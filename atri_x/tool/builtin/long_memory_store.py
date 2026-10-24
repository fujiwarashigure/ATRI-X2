"""长期记忆的 SQLite 索引、文件回填与检索。

对应 Go：``internal/tool/builtin/long_memory.go`` 里的 ``longMemoryStore``
（``ensureLocked`` / ``syncLocked`` / ``indexRecordLocked`` / ``search*`` /
``save`` / ``update`` / ``delete`` / ``formatSearchResult`` 等）。

拆分来源：``long_memory.go``（1181 行）→ 本模块承载持久化与检索，
:mod:`atri_x.tool.builtin.long_memory_model` 承载模型与文本，
:mod:`atri_x.tool.builtin.long_memory` 承载三个工具。

与 Go 的差异：

* ``database/sql`` + ``modernc.org/sqlite`` → 标准库 :mod:`sqlite3`；
  ``sync.Mutex`` → :class:`threading.Lock`（同步 SQLite 路径，PORTING.md §1.6）。
* ``os.OpenRoot`` 之类不需要：记忆文件名由本模块自己生成（``%06d-slug.md``）。
* FTS5 不可用时**不报错**，与 Go 一致地回落到 LIKE 扫描（``fts_available=False``）。
* Go 的 ``ctx.Err()`` 取消检查改为 :func:`atri_x.tool.builtin.common.cancel_token_from_ctx`
  + ``is_cancelled``；ctx 不是取消令牌时视为永不取消。
"""

from __future__ import annotations

import glob
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from atri_x.errors import InvalidError
from atri_x.tool.builtin.common import cancel_token_from_ctx
from atri_x.utils.fileops import (
    Edit,
    content_revision,
    edit_file,
    normalize_edit_text,
    read_file,
    split_lines,
    unified_diff,
)
from atri_x.tool.builtin.long_memory_model import (
    LONG_MEMORY_SYNC_COOLDOWN_SECONDS,
    LongMemoryInvalidFile,
    LongMemoryRecord,
    LongMemorySaveArgs,
    LongMemorySearchArgs,
    LongMemoryUpdateArgs,
    LongMemoryUpdatePreview,
    clean_long_memory_text,
    format_memory_id,
    long_memory_fts_query,
    long_memory_search_tokens,
    long_memory_text_matches,
    marshal_long_memory_file,
    normalize_long_memory_limit,
    normalize_long_memory_match_mode,
    parse_long_memory_file,
    parse_long_memory_text,
    record_from_parsed_file,
    slug_long_memory_title,
    split_long_memory_keywords,
    tokenize_long_memory,
    validate_long_memory_record,
)
from atri_x.tool.builtin.long_memory_scope import (
    GLOBAL_SCOPE,
    check_record_access,
    current_actor_id,
    current_scope_key,
    resolve_scope_filter,
    scope_allows,
    scope_label,
    scope_origin_line,
)

#: ``memory_files`` 的列顺序（对应 Go 的两条 SELECT 列表）。
_RECORD_COLUMNS = (
    "id",
    "file_path",
    "category",
    "title",
    "summary",
    "keywords",
    "created_at",
    "updated_at",
    "source_scope",
    "source_actor",
    "file_mtime_ns",
    "file_size",
)

#: 索引表里后加的「来源会话」列（老库用 ALTER TABLE 补上）。
_SCOPE_COLUMNS = ("source_scope", "source_actor")

_WARNING_HEADER = "长期记忆格式警告\n以下文件无法同步到索引，请手动修复：\n"
_REPAIR_ADVICE = (
    "修复 +++ 之间的 TOML 元数据；若无法修复，可用 long_memory_write 新建记忆，"
    "然后从原文件手动复制正文。"
)
_SEPARATOR = "=" * 30


class LongMemoryStore:
    """全局长期记忆的存储与索引。对应 Go ``longMemoryStore``。"""

    def __init__(self, root_dir: str) -> None:
        root_dir = (root_dir or "").strip()
        self.root_dir = root_dir
        self.memories_dir = os.path.join(root_dir, "memories")
        self.index_path = os.path.join(root_dir, "index.db")
        self._lock = threading.Lock()
        self.db: sqlite3.Connection | None = None
        self.fts_available = False
        self.initialized = False
        self.last_sync = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭索引连接。对应 Go ``longMemoryStore.close``。"""
        with self._lock:
            if self.db is None:
                return
            try:
                self.db.close()
            finally:
                self.db = None
                self.initialized = False
                self.fts_available = False

    def _ensure(self, ctx: Any) -> None:
        """建目录、开库、建表。对应 Go ``ensureLocked``。"""
        if self.initialized:
            return
        if self.root_dir.strip() == "":
            raise InvalidError("long memory directory is not configured")
        try:
            os.makedirs(self.memories_dir, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise InvalidError(
                f"create long memory directory {self.memories_dir!r}: {exc}"
            ) from exc
        try:
            db = sqlite3.connect(self.index_path)
        except sqlite3.Error as exc:
            raise InvalidError(f"open long memory index: {exc}") from exc
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """CREATE TABLE IF NOT EXISTS memory_files (
                    id INTEGER PRIMARY KEY,
                    file_path TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_scope TEXT NOT NULL DEFAULT '',
                    source_actor TEXT NOT NULL DEFAULT '',
                    file_mtime_ns INTEGER NOT NULL,
                    file_size INTEGER NOT NULL
                )"""
            )
            self._ensure_scope_columns(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS memory_invalid_files (
                    file_path TEXT PRIMARY KEY,
                    error TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    file_mtime_ns INTEGER NOT NULL,
                    file_size INTEGER NOT NULL
                )"""
            )
        except sqlite3.Error as exc:
            db.close()
            raise InvalidError(f"create long memory metadata table: {exc}") from exc
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                "category, title, summary, keywords, content_tokens, content_id UNINDEXED)"
            )
            self.fts_available = True
        except sqlite3.Error:
            self.fts_available = False
        self.db = db
        self.initialized = True

    @staticmethod
    def _ensure_scope_columns(db: sqlite3.Connection) -> None:
        """给老索引库补「来源会话」列（幂等）。

        1.0.5 之前建的 ``memory_files`` 没有 ``source_scope`` / ``source_actor``；
        升级后第一次打开索引时补列，已有行默认空串 = 旧数据全局可见。只加列、
        不改数据，因此老库可以直接原地升级。
        """
        rows = db.execute("PRAGMA table_info(memory_files)").fetchall()
        existing = {row[1] for row in rows}
        for name in _SCOPE_COLUMNS:
            if name in existing:
                continue
            db.execute(
                f"ALTER TABLE memory_files ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
        db.commit()

    def _connection(self) -> sqlite3.Connection:
        db = self.db
        if db is None:  # pragma: no cover - 调用方都先 _ensure
            raise InvalidError("long memory index is not open")
        return db

    @staticmethod
    def _check_cancelled(ctx: Any) -> None:
        token = cancel_token_from_ctx(ctx)
        if token is None:
            return
        checker = getattr(token, "is_cancelled", None)
        if callable(checker) and checker():
            raise InvalidError("long memory operation was cancelled")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def save(self, ctx: Any, args: LongMemorySaveArgs) -> LongMemoryRecord:
        """新建一条记忆。对应 Go ``longMemoryStore.save``。

        「按会话隔离」：记录自动带上当前会话作用域与主体（拿不到会话上下文时为空，
        等价于旧的全局记忆）。
        """
        self._check_cancelled(ctx)
        record = LongMemoryRecord(
            category=clean_long_memory_text(args.category),
            title=clean_long_memory_text(args.title),
            summary=clean_long_memory_text(args.summary),
            keywords=" ".join(split_long_memory_keywords(args.keywords)),
            content=(args.content or "").strip(),
            source_scope=current_scope_key(ctx),
            source_actor=current_actor_id(ctx),
        )
        with self._lock:
            self._ensure(ctx)
            now = _utc_rfc3339()
            record.id = self._next_id()
            record.created_at = now
            record.updated_at = now
            record.file_path = os.path.join(
                self.memories_dir,
                f"{format_memory_id(record.id)}-{slug_long_memory_title(record.title)}.md",
            )
            validate_long_memory_record(record, True)
            self._write_record_file(record)
            self._index_record(ctx, record)
            return record

    def update(self, ctx: Any, args: LongMemoryUpdateArgs) -> LongMemoryRecord:
        """按预检结果落盘并重建索引。对应 Go ``longMemoryStore.update``。"""
        self._check_cancelled(ctx)
        with self._lock:
            preview = self._preview_update(ctx, args)
            self._write_update_preview(ctx, preview)
            self._index_record(ctx, preview.after)
            return preview.after

    def delete(self, ctx: Any, id: int, scope: str = "") -> str:
        """永久删除一条记忆及其索引。对应 Go ``longMemoryStore.delete``。

        ``scope`` 是跨会话操作时的显式来源作用域（仅超级管理员可用），
        与检索参数 ``scope`` 语义一致。
        """
        self._check_cancelled(ctx)
        if id <= 0:
            raise InvalidError("id is required")
        with self._lock:
            self._ensure(ctx)
            record = self._record_by_id(id)
            if record is None:
                self._sync(ctx, True)
                record = self._record_by_id(id)
            if record is None:
                raise InvalidError(f"未找到 ID 为 {id} 的长期记忆")
            check_record_access(ctx, record.source_scope, scope)
            try:
                os.remove(record.file_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise InvalidError(
                    f"delete memory file {record.file_path!r}: {exc}"
                ) from exc
            self._delete_index(ctx, id, record.file_path)
            return record.file_path

    # ------------------------------------------------------------------
    # 预检
    # ------------------------------------------------------------------
    def preview_update(
        self, ctx: Any, args: LongMemoryUpdateArgs
    ) -> LongMemoryUpdatePreview:
        """对应 Go ``longMemoryStore.previewUpdate``。"""
        self._check_cancelled(ctx)
        with self._lock:
            return self._preview_update(ctx, args)

    def _preview_update(
        self, ctx: Any, args: LongMemoryUpdateArgs
    ) -> LongMemoryUpdatePreview:
        """对应 Go ``previewUpdateLocked``。"""
        if args.id <= 0:
            raise InvalidError("id is required")
        if args.content is not None and len(args.content_edits) > 0:
            raise InvalidError("content and content_edits cannot be used together")
        if not args.has_changes():
            raise InvalidError(
                "update requires at least one changed field or content_edits"
            )
        self._ensure(ctx)
        record = self._record_by_id(args.id)
        if record is None:
            self._sync(ctx, True)
            record = self._record_by_id(args.id)
        if record is None:
            raise InvalidError(f"未找到 ID 为 {args.id} 的长期记忆")
        check_record_access(ctx, record.source_scope, args.scope)

        file = read_file(record.file_path, "")
        try:
            parsed = parse_long_memory_text(file.text)
        except InvalidError as exc:
            raise InvalidError(
                "无法更新：记忆文件格式损坏，工具不会覆盖原文件。\n"
                + long_memory_repair_advice(record.file_path, exc)
            ) from exc
        record = record_from_parsed_file(
            record.file_path, parsed, record.file_mtime_ns, record.file_size
        )

        nxt = LongMemoryRecord(**vars(record))
        changes: list[str] = []
        if args.category is not None:
            old = nxt.category
            nxt.category = clean_long_memory_text(args.category)
            changes = append_long_memory_change(changes, "分类", old, nxt.category)
        if args.title is not None:
            old = nxt.title
            nxt.title = clean_long_memory_text(args.title)
            changes = append_long_memory_change(changes, "标题", old, nxt.title)
        if args.summary is not None:
            old = nxt.summary
            nxt.summary = clean_long_memory_text(args.summary)
            changes = append_long_memory_change(changes, "摘要", old, nxt.summary)
        if args.keywords is not None:
            old = nxt.keywords
            nxt.keywords = " ".join(split_long_memory_keywords(args.keywords))
            changes = append_long_memory_change(changes, "关键词", old, nxt.keywords)
        if args.content is not None:
            old = nxt.content
            nxt.content = (args.content or "").strip()
            changes = append_long_memory_change(changes, "正文", old, nxt.content)
        if len(args.content_edits) > 0:
            from atri_x.utils.fileops import apply_edits

            try:
                content = apply_edits(nxt.content, args.content_edits)
            except BaseException as exc:  # noqa: BLE001 - fileops 的错误统一上抛
                raise InvalidError(str(exc)) from exc
            old = nxt.content
            nxt.content = (content or "").strip()
            changes = append_long_memory_change(changes, "正文", old, nxt.content)
        validate_long_memory_record(nxt, True)
        if len(changes) == 0:
            raise InvalidError("edit produced no changes")
        nxt.updated_at = _utc_rfc3339()
        if nxt.created_at == "":
            nxt.created_at = nxt.updated_at
        data = marshal_long_memory_file(nxt)
        old_text = normalize_edit_text(file.text)
        new_text = normalize_edit_text(data.decode("utf-8"))
        if old_text == new_text:
            raise InvalidError("edit produced no changes")
        return LongMemoryUpdatePreview(
            before=record,
            after=nxt,
            file=file,
            full_content=data.decode("utf-8"),
            diff=unified_diff(
                record.file_path,
                split_lines(old_text),
                split_lines(new_text),
                3,
            ),
            changes=changes,
        )

    def _write_update_preview(self, ctx: Any, preview: LongMemoryUpdatePreview) -> None:
        """对应 Go ``writeUpdatePreviewLocked``（overwrite + revision 校验）。"""
        edit = Edit(operation="overwrite", new_text=preview.full_content)
        edit_file(
            preview.after.file_path,
            preview.file.encoding,
            content_revision(preview.file.bytes),
            False,
            False,
            3,
            [edit],
        )
        self._check_cancelled(ctx)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(self, ctx: Any, args: LongMemorySearchArgs) -> str:
        """检索并渲染结果。对应 Go ``longMemoryStore.search``。

        「按会话隔离」：默认只检索当前会话 + 未归属会话的旧数据；``args.scope``
        传 ``"all"`` 或具体作用域键时跨会话（仅超级管理员）。
        """
        self._check_cancelled(ctx)
        keywords = (args.keywords or "").strip()
        category = clean_long_memory_text(args.category)
        if keywords == "" and category == "":
            raise InvalidError("keywords 和 category 不能同时为空")
        brief_only = True if args.brief_only is None else bool(args.brief_only)
        limit = normalize_long_memory_limit(args.limit)
        match_mode = normalize_long_memory_match_mode(args.match_mode)
        target_scope, cross_scope = resolve_scope_filter(ctx, args.scope)
        with self._lock:
            self._ensure(ctx)
            self._sync(ctx, False)
            invalids = self._invalid_files(5)
            overview = self._category_overview(target_scope, cross_scope)
            records = self._search_fts(
                keywords, category, match_mode, limit, target_scope, cross_scope
            )
            if records is None or not self.fts_available:
                records = self._search_like(
                    ctx, keywords, category, match_mode, limit, target_scope, cross_scope
                )
            return self._format_search_result(
                invalids,
                overview,
                records,
                brief_only,
                cross_scope,
                _overview_header(target_scope, cross_scope),
            )

    def categories(self, ctx: Any, scope: str = "") -> str:
        """返回分类目录。对应 Go ``longMemoryStore.categories``。"""
        self._check_cancelled(ctx)
        target_scope, cross_scope = resolve_scope_filter(ctx, scope)
        with self._lock:
            self._ensure(ctx)
            self._sync(ctx, False)
            invalids = self._invalid_files(5)
            overview = self._category_overview(target_scope, cross_scope)
            parts: list[str] = []
            write_invalid_warning(parts, invalids)
            if overview.strip() == "":
                parts.append("当前长期记忆库为空，尚未建立任何分类。你可以自由创建新的分类。")
                return "".join(parts).strip()
            parts.append("当前可用的长期记忆分类目录\n")
            parts.append(overview)
            return "".join(parts).strip()

    # ------------------------------------------------------------------
    # 索引维护
    # ------------------------------------------------------------------
    def _sync(self, ctx: Any, force: bool) -> None:
        """把 ``memories/*.md`` 回填进索引。对应 Go ``syncLocked``。"""
        now = time.monotonic()
        if (
            not force
            and self.last_sync != 0.0
            and now - self.last_sync < LONG_MEMORY_SYNC_COOLDOWN_SECONDS
        ):
            return
        self.last_sync = now
        try:
            entries = sorted(os.listdir(self.memories_dir))
        except OSError as exc:
            raise InvalidError(
                f"read long memory directory {self.memories_dir!r}: {exc}"
            ) from exc
        seen: dict[str, bool] = {}
        seen_ids: dict[int, str] = {}
        for name in entries:
            if os.path.splitext(name)[1].lower() != ".md":
                continue
            path = os.path.join(self.memories_dir, name)
            if os.path.isdir(path):
                continue
            seen[path] = True
            try:
                stat = os.stat(path)
            except OSError as exc:
                raise InvalidError(f"stat long memory file {path!r}: {exc}") from exc
            mtime_ns = int(stat.st_mtime_ns)
            size = int(stat.st_size)
            indexed = self._file_state_by_path(path)
            if (
                indexed is not None
                and indexed.file_mtime_ns == mtime_ns
                and indexed.file_size == size
            ):
                continue
            try:
                parsed = parse_long_memory_file(path)
                record = record_from_parsed_file(path, parsed, mtime_ns, size)
                validate_long_memory_record(record, False)
            except InvalidError as exc:
                self._upsert_invalid(path, str(exc), mtime_ns, size)
                continue
            previous = seen_ids.get(record.id)
            if previous is not None and previous != path:
                self._upsert_invalid(
                    path,
                    f"id {record.id} 与文件 {previous} 重复",
                    mtime_ns,
                    size,
                )
                continue
            seen_ids[record.id] = path
            self._index_record(ctx, record)

        for indexed in self._indexed_paths():
            if not seen.get(indexed.file_path):
                self._delete_index(ctx, indexed.id, indexed.file_path)
        for path in self._invalid_paths():
            if not seen.get(path):
                db = self._connection()
                db.execute(
                    "DELETE FROM memory_invalid_files WHERE file_path = ?", (path,)
                )
        self._connection().commit()

    def _write_record_file(self, record: LongMemoryRecord) -> None:
        """原子写记忆文件。对应 Go ``writeRecordFileLocked``。"""
        directory = os.path.dirname(record.file_path)
        try:
            os.makedirs(directory, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise InvalidError(f"create long memory file dir: {exc}") from exc
        data = marshal_long_memory_file(record)
        tmp = record.file_path + ".tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            raise InvalidError(f"write temp long memory file {tmp!r}: {exc}") from exc
        try:
            os.replace(tmp, record.file_path)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise InvalidError(
                f"replace long memory file {record.file_path!r}: {exc}"
            ) from exc

    def _index_record(self, ctx: Any, record: LongMemoryRecord) -> None:
        """写 ``memory_files`` 与 FTS 行。对应 Go ``indexRecordLocked``。"""
        db = self._connection()
        try:
            stat = os.stat(record.file_path)
        except OSError as exc:
            raise InvalidError(
                f"stat long memory file {record.file_path!r}: {exc}"
            ) from exc
        record.file_mtime_ns = int(stat.st_mtime_ns)
        record.file_size = int(stat.st_size)
        try:
            db.execute(
                """INSERT INTO memory_files
                   (id, file_path, category, title, summary, keywords,
                    created_at, updated_at, source_scope, source_actor,
                    file_mtime_ns, file_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     file_path=excluded.file_path, category=excluded.category,
                     title=excluded.title, summary=excluded.summary,
                     keywords=excluded.keywords, created_at=excluded.created_at,
                     updated_at=excluded.updated_at,
                     source_scope=excluded.source_scope,
                     source_actor=excluded.source_actor,
                     file_mtime_ns=excluded.file_mtime_ns, file_size=excluded.file_size""",
                (
                    record.id,
                    record.file_path,
                    record.category,
                    record.title,
                    record.summary,
                    record.keywords,
                    record.created_at,
                    record.updated_at,
                    record.source_scope,
                    record.source_actor,
                    record.file_mtime_ns,
                    record.file_size,
                ),
            )
        except sqlite3.Error as exc:
            raise InvalidError(f"index long memory metadata: {exc}") from exc
        if self.fts_available:
            try:
                db.execute("DELETE FROM memory_fts WHERE content_id = ?", (record.id,))
                db.execute(
                    "INSERT INTO memory_fts (category, title, summary, keywords, "
                    "content_tokens, content_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        tokenize_long_memory(record.category),
                        tokenize_long_memory(record.title),
                        tokenize_long_memory(record.summary),
                        " ".join(split_long_memory_keywords(record.keywords)),
                        tokenize_long_memory(record.content),
                        record.id,
                    ),
                )
            except sqlite3.Error:
                self.fts_available = False
        try:
            db.execute(
                "DELETE FROM memory_invalid_files WHERE file_path = ?",
                (record.file_path,),
            )
        except sqlite3.Error as exc:
            raise InvalidError(f"clear long memory invalid state: {exc}") from exc
        db.commit()

    def _delete_index(self, ctx: Any, id: int, path: str) -> None:
        """对应 Go ``deleteIndexLocked``。"""
        db = self._connection()
        if self.fts_available:
            try:
                db.execute("DELETE FROM memory_fts WHERE content_id = ?", (id,))
            except sqlite3.Error:
                self.fts_available = False
        try:
            db.execute("DELETE FROM memory_files WHERE id = ?", (id,))
        except sqlite3.Error as exc:
            raise InvalidError(f"delete long memory metadata: {exc}") from exc
        try:
            db.execute("DELETE FROM memory_invalid_files WHERE file_path = ?", (path,))
        except sqlite3.Error as exc:
            raise InvalidError(f"delete long memory invalid state: {exc}") from exc
        db.commit()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _file_state_by_path(self, path: str) -> LongMemoryRecord | None:
        """对应 Go ``fileStateByPathLocked``（无行时返回 ``None``）。"""
        db = self._connection()
        row = db.execute(
            "SELECT id, file_path, file_mtime_ns, file_size FROM memory_files "
            "WHERE file_path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return LongMemoryRecord(
            id=row["id"],
            file_path=row["file_path"],
            file_mtime_ns=row["file_mtime_ns"],
            file_size=row["file_size"],
        )

    def _record_by_id(self, id: int) -> LongMemoryRecord | None:
        """对应 Go ``recordByIDLocked``（无行时返回 ``None``）。"""
        db = self._connection()
        row = db.execute(
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM memory_files WHERE id = ?",
            (id,),
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def _indexed_paths(self) -> list[LongMemoryRecord]:
        """对应 Go ``indexedPathsLocked``。"""
        rows = self._connection().execute(
            "SELECT id, file_path FROM memory_files"
        ).fetchall()
        return [
            LongMemoryRecord(id=row["id"], file_path=row["file_path"]) for row in rows
        ]

    def _invalid_paths(self) -> list[str]:
        """对应 Go ``invalidPathsLocked``。"""
        rows = self._connection().execute(
            "SELECT file_path FROM memory_invalid_files"
        ).fetchall()
        return [row["file_path"] for row in rows]

    def _upsert_invalid(self, path: str, reason: str, mtime_ns: int, size: int) -> None:
        """对应 Go ``upsertInvalidLocked``。"""
        db = self._connection()
        try:
            db.execute(
                """INSERT INTO memory_invalid_files
                   (file_path, error, detected_at, file_mtime_ns, file_size)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET error=excluded.error,
                     detected_at=excluded.detected_at,
                     file_mtime_ns=excluded.file_mtime_ns,
                     file_size=excluded.file_size""",
                (path, reason, _utc_rfc3339(), mtime_ns, size),
            )
        except sqlite3.Error as exc:
            raise InvalidError(f"record invalid long memory {path!r}: {exc}") from exc
        db.commit()

    def _invalid_files(self, limit: int) -> list[LongMemoryInvalidFile]:
        """对应 Go ``invalidFilesLocked``。"""
        rows = self._connection().execute(
            "SELECT file_path, error, detected_at, file_mtime_ns, file_size "
            "FROM memory_invalid_files ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            LongMemoryInvalidFile(
                file_path=row["file_path"],
                error=row["error"],
                detected_at=row["detected_at"],
                file_mtime_ns=row["file_mtime_ns"],
                file_size=row["file_size"],
            )
            for row in rows
        ]

    def _category_overview(
        self, target_scope: str = "", cross_scope: bool = True
    ) -> str:
        """对应 Go ``categoryOverviewLocked``（按会话隔离时只统计可见记录）。"""
        sql_text = "SELECT category, COUNT(*) AS n FROM memory_files"
        params: list[Any] = []
        if (target_scope or "").strip() != GLOBAL_SCOPE:
            sql_text += " WHERE (source_scope = '' OR source_scope = ?)"
            params.append(target_scope)
        sql_text += " GROUP BY category ORDER BY n DESC, category ASC"
        rows = self._connection().execute(sql_text, params).fetchall()
        return "\n".join(f"-{row['category']}（共{row['n']}条记录）" for row in rows)

    def _search_fts(
        self,
        keywords: str,
        category: str,
        match_mode: str,
        limit: int,
        target_scope: str = "",
        cross_scope: bool = True,
    ) -> list[LongMemoryRecord] | None:
        """FTS5 检索。不可用时返回 ``None``。对应 Go ``searchFTSLocked``。"""
        if not self.fts_available:
            return None
        tokens = long_memory_search_tokens(keywords)
        sql_text = (
            "SELECT f.id, f.file_path, f.category, f.title, f.summary, f.keywords, "
            "f.created_at, f.updated_at, f.source_scope, f.source_actor, "
            "f.file_mtime_ns, f.file_size "
            "FROM memory_files f JOIN memory_fts x ON x.content_id = f.id WHERE 1=1"
        )
        params: list[Any] = []
        if tokens:
            sql_text += " AND memory_fts MATCH ?"
            params.append(long_memory_fts_query(tokens, match_mode))
        if category != "":
            sql_text += " AND f.category = ?"
            params.append(category)
        if (target_scope or "").strip() != GLOBAL_SCOPE:
            sql_text += " AND (f.source_scope = '' OR f.source_scope = ?)"
            params.append(target_scope)
        if tokens:
            sql_text += " ORDER BY rank, f.updated_at DESC LIMIT ?"
        else:
            sql_text += " ORDER BY f.updated_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self._connection().execute(sql_text, params).fetchall()
        except sqlite3.Error:
            self.fts_available = False
            return None
        return [_row_to_record(row) for row in rows]

    def _search_like(
        self,
        ctx: Any,
        keywords: str,
        category: str,
        match_mode: str,
        limit: int,
        target_scope: str = "",
        cross_scope: bool = True,
    ) -> list[LongMemoryRecord]:
        """无 FTS 时的扫描回落。对应 Go ``searchLikeLocked``。"""
        sql_text = (
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM memory_files "
            "WHERE (? = '' OR category = ?)"
        )
        params: list[Any] = [category, category]
        if (target_scope or "").strip() != GLOBAL_SCOPE:
            sql_text += " AND (source_scope = '' OR source_scope = ?)"
            params.append(target_scope)
        sql_text += " ORDER BY updated_at DESC"
        rows = self._connection().execute(sql_text, params).fetchall()
        candidates = [_row_to_record(row) for row in rows]
        tokens = long_memory_search_tokens(keywords)
        matched: list[LongMemoryRecord] = []
        for record in candidates:
            if not tokens:
                matched.append(record)
            else:
                self._check_cancelled(ctx)
                content = " ".join(
                    (record.category, record.title, record.summary, record.keywords)
                )
                try:
                    parsed = parse_long_memory_file(record.file_path)
                    content += " " + parsed.content
                except InvalidError:
                    pass
                if long_memory_text_matches(content, tokens, match_mode):
                    matched.append(record)
            if len(matched) >= limit:
                break
        return matched

    def _format_search_result(
        self,
        invalids: list[LongMemoryInvalidFile],
        overview: str,
        records: list[LongMemoryRecord],
        brief_only: bool,
        cross_scope: bool = False,
        header: str = "全库分类概览",
    ) -> str:
        """对应 Go ``formatSearchResult``（多一列「来源会话」，仅跨会话检索时显示）。"""
        parts: list[str] = []
        write_invalid_warning(parts, invalids)
        if overview == "":
            parts.append(f"{header}：当前长期记忆库为空，尚未建立任何分类。\n")
        else:
            parts.append(f"{header}\n")
            parts.append(overview)
            parts.append("\n")
        parts.append(_SEPARATOR)
        parts.append("\n")
        if not records:
            parts.append(
                "长期记忆库中未找到相关内容。建议检查关键词是否过于严格，"
                "尝试 match_mode=OR，或参考分类概览调整分类。"
            )
            return "".join(parts).strip()
        parts.append(f"成功检索到{len(records)}条记录\n")
        invalid_set = {item.file_path for item in invalids}
        for record in records:
            parts.append(
                f"\n记忆ID：{record.id}\n分类：{record.category}\n标题：{record.title}\n"
                f"摘要：{record.summary}\n创建时间：{record.created_at}\n"
                f"更新时间：{record.updated_at}\n文件：{record.file_path}\n"
            )
            parts.append(scope_origin_line(record.source_scope, cross_scope))
            if record.file_path in invalid_set:
                parts.append("注意：该文件当前格式损坏，以上元数据可能来自上次成功索引。\n")
            if not brief_only:
                try:
                    parsed = parse_long_memory_file(record.file_path)
                except InvalidError:
                    parts.append(
                        "详细内容：文件格式损坏，无法安全读取正文。请按格式警告修复。\n"
                    )
                else:
                    parts.append("详细内容（正文行号从 1 开始，可用于 content_edits）：\n")
                    write_numbered_content(parts, parsed.content)
        return "".join(parts).strip()

    def _next_id(self) -> int:
        """取下一个可用 ID（跳过已存在文件占用的号）。对应 Go ``nextIDLocked``。"""
        row = self._connection().execute("SELECT MAX(id) FROM memory_files").fetchone()
        max_id = 0 if row is None or row[0] is None else int(row[0])
        id = max_id + 1
        while True:
            pattern = os.path.join(self.memories_dir, f"{format_memory_id(id)}-*.md")
            if not glob.glob(pattern):
                return id
            id += 1


# ---------------------------------------------------------------------------
# 渲染助手
# ---------------------------------------------------------------------------
def _overview_header(target_scope: str, cross_scope: bool) -> str:
    """分类概览标题：区分「全库」与「当前会话可见」，避免隔离后仍写"全库"。"""
    if (target_scope or "").strip() == GLOBAL_SCOPE:
        return "全库分类概览"
    if cross_scope:
        return f"会话 {scope_label(target_scope)} 分类概览"
    return "当前会话可见分类概览"


def write_numbered_content(parts: list[str], content: str) -> None:
    """行号 + ``|`` 渲染正文。对应 Go ``writeNumberedLongMemoryContent``。"""
    lines = split_lines(content)
    if not lines:
        parts.append("  (空)\n")
        return
    width = len(str(len(lines)))
    for index, line in enumerate(lines):
        parts.append(f"{index + 1:>{width}} | {line}\n")


def write_invalid_warning(
    parts: list[str], invalids: list[LongMemoryInvalidFile]
) -> None:
    """坏文件警告块。对应 Go ``writeInvalidLongMemoryWarning``。"""
    if not invalids:
        return
    parts.append(_WARNING_HEADER)
    for invalid in invalids:
        parts.append(f"- {invalid.file_path}\n  原因：{invalid.error}\n  建议：{_REPAIR_ADVICE}\n")
    parts.append(_SEPARATOR)
    parts.append("\n")


def long_memory_repair_advice(path: str, err: BaseException) -> str:
    """对应 Go ``longMemoryRepairAdvice``。"""
    return (
        f"损坏文件：{path}\n原因：{err}\n处理建议：请修复文件开头 +++ 到结束 +++ "
        "之间的 TOML 元数据；如果无法修复，请用 long_memory_write 新建记忆，"
        "再从原文件手动复制正文。原文件未被修改。"
    )


def append_long_memory_change(
    changes: list[str], label: str, old_value: str, new_value: str
) -> list[str]:
    """对应 Go ``appendLongMemoryChange``。"""
    if old_value == new_value:
        return changes
    changes.append(
        f"{label}：{long_memory_inline_value(old_value)} -> "
        f"{long_memory_inline_value(new_value)}"
    )
    return changes


def long_memory_inline_value(value: str) -> str:
    """对应 Go ``longMemoryInlineValue``。"""
    value = clean_long_memory_text(value)
    return "(空)" if value == "" else value


def _row_to_record(row: sqlite3.Row) -> LongMemoryRecord:
    return LongMemoryRecord(
        id=row["id"],
        file_path=row["file_path"],
        category=row["category"],
        title=row["title"],
        summary=row["summary"],
        keywords=row["keywords"],
        content="",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_scope=row["source_scope"],
        source_actor=row["source_actor"],
        file_mtime_ns=row["file_mtime_ns"],
        file_size=row["file_size"],
    )


def _utc_rfc3339() -> str:
    """UTC RFC3339（秒精度），与 Go ``time.Now().UTC().Format(time.RFC3339)`` 一致。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


__all__ = [name for name in dir() if not name.startswith("_")]
