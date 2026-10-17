"""远程 CLI 客户端：把本地终端接到远端（或本机）的远程 CLI 服务端。

对应 Go：``internal/platform/cli/remote_client.go``。

职责：按档案解析 URL 与 token → 连上 WebSocket → 发 ``hello`` → 等 ``hello_ok`` →
起读循环把服务端的帧变成 TUI 消息 → 进入 TUI（:func:`atri_x.platform.cli.tui.run_tui`）。
它同时是 TUI 的 ``completion`` 提供者（``complete`` 帧的往返）与
``handler``（把提交的输入写成 ``input`` 帧）。

与 Go 的差异（仅形式）：

* ``websocket.Dial`` + ``wsjson`` → :func:`atri_x.net.websocket.connect` +
  ``recv`` / ``send_text``（见 :mod:`atri_x.platform.cli.remote_server` 的同类说明）。
* ``sync.Mutex`` 保护 ``pending`` → :class:`asyncio.Lock`；``atomic.Uint64`` 的序号
  用普通整数（事件循环单线程）。
* ``go c.readLoop(ctx)`` → ``asyncio.create_task``，句柄登记在
  :meth:`RemoteClient.pending_tasks`。
* Go 的 ``Complete`` 是同步接口（内部 select 等 2 秒）；Python 侧
  :meth:`RemoteClient.complete` 是协程（供 ``completion.Service`` 直接用），
  另留同步的 :meth:`RemoteClient.complete_sync` 保留 Go 的 2 秒等待语义。
* ``appconfig.ConfigEnv`` → :func:`atri_x.config.loader.config_env`
  （``None`` 表示 Go 的 ``ok=false``）。
* ``runTUI`` 由 :mod:`atri_x.platform.cli.tui` 提供，**函数内延迟导入**
  （该包此刻可能尚未落地）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from atri_x.completion.completion import Item, Request as CompletionRequest
from atri_x.config.loader import config_env
from atri_x.errors import AtriXError, InvalidError
from atri_x.net import websocket as ws
from atri_x.platform.cli.config import Config, ClientConfig, apply_config_defaults
from atri_x.platform.cli.protocol import (
    LEVEL_ERROR,
    REMOTE_MSG_CHAT,
    REMOTE_MSG_COMPLETE,
    REMOTE_MSG_COMPLETE_RESULT,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_INPUT,
    REMOTE_MSG_NOTICE,
    REMOTE_MSG_REASONING,
    REMOTE_MSG_STATUS,
    REMOTE_MSG_STREAM_APPEND,
    REMOTE_MSG_STREAM_FINISH,
    REMOTE_MSG_STREAM_REPLACE,
    REMOTE_WRITE_TIMEOUT,
    RemoteMessage,
    remote_notice_level,
)

#: 补全往返的等待上限（秒）。对应 Go ``Complete`` 里的 ``2 * time.Second``。
COMPLETE_TIMEOUT = 2.0

#: 平台名。对应 Go ``(*RemoteClient).Name``（与本地适配器同名）。
PLATFORM_NAME = "cli"

#: TUI 消息队列容量。对应 Go ``make(chan tea.Msg, 256)``。
OUTPUT_QUEUE_SIZE = 256

__all__ = [
    "COMPLETE_TIMEOUT",
    "OUTPUT_QUEUE_SIZE",
    "PLATFORM_NAME",
    "RemoteClient",
    "RemoteClientOptions",
    "new_remote_client",
    "remote_client_notice_level",
]


@dataclass
class RemoteClientOptions:
    """构造 :class:`RemoteClient` 的入参。对应 Go ``RemoteClientOptions``。"""

    config: Config = field(default_factory=Config)
    config_dir: str = ""
    client_name: str = ""


def new_remote_client(opts: RemoteClientOptions) -> "RemoteClient":
    """对应 Go ``NewRemoteClient``；配置非法时抛 :class:`atri_x.errors.InvalidError`。"""
    return RemoteClient(opts)


def remote_client_notice_level(text: str) -> int:
    """线上级别文本 → ``logging`` 级别整数。对应 Go ``remoteNoticeLevel``。"""
    return remote_notice_level(text)


class RemoteClient:
    """远程 CLI 客户端。对应 Go ``RemoteClient``。"""

    def __init__(self, opts: RemoteClientOptions) -> None:
        cfg = opts.config
        apply_config_defaults(cfg)
        profile, client = cfg.client(opts.client_name)
        if not (client.url or "").strip():
            raise InvalidError(f'cli client "{profile}" url is empty')
        self.cfg = cfg
        self.config_dir = opts.config_dir
        self.profile = profile
        self.client = client
        self.connection: Any = None
        self.seq = 0
        # Go 用 sync.Mutex 保护 pending；本实现只在事件循环线程上读写这张表，
        # 因此不需要锁（await 期间不会有其它协程插入到这两行之间）。
        self._pending: dict[str, asyncio.Queue[list[Item]]] = {}
        self.output: asyncio.Queue[Any] = asyncio.Queue(maxsize=OUTPUT_QUEUE_SIZE)
        self._tasks: set[asyncio.Task[Any]] = set()
        self.logger: logging.Logger | None = None

    # -- 取值 ---------------------------------------------------------------
    def url(self) -> str:
        """对应 Go ``(*RemoteClient).URL``。"""
        return self.client.url

    def name(self) -> str:
        """对应 Go ``(*RemoteClient).Name``。"""
        return PLATFORM_NAME

    def pending_tasks(self) -> set[asyncio.Task[Any]]:
        """尚未结束的后台任务（Python 侧测试接缝）。"""
        return set(self._tasks)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """对应 Go 的 ``go`` 语句。"""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- 生命周期 -----------------------------------------------------------
    async def run(self, ctx: Any) -> None:
        """连上服务端并进入 TUI。对应 Go ``(*RemoteClient).Run``。"""
        token = self.resolve_token()
        try:
            self.connection = await ws.connect(self.client.url)
        except (ws.WebSocketError, OSError) as exc:
            raise InvalidError(f"connect cli server {self.client.url}: {exc}") from exc
        try:
            await self.write(
                ctx, RemoteMessage(type=REMOTE_MSG_HELLO, client_id=self.client.id, token=token)
            )
            hello = await self._read_hello()
            if hello.type == REMOTE_MSG_ERROR:
                raise InvalidError(f"cli server rejected connection: {hello.text}")
            if hello.type != REMOTE_MSG_HELLO_OK:
                raise InvalidError(f'unexpected cli server message "{hello.type}"')
            self._spawn(self.read_loop(ctx))
            await self.run_tui(ctx)
        finally:
            with contextlib.suppress(Exception):
                await self.connection.close(ws.CLOSE_NORMAL, "bye")

    async def run_tui(self, ctx: Any) -> None:
        """进入 TUI。对应 Go 里 ``runTUI(ctx, c, c, c.output, nil, c.client.ID, "assistant")``。"""
        from atri_x.platform.cli.tui import run_tui  # 延迟导入：见模块 docstring

        await run_tui(
            ctx,
            self,
            self,
            self.output,
            None,
            self.client.id,
            "assistant",
        )

    async def _read_hello(self) -> RemoteMessage:
        """读服务端的握手回应。"""
        raw = await self.connection.recv()
        return RemoteMessage.from_json(_decode(raw))

    # -- 入站 ---------------------------------------------------------------
    async def read_loop(self, ctx: Any) -> None:
        """读循环。对应 Go ``(*RemoteClient).readLoop``。"""
        from atri_x.platform.cli.tui import TuiNoticeMsg

        while True:
            try:
                raw = await self.connection.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断开即通知并结束
                self.send_tui(
                    TuiNoticeMsg("cli server disconnected: " + str(exc), LEVEL_ERROR)
                )
                return
            self.handle_server_message(RemoteMessage.from_json(_decode(raw)))

    def handle_server_message(self, msg: RemoteMessage) -> None:
        """把一帧服务端消息变成 TUI 消息。对应 Go ``(*RemoteClient).handleServerMessage``。"""
        from atri_x.platform.cli.tui import (
            TuiFinishAssistantMsg,
            TuiNoticeMsg,
            TuiOutputMsg,
            TuiReasoningMsg,
            TuiReplaceAssistantMsg,
            TuiStatusMsg,
        )

        if msg.type == REMOTE_MSG_CHAT:
            self.send_tui(TuiOutputMsg(msg.text))
        elif msg.type == REMOTE_MSG_NOTICE:
            self.send_tui(TuiNoticeMsg(msg.text, remote_client_notice_level(msg.level)))
        elif msg.type == REMOTE_MSG_REASONING:
            self.send_tui(TuiReasoningMsg(msg.text))
        elif msg.type == REMOTE_MSG_STATUS:
            self.send_tui(TuiStatusMsg(msg.snapshot))
        elif msg.type == REMOTE_MSG_STREAM_APPEND:
            self.send_tui(TuiOutputMsg(msg.text))
        elif msg.type == REMOTE_MSG_STREAM_REPLACE:
            self.send_tui(TuiReplaceAssistantMsg(msg.text))
        elif msg.type == REMOTE_MSG_STREAM_FINISH:
            self.send_tui(TuiFinishAssistantMsg())
        elif msg.type == REMOTE_MSG_COMPLETE_RESULT:
            queue = self._pending.get(msg.id)
            if queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(list(msg.items))
        elif msg.type == REMOTE_MSG_ERROR:
            self.send_tui(TuiNoticeMsg("error: " + msg.text, LEVEL_ERROR))

    def send_tui(self, msg: Any) -> None:
        """非阻塞投递一条 TUI 消息。对应 Go ``(*RemoteClient).sendTUI``。"""
        with contextlib.suppress(asyncio.QueueFull):
            self.output.put_nowait(msg)

    # -- 出站 ---------------------------------------------------------------
    async def handle_message(self, ctx: Any, text: str) -> None:
        """把提交的输入写成 ``input`` 帧。对应 Go ``(*RemoteClient).HandleMessage``。"""
        await self.write(ctx, RemoteMessage(type=REMOTE_MSG_INPUT, text=text))

    async def write(self, ctx: Any, msg: RemoteMessage) -> None:
        """写一帧（15s 超时）。对应 Go ``(*RemoteClient).write``。"""
        payload = json.dumps(msg.to_json(), ensure_ascii=False)
        try:
            await asyncio.wait_for(self.connection.send_text(payload), REMOTE_WRITE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise InvalidError(
                f"write cli message timed out after {REMOTE_WRITE_TIMEOUT}s"
            ) from exc

    # -- 补全 ---------------------------------------------------------------
    async def complete(self, ctx: Any, req: CompletionRequest) -> list[Item]:
        """协程版补全（可直接当 :class:`atri_x.completion.completion.Source`）。

        对应 Go ``(*RemoteClient).Complete``（Go 是同步接口，见模块 docstring）。
        """
        self.seq += 1
        request_id = f"c-{self.seq}"
        queue: asyncio.Queue[list[Item]] = asyncio.Queue(maxsize=1)
        self._pending[request_id] = queue
        try:
            try:
                await self.write(
                    ctx,
                    RemoteMessage(
                        type=REMOTE_MSG_COMPLETE,
                        id=request_id,
                        text=req.text,
                        cursor=req.cursor,
                    ),
                )
            except Exception:  # noqa: BLE001 - 与 Go 一致：写失败返回空
                return []
            try:
                return await asyncio.wait_for(queue.get(), COMPLETE_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                return []
        finally:
            self.remove_pending(request_id)

    def complete_sync(self, ctx: Any, req: CompletionRequest) -> list[Item]:
        """同步重入版补全（保留 Go 的"等 2 秒"语义，需在事件循环外调用）。

        在已运行的事件循环里调用会抛 ``RuntimeError`` —— 那种场景应当 ``await``
        :meth:`complete`。
        """
        return asyncio.run(self.complete(ctx, req))

    def remove_pending(self, request_id: str) -> None:
        """对应 Go ``(*RemoteClient).removePending``。"""
        self._pending.pop(request_id, None)

    def pending_requests(self) -> list[str]:
        """尚未完成的补全请求 ID（测试接缝）。"""
        return list(self._pending)

    # -- 凭据 ---------------------------------------------------------------
    def resolve_token(self) -> str:
        """按 ``token_env`` 解析 token。对应 Go ``(*RemoteClient).resolveToken``。

        ``config_env`` 返回 ``None`` 对应 Go 的 ``ok=false``（变量不存在），继续看下一个；
        读 ``.env`` 失败会抛异常，那时才按 Go 的 ``err != nil`` 分支报错。
        """
        for env_name in self.client.token_env:
            try:
                value = config_env(env_name, self.config_dir)
            except AtriXError as exc:
                raise InvalidError(f'resolve cli token "{env_name}": {exc}') from exc
            if value is not None and value.strip():
                return value.strip()
        raise InvalidError(f'cli client "{self.profile}" token is not configured')

    # -- 诊断 ---------------------------------------------------------------
    def profile_name(self) -> str:
        """当前档案名（Go 里是未导出的 ``profile`` 字段）。"""
        return self.profile

    def client_config(self) -> ClientConfig:
        """当前档案的配置对象。"""
        return self.client


def _decode(raw: Any) -> Any:
    """把一条 WebSocket 消息解成 JSON 值。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):  # pragma: no cover - 防御性
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}
