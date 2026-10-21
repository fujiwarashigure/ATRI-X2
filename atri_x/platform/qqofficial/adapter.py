"""qqofficial 适配器主体：网关生命周期与出站发送。

对应 Go：``internal/platform/qqofficial/adapter.go``。

硬边界（上游）：适配器**只**做平台输入输出 —— 网关事件变成平台无关消息交给
``handler``，:class:`atri_x.delivery.delivery.Output` 变成官方 API 调用；它不驱动
LLM。群聊里**跳过**工具调用进度预览（规则在交付层
:func:`atri_x.delivery.delivery.should_drop_group_tool_preview`，这里只回答
"是不是群聊"）。

形式差异：

* ``(T, error)`` → 抛异常；一次通知里多目标只成功一部分时抛
  :class:`atri_x.delivery.delivery.DeliveryError`（``receipt`` 描述已成功项）。
* ``Logger`` 接口（``WarnContext`` 等）→ 本仓既有的鸭子类型 ``logger.warn`` /
  ``info`` / ``debug``（与 Telegram 适配器同一口径）。
* ``sync.Mutex`` → :class:`threading.Lock`（``next_msg_seq`` 是同步方法，
  改成 ``asyncio.Lock`` 会强迫调用方 ``await``）；``wsWriteMu`` → ``asyncio.Lock``。
* ``sleepContext`` / ``raiseContextError`` 在 :mod:`atri_x.platform.qqofficial.inbound`
  没有对应物，直接放在本模块（与 Telegram 适配器一致）。
* 入站处理在 :mod:`atri_x.platform.qqofficial.message`，出站在
  :mod:`atri_x.platform.qqofficial.send`，网关在
  :mod:`atri_x.platform.qqofficial.gateway`；本类保留 Go 的同名方法做委托。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any, Callable, Sequence

from atri_x.delivery.delivery import (
    DeliveryError,
    Notice,
    Output,
    Receipt,
    Target as DeliveryTarget,
    ctx_error,
    should_drop_group_tool_preview,
)
from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.backoff import new_backoff
from atri_x.platform.qqofficial import gateway as qq_gateway
from atri_x.platform.qqofficial import message as qq_message
from atri_x.platform.qqofficial import send as qq_send
from atri_x.platform.qqofficial.client import ApiClient
from atri_x.platform.qqofficial.config import (
    PLATFORM_NAME,
    Config,
    apply_defaults,
)
from atri_x.platform.qqofficial.target import (
    TARGET_C2C,
    TARGET_GROUP,
    SendTarget,
    is_group_context,
    with_target,
)


def new_adapter(cfg: Config, store: Any, chat_history: Any, logger: Any) -> "Adapter":
    """对应 Go ``New``。"""
    return Adapter(cfg, store, chat_history, logger)


class Adapter:
    """QQ 官方机器人适配器。对应 Go ``Adapter``。"""

    def __init__(self, cfg: Config, store: Any, chat_history: Any, logger: Any) -> None:
        apply_defaults(cfg)
        self.cfg = cfg
        self.store = store
        self.chat_history = chat_history
        self.logger = logger
        self.client = ApiClient(cfg)
        self.notify: Callable[..., Any] | None = None
        self.seq_by_id: dict[str, int] = {}
        self._seq_lock = threading.Lock()
        self.ws_write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # -- 生命周期 -----------------------------------------------------------
    def name(self) -> str:
        """对应 Go ``Name``。"""
        return PLATFORM_NAME

    def enabled(self) -> bool:
        """对应 Go ``Enabled``。"""
        return bool(self.cfg.enabled)

    def set_connect_notifier(self, notify: Callable[..., Any] | None) -> None:
        """对应 Go ``SetConnectNotifier``。"""
        self.notify = notify

    async def notify_connected(self, ctx: Any) -> None:
        """对应 Go ``notifyConnected``。"""
        if self.notify is None:
            return
        result = self.notify(ctx, self.name())
        if asyncio.iscoroutine(result):
            await result

    def spawn(self, coro: Any) -> asyncio.Task:
        """对应 Go 的 ``go`` 语句。"""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._discard_task)
        return task

    def _discard_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)

    def pending_tasks(self) -> set[asyncio.Task]:
        """尚未结束的后台任务（Python 侧测试接缝；对应 Go 的 ``go`` 句柄缺失）。"""
        return set(self._tasks)

    async def run(self, ctx: Any, handler: Any) -> None:
        """连网关 → 读事件 → 断开后按退避重连。对应 Go ``Run``。"""
        if not self.cfg.enabled:
            return
        state = qq_gateway.GatewayState()
        backoff = new_backoff(self.cfg.reconnect_interval(), 10.0)
        while True:
            err = ctx_error(ctx)
            if err is not None:
                raise err
            reason, failure = await self.run_gateway_once(ctx, handler, state)
            if failure is not None:
                if backoff.should_warn():
                    self.log_warn(
                        ctx,
                        "qqofficial gateway disconnected",
                        error=failure,
                        reconnect_mode=qq_gateway.reconnect_mode_text(reason.mode),
                    )
            else:
                backoff.reset()
            if reason.fatal:
                if failure is not None:
                    raise failure
                raise AtriXError("qqofficial gateway stopped")
            if not await sleep_context(ctx, backoff.delay()):
                raise_context_error(ctx)

    # -- 出站 ---------------------------------------------------------------
    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``SendChat``（落点来自 ctx，含 MessageContext 兜底）。"""
        return await qq_send.send_context_output(self, ctx, outputs)

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """对应 Go ``SendNotice``：空 Target 表示发回当前会话。"""
        target = notice.target
        outputs = notice.outputs
        if should_drop_group_tool_preview(
            target,
            outputs,
            is_group_context(ctx),
            getattr(self.cfg, "show_tool_preview_in_group", False),
        ):
            return Receipt()
        if target.empty():
            return await self.send_chat(ctx, outputs)
        targets = self.targets(target)
        receipt = Receipt()
        for one in targets:
            one.proactive = True
            target_ctx = with_target(ctx, one)
            try:
                sent = await qq_send.send_context_output(self, target_ctx, outputs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 保留已成功目标的回执
                partial = exc.receipt if isinstance(exc, DeliveryError) else Receipt()
                merged = receipt.merge(partial)
                raise DeliveryError(str(exc), merged.mark_partial_failure(exc)) from exc
            receipt = receipt.merge(sent)
        return receipt

    def targets(self, target: DeliveryTarget) -> list[SendTarget]:
        """把交付层目标展开成一组发送落点。对应 Go ``(*Adapter).targets``。"""
        platform_name = (target.platform or "").strip()
        if platform_name != "" and platform_name != self.name():
            raise InvalidError(f"qqofficial cannot send to platform {platform_name!r}")
        if target.superadmins:
            targets: list[SendTarget] = []
            for value in self.cfg.superadmins:
                identifier = (value or "").strip()
                if identifier.startswith(PLATFORM_NAME + ":"):
                    identifier = identifier[len(PLATFORM_NAME) + 1 :]
                if identifier != "":
                    targets.append(SendTarget(kind=TARGET_C2C, openid=identifier))
            if not targets:
                raise AtriXError("qqofficial superadmins are not configured")
            return targets
        private_user_id = (target.private_user_id or "").strip()
        if private_user_id != "":
            return [SendTarget(kind=TARGET_C2C, openid=private_user_id)]
        group_id = (target.group_id or "").strip()
        if group_id != "":
            return [SendTarget(kind=TARGET_GROUP, openid=group_id)]
        scope = (target.scope_id or "").strip()
        if scope.startswith("c2c:"):
            return [SendTarget(kind=TARGET_C2C, openid=scope.removeprefix("c2c:"))]
        if scope.startswith("group:"):
            return [SendTarget(kind=TARGET_GROUP, openid=scope.removeprefix("group:"))]
        raise InvalidError("qqofficial target missing private_user_id, group_id or scope_id")

    def next_msg_seq(self, msg_id: str) -> int:
        """同一被回复消息的发送序号。对应 Go ``(*Adapter).nextMsgSeq``。"""
        msg_id = (msg_id or "").strip()
        if msg_id == "":
            return 0
        with self._seq_lock:
            self.seq_by_id[msg_id] = self.seq_by_id.get(msg_id, 0) + 1
            return self.seq_by_id[msg_id]

    # -- 出站委托（send.py） -----------------------------------------------
    async def send_context_output(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``(*Adapter).sendContextOutput``。"""
        return await qq_send.send_context_output(self, ctx, outputs)

    async def send_output(self, ctx: Any, target: SendTarget, out: Output) -> Receipt:
        """对应 Go ``(*Adapter).sendOutput``。"""
        return await qq_send.send_output(self, ctx, target, out)

    async def send_text(self, ctx: Any, target: SendTarget, text: str) -> Receipt:
        """对应 Go ``(*Adapter).sendText``。"""
        return await qq_send.send_text(self, ctx, target, text)

    async def send_text_pages(
        self, ctx: Any, target: SendTarget, text: str, msg_type: int, prefix: str
    ) -> Receipt:
        """对应 Go ``(*Adapter).sendTextPages``。"""
        return await qq_send.send_text_pages(self, ctx, target, text, msg_type, prefix)

    async def send_media(
        self, ctx: Any, target: SendTarget, out: Output, file_type: int
    ) -> Receipt:
        """对应 Go ``(*Adapter).sendMedia``。"""
        return await qq_send.send_media(self, ctx, target, out, file_type)

    async def send_ark(self, ctx: Any, target: SendTarget, ark: Any) -> Receipt:
        """对应 Go ``(*Adapter).sendArk``。"""
        return await qq_send.send_ark(self, ctx, target, ark)

    def base_message(self, target: SendTarget) -> Any:
        """对应 Go ``(*Adapter).baseMessage``。"""
        return qq_send.base_message(self, target)

    # -- 入站委托（message.py） --------------------------------------------
    async def handle_c2c_message(
        self, ctx: Any, handler: Any, p: Any, msg: Any
    ) -> None:
        """对应 Go ``(*Adapter).handleC2CMessage``。"""
        await qq_message.handle_c2c_message(self, ctx, handler, p, msg)

    async def handle_group_message(
        self, ctx: Any, handler: Any, p: Any, msg: Any
    ) -> None:
        """对应 Go ``(*Adapter).handleGroupMessage``。"""
        await qq_message.handle_group_message(self, ctx, handler, p, msg)

    async def handle_inbound_message(
        self, ctx: Any, handler: Any, p: Any, msg: Any, conversation: str, mentioned_bot: bool
    ) -> None:
        """对应 Go ``(*Adapter).handleInboundMessage``。"""
        await qq_message.handle_inbound_message(
            self, ctx, handler, p, msg, conversation, mentioned_bot
        )

    # -- 网关委托（gateway.py） --------------------------------------------
    async def run_gateway_once(
        self, ctx: Any, handler: Any, state: Any
    ) -> tuple[Any, BaseException | None]:
        """对应 Go ``(*Adapter).runGatewayOnce``。"""
        return await qq_gateway.run_gateway_once(self, ctx, handler, state)

    # -- 日志 ---------------------------------------------------------------
    def log_debug(self, ctx: Any, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.debug(message, **fields)

    def log_info(self, ctx: Any, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.info(message, **fields)

    def log_warn(self, ctx: Any, message: str, **fields: Any) -> None:
        if self.logger is None:
            return
        self.logger.warn(message, **fields)


async def sleep_context(ctx: Any, delay: float) -> bool:
    """可被 ctx 取消的 sleep。对应 Go ``sleepContext``。

    返回 False 表示等待期间 ctx 被取消。
    """
    if ctx_error(ctx) is not None:
        return False
    if delay <= 0:
        await asyncio.sleep(0)
        return ctx_error(ctx) is None
    event = asyncio.Event()
    remove: Callable[[], None] | None = None
    adder = getattr(ctx, "add_callback", None)
    if callable(adder):
        remove = adder(event.set)
    try:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(event.wait(), delay)
    finally:
        if remove is not None:
            remove()
    return ctx_error(ctx) is None


def raise_context_error(ctx: Any) -> None:
    """抛出 ctx 的取消原因。对应 Go 的 ``return ctx.Err()``。"""
    err = ctx_error(ctx)
    if err is not None:
        raise err
    raise asyncio.CancelledError("qqofficial adapter context cancelled")


__all__ = [name for name in dir() if not name.startswith("_")]
