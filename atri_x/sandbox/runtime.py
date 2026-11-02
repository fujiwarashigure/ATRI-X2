"""沙箱服务在进程内的登记处（唯一实例）。

工具层（shell / 文件 / 发送 / 看图）通过 :func:`get_service` 取当前沙箱服务：
有服务且 ``enabled`` 时按用户安全域执行；没有服务时维持 1.1.9 及以前的宿主行为
（仅用于「显式关闭沙箱」的部署与单元测试）。

**fail-closed**：配置要求 ``mode=container`` 但装配失败时，登记的是一个
:class:`UnavailableService`——所有执行与文件访问都抛「沙箱不可用」，绝不回落宿主。
"""

from __future__ import annotations

from typing import Any

from atri_x.sandbox.errors import SandboxUnavailableError
from atri_x.sandbox.manager import SandboxService, ShellOutcome
from atri_x.sandbox.settings import from_config

__all__ = [
    "UnavailableService",
    "build_service",
    "clear_service",
    "get_service",
    "install",
    "service_or_none",
    "set_service",
]

_SERVICE: Any = None


def get_service() -> Any:
    """当前沙箱服务（``None`` = 未启用沙箱，走宿主旧行为）。"""
    return _SERVICE


def service_or_none() -> Any:
    """当前**启用中**的沙箱服务：``None`` 表示应维持宿主旧行为。"""
    service = _SERVICE
    return service if getattr(service, "enabled", False) else None


def set_service(service: Any) -> None:
    """登记沙箱服务（``None`` = 清除）。"""
    global _SERVICE
    _SERVICE = service


def clear_service() -> None:
    """清除登记（测试/关闭用）。"""
    global _SERVICE
    _SERVICE = None


def install(service: Any) -> Any:
    """登记并返回服务（``None`` 时清除）。"""
    set_service(service)
    return service


def build_service(sandbox_section: Any) -> Any:
    """按 ``[sandbox]`` 配置构造服务；``mode=off`` 返回 ``None``（显式宿主模式）。

    配置解析失败时返回 :class:`UnavailableService` 而不是 ``None``：用户写了
    「要沙箱」却写错了配置，正确行为是明确拒绝执行，不是静默换成宿主 Shell。
    """
    try:
        settings = from_config(sandbox_section)
    except Exception as exc:  # noqa: BLE001 - 配置错误转成不可用服务
        return UnavailableService(f"沙箱配置无效：{exc}")
    if not settings.enabled:
        return None
    try:
        return SandboxService(settings)
    except Exception as exc:  # noqa: BLE001 - 装配失败同样 fail-closed
        return UnavailableService(f"沙箱服务装配失败：{exc}")


class UnavailableService:
    """占位服务：``enabled=True`` 但任何操作都明确报「沙箱不可用」。"""

    enabled = True
    last_error = ""

    def __init__(self, reason: str) -> None:
        self.last_error = str(reason or "沙箱不可用")

    def _fail(self) -> None:
        raise SandboxUnavailableError(
            "沙箱不可用，已拒绝执行（不会回落宿主 Shell）", detail=self.last_error
        )

    async def run_shell(self, ctx: Any, command: str, **kwargs: Any) -> ShellOutcome:
        """拒绝执行。"""
        del ctx, command, kwargs
        self._fail()
        return ShellOutcome()  # pragma: no cover - _fail 必抛

    def tenant_workspace(self, ctx: Any = None) -> str:
        """拒绝文件访问。"""
        del ctx
        self._fail()
        return ""  # pragma: no cover

    def resolve_path(self, ctx: Any, raw_path: str, **kwargs: Any) -> str:
        """拒绝路径解析。"""
        del ctx, raw_path, kwargs
        self._fail()
        return ""  # pragma: no cover

    def export_path(self, ctx: Any, raw_path: str) -> str:
        """拒绝产物导出。"""
        del ctx, raw_path
        self._fail()
        return ""  # pragma: no cover

    async def import_media(self, ctx: Any, source_path: str, **kwargs: Any) -> str:
        """拒绝媒体导入。"""
        del ctx, source_path, kwargs
        self._fail()
        return ""  # pragma: no cover

    async def status(self, *, refresh: bool = False) -> dict:
        """状态里明确写着不可用。"""
        del refresh
        return {"enabled": True, "ok": False, "error": self.last_error}

    def dump(self) -> dict:
        """GUI / doctor 用。"""
        return {"enabled": True, "ok": False, "error": self.last_error}
