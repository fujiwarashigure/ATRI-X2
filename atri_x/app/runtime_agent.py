"""Agent 装配。

对应 Go：``internal/app/runtime.go`` 的 ``buildAgent`` 与三个 profile 表构建助手。

从 :mod:`atri_x.app.runtime` 拆出（PORTING §2 单文件行数上限）。

与 Go 的差异：

* ``agt.StartGroupServices(ctx)`` / ``agt.StartRuntimeStateWatch(ctx)`` 在 Go 里是
  **同步调用、不等返回值**；Python 侧 ``Agent.start_group_services`` 返回常驻
  ``asyncio.Task``（``while True: sleep``，群服务默认启用），因此这里同样只调用、
  不 ``await`` —— 否则 ``build_agent`` 永远不返回（见函数内注释）。

Python 版补上的三条**生产接线**（此前只在测试里显式传参才成立）：

* ``rate_limit_*``：按 ``[ops]`` 的 per-minute/burst 构造入站限流器并随 ``opts``
  传给 bootstrap；``0`` 表示未配置（不构造、由 ``/diagnostics`` 如实标注）。
* ``hook_service``：把 ``app/runtime.py`` 建好的控制面传下去，``/hooks`` 才可用。
* 装配报告：``AgentServicesReport.to_dict()`` 落成结构化日志并挂到宿主，
  ``/diagnostics.agent_services`` 与 doctor 的能力自检复用同一份快照。
"""

from __future__ import annotations

import os
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
        ctx, cfg, models, vision_metrics.counters_value(), logger=foundation.logger
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
            # Python 版新增：``[agent.dsh]`` 深度路由配置（没有该段时为 None）。
            dsh_config=getattr(getattr(cfg, "agent", None), "dsh", None),
            # Python 版新增：工具进度预览文案 / 本地表情图库（没有该段时用默认值）。
            tool_preview_config=getattr(cfg, "tool_preview", None),
            reactions_config=getattr(cfg, "reactions", None),
            quiet_config=getattr(cfg, "quiet", None),
            participation_config=getattr(cfg, "participation", None),
        )
    )
    # 1.0.3：内容安全 guard 与识图开关（不配置 = 完全保持旧行为）。
    _wire_safety_and_vision(agent, cfg, foundation.logger)
    # 把三组同僚实现的服务真正装到 Agent 上（``atri_x/agent/bootstrap.py``）。
    # 不做这一步 ``atri_x/agent/seams.py`` 只会走**保守降级**：预算、限流、上下文
    # 压缩、工具预载全部静默失效 —— Agent 能构造、测试也全绿，只是能力悄悄没了。
    # installer 是幂等的，缺件只记 warning 不抛，所以这里默认不阻断启动；
    # ``[runtime] fail_fast_agent_services = true`` 时改为让启动失败（见下）。
    #
    # ``opts`` 必须显式带上工具运行时：``bootstrap._wire_extra_host_fields`` 读的是
    # ``opts["registry"]``（对应 Go ``core.go:428-434`` 的
    # ``SetToolRuntime(opts.ToolRegistry, opts.Skills)``），而它拿不到
    # ``agent.Options`` —— 只传 ``agent`` 会让 ``registry is None`` 直接 return，
    # 内置工具注册表就静默丢掉（模型一个工具都看不到）。
    #
    # 同一个 ``opts`` 还负责另外两条**生产接线**（此前只写进 ``AgentOptions``，
    # 而 ``Agent.__init__`` 并不落这两个字段，于是能力在真实路径上根本不存在）：
    #
    # * ``rate_limit_*``：按 ``[ops]`` 的 per-minute/burst 构造令牌桶。配置为 0
    #   （默认）时不传任何键 = 显式"未配置"，由 ``/diagnostics`` 如实区分。
    # * ``hook_service``：``app/runtime.py`` 的 ``build_hook_service`` 已经建好控制面
    #   （对应 Go ``buildHookService``），不传下去 ``deps.hooks`` 恒为 ``None``、
    #   ``/hooks`` 会一直回 "hook service is not configured"。
    install_options = _agent_install_options(tool_runtime, hook_service, getattr(cfg, "ops", None))
    _announce_redaction(foundation.logger)
    report_payload = _install_agent_services(agent, install_options, foundation.logger)
    _publish_capabilities(agent, report_payload, install_options, foundation.logger)
    if _fail_fast_agent_services(cfg) and not report_payload.get("ok", False):
        failed = report_payload.get("failed") or []
        detail = ", ".join(
            "%s: %s" % (item[0], item[1]) for item in failed if len(item) >= 2
        ) or "assembly raised before a report could be produced"
        raise RuntimeError(
            "agent services are incomplete and runtime.fail_fast_agent_services=true: "
            + detail
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


def _wire_safety_and_vision(agent: Any, cfg: Any, logger: Any) -> None:
    """把 1.0.3 的内容安全 guard 与识图开关挂到宿主上。

    不配置 ``[safety] enabled`` / ``[vision] trigger`` 时属性保持默认值，集成点因此
    走"什么都不做"的分支——**不配置就不改变 1.0.2 行为**。
    """
    _set_attribute(agent, "safety_guard", _build_safety_guard(cfg, logger))
    section = getattr(cfg, "vision", None)
    _configure_vision_privacy(section, logger)
    trigger = "on_error"
    method = getattr(section, "trigger_value", None)
    if callable(method):
        trigger = str(method())
    elif section is not None:
        trigger = str(getattr(section, "trigger", "") or "on_error").strip().lower()
    if trigger not in ("on_error", "always"):
        trigger = "on_error"
    timeout = 4.0
    timeout_method = getattr(section, "probe_timeout_value", None)
    if callable(timeout_method):
        timeout = float(timeout_method())
    else:
        try:
            timeout = float(getattr(section, "probe_timeout_seconds", 4.0) or 4.0)
        except (TypeError, ValueError):
            timeout = 4.0
    _set_attribute(agent, "vision_trigger", trigger)
    _set_attribute(agent, "vision_probe_timeout_seconds", timeout)
    _set_attribute(
        agent,
        "vision_allow_group_images",
        bool(getattr(section, "allow_group_images", True)),
    )
    detail = str(getattr(section, "detail", "") or "original").strip().lower()
    _set_attribute(
        agent, "vision_detail", detail if detail in ("original", "high", "auto", "low") else "original"
    )


def _configure_vision_privacy(section: Any, logger: Any) -> None:
    """把 ``[vision]`` 的图片外发开关装成**进程级**隐私设置。

    工具路径（``view_image`` / ``image_to_prompt``）不经过 Agent，只认这份进程级
    开关；聊天路径另外还有 ``agent.vision_allow_group_images`` 的前置判定。
    未配置时保持"两个开关都开"（与改造前一致）。
    """
    try:
        from atri_x.vision.privacy import configure_privacy

        configure_privacy(
            allow_external_images=bool(
                getattr(section, "allow_external_images", True)
            ),
            allow_group_images=bool(getattr(section, "allow_group_images", True)),
        )
    except Exception as exc:  # noqa: BLE001 - 隐私层缺件时退回"不额外限制"
        if logger is not None:
            logger.warn(
                "vision privacy switches unavailable; keeping defaults",
                event="vision_privacy_unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )


def _safety_guard_needed(section: Any) -> bool:
    """是否需要装配 guard：全局开了，或任一会话模式的覆盖段把 enabled 打开了。

    默认（``[safety] enabled = false`` 且没有覆盖段）返回 ``False``，即"不配置就
    完全不改变既有行为"——集成点连 guard 都拿不到。
    """
    if section is None:
        return False
    if bool(getattr(section, "enabled", False)):
        return True
    for name in ("chat", "work"):
        override = getattr(section, name, None)
        if override is not None and bool(getattr(override, "enabled", False)):
            return True
    return False


def _build_safety_guard(cfg: Any, logger: Any) -> Any:
    """构造内容安全 guard；未启用/依赖缺失时返回 ``None``（fail-open，不阻断启动）。"""
    section = getattr(cfg, "safety", None)
    if not _safety_guard_needed(section):
        return None
    try:
        from atri_x.safety import SafetyGuard, policy_from_section

        policy = policy_from_section(section)
        config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
        return SafetyGuard(
            policy,
            logger=logger,
            config_dir=config_dir,
            chat=getattr(section, "chat", None),
            work=getattr(section, "work", None),
        )
    except Exception as exc:  # noqa: BLE001 - 安全层不可用不应让整个服务起不来
        if logger is not None:
            logger.warn(
                "safety guard unavailable; content filtering is disabled",
                event="safety_unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )
        return None


def _agent_install_options(
    tool_runtime: Any, hook_service: Any, ops_cfg: Any
) -> dict[str, Any]:
    """装配 ``install_agent_services`` 需要的 ``opts``（只放真的配了的东西）。"""
    options: dict[str, Any] = {
        "registry": tool_runtime.registry,
        "skills": tool_runtime.skill_manager,
    }
    options.update(_rate_limit_options(ops_cfg))
    if hook_service is not None:
        options["hook_service"] = hook_service
    return options


def _rate_limit_options(ops_cfg: Any) -> dict[str, Any]:
    """按 ``[ops]`` 的 ``*_messages_per_minute`` / ``*_burst`` 构造入站限流器。

    ``0`` 表示不限制（见 ``atri_x/config/assets_data/app.toml`` 的 ``[ops]`` 注释），
    因此**一个阈值都不配时不返回任何键**：宿主编排保持"没有限流器"，由
    ``/diagnostics`` 的 ``rate_limit.configured=False`` 如实表达，而不是伪装成
    "限流已启用、0 次拒绝"。
    """
    if ops_cfg is None:
        return {}
    user_per_minute = _non_negative_int(getattr(ops_cfg, "user_messages_per_minute", 0))
    group_per_minute = _non_negative_int(
        getattr(ops_cfg, "group_messages_per_minute", 0)
    )
    if user_per_minute <= 0 and group_per_minute <= 0:
        return {}

    from atri_x.ops.ratelimit import Limit, new as new_limiter

    idle_ttl = float(_non_negative_int(getattr(ops_cfg, "rate_limit_idle_ttl_seconds", 0)))
    options: dict[str, Any] = {}
    if user_per_minute > 0:
        burst = max(1, _non_negative_int(getattr(ops_cfg, "user_burst", 0)))
        options["rate_limit_user"] = new_limiter(
            Limit(rate_per_second=user_per_minute / 60.0, burst=burst, idle_ttl=idle_ttl)
        )
        options["rate_limit_user_per_minute"] = user_per_minute
        options["rate_limit_user_burst"] = burst
    if group_per_minute > 0:
        burst = max(1, _non_negative_int(getattr(ops_cfg, "group_burst", 0)))
        options["rate_limit_group"] = new_limiter(
            Limit(rate_per_second=group_per_minute / 60.0, burst=burst, idle_ttl=idle_ttl)
        )
        options["rate_limit_group_per_minute"] = group_per_minute
        options["rate_limit_group_burst"] = burst
    return options


def _non_negative_int(value: Any) -> int:
    """宽松取非负整数（配置里写成字符串/None 时按 0）。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _announce_redaction(logger: Any) -> None:
    """脱敏降级时在启动日志里报一次 ERROR（模块侧保证进程内只报一次）。"""
    try:
        from atri_x.logging.logging import announce_redact_degradation

        announce_redact_degradation(logger)
    except Exception as exc:  # noqa: BLE001 - 自检失败不能阻断启动
        if logger is not None:
            logger.warn(
                "redaction self-check failed",
                event="redact_unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )


def _install_agent_services(
    agent: Any, options: dict[str, Any], logger: Any
) -> dict[str, Any]:
    """跑装配并把结果落成一条结构化日志 + 机器可读快照。

    旧实现对 ``report.failed`` 只打名字、对 ``skipped``/``warnings`` 完全不打，
    而 ``to_dict()`` 在生产路径零消费。这里把四类结果（含每项原因）一次写全：
    正常情况下是 INFO，有任何跳过/失败/告警时升到 WARN；装配整体抛异常时
    也要留一条可以查的报告，而不是让 ``/diagnostics`` 变成空白。
    """
    try:
        from atri_x.agent.bootstrap import install_agent_services

        report = install_agent_services(agent, opts=options)
        payload = report.to_dict()
    except Exception as exc:  # noqa: BLE001 - 装配失败默认不阻断启动（fail-fast 另判）
        payload = _failed_services_payload("%s: %s" % (type(exc).__name__, exc))
    _log_install_report(logger, payload)
    _set_attribute(agent, "agent_services_report", payload)
    return payload


def _failed_services_payload(error: str) -> dict[str, Any]:
    """装配整体失败时的报告形状（与 :meth:`AgentServicesReport.to_dict` 对齐）。"""
    return {
        "installed": [],
        "skipped": [],
        "failed": [("install", error)],
        "warnings": ["agent service installation raised: " + error],
        "services": [],
        "models": "",
        "reused": False,
        "ok": False,
    }


def _log_install_report(logger: Any, payload: dict[str, Any]) -> None:
    """把装配报告落成一条结构化日志（四类结果全量，每项带原因）。"""
    if logger is None:
        return
    failed = list(payload.get("failed") or [])
    fields: dict[str, Any] = {
        "event": "agent_service_install",
        "ok": bool(payload.get("ok", False)),
        "installed": list(payload.get("installed") or []),
        "skipped": list(payload.get("skipped") or []),
        "failed": failed,
        "warnings": list(payload.get("warnings") or []),
    }
    degraded = bool(failed or fields["skipped"] or fields["warnings"])
    if degraded:
        logger.warn("agent services assembled with degraded entries", **fields)
    else:
        logger.info("agent services assembled", **fields)


def _publish_capabilities(
    agent: Any,
    services_payload: dict[str, Any],
    options: dict[str, Any],
    logger: Any,
) -> None:
    """把能力自检快照挂到宿主上，并同步给 ``/doctor`` 用的 :class:`DoctorService`。

    ``/diagnostics`` 直接从 ``agent.agent_services_report`` 读装配报告；doctor
    （``/doctor`` 命令）自己没法重建 Agent，于是复用同一份快照 —— 拿不到快照时
    它会把检查标成 ``skipped``，绝不伪造"通过"。
    """
    redaction = _redaction_status()
    capabilities = {
        "agent_services": services_payload,
        "rate_limit_configured": any(
            options.get(name) is not None
            for name in ("rate_limit_user", "rate_limit_group")
        ),
        "hook_service": options.get("hook_service") is not None,
        "redaction_available": bool(redaction.get("available", True)),
        "redaction": redaction,
    }
    _set_attribute(agent, "agent_capabilities", capabilities)
    doctor = getattr(agent, "doctor", None)
    binder = getattr(doctor, "bind_capabilities", None)
    if callable(binder):
        try:
            binder(capabilities)
        except Exception as exc:  # noqa: BLE001 - 绑定失败不能阻断启动
            if logger is not None:
                logger.warn(
                    "doctor capability binding failed",
                    event="doctor_capabilities",
                    error="%s: %s" % (type(exc).__name__, exc),
                )


def _redaction_status() -> dict[str, Any]:
    """脱敏自检快照；导入失败按"不可用"处理（保守方向）。"""
    try:
        from atri_x.logging.logging import redact_status

        return dict(redact_status())
    except Exception:  # noqa: BLE001 - 自检失败不能阻断启动
        return {"available": False, "mode": "unknown"}


def _set_attribute(target: Any, name: str, value: Any) -> None:
    """给宿主挂字段；只读/``__slots__`` 宿主静默跳过。"""
    if target is None:
        return
    try:
        setattr(target, name, value)
    except AttributeError:  # pragma: no cover - 只读宿主
        pass


def _fail_fast_agent_services(cfg: Any) -> bool:
    """``[runtime] fail_fast_agent_services``（默认 ``False``，保持既有行为）。"""
    runtime_cfg = getattr(cfg, "runtime", None)
    return bool(getattr(runtime_cfg, "fail_fast_agent_services", False))


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
