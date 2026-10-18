"""GUI 的 WS 客户端：连 ``ws://…/cli/v1/ws``、握手、收发帧、断线重连。

服务端协议见 ``docs/frontend-api.md``。本模块是**同步**封装：内部一个后台线程
（复用 :class:`atri_x.net.websocket.SyncWebSocket` 自带的事件循环线程），回调在
该线程里触发——GUI 侧必须把它转成主线程事件（Tk 用 ``after`` 轮询队列）。

生命周期::

    client = RemoteClient(url, "local", token, on_message=..., on_state=...)
    client.start()          # 后台连接，状态经 on_state 通知
    client.send_input("你好")
    client.stop()           # 关连接 + 收线程（窗口退出时调用）

回调约定：

- ``on_message(RemoteMessage)``：每收到一帧调用一次（含 ``hello_ok`` 等控制帧）；
- ``on_state(state, detail)``：``connecting`` / ``connected`` / ``disconnected`` /
  ``auth_failed`` / ``stopped``，``detail`` 是给人看的原因（不保证非空）。

线程纪律：``send_*`` 可以从任意线程调用；``stop()`` 幂等，可在窗口销毁时调用。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Mapping

from atri_x.net.websocket import SyncWebSocket
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_COMPLETE,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_HISTORY,
    REMOTE_MSG_INPUT,
    REMOTE_MSG_SESSIONS,
    RemoteMessage,
)

__all__ = [
    "REMOTE_MSG_COMPLETE",
    "RemoteClient",
    "STATE_AUTH_FAILED",
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_DISCONNECTED",
    "STATE_STOPPED",
]

#: 连接状态（``on_state`` 的第一个参数）。
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_AUTH_FAILED = "auth_failed"
STATE_STOPPED = "stopped"

#: 握手等待上限（秒）。
HELLO_TIMEOUT = 10.0

#: 断线重连间隔（秒）。
RECONNECT_DELAY = 2.0


class RemoteClient:
    """到 ATRI-X CLI 远程服务端的同步客户端（后台线程 + 回调）。"""

    def __init__(
        self,
        url: str,
        client_id: str,
        token: str,
        *,
        on_message: Callable[[RemoteMessage], None] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        reconnect: bool = True,
        reconnect_delay: float = RECONNECT_DELAY,
    ) -> None:
        self.url = url
        self.client_id = client_id
        self.token = token
        self._on_message = on_message
        self._on_state = on_state
        self._reconnect = bool(reconnect)
        self._reconnect_delay = float(reconnect_delay)
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: SyncWebSocket | None = None
        self._send_lock = threading.Lock()
        self._state = STATE_STOPPED
        self._detail = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动后台线程（重复调用无副作用）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atri-x-gui-client", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止线程并关闭连接（幂等）。"""
        self._stop.set()
        socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._connected.clear()
        self._emit_state(STATE_STOPPED, "")

    @property
    def connected(self) -> bool:
        """是否已经握手成功（可以发消息）。"""
        return self._connected.is_set()

    @property
    def state(self) -> str:
        """最近一次连接状态。"""
        return self._state

    @property
    def detail(self) -> str:
        """最近一次状态的原因文本。"""
        return self._detail

    def set_callbacks(
        self,
        on_message: Callable[[RemoteMessage], None] | None = None,
        on_state: Callable[[str, str], None] | None = None,
    ) -> None:
        """替换回调（界面重建连接时用；线程安全）。"""
        self._on_message = on_message
        self._on_state = on_state

    # ---------------------------------------------------------------- 发送

    def send_input(self, text: str) -> bool:
        """发一条用户输入；未连接时返回 ``False``（界面提示离线）。"""
        return self._send({"type": REMOTE_MSG_INPUT, "text": text})

    def request_complete(self, request_id: str, text: str, cursor: int) -> bool:
        """请求补全候选（服务端回 ``complete_result``）。"""
        payload: dict[str, Any] = {"type": REMOTE_MSG_COMPLETE, "id": request_id, "text": text}
        if cursor >= 0:
            payload["cursor"] = int(cursor)
        return self._send(payload)

    def request_sessions(self, limit: int = 30, text: str = "") -> bool:
        """请求会话列表（服务端回 ``sessions_result``，Python 版新增帧）。"""
        payload: dict[str, Any] = {"type": REMOTE_MSG_SESSIONS}
        if limit > 0:
            payload["limit"] = int(limit)
        if text:
            payload["text"] = text
        return self._send(payload)

    def request_history(self, session_id: str, limit: int = 200) -> bool:
        """请求某会话的历史消息（服务端回 ``history_result``）。"""
        payload: dict[str, Any] = {"type": REMOTE_MSG_HISTORY, "session_id": session_id}
        if limit > 0:
            payload["limit"] = int(limit)
        return self._send(payload)

    def send_raw(self, payload: Mapping[str, Any]) -> bool:
        """发任意协议帧（测试/扩展用）。"""
        return self._send(dict(payload))

    # ---------------------------------------------------------------- 线程

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            self._emit_state(STATE_CONNECTING, "" if first else "正在重连")
            first = False
            socket: SyncWebSocket | None = None
            try:
                socket = SyncWebSocket.connect(self.url, open_timeout=HELLO_TIMEOUT)
                self._socket = socket
                self._handshake(socket)
                self._connected.set()
                self._emit_state(STATE_CONNECTED, "")
                self._pump(socket)
            except _AuthFailed as exc:
                self._connected.clear()
                self._emit_state(STATE_AUTH_FAILED, str(exc))
                _safe_close(socket)
                return
            except Exception as exc:  # noqa: BLE001 - 连接层失败统一按断线处理
                self._connected.clear()
                self._emit_state(STATE_DISCONNECTED, _reason(exc))
            finally:
                self._connected.clear()
                _safe_close(socket)
                self._socket = None
            if self._stop.is_set() or not self._reconnect:
                break
            self._stop.wait(self._reconnect_delay)
        self._emit_state(STATE_STOPPED, "")

    def _handshake(self, socket: SyncWebSocket) -> None:
        """发 ``hello`` 并等 ``hello_ok``；鉴权失败抛 :class:`_AuthFailed`。"""
        socket.send_text(
            json.dumps(
                {"type": REMOTE_MSG_HELLO, "client_id": self.client_id, "token": self.token},
                ensure_ascii=False,
            )
        )
        deadline = time.monotonic() + HELLO_TIMEOUT
        while time.monotonic() < deadline:
            raw = socket.recv(timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(raw, bytes):
                continue
            message = _decode(raw)
            if message is None:
                continue
            if message.type == REMOTE_MSG_HELLO_OK:
                return
            if message.type == REMOTE_MSG_ERROR:
                raise _AuthFailed(message.text or "服务端拒绝握手")
        raise _AuthFailed("等待 hello_ok 超时")

    def _pump(self, socket: SyncWebSocket) -> None:
        """读循环：逐帧回调，直到对端关闭或 stop()。"""
        while not self._stop.is_set():
            raw = socket.recv()
            if isinstance(raw, bytes):
                continue
            message = _decode(raw)
            if message is None:
                continue
            callback = self._on_message
            if callback is not None:
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - 回调异常不能弄死读线程
                    pass

    # ---------------------------------------------------------------- 内部

    def _send(self, payload: Mapping[str, Any]) -> bool:
        socket = self._socket
        if socket is None or not self._connected.is_set():
            return False
        text = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            try:
                socket.send_text(text)
            except Exception:  # noqa: BLE001 - 断线后发送失败由状态回调反映
                return False
        return True

    def _emit_state(self, state: str, detail: str) -> None:
        self._state = state
        self._detail = detail
        callback = self._on_state
        if callback is None:
            return
        try:
            callback(state, detail)
        except Exception:  # noqa: BLE001
            pass


class _AuthFailed(Exception):
    """握手被服务端拒绝（token/客户端 id 不对）。"""


def _decode(raw: Any) -> RemoteMessage | None:
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return RemoteMessage.from_json(payload)


def _safe_close(socket: SyncWebSocket | None) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:  # noqa: BLE001
        pass


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__
