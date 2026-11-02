"""网络档案的落地参数：出口代理如何被创建、如何注入到租户环境。

分层原则（修改清单 §6）：

* **网络层**：租户容器只接一个 Docker ``internal`` 网络，没有默认路由、没有 DNS
  转发，因此直接连公网 / 内网 / 宿主机在路由层就失败；
* **出口服务**：唯一跨边界的通道是同一网络里的出口代理容器（它同时接 ``bridge``），
  代理按目标地址做 deny/allow、限速、限并发、记流量；
* ``HTTP_PROXY`` 等环境变量只是**告诉程序走代理**，即使程序不理会它们，也依然
  无法直连——真正的限制在网络层。

本模块只做纯数据与参数拼装，不执行引擎命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.sandbox.errors import SandboxDeniedError
from atri_x.sandbox.profiles import NetworkProfile

__all__ = [
    "DENIED_PORTS",
    "EgressSpec",
    "build_proxy_env",
    "normalize_allowed_hosts",
]

#: 即使授权出口也不放行的端口：容器引擎 / 编排 / 运维管理面 / 邮件滥用面。
DENIED_PORTS: frozenset[int] = frozenset(
    {
        22,  # ssh
        25, 465, 587,  # smtp（防垃圾邮件滥用）
        2375, 2376,  # docker API
        2379, 2380,  # etcd
        6443, 10250, 10255,  # kubernetes
        5000,  # docker registry 未加密（管理器端口）
        9090,  # 常见管理面
    }
)


@dataclass(frozen=True)
class EgressSpec:
    """一个租户的出口代理参数（由控制面计算，模型不可指定）。"""

    tenant_slug: str = ""
    proxy_name: str = ""
    network_name: str = ""
    proxy_state_dir: str = ""
    proxy_port: int = 3128
    #: 每日流量额度（字节）；0 = 不限（仅 ``offline`` 档才会是 0）。
    daily_limit_bytes: int = 0
    download_bps: int = 0
    upload_bps: int = 0
    max_connections: int = 64
    #: ``restricted`` 档的授权主机；``public`` 档为空表示「除禁止段外都允许」。
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    image: str = ""

    @property
    def restricted(self) -> bool:
        """是否处于「只允许指定主机」模式。"""
        return bool(self.allowed_hosts)

    def proxy_env(self) -> dict[str, str]:
        """传给代理容器的环境变量（``ATRIX_EGRESS_*``）。"""
        env = {
            "ATRIX_EGRESS_PORT": str(int(self.proxy_port)),
            "ATRIX_EGRESS_STATE": "/state/stats.json",
            "ATRIX_EGRESS_TENANT": self.tenant_slug,
            "ATRIX_EGRESS_DAILY_BYTES": str(max(0, int(self.daily_limit_bytes))),
            "ATRIX_EGRESS_DOWN_BPS": str(max(0, int(self.download_bps))),
            "ATRIX_EGRESS_UP_BPS": str(max(0, int(self.upload_bps))),
            "ATRIX_EGRESS_MAX_CONNS": str(max(1, int(self.max_connections))),
            "ATRIX_EGRESS_ALLOW_HOSTS": ",".join(self.allowed_hosts),
        }
        return env

    def dump(self) -> dict:
        """审计/GUI 用字典（不含宿主路径）。"""
        return {
            "tenant_slug": self.tenant_slug,
            "proxy_name": self.proxy_name,
            "network": self.network_name,
            "port": self.proxy_port,
            "daily_limit_bytes": self.daily_limit_bytes,
            "download_bps": self.download_bps,
            "upload_bps": self.upload_bps,
            "max_connections": self.max_connections,
            "restricted_hosts": list(self.allowed_hosts),
        }


def build_proxy_env(spec: EgressSpec, *, host: str) -> dict[str, str]:
    """租户容器里的代理环境变量（只是「配置」；强制点在网络层与代理本身）。"""
    proxy_url = f"http://{host}:{int(spec.proxy_port)}"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": "",
        "no_proxy": "",
        "GIT_HTTP_PROXY": proxy_url,
        "GIT_HTTPS_PROXY": proxy_url,
    }


def normalize_allowed_hosts(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """归一化 ``restricted_hosts``：``host`` 或 ``host:port``，去重保序。"""
    out: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text.startswith(("http://", "https://")):
            raise SandboxDeniedError(
                f"restricted_hosts 只写 host 或 host:port，不要带协议：{raw!r}"
            )
        if "/" in text:
            raise SandboxDeniedError(
                f"restricted_hosts 只写 host 或 host:port，不要带路径：{raw!r}"
            )
        if text not in out:
            out.append(text)
    return tuple(out)


def spec_for(
    *,
    tenant_slug: str,
    proxy_name: str,
    network_name: str,
    proxy_state_dir: str,
    network: NetworkProfile,
    proxy_port: int,
    image: str,
    remaining_bytes: int,
) -> EgressSpec | None:
    """按网络档构造出口参数；``offline`` 档返回 ``None``（不创建代理）。"""
    if not network.online:
        return None
    return EgressSpec(
        tenant_slug=tenant_slug,
        proxy_name=proxy_name,
        network_name=network_name,
        proxy_state_dir=proxy_state_dir,
        proxy_port=int(proxy_port),
        daily_limit_bytes=max(0, int(remaining_bytes)),
        download_bps=max(0, int(network.download_bps)),
        upload_bps=max(0, int(network.upload_bps)),
        max_connections=max(1, int(network.max_connections)),
        allowed_hosts=normalize_allowed_hosts(list(network.restricted_hosts)),
        image=image,
    )
