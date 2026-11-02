"""沙箱后端抽象：把 Docker / Podman / 未来 Windows 原生后端的差异收在一层接口后面。

工具层与 :class:`atri_x.sandbox.manager.SandboxService` 只依赖这里的数据类与
:class:`SandboxBackend` 协议，不出现任何引擎命令；后端只接受**受控参数**
（镜像、资源档案、网络档案由控制面决定），模型无法指定挂载、特权模式或容器启动参数。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from atri_x.sandbox.network import EgressSpec
from atri_x.sandbox.profiles import NetworkProfile, ResourceProfile
from atri_x.sandbox.tenant import TenantEnvPaths, TenantKey

__all__ = [
    "BackendInfo",
    "CapabilityCheck",
    "CapabilityReport",
    "EnvInfo",
    "EnvRequest",
    "ExecResult",
    "SandboxBackend",
    "ShellOutcome",
]


@dataclass
class CapabilityCheck:
    """一项隔离能力探测结果。"""

    name: str = ""
    ok: bool = False
    detail: str = ""
    required: bool = True

    def dump(self) -> dict:
        """字典形态。"""
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass
class CapabilityReport:
    """后端 + 隔离能力的实测结论（**不是**「配置已开启」）。"""

    backend: str = ""
    engine_version: str = ""
    image: str = ""
    ok: bool = False
    checked_at: str = ""
    checks: list[CapabilityCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[CapabilityCheck]:
        """失败的必需检查项。"""
        return [item for item in self.checks if item.required and not item.ok]

    def dump(self) -> dict:
        """GUI / doctor 用字典。"""
        return {
            "backend": self.backend,
            "engine_version": self.engine_version,
            "image": self.image,
            "ok": self.ok,
            "checked_at": self.checked_at,
            "checks": [item.dump() for item in self.checks],
            "warnings": list(self.warnings),
        }


@dataclass
class BackendInfo:
    """后端可用性快照。"""

    backend: str = ""
    engine: str = ""
    version: str = ""
    available: bool = False
    error: str = ""

    def dump(self) -> dict:
        """字典形态。"""
        return {
            "backend": self.backend,
            "engine": self.engine,
            "version": self.version,
            "available": self.available,
            "error": self.error,
        }


@dataclass
class ExecResult:
    """一次容器内执行的结果（输出已在后端侧截断）。"""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0
    error_kind: str = ""

    @property
    def ok(self) -> bool:
        """退出码为 0 且没有后端错误。"""
        return self.exit_code == 0 and not self.error_kind


@dataclass
class ShellOutcome:
    """一次沙箱 shell 执行的结果（与宿主 Shell 的 ShellData 对齐）。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    tenant: str = ""
    truncated: bool = False


@dataclass
class EnvRequest:
    """创建一个租户执行环境所需的全部受控参数。"""

    tenant: TenantKey = field(default_factory=lambda: TenantKey())
    paths: TenantEnvPaths = field(default_factory=lambda: TenantEnvPaths())
    image: str = ""
    resources: ResourceProfile = field(default_factory=ResourceProfile)
    network: NetworkProfile = field(default_factory=NetworkProfile)
    egress: EgressSpec | None = None
    proxy_port: int = 3128
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class EnvInfo:
    """后端视角的一个环境。"""

    name: str = ""
    tenant_slug: str = ""
    running: bool = False
    created_at: str = ""
    image: str = ""
    network: str = ""
    proxy: str = ""

    def dump(self) -> dict:
        """字典形态。"""
        return {
            "name": self.name,
            "tenant": self.tenant_slug,
            "running": self.running,
            "created_at": self.created_at,
            "image": self.image,
            "network": self.network,
            "proxy": self.proxy,
        }


@dataclass
class EnvStats:
    """环境资源占用（近似值，来自引擎统计）。"""

    cpu_percent: float = 0.0
    memory_bytes: int = 0
    memory_limit_bytes: int = 0
    pids: int = 0
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0

    def dump(self) -> dict:
        """字典形态。"""
        return {
            "cpu_percent": self.cpu_percent,
            "memory_bytes": self.memory_bytes,
            "memory_limit_bytes": self.memory_limit_bytes,
            "pids": self.pids,
            "net_rx_bytes": self.net_rx_bytes,
            "net_tx_bytes": self.net_tx_bytes,
        }


class SandboxBackend(ABC):
    """执行后端协议。所有实现必须是「失败即拒绝」，不得回落宿主执行。"""

    #: 稳定后端名（``docker`` / ``podman`` / ``wsl`` / ``unavailable``）。
    name: str = "unknown"

    @abstractmethod
    async def backend_info(self) -> BackendInfo:
        """探测引擎是否存在、守护进程是否可达。"""

    @abstractmethod
    async def probe(self) -> CapabilityReport:
        """实测隔离能力；任何必需项失败都必须是不可用。"""

    @abstractmethod
    async def ensure_image(self) -> str:
        """确保镜像存在（必要时按内置 Dockerfile 构建），返回镜像名。"""

    @abstractmethod
    async def create_env(self, request: EnvRequest) -> EnvInfo:
        """创建（或复用）一个租户环境；失败抛后端错误。"""

    @abstractmethod
    async def start_env(self, name: str) -> None:
        """启动环境。"""

    @abstractmethod
    async def stop_env(self, name: str, *, grace_seconds: float = 5.0) -> None:
        """停止环境并杀掉其中全部进程。"""

    @abstractmethod
    async def remove_env(self, name: str, *, proxy_name: str = "") -> None:
        """销毁环境（含代理）但保留宿主上的租户数据目录。"""

    @abstractmethod
    async def exec_in_env(
        self,
        name: str,
        command: str,
        *,
        timeout: float,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 256 * 1024,
    ) -> ExecResult:
        """在环境内执行一条 shell 命令（整棵进程树受限、超时即杀）。"""

    @abstractmethod
    async def env_stats(self, name: str) -> EnvStats:
        """读取环境资源占用。"""

    @abstractmethod
    async def list_envs(self) -> list[EnvInfo]:
        """列出全部沙箱环境（按标签过滤）。"""

    @abstractmethod
    async def set_online(self, name: str, online: bool, network: NetworkProfile) -> None:
        """联网/断网（流量配额用尽时由控制面强制断开）。"""

    @abstractmethod
    async def cleanup_orphans(self, known_envs: set[str], known_proxies: set[str]) -> int:
        """回收崩溃/重启后遗留的容器与网络，返回清理数量。"""

    async def aclose(self) -> None:
        """释放后端自身资源（默认无操作）。"""
        return None

    def dump(self) -> dict[str, Any]:
        """GUI/doctor 用描述。"""
        return {"backend": self.name}
