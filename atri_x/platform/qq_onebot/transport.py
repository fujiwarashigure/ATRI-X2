"""OneBot v11 正向 WebSocket 传输：连接、事件读取、动作调用。

对应 Go：``internal/platform/qq-onebot/transport.go``。

形式差异：

* ``github.com/coder/websocket`` → :mod:`atri_x.net.websocket`
  （``connect`` / ``WebSocketConnection``；关闭码常量同名）。
* Go 的 ``Close`` 是同步方法（配合 ``defer``）→ 这里 :meth:`Transport.close`
  是协程，调用方必须 ``await``。
* 写入门闸：Go 用容量 1 的 channel + ``context.WithTimeout`` 等待；Python 没有
  可取消的 ``acquire``，这里用「非阻塞先试 → 25 ms 轮询 ctx / 截止时间」实现同一
  语义（等待期间 ``ctx`` 被取消立刻抛该取消原因）。
* ``conn.SetReadLimit`` → 直接改 :class:`atri_x.net.websocket` 连接与帧解码器的
  上限（该库在建连时固定 ``max_size``，无 setter）；用于 ``get_forward_msg``
  的单次响应体积上限。
* 写失败时 Go 会 ``CloseNow``；Python 侧不做立即中止（该库无对应 API），
  失败会由读取侧观察并触发重连。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.delivery.delivery import ctx_error
from atri_x.errors import AtriXError, ForbiddenError, InvalidError
from atri_x.net.websocket import CLOSE_NORMAL, WebSocketConnection, connect as ws_connect
from atri_x.platform.qq_onebot.backend import (
    BackendProfile,
    backend_profile,
    resolve_protocol_backend,
)
from atri_x.platform.qq_onebot.types import Event, Sender, json_int64, json_str

DEFAULT_TIMEOUT = 15.0

#: 等待写入门闸时的轮询间隔（秒）。
_WRITE_GATE_POLL = 0.025

#: ``get_forward_msg`` 读取上限的信封余量（Go 的 ``64*1024``）。
ENVELOPE_ALLOWANCE = 64 * 1024


@dataclass
class Request:
    """OneBot 动作请求。对应 Go ``request``。"""

    action: str = ""
    params: dict[str, Any] | None = None
    echo: str = ""

    def to_json(self) -> dict[str, Any]:
        """对应 Go ``request`` 的 json tag（``params`` 带 omitempty）。"""
        out: dict[str, Any] = {"action": self.action}
        if self.params:
            out["params"] = self.params
        out["echo"] = self.echo
        return out


@dataclass
class Response:
    """OneBot 动作响应。对应 Go ``response``。"""

    status: str = ""
    retcode: int = 0
    data: Any = None
    echo: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> "Response":
        """对应 Go ``json.Unmarshal(raw, &response)``；类型不符抛 ``InvalidError``。"""
        if not isinstance(raw, Mapping):
            raise InvalidError("decode onebot response: top-level value is not a JSON object")
        return cls(
            status=json_str(raw.get("status")),
            retcode=json_int64(raw.get("retcode", 0), "retcode"),
            data=raw.get("data"),
            echo=json_str(raw.get("echo")),
        )


@dataclass
class SendMessageData:
    """``send_*_msg`` 的响应体。对应 Go ``sendMessageData``。"""

    message_id: int = 0


@dataclass
class GetMessageData:
    """``get_msg`` 的响应体。对应 Go ``getMessageData``。"""

    time: int = 0
    message_type: str = ""
    message_id: int = 0
    user_id: int = 0
    group_id: int = 0
    message: Any = None
    raw_message: str = ""
    sender: Sender = field(default_factory=Sender)


@dataclass
class GetImageData:
    """``get_image`` 的响应体。对应 Go ``getImageData``。"""

    file: str = ""
    url: str = ""


@dataclass
class GetFileData:
    """``get_file`` 的响应体。对应 Go ``getFileData``。"""

    file: str = ""
    url: str = ""
    file_size: str = ""
    file_name: str = ""


@dataclass
class LoginInfo:
    """``get_login_info`` 的响应体（"协议端已登录的账号"）。

    注意：这是"**QQ 已登录**"，与"WebSocket 连上了"不是一回事——协议端进程在跑、
    端口在听，但 QQ 没登录时这个动作会失败或返回空。
    """

    user_id: int = 0
    nickname: str = ""

    def logged_in(self) -> bool:
        return self.user_id > 0


@dataclass
class VersionInfo:
    """``get_version_info`` 的响应体；``app_name`` 用来识别实际协议端（NapCat / SnowLuma）。"""

    app_name: str = ""
    app_version: str = ""
    protocol_version: str = ""


def write_timeout_for(timeout: float, frame_bytes: int) -> float:
    """按帧体积放宽写超时（自由函数形态）。对应 Go ``(*Transport).writeTimeout``。"""
    base = timeout if timeout > 0 else DEFAULT_TIMEOUT
    extra = float(frame_bytes // (1024 * 1024))
    if extra > base:
        extra = base
    return base + extra


def _mapping(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidError(f"decode {what}: top-level value is not a JSON object")
    return raw


class Transport:
    """一条 OneBot WebSocket 连接与它的动作表。对应 Go ``Transport``。"""

    def __init__(
        self,
        url: str = "",
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        read_limit_bytes: int = 0,
        logger: Any = None,
        backend: str = "",
    ) -> None:
        self.url = url
        self.access_token = access_token
        self.timeout = timeout
        #: 单次 ``get_forward_msg`` 响应的体积上限（0 表示不额外限制）。
        self.read_limit_bytes = read_limit_bytes
        self.logger = logger
        #: 协议端类型（差异只在 :attr:`profile`，**不参与** url/令牌解析）。
        self.backend = resolve_protocol_backend(backend)
        self.profile: BackendProfile = backend_profile(self.backend)
        self._conn: WebSocketConnection | None = None
        self._pending: dict[str, asyncio.Queue] = {}
        self._seq = 0
        self._write_gate = asyncio.Lock()
        self._read_limited = False
        self._saved_max_size = 0

    # -- 连接 ---------------------------------------------------------------
    async def connect(self, ctx: Any) -> None:
        """建立 WebSocket。对应 Go ``Transport.Connect``。"""
        if not self.url:
            raise InvalidError("onebot ws url is empty")
        headers = {}
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        open_timeout = self.timeout if self.timeout > 0 else DEFAULT_TIMEOUT
        try:
            conn = await ws_connect(self.url, headers=headers or None, open_timeout=open_timeout)
        except Exception as exc:  # noqa: BLE001 - 连接失败按 Go 返回 error
            # 401/403 是"令牌不对"，不是"服务没起"：上层必须能区分，否则用户看到的
            # 只是"连不上"然后无限退避。SnowLuma 在 WS 升级阶段就用 401 拒绝错令牌。
            status = int(getattr(exc, "status", 0) or 0)
            if status in (401, 403):
                raise ForbiddenError(
                    "onebot websocket rejected the access token "
                    f"(HTTP {status}); check [platform.qqonebot] access_token / access_token_env"
                ) from exc
            raise AtriXError(f"connect onebot websocket: {exc}") from exc
        self._conn = conn
        self._pending = {}

    async def close(self, status: int = CLOSE_NORMAL, reason: str = "") -> None:
        """关闭连接并唤醒所有等待中的调用。对应 Go ``Transport.Close``。"""
        conn, self._conn = self._conn, None
        pending, self._pending = self._pending, {}
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close(status, reason)
        for queue in pending.values():
            # Go 关闭 channel：等待方读到零值并报 "onebot websocket disconnected"。
            queue.put_nowait(None)

    def current_conn(self) -> WebSocketConnection | None:
        """对应 Go ``currentConn``。"""
        return self._conn

    @property
    def closed(self) -> bool:
        return self._conn is None

    # -- 读取 ---------------------------------------------------------------
    async def read(self, ctx: Any) -> Event | None:
        """读一帧。对应 Go ``Transport.Read``。

        返回 ``None`` 表示这一帧是动作响应（已分发给等待方），调用方应继续读；
        与 Go 返回零值 ``Event`` 后被 ``PostType == ""`` 跳过的行为等价。

        ``ctx`` 非空时，等待帧的同时每 200 ms 检查一次取消（Go 的
        ``wsjson.Read(ctx, conn, &raw)`` 会在 ctx 取消时立即返回；
        :mod:`atri_x.net.websocket` 的 ``recv`` 不接受 ctx，故在此轮询）。
        """
        conn = self._conn
        if conn is None:
            raise AtriXError("onebot websocket is not connected")
        raw = await self._recv(conn, ctx)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise InvalidError(f"decode onebot event: {exc}") from exc
        if self.dispatch_response(parsed):
            return None
        return Event.from_json(parsed)

    async def _recv(self, conn: WebSocketConnection, ctx: Any) -> Any:
        """可被 ctx 取消的收帧。"""
        if ctx is None:
            return await conn.recv()
        pending = asyncio.ensure_future(conn.recv())
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

    async def read_responses(self, ctx: Any) -> None:
        """只消费动作响应、丢弃事件的读循环。对应 Go ``readResponses``。"""
        while True:
            try:
                await self.read(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 与 Go 一致：读到错误就退出
                return

    def dispatch_response(self, raw: Any) -> bool:
        """把响应帧交给等待方。对应 Go ``dispatchResponse``。

        返回 True 表示"这帧是响应"，不再当作事件解析。
        """
        if not isinstance(raw, Mapping):
            return False
        echo = raw.get("echo")
        if not isinstance(echo, str) or echo == "":
            return False
        try:
            resp = Response.from_json(raw)
        except InvalidError:
            return False
        queue = self._pending.get(resp.echo)
        if queue is None:
            return True
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(resp)
        return True

    # -- 动作 ---------------------------------------------------------------
    async def send_private_message(self, ctx: Any, user_id: int, text: str) -> str:
        """对应 Go ``SendPrivateMessage``。"""
        return await self.send_message(
            ctx, "send_private_msg", {"user_id": user_id, "message": text, "auto_escape": True}
        )

    async def send_group_message(self, ctx: Any, group_id: int, text: str) -> str:
        """对应 Go ``SendGroupMessage``。"""
        return await self.send_message(
            ctx, "send_group_msg", {"group_id": group_id, "message": text, "auto_escape": True}
        )

    async def send_private_segments(self, ctx: Any, user_id: int, segments: list[Any]) -> str:
        """对应 Go ``SendPrivateSegments``。"""
        return await self.send_message(
            ctx, "send_private_msg", {"user_id": user_id, "message": segments}
        )

    async def send_group_segments(self, ctx: Any, group_id: int, segments: list[Any]) -> str:
        """对应 Go ``SendGroupSegments``。"""
        return await self.send_message(
            ctx, "send_group_msg", {"group_id": group_id, "message": segments}
        )

    async def send_private_forward_message(
        self, ctx: Any, user_id: int, nodes: list[Any]
    ) -> str:
        """对应 Go ``SendPrivateForwardMessage``。"""
        return await self.send_forward_message(
            ctx, "send_private_forward_msg", {"user_id": user_id, "messages": nodes}
        )

    async def send_group_forward_message(self, ctx: Any, group_id: int, nodes: list[Any]) -> str:
        """对应 Go ``SendGroupForwardMessage``。"""
        return await self.send_forward_message(
            ctx, "send_group_forward_msg", {"group_id": group_id, "messages": nodes}
        )

    async def send_forward_message(self, ctx: Any, action: str, params: dict[str, Any]) -> str:
        """合并转发发送：响应缺 ``message_id`` 视为失败。对应 Go ``sendForwardMessage``。"""
        message_id = await self.send_message(ctx, action, params)
        if message_id == "":
            raise AtriXError(f"{action} response has no message_id")
        return message_id

    async def get_message(self, ctx: Any, message_id: str) -> GetMessageData:
        """``get_msg``。对应 Go ``GetMessage``。"""
        from atri_x.platform.qq_onebot.types import parse_int64

        value = parse_int64(message_id)
        if value is None:
            raise InvalidError(f"parse message id: invalid syntax {message_id!r}")
        resp = await self.call(ctx, "get_msg", {"message_id": value})
        raw = _mapping(resp.data, "get_msg response")
        return GetMessageData(
            time=json_int64(raw.get("time", 0), "time"),
            message_type=json_str(raw.get("message_type")),
            message_id=json_int64(raw.get("message_id", 0), "message_id"),
            user_id=json_int64(raw.get("user_id", 0), "user_id"),
            group_id=json_int64(raw.get("group_id", 0), "group_id"),
            message=raw.get("message"),
            raw_message=json_str(raw.get("raw_message")),
            sender=Sender.from_json(raw.get("sender")),
        )

    async def get_forward_msg(self, ctx: Any, message_id: str) -> Any:
        """``get_forward_msg``（带单次响应体积上限）。对应 Go ``GetForwardMsg``。

        参数名按协议端差异走：SnowLuma 的合并转发 id 是 res_id 字符串，主参数键是
        ``id``；NapCat 用 ``message_id``。主键失败时用另一套键再试一次——NapCat 的
        调用序列与改动前完全一致，SnowLuma 则覆盖两种 id 形态。
        """
        message_id = (message_id or "").strip()
        if message_id == "":
            raise InvalidError("forward message id is empty")
        limit = 0
        if self.read_limit_bytes > 0:
            # 给 JSON 键/echo 留一点信封余量，正文本身仍受预算约束。
            limit = self.read_limit_bytes + ENVELOPE_ALLOWANCE
        primary = self.profile.forward_id_param or "message_id"
        try:
            resp = await self.call_with_limit(
                ctx, "get_forward_msg", {primary: message_id}, limit
            )
        except AtriXError:
            secondary = "id" if primary != "id" else "message_id"
            resp = await self.call_with_limit(
                ctx, "get_forward_msg", {secondary: message_id}, limit
            )
        return resp.data

    async def get_image(self, ctx: Any, file: str) -> GetImageData:
        """``get_image``。对应 Go ``GetImage``。"""
        resp = await self.call(ctx, "get_image", {"file": file})
        raw = _mapping(resp.data, "get_image response")
        return GetImageData(file=json_str(raw.get("file")), url=json_str(raw.get("url")))

    async def get_file(self, ctx: Any, file: str) -> GetFileData:
        """``get_file``（``download=true``）。对应 Go ``GetFile``。"""
        resp = await self.call(ctx, "get_file", {"file": file, "download": True})
        raw = _mapping(resp.data, "get_file response")
        return GetFileData(
            file=json_str(raw.get("file")),
            url=json_str(raw.get("url")),
            file_size=json_str(raw.get("file_size")),
            file_name=json_str(raw.get("file_name")),
        )

    async def get_login_info(self, ctx: Any = None) -> LoginInfo:
        """``get_login_info``：**QQ 已登录**的账号（不是"端口在听"）。

        协议端进程在跑、WS 也连上，但 QQ 没登录时这个动作会失败；上层据此区分
        "协议端就绪"与"QQ 已登录"两个状态。
        """
        resp = await self.call(ctx, "get_login_info", {})
        raw = _mapping(resp.data, "get_login_info response")
        return LoginInfo(
            user_id=json_int64(raw.get("user_id", 0), "user_id"),
            nickname=json_str(raw.get("nickname")),
        )

    async def get_version_info(self, ctx: Any = None) -> VersionInfo:
        """``get_version_info``：协议端自报家门（``app_name`` 用来识别真身）。"""
        resp = await self.call(ctx, "get_version_info", {})
        raw = _mapping(resp.data, "get_version_info response")
        return VersionInfo(
            app_name=json_str(raw.get("app_name")),
            app_version=json_str(raw.get("app_version")),
            protocol_version=json_str(raw.get("protocol_version")),
        )

    async def can_send_image(self, ctx: Any = None) -> bool:
        """``can_send_image``（差异探测用，运行期不阻断发送）。"""
        resp = await self.call(ctx, "can_send_image", {})
        return bool(_mapping(resp.data, "can_send_image response").get("yes"))

    async def can_send_record(self, ctx: Any = None) -> bool:
        """``can_send_record``（差异探测用，运行期不阻断发送）。"""
        resp = await self.call(ctx, "can_send_record", {})
        return bool(_mapping(resp.data, "can_send_record response").get("yes"))

    async def get_group_file_url(
        self, ctx: Any, group_id: int, file_id: str, file_hash: str = ""
    ) -> str:
        """``get_group_file_url``：群文件下载直链。

        SnowLuma 的 ``get_file`` **只覆盖图片/语音缓存**，群文件必须走这个动作；
        NapCat 也实现了它，所以两端共用。``busid`` 两端都容忍缺省。
        """
        params: dict[str, Any] = {"group_id": group_id, "file_id": file_id}
        if file_hash:
            params["file_hash"] = file_hash
        resp = await self.call(ctx, "get_group_file_url", params)
        return json_str(_mapping(resp.data, "get_group_file_url response").get("url"))

    async def get_private_file_url(
        self, ctx: Any, user_id: int, file_id: str, file_hash: str = ""
    ) -> str:
        """``get_private_file_url``：私聊文件下载直链。

        SnowLuma 的 ``file_id`` 是裸 fileUUID，**依赖入站 ``file`` 段的
        ``file_hash``** 才能取到；NapCat 能从复合 file_id 自己还原，两者都把
        ``file_hash`` 当可选参数，所以这里始终按"有就带上"处理。
        """
        params: dict[str, Any] = {"file_id": file_id}
        if user_id > 0:
            params["user_id"] = user_id
        if file_hash:
            params["file_hash"] = file_hash
        resp = await self.call(ctx, "get_private_file_url", params)
        return json_str(_mapping(resp.data, "get_private_file_url response").get("url"))

    async def upload_group_file(
        self, ctx: Any, group_id: int, file: str, name: str = "", folder: str = ""
    ) -> str:
        """``upload_group_file`` → **file_id**（不是 message_id）。

        与"发消息"是两回事：上传成功不表示发出去过。
        """
        params: dict[str, Any] = {"group_id": group_id, "file": file, "name": name or ""}
        if folder:
            params["folder"] = folder
        resp = await self.call(ctx, "upload_group_file", params)
        return json_str(_mapping(resp.data, "upload_group_file response").get("file_id"))

    async def upload_private_file(
        self, ctx: Any, user_id: int, file: str, name: str = ""
    ) -> str:
        """``upload_private_file`` → **file_id**（不是 message_id）。"""
        resp = await self.call(
            ctx,
            "upload_private_file",
            {"user_id": user_id, "file": file, "name": name or ""},
        )
        return json_str(_mapping(resp.data, "upload_private_file response").get("file_id"))

    async def get_chat_file_url(
        self,
        ctx: Any,
        file_id: str,
        *,
        group_id: int = 0,
        user_id: int = 0,
        file_hash: str = "",
    ) -> str:
        """群/私聊文件下载直链：按后端差异选动作，取不到返回空串。

        SnowLuma 的 ``get_file`` **只覆盖图片/语音缓存**，群/私聊文件必须走
        ``get_group_file_url`` / ``get_private_file_url``；NapCat 两者都实现了，
        但同样优先用专有动作（直链语义更明确）。两端都没有对应动作时返回空串，
        让调用方回落到 ``get_file``。
        """
        if group_id > 0 and self.profile.group_file_url_action:
            return await self.get_group_file_url(ctx, group_id, file_id, file_hash)
        if user_id > 0 and self.profile.private_file_url_action:
            return await self.get_private_file_url(ctx, user_id, file_id, file_hash)
        return ""

    async def get_group_member_info(self, ctx: Any, group_id: int, user_id: int) -> Sender:
        """``get_group_member_info``。对应 Go ``GetGroupMemberInfo``。"""
        resp = await self.call(
            ctx, "get_group_member_info", {"group_id": group_id, "user_id": user_id}
        )
        return Sender.from_json(resp.data)

    async def call(self, ctx: Any, action: str, params: dict[str, Any] | None) -> Response:
        """对应 Go ``Transport.Call``。"""
        return await self.call_with_limit(ctx, action, params, 0)

    async def call_with_limit(
        self,
        ctx: Any,
        action: str,
        params: dict[str, Any] | None,
        max_response_bytes: int = 0,
    ) -> Response:
        """发一个动作并等它的响应。对应 Go ``callWithLimit``。"""
        started_at = time.monotonic()
        frame = ""
        registered = False
        limited = False
        try:
            err = ctx_error(ctx)
            if err is not None:
                raise err
            conn = self._conn
            if conn is None:
                raise AtriXError("onebot websocket is not connected")
            self._seq += 1
            echo = f"atri-x-{self._seq}"
            frame = json.dumps(
                Request(action=action, params=params, echo=echo).to_json(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            queue: asyncio.Queue = asyncio.Queue()
            self._pending[echo] = queue
            registered = True
            if max_response_bytes > 0:
                self.set_read_limit(max_response_bytes)
                limited = True
            await self.acquire_write(ctx, self.write_timeout(len(frame)), action)
            try:
                await conn.send_text(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 写失败按 Go 返回 error
                raise AtriXError(f"send onebot action {action}: {exc}") from exc
            finally:
                self.release_write()
            timeout = self.timeout if self.timeout > 0 else None
            try:
                resp = await asyncio.wait_for(queue.get(), timeout)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise TimeoutError(
                    f"onebot action {action} timed out waiting for response"
                ) from exc
            if resp is None:
                raise AtriXError("onebot websocket disconnected")
            if resp.status != "ok" or resp.retcode != 0:
                raise AtriXError(
                    f"onebot action {action} failed: status={resp.status} retcode={resp.retcode}"
                )
            return resp
        finally:
            if registered:
                self._pending.pop(echo, None)
            if limited:
                self.clear_read_limit()
            self.log_call(ctx, action, len(frame), started_at)

    # -- 写入闸与读取上限 ---------------------------------------------------
    def write_timeout(self, frame_bytes: int) -> float:
        """按帧体积放宽写超时。对应 Go ``writeTimeout``。"""
        return write_timeout_for(self.timeout, frame_bytes)

    async def acquire_write(self, ctx: Any, timeout: float, action: str = "") -> None:
        """取得写入门闸。对应 Go ``acquireWrite``（含 ctx 取消与超时）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            err = ctx_error(ctx)
            if err is not None:
                raise err
            if not self._write_gate.locked():
                await self._write_gate.acquire()
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AtriXError(f"wait to send onebot action {action}: timeout")
            await asyncio.sleep(min(_WRITE_GATE_POLL, remaining))

    def release_write(self) -> None:
        """释放写入门闸。对应 Go ``acquireWrite`` 返回的 release。"""
        if self._write_gate.locked():
            self._write_gate.release()

    def set_read_limit(self, limit: int) -> None:
        """临时收紧读取上限。对应 Go ``setReadLimit``。"""
        if limit <= 0:
            return
        conn = self._conn
        if conn is None:
            return
        if not self._read_limited:
            self._saved_max_size = int(getattr(conn, "max_size", 0))
            self._read_limited = True
        conn.max_size = limit
        decoder = getattr(conn, "_decoder", None)
        if decoder is not None:
            decoder.max_frame_size = limit

    def clear_read_limit(self) -> None:
        """恢复读取上限。对应 Go ``clearReadLimit``。"""
        if not self._read_limited:
            return
        self._read_limited = False
        conn = self._conn
        if conn is None:
            return
        conn.max_size = self._saved_max_size
        decoder = getattr(conn, "_decoder", None)
        if decoder is not None:
            decoder.max_frame_size = self._saved_max_size

    # -- 内部 ---------------------------------------------------------------
    async def send_message(self, ctx: Any, action: str, params: dict[str, Any]) -> str:
        """发消息并取回 ``message_id``（0 视为没有）。对应 Go ``sendMessage``。"""
        resp = await self.call(ctx, action, params)
        raw = _mapping(resp.data, "send response")
        message_id = json_int64(raw.get("message_id", 0), "message_id")
        if message_id == 0:
            return ""
        return str(message_id)

    def log_call(self, ctx: Any, action: str, frame_bytes: int, started_at: float) -> None:
        """慢调用/失败调用的 WARN/DEBUG 日志。对应 Go ``logCall``。

        只记 ``action`` / 体积 / 耗时 / 后端名，**不记 ``params``**：那里会出现
        base64 媒体与令牌，落日志等于泄露。
        """
        if self.logger is None:
            return
        elapsed = time.monotonic() - started_at
        try:
            self.logger.debug(
                "onebot action",
                action=action,
                backend=self.backend,
                frame_bytes=frame_bytes,
                elapsed_ms=round(elapsed * 1000),
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响调用
            pass


__all__ = [name for name in dir() if not name.startswith("_")]
