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

from atri_x.errors import AtriXError

__all__ = [
    "DefaultDetail",
    "DefaultIdentityApiKeyEnv",
    "DefaultIdentityDailyLimit",
    "DefaultIdentityMinSimilarity",
    "DefaultIdentityTimeoutSeconds",
    "DefaultProbeTimeoutSeconds",
    "MaxProbeTimeoutSeconds",
    "MinProbeTimeoutSeconds",
    "Options",
    "PrivacyBlockedError",
    "Prompt",
    "Request",
    "Result",
    "Stats",
    "clamp_probe_timeout",
]

#: 图片段默认的视觉细节档位。**绝不**默认 ``low``：默认被降采样后，发色、纹样、
#: 徽章、画面文字这些两段式识图要用的证据会先丢在第一步。
DefaultDetail = "original"
#: 聊天前置描述的默认短超时（秒）。
DefaultProbeTimeoutSeconds = 4.0
#: 前置描述短超时的取值区间（秒）；越界一律夹到区间内。
MinProbeTimeoutSeconds = 1.0
MaxProbeTimeoutSeconds = 30.0
#: 以图搜图兜底的默认值。
DefaultIdentityApiKeyEnv = "SAUCENAO_API_KEY"
DefaultIdentityDailyLimit = 100
DefaultIdentityTimeoutSeconds = 8.0
DefaultIdentityMinSimilarity = 0.7


class PrivacyBlockedError(AtriXError):
    """图片外发被隐私开关拒绝。

    服务层在**打开任何上游连接之前**抛它（不建立连接、不读图、不消耗配额），
    因此「关掉外部识图」是真的没有外发，而不是发出去以后才丢弃结果。
    """


def clamp_probe_timeout(value: Any) -> float:
    """把聊天前置描述的超时夹到 1~30 秒；读不到/非法/非正数用默认 4 秒。

    这是**唯一**的超时归一实现：服务层（``Service.probe_timeout_seconds``）与
    聊天前置路径（:mod:`atri_x.vision.prechat`）都调用它，避免两处各写一套边界。
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = DefaultProbeTimeoutSeconds
    if seconds <= 0:
        seconds = DefaultProbeTimeoutSeconds
    return max(MinProbeTimeoutSeconds, min(MaxProbeTimeoutSeconds, seconds))


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

    # -- 视觉改造新增（全部带默认值：读不到旧配置也不崩） -------------------
    #: 发图片段时带的视觉细节档位；空串表示不带该键。
    detail: str = DefaultDetail
    #: 聊天前置描述的短超时（秒）。越界夹到 1~30。
    probe_timeout_seconds: float = DefaultProbeTimeoutSeconds
    #: 外部识图总开关。``False`` 时服务层在发上游前直接拒绝。
    allow_external_images: bool = True
    #: 群聊识图开关。``False`` 且当前是群聊作用域时同样拒绝。
    allow_group_images: bool = True
    #: 额外判定「当前调用是否来自群聊」的可选回调；未装配时只看进程级开关。
    group_scope: Callable[[Any], bool] | None = None
    #: 以图搜图兜底（SauceNAO）开关，默认关闭。
    identity_enabled: bool = False
    #: 优先使用的 SauceNAO key；留空时按 :attr:`identity_api_key_env` 读环境变量。
    identity_api_key: str = ""
    #: 读取 key 的环境变量名。
    identity_api_key_env: str = DefaultIdentityApiKeyEnv
    #: 每日上限（UTC 日切）。
    identity_daily_limit: int = DefaultIdentityDailyLimit
    #: 单次检索的超时（秒）。
    identity_timeout_seconds: float = DefaultIdentityTimeoutSeconds
    #: 结果相似度下限，低于它的候选直接丢弃。
    identity_min_similarity: float = DefaultIdentityMinSimilarity
    #: 每日计数的 JSON 持久化路径；留空表示只记在进程内。
    identity_state_path: str = ""
    #: 可选 logger：只记一行安全摘要（不含图片内容、URL 与凭据）。
    logger: Any = None


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
    """一张待描述的图片。对应 Go ``Request``。

    ``detail`` 是 Python 侧新增的单次覆盖（空串表示用 ``Options.detail``）：
    agent 层装配了 ``vision_detail`` 时可以只影响这一张图，而不必改动共享服务。
    """

    media_id: str = ""
    data: bytes = b""
    mime_type: str = ""
    prompt: Prompt = field(default_factory=Prompt)
    detail: str = ""


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
    #: 失败类别计数（见 :data:`atri_x.vision.failures.FAILURE_KINDS`）。键是固定
    #: 枚举，不含 media ID / URL / 凭据，可以安全地进运维接口。
    failures: dict[str, int] = field(default_factory=dict)
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
        if self.failures:
            out["failures"] = dict(self.failures)
        if self.durations_ms:
            out["durations_ms"] = dict(self.durations_ms)
        out["active_jobs"] = self.active_jobs
        out["queued_jobs"] = self.queued_jobs
        out["max_concurrent_jobs"] = self.max_concurrent_jobs
        out["max_waiters"] = self.max_waiters
        return out
