"""普通 Hook 的注册、排序、执行与原子快照替换。

对应 Go：``internal/hook/manager.go``。

并发说明：Go 用 ``sync.RWMutex``。这里用 ``threading.RLock``，因为：

* ``run`` / ``notify`` 是 async（handler 是 await 的），但临界区只有"取快照"这一步，
  不阻塞事件循环；
* ``register`` / ``replace`` / ``stop`` 可能被别的线程（配置热重载）调用。

与 Go 的一处**有意的语义差异**（已记录，不视为偏离契约）：

* Go 的 ``Event`` 是值语义，handler 对普通字段的修改不会回传调用方；
  Python 的 ``Event`` 是可变对象。``Run`` 采用"把 handler 返回值作为新的当前事件"
  的约定，handler 就地修改也会体现在返回值上 —— 效果等价于 Go 里 handler 改
  ``Metadata`` map 的情形。``Notify`` 则**显式克隆**再交给 handler，避免 handler
  的修改泄漏到调用方（与 Go 一致）。
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from atri_x.delivery.delivery import META_HOOK_MODE, META_HOOK_NAME, META_HOOK_POINT, Output
from atri_x.errors import AtriXError
from atri_x.hook.event import Event, Handler, Point, prepare_event
from atri_x.hook.match import BlockPolicy, Match
from atri_x.llm.segment import segments_content_text

#: ``hooks.toml`` 里 wakeup 策略的字面量。对应 Go 的 WakeupPolicy 常量。
WAKEUP_REQUIRED = "required"
WAKEUP_ANY = "any"
WAKEUP_FORBIDDEN = "forbidden"

WakeupRequired = WAKEUP_REQUIRED
WakeupAny = WAKEUP_ANY
WakeupForbidden = WAKEUP_FORBIDDEN

WAKEUP_POLICIES = (WAKEUP_REQUIRED, WAKEUP_ANY, WAKEUP_FORBIDDEN)

#: 日志里截断的 rune 上限。对应 Go ``trimLogText`` 的 ``max``。
TRIM_LOG_TEXT_MAX = 300


class HookError(AtriXError):
    """handler 抛错时包装出的异常。对应 Go ``wrapHookError`` 的 ``fmt.Errorf``。"""


# ---------------------------------------------------------------------------
# 注册与信息
# ---------------------------------------------------------------------------
@dataclass
class Registration:
    """一条显式匹配的 hook handler。对应 Go ``Registration``。"""

    point: str = ""
    priority: int = 0
    plugin_id: str = ""
    name: str = ""
    description: str = ""
    match: Match = field(default_factory=Match)
    block: BlockPolicy = field(default_factory=BlockPolicy)
    handler: Handler | None = None
    detail: str = ""
    wakeup: str = ""


@dataclass
class Info:
    """供 ``/hooks`` 检查命令展示的一条注册信息。对应 Go ``Info``。"""

    plugin_id: str = ""
    name: str = ""
    description: str = ""
    point: str = ""
    priority: int = 0
    detail: str = ""
    active: int = 0


@dataclass
class ReloadReport:
    """对应 Go ``ReloadReport``。"""

    notices: list[str] = field(default_factory=list)


@runtime_checkable
class Registrar(Protocol):
    """对应 Go ``Registrar``。"""

    def register(self, reg: Registration) -> None: ...


@runtime_checkable
class Module(Protocol):
    """对应 Go ``Module``。"""

    def register_hooks(self, registrar: Registrar) -> None: ...


@runtime_checkable
class ManagerProtocol(Protocol):
    """对应 Go ``Manager`` 接口。"""

    def register(self, reg: Registration) -> None: ...

    async def run(self, ctx: Any, event: Event) -> Event: ...

    async def notify(self, ctx: Any, event: Event) -> None: ...

    def list(self) -> list[Info]: ...


# ---------------------------------------------------------------------------
# 钩子函数类型
# ---------------------------------------------------------------------------
#: ``wakeup(ctx, event) -> bool``；同步或异步都可以。
WakeupFunc = Callable[[Any, Event], Any]

#: ``observer(ctx, event, info) -> (ctx, done)``；同步或异步都可以。
Observer = Callable[[Any, Event, "ObserverInfo"], Any]


@dataclass
class ObserverInfo:
    """一次匹配到的 handler 执行。对应 Go ``ObserverInfo``。"""

    point: str = ""
    name: str = ""
    priority: int = 0
    mode: str = ""


@runtime_checkable
class HookLogger(Protocol):
    """``log_hook`` 需要的日志接口。

    上游用 ``*slog.Logger``；这里不绑定到具体实现，只要提供
    ``debug(msg, **fields)`` / ``info(msg, **fields)`` / ``warning(msg, **fields)``
    即可（``atri_x.logging`` 的 Logger 由 app 层适配，见 ``app/runtime.py``）。
    """

    def debug(self, msg: str, **fields: Any) -> None: ...

    def info(self, msg: str, **fields: Any) -> None: ...

    def warning(self, msg: str, **fields: Any) -> None: ...


@dataclass
class _Registration:
    """内部注册记录（含排序用的 order）。对应 Go 的非导出 ``registration``。"""

    priority: int = 0
    order: int = 0
    plugin_id: str = ""
    name: str = ""
    description: str = ""
    match: Match = field(default_factory=Match)
    block: BlockPolicy = field(default_factory=BlockPolicy)
    handler: Handler | None = None
    detail: str = ""
    wakeup: str = WAKEUP_REQUIRED


@dataclass
class _Active:
    name: str = ""
    plugin_id: str = ""
    cancel: Callable[[], None] | None = None


# ---------------------------------------------------------------------------
# Wakeup 策略
# ---------------------------------------------------------------------------
def normalize_wakeup_policy(policy: str) -> str:
    """对应 Go ``normalizeWakeupPolicy``：空串落到默认 ``required``。"""
    return WAKEUP_REQUIRED if policy == "" else policy


def validate_wakeup_policy(policy: str) -> None:
    """对应 Go ``validateWakeupPolicy``；不合法抛 :class:`ValueError`。"""
    if normalize_wakeup_policy(policy) not in WAKEUP_POLICIES:
        raise ValueError(f'unsupported wakeup policy "{policy}"')


# ---------------------------------------------------------------------------
# NoopManager
# ---------------------------------------------------------------------------
class NoopManager:
    """没有配置任何 Hook 时使用。对应 Go ``NoopManager``。"""

    def register(self, reg: Registration) -> None:
        return None

    async def run(self, ctx: Any, event: Event) -> Event:
        return prepare_event(event)

    async def notify(self, ctx: Any, event: Event) -> None:
        return None

    def list(self) -> list[Info]:
        return []


# ---------------------------------------------------------------------------
# DefaultManager
# ---------------------------------------------------------------------------
class DefaultManager:
    """按优先级顺序执行某个 Hook 点上的 handler。对应 Go ``DefaultManager``。"""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._next = 0
        self._active_id = 0
        self._handlers: dict[str, list[_Registration]] = {}
        self._logger: HookLogger | None = None
        self._wakeup: WakeupFunc | None = None
        self._observer: Observer | None = None
        self._active: dict[str, _Active] = {}

    # -- 注入 ---------------------------------------------------------------
    def set_logger(self, logger: HookLogger | None) -> None:
        with self._mu:
            self._logger = logger

    def set_wakeup_func(self, fn: WakeupFunc | None) -> None:
        with self._mu:
            self._wakeup = fn

    def set_observer(self, fn: Observer | None) -> None:
        with self._mu:
            self._observer = fn

    # -- 注册 ---------------------------------------------------------------
    def register(self, reg: Registration) -> None:
        """对应 Go ``DefaultManager.Register``；不合法抛 :class:`ValueError`。"""
        _validate_registration(reg)
        with self._mu:
            self._next += 1
            item = _Registration(
                priority=reg.priority,
                order=self._next,
                plugin_id=(reg.plugin_id or "").strip(),
                name=reg.name,
                description=(reg.description or "").strip(),
                match=reg.match,
                block=reg.block,
                handler=reg.handler,
                detail=reg.detail,
                wakeup=normalize_wakeup_policy(reg.wakeup),
            )
            bucket = self._handlers.setdefault(reg.point, [])
            bucket.append(item)
            # 与 Go 一致：先按 priority 升序，priority 相同按注册顺序（稳定）。
            bucket.sort(key=lambda r: (r.priority, r.order))

    def handlers_for(self, point: str) -> list[_Registration]:
        """取某个 Hook 点的注册快照（对应 Go ``handlersFor``）。"""
        with self._mu:
            return list(self._handlers.get(point, ()))

    # -- 执行 ---------------------------------------------------------------
    async def run(self, ctx: Any, event: Event) -> Event:
        """串行执行匹配到的 handler，返回最终事件。对应 Go ``Run``。

        遇到 handler 抛错**立即返回**（与 Go 一致：``Run`` 不吞错）。
        """
        event = prepare_event(event)
        for reg in self.handlers_for(event.point):
            if reg.block.blocks(event):
                continue
            if await self._skip_for_wakeup(ctx, event, reg):
                continue
            match_result = reg.match.match_event(event)
            if not match_result.ok:
                continue
            event.metadata["match"] = match_result.context
            before = event
            # 必须是**调用 handler 之前**就取好的长度。Go 的 `before := event` 复制了
            # slice header，所以 `len(before.Outputs)` 是旧长度；Python 里
            # `before is event`，事后再取长度会拿到 handler 追加后的新长度，
            # 于是新增输出一个都不会被标记。
            outputs_before = len(event.outputs)
            handler_ctx = _derived_ctx(ctx)
            cancel = _make_cancel(handler_ctx)
            active_id = self._start_active(reg, cancel)
            run_ctx, done = await self._observe(handler_ctx, event, reg, "run")
            try:
                updated = await _call_handler(reg.handler, run_ctx, event)
            except BaseException as exc:  # noqa: BLE001 - 需要先记录再包装
                done()
                self._finish_active(active_id)
                event.metadata.pop("match", None)
                self._log_hook(run_ctx, "run", reg, before, before, exc)
                raise _wrap_hook_error(reg, exc) from exc
            done()
            self._finish_active(active_id)
            event.metadata.pop("match", None)
            if updated is None:
                updated = event
            updated = mark_hook_outputs(updated, outputs_before, reg, "run")
            event = prepare_event(updated)
            self._log_hook(run_ctx, "run", reg, before, event, None)
            if event.control.stop_propagation:
                break
        return event

    async def notify(self, ctx: Any, event: Event) -> None:
        """通知型执行：不改变调用方的事件，串行调用全部匹配 handler。

        对应 Go ``Notify``。handler 的返回值只用于判断 ``stop_propagation``；
        出错**继续**执行其余 handler，最后把错误聚合成 :class:`ExceptionGroup` 抛出
        （Go 用 ``errors.Join``）。
        """
        event = prepare_event(event)
        errors: list[BaseException] = []
        for reg in self.handlers_for(event.point):
            if reg.block.blocks(event):
                continue
            if await self._skip_for_wakeup(ctx, event, reg):
                continue
            match_result = reg.match.match_event(event)
            if not match_result.ok:
                continue
            event.metadata["match"] = match_result.context
            # 同 Run：长度必须在 handler 之前取。
            outputs_before = len(event.outputs)
            handler_ctx = _derived_ctx(ctx)
            cancel = _make_cancel(handler_ctx)
            active_id = self._start_active(reg, cancel)
            run_ctx, done = await self._observe(handler_ctx, event, reg, "notify")
            # Go 是值语义：handler 看不到调用方后续状态，也改不动它。
            working = event.clone()
            working.metadata["match"] = match_result.context
            try:
                updated = await _call_handler(reg.handler, run_ctx, working)
            except BaseException as exc:  # noqa: BLE001
                done()
                self._finish_active(active_id)
                event.metadata.pop("match", None)
                self._log_hook(run_ctx, "notify", reg, event, event, exc)
                errors.append(_wrap_hook_error(reg, exc))
                continue
            done()
            self._finish_active(active_id)
            event.metadata.pop("match", None)
            if updated is None:
                updated = working
            # 与 Go 一致：``_ = markHookOutputs(updated, ...)`` —— 标记只打在
            # handler 自己追加的输出上，而它们不在调用方的 slice header 里，
            # 因此调用方看不到任何变化。这里照抄"算完即弃"。
            mark_hook_outputs(updated, outputs_before, reg, "notify")
            self._log_hook(run_ctx, "notify", reg, event, event, None)
            if updated.control.stop_propagation:
                break
        if errors:
            raise ExceptionGroup("hook notify errors", errors)

    # -- Wakeup -------------------------------------------------------------
    async def _skip_for_wakeup(self, ctx: Any, event: Event, reg: _Registration) -> bool:
        if reg.wakeup == WAKEUP_ANY:
            return False
        wakeup = self._wakeup_for_run()
        if wakeup is None:
            return False
        value = wakeup(ctx, event)
        if inspect.isawaitable(value):
            value = await value
        if reg.wakeup == WAKEUP_FORBIDDEN:
            return bool(value)
        return not bool(value)

    def _wakeup_for_run(self) -> WakeupFunc | None:
        with self._mu:
            return self._wakeup

    # -- Observer -----------------------------------------------------------
    async def _observe(
        self, ctx: Any, event: Event, reg: _Registration, mode: str
    ) -> tuple[Any, Callable[[], None]]:
        observer = self._observer_for_run()
        if observer is None:
            return ctx, _noop
        info = ObserverInfo(
            point=event.point, name=reg.name, priority=reg.priority, mode=mode
        )
        result = observer(ctx, event, info)
        if inspect.isawaitable(result):
            result = await result
        run_ctx, done = result
        if run_ctx is None:
            run_ctx = ctx
        if done is None:
            done = _noop
        return run_ctx, done

    def _observer_for_run(self) -> Observer | None:
        with self._mu:
            return self._observer

    # -- 活跃执行跟踪 -------------------------------------------------------
    def _start_active(self, reg: _Registration, cancel: Callable[[], None]) -> str:
        with self._mu:
            self._active_id += 1
            ident = f"{reg.name}:{self._active_id}"
            self._active[ident] = _Active(
                name=reg.name, plugin_id=reg.plugin_id, cancel=cancel
            )
            return ident

    def _finish_active(self, ident: str) -> None:
        with self._mu:
            self._active.pop(ident, None)

    def stop(self, ident: str) -> int:
        """取消匹配该规则名或插件 ID 的活跃执行，返回被取消的数量。

        对应 Go ``DefaultManager.Stop``。
        """
        ident = (ident or "").strip()
        if not ident:
            return 0
        with self._mu:
            cancels = [
                a.cancel
                for a in self._active.values()
                if a.name == ident or a.plugin_id == ident
            ]
        for cancel in cancels:
            if cancel is not None:
                cancel()
        return len(cancels)

    def matches(self, ident: str) -> bool:
        """``ident`` 是否命中某条规则名或插件 ID。对应 Go ``Matches``。"""
        ident = (ident or "").strip()
        with self._mu:
            for items in self._handlers.values():
                for reg in items:
                    if reg.name == ident or (reg.plugin_id != "" and reg.plugin_id == ident):
                        return True
        return False

    # -- 列表 ---------------------------------------------------------------
    def list(self) -> list[Info]:
        """对应 Go ``List``：按 (point, name) 稳定排序。"""
        with self._mu:
            active_by_name: dict[str, int] = {}
            for running in self._active.values():
                active_by_name[running.name] = active_by_name.get(running.name, 0) + 1
            out: list[Info] = []
            for point, items in self._handlers.items():
                for reg in items:
                    out.append(
                        Info(
                            plugin_id=reg.plugin_id,
                            name=reg.name,
                            description=reg.description,
                            point=point,
                            priority=reg.priority,
                            detail=reg.detail,
                            active=active_by_name.get(reg.name, 0),
                        )
                    )
        out.sort(key=lambda i: (i.point, i.name))
        return out

    # -- 快照替换 -----------------------------------------------------------
    def replace(self, next_manager: "DefaultManager") -> None:
        """用 ``next_manager`` 的注册快照原子替换本管理器的 handler。

        对应 Go ``Replace``：logging / wakeup / observer 这些运行期接线**保持**
        挂在接收方上。
        """
        if next_manager is None or next_manager is self:
            return
        with next_manager._mu:
            handlers = {p: list(items) for p, items in next_manager._handlers.items()}
            order = next_manager._next
        with self._mu:
            self._handlers = handlers
            self._next = order

    def replace_plugin(self, plugin_id: str, next_manager: "DefaultManager") -> None:
        """只替换属于 ``plugin_id`` 的注册。

        对应 Go ``ReplacePlugin``：新注册插在**该插件原第一条注册的位置**，
        并给该点上的全部注册重排 ``order``。
        """
        if next_manager is None or next_manager is self:
            return
        plugin_id = (plugin_id or "").strip()
        if not plugin_id:
            return

        with next_manager._mu:
            replacements: dict[str, list[_Registration]] = {}
            for point, items in next_manager._handlers.items():
                for reg in items:
                    if reg.plugin_id == plugin_id:
                        replacements.setdefault(point, []).append(reg)

        with self._mu:
            points = set(self._handlers) | set(replacements)
            for point in points:
                kept: list[_Registration] = []
                insert_at = -1
                for reg in self._handlers.get(point, ()):
                    if reg.plugin_id == plugin_id:
                        if insert_at == -1:
                            insert_at = len(kept)
                        continue
                    kept.append(reg)
                if insert_at == -1:
                    insert_at = len(kept)
                repl = replacements.get(point, [])
                n = len(repl)
                kept.extend([None] * n)  # type: ignore[list-item]
                if n:
                    kept[insert_at + n:] = kept[insert_at: len(kept) - n]
                    kept[insert_at: insert_at + n] = repl
                for item in kept:
                    self._next += 1
                    item.order = self._next
                if not kept:
                    self._handlers.pop(point, None)
                    continue
                kept.sort(key=lambda r: (r.priority, r.order))
                self._handlers[point] = kept

    # -- 日志 ---------------------------------------------------------------
    def _log_hook(
        self,
        ctx: Any,
        mode: str,
        reg: _Registration,
        before: Event,
        after: Event,
        err: BaseException | None,
    ) -> None:
        logger = self._logger_for_log()
        if logger is None:
            return
        fields: dict[str, Any] = {
            "point": before.point,
            "hook": reg.name,
            "priority": reg.priority,
            "order": reg.order,
            "mode": mode,
        }
        if err is not None:
            fields["error"] = str(err)
            if _is_cancelled(err):
                logger.info("hook canceled", **fields)
                return
            logger.warning("hook error", **fields)
            return
        if _logger_debug_enabled(logger):
            fields.update(
                {
                    "session_id": after.session.id,
                    "provider": after.llm.provider,
                    "model": after.llm.model,
                    "tool": after.tool.name,
                    "before_text": trim_log_text(event_text(before)),
                    "after_text": trim_log_text(event_text(after)),
                    "source_text": trim_log_text(after.llm.source_text),
                }
            )
            logger.debug("hook triggered", **fields)
        else:
            logger.info("hook triggered", **fields)

    def _logger_for_log(self) -> HookLogger | None:
        with self._mu:
            return self._logger


# ---------------------------------------------------------------------------
# 校验与工具
# ---------------------------------------------------------------------------
def _validate_registration(reg: Registration) -> None:
    """对应 Go ``validateRegistration``；不合法抛 :class:`ValueError`。"""
    if not reg.point:
        raise ValueError("hook point is required")
    if not (reg.name or "").strip():
        raise ValueError("hook name is required")
    if reg.handler is None:
        raise ValueError("hook handler is required")
    try:
        validate_wakeup_policy(reg.wakeup)
    except ValueError as exc:
        raise ValueError(f'hook "{reg.name}": {exc}') from exc
    try:
        reg.match.validate()
    except ValueError as exc:
        raise ValueError(f'hook "{reg.name}" match: {exc}') from exc


def _wrap_hook_error(reg: _Registration, err: BaseException) -> BaseException:
    """对应 Go ``wrapHookError``：名字为空时原样返回。"""
    if not (reg.name or "").strip():
        return err
    return HookError(f"hook {reg.name}: {err}")


def mark_hook_outputs(event: Event, start: int, reg: _Registration, mode: str) -> Event:
    """给 ``[start, len(outputs))`` 区间内新增的输出打上 Hook 来源标记。

    对应 Go ``markHookOutputs``。``setOutputMeta`` 只在键**不存在**时写入，
    因此已有标记不会被覆盖。
    """
    if start < 0:
        start = 0
    if start > len(event.outputs):
        start = len(event.outputs)
    for i in range(start, len(event.outputs)):
        out = event.outputs[i]
        if not isinstance(out, Output):
            continue
        meta = dict(out.meta) if out.meta else {}
        _set_output_meta(meta, META_HOOK_POINT, event.point)
        _set_output_meta(meta, META_HOOK_NAME, reg.name)
        _set_output_meta(meta, META_HOOK_MODE, mode)
        out.meta = meta
    return event


def _set_output_meta(meta: dict[str, Any], key: str, value: str) -> None:
    """对应 Go ``setOutputMeta``：值空则跳过；键已存在则不动。"""
    if not (value or "").strip():
        return
    if key in meta:
        return
    meta[key] = value


def event_text(event: Event) -> str:
    """对应 Go ``eventText``：按优先级取一个可用于日志的文本。"""
    if event.llm.text:
        return event.llm.text
    content = segments_content_text(event.message.segments)
    if content:
        return content
    if event.tool.result:
        return event.tool.result
    if event.tool.arguments:
        return event.tool.arguments
    return ""


def trim_log_text(text: str) -> str:
    """对应 Go ``trimLogText``：超过 300 个 rune 截断并加省略号。"""
    if len(text) <= TRIM_LOG_TEXT_MAX:
        return text
    return text[:TRIM_LOG_TEXT_MAX] + "..."


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _noop() -> None:
    return None


async def _call_handler(handler: Handler | None, ctx: Any, event: Event) -> Event | None:
    """调用 handler，兼容同步函数（Go 侧全是同步接口）。"""
    if handler is None:
        return event
    result = handler.handle_hook(ctx, event)
    if inspect.isawaitable(result):
        result = await result
    return result


def _derived_ctx(ctx: Any) -> Any:
    """为一次 handler 执行派生一个可取消的子上下文（Go 的 ``context.WithCancel``）。

    若 ``ctx`` 提供 ``with_cancel()``（见 :mod:`atri_x.safecontext`）就用它；
    否则退化为原样返回 —— 此时 ``cancel`` 只是空操作。
    """
    factory = getattr(ctx, "with_cancel", None)
    if callable(factory):
        try:
            return factory()
        except Exception:  # noqa: BLE001 - 上下文不可派生时不要让 Hook 挂掉
            return ctx
    return ctx


def _make_cancel(ctx: Any) -> Callable[[], None]:
    cancel = getattr(ctx, "cancel", None)
    if callable(cancel):
        return cancel
    return _noop


def _is_cancelled(err: BaseException) -> bool:
    import asyncio

    if isinstance(err, asyncio.CancelledError):
        return True
    checker = getattr(err, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001
            return False
    return False


def _logger_debug_enabled(logger: HookLogger) -> bool:
    for attr in ("is_debug_enabled", "isEnabledFor"):
        probe = getattr(logger, attr, None)
        if callable(probe):
            try:
                return bool(probe())
            except TypeError:
                continue
    return False


def new_manager() -> DefaultManager:
    """对应 Go ``NewManager``。"""
    return DefaultManager()


__all__ = [name for name in dir() if not name.startswith("_")]
