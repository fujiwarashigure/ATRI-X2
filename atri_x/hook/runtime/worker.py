"""一个有状态 Hook 进程（hook.v2 worker）。

对应 Go：``internal/hook/runtime/worker.go``。

结构对照：

* Go 的 ``run()`` goroutine → :meth:`Worker.run` 协程任务；
* Go 的 ``readLoop`` / ``waitLoop`` goroutine → 两个 ``asyncio`` 任务；
* Go 的 ``pending map[string]chan frame`` → ``pending: dict[str, _Pending]``，
  每个请求一个 ``asyncio.Future``；
* Go 的 ``lockRoute``（每路由一个容量 1 的 channel）→ 每路由一个
  ``asyncio.Lock``。

值语义：Go 的 ``handle`` 收到的是 ``Event`` 的**副本**，所以内部改动不会回传
调用方；Python 的 ``Event`` 是可变对象，因此这里在入口显式 ``clone()``，保持
与 Go 相同的可观察行为。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from atri_x.errors import AtriXError
from atri_x.hook.event import Event, Point
from atri_x.hook.match import event_match_context
from atri_x.hook.output import BuildOptions, build_group, build_message_segments
from atri_x.hook.protocol import apply_llm_result
from atri_x.hook.protocol.frames import Frame
from atri_x.hook.output.jsonstrict import JSONDecodeError, decode_json  # noqa: F401
from atri_x.hook.runtime import tool_bridge, transport
from atri_x.hook.runtime.config import (
    Config,
    Info,
    Mode,
    Status,
    log_warning,
    resolve_cwd,
)
from atri_x.hook.runtime.protocol import (
    PROTOCOL_VERSION,
    EventHandle,
    EventResult,
    SystemHook,
    SystemInit,
    must_json,
)
from atri_x.hook.runtime.routes import (
    RouteKey,
    begin_invocation,
    clear_lease,
    end_invocation,
    put_tool_context,
    route_key_for,
    set_lease,
)
from atri_x.llm import llm

__all__ = [
    "WorkerError",
    "WorkerProcessError",
    "WorkerTimeoutError",
    "Worker",
]

#: 启动就绪的轮询间隔（Go 用 10ms ticker）。
READY_POLL_SECONDS = 0.01


class WorkerError(AtriXError):
    """worker 生命周期错误。对应 Go 里这一层的 ``fmt.Errorf``。"""


class WorkerProcessError(WorkerError):
    """进程没在跑 / 提前退出。"""


class WorkerTimeoutError(WorkerError):
    """等待超时（对应 Go ``context.DeadlineExceeded``）。"""


class Worker:
    """一个 hook.v2 子进程。对应 Go ``worker``。"""

    def __init__(self, manager: Any, config: Config) -> None:
        self.manager = manager
        self.config = config
        # 并发说明：Go 用 sync.Mutex；临界区里不含 await，所以这里用 RLock
        # （与 atri_x.hook.manager 的既有做法一致）。
        self.mu = threading.RLock()
        self.status = Status.STOPPED
        self.detail = ""
        self.proc: Any = None
        self.stdin: Any = None
        self.pending: dict[str, transport.Pending] = {}
        self.starting = False
        self.stopping = False
        self.manual_stop = False
        self.restart_delay = 0.0
        self.done: "asyncio.Future[None] | None" = None
        self.active = 0
        self.reload: Callable[[], Any] | None = None
        self.reload_armed = False
        self.reload_prep = False
        self.write_mu = asyncio.Lock()
        self._route_locks: dict[RouteKey, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task] = []

    # -- 状态 ---------------------------------------------------------------
    async def start(self) -> None:
        """请求启动（幂等，由 :meth:`run` 再判一次状态）。"""
        with self.mu:
            self.manual_stop = False
        await self.manager.start_worker(self)

    def info(self) -> Info:
        with self.mu:
            return Info(
                id=self.config.id,
                description=self.config.description,
                mode=self.config.mode_or_once(),
                status=self.status,
                detail=self.detail,
                active=self.active,
            )

    def set_status(self, status: str, detail: str = "") -> None:
        with self.mu:
            self.status = status
            self.detail = (detail or "").strip()

    def _status(self) -> tuple[str, str]:
        with self.mu:
            return self.status, self.detail

    async def wait_ready(self, ctx: Any = None) -> None:
        """等进程进入 ready/running。对应 Go ``worker.waitReady``。"""
        timeout = float(self.config.startup_timeout_seconds or 0)
        if timeout <= 0:
            timeout = 1.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status, detail = self._status()
            if status in (Status.READY, Status.RUNNING):
                return
            if status in (Status.FAILED, Status.DEGRADED):
                raise WorkerError(
                    f"hook worker {self.config.id!r} failed to start: "
                    f"{detail or status}"
                )
            _raise_if_cancelled(ctx)
            if loop.time() >= deadline:
                raise WorkerError(
                    f"hook worker {self.config.id!r} startup timed out"
                )
            await asyncio.sleep(READY_POLL_SECONDS)

    # -- 主循环 -------------------------------------------------------------
    async def run(self) -> None:
        """启动子进程并完成 ``system.init`` 握手。对应 Go ``worker.run``。"""
        loop = asyncio.get_running_loop()
        with self.mu:
            if (
                self.proc is not None
                or self.starting
                or self.stopping
                or self.manual_stop
            ):
                return
            self.starting = True
            self.status = Status.STARTING
            self.detail = ""
            self.done = loop.create_future()

        argv = list(self.config.command or [])
        if not argv or not (argv[0] or "").strip():
            await self._start_failed(WorkerError("runtime command is required"))
            return
        try:
            cwd = resolve_cwd(self.config.dir, self.config.cwd)
        except AtriXError as exc:
            await self._start_failed(exc)
            return

        process_env = self.manager.opts.process_env
        if self.config.process_env.configured():
            process_env = self.config.process_env
        command = process_env.command(argv[0], *argv[1:])

        with self.manager.mu:
            closed = self.manager.closed
        with self.mu:
            if closed or self.proc is not None or self.stopping or self.manual_stop:
                self.starting = False
                self.status = Status.STOPPED
                return
        try:
            proc = await asyncio.create_subprocess_exec(
                *command.args,
                executable=command.executable or None,
                cwd=cwd,
                env=command.env_dict(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            with self.mu:
                self.starting = False
                self.status = Status.FAILED
                self.detail = str(exc).strip()
            if self.should_restart():
                self._spawn_restart()
            return
        with self.mu:
            if self.stopping or self.manual_stop:
                self.starting = False
            self.proc = proc
            self.stdin = proc.stdin
            self.starting = False
            self.pending = {}

        self._tasks = [
            asyncio.ensure_future(self.read_loop(proc)),
            asyncio.ensure_future(self.wait_loop(proc)),
            asyncio.ensure_future(self._stderr_loop(proc)),
        ]

        init = SystemInit(
            version=PROTOCOL_VERSION,
            hook=SystemHook(
                id=self.config.id,
                description=self.config.description,
                dir=self.config.dir,
                cwd=cwd,
                shared_dir=self.manager.opts.shared_dir,
            ),
            tools=self.schemas(),
        )
        token = _with_timeout(
            self.manager.root_token, float(self.config.startup_timeout_seconds)
        )
        try:
            await self.request("system.init", init, ctx=token)
        except BaseException as exc:  # noqa: BLE001 - 启动失败一律标记并杀进程
            self.set_status(Status.FAILED, str(exc))
            await self.kill()
            return
        finally:
            _cancel_of(token)()
        with self.mu:
            self.restart_delay = float(self.config.restart.initial_delay_seconds)
        self.set_status(Status.READY, "")

    async def _start_failed(self, err: BaseException) -> None:
        with self.mu:
            self.starting = False
            self.status = Status.FAILED
            self.detail = str(err).strip()
        if self.should_restart():
            self._spawn_restart()

    # -- 协议（实现在 transport.py） ----------------------------------------
    async def request(
        self,
        method: str,
        params: Any,
        timeout: float | None = None,
        ctx: Any = None,
    ) -> str | None:
        """发一个请求并等响应。对应 Go ``worker.request``。"""
        return await transport.request(self, method, params, timeout, ctx)

    async def write(self, frame: Frame) -> None:
        """写一帧到插件 stdin。对应 Go ``worker.write``。"""
        await transport.write(self, frame)

    async def read_loop(self, proc: Any) -> None:
        """读插件 stdout。对应 Go ``worker.readLoop``。"""
        await transport.read_loop(self, proc)

    def handle_plugin_event(self, value: Frame) -> None:
        """插件主动上报的事件（目前只有 ``hook.log``）。对应 Go ``handlePluginEvent``。"""
        transport.handle_plugin_event(self, value)

    async def kill(self) -> None:
        """强杀进程树。对应 Go ``worker.kill`` + ``killHookProcessTree``。"""
        await transport.kill(self)

    async def notify_cancel(self, conversation_id: str) -> None:
        """通知插件事件已取消。对应 Go ``worker.notifyCancel``。"""
        await transport.notify_cancel(self, conversation_id)

    async def _stderr_loop(self, proc: Any) -> None:
        """把子进程 stderr 逐行写进日志。对应 Go 的 ``stderrLogger``。"""
        await transport.stderr_loop(self, proc)

    async def wait_loop(self, proc: Any) -> None:
        """等进程退出并做收尾。对应 Go ``waitLoop``。"""
        try:
            returncode = await proc.wait()
        except asyncio.CancelledError:  # pragma: no cover - 关闭竞态
            raise
        with self.mu:
            stopping = self.stopping
            manual_stop = self.manual_stop
            self.proc = None
            self.stdin = None
            pending, self.pending = self.pending, {}
            done, self.done = self.done, None
        for item in pending.values():
            if not item.future.done():
                item.future.set_exception(
                    WorkerProcessError(
                        f"hook process exited while waiting for {item.method}"
                    )
                )
        if done is not None and not done.done():
            done.set_result(None)
        if not stopping and not manual_stop:
            detail = "hook process exited"
            if returncode:
                detail = (
                    f"exit status {returncode}"
                    if returncode > 0
                    else f"signal {-returncode}"
                )
            self.set_status(Status.DEGRADED, detail)
            with self.manager.mu:
                self.manager.clear_routes_locked(self.config.id)
                for key, transient in list(self.manager.transient.items()):
                    if transient is self:
                        del self.manager.transient[key]
            if self.should_restart():
                self._spawn_restart()
            return
        with self.mu:
            self.stopping = False
            self.status = Status.STOPPED
            self.detail = ""

    # -- 重启 ---------------------------------------------------------------
    def should_restart(self) -> bool:
        """对应 Go ``worker.shouldRestart``。"""
        return self.config.mode_or_once() == Mode.PERSISTENT and (
            (self.config.restart.strategy or "") in ("on_failure", "always")
        )

    def _spawn_restart(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 同步上下文里不重启
            return
        loop.create_task(self.restart_later())

    async def restart_later(self) -> None:
        """按指数退避重启。对应 Go ``worker.restartLater``。"""
        with self.mu:
            delay = self.restart_delay or float(
                self.config.restart.initial_delay_seconds or 0
            )
            nxt = delay * 2
            max_delay = float(self.config.restart.max_delay_seconds or 0)
            self.restart_delay = min(nxt, max_delay) if max_delay else nxt
        if delay <= 0:
            return
        if await self.manager.wait_root(delay):
            return
        with self.mu:
            stopping = self.stopping or self.manual_stop
        if not stopping:
            await self.start()

    # -- 停止 ---------------------------------------------------------------
    async def stop(self, ctx: Any = None) -> None:
        """优雅关闭；超时则杀进程树。对应 Go ``worker.stop``。"""
        with self.mu:
            waiting_only = self.stopping
            if not waiting_only:
                self.stopping = True
                self.manual_stop = True
            proc = self.proc
            done = self.done
        shutdown = float(self.config.shutdown_timeout_seconds or 0)
        if waiting_only:
            if done is not None:
                await self._wait_done(done, shutdown, ctx)
            return
        if proc is None:
            with self.mu:
                self.stopping = False
                self.status = Status.STOPPED
                self.detail = ""
            return
        self.set_status(Status.STOPPING, "")
        token = _with_timeout(ctx, shutdown)
        try:
            await self.request("system.shutdown", {}, ctx=token)
        except asyncio.CancelledError:
            # 关闭期被取消：直接强杀，绝不让子进程拖住事件循环收尾。
            await self.kill()
            _cancel_of(token)()
            with self.mu:
                self.stopping = False
            raise
        except BaseException:  # noqa: BLE001 - 关停请求失败不阻断关闭
            pass
        finally:
            _cancel_of(token)()
        graceful = await self._wait_done(done, shutdown, ctx)
        if not graceful:
            await self.kill()
            await self._wait_done(done, shutdown, ctx, kill_phase=True)
        with self.mu:
            self.stopping = False

    async def _wait_done(
        self, done: Any, timeout: float, ctx: Any, kill_phase: bool = False
    ) -> bool:
        if done is None:
            return True
        if timeout <= 0:
            timeout = 1.0
        try:
            await asyncio.wait_for(asyncio.shield(done), timeout)
            return True
        except asyncio.TimeoutError:
            if kill_phase:
                raise WorkerError(
                    f"hook worker {self.config.id!r} did not exit after kill"
                )
            return False
        except asyncio.CancelledError:
            if _is_cancelled(ctx):
                if kill_phase:
                    raise WorkerError(
                        f"hook worker {self.config.id!r} did not exit after kill"
                    ) from None
                return False
            raise

    # -- 事件处理 -----------------------------------------------------------
    async def handle(
        self, ctx: Any, event: Event, continuation: bool, defaults: Any
    ) -> Event:
        """把一个事件交给子进程处理。对应 Go ``worker.handle``。"""
        event = event.clone()
        key = route_key_for(event)
        unlock = await self._lock_route(ctx, key)
        try:
            status, _detail = self._status()
            started = status in (Status.READY, Status.RUNNING)
            with self.mu:
                if started:
                    self.active += 1
            if not started:
                raise WorkerError(
                    f"hook worker {self.config.id!r} is {status}"
                )
            try:
                return await self._handle_locked(ctx, event, continuation, defaults, key)
            finally:
                await self._finish_handle()
        finally:
            unlock()

    async def _handle_locked(
        self, ctx: Any, event: Event, continuation: bool, defaults: Any, key: RouteKey
    ) -> Event:
        self.set_status(Status.RUNNING, "")
        timeout = float(self.config.event_timeout_seconds or 0)
        token = _with_timeout(ctx, timeout)
        begin_invocation(self.manager, key, self.config.id, _cancel_of(token))
        try:
            tool_token = put_tool_context(
                self.manager, self.config.id, token, event, timeout
            )
            params = EventHandle(
                event=event,
                match=event_match_context(event),
                continuation=continuation,
                tool_context=tool_token,
            )
            try:
                response = await self.request("event.handle", params, ctx=token)
            except BaseException as exc:  # noqa: BLE001
                if _is_context_cancelled(exc):
                    await self.notify_cancel(event.platform.conversation_id)
                raise
            result = EventResult()
            if response:
                result = decode_json(
                    response,
                    lambda obj: EventResult.from_object(obj, "event result"),
                )
            event = apply_llm_result(event, result.llm)
            status = (result.status or "").strip()
            if status in ("", "completed"):
                pass
            elif status == "waiting":
                await set_lease(self.manager, self.config.id, event, result, defaults)
            else:
                raise WorkerError(f"unsupported hook event status {result.status!r}")
            outputs = build_group(
                result.group(), BuildOptions(base_dir=self.config.dir)
            )
            event.outputs.extend(outputs)
            if result.message is not None and result.message.segments is not None:
                segments = build_message_segments(
                    result.message.segments, self.config.dir
                )
                event.message.segments = segments
                if event.point == Point.TOOL_CALL_COMPLETED:
                    event.tool.result = llm.segments_text_only(segments)
            elif result.message is not None and result.message.text is not None:
                event.message.segments = llm.set_segment_text(
                    event.message.segments, result.message.text
                )
                if event.point == Point.TOOL_CALL_COMPLETED:
                    event.tool.result = llm.segments_text_only(event.message.segments)
            consume = bool(getattr(defaults, "consume", False))
            stop_propagation = bool(getattr(defaults, "stop_propagation", False))
            if result.pass_through is not None:
                consume = not result.pass_through
                stop_propagation = not result.pass_through
            if consume:
                event.control.consume = True
            if stop_propagation:
                event.control.stop_propagation = True
            if continuation and status != "waiting":
                clear_lease(self.manager, self.config.id, event)
            return event
        finally:
            _cancel_of(token)()
            end_invocation(self.manager, key, _cancel_of(token))
            with self.mu:
                if self.proc is not None and self.status == Status.RUNNING:
                    self.status = Status.READY

    async def _finish_handle(self) -> None:
        with self.mu:
            if self.active > 0:
                self.active -= 1
            reload = self._take_reload_locked()
        self._run_reload(reload)

    async def _lock_route(self, ctx: Any, key: RouteKey) -> Callable[[], None]:
        """同一路由串行化。对应 Go ``worker.lockRoute``。"""
        with self.mu:
            lock = self._route_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._route_locks[key] = lock
        await _await_cancelable(lock.acquire(), ctx)
        return lock.release

    # -- 插件请求（见 tool_bridge.py） --------------------------------------
    async def handle_plugin_request(self, value: Frame) -> None:
        """处理插件发来的请求并回写响应。对应 Go ``handlePluginRequest``。"""
        result, error = await self.plugin_request(value)
        response = Frame(type="response", id=value.id, ok=error is None)
        if error is not None:
            response.error = str(error)
        else:
            response.result = must_json(result)
        write_error: BaseException | None = None
        try:
            await self.write(response)
        except AtriXError as exc:
            write_error = exc
        if write_error is not None and self.manager.opts.logger is not None:
            log_warning(
                self.manager.opts.logger,
                "write hook request response failed",
                hook=self.config.id,
                method=value.method,
                error=str(write_error),
            )
        if value.method == "hooks.reload" and error is None:
            if write_error is None:
                self.arm_reload()
            else:
                self.discard_reload()

    async def plugin_request(self, value: Frame) -> tuple[Any, BaseException | None]:
        """对应 Go ``worker.pluginRequest``（实现在 tool_bridge.py）。"""
        return await tool_bridge.plugin_request(self, value)

    async def call_tool(self, raw: Any) -> Any:
        """对应 Go ``worker.callTool``（实现在 tool_bridge.py）。"""
        return await tool_bridge.call_tool(self, raw)

    def schemas(self) -> list[Any]:
        """对应 Go ``worker.schemas``。"""
        return tool_bridge.schemas(self)

    # -- 自热重载 -----------------------------------------------------------
    async def prepare_self_reload(self) -> Any:
        """准备一次自热重载（真正生效等当前调用结束）。对应 Go ``prepareSelfReload``。"""
        with self.mu:
            if self.status not in (Status.READY, Status.RUNNING):
                raise WorkerError(
                    f"stateful hook {self.config.id!r} cannot reload while {self.status}"
                )
            if self.reload is not None or self.reload_prep:
                raise WorkerError(
                    f"stateful hook {self.config.id!r} already has a reload scheduled"
                )
            self.reload_prep = True
        try:
            commit = await self.manager.prepare_plugin_reload(self.config.id)
        finally:
            with self.mu:
                self.reload_prep = False
        if commit is None:
            raise WorkerError("hook plugin reload returned an empty commit")
        with self.mu:
            self.reload = commit
        return {"scheduled": True}

    def arm_reload(self) -> None:
        """标记重载已就绪；当前没有在跑就立刻提交。对应 Go ``armReload``。"""
        with self.mu:
            self.reload_armed = True
            reload = self._take_reload_locked()
        self._run_reload(reload)

    def discard_reload(self) -> None:
        """丢弃待提交的重载。对应 Go ``discardReload``。"""
        with self.mu:
            self.reload = None
            self.reload_armed = False

    def _take_reload_locked(self) -> Callable[[], Any] | None:
        if not self.reload_armed or self.active > 0 or self.reload is None:
            return None
        reload = self.reload
        self.reload = None
        self.reload_armed = False
        return reload

    def _run_reload(self, reload: Callable[[], Any] | None) -> None:
        if reload is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            return
        loop.create_task(self._run_reload_task(reload))

    async def _run_reload_task(self, reload: Callable[[], Any]) -> None:
        try:
            result = reload()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except BaseException as exc:  # noqa: BLE001 - 热重载失败只记日志
            log_warning(
                self.manager.opts.logger,
                "hook plugin reload failed",
                hook=self.config.id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _with_timeout(ctx: Any, timeout: float) -> Any:
    """派生一个带截止时间的取消令牌。对应 Go ``context.WithTimeout``。"""
    from atri_x.safecontext import CancelToken, with_timeout

    parent = ctx if isinstance(ctx, CancelToken) else None
    if timeout <= 0:
        return with_timeout(parent, None)
    return with_timeout(parent, timeout)


def _cancel_of(token: Any) -> Callable[[], None]:
    cancel = getattr(token, "cancel", None)
    if callable(cancel):
        return lambda: cancel()
    return lambda: None


def _raise_if_cancelled(ctx: Any) -> None:
    if ctx is None:
        return
    checker = getattr(ctx, "is_cancelled", None)
    if callable(checker) and checker():
        error = getattr(ctx, "error", None)
        raise (error() if callable(error) and error() else AtriXError("canceled"))


def _is_cancelled(ctx: Any) -> bool:
    if ctx is None:
        return False
    checker = getattr(ctx, "is_cancelled", None)
    return bool(callable(checker) and checker())


def _is_context_cancelled(err: BaseException) -> bool:
    """对应 Go 的 ``errors.Is(err, context.Canceled)``。"""
    from atri_x.safecontext import CancelledError

    return isinstance(err, CancelledError)


async def _await_cancelable(awaitable: Any, ctx: Any) -> Any:
    """等待 ``awaitable``，``ctx`` 取消时抛它自己的错误。"""
    from atri_x.hook.protocol.stream import await_cancelable

    return await await_cancelable(awaitable, ctx)
