"""DSH 运行时的进程与并发管理：长驻 AcpClient + 每次路由一个一次性会话。

为什么长驻：``dsh --profile acp`` 是 node 进程，冷启动要 1~2 秒；路由的收益是
"复杂任务一次做对"，不是每轮都付启动成本。因此进程按需拉起、成功后复用；子进程
死了下次调用自动重启。

为什么每次路由新开会话：见 :mod:`atri_x.agent.dsh.handoff`（无状态交接）。会话
在 ``session/close`` 里停稳后释放，不留跨轮状态。

线程模型：``AcpClient.prompt`` 是**同步阻塞**的（见 :mod:`atri_x.agent.dsh.client`），
而对话链路是 asyncio。这里的桥接方式是：

1. 把「建会话 → prompt → 关会话」整段丢进线程池执行；
2. ACP 的 ``session/update`` 通知在读线程里触发回调，用
   ``loop.call_soon_threadsafe`` 塞进 :class:`asyncio.Queue`；
3. 事件循环侧边收事件边把文本写进界面的 ``MessageStream``，等线程返回后再取结果。

同一进程内用一把 ``threading.Lock`` 串行化 prompt（ACP 单会话不允许并发轮次，
DSH 也未必扛得住多会话并发；串行是最保守也最可预测的选择）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from atri_x.agent.dsh.client import AcpClient
from atri_x.agent.dsh.command import format_command, resolve_command
from atri_x.agent.dsh.router import DshRouteConfig
from atri_x.agent.dsh.types import AcpError, AcpEvent, AcpUsage, as_text
from atri_x.processenv import new as new_process_env

__all__ = [
    "DshRunResult",
    "DshRuntime",
    "WorkspacePaths",
    "allow_permission",
    "child_environment",
    "config_dir_of",
    "resolve_paths",
]

#: 事件队列里标记「线程已结束」的哨兵。
_SENTINEL: Any = object()
#: 一次 prompt 的事件队列上限（防止界面上游卡住时无限堆积）。
_MAX_QUEUED_EVENTS = 4096
#: 工具事件最多记录多少条标题（审计用，不需要全量）。
_MAX_TOOL_TITLES = 32


@dataclass(frozen=True)
class WorkspacePaths:
    """DSH 需要的两个目录：会话工作区与 ``DSH_HOME``。"""

    workspace: str
    home: str


def config_dir_of(agent: Any) -> str:
    """本轮配置目录（``state.toml`` 所在目录；拿不到时退回当前目录）。"""
    state_path = str(getattr(agent, "state_path", "") or "").strip()
    if state_path:
        return os.path.dirname(os.path.abspath(state_path))
    return os.getcwd()


def resolve_paths(agent: Any, config: DshRouteConfig) -> WorkspacePaths:
    """解析工作区与 ``DSH_HOME``；相对路径按配置目录展开。"""
    base = config_dir_of(agent)

    def resolve(value: str, fallback: str) -> str:
        text = (value or "").strip()
        path = text if text else fallback
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        return os.path.abspath(path)

    return WorkspacePaths(
        workspace=resolve(config.workspace, os.path.join("dsh", "workspace")),
        home=resolve(config.home, os.path.join("dsh", "home")),
    )


def allow_permission(params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``[agent.dsh].allow_permissions=true`` 时的权限应答：选第一个 allow 选项。"""
    options = params.get("options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            if as_text(option.get("kind")).startswith("allow"):
                option_id = as_text(option.get("optionId"))
                if option_id:
                    return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return None


def child_environment(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """给 DSH 子进程构造环境：先收敛掉继承来的凭据，再放回显式注入项。

    子进程不该无脑继承整份 ``os.environ``（内核日志里就会出现宿主的密钥变量），
    因此复用 :mod:`atri_x.processenv` 的 :meth:`Environment.without_sensitive_keys`
    过滤 ``*_KEY`` / ``*_TOKEN`` / ``*SECRET*`` 之类的名字，再叠回调用方
    **明确要注入**的变量（例如 ATRI-X 自己从 ``.env`` 解析出来的 provider 密钥）。
    ``extra_env`` 的值覆盖继承值，空值不进环境。
    """
    entries = [f"{name}={value}" for name, value in os.environ.items()]
    filtered = new_process_env(entries).without_sensitive_keys()
    overlay = {
        str(name): str(value)
        for name, value in (extra_env or {}).items()
        if str(name).strip() and str(value)
    }
    if overlay:
        filtered = filtered.overlay(overlay)
    out: dict[str, str] = {}
    for entry in filtered.environ():
        name, sep, value = entry.partition("=")
        if sep and name:
            out[name] = value
    return out


@dataclass
class DshRunResult:
    """一次路由执行的结果（与 ACP 结果解耦，便于测试构造）。"""

    text: str = ""
    thought: str = ""
    usage: AcpUsage = field(default_factory=AcpUsage)
    stop_reason: str = ""
    session_id: str = ""
    elapsed_seconds: float = 0.0
    tool_titles: list[str] = field(default_factory=list)
    server: str = ""
    #: 内核是否真的回报了 ``usage``；为 ``False`` 时上面的 ``usage`` 只是全 0 占位。
    usage_reported: bool = False
    #: 本轮被内核拒绝的会话配置（例如 ``model`` 填了裸模型 id）：降级说明，供审计。
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否正常结束（``end_turn``）。"""
        return self.stop_reason == "end_turn"

    def to_dict(self) -> dict[str, Any]:
        """给审计用的普通字典（不展开事件）。"""
        return {
            "sessionId": self.session_id,
            "stopReason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "usageReported": self.usage_reported,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "toolCalls": len(self.tool_titles),
            "server": self.server,
            "warnings": list(self.warnings),
        }


class DshRuntime:
    """长驻 DSH ACP 客户端 + 串行 prompt 执行器。"""

    def __init__(
        self,
        config: DshRouteConfig | None = None,
        *,
        command: Sequence[str] | str | None = None,
        paths: WorkspacePaths | None = None,
        client_factory: Callable[..., AcpClient] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or DshRouteConfig()
        self._command = command
        self.paths = paths or WorkspacePaths(workspace=os.getcwd(), home="")
        self._client_factory = client_factory or AcpClient
        self._extra_env = {
            str(name): str(value)
            for name, value in (extra_env or {}).items()
            if str(name).strip() and str(value)
        }
        self._lock = threading.Lock()
        self._client: AcpClient | None = None
        self._active_session = ""
        self._closed = False
        #: 会话配置覆盖被内核拒绝过一次之后就不再重试（见 :meth:`_apply_overrides`）。
        self._model_override_rejected = False
        self._effort_override_rejected = False

    # -- 生命周期 -------------------------------------------------------------
    @property
    def command(self) -> Sequence[str] | str | None:
        """启动命令（构造时未给则按统一顺序解析一次）。"""
        if self._command is None:
            self._command = resolve_command(self.config.command)
        return self._command

    def _ensure_client(self) -> AcpClient:
        """取活着的客户端；没起或已死就重建（在调用线程里执行）。"""
        client = self._client
        if client is not None and client.alive:
            return client
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - 旧进程收尸失败不影响重建
                pass
            self._client = None
        command = self.command
        if not command:
            raise RuntimeError("DSH 启动命令不可用（没有内置内核，PATH 上也没有 dsh）")
        os.makedirs(self.paths.workspace, exist_ok=True)
        env = child_environment(self._extra_env)
        if self.paths.home:
            os.makedirs(self.paths.home, exist_ok=True)
            env["DSH_HOME"] = self.paths.home
        client = self._client_factory(
            command,
            cwd=self.paths.workspace,
            env=env,
            isolate_env=True,
            prompt_timeout=float(self.config.timeout_seconds or 180.0),
            on_permission=allow_permission if self.config.allow_permissions else None,
        )
        client.start()
        self._client = client
        return client

    def close(self) -> None:
        """终止子进程（幂等；可在任意线程调用）。"""
        with self._lock:
            self._closed = True
            client, self._client = self._client, None
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass

    async def aclose(self) -> None:
        """异步包装 :meth:`close`（关停路径复用）。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.close)

    # -- 执行 -----------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_thought: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[AcpEvent], Any] | None = None,
        timeout: float | None = None,
    ) -> DshRunResult:
        """跑一轮 prompt；流式文本经 ``on_chunk`` 回调（在事件循环里 await）。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        dropped = 0

        def emit(event: AcpEvent) -> None:
            nonlocal dropped
            if queue.qsize() >= _MAX_QUEUED_EVENTS:
                dropped += 1
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # 事件循环已关闭（进程退出中）
                pass

        def work() -> DshRunResult:
            try:
                with self._lock:
                    return self._prompt_once(prompt, emit, timeout)
            finally:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
                except RuntimeError:
                    pass

        task = asyncio.ensure_future(loop.run_in_executor(None, work))
        tool_titles: list[str] = []
        try:
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    break
                kind = event.kind
                if kind == "agent_message_chunk" and event.text:
                    if on_chunk is not None:
                        await on_chunk(event.text)
                elif kind == "agent_thought_chunk" and event.text:
                    if on_thought is not None:
                        await on_thought(event.text)
                elif kind in ("tool_call", "tool_call_update"):
                    if event.tool_title and len(tool_titles) < _MAX_TOOL_TITLES:
                        title = f"{event.tool_title} [{event.tool_status or 'pending'}]"
                        if title not in tool_titles:
                            tool_titles.append(title)
                    if on_tool is not None:
                        try:
                            on_tool(event)
                        except Exception:  # noqa: BLE001 - 观察回调不能弄死本轮
                            pass
            result = await task
        except BaseException:
            task.cancel()
            raise
        if not result.tool_titles and tool_titles:
            result.tool_titles = tool_titles
        if dropped and not result.text:
            result.stop_reason = result.stop_reason or "dropped_events"
        return result

    def cancel_active(self) -> None:
        """取消在途 prompt（best-effort；ACP 只有这一条取消路径）。"""
        client = self._client
        session_id = self._active_session
        if client is None or not session_id:
            return
        try:
            client.cancel(session_id)
        except Exception:  # noqa: BLE001 - 取消失败由 prompt 超时兜底
            pass

    # -- 内部 -----------------------------------------------------------------
    def _prompt_once(
        self,
        prompt: str,
        emit: Callable[[AcpEvent], None],
        timeout: float | None,
    ) -> DshRunResult:
        """线程池里执行的同步一次：建会话 → prompt → 关会话。"""
        if self._closed:
            raise RuntimeError("DSH 运行时已关闭")
        client = self._ensure_client()
        info = client.server_info
        started = time.monotonic()
        session = client.new_session(self.paths.workspace)
        self._active_session = session.session_id
        tool_titles: list[str] = []
        warnings: list[str] = []
        try:
            warnings = self._apply_overrides(client, session)
            result = client.prompt(
                session.session_id,
                prompt,
                timeout=timeout if timeout is not None else None,
                on_event=emit,
            )
            for event in result.events:
                if event.kind in ("tool_call", "tool_call_update") and event.tool_title:
                    title = f"{event.tool_title} [{event.tool_status or 'pending'}]"
                    if title not in tool_titles and len(tool_titles) < _MAX_TOOL_TITLES:
                        tool_titles.append(title)
            return DshRunResult(
                text=result.text,
                thought=result.thought,
                usage=result.usage,
                stop_reason=result.stop_reason,
                session_id=session.session_id,
                elapsed_seconds=time.monotonic() - started,
                tool_titles=tool_titles,
                server=(
                    f"{info.agent_name}/{info.agent_version}" if info is not None else ""
                ),
                usage_reported=bool(result.usage_reported),
                warnings=warnings,
            )
        finally:
            self._active_session = ""
            try:
                client.close_session(session.session_id)
            except Exception:  # noqa: BLE001 - 关会话失败不覆盖本轮结果
                pass

    def _apply_overrides(self, client: AcpClient, session: Any) -> list[str]:
        """下发 ``model`` / ``reasoning_effort``；被内核拒绝时**一次性降级**。

        内核只接受它自己 ``session/set_config_option`` 公布的取值；用户把
        ``[agent.dsh].model`` 填成裸模型 id（例如 ``deepseek-v4-flash``）时会回
        ``AcpError [-32602] unknown model option``。这里捕获它、放弃本次覆盖、继续用
        内核默认模型跑完本轮，并把说明记进 :attr:`DshRunResult.warnings`（走审计）。

        两者各自独立降级；同一进程内失败过就不再重试（否则等于每轮都白失败一次，
        连续失败还会把熔断打开，表现为"静默回原生"）。审计只需要一次说明，所以
        标志置位后连 warning 也不再产生。
        """
        warnings: list[str] = []
        model = (self.config.model or "").strip()
        if model and not self._model_override_rejected and session.current_value("model") != model:
            try:
                client.set_config_option(session.session_id, "model", model)
            except AcpError as exc:
                self._model_override_rejected = True
                warnings.append(f"model override rejected: {exc}")
        effort = (self.config.reasoning_effort or "").strip()
        if (
            effort
            and not self._effort_override_rejected
            and session.current_value("reasoning_effort") != effort
        ):
            try:
                client.set_config_option(session.session_id, "reasoning_effort", effort)
            except AcpError as exc:
                self._effort_override_rejected = True
                warnings.append(f"reasoning_effort override rejected: {exc}")
        return warnings

    # -- 诊断 -----------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """运行时状态快照（doctor/测试）。"""
        client = self._client
        command = self.command
        return {
            "command": format_command(command) if command else "",
            "running": bool(client is not None and client.alive),
            "workspace": self.paths.workspace,
            "home": self.paths.home,
            "closed": self._closed,
            # 只报变量名，不回显任何凭据值。
            "extraEnvKeys": sorted(self._extra_env),
        }
