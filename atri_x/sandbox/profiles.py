"""资源/网络档案：沙箱第一版的默认档位表。

修改清单要求「CPU 弹性、内存硬顶」，并且「不在 UI 里把权重误报成固定分配量」。
所以这里把两件事分开表达：

* ``cpu_cores`` —— ``cpu.max`` 的**上限**（容器最多能用多少核），不是保底；
* ``cpu_weight`` —— ``cpu.weight`` 的**竞争权重**（忙时按比例分空闲 CPU），
  不是「保证 1 核」。

网络档分三档：``offline`` / ``public`` / ``restricted``。真正的目标地址过滤发生在
出口代理（:mod:`atri_x.sandbox.egress`）与网络层（租户容器只接内部网络），
本模块只描述策略数值。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from atri_x.sandbox.errors import SandboxDeniedError

__all__ = [
    "NETWORK_PROFILES",
    "PROFILES",
    "NetworkProfile",
    "ResourceProfile",
    "network_profile",
    "profile_names",
    "resource_profile",
]


@dataclass(frozen=True)
class ResourceProfile:
    """一档资源档案。对应容器启动参数的一整套配额。"""

    name: str = "standard"
    #: ``cpu.max`` 上限（核，允许小数）。
    cpu_cores: float = 2.0
    #: ``cpu.weight`` 竞争权重（忙时公平分配空闲 CPU；不是保底核数）。
    cpu_weight: int = 100
    #: 内存硬顶（MiB）—— ``--memory`` 与 ``--memory-swap`` 同时设成它，禁用 swap 增长。
    memory_mb: int = 1024
    #: 进程/线程上限（``--pids-limit``）。
    pids: int = 256
    #: 工作区 + 私有 home 的磁盘配额（MiB）。
    disk_mb: int = 3072
    #: inode 上限；0 = 按磁盘配额推算（每 MiB 约 1024 个）。
    inode_limit: int = 0
    #: 普通命令总时限（秒）。
    command_timeout_s: int = 120
    #: 长任务档时限（秒）：``timeout_ms`` 显式超过普通时限时用这一档，且不能超过它。
    long_task_timeout_s: int = 900
    #: 空闲多久停止运行环境（秒）；工作区数据保留。
    idle_ttl_s: int = 900
    #: 租户数据保留期（小时）：超过后清理不活跃租户的工作区/私有 home。
    data_retention_hours: int = 24

    def dump(self) -> dict:
        """审计/GUI 用的字典（权重与上限分开标注）。"""
        data = asdict(self)
        data["cpu_cores_note"] = "上限（cpu.max）；不是保底分配"
        data["cpu_weight_note"] = "竞争权重（cpu.weight）；忙时按比例分空闲 CPU"
        return data


@dataclass(frozen=True)
class NetworkProfile:
    """一档网络档案。``tier`` 决定出口代理与容器网络形态。"""

    name: str = "public"
    #: ``offline`` / ``public`` / ``restricted``。
    tier: str = "public"
    #: 每租户每日累计流量额度（字节，上下行合计）；0 = 不限。
    daily_bytes: int = 1024 * 1024 * 1024
    #: 下行（进沙箱）速率上限（字节/秒）；0 = 不限。
    download_bps: int = 0
    #: 上行（出沙箱）速率上限（字节/秒）；0 = 不限。
    upload_bps: int = 0
    #: 并发连接上限。
    max_connections: int = 64
    #: ``restricted`` 档允许访问的 ``host`` / ``host:port`` 列表。
    restricted_hosts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def online(self) -> bool:
        """是否需要出口代理与内部网络。"""
        return self.tier in ("public", "restricted")

    def dump(self) -> dict:
        """审计/GUI 用字典。"""
        data = asdict(self)
        data["restricted_hosts"] = list(self.restricted_hosts)
        data["online"] = self.online
        return data


#: 第一版内置资源档。数值是**初始建议值**，后续按实际压测调整（不是别处抄来的标准）。
PROFILES: dict[str, ResourceProfile] = {
    "light": ResourceProfile(
        name="light",
        cpu_cores=0.5,
        cpu_weight=50,
        memory_mb=512,
        pids=128,
        disk_mb=2048,
        command_timeout_s=120,
        idle_ttl_s=900,
        data_retention_hours=24,
    ),
    "standard": ResourceProfile(name="standard"),
    #: 重任务（编译、批量处理）：更大内存与更长时限，仍受硬顶约束。
    "heavy": ResourceProfile(
        name="heavy",
        cpu_cores=4.0,
        cpu_weight=200,
        memory_mb=4096,
        pids=512,
        disk_mb=8192,
        command_timeout_s=600,
        long_task_timeout_s=1800,
        idle_ttl_s=1800,
        data_retention_hours=72,
    ),
    #: DSH / 浏览器渲染这类重任务单独定档，不塞进 512 MiB 档。
    "dsh": ResourceProfile(
        name="dsh",
        cpu_cores=2.0,
        cpu_weight=100,
        memory_mb=2048,
        pids=512,
        disk_mb=4096,
        command_timeout_s=1800,
        long_task_timeout_s=3600,
        idle_ttl_s=1800,
        data_retention_hours=72,
    ),
}

#: 内置网络档。``public`` 是默认档：公网可用，宿主/内网/云元数据由代理与网络层隔离。
NETWORK_PROFILES: dict[str, NetworkProfile] = {
    "offline": NetworkProfile(
        name="offline",
        tier="offline",
        daily_bytes=0,
        max_connections=0,
    ),
    "public": NetworkProfile(name="public"),
    "restricted": NetworkProfile(
        name="restricted",
        tier="restricted",
        daily_bytes=512 * 1024 * 1024,
        max_connections=32,
    ),
}


def profile_names() -> list[str]:
    """全部资源档名（GUI 下拉用，顺序稳定）。"""
    return list(PROFILES)


def resource_profile(name: str) -> ResourceProfile:
    """按名字取资源档；未知档位明确报错，不静默降级。"""
    key = (name or "").strip().lower()
    profile = PROFILES.get(key)
    if profile is None:
        raise SandboxDeniedError(
            f"未知的沙箱资源档 {name!r}（可选：{', '.join(PROFILES)}）"
        )
    return profile


def network_profile(name: str) -> NetworkProfile:
    """按名字取网络档；未知档位明确报错。"""
    key = (name or "").strip().lower()
    profile = NETWORK_PROFILES.get(key)
    if profile is None:
        raise SandboxDeniedError(
            f"未知的沙箱网络档 {name!r}（可选：{', '.join(NETWORK_PROFILES)}）"
        )
    return profile
