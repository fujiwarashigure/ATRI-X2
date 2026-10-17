"""SQLite store：连接、事务与门面。对应 Go：``internal/storage/sqlite/store.go``。

职责与 Go 的 ``New`` 一致：建库目录、打开库、``PRAGMA foreign_keys = ON``、跑 migration、
把各 repository 装配到一个门面上；外加 Go 里由 ``*sql.DB`` 提供的连接与事务语义。

**并发语义（与 Go 对齐）**：Go 侧有多处 ``db.SetMaxOpenConns(1)``，把连接池压到单连接，
所有操作因此被 ``database/sql`` 串行化。本实现用
"单条 :class:`sqlite3.Connection`（``check_same_thread=False``）+ 一把 :class:`threading.RLock`"
复现同一语义：每个操作、以及每个显式事务的**全程**都在锁内，任意时刻只有一个线程碰连接。

**PRAGMA 与上游严格一致**：Go 只执行 ``PRAGMA foreign_keys = ON``，**没有**设置
``journal_mode`` / ``synchronous`` / ``busy_timeout``。本模块默认同样不设置；
三个开关作为显式参数提供、**默认关闭**（``journal_mode=""`` / ``synchronous=""`` /
``busy_timeout_ms=0``），需要时由调用方显式打开。

> 出处：《ATRI-X Python/Go 研究报告》§4.3 把这三项列为"与语言无关的性能杠杆"，
> 并指出上游全仓都没做：默认 DELETE 日志 → WAL 在 Go 侧是 **+11.4x** 写吞吐（848 → 9,704 行/s）、
> 在 Python 侧 +6.1x；"逐行事务 → 整批一个事务" 是 **+121x（Go）/ +147x（Python）**；
> "``db.QueryRow`` → 复用预备语句" 是 **+37.7%** 点查。本模块**故意不修**这些：
> 本次是**保真移植**而不是优化，开关一旦默认打开就会与 Go 侧的行为分叉
> （WAL 会多出 ``-wal`` / ``-shm`` 文件并改变崩溃恢复语义）。同理，本模块**不**把
> ``Connection.execute`` 换成预备语句缓存，保持与 Go 相同的一次性 prepare。

> CPython 差异（唯一一处默认仍会执行的额外 PRAGMA）：``sqlite3.connect`` 会隐式设置
> ``sqlite3_busy_timeout(db, 5000)``，而 Go 侧驱动默认是 0。为了让"默认关闭"名副其实，
> 构造时显式写回 ``PRAGMA busy_timeout = 0`` —— 它是**抵消** CPython 的隐式默认，
> 不是新增的调优；要等锁就用 ``busy_timeout_ms`` 显式打开。

事务：:meth:`SQLiteDatabase.with_transaction` 进入 ``BEGIN IMMEDIATE``、正常退出 ``COMMIT``、
异常 ``ROLLBACK``。Go 的 ``BeginTx`` 是 deferred ``BEGIN``；本仓统一用 IMMEDIATE
（单连接 + 锁已经把事务串行化，IMMEDIATE 只是把锁的获取提前到事务开始，语义等价，
且不会出现"读到一半才发现写不进去"的升级失败）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from atri_x.errors import AtriXError, ClosedError, InvalidError
from atri_x.storage.sqlite.migrations import migrate
from atri_x.storage.times import format_time, parse_time

if TYPE_CHECKING:  # 只为注解；运行期由 Store.__init__ 延迟 import，避免成环
    from atri_x.storage.sqlite.atrinis_event_repository import AtrinisEventRepository
    from atri_x.storage.sqlite.context_summary_repository import ContextSummaryRepository
    from atri_x.storage.sqlite.cron_job_repository import CronJobRepository
    from atri_x.storage.sqlite.media_repository import MediaReferenceRepository, MediaRepository
    from atri_x.storage.sqlite.message_repository import MessageRepository
    from atri_x.storage.sqlite.session_repository import SessionRepository
    from atri_x.storage.sqlite.tool_call_repository import ToolCallRepository

__all__ = [
    "SQLiteDatabase",
    "SQLiteError",
    "SQLiteTransaction",
    "Store",
    "bool_int",
    "new",
    "null_string",
    "null_time",
    "open_database",
    "soft_time",
    "text_of",
]


class SQLiteError(AtriXError):
    """SQLite 层错误（对应 Go 的驱动错误）。

    Go 用 ``fmt.Errorf("...: %w", err)`` 包装 ``modernc.org/sqlite`` 的错误；本仓把等价的
    ``sqlite3.Error`` 统一包成这个类型，原始异常保留在 ``__cause__``。
    会被上层按类型判定的失败（``storage.ErrNotFound``）**不**走这里：那些路径在
    repository 里直接抛 :data:`atri_x.errors.ErrNotFound`，与 Go 逐一对应。
    """


# --- 列读写 helper ---------------------------------------------------------
#
# Go 把这些 helper 放在 `package sqlite` 的包级作用域（nullString / nullTime /
# nullableTime 在 session_repository.go，boolInt 在 tool_call_repository.go），
# 全包共用。本仓的 sqlite 子包同样共用一份，放在这里而不是各 repository 里复制。


def null_string(value: str) -> str | None:
    """写可空 TEXT 列：空串 → NULL。对应 Go ``nullString``。"""
    return value if value else None


def null_time(value: datetime | None) -> str | None:
    """写可空时间列：``None`` → NULL，否则 RFC3339 文本。对应 Go ``nullTime``。"""
    if value is None:
        return None
    return format_time(value)


def bool_int(value: bool) -> int:
    """写 SQLite 的 INTEGER 布尔列。对应 Go ``boolInt``。"""
    return 1 if value else 0


def text_of(value: Any) -> str:
    """读可空 TEXT 列：NULL → 空串。对应 Go 读 ``sql.NullString.String`` 的惯用法。"""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def soft_time(value: Any) -> datetime | None:
    """读时间列并**容忍脏数据**：空串/非法文本 → ``None``。

    对应 Go 里 `m.CreatedAt, _ = storage.ParseTime(...)` 这种忽略错误的写法
    （Go 拿到的是零值时间，本仓用 ``None`` —— ``times.is_zero_time`` 对两者都判真）。
    只有 media / media_references 的读路径这么写，其余读路径解析失败一律报错。
    """
    text = text_of(value)
    if not text:
        return None
    try:
        return parse_time(text)
    except ValueError:
        return None


class SQLiteTransaction:
    """事务内的语句入口（Go 的 ``*sql.Tx``）。

    方法名与 :class:`SQLiteDatabase` 相同，保证事务内外错误类型一致
    （``execute`` / ``query`` / ``query_row`` 都抛 :class:`SQLiteError`）。
    """

    __slots__ = ("_database",)

    def __init__(self, database: "SQLiteDatabase") -> None:
        self._database = database

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只在需要执行 ``PRAGMA`` 之类的原生语句时使用）。"""
        return self._database.connection

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._database.execute(sql, parameters)

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._database.query(sql, parameters)

    def query_row(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._database.query_row(sql, parameters)


class SQLiteDatabase:
    """单连接 + 一把 ``RLock`` 的 SQLite 句柄（Go 的 ``*sql.DB`` 且 ``SetMaxOpenConns(1)``）。

    语句一律走 :meth:`execute` / :meth:`query` / :meth:`query_row`，错误统一成
    :class:`SQLiteError`；多语句操作走 :meth:`with_transaction`，全程持锁。
    """

    def __init__(
        self,
        path: str,
        *,
        journal_mode: str = "",
        synchronous: str = "",
        busy_timeout_ms: int = 0,
    ) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._closed = False
        self._depth = 0
        # 先校验 PRAGMA 取值，避免连接建好之后才因为参数非法抛异常、把连接漏掉。
        if journal_mode:
            journal_mode = _pragma_value("journal_mode", journal_mode)
        if synchronous:
            synchronous = _pragma_value("synchronous", synchronous)
        connection = sqlite3.connect(path, check_same_thread=False, autocommit=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            # 抵消 CPython 隐式的 5000ms busy timeout（Go 侧是 0）；见模块 docstring。
            connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            if journal_mode:
                connection.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()
            if synchronous:
                connection.execute(f"PRAGMA synchronous = {synchronous}")
        except sqlite3.Error:
            connection.close()
            raise
        self._connection = connection

    # -- 基础访问 -----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """底层 :class:`sqlite3.Connection`（调用方自行保证线程安全）。"""
        self._ensure_open()
        return self._connection

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        """执行一条语句并返回游标（``cursor.rowcount`` 可用于判定是否命中行）。"""
        with self._lock:
            self._ensure_open()
            try:
                return self._connection.execute(sql, tuple(parameters))
            except sqlite3.Error as exc:
                raise SQLiteError(str(exc)) from exc

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """执行查询并一次性取回全部行。"""
        with self._lock:
            return self.execute(sql, parameters).fetchall()

    def query_row(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        """执行查询并取回第一行；无行时返回 ``None``（对应 Go 的 ``sql.ErrNoRows``）。"""
        with self._lock:
            return self.execute(sql, parameters).fetchone()

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def with_transaction(self) -> Iterator[SQLiteTransaction]:
        """``BEGIN IMMEDIATE`` → 正常 ``COMMIT``，异常 ``ROLLBACK``。

        全程持有连接锁，因此事务内的读-改-写不会被本进程的其他线程插进来
        （与 Go 侧 ``SetMaxOpenConns(1)`` 的效果一致）。

        嵌套调用是 Python 侧的便利（Go 在 ``SetMaxOpenConns(1)`` 下嵌套 ``BeginTx`` 会死锁）：
        内层不再开事务，写操作并入外层事务一起提交或回滚。
        """
        with self._lock:
            self._ensure_open()
            nested = self._depth > 0
            if not nested:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise SQLiteError(str(exc)) from exc
            self._depth += 1
            try:
                yield SQLiteTransaction(self)
            except BaseException:
                self._depth -= 1
                if not nested:
                    self._rollback()
                raise
            else:
                self._depth -= 1
                if not nested:
                    self._commit()

    def close(self) -> None:
        """关闭连接（Go 的 ``db.Close()``）。重复调用无副作用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    # -- 内部 ---------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClosedError("sqlite database is closed")

    def _commit(self) -> None:
        try:
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            # Go 侧 `defer tx.Rollback()` 在 Commit 失败后仍会兜底回滚。
            self._rollback()
            raise SQLiteError(str(exc)) from exc

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            # Go 的 `_ = tx.Rollback()` 同样忽略回滚本身的错误。
            pass


class Store:
    """存储门面。对应 Go ``sqlite.Store``。

    聊天历史（``chat_messages`` / ``outbound_messages``）在 Go 里是独立 store
    （``NewChatHistory``），因此这里同样不出现，见
    :class:`atri_x.storage.sqlite.chat_history_repository.ChatHistoryStore`。
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        # 延迟到调用点 import：这些模块要从本模块取列读写 helper，
        # 模块级互相 import 会成环（Go 里同属一个 package，不存在这个问题）。
        from atri_x.storage.sqlite.atrinis_event_repository import AtrinisEventRepository
        from atri_x.storage.sqlite.context_summary_repository import ContextSummaryRepository
        from atri_x.storage.sqlite.cron_job_repository import CronJobRepository
        from atri_x.storage.sqlite.media_repository import MediaReferenceRepository, MediaRepository
        from atri_x.storage.sqlite.message_repository import MessageRepository
        from atri_x.storage.sqlite.session_repository import SessionRepository
        from atri_x.storage.sqlite.tool_call_repository import ToolCallRepository

        self.database = database
        self._sessions = SessionRepository(database)
        self._messages = MessageRepository(database)
        self._context_summaries = ContextSummaryRepository(database)
        self._tool_calls = ToolCallRepository(database)
        self._cron_jobs = CronJobRepository(database)
        self._atrinis_events = AtrinisEventRepository(database)
        self._media = MediaRepository(database)
        self._media_references = MediaReferenceRepository(database)

    def sessions(self) -> "SessionRepository":
        return self._sessions

    def messages(self) -> "MessageRepository":
        return self._messages

    def media(self) -> "MediaRepository":
        return self._media

    def media_references(self) -> "MediaReferenceRepository":
        return self._media_references

    def context_summaries(self) -> "ContextSummaryRepository":
        return self._context_summaries

    def tool_calls(self) -> "ToolCallRepository":
        return self._tool_calls

    def cron_jobs(self) -> "CronJobRepository":
        return self._cron_jobs

    def atrinis_events(self) -> "AtrinisEventRepository":
        return self._atrinis_events

    def close(self) -> None:
        self.database.close()


def open_database(
    path: str,
    *,
    journal_mode: str = "",
    synchronous: str = "",
    busy_timeout_ms: int = 0,
) -> SQLiteDatabase:
    """建目录并打开连接（Go 的 ``New`` / ``NewChatHistory`` 共用的那一段装配）。"""
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
    except OSError as exc:
        raise SQLiteError(f"create sqlite directory: {exc}") from exc
    return SQLiteDatabase(
        path,
        journal_mode=journal_mode,
        synchronous=synchronous,
        busy_timeout_ms=busy_timeout_ms,
    )


def new(
    path: str,
    *,
    journal_mode: str = "",
    synchronous: str = "",
    busy_timeout_ms: int = 0,
) -> Store:
    """打开（必要时创建）库并跑完 migration，返回 :class:`Store`。对应 Go ``sqlite.New``。

    ``journal_mode`` / ``synchronous`` / ``busy_timeout_ms`` 默认关闭，语义与上游一致；
    见模块 docstring 的性能与保真说明。
    """
    if not path:
        raise InvalidError("sqlite path is empty")
    database = open_database(
        path,
        journal_mode=journal_mode,
        synchronous=synchronous,
        busy_timeout_ms=busy_timeout_ms,
    )
    try:
        migrate(database)
    except BaseException:
        database.close()
        raise
    return Store(database)


def _pragma_value(name: str, value: str) -> str:
    """校验 PRAGMA 取值（这些开关是开发者手写的常量，仍不允许拼进任意 SQL）。"""
    text = str(value)
    if not text.replace("_", "").isalnum():
        raise InvalidError(f"invalid pragma {name} value: {value!r}")
    return text
