"""``[time]`` / ``[time.verification]`` / ``[[time.sources]]`` 配置段。

Go 侧没有对应段：Go 直接用 ``time.Now()`` 与 ``time.Local``。本模块是 Python 侧
「内置时间服务」的配置入口，严格解码仍走 :class:`atri_x.config.inspect.StrictConfig`。

默认值口径（与 :mod:`atri_x.config.app` 一致：字段默认值 = 生效默认值）：

* ``timezone = "Asia/Shanghai"``：新装与旧配置**都**用这个默认。旧配置没有 ``[time]``
  时行为会从「进程本地时区」切到 Asia/Shanghai —— 这是有意的、已在 ``CHANGELOG.md``
  标注的行为变更；要回到旧语义请显式写 ``timezone = "system"``。
* ``inject_context = true``：每轮请求把当前时间注入系统提示（见
  :mod:`atri_x.agent.time_context`）。
* ``[time.verification] enabled = false``：**默认不联网**。首版只允许
  ``mode = "verify_only"``（网络观测只写诊断，绝不接管业务时间）。
* ``[[time.sources]]`` 默认**不带任何来源**——不内置公共 NTP/时间 API 端点，
  必须由管理员显式填写。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atri_x.config.inspect import ConfigDecodeError, StrictConfig
from atri_x.timezones import DEFAULT_TIMEZONE, resolve_timezone

__all__ = [
    "DEFAULT_TIMEZONE",
    "TIME_MODES",
    "TIME_PROTOCOLS",
    "TimeConfig",
    "TimeSourceConfig",
    "TimeVerificationConfig",
]

#: 首版允许的校验模式。``takeover`` 在准入条件满足前**显式拒绝**（不是"暂不支持"）。
TIME_MODES: tuple[str, ...] = ("verify_only",)
#: 已知的时间来源协议。NTS（加密 NTP）尚未实现，不在列表里。
TIME_PROTOCOLS: tuple[str, ...] = ("sntp", "https")

_DEFAULT_INTERVAL_SECONDS = 1800
_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 604800


def _require_int_range(value: object, *, key: str, low: int, high: int) -> int:
    """校验整数区间；越界是**配置错误**而不是静默夹紧。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigDecodeError(f"{key} must be an integer, got {value!r}") from exc
    if number < low or number > high:
        raise ConfigDecodeError(f"{key} must be within [{low}, {high}], got {number}")
    return number


def _require_float_range(value: object, *, key: str, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigDecodeError(f"{key} must be a number, got {value!r}") from exc
    if number < low or number > high:
        raise ConfigDecodeError(f"{key} must be within [{low}, {high}], got {number}")
    return number


@dataclass
class TimeSourceConfig(StrictConfig):
    """一个网络时间来源（``[[time.sources]]``）。

    只有管理员能配置它（``app.toml`` 是超管文件）；模型与普通成员没有任何写入口。
    """

    #: 来源标识（诊断/审计里出现；必须唯一且非空）。
    id: str = ""
    #: ``sntp``（UDP 单播）或 ``https``（JSON 时间 API）。
    protocol: str = "sntp"
    #: SNTP 主机名或 IP。
    host: str = ""
    #: SNTP 端口。
    port: int = 123
    #: HTTPS 时间 API 的完整 URL；必须 ``https://``。
    url: str = ""
    #: 来源独立性分组（**配置声明**，不由域名/AS 推断）。同一组只算一份证据。
    group: str = ""
    #: 是否启用。默认关闭：不内置任何来源。
    enabled: bool = False
    #: 是否允许私网/回环地址（内网授时需要显式例外）。
    allow_private: bool = False
    #: HTTPS 响应体大小上限。
    max_body_bytes: int = 4096
    #: 要求 ``Cache-Control: no-store``（或等价的新鲜度证据）。
    require_no_store: bool = True
    #: 该来源的单次超时（秒）；0 = 用 ``[time.verification].timeout_seconds``。
    timeout_seconds: float = 0.0

    def address(self) -> str:
        """人类可读地址（日志/诊断用，**不含凭据**）。"""
        if self.protocol == "https":
            return self.url
        return f"{self.host}:{self.port}"

    def normalized(self) -> "TimeSourceConfig":
        """去空白、校验协议与必填字段（幂等，越界即报错）。"""
        self.id = (self.id or "").strip()
        self.protocol = (self.protocol or "").strip().lower()
        self.host = (self.host or "").strip()
        self.url = (self.url or "").strip()
        self.group = (self.group or "").strip()
        key = f"[[time.sources]] id={self.id!r}"
        if not self.id:
            raise ConfigDecodeError("[[time.sources]].id is required")
        if self.protocol not in TIME_PROTOCOLS:
            raise ConfigDecodeError(
                f"{key}: protocol must be one of {', '.join(TIME_PROTOCOLS)}, "
                f"got {self.protocol!r}"
            )
        try:
            self.port = int(self.port)
        except (TypeError, ValueError) as exc:
            raise ConfigDecodeError(f"{key}: port must be an integer") from exc
        if self.protocol == "sntp":
            if self.enabled and not self.host:
                raise ConfigDecodeError(f"{key}: sntp source requires host")
            if self.port < 1 or self.port > 65535:
                raise ConfigDecodeError(f"{key}: port must be 1-65535, got {self.port}")
        if self.protocol == "https":
            if not self.url:
                raise ConfigDecodeError(f"{key}: https source requires url")
            if not self.url.lower().startswith("https://"):
                raise ConfigDecodeError(
                    f"{key}: url must use https:// (TLS verification is never disabled)"
                )
            if "@" in self.url.split("://", 1)[-1].split("/", 1)[0]:
                raise ConfigDecodeError(f"{key}: credentials in url are not allowed")
        self.max_body_bytes = _require_int_range(
            self.max_body_bytes, key=f"{key}: max_body_bytes", low=256, high=65536
        )
        self.timeout_seconds = float(self.timeout_seconds or 0.0)
        if self.timeout_seconds < 0 or self.timeout_seconds > 30:
            raise ConfigDecodeError(f"{key}: timeout_seconds must be within [0, 30]")
        return self


@dataclass
class TimeVerificationConfig(StrictConfig):
    """``[time.verification]``：间歇性多来源校验（默认关闭）。"""

    #: 总开关；关闭时不会发起任何网络请求。
    enabled: bool = False
    #: 首版只允许 ``verify_only``。
    mode: str = "verify_only"
    #: 两轮校验的基准间隔（秒，带抖动）。
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    #: 抖动比例（±interval×ratio），避免多实例同时打同一来源。
    jitter_ratio: float = 0.2
    #: 单个来源的单次超时（秒）。
    timeout_seconds: float = 2.0
    #: 单轮并发上限。
    max_concurrency: int = 2
    #: 单来源重试次数（不含首次）。
    max_retries: int = 1
    #: 整轮 deadline（秒）。
    round_deadline_seconds: float = 8.0
    #: 本地与来源偏差超过该值即告警（毫秒）。
    warn_offset_ms: int = 2000
    #: 来源之间/与本地偏差超过该值即判冲突（毫秒）。
    conflict_offset_ms: int = 30000
    #: 构成"一致"证据所需的最少独立分组数。
    min_groups: int = 2
    #: 单轮最多使用几个来源。
    max_sources: int = 4
    #: 手动 ``/time verify`` 的最小间隔（秒）。
    manual_min_interval_seconds: int = 60
    #: 可疑偏差上限（秒）：超过即视为"响应明显不可信"直接丢弃。
    max_plausible_skew_seconds: int = 604800
    #: 挂起/校时判定阈值（秒）：墙钟与单调时钟增量差超过它就作废本轮样本。
    suspend_threshold_seconds: float = 5.0

    def normalized(self) -> "TimeVerificationConfig":
        """校验并归一（幂等；越界即报错，不静默夹紧）。"""
        self.mode = (self.mode or "").strip().lower()
        if self.mode not in TIME_MODES:
            if self.mode == "takeover":
                raise ConfigDecodeError(
                    "[time.verification].mode = \"takeover\" is not available in this "
                    "release; network time only produces diagnostics (verify_only)"
                )
            raise ConfigDecodeError(
                f"[time.verification].mode must be {', '.join(TIME_MODES)}, got "
                f"{self.mode!r}"
            )
        self.interval_seconds = _require_int_range(
            self.interval_seconds,
            key="[time.verification].interval_seconds",
            low=_MIN_INTERVAL_SECONDS,
            high=_MAX_INTERVAL_SECONDS,
        )
        self.jitter_ratio = _require_float_range(
            self.jitter_ratio,
            key="[time.verification].jitter_ratio",
            low=0.0,
            high=0.5,
        )
        self.timeout_seconds = _require_float_range(
            self.timeout_seconds,
            key="[time.verification].timeout_seconds",
            low=0.5,
            high=10.0,
        )
        self.max_concurrency = _require_int_range(
            self.max_concurrency,
            key="[time.verification].max_concurrency",
            low=1,
            high=4,
        )
        self.max_retries = _require_int_range(
            self.max_retries, key="[time.verification].max_retries", low=0, high=3
        )
        self.round_deadline_seconds = _require_float_range(
            self.round_deadline_seconds,
            key="[time.verification].round_deadline_seconds",
            low=2.0,
            high=60.0,
        )
        self.warn_offset_ms = _require_int_range(
            self.warn_offset_ms,
            key="[time.verification].warn_offset_ms",
            low=1,
            high=3600000,
        )
        self.conflict_offset_ms = _require_int_range(
            self.conflict_offset_ms,
            key="[time.verification].conflict_offset_ms",
            low=1,
            high=86400000,
        )
        if self.warn_offset_ms >= self.conflict_offset_ms:
            raise ConfigDecodeError(
                "[time.verification].warn_offset_ms must be smaller than "
                "conflict_offset_ms"
            )
        self.min_groups = _require_int_range(
            self.min_groups, key="[time.verification].min_groups", low=1, high=4
        )
        self.max_sources = _require_int_range(
            self.max_sources, key="[time.verification].max_sources", low=1, high=8
        )
        self.manual_min_interval_seconds = _require_int_range(
            self.manual_min_interval_seconds,
            key="[time.verification].manual_min_interval_seconds",
            low=10,
            high=86400,
        )
        self.max_plausible_skew_seconds = _require_int_range(
            self.max_plausible_skew_seconds,
            key="[time.verification].max_plausible_skew_seconds",
            low=60,
            high=31536000,
        )
        self.suspend_threshold_seconds = _require_float_range(
            self.suspend_threshold_seconds,
            key="[time.verification].suspend_threshold_seconds",
            low=1.0,
            high=300.0,
        )
        return self


@dataclass
class TimeConfig(StrictConfig):
    """``[time]`` 段。"""

    #: 业务时区：IANA 名 / ``fixed:+08:00`` / ``system``。非法值启动即失败。
    timezone: str = DEFAULT_TIMEZONE
    #: 是否把当前时间注入每轮系统提示（请求级，不落库）。
    inject_context: bool = True
    #: 网络样本的有效期上限（秒）；超过即 ``stale``。
    max_sample_age_seconds: int = 21600
    verification: TimeVerificationConfig = field(default_factory=TimeVerificationConfig)
    sources: list[TimeSourceConfig] = field(default_factory=list)

    def normalized(self) -> "TimeConfig":
        """校验并归一段落（幂等；时区非法**硬失败**，不回落）。"""
        raw = (self.timezone or "").strip()
        if not raw:
            raw = DEFAULT_TIMEZONE
        # resolve_timezone 负责抛 InvalidTimezoneError / TimezoneDataUnavailableError；
        # 配置层统一包成 ConfigDecodeError，带出是哪个键写错了。
        try:
            resolve_timezone(raw)
        except Exception as exc:  # noqa: BLE001 - 统一转成配置错误
            raise ConfigDecodeError(f"[time].timezone: {exc}") from exc
        self.timezone = raw
        self.max_sample_age_seconds = _require_int_range(
            self.max_sample_age_seconds,
            key="[time].max_sample_age_seconds",
            low=60,
            high=31536000,
        )
        self.verification = self.verification.normalized()
        normalized: list[TimeSourceConfig] = []
        seen: set[str] = set()
        for index, source in enumerate(self.sources):
            if not isinstance(source, TimeSourceConfig):  # pragma: no cover - 解码保证
                raise ConfigDecodeError(f"[[time.sources]] #{index}: not a table")
            item = source.normalized()
            if item.id in seen:
                raise ConfigDecodeError(
                    f"[[time.sources]] duplicate id {item.id!r}"
                )
            seen.add(item.id)
            normalized.append(item)
        self.sources = normalized
        return self

    def enabled_sources(self) -> list[TimeSourceConfig]:
        """已启用的来源（不触网）。"""
        return [source for source in self.sources if source.enabled]

    def verification_enabled(self) -> bool:
        """联网校验是否真正可运行（开关打开且至少有一个启用来源）。"""
        return bool(self.verification.enabled and self.enabled_sources())
