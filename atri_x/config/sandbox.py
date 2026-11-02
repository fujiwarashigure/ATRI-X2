"""``[sandbox]`` 段：用户安全域执行环境（沙箱）的配置模型。

对应 1.1.9 之后新增的《ATRI-X2 沙箱功能修改清单》P0 批次。这一段从原来的
``root`` 一项扩展成完整档案：

* **模式与后端**：``mode`` / ``backend`` / ``engine_path`` / ``image``；
* **租户环境**：``profile`` / ``network`` / ``allow_host_files`` / ``require_probe``；
* **资源档案**：CPU / 内存 / 进程数 / 磁盘 / 命令时限 / 空闲回收 / 数据保留；
* **准入队列**：同时运行环境数 / 队列上限 / 单租户排队上限 / 等待上限；
* **网络档案**：每日流量额度 / 上下行速率 / 并发连接 / 授权出口主机；
* **输入输出与审计**：导入导出上限 / 输出截断 / 审计轮转。

默认值口径与仓库其它段一致：这里是「Go 零值 + 有效默认」，数值为 ``0`` 表示
「用资源档案里的默认值」，真正的归一化（夹取、派生路径）在
:mod:`atri_x.sandbox.settings` 里做，不把业务规则塞进配置模型。``root`` 仍由
:meth:`atri_x.config.app.AppConfig.apply_app_defaults` 填默认值
（``<data>/sandbox``），其它路径全部从 ``root`` 派生，不额外占配置字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.config.inspect import StrictConfig

__all__ = ["SandboxConfig"]


@dataclass
class SandboxConfig(StrictConfig):
    """``[sandbox]``：按用户安全域复用的可联网、可重置执行环境。

    ``mode``：

    * ``container``（默认）—— 命令在 Linux 容器里执行；后端不可用时**拒绝执行**，
      不回落宿主 Shell；
    * ``off`` —— 显式恢复 1.1.9 及以前的宿主 Shell 行为（不安全，默认不写、需要
      管理员明确打开）。

    ``backend``：``auto`` / ``docker`` / ``podman``。``auto`` 在 Windows 上优先找
    Docker Desktop，其次 Podman；在 Linux 上优先 rootless Podman，其次 Docker。
    """

    #: 沙箱数据根（租户工作区 / 私有 home / 代理状态 / 审计）；空 = ``<data>/sandbox``。
    root: str = ""
    #: ``container`` = 命令进沙箱；``off`` = 显式恢复宿主 Shell（不安全，仅运维应急）。
    mode: str = "container"
    #: ``auto`` / ``docker`` / ``podman``。
    backend: str = "auto"
    #: 引擎可执行文件路径；空 = 按 ``backend`` 在 PATH 里找。
    engine_path: str = ""
    #: 沙箱镜像名（由 :mod:`atri_x.sandbox.image` 用内置 Dockerfile 构建）。
    image: str = "atri-x-sandbox:1.2.0"
    #: 镜像缺失时是否允许自动 ``docker build``（需要能访问镜像源/基础镜像）。
    image_auto_build: bool = True
    #: 构建镜像时可选的 Debian 镜像源；空 = 官方源（https）。
    #: 网络到官方源很慢时可设为 ``https://mirrors.tuna.tsinghua.edu.cn/debian`` 之类。
    apt_mirror: str = ""
    #: 资源档：``light`` / ``standard`` / ``heavy`` / ``dsh``。
    profile: str = "standard"
    #: 网络档：``offline`` / ``public``（默认，公网出站 + 隔离宿主/内网）/ ``restricted``。
    network: str = "public"
    #: 是否允许文件工具访问租户工作区之外的宿主文件（默认禁止；属于独立授权能力）。
    allow_host_files: bool = False
    #: 启动时是否必须跑通隔离能力探测；``True`` 时探测失败即判定沙箱不可用。
    require_probe: bool = True
    #: 出口代理在沙箱网络内的监听端口（容器内地址 ``<proxy>:<port>``）。
    proxy_port: int = 3128

    # --- 资源档案（0 = 用 profile 默认值） ---------------------------------
    #: 单租户 CPU 上限（核，允许小数）；忙时按权重公平竞争，不保证独占。
    cpu_cores: float = 0.0
    #: CPU 竞争权重（默认 100，与 profile 的 cpu_cores 换算）。
    cpu_weight: int = 0
    #: 内存硬顶（MiB）。
    memory_mb: int = 0
    #: 进程/线程数上限（pids）。
    pids_limit: int = 0
    #: 工作区 + 私有 home 的磁盘配额（MiB）。
    disk_mb: int = 0
    #: inode 数量上限（0 = 用磁盘配额的固定比例）。
    inode_limit: int = 0
    #: 普通命令总时限（秒）。
    command_timeout_seconds: int = 0
    #: 长任务档时限（秒）；``timeout_ms`` 显式超过普通时限时用这一档。
    long_task_timeout_seconds: int = 0
    #: 空闲多久后停止运行环境（秒）；工作区数据保留。
    idle_ttl_seconds: int = 0
    #: 租户环境数据保留期（小时）；超过后清理不活跃租户的数据。
    data_retention_hours: int = 0

    # --- 准入队列（0 = 用默认值） ------------------------------------------
    #: 全局同时运行的环境数上限。
    max_environments: int = 0
    #: 全局排队上限。
    queue_max_size: int = 0
    #: 单租户排队上限（含运行中）。
    queue_max_per_tenant: int = 0
    #: 排队等待上限（秒）；超时返回明确的「排队超时」。
    queue_wait_seconds: int = 0
    #: 后台任务最多连续让出多少轮后强制提升（防饿死）。
    background_starvation_rounds: int = 0

    # --- 网络档案（0 = 用默认值） ------------------------------------------
    #: 每租户每日累计流量额度（MiB，上下行合计）；0 = 用默认 1024。
    daily_traffic_mb: int = 0
    #: 下行（进沙箱）速率上限 KiB/s；0 = 不限。
    download_kbps: int = 0
    #: 上行（出沙箱）速率上限 KiB/s；0 = 不限。
    upload_kbps: int = 0
    #: 单租户并发连接上限。
    max_connections: int = 0
    #: ``restricted`` 网络档允许访问的主机（``host`` 或 ``host:port``）。
    restricted_hosts: list[str] = field(default_factory=list)

    # --- 输入输出与审计 ----------------------------------------------------
    #: 允许文件/媒体导入单个文件的上限（MiB）。
    import_max_mb: int = 0
    #: 允许导出到会话的单个产物上限（MiB）。
    export_max_mb: int = 0
    #: 审计 JSONL 单文件上限（MiB），超限轮转。
    audit_max_mb: int = 0
    #: 命令输出上限（KiB），与宿主 Shell 的截断口径一致。
    max_output_kib: int = 0
