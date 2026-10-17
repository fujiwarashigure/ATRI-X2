"""集成阶段：把平台运行时接到 Agent 上，并按需拉起 atrinis。

对应 Go：``internal/app/integrations.go``。

``Attach`` 做四件事，顺序不能换：

1. 配置启用且非 CLI-only 时构造 atrinis 服务与 HTTP 运行时，并把它追加成一个
   平台运行时（``atrinis``）；
2. 给支持补全/命令目录的适配器装上对应数据；
3. 注册平台发送器与连接通知；
4. **在适配器启动前**订阅平台连接事件 —— 连接事实走全局信号，订阅方必须在第一个
   ``Emit`` 之前就位，否则那次连接会被丢掉（信号不缓存、不重放）。

与 Go 的差异：``atri_x.atrinis`` 的 ``Options`` 字段名与 Go 一一对应；
``resolve_model`` 是 ``(slot) -> ModelSelection`` 的同步可调用。
"""

from __future__ import annotations

import os
from typing import Any

from atri_x.app.dependencies import IntegrationRequest, PlatformComponents
from atri_x.app.platforms import (
    AtrinisRuntimeAdapter,
    register_command_catalogs,
    register_completion_platforms,
    register_platform_hooks,
    runtime_names,
)
from atri_x.app.platform_caller import PlatformCallerResolver
from atri_x.app.support import maybe_await
from atri_x.config import ModelSelection, config_env
from atri_x.errors import InvalidError

__all__ = [
    "DefaultIntegrationFactory",
    "defaultIntegrationFactory",
    "resolveAtrinisTokens",
    "resolve_atrinis_tokens",
    "runtimeNames",
    "runtime_names",
]


class DefaultIntegrationFactory:
    """对应 Go ``defaultIntegrationFactory``。"""

    async def attach(self, ctx: Any, req: IntegrationRequest) -> PlatformComponents:
        """对应 Go ``Attach``。"""
        foundation = req.foundation
        runtime = req.runtime
        if foundation is None or runtime is None:
            raise InvalidError("app: integration request is incomplete")
        platforms = req.platforms
        cfg = foundation.config
        agent = runtime.agent
        if agent is None:
            raise InvalidError("app: integration runtime has no agent")

        atrinis_cfg = getattr(cfg, "atrinis", None)
        if bool(getattr(atrinis_cfg, "enabled", False)) and _mode(req.mode) != "cli":
            if runtime.atriena_bus is None:
                raise InvalidError("app: integration runtime has no atriena bus")
            tokens = resolve_atrinis_tokens(cfg)
            atrinis_service = _new_atrinis_service(
                cfg, foundation, runtime, platforms, tokens
            )
            runtime.atriena_bus.set_dispatcher(atrinis_service)
            from atri_x.atrinis import new_runtime as new_atrinis_runtime

            platforms.runtimes.append(
                AtrinisRuntimeAdapter(
                    new_atrinis_runtime(getattr(atrinis_cfg, "http", None), atrinis_service)
                )
            )
            if req.profiler is not None:
                req.profiler.mark("atrinis init")

        register_completion_platforms(agent, platforms.runtimes)
        register_command_catalogs(agent, platforms.runtimes)
        register_platform_hooks(agent, platforms.runtimes, req.health)
        if req.profiler is not None:
            req.profiler.mark("platform hooks")
        # 适配器启动前先订阅：连接事实走全局信号，订阅方必须在第一个 Emit 之前就位。
        await maybe_await(agent.start_platform_events(ctx))
        if req.profiler is not None:
            req.profiler.mark("platform events")
        return platforms

    # Go 命名别名。
    Attach = attach


def _mode(value: Any) -> str:
    return str(value or "")


def _new_atrinis_service(
    cfg: Any, foundation: Any, runtime: Any, platforms: PlatformComponents, tokens: dict[str, str]
) -> Any:
    from atri_x.atrinis import Options as AtrinisOptions
    from atri_x.atrinis import new_service as new_atrinis_service

    def send(cctx: Any, target: Any, outputs: Any) -> Any:
        from atri_x.delivery import Notice

        return runtime.agent.send_notice(
            cctx, Notice(target=target, outputs=list(outputs or []))
        )

    def resolve_model(slot: str) -> ModelSelection:
        selected = runtime.agent.current_model_for_mode(slot)
        return ModelSelection(
            provider=str(getattr(selected, "provider", "") or ""),
            model=str(getattr(selected, "model", "") or ""),
        )

    return new_atrinis_service(
        AtrinisOptions(
            media=runtime.media,
            config=getattr(cfg, "atrinis", None),
            sandbox_root=str(getattr(getattr(cfg, "sandbox", None), "root", "") or ""),
            tokens=tokens,
            store=foundation.store,
            logger=foundation.logs.atrinis(),
            enabled_platforms=runtime_names(platforms.runtimes),
            platform_callers=PlatformCallerResolver(platforms.runtimes),
            audit=_audit_func(foundation.logs),
            send=send,
            runner=runtime.agent,
            resolve_model=resolve_model,
        )
    )


def resolve_atrinis_tokens(cfg: Any) -> dict[str, str]:
    """按 ``[atrinis.tokens.<name>].token_env`` 逐个解析令牌值。

    对应 Go ``resolveAtrinisTokens``：同一名字下按顺序取第一个非空的环境变量。
    """
    out: dict[str, str] = {}
    config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
    atrinis_cfg = getattr(cfg, "atrinis", None)
    tokens = getattr(atrinis_cfg, "tokens", None) or {}
    for name, token_cfg in tokens.items():
        name = (name or "").strip()
        if not name:
            continue
        for env_name in getattr(token_cfg, "token_env", None) or []:
            env_name = (env_name or "").strip()
            if not env_name:
                continue
            value = config_env(env_name, config_dir)
            if value is not None and value.strip():
                out[name] = value.strip()
                break
    return out


def _audit_func(logs: Any) -> Any:
    """app 层审计来源的统一入口（补 ``module=app``）。

    与 Go 的 ``auditFunc`` 一致：来源标识是日志契约的一部分，
    ``/audit --module`` 类的筛选依赖它。
    """

    def audit(event: str, **attrs: Any) -> None:
        from atri_x.logging import ModuleApp

        head: dict[str, Any] = {"event": event, "module": ModuleApp}
        head.update(attrs)
        audit_logger = logs.audit()
        emit = getattr(audit_logger, "info", None)
        if callable(emit):
            emit("audit event", **head)

    return audit


# Go 命名别名。
defaultIntegrationFactory = DefaultIntegrationFactory
resolveAtrinisTokens = resolve_atrinis_tokens
runtimeNames = runtime_names
