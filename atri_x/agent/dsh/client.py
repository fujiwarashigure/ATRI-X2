"""``atri_x.agent.dsh.client``：DSH ACP 客户端（会话语义与 prompt 生命周期）。

在 :class:`~atri_x.agent.dsh.transport.JsonRpcStdio` 之上实现 ACP v1 的调用面：

- ``initialize`` / ``authenticate``：握手与（可选的）认证；
- ``session/new`` / ``session/resume`` / ``session/list`` / ``session/close``：
  持久会话的建、续、列、关；
- ``session/set_config_option``：按会话选 ``model`` / ``reasoning_effort``；
- ``session/prompt``：一轮提问，期间 ``session/update`` 通知聚合成事件流；
- ``session/cancel``：取消在途轮次（ACP 没有别的取消路径）。

权限请求（``session/request_permission``）默认**拒绝**，由 ``on_permission`` 覆盖：
自动放行等于绕过 ATRI-X 自己的风险确认体系，因此安全默认必须是拒绝。
"""

from __future__ import annotations

import shlex
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from atri_x.agent.dsh.transport import JsonRpcStdio
from atri_x.agent.dsh.types import (
    ACP_PROTOCOL_VERSION,
    DEFAULT_COMMAND,
    TEXT_LIMIT,
    AcpError,
    AcpEvent,
    AcpPromptResult,
    AcpProtocolError,
    AcpServerInfo,
    AcpSession,
    AcpUsage,
    as_int,
    as_text,
    client_version,
    event_from_update,
    parse_config_options,
    reject_permission,
)

__all__ = ["AcpClient"]


class _TurnBuffer:
    """单轮内收集流式片段、用量与每轮回调；读线程写、调用线程读。"""

    __slots__ = ("context_size", "events", "lock", "on_event", "texts", "thoughts", "used_tokens")

    def __init__(self, on_event: Callable[[AcpEvent], None] | None = None) -> None:
        self.lock = threading.Lock()
        self.events: list[AcpEvent] = []
        self.texts: list[str] = []
        self.thoughts: list[str] = []
        self.used_tokens = 0
        self.context_size = 0
        self.on_event = on_event

    def add(self, event: AcpEvent) -> None:
        """累积一条事件；文本按块拼接，``usage_update`` 只记最新值。"""
        with self.lock:
            self.events.append(event)
            if event.kind == "agent_message_chunk" and event.text:
                self.texts.append(event.text)
            elif event.kind == "agent_thought_chunk" and event.text:
                self.thoughts.append(event.text)
            if event.kind == "usage_update":
                self.used_tokens = event.used_tokens or self.used_tokens
                self.context_size = event.context_size or self.context_size
            callback = self.on_event
        if callback is not None:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 回调异常不能弄死读线程
                pass

    def snapshot(self) -> tuple[tuple[AcpEvent, ...], str, str, int, int]:
        """返回 (事件, 正文, 思考, 已用 token, 上下文窗口)。"""
        with self.lock:
            text = "".join(self.texts)[:TEXT_LIMIT]
            thought = "".join(self.thoughts)[:TEXT_LIMIT]
            return tuple(self.events), text, thought, self.used_tokens, self.context_size


class AcpClient:
    """``dsh --profile acp`` 子进程的同步客户端（线程安全，一次连接多会话）。

    典型用法::

        with AcpClient(["dsh", "--profile", "acp"]) as client:
            session = client.new_session("/data/dsh-ws")
            client.set_config_option(session.session_id, "model", "deepseek-v4-flash")
            result = client.prompt(session.session_id, "用一句话解释 ACP")
            print(result.text, result.usage.to_dict())
    """

    def __init__(
        self,
        command: Sequence[str] | str = DEFAULT_COMMAND,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 30.0,
        prompt_timeout: float = 600.0,
        on_event: Callable[[AcpEvent], None] | None = None,
        on_permission: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        client_name: str = "atri-x",
        isolate_env: bool = False,
    ) -> None:
        parts = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
        self._transport = JsonRpcStdio(
            parts,
            cwd=cwd,
            env=env,
            request_timeout=request_timeout,
            on_notification=self._handle_notification,
            on_request=self._handle_request,
            isolate_env=isolate_env,
        )
        self._prompt_timeout = float(prompt_timeout)
        self._on_event = on_event
        self._on_permission = on_permission
        self._client_name = client_name
        self._lock = threading.RLock()
        self._turns: dict[str, _TurnBuffer] = {}
        self._sessions: dict[str, AcpSession] = {}
        self._server_info: AcpServerInfo | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> AcpServerInfo:
        """拉起子进程并完成 ``initialize``；失败时回收进程。"""
        if self._server_info is not None and self._transport.alive:
            return self._server_info
        self._transport.start()
        try:
            return self.initialize()
        except AcpError:
            self._transport.stop()
            raise

    def stop(self) -> None:
        """关闭所有会话、终止子进程（幂等）。"""
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            try:
                self.close_session(session_id)
            except AcpError:
                continue
        self._transport.stop()

    def __enter__(self) -> "AcpClient":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    @property
    def alive(self) -> bool:
        """子进程是否仍在运行。"""
        return self._transport.alive

    @property
    def exit_code(self) -> int | None:
        """子进程退出码；还在跑或从未启动给 ``None``。"""
        return self._transport.exit_code

    @property
    def server_info(self) -> AcpServerInfo | None:
        """握手结果；未启动给 ``None``。"""
        return self._server_info

    def stderr_tail(self) -> str:
        """DSH 诊断日志尾部（报错时带出去）。"""
        return self._transport.stderr_tail()

    # ------------------------------------------------------------------ 握手

    def initialize(self) -> AcpServerInfo:
        """握手；协议版本高于本客户端时直接判不兼容。"""
        result = self._transport.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": self._client_name, "version": client_version()},
            },
        )
        mapping = result if isinstance(result, Mapping) else {}
        agent = mapping.get("agentInfo")
        agent_map = agent if isinstance(agent, Mapping) else {}
        auth_raw = mapping.get("authMethods")
        auth_ids: list[str] = []
        if isinstance(auth_raw, list):
            for item in auth_raw:
                if isinstance(item, Mapping):
                    method_id = as_text(item.get("id"))
                    if method_id:
                        auth_ids.append(method_id)
        capabilities = mapping.get("agentCapabilities")
        info = AcpServerInfo(
            protocol_version=as_int(mapping.get("protocolVersion")),
            agent_name=as_text(agent_map.get("name")),
            agent_version=as_text(agent_map.get("version")),
            auth_methods=tuple(auth_ids),
            capabilities=capabilities if isinstance(capabilities, Mapping) else {},
        )
        if info.protocol_version > ACP_PROTOCOL_VERSION:
            raise AcpProtocolError(
                f"agent speaks ACP {info.protocol_version}, this client supports {ACP_PROTOCOL_VERSION}"
            )
        self._server_info = info
        self._authenticate_if_needed(info)
        return info

    def _authenticate_if_needed(self, info: AcpServerInfo) -> None:
        """服务端公布认证方式时挑第一个调用；DSH 通常不需要认证。"""
        if not info.auth_methods:
            return
        self._transport.request("authenticate", {"methodId": info.auth_methods[0]})

    # ------------------------------------------------------------------ 会话

    def new_session(self, cwd: str, mcp_servers: Sequence[Mapping[str, Any]] | None = None) -> AcpSession:
        """建持久会话；``cwd`` 必须是绝对路径（DSH 会校验）。"""
        if not cwd:
            raise AcpError("session cwd is required")
        result = self._transport.request("session/new", {"cwd": cwd, "mcpServers": list(mcp_servers or [])})
        mapping = result if isinstance(result, Mapping) else {}
        session = AcpSession(
            session_id=as_text(mapping.get("sessionId")),
            cwd=cwd,
            config_options=parse_config_options(mapping),
        )
        if not session.session_id:
            raise AcpProtocolError("session/new returned no sessionId")
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def resume_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: Sequence[Mapping[str, Any]] | None = None,
    ) -> AcpSession:
        """恢复已持久化且非活跃的会话（不回放历史，上下文交接由调用方负责）。"""
        result = self._transport.request(
            "session/resume",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers or [])},
        )
        mapping = result if isinstance(result, Mapping) else {}
        session = AcpSession(
            session_id=as_text(mapping.get("sessionId")) or session_id,
            cwd=cwd,
            config_options=parse_config_options(mapping),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def list_sessions(self, cwd: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        """分页列出可恢复的根会话；``cwd`` 可作筛选条件。"""
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        result = self._transport.request("session/list", params)
        return dict(result) if isinstance(result, Mapping) else {}

    def set_config_option(self, session_id: str, config_id: str, value: str) -> tuple[Mapping[str, Any], ...]:
        """串行更新会话的 ``model`` / ``reasoning_effort``，返回最新配置项。"""
        result = self._transport.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
        )
        mapping = result if isinstance(result, Mapping) else {}
        options = parse_config_options(mapping)
        if options:
            with self._lock:
                current = self._sessions.get(session_id)
                if current is not None:
                    self._sessions[session_id] = AcpSession(
                        session_id=current.session_id, cwd=current.cwd, config_options=options
                    )
        return options

    def close_session(self, session_id: str) -> None:
        """停稳式关闭：取消、drain 更新、flush 持久化并释放该会话。"""
        try:
            self._transport.request("session/close", {"sessionId": session_id})
        finally:
            with self._lock:
                self._sessions.pop(session_id, None)
                self._turns.pop(session_id, None)

    # ------------------------------------------------------------------ prompt

    def prompt(
        self,
        session_id: str,
        text: str | None = None,
        *,
        blocks: Sequence[Mapping[str, Any]] | None = None,
        timeout: float | None = None,
        on_event: Callable[[AcpEvent], None] | None = None,
    ) -> AcpPromptResult:
        """提交一轮 prompt 并等它停稳；期间的通知进回调与结果里的 ``events``。

        同一会话同一时刻只允许一轮（ACP 的约束），重复调用直接报错。
        """
        buffer = _TurnBuffer(on_event)
        with self._lock:
            if session_id in self._turns:
                raise AcpError(f"session {session_id} already has a prompt in flight")
            self._turns[session_id] = buffer
        payload = (
            list(blocks)
            if blocks is not None
            else [{"type": "text", "text": "" if text is None else str(text)}]
        )
        started = time.monotonic()
        try:
            result = self._transport.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": payload},
                self._prompt_timeout if timeout is None else float(timeout),
            )
        except Exception:
            try:
                self.cancel(session_id)
            except AcpError:
                pass
            raise
        finally:
            with self._lock:
                self._turns.pop(session_id, None)
        mapping = result if isinstance(result, Mapping) else {}
        events, body, thought, used, size = buffer.snapshot()
        raw_usage = mapping.get("usage")
        return AcpPromptResult(
            session_id=session_id,
            stop_reason=as_text(mapping.get("stopReason")),
            usage=AcpUsage.from_mapping(raw_usage),
            text=body,
            thought=thought,
            used_tokens=used,
            context_size=size,
            events=events,
            elapsed_seconds=time.monotonic() - started,
            # 只有响应里真的带 usage（对象）才算"内核回报了用量"；缺字段时不猜 0。
            usage_reported=isinstance(raw_usage, Mapping),
        )

    def cancel(self, session_id: str) -> None:
        """发取消通知；不等待响应（``session/prompt`` 会以 ``cancelled`` 停稳）。"""
        self._transport.notify("session/cancel", {"sessionId": session_id})

    def has_prompt_in_flight(self, session_id: str) -> bool:
        """该会话是否有一轮 prompt 正在等待停稳。"""
        with self._lock:
            return session_id in self._turns

    # ------------------------------------------------------------------ 服务端事件

    def _handle_notification(self, method: str, params: Mapping[str, Any]) -> None:
        """处理服务端通知；目前只认 ``session/update``（读线程调用）。"""
        if method != "session/update":
            return
        session_id = as_text(params.get("sessionId"))
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        event = event_from_update(session_id, update)
        with self._lock:
            buffer = self._turns.get(session_id)
        if buffer is not None:
            buffer.add(event)
        callback = self._on_event
        if callback is not None:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 回调异常不能弄死读线程
                pass

    def _handle_request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """应答服务端请求；权限默认拒绝，其余交回 transport 报 method not found。"""
        if method != "session/request_permission":
            return None
        if self._on_permission is not None:
            try:
                outcome = self._on_permission(params)
            except Exception:  # noqa: BLE001 - 回调失败按默认拒绝处理
                outcome = None
            if isinstance(outcome, Mapping):
                return dict(outcome)
        return reject_permission(params)

    # ------------------------------------------------------------------ 调试

    def describe(self) -> dict[str, Any]:
        """当前连接与已知会话的快照（日志与冒烟输出用）。"""
        info = self._server_info
        with self._lock:
            sessions = [
                {
                    "sessionId": session.session_id,
                    "cwd": session.cwd,
                    "options": [dict(item) for item in session.config_options],
                }
                for session in self._sessions.values()
            ]
        return {
            "alive": self.alive,
            "exitCode": self.exit_code,
            "protocolVersion": None if info is None else info.protocol_version,
            "agent": None if info is None else f"{info.agent_name}/{info.agent_version}",
            "sessions": sessions,
        }
