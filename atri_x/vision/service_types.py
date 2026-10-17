"""视觉服务的输入/输出/配置类型。

对应 Go：``internal/vision/service.go`` 里的 ``Options`` / ``Prompt`` /
``Request`` / ``Result`` / ``Stats``。

拆分来源：Go 的 ``service.go`` 是 410 行一个文件，类型与 ``Service`` 混在一起；
Python 侧受「单文件 ≤ 600 行」与 import 方向约束，把纯数据类型抽到本模块，
:mod:`atri_x.vision.service` 再原样再导出，调用方仍可用
``atri_x.vision.service.Result`` 或 ``atri_x.vision.Result``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Options", "Prompt", "Request", "Result", "Stats"]


@dataclass
class Options:
    """配置一个 :class:`atri_x.vision.service.Service`。对应 Go ``Options``。

    零值不可用；:func:`atri_x.vision.service.new` 会给未设置的字段填上默认值。
    ``timeout`` / ``cache_ttl`` / ``negative_ttl`` / ``shared_timeout`` 单位是
    **秒**（Go 为 ``time.Duration``）。三个 ``max*`` 上限的语义：``0`` 用默认，
    **负数**表示不限。
    """

    client: Any = None
    provider: str = ""
    endpoint: str = ""
    model: str = ""
    max_tokens: int = 0
    temperature: float = 0.0
    max_edge: int = 0
    max_image_bytes: int = 0
    timeout: float = 0.0
    #: 累计流式答案的上限（默认 64 KiB）。
    max_output_bytes: int = 0

    cache_ttl: float = 0.0
    cache_max_entries: int = 0
    max_cache_value_size: int = 0
    negative_ttl: float = 0.0
    negative_max: int = 0
    shared_timeout: float = 0.0

    #: 同时在跑的上游任务上限（默认 ``DefaultMaxConcurrentJobs``）。负数表示不限。
    max_concurrent_jobs: int = 0
    #: 等待 runner 槽位的任务上限（默认 ``DefaultMaxJobQueue``）。负数表示不限，
    #: 此时任务只等自己的上下文。
    max_job_queue: int = 0
    #: 合并到同一个在途任务的调用者上限（默认 ``DefaultMaxJobWaiters``）。负数表示不限。
    max_job_waiters: int = 0

    #: 设置后，它被取消时所有在途共享任务一并取消。嵌入方传进程/关闭上下文，
    #: 这样慢上游在关闭时会被停掉，而不是跑到 ``shared_timeout``。
    base_context: Any = None

    #: 凭据/账号变更时递增，使缓存失效，而**不必**把凭据放进键里。
    credential_epoch: int = 0

    metrics: Any = None
    now: Callable[[], float] | None = None


@dataclass
class Prompt:
    """一段描述模板。对应 Go ``Prompt``。

    ``version`` 必须在 ``system`` / ``user`` 发生会改变输出形态的修改时同步
    变更；它与实际文本的哈希一起进入缓存指纹。
    """

    version: str = ""
    system: str = ""
    user: str = ""


@dataclass
class Request:
    """一张待描述的图片。对应 Go ``Request``。"""

    media_id: str = ""
    data: bytes = b""
    mime_type: str = ""
    prompt: Prompt = field(default_factory=Prompt)


@dataclass
class Result:
    """一次成功的描述。对应 Go ``Result``。

    ``provider`` / ``model`` 说明**实际**产出它的模型，这样调用方永远不会把
    兜底结果当成主模型的输出。
    """

    text: str = ""
    provider: str = ""
    model: str = ""
    cached: bool = False


@dataclass
class Stats:
    """一个 Service 的进程内运维视图。对应 Go ``Stats``。

    只有计数、上限与限流器占用：没有图像字节、media ID、缓存键或凭据，因此
    可以安全地从运维 ``/metrics`` 端点对外提供。JSON 键名逐字对齐 Go struct tag。
    """

    cache: dict[str, int] = field(default_factory=dict)
    coalesced: int = 0
    jobs: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    active_jobs: int = 0
    queued_jobs: int = 0
    max_concurrent_jobs: int = 0
    max_waiters: int = 0

    def to_json(self) -> dict[str, Any]:
        """按 Go ``Stats`` 的 ``omitempty`` 规则输出。"""
        out: dict[str, Any] = {}
        if self.cache:
            out["cache"] = dict(self.cache)
        out["coalesced"] = self.coalesced
        out["jobs_started"] = self.jobs
        if self.errors:
            out["errors"] = dict(self.errors)
        if self.durations_ms:
            out["durations_ms"] = dict(self.durations_ms)
        out["active_jobs"] = self.active_jobs
        out["queued_jobs"] = self.queued_jobs
        out["max_concurrent_jobs"] = self.max_concurrent_jobs
        out["max_waiters"] = self.max_waiters
        return out
