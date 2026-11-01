"""DSH 路由的服务对象：路由器 + 懒创建运行时 + 凭据计划 + 预热 + 指标。

从 :mod:`atri_x.agent.dsh.route_call` 拆出来（600 行约定）：这里只放**服务生命周期**，
真正的"一轮怎么跑、怎么记账、失败怎么回退"仍在 route_call。

挂载方式：``agent.services['dsh_route']``（Agent 每进程一个）。``prepare`` 解析凭据
协议计划并缓存；``ensure_runtime`` 第一次真正要路由时才建 :class:`DshRuntime`；
``start_prewarm`` 由应用启动钩子调用（runner 的 ``after_start``），失败静默。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from atri_x.agent.dsh.credentials import CredentialPlan, resolve_credential_plan
from atri_x.agent.dsh.gateway import HeaderProxy
from atri_x.agent.dsh.router import DshRouteConfig, DshRouter
from atri_x.agent.dsh.runtime import DshRuntime, resolve_paths

__all__ = [
    "DshRouteService",
    "SERVICE_KEY",
    "close_route_service",
    "describe_route",
    "dsh_metrics",
    "ensure_service",
    "prewarm_route_service",
]

#: 服务键（``agent.services['dsh_route']``）。
SERVICE_KEY = "dsh_route"


@dataclass
class DshRouteService:
    """Agent 生命周期内的路由服务：路由器 + 懒创建的运行时 + 凭据计划。"""

    router: DshRouter
    runtime: DshRuntime | None = None
    _paths_agent: Any = field(default=None, repr=False)
    _credential: CredentialPlan | None = field(default=None, repr=False)
    _prewarm_started: bool = field(default=False, repr=False)

    def prepare(self, agent: Any) -> CredentialPlan:
        """解析并缓存凭据计划（幂等；不可用时只审计一次，路由随后静默回原生）。"""
        if self._credential is not None:
            return self._credential
        try:
            plan = resolve_credential_plan(agent, self.router.config)
        except Exception as exc:  # noqa: BLE001 - 解析失败按不可用处理
            plan = CredentialPlan(
                usable=False, reason=f"凭据解析失败：{type(exc).__name__}: {exc}"
            )
        self._credential = plan
        self.router.set_credential(plan.usable, plan.reason)
        if not plan.usable:
            _audit(
                agent,
                "dsh_route_credential_unusable",
                "reason",
                plan.reason,
                "protocol",
                plan.protocol,
                "provider",
                plan.provider,
            )
        return plan

    def ensure_runtime(self, agent: Any) -> DshRuntime:
        """懒创建运行时（第一次真正要路由时才解析命令与目录）。"""
        if self.runtime is None:
            config = self.router.config
            plan = self.prepare(agent)
            # 网关需要额外请求头时，内核的 base_url 指向本地头代理（见 gateway 模块）。
            gateway = None
            if plan.gateway_spec is not None:
                try:
                    gateway = HeaderProxy(plan.gateway_spec)
                except Exception:  # noqa: BLE001 - 代理建不起来就按不可用回退
                    gateway = None
            self.runtime = DshRuntime(
                config,
                command=self.router.command() if self.router.available() else None,
                paths=resolve_paths(agent, config),
                extra_env=dict(plan.env),
                provider=plan.provider,
                gateway=gateway,
            )
            self._paths_agent = agent
        return self.runtime

    def start_prewarm(self, agent: Any) -> bool:
        """后台预热内核（``prewarm`` + 凭据可用 + 未关停时）；返回是否已安排。"""
        config = self.router.config
        if not config.prewarm or not config.enabled:
            return False
        if self._prewarm_started:
            return True
        if not self.prepare(agent).usable:
            return False
        try:
            runtime = self.ensure_runtime(agent)
        except Exception:  # noqa: BLE001 - 预热失败回落到"首轮再拉"
            return False
        self._prewarm_started = True
        threading.Thread(
            target=runtime.prewarm, name="dsh-prewarm", daemon=True
        ).start()
        return True

    async def aclose(self) -> None:
        """关掉 DSH 子进程（应用关停时调用）。"""
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            await runtime.aclose()

    def describe(self) -> dict[str, Any]:
        """状态快照（doctor / 测试 / ``/metrics``）。"""
        out = self.router.describe()
        if self._credential is not None:
            out["credentialPlan"] = self._credential.to_dict()
        if self._prewarm_started:
            out["prewarmStarted"] = True
        if self.runtime is not None:
            out["runtime"] = self.runtime.describe()
        return out


# ---------------------------------------------------------------------------
# 查找/创建
# ---------------------------------------------------------------------------
def _find_service(agent: Any) -> DshRouteService | None:
    services = getattr(agent, "services", None)
    service = services.get(SERVICE_KEY) if isinstance(services, dict) else None
    if service is None:
        service = getattr(agent, "dsh_route_service", None)
    return service if isinstance(service, DshRouteService) else None


def ensure_service(agent: Any) -> DshRouteService:
    """取/建路由服务并挂在 Agent 上（幂等）。"""
    existing = _find_service(agent)
    if existing is not None:
        return existing
    services = getattr(agent, "services", None)
    config = DshRouteConfig.from_section(getattr(agent, "dsh_config", None))
    service = DshRouteService(router=DshRouter(config))
    if isinstance(services, dict):
        services[SERVICE_KEY] = service
    try:
        agent.dsh_route_service = service
    except Exception:  # noqa: BLE001 - 只读/冻结的 Agent 替身不挂属性
        pass
    return service


async def close_route_service(agent: Any) -> None:
    """关停路由服务（没有创建过则什么都不做）。"""
    if agent is None:
        return
    service = _find_service(agent)
    aclose = getattr(service, "aclose", None)
    if callable(aclose):
        await aclose()


def describe_route(agent: Any) -> dict[str, Any]:
    """路由状态快照（没有创建过服务时返回 ``enabled`` 与可用性）。"""
    service = _find_service(agent)
    if service is not None:
        return service.describe()
    config = DshRouteConfig.from_section(getattr(agent, "dsh_config", None))
    return DshRouter(config).describe()


def prewarm_route_service(agent: Any) -> bool:
    """应用启动后调用：后台预热 DSH 内核（失败静默；首轮路由会再拉一次）。"""
    try:
        return ensure_service(agent).start_prewarm(agent)
    except Exception:  # noqa: BLE001 - 预热是增强能力，任何失败都不影响启动
        return False


def dsh_metrics(agent: Any) -> dict[str, Any] | None:
    """``/metrics`` 的 ``dsh`` 段；未启用/没有服务时返回 ``None``（omitempty）。"""
    try:
        service = _find_service(agent)
        if service is None:
            return None
        config = service.router.config
        if not config.enabled:
            return None
        plan = service.prepare(agent)
        gateway_headers = plan.gateway_spec.header_names if plan.gateway_spec else []
        snapshot: dict[str, Any] = {
            "enabled": True,
            "available": service.router.available(),
            "credentialUsable": plan.usable,
            "credentialProtocol": plan.protocol,
            "credentialProvider": plan.provider,
            "credentialReason": plan.reason,
            "credentialGateway": plan.gateway_spec is not None,
            "credentialGatewayHeaders": gateway_headers,
            "gatewayRunning": False,
            "gatewayUpstreamHost": "",
            "gatewayHeaderNames": gateway_headers,
            "breaker": service.router.breaker_state(),
            "prewarm": bool(config.prewarm),
            "running": False,
            "prewarmed": False,
            "active": 0,
            "maxConcurrent": max(1, int(config.max_concurrent_prompts or 2)),
            "busyWaitSeconds": float(config.busy_wait_seconds or 0.0),
            "stallTimeoutSeconds": float(config.stall_timeout_seconds or 0.0),
            "idleTimeoutSeconds": float(config.idle_timeout_seconds or 0.0),
            "affinityEnabled": bool(config.session_affinity),
            "stats": {},
            "affinity": {},
        }
        runtime = service.runtime
        if runtime is not None:
            described = runtime.describe()
            scheduler = described.get("scheduler") or {}
            affinity = described.get("affinity") or {}
            gateway = described.get("gateway") or {}
            snapshot.update(
                {
                    "running": bool(described.get("running")),
                    "prewarmed": bool(described.get("prewarmed")),
                    "active": int(scheduler.get("active", 0) or 0),
                    "maxConcurrent": int(
                        scheduler.get("maxConcurrent", snapshot["maxConcurrent"])
                        or snapshot["maxConcurrent"]
                    ),
                    "lastError": str(described.get("lastError", "") or ""),
                    "stats": scheduler.get("stats") or {},
                    "affinity": affinity,
                    "gatewayRunning": bool(gateway.get("running")),
                    "gatewayUpstreamHost": str(gateway.get("upstreamHost", "") or ""),
                    "gatewayHeaderNames": list(gateway.get("headerNames") or []),
                }
            )
        return snapshot
    except Exception:  # noqa: BLE001 - 指标采集失败不该让 /metrics 500
        return None


def _audit(agent: Any, event: str, *attrs: Any) -> None:
    audit = getattr(agent, "audit", None)
    if not callable(audit):
        return
    try:
        audit(event, *attrs)
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass
