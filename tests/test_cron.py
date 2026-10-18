"""``atri_x.cron`` 的单元测试：调度、Manager、CRUD、LLM 执行。

对应 Go 测试：``internal/cron/manager_test.go``（全程）与 ``internal/cron/service_test.go``
的调度/CRUD/LLM 部分；投递与补发用例在 ``tests/test_cron_delivery.py``
（那些用例需要 :mod:`tests.test_cron` 里的 fake 与临时目录 helper，故在此集中定义）。

**存储口径**：``atri_x.storage.sqlite`` 尚未落地，所以：

* 大多数用例用本文件顶部的 in-memory fake repository（逐方法对齐
  ``internal/cron/manager_test.go`` 的 ``fakeCronRepo``，包括 CAS 与"仅启用时可写"的语义）；
* 需要真实事务/唯一约束的用例（receipt 映射、Session 复制）用
  :class:`SqliteCronStore` —— 用标准库 ``sqlite3`` 按 ``internal/storage/sqlite`` 的表结构
  实现 cron_jobs / sessions / messages / platform_message_map 四张表。集成期把它换成
  ``atri_x.storage.sqlite`` 即可，用例本身不用改。

**时钟**：所有时间断言都用固定时钟 + 固定时区 ``LOCATION``（UTC+8），不依赖机器时区、
不等真实时间（``Manager`` 的真实调度只用一例 ``@every 1s`` 验证"到点会触发"）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
import sqlite3
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from atri_x.background import RunRequest, RunResult
from atri_x.cron import (
    METADATA_KIND,
    USER_HANDLER_NAME,
    CronActor,
    CronDeliveryOutputState,
    CronDeliveryState,
    CronDeliveryTargetState,
    CronLLMMetadata,
    CronSchedule,
    CronTarget,
    CronTrigger,
    DeliveryDelivered,
    DeliveryFallbackDelivered,
    JobView,
    Manager,
    Metadata,
    Options,
    PatchRequest,
    PlatformTarget,
    ScheduleCron,
    ScheduleOnce,
    Service,
    TriggerDirect,
    TriggerLLM,
    UpsertRequest,
    compute_next_run_at,
    decode_delivery_state,
    decode_metadata,
    delivery_status_done,
    encode_delivery_state,
    find_delivery_output_status,
    find_delivery_target_state,
    normalize_job_name,
    parse_llm_result,
    schedule_expr,
    user_once_run_at_expired,
)
from atri_x.cron.manager import UpsertJobRequest
from atri_x.errors import ForbiddenError, InvalidError, NotFoundError
from atri_x.delivery.delivery import Receipt
from atri_x.llm.llm import MessageSegment, SegmentType
from atri_x.security import Actor, RoleSuperadmin
from atri_x.storage.models import (
    CronJob,
    CronJobRunState,
    Message,
    PlatformMessageMap,
    Session,
    UpsertCronJobRequest,
)
from atri_x.storage.times import format_time, now as storage_now, parse_time
from atri_x.storage.ids import new_id

# 固定时区（UTC+8）与固定时钟：等价 Go 测试里"本机时区"的确定性替身。
LOCATION = timezone(timedelta(hours=8))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root() -> str:
    """仓库根目录（测试临时目录建在它下面）。"""
    return _REPO_ROOT


def make_temp_dir(prefix: str) -> str:
    """在 ``tests/_tmp_cron/`` 下建一个临时目录并返回路径。

    刻意不用 ``tempfile.TemporaryDirectory`` / ``mkdtemp``：本机沙箱下这些目录带
    0o700 ACL，清理时会抛 ``PermissionError``。测试在 ``tearDown`` 里
    ``shutil.rmtree(ignore_errors=True)``；即使测试被中断，残留也只落在
    ``tests/_tmp_cron/``（已在 ``.gitignore`` 里），不会散到仓库根。
    """
    base = os.path.join(repo_root(), "tests", "_tmp_cron")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{prefix}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    return path


def fixed_time(value: str) -> datetime:
    """把 ``YYYY-MM-DD HH:MM:SS`` 解析成 ``LOCATION`` 时区的固定时钟。"""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCATION)


def superadmin() -> Actor:
    """cli 超管主体（与 Go 测试里的 actor 一致）。"""
    return Actor(
        id="cli:local", platform="cli", platform_user_id="local", role=RoleSuperadmin
    )


def test_atriyph_task(name: str) -> str:
    """合法 Atriyph ``#task``（对应 Go 测试的 ``testAtriyphTask``）。"""
    return f"#task {name} - test task\n-> $report:str\n"


# ---------------------------------------------------------------------------
# in-memory fake（对齐 internal/cron/manager_test.go 的 fakeCronRepo / fakeCronStore）
# ---------------------------------------------------------------------------
class FakeCronRepo:
    """cron 仓库的 in-memory 实现。对应 Go ``fakeCronRepo``。"""

    def __init__(self) -> None:
        self.jobs: dict[str, CronJob] = {}

    def upsert(self, req: UpsertCronJobRequest) -> CronJob:
        if req.name in self.jobs:
            job = self.jobs[req.name]
            job.handler = req.handler
            job.schedule = req.schedule
            job.enabled = req.enabled
            job.metadata = req.metadata
            job.next_run_at = req.next_run_at
            if req.reset_delivery:
                job.delivery_state = ""
                job.delivery_token = ""
            job.updated_at = storage_now()
            return replace(job)
        job = CronJob(
            id=new_id(),
            name=req.name,
            handler=req.handler,
            schedule=req.schedule,
            enabled=req.enabled,
            metadata=req.metadata,
            next_run_at=req.next_run_at,
            created_at=storage_now(),
            updated_at=storage_now(),
        )
        self.jobs[job.name] = job
        return replace(job)

    def get_by_name(self, name: str) -> CronJob:
        job = self.jobs.get(name)
        if job is None:
            raise NotFoundError(f"cron job {name!r} not found")
        return replace(job)

    def list(self, include_disabled: bool) -> list[CronJob]:
        return [
            replace(job)
            for job in self.jobs.values()
            if include_disabled or job.enabled
        ]

    def list_enabled(self) -> list[CronJob]:
        return self.list(False)

    def update_next_run_at(
        self, id: str, next_run_at: datetime | None, updated_at: datetime
    ) -> None:
        for job in self.jobs.values():
            if job.id == id:
                job.next_run_at = next_run_at
                job.updated_at = updated_at
                return
        raise NotFoundError(f"cron job {id!r} not found")

    def update_run_state(self, id: str, state: CronJobRunState) -> None:
        for job in self.jobs.values():
            if job.id == id:
                job.last_run_at = state.last_run_at
                job.next_run_at = state.next_run_at
                job.run_count = state.run_count
                job.last_error = state.last_error
                job.enabled = state.enabled
                job.updated_at = state.updated_at
                return
        raise NotFoundError(f"cron job {id!r} not found")

    def compare_and_swap_delivery(
        self, id: str, expected_token: str, next_token: str, delivery_state: str
    ) -> bool:
        for job in self.jobs.values():
            if job.id == id and job.enabled and job.delivery_token == expected_token:
                job.delivery_token = next_token
                job.delivery_state = delivery_state
                job.updated_at = storage_now()
                return True
        return False

    def disable_by_name(self, name: str) -> None:
        job = self.jobs.get(name)
        if job is None:
            raise NotFoundError(f"cron job {name!r} not found")
        job.enabled = False
        job.next_run_at = None

    def disable_by_name_if_delivery_token(self, name: str, delivery_token: str) -> bool:
        job = self.jobs.get(name)
        if job is None or not job.enabled or job.delivery_token != delivery_token:
            return False
        job.enabled = False
        job.next_run_at = None
        return True

    def delete_by_name(self, name: str) -> None:
        if name not in self.jobs:
            raise NotFoundError(f"cron job {name!r} not found")
        del self.jobs[name]


class FakeStore:
    """Store 门面 fake。对应 Go ``fakeCronStore``（只需要 cron 仓库）。"""

    def __init__(self, cron: FakeCronRepo | None = None, sessions=None, messages=None) -> None:
        self.cron = cron or FakeCronRepo()
        self._sessions = sessions
        self._messages = messages

    def sessions(self):
        return self._sessions

    def messages(self):
        return self._messages

    def media(self):
        return None

    def media_references(self):
        return None

    def context_summaries(self):
        return None

    def tool_calls(self):
        return None

    def cron_jobs(self) -> FakeCronRepo:
        return self.cron

    def atrinis_events(self):
        return None

    def close(self) -> None:
        return None


class FakeRunner:
    """后台 runner fake。对应 Go ``fakeCronRunner``。"""

    def __init__(
        self,
        text: str = "",
        texts: list[str] | None = None,
        session_ids: list[str] | None = None,
        message_ids: list[str] | None = None,
        taken_over_at: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.text = text
        self.texts = texts or []
        self.session_ids = session_ids or []
        self.message_ids = message_ids or []
        self.taken_over_at = taken_over_at
        self.error = error
        self.calls = 0
        self.requests: list[RunRequest] = []

    async def run_background(self, ctx, req: RunRequest) -> RunResult:
        self.requests.append(req)
        index = self.calls
        self.calls += 1
        if self.error is not None:
            raise self.error
        text = self.text
        if index < len(self.texts):
            text = self.texts[index]
        session_id = "cron-session"
        if index < len(self.session_ids):
            session_id = self.session_ids[index]
        message_id = "cron-message"
        if index < len(self.message_ids):
            message_id = self.message_ids[index]
        return RunResult(
            session_id=session_id,
            message_id=message_id,
            text=text,
            taken_over=self.taken_over_at is not None and index >= self.taken_over_at,
        )


# ---------------------------------------------------------------------------
# 真实 SQLite（标准库 sqlite3）实现的最小 Store
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_jobs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, handler TEXT NOT NULL,
    schedule TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, metadata TEXT,
    delivery_state TEXT, delivery_token TEXT, last_run_at TEXT, next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, owner_id TEXT, platform TEXT, platform_scope_id TEXT,
    mode TEXT, title TEXT, status TEXT, metadata TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT, content TEXT,
    tool_call_id TEXT, segments TEXT, metadata TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS platform_message_map (
    id TEXT PRIMARY KEY, platform TEXT, platform_scope_id TEXT, platform_message_id TEXT,
    session_id TEXT, message_id TEXT,
    UNIQUE (platform, platform_scope_id, platform_message_id)
);
"""


class SqliteCronStore:
    """用标准库 ``sqlite3`` 实现的最小 Store（cron + session + message）。

    表结构与 ``internal/storage/sqlite`` 的同名表对齐（列名/可空口径一致），
    CAS 走 ``UPDATE ... WHERE enabled=1 AND delivery_token=?`` 的真实条件更新，
    所以它是"真 SQLite"而不是字典假实现。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    def sessions(self) -> "SqliteSessionRepo":
        return SqliteSessionRepo(self)

    def messages(self) -> "SqliteMessageRepo":
        return SqliteMessageRepo(self)

    def cron_jobs(self) -> "SqliteCronJobRepo":
        return SqliteCronJobRepo(self)

    def media(self):
        return None

    def media_references(self):
        return None

    def context_summaries(self):
        return None

    def tool_calls(self):
        return None

    def atrinis_events(self):
        return None

    # -- 内部 ---------------------------------------------------------------
    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args).fetchall())


def _row_int(row: sqlite3.Row, key: str) -> bool:
    return bool(row[key])


def _nullable(moment: datetime | None) -> str:
    return format_time(moment)


def _parse_optional(text: str | None) -> datetime | None:
    if not text:
        return None
    return parse_time(text)


class SqliteCronJobRepo:
    """真实 SQLite 的 cron 仓库。对应 ``internal/storage/sqlite/cron_job_repository.go``。"""

    def __init__(self, store: SqliteCronStore) -> None:
        self.store = store

    def upsert(self, req: UpsertCronJobRequest) -> CronJob:
        moment = format_time(storage_now())
        existing = None
        try:
            existing = self.get_by_name(req.name)
        except NotFoundError:
            existing = None
        if existing is None:
            job = CronJob(
                id=new_id(),
                name=req.name,
                handler=req.handler,
                schedule=req.schedule,
                enabled=req.enabled,
                metadata=req.metadata,
                next_run_at=req.next_run_at,
                created_at=storage_now(),
                updated_at=storage_now(),
            )
            self.store.execute(
                "INSERT INTO cron_jobs (id,name,handler,schedule,enabled,metadata,delivery_state,"
                "delivery_token,run_count,created_at,updated_at,next_run_at) "
                "VALUES (?,?,?,?,?,?,'','',0,?,?,?)",
                (
                    job.id,
                    job.name,
                    job.handler,
                    job.schedule,
                    int(job.enabled),
                    job.metadata,
                    moment,
                    moment,
                    _nullable(req.next_run_at),
                ),
            )
            return job
        fields = "handler=?,schedule=?,enabled=?,metadata=?,next_run_at=?,updated_at=?"
        args: list = [
            req.handler,
            req.schedule,
            int(req.enabled),
            req.metadata,
            _nullable(req.next_run_at),
            moment,
        ]
        if req.reset_delivery:
            fields += ",delivery_state='',delivery_token=''"
        self.store.execute(f"UPDATE cron_jobs SET {fields} WHERE id=?", (*args, existing.id))
        return self.get_by_name(req.name)

    def get_by_name(self, name: str) -> CronJob:
        rows = self.store.query("SELECT * FROM cron_jobs WHERE name=?", (name,))
        if not rows:
            raise NotFoundError(f"cron job {name!r} not found")
        return _cron_job(rows[0])

    def list(self, include_disabled: bool) -> list[CronJob]:
        sql = "SELECT * FROM cron_jobs"
        if not include_disabled:
            sql += " WHERE enabled=1"
        return [_cron_job(row) for row in self.store.query(sql)]

    def list_enabled(self) -> list[CronJob]:
        return self.list(False)

    def update_next_run_at(
        self, id: str, next_run_at: datetime | None, updated_at: datetime
    ) -> None:
        self.store.execute(
            "UPDATE cron_jobs SET next_run_at=?,updated_at=? WHERE id=?",
            (_nullable(next_run_at), format_time(updated_at), id),
        )

    def update_run_state(self, id: str, state: CronJobRunState) -> None:
        self.store.execute(
            "UPDATE cron_jobs SET last_run_at=?,next_run_at=?,run_count=?,last_error=?,"
            "enabled=?,updated_at=? WHERE id=?",
            (
                _nullable(state.last_run_at),
                _nullable(state.next_run_at),
                state.run_count,
                state.last_error,
                int(state.enabled),
                format_time(state.updated_at),
                id,
            ),
        )

    def compare_and_swap_delivery(
        self, id: str, expected_token: str, next_token: str, delivery_state: str
    ) -> bool:
        cursor = self.store.execute(
            "UPDATE cron_jobs SET delivery_token=?,delivery_state=?,updated_at=? "
            "WHERE id=? AND enabled=1 AND delivery_token=?",
            (next_token, delivery_state, format_time(storage_now()), id, expected_token),
        )
        return cursor.rowcount > 0

    def disable_by_name(self, name: str) -> None:
        cursor = self.store.execute(
            "UPDATE cron_jobs SET enabled=0,next_run_at=NULL WHERE name=?", (name,)
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"cron job {name!r} not found")

    def disable_by_name_if_delivery_token(self, name: str, delivery_token: str) -> bool:
        cursor = self.store.execute(
            "UPDATE cron_jobs SET enabled=0,next_run_at=NULL "
            "WHERE name=? AND enabled=1 AND delivery_token=?",
            (name, delivery_token),
        )
        return cursor.rowcount > 0

    def delete_by_name(self, name: str) -> None:
        cursor = self.store.execute("DELETE FROM cron_jobs WHERE name=?", (name,))
        if cursor.rowcount == 0:
            raise NotFoundError(f"cron job {name!r} not found")


def _cron_job(row: sqlite3.Row) -> CronJob:
    return CronJob(
        id=row["id"],
        name=row["name"],
        handler=row["handler"],
        schedule=row["schedule"],
        enabled=_row_int(row, "enabled"),
        metadata=row["metadata"] or "",
        delivery_state=row["delivery_state"] or "",
        delivery_token=row["delivery_token"] or "",
        last_run_at=_parse_optional(row["last_run_at"]),
        next_run_at=_parse_optional(row["next_run_at"]),
        run_count=row["run_count"],
        last_error=row["last_error"] or "",
        created_at=_parse_optional(row["created_at"]),
        updated_at=_parse_optional(row["updated_at"]),
    )


class SqliteSessionRepo:
    """真实 SQLite 的会话仓库（只实现 cron 用到的部分）。"""

    def __init__(self, store: SqliteCronStore) -> None:
        self.store = store

    def create(self, session: Session) -> None:
        if not session.id:
            session.id = new_id()
        if session.created_at is None:
            session.created_at = storage_now()
        if not session.updated_at:
            session.updated_at = session.created_at
        if not session.mode:
            session.mode = "work"
        if not session.status:
            session.status = "active"
        self.store.execute(
            "INSERT INTO sessions (id,owner_id,platform,platform_scope_id,mode,title,status,"
            "metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session.id,
                session.owner_id,
                session.platform,
                session.platform_scope_id,
                session.mode,
                session.title,
                session.status,
                session.metadata,
                format_time(session.created_at),
                format_time(session.updated_at),
            ),
        )

    def get(self, id: str) -> Session:
        rows = self.store.query("SELECT * FROM sessions WHERE id=?", (id,))
        if not rows:
            raise NotFoundError(f"session {id!r} not found")
        row = rows[0]
        return Session(
            id=row["id"],
            owner_id=row["owner_id"] or "",
            platform=row["platform"] or "",
            platform_scope_id=row["platform_scope_id"] or "",
            mode=row["mode"] or "",
            title=row["title"] or "",
            status=row["status"] or "",
            metadata=row["metadata"] or "",
            created_at=_parse_optional(row["created_at"]),
            updated_at=_parse_optional(row["updated_at"]),
        )

    def list(self, req) -> list:
        raise NotImplementedError("cron 不用 SessionRepository.List（Go 测的是 SQL 侧过滤）")

    def count(self) -> int:
        return len(self.store.query("SELECT 1 FROM sessions"))


class SqliteMessageRepo:
    """真实 SQLite 的消息仓库（只实现 cron 用到的部分）。"""

    def __init__(self, store: SqliteCronStore) -> None:
        self.store = store

    def append(self, message: Message) -> None:
        if not message.id:
            message.id = new_id()
        if message.created_at is None:
            message.created_at = storage_now()
        self.store.execute(
            "INSERT INTO messages (id,session_id,role,content,tool_call_id,segments,metadata,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                message.id,
                message.session_id,
                message.role,
                message.content,
                message.tool_call_id,
                message.segments,
                message.metadata,
                format_time(message.created_at),
            ),
        )

    def list_by_session(self, session_id: str) -> list[Message]:
        rows = self.store.query(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at,id", (session_id,)
        )
        return [_message(row) for row in rows]

    def get(self, id: str) -> Message:
        rows = self.store.query("SELECT * FROM messages WHERE id=?", (id,))
        if not rows:
            raise NotFoundError(f"message {id!r} not found")
        return _message(rows[0])

    def map_platform_message(self, mapping: PlatformMessageMap) -> None:
        self.store.execute(
            "INSERT OR REPLACE INTO platform_message_map "
            "(id,platform,platform_scope_id,platform_message_id,session_id,message_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                mapping.id or new_id(),
                mapping.platform,
                mapping.platform_scope_id,
                mapping.platform_message_id,
                mapping.session_id,
                mapping.message_id,
            ),
        )

    def find_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> Message:
        rows = self.store.query(
            "SELECT m.* FROM platform_message_map p JOIN messages m ON m.id=p.message_id "
            "WHERE p.platform=? AND p.platform_scope_id=? AND p.platform_message_id=?",
            (platform, scope_id, platform_message_id),
        )
        if not rows:
            raise NotFoundError("platform message not mapped")
        return _message(rows[0])

    def count(self) -> int:
        return len(self.store.query("SELECT 1 FROM messages"))


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"] or "",
        content=row["content"] or "",
        tool_call_id=row["tool_call_id"] or "",
        segments=row["segments"] or "",
        metadata=row["metadata"] or "",
        created_at=_parse_optional(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# 测试基类与通用 helper
# ---------------------------------------------------------------------------
def upsert_test_cron_job(repo, meta: Metadata, name: str = "user.cron.test") -> CronJob:
    """写入一条测试任务（对应 Go ``upsertTestCronJob``）。"""
    return repo.upsert(
        UpsertCronJobRequest(
            name=name,
            handler=USER_HANDLER_NAME,
            schedule="4 3 2 1 *",
            enabled=True,
            metadata=meta.encode(),
        )
    )


def must_marshal_delivery(state: CronDeliveryState) -> str:
    return encode_delivery_state(state)


def must_decode_delivery(raw: str) -> CronDeliveryState:
    return decode_delivery_state(raw)


def delivery_platform_done(state: CronDeliveryState, platform_name: str) -> bool:
    """某平台下所有目标的所有输出是否都完成（对应 Go 测试 helper 同名函数）。"""
    prefix = platform_name + "|"
    found = False
    for target in state.targets:
        if not target.key.startswith(prefix):
            continue
        found = True
        for output in target.outputs:
            if not delivery_status_done(output.status):
                return False
    return found


class CronTestCase(unittest.IsolatedAsyncioTestCase):
    """公共基类：临时目录 + 固定时钟 + 常用装配。"""

    def setUp(self) -> None:
        self._dirs: list[str] = []
        self.now_value = fixed_time("2026-01-02 03:05:00")

    def tearDown(self) -> None:
        for path in self._dirs:
            shutil.rmtree(path, ignore_errors=True)

    def temp_dir(self, prefix: str = "tmp") -> str:
        path = make_temp_dir(prefix)
        self._dirs.append(path)
        return path

    def make_service(
        self,
        store: FakeStore | None = None,
        manager: Manager | None = None,
        runner: FakeRunner | None = None,
        enabled_platforms: list[PlatformTarget] | None = None,
        send_target=None,
        sandbox_root: str = "",
        audit=None,
    ) -> Service:
        options = Options(
            manager=manager,
            store=store,
            runner=runner,
            enabled_platforms=enabled_platforms or [],
            send_target=send_target,
            sandbox_root=sandbox_root,
            location=LOCATION,
            now=lambda: self.now_value,
            audit=audit,
        )
        return Service(options)

    def fixed(self, value: str) -> None:
        self.now_value = fixed_time(value)


# ---------------------------------------------------------------------------
# 调度语义（对照 robfig/cron v3）
# ---------------------------------------------------------------------------
class TestComputeNextRunAt(unittest.IsolatedAsyncioTestCase):
    """``next_run_at`` 计算与 Go/robfig 一致。"""

    def _next(self, schedule: str, after: str) -> datetime | None:
        now = datetime.strptime(after, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCATION)
        return compute_next_run_at(schedule, "", True, now, LOCATION)

    def test_daily_descriptor(self) -> None:
        got = self._next("@daily", "2026-01-02 03:04:00")
        self.assertEqual(got, fixed_time("2026-01-03 00:00:00"))

    def test_every_five_minutes(self) -> None:
        got = self._next("*/5 * * * *", "2026-01-02 03:04:00")
        self.assertEqual(got, fixed_time("2026-01-02 03:05:00"))

    def test_dom_dow_or_semantics(self) -> None:
        # 13 号或周五：2026-03-13 是周五，两者同时命中。
        got = self._next("0 0 13 * 5", "2026-03-01 00:00:00")
        self.assertEqual(got, fixed_time("2026-03-06 00:00:00"))
        # 只命中 DOM（13 号）也要触发：2026-04-13 是周一。
        got = self._next("0 0 13 * 5", "2026-04-07 00:00:00")
        self.assertEqual(got, fixed_time("2026-04-10 00:00:00"))

    def test_month_end(self) -> None:
        got = self._next("0 0 31 * *", "2026-04-01 00:00:00")
        self.assertEqual(got, fixed_time("2026-05-31 00:00:00"))

    def test_leap_year_feb_29(self) -> None:
        got = self._next("0 0 29 2 *", "2026-03-01 00:00:00")
        self.assertEqual(got, fixed_time("2028-02-29 00:00:00"))

    def test_disabled_returns_none(self) -> None:
        now = fixed_time("2026-01-02 03:03:00")
        self.assertIsNone(compute_next_run_at("0 3 * * *", "", False, now, LOCATION))

    def test_invalid_schedule_raises(self) -> None:
        now = fixed_time("2026-01-02 03:03:00")
        with self.assertRaises(Exception):
            compute_next_run_at("0 1 2 3", "", True, now, LOCATION)

    def test_user_once_run_at(self) -> None:
        meta = Metadata(
            kind=METADATA_KIND,
            version=1,
            schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
        )
        now = fixed_time("2026-01-02 03:03:00")
        got = compute_next_run_at("4 3 2 1 *", meta.encode(), True, now, LOCATION)
        self.assertEqual(got, fixed_time("2026-01-02 03:04:00"))
        expired = compute_next_run_at(
            "4 3 2 1 *", meta.encode(), True, fixed_time("2026-01-02 03:04:00"), LOCATION
        )
        self.assertIsNone(expired)
        self.assertTrue(
            user_once_run_at_expired(meta.encode(), fixed_time("2026-01-02 03:04:00"), LOCATION)
        )
        self.assertFalse(
            user_once_run_at_expired(meta.encode(), fixed_time("2026-01-02 03:03:00"), LOCATION)
        )


class TestScheduleExpr(unittest.TestCase):
    """``scheduleExpr`` / ``normalizeJobName``（service_test.go 的两个小用例）。"""

    def test_schedule_expr_once_uses_minute_precision(self) -> None:
        expr = schedule_expr(
            CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:05"), LOCATION
        )
        self.assertEqual(expr, "4 3 2 1 *")

    def test_schedule_expr_cron_requires_five_fields(self) -> None:
        with self.assertRaises(InvalidError):
            schedule_expr(CronSchedule(mode=ScheduleCron, cron_expr="0 1 2 3"))
        self.assertEqual(
            schedule_expr(CronSchedule(mode=ScheduleCron, cron_expr="0 1 * * *")), "0 1 * * *"
        )

    def test_normalize_job_name_adds_user_prefix(self) -> None:
        got = normalize_job_name(" daily summary! ")
        self.assertEqual(got, "user.cron.daily_summary")
        self.assertNotIn(" ", got)

    def test_parse_llm_result_accepts_code_fence_and_extra_text(self) -> None:
        result = parse_llm_result("前缀```json\n{\"completed\":true,\"need_report\":true,\"report\":\"ok\"}\n```后缀")
        self.assertTrue(result.completed)
        self.assertTrue(result.need_report)
        self.assertEqual(result.report, "ok")

    def test_metadata_json_shape_matches_go(self) -> None:
        """零值结构体必须写出（``"llm":{}``），否则 Go 侧的字符串替换式迁移会失配。"""
        meta = Metadata(kind=METADATA_KIND, version=1, schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"))
        self.assertIn('"llm":{}', meta.encode())
        self.assertIn('"all_enabled_platforms":false', meta.encode())


# ---------------------------------------------------------------------------
# Manager（manager_test.go 逐条移植）
# ---------------------------------------------------------------------------
class TestManager(CronTestCase):
    async def test_manager_runs_registered_handler_and_updates_state(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        called = False

        async def handler(ctx, job):
            nonlocal called
            called = True
            self.assertEqual(job.metadata, '{"hello":"world"}')

        await manager.register_handler("test.handler", handler)
        job = await manager.upsert_job(
            UpsertJobRequest(
                name="test.job",
                handler="test.handler",
                schedule="0 3 * * *",
                enabled=True,
                metadata='{"hello":"world"}',
            )
        )
        await manager.run_job(job.name)
        self.assertTrue(called)
        got = repo.jobs[job.name]
        self.assertEqual(got.run_count, 1)
        self.assertEqual(got.last_error, "")
        self.assertIsNotNone(got.last_run_at)

    async def test_manager_stores_handler_error(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)

        async def handler(ctx, job):
            raise RuntimeError("boom")

        await manager.register_handler("test.handler", handler)
        job = await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="0 3 * * *", enabled=True
            )
        )
        await manager.run_job(job.name)
        got = repo.jobs[job.name]
        self.assertEqual(got.run_count, 1)
        self.assertEqual(got.last_error, "boom")

    async def test_manager_upsert_stores_and_disable_clears_next_run_at(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        job = await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="0 3 * * *", enabled=True
            )
        )
        self.assertIsNotNone(job.next_run_at)
        await manager.disable_job(job.name)
        self.assertIsNone(repo.jobs[job.name].next_run_at)

    async def test_manager_run_job_does_not_reenable_job_disabled_by_handler(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)

        async def handler(ctx, job):
            await manager.disable_job(job.name)

        await manager.register_handler("test.handler", handler)
        job = await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="0 3 * * *", enabled=True
            )
        )
        await manager.run_job(job.name)
        got = repo.jobs[job.name]
        self.assertFalse(got.enabled)
        self.assertEqual(got.run_count, 1)
        self.assertIsNotNone(got.last_run_at)

    async def test_manager_skips_unregistered_handler(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        job = await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="missing.handler", schedule="0 3 * * *", enabled=True
            )
        )
        await manager.run_job(job.name)
        got = repo.jobs[job.name]
        self.assertEqual(got.run_count, 0)
        self.assertIsNone(got.last_run_at)

    async def test_manager_delete_job_removes_repository_record(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="0 3 * * *", enabled=True
            )
        )
        await manager.delete_job("test.job")
        self.assertNotIn("test.job", repo.jobs)

    async def test_manager_start_schedules_enabled_jobs(self) -> None:
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        await manager.register_handler("test.handler", lambda ctx, job: None)
        await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="0 3 * * *", enabled=True
            )
        )
        await manager.start()
        try:
            self.assertIn("test.job", manager.entries)
        finally:
            await asyncio.wait_for(manager.stop(), timeout=5)
        self.assertEqual(manager.entries, {})

    async def test_manager_stop_before_start_does_not_block(self) -> None:
        manager = Manager(None, location=LOCATION)
        await asyncio.wait_for(manager.stop(), timeout=1)

    async def test_manager_fires_job_when_next_run_arrives(self) -> None:
        """真实调度一例：单次 sleep 到 next_run 后触发（不忙轮询）。"""
        repo = FakeCronRepo()
        manager = Manager(repo, location=LOCATION)
        fired = asyncio.Event()

        async def handler(ctx, job):
            fired.set()

        await manager.register_handler("test.handler", handler)
        await manager.upsert_job(
            UpsertJobRequest(
                name="test.job", handler="test.handler", schedule="@every 1s", enabled=True
            )
        )
        await manager.start()
        try:
            await asyncio.wait_for(fired.wait(), timeout=5)
        finally:
            await asyncio.wait_for(manager.stop(), timeout=5)
        self.assertEqual(repo.jobs["test.job"].run_count, 1)

    async def test_manager_upsert_requires_fields(self) -> None:
        manager = Manager(FakeCronRepo(), location=LOCATION)
        with self.assertRaises(InvalidError):
            await manager.upsert_job(UpsertJobRequest(name="", handler="h", schedule="* * * * *"))
        with self.assertRaises(InvalidError):
            await manager.upsert_job(UpsertJobRequest(name="n", handler="", schedule="* * * * *"))
        with self.assertRaises(InvalidError):
            await manager.upsert_job(UpsertJobRequest(name="n", handler="h", schedule=""))


# ---------------------------------------------------------------------------
# Service：CRUD（service_test.go）
# ---------------------------------------------------------------------------
class TestServiceCrud(CronTestCase):
    async def test_create_rejects_past_and_bumps_current_minute_once_run_at(self) -> None:
        store = FakeStore()
        svc = self.make_service(store=store)
        self.fixed("2026-01-02 03:04:30")
        base = UpsertRequest(
            name="bad",
            title="bad",
            schedule_mode=ScheduleOnce,
            trigger_mode=TriggerDirect,
            message="提醒",
            enabled=True,
            actor=superadmin(),
            source_platform="cli",
        )
        base.run_at = "2026-01-02 03:03:59"
        with self.assertRaises(InvalidError) as ctx:
            await svc.create(None, base)
        self.assertIn("当前时间：2026-01-02 03:04:30", str(ctx.exception))

        base.run_at = "2026-01-02 03:04:59"
        job = await svc.create(None, base)
        meta = decode_metadata(job.metadata)
        self.assertEqual(meta.schedule.run_at, "2026-01-02 03:05:00")

        base.name = "future"
        base.run_at = "2026-01-02 03:05:00"
        await svc.create(None, base)

    async def test_update_rejects_current_minute_once_run_at(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        self.fixed("2026-01-02 03:04:30")
        await svc.create(
            None,
            UpsertRequest(
                name="update_bad",
                title="bad",
                schedule_mode=ScheduleOnce,
                run_at="2026-01-02 03:05:00",
                trigger_mode=TriggerDirect,
                message="提醒",
                enabled=True,
                actor=superadmin(),
                source_platform="cli",
            ),
        )
        await svc.update(
            None,
            PatchRequest(name="update_bad", run_at="2026-01-02 03:04:59", actor=superadmin()),
        )
        meta = decode_metadata(repo.jobs["user.cron.update_bad"].metadata)
        self.assertEqual(meta.schedule.run_at, "2026-01-02 03:05:00")

    async def test_update_reenables_completed_once_cron_with_fresh_delivery_state(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        self.fixed("2026-01-02 03:04:30")
        actor = superadmin()
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                created_by=CronActor(
                    actor_id=actor.id,
                    platform=actor.platform,
                    platform_user_id=actor.platform_user_id,
                ),
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:03:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        old_state = CronDeliveryState(
            run_id="old-run",
            report_ready=True,
            task_completed=True,
            report="旧报告",
            report_segments=[MessageSegment(type=SegmentType.IMAGE, url="old.png")],
            report_session_id="old-session",
            report_message_id="old-message",
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(old_state)
        repo.jobs[job.name].delivery_token = "old-session"
        repo.jobs[job.name].enabled = False
        repo.jobs[job.name].run_count = 1

        updated = await svc.update(
            None,
            PatchRequest(
                name=job.name,
                run_at="2026-01-02 03:05:00",
                enabled=True,
                actor=actor,
            ),
        )
        self.assertEqual(updated.delivery_state, "")
        self.assertEqual(updated.delivery_token, "")
        meta = decode_metadata(updated.metadata)
        self.assertFalse(svc.is_completed_cron(updated, meta, CronDeliveryState()))

    async def test_create_llm_cron_requires_atriyph_task(self) -> None:
        svc = self.make_service(store=FakeStore())
        self.fixed("2026-01-02 03:04:30")
        base = UpsertRequest(
            name="daily",
            title="daily",
            schedule_mode=ScheduleOnce,
            run_at="2026-01-02 03:05:00",
            trigger_mode=TriggerLLM,
            message="自然语言任务",
            enabled=True,
            actor=superadmin(),
            source_platform="cli",
        )
        with self.assertRaises(Exception) as ctx:
            await svc.create(None, base)
        self.assertIn("#task", str(ctx.exception))
        base.message = test_atriyph_task("daily_task")
        await svc.create(None, base)

    async def test_create_direct_cron_does_not_require_atriyph_task(self) -> None:
        svc = self.make_service(store=FakeStore())
        self.fixed("2026-01-02 03:04:30")
        await svc.create(
            None,
            UpsertRequest(
                name="direct",
                title="direct",
                schedule_mode=ScheduleOnce,
                run_at="2026-01-02 03:05:00",
                trigger_mode=TriggerDirect,
                message="自然语言提醒",
                enabled=True,
                actor=superadmin(),
                source_platform="cli",
            ),
        )

    async def test_create_and_update_llm_cron_tool_list_names(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        self.fixed("2026-01-02 03:04:30")
        actor = superadmin()
        job = await svc.create(
            None,
            UpsertRequest(
                name="tools",
                title="tools",
                schedule_mode=ScheduleOnce,
                run_at="2026-01-02 03:05:00",
                trigger_mode=TriggerLLM,
                message=test_atriyph_task("tools"),
                tool_list_names=[" web_search ", "web_search", "shell"],
                enabled=True,
                actor=actor,
                source_platform="cli",
            ),
        )
        meta = decode_metadata(job.metadata)
        self.assertEqual(",".join(meta.llm.tool_list_names), "web_search,shell")
        await svc.update(None, PatchRequest(name="tools", title="renamed", actor=actor))
        meta = decode_metadata(repo.jobs["user.cron.tools"].metadata)
        self.assertEqual(",".join(meta.llm.tool_list_names), "web_search,shell")
        await svc.update(None, PatchRequest(name="tools", tool_list_names=[], actor=actor))
        meta = decode_metadata(repo.jobs["user.cron.tools"].metadata)
        self.assertEqual(meta.llm.tool_list_names, [])
        self.assertFalse(svc.is_completed_cron(repo.jobs["user.cron.tools"], meta, CronDeliveryState()))

    async def test_list_hides_completed_cron_by_default(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="done",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerDirect, message="done"),
                target=CronTarget(source_platform="cli"),
            ),
        )
        state = CronDeliveryState(
            run_id="run",
            report_ready=True,
            task_completed=True,
            report="done",
            targets=[
                _target_state(
                    "cli|superadmins", [("text", DeliveryDelivered)]
                )
            ],
        )
        repo.jobs[job.name].delivery_state = must_marshal_delivery(state)
        repo.jobs[job.name].delivery_token = "run"
        self.assertEqual(await svc.list(None, True, False, superadmin()), [])
        views = await svc.list(None, True, True, superadmin())
        self.assertEqual(len(views), 1)
        self.assertIsInstance(views[0], JobView)

    async def test_rejects_non_superadmin_operations(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        user = Actor(id="cli:u", platform="cli", platform_user_id="u", role="user")
        with self.assertRaises(ForbiddenError):
            await svc.create(None, UpsertRequest(name="x", actor=user))
        with self.assertRaises(ForbiddenError):
            await svc.update(None, PatchRequest(name="x", actor=user))
        with self.assertRaises(ForbiddenError):
            await svc.disable(None, "x", user)
        with self.assertRaises(ForbiddenError):
            await svc.delete(None, "x", user)
        with self.assertRaises(ForbiddenError):
            await svc.get(None, "x", user)
        with self.assertRaises(ForbiddenError):
            await svc.list(None, True, True, user)

    async def test_disable_and_delete_via_service(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        self.fixed("2026-01-02 03:04:30")
        await svc.create(
            None,
            UpsertRequest(
                name="live",
                title="live",
                schedule_mode=ScheduleOnce,
                run_at="2026-01-02 03:05:00",
                trigger_mode=TriggerDirect,
                message="提醒",
                enabled=True,
                actor=superadmin(),
                source_platform="cli",
            ),
        )
        await svc.disable(None, "live", superadmin())
        self.assertFalse(repo.jobs["user.cron.live"].enabled)
        await svc.delete(None, "live", superadmin())
        self.assertNotIn("user.cron.live", repo.jobs)

    async def test_get_returns_view(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="view",
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerDirect, message="x"),
                target=CronTarget(source_platform="cli"),
            ),
        )
        view = await svc.get(None, job.name, superadmin())
        self.assertEqual(view.metadata.title, "view")
        self.assertEqual(view.job.name, job.name)


def _target_state(key: str, outputs: list[tuple[str, str]]) -> CronDeliveryTargetState:
    return CronDeliveryTargetState(
        key=key,
        outputs=[CronDeliveryOutputState(id=oid, status=status) for oid, status in outputs],
    )


# ---------------------------------------------------------------------------
# Service：LLM 执行（service_test.go）
# ---------------------------------------------------------------------------
class TestServiceLlmExecution(CronTestCase):
    async def test_run_llm_report_passes_tool_list_names(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":true,"need_report":false,"report":"ok"}')
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
                llm=CronLLMMetadata(tool_list_names=["web_search"]),
            ),
        )
        meta = decode_metadata(job.metadata)
        _, _, err = await svc.run_llm_report(None, job, meta, CronDeliveryState(run_id="run"))
        self.assertIsNone(err)
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(",".join(runner.requests[0].tool_list_names), "web_search")
        self.assertEqual(runner.requests[0].sandbox_subdir, "cron/test")
        self.assertEqual(runner.requests[0].kind, "cron")
        self.assertEqual(runner.requests[0].scope_id, "cron:user.cron.test")
        self.assertEqual(runner.requests[0].metadata, {"cron_job_name": "user.cron.test"})

    async def test_run_llm_report_starts_fresh_session_each_cron_trigger(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(
            text='{"completed":true,"need_report":false,"report":"ok"}',
            session_ids=["cron-session-1", "cron-session-2"],
        )
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
                llm=CronLLMMetadata(tool_list_names=["web_search"]),
            ),
        )
        meta = decode_metadata(job.metadata)
        await svc.run_llm_report(None, job, meta, CronDeliveryState(run_id="run-1"))
        await svc.run_llm_report(None, job, meta, CronDeliveryState(run_id="run-2"))
        self.assertEqual(len(runner.requests), 2)
        self.assertEqual(runner.requests[0].session_id, "")
        self.assertEqual(runner.requests[1].session_id, "")

    async def test_run_llm_report_does_not_reuse_completed_once_cron_output(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(
            text='{"completed":true,"need_report":true,"report":"新报告"}',
            session_ids=["new-session"],
        )
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        # 历史版本把投递数据写在 metadata 的 delivery 段里：新一轮不得复用它。
        job.metadata = (
            job.metadata[:-1]
            + ',"delivery":{"completed":true,"report":"旧报告","report_session_id":"old-session"}}'
        )
        updated, report, err = await svc.run_llm_report(
            None, job, decode_metadata(job.metadata), CronDeliveryState(run_id="new-run")
        )
        self.assertIsNone(err)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.requests[0].session_id, "")
        self.assertEqual(report, "新报告")
        self.assertEqual(updated.report, "新报告")
        self.assertEqual(updated.report_session_id, "new-session")

    async def test_run_llm_report_ignores_legacy_session_id_metadata(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":true,"need_report":false,"report":"ok"}')
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        job.metadata = job.metadata.replace('"llm":{}', '"llm":{"session_id":"legacy-session"}', 1)
        await svc.run_llm_report(
            None, job, decode_metadata(job.metadata), CronDeliveryState(run_id="run")
        )
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(runner.requests[0].session_id, "")

    async def test_run_llm_report_sends_non_empty_report_even_when_need_report_false(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":true,"need_report":false,"report":"仍然汇报"}')
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        meta = decode_metadata(job.metadata)
        updated, report, err = await svc.run_llm_report(
            None, job, meta, CronDeliveryState(run_id="run")
        )
        self.assertIsNone(err)
        prompt = runner.requests[0].prompt
        self.assertIn("Atriyph v0.3规则", prompt)
        self.assertIn("#task test", prompt)
        self.assertIn("最终回复必须是严格 JSON", prompt)
        self.assertEqual(report, "仍然汇报")
        self.assertEqual(updated.report, "仍然汇报")

    async def test_run_llm_report_retries_invalid_json_once(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(
            texts=["不是 JSON", '{"completed":true,"need_report":true,"report":"修正后报告"}']
        )
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
                llm=CronLLMMetadata(tool_list_names=["web_search"]),
            ),
        )
        meta = decode_metadata(job.metadata)
        updated, report, err = await svc.run_llm_report(
            None, job, meta, CronDeliveryState(run_id="run")
        )
        self.assertIsNone(err)
        self.assertEqual(runner.calls, 2)
        self.assertEqual(runner.requests[1].session_id, "cron-session")
        self.assertEqual(",".join(runner.requests[1].tool_list_names), "web_search")
        self.assertIn("你返回的格式有误", runner.requests[1].prompt)
        self.assertEqual(report, "修正后报告")
        self.assertEqual(updated.report, "修正后报告")

    async def test_run_llm_sends_admin_notice_when_retry_still_invalid(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(texts=["不是 JSON", "仍然不是 JSON"])
        sent: list[str] = []

        async def send_target(ctx, target, outputs):
            sent.append(f"{target.platform}:{outputs[0].text}")
            return None

        svc = self.make_service(store=FakeStore(repo), runner=runner, send_target=send_target)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        meta = decode_metadata(job.metadata)
        err = await svc.run_llm(None, job, meta)
        self.assertIsNotNone(err)
        self.assertEqual(runner.calls, 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("cli:cron 任务 LLM 解析格式失败", sent[0])
        self.assertIn("请 /resume 到 cron session 查看详情", sent[0])

    async def test_run_llm_report_requires_runner(self) -> None:
        repo = FakeCronRepo()
        svc = self.make_service(store=FakeStore(repo))
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        state, report, err = await svc.run_llm_report(
            None, job, decode_metadata(job.metadata), CronDeliveryState(run_id="run")
        )
        self.assertIsNotNone(err)
        self.assertFalse(state.report_ready)
        self.assertEqual(report, "")

    async def test_run_llm_report_reports_incomplete_task_with_resume_hint(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text='{"completed":false,"need_report":true,"report":""}')
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        updated, report, err = await svc.run_llm_report(
            None, job, decode_metadata(job.metadata), CronDeliveryState(run_id="run")
        )
        self.assertIsNone(err)
        self.assertFalse(updated.task_completed)
        self.assertIn("任务未完成。", report)
        self.assertIn("session: cron-session", report)

    async def test_run_llm_report_taken_over_session_produces_no_report(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(text="", session_ids=["cron-session"], taken_over_at=0)
        svc = self.make_service(store=FakeStore(repo), runner=runner)
        job = upsert_test_cron_job(
            repo,
            Metadata(
                kind=METADATA_KIND,
                version=1,
                title="LLM",
                schedule=CronSchedule(mode=ScheduleCron, cron_expr="*/5 * * * *"),
                trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                target=CronTarget(source_platform="cli"),
            ),
        )
        updated, report, err = await svc.run_llm_report(
            None, job, decode_metadata(job.metadata), CronDeliveryState(run_id="run")
        )
        self.assertIsNone(err)
        self.assertTrue(updated.report_ready)
        self.assertTrue(updated.task_completed)
        self.assertEqual(updated.report, "")
        self.assertEqual(report, "")
        # 接管时 Text 为空的首次解析必然失败，所以会先发一次同 Session 的格式重试
        # （后端 runner 对已接管的 Session 同样短路返回 TakenOver）；两次之后才判接管。
        self.assertEqual(runner.calls, 2)
        self.assertEqual(runner.requests[1].session_id, "cron-session")

    async def test_handler_writes_run_state_and_logs_error(self) -> None:
        repo = FakeCronRepo()
        runner = FakeRunner(error=RuntimeError("llm down"))
        manager = Manager(repo, location=LOCATION)
        svc = self.make_service(store=FakeStore(repo), manager=manager, runner=runner)
        await manager.register_handler(USER_HANDLER_NAME, svc.handler)
        self.fixed("2026-01-02 03:04:30")
        job = await svc.create(
            None,
            UpsertRequest(
                name="failing",
                title="failing",
                schedule_mode=ScheduleOnce,
                run_at="2026-01-02 03:05:00",
                trigger_mode=TriggerLLM,
                message=test_atriyph_task("failing"),
                enabled=True,
                actor=superadmin(),
                source_platform="cli",
            ),
        )
        await manager.run_job(job.name)
        stored = repo.jobs["user.cron.failing"]
        self.assertEqual(stored.run_count, 1)
        self.assertIn("llm down", stored.last_error)


# ---------------------------------------------------------------------------
# 真实 SQLite 上的行为（service_test.go 的 *SQLite 用例）
# ---------------------------------------------------------------------------
class TestServiceWithSqlite(CronTestCase):
    def make_sqlite_store(self) -> SqliteCronStore:
        path = os.path.join(self.temp_dir("sqlite"), "test.db")
        store = SqliteCronStore(path)
        self.addCleanup(store.close)
        return store

    async def test_run_llm_maps_report_notice_to_background_message(self) -> None:
        store = self.make_sqlite_store()
        runner = FakeRunner(text='{"completed":true,"need_report":true,"report":"报告内容"}')

        async def send_target(ctx, target, outputs):
            if target.platform == "qq-onebot" and target.private_user_id == "1001":
                return Receipt(platform_message_ids=["notice-1"])
            return Receipt()

        svc = self.make_service(
            store=store,
            runner=runner,
            enabled_platforms=[PlatformTarget(name="qq-onebot", superadmin_ids=["1001"])],
            send_target=send_target,
        )
        bg_session = Session(
            id="cron-session",
            owner_id="cli:local",
            platform="cli",
            platform_scope_id="cron:user.cron.test",
            mode="work",
            status="active",
            title="cron",
        )
        store.sessions().create(bg_session)
        store.messages().append(
            Message(id="cron-message", session_id=bg_session.id, role="assistant", content="报告内容")
        )
        job = store.cron_jobs().upsert(
            UpsertCronJobRequest(
                name="user.cron.test",
                handler=USER_HANDLER_NAME,
                schedule="4 3 2 1 *",
                enabled=True,
                metadata=Metadata(
                    kind=METADATA_KIND,
                    version=1,
                    title="总结",
                    schedule=CronSchedule(mode=ScheduleOnce, run_at="2026-01-02 03:04:00"),
                    trigger=CronTrigger(mode=TriggerLLM, message=test_atriyph_task("test")),
                    target=CronTarget(all_enabled_platforms=True, source_platform="cli"),
                ).encode(),
            )
        )
        err = await svc.run_llm(None, job, decode_metadata(job.metadata))
        self.assertIsNone(err)
        message = store.messages().find_by_platform_message(
            "qq-onebot", "private:1001", "notice-1"
        )
        self.assertEqual(message.id, "cron-message")
        self.assertEqual(message.session_id, "cron-session")

    async def test_copy_cron_session_is_visible_on_broadcast_platforms(self) -> None:
        store = self.make_sqlite_store()
        source = Session(
            owner_id="cli:local",
            platform="cli",
            platform_scope_id="cron:user.cron.test",
            mode="work",
            status="active",
            title="cron",
        )
        store.sessions().create(source)
        for role, content in (("user", "task"), ("assistant", "result")):
            store.messages().append(
                Message(session_id=source.id, role=role, content=content)
            )
        svc = self.make_service(
            store=store,
            enabled_platforms=[
                PlatformTarget(name="qq-onebot", superadmin_ids=["1001"]),
                PlatformTarget(name="telegram", superadmin_ids=["2002"]),
            ],
        )
        meta = Metadata(
            title="cron", target=CronTarget(all_enabled_platforms=True, source_platform="cli")
        )
        await svc.copy_session_to_broadcast_targets(None, source.id, meta, "user.cron.test")

        rows = store.query(
            "SELECT * FROM sessions WHERE platform_scope_id='cron:user.cron.test' ORDER BY platform"
        )
        self.assertEqual([row["platform"] for row in rows], ["cli", "qq-onebot", "telegram"])
        qq = [row for row in rows if row["platform"] == "qq-onebot"][0]
        self.assertEqual(qq["owner_id"], "qq-onebot:1001")
        self.assertEqual(
            len(store.messages().list_by_session(qq["id"])), 2
        )
        copied = store.messages().list_by_session(qq["id"])
        self.assertEqual([m.role for m in copied], ["user", "assistant"])
        self.assertEqual([m.content for m in copied], ["task", "result"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
