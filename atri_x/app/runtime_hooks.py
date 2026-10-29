"""Hook 控制面装配。

对应 Go：``internal/app/runtime.go`` 的 ``buildHookService``。

从 :mod:`atri_x.app.runtime` 拆出（PORTING §2 单文件行数上限）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from atri_x.app.platform_caller import HookPlatformCallerResolver
from atri_x.app.platforms import register_connect_notice_hook, register_cron_platform_hook
from atri_x.app.runtime_support import (
    audit_func,
    log_error as _log_error,
    schedule as _schedule,
)
from atri_x.app.support import maybe_await
from atri_x.config import plugin_config_dir
from atri_x.hook.builtin import Options as HookBuiltinOptions
from atri_x.hook.builtin import RegisterAll as register_builtin_hooks
from atri_x.hook.control import Service as HookControlService
from atri_x.hook.manager import DefaultManager, ReloadReport

__all__ = ["build_hook_service", "buildHookService"]


# ---------------------------------------------------------------------------
# Hook 服务
# ---------------------------------------------------------------------------


async def build_hook_service(
    foundation: Any,
    platforms: Any,
    tool_runtime: Any,
    cron_service: Any,
    hooks: DefaultManager,
    hook_runtime: Any,
    hook_process_env: Any,
    notify_hook_issue: Any,
    send_notice: Any,
) -> HookControlService:
    """对应 Go ``buildHookService``。"""
    from atri_x.app.platforms import register_cron_platform_hook

    cfg = foundation.config
    hook_opts = HookBuiltinOptions(
        config_dir=plugin_config_dir(cfg.config_path),
        tools=tool_runtime.registry,
        logger=foundation.logger,
        audit=audit_func(foundation.logs),
        notify=lambda _ctx, body: _schedule(notify_hook_issue(None, body)),
        send=send_notice,
        platform_callers=HookPlatformCallerResolver(platforms.runtimes),
        runtime=hook_runtime,
        process_env=hook_process_env,
        outbound_messages=foundation.outbound_messages,
        angel_memory=tool_runtime.angel_memory,
        self_learning=tool_runtime.self_learning,
    )

    def load_hooks(registrar: Any) -> tuple[ReloadReport, list[Any]]:
        """对应 Go 的 ``loadHooks``。

        Go 返回 ``(report, configs, err)``；Python 的 ``Loader`` 契约只允许
        ``(report, configs)``，因此注册失败被折进 ``report.notices`` —— 调用方
        （:meth:`HookControlService.hook_reload`）本来就会把 notices 逐条投递，
        行为上等价于 Go 里 "report + err 一起处理"。
        """
        notices: list[str] = []

        def collect(_ctx: Any, body: str) -> None:
            body = (body or "").strip()
            if body:
                notices.append(body)

        load_opts = replace(hook_opts, notify=collect)
        configs: list[Any] = []
        try:
            configs = list(register_builtin_hooks(registrar, load_opts) or [])
        except Exception as exc:  # noqa: BLE001 - 折进 notices 而不是炸掉启动
            notices.append(f"Hook 注册失败：{exc}")
            _log_error(foundation.logger, "hook registration failed", error=str(exc))
        else:
            try:
                register_cron_platform_hook(registrar, cron_service)
            except Exception as exc:  # noqa: BLE001
                notices.append(f"Hook 注册失败：{exc}")
                _log_error(foundation.logger, "hook registration failed", error=str(exc))
            # 1.1.3 修订：上线通知改成内置 Hook，开关在 app.toml [connect_notice]，
            # 这样私聊/群聊两个落点才能各自独立开关（hooks 规则的 enabled 做不到）。
            try:
                register_connect_notice_hook(registrar, cfg)
            except Exception as exc:  # noqa: BLE001
                notices.append(f"Hook 注册失败：{exc}")
                _log_error(
                    foundation.logger, "connect notice hook registration failed", error=str(exc)
                )
        return ReloadReport(notices=notices), configs

    hook_service = HookControlService(hooks, hook_runtime, load_hooks)
    preparer = getattr(hook_runtime, "set_plugin_reload_preparer", None)
    if callable(preparer):
        preparer(hook_service.prepare_plugin_reload)
    try:
        report = await hook_service.hook_reload()
    except Exception as exc:  # noqa: BLE001 - 控制面异常也不能阻断启动
        _log_error(foundation.logger, "hook registration failed", error=str(exc))
        await maybe_await(notify_hook_issue(None, f"Hook 注册失败：{exc}"))
        return hook_service
    for notice in getattr(report, "notices", None) or []:
        await maybe_await(notify_hook_issue(None, notice))
    return hook_service



buildHookService = build_hook_service
