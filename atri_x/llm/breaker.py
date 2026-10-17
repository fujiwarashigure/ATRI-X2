"""Provider 级熔断器。

对应 Go：``internal/llm/breaker/breaker.go``（单个熔断状态机）与
``internal/app/breaker_llm.go`` / ``internal/app/models.go``（按 Provider 各建一个
熔断器、``breaker.ErrOpen`` 拒绝请求、``/healthz`` 与 ``/metrics`` 读状态）。

Python 侧把两层放在同一个模块里：

* :class:`Config` / :class:`Breaker` 与 Go 的 ``breaker.Config`` / ``breaker.Breaker``
  逐字段、逐分支对齐（``Enabled`` / ``Allow`` / ``Success`` / ``Failure`` / ``State``，
  默认冷却 1 分钟、半开探测上限 1、``failure_threshold <= 0`` 表示禁用）；
* :class:`ProviderBreakers` 是 Provider 名到熔断器的注册表，给上层提供
  ``is_open`` / ``allow`` / ``record_success`` / ``record_failure`` / ``state`` /
  ``reset``，对应 Go 里「每个 provider 一个 ``*breaker.Breaker``（存在
  ``ModelClients.ByProvider`` 的包装层里）」的用法。

并发：Go 用 ``sync.Mutex``；这里用 ``threading.Lock``（同步状态机既会被
asyncio 任务调用，也会被健康检查的同步路径读取）。锁内不做任何 await。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from atri_x.errors import ErrOpen, OpenError

__all__ = [
    "Breaker",
    "Config",
    "ErrOpen",
    "OpenError",
    "ProviderBreakers",
    "STATE_CLOSED",
    "STATE_DISABLED",
    "STATE_HALF_OPEN",
    "STATE_OPEN",
    "new_breaker",
]

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"
STATE_DISABLED = "disabled"

DEFAULT_OPEN_COOLDOWN_SECONDS = 60.0
DEFAULT_HALF_OPEN_MAX = 1


@dataclass
class Config:
    """对应 Go ``breaker.Config``。"""

    #: 非正数表示熔断器被禁用（不可拒绝请求）。
    failure_threshold: int = 0
    #: 打开后的冷却时间（秒）。对应 Go ``OpenCooldown time.Duration``。
    open_cooldown: float = DEFAULT_OPEN_COOLDOWN_SECONDS
    #: 半开状态下允许同时在飞的探测数。
    half_open_max: int = DEFAULT_HALF_OPEN_MAX

    def with_defaults(self) -> "Config":
        """对应 Go ``New`` 里的默认值填充。"""
        if self.open_cooldown <= 0:
            self.open_cooldown = DEFAULT_OPEN_COOLDOWN_SECONDS
        if self.half_open_max <= 0:
            self.half_open_max = DEFAULT_HALF_OPEN_MAX
        return self


class Breaker:
    """并发安全的熔断状态机。对应 Go ``breaker.Breaker``。"""

    def __init__(
        self,
        config: Config | None = None,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = (config or Config()).with_defaults()
        #: 可注入时钟（秒，单调即可）；Go 用 ``Breaker.now`` 字段，测试直接替换。
        self.now: Callable[[], float] = now or time.monotonic
        self._lock = threading.Lock()
        self.state_name = STATE_CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self.half_open_in_flight = 0

    # -- Go 的方法名 ---------------------------------------------------------
    def enabled(self) -> bool:
        """对应 Go ``Enabled``：熔断器是否有权拒绝请求。"""
        return self.cfg.failure_threshold > 0

    def allow(self) -> bool:
        """对应 Go ``Allow``：现在是否放行一个请求。"""
        if not self.enabled():
            return True
        now = self.now()
        with self._lock:
            if self.state_name == STATE_OPEN:
                if now - self.opened_at < self.cfg.open_cooldown:
                    return False
                self.state_name = STATE_HALF_OPEN
                self.half_open_in_flight = 1
                return True
            if self.state_name == STATE_HALF_OPEN:
                if self.half_open_in_flight >= self.cfg.half_open_max:
                    return False
                self.half_open_in_flight += 1
                return True
            return True

    def success(self) -> None:
        """对应 Go ``Success``。"""
        if not self.enabled():
            return
        with self._lock:
            self.state_name = STATE_CLOSED
            self.failures = 0
            self.half_open_in_flight = 0

    def failure(self) -> None:
        """对应 Go ``Failure``。"""
        if not self.enabled():
            return
        with self._lock:
            if self.state_name == STATE_HALF_OPEN:
                self.opened_at = self.now()
                self.state_name = STATE_OPEN
                self.half_open_in_flight = 0
                return
            if self.state_name == STATE_OPEN:
                self.opened_at = self.now()
                return
            self.failures += 1
            if self.failures >= self.cfg.failure_threshold:
                self.opened_at = self.now()
                self.state_name = STATE_OPEN
                self.half_open_in_flight = 0

    def state(self) -> str:
        """对应 Go ``State``：返回当前状态名（禁用时为 ``disabled``）。"""
        if not self.enabled():
            return STATE_DISABLED
        with self._lock:
            return self.state_name

    # -- Python 侧只读视图（供 /healthz、/metrics） --------------------------
    def snapshot(self) -> dict[str, Any]:
        """不改变状态机的只读快照。"""
        if not self.enabled():
            return {"state": STATE_DISABLED, "failures": 0, "enabled": False}
        with self._lock:
            return {
                "state": self.state_name,
                "failures": self.failures,
                "enabled": True,
                "half_open_in_flight": self.half_open_in_flight,
            }

    def reset(self) -> None:
        """回到关闭状态并清零计数（Go 没有对应方法，供运维手动恢复用）。"""
        with self._lock:
            self.state_name = STATE_CLOSED
            self.failures = 0
            self.opened_at = 0.0
            self.half_open_in_flight = 0


def new_breaker(config: Config | None = None, *, now: Callable[[], float] | None = None) -> Breaker:
    """对应 Go ``breaker.New``。"""
    return Breaker(config, now=now)


class ProviderBreakers:
    """按 Provider 统计失败的熔断器注册表。

    对应 Go ``internal/app/models.go`` 里「每个 provider 建一个
    ``breaker.New(...)``」+ ``breaker_llm`` 里的 ``Allow`` / ``Success`` /
    ``Failure`` / ``ErrOpen`` 用法：本类把「provider 名 → 熔断器」的映射、
    拒绝时的 ``ErrOpen`` 抛出与状态快照集中在一处，避免每个调用点各自记一份。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        configs: Mapping[str, Config] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.config = (config or Config()).with_defaults()
        self._configs = dict(configs or {})
        self._now = now
        self._lock = threading.Lock()
        self._breakers: dict[str, Breaker] = {}

    # -- 查表 ---------------------------------------------------------------
    def breaker_for(self, provider: str) -> Breaker:
        """取（必要时创建）该 Provider 的熔断器。"""
        key = provider or ""
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                cfg = self._configs.get(key, self.config)
                breaker = Breaker(cfg, now=self._now)
                self._breakers[key] = breaker
            return breaker

    def providers(self) -> list[str]:
        with self._lock:
            return sorted(self._breakers)

    # -- 判定 ---------------------------------------------------------------
    def is_open(self, provider: str) -> bool:
        """现在是否会拒绝该 Provider 的请求（只读，不消耗半开探测名额）。

        与 :meth:`allow` 判定一致：冷却期已过的 ``open`` 会转成
        ``half_open``，此时本方法返回 False（下一次 :meth:`allow` 会真的放行
        一个探测）。要看未经翻译的原始状态请用 :meth:`state`。
        """
        breaker = self.breaker_for(provider)
        if not breaker.enabled():
            return False
        with breaker._lock:  # noqa: SLF001 - 同模块内的只读快照
            if breaker.state_name == STATE_OPEN:
                return breaker.now() - breaker.opened_at < breaker.cfg.open_cooldown
            if breaker.state_name == STATE_HALF_OPEN:
                return breaker.half_open_in_flight >= breaker.cfg.half_open_max
            return False

    def allow(self, provider: str) -> bool:
        """是否放行该 Provider 的一个请求（可能消耗半开探测名额）。"""
        return self.breaker_for(provider).allow()

    def check(self, provider: str) -> None:
        """拒绝时抛 ``ErrOpen``（对应 Go ``fmt.Errorf("%w: provider=%s", ErrOpen, p)``）。"""
        if not self.allow(provider):
            raise OpenError(f"{ErrOpen}: provider={provider}")

    # -- 记账 ---------------------------------------------------------------
    def record_success(self, provider: str) -> None:
        self.breaker_for(provider).success()

    def record_failure(self, provider: str) -> None:
        self.breaker_for(provider).failure()

    # -- 展示与运维 ---------------------------------------------------------
    def state(self, provider: str | None = None) -> Any:
        """状态快照。

        传 ``provider`` 返回该 Provider 的状态名（``disabled`` / ``closed`` /
        ``open`` / ``half_open``），对应 Go ``Breaker.State()``；不传则返回
        ``{provider: state}`` 全量字典，供 ``/healthz`` 与 ``/metrics`` 展示。
        """
        if provider is not None:
            return self.breaker_for(provider).state()
        return {name: self.breaker_for(name).state() for name in self.providers()}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """带计数的全量只读快照。"""
        return {name: self.breaker_for(name).snapshot() for name in self.providers()}

    def reset(self, provider: str | None = None) -> None:
        """手动复位：传 ``provider`` 只复位一个，不传则全部复位。"""
        if provider is not None:
            self.breaker_for(provider).reset()
            return
        for name in self.providers():
            self.breaker_for(name).reset()
