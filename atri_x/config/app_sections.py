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
# 表情图库的推荐情绪清单只有一处定义（图库模块），配置/提示词/GUI 都从这里取。
from atri_x.reaction.catalog import DEFAULT_EMOTIONS

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
    "ConnectNoticeConfig", "CONNECT_NOTICE_DEFAULT_TEXT",
    "ContextConfig", "CronTaskConfig", "DailyReportConfig", "DailySummaryConfig",
    "DshCleanupConfig", "FileDeliveryConfig",
    "GroupAnalysisConfig", "GroupKnowledgeConfig", "GroupServicesConfig",
    "LLMRequestConfig", "MaintenanceCleanupConfig", "MaintenanceConfig", "MediaConfig",
    "ModelPriceConfig", "ModelSelection", "OpsConfig", "PlatformFilesConfig",
    "ResidentMemoryConfig", "RuntimeConfig", "SafetyConfig", "SafetyOverrideConfig",
    "SandboxConfig",
    "SecurityConfig", "SelfLearningConfig", "SessionConfig", "SessionIdleExpirationConfig",
    "SessionNamingConfig", "SoulConfig", "StorageConfig", "ToolsConfig",
    "ToolPreviewConfig", "ReactionsConfig",
    "REACTION_DEFAULT_SUPPRESS_KEYWORDS",
    "TOOL_PREVIEW_DEFAULT_EXTRACT_TEXT", "TOOL_PREVIEW_DEFAULT_SEARCH_TEXT",
    "TOOL_PREVIEW_DEFAULT_TEXT", "TOOL_PREVIEW_FAILURE_TEMPLATE",
    "TOOL_PREVIEW_SYSTEM_TEMPLATE",
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


# --- 工具进度预览 / 本地表情图库（Python 侧新增段，无 Go 对应） -------------
#: ``[reactions].suppress_keywords`` 的默认抑制词：报错、道歉、明确拒绝这类场景
#: 不该配轻松的表情。
REACTION_DEFAULT_SUPPRESS_KEYWORDS: tuple[str, ...] = (
    "错误",
    "报错",
    "失败",
    "异常",
    "抱歉",
    "对不起",
    "无法",
    "不能",
)

#: ``[connect_notice]`` 的默认文案（``text`` 留空时用它）。
CONNECT_NOTICE_DEFAULT_TEXT = "ATRI-X 已连接 QQ OneBot。"

#: ``[tool_preview]`` 各文案字段的默认值（``normalized()`` 与 GUI 共用）。
TOOL_PREVIEW_DEFAULT_SEARCH_TEXT = "检索中……请稍候。"
TOOL_PREVIEW_DEFAULT_EXTRACT_TEXT = "正在读取页面内容。"
TOOL_PREVIEW_DEFAULT_TEXT = "正在调用 {tool}……"
TOOL_PREVIEW_SYSTEM_TEMPLATE = "正在调用 {tool}：{args}"
TOOL_PREVIEW_FAILURE_TEMPLATE = "{tool} 调用失败：{error}"


def _clamp_int(value: int, fallback: int, *, minimum: int, maximum: int) -> int:
    """把整数配置夹到 `[minimum, maximum]`；非法/越界回落边界值。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return max(minimum, min(maximum, value))


@dataclass
class ToolPreviewConfig(StrictConfig):
    """``[tool_preview]``：工具调用进度预览的开关与文案（Python 侧新增段）。

    默认值与 1.0.3 之前的行为对齐：``enabled = true``（不清空），群聊里是否投递
    仍由各平台段自己的 ``show_tool_preview_in_group`` 决定。``style`` 只影响
    **程序兜底文案**：

    * ``"persona"``（默认）—— 角色化短句，且**不展示工具参数**，避免把查询词/
      路径无意泄露到群里；
    * ``"system"`` —— 旧行为「正在调用 工具名：参数」（参数按 160 字符截断）。
    """

    enabled: bool = True
    style: str = "persona"
    search_text: str = TOOL_PREVIEW_DEFAULT_SEARCH_TEXT
    extract_text: str = TOOL_PREVIEW_DEFAULT_EXTRACT_TEXT
    default_text: str = TOOL_PREVIEW_DEFAULT_TEXT
    system_template: str = TOOL_PREVIEW_SYSTEM_TEMPLATE
    failure_template: str = TOOL_PREVIEW_FAILURE_TEMPLATE

    def normalized(self) -> "ToolPreviewConfig":
        """补默认值并夹紧非法值（``apply_app_defaults`` 调用，幂等）。"""
        style = (self.style or "").strip().lower()
        if style not in ("persona", "system"):
            style = "persona"
        return ToolPreviewConfig(
            enabled=bool(self.enabled),
            style=style,
            search_text=(self.search_text or "").strip()
            or TOOL_PREVIEW_DEFAULT_SEARCH_TEXT,
            extract_text=(self.extract_text or "").strip()
            or TOOL_PREVIEW_DEFAULT_EXTRACT_TEXT,
            default_text=(self.default_text or "").strip()
            or TOOL_PREVIEW_DEFAULT_TEXT,
            system_template=(self.system_template or "").strip()
            or TOOL_PREVIEW_SYSTEM_TEMPLATE,
            failure_template=(self.failure_template or "").strip()
            or TOOL_PREVIEW_FAILURE_TEMPLATE,
        )


@dataclass
class ReactionsConfig(StrictConfig):
    """``[reactions]``：本地表情图库 + 表情图片输出策略（Python 侧新增段）。

    默认 **关闭**：这是会改变出站内容的新能力，必须显式打开；不打开时行为与
    1.0.3 完全一致（正文里即使出现表情标记也会被清掉，但不发图）。

    图库位于 ``<配置目录>/<gallery_dir>/catalog.json`` + 图片文件；相对路径由
    :func:`atri_x.config.loader.load` 按 app.toml 所在目录解析成绝对路径。
    """

    enabled: bool = False
    gallery_dir: str = "reactions"
    show_in_group: bool = True
    show_in_private: bool = True
    cooldown_text_replies: int = 2
    session_cooldown_seconds: int = 60
    group_cooldown_seconds: int = 120
    dedupe_recent: int = 5
    probability: float = 1.0
    max_text_chars: int = 160
    suppress_keywords: list[str] = field(
        default_factory=lambda: list(REACTION_DEFAULT_SUPPRESS_KEYWORDS)
    )
    emotions: list[str] = field(default_factory=lambda: list(DEFAULT_EMOTIONS))

    def normalized(self) -> "ReactionsConfig":
        """补默认值并夹紧范围（``apply_app_defaults`` 调用，幂等）。"""
        probability = float(self.probability)
        if probability != probability or probability < 0:
            probability = 0.0
        elif probability > 1:
            probability = 1.0
        emotions = _reaction_text_list(self.emotions, list(DEFAULT_EMOTIONS))
        keywords = _reaction_text_list(list(self.suppress_keywords), [])
        return ReactionsConfig(
            enabled=bool(self.enabled),
            gallery_dir=(self.gallery_dir or "").strip() or "reactions",
            show_in_group=bool(self.show_in_group),
            show_in_private=bool(self.show_in_private),
            cooldown_text_replies=_clamp_int(
                self.cooldown_text_replies, 2, minimum=0, maximum=20
            ),
            session_cooldown_seconds=_clamp_int(
                self.session_cooldown_seconds, 60, minimum=0, maximum=86400
            ),
            group_cooldown_seconds=_clamp_int(
                self.group_cooldown_seconds, 120, minimum=0, maximum=86400
            ),
            dedupe_recent=_clamp_int(self.dedupe_recent, 5, minimum=0, maximum=50),
            probability=probability,
            max_text_chars=_clamp_int(
                self.max_text_chars, 160, minimum=0, maximum=4000
            ),
            suppress_keywords=keywords,
            emotions=emotions,
        )


def _reaction_text_list(values: Any, fallback: list[str]) -> list[str]:
    """列表型配置的去空白/去重；显式空列表保持为空（表示"不启用"）。"""
    if not isinstance(values, (list, tuple)):
        return list(fallback)
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


#: 群号里允许带的作用域前缀（用户从日志 / 群策略里复制过来时常见）。
_CONNECT_NOTICE_GROUP_PREFIXES = ("qqonebot:group:", "qqonebot:", "group:")


@dataclass
class ConnectNoticeConfig(StrictConfig):
    """``[connect_notice]``：平台上线通知的开关与落点（Python 侧新增段）。

    私聊与群聊**各自独立**：

    * ``private`` —— 平台（当前只对 ``qqonebot`` 生效）连上 OneBot 后，给
      ``[security.superadmins]`` 里配置的超管发一条私聊提醒；
    * ``group`` —— 同时给 ``groups`` 列出的 QQ 群发一条群提醒。群号写纯数字即可，
      也接受从日志里复制来的 ``qqonebot:group:123`` / ``group:123``（会自动剥前缀）。

    两个开关互不影响；群聊开关打开但 ``groups`` 为空时退化为"不发群提醒"
    （不猜群、不群发）。``text`` 留空用 :data:`CONNECT_NOTICE_DEFAULT_TEXT`。
    这些通知是"连接事实"的通知，**不经过** hooks.toml；旧版 hooks.toml 里的
    ``notify_qqonebot_connected`` 规则如果保留会重复发送，1.1.3 修订已从默认模板移除。
    """

    private: bool = True
    group: bool = True
    groups: list[str] = field(default_factory=list)
    text: str = ""

    def normalized(self) -> "ConnectNoticeConfig":
        """群号去前缀/去空白/去重，文案去空白（``apply_app_defaults`` 调用，幂等）。"""
        groups: list[str] = []
        for value in self.groups or []:
            text = str(value or "").strip()
            for prefix in _CONNECT_NOTICE_GROUP_PREFIXES:
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            if text and text not in groups:
                groups.append(text)
        return ConnectNoticeConfig(
            private=bool(self.private),
            group=bool(self.group),
            groups=groups,
            text=(self.text or "").strip(),
        )



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
    #: 普通成员是否允许建卡（默认允许）。关闭后只有超级管理员能建 / 导入新卡；
    #: 修改自己已有的卡不受影响（不需要超管）。
    allow_user_create: bool = True

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

    core_max_units: int = 500
    normal_max_units: int = 500
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


def _valid_clock(value: str) -> bool:
    """``HH:MM`` 校验（静默时段用）。"""
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _valid_quiet_window(value: str) -> bool:
    """``HH:MM-HH:MM`` 校验（与群策略 ``quiet_hours`` 同格式）。"""
    left, separator, right = (value or "").strip().partition("-")
    if not separator:
        return False
    return _valid_clock(left) and _valid_clock(right)


@dataclass
class QuietConfig(StrictConfig):
    """``[quiet]``：全局静默时段（Python 侧新增段，对应评估 §E）。

    静默期只压**计算**：消息照常入库与展示，只是不叫醒模型；cron / 外部事件
    默认跳过且**不补跑**（避免补跑风暴）。默认关闭，打开后行为才变化。
    群聊另有 ``[group_policy]`` 的 ``quiet_hours``（群级更严，唤醒判定里已生效）。
    """

    enabled: bool = False
    #: ``HH:MM-HH:MM``；跨零点写成 ``23:00-07:00``。格式非法时回落默认值。
    window: str = "23:00-07:00"
    #: 空 = 进程本地时区；否则是 ``zoneinfo`` 认识的 IANA 时区名（如 ``Asia/Shanghai``）。
    timezone: str = ""
    #: 静默期是否仍允许 cron 报告 / 外部事件（默认都跳过）。
    allow_cron: bool = False
    allow_external: bool = False
    #: 静默期是否仍允许用户直聊（私聊直呼）。
    allow_direct: bool = True
    #: 静默期是否仍允许超级管理员触发。
    allow_superadmin: bool = True

    def normalized(self) -> "QuietConfig":
        """补默认值并校验窗口（``apply_app_defaults`` 调用，幂等）。"""
        if not _valid_quiet_window(self.window):
            self.window = "23:00-07:00"
        self.window = self.window.strip()
        self.timezone = (self.timezone or "").strip()
        return self


@dataclass
class ParticipationConfig(StrictConfig):
    """``[participation]``：群聊参与状态机（Python 侧新增段，评估 §D）。

    默认 **关闭**：开启前行为与之前完全一致。开启后只做**抑制**，不主动找话题：

    * 机器人最近在某个群里主动接过话，``cooldown_seconds`` 内普通闲聊不再唤醒它
      （点名 / 回复 / 唤醒词仍然会唤醒）；
    * 连续 ``no_reply_streak_max`` 次"被叫到但没人接"之后，暂停该群的主动参与。
    """

    guard: bool = False
    #: 主动参与冷却（秒）：冷却期内普通闲聊不唤醒。
    cooldown_seconds: int = 120
    #: 连续无回应达到该次数后暂停主动参与（0 = 不限制）。
    no_reply_streak_max: int = 3

    def normalized(self) -> "ParticipationConfig":
        """夹紧范围（``apply_app_defaults`` 调用，幂等）。"""
        self.cooldown_seconds = max(0, int(self.cooldown_seconds or 0))
        self.no_reply_streak_max = max(0, int(self.no_reply_streak_max or 0))
        return self


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
class SafetyOverrideConfig(StrictConfig):
    """``[safety.chat]`` / ``[safety.work]``：按会话模式覆盖 ``[safety]``。

    字段为 ``None`` 表示**继承**全局默认（这是默认状态）；只有显式写了的键才覆盖。
    审计文件全局唯一（``[safety].audit_path``），所以覆盖段没有 ``audit_path`` /
    ``audit_preview_chars``；词表类别 / 敏感信息类别 / 白名单也可以在覆盖段里改，
    但 GUI 只暴露常用的 7 项。
    """

    #: None = 继承 ``[safety].enabled``。
    enabled: bool | None = None
    #: None = 继承 ``[safety].mode``；否则 off / audit / enforce。
    mode: str | None = None
    #: None = 继承；显式 [] 表示"该模式用全部内置类别"（与 ``[safety]`` 语义一致）。
    categories: list[str] | None = None
    #: None = 继承。
    sensitive: list[str] | None = None
    #: None = 继承；显式表只覆盖写了的类别动作。
    sensitive_actions: dict[str, str] | None = None
    #: None = 继承。
    allowlist: list[str] | None = None
    #: None = 继承。
    block_reply: str | None = None
    #: None = 继承。
    strip_tool_images: bool | None = None
    #: None = 继承。
    block_image_output: bool | None = None
    #: None = 继承。
    block_file_output: bool | None = None
    #: None = 继承。
    max_scan_chars: int | None = None

    def normalized(self) -> "SafetyOverrideConfig":
        """归一化显式写了的字段；``None`` 继续表示"继承"，不填默认值。"""
        out = self.copy()
        if out.mode is not None:
            mode = (out.mode or "").strip().lower()
            out.mode = mode if mode in ("off", "audit", "enforce") else "audit"
        for name in ("categories", "sensitive", "allowlist"):
            value = getattr(out, name)
            if value is not None:
                setattr(out, name, normalize_string_list(value))
        if out.sensitive_actions is not None:
            actions: dict[str, str] = {}
            for key, value in out.sensitive_actions.items():
                name = str(key or "").strip().lower()
                action = str(value or "").strip().lower()
                if name and action in ("flag", "redact"):
                    actions[name] = action
            out.sensitive_actions = actions
        if out.block_reply is not None:
            out.block_reply = (out.block_reply or "").strip()
        if out.max_scan_chars is not None and out.max_scan_chars <= 0:
            out.max_scan_chars = None
        return out


@dataclass
class SafetyConfig(StrictConfig):
    """``[safety]``：内置“道德限制 + 敏感信息过滤”。

    与 ``[security]``（工具权限/风险等级）是两件事：本段管**内容**，``[security]``
    管**能力**。默认 ``enabled = false``：不配置就完全不改变任何既有行为。

    处理链路（原生 Python，不依赖外部插件）：

    * 入站用户消息 → 命中词表类违规时按 ``mode`` 拦截或只记审计；
    * 模型输出文本 / 工具结果 / 发送边界 → 词表类替换 + 敏感信息脱敏；
    * 图片/文件出口 → 按 ``block_image_output`` / ``block_file_output`` 丢弃并替换文本。

    ``mode``：``off`` 关闭；``audit`` 只写审计日志不改输出；``enforce`` 真正拦截/替换。
    """

    enabled: bool = False
    #: off | audit | enforce
    mode: str = "audit"
    #: 启用的词表类别（空 = 全部内置类别）：hate_speech / illegal_method /
    #: minor_sexual / privacy_violation / self_harm / sexual_explicit /
    #: terror_extremism / violence_gore。
    categories: list[str] = field(default_factory=list)
    #: 启用的敏感信息类别（空 = 全部内置类别）：private_key / api_key / jwt /
    #: password / id_card / bank_card / phone / email / internal_ip / local_path。
    sensitive: list[str] = field(default_factory=list)
    #: 覆盖单个敏感类别的动作：``flag``（只记审计）或 ``redact``（替换）。
    sensitive_actions: dict[str, str] = field(default_factory=dict)
    #: 命中豁免（子串匹配，命中处不处理），用来压误杀。
    allowlist: list[str] = field(default_factory=list)
    #: 入站消息被拦截时回复用户的文本；空 = 用内置文案。
    block_reply: str = ""
    #: 审计日志路径；空 = ``<配置目录>/safety-audit.jsonl``。
    audit_path: str = ""
    #: 审计预览保留的字符数（审计只存 sha256 + 脱敏预览，绝不存原文）。
    audit_preview_chars: int = 120
    #: true 时把工具结果里的图片段剥离成文字（模型看不到图）。
    strip_tool_images: bool = False
    #: true 时出站图片被丢弃并替换成文本说明（图片内容本身不做识别）。
    block_image_output: bool = False
    #: true 时出站文件被丢弃并替换成文本说明。
    block_file_output: bool = False
    #: 单次扫描的最大字符数，超出部分截断扫描（防超长文本拖慢链路）。
    max_scan_chars: int = 8000
    #: ``[safety.chat]``：chat 模式（闲聊 / 低成本模型槽位）的按模式覆盖。
    chat: SafetyOverrideConfig = field(default_factory=SafetyOverrideConfig)
    #: ``[safety.work]``：work 模式（工作 / 工具模型槽位）的按模式覆盖。
    work: SafetyOverrideConfig = field(default_factory=SafetyOverrideConfig)

    def normalized(self) -> SafetyConfig:
        """补齐默认值并归一化枚举/列表，保证 ``to_dict`` 往返幂等。"""
        out = self.copy()
        mode = (out.mode or "").strip().lower()
        out.mode = mode if mode in ("off", "audit", "enforce") else "audit"
        out.categories = normalize_string_list(out.categories or [])
        out.sensitive = normalize_string_list(out.sensitive or [])
        out.allowlist = normalize_string_list(out.allowlist or [])
        actions: dict[str, str] = {}
        for key, value in (out.sensitive_actions or {}).items():
            name = str(key or "").strip().lower()
            action = str(value or "").strip().lower()
            if name and action in ("flag", "redact"):
                actions[name] = action
        out.sensitive_actions = actions
        out.block_reply = (out.block_reply or "").strip()
        out.audit_path = (out.audit_path or "").strip()
        if out.audit_preview_chars <= 0:
            out.audit_preview_chars = 120
        if out.max_scan_chars <= 0:
            out.max_scan_chars = 8000
        out.chat = out.chat.normalized()
        out.work = out.work.normalized()
        return out


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
    """Asia/Shanghai 时区（峰谷计价口径）。

    1.0.10 起**不再静默回退固定 +08:00**：时区数据缺失时抛
    :class:`atri_x.errors.TimezoneDataUnavailableError`，与 ``[time].timezone`` 的
    硬失败口径一致。冻结产物由打包期断言保证数据存在，Docker 显式安装 tzdata。
    """
    if _SHANGHAI_CACHE:
        return _SHANGHAI_CACHE[0]
    from atri_x.timezones import resolve_timezone

    zone: Any = resolve_timezone("Asia/Shanghai").tz
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
