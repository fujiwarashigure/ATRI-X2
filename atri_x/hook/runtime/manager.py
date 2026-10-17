"""有状态 Hook 运行时的协调者：应用配置、启停 worker、路由事件。

对应 Go：``internal/hook/runtime/manager.go``（路由细节见
:mod:`atri_x.hook.runtime.routes`，媒体桥见
:mod:`atri_x.hook.runtime.media`）。

并发说明：Go 用 ``sync.RWMutex``。这里用 ``threading.RLock``（与
``atri_x.hook.manager`` 的既有做法一致）：所有临界区都不含 ``await``，
而 ``Apply`` / ``Close`` 可能被配置热重载线程调用。
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Callable

from atri_x.errors import AtriXError, ClosedError, ErrClosed
from atri_x.hook.event import Event
from atri_x.hook.runtime.config import (
    Config,
    Info,
    Mode,
    Options,
    Status,
    RuntimeConfigError,
    log_warning,
)
from atri_x.hook.runtime.media import MediaBridge, new_media_bridge
from atri_x.hook.runtime.routes import (
    DISPATCHED_METADATA_KEY,
    SKIP_HOOK_ID_METADATA_KEY,
    RouteKey,
    clear_routes_locked,
    has_lease,
    route,
    route_hook_id,
    spawn,
)
from atri_x.hook.runtime.shared import (
    SHARED_CLEANUP_INTERVAL,
    SharedState,
)
from atri_x.hook.runtime.worker import Worker
from atri_x.safecontext import CancelToken, with_cancel

__all__ = ["ManagerError", "Manager", "new_manager"]


class ManagerError(AtriXError):
    """运行期管理错误。对应 Go 里这一层的 ``fmt.Errorf``。"""


class Manager:
    """持久/瞬态 Hook worker 的管理器。对应 Go ``Manager``。"""

    def __init__(self, opts: Options | None = None) -> None:
        self.opts = opts or Options()
        if (self.opts.shared_dir or "").strip():
            try:
                os.makedirs(self.opts.shared_dir, exist_ok=True)
            except OSError:  # pragma: no cover - 目录建不出来不致命
                pass
        self.mu = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.transient: dict[RouteKey, Worker] = {}
        self.configs: dict[str, Config] = {}
        self.routes: dict[RouteKey, Any] = {}
        self.running: dict[RouteKey, Any] = {}
        self.tokens: dict[str, Any] = {}
        self.shared = SharedState()
        self.media: MediaBridge = new_media_bridge(self.opts.media)
        self.closed = False
        self.close_error: BaseException | None = None
        self.root_token: CancelToken = with_cancel(None)
        self._prepare_reload: Callable[[str], Any] | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._start_tasks: set[asyncio.Task] = set()
        self._close_done: asyncio.Future | None = None
        self._close_future: asyncio.Future | None = None

    # -- 生命周期 -----------------------------------------------------------
    def _ensure_cleanup(self) -> None:
        """惰性启动过期清理任务（Go 在 ``NewManager`` 里就 go 出去了）。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 无循环时不启动
            return
        self._cleanup_task = loop.create_task(self._clean_shared_state())

    async def _clean_shared_state(self) -> None:
        token = self.root_token
        while not token.is_cancelled():
            if await self.wait_root(SHARED_CLEANUP_INTERVAL):
                return
            self.shared.prune_expired()
            try:
                await self.media.prune(None, close_all=False)
            except AtriXError as exc:
                log_warning(self.opts.logger, "prune hook media", error=str(exc))

    async def wait_root(self, delay: float) -> bool:
        """等 ``delay`` 秒或被根令牌取消；被取消返回 ``True``。"""
        token = self.root_token
        if token.is_cancelled():
            return True
        waiter = asyncio.ensure_future(token.wait())
        try:
            done, _pending = await asyncio.wait({waiter}, timeout=delay)
        finally:
            waiter.cancel()
        return bool(done)

    def shared_state(self) -> SharedState:
        """对应 Go ``Manager.SharedState``。"""
        return self.shared

    def set_plugin_reload_preparer(self, fn: Callable[[str], Any] | None) -> None:
        """注册自热重载的准备函数。对应 Go ``SetPluginReloadPreparer``。"""
        with self.mu:
            self._prepare_reload = fn

    async def prepare_plugin_reload(self, id: str) -> Any:
        """对应 Go ``preparePluginReload``。"""
        with self.mu:
            prepare = self._prepare_reload
        if prepare is None:
            raise ManagerError("hook plugin reload is not configured")
        result = prepare(id)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        return result

    # -- 应用配置 -----------------------------------------------------------
    async def apply(self, configs: list[Config] | None = None) -> None:
        """按配置重建 worker 集合。对应 Go ``Apply``。

        persistent 立即启动；transient 的进程等触发规则命中才起。
        """
        self._ensure_cleanup()
        next_configs: dict[str, Config] = {}
        for config in configs or []:
            self.validate_config(config)
            if config.id in next_configs:
                raise ManagerError(f"duplicate hook worker {config.id!r}")
            next_configs[config.id] = config
        next_workers: dict[str, Worker] = {}
        workers: list[Worker] = []
        for ident, config in next_configs.items():
            if config.mode_or_once() != Mode.PERSISTENT:
                continue
            worker = Worker(self, config)
            next_workers[ident] = worker
            workers.append(worker)

        with self.mu:
            if self.closed:
                raise ClosedError("hook runtime manager is closed")
            previous = list(self.workers.values())
            previous_transient = list(self.transient.values())
            self.workers = next_workers
            self.transient = {}
            self.configs = next_configs
            previous_ids: set[str] = set()
            for old in previous:
                previous_ids.add(old.config.id)
                clear_routes_locked(self, old.config.id)
            for token in list(self.tokens):
                value = self.tokens[token]
                if value.hook_id in previous_ids:
                    del self.tokens[token]
        for old in previous:
            await old.stop(None)
        for old in previous_transient:
            await old.stop(None)
        for worker in workers:
            await self.start_worker(worker)

    def validate_config(self, config: Config) -> None:
        """校验一个 worker 配置与它的工具允许清单。对应 Go ``validateConfig``。"""
        config.validate()
        seen: set[str] = set()
        for name in config.tools.allow:
            name = (name or "").strip()
            if not name or name in seen:
                raise RuntimeConfigError(
                    "runtime tools.allow contains an empty or duplicate tool name"
                )
            seen.add(name)
            if self.opts.registry is None:
                raise RuntimeConfigError("runtime tool registry is not configured")
            tool, ok = self.opts.registry.get(name)
            if not ok or tool is None:
                raise RuntimeConfigError(
                    f"runtime tools.allow references unknown tool {name!r}"
                )
        for name in config.tools.background_allow:
            name = (name or "").strip()
            if not name or name not in seen:
                raise RuntimeConfigError(
                    f"runtime tools.background_allow tool {name!r} must also appear "
                    "in tools.allow"
                )

    async def validate_plugin(self, config: Config) -> None:
        """校验一次自热重载是否被允许。对应 Go ``ValidatePlugin``。"""
        if not config.is_worker():
            raise ManagerError(f"hook {config.id!r} is not a worker")
        self.validate_config(config)
        with self.mu:
            current = self.configs.get(config.id)
        if current is None:
            raise ManagerError(f"hook worker {config.id!r} is not configured")
        if (
            current.dir != config.dir
            or current.config_path != config.config_path
            or current.mode_or_once() != config.mode_or_once()
        ):
            raise ManagerError(
                f"hook worker {config.id!r} cannot change plugin path or mode "
                "during self reload"
            )

    async def replace_plugin(self, config: Config) -> None:
        """替换一个 worker 定义并（持久模式）重启它。对应 Go ``ReplacePlugin``。"""
        self._ensure_cleanup()
        await self.validate_plugin(config)
        ident = (config.id or "").strip()
        with self.mu:
            if self.closed:
                raise ClosedError("hook runtime manager is closed")
            old = self.workers.get(ident)
            self.configs[ident] = config
            clear_routes_locked(self, ident)
            for token in list(self.tokens):
                if self.tokens[token].hook_id == ident:
                    del self.tokens[token]
            if config.mode_or_once() == Mode.PERSISTENT:
                self.workers[ident] = Worker(self, config)
            else:
                self.workers.pop(ident, None)
            nxt = self.workers.get(ident)
        if old is not None:
            await old.stop(None)
        if nxt is not None:
            await self.start_worker(nxt)

    async def close(self, ctx: Any = None) -> None:
        """关闭全部 worker 并回收媒体租约。对应 Go ``Close``。"""
        with self.mu:
            if self.closed:
                done = self._close_done
            else:
                self.closed = True
                done = None
                workers = list(self.workers.values()) + list(self.transient.values())
                self.routes = {}
                self.running = {}
                self.tokens = {}
        if done is not None:
            await self._await_close(done, ctx)
            return
        self.root_token.cancel("hook runtime manager closed")
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        with self.mu:
            self._close_done = done
        await self._finish_close(workers, done)
        await self._await_close(done, ctx)

    async def _await_close(self, done: asyncio.Future, ctx: Any) -> None:
        if done.done():
            if self.close_error is not None:
                raise self.close_error
            return
        try:
            await done
        except asyncio.CancelledError:  # pragma: no cover - 关闭期取消
            raise
        if self.close_error is not None:
            raise self.close_error

    async def _finish_close(
        self, workers: list[Worker], done: asyncio.Future
    ) -> None:
        errors: list[BaseException] = []
        results = await asyncio.gather(
            *(worker.stop(None) for worker in workers), return_exceptions=True
        )
        for item in results:
            if isinstance(item, BaseException):
                errors.append(item)
        try:
            await self.media.prune(None, close_all=True)
        except AtriXError as exc:
            errors.append(exc)
        self.close_error = errors[0] if errors else None
        if not done.done():
            done.set_result(None)

    # -- worker 起停 --------------------------------------------------------
    async def start_worker(self, worker: Worker) -> None:
        """起一个 worker 任务。对应 Go ``startWorker``。"""
        if worker is None:
            return
        with self.mu:
            if self.closed:
                raise ClosedError("hook runtime manager is closed")
        task = asyncio.ensure_future(worker.run())
        self._start_tasks.add(task)
        task.add_done_callback(self._start_tasks.discard)

    def is_closed(self) -> bool:
        """对应 Go ``isClosed``。"""
        with self.mu:
            return self.closed

    def list(self) -> list[Info]:
        """列出全部有状态 Hook。对应 Go ``List``。"""
        with self.mu:
            items: dict[str, Info] = {}
            for ident, config in self.configs.items():
                items[ident] = Info(
                    id=ident,
                    description=config.description,
                    mode=config.mode_or_once(),
                )
            for ident, worker in self.workers.items():
                items[ident] = worker.info()
            for worker in self.transient.values():
                info = items.get(worker.config.id)
                if not info or not info.id:
                    info = Info(
                        id=worker.config.id,
                        description=worker.config.description,
                        mode=Mode.TRANSIENT,
                    )
                worker_info = worker.info()
                info.active += worker_info.active
                if worker_info.status == Status.RUNNING:
                    info.status = Status.RUNNING
                elif not info.status:
                    info.status = worker_info.status
                items[worker.config.id] = info
            for lease in self.routes.values():
                info = items.get(lease.hook_id)
                if info is None:
                    info = Info(id=lease.hook_id)
                info.waiting += 1
                items[lease.hook_id] = info
        out = list(items.values())
        for info in out:
            if info.mode == Mode.TRANSIENT and not info.status:
                info.status = Status.STOPPED
        out.sort(key=lambda item: item.id)
        return out

    async def start(self, id: str) -> None:
        """启动一个持久 Hook 并等它就绪。对应 Go ``Start``。"""
        if self.is_closed():
            raise ClosedError("hook runtime manager is closed")
        worker = self.worker(id)
        if worker is None:
            raise ManagerError(f"persistent hook {id!r} not found")
        await worker.start()
        await worker.wait_ready(None)

    async def stop(self, ctx: Any, id: str) -> bool:
        """停止一个 worker（含全部瞬态实例）。对应 Go ``Stop``。"""
        ident = (id or "").strip()
        persistent = self.worker(ident)
        if persistent is not None:
            await persistent.stop(ctx)
            with self.mu:
                clear_routes_locked(self, ident)
            return True
        with self.mu:
            config = self.configs.get(ident)
            if config is None or config.mode_or_once() != Mode.TRANSIENT:
                raise ManagerError(f"hook worker {ident!r} not found")
            workers: list[Worker] = []
            for key, transient in list(self.transient.items()):
                if transient.config.id == ident:
                    del self.transient[key]
                    self.routes.pop(key, None)
                    workers.append(transient)
            clear_routes_locked(self, ident)
        for worker in workers:
            await worker.stop(ctx)
        return True

    async def restart(self, ctx: Any, id: str) -> None:
        """重启一个 Hook。对应 Go ``Restart``。"""
        await self.stop(ctx, id)
        await self.start(id)

    def worker(self, id: str) -> Worker | None:
        """按 ID 取持久 worker。对应 Go ``worker``。"""
        with self.mu:
            return self.workers.get((id or "").strip())

    # -- 事件派发 -----------------------------------------------------------
    async def handle(
        self, ctx: Any, id: str, event: Event, defaults: Any
    ) -> Event:
        """把事件交给某个有状态 Hook。对应 Go ``Manager.Handle``。"""
        if has_lease(self, event):
            return event
        if event.metadata.get(SKIP_HOOK_ID_METADATA_KEY) == id:
            return event
        with self.mu:
            config = self.configs.get(id)
        if config is None:
            raise ManagerError(f"hook worker {id!r} is not configured")
        if config.mode_or_once() == Mode.PERSISTENT:
            worker = self.worker(id)
        elif config.mode_or_once() == Mode.TRANSIENT:
            from atri_x.hook.runtime.routes import route_key_for

            worker = await self.start_transient(ctx, config, route_key_for(event))
        else:
            raise ManagerError(f"hook {id!r} is not configured as a worker")
        if worker is None:
            raise ManagerError(f"hook worker {id!r} is not available")
        if worker.config.block.blocks(event):
            return event
        from atri_x.hook.runtime.routes import route_key_for

        transient = config.mode_or_once() == Mode.TRANSIENT
        try:
            updated = await worker.handle(ctx, event, False, defaults)
        except BaseException:
            # Go: ``err != nil || !m.hasLease(event)`` —— 出错也要回收瞬态 worker。
            if transient:
                self.stop_transient(route_key_for(event), worker)
            raise
        if transient and not has_lease(self, event):
            self.stop_transient(route_key_for(event), worker)
        if updated.metadata is None:
            updated.metadata = {}
        updated.metadata[DISPATCHED_METADATA_KEY] = True
        return updated

    async def start_transient(
        self, ctx: Any, config: Config, key: RouteKey
    ) -> Worker | None:
        """为一个路由起瞬态 worker。对应 Go ``startTransient``。"""
        with self.mu:
            if self.closed:
                raise ClosedError("hook runtime manager is closed")
            worker = self.transient.get(key)
            created = False
            if worker is None:
                worker = Worker(self, config)
                self.transient[key] = worker
                created = True
        if created:
            await self.start_worker(worker)
        try:
            await worker.wait_ready(ctx)
        except BaseException:
            self.stop_transient(key, worker)
            raise
        return worker

    def stop_transient(self, key: RouteKey, wanted: Worker | None = None) -> None:
        """停掉一个瞬态 worker。对应 Go ``stopTransient``。"""
        with self.mu:
            worker = self.transient.get(key)
            if worker is None or (wanted is not None and worker is not wanted):
                return
            del self.transient[key]
            self.routes.pop(key, None)
        spawn(lambda: worker.stop(None))

    # -- 路由（实现见 routes.py） -------------------------------------------
    def has_lease(self, event: Event) -> bool:
        """对应 Go ``hasLease``。"""
        return has_lease(self, event)

    def route_hook_id(self, event: Event) -> str:
        """对应 Go ``RouteHookID``。"""
        return route_hook_id(self, event)

    async def route(self, ctx: Any, event: Event) -> tuple[Event, bool]:
        """对应 Go ``Route``。调用方负责先跑普通 Hook。"""
        self._ensure_cleanup()
        return await route(self, ctx, event)

    async def cancel(self, event: Event) -> bool:
        """对应 Go ``Cancel``。"""
        from atri_x.hook.runtime.routes import cancel

        return await cancel(self, event)

    def clear_routes_locked(self, id: str) -> None:
        """对应 Go ``clearRoutesLocked``（需持有 ``mu``）。"""
        clear_routes_locked(self, id)

    # -- 媒体 ---------------------------------------------------------------
    async def media_request(
        self, ctx: Any, base_dir: str, method: str, raw: Any
    ) -> Any:
        """``media.*`` 请求入口。对应 Go ``Manager.MediaRequest``。"""
        if self.media is None or self.media.api is None:
            raise ManagerError("hook media API is not configured")
        if self.is_closed():
            raise ClosedError("hook runtime manager is closed")
        return await self.media.request(ctx, base_dir, method, raw)


def new_manager(opts: Options | None = None) -> Manager:
    """对应 Go ``NewManager``。"""
    return Manager(opts)


# 交叉检索：Go 里的哨兵 ``ErrClosed``。
ErrManagerClosed = ErrClosed
