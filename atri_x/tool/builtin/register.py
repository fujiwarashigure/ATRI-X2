"""把所有已落地的内置工具装进 :class:`atri_x.tool.Registry`。

对应 Go：``internal/tool/builtin/register.go``。

**注册条件与 Go 逐条对齐（重要）**：Go ``RegisterAll`` 里只有一部分工具是**无条件**注册的
（``register.go:64-224`` 中不带 ``if`` 的那些），其余每一个都挂在 ``RegisterOptions`` 的某个
依赖上（``if opts.Xxx != nil`` / ``!= ""`` / ``.Enabled()``）。本模块用
:data:`ALWAYS_REGISTERED_TOOLS` 与 :data:`CONDITIONAL_TOOLS` 把这两类分开记录：

* :data:`ALWAYS_REGISTERED_TOOLS` —— 无条件注册，工具自己容忍 nil 依赖（调用时报「未配置」）。
* :data:`CONDITIONAL_TOOLS` —— 键是 Go 的守卫字段，值是它满足时才注册的工具组；依赖缺失时
  **整组不注册、也不留桩**，因为注册一个只会失败的工具会让模型以为它可用，比缺注册更危险。
* :data:`REGISTERED_TOOLS` —— 只是上面两者的**并集**（「本仓接线过的全部工具名」），
  **不代表**最小配置下它们都会出现；要「最小配置下必然出现」的集合请用
  :data:`ALWAYS_REGISTERED_TOOLS`。

:data:`UNPORTED_TOOLS` 记录 Go 有、而本仓 ``register_all`` 尚未**接线**的部分。它在
``internal/tool/skill`` 一族接线后**为空**：``register.go`` 里注册的每一条现在都有对应实现
与对应语句（接线时序见 :mod:`atri_x.app.tool_runtime`）。

与 Go 的差异：

* ``RegisterOptions`` 的字段名 snake_case，类型用鸭子类型（工具只按方法消费依赖对象，
  不做 ``isinstance`` 检查）；``None`` 对应 Go 的 ``nil`` 指针。
* Go 用 ``error`` 返回注册失败；Python 里 :meth:`atri_x.tool.Registry.register`
  自己抛 :class:`atri_x.errors.InvalidError`，因此 ``register_all`` 直接让它冒泡。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.processenv import Environment
from atri_x.tool.builtin.angel_memory import (
    AngelMemoryToolOptions,
    new_angel_memory_tools,
)
from atri_x.tool.builtin.artist_extract import new_artist_extract_tool
from atri_x.tool.builtin.artist_library import new_artist_library_tools
from atri_x.tool.builtin.artist_tools import ArtistTools
from atri_x.tool.builtin.atriwisp_creator import new_atriwisp_creator_tool
from atri_x.tool.builtin.character import new_character_tools
from atri_x.tool.builtin.chat_history import (
    new_get_chat_history_around_tool,
    new_reply_to_chat_history_message_tool,
    new_search_chat_history_tool,
)
from atri_x.tool.builtin.common import with_config_env_dir
from atri_x.tool.builtin.cron import new_cron_tools
from atri_x.tool.builtin.file_guard import (
    FileGuard,
    new_atrix_skill_file_guard_rule,
    new_file_guard,
    new_long_memory_file_guard_rule,
    new_resident_memory_file_guard_rule,
)
from atri_x.tool.builtin.file_manager import FileManager
from atri_x.tool.builtin.file_rollback import new_rollback_file_tool
from atri_x.tool.builtin.file_tools import EditFileTool, ReadFileTool
from atri_x.tool.builtin.get_media import new_get_media_tool
from atri_x.tool.builtin.fetch_message import new_fetch_message_tool
from atri_x.tool.builtin.group_analysis import new_group_analysis_tool
from atri_x.tool.builtin.image_generate import ImageGenerateTool, ImageProfile
from atri_x.tool.builtin.image_to_prompt import new_image_to_prompt_tool
from atri_x.tool.builtin.long_memory import new_long_memory_tools
from atri_x.tool.builtin.prompt_library import PromptLibrarySearchTool
from atri_x.tool.builtin.resident_memory import new_resident_memory_tools
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo
from atri_x.tool.builtin.self_learning import new_self_learning_review_tool
from atri_x.tool.builtin.send_file import new_send_file_tool
from atri_x.tool.builtin.shell import new_shell_tool_with_environment
from atri_x.tool.builtin.time_tool import new_get_current_time_tool
from atri_x.tool.builtin.view_image import new_view_image_tool
from atri_x.tool.builtin.web_extract import new_web_extract_tool
from atri_x.tool.builtin.web_extract_engines import (
    specs_from_config as extract_specs_from_config,
)
from atri_x.tool.builtin.web_search import new_web_search_tool
from atri_x.tool.builtin.web_search_engines import specs_from_config
from atri_x.tool.builtin.workspace import new_workspace_tool
from atri_x.tool.discover import new_discover_tool
from atri_x.tool.media_runtime import MediaRuntime
from atri_x.tool.skill.agent_skill_tool import new_agent_skill_tool
from atri_x.tool.skill.creator import new_create_atrix_skill_tool
from atri_x.tool.skill.finalize_atrix_skill import new_finalize_atrix_skill_tool
from atri_x.tool.skill.modify_atrix_skill import (
    new_modify_atrix_skill_tool,
    new_read_atrix_skill_tool,
)
from atri_x.tool.skill.runner import new_go_runner
from atri_x.tool.tool import Registry

#: Go ``RegisterAll`` 里**没有 if 守卫**、无条件注册的工具名。
#:
#: 这些工具自身容忍 nil 依赖：服务缺失时 ``call`` 返回「未配置」，而不是不被注册。
#: 名字取自各 ``XxxName`` 常量；``discover_tool`` 是入口工具（对模型隐藏，见
#: :meth:`atri_x.tool.Registry.tool_names`）。
ALWAYS_REGISTERED_TOOLS: tuple[str, ...] = (
    "discover_tool",
    "workspace",
    "web_search",
    "web_extract",
    "artist_extract",
    "prompt_library_search",
    "read_file",
    "edit_file",
    "rollback_file",
    "shell",
    "atriwisp_creator",
    # Go ``register.go:229-236``：``skill.NewAgentSkillTool(nil)`` 与
    # ``skill.NewGoRunner(nil, env)`` 都容忍 nil（manager / catalog 缺失时调用报错），
    # 所以这两条没有守卫。
    "agent_skill",
    "go_skill_run",
    # Python 侧新增：按 id 回查消息 / 转发的完整内容（配合引用 / 转发的慢取占位）。
    # 非 QQ 平台调用时返回"不支持"而不是报错，所以没有守卫。
    "fetch_message",
    # Python 侧新增（1.0.10）：只读的当前时间查询（``get_current_time``）。时间服务
    # 未装配时工具回退到进程级默认实例，因此同样没有守卫。
    "get_current_time",
)

#: 需要 ``RegisterOptions`` 里对应字段才注册的工具：键 = Go ``RegisterAll`` 的守卫字段，
#: 值 = 该字段满足时一并注册的工具组。字段缺失/为空/``Enabled()`` 为假即**整组不注册**。
CONDITIONAL_TOOLS: dict[str, tuple[str, ...]] = {
    "resident_memory_store": (
        "resident_memory",
        "resident_memory_read",
        "resident_memory_normal",
        "resident_memory_core",
    ),
    "angel_memory": ("angel_remember", "angel_recall"),
    # Go 的 angel_forget 由 ``NewAngelMemoryTools`` 在 ``AllowForget`` 时追加
    # （angel_memory.go:51-54），所以除服务外还要这个开关。
    "angel_memory_allow_forget": ("angel_forget",),
    "self_learning": ("self_learning_review",),
    "long_memory_dir": ("long_memory", "long_memory_search", "long_memory_write"),
    "cron_service": ("cron", "cron_query", "cron_write"),
    "file_manager": ("send_file",),
    "chat_history": (
        "get_media",
        "view_image",
        "search_chat_history",
        "get_chat_history_around",
        "reply_to_chat_history_message",
    ),
    "group_analysis_service": ("group_analysis",),
    # Go 的守卫是 ``CharacterStore != nil && CharacterStore.Enabled()``。
    "character_store": (
        "character_list",
        "character_read",
        "character_search",
        "character_manage",
        "character_delete",
    ),
    "image_profiles": ("image_generate",),
    # Go 的守卫是 ``Artists.Enabled()``（有可写画师串库）；artist_extract 不在此列。
    "artists": (
        "artist_library_search",
        "artist_library_read",
        "artist_library_save",
        "artist_library_delete",
    ),
    "image_prompt_service": ("image_to_prompt",),
    # Go 的守卫是 ``SkillManager != nil``（``register.go:237-250``）。注意 Go 的两条
    # 无守卫 skill 工具在 :data:`ALWAYS_REGISTERED_TOOLS` 里（它们容忍 nil manager）。
    "skill_manager": (
        "create_atrix_skill",
        "read_atrix_skill",
        "modify_atrix_skill",
        "finalize_atrix_skill",
    ),
}

#: 本仓 ``register_all`` 已接线的**全部**工具名 = 无条件 + 条件注册。
#:
#: 它**不是**「最小配置下必然出现的工具集」——那取决于走哪条装配路径：
#:
#: * ``register_all(Registry(), RegisterOptions())``（全空选项；Go 里**没有**这条路径）
#:   恰好在 :data:`ALWAYS_REGISTERED_TOOLS`（13 个）；
#: * 最小**真实**装配 ``build_tool_runtime(ToolRuntimeOptions(config_dir=...))`` 是 **25** 个：
#:   ``build_tool_runtime`` 自己补齐 ``resident_memory_store`` / ``long_memory_dir`` /
#:   ``file_manager`` / ``skill_manager`` 四组守卫对象，与 Go 的 ``builtin.NewRuntime``
#:   （``internal/tool/builtin/runtime.go:78,98,194,195,219``）同义。
REGISTERED_TOOLS: tuple[str, ...] = ALWAYS_REGISTERED_TOOLS + tuple(
    name for group in CONDITIONAL_TOOLS.values() for name in group
)

#: Go ``RegisterAll`` 里有、但本仓 ``register_all`` 尚未**接线**的工具（Lead 排期用）。
#:
#: 现在是**空**的：``internal/tool/skill`` 一族的 6 个工具已接线（见
#: :data:`ALWAYS_REGISTERED_TOOLS` 与 :data:`CONDITIONAL_TOOLS` 的 ``skill_manager``），
#: ``RegisterAll`` 的每一条注册语句都有对应实现。
UNPORTED_TOOLS: tuple[str, ...] = ()


@dataclass
class RegisterOptions:
    """对应 Go ``RegisterOptions``（只保留本仓已能接线的字段）。"""

    runtime_info: RuntimeInfo = field(default_factory=RuntimeInfo)
    resident_memory_store: Any = None
    angel_memory: Any = None
    angel_memory_allow_forget: bool = False
    self_learning: Any = None
    chat_history: Any = None
    long_memory_dir: str = ""
    cron_service: Any = None
    group_analysis_service: Any = None
    group_analysis_max_messages: int = 0
    skill_manager: Any = None
    file_manager: FileManager | None = None
    file_backups: Any = None
    process_env: Environment = field(default_factory=Environment)
    child_process_env: Environment = field(default_factory=Environment)
    config_dir: str = ""
    web_search: Any = None
    web_extract: Any = None
    #: 后台（cron / Atrinis）无人值守的 shell 放行策略；``None`` 用 ShellTool 内置默认。
    background_shell_policy: Any = None
    character_store: Any = None
    image_profiles: dict[str, ImageProfile] = field(default_factory=dict)
    default_image_profile: str = ""
    image_limiter: Any = None
    artists: ArtistTools = field(default_factory=ArtistTools)
    prompt_rewriter: Any = None
    image_prompt_service: Any = None
    #: Python 侧新增：统一时间服务（``get_current_time`` 用）；``None`` = 回退默认实例。
    time_service: Any = None
    #: Python 侧新增：网络时间校验状态提供者（只读快照，绝不在工具里触网）。
    time_verification: Any = None


def register_all(registry: Registry, opts: RegisterOptions) -> None:
    """把当前已接线的内置工具注册进 ``registry``。对应 Go ``RegisterAll``。

    每个工具是否注册由 :data:`ALWAYS_REGISTERED_TOOLS` / :data:`CONDITIONAL_TOOLS`
    描述，与 Go 的守卫一一对应；注册顺序也与 Go 保持一致（便于人工逐行比对）。
    注册失败（重名 / 无名）由 :meth:`atri_x.tool.Registry.register` 抛
    :class:`atri_x.errors.InvalidError`，这里直接让它冒泡（Go 是返回 error）。
    """
    info = opts.runtime_info.normalize()
    media_center = opts.file_manager.media if opts.file_manager is not None else None
    media_runtime = (
        MediaRuntime(media_center, info.sandbox_root)
        if media_center is not None
        else None
    )
    # Go ``register.go:57-59``：skill 扫描器拿到同一个媒体运行时，AgentSkill 的
    # 可调用命令才能在 call 里交接媒体。
    if opts.skill_manager is not None:
        opts.skill_manager.scanner.media = media_runtime

    # Go 用 ``skillManager.EnsureLoaded`` 作为 discover 前的钩子；未落地时留 None。
    before_discover = None
    if opts.skill_manager is not None:
        candidate = getattr(opts.skill_manager, "ensure_loaded", None)
        if callable(candidate):
            before_discover = candidate
    registry.register(new_discover_tool(registry, before_discover))

    registry.register(new_workspace_tool())

    if opts.resident_memory_store is not None:
        for memory_tool in new_resident_memory_tools(opts.resident_memory_store):
            registry.register(memory_tool)

    if opts.angel_memory is not None:
        for angel_tool in new_angel_memory_tools(
            opts.angel_memory,
            AngelMemoryToolOptions(allow_forget=opts.angel_memory_allow_forget),
        ):
            registry.register(angel_tool)

    if opts.self_learning is not None:
        registry.register(new_self_learning_review_tool(opts.self_learning, info))

    if opts.long_memory_dir:
        for memory_tool in new_long_memory_tools(opts.long_memory_dir):
            registry.register(memory_tool)

    if opts.cron_service is not None:
        for cron_tool in new_cron_tools(opts.cron_service, info):
            registry.register(cron_tool)

    if opts.file_manager is not None:
        registry.register(new_send_file_tool(opts.file_manager))

    if opts.chat_history is not None:
        search = new_search_chat_history_tool(opts.chat_history, info)
        around = new_get_chat_history_around_tool(opts.chat_history, info)
        if media_center is not None:
            search.center = media_center
            around.center = media_center
        registry.register(new_get_media_tool(opts.chat_history, search.center))
        registry.register(new_view_image_tool(opts.chat_history, search.center))
        registry.register(search)
        registry.register(around)
        registry.register(
            new_reply_to_chat_history_message_tool(opts.chat_history, info)
        )

    # Python 侧新增：按 id 回查消息 / 转发的完整内容（配合引用 / 转发的慢取占位）。
    registry.register(new_fetch_message_tool())

    # Python 侧新增（1.0.10）：只读当前时间（含来源与校验状态）。
    registry.register(new_get_current_time_tool(opts.time_service, opts.time_verification))

    if opts.group_analysis_service is not None:
        registry.register(
            new_group_analysis_tool(
                opts.group_analysis_service, opts.group_analysis_max_messages, info
            )
        )

    registry.register(
        new_web_search_tool(opts.process_env, specs=specs_from_config(opts.web_search) or None)
    )
    registry.register(
        new_web_extract_tool(
            opts.process_env,
            specs=extract_specs_from_config(opts.web_extract) or None,
        )
    )

    if opts.character_store is not None and _enabled(opts.character_store):
        for character_tool in new_character_tools(opts.character_store, media_center):
            registry.register(character_tool)

    if opts.image_profiles:
        image_tool = ImageGenerateTool(
            opts.image_profiles,
            opts.default_image_profile,
            opts.character_store,
            media_center,
            opts.chat_history,
            opts.prompt_rewriter,
            opts.image_limiter,
        )
        image_tool.artists = opts.artists
        registry.register(image_tool)

    # artist_extract 不需要串库，所以只要能生图就注册；串库工具只在库存在时
    # 注册，因为没有一个时它们每个都只能失败。
    registry.register(new_artist_extract_tool(opts.artists))
    if _artists_enabled(opts.artists):
        for artist_tool in new_artist_library_tools(opts.artists):
            registry.register(artist_tool)

    if opts.image_prompt_service is not None:
        image_to_prompt_tool = new_image_to_prompt_tool(
            media_center, opts.image_prompt_service
        )
        image_to_prompt_tool.artists = opts.artists
        registry.register(image_to_prompt_tool)

    registry.register(PromptLibrarySearchTool())

    file_guard = _build_file_guard(opts)
    registry.register(ReadFileTool(file_guard))
    registry.register(EditFileTool(file_guard, opts.file_backups))
    registry.register(new_rollback_file_tool(opts.file_backups, file_guard))

    shell_env = opts.child_process_env
    if not shell_env.configured():
        shell_env = opts.process_env
    shell = new_shell_tool_with_environment(shell_env, file_guard)
    if opts.background_shell_policy is not None:
        shell.background_shell_policy = opts.background_shell_policy
    shell.media = media_runtime
    registry.register(shell)
    registry.register(new_atriwisp_creator_tool(info))

    # skill 一族（Go ``register.go:225-250``）。注册顺序与守卫都逐条对齐：
    # catalog 只在 manager 存在时取（Go 里是 ``(*skill.Catalog)(nil)``），
    # agent_skill / go_skill_run 无条件注册、容忍 nil，另外四个挂 manager 守卫。
    catalog = opts.skill_manager.catalog if opts.skill_manager is not None else None
    registry.register(new_agent_skill_tool(opts.skill_manager))
    go_runner = new_go_runner(catalog, shell_env)
    go_runner.media = media_runtime
    registry.register(go_runner)
    if opts.skill_manager is not None:
        registry.register(new_create_atrix_skill_tool(opts.skill_manager))
        registry.register(new_read_atrix_skill_tool(opts.skill_manager))
        registry.register(new_modify_atrix_skill_tool(opts.skill_manager))
        registry.register(new_finalize_atrix_skill_tool(opts.skill_manager))


def _enabled(target: Any) -> bool:
    """``CharacterStore.Enabled()`` 的鸭子类型版本。"""
    checker = getattr(target, "enabled", None)
    return bool(checker()) if callable(checker) else target is not None


def _artists_enabled(artists: ArtistTools) -> bool:
    """``ArtistTools.Enabled()``。"""
    checker = getattr(artists, "enabled", None)
    return bool(checker()) if callable(checker) else False


def _build_file_guard(opts: RegisterOptions) -> FileGuard:
    """对应 Go ``RegisterAll`` 里组装 FileGuard 的那几段。"""
    guard = new_file_guard()
    if opts.skill_manager is not None:
        guard.add_rule(new_atrix_skill_file_guard_rule(opts.skill_manager.root))
    if opts.resident_memory_store is not None:
        path = getattr(opts.resident_memory_store, "path", "")
        if path:
            guard.add_rule(new_resident_memory_file_guard_rule(path))
    if opts.long_memory_dir:
        guard.add_rule(new_long_memory_file_guard_rule(opts.long_memory_dir))
    return guard


def new_registry_with_builtin(opts: RegisterOptions) -> Registry:
    """便捷入口：新建 Registry 并注册全部已落地内置工具。"""
    registry = Registry()
    register_all(registry, opts)
    return registry


def with_config_env_dir_on_ctx(ctx: Any, config_dir: str) -> Any:
    """给工具 ctx 补上 ``builtinEnv`` 需要的配置目录（对应 Go ``WithConfigEnvDir``）。"""
    if not config_dir:
        raise InvalidError("config dir is required")
    return with_config_env_dir(ctx, config_dir)


__all__ = [name for name in dir() if not name.startswith("_")]
