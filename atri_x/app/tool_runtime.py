"""内置工具运行时的 app 侧装配。

对应 Go：``internal/tool/builtin/runtime.go`` 的 ``NewRuntime`` / ``RuntimeOptions``
/ ``Runtime``。

**为什么这个文件在 app 而不是 tool/builtin**：本仓 ``atri_x/tool/builtin`` 只交付了
``RegisterOptions`` / ``register_all``，没有交付 Go 的 ``Runtime`` 装配壳 —— 那个壳要
同时拉起 angelmemory / selflearning / character / imagegen / groupanalysis / artist
六个服务。app 层是唯一有全部配置与存储句柄的地方，所以装配放在这里：

* ``registry``         —— 交给 Agent
* ``file_manager``     —— 交给 Agent 的 ``media`` 与 hook runtime
* ``skill_manager``    —— 交给 Agent 的 ``skills``，并负责延迟重载
* ``image_limiter``    —— ``/metrics`` 与生图并发
* ``angel_memory`` / ``self_learning`` / ``character_store`` / ``resident_memory_store``
* ``group_analysis``   —— cron 里的群分析报告 handler
* ``image_profiles`` / ``default_image_profile`` / ``artists``

**装配顺序（与 Go ``runtime.go:97-226`` 逐行对齐）**：空 ``Registry`` → ``SkillManager``
→ ``register_all``。SkillManager 需要 registry，而 ``register_all`` 又需要 SkillManager
（``RegisterOptions.SkillManager`` 决定 6 个 skill 工具与 skill 文件守卫），所以三者
只能是这个顺序；早期版本先注册再建 manager，导致 skill 一族工具永远注册不上。

**接线的铁律**：``build_tool_runtime`` 里建好的每个依赖都必须出现在 ``RegisterOptions``
里 —— ``register_all`` 的守卫只认 ``RegisterOptions`` 的字段，漏传一个字段就等于整组
工具静默消失（这正是 image_generate / character_* / artist_library_* / cron / 群分析 /
self_learning / image_to_prompt 曾经一个都注册不上的原因）。

与 Go 的差异：

* Go 里 imagegen 客户端会为每个 profile 建一个 client 并塞进 ``ImageProfiles``；
  本仓同样为每个 profile 建 :class:`atri_x.tool.builtin.image_generate.ImageProfile`
  （``client`` + 归一化后的 ``config``），因为 ``ImageGenerateTool`` 读的就是这两个字段。
* 任何可选服务模块导入失败都不致命：记 warning + 置 ``None``，让应用能起来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

from atri_x.config import FileDeliveryConfig
from atri_x.errors import InvalidError
from atri_x.processenv import Environment
from atri_x.tool import Registry
from atri_x.tool.builtin import RegisterOptions
from atri_x.tool.builtin import register_all
from atri_x.tool.builtin.artist_tools import ArtistTools
from atri_x.tool.builtin.file_manager import new_file_manager_with_media
from atri_x.tool.builtin.image_generate import ImageProfile
from atri_x.tool.builtin.runtime import image_config_from
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo

__all__ = ["ToolRuntime", "ToolRuntimeOptions", "build_tool_runtime"]


@dataclass
class ToolRuntimeOptions:
    """对应 Go ``builtin.RuntimeOptions``（只保留本仓可接线的字段）。"""

    config_dir: str = ""
    runtime_info: Any = None
    cron_service: Any = None
    chat_history: Any = None
    outbound_messages: Any = None
    group_analysis: Any = None
    group_analysis_summarizer: Any = None
    angel_memory: Any = None
    self_learning: Any = None
    store: Any = None
    media: Any = None
    sandbox_root: str = ""
    file_delivery: Any = None
    resident_memory_max_units: Any = None
    resident_memory_policy: Any = None
    character_enabled: bool = False
    character_root: str = ""
    artist_library_enabled: bool = False
    artist_library_root: str = ""
    artist_metadata_store: Any = None
    image_generation: Any = None
    web_search: Any = None
    web_extract: Any = None
    prompt_rewriter: Any = None
    image_prompt_service: Any = None
    process_env: Any = None
    child_process_env: Any = None
    file_backups: Any = None
    logger: Any = None
    #: 后台（cron / Atrinis）无人值守的 shell 放行策略；``None`` 用内置默认。
    background_shell_policy: Any = None
    #: Python 侧新增：统一时间服务（``get_current_time`` 与提示词注入共用）。
    time_service: Any = None
    #: Python 侧新增：网络时间校验状态（只读快照提供者）。
    time_verification: Any = None


@dataclass
class ToolRuntime:
    """Go ``builtin.Runtime`` 的 app 侧对等物。"""

    registry: Any = None
    file_manager: Any = None
    skill_manager: Any = None
    image_limiter: Any = None
    angel_memory: Any = None
    angel_memory_store: Any = None
    self_learning: Any = None
    self_learning_store: Any = None
    resident_memory_store: Any = None
    character_store: Any = None
    group_analysis: Any = None
    artists: Any = None
    image_profiles: dict[str, Any] = field(default_factory=dict)
    default_image_profile: str = ""
    warnings: list[str] = field(default_factory=list)

    # Go 命名别名（字段在 Go 里是 PascalCase）。
    @property
    def Registry(self) -> Any:  # pragma: no cover - 交叉检索用
        return self.registry

    @property
    def FileManager(self) -> Any:  # pragma: no cover
        return self.file_manager

    @property
    def ImageLimiter(self) -> Any:  # pragma: no cover
        return self.image_limiter


def build_tool_runtime(opts: ToolRuntimeOptions) -> ToolRuntime:
    """对应 Go ``builtin.NewRuntime``。

    **顺序与 Go 逐行对齐**（``runtime.go:97-226``）：先建空 registry，再建 skill
    manager（它需要 registry 做扫描期注册），最后才 ``register_all``。Go 里
    ``skill.NewManager(...)`` 就在 ``RegisterAll`` 之前，``RegisterOptions.SkillManager``
    因此非 nil；本仓必须保持同样顺序，否则 skill 一族工具没机会被注册。
    """
    if not opts.config_dir:
        raise InvalidError("builtin runtime config dir is required")

    warnings: list[str] = []
    info = _normalize_info(opts)
    # Go 的这两个字段是结构体值（``processenv.Environment``），没有 nil 形态；
    # 本仓的 dataclass 默认是 None，这里补成零值实例（``XxxConfigured() == false``）。
    process_env = opts.process_env if opts.process_env is not None else Environment()
    child_process_env = (
        opts.child_process_env if opts.child_process_env is not None else Environment()
    )
    if not _configured(child_process_env):
        child_process_env = process_env

    resident_memory_store = _build_resident_store(opts, warnings)
    angel_memory_store, angel_memory = _build_angel_memory(opts, warnings)
    self_learning_store, self_learning = _build_self_learning(opts, warnings)
    character_store = _build_character_store(opts, warnings)
    group_analysis = _build_group_analysis(opts, warnings)
    artists = _build_artists(opts, warnings)
    image_profiles, default_image_profile, image_limiter = _build_image_profiles(
        opts, process_env, warnings
    )

    file_manager = new_file_manager_with_media(
        str(getattr(info, "sandbox_root", "") or ""),
        getattr(info, "file_delivery", None),
        opts.store,
    )
    if opts.media is not None:
        file_manager.media = opts.media

    registry = Registry()
    skill_manager = _build_skill_manager(opts, registry, child_process_env, warnings)
    register_all(
        registry,
        RegisterOptions(
            runtime_info=info,
            resident_memory_store=resident_memory_store,
            character_store=character_store,
            image_profiles=image_profiles,
            default_image_profile=default_image_profile,
            image_limiter=image_limiter,
            artists=artists,
            web_search=opts.web_search,
            web_extract=opts.web_extract,
            prompt_rewriter=opts.prompt_rewriter,
            image_prompt_service=opts.image_prompt_service,
            skill_manager=skill_manager,
            cron_service=opts.cron_service,
            chat_history=opts.chat_history,
            group_analysis_service=group_analysis,
            group_analysis_max_messages=_section_int(opts.group_analysis, "max_messages"),
            angel_memory=angel_memory,
            angel_memory_allow_forget=_section_flag(
                opts.angel_memory, "allow_tool_forget_enabled", "allow_tool_forget"
            ),
            self_learning=self_learning,
            long_memory_dir=os.path.join(opts.config_dir, "long_memory"),
            file_manager=file_manager,
            file_backups=opts.file_backups,
            process_env=process_env,
            child_process_env=child_process_env,
            background_shell_policy=opts.background_shell_policy,
            time_service=opts.time_service,
            time_verification=opts.time_verification,
        ),
    )

    return ToolRuntime(
        registry=registry,
        file_manager=file_manager,
        skill_manager=skill_manager,
        image_limiter=image_limiter,
        angel_memory=angel_memory,
        angel_memory_store=angel_memory_store,
        self_learning=self_learning,
        self_learning_store=self_learning_store,
        resident_memory_store=resident_memory_store,
        character_store=character_store,
        group_analysis=group_analysis,
        artists=artists,
        image_profiles=image_profiles,
        default_image_profile=default_image_profile,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 各可选服务的装配（缺件只降级，不致命）
# ---------------------------------------------------------------------------


def _normalize_info(opts: ToolRuntimeOptions) -> Any:
    """对应 Go ``runtime.go:82-92`` 的 ``info`` 补全。

    ``Info`` 是 frozen dataclass（Go 那边是值拷贝），所以补默认值必须用
    ``dataclasses.replace`` 造新实例，不能原地赋值——原地赋值会抛
    ``FrozenInstanceError``，而生产路径的 ``RuntimeInfo`` 恰恰不带 ``config_dir``。
    """
    info = opts.runtime_info or RuntimeInfo()
    changes: dict[str, Any] = {}
    if not getattr(info, "config_dir", ""):
        changes["config_dir"] = opts.config_dir
    if not getattr(info, "sandbox_root", ""):
        changes["sandbox_root"] = opts.sandbox_root
    # Go 比的是零值结构体（``info.FileDelivery == (config.FileDeliveryConfig{})``），
    # 不是 nil 指针：调用方给了非零值就保留。
    if getattr(info, "file_delivery", None) == FileDeliveryConfig():
        changes["file_delivery"] = opts.file_delivery or FileDeliveryConfig()
    if changes:
        info = replace(info, **changes)
    normalize = getattr(info, "normalize", None)
    return normalize() if callable(normalize) else info


def _configured(env: Any) -> bool:
    method = getattr(env, "configured", None)
    return bool(method()) if callable(method) else False


def _build_resident_store(opts: ToolRuntimeOptions, warnings: list[str]) -> Any:
    try:
        from atri_x.memory.resident import new_store_with_options
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"resident memory store unavailable: {exc}")
        return None
    try:
        return new_store_with_options(
            os.path.join(opts.config_dir, "memories.toml"),
            opts.resident_memory_max_units,
            opts.resident_memory_policy,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"open resident memory store: {exc}")
        return None


def _build_angel_memory(
    opts: ToolRuntimeOptions, warnings: list[str]
) -> tuple[Any, Any]:
    section = opts.angel_memory
    if section is None or not _enabled(section):
        return None, None
    try:
        from atri_x.angelmemory import NewService, Open, Options as AngelOptions
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"angel memory unavailable: {exc}")
        return None, None
    try:
        store = Open(os.path.join(opts.config_dir, "angel_memory.db"))
        service = NewService(
            store,
            AngelOptions(
                max_content_runes=int(getattr(section, "max_content_runes", 0) or 0),
                max_context_runes=int(getattr(section, "max_context_runes", 0) or 0),
                max_per_scope=int(getattr(section, "max_per_scope", 0) or 0),
                max_writes_per_minute=int(
                    getattr(section, "max_writes_per_minute", 0) or 0
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"open angel memory: {exc}")
        return None, None
    return store, service


def _build_self_learning(
    opts: ToolRuntimeOptions, warnings: list[str]
) -> tuple[Any, Any]:
    section = opts.self_learning
    if section is None or not _enabled(section):
        return None, None
    try:
        from atri_x.selflearning import NewService, Open, Options as LearningOptions
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"self learning unavailable: {exc}")
        return None, None
    try:
        store = Open(os.path.join(opts.config_dir, "self_learning.db"))
        service = NewService(
            store,
            LearningOptions(
                min_users=int(getattr(section, "min_users", 0) or 0),
                max_meaning_runes=int(getattr(section, "max_meaning_runes", 0) or 0),
                max_context_runes=int(getattr(section, "max_context_runes", 0) or 0),
                max_observation_runes=int(
                    getattr(section, "max_observation_runes", 0) or 0
                ),
                max_observations_per_scope=int(
                    getattr(section, "max_observations_per_scope", 0) or 0
                ),
                max_observation_writes_per_minute=int(
                    getattr(section, "max_observation_writes_per_minute", 0) or 0
                ),
                max_mine_chars=int(getattr(section, "max_mine_chars", 0) or 0),
                mine_timeout=float(
                    getattr(section, "mine_timeout_seconds", 0) or 0
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"open self learning: {exc}")
        return None, None
    return store, service


def _build_character_store(opts: ToolRuntimeOptions, warnings: list[str]) -> Any:
    if not opts.character_enabled:
        return None
    try:
        from atri_x.character import NewStore
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"character store unavailable: {exc}")
        return None
    try:
        return NewStore(opts.character_root)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"open character store: {exc}")
        return None


def _build_group_analysis(opts: ToolRuntimeOptions, warnings: list[str]) -> Any:
    section = opts.group_analysis
    if opts.chat_history is None or section is None or not _enabled(section):
        return None
    try:
        from atri_x.groupanalysis import NewService
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"group analysis unavailable: {exc}")
        return None
    try:
        return NewService(
            opts.chat_history,
            opts.outbound_messages,
            opts.group_analysis_summarizer,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"create group analysis service: {exc}")
        return None


def _build_artists(opts: ToolRuntimeOptions, warnings: list[str]) -> ArtistTools:
    """画师工具集。对应 Go ``runtime.go:148-162`` 的 ``ArtistTools`` 组装。

    Go 的四个字段一个都不能少：``Service``（串库；未启用时是 nil store）、
    ``Media``、``Vision``（= ``ImagePromptService``），外加把
    ``ArtistMetadataStore`` 挂到 ``Service.Generations``。少了任何一个，
    ``ArtistTools.Enabled()`` 或元数据回读就会静默失效。
    """
    try:
        from atri_x.artist import Service as ArtistService
        from atri_x.artist import Store as ArtistStore
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"artist library unavailable: {exc}")
        return ArtistTools()
    # Go：``if opts.ArtistLibraryEnabled { artistStore = artist.NewStore(root) }``；
    # 未启用时 store 为 nil，交给 ``Service.Enabled()`` 判定（空 root 同样为假）。
    store = ArtistStore(opts.artist_library_root) if opts.artist_library_enabled else None
    service = ArtistService(store)
    # 读图片自带生成参数不需要可写串库（Go ``artists.Service.Generations = ...``）。
    service.generations = opts.artist_metadata_store
    return ArtistTools(
        service=service, media=opts.media, vision=opts.image_prompt_service
    )


def _build_image_profiles(
    opts: ToolRuntimeOptions, process_env: Any, warnings: list[str]
) -> tuple[dict[str, Any], str, Any]:
    limiter = _build_image_limiter(opts, warnings)
    section = opts.image_generation
    profiles: dict[str, ImageProfile] = {}
    default = ""
    if section is None or not bool(getattr(section, "enabled", False)):
        return profiles, default, limiter
    lookup = _env_lookup(process_env)
    resolve = getattr(section, "resolve_profile", None)
    if callable(resolve):
        base = resolve("")
        if base is not None:
            _add_image_profile(profiles, "", base, lookup, warnings)
        for name in (getattr(section, "profiles", None) or {}):
            resolved = resolve(name)
            if resolved is None or not bool(getattr(resolved, "enabled", False)):
                continue
            _add_image_profile(profiles, name, resolved, lookup, warnings)
    name_method = getattr(section, "default_profile_name", None)
    candidate = str(name_method() if callable(name_method) else "") or ""
    if candidate and candidate in profiles:
        default = candidate
    return profiles, default, limiter


def _add_image_profile(
    profiles: dict[str, ImageProfile],
    name: str,
    section: Any,
    lookup: Any,
    warnings: list[str],
) -> None:
    """建一个 profile 并塞进表里。对应 Go ``addProfile``（``runtime.go:171-177``）。

    ``RegisterOptions.ImageProfiles`` 的元素是 :class:`ImageProfile`，不是裸
    client：``ImageGenerateTool`` 读的是 ``profile.client`` / ``profile.config``。
    """
    client = _image_client(section, lookup, warnings)
    if client is None:
        return
    profiles[name] = ImageProfile(name=name, client=client, config=client.config)


def _image_client(section: Any, lookup: Any, warnings: list[str]) -> Any:
    """``imagegen.New(imageConfigFrom(section), lookup)``。"""
    try:
        from atri_x.imagegen import New as new_imagegen_client
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"image generation unavailable: {exc}")
        return None
    try:
        return new_imagegen_client(image_config_from(section), lookup)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"create image generation client: {exc}")
        return None


def _build_image_limiter(opts: ToolRuntimeOptions, warnings: list[str]) -> Any:
    section = opts.image_generation
    try:
        from atri_x.ops.concurrency import Config as LimitConfig
        from atri_x.ops.concurrency import Limiter
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"image limiter unavailable: {exc}")
        return None
    return Limiter(
        LimitConfig(
            max=int(getattr(section, "max_concurrent", 0) or 0),
            queue_size=int(getattr(section, "queue_size", 0) or 0),
            wait_timeout=float(getattr(section, "queue_timeout_seconds", 0) or 0),
        )
    )


def _build_skill_manager(
    opts: ToolRuntimeOptions, registry: Any, environment: Any, warnings: list[str]
) -> Any:
    try:
        from atri_x.tool.skill import new_manager
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"skill manager unavailable: {exc}")
        return None
    try:
        return new_manager(
            os.path.join(opts.config_dir, "skills"), registry, environment
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"create skill manager: {exc}")
        return None


def _env_lookup(env: Any) -> Any:
    if env is None:
        return None
    lookup = getattr(env, "lookup", None)
    return lookup if callable(lookup) else None


def _enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


def _section_int(section: Any, name: str) -> int:
    """取配置段里的整数字段（缺段即 0，与 Go 的零值一致）。"""
    if section is None:
        return 0
    return int(getattr(section, name, 0) or 0)


def _section_flag(section: Any, method_name: str, field_name: str) -> bool:
    """取配置段里的开关：优先 ``XxxEnabled()`` 形式的方法，再退回同名字段。"""
    if section is None:
        return False
    method = getattr(section, method_name, None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, field_name, False))
