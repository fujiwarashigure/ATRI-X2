"""租户环境记录与创建参数：把「租户 + 档案 + 配额」翻译成后端请求。

控制面只在这里拼 EnvRequest（镜像、资源档案、网络档案、代理参数），
模型无法参与；后端拿到的都是已经校验过的受控字段。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x.sandbox.backend import EnvRequest
from atri_x.sandbox.files import disk_usage_bytes, ensure_tenant_dirs
from atri_x.sandbox.network import EgressSpec, spec_for
from atri_x.sandbox.profiles import NetworkProfile, ResourceProfile
from atri_x.sandbox.queue import Lease
from atri_x.sandbox.settings import SandboxSettings
from atri_x.sandbox.tenant import TenantEnvPaths, TenantKey

__all__ = ["EnvRecord", "build_env_request", "check_disk_quota"]


@dataclass
class EnvRecord:
    """一个租户在控制面里的环境状态（容器本身由后端管理）。"""

    tenant: TenantKey = field(default_factory=lambda: TenantKey())
    paths: TenantEnvPaths = field(default_factory=lambda: TenantEnvPaths())
    name: str = ""
    proxy_name: str = ""
    network_name: str = ""
    lease: Lease | None = None
    running: bool = False
    busy: int = 0
    last_used: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    proxy_up_bytes: int = 0
    proxy_down_bytes: int = 0
    online: bool = True
    profile: str = "standard"
    network: str = "public"

    @property
    def idle_seconds(self) -> float:
        """空闲秒数（GUI / GC 用）。"""
        return max(0.0, time.time() - self.last_used)

    def dump(self) -> dict:
        """GUI / 审计用快照（不含宿主绝对路径）。"""
        return {
            "tenant": self.tenant.display,
            "slug": self.tenant.slug,
            "container": self.name,
            "running": self.running,
            "busy": self.busy,
            "idle_seconds": int(self.idle_seconds),
            "profile": self.profile,
            "network": self.network,
            "online": self.online,
            "proxy_up_bytes": self.proxy_up_bytes,
            "proxy_down_bytes": self.proxy_down_bytes,
        }


def build_env_request(
    settings: SandboxSettings,
    tenant: TenantKey,
    paths: TenantEnvPaths,
    *,
    remaining_quota_bytes: int,
) -> EnvRequest:
    """组装一个受控的环境创建请求（网络档 → 出口参数 → 后端请求）。"""
    network = settings.network_policy
    egress: EgressSpec | None = spec_for(
        tenant_slug=tenant.slug,
        proxy_name=tenant.proxy_name,
        network_name=tenant.network_name,
        proxy_state_dir=paths.proxy_state,
        network=network,
        proxy_port=settings.proxy_port,
        image=settings.image,
        remaining_bytes=remaining_quota_bytes,
    )
    labels = {
        "atri-x.profile": settings.profile,
        "atri-x.network": network.name,
    }
    return EnvRequest(
        tenant=tenant,
        paths=paths,
        image=settings.image,
        resources=ResourceProfile(
            name=settings.resources.name,
            cpu_cores=settings.resources.cpu_cores,
            cpu_weight=settings.resources.cpu_weight,
            memory_mb=settings.resources.memory_mb,
            pids=settings.resources.pids,
            disk_mb=settings.resources.disk_mb,
            inode_limit=settings.resources.inode_limit,
            command_timeout_s=settings.resources.command_timeout_s,
            long_task_timeout_s=settings.resources.long_task_timeout_s,
            idle_ttl_s=settings.resources.idle_ttl_s,
            data_retention_hours=settings.resources.data_retention_hours,
        ),
        network=NetworkProfile(
            name=network.name,
            tier=network.tier,
            daily_bytes=network.daily_bytes,
            download_bps=network.download_bps,
            upload_bps=network.upload_bps,
            max_connections=network.max_connections,
            restricted_hosts=network.restricted_hosts,
        ),
        egress=egress,
        proxy_port=settings.proxy_port,
        labels=labels,
    )


def check_disk_quota(settings: SandboxSettings, paths: TenantEnvPaths) -> None:
    """检查租户工作区 + 私有 home 是否接近磁盘配额（超 95% 拒绝执行）。"""
    limit = int(settings.resources.disk_mb) * 1024 * 1024
    if limit <= 0:
        return
    used = 0
    for directory in (paths.workspace, paths.home):
        if os.path.isdir(directory):
            used += disk_usage_bytes(directory)
    if used >= limit:
        from atri_x.sandbox.errors import SandboxQuotaError

        raise SandboxQuotaError(
            "沙箱磁盘配额已用尽",
            detail=f"已用 {used / 1048576:.0f} MiB / 配额 {settings.resources.disk_mb} MiB；"
            "可用 /sandbox_reset wipe 清空工作区",
        )
    return None


def ensure_dirs(paths: TenantEnvPaths) -> None:
    """创建租户目录。"""
    ensure_tenant_dirs(paths)


def record_for(settings: SandboxSettings, tenant: TenantKey) -> EnvRecord:
    """按设置与租户构造（尚未启动的）环境记录。"""
    paths = TenantEnvPaths.for_root(settings.root, tenant)
    return EnvRecord(
        tenant=tenant,
        paths=paths,
        name=tenant.container_name,
        proxy_name=tenant.proxy_name,
        network_name=tenant.network_name,
        profile=settings.profile,
        network=settings.network,
        online=settings.network_policy.online,
    )


def as_dict(record: EnvRecord) -> dict[str, Any]:
    """供 ``SandboxService.status`` 使用的记录视图。"""
    return record.dump()
