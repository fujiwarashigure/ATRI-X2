"""沙箱服务：租户环境生命周期、准入队列、配额、重置与审计的统一入口。

工具层只依赖这里的三类能力：

* ``run_shell(ctx, cmd, timeout)`` —— 在**当前用户安全域**的容器里执行命令；
* ``resolve_path(ctx, raw, ...)`` / ``tenant_workspace(ctx)`` —— 把文件工具钉进
  同一租户空间；
* ``status`` / ``reset`` / ``stop_all`` —— 运维与 ``/panic``。

关键不变量（修改清单的验收项）：

1. **fail-closed**：后端不可用 / 隔离探测未通过 / 镜像缺失 → 抛
   :class:`SandboxUnavailableError`，绝不回落宿主 Shell；
2. **租户隔离**：租户键 = 平台 + 机器人 + scope + 用户；不同租户的容器、网络、
   工作区互不可见；
3. **名额先于环境**：先拿到准入队列的名额，再启动/创建环境；
4. **配额不清零**：流量与执行账本在 ``reset`` / ``destroy`` 后仍然保留；
5. **环境坏了重建**：``reset`` 分「重启 / 保留文件重建 / 清空全部文件」三档。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from atri_x.sandbox import lifecycle
from atri_x.sandbox.audit import SandboxAudit
from atri_x.sandbox.backend import (
    CapabilityReport,
    EnvStats,
    ExecResult,
    SandboxBackend,
    ShellOutcome,
)
from atri_x.sandbox.backend_container import ContainerBackend
from atri_x.sandbox.envs import (
    EnvRecord,
    build_env_request,
    check_disk_quota,
    ensure_dirs,
    record_for,
)
from atri_x.sandbox.errors import (
    SandboxBackendError,
    SandboxDeniedError,
    SandboxError,
    SandboxQuotaError,
    SandboxUnavailableError,
)
from atri_x.sandbox.files import export_file, import_file, resolve_workspace_path
from atri_x.sandbox.ledger import UsageLedger
from atri_x.sandbox.queue import AdmissionController
from atri_x.sandbox.settings import SandboxSettings
from atri_x.sandbox.tenant import TenantEnvPaths, TenantKey, tenant_from_context

__all__ = ["SandboxService", "ShellOutcome"]

#: 导入媒体在租户工作区里的子目录。
_IMPORT_DIR = "imports"


class SandboxService:
    """统一沙箱服务。进程内单例由 :mod:`atri_x.sandbox.runtime` 托管。"""

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        backend: SandboxBackend | None = None,
        ledger: UsageLedger | None = None,
        audit: SandboxAudit | None = None,
        clock: Any = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or ContainerBackend(settings)
        self.ledger = ledger or UsageLedger(settings.ledger_path, clock=clock)
        self.audit = audit or SandboxAudit(
            settings.audit_path, max_bytes=settings.audit_max_bytes
        )
        self.queue = AdmissionController(
            capacity=settings.max_environments,
            queue_max_size=settings.queue_max_size,
            per_tenant_limit=settings.queue_max_per_tenant,
            wait_timeout=settings.queue_wait_seconds,
            starvation_rounds=settings.background_starvation_rounds,
        )
        self._clock = clock or time.time
        self._started = False
        self._start_lock = asyncio.Lock()
        self._report: CapabilityReport | None = None
        self._backend_info: Any = None
        self._backend_info_at = 0.0
        self._envs: dict[str, EnvRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._gc_task: asyncio.Task | None = None
        self._closed = False

    # -- 只读属性 -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """是否处于容器沙箱模式。"""
        return self.settings.enabled

    @property
    def report(self) -> CapabilityReport | None:
        """最近一次隔离能力探测结果（未探测时为 ``None``）。"""
        return self._report

    def tenant_paths(self, ctx: Any = None, tenant: TenantKey | None = None) -> TenantEnvPaths:
        """取（并创建）租户目录。``tenant`` 优先，否则从 ctx 推导。"""
        key = tenant or tenant_from_context(ctx)
        paths = TenantEnvPaths.for_root(self.settings.root, key)
        ensure_dirs(paths)
        return paths

    def tenant_workspace(self, ctx: Any = None) -> str:
        """当前用户安全域的工作区宿主路径。"""
        return self.tenant_paths(ctx).workspace

    def resolve_path(
        self,
        ctx: Any,
        raw_path: str,
        *,
        allow_create: bool = False,
        allow_directory: bool = False,
    ) -> str:
        """把工具路径解析进当前租户工作区（越界直接拒绝）。"""
        paths = self.tenant_paths(ctx)
        return resolve_workspace_path(
            paths.workspace,
            raw_path,
            allow_create=allow_create,
            allow_directory=allow_directory,
            allow_host_files=self.settings.allow_host_files,
        )

    # -- 执行 ---------------------------------------------------------------
    async def run_shell(
        self,
        ctx: Any,
        command: str,
        *,
        timeout: float = 0.0,
        background_kind: str = "",
        env: dict[str, str] | None = None,
    ) -> ShellOutcome:
        """在当前用户安全域的沙箱里执行 shell 命令。"""
        tenant = tenant_from_context(ctx)
        kind = "background" if (background_kind or "").strip() else "interactive"
        limit = self._command_timeout(timeout)
        result = await self._execute(
            tenant, command, timeout=limit, kind=kind, ctx=ctx, env=env
        )
        return ShellOutcome(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            tenant=tenant.display,
            truncated=result.truncated,
        )

    async def run_command(
        self,
        tenant: TenantKey,
        command: str,
        *,
        timeout: float = 0.0,
        kind: str = "interactive",
        ctx: Any = None,
    ) -> ExecResult:
        """按显式租户执行命令（框架内部/测试用；工具层用 :meth:`run_shell`）。"""
        return await self._execute(
            tenant, command, timeout=self._command_timeout(timeout), kind=kind, ctx=ctx
        )

    async def _execute(
        self,
        tenant: TenantKey,
        command: str,
        *,
        timeout: float,
        kind: str,
        ctx: Any = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """执行核心流程：启动 → 配额 → 名额 → 环境 → 执行 → 记账。"""
        text = (command or "").strip()
        if not text:
            raise SandboxDeniedError("沙箱命令为空")
        await self._ensure_started()
        slug = tenant.slug
        async with self._tenant_lock(slug):
            await self._pre_exec_quota(tenant)
            record = await self._ensure_env(tenant, kind=kind)
            record.busy += 1
            record.last_used = self._clock()
            try:
                result = await self.backend.exec_in_env(
                    record.name,
                    text,
                    timeout=timeout,
                    workdir="/workspace",
                    env=dict(env or {}),
                    max_output_bytes=self.settings.max_output_bytes,
                )
            except SandboxError:
                self._audit_failure(tenant, "backend_error", ctx)
                self._mark_env_broken(record)
                raise
            finally:
                record.busy = max(0, record.busy - 1)
                record.last_used = self._clock()
                await self._release_on_contention(record)
            await self._sync_traffic(record)
            self.ledger.record_exec(
                slug,
                failure_kind="" if result.ok else (result.error_kind or "exit_nonzero"),
                denied=False,
            )
            self.audit.write(
                "exec",
                tenant=tenant.display,
                slug=slug,
                task=_task_id(ctx),
                image=self.settings.image,
                profile=record.profile,
                network=record.network,
                exit_code=result.exit_code,
                exit_reason=result.error_kind or ("ok" if result.ok else "nonzero"),
                duration_ms=result.duration_ms,
                truncated=result.truncated,
            )
            if not result.ok:
                self._audit_failure(tenant, result.error_kind or "exit_nonzero", ctx)
            return result

    # -- 环境管理 -----------------------------------------------------------
    async def _ensure_env(self, tenant: TenantKey, *, kind: str) -> EnvRecord:
        """确保环境已创建并运行；环境不存在时先拿准入名额再创建。"""
        slug = tenant.slug
        record = self._envs.get(slug)
        if record is not None and record.running:
            return record
        if record is not None and record.lease is not None:
            # 环境被超时/取消杀掉了：名额还在，先尝试重启（工作区数据保留）；
            # 容器被外部删除时退回「重新创建」。
            try:
                await self.backend.start_env(record.name)
                record.running = True
                return record
            except SandboxError:
                self._release_lease(record)
                self._envs.pop(slug, None)
        await self._evict_idle_if_needed()
        lease = await self.queue.acquire(slug, kind=kind)
        record = record_for(self.settings, tenant)
        record.lease = lease
        try:
            ensure_dirs(record.paths)
            check_disk_quota(self.settings, record.paths)
            quota = self.ledger.remaining_bytes(slug, self.settings.network_policy.daily_bytes)
            request = build_env_request(
                self.settings, tenant, record.paths, remaining_quota_bytes=quota
            )
            await self.backend.create_env(request)
        except SandboxError:
            self._release_lease(record)
            raise
        except Exception as exc:  # noqa: BLE001 - 后端异常统一转成沙箱错误
            self._release_lease(record)
            raise SandboxBackendError(
                "创建沙箱环境失败", detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        record.running = True
        record.last_used = self._clock()
        self._envs[slug] = record
        self.audit.write(
            "env_start",
            tenant=tenant.display,
            slug=slug,
            image=self.settings.image,
            profile=record.profile,
            network=record.network,
            backend=self.backend.name,
        )
        return record

    async def _evict_idle_if_needed(self) -> list[str]:
        """名额已满时先回收「正在运行但空闲」的环境（保留其工作区数据）。"""
        if self.queue.snapshot().running < self.queue.capacity:
            return []
        evicted: list[str] = []
        candidates = sorted(
            (item for item in self._envs.values() if item.busy == 0 and item.running),
            key=lambda item: item.last_used,
        )
        for record in candidates:
            await self._stop_record(record, reason="evict")
            evicted.append(record.tenant.slug)
            if self.queue.snapshot().running < self.queue.capacity:
                break
        return evicted

    async def _stop_record(self, record: EnvRecord, *, reason: str) -> None:
        """停止一个环境并归还名额（保留租户数据）。"""
        try:
            await self.backend.stop_env(record.name)
        except SandboxError as exc:
            self.audit.write("env_stop_failed", slug=record.tenant.slug, error=str(exc))
        record.running = False
        self._release_lease(record)
        self.audit.write(
            "env_stop", tenant=record.tenant.display, slug=record.tenant.slug, reason=reason
        )

    def _release_lease(self, record: EnvRecord) -> None:
        """归还准入名额（幂等）。"""
        lease = record.lease
        record.lease = None
        if lease is not None:
            lease.release()

    def _mark_env_broken(self, record: EnvRecord) -> None:
        """后端失败后标记环境不可信：下次使用时重建容器（数据保留）。"""
        record.running = False

    async def reset(self, ctx: Any = None, *, mode: str = "rebuild") -> dict:
        """重置当前用户环境：``restart`` / ``rebuild`` / ``wipe``。"""
        return await lifecycle.reset_tenant(self, tenant_from_context(ctx), mode=mode)

    async def reset_tenant(self, tenant: TenantKey, *, mode: str = "rebuild") -> dict:
        """重置指定租户环境；流量/滥用账本与审计**不会**被清空。"""
        return await lifecycle.reset_tenant(self, tenant, mode=mode)

    async def destroy_tenant(self, tenant: TenantKey) -> dict:
        """销毁租户环境与数据目录（账本保留，用于滥用追溯）。"""
        return await lifecycle.destroy_tenant(self, tenant)

    async def stop_all(self, reason: str = "panic") -> int:
        """紧急停止：取消排队 + 停止全部环境（``/panic`` 用）。"""
        return await lifecycle.stop_all(self, reason)

    async def wait_idle(self) -> None:
        """等待所有正在执行的任务结束（关闭/测试用；有上限）。"""
        for _ in range(100):
            if not any(item.busy for item in self._envs.values()):
                return
            await asyncio.sleep(0.1)

    # -- 状态 ---------------------------------------------------------------
    async def status(self, *, refresh: bool = False) -> dict:
        """GUI / doctor 用的只读快照。"""
        info = await self._backend_info_cached(refresh=refresh)
        envs = [record.dump() for record in self._envs.values()]
        if refresh:
            try:
                envs = [item.dump() for item in await self.backend.list_envs()]
            except SandboxError:
                pass
        return {
            "settings": self.settings.dump(),
            "backend": info.dump(),
            "probe": self._report.dump() if self._report is not None else None,
            "queue": self.queue.snapshot().dump(),
            "environments": envs,
            "ledger": self.ledger.snapshot(),
            "audit_writable": self.audit.writable(),
            "started": self._started,
        }

    async def probe(self, *, refresh: bool = False) -> CapabilityReport:
        """隔离能力探测（缓存；``refresh=True`` 重新实测）。"""
        if self._report is not None and not refresh:
            return self._report
        self._report = await self.backend.probe()
        return self._report

    async def env_stats(self, tenant: TenantKey) -> EnvStats:
        """读取环境资源占用（GUI 用）。"""
        record = self._envs.get(tenant.slug)
        if record is None:
            raise SandboxDeniedError("该租户没有正在运行的环境")
        return await self.backend.env_stats(record.name)

    async def _backend_info_cached(self, *, refresh: bool = False) -> Any:
        """后端可用性快照（30s 缓存，避免 GUI 频繁拉起引擎命令）。"""
        now = self._clock()
        if self._backend_info is None or refresh or now - self._backend_info_at > 30:
            self._backend_info = await self.backend.backend_info()
            self._backend_info_at = now
        return self._backend_info

    # -- 启动与回收 ---------------------------------------------------------
    async def _ensure_started(self) -> None:
        """首次执行前：探测后端与隔离能力，回收上次崩溃遗留的容器。"""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            info = await self.backend.backend_info()
            if not info.available:
                raise SandboxUnavailableError(
                    "沙箱后端不可用，已拒绝执行（不会回落宿主 Shell）",
                    detail=info.error or f"{self.settings.backend} 不可用",
                )
            if self.settings.require_probe:
                report = await self.probe(refresh=True)
                if not report.ok:
                    names = ", ".join(item.name for item in report.failed)
                    raise SandboxUnavailableError(
                        "沙箱隔离能力探测未通过，已拒绝执行",
                        detail=f"失败项：{names or '未知'}；见 `atri-x sandbox doctor`",
                    )
            try:
                removed = await self.backend.cleanup_orphans(set(), set())
                if removed:
                    self.audit.write("orphans_cleaned", removed=removed)
            except SandboxError:
                pass
            self._started = True
            self._start_gc()
            self.audit.write(
                "service_start",
                backend=self.backend.name,
                image=self.settings.image,
                profile=self.settings.profile,
                network=self.settings.network,
            )

    def _start_gc(self) -> None:
        """启动后台空闲回收/保留期清理任务（只启动一次）。"""
        if self._gc_task is not None and not self._gc_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._gc_task = loop.create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        """空闲环境停止 + 过期租户数据清理（实现见 :mod:`atri_x.sandbox.lifecycle`）。"""
        await lifecycle.gc_loop(self)

    async def _gc_once(self) -> None:
        """执行一轮回收（实现见 lifecycle）。"""
        await lifecycle.gc_once(self)

    async def _release_on_contention(self, record: EnvRecord) -> None:
        """有人在排队时，执行完的环境立刻让出名额（空闲环境不占着运行资源）。

        环境停止但工作区/私有 home 保留；下一个命令进来时按需重启。
        """
        if record.busy > 0 or not record.running:
            return
        if self.queue.snapshot().waiting <= 0:
            return
        self._envs.pop(record.tenant.slug, None)
        await self._stop_record(record, reason="contention")

    async def _pre_exec_quota(self, tenant: TenantKey) -> None:
        """执行前检查流量额度；超限时强制断网并给出明确原因。"""
        limit = self.settings.network_policy.daily_bytes
        try:
            self.ledger.check_quota(tenant.slug, limit)
        except SandboxQuotaError as exc:
            record = self._envs.get(tenant.slug)
            if record is not None and record.online:
                try:
                    await self.backend.set_online(
                        record.name, False, self.settings.network_policy
                    )
                    record.online = False
                except SandboxError:
                    pass
            self.ledger.record_exec(tenant.slug, failure_kind="quota", denied=True)
            self.audit.write(
                "quota_denied", tenant=tenant.display, slug=tenant.slug, error=str(exc)
            )
            raise

    async def _sync_traffic(self, record: EnvRecord) -> None:
        """把代理侧流量计数增量写进账本（配额跨容器重建累计）。"""
        path = os.path.join(record.paths.proxy_state, "stats.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        up = int(data.get("up_bytes", 0) or 0)
        down = int(data.get("down_bytes", 0) or 0)
        delta_up = max(0, up - record.proxy_up_bytes)
        delta_down = max(0, down - record.proxy_down_bytes)
        record.proxy_up_bytes = max(record.proxy_up_bytes, up)
        record.proxy_down_bytes = max(record.proxy_down_bytes, down)
        if delta_up or delta_down:
            self.ledger.add_traffic(record.tenant.slug, delta_up, delta_down)

    def _audit_failure(self, tenant: TenantKey, kind: str, ctx: Any) -> None:
        """记录一次失败（分类写审计）。"""
        self.audit.write(
            "exec_failed",
            tenant=tenant.display,
            slug=tenant.slug,
            task=_task_id(ctx),
            error_kind=kind,
        )

    def _command_timeout(self, timeout: float) -> float:
        """命令时限：0 = 普通档；显式超过普通档时用长任务档，且受硬顶约束。"""
        resources = self.settings.resources
        normal = float(resources.command_timeout_s)
        requested = float(timeout or 0)
        if requested <= 0:
            return normal
        if requested <= normal:
            return requested
        return min(requested, float(resources.long_task_timeout_s))

    def _tenant_lock(self, slug: str) -> asyncio.Lock:
        """同一租户内的命令串行（避免互相改坏文件）。"""
        lock = self._locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[slug] = lock
        return lock

    # -- 文件空间 -----------------------------------------------------------
    async def import_media(self, ctx: Any, source_path: str, *, name: str = "") -> str:
        """把控制面的媒体文件复制进租户工作区，返回容器内路径。"""
        tenant = tenant_from_context(ctx)
        paths = self.tenant_paths(tenant=tenant)
        filename = (name or os.path.basename(source_path) or "import.bin").strip()
        filename = os.path.basename(filename).replace("\\", "_").replace("/", "_")
        dest = os.path.join(paths.workspace, _IMPORT_DIR, filename)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(filename)
            dest = os.path.join(paths.workspace, _IMPORT_DIR, f"{stem}-{int(self._clock())}{ext}")
        await asyncio.to_thread(
            import_file,
            source_path,
            dest,
            max_bytes=self.settings.import_max_bytes,
            workspace=paths.workspace,
        )
        return "/workspace/" + _IMPORT_DIR + "/" + os.path.basename(dest)

    def export_path(self, ctx: Any, raw_path: str) -> str:
        """把租户空间里的产物解析成宿主路径（供发送工具读取）。"""
        tenant = tenant_from_context(ctx)
        paths = TenantEnvPaths.for_root(self.settings.root, tenant)
        return export_file(
            resolve_workspace_path(
                paths.workspace,
                raw_path,
                allow_host_files=self.settings.allow_host_files,
            ),
            max_bytes=self.settings.export_max_bytes,
        )

    # -- 关闭 ---------------------------------------------------------------
    async def aclose(self) -> None:
        """关闭服务：停止 GC、回收环境、断开后端（应用退出用）。"""
        self._closed = True
        if self._gc_task is not None:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._gc_task = None
        for record in list(self._envs.values()):
            try:
                await self.backend.stop_env(record.name)
            except SandboxError:
                continue
            self._release_lease(record)
        self._envs.clear()
        await self.backend.aclose()


def _task_id(ctx: Any) -> str:
    """从 ctx 取回合请求 id（取不到返回空串，审计字段可空）。"""
    try:
        from atri_x.agent.request_context import turn_request_id_from_context

        return str(turn_request_id_from_context(ctx) or "")
    except Exception:  # noqa: BLE001 - 审计字段不应影响执行
        return ""
