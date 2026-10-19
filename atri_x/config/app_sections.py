"""``app.toml`` 静态行为段的 dataclass。

对应 Go：``internal/config/config.go`` 的 app.toml 段（``StorageConfig`` /
``RuntimeConfig`` / ``OpsConfig`` / ``BudgetLimitsConfig`` / ``ContextConfig`` /
``MaintenanceConfig`` / ``SessionConfig`` / ``SecurityConfig`` / ``TurnDirectivesConfig`` /
``Group*Config`` / ``AngelMemoryConfig`` / ``SelfLearningConfig`` / ``Atrinis*Config`` …）
与 ``ComputeCost`` / ``PriceFor`` / ``isOffPeak`` 这几个价格助手。

同目录其他模块的分工：:mod:`atri_x.config.inspect` 是**严格解码基座**
（``StrictConfig`` / ``load_toml`` / 带键路径的错误）与未知键自检，本模块从中导入并重新
导出；:mod:`atri_x.config.app` 是 ``AppConfig`` 根结构与默认值应用；
:mod:`atri_x.config.services` 管 ``services.toml`` / 生图 / 视觉 / ASR；
:mod:`atri_x.config.state` 管 ``state.toml`` 运行态；:mod:`atri_x.config.tool_tags` 管
``tool_tags.toml``。

**默认值口径**：字段默认值取「Go 零值 + ``applyAppDefaults``/``Normalized`` 之后的有效
值」，因此 ``from_dict({})`` 与 Go ``Load`` 一致、``to_dict`` 往返幂等；平台相关默认值
（SQLite 路径、sandbox 根）由 :meth:`atri_x.config.app.AppConfig.apply_app_defaults` 填。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from atri_x.config.inspect import (
    OFF_EMPTY,
    OMITEMPTY,
    TOML_OFF,
    ConfigDecodeError,
    ConfigError,
    MissingConfigFileError,
    StrictConfig,
    UnknownConfigKeysError,
    key_path,
    load_toml,
    normalize_string_list,
)

__all__ = [
    # 解码基座（定义在 atri_x.config.inspect，这里重新导出）
    "OFF_EMPTY", "OMITEMPTY", "TOML_OFF", "ConfigDecodeError", "ConfigError",
    "MissingConfigFileError", "StrictConfig", "UnknownConfigKeysError", "key_path",
    "load_toml", "normalize_string_list",
    # app.toml 静态段
    "AgentConfig", "AgentDshConfig", "AngelMemoryConfig", "ArtistLibraryConfig", "AtrinisAtriwispConfig", "AtrinisConfig",
    "AtrinisDeliveryDisabledConfig", "AtrinisHTTPConfig", "AtrinisSegmentConfig",
    "AtrinisTargetConfig", "AtrinisTokenConfig", "BudgetLimitsConfig",
    "CharacterLibraryConfig", "ChatHistoryCleanupConfig", "CommandsConfig",
    "ContextConfig", "CronTaskConfig", "DailyReportConfig", "DailySummaryConfig",
    "DshCleanupConfig", "FileDeliveryConfig",
    "GroupAnalysisConfig", "GroupKnowledgeConfig", "GroupServicesConfig",
    "LLMRequestConfig", "MaintenanceCleanupConfig", "MaintenanceConfig", "MediaConfig",
    "ModelPriceConfig", "ModelSelection", "OpsConfig", "PlatformFilesConfig",
    "ResidentMemoryConfig", "RuntimeConfig", "SandboxConfig", "SecurityConfig",
    "SelfLearningConfig", "SessionConfig", "SessionIdleExpirationConfig",
    "SessionNamingConfig", "SoulConfig", "StorageConfig", "ToolsConfig",
    "TurnDirectivesConfig", "ViewConfig", "off_peak", "offpeak_price", "shanghai_tz",
]


# --- 基础段 ---------------------------------------------------------------
@dataclass
class ModelSelection(StrictConfig):
    """``[mode_models.*]`` / ``[naming_model]`` 里的一对 provider+model。"""

    provider: str = ""
    model: str = ""


@dataclass
class StorageConfig(StrictConfig):
    sessions_sqlite_path: str = ""
    chat_history_sqlite_path: str = ""
    disk_warn_ratio: float = 0.85
    disk_critical_ratio: float = 0.95
    disk_min_free_bytes: int = 0


@dataclass
class RuntimeConfig(StrictConfig):
    log_level: str = "info"
    log_retention_days: int = 30
    #: 核心装配器（模型/上下文/预算/限流/工具指令）缺失时是否让启动失败。
    #: 默认 ``False``：只记 WARN 并继续启动；``True`` 时 ``build_agent`` 抛错。
    fail_fast_agent_services: bool = False


@dataclass
class SoulConfig(StrictConfig):
    path: str = "SOUL.md"


@dataclass
class ViewConfig(StrictConfig):
    session_list_page_size: int = 10


@dataclass
class CommandsConfig(StrictConfig):
    prefixes: list[str] = field(default_factory=lambda: ["/"])


@dataclass
class ToolsConfig(StrictConfig):
    max_rounds_per_turn: int = 2


@dataclass
class SandboxConfig(StrictConfig):
    root: str = ""


@dataclass
class MediaConfig(StrictConfig):
    llm_image_compression_threshold_bytes: int = 4 * 1024 * 1024
    llm_image_max_length: int = 4096


@dataclass
class FileDeliveryConfig(StrictConfig):
    max_direct_base64_bytes: int = 8 * 1024 * 1024
    backend: str = "base64"
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    s3_access_key_env: str = ""
    s3_secret_key_env: str = ""
    s3_public_base_url: str = ""


@dataclass
class PlatformFilesConfig(StrictConfig):
    max_receive_file_bytes: int = 100 * 1024 * 1024
    download_timeout_secs: int = 60


@dataclass
class CharacterLibraryConfig(StrictConfig):
    enabled: bool | None = None
    root: str = "characters"

    def is_enabled(self) -> bool:
        """默认启用。对应 Go ``IsEnabled``。"""
        return self.enabled is None or self.enabled


@dataclass
class ArtistLibraryConfig(StrictConfig):
    """可写画师串库；只有配好 root 才启用（对应 Go ``ArtistLibraryConfig``）。"""

    enabled: bool | None = None
    root: str = ""

    def is_enabled(self) -> bool:
        if self.enabled is not None:
            return self.enabled
        return self.root.strip() != ""


# --- ops / budget / context / llm_request / resident_memory -----------------
@dataclass
class OpsConfig(StrictConfig):
    tool_timeout_seconds: int = 0
    hook_timeout_seconds: int = 0
    compress_timeout_seconds: int = 0
    max_concurrent_turns: int = 0
    max_concurrent_tools: int = 0
    max_concurrent_hooks: int = 0
    user_messages_per_minute: int = 0
    user_burst: int = 0
    group_messages_per_minute: int = 0
    group_burst: int = 0
    rate_limit_idle_ttl_seconds: int = 0
    queue_max_size: int = 0
    queue_max_per_user: int = 0
    queue_max_per_scope: int = 0
    queue_wait_timeout_seconds: int = 0
    queue_wait_kinds: list[str] = field(default_factory=list)
    provider_max_concurrent: int = 0
    provider_queue_max_size: int = 0
    provider_wait_timeout_seconds: int = 0
    # Provider 熔断：连续失败到阈值后打开，冷却后放少量半开探测；0 表示关闭。
    circuit_breaker_failure_threshold: int = 0
    circuit_breaker_open_cooldown_seconds: int = 0
    circuit_breaker_half_open_max: int = 1


@dataclass
class BudgetLimitsConfig(StrictConfig):
    """每日额度；0 表示不限制。生图/视觉/语音按调用次数，chat 按 token 与费用。"""

    global_image_daily: int = 0
    user_image_daily: int = 0
    global_vision_daily: int = 0
    user_vision_daily: int = 0
    global_asr_daily: int = 0
    user_asr_daily: int = 0
    global_chat_tokens_daily: int = 0
    user_chat_tokens_daily: int = 0
    global_chat_cost_daily: float = 0.0
    user_chat_cost_daily: float = 0.0
    #: 把 token/费用限制变成调用前原子预占、返回 usage 后结算的硬预算。
    chat_hard_limit: bool = False
    chat_hard_limit_reserve_output_tokens: int = 0

    def normalized(self) -> BudgetLimitsConfig:
        """负额度按 0（不限）处理。对应 Go ``Normalized``。"""
        out = self.copy()
        for name in (
            "global_image_daily",
            "user_image_daily",
            "global_vision_daily",
            "user_vision_daily",
            "global_asr_daily",
            "user_asr_daily",
            "global_chat_tokens_daily",
            "user_chat_tokens_daily",
            "global_chat_cost_daily",
            "user_chat_cost_daily",
            "chat_hard_limit_reserve_output_tokens",
        ):
            if getattr(out, name) < 0:
                setattr(out, name, 0)
        return out


@dataclass
class ContextConfig(StrictConfig):
    compact_enabled: bool = False
    compact_trigger_ratio: float = 0.8
    max_prompt_ratio: float = 0.8
    reserve_output_tokens: int = 0
    single_message_max_ratio: float = 0.5
    user_original_max_runes: int = 4000
    #: 长消息保护默认策略：reject / truncate / summarize。
    overflow_mode: str = "reject"

    def normalized(self) -> ContextConfig:
        """补齐安全默认值。对应 Go ``Normalized``。"""
        out = self.copy()
        for name, fallback in (
            ("compact_trigger_ratio", 0.8),
            ("max_prompt_ratio", 0.8),
            ("single_message_max_ratio", 0.5),
        ):
            value = getattr(out, name)
            if value <= 0 or value >= 1:
                setattr(out, name, fallback)
        if out.reserve_output_tokens < 0:
            out.reserve_output_tokens = 0
        if out.user_original_max_runes <= 0:
            out.user_original_max_runes = 4000
        mode = out.overflow_mode.strip().lower()
        out.overflow_mode = mode if mode in ("reject", "truncate", "summarize") else "reject"
        return out


@dataclass
class LLMRequestConfig(StrictConfig):
    first_chunk_timeout_seconds: int = 180
    stream_idle_timeout_seconds: int = 60
    response_timeout_seconds: int = 0
    max_retries: int = 3
    retry_initial_delay_seconds: int = 2


@dataclass
class ResidentMemoryConfig(StrictConfig):
    """常驻记忆长度与 normal 写入保护；指针字段区分「未设置」与显式 0。"""

    core_max_units: int = 200
    normal_max_units: int = 300
    normal_write_min_interval_seconds: int | None = None
    normal_write_window_seconds: int | None = None
    normal_write_max_per_window: int | None = None
    normal_max_lines: int | None = None
    normal_max_units_per_entry: int | None = None
    normal_block_instruction_patterns: bool | None = None

    def is_normal_block_instruction_patterns(self) -> bool:
        return (
            self.normal_block_instruction_patterns is None
            or self.normal_block_instruction_patterns
        )

    def normal_write_min_interval_seconds_value(self) -> int:
        return _value_or(self.normal_write_min_interval_seconds, 5)

    def normal_write_window_seconds_value(self) -> int:
        return _value_or(self.normal_write_window_seconds, 60)

    def normal_write_max_per_window_value(self) -> int:
        return _value_or(self.normal_write_max_per_window, 6)

    def normal_max_lines_value(self) -> int:
        return _value_or(self.normal_max_lines, 20)

    def normal_max_units_per_entry_value(self) -> int:
        return _value_or(self.normal_max_units_per_entry, 80)


def _value_or(value: int | None, fallback: int) -> int:
    return fallback if value is None else value


# --- 维护任务与用量报告 ---------------------------------------------------
@dataclass
class CronTaskConfig(StrictConfig):
    enabled: bool = False
    schedule: str = ""
    #: 一次性执行时间（本地墙钟 ``YYYY-MM-DD HH:MM[:SS]``）。非空时该任务改为
    #: 「到点执行一次」：调度器按 metadata 里的 ``run_at`` 定点，跑完不再重复。
    #: ``schedule`` 仍要保留一个合法 cron 表达式（引擎用它做表达式校验与下次时间计算）。
    run_at: str = ""


@dataclass
class MaintenanceCleanupConfig(StrictConfig):
    enabled: bool = False
    schedule: str = ""
    retention_days: int = 0
    run_at: str = ""


@dataclass
class ChatHistoryCleanupConfig(StrictConfig):
    enabled: bool = False
    schedule: str = ""
    retention_days: int = 0
    run_at: str = ""


@dataclass
class DshCleanupConfig(MaintenanceCleanupConfig):
    """``[maintenance.dsh_cleanup]``：内置 DSH 内核状态目录的保留清理。

    字段口径与其它清理段一致；清理范围见 :mod:`atri_x.maintenance.dsh`：
    只删 ``DSH_HOME/sessions`` 下过期的一次性会话目录与会话缓存，
    ``profiles/``、``storages/`` 和顶层文件一律保留（删了要重新准备运行配置）。
    """


@dataclass
class ModelPriceConfig(StrictConfig):
    """单模型每百万 token 价格；基准字段是高峰价，offpeak_* 为空则沿用基准。"""

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_input_per_million: float = 0.0
    offpeak_input_per_million: float = 0.0
    offpeak_output_per_million: float = 0.0
    offpeak_cache_input_per_million: float = 0.0

    def compute_cost(
        self, prompt_tokens: int, completion_tokens: int, cache_hit_tokens: int
    ) -> float:
        """按一次 usage 计算费用。对应 Go ``ComputeCost``。"""
        cache_hit = min(max(cache_hit_tokens, 0), max(prompt_tokens, 0))
        cache_miss = max(prompt_tokens - cache_hit, 0)
        cache_price = self.cache_input_per_million
        if cache_price <= 0:
            cache_price = self.input_per_million
        return (
            cache_miss / 1_000_000 * self.input_per_million
            + cache_hit / 1_000_000 * cache_price
            + completion_tokens / 1_000_000 * self.output_per_million
        )


@dataclass
class DailyReportConfig(StrictConfig):
    enabled: bool = False
    schedule: str = "0 9,21 * * *"
    window_hours: int = 12
    provider: str = "deepseek"
    platform: str = ""
    currency: str = "CNY"
    data_root: str = ""
    image_price_per_image: float = 0.0
    #: 高峰/空闲双档（默认启用；DeepSeek 空闲时段半价）。
    peak_pricing: bool | None = None
    holidays: list[str] = field(default_factory=list)
    prices: dict[str, ModelPriceConfig] = field(default_factory=dict)
    #: 一次性执行时间（本地墙钟）；非空时改为「到点执行一次」，见 :class:`CronTaskConfig`。
    run_at: str = ""

    def is_peak_pricing(self) -> bool:
        """默认启用双档。对应 Go ``IsPeakPricing``。"""
        return self.peak_pricing is None or self.peak_pricing

    def price_for(self, model: str, at: Any) -> ModelPriceConfig | None:
        """返回该时刻生效的价格档；未配置该模型时返回 ``None``。对应 Go ``PriceFor``。"""
        price = self.prices.get(model)
        if price is None:
            return None
        if not self.is_peak_pricing() or not off_peak(at, self.holidays):
            return price
        return offpeak_price(price)


@dataclass
class DailySummaryConfig(StrictConfig):
    """``[maintenance.daily_summary]``：每天把会话信息汇总成一段可读文本。

    生成物是 ``<数据目录>/daily_summaries/<YYYY-MM-DD>.json``（纯文本 + 统计），
    对应维护任务 ``maintenance.daily_summary``；实现见
    :mod:`atri_x.maintenance.summary`。口径：

    - ``days``：统计最近几天（0 → 1，按 ``updated_at`` 落在窗口内的会话算）；
    - ``max_sessions``：最多汇总多少个会话（0 → 20），按最近更新时间倒序取；
    - ``use_llm``：是否再交给压缩模型二次摘要（会花 token；默认关）；
    - ``notify``：是否复用资源报告的投递通道（:attr:`Service.report_sender`）发出去。
    """

    enabled: bool = False
    schedule: str = "0 8 * * *"
    days: int = 0
    max_sessions: int = 0
    use_llm: bool = False
    notify: bool = False
    #: 一次性执行时间（本地墙钟）；非空时改为「到点执行一次」，见 :class:`CronTaskConfig`。
    run_at: str = ""


@dataclass
class MaintenanceConfig(StrictConfig):
    log_cleanup: CronTaskConfig = field(
        default_factory=lambda: CronTaskConfig(schedule="0 3 * * *")
    )
    session_cleanup: MaintenanceCleanupConfig = field(
        default_factory=lambda: MaintenanceCleanupConfig(schedule="15 3 * * *", retention_days=30)
    )
    sandbox_cleanup: MaintenanceCleanupConfig = field(
        default_factory=lambda: MaintenanceCleanupConfig(schedule="0 4 * * *", retention_days=7)
    )
    chat_history_cleanup: ChatHistoryCleanupConfig = field(
        default_factory=lambda: ChatHistoryCleanupConfig(schedule="35 4 * * *", retention_days=180)
    )
    #: 只在 enabled 时才补 schedule（默认关闭，保持空串）。
    privacy_cleanup: MaintenanceCleanupConfig = field(default_factory=MaintenanceCleanupConfig)
    #: 内置 DSH 内核（``DSH_HOME/sessions``）的保留清理；默认 30 天。
    dsh_cleanup: DshCleanupConfig = field(
        default_factory=lambda: DshCleanupConfig(schedule="50 4 * * *", retention_days=30)
    )
    daily_report: DailyReportConfig = field(default_factory=DailyReportConfig)
    #: 每日会话总结（本地文件，默认由配置资产打开）。
    daily_summary: DailySummaryConfig = field(default_factory=DailySummaryConfig)


# --- 会话 / 安全 / 指令 ----------------------------------------------------
@dataclass
class SessionIdleExpirationConfig(StrictConfig):
    group_user_ttl_minutes: int = 10
    group_superadmin_ttl_minutes: int = 10
    private_user_ttl_minutes: int = 10
    private_superadmin_ttl_minutes: int = 0


@dataclass
class SessionNamingConfig(StrictConfig):
    trigger_step: int = 1


@dataclass
class SessionConfig(StrictConfig):
    default_mode: str = ""
    idle_expiration: SessionIdleExpirationConfig = field(
        default_factory=SessionIdleExpirationConfig
    )
    naming: SessionNamingConfig = field(default_factory=SessionNamingConfig)


@dataclass
class SecurityConfig(StrictConfig):
    user_max_tool_risk: str = "low"
    superadmin_confirm_risk: str = "high"
    superadmins: dict[str, list[str]] | None = None
    #: 后台（cron / Atrinis）无人值守时允许**自动执行**的 shell 命令名白名单。
    #: ``None`` = 用内置只读白名单；``[]`` = 后台 shell 全部拒绝；
    #: 只有"整条命令低风险 + 命令名都在白名单里"才会被自动放行。
    background_shell_allow: list[str] | None = None
    #: 显式恢复旧行为（后台 shell 只拦 critical，其余自动放行）。默认 false。
    #: 打开等于把无人值守的 shell 交给模型自行决定，风险自负。
    background_shell_allow_all: bool = False


@dataclass
class TurnDirectivesConfig(StrictConfig):
    """``@model:`` / ``@image:`` / ``@use:`` 这类声明的触发词（前缀与关键词可叠加）。"""

    prefixes: list[str] = field(default_factory=lambda: ["@", "#"])
    model_keywords: list[str] = field(default_factory=lambda: ["model", "m", "模型", "用模型"])
    image_keywords: list[str] = field(default_factory=lambda: ["image", "img", "生图", "出图"])
    tool_keywords: list[str] = field(default_factory=lambda: ["use", "工具", "用工具"])

    def normalized(self) -> TurnDirectivesConfig:
        """补默认触发词。对应 Go ``Normalized``。"""
        out = self.copy()
        if not out.prefixes:
            out.prefixes = ["@", "#"]
        if not out.model_keywords:
            out.model_keywords = ["model", "m", "模型", "用模型"]
        if not out.image_keywords:
            out.image_keywords = ["image", "img", "生图", "出图"]
        if not out.tool_keywords:
            out.tool_keywords = ["use", "工具", "用工具"]
        return out


# --- 群功能与学习层 --------------------------------------------------------
@dataclass
class GroupAnalysisConfig(StrictConfig):
    enabled: bool | None = None
    max_messages: int = 0
    report_enabled: bool | None = None
    report_schedule: str = "0 9 * * *"
    report_platform: str = ""
    report_scope_id: str = ""
    report_days: int = 1

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled

    def is_report_enabled(self) -> bool:
        return self.report_enabled is not None and self.report_enabled


@dataclass
class GroupKnowledgeConfig(StrictConfig):
    """确定性群知识库 / FAQ；命中后本地回答，不进模型提示词。"""

    enabled: bool | None = None
    max_entries_per_scope: int = 200
    max_question_runes: int = 200
    max_answer_runes: int = 2000
    max_aliases: int = 8
    max_keywords: int = 16
    max_match_runes: int = 2000

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled

    def normalized(self) -> GroupKnowledgeConfig:
        """显式 0 视为「用默认值」。对应 Go ``Normalized``。"""
        out = self.copy()
        for name, fallback in (
            ("max_entries_per_scope", 200),
            ("max_question_runes", 200),
            ("max_answer_runes", 2000),
            ("max_aliases", 8),
            ("max_keywords", 16),
            ("max_match_runes", 2000),
        ):
            if getattr(out, name) <= 0:
                setattr(out, name, fallback)
        return out


@dataclass
class GroupServicesConfig(StrictConfig):
    """群内提醒 / 投票 / 报名；全部本地确定性执行，不调用模型。"""

    enabled: bool | None = None
    max_reminders_per_scope: int = 50
    max_reminder_days: int = 365
    max_polls_per_scope: int = 20
    max_poll_options: int = 10
    max_signups_per_scope: int = 20
    max_signup_capacity: int = 500
    max_text_runes: int = 500

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled

    def normalized(self) -> GroupServicesConfig:
        out = self.copy()
        for name, fallback in (
            ("max_reminders_per_scope", 50),
            ("max_reminder_days", 365),
            ("max_polls_per_scope", 20),
            ("max_poll_options", 10),
            ("max_signups_per_scope", 20),
            ("max_signup_capacity", 500),
            ("max_text_runes", 500),
        ):
            if getattr(out, name) <= 0:
                setattr(out, name, fallback)
        return out


@dataclass
class AngelMemoryConfig(StrictConfig):
    """长期记忆。数值上限的**生效默认值**在 ``internal/angelmemory``（1000 / 1200 / …），
    配置层保持 Go 的零值，不在这里再编一套。"""

    enabled: bool | None = None
    retention_days: int = 365
    max_content_runes: int = 0
    max_context_runes: int = 0
    max_per_scope: int = 0
    max_writes_per_minute: int = 0
    #: 源消息被撤回时删除派生记忆；默认 true。
    forget_on_recall: bool | None = None
    #: 允许模型用 angel_forget 删除自己的记忆；默认 false（模型发起是 opt-in）。
    allow_tool_forget: bool | None = None

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled

    def forget_on_recall_enabled(self) -> bool:
        return self.forget_on_recall is None or self.forget_on_recall

    def allow_tool_forget_enabled(self) -> bool:
        return self.allow_tool_forget is not None and self.allow_tool_forget


@dataclass
class SelfLearningConfig(StrictConfig):
    """表达 / 黑话学习层；只有 review 通过的内容才会注入。"""

    enabled: bool | None = None
    retention_days: int = 365
    min_count: int = 0
    min_users: int = 0
    max_meaning_runes: int = 0
    max_context_runes: int = 0
    max_observation_runes: int = 0
    max_observations_per_scope: int = 0
    max_observation_writes_per_minute: int = 0
    max_mine_chars: int = 0
    mine_timeout_seconds: int = 0

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled


# --- agent.dsh（Python 版新增：DSH 深度路由） ------------------------------
@dataclass
class AgentDshConfig(StrictConfig):
    """``[agent.dsh]``：把复杂任务一次性交给内置 DSH 内核处理。

    **Python 版新增段**（Go 上游没有）：DSH 只在「任务够复杂」时接管一轮
    ``session/prompt``，普通聊天仍走 ATRI-X 原生低成本链路。判定与成本闸门见
    :mod:`atri_x.agent.dsh.router`，上下文交接见 :mod:`atri_x.agent.dsh.handoff`。

    0 / 空串一律表示「用内置默认」，与其它段的口径一致：

    - ``enabled``：总开关（内核缺失或命令找不到时自动视为不可用，不会报错）；
    - ``min_score``：任务复杂度阈值 0..1，越大越保守；
    - ``lambda_penalty``：成本闸门权重，``score - λ·cost_pressure`` 必须仍达标；
    - ``cost_ratio``：DSH 单轮成本相对原生链路的估计倍数（用于 cost_pressure）；
    - ``cost_scale_tokens``：cost_pressure 的归一化尺度（多少 token 算「一次大调用」）；
    - ``command``：显式启动命令；空串按「``DSH_ACP_COMMAND`` → 内置内核 → PATH 上的 ``dsh``」；
    - ``model`` / ``reasoning_effort``：下发 ``session/set_config_option`` 的会话配置；
    - ``timeout_seconds``：单轮 prompt 超时；
    - ``max_history_messages`` / ``prompt_char_budget``：交给 DSH 的历史消息条数与字符预算；
    - ``workspace`` / ``home``：DSH 会话工作区与 ``DSH_HOME``；空串落在配置目录下；
    - ``allow_permissions``：是否放行 DSH 的权限请求（默认拒绝，安全优先）；
    - ``circuit_breaker_failures`` / ``circuit_breaker_cooldown_seconds``：连续失败熔断；
    - ``credential_provider`` / ``credential_env`` / ``credential_base_url_env``：把
      ATRI-X 解析好的模型凭据注入 DSH 子进程（见 :mod:`atri_x.agent.dsh.runtime` 的
      环境收敛）。DSH 侧凭据不落盘也能跑通，Windows 图形端只写 ``.env`` 的场景尤其需要。
    """

    enabled: bool = True
    min_score: float = 0.40
    lambda_penalty: float = 1.0
    cost_ratio: float = 2.0
    cost_scale_tokens: int = 20000
    command: str = ""
    model: str = ""
    reasoning_effort: str = ""
    timeout_seconds: float = 180.0
    max_history_messages: int = 8
    prompt_char_budget: int = 12000
    workspace: str = ""
    home: str = ""
    allow_permissions: bool = False
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: float = 300.0
    #: 用哪个 ATRI-X provider 的密钥喂给 DSH；空 = 自动（优先名字含 deepseek 的 provider，
    #: 其次当前 work provider）。
    credential_provider: str = ""
    #: 注入的凭据环境变量名（内核默认找 DEEPSEEK_API_KEY）；空 = 内置默认。
    credential_env: str = "DEEPSEEK_API_KEY"
    #: 选中 provider 的 base_url 非官方默认时，一并注入的变量名；空 = 内置默认。
    credential_base_url_env: str = "DEEPSEEK_BASE_URL"


@dataclass
class AgentConfig(StrictConfig):
    """``[agent]``：Agent 行为段（Python 版新增，目前只有 ``dsh``）。"""

    dsh: AgentDshConfig = field(default_factory=AgentDshConfig)


# --- atrinis.toml ----------------------------------------------------------
@dataclass
class AtrinisTargetConfig(StrictConfig):
    platform: str = ""
    type: str = ""
    id: str = ""


@dataclass
class AtrinisSegmentConfig(StrictConfig):
    max_file_bytes: int = 100 * 1024 * 1024
    download_timeout_secs: int = 60


@dataclass
class AtrinisHTTPConfig(StrictConfig):
    addr: str = "127.0.0.1:32170"
    max_body_bytes: int = 1024 * 1024
    queue_size: int = 128
    workers: int = 2
    read_header_timeout_seconds: int = 5
    read_timeout_seconds: int = 30
    write_timeout_seconds: int = 300
    idle_timeout_seconds: int = 60


@dataclass
class AtrinisTokenConfig(StrictConfig):
    token_env: list[str] = field(default_factory=list)


@dataclass
class AtrinisDeliveryDisabledConfig(StrictConfig):
    targets: list[AtrinisTargetConfig] = field(default_factory=list)


@dataclass
class AtrinisAtriwispConfig(StrictConfig):
    enabled: bool | None = None
    allowed_tokens: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    disabled_external_tools: list[str] = field(default_factory=list)
    disabled_targets: list[AtrinisTargetConfig] = field(default_factory=list)


@dataclass
class AtrinisConfig(StrictConfig):
    """整个 ``atrinis.toml``（也可写在 app.toml 的 ``[atrinis]``）。"""

    enabled: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    http: AtrinisHTTPConfig = field(default_factory=AtrinisHTTPConfig)
    tokens: dict[str, AtrinisTokenConfig] = field(default_factory=dict)
    delivery_disabled: AtrinisDeliveryDisabledConfig = field(
        default_factory=AtrinisDeliveryDisabledConfig
    )
    segment: AtrinisSegmentConfig = field(default_factory=AtrinisSegmentConfig)
    atriwisps: dict[str, AtrinisAtriwispConfig] = field(default_factory=dict)


# --- 高峰/空闲判定（与 Go 的 shanghaiLocation / isOffPeak 一致） -----------
_SHANGHAI_CACHE: list[Any] = []


def shanghai_tz() -> Any:
    """Asia/Shanghai 时区；标准库缺 tzdata 时退回固定 +08:00（同 Go 的 FixedZone 兜底）。"""
    import datetime as _dt

    if _SHANGHAI_CACHE:
        return _SHANGHAI_CACHE[0]
    try:
        from zoneinfo import ZoneInfo

        zone: Any = ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001 - 缺 tzdata 时按固定偏移处理
        zone = _dt.timezone(_dt.timedelta(hours=8), "CST")
    _SHANGHAI_CACHE.append(zone)
    return zone


def off_peak(at: Any, holidays: Sequence[str]) -> bool:
    """判断某时刻是否属于 DeepSeek 空闲档。对应 Go ``isOffPeak``。

    规则：节假日整天、周末整天、工作日 09:00-12:00 与 14:00-18:00 之外为空闲档。
    """
    import datetime as _dt

    if at is None:
        return False
    if isinstance(at, _dt.datetime):
        if at.tzinfo is None:
            return False
        local = at.astimezone(shanghai_tz())
    elif isinstance(at, _dt.date):
        local = _dt.datetime(at.year, at.month, at.day, tzinfo=shanghai_tz())
    else:
        return False
    day = local.strftime("%Y-%m-%d")
    if any(holiday.strip() == day for holiday in holidays):
        return True
    if local.weekday() >= 5:  # 5=周六、6=周日
        return True
    minutes = local.hour * 60 + local.minute
    peak = (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)
    return not peak


def offpeak_price(price: ModelPriceConfig) -> ModelPriceConfig:
    """把基准价替换为已配置的空闲价。对应 Go ``offpeakPrice``。"""
    out = price.copy()
    if price.offpeak_input_per_million > 0:
        out.input_per_million = price.offpeak_input_per_million
    if price.offpeak_cache_input_per_million > 0:
        out.cache_input_per_million = price.offpeak_cache_input_per_million
    if price.offpeak_output_per_million > 0:
        out.output_per_million = price.offpeak_output_per_million
    return out
