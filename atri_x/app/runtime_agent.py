"""Agent 装配。

对应 Go：``internal/app/runtime.go`` 的 ``buildAgent`` 与三个 profile 表构建助手。

从 :mod:`atri_x.app.runtime` 拆出（PORTING §2 单文件行数上限）。

与 Go 的差异：

* ``agt.StartGroupServices(ctx)`` / ``agt.StartRuntimeStateWatch(ctx)`` 在 Go 里是
  **同步调用、不等返回值**；Python 侧 ``Agent.start_group_services`` 返回常驻
  ``asyncio.Task``（``while True: sleep``，群服务默认启用），因此这里同样只调用、
  不 ``await`` —— 否则 ``build_agent`` 永远不返回（见函数内注释）。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.asr import build_audio_transcriber
from atri_x.app.naming import NamingLogger
from atri_x.app.ops_health import VisionMetricsState
from atri_x.app.runtime_support import audit_func, register_turn_alias
from atri_x.app.vision import VisionChatDescriber, build_vision_describer
from atri_x.config import ModelSelection
from atri_x.delivery import new_manager as new_delivery_manager
from atri_x.session import Config as SessionConfig

__all__ = ["build_agent", "buildAgent"]


def _is_enabled(section: Any) -> bool:
    """``[xxx] enabled`` 判定（与 runtime.py 里的同名助手一致）。

    刻意重复 4 行而不是跨模块 import：避免 runtime.py ↔ runtime_agent.py 成环。
    """
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


async def build_agent(
    ctx: Any,
    foundation: Any,
    models: Any,
    platforms: Any,
    tool_runtime: Any,
    security_policy: Any,
    hooks: DefaultManager,
    hook_runtime: Any,
    hook_service: HookControlService,
    vision_metrics: VisionMetricsState,
    file_backups: Any,
) -> Any:
    """对应 Go ``buildAgent``。"""
    from atri_x.agent import Options as AgentOptions
    from atri_x.agent import new_options as new_agent
    from atri_x.app.doctor import DoctorService

    cfg = foundation.config
    model_profiles, model_aliases = _model_profiles(cfg, models)
    tool_profiles, tool_aliases = _tool_profiles(cfg)
    image_profiles, image_aliases = _image_profiles(cfg, tool_runtime)

    vision_describer = build_vision_describer(
        ctx, cfg, models, vision_metrics.counters_value()
    )
    if isinstance(vision_describer, VisionChatDescriber):
        vision_metrics.set("fallback", vision_describer.service)
    vision_selection = ModelSelection()
    if _is_enabled(getattr(cfg, "vision", None)):
        section = getattr(cfg, "vision", None)
        vision_selection = ModelSelection(
            provider=str(getattr(section, "provider", "") or ""),
            model=str(getattr(section, "model", "") or ""),
        )

    audio_transcriber = build_audio_transcriber(ctx, cfg)
    asr_cfg = getattr(cfg, "asr", None)
    asr_normalized = asr_cfg.normalized() if asr_cfg is not None and callable(getattr(asr_cfg, "normalized", None)) else asr_cfg
    asr_selection = ModelSelection()
    if _is_enabled(asr_cfg):
        asr_selection = ModelSelection(
            provider=str(getattr(asr_normalized, "provider", "") or ""),
            model=str(getattr(asr_normalized, "model", "") or ""),
        )

    group_analysis_selection = ModelSelection()
    default_selection = (
        cfg.default_model_selection()
        if callable(getattr(cfg, "default_model_selection", None))
        else ModelSelection()
    )
    if (
        default_selection.provider
        and default_selection.model
        and (models.by_provider or {}).get(default_selection.provider) is not None
    ):
        group_analysis_selection = default_selection

    session_cfg = getattr(cfg, "session", None)
    naming_cfg = getattr(session_cfg, "naming", None)
    maintenance = getattr(cfg, "maintenance", None)
    session_idle = getattr(session_cfg, "idle_expiration", None)

    agent = new_agent(
        AgentOptions(
            platform=platforms.primary,
            clients=models.by_provider,
            mode_models=getattr(cfg, "mode_models", None),
            providers=getattr(cfg, "providers", None),
            state_path=str(getattr(cfg, "state_config_path", "") or ""),
            context_overflow=getattr(cfg, "context_overflow", None),
            group_policy=getattr(cfg, "group_policy", None),
            group_knowledge=getattr(cfg, "group_knowledge", None),
            group_services=getattr(cfg, "group_services", None),
            store=foundation.store,
            media=tool_runtime.file_manager.media,
            vision_describer=vision_describer,
            vision_selection=vision_selection,
            audio_transcriber=audio_transcriber,
            asr_selection=asr_selection,
            asr_parallelism=int(getattr(asr_normalized, "max_concurrent", 0) or 0),
            asr_max_segments=int(getattr(asr_normalized, "max_segments", 0) or 0),
            asr_max_audio_bytes=int(getattr(asr_normalized, "max_audio_bytes", 0) or 0),
            group_analysis_selection=group_analysis_selection,
            command_prefixes=list(getattr(getattr(cfg, "commands", None), "prefixes", None) or []),
            session_config=SessionConfig(
                trigger_step=int(getattr(naming_cfg, "trigger_step", 0) or 0),
                default_mode=str(getattr(session_cfg, "default_mode", "") or ""),
            ),
            naming_selection=getattr(cfg, "naming_model", None),
            naming_notifier=NamingLogger(foundation.logger, audit_func(foundation.logs)),
            soul_path=str(getattr(getattr(cfg, "soul", None), "path", "") or ""),
            resident_memory_store=tool_runtime.resident_memory_store,
            angel_memory=tool_runtime.angel_memory,
            angel_memory_config=getattr(cfg, "angel_memory", None),
            self_learning=tool_runtime.self_learning,
            character_store=tool_runtime.character_store,
            llm_request_config=getattr(cfg, "llm_request", None),
            ops=getattr(cfg, "ops", None),
            budget_limits=getattr(cfg, "budget_limits", None),
            pricing=getattr(maintenance, "daily_report", None),
            hook_service=hook_service,
            hook_manager=hooks,
            hook_runtime=hook_runtime,
            output_manager=new_delivery_manager(None, foundation.logger),
            logs=foundation.logs,
            tool_registry=tool_runtime.registry,
            file_backups=file_backups,
            doctor=DoctorService(str(getattr(cfg, "config_path", "") or "")),
            skills=tool_runtime.skill_manager,
            security_policy=security_policy,
            context_config=getattr(cfg, "context", None),
            model_metadata=getattr(cfg, "model_metadata", None),
            compact_model=getattr(cfg, "compact_model", None),
            session_list_page_size=int(
                getattr(getattr(cfg, "view", None), "session_list_page_size", 0) or 0
            ),
            cleanup_retention_days=int(
                getattr(
                    getattr(maintenance, "session_cleanup", None), "retention_days", 0
                )
                or 0
            ),
            media_retention_days=int(
                getattr(
                    getattr(maintenance, "sandbox_cleanup", None), "retention_days", 0
                )
                or 0
            ),
            session_idle_expiration=session_idle,
            sandbox_root=str(getattr(getattr(cfg, "sandbox", None), "root", "") or ""),
            tools_config=getattr(cfg, "tools", None),
            tool_tags_path=str(getattr(cfg, "tool_tags_config_path", "") or ""),
            tool_tags=getattr(cfg, "tool_tags", None),
            model_profiles=model_profiles,
            model_aliases=model_aliases,
            tool_profiles=tool_profiles,
            tool_aliases=tool_aliases,
            image_profiles=image_profiles,
            image_aliases=image_aliases,
            turn_directives=getattr(cfg, "turn_directives", None),
        )
    )
    # 把三组同僚实现的服务真正装到 Agent 上（``atri_x/agent/bootstrap.py``）。
    # 不做这一步 ``atri_x/agent/seams.py`` 只会走**保守降级**：预算、限流、上下文
    # 压缩、工具预载全部静默失效 —— Agent 能构造、测试也全绿，只是能力悄悄没了。
    # installer 是幂等的，缺件只记 warning 不抛，所以这里不阻断启动。
    #
    # ``opts`` 必须显式带上工具运行时：``bootstrap._wire_extra_host_fields`` 读的是
    # ``opts["registry"]``（对应 Go ``core.go:428-434`` 的
    # ``SetToolRuntime(opts.ToolRegistry, opts.Skills)``），而它拿不到
    # ``agent.Options`` —— 只传 ``agent`` 会让 ``registry is None`` 直接 return，
    # 内置工具注册表就静默丢掉（模型一个工具都看不到）。
    try:
        from atri_x.agent.bootstrap import install_agent_services

        report = install_agent_services(
            agent,
            opts={
                "registry": tool_runtime.registry,
                "skills": tool_runtime.skill_manager,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 装配失败不能阻断启动
        foundation.logger.warn(
            "agent service installation failed",
            event="agent_service_install",
            error=f"{type(exc).__name__}: {exc}",
        )
    else:
        if report.failed:
            foundation.logger.warn(
                "agent service installation incomplete",
                event="agent_service_install",
                failed=[name for name, _detail in report.failed],
            )
    # Go：``agt.StartGroupServices(ctx)`` / ``agt.StartRuntimeStateWatch(ctx)``
    # 都是**同步调用、不等待**，两者各自在内部起一个后台循环。
    # 这里不能 ``await`` ``start_group_services`` 的返回值：它是常驻 Task
    # （``while True: sleep``），而群服务默认 ``enabled=None`` → 启用，
    # await 它等于等进程结束 —— 真实 ``build_agent`` 会永久停在 runtime 阶段
    # （实测 25 秒后事件循环仍在空转，启动既不报错也不继续）。
    start_group = getattr(agent, "start_group_services", None)
    if callable(start_group):
        start_group(ctx)
    watch = getattr(agent, "start_runtime_state_watch", None)
    if callable(watch):
        watch(ctx)
    return agent


def _model_profiles(cfg: Any, models: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """对应 Go ``buildAgent`` 里遍历 ``cfg.ModelProfiles`` 的那一段。"""
    profiles: dict[str, Any] = {}
    aliases: dict[str, str] = {}
    for name, profile in (getattr(cfg, "model_profiles", None) or {}).items():
        provider = str(getattr(profile, "provider", "") or "").strip()
        model = str(getattr(profile, "model", "") or "").strip()
        if not provider or not model:
            continue
        if (models.by_provider or {}).get(provider) is None:
            continue
        profiles[name] = ModelSelection(provider=provider, model=model)
        register_turn_alias(aliases, name, name)
        for alias in getattr(profile, "aliases", None) or []:
            register_turn_alias(aliases, alias, name)
    return profiles, aliases


def _tool_profiles(cfg: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    profiles: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for name, profile in (getattr(cfg, "tool_profiles", None) or {}).items():
        tools = list(getattr(profile, "tools", None) or [])
        if not tools:
            continue
        profiles[name] = tools
        register_turn_alias(aliases, name, name)
        for alias in getattr(profile, "aliases", None) or []:
            register_turn_alias(aliases, alias, name)
    return profiles, aliases


def _image_profiles(cfg: Any, tool_runtime: Any) -> tuple[dict[str, bool], dict[str, str]]:
    profiles: dict[str, bool] = {}
    aliases: dict[str, str] = {}
    if tool_runtime is None:
        return profiles, aliases
    generation_profiles = getattr(
        getattr(cfg, "image_generation", None), "profiles", None
    ) or {}
    for name in tool_runtime.image_profiles:
        if not str(name or "").strip():
            continue
        profiles[name] = True
        register_turn_alias(aliases, name, name)
        profile = generation_profiles.get(name)
        for alias in getattr(profile, "aliases", None) or []:
            register_turn_alias(aliases, alias, name)
    return profiles, aliases



buildAgent = build_agent
