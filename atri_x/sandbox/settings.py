"""沙箱运行配置：把 ``[sandbox]`` 段归一化成可直接使用的不可变设置。

配置模型（:class:`atri_x.config.sandbox.SandboxConfig`）只描述字段与默认值；
这里负责三件事：

1. **解析档位**：``profile`` / ``network`` 映射成 :class:`ResourceProfile` /
   :class:`NetworkProfile`，配置里的非零数值覆盖档位默认值（0 = 用档位默认）；
2. **夹取**：内存/磁盘/时限等做上下界夹取，避免一条手写配置把宿主拖垮；
3. **派生路径**：租户根、审计、账本、镜像构建上下文全部从 ``root`` 派生。

``from_config`` 接受**鸭子类型**（任何带同名属性的对象），因此单元测试可以直接传
``SimpleNamespace``，不依赖完整 ``AppConfig``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.sandbox.errors import SandboxDeniedError
from atri_x.sandbox.profiles import (
    NetworkProfile,
    ResourceProfile,
    network_profile,
    resource_profile,
)

__all__ = [
    "MODES",
    "SandboxSettings",
    "clamp",
    "from_config",
]

MODES = ("container", "off")
BACKENDS = ("auto", "docker", "podman")
_MIB = 1024 * 1024
_KIB = 1024


@dataclass(frozen=True)
class SandboxSettings:
    """一次装配期解析好的沙箱设置（之后不再随配置文件变化）。"""

    # --- 模式与后端 --------------------------------------------------------
    mode: str = "container"
    backend: str = "auto"
    engine_path: str = ""
    image: str = "atri-x-sandbox:1.2.0"
    image_auto_build: bool = True
    apt_mirror: str = ""
    require_probe: bool = True
    proxy_port: int = 3128
    # --- 租户策略 ----------------------------------------------------------
    profile: str = "standard"
    network: str = "public"
    allow_host_files: bool = False
    # --- 派生路径 ----------------------------------------------------------
    root: str = ""
    tenants_root: str = ""
    ledger_path: str = ""
    audit_path: str = ""
    image_context_dir: str = ""
    # --- 资源档案（已归一） ------------------------------------------------
    resources: ResourceProfile = field(default_factory=ResourceProfile)
    network_policy: NetworkProfile = field(default_factory=NetworkProfile)
    # --- 准入队列 ----------------------------------------------------------
    max_environments: int = 4
    queue_max_size: int = 64
    queue_max_per_tenant: int = 4
    queue_wait_seconds: float = 300.0
    background_starvation_rounds: int = 8
    # --- 输入输出/审计 -----------------------------------------------------
    import_max_bytes: int = 64 * _MIB
    export_max_bytes: int = 256 * _MIB
    audit_max_bytes: int = 8 * _MIB
    max_output_bytes: int = 256 * 1024

    @property
    def enabled(self) -> bool:
        """是否启用沙箱执行（``mode=container``）。"""
        return self.mode == "container"

    @property
    def online(self) -> bool:
        """默认网络档是否需要出口代理。"""
        return self.network_policy.online

    def dump(self) -> dict:
        """GUI / doctor / 审计用的只读快照（不含引擎凭据）。"""
        return {
            "mode": self.mode,
            "backend": self.backend,
            "engine_path": self.engine_path,
            "image": self.image,
            "profile": self.profile,
            "network": self.network,
            "allow_host_files": self.allow_host_files,
            "require_probe": self.require_probe,
            "root": self.root,
            "resources": self.resources.dump(),
            "network_policy": self.network_policy.dump(),
            "queue": {
                "max_environments": self.max_environments,
                "queue_max_size": self.queue_max_size,
                "queue_max_per_tenant": self.queue_max_per_tenant,
                "queue_wait_seconds": self.queue_wait_seconds,
            },
            "limits": {
                "import_max_bytes": self.import_max_bytes,
                "export_max_bytes": self.export_max_bytes,
                "audit_max_bytes": self.audit_max_bytes,
                "max_output_bytes": self.max_output_bytes,
            },
        }


def clamp(value: Any, low: int, high: int, default: int) -> int:
    """把 ``value`` 夹到 ``[low, high]``；非数字或 0 用 ``default``。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number == 0:
        number = int(default)
    return max(low, min(high, number))


def from_config(section: Any = None, *, root_override: str = "") -> SandboxSettings:
    """把 ``[sandbox]`` 配置段归一化成 :class:`SandboxSettings`。

    ``section`` 为 ``None`` 时按默认档构造（测试与未配置场景）；``root_override``
    用来在测试/嵌入式场景指定数据根。
    """
    get = _reader(section)
    mode = str(get("mode", "container") or "container").strip().lower()
    if mode not in MODES:
        raise SandboxDeniedError(
            f"[sandbox].mode 只能是 {' / '.join(MODES)}，当前为 {mode!r}"
        )
    backend = str(get("backend", "auto") or "auto").strip().lower()
    if backend not in BACKENDS:
        raise SandboxDeniedError(
            f"[sandbox].backend 只能是 {' / '.join(BACKENDS)}，当前为 {backend!r}"
        )
    profile = resource_profile(str(get("profile", "standard") or "standard"))
    network = network_profile(str(get("network", "public") or "public"))

    # 配置里的非零值覆盖档位默认值。
    resources = ResourceProfile(
        name=profile.name,
        cpu_cores=_float_or(get("cpu_cores", 0.0), profile.cpu_cores, 0.1, 32.0),
        cpu_weight=clamp(get("cpu_weight", 0), 10, 10000, profile.cpu_weight),
        memory_mb=clamp(get("memory_mb", 0), 128, 65536, profile.memory_mb),
        pids=clamp(get("pids_limit", 0), 16, 65536, profile.pids),
        disk_mb=clamp(get("disk_mb", 0), 64, 102400, profile.disk_mb),
        inode_limit=clamp(get("inode_limit", 0), 0, 100_000_000, profile.inode_limit),
        command_timeout_s=clamp(
            get("command_timeout_seconds", 0), 5, 86400, profile.command_timeout_s
        ),
        long_task_timeout_s=clamp(
            get("long_task_timeout_seconds", 0),
            profile.command_timeout_s,
            7 * 86400,
            profile.long_task_timeout_s,
        ),
        idle_ttl_s=clamp(get("idle_ttl_seconds", 0), 60, 7 * 86400, profile.idle_ttl_s),
        data_retention_hours=clamp(
            get("data_retention_hours", 0), 1, 24 * 90, profile.data_retention_hours
        ),
    )
    restricted = tuple(
        str(item).strip() for item in (get("restricted_hosts", []) or []) if str(item).strip()
    )
    network_policy = NetworkProfile(
        name=network.name,
        tier=network.tier,
        daily_bytes=clamp(
            get("daily_traffic_mb", 0), 0, 1024 * 1024, network.daily_bytes // _MIB
        )
        * _MIB,
        download_bps=clamp(get("download_kbps", 0), 0, 100 * 1024 * 1024, network.download_bps // _KIB)
        * _KIB,
        upload_bps=clamp(get("upload_kbps", 0), 0, 100 * 1024 * 1024, network.upload_bps // _KIB)
        * _KIB,
        max_connections=clamp(
            get("max_connections", 0), 0, 4096, network.max_connections
        ),
        restricted_hosts=restricted or network.restricted_hosts,
    )

    root = (root_override or str(get("root", "") or "")).strip()
    if not root:
        raise SandboxDeniedError(
            "沙箱数据根为空：应由 AppConfig.apply_app_defaults 填 <data>/sandbox，"
            "或显式传入 root_override"
        )
    root = os.path.abspath(os.path.normpath(root))
    default_envs = _default_max_environments()
    return SandboxSettings(
        mode=mode,
        backend=backend,
        engine_path=str(get("engine_path", "") or "").strip(),
        image=str(get("image", "atri-x-sandbox:1.2.0") or "atri-x-sandbox:1.2.0").strip(),
        image_auto_build=bool(get("image_auto_build", True)),
        apt_mirror=str(get("apt_mirror", "") or "").strip(),
        require_probe=bool(get("require_probe", True)),
        proxy_port=clamp(get("proxy_port", 0), 1024, 65535, 3128),
        profile=profile.name,
        network=network_policy.name,
        allow_host_files=bool(get("allow_host_files", False)),
        root=root,
        tenants_root=os.path.join(root, "tenants"),
        ledger_path=os.path.join(root, "usage-ledger.json"),
        audit_path=os.path.join(root, "audit", "sandbox.jsonl"),
        image_context_dir=os.path.join(root, "image-context"),
        resources=resources,
        network_policy=network_policy,
        max_environments=clamp(get("max_environments", 0), 1, 64, default_envs),
        queue_max_size=clamp(get("queue_max_size", 0), 1, 10000, 64),
        queue_max_per_tenant=clamp(get("queue_max_per_tenant", 0), 1, 64, 4),
        queue_wait_seconds=float(
            clamp(get("queue_wait_seconds", 0), 1, 3600, 300)
        ),
        background_starvation_rounds=clamp(
            get("background_starvation_rounds", 0), 1, 1000, 8
        ),
        import_max_bytes=clamp(get("import_max_mb", 0), 1, 1024 * 1024, 64) * _MIB,
        export_max_bytes=clamp(get("export_max_mb", 0), 1, 1024 * 1024, 256) * _MIB,
        audit_max_bytes=clamp(get("audit_max_mb", 0), 1, 1024, 8) * _MIB,
        max_output_bytes=clamp(get("max_output_kib", 0), 16, 65536, 256) * 1024,
    )


def _reader(section: Any) -> Any:
    """返回 ``name -> value`` 的读取函数（缺段返回默认）。"""

    def get(name: str, default: Any) -> Any:
        if section is None:
            return default
        value = getattr(section, name, None)
        return default if value is None else value

    return get


def _float_or(value: Any, default: float, low: float, high: float) -> float:
    """浮点夹取：非数字或 0 用 ``default``。"""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if number <= 0:
        number = float(default)
    return max(low, min(high, number))


def _default_max_environments() -> int:
    """同时运行环境数的默认值：按 CPU 核数的一半，夹在 2..8。"""
    try:
        cpus = int(os.cpu_count() or 4)
    except Exception:  # noqa: BLE001 - 取不到就按 4 核估
        cpus = 4
    return max(2, min(8, cpus // 2 or 2))
