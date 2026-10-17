"""QQ 官方网关：握手、心跳、事件分发与重连判定。

对应 Go：``internal/platform/qqofficial/gateway.go``。

形式差异：

* ``github.com/coder/websocket`` → :mod:`atri_x.net.websocket`；
  ``websocket.CloseStatus(err)`` → :attr:`atri_x.net.websocket.ConnectionClosed.code`。
* ``runGatewayOnce`` 返回 ``(reconnectReason, error)``：Python 侧返回
  ``(ReconnectReason, BaseException | None)`` —— 调用方（``Adapter.run``）需要
  同时拿到"怎么重连"和"为什么断"，把 error 变成异常会丢掉前者。
* ``go a.handleC2CMessage(...)`` → :meth:`atri_x.platform.qqofficial.adapter.Adapter.spawn`。
* ``time.NewTicker`` → ``asyncio.sleep`` 循环；退出由 task 取消完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform as platform_module
from dataclasses import dataclass

from atri_x.errors import AtriXError, InvalidError
from atri_x.net.websocket import (
    CLOSE_NORMAL,
    WebSocketConnection,
    connect as ws_connect,
)
from atri_x.platform.qqofficial import message as qq_message
from atri_x.platform.qqofficial.types import (
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    EVENT_GROUP_MESSAGE_CREATE,
    EVENT_READY,
    EVENT_RESUMED,
    INTENT_GROUP_AND_C2C,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    HelloData,
    IdentifyData,
    InboundMessage,
    Payload,
    ReadyData,
    ResumeData,
)

#: 重连方式。对应 Go ``reconnectMode``。
RECONNECT_IDENTIFY = 0
RECONNECT_RESUME = 1

#: 心跳间隔兜底（秒）。对应 Go 的 ``45 * time.Second``。
DEFAULT_HEARTBEAT_INTERVAL = 45.0


def reconnect_mode_text(mode: int) -> str:
    """对应 Go ``reconnectMode.String()``。"""
    return "resume" if mode == RECONNECT_RESUME else "identify"


@dataclass
class ReconnectReason:
    """一次网关连接的结束原因。对应 Go ``reconnectReason``。"""

    mode: int = RECONNECT_IDENTIFY
    fatal: bool = False


@dataclass
class GatewayState:
    """跨重连保留的会话状态。对应 Go ``gatewayState``。"""

    session_id: str = ""
    seq: int = 0
    resume: bool = False


def must_json(value: object) -> object:
    """对应 Go ``mustJSON``。

    Python 侧 ``Payload.data`` 保存的是已解析的 JSON 值，序列化由
    :meth:`Payload.to_json` 负责，所以这里是恒等函数（保留名字以便交叉检索）。
    """
    return value


async def read_payload(conn: WebSocketConnection) -> Payload:
    """读一帧网关载荷。对应 Go 的 ``wsjson.Read(ctx, conn, &p)``。"""
    raw = await conn.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"decode qqofficial gateway frame: {exc}") from exc
    return Payload.from_json(parsed)


async def read_payload_ctx(conn: WebSocketConnection, ctx) -> Payload:
    """可被 ctx 取消的读帧。对应 Go ``wsjson.Read(ctx, conn, &p)``。

    :mod:`atri_x.net.websocket` 的 ``recv`` 不接受 ctx，这里改为"等帧的同时
    每 200 ms 检查一次 ctx 是否被取消"，语义与 Go 一致（取消时立即抛出取消原因）。
    """
    from atri_x.delivery.delivery import ctx_error

    if ctx is None:
        return await read_payload(conn)
    pending = asyncio.ensure_future(read_payload(conn))
    try:
        while True:
            done, _pending = await asyncio.wait({pending}, timeout=0.2)
            if done:
                return pending.result()
            err = ctx_error(ctx)
            if err is not None:
                raise err
    except BaseException:
        if not pending.done():
            pending.cancel()
        raise


async def write_gateway(adapter, ctx, conn: WebSocketConnection, p: Payload) -> None:
    """加写锁发送一帧。对应 Go ``(*Adapter).writeGateway``。"""
    async with adapter.ws_write_lock:
        await conn.send_text(
            json.dumps(p.to_json(), ensure_ascii=False, separators=(",", ":"))
        )


def classify_gateway_error(exc: BaseException) -> ReconnectReason:
    """按关闭码决定重连方式。对应 Go ``classifyGatewayError``。"""
    status = int(getattr(exc, "code", 0) or 0)
    if status == 4009:
        return ReconnectReason(mode=RECONNECT_RESUME)
    if status in (4006, 4007):
        return ReconnectReason(mode=RECONNECT_IDENTIFY)
    if status in (4013, 4014, 4914, 4915):
        return ReconnectReason(mode=RECONNECT_IDENTIFY, fatal=True)
    return ReconnectReason(mode=RECONNECT_IDENTIFY)


async def send_identify(adapter, ctx, conn: WebSocketConnection) -> None:
    """发 ``op=2`` 鉴权。对应 Go ``(*Adapter).sendIdentify``。"""
    token = await adapter.client.tokens.token_value(ctx)
    body = IdentifyData(
        token="QQBot " + token,
        intents=INTENT_GROUP_AND_C2C,
        shard=[0, 1],
        properties={
            "$os": platform_module.system(),
            "$browser": "atri-x",
            "$device": "atri-x",
        },
    )
    await write_gateway(adapter, ctx, conn, Payload(op=OP_IDENTIFY, data=body.to_json()))


async def send_resume(adapter, ctx, conn: WebSocketConnection, state: GatewayState) -> None:
    """发 ``op=6`` 续连。对应 Go ``(*Adapter).sendResume``。"""
    token = await adapter.client.tokens.token_value(ctx)
    body = ResumeData(token="QQBot " + token, session_id=state.session_id, seq=state.seq)
    await write_gateway(adapter, ctx, conn, Payload(op=OP_RESUME, data=body.to_json()))


async def heartbeat_loop(
    adapter, ctx, conn: WebSocketConnection, state: GatewayState, interval: float
) -> None:
    """按 ``hello`` 给的间隔发心跳。对应 Go ``(*Adapter).heartbeatLoop``。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await write_gateway(
                adapter, ctx, conn, Payload(op=OP_HEARTBEAT, data=must_json(state.seq))
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 心跳失败即退出，由读循环发现断开
            adapter.log_warn(ctx, "send qqofficial heartbeat failed", error=exc)
            return


async def handle_dispatch(adapter, ctx, handler, p: Payload, state: GatewayState) -> None:
    """处理一条 ``op=0`` 分发事件。对应 Go ``(*Adapter).handleDispatch``。"""
    if p.type == EVENT_READY:
        ready = ReadyData.from_json(p.data)
        state.session_id = ready.session_id
        state.resume = True
        adapter.log_info(
            ctx, "qqofficial gateway ready", bot_id=ready.user.id, bot_name=ready.user.username
        )
        adapter.spawn(adapter.notify_connected(ctx))
        return
    if p.type == EVENT_RESUMED:
        state.resume = True
        return
    if p.type == EVENT_C2C_MESSAGE_CREATE:
        msg = InboundMessage.from_json(p.data)
        adapter.spawn(qq_message.handle_c2c_message(adapter, ctx, handler, p, msg))
        return
    if p.type in (EVENT_GROUP_AT_MESSAGE_CREATE, EVENT_GROUP_MESSAGE_CREATE):
        msg = InboundMessage.from_json(p.data)
        adapter.spawn(qq_message.handle_group_message(adapter, ctx, handler, p, msg))
        return
    if (p.type or "").strip() != "":
        adapter.log_debug(ctx, "qqofficial dispatch ignored", event=p.type)


async def run_gateway_once(
    adapter, ctx, handler, state: GatewayState
) -> tuple[ReconnectReason, BaseException | None]:
    """连一次网关并读到断开。对应 Go ``(*Adapter).runGatewayOnce``。

    返回 ``(重连方式, 断开原因)``；``断开原因`` 为 ``None`` 表示是网关主动要求
    重连（``op=7``）。
    """
    identify_reason = ReconnectReason(mode=RECONNECT_IDENTIFY)
    try:
        gateway_url = await adapter.client.gateway(ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 取网关失败按 identify 重连
        return identify_reason, exc
    try:
        conn = await ws_connect(gateway_url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return identify_reason, AtriXError(f"connect qqofficial gateway: {exc}")
    try:
        try:
            hello = await read_payload_ctx(conn, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return classify_gateway_error(exc), exc
        if hello.op != OP_HELLO:
            return identify_reason, AtriXError(
                f"qqofficial gateway expected hello, got op {hello.op}"
            )
        try:
            hello_body = HelloData.from_json(hello.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return identify_reason, AtriXError(f"decode qqofficial hello: {exc}")
        interval = float(hello_body.heartbeat_interval) / 1000.0
        if interval <= 0:
            interval = DEFAULT_HEARTBEAT_INTERVAL

        try:
            if state.resume and state.session_id != "":
                await send_resume(adapter, ctx, conn, state)
            else:
                await send_identify(adapter, ctx, conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 鉴权/续连失败都退回 identify
            return identify_reason, exc

        heartbeat = asyncio.ensure_future(
            heartbeat_loop(adapter, ctx, conn, state, interval)
        )
        try:
            while True:
                try:
                    p = await read_payload_ctx(conn, ctx)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    return classify_gateway_error(exc), exc
                if p.seq is not None:
                    state.seq = p.seq
                if p.op == OP_DISPATCH:
                    try:
                        await handle_dispatch(adapter, ctx, handler, p, state)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单条事件失败不影响连接
                        adapter.log_warn(
                            ctx, "handle qqofficial dispatch failed", event=p.type, error=exc
                        )
                elif p.op == OP_HEARTBEAT:
                    with contextlib.suppress(Exception):
                        await write_gateway(
                            adapter, ctx, conn, Payload(op=OP_HEARTBEAT, data=state.seq)
                        )
                elif p.op == OP_HEARTBEAT_ACK:
                    pass
                elif p.op == OP_RECONNECT:
                    state.resume = True
                    return ReconnectReason(mode=RECONNECT_RESUME), None
                elif p.op == OP_INVALID_SESSION:
                    state.resume = False
                    state.session_id = ""
                    return identify_reason, AtriXError("qqofficial invalid session")
                else:
                    adapter.log_debug(ctx, "qqofficial gateway op ignored", op=p.op)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    finally:
        with contextlib.suppress(Exception):
            await conn.close(CLOSE_NORMAL, "reconnect")


__all__ = [name for name in dir() if not name.startswith("_")]
