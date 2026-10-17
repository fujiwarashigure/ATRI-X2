"""Hook 控制面：给命令层用的列表 / 热重载 / 启停。

对应 Go：``internal/hook/control/service.go``。

Go 的 ``Reloader`` 是同步的；Python 侧因为 runtime 的 Apply/Stop 是 ``async``，
对外方法一律是协程（列表这类纯查询仍是同步方法）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from atri_x.errors import AtriXError
from atri_x.hook.manager import DefaultManager, Info, ReloadReport

__all__ = ["ControlError", "Loader", "Runtime", "Service", "New"]


class ControlError(AtriXError):
    """控制面错误。对应 Go 里这一层的 ``fmt.Errorf``。"""


#: ``loader(registrar) -> (ReloadReport, list[runtime.Config])``。
Loader = Callable[[Any], tuple[ReloadReport, list[Any]]]


@runtime_checkable
class Runtime(Protocol):
    """有状态 Hook 运行时（:class:`atri_x.hook.runtime.Manager` 满足它）。"""

    async def apply(self, configs: list[Any]) -> None: ...

    async def validate_plugin(self, config: Any) -> None: ...

    async def replace_plugin(self, config: Any) -> None: ...

    def list(self) -> list[Any]: ...

    async def start(self, id: str) -> None: ...

    async def stop(self, ctx: Any, id: str) -> bool: ...

    async def restart(self, ctx: Any, id: str) -> None: ...


class Service:
    """Hook 控制面。对应 Go ``Service``。"""

    def __init__(
        self,
        manager: DefaultManager | None = None,
        runtime: Runtime | None = None,
        loader: Loader | None = None,
    ) -> None:
        self.manager = manager
        self.runtime = runtime
        self.loader = loader
        self._reload_lock: Any = None

    # -- 查询 ---------------------------------------------------------------
    def hook_list(self) -> list[Info]:
        """全部普通 Hook 注册。对应 Go ``HookList``。"""
        if self.manager is None:
            return []
        return self.manager.list()

    def stateful_hooks(self) -> list[Any]:
        """全部有状态 Hook。对应 Go ``StatefulHooks``。"""
        if self.runtime is None:
            return []
        return self.runtime.list()

    # -- 热重载 -------------------------------------------------------------
    async def hook_reload(self) -> ReloadReport:
        """原子替换全部普通 Hook。对应 Go ``HookReload``。"""
        if self.manager is None or self.loader is None:
            raise ControlError("hook reloader is not configured")
        async with self._reload_guard():
            return await self._hook_reload()

    async def _hook_reload(self) -> ReloadReport:
        if self.manager is None or self.loader is None:
            raise ControlError("hook reloader is not configured")
        candidate = DefaultManager()
        report, configs = self.loader(candidate)
        if configs and self.runtime is None:
            raise ControlError("stateful hook runtime is not configured")
        if self.runtime is not None:
            await self.runtime.apply(configs)
        self.manager.replace(candidate)
        return report

    async def prepare_plugin_reload(self, id: str) -> Callable[[], Awaitable[None]]:
        """只热重载某个持久插件，返回提交函数。对应 Go ``PreparePluginReload``。"""
        if (
            self.manager is None
            or self.runtime is None
            or self.loader is None
        ):
            raise ControlError("hook plugin reloader is not configured")
        ident = (id or "").strip()
        if not ident:
            raise ControlError("hook plugin id is required")
        candidate = DefaultManager()
        async with self._reload_guard():
            report, configs = self.loader(candidate)
        config = None
        for item in configs:
            if getattr(item, "id", "") == ident:
                config = item
                break
        if config is None:
            detail = "\n".join(
                notice.strip() for notice in (report.notices or []) if notice.strip()
            )
            if detail:
                raise ControlError(
                    f"stateful hook {ident!r} is missing from reloaded config: {detail}"
                )
            raise ControlError(
                f"stateful hook {ident!r} is missing from reloaded config"
            )
        await self.runtime.validate_plugin(config)

        async def commit() -> None:
            async with self._reload_guard():
                await self.runtime.replace_plugin(config)
                self.manager.replace_plugin(ident, candidate)

        return commit

    # -- 启停 ---------------------------------------------------------------
    async def start_stateful_hook(self, id: str) -> None:
        """启动一个有状态 Hook。对应 Go ``StartStatefulHook``。"""
        if self.runtime is None:
            raise ControlError("stateful hook runtime is not configured")
        await self.runtime.start(id)

    async def stop_stateful_hook(self, ctx: Any, id: str) -> None:
        """停止一个有状态 Hook。对应 Go ``StopStatefulHook``。"""
        await self.stop_hook(ctx, id)

    async def stop_hook(self, ctx: Any, id: str) -> bool:
        """按名/插件 ID 停止 Hook。对应 Go ``StopHook``。"""
        if self.manager is None:
            raise ControlError("hook service is not configured")
        matched = self.manager.matches(id)
        stopped = self.manager.stop(id)
        if self.runtime is not None:
            try:
                runtime_stopped = await self.runtime.stop(ctx, id)
            except AtriXError as exc:
                if not matched:
                    raise
                runtime_stopped = False
                _ = exc
            else:
                matched = matched or runtime_stopped
        return matched or stopped > 0

    async def restart_stateful_hook(self, ctx: Any, id: str) -> None:
        """重启一个有状态 Hook。对应 Go ``RestartStatefulHook``。"""
        if self.runtime is None:
            raise ControlError("stateful hook runtime is not configured")
        await self.runtime.restart(ctx, id)

    # -- 内部 ---------------------------------------------------------------
    def _reload_guard(self) -> Any:
        """串行化热重载（对应 Go 的 ``reloadMu``）。"""
        import asyncio

        lock = self._reload_lock
        if lock is None:
            lock = asyncio.Lock()
            self._reload_lock = lock
        return lock


def New(
    manager: DefaultManager | None = None,
    runtime: Runtime | None = None,
    loader: Loader | None = None,
) -> Service:
    """对应 Go ``New``。"""
    return Service(manager, runtime, loader)
