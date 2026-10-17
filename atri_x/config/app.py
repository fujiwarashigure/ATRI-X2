"""``app.toml`` 根结构 ``AppConfig``（Go ``Config``）与默认值应用 / 校验 / 合并。

对应 Go：``internal/config/config.go`` 的 ``Config`` 根结构与
``applyAppDefaults`` / ``applyProviderDefaults`` / ``applyState`` / ``mergeProviders`` /
``mergeServices`` / ``Default`` / ``defaultAppConfig`` / ``newLoadConfig`` /
``DefaultModelSelection`` / ``validateProviderProtocols`` / ``validateModeModels`` /
``validateImageGeneration`` / ``VisionSupportFor`` / ``AudioSupportFor``；
平台默认目录（``platformDefaultConfigPath`` / ``platformDefaultDataDir``）也在这里。

同目录其他模块（互相注明）：静态段 dataclass 与严格解码框架见
:mod:`atri_x.config.app_sections`；``services.toml`` / ``providers.toml`` 的段见
:mod:`atri_x.config.services`；``state.toml`` 运行态见 :mod:`atri_x.config.state`；
文件查找与 ``.env`` 读取见 :mod:`atri_x.config.loader`（因此
``resolveProviderAPIKeys`` 也落在 loader：它需要 ``.env``，放进本模块会形成循环导入）。

**默认值口径**：字段默认值 = Go 零值 + ``applyAppDefaults`` 之后的**有效值**；
``apply_app_defaults()`` 仍按 Go 的顺序逐项补齐/夹紧，保证 ``load()`` 与 Go 的
``Load`` 结果一致，且 ``to_dict()`` 往返幂等。平台相关默认值（SQLite 路径、
sandbox 根目录）在 ``apply_app_defaults()`` 里按运行环境填。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from atri_x.config.app_sections import (
    TOML_OFF,
    AtrinisConfig,
    AngelMemoryConfig,
    BudgetLimitsConfig,
    CharacterLibraryConfig,
    CommandsConfig,
    ConfigDecodeError,
    ContextConfig,
    FileDeliveryConfig,
    GroupAnalysisConfig,
    GroupKnowledgeConfig,
    GroupServicesConfig,
    LLMRequestConfig,
    MaintenanceConfig,
    MediaConfig,
    ModelSelection,
    OpsConfig,
    PlatformFilesConfig,
    ResidentMemoryConfig,
    RuntimeConfig,
    SandboxConfig,
    SecurityConfig,
    SelfLearningConfig,
    SessionConfig,
    SoulConfig,
    StorageConfig,
    StrictConfig,
    ToolsConfig,
    TurnDirectivesConfig,
    ViewConfig,
)
from atri_x.config.services import (
    APIProtocol,
    ASRConfig,
    AudioSupport,
    DEFAULT_CONTEXT_WINDOW,
    ImageGenerationConfig,
    ImageToPromptConfig,
    ModelMetadataConfig,
    ModelProfileConfig,
    ProviderConfig,
    ServicesConfig,
    VisionConfig,
    VisionSupport,
    parse_api_protocol,
)
from atri_x.config.state import (
    ContextOverflowConfig,
    GroupPolicyConfig,
    StateConfig,
)
from atri_x.config.tool_tags import ToolTagsConfig

__all__ = [
    "APP_DIR_NAME",
    "AppConfig",
    "ConfigFilesConfig",
    "ToolProfileConfig",
    "XDG_APP_DIR_NAME",
    "default",
    "default_app_config",
    "new_load_config",
]

#: Windows 下的配置目录名。对应 Go ``AppDirName``。
APP_DIR_NAME = "ATRI-X"
#: 非 Windows（XDG 风格）下的配置目录名。对应 Go ``XDGAppDirName``。
XDG_APP_DIR_NAME = "atri-x"


@dataclass
class ConfigFilesConfig(StrictConfig):
    """``[config_files]``：列出同目录下其他配置文件（相对 app.toml 所在目录）。"""

    services: str = ""
    providers: str = ""
    state: str = ""
    atrinis: str = ""
    tool_tags: str = ""


@dataclass
class ToolProfileConfig(StrictConfig):
    """``[tool_profiles.<name>]``：``@use:<别名>`` 注入的一组工具。"""

    tools: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass
class AppConfig(StrictConfig):
    """整个配置模型（Go ``Config``）。

    ``toml:"-"`` 的字段（``compact_model`` / ``tool_tags`` / ``context_overflow`` /
    ``group_policy`` / 各 ``*_config_path``）不参与 TOML 读写，只用 ``TOML_OFF`` 标记。
    ``config_warnings`` 是 Python 专有字段：记录降级（如 ``services.toml`` 缺失时退回
    ``providers.toml``）之类「不致命但必须可见」的情况。
    """

    config_files: ConfigFilesConfig = field(default_factory=ConfigFilesConfig)
    mode_models: dict[str, ModelSelection] = field(default_factory=dict)
    naming_model: ModelSelection = field(default_factory=ModelSelection)
    compact_model: ModelSelection = field(default_factory=ModelSelection, metadata=TOML_OFF)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    model_metadata: ModelMetadataConfig = field(default_factory=ModelMetadataConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    ops: OpsConfig = field(default_factory=OpsConfig)
    budget_limits: BudgetLimitsConfig = field(default_factory=BudgetLimitsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    resident_memory: ResidentMemoryConfig = field(default_factory=ResidentMemoryConfig)
    view: ViewConfig = field(default_factory=ViewConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    llm_request: LLMRequestConfig = field(default_factory=LLMRequestConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    file_delivery: FileDeliveryConfig = field(default_factory=FileDeliveryConfig)
    platform_files: PlatformFilesConfig = field(default_factory=PlatformFilesConfig)
    platform: dict[str, dict[str, Any]] = field(default_factory=dict)
    atrinis: AtrinisConfig = field(default_factory=AtrinisConfig)
    soul: SoulConfig = field(default_factory=SoulConfig)
    character_library: CharacterLibraryConfig = field(default_factory=CharacterLibraryConfig)
    image_generation: ImageGenerationConfig = field(default_factory=ImageGenerationConfig)
    image_to_prompt: ImageToPromptConfig = field(default_factory=ImageToPromptConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    group_analysis: GroupAnalysisConfig = field(default_factory=GroupAnalysisConfig)
    group_knowledge: GroupKnowledgeConfig = field(default_factory=GroupKnowledgeConfig)
    group_services: GroupServicesConfig = field(default_factory=GroupServicesConfig)
    angel_memory: AngelMemoryConfig = field(default_factory=AngelMemoryConfig)
    self_learning: SelfLearningConfig = field(default_factory=SelfLearningConfig)
    model_profiles: dict[str, ModelProfileConfig] = field(default_factory=dict)
    tool_profiles: dict[str, ToolProfileConfig] = field(default_factory=dict)
    turn_directives: TurnDirectivesConfig = field(default_factory=TurnDirectivesConfig)
    tool_tags: ToolTagsConfig = field(default_factory=ToolTagsConfig, metadata=TOML_OFF)
    context_overflow: dict[str, ContextOverflowConfig] = field(
        default_factory=dict, metadata=TOML_OFF
    )
    group_policy: dict[str, GroupPolicyConfig] = field(default_factory=dict, metadata=TOML_OFF)
    config_path: str = field(default="", metadata=TOML_OFF)
    providers_config_path: str = field(default="", metadata=TOML_OFF)
    services_config_path: str = field(default="", metadata=TOML_OFF)
    state_config_path: str = field(default="", metadata=TOML_OFF)
    atrinis_config_path: str = field(default="", metadata=TOML_OFF)
    tool_tags_config_path: str = field(default="", metadata=TOML_OFF)
    config_warnings: list[str] = field(default_factory=list, metadata=TOML_OFF)

    # -- 查询 ---------------------------------------------------------------
    def default_model_selection(self) -> ModelSelection:
        """默认会话模式所用的模型；回退顺序 work → chat → 任意已配模式。

        对应 Go ``DefaultModelSelection``。
        """
        mode = self.session.default_mode.strip() or "work"
        selection = self.mode_models.get(mode)
        if selection is not None and selection.provider and selection.model:
            return selection
        for fallback in ("chat", "work"):
            selection = self.mode_models.get(fallback)
            if selection is not None and selection.provider and selection.model:
                return selection
        for selection in self.mode_models.values():
            if selection.provider and selection.model:
                return selection
        return ModelSelection()

    def vision_support_for(self, provider: str, model: str) -> VisionSupport:
        """provider/model 的已声明图像输入能力。对应 Go ``VisionSupportFor``。"""
        provider_config = self.providers.get(provider)
        if provider_config is None:
            return VisionSupport.UNKNOWN
        return provider_config.vision_support(model)

    def audio_support_for(self, provider: str, model: str) -> AudioSupport:
        """provider/model 的已声明语音转写能力。对应 Go ``AudioSupportFor``。"""
        provider_config = self.providers.get(provider)
        if provider_config is None:
            return AudioSupport.UNKNOWN
        return provider_config.audio_support(model)

    def api_protocol_for(self, provider: str, model: str) -> APIProtocol:
        """provider/model 生效的上游协议（未知 provider 按 chat）。"""
        provider_config = self.providers.get(provider)
        if provider_config is None:
            return APIProtocol.CHAT
        return provider_config.api_protocol_for(model)

    # -- 默认值 -------------------------------------------------------------
    def apply_app_defaults(self) -> None:
        """按 Go ``applyAppDefaults`` 的顺序补齐默认值（幂等）。"""
        files = self.config_files
        # 新装部署用 services.toml；只有没显式选 services 的旧部署才保留 providers.toml。
        if not files.providers and not files.services:
            files.providers = "providers.toml"
        if not files.state:
            files.state = "state.toml"
        if not files.atrinis:
            files.atrinis = "atrinis.toml"
        if not files.tool_tags:
            files.tool_tags = "tool_tags.toml"

        # 延迟导入：平台目录解析属于 loader，而 loader 依赖本模块（避免循环导入）。
        from atri_x.config.loader import platform_default_data_dir

        data_dir = platform_default_data_dir()
        if not self.storage.sessions_sqlite_path:
            self.storage.sessions_sqlite_path = os.path.join(data_dir, "atrix_sessions.db")
        if not self.storage.chat_history_sqlite_path:
            self.storage.chat_history_sqlite_path = os.path.join(data_dir, "atrix_chat_history.db")
        if not 0 < self.storage.disk_warn_ratio < 1:
            self.storage.disk_warn_ratio = 0.85
        if not 0 < self.storage.disk_critical_ratio < 1:
            self.storage.disk_critical_ratio = 0.95
        if self.storage.disk_min_free_bytes < 0:
            self.storage.disk_min_free_bytes = 0
        if not self.soul.path:
            self.soul.path = "SOUL.md"
        self.group_knowledge = self.group_knowledge.normalized()
        self.group_services = self.group_services.normalized()
        if not self.character_library.root:
            self.character_library.root = "characters"
        # 画师串库是 opt-in：只有该段要求启用时才补默认 root。
        artist_library = self.image_generation.artist_library
        if artist_library.is_enabled() and not artist_library.root.strip():
            artist_library.root = "artist_library.json"
        image = self.image_generation
        if not image.api_key_env:
            image.api_key_env = "IMAGE_API_KEY"
        if not image.model:
            image.model = "gpt-image-2.5"
        if not image.size:
            image.size = "1024x1024"
        if not image.quality:
            image.quality = "high"
        if not image.output_format:
            image.output_format = "png"
        if image.timeout_seconds <= 0:
            image.timeout_seconds = 180
        for name in ("max_concurrent", "queue_size", "queue_timeout_seconds"):
            if getattr(image, name) < 0:
                setattr(image, name, 0)
        for name, fallback in (
            ("max_prompt_runes", 4000),
            ("optimize_max_anchors", 4),
            ("optimize_max_negatives", 10),
            ("optimize_max_added_runes", 400),
            ("optimize_max_tags", 12),
            ("optimize_rewrite_min_runes", 40),
            ("context_default_limit", 6),
        ):
            if getattr(image, name) <= 0:
                setattr(image, name, fallback)
        for name, fallback in (
            ("optimize", "rules"),
            ("optimize_term_mode", "phrase"),
            ("optimize_rewrite", "auto"),
            ("optimize_rewrite_model", "naming"),
            ("reference_field", "image"),
        ):
            if not getattr(image, name):
                setattr(image, name, fallback)
        self._apply_vision_defaults(self.image_to_prompt)
        self._apply_vision_defaults(self.vision)
        # [vision] 默认继承 image_to_prompt 的后端；显式 provider/model 始终优先。
        if not self.vision.provider.strip() and not self.vision.model.strip():
            self.vision.provider = self.image_to_prompt.provider
            self.vision.model = self.image_to_prompt.model
        self.asr = self.asr.normalized()
        report = self.maintenance.daily_report
        if not report.schedule:
            report.schedule = "0 9,21 * * *"
        if report.window_hours <= 0:
            report.window_hours = 12
        if not report.provider:
            report.provider = "deepseek"
        if not report.currency:
            report.currency = "CNY"
        self.turn_directives = self.turn_directives.normalized()
        if not self.runtime.log_level:
            self.runtime.log_level = "info"
        if self.runtime.log_retention_days <= 0:
            self.runtime.log_retention_days = 30
        self._apply_ops_defaults()
        self.budget_limits = self.budget_limits.normalized()
        self.context = self.context.normalized()
        self._normalize_group_policy()
        if not self.commands.prefixes:
            self.commands.prefixes = ["/"]
        if self.tools.max_rounds_per_turn <= 0:
            self.tools.max_rounds_per_turn = 2
        if self.resident_memory.core_max_units <= 0:
            self.resident_memory.core_max_units = 200
        if self.resident_memory.normal_max_units <= 0:
            self.resident_memory.normal_max_units = 300
        request = self.llm_request
        for name, fallback in (
            ("first_chunk_timeout_seconds", 180),
            ("stream_idle_timeout_seconds", 60),
            ("max_retries", 3),
            ("retry_initial_delay_seconds", 2),
        ):
            if getattr(request, name) <= 0:
                setattr(request, name, fallback)
        if request.response_timeout_seconds < 0:
            request.response_timeout_seconds = 0
        if self.view.session_list_page_size <= 0:
            self.view.session_list_page_size = 10
        if not self.security.user_max_tool_risk:
            self.security.user_max_tool_risk = "low"
        if not self.security.superadmin_confirm_risk:
            self.security.superadmin_confirm_risk = "high"
        if self.security.superadmins is None:
            self.security.superadmins = {"cli": ["local"]}
        self.apply_atrinis_defaults()
        self._apply_session_idle_expiration_defaults()
        maintenance = self.maintenance
        for cleanup, schedule, retention in (
            (maintenance.session_cleanup, "15 3 * * *", 30),
            (maintenance.sandbox_cleanup, "0 4 * * *", 7),
            (maintenance.chat_history_cleanup, "35 4 * * *", 180),
        ):
            if not cleanup.schedule:
                cleanup.schedule = schedule
            if cleanup.retention_days == 0:
                cleanup.retention_days = retention
        # 隐私清理只在启用时才补 schedule（默认关闭时保持为空）。
        if maintenance.privacy_cleanup.enabled and not maintenance.privacy_cleanup.schedule:
            maintenance.privacy_cleanup.schedule = "45 4 * * *"
        if not self.group_analysis.report_schedule:
            self.group_analysis.report_schedule = "0 9 * * *"
        if self.group_analysis.report_days <= 0:
            self.group_analysis.report_days = 1
        if self.angel_memory.retention_days == 0:
            self.angel_memory.retention_days = 365
        if self.self_learning.retention_days == 0:
            self.self_learning.retention_days = 365
        if not self.sandbox.root:
            self.sandbox.root = os.path.join(data_dir, "sandbox")
        if self.media.llm_image_compression_threshold_bytes <= 0:
            self.media.llm_image_compression_threshold_bytes = 4 * 1024 * 1024
        if self.media.llm_image_max_length <= 0:
            self.media.llm_image_max_length = 4096
        if self.file_delivery.max_direct_base64_bytes <= 0:
            self.file_delivery.max_direct_base64_bytes = 8 * 1024 * 1024
        if not self.file_delivery.backend:
            self.file_delivery.backend = "base64"
        if not self.file_delivery.s3_region:
            self.file_delivery.s3_region = "auto"
        for name, fallback in (
            ("max_receive_file_bytes", 100 * 1024 * 1024),
            ("download_timeout_secs", 60),
        ):
            if getattr(self.platform_files, name) <= 0:
                setattr(self.platform_files, name, fallback)
        if self.session.naming.trigger_step <= 0:
            self.session.naming.trigger_step = 1

    @staticmethod
    def _apply_vision_defaults(config: ImageToPromptConfig | VisionConfig) -> None:
        if config.max_tokens <= 0:
            config.max_tokens = 400
        if config.temperature is None:
            config.temperature = 0.2
        if config.max_edge <= 0:
            config.max_edge = 1536
        if config.max_image_bytes <= 0:
            config.max_image_bytes = 12 * 1024 * 1024
        if config.timeout_seconds <= 0:
            config.timeout_seconds = 90

    def _apply_ops_defaults(self) -> None:
        ops = self.ops
        for name in (
            "tool_timeout_seconds",
            "hook_timeout_seconds",
            "compress_timeout_seconds",
            "max_concurrent_turns",
            "max_concurrent_tools",
            "max_concurrent_hooks",
            "queue_max_size",
            "queue_max_per_user",
            "queue_max_per_scope",
            "queue_wait_timeout_seconds",
            "provider_max_concurrent",
            "provider_queue_max_size",
            "provider_wait_timeout_seconds",
            "circuit_breaker_failure_threshold",
            "circuit_breaker_open_cooldown_seconds",
        ):
            if getattr(ops, name) < 0:
                setattr(ops, name, 0)
        if ops.circuit_breaker_half_open_max <= 0:
            ops.circuit_breaker_half_open_max = 1
        for name, provider in self.providers.items():
            if provider.fallback_timeout_seconds < 0:
                provider.fallback_timeout_seconds = 0

    def _apply_session_idle_expiration_defaults(self) -> None:
        idle = self.session.idle_expiration
        for name in (
            "group_user_ttl_minutes",
            "group_superadmin_ttl_minutes",
            "private_user_ttl_minutes",
            "private_superadmin_ttl_minutes",
        ):
            if getattr(idle, name) < 0:
                setattr(idle, name, 0)

    def _normalize_group_policy(self) -> None:
        """清掉空键与「等价于未配置」的群策略（对应 Go 在 applyAppDefaults 里的清理）。"""
        if not self.group_policy:
            return
        cleaned: dict[str, GroupPolicyConfig] = {}
        for key, value in self.group_policy.items():
            name = key.strip()
            if not name:
                continue
            normalized = value.normalize()
            if normalized.is_zero():
                continue
            cleaned[name] = normalized
        self.group_policy = cleaned

    def apply_atrinis_defaults(self) -> None:
        """``atrinis.toml``（或 app.toml 的 ``[atrinis]``）默认值。对应 Go ``applyAtrinisDefaults``。"""
        atrinis = self.atrinis
        http = atrinis.http
        if not http.addr:
            http.addr = "127.0.0.1:32170"
        if http.max_body_bytes <= 0:
            http.max_body_bytes = 1024 * 1024
        if http.queue_size <= 0:
            http.queue_size = 128
        if http.workers <= 0:
            http.workers = 2
        if http.read_header_timeout_seconds <= 0:
            http.read_header_timeout_seconds = 5
        if http.read_timeout_seconds <= 0:
            http.read_timeout_seconds = 30
        if http.write_timeout_seconds <= 0:
            http.write_timeout_seconds = 300
        if http.idle_timeout_seconds <= 0:
            http.idle_timeout_seconds = 60
        if atrinis.tokens is None:
            atrinis.tokens = {}
        if atrinis.segment.max_file_bytes <= 0:
            atrinis.segment.max_file_bytes = 100 * 1024 * 1024
        if atrinis.segment.download_timeout_secs <= 0:
            atrinis.segment.download_timeout_secs = 60
        if atrinis.atriwisps is None:
            atrinis.atriwisps = {}

    def apply_provider_defaults(self) -> None:
        """Provider 段的内置默认值。对应 Go ``applyProviderDefaults``。"""
        if self.providers is None:
            self.providers = {}
        if self.model_metadata.default_context_window <= 0:
            self.model_metadata.default_context_window = DEFAULT_CONTEXT_WINDOW

    def apply_state(self, state: StateConfig) -> None:
        """把 ``state.toml`` 的运行态选择合并进只读配置。对应 Go ``(*Config).applyState``。

        只合并「配置层拥有的运行态选择」；群运行态、群服务条目与 ``[budget]`` 账本由
        app/agent 层直接持有（见 :class:`atri_x.config.state.RuntimeStateStore`）。
        """
        if state.mode_models:
            self.mode_models.update(state.mode_models)
        if state.naming_model.provider or state.naming_model.model:
            self.naming_model = state.naming_model.copy()
        if state.compact_model.provider or state.compact_model.model:
            # 压缩模型是运行态选择，只从 state.toml 读；未配置时由调用方用当前模式模型兜底。
            self.compact_model = state.compact_model.copy()
        if state.session.default_mode:
            self.session.default_mode = state.session.default_mode
        if state.context_overflow:
            self.context_overflow.update(state.context_overflow)
        if state.group_policy:
            for key, value in state.group_policy.items():
                name = key.strip()
                if not name:
                    continue
                normalized = value.normalize()
                if normalized.is_zero():
                    self.group_policy.pop(name, None)
                    continue
                self.group_policy[name] = normalized

    # -- 合并（services.toml / 旧 providers.toml） --------------------------
    def merge_providers(self, legacy: AppConfig) -> None:
        """旧 ``providers.toml``：只取 providers 与 model_metadata。对应 Go ``mergeProviders``。"""
        self.providers = legacy.providers
        self.model_metadata = legacy.model_metadata

    def merge_services(self, services: ServicesConfig) -> None:
        """把共享只读服务配置覆盖到旧 providers/app 配置之上。

        对应 Go ``mergeServices``：**缺席的段保留**旧值，因此迁移可以渐进进行。
        """
        if services.providers:
            self.providers = services.providers
        if services.model_metadata is not None:
            self.model_metadata = services.model_metadata
        if services.model_profiles:
            self.model_profiles = services.model_profiles
        if services.image_generation is not None:
            self.image_generation = services.image_generation
        if services.image_to_prompt is not None:
            self.image_to_prompt = services.image_to_prompt
        if services.vision is not None:
            self.vision = services.vision
        if services.asr is not None:
            self.asr = services.asr

    # -- 校验 ---------------------------------------------------------------
    def validate_provider_protocols(self) -> None:
        """拒绝无法识别的 ``api_mode``（而不是静默回落到 chat）。"""
        for name, provider in self.providers.items():
            _, ok = parse_api_protocol(provider.api_mode)
            if not ok:
                raise ConfigDecodeError(
                    f"[providers.{name}].api_mode must be chat or response, got "
                    f"{provider.api_mode!r}"
                )
            for model, model_config in provider.model_configs.items():
                _, ok = parse_api_protocol(model_config.api_mode)
                if not ok:
                    raise ConfigDecodeError(
                        f"[providers.{name}.model_configs.{model!r}].api_mode must be chat or "
                        f"response, got {model_config.api_mode!r}"
                    )

    def validate_mode_models(self) -> None:
        """``default_mode`` 只能是 work/chat，且两种模式都必须配好 provider/model。"""
        if not self.session.default_mode:
            self.session.default_mode = "work"
        if self.session.default_mode not in ("work", "chat"):
            raise ConfigDecodeError(
                f"session.default_mode must be work or chat, got {self.session.default_mode!r}"
            )
        if self.mode_models is None:
            self.mode_models = {}
        for mode in ("work", "chat"):
            selected = self.mode_models.get(mode)
            if selected is None or not selected.provider or not selected.model:
                raise ConfigDecodeError(f"mode_models.{mode} provider/model is required")

    def validate_image_generation(self, config_path: str, config_files: Mapping[str, str]) -> None:
        """拒绝会被静默折进默认值的生图设置，以及两个 JSON 存储的文件冲突。

        对应 Go ``validateImageGeneration``：方言与负向提示词模式的笔误会改变真正发给
        后端的字节，所以在启动期失败，而不是第一次出图时。``config_files`` 是
        「描述 → 本次 Load 用到的配置文件路径」，两个存储会被整份重写，不能与它们同文件。
        """

        def check(section: str, negative_mode: str, dialect: str) -> None:
            value = negative_mode.strip()
            if value and value.lower() not in ("field", "inline", "off"):
                raise ConfigDecodeError(
                    f"{section}.negative_prompt_mode must be field, inline or off, got "
                    f"{negative_mode!r}"
                )
            value = dialect.strip()
            if value and value.lower() not in (
                "auto",
                "natural",
                "sd_weighted",
                "sd",
                "a1111",
                "webui",
                "nai",
                "novelai",
                "tags",
                "tag",
            ):
                raise ConfigDecodeError(
                    f"{section}.prompt_dialect must be auto, natural, sd_weighted "
                    f"(sd/a1111/webui), nai (novelai) or tags (tag), got {dialect!r}"
                )

        image = self.image_generation
        check("[image_generation]", image.negative_prompt_mode, image.prompt_dialect)
        for name, profile in image.profiles.items():
            check(f"[image_generation.profiles.{name}]", profile.negative_prompt_mode,
                  profile.prompt_dialect)
        artist_library = image.artist_library
        dedicated = (
            ("image_generation.artist_library.root", artist_library.root.strip(),
             artist_library.is_enabled()),
            ("image_generation.metadata_store", image.metadata_store.strip(), True),
        )
        config_dir = os.path.dirname(config_path)
        shared: list[tuple[str, str]] = [
            ("storage.sessions_sqlite_path", self.storage.sessions_sqlite_path),
            ("storage.chat_history_sqlite_path", self.storage.chat_history_sqlite_path),
            ("config_files.state", self.config_files.state),
            ("character_library.root", self.character_library.root),
            # 这几份由内置运行时在配置目录旁创建而不是在配置里声明，名字在此刻意重复：
            # 写错只是弱化检查，漏掉则会让存储覆盖运行中服务的常驻记忆。
            ("the resident memory store", os.path.join(config_dir, "memories.toml")),
            ("the angel memory store", os.path.join(config_dir, "angel_memory.db")),
            ("the self-learning store", os.path.join(config_dir, "self_learning.db")),
        ]
        for name in sorted(config_files):
            shared.append((name, config_files[name]))
        claimed: dict[str, str] = {}
        for key, path, in_use in dedicated:
            if not in_use or not path:
                continue
            resolved = resolve_relative(config_path, path)
            owner = claimed.get(resolved)
            if owner is not None:
                raise ConfigDecodeError(
                    f"{key} must point to a dedicated writable file, but it is the same file "
                    f"as {owner}"
                )
            claimed[resolved] = key
            for name, shared_path in shared:
                if not shared_path.strip():
                    continue
                if resolve_relative(config_path, shared_path) == resolved:
                    raise ConfigDecodeError(
                        f"{key} must point to a dedicated writable file, but {path!r} is also "
                        f"used by {name}"
                    )


def resolve_relative(base_file: str, path: str) -> str:
    """相对 ``base_file`` 所在目录解析路径；空串按 Go 语义归一为 ``.``。"""
    if not path or os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(os.path.dirname(base_file), path))


def new_load_config() -> AppConfig:
    """只带「解析 app.toml 之前就存在」的内部默认值的配置。

    对应 Go ``newLoadConfig``：目前只有会话空闲过期（由 dataclass 默认值承担）。
    ``config_files`` 的默认值在解析之后才应用，好让 app.toml 显式选 ``services.toml``
    的部署不会继承旧的 ``providers.toml`` 默认。
    """
    return AppConfig()


def default_app_config() -> AppConfig:
    """内置默认 app 配置（不读文件、不解析密钥）。对应 Go ``defaultAppConfig``。"""
    config = new_load_config()
    config.apply_app_defaults()
    return config


def default() -> AppConfig:
    """``atri-x`` 无配置文件时的默认配置。对应 Go ``Default``。"""
    config = default_app_config()
    config.apply_provider_defaults()
    return config
