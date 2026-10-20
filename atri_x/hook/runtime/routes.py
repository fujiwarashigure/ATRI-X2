"""Hook 路由：租约、等待态、单飞调用与工具上下文令牌。

对应 Go：``internal/hook/runtime/routes.go``。

Go 把这些方法挂在 ``Manager`` 上（跨文件同包）；Python 一个类只能有一个类体，
所以这里以"接收 manager 的函数"形式实现，``Manager`` 再薄薄地委托一层。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from atri_x.errors import AtriXError
from atri_x.hook.event import Event

#: 一次调用里最多允许的 Hook 触发工具次数。对应 Go ``maxToolCallsPerInvocation``。
MAX_TOOL_CALLS_PER_INVOCATION = 32
#: 该事件已由有状态 Hook 处理过的标记。对应 Go ``dispatchedMetadataKey``。
DISPATCHED_METADATA_KEY = "hook.runtime.dispatched"
#: 该事件应跳过的 Hook ID。对应 Go ``skipHookIDMetadataKey``。
SKIP_HOOK_ID_METADATA_KEY = "hook.runtime.skip_id"

__all__ = [
    "MAX_TOOL_CALLS_PER_INVOCATION",
    "DISPATCHED_METADATA_KEY",
    "SKIP_HOOK_ID_METADATA_KEY",
    "RouteError",
    "RouteKey",
    "Lease",
    "Invocation",
    "ToolContext",
    "route_key_for",
    "random_id",
    "spawn",
    "has_lease",
    "route_hook_id",
    "route",
    "cancel",
    "begin_invocation",
    "end_invocation",
    "set_lease",
    "clear_lease",
    "clear_routes_locked",
    "put_tool_context",
    "take_tool_context",
]


class RouteError(AtriXError):
    """路由/租约错误。对应 Go 里这一层的 ``fmt.Errorf``。"""


@dataclass(frozen=True)
class RouteKey:
    """一个会话路由身份。对应 Go ``routeKey``。"""

    platform: str = ""
    scope_id: str = ""
    actor_id: str = ""


@dataclass
class Lease:
    """等待态租约。对应 Go ``lease``。"""

    hook_id: str = ""
    conversation_id: str = ""
    expires_at: datetime | None = None
    control: Any = None  # hook.Control


@dataclass
class Invocation:
    """一次正在执行的调用。对应 Go ``invocation``。"""

    hook_id: str = ""
    cancel: Callable[[], None] | None = None


@dataclass
class ToolContext:
    """一次调用里发给插件的工具上下文令牌。对应 Go ``toolContext``。"""

    hook_id: str = ""
    event: Event = field(default_factory=Event)
    context: Any = None
    expires_at: datetime | None = None
    calls: int = 0


def route_key_for(event: Event) -> RouteKey:
    """对应 Go ``routeKeyFor``。"""
    return RouteKey(
        platform=event.platform.name,
        scope_id=event.platform.scope_id,
        actor_id=event.actor.id,
    )


def random_id(prefix: str) -> str:
    """``prefix:12 字节十六进制``。对应 Go ``randomID``。"""
    return prefix + ":" + secrets.token_hex(12)


def spawn(factory: Callable[[], Any]) -> Any:
    """在运行中的事件循环里起一个后台任务；没有循环时安静地跳过。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(factory())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired(lease: Lease, moment: datetime) -> bool:
    return lease.expires_at is not None and moment > lease.expires_at


def has_lease(manager: Any, event: Event) -> bool:
    """该路由当前是否持有未过期租约。对应 Go ``Manager.hasLease``。"""
    if manager is None:
        return False
    key = route_key_for(event)
    moment = _now()
    with manager.mu:
        lease = manager.routes.get(key)
        if lease is not None and _expired(lease, moment):
            del manager.routes[key]
            worker = manager.transient.pop(key, None)
            stale = worker
            lease = None
        else:
            stale = None
    if stale is not None:
        spawn(lambda: stale.stop(None))
    return lease is not None


def route_hook_id(manager: Any, event: Event) -> str:
    """当前路由归属的 Hook ID。对应 Go ``Manager.RouteHookID``。"""
    if manager is None:
        return ""
    key = route_key_for(event)
    with manager.mu:
        lease = manager.routes.get(key)
    if lease is None or _expired(lease, _now()):
        return ""
    return lease.hook_id


async def route(
    manager: Any, ctx: Any, event: Event
) -> tuple[Event, bool, BaseException | None]:
    """把事件继续发给持有租约的 Hook。对应 Go ``Manager.Route``。

    Go 签名是 ``(Event, bool, error)``；Python 侧错误按本项目约定抛异常，
    所以第三位固定返回 ``None``。**必须**保持三元组：:mod:`atri_x.agent.hooks`
    的 ``HookRouter`` 协议与 :func:`atri_x.agent.message.handle_message` 都按
    三元组解包，返回二元组会直接 ``UNPACK_SEQUENCE`` 失败。
    """
    if manager is None:
        return event, False, None
    if event.metadata.get(DISPATCHED_METADATA_KEY) is True:
        return event, False, None
    key = route_key_for(event)
    moment = _now()
    stale = None
    with manager.mu:
        lease = manager.routes.get(key)
        if lease is not None and _expired(lease, moment):
            del manager.routes[key]
            stale = manager.transient.pop(key, None)
            lease = None
    if stale is not None:
        spawn(lambda: stale.stop(None))
        return event, False, None
    if lease is None:
        return event, False, None
    worker = manager.worker(lease.hook_id)
    if worker is None:
        with manager.mu:
            worker = manager.transient.get(key)
    if worker is None:
        with manager.mu:
            manager.routes.pop(key, None)
        return event, False, None
    if worker.config.block.blocks(event):
        clear_lease(manager, lease.hook_id, event)
        return event, False, None
    from atri_x.hook.runtime.config import Mode

    transient = worker.config.mode_or_once() == Mode.TRANSIENT
    try:
        updated = await worker.handle(ctx, event, True, lease.control)
    except BaseException:
        # Go: ``err != nil || !m.hasLease(event)`` —— 出错也要回收瞬态 worker。
        if transient:
            manager.stop_transient(key, worker)
        raise
    if transient and not has_lease(manager, event):
        manager.stop_transient(key, worker)
    if not updated.control.stop_propagation:
        if updated.metadata is None:
            updated.metadata = {}
        updated.metadata[SKIP_HOOK_ID_METADATA_KEY] = lease.hook_id
    return updated, True, None


async def cancel(manager: Any, event: Event) -> bool:
    """取消该路由上的等待/执行。对应 Go ``Manager.Cancel``。"""
    if manager is None:
        return False
    key = route_key_for(event)
    with manager.mu:
        lease = manager.routes.pop(key, None)
        running = manager.running.get(key)
        transient = manager.transient.get(key)
    if lease is None and running is None:
        return False
    if running is not None and running.cancel is not None:
        running.cancel()
    hook_id = lease.hook_id if lease is not None else ""
    conversation_id = lease.conversation_id if lease is not None else ""
    if not hook_id and running is not None:
        hook_id = running.hook_id
    selected = manager.worker(hook_id)
    if selected is None:
        selected = transient
    if selected is not None:
        if running is None:
            await selected.notify_cancel(conversation_id)
        from atri_x.hook.runtime.config import Mode

        if selected.config.mode_or_once() == Mode.TRANSIENT:
            manager.stop_transient(key, selected)
    return True


def begin_invocation(
    manager: Any, key: RouteKey, hook_id: str, cancel_fn: Callable[[], None]
) -> None:
    """对应 Go ``beginInvocation``。"""
    with manager.mu:
        manager.running[key] = Invocation(hook_id=hook_id, cancel=cancel_fn)


def end_invocation(manager: Any, key: RouteKey, cancel_fn: Any = None) -> None:
    """对应 Go ``endInvocation``。"""
    with manager.mu:
        manager.running.pop(key, None)


async def set_lease(
    manager: Any, id: str, event: Event, result: Any, defaults: Any
) -> None:
    """登记等待态租约。对应 Go ``Manager.setLease``。"""
    if result.status != "waiting":
        return
    if not (result.conversation_id or "").strip():
        raise RouteError("hook waiting response requires conversation_id")
    expires_at = result.expires_at
    if expires_at is None or not expires_at > _now():
        raise RouteError("hook waiting response requires a future expires_at")
    with manager.mu:
        config = manager.configs.get(id)
    if config is None:
        raise RouteError(f"hook worker {id!r} is no longer configured")
    maximum = timedelta(seconds=config.max_wait_seconds)
    if expires_at > _now() + maximum:
        raise RouteError("hook waiting response exceeds max_wait_seconds")
    key = route_key_for(event)
    with manager.mu:
        manager.routes[key] = Lease(
            hook_id=id,
            conversation_id=result.conversation_id,
            expires_at=expires_at,
            control=defaults,
        )


def clear_lease(manager: Any, id: str, event: Event) -> None:
    """清掉归属 ``id`` 的租约。对应 Go ``Manager.clearLease``。"""
    key = route_key_for(event)
    with manager.mu:
        current = manager.routes.get(key)
        if current is not None and current.hook_id == id:
            del manager.routes[key]


def clear_routes_locked(manager: Any, id: str) -> None:
    """清掉某个 Hook 的全部租约与执行。对应 Go ``Manager.clearRoutesLocked``。

    调用方**必须**已经持有 ``manager.mu``（函数名里的 ``locked``）。
    """
    stopping: list[Any] = []
    for key in list(manager.routes):
        lease = manager.routes[key]
        if lease.hook_id != id:
            continue
        del manager.routes[key]
        worker = manager.transient.pop(key, None)
        if worker is not None:
            stopping.append(worker)
    for key in list(manager.running):
        running = manager.running[key]
        if running.hook_id != id:
            continue
        if running.cancel is not None:
            running.cancel()
        del manager.running[key]
    for worker in stopping:
        spawn(lambda worker=worker: worker.stop(None))


def put_tool_context(
    manager: Any, id: str, ctx: Any, event: Event, ttl: float
) -> str:
    """登记一次调用可用的工具上下文令牌。对应 Go ``putToolContext``。"""
    token = random_id("ctx")
    with manager.mu:
        manager.tokens[token] = ToolContext(
            hook_id=id,
            event=event,
            context=ctx,
            expires_at=_now() + timedelta(seconds=ttl),
        )
    return token


def take_tool_context(manager: Any, token: str, hook_id: str) -> ToolContext | None:
    """取出令牌并计数（超过 32 次即失效）。对应 Go ``takeToolContext``。"""
    with manager.mu:
        value = manager.tokens.get(token)
        if (
            value is None
            or value.hook_id != hook_id
            or _expired_time(value.expires_at)
            or value.calls >= MAX_TOOL_CALLS_PER_INVOCATION
        ):
            manager.tokens.pop(token, None)
            return None
        value.calls += 1
        manager.tokens[token] = value
        return value


def _expired_time(moment: datetime | None) -> bool:
    return moment is not None and _now() > moment
