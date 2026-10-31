"""桌面端的后台服务看护：断线后自动探测 → 重启 → 退避重试。

为什么需要它（真实事故）：GUI 只是 WS 前端，Agent 跑在 ``atri-x service run`` 里。
后台服务可能因为崩溃、被系统/安全软件清理、被脚本误杀而消失。此前 GUI 只会每 2 秒
重连一次，永远停在「已断开（… WinError 1225）」，**不会自己把服务拉起来**；用户必须
手动点「服务 → 启动/确认后台服务」，期间 QQ 机器人和桌面端一起长期掉线。

本模块把「恢复到能连上」做成一个只在主线程 tick 的状态机；真正阻塞的重启动作在后台
线程里跑（避免卡住 Tk 事件循环）::

    sup = ServiceSupervisor(service_recovery(settings))
    sup.note_state(state)          # GUI 每次收到连接状态事件（on_state）
    sup.tick()                     # 主线程定时器里（:data:`atri_x.gui.app.POLL_INTERVAL_MS`）
    for outcome in sup.take_outcomes():   # 取回后台线程的结果并提示用户
        ...

安全边界（每条都有对应的回归用例）：

* **只对回环地址启用**（:func:`is_loopback_url`）：连远端服务器时绝不替对方重启进程；
* ``--no-service`` 的 GUI 不构造本对象；用户从菜单显式停止/重启服务时用
  :meth:`ServiceSupervisor.suspend` 暂停，恢复成功后 :meth:`~ServiceSupervisor.resume`，
  不跟用户的意图打架；
* 断线要持续 ``grace`` 秒才动手，避免网络瞬断、服务重启的 1–2 秒窗口触发自动重启；
* 失败按 ``backoff`` 退避，``max_attempts`` 次后停下来只提示一次（等下一次连上、
  手动恢复或重启 GUI 重置），不会无限刷屏式重启。
"""

from __future__ import annotations

import ipaddress
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from atri_x.gui.client import STATE_CONNECTED, STATE_DISCONNECTED
from atri_x.gui.process import (
    is_process_alive,
    read_service_pid,
    stop_service,
)
from atri_x.gui.service import GuiSettings, probe_server, spawn_service, wait_until_ready

__all__ = [
    "GRACE_SECONDS",
    "MAX_ATTEMPTS",
    "RECOVERY_BACKOFF_SECONDS",
    "RecoveryOutcome",
    "ServiceSupervisor",
    "is_loopback_url",
    "service_recovery",
]

#: 断线持续多久才尝试重启（秒）。
GRACE_SECONDS = 8.0

#: 连续失败之间的退避（秒）；用尽后按最后一档重复，最多 :data:`MAX_ATTEMPTS` 次。
RECOVERY_BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0)

#: 一次掉线最多自动尝试几次。
MAX_ATTEMPTS = 5

#: 重启后等待服务就绪的上限（秒）。
RECOVERY_READY_TIMEOUT = 30.0

#: 本地服务探测的握手超时（秒）。
RECOVERY_PROBE_TIMEOUT = 1.5


@dataclass(frozen=True)
class RecoveryOutcome:
    """一次自动恢复尝试的结果（后台线程 → 主线程）。"""

    #: 服务是否已恢复到可连接。
    ok: bool
    #: 失败原因（成功时为空串或说明）。
    detail: str
    #: 这是本次掉线的第几次尝试（从 1 开始）。
    attempt: int


def is_loopback_url(url: str) -> bool:
    """``ws://…`` 地址是否指向本机回环。

    只有本地后台服务才允许被自动重启：``ws://127.0.0.1:32172/…`` /
    ``ws://localhost:…`` / ``ws://[::1]:…`` 返回 True；域名、局域网 IP、公网地址一律
    False（那是「连别的机器」，替对方重启进程既无权限也不合理）。
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    if parts.scheme not in ("ws", "wss"):
        return False
    host = (parts.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "0.0.0.0"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def service_recovery(
    settings: GuiSettings,
    *,
    probe: Callable[..., tuple[bool, str]] = probe_server,
    spawn: Callable[[GuiSettings], Any] = spawn_service,
    wait_ready: Callable[..., tuple[bool, str]] = wait_until_ready,
    read_pid: Callable[[], int] = read_service_pid,
    alive: Callable[[int], bool] = is_process_alive,
    stop: Callable[[], tuple[bool, str]] = stop_service,
    probe_timeout: float = RECOVERY_PROBE_TIMEOUT,
    ready_timeout: float = RECOVERY_READY_TIMEOUT,
) -> Callable[[], tuple[bool, str]]:
    """造一个「把本地后台服务恢复到可连接」的动作。

    步骤：先探测一次（可能已经自己好了）→ pid 记录里的进程**还活着但端口不通**时
    先强制停掉（挂死/绑定失败的服务重启才能好）→ 拉起 → 等就绪。依赖全部可注入，
    单测不用真的起进程。
    """

    def recover() -> tuple[bool, str]:
        ok, _reason = probe(settings.url, settings.client_id, settings.token, timeout=probe_timeout)
        if ok:
            return True, "后台服务已在运行"
        pid = read_pid()
        if pid > 0 and alive(pid):
            # 进程还在但端口不通：先停（否则新进程抢不到端口）。
            stop()
        try:
            spawn(settings)
        except Exception as exc:  # noqa: BLE001 - 失败原因要原样带回去展示
            return False, f"启动后台服务失败：{type(exc).__name__}: {exc}"
        ok, reason = wait_ready(settings, timeout=ready_timeout)
        if ok:
            return True, ""
        return False, reason or "后台服务未在限定时间内就绪"

    return recover


class ServiceSupervisor:
    """断线自愈状态机（主线程 tick + 后台线程恢复）。"""

    def __init__(
        self,
        recover: Callable[[], tuple[bool, str]],
        *,
        grace: float = GRACE_SECONDS,
        backoff: tuple[float, ...] = RECOVERY_BACKOFF_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._recover = recover
        self._grace = max(0.0, float(grace))
        self._backoff = tuple(max(0.0, float(item)) for item in backoff) or (0.0,)
        self._max_attempts = max(1, int(max_attempts))
        self._clock = clock
        self._enabled = True
        self._connected = False
        self._disconnected_since: float | None = None
        self._attempts = 0
        self._next_at = 0.0
        self._in_flight = False
        self._thread: threading.Thread | None = None
        self._outcomes: queue.Queue[RecoveryOutcome] = queue.Queue()

    # -------------------------------------------------------------- 状态输入

    def note_state(self, state: str) -> None:
        """GUI 收到连接状态事件时调用（``on_state`` 的两类关键状态）。"""
        if state == STATE_CONNECTED:
            self._connected = True
            self._disconnected_since = None
            self._attempts = 0
            self._next_at = 0.0
        elif state == STATE_DISCONNECTED:
            self._connected = False
            if self._disconnected_since is None:
                self._disconnected_since = self._clock()
        # connecting / auth_failed / stopped 不改计时：鉴权失败重启服务没有意义，
        # 用户主动 stop 则由 suspend() 负责。

    # -------------------------------------------------------------- 主线程

    def tick(self) -> bool:
        """主线程定时调用；到点且允许时启动一次后台恢复，返回是否启动。"""
        if not self._enabled or self._in_flight or self._connected:
            return False
        since = self._disconnected_since
        if since is None:
            return False
        now = self._clock()
        if now - since < self._grace:
            return False
        if self._attempts >= self._max_attempts:
            return False
        if now < self._next_at:
            return False
        self._attempts += 1
        attempt = self._attempts
        index = min(attempt - 1, len(self._backoff) - 1)
        self._next_at = now + self._backoff[index]
        self._in_flight = True
        self._thread = threading.Thread(
            target=self._run, args=(attempt,), name="atri-x-gui-service-recover", daemon=True
        )
        self._thread.start()
        return True

    def take_outcomes(self) -> list[RecoveryOutcome]:
        """取回后台线程已经完成的尝试结果（主线程调用，不阻塞）。"""
        outcomes: list[RecoveryOutcome] = []
        while True:
            try:
                outcomes.append(self._outcomes.get_nowait())
            except queue.Empty:
                return outcomes

    def suspend(self) -> None:
        """暂停自愈（用户显式停止/重启服务、退出程序时调用）。"""
        self._enabled = False
        self._disconnected_since = None
        self._next_at = 0.0

    def resume(self) -> None:
        """恢复自愈并重新开始计时（用户手动启动/重启服务成功、或重启失败后）。"""
        self._enabled = True
        self._disconnected_since = None
        self._next_at = 0.0
        self._attempts = 0

    # -------------------------------------------------------------- 后台线程

    def _run(self, attempt: int) -> None:
        try:
            ok, detail = self._recover()
        except Exception as exc:  # noqa: BLE001 - 恢复失败不能弄死看护线程
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self._in_flight = False
        self._outcomes.put(RecoveryOutcome(ok=bool(ok), detail=str(detail or ""), attempt=attempt))

    # -------------------------------------------------------------- 只读状态

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def in_flight(self) -> bool:
        """是否正在后台重启（状态栏据此显示「正在自动重启后台服务…」）。"""
        return self._in_flight

    @property
    def attempts(self) -> int:
        """本次掉线已经尝试了几次。"""
        return self._attempts

    @property
    def exhausted(self) -> bool:
        """尝试次数是否已经用尽（不会再自动重试，等用户或下一次连上）。"""
        return self._attempts >= self._max_attempts

    def join(self, timeout: float = 5.0) -> None:
        """等后台恢复线程结束（退出/测试用；线程本身是 daemon）。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
