"""远程 CLI 服务端：让远程终端经 WebSocket 接入本机的 CLI 适配器。

对应 Go：``internal/platform/cli/remote_server.go``。

职责：在 ``cfg.server.listen`` 上提供 ``/cli/v1/ws``，用 token 校验 ``hello`` 握手，
把 ``input`` 帧变成 ``handler.handle_message(...)``，把
:class:`atri_x.delivery.delivery.Output` / ``Notice`` / 运行状态 / 流式片段写成帧；
ctx 里携带"当前客户端"，因此 :meth:`RemoteServer.send_chat` 能回到正确的那条连接。

与 Go 的差异（仅形式）：

* ``net/http.Server`` + ``coder/websocket.Accept`` → :func:`atri_x.net.websocket.serve`
  （asyncio 自带能力 + 既有 WebSocket 实现，**不引入**任何 Web 框架）。
  ``ReadHeaderTimeout: 5s`` 用 ``handshake_timeout=5.0`` 近似；
  ``AcceptOptions{InsecureSkipVerify: true}`` 对应"不校验 Origin"（``origins=None``）。
* ``wsjson.Read/Write`` → ``connection.recv()`` / ``send_text()`` + ``json``。
* ``context.WithTimeout`` 的 15s 写超时 → ``asyncio.wait_for``，超时抛
  :class:`atri_x.errors.InvalidError`（Go 侧写入失败也是返回 error）。
* ``context.WithValue(ctx, remoteClientKey{}, client)`` → 用
  :func:`atri_x.delivery.delivery.ctx_with_value` / :func:`ctx_value` 携带同一个
  私有哨兵键。
* ``go func(){ HandleMessage }()`` → ``asyncio.create_task``，任务登记在
  :meth:`RemoteServer.pending_tasks`（Python 侧测试接缝）。
* ``sync.Mutex`` 保护 ``clients`` → :class:`asyncio.Lock`。
* ``(T, error)`` → 正常返回值 / 抛异常。
* ``(*RemoteServer).SetCompleter`` 收 ``*completion.Service``；Python 侧收任意有
  ``complete(ctx, req)`` 的对象（协程或同步都接受，见 :meth:`RemoteServer._complete`）。
* ``(*RemoteServer).SetRuntimeStatus`` / ``StartStream`` 在 Go 里都是**同步**方法。
  Python 侧保持同步签名（调用方 :func:`atri_x.agent.status.publish_runtime_status`
  与 :func:`atri_x.agent.turn_output.start_message_stream` 都是同步的）：
  ``start_stream`` 只构造 :class:`RemoteMessageStream`，直接返回；
  ``set_runtime_status`` 需要写连接（协程），因此按 Go 里
  ``_ = sender.SetRuntimeStatus(...)`` 的语义把写任务投给事件循环并丢弃错误，
  任务同时登记在 :attr:`RemoteClientConn.pending_writes`，
  由下一次 :meth:`RemoteClientConn.write` 先 await，保证帧顺序与 Go 一致。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.completion.completion import Request as CompletionRequest
from atri_x.config.loader import config_env
from atri_x.delivery.delivery import (
    MessageStream,
    Notice,
    Output,
    Receipt,
    Target,
    ctx_error,
    ctx_value,
    ctx_with_value,
    fallback_output,
)
from atri_x.errors import InvalidError
from atri_x.net import websocket as ws
from atri_x.platform.cli.config import (
    DEFAULT_REMOTE_PATH,
    Config,
    apply_config_defaults,
)
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_CHAT,
    REMOTE_MSG_COMPLETE,
    REMOTE_MSG_COMPLETE_RESULT,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_HISTORY,
    REMOTE_MSG_HISTORY_RESULT,
    REMOTE_MSG_INPUT,
    REMOTE_MSG_REASONING,
    REMOTE_MSG_SESSIONS,
    REMOTE_MSG_SESSIONS_RESULT,
    REMOTE_MSG_STATUS,
    REMOTE_MSG_STREAM_APPEND,
    REMOTE_MSG_STREAM_FINISH,
    REMOTE_MSG_STREAM_REPLACE,
    REMOTE_WRITE_TIMEOUT,
    RemoteMessage,
    output_message,
    remote_notice_message,
    set_from_strings,
)
from atri_x.platform.cli.session_browse import SessionBrowseMixin
from atri_x.platform.platform import MessageContext, with_message_context
from atri_x.runtime.status import Snapshot
from atri_x.security import ActorID

#: 平台名。对应 Go ``(*RemoteServer).Name``。
PLATFORM_NAME = "cli"

#: 握手读超时（秒）。对应 Go ``handleWebSocket`` 里的 ``10 * time.Second``。
HELLO_READ_TIMEOUT = 10.0

#: 关闭时的宽限（秒）。对应 Go ``Run`` 里的 ``WithTimeout(..., 5*time.Second)``。
SHUTDOWN_TIMEOUT = 5.0

#: HTTP 头部读超时（秒）。对应 Go ``ReadHeaderTimeout: 5 * time.Second``。
READ_HEADER_TIMEOUT = 5.0

#: ctx 中携带"当前远程客户端"的私有键。
_REMOTE_CLIENT_KEY = object()

__all__ = [
    "HELLO_READ_TIMEOUT",
    "PLATFORM_NAME",
    "READ_HEADER_TIMEOUT",
    "SHUTDOWN_TIMEOUT",
    "CompletionProvider",
    "RemoteClientConn",
    "RemoteMessageStream",
    "RemoteServer",
    "RemoteServerOptions",
    "new_remote_server",
    "remote_client_from",
    "with_remote_client",
]


class CompletionProvider:
    """能给出补全候选的对象。对应 Go ``CompletionProvider``（本类只为标注形状）。

    Python 侧是鸭子类型：只要有 ``complete(ctx, req)`` 就行，协程或同步都可以。
    """

    def complete(self, ctx: Any, req: CompletionRequest) -> Any:  # pragma: no cover - 协议
        raise NotImplementedError


def with_remote_client(ctx: Any, client: "RemoteClientConn") -> Any:
    """对应 ``context.WithValue(ctx, remoteClientKey{}, client)``。"""
    return ctx_with_value(ctx, _REMOTE_CLIENT_KEY, client)


def remote_client_from(ctx: Any) -> "RemoteClientConn | None":
    """对应 ``ctx.Value(remoteClientKey{}).(*remoteClientConn)``。"""
    client = ctx_value(ctx, _REMOTE_CLIENT_KEY)
    return client if isinstance(client, RemoteClientConn) else None


@dataclass
class RemoteServerOptions:
    """构造 :class:`RemoteServer` 的入参。对应 Go ``RemoteServerOptions``。"""

    config: Config = field(default_factory=Config)
    config_dir: str = ""
    superadmins: Sequence[str] = field(default_factory=list)
    completer: Any = None
    #: 会话浏览扩展（Python 版新增）：鸭子类型对象，提供
    #: ``scope(client_id)`` / ``list(scope, limit, query)`` / ``history(scope, session_id, limit)``。
    session_browser: Any = None


@dataclass(eq=False)
class RemoteClientConn:
    """一条已握手的远程连接。对应 Go ``remoteClientConn``。

    ``eq=False`` 是刻意的：Go 用 ``*remoteClientConn`` 指针作 map key，同一客户端
    的多条连接靠**身份**区分；dataclass 默认的按值相等既不可哈希、语义也不对。
    """

    id: str = ""
    connection: Any = None
    server: "RemoteServer | None" = None

    def __post_init__(self) -> None:
        # 由同步调用方（``RemoteServer.set_runtime_status``）投递的后台写任务。
        # Go 的 ``client.write`` 是阻塞写，帧顺序天然成立；Python 的连接写是协程，
        # 所以这里先把任务记下来，交给下一次 :meth:`write` 先 await（见其 docstring）。
        self.pending_writes: list[asyncio.Task[Any]] = []

    def track_write(self, task: "asyncio.Task[Any]") -> None:
        """登记一个同步投递的后台写任务（下一次 :meth:`write` 会先 await 它）。"""
        self.pending_writes.append(task)

    async def drain_pending_writes(self) -> None:
        """等掉同步投递的写任务，保住帧顺序。错误按 Go 的 ``_ = ...`` 丢弃。"""
        pending, self.pending_writes = self.pending_writes, []
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 同步调用方本来就丢弃写错误
                pass

    async def write(self, ctx: Any, msg: RemoteMessage) -> None:
        """写一帧（15s 超时）。对应 Go ``(*remoteClientConn).write``。

        写之前先把同步投递的帧落地，这样「同步调用 → 之后的第一条异步写」的
        帧顺序与 Go 的同步写一致。
        """
        await self.drain_pending_writes()
        await self.write_now(ctx, msg)

    async def write_now(self, ctx: Any, msg: RemoteMessage) -> None:
        """直接写一帧（不先清同步投递的帧）。对应 Go ``(*remoteClientConn).write``。"""
        payload = json.dumps(msg.to_json(), ensure_ascii=False)
        try:
            await asyncio.wait_for(self.connection.send_text(payload), REMOTE_WRITE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise InvalidError(f"write cli message timed out after {REMOTE_WRITE_TIMEOUT}s") from exc


def new_remote_server(opts: RemoteServerOptions) -> "RemoteServer":
    """对应 Go ``NewRemoteServer``；配置非法时抛 :class:`atri_x.errors.InvalidError`。"""
    return RemoteServer(opts)


class RemoteServer(SessionBrowseMixin):
    """远程 CLI 服务端。对应 Go ``RemoteServer``。"""

    def __init__(self, opts: RemoteServerOptions) -> None:
        cfg = opts.config
        apply_config_defaults(cfg)
        if not cfg.server.enabled:
            raise InvalidError("cli remote server is disabled")
        if not (cfg.server.listen or "").strip():
            raise InvalidError("cli remote server listen address is empty")
        if not cfg.server.tokens:
            raise InvalidError("cli remote server tokens are not configured")
        self.cfg = cfg
        self.config_dir = opts.config_dir
        self.superadmins = set_from_strings(opts.superadmins)
        self.completer = opts.completer
        #: 会话浏览扩展（Python 版新增）。没有注入时 ``sessions``/``history`` 帧
        #: 返回空结果，不影响原有协议。
        self.session_browser = opts.session_browser
        self.server: ws.WebSocketServer | None = None
        self._lock = asyncio.Lock()
        self._clients: dict[str, set[RemoteClientConn]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.logger: logging.Logger | None = None

    # -- 装配 ---------------------------------------------------------------
    def set_completer(self, completer: Any) -> None:
        """对应 Go ``(*RemoteServer).SetCompleter``。"""
        self.completer = completer

    def set_session_browser(self, browser: Any) -> None:
        """装上会话浏览实现（Python 版新增；``None`` 表示不支持浏览帧）。

        与 :meth:`set_completer` 同一个装配时机（``app/integrations.py`` 的
        ``register_session_browsers``），只是数据来源是会话服务而不是补全服务。
        """
        self.session_browser = browser

    def name(self) -> str:
        """对应 Go ``(*RemoteServer).Name``。"""
        return PLATFORM_NAME

    @property
    def port(self) -> int:
        """实际监听端口（``listen`` 用 ``:0`` 时由内核分配）。"""
        return self.server.port if self.server is not None else 0

    def pending_tasks(self) -> set[asyncio.Task[Any]]:
        """尚未结束的后台任务（Python 侧测试接缝；Go 不保留句柄）。"""
        return set(self._tasks)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """对应 Go 的 ``go`` 语句。"""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- 生命周期 -----------------------------------------------------------
    async def run(self, ctx: Any, handler: Any) -> None:
        """启动服务端直到 ctx 取消。对应 Go ``(*RemoteServer).Run``。

        ``ws.serve`` 在端口被占用/地址非法时**立刻**抛 ``OSError``，对应 Go 的
        ``ListenAndServe`` 错误分支；否则等到 ctx 取消后关停并抛出 ctx 的取消原因。
        """
        host, port = parse_listen(self.cfg.server.listen)
        self.server = await ws.serve(
            lambda connection: self.handle_websocket(ctx, handler, connection),
            host,
            port,
            path=DEFAULT_REMOTE_PATH,
            handshake_timeout=READ_HEADER_TIMEOUT,
        )
        try:
            await _wait_cancel(ctx)
        finally:
            await self._shutdown()
        err = ctx_error(ctx)
        if err is not None:
            raise err

    async def _shutdown(self) -> None:
        """停监听并关掉所有客户端（带 5s 宽限）。对应 Go ``Shutdown`` + ``closeAll``。"""
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.close(), SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError, OSError):
                pass
            self.server = None
        await self.close_all()

    # -- 单连接 -------------------------------------------------------------
    async def handle_websocket(self, ctx: Any, handler: Any, connection: Any) -> None:
        """一条远程连接的会话。对应 Go ``(*RemoteServer).handleWebSocket``。"""
        try:
            hello = await self._read_hello(connection, ctx)
            if hello is None:
                return
            client_id = (hello.client_id or "").strip()
            if hello.type != REMOTE_MSG_HELLO or client_id == "" or not self.valid_token(
                client_id, hello.token
            ):
                await _safe_write(connection, RemoteMessage(type=REMOTE_MSG_ERROR, text="unauthorized"))
                return
            client = RemoteClientConn(id=client_id, connection=connection, server=self)
            await self.add_client(client)
            try:
                await client.write(ctx, RemoteMessage(type=REMOTE_MSG_HELLO_OK, client_id=client_id))
                message_ctx = self.message_context(ctx, client)
                while True:
                    raw = await connection.recv()
                    msg = RemoteMessage.from_json(_decode(raw))
                    await self.handle_client_message(message_ctx, handler, client, msg)
            finally:
                await self.remove_client(client)
        except ws.ConnectionClosed:
            return
        except ws.WebSocketError:
            return
        finally:
            try:
                await connection.close(ws.CLOSE_NORMAL, "bye")
            except Exception:  # noqa: BLE001 - 关闭失败不影响其它连接
                pass

    async def _read_hello(self, connection: Any, ctx: Any) -> RemoteMessage | None:
        """读握手帧（10s 超时）。对应 Go 里 ``WithTimeout(ctx, 10*time.Second)`` 的读。"""
        try:
            raw = await connection.recv(timeout=HELLO_READ_TIMEOUT)
        except (ws.ConnectionClosed, ws.WebSocketError, TimeoutError, asyncio.TimeoutError):
            return None
        return RemoteMessage.from_json(_decode(raw))

    async def handle_client_message(
        self, ctx: Any, handler: Any, client: RemoteClientConn, msg: RemoteMessage
    ) -> None:
        """处理一条客户端消息。对应 Go ``(*RemoteServer).handleClientMessage``。"""
        if msg.type == REMOTE_MSG_INPUT:
            text = (msg.text or "").strip()
            if text == "":
                return
            self._spawn(self._dispatch_input(ctx, handler, client, text))
        elif msg.type == REMOTE_MSG_COMPLETE:
            items = await self._complete(ctx, msg)
            await _safe_write(
                client.connection,
                RemoteMessage(type=REMOTE_MSG_COMPLETE_RESULT, id=msg.id, items=items),
            )
        elif msg.type == REMOTE_MSG_SESSIONS:
            sessions = self._browse_sessions(client, msg)
            await _safe_write(
                client.connection,
                RemoteMessage(
                    type=REMOTE_MSG_SESSIONS_RESULT,
                    id=msg.id,
                    sessions=sessions,
                ),
            )
        elif msg.type == REMOTE_MSG_HISTORY:
            history = self._browse_history(client, msg)
            await _safe_write(
                client.connection,
                RemoteMessage(
                    type=REMOTE_MSG_HISTORY_RESULT,
                    id=msg.id,
                    session_id=msg.session_id,
                    history=history,
                ),
            )

    async def _dispatch_input(
        self, ctx: Any, handler: Any, client: RemoteClientConn, text: str
    ) -> None:
        """``input`` 帧 → handler（对应 Go 的 goroutine）。"""
        try:
            await handler.handle_message(ctx, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 错误回给客户端
            await _safe_write(
                client.connection, RemoteMessage(type=REMOTE_MSG_ERROR, text=str(exc))
            )

    async def _complete(self, ctx: Any, msg: RemoteMessage) -> list[Any]:
        """跑补全（协程或同步都接受）。对应 Go ``s.completer.Complete(...)``。"""
        if self.completer is None:
            return []
        request = CompletionRequest(text=msg.text, cursor=msg.cursor)
        result = self.completer.complete(ctx, request)
        if inspect.isawaitable(result):
            result = await result
        return list(result or [])

    # -- 出站 ---------------------------------------------------------------
    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        """对应 Go ``(*RemoteServer).SendChat``（落点来自 ctx 里的客户端）。"""
        client = remote_client_from(ctx)
        if client is None:
            raise InvalidError("cli chat target missing")
        await client.write(ctx, output_message(REMOTE_MSG_CHAT, fallback_output(outputs)))
        return Receipt()

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        """对应 Go ``(*RemoteServer).SendNotice``。"""
        clients = await self.target_clients(ctx, notice.target)
        await self.write_clients(ctx, clients, remote_notice_message(notice))
        return Receipt()

    def set_runtime_status(self, ctx: Any, snapshot: Snapshot) -> None:
        """对应 Go ``(*RemoteServer).SetRuntimeStatus``（没有客户端时静默）。

        同步方法：Go 的调用方 ``agent.status.publishRuntimeStatus`` 是同步的
        （``_ = sender.SetRuntimeStatus(...)``），而连接写是协程，所以这里按 Go 的
        "投出去、丢弃 error" 语义把写交给事件循环：任务登记进
        :attr:`RemoteClientConn.pending_writes`，下一次 :meth:`RemoteClientConn.write`
        会先 await 它，帧顺序因此与 Go 的同步写一致。写失败只记 debug 日志；
        只有「没有事件循环可投递」这种环境错误会抛给调用方（调用方按 Go 的 ``_ =``
        忽略它，见 :func:`atri_x.agent.status.publish_runtime_status`）。
        """
        client = remote_client_from(ctx)
        if client is None:
            return
        client.track_write(self._spawn(self._write_status(client, ctx, snapshot)))

    async def _write_status(
        self, client: RemoteClientConn, ctx: Any, snapshot: Snapshot
    ) -> None:
        """把一条状态帧投给客户端；失败只记日志。对应 Go 里被丢弃的 ``error``。"""
        try:
            await client.write_now(ctx, RemoteMessage(type=REMOTE_MSG_STATUS, snapshot=snapshot))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Go 丢弃 SetRuntimeStatus 的错误
            if self.logger is not None:
                self.logger.debug("cli remote status write failed: %s", exc)

    async def send_reasoning(self, ctx: Any, text: str) -> None:
        """对应 Go ``(*RemoteServer).SendReasoning``（没有客户端时静默）。"""
        if not (text or "").strip():
            return
        client = remote_client_from(ctx)
        if client is None:
            return
        await client.write(ctx, RemoteMessage(type=REMOTE_MSG_REASONING, text=text))

    def start_stream(self, ctx: Any) -> MessageStream:
        """对应 Go ``(*RemoteServer).StartStream``。

        同步工厂：:class:`RemoteMessageStream` 本身就是同步构造，Go 侧同样是同步的
        ``(MessageStream, error)``（这里按契约抛异常）。
        """
        client = remote_client_from(ctx)
        if client is None:
            raise InvalidError("cli stream target missing")
        return RemoteMessageStream(client)

    # -- 目标解析 -----------------------------------------------------------
    def message_context(self, ctx: Any, client: RemoteClientConn) -> Any:
        """为一条远程连接构造消息上下文。对应 Go ``(*RemoteServer).messageContext``。"""
        message = MessageContext(
            platform=self.name(),
            actor_id=ActorID(self.name(), client.id),
            platform_user_id=client.id,
            nickname=client.id,
            display_name=client.id,
            scope_id=client.id,
            sender=self,
        )
        return with_remote_client(with_message_context(ctx, message), client)

    async def target_clients(self, ctx: Any, target: Target) -> list[RemoteClientConn]:
        """解析通知落点。对应 Go ``(*RemoteServer).targetClients``。"""
        platform_name = (target.platform or "").strip()
        if platform_name and platform_name != self.name():
            raise InvalidError(f'cli cannot send to platform "{platform_name}"')
        if target.empty():
            client = remote_client_from(ctx)
            if client is not None:
                return [client]
            raise InvalidError("cli target missing")
        if target.superadmins:
            return await self.online_clients(lambda cid: cid in self.superadmins)
        user_id = (target.private_user_id or "").strip()
        if user_id:
            return await self.online_clients(lambda cid: cid == user_id)
        raise InvalidError("cli target missing private_user_id or superadmins")

    async def online_clients(self, match: Callable[[str], bool]) -> list[RemoteClientConn]:
        """按条件取在线连接。对应 Go ``(*RemoteServer).onlineClients``。"""
        async with self._lock:
            out: list[RemoteClientConn] = []
            for client_id, conns in self._clients.items():
                if not match(client_id):
                    continue
                out.extend(conns)
        if not out:
            raise InvalidError("no matching cli clients are connected")
        return out

    async def write_clients(
        self, ctx: Any, clients: Sequence[RemoteClientConn], msg: RemoteMessage
    ) -> None:
        """给多个连接写同一帧。对应 Go ``(*RemoteServer).writeClients``。"""
        errors: list[str] = []
        sent = 0
        for client in clients:
            try:
                await client.write(ctx, msg)
            except Exception as exc:  # noqa: BLE001 - 单个失败不影响其余
                errors.append(client.id + ": " + str(exc))
                await self.remove_client(client)
                try:
                    await client.connection.close(ws.CLOSE_INTERNAL_ERROR, "write failed")
                except Exception:  # noqa: BLE001
                    pass
                continue
            sent += 1
        if sent == 0:
            if errors:
                raise InvalidError("send cli message failed: " + "; ".join(errors))
            raise InvalidError("no matching cli clients are connected")

    # -- 连接表 -------------------------------------------------------------
    async def add_client(self, client: RemoteClientConn) -> None:
        """对应 Go ``(*RemoteServer).addClient``。"""
        async with self._lock:
            self._clients.setdefault(client.id, set()).add(client)

    async def remove_client(self, client: RemoteClientConn) -> None:
        """对应 Go ``(*RemoteServer).removeClient``。"""
        async with self._lock:
            conns = self._clients.get(client.id)
            if conns is None:
                return
            conns.discard(client)
            if not conns:
                self._clients.pop(client.id, None)

    async def close_all(self) -> None:
        """关掉全部连接。对应 Go ``(*RemoteServer).closeAll``。"""
        async with self._lock:
            clients = [client for conns in self._clients.values() for client in conns]
            self._clients = {}
        for client in clients:
            try:
                await client.connection.close(ws.CLOSE_NORMAL, "server stopped")
            except Exception:  # noqa: BLE001
                pass

    def connected_client_ids(self) -> list[str]:
        """当前在线的客户端 ID（升序）。对应 Go ``ConnectedClientIDs``。"""
        return sorted(self._clients)

    # -- 鉴权 ---------------------------------------------------------------
    def valid_token(self, client_id: str, token: str) -> bool:
        """校验 hello 里的 token。对应 Go ``(*RemoteServer).validToken``。"""
        envs = self.cfg.server.tokens.get(client_id) or []
        if not envs:
            return False
        for env_name in envs:
            value = config_env(env_name, self.config_dir)
            if value is None or not value.strip():
                continue
            if token == value.strip():
                return True
        return False


@dataclass
class RemoteMessageStream:
    """远程连接上的流式助手消息。对应 Go ``remoteMessageStream``。"""

    client: RemoteClientConn = field(default_factory=RemoteClientConn)

    async def append(self, ctx: Any, text: str) -> None:
        """对应 Go ``remoteMessageStream.Append``。"""
        if not text:
            return
        await self.client.write(ctx, RemoteMessage(type=REMOTE_MSG_STREAM_APPEND, text=text))

    async def replace(self, ctx: Any, text: str) -> Receipt:
        """对应 Go ``remoteMessageStream.Replace``。"""
        await self.client.write(ctx, RemoteMessage(type=REMOTE_MSG_STREAM_REPLACE, text=text))
        return Receipt()

    async def finish(self, ctx: Any) -> Receipt:
        """对应 Go ``remoteMessageStream.Finish``。"""
        await self.client.write(ctx, RemoteMessage(type=REMOTE_MSG_STREAM_FINISH))
        return Receipt()


# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------

def parse_listen(listen: str) -> tuple[str, int]:
    """把 ``host:port`` 拆成 ``(host, port)``。对应 Go ``http.Server{Addr: ...}`` 的解析。"""
    text = (listen or "").strip()
    if text.startswith("["):  # IPv6 字面量
        host, _, rest = text[1:].partition("]")
        _, _, port_text = rest.partition(":")
        return host, int(port_text or "0")
    host, separator, port_text = text.rpartition(":")
    if not separator:
        return text, 0
    return host, int(port_text or "0")


def _decode(raw: Any) -> Any:
    """把一条 WebSocket 消息解成 JSON 值（二进制帧按 UTF-8 文本处理）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):  # pragma: no cover - 防御性
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


async def _safe_write(connection: Any, msg: RemoteMessage) -> None:
    """尽力写一帧（失败静默）。对应 Go 里被忽略返回值的 ``wsjson.Write``。"""
    try:
        await asyncio.wait_for(
            connection.send_text(json.dumps(msg.to_json(), ensure_ascii=False)),
            REMOTE_WRITE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        pass


async def _wait_cancel(ctx: Any) -> None:
    """等到 ctx 取消。对应 Go 的 ``<-ctx.Done()``。"""
    while ctx_error(ctx) is None:
        event = asyncio.Event()
        remover = None
        adder = getattr(ctx, "add_callback", None)
        if callable(adder):
            try:
                remover = adder(event.set)
            except Exception:  # noqa: BLE001 - 不支持回调的 ctx 走轮询
                remover = None
        try:
            try:
                await asyncio.wait_for(event.wait(), 0.1)
            except (asyncio.TimeoutError, TimeoutError):
                continue
        finally:
            if remover is not None:
                try:
                    remover()
                except Exception:  # noqa: BLE001
                    pass
