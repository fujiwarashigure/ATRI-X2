"""平台装配与运行循环。

对应 Go：``internal/app/platforms.go``。

包含四块：

1. :class:`DefaultPlatformFactory` —— 用 ``internal/platform/builtin`` 造出主平台与
   运行时列表。**延迟导入** ``atri_x.platform.builtin``（并行移植期可能尚未交付）。
2. :class:`DefaultPlatformExecutor` + :func:`run_platforms` —— 启动全部平台运行时并
   等它们结束，处理"非预期退出"与"交互式 CLI 退出即应用退出"两种情况。
3. Hook / 补全 / 命令目录 / 平台发送器的注册助手。
4. 启动期 Hook 告警的目标选择与补投递。

与 Go 的差异：

* goroutine + ``sync.WaitGroup`` → ``asyncio.Task`` + ``asyncio.gather``；
  ``sync.Mutex`` 保护的 ``running`` 集合在单线程事件循环里不需要锁，
  但保留一个 :class:`asyncio.Lock` 以便将来在 task 之间原子更新。
* Go 的 ``time.NewTicker(10*time.Second)`` → 心跳循环里的 ``asyncio.sleep(10)``。
* ``service`` 模式下即使全部运行时退出也继续等外部停止，让 ``/live``、``/ready``、
  ``/tasks`` 与 systemd/Docker 重启策略仍然有效（逐字复刻 Go 的这段语义）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_x.app.app import RUN_MODE_SERVICE, RunMode
from atri_x.app.dependencies import PlatformComponents, PlatformRequest
from atri_x.app.environment import platform_mode
from atri_x.app.platform_caller import resolve_platform_caller
from atri_x.app.support import maybe_await
from atri_x.delivery import Notice, Output, Target, text as text_output
from atri_x.errors import InvalidError
from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.manager import Registration
from atri_x.hook.match import always

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "DefaultPlatformFactory",
    "defaultPlatformFactory",
    "DefaultPlatformExecutor",
    "defaultPlatformExecutor",
    "runPlatforms",
    "run_platforms",
    "registerCompletionPlatforms",
    "register_completion_platforms",
    "registerSessionBrowsers",
    "register_session_browsers",
    "registerCommandCatalogs",
    "register_command_catalogs",
    "registerPlatformHooks",
    "register_platform_hooks",
    "registerCronPlatformHook",
    "register_cron_platform_hook",
    "platformStopsAppOnExit",
    "platform_stops_app_on_exit",
    "notifyPlatformFailure",
    "notify_platform_failure",
    "hookNoticeTarget",
    "hook_notice_target",
    "deliverHookNotices",
    "deliver_hook_notices",
    "runtimeNames",
    "runtime_names",
    "AtrinisRuntimeAdapter",
    "atrinisRuntimeAdapter",
]

#: 对应 Go ``time.NewTicker(10 * time.Second)``。
HEARTBEAT_INTERVAL_SECONDS = 10.0

#: ``ERROR`` 级别的数值（``logging.ERROR``），用于告警消息的 level 字段。
_LEVEL_WARNING = 30
_LEVEL_ERROR = 40


class DefaultPlatformFactory:
    """对应 Go ``defaultPlatformFactory``。"""

    async def build(self, req: PlatformRequest) -> PlatformComponents:
        """对应 Go ``Build``；``atri_x.platform.builtin`` 延迟导入。"""
        foundation = req.foundation
        if foundation is None:
            raise InvalidError("app: platform factory requires foundation components")
        from atri_x.platform.builtin import New as new_platform_bundle
        from atri_x.platform.builtin import Options as BuiltinOptions

        bundle = new_platform_bundle(
            BuiltinOptions(mode=platform_mode(req.mode)),
            foundation.config,
            foundation.store,
            foundation.chat_history,
            foundation.logger,
        )
        if req.profiler is not None:
            req.profiler.mark("platform init")
        return PlatformComponents(
            primary=getattr(bundle, "primary", None),
            runtimes=list(getattr(bundle, "runtimes", None) or []),
        )

    # Go 命名别名。
    Build = build


class DefaultPlatformExecutor:
    """对应 Go ``defaultPlatformExecutor``。"""

    async def run(self, ctx: Any, req: Any) -> None:
        """对应 Go ``Run(ctx, req)``。"""
        return await run_platforms(
            ctx,
            req.handler,
            req.logger,
            req.runtimes,
            req.after_start,
            req.heartbeat,
            req.mode,
            req.platform_failure,
        )

    # Go 命名别名。
    Run = run


def _adapter_name(adapter: Any) -> str:
    name = getattr(adapter, "name", None)
    if callable(name):
        return str(name() or "")
    return str(name or "")


def _runtime_names(runtimes: Any) -> list[str]:
    return [name for name in (_adapter_name(item) for item in runtimes or []) if name]


def runtime_names(runtimes: Any) -> list[str]:
    """对应 Go ``runtimeNames``：跳过 ``atrinis``（它不是消息平台）。"""
    out: list[str] = []
    for runtime in runtimes or []:
        if runtime is None:
            continue
        name = _adapter_name(runtime).strip()
        if name and name != "atrinis":
            out.append(name)
    return out


def register_completion_platforms(agent: Any, adapters: Any) -> None:
    """给支持补全的适配器装上补全服务。对应 Go ``registerCompletionPlatforms``。"""
    if agent is None:
        return
    completer = getattr(agent, "completion_service", None)
    service = completer() if callable(completer) else None
    for adapter in adapters or []:
        if adapter is None:
            continue
        setter = getattr(adapter, "set_completer", None)
        if callable(setter):
            setter(service)


def register_session_browsers(agent: Any, adapters: Any) -> None:
    """给支持会话浏览的适配器装上只读浏览器（Python 版新增）。

    对应装配时机与 :func:`register_completion_platforms` 相同：Agent 已就绪、
    平台还没启动。没有 ``sessions`` 服务的 Agent 桩直接跳过。
    """
    sessions = getattr(agent, "sessions", None) if agent is not None else None
    if sessions is None:
        return
    # ``atri_x.session.browse`` 是本仓的硬模块；缺失时应当直接失败，而不是
    # 静默跳过会话浏览扩展（研究报告 §5.1 的静默降级样例）。
    from atri_x.session.browse import SessionBrowser

    browser = SessionBrowser(sessions)
    for adapter in adapters or []:
        if adapter is None:
            continue
        setter = getattr(adapter, "set_session_browser", None)
        if callable(setter):
            setter(browser)


def register_command_catalogs(agent: Any, adapters: Any) -> None:
    """给支持命令目录的适配器装上命令清单。对应 Go ``registerCommandCatalogs``。"""
    if agent is None:
        return
    infos = getattr(agent, "command_infos", None)
    commands = infos() if callable(infos) else []
    for adapter in adapters or []:
        if adapter is None:
            continue
        setter = getattr(adapter, "set_command_catalog", None)
        if callable(setter):
            setter(commands)


def register_platform_hooks(agent: Any, adapters: Any, health_state: Any) -> None:
    """注册平台发送器与连接通知。对应 Go ``registerPlatformHooks``。"""
    if agent is None:
        return
    register = getattr(agent, "register_platform_sender", None)
    for adapter in adapters or []:
        if adapter is None:
            continue
        name = _adapter_name(adapter)
        if callable(register):
            register(name, adapter)
        notifier = getattr(adapter, "set_connect_notifier", None)
        if not callable(notifier):
            continue
        if health_state is not None:
            expect = getattr(health_state, "expect_platform", None)
            if callable(expect):
                expect(name)
        notifier(_connect_notifier(health_state, name))
        login_notifier = getattr(adapter, "set_login_notifier", None)
        if callable(login_notifier) and health_state is not None:
            login_notifier(_login_notifier(health_state, name))


def _connect_notifier(health_state: Any, default_name: str) -> Any:
    """构造"平台已连接"回调：只发布事实，订阅方自己排队执行。"""

    def on_connect(ctx: Any, platform_name: str = "") -> Any:
        name = (platform_name or "").strip() or default_name
        if health_state is not None:
            mark = getattr(health_state, "mark_platform_connected", None)
            if callable(mark):
                mark(name)
        from atri_x.platform import ConnectedEvent, PlatformConnected

        emit = getattr(PlatformConnected, "emit", None)
        if callable(emit):
            return emit(ctx, ConnectedEvent(platform=name))
        return None

    return on_connect


def _login_notifier(health_state: Any, default_name: str) -> Any:
    """构造"登录态已刷新"回调（Python 侧新增）。

    只登记事实，不派发事件：QQ 登录成功后不需要触发"平台已连接"那套钩子，
    但 ``/healthz`` 必须能把"WS 连上"与"QQ 已登录"分开显示。
    """

    def on_login(ctx: Any, platform_name: str = "", state: Any = None) -> None:
        name = (platform_name or "").strip() or default_name
        mark = getattr(health_state, "mark_platform_login", None)
        if not callable(mark):
            return
        payload = state if isinstance(state, dict) else {}
        mark(
            name,
            backend=str(payload.get("backend") or ""),
            login_ok=bool(payload.get("login_ok")),
            self_id=str(payload.get("self_id") or ""),
            nickname=str(payload.get("nickname") or ""),
            app_name=str(payload.get("app_name") or ""),
        )

    return on_login


def register_cron_platform_hook(hooks: Any, service: Any) -> None:
    """平台连接时补投递 missed once cron。对应 Go ``registerCronPlatformHook``。"""
    if hooks is None or service is None:
        return
    register = getattr(hooks, "register", None)
    if not callable(register):
        return

    async def handle(ctx: Any, event: Event) -> Event:
        platform_ctx = getattr(event, "platform", None)
        name = str(getattr(platform_ctx, "name", "") or "")
        await maybe_await(service.notify_platform_connected(ctx, name))
        return event

    register(
        Registration(
            point=Point.PLATFORM_CONNECTED,
            name="builtin.cron.missed_once",
            description="平台连接时补投递 missed once cron",
            match=always(),
            handler=HandlerFunc(handle),
        )
    )


def platform_stops_app_on_exit(adapter: Any) -> bool:
    """对应 Go ``platformStopsAppOnExit``。"""
    method = getattr(adapter, "stop_app_on_exit", None)
    return bool(method()) if callable(method) else False


async def notify_platform_failure(
    ctx: Any,
    cfg: Any,
    sender: Any,
    failed: str,
    still_running: Any,
    failure: BaseException,
) -> None:
    """通知其他仍在运行平台上的配置超管。对应 Go ``notifyPlatformFailure``。

    只通知仍然活着的平台、目标 ID 去重且不补造，未配置超管的平台直接跳过；
    ``atrinis`` 是后台运行时而不是消息平台，不作为通知对象。
    """
    if cfg is None or sender is None:
        return
    text = f"平台 {failed} 异常退出：{failure}"
    security = getattr(cfg, "security", None)
    superadmins = getattr(security, "superadmins", None) or {}
    for name in still_running or []:
        name = (name or "").strip()
        if not name or name == failed or name == "atrinis":
            continue
        seen: set[str] = set()
        for user_id in superadmins.get(name) or []:
            user_id = (user_id or "").strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            try:
                await maybe_await(
                    sender.send_notice(
                        ctx,
                        Notice(
                            target=Target(platform=name, private_user_id=user_id),
                            outputs=[text_output(text)],
                            level=_LEVEL_ERROR,
                        ),
                    )
                )
            except Exception:  # noqa: BLE001 - 通知失败不得影响失败处理
                continue


def hook_notice_target(primary: Any, runtimes: Any) -> str:
    """给启动期 Hook 告警挑一个真正能送达的平台。对应 Go ``hookNoticeTarget``。

    优先主平台（CLI-only 与完整前台模式下就是交互式 CLI），否则退到第一个真实平台
    适配器；``service`` 模式的 headless 主平台与 ``atrinis`` 都不是可送达落点。
    """
    names = runtime_names(runtimes)
    if primary is not None:
        name = _adapter_name(primary).strip()
        if name in names:
            return name
    if names:
        return names[0]
    return ""


async def deliver_hook_notices(
    ctx: Any,
    logger: Any,
    sender: Any,
    platform_name: str,
    notices: Any,
) -> None:
    """平台发送器注册完成后补投递启动期攒下的 Hook 告警。

    对应 Go ``deliverHookNotices``。没有可用平台时退回日志，不静默丢弃。
    """
    for raw in notices or []:
        text = (raw or "").strip()
        if not text:
            continue
        if sender is None or not platform_name:
            _warn(logger, "hook notice", text=text)
            continue
        try:
            await maybe_await(
                sender.send_notice(
                    ctx,
                    Notice(
                        target=Target(platform=platform_name, superadmins=True),
                        outputs=[text_output(text)],
                        level=_LEVEL_WARNING,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 投递失败只记日志
            _warn(
                logger,
                "hook notice was not delivered",
                text=text,
                platform=platform_name,
                error=str(exc),
            )


def _warn(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "warn", None)
    if callable(emit):
        emit(message, **fields)


async def run_platforms(
    ctx: Any,
    handler: Any,
    logger: Any,
    adapters: Any,
    after_start: Any = None,
    heartbeat: Any = None,
    mode: RunMode | str = "",
    failure: Any = None,
) -> None:
    """启动全部平台运行时并等待它们结束。对应 Go ``runPlatforms``。

    适配器自己负责重连退避，所以这里只处理"运行时真的退出"的两种情况：

    * 非预期退出（运行上下文还没结束）：记 WARN，并交给调用方（health + 超管通知）；
    * 交互式 CLI 退出（``StopAppOnExit``）：非 service 模式下这就是应用退出意图。

    ``service`` 模式下平台循环不拥有进程生命周期：即使全部运行时退出也继续等外部
    停止，这样 health 接口与 systemd/Docker 的重启策略仍然有效。
    """
    adapters = list(adapters or [])
    run_ctx = _RunFlag(ctx)
    running: set[str] = set()
    lock = asyncio.Lock()

    async def supervise(adapter: Any) -> None:
        name = _adapter_name(adapter)
        async with lock:
            running.add(name)
        error: BaseException | None = None
        try:
            await maybe_await(adapter.run(run_ctx, handler))
        except asyncio.CancelledError:
            error = None
            raise
        except BaseException as exc:  # noqa: BLE001 - 平台异常退出要上报
            error = exc
        finally:
            async with lock:
                running.discard(name)
                still_running = sorted(running)
            # 运行上下文已结束时的退出是正常关停：既不记失败，也不通知。
            if error is not None and not run_ctx.cancelled:
                _warn(
                    logger,
                    "platform stopped with error",
                    platform=name,
                    error=str(error),
                )
                if not run_ctx.cancelled and failure is not None:
                    await maybe_await(failure(run_ctx, name, still_running, error))
            if str(mode or "") != RUN_MODE_SERVICE and platform_stops_app_on_exit(adapter):
                run_ctx.cancel()

    tasks = [asyncio.ensure_future(supervise(adapter)) for adapter in adapters]
    if heartbeat is not None:
        await maybe_await(heartbeat())
    if after_start is not None:
        await maybe_await(after_start(run_ctx))
    if not adapters:
        # 没有任何平台被启用时进程必须保持存活：直接返回会让进程立即以 0 退出，
        # 在 Docker / systemd 下意味着新装的容器被反复拉起，health 接口也随之消失。
        _warn(
            logger,
            "no platform is enabled; service stays alive so health endpoints remain "
            "available until a platform is configured",
        )

    all_exited = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
    stop_wait = asyncio.ensure_future(_wait_outer(ctx))
    try:
        while True:
            done, _pending = await asyncio.wait(
                {all_exited, stop_wait},
                timeout=HEARTBEAT_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_wait in done:
                run_ctx.cancel()
                await _cancel_all(tasks)
                await _drain(all_exited)
                raise asyncio.CancelledError()
            if all_exited in done:
                await all_exited
                if str(mode or "") != RUN_MODE_SERVICE:
                    return
                # service 模式：全部退出也继续等外部停止，只保留心跳分支。
                break
            if heartbeat is not None:
                await maybe_await(heartbeat())
        while True:
            done, _pending = await asyncio.wait(
                {stop_wait}, timeout=HEARTBEAT_INTERVAL_SECONDS
            )
            if stop_wait in done:
                run_ctx.cancel()
                raise asyncio.CancelledError()
            if heartbeat is not None:
                await maybe_await(heartbeat())
    finally:
        stop_wait.cancel()
        await _cancel_all(tasks)


async def _cancel_all(tasks: Any) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001 - 关停期吞掉
            pass


async def _wait_outer(ctx: Any) -> None:
    """等外部停止信号；没有可用信号时永不返回。"""
    waiter = getattr(ctx, "wait", None)
    if callable(waiter):
        await maybe_await(waiter())
        return
    await asyncio.Event().wait()


async def _drain(awaitable: Any) -> None:
    try:
        await asyncio.shield(awaitable)
    except BaseException:  # noqa: BLE001 - 关停期吞掉
        pass


class _RunFlag:
    """运行上下文：可取消 + 可查询是否已取消。"""

    def __init__(self, ctx: Any = None) -> None:
        self._outer = ctx
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        if self._cancelled:
            return True
        check = getattr(self._outer, "is_set", None)
        if callable(check):
            try:
                return bool(check())
            except Exception:  # noqa: BLE001
                return False
        return False

    def cancel(self) -> None:
        self._cancelled = True

    # ``adapter.run(run_ctx, handler)`` 只需要一个能查取消状态的对象。
    def is_set(self) -> bool:
        return self.cancelled


class AtrinisRuntimeAdapter:
    """把 atrinis 运行时包装成平台运行时。对应 Go ``atrinisRuntimeAdapter``。

    atrinis 是后台运行时，不是消息平台：``send_chat`` / ``send_notice`` 一律抛错。
    """

    def __init__(self, runtime: Any = None) -> None:
        self.runtime = runtime

    def name(self) -> str:
        return "atrinis"

    async def run(self, ctx: Any, handler: Any = None) -> None:
        return await maybe_await(self.runtime.run(ctx))

    async def send_chat(self, ctx: Any, outputs: Any) -> Any:
        raise InvalidError("atrinis cannot send chat output")

    async def send_notice(self, ctx: Any, notice: Any) -> Any:
        raise InvalidError("atrinis cannot send notice output")

    # Go 命名别名。
    Name = name
    Run = run
    SendChat = send_chat
    SendNotice = send_notice


# 交叉检索：``resolve_platform_caller`` 供 atrinis / hook.rules 的解析器复用。
_RESOLVE = resolve_platform_caller
_RUNTIME_NAMES = _runtime_names
_OUTPUT = Output

defaultPlatformFactory = DefaultPlatformFactory
defaultPlatformExecutor = DefaultPlatformExecutor
runPlatforms = run_platforms
registerCompletionPlatforms = register_completion_platforms
registerSessionBrowsers = register_session_browsers
registerCommandCatalogs = register_command_catalogs
registerPlatformHooks = register_platform_hooks
registerCronPlatformHook = register_cron_platform_hook
platformStopsAppOnExit = platform_stops_app_on_exit
notifyPlatformFailure = notify_platform_failure
hookNoticeTarget = hook_notice_target
deliverHookNotices = deliver_hook_notices
runtimeNames = runtime_names
atrinisRuntimeAdapter = AtrinisRuntimeAdapter
