"""DSH 运行时的进程与并发管理：长驻 AcpClient + 有界并发 + 看门狗 + 会话亲和。

为什么长驻：``dsh --profile acp`` 是 node 进程，冷启动要 1~2 秒；路由的收益是
"复杂任务一次做对"，不是每轮都付启动成本。因此进程按需拉起（或启动期预热）、成功后
复用；子进程死了下次调用自动重启。

并发模型（1.1.8 起）：内核的 ACP 连接本来就支持**多会话并发**（每个会话自带 inflight
槽），所以这里用 :class:`~atri_x.agent.dsh.scheduler.TurnScheduler` 开 N 个并发槽，
槽满时短暂等待、等不到就回退原生（见 ``busy_wait_seconds``）。同一会话仍然只允许
一轮 prompt（由内核保证，客户端也会拦）。

会话复用（P2）：默认"每轮新会话 + 完整交接"，但同一聊天**连续**路由时走
:mod:`atri_x.agent.dsh.affinity`，只发增量、省掉重复交接；连续性校验不通过就退回
完整交接。

线程模型：``AcpClient.prompt`` 是**同步阻塞**的（见 :mod:`atri_x.agent.dsh.client`），
而对话链路是 asyncio。这里的桥接方式是：

1. 把「取槽 → 建/续会话 → prompt → 关/留会话」整段丢进线程池执行；
2. ACP 的 ``session/update`` 通知在读线程里触发回调，用
   ``loop.call_soon_threadsafe`` 塞进 :class:`asyncio.Queue`；
3. 事件循环侧边收事件边把文本写进界面的 ``MessageStream``，等线程返回后再取结果；
4. 监控线程（``dsh-monitor``）负责卡死取消与空闲回收，见 scheduler 模块。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

from atri_x.agent.dsh.affinity import AffinityEntry, SessionAffinity
from atri_x.agent.dsh.client import AcpClient
from atri_x.agent.dsh.command import format_command, resolve_command
from atri_x.agent.dsh.handoff import latest_user_text
from atri_x.agent.dsh.router import DshRouteConfig
from atri_x.agent.dsh.scheduler import TurnScheduler
from atri_x.agent.dsh.turn import DshRunResult, DshTurnPlan
from atri_x.agent.dsh.types import AcpError, AcpEvent, as_text
from atri_x.agent.dsh.workspace import (
    WorkspacePaths,
    allow_permission,
    child_environment,
    config_dir_of,
    resolve_paths,
)

__all__ = [
    "DshRunResult",
    "DshRuntime",
    "DshTurnPlan",
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


class DshRuntime:
    """长驻 DSH ACP 客户端 + 有界并发执行器（线程安全）。"""

    def __init__(
        self,
        config: DshRouteConfig | None = None,
        *,
        command: Sequence[str] | str | None = None,
        paths: WorkspacePaths | None = None,
        client_factory: Callable[..., AcpClient] | None = None,
        extra_env: Mapping[str, str] | None = None,
        provider: str = "",
        monitor_interval: float = 0.0,
    ) -> None:
        self.config = config or DshRouteConfig()
        self._command = command
        self.provider = str(provider or "").strip()
        self.paths = paths or WorkspacePaths(workspace=os.getcwd(), home="")
        self._client_factory = client_factory or AcpClient
        self._extra_env = {
            str(name): str(value)
            for name, value in (extra_env or {}).items()
            if str(name).strip() and str(value)
        }
        self._client_lock = threading.RLock()
        self._client: AcpClient | None = None
        self._closed = False
        #: 正在"取槽后、登记在途轮次前"的 worker 数（空闲回收不能把它们的内核关掉）。
        self._starting = 0
        self._prewarmed = False
        self._last_error = ""
        #: 会话配置覆盖被内核拒绝过一次之后就不再重试（见 :meth:`_apply_overrides`）。
        self._model_override_rejected = False
        self._effort_override_rejected = False
        self._scheduler = TurnScheduler(
            max_concurrent=max(1, int(self.config.max_concurrent_prompts or 2)),
            busy_wait_seconds=float(self.config.busy_wait_seconds or 0.0),
            stall_timeout_seconds=float(self.config.stall_timeout_seconds or 0.0),
            idle_timeout_seconds=float(self.config.idle_timeout_seconds or 0.0),
            on_idle=self._idle_close,
            interval=float(monitor_interval or 5.0),
        )
        self._affinity: SessionAffinity | None = (
            SessionAffinity(
                max_entries=int(self.config.affinity_max_sessions or 4),
                ttl_seconds=float(self.config.affinity_ttl_seconds or 0.0),
            )
            if self.config.session_affinity
            else None
        )

    # -- 生命周期 -------------------------------------------------------------
    @property
    def command(self) -> Sequence[str] | str | None:
        """启动命令（构造时未给则按统一顺序解析一次）。"""
        if self._command is None:
            self._command = resolve_command(self.config.command)
        return self._command

    @property
    def prewarmed(self) -> bool:
        """是否已成功预热过内核。"""
        return self._prewarmed

    def _ensure_client(self) -> AcpClient:
        """取活着的客户端；没起或已死就重建（在调用线程里执行）。"""
        with self._client_lock:
            if self._closed:
                raise RuntimeError("DSH 运行时已关闭")
            client = self._client
            if client is not None and client.alive:
                return client
            if client is not None:
                try:
                    client.stop()
                except Exception:  # noqa: BLE001 - 旧进程收尸失败不影响重建
                    pass
                self._client = None
                if self._affinity is not None:
                    self._affinity.clear()  # 进程没了，会话也就不存在了
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
                on_permission=(
                    allow_permission if self.config.allow_permissions else None
                ),
            )
            client.start()
            self._client = client
            self._scheduler.record_cold_start()
            self._scheduler.mark_activity()
            self._scheduler.start()
            return client

    def prewarm(self) -> bool:
        """同步预热内核（spawn + initialize）；失败只记录、不抛。"""
        if self._closed:
            return False
        try:
            with self._client_lock:
                self._starting += 1
            try:
                self._ensure_client()
            finally:
                with self._client_lock:
                    self._starting -= 1
        except Exception as exc:  # noqa: BLE001 - 预热失败回落到"首轮再拉"
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        self._prewarmed = True
        self._scheduler.record_prewarm()
        return True

    async def aprewarm(self) -> bool:
        """异步预热（关停/测试路径复用）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.prewarm)

    def close(self) -> None:
        """终止子进程并停止监控（幂等；可在任意线程调用）。"""
        with self._client_lock:
            self._closed = True
            client, self._client = self._client, None
        self._scheduler.cancel_all()
        self._scheduler.stop()
        if client is not None:
            if self._affinity is not None:
                self._affinity.clear()
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass

    async def aclose(self) -> None:
        """异步包装 :meth:`close`（关停路径复用）。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.close)

    # -- 计划 -----------------------------------------------------------------
    def plan_turn(
        self, turn_key: str, messages: Sequence[Any], *, handoff_prompt: str
    ) -> DshTurnPlan:
        """决定本轮发完整交接还是只发增量（同步、快；不碰子进程）。"""
        body = tuple(messages or ())
        base = DshTurnPlan(
            prompt=handoff_prompt,
            fallback_prompt=handoff_prompt,
            key=turn_key,
            messages=body,
        )
        if self._affinity is None or not turn_key:
            return base
        entry, stale = self._affinity.consume(turn_key, body)
        self._close_entries(stale)
        if entry is None:
            return base
        incremental = latest_user_text(body)
        return DshTurnPlan(
            prompt=incremental or handoff_prompt,
            fallback_prompt=handoff_prompt,
            session_id=entry.session_id,
            reused=True,
            key=turn_key,
            messages=body,
        )

    # -- 执行 -----------------------------------------------------------------
    async def run(
        self,
        prompt: str = "",
        *,
        plan: DshTurnPlan | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        on_thought: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[AcpEvent], Any] | None = None,
        timeout: float | None = None,
        cancel_token: object | None = None,
    ) -> DshRunResult:
        """跑一轮 prompt；流式文本经 ``on_chunk`` 回调（在事件循环里 await）。

        ``cancel_token`` 给出后，可以用 :meth:`cancel` 只取消这一轮（不再"取消所有"）。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        dropped = 0
        token = cancel_token if cancel_token is not None else object()

        def emit(event: AcpEvent) -> None:
            nonlocal dropped
            self._scheduler.touch(token)
            if queue.qsize() >= _MAX_QUEUED_EVENTS:
                dropped += 1
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # 事件循环已关闭（进程退出中）
                pass

        def work() -> DshRunResult:
            try:
                waited = self._scheduler.admit()
                try:
                    return self._prompt_once(
                        prompt, emit, timeout, token=token, plan=plan, waited=waited
                    )
                finally:
                    self._scheduler.release()
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

    def cancel(self, token: object) -> bool:
        """取消某一轮（``run(cancel_token=...)``）；返回是否真的发了 cancel。"""
        return self._scheduler.cancel(token)

    def cancel_active(self) -> int:
        """取消所有在途 prompt（关停/兼容路径）；返回取消了几轮。"""
        return self._scheduler.cancel_all()

    # -- 内部 -----------------------------------------------------------------
    def _prompt_once(
        self,
        prompt: str,
        emit: Callable[[AcpEvent], None],
        timeout: float | None,
        *,
        token: object,
        plan: DshTurnPlan | None,
        waited: float,
    ) -> DshRunResult:
        """线程池里执行的同步一次：取槽后建/续会话 → prompt → 关/留会话。"""
        plan = plan or DshTurnPlan(prompt=prompt, fallback_prompt=prompt)
        with self._client_lock:
            self._starting += 1
        try:
            client = self._ensure_client()
            info = client.server_info
            started = time.monotonic()
            session, live, created = self._acquire_session(client, plan)
            warnings: list[str] = []
            if created:
                prompt_text = plan.fallback_prompt or plan.prompt or prompt
                warnings = self._apply_overrides(client, session)
            else:
                prompt_text = plan.prompt or prompt
                if not live:
                    warnings = self._apply_overrides(client, session)
            self._scheduler.register(
                token, session.session_id, lambda: client.cancel(session.session_id)
            )
        finally:
            with self._client_lock:
                self._starting -= 1
        return self._execute_prompt(
            client, session, prompt_text, emit, timeout, token, plan, waited,
            started, info, warnings, created,
        )

    def _execute_prompt(
        self,
        client: AcpClient,
        session: Any,
        prompt_text: str,
        emit: Callable[[AcpEvent], None],
        timeout: float | None,
        token: object,
        plan: DshTurnPlan,
        waited: float,
        started: float,
        info: Any,
        warnings: list[str],
        created: bool,
    ) -> DshRunResult:
        """真正发 prompt 并收尾（异常时关闭会话、记账失败）。"""
        session_id = session.session_id
        recorded = False
        keep = False
        turn = None
        try:
            result = client.prompt(
                session_id, prompt_text, timeout=timeout, on_event=emit
            )
            elapsed = max(0.0, time.monotonic() - started)
            turn = self._scheduler.unregister(token)
            stalled = bool(turn and turn.stalled)
            stop_reason = as_text(result.stop_reason)
            tool_titles: list[str] = []
            for event in result.events:
                if event.kind in ("tool_call", "tool_call_update") and event.tool_title:
                    title = f"{event.tool_title} [{event.tool_status or 'pending'}]"
                    if title not in tool_titles and len(tool_titles) < _MAX_TOOL_TITLES:
                        tool_titles.append(title)
            out = DshRunResult(
                text=result.text,
                thought=result.thought,
                usage=result.usage,
                stop_reason=stop_reason,
                session_id=session_id,
                elapsed_seconds=elapsed,
                tool_titles=tool_titles,
                server=(
                    f"{info.agent_name}/{info.agent_version}" if info is not None else ""
                ),
                usage_reported=bool(result.usage_reported),
                warnings=warnings,
                queue_wait_seconds=waited,
                session_reused=not created,
                stalled=stalled,
            )
            if stalled:
                self._scheduler.record_failure(elapsed)
                recorded = True
                raise RuntimeError(
                    f"DSH 轮次卡死：{self.config.stall_timeout_seconds:g}s 内没有任何事件，已取消"
                )
            if stop_reason == "end_turn":
                self._scheduler.record_success(elapsed)
                recorded = True
                keep = self._store_affinity(plan, session_id)
            else:
                self._scheduler.record_failure(elapsed)
                recorded = True
            return out
        except BaseException:
            if not recorded:
                self._scheduler.record_failure(max(0.0, time.monotonic() - started))
            raise
        finally:
            self._scheduler.unregister(token)
            if not keep:
                self._forget_session(client, plan, session_id)

    def _acquire_session(
        self, client: AcpClient, plan: DshTurnPlan
    ) -> tuple[Any, bool, bool]:
        """返回 ``(session, live, created)``：live = 复用本连接上已打开的会话。"""
        if plan.reused and plan.session_id:
            existing = client.session(plan.session_id)
            if existing is not None:
                return existing, True, False
            try:
                resumed = client.resume_session(plan.session_id, self.paths.workspace)
            except AcpError:
                resumed = None
            if resumed is not None:
                return resumed, False, False
        return client.new_session(self.paths.workspace), False, True

    def _store_affinity(self, plan: DshTurnPlan, session_id: str) -> bool:
        """把会话登记为可复用；返回是否保留会话（``False`` 时调用方负责关闭）。"""
        if self._affinity is None or not plan.key or not plan.messages:
            return False
        evicted = [
            entry
            for entry in self._affinity.store(plan.key, session_id, plan.messages)
            if entry.session_id != session_id
        ]
        self._close_entries(evicted)
        return True

    def _forget_session(
        self, client: AcpClient, plan: DshTurnPlan, session_id: str
    ) -> None:
        """失败/不保留时：从亲和缓存移除并关闭会话（best-effort）。"""
        stale: list[AffinityEntry] = []
        if self._affinity is not None and plan.key:
            entry = self._affinity.pop(plan.key)
            if entry is not None:
                stale.append(entry)
        for entry in stale:
            if entry.session_id != session_id:
                self._close_entry(client, entry)
        try:
            client.close_session(session_id)
        except Exception:  # noqa: BLE001 - 关会话失败不覆盖本轮结果
            pass

    def _close_entries(self, entries: Sequence[AffinityEntry]) -> None:
        client = self._client
        if client is None or not client.alive:
            return
        for entry in entries:
            self._close_entry(client, entry)

    def _close_entry(self, client: AcpClient, entry: AffinityEntry) -> None:
        try:
            client.close_session(entry.session_id)
        except Exception:  # noqa: BLE001 - 淘汰/失效路径不抛
            pass

    def _idle_close(self) -> bool:
        """空闲回收：关掉内核进程（监控线程调用）；有 worker 正在启动时跳过。"""
        with self._client_lock:
            if self._closed or self._starting > 0:
                return False
            client, self._client = self._client, None
        if client is None:
            return False
        if self._affinity is not None:
            self._affinity.clear()
        try:
            client.stop()
        except Exception:  # noqa: BLE001 - 回收失败不影响后续轮次
            pass
        self._scheduler.record_idle_close()
        return True

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
        """运行时状态快照（doctor/测试/metrics）。"""
        client = self._client
        command = self.command
        return {
            "command": format_command(command) if command else "",
            "running": bool(client is not None and client.alive),
            "workspace": self.paths.workspace,
            "home": self.paths.home,
            "closed": self._closed,
            "prewarmed": self._prewarmed,
            "lastError": self._last_error,
            "provider": self.provider,
            # 只报变量名，不回显任何凭据值。
            "extraEnvKeys": sorted(self._extra_env),
            "scheduler": self._scheduler.snapshot(),
            "affinity": None if self._affinity is None else self._affinity.snapshot(),
        }
