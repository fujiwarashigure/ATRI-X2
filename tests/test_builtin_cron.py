"""``cron_query`` / ``cron_write`` 工具层的端到端回归：真实 ``cron.Service`` + 假存储。

**为什么有这个文件**：第二轮对抗性验证的 P0-5 —— ``atri_x/cron/**`` 自己的用例全绿，
但工具层的 ``call()`` 从来没有用**真实** ``Service`` 跑过，于是 ``_create`` / ``_update``
漏 await、``disable`` / ``delete`` 把协程丢掉之后照样返回 ``{"ok": true}``。
本文件每条用例都断言**存储里的真实状态**（``FakeCronRepo.jobs`` / ``get_by_name``），
而不是只看返回值里的 ``ok``；假存储与 :class:`~atri_x.cron.Service` 的接缝（同步/异步）
也用 :func:`inspect.iscoroutinefunction` 显式锁住。

对应 Go：``internal/tool/builtin/cron.go`` 的 ``CronQueryTool`` / ``CronWriteTool``
（Go 的 ``Service`` 六个 CRUD 方法都是同步的，见 ``internal/cron/service.go``）。
"""

from __future__ import annotations

import inspect
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from atri_x.cron import (
    Options,
    Service,
    TriggerDirect,
    decode_metadata,
)
from atri_x.errors import ForbiddenError, NotFoundError
from atri_x.security import Actor, RoleSuperadmin, with_actor
from atri_x.storage.ids import new_id
from atri_x.storage.models import CronJob, UpsertCronJobRequest
from atri_x.storage.times import now as storage_now
from atri_x.tool.builtin.cron import (
    CRON_QUERY_TOOL_NAME,
    CRON_WRITE_TOOL_NAME,
    CronQueryTool,
    CronWriteTool,
)
from atri_x.tool.runtimeinfo import Info
from atri_x.tool.tool import CallRequest

#: 固定时区（UTC+8）与固定时钟：不依赖机器时区、不等真实时间。
LOCATION = timezone(timedelta(hours=8))
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=LOCATION)

#: Service 会把任务名规范化成 ``user.cron.<name>``。
JOB = "user.cron.p0probe"

#: Go ``json.Marshal(storage.CronJob)`` 的键（该 struct 没有 json tag → 键即字段名）。
GO_CRON_JOB_KEYS = {
    "ID",
    "Name",
    "Handler",
    "Schedule",
    "Enabled",
    "Metadata",
    "DeliveryState",
    "DeliveryToken",
    "LastRunAt",
    "NextRunAt",
    "RunCount",
    "LastError",
    "CreatedAt",
    "UpdatedAt",
}


class FakeCronRepo:
    """最小假存储。对应 Go ``internal/cron/manager_test.go`` 的 ``fakeCronRepo``。

    只实现 :class:`~atri_x.cron.Service` 在 ``manager is None`` 时用到的四个方法；
    行为对齐 ``cron_job_repository.go``：``delete`` / ``disable`` 对不存在的名字抛
    ``NotFoundError``（Go 的 ``ErrNotFound``），而不是静默成功。
    """

    def __init__(self) -> None:
        self.jobs: dict[str, CronJob] = {}

    def upsert(self, req: UpsertCronJobRequest) -> CronJob:
        job = self.jobs.get(req.name)
        if job is None:
            job = CronJob(id=new_id(), name=req.name, created_at=storage_now())
            self.jobs[req.name] = job
        job.handler = req.handler
        job.schedule = req.schedule
        job.enabled = req.enabled
        job.metadata = req.metadata
        job.next_run_at = req.next_run_at
        job.updated_at = storage_now()
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

    def disable_by_name(self, name: str) -> None:
        job = self.jobs.get(name)
        if job is None:
            raise NotFoundError(f"cron job {name!r} not found")
        job.enabled = False
        job.next_run_at = None

    def delete_by_name(self, name: str) -> None:
        if name not in self.jobs:
            raise NotFoundError(f"cron job {name!r} not found")
        del self.jobs[name]


class FakeStore:
    """Store 门面 fake：只暴露 cron 仓库。对应 Go ``fakeCronStore``。"""

    def __init__(self, repo: FakeCronRepo) -> None:
        self.repo = repo

    def cron_jobs(self) -> FakeCronRepo:
        return self.repo


def superadmin() -> Actor:
    """cli 超管主体（cron 工具是 ``superadmin_only``）。"""
    return Actor(
        id="cli:local", platform="cli", platform_user_id="local", role=RoleSuperadmin
    )


def plain_user() -> Actor:
    """普通用户：Service 的 ``requireSuperadmin`` 必须拒绝。"""
    return Actor(id="cli:guest", platform="cli", platform_user_id="guest")


class CronToolE2ETestCase(unittest.IsolatedAsyncioTestCase):
    """真实 ``Service`` + 假存储；工具只经 ``call()`` 驱动。"""

    def setUp(self) -> None:
        self.repo = FakeCronRepo()
        self.service = Service(
            Options(
                store=FakeStore(self.repo),
                location=LOCATION,
                now=lambda: NOW,
            )
        )
        self.info = Info(now=lambda: NOW)
        self.write = CronWriteTool(self.service, self.info)
        self.query = CronQueryTool(self.service, self.info)
        self.ctx = with_actor(None, superadmin())

    # -- helpers -----------------------------------------------------------
    def _request(self, tool_name: str, **args: object) -> CallRequest:
        return CallRequest(
            id="call-1",
            name=tool_name,
            arguments=json.dumps(args, ensure_ascii=False),
        )

    async def write_call(self, **args: object) -> str:
        result = await self.write.call(
            self.ctx, self._request(CRON_WRITE_TOOL_NAME, **args)
        )
        return result.content

    async def query_call(self, **args: object) -> str:
        result = await self.query.call(
            self.ctx, self._request(CRON_QUERY_TOOL_NAME, **args)
        )
        return result.content

    async def create_job(self, name: str = "p0probe") -> None:
        await self.write_call(
            operation="create",
            name=name,
            title="P0 probe",
            schedule_mode="cron",
            cron_expr="*/5 * * * *",
            trigger_mode="direct",
            message="hello",
        )

    def stored(self, name: str = JOB) -> CronJob:
        """直接读假存储里的任务行（不经 Service 的查询路径；返回快照）。"""
        return replace(self.repo.jobs[name])

    # -- 契约锁 -------------------------------------------------------------
    def test_service_crud_methods_are_async(self) -> None:
        """Service 的 CRUD 是 ``async``：工具侧必须 await，不允许照抄 Go 的同步调用。"""
        for name in ("create", "update", "disable", "delete", "get", "list"):
            method = getattr(Service, name)
            self.assertTrue(
                inspect.iscoroutinefunction(method), f"Service.{name} 应当是 async"
            )
        # 工具侧对应入口也必须是 async，否则 ``call()`` 无法 await 服务。
        for method in (
            CronWriteTool.call,
            CronWriteTool._create,
            CronWriteTool._update,
            CronQueryTool.call,
        ):
            self.assertTrue(inspect.iscoroutinefunction(method), method.__qualname__)

    # -- create ------------------------------------------------------------
    async def test_write_create_really_creates_the_job(self) -> None:
        content = await self.write_call(
            operation="create",
            name="p0probe",
            title="P0 probe",
            schedule_mode="cron",
            cron_expr="*/5 * * * *",
            trigger_mode="direct",
            message="hello",
        )

        # 1) 存储里真的有一行（不是只有返回值的 ok）。
        self.assertEqual(sorted(self.repo.jobs), [JOB])
        job = self.stored()
        self.assertTrue(job.enabled)
        self.assertEqual(job.handler, "builtin.cron")
        self.assertEqual(job.schedule, "*/5 * * * *")
        self.assertIsNotNone(job.next_run_at)
        self.assertTrue(job.id)

        # 2) 落库的 metadata 来自工具入参。
        meta = decode_metadata(job.metadata)
        self.assertEqual(meta.title, "P0 probe")
        self.assertEqual(meta.trigger.mode, TriggerDirect)
        self.assertEqual(meta.trigger.message, "hello")
        self.assertEqual(meta.schedule.mode, "cron")
        self.assertEqual(meta.schedule.cron_expr, "*/5 * * * *")
        self.assertEqual(meta.target.source_platform, "cli")

        # 3) 返回值是 Go 的 ``{"job": CronJob, "ok": true}`` 形态。
        payload = json.loads(content)
        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["job"]), GO_CRON_JOB_KEYS)
        self.assertEqual(payload["job"]["Name"], JOB)
        self.assertIsNone(payload["job"]["LastRunAt"])

    async def test_write_create_once_resolves_relative_run_at(self) -> None:
        await self.write_call(
            operation="create",
            name="p0once",
            title="P0 once",
            schedule_mode="once",
            run_after_minutes=5,
            trigger_mode="direct",
            message="hi",
        )
        job = self.stored("user.cron.p0once")
        meta = decode_metadata(job.metadata)
        self.assertEqual(meta.schedule.mode, "once")
        self.assertEqual(meta.schedule.run_at, "2026-03-04 05:11:07")
        # once 是分钟级调度：落库的 cron 表达式取那一刻的分/时/日/月。
        self.assertEqual(job.schedule, "11 5 4 3 *")

    # -- update ------------------------------------------------------------
    async def test_write_update_really_updates_the_store(self) -> None:
        await self.create_job()
        before = self.stored()
        content = await self.write_call(
            operation="update",
            name="p0probe",
            title="Updated",
            message="hello2",
            enabled=False,
        )

        after = self.stored()
        self.assertNotEqual(after.metadata, before.metadata)
        meta = decode_metadata(after.metadata)
        self.assertEqual(meta.title, "Updated")
        self.assertEqual(meta.trigger.message, "hello2")
        # 没传的字段保持原值（Go 的 PatchRequest 指针语义）。
        self.assertEqual(meta.schedule.mode, "cron")
        self.assertEqual(meta.schedule.cron_expr, "*/5 * * * *")
        self.assertFalse(after.enabled)
        self.assertTrue(json.loads(content)["ok"])

    async def test_write_update_missing_job_raises_instead_of_ok(self) -> None:
        with self.assertRaises(NotFoundError):
            await self.write_call(operation="update", name="nope", title="x")
        self.assertEqual(self.repo.jobs, {})

    # -- disable -----------------------------------------------------------
    async def test_write_disable_really_disables_the_job(self) -> None:
        await self.create_job()
        self.assertTrue(self.stored().enabled)

        content = await self.write_call(operation="disable", name="p0probe")

        self.assertEqual(content, '{"ok":true}')
        self.assertFalse(self.stored().enabled)
        self.assertIsNone(self.stored().next_run_at)
        # 停用但保留记录（Go 的 Disable 语义）。
        self.assertIn(JOB, self.repo.jobs)

    async def test_write_disable_missing_job_raises_instead_of_ok(self) -> None:
        with self.assertRaises(NotFoundError):
            await self.write_call(operation="disable", name="nope")

    # -- delete ------------------------------------------------------------
    async def test_write_delete_really_removes_the_job(self) -> None:
        await self.create_job()
        self.assertIn(JOB, self.repo.jobs)

        content = await self.write_call(operation="delete", name="p0probe")

        self.assertEqual(content, '{"ok":true}')
        # 关键断言：任务真的没了 —— 存储为空、按名取抛 NotFound、查询列表为空。
        self.assertEqual(self.repo.jobs, {})
        with self.assertRaises(NotFoundError):
            self.repo.get_by_name(JOB)
        self.assertEqual(await self.query_call(), "[]")

    async def test_write_delete_missing_job_raises_instead_of_ok(self) -> None:
        with self.assertRaises(NotFoundError):
            await self.write_call(operation="delete", name="nope")

    # -- 权限 ---------------------------------------------------------------
    async def test_non_superadmin_write_is_rejected_and_store_unchanged(self) -> None:
        self.ctx = with_actor(None, plain_user())
        with self.assertRaises(ForbiddenError):
            await self.create_job()
        self.assertEqual(self.repo.jobs, {})

        self.ctx = with_actor(None, superadmin())
        await self.create_job()
        self.ctx = with_actor(None, plain_user())
        with self.assertRaises(ForbiddenError):
            await self.write_call(operation="delete", name="p0probe")
        self.assertIn(JOB, self.repo.jobs)

    # -- query -------------------------------------------------------------
    async def test_query_lists_and_gets_real_jobs(self) -> None:
        await self.create_job("p0probe")
        await self.create_job("p0second")

        listed = json.loads(await self.query_call())
        self.assertEqual(
            sorted(view["job"]["Name"] for view in listed),
            ["user.cron.p0probe", "user.cron.p0second"],
        )
        self.assertEqual(set(listed[0]), {"job", "metadata", "delivery"})

        single = json.loads(await self.query_call(name="p0second"))
        self.assertEqual(single["job"]["Name"], "user.cron.p0second")
        self.assertEqual(single["metadata"]["title"], "P0 probe")

    async def test_query_respects_include_disabled(self) -> None:
        await self.create_job()
        await self.write_call(operation="disable", name="p0probe")

        self.assertEqual(json.loads(await self.query_call()), [])
        listed = json.loads(await self.query_call(include_disabled=True))
        self.assertEqual([view["job"]["Name"] for view in listed], [JOB])

    async def test_query_get_missing_job_propagates_the_error(self) -> None:
        with self.assertRaises(NotFoundError):
            await self.query_call(name="user.cron.missing")


if __name__ == "__main__":
    unittest.main()
