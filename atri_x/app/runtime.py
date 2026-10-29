"""运行期装配主链：媒体中心 → 工具运行时 → Hook → Agent → 生命周期。

对应 Go：``internal/app/runtime.go``。

装配顺序（与 Go 完全一致，顺序有依赖）：

1. ``.env`` 与进程环境：shell/skill/子进程只拿非凭据变量，宿主侧工具用完整环境；
2. cron 服务（它需要一个"Agent 就绪后才可用"的 ``send_notice`` 闭包）；
3. 媒体中心 + 中断媒体恢复 + 历史对账 + 磁盘守卫；
4. 生图改写器、共享视觉计数、文件回滚仓、``image_to_prompt`` 引擎；
5. 工具运行时（app 侧装配，见 :mod:`atri_x.app.tool_runtime`）；
6. 群分析报告 cron handler 与保留期清理注册；
7. skill 延迟重载；
8. Hook 管理器 + 安全策略 + atriena 总线 + Hook 运行时的告警攒存；
9. Hook 服务与内置 Hook 注册；
10. Agent 构造、历史策略接线、cron runner 接线、维护报告发送器接线。

PORTING §2 的单文件行数上限把三个大块拆到了同包模块：

* :mod:`atri_x.app.runtime_support` —— 审计入口、凭据解析、cron 服务、
  ``HookRuntimeLifecycle``、日志小助手；
* :mod:`atri_x.app.runtime_hooks` —— ``build_hook_service``；
* :mod:`atri_x.app.runtime_agent` —— ``build_agent`` 与 profile 表构建。

与 Go 的差异：

* Go 的 ``builtin.NewRuntime`` 在 ``internal/tool/builtin``；本仓那个包没有交付
  ``Runtime`` 壳，装配改放 :mod:`atri_x.app.tool_runtime`（原因见该文件 docstring）。
* Go 的 ``sendNotice`` 闭包直接返回 ``delivery.Receipt``；Python 侧是协程。
* Hook 加载错误在 Go 里走 ``HookReload`` 的第二返回值；Python 的 ``Loader`` 契约
  只能返回 ``(report, configs)``，因此错误被折进 ``report.notices``
  （见 :func:`atri_x.app.runtime_hooks.build_hook_service`）。
* 媒体 / 存储的部分访问器按 Python 侧现状适配（``store.media()`` 是方法而非字段）。
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

from atri_x.app.dependencies import RuntimeComponents, RuntimeRequest
from atri_x.app.image_rewriter import build_image_prompt_rewriter
from atri_x.app.image_to_prompt import build_image_prompt_service
from atri_x.app.ops_health import VisionMetricsState
from atri_x.app.platforms import hook_notice_target
from atri_x.app.runtime_agent import build_agent
from atri_x.app.runtime_hooks import build_hook_service
from atri_x.app.runtime_support import (
    HookRuntimeLifecycle,
    _group_analysis_summarizer,
    _register_group_analysis,
    _register_retention_cleanups,
    audit_func,
    build_cron_service,
    config_dir_of,
    log_error as _log_error,
    max_int64,
    register_turn_alias,
    resolve_file_delivery_credentials,
    schedule as _schedule,
    warn as _warn,
)
from atri_x.app.support import call_bool, maybe_await
from atri_x.app.tool_runtime import ToolRuntimeOptions, build_tool_runtime
from atri_x.config import load_dot_env, plugin_config_dir
from atri_x.delivery import Notice, Target, text
from atri_x.errors import InvalidError
from atri_x.hook.manager import new_manager as new_hook_manager
from atri_x.hook.process import new_process_environment
from atri_x.hook.runtime import Options as HookRuntimeOptions
from atri_x.hook.runtime import new_manager as new_hook_runtime
from atri_x.media import new_configured as new_media_manager
from atri_x.ops.diskguard import Config as DiskGuardConfig
from atri_x.ops.diskguard import new as new_disk_guard
from atri_x.processenv import new as new_process_env
from atri_x.security import NewPolicy as new_security_policy
from atri_x.utils.fileops import new_rollback_store

__all__ = [
    "DefaultRuntimeFactory",
    "defaultRuntimeFactory",
    "build_cron_service",
    "build_hook_service",
    "build_agent",
    "audit_func",
    "register_turn_alias",
    "max_int64",
    "config_dir_of",
    "resolve_file_delivery_credentials",
    "HookRuntimeLifecycle",
    "hookRuntimeLifecycle",
    "maxInt64",
]

#: 每日会话总结交给压缩模型时的单次输入/输出上限（token）。摘要不是正文，1200 够用。
_SUMMARY_MAX_TOKENS = 1200


# ---------------------------------------------------------------------------
# 默认运行时工厂
# ---------------------------------------------------------------------------


class DefaultRuntimeFactory:
    """对应 Go ``defaultRuntimeFactory``。"""

    async def build(self, ctx: Any, req: RuntimeRequest) -> RuntimeComponents:
        """对应 Go ``Build``。"""
        foundation = req.foundation
        if foundation is None:
            raise InvalidError("app: runtime factory requires foundation components")
        cfg = foundation.config
        logger = foundation.logger
        config_dir = config_dir_of(cfg)
        # 统一时间服务：Foundation 已按 ``[time]`` 建好；旧装配路径（测试桩 / 裁剪版）
        # 没提供时在这里补一个，保证工具与提示词永远有同一个时间口径。
        time_service = getattr(foundation, "time_service", None)
        if time_service is None:
            from atri_x.time_service import TimeService

            time_service = TimeService.from_config(getattr(cfg, "time", None))

        dot_env = load_dot_env(config_dir)
        env_entries = [f"{key}={value}" for key, value in os.environ.items()]
        base_process_env = new_process_env(env_entries)
        credential_env = base_process_env.fill(dot_env)
        # Shell / Skill / Go Skill 子进程很强，只给 .env 里的非凭据变量；
        # 宿主侧工具（联网搜索、生图、媒体下载）仍然用 credential_env 解析密钥。
        shell_process_env = credential_env.without_sensitive_keys()
        hook_process_env = new_process_environment(env_entries, dict(dot_env))

        try:
            file_delivery_credentials = resolve_file_delivery_credentials(
                getattr(cfg, "file_delivery", None), config_dir
            )
        except Exception as exc:  # noqa: BLE001 - S3 不可用不应阻断启动
            _warn(
                logger,
                "S3 media backend is unavailable; remote operations will fail until "
                "configuration is fixed",
                error=str(exc),
            )
            file_delivery_credentials = None

        state: dict[str, Any] = {"agent": None}

        async def send_notice(cctx: Any, target: Any, outputs: Any) -> Any:
            agent = state["agent"]
            if agent is None:
                raise InvalidError("agent is not ready")
            return await maybe_await(
                agent.send_notice(cctx, Notice(target=target, outputs=list(outputs or [])))
            )

        cron_service = await build_cron_service(ctx, foundation, send_notice)

        sandbox_root = str(getattr(getattr(cfg, "sandbox", None), "root", "") or "")
        storage_cfg = getattr(cfg, "storage", None)
        media_center = new_media_manager(
            foundation.store,
            os.path.join(os.path.dirname(sandbox_root), "media"),
            getattr(cfg, "file_delivery", None),
            file_delivery_credentials,
        )
        store_media = getattr(foundation.store, "media", None)
        if callable(store_media):
            await maybe_await(store_media().recover_interrupted())
        media_center.history = foundation.chat_history
        await maybe_await(media_center.reconcile_history(ctx))
        platform_files = getattr(cfg, "platform_files", None)
        media_center.max_import_bytes = int(
            getattr(platform_files, "max_receive_file_bytes", 0) or 0
        )
        media_center.download_timeout = float(
            getattr(platform_files, "download_timeout_secs", 0) or 0
        )
        media_center.media = getattr(cfg, "media", None)
        artist_metadata = _build_artist_metadata(cfg, logger)
        if artist_metadata is not None:
            # 入库转码**之前**的原始字节只会经过这里；不挂上就永远拿不到
            # 超大 PNG 里的 A1111 / NovelAI / ComfyUI 参数。
            media_center.generation = artist_metadata
        media_center.logger = logger
        media_center.guard = new_disk_guard(
            os.path.dirname(str(getattr(storage_cfg, "sessions_sqlite_path", "") or "")),
            DiskGuardConfig(
                warn_ratio=float(getattr(storage_cfg, "disk_warn_ratio", 0.0) or 0.0),
                critical_ratio=float(
                    getattr(storage_cfg, "disk_critical_ratio", 0.0) or 0.0
                ),
                min_free_bytes=max_int64(
                    int(getattr(storage_cfg, "disk_min_free_bytes", 0) or 0), 0
                ),
            ),
        )
        if foundation.maintenance is not None:
            foundation.maintenance.media = media_center

        image_rewriter = build_image_prompt_rewriter(cfg, req.models)
        vision_metrics = VisionMetricsState()
        file_backups = new_rollback_store()
        image_to_prompt = build_image_prompt_service(
            ctx, cfg, req.models, vision_metrics.counters_value(), logger=logger
        )
        vision_metrics.set("image_to_prompt", image_to_prompt)

        tool_runtime = build_tool_runtime(
            _tool_options(
                cfg,
                foundation,
                req,
                cron_service,
                media_center,
                image_rewriter,
                image_to_prompt,
                file_backups,
                credential_env,
                shell_process_env,
                artist_metadata,
                logger,
                time_service,
            )
        )
        for warning in tool_runtime.warnings:
            _warn(logger, "builtin runtime degraded", warning=warning)
        if req.profiler is not None:
            req.profiler.mark("builtin tools register")

        await _register_group_analysis(foundation, req, tool_runtime, send_notice)
        _register_retention_cleanups(foundation, tool_runtime)
        if tool_runtime.skill_manager is not None:
            schedule = getattr(tool_runtime.skill_manager, "start_delayed_reload", None)
            if callable(schedule):
                schedule(ctx, 1.0)
        if req.profiler is not None:
            req.profiler.mark("skill reload scheduled")

        hooks = new_hook_manager()
        # 每个 hook 模块的日志都带 module=hook，`/log --hook` 与 `/audit --hook`
        # 就是按它筛选的。
        hook_logger = _with_module(logger, "hook")
        set_logger = getattr(hooks, "set_logger", None)
        if callable(set_logger):
            set_logger(hook_logger)
        security_cfg = getattr(cfg, "security", None)
        from atri_x.security import RiskHigh, RiskLow

        security_policy = new_security_policy(
            _risk_value(getattr(security_cfg, "user_max_tool_risk", ""), RiskLow),
            _risk_value(getattr(security_cfg, "superadmin_confirm_risk", ""), RiskHigh),
            dict(getattr(security_cfg, "superadmins", None) or {}),
        )
        from atri_x.atriena import new_bus as new_atriena_bus

        atriena_bus = new_atriena_bus()

        # 启动期的 Hook 加载告警必须等平台发送器注册好才能送达（那是
        # Integrations.Attach → registerPlatformHooks 的事），而平台发送器在
        # build_agent 之后、本函数返回之前都还没有。因此 Agent 就绪之前先攒起来，
        # 由 runner 在 Attach 之后用 deliverHookNotices 统一投递。
        hook_notices: list[str] = []
        hook_notice_channel = hook_notice_target(
            req.platforms.primary, req.platforms.runtimes
        )

        async def notify_hook_issue(cctx: Any, message: str) -> None:
            body = (message or "").strip()
            if not body:
                return
            agent = state["agent"]
            if agent is None:
                hook_notices.append(body)
                return
            if not hook_notice_channel:
                _warn(logger, "hook notice", text=body)
                return
            try:
                await maybe_await(
                    agent.send_notice(
                        cctx,
                        Notice(
                            target=Target(platform=hook_notice_channel, superadmins=True),
                            outputs=[text(body)],
                            level=30,
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _warn(
                    logger,
                    "hook notice was not delivered",
                    text=body,
                    platform=hook_notice_channel,
                    error=str(exc),
                )

        hook_runtime = new_hook_runtime(
            HookRuntimeOptions(
                media=media_center,
                registry=tool_runtime.registry,
                logger=hook_logger,
                audit=audit_func(foundation.logs),
                send=send_notice,
                shared_dir=os.path.join(plugin_config_dir(cfg.config_path), "_shared"),
                process_env=hook_process_env,
            )
        )

        hook_service = await build_hook_service(
            foundation,
            req.platforms,
            tool_runtime,
            cron_service,
            hooks,
            hook_runtime,
            hook_process_env,
            notify_hook_issue,
            send_notice,
        )
        if req.profiler is not None:
            req.profiler.mark("hook register")

        try:
            agent = await build_agent(
                ctx,
                foundation,
                req.models,
                req.platforms,
                tool_runtime,
                security_policy,
                hooks,
                hook_runtime,
                hook_service,
                vision_metrics,
                file_backups,
                time_service=time_service,
            )
        except BaseException:
            try:
                await maybe_await(hook_runtime.close(None))
            except BaseException:  # noqa: BLE001 - 清理失败不覆盖原错误
                pass
            raise
        state["agent"] = agent
        if foundation.history_policy is not None:
            enabled_for = getattr(agent, "group_history_enabled_for_scope", None)
            if callable(enabled_for):
                foundation.history_policy.set(enabled_for)
        set_runner = getattr(cron_service, "set_runner", None)
        if callable(set_runner):
            set_runner(agent)
        if foundation.maintenance is not None:
            platform = str(
                getattr(
                    getattr(getattr(cfg, "maintenance", None), "daily_report", None),
                    "platform",
                    "",
                )
                or ""
            )
            foundation.maintenance.report_sender = _report_sender(
                state, send_notice, platform
            )
            # 每日会话总结的 LLM 二次摘要（use_llm 时才用，失败自动回退确定性摘要）。
            foundation.maintenance.summary_generator = _summary_generator(state)
        if req.profiler is not None:
            req.profiler.mark("agent init")

        return RuntimeComponents(
            media=media_center,
            agent=agent,
            handler=agent,
            cron_service=cron_service,
            atriena_bus=atriena_bus,
            image_limiter=tool_runtime.image_limiter,
            vision_stats=vision_metrics.snapshot,
            hook_notices=hook_notices,
            lifecycle=HookRuntimeLifecycle(runtime=hook_runtime, agent=agent),
        )

    # Go 命名别名。
    Build = build


def _report_sender(state: dict[str, Any], send_notice: Any, platform: str) -> Any:
    async def sender(cctx: Any, body: str) -> None:
        agent = state["agent"]
        if agent is None:
            raise InvalidError("agent is not ready")
        await maybe_await(
            send_notice(
                cctx,
                Target(platform=platform, superadmins=True),
                [text(body)],
            )
        )

    return sender


def _summary_generator(state: dict[str, Any]) -> Any:
    """把确定性会话摘要交给压缩模型再摘要一遍（``[maintenance.daily_summary].use_llm``）。

    走的是上下文压缩那条既有链路（``ensure_context_runtime`` + ``summarize_text``），
    因此模型选择、provider 客户端、超时与配额口径都与 ``/compact`` 完全一致；
    没配压缩模型时抛 :class:`InvalidError`，维护任务的 handler 会回退到确定性摘要。
    """

    async def generator(cctx: Any, text: str) -> str:
        agent = state["agent"]
        if agent is None:
            raise InvalidError("agent is not ready")
        from atri_x.agent.context_runtime import ensure_context_runtime  # noqa: PLC0415
        from atri_x.storage import SessionModeWork  # noqa: PLC0415

        runtime = ensure_context_runtime(agent)
        selection = runtime.compact_selection(agent.model_for_mode(SessionModeWork))
        if not selection.provider or not selection.model:
            raise InvalidError("未配置压缩模型，无法生成 LLM 会话总结")
        return await runtime.summarize_text(cctx, selection, text, _SUMMARY_MAX_TOKENS)

    return generator


def _with_module(logger: Any, module: str) -> Any:
    method = getattr(logger, "with_", None)
    if callable(method):
        return method("module", module)
    return logger


def _risk_value(raw: Any, fallback: Any) -> Any:
    """``[security]`` 的风险等级既可能是字符串也可能已是枚举值。

    字符串按 Go ``ParseRisk(value, fallback)`` 归一（``security.go:76``）；已经是风险
    等级时原样返回。**必须带 fallback**：Go 的两处调用分别是
    ``ParseRisk(userMaxToolRisk, RiskLow)`` / ``ParseRisk(superadminConfirmRisk, RiskHigh)``
    （``security.go:50-51``）。真实事故：这里原先只传一个实参，真实 ``service run``
    第一次走到就抛 ``parse_risk() missing 1 required positional argument: 'fallback'``
    —— 单测走的是别的策略构造路径，看不见。
    """
    from atri_x.security import ParseRisk

    if isinstance(raw, str):
        return ParseRisk(raw, fallback)
    return raw


def _build_artist_metadata(cfg: Any, logger: Any = None) -> Any:
    """``[image_generation].metadata_store`` → 入库前的生成参数留档库。

    对应 Go ``buildArtistMetadata``。**返回值必须被挂到**
    ``media_center.generation``（入库转码前的原始字节只在这里能拿到）**和**
    ``ArtistTools.Service.Generations``（工具回读）两处；只挂一处就等于没接线。

    教训（1.1.0 实机）：这里曾经 ``from atri_x.artist.metadata import
    GenerationStore``——该模块只有容器解析，没有 ``GenerationStore``——导入必然
    ``ImportError``，又被下面的裸 ``except`` 吞掉，于是**即使配置了
    ``metadata_store``，留档也永远是 None**：超大 PNG 在入库转码时丢掉参数后，
    ``artist_extract`` / ``image_to_prompt`` 再也读不回来。现在导入失败要留 WARN，
    不允许再静默降级。
    """
    path = str(
        getattr(getattr(cfg, "image_generation", None), "metadata_store", "") or ""
    ).strip()
    if not path:
        return None
    try:
        from atri_x.artist.metadata_store import GenerationStore
    except Exception as exc:  # noqa: BLE001 - 缺件只降级，但必须可观测
        _warn(
            logger,
            "image generation metadata archive is unavailable; "
            "generation parameters will be lost when import transcodes an image",
            error=str(exc),
        )
        return None
    store = GenerationStore(path)
    if logger is not None:
        try:
            logger.info(
                "image generation metadata archive enabled", path=store.backing_path()
            )
        except Exception:  # noqa: BLE001 - 日志实现差异不应影响装配
            pass
    return store


def _tool_options(
    cfg: Any,
    foundation: Any,
    req: RuntimeRequest,
    cron_service: Any,
    media_center: Any,
    image_rewriter: Any,
    image_to_prompt: Any,
    file_backups: Any,
    credential_env: Any,
    shell_process_env: Any,
    artist_metadata: Any,
    logger: Any,
    time_service: Any = None,
) -> ToolRuntimeOptions:
    """把 Go ``builtin.RuntimeOptions{...}`` 的字面量逐字段搬过来。"""
    resident = getattr(cfg, "resident_memory", None)
    character_library = getattr(cfg, "character_library", None)
    image_generation = getattr(cfg, "image_generation", None)
    artist_library = getattr(image_generation, "artist_library", None)
    from atri_x.memory.resident import Limits as ResidentLimits
    from atri_x.tool.builtin.runtimeinfo import RuntimeInfo
    from atri_x.tool.builtin.shell_sandbox import background_shell_policy_from_config

    return ToolRuntimeOptions(
        config_dir=config_dir_of(cfg),
        runtime_info=RuntimeInfo(
            config_path=str(getattr(cfg, "config_path", "") or ""),
            sandbox_root=str(getattr(getattr(cfg, "sandbox", None), "root", "") or ""),
            file_delivery=getattr(cfg, "file_delivery", None),
            # 工具侧的 ``Info.now`` 也走统一时间服务（绝不复用 ``datetime.now``）。
            now=getattr(time_service, "now_local", None),
        ),
        time_service=time_service,
        time_verification=getattr(foundation, "time_verifier", None),
        cron_service=cron_service,
        chat_history=foundation.chat_history,
        outbound_messages=foundation.outbound_messages,
        group_analysis=getattr(cfg, "group_analysis", None),
        group_analysis_summarizer=_group_analysis_summarizer(cfg, req.models),
        angel_memory=getattr(cfg, "angel_memory", None),
        self_learning=getattr(cfg, "self_learning", None),
        store=foundation.store,
        media=media_center,
        sandbox_root=str(getattr(getattr(cfg, "sandbox", None), "root", "") or ""),
        file_delivery=getattr(cfg, "file_delivery", None),
        resident_memory_max_units=ResidentLimits(
            core=int(getattr(resident, "core_max_units", 0) or 0),
            normal=int(getattr(resident, "normal_max_units", 0) or 0),
        ),
        resident_memory_policy=resident_normal_write_policy(resident),
        character_enabled=_is_enabled(character_library),
        character_root=str(getattr(character_library, "root", "") or ""),
        character_allow_user_create=bool(
            getattr(character_library, "allow_user_create", True)
        ),
        soul_path=str(getattr(getattr(cfg, "soul", None), "path", "") or ""),
        artist_library_enabled=_is_enabled(artist_library),
        artist_library_root=str(getattr(artist_library, "root", "") or ""),
        artist_metadata_store=artist_metadata,
        image_generation=image_generation,
        web_search=getattr(cfg, "web_search", None),
        web_extract=getattr(cfg, "web_extract", None),
        prompt_rewriter=image_rewriter,
        image_prompt_service=image_to_prompt,
        process_env=credential_env,
        child_process_env=shell_process_env,
        file_backups=file_backups,
        logger=logger,
        background_shell_policy=background_shell_policy_from_config(
            getattr(cfg, "security", None)
        ),
    )


def _call_int(section: Any, name: str) -> int:
    method = getattr(section, name, None)
    return int(method() or 0) if callable(method) else 0


def resident_normal_write_policy(resident: Any) -> Any:
    """``[resident_memory]`` 的 normal 写入策略。

    对应 Go ``runtime.go:140-145``：配置里是**秒**，策略字段是 ``time.Duration``，
    所以这里必须换算成 :class:`datetime.timedelta`。

    真实事故（第三轮验证在真实启动日志里抓到）：这里以前用 ``float(...)`` 直接把秒数
    填进 ``NormalWritePolicy``，而该 dataclass 的 ``min_interval`` / ``window``
    是 ``timedelta`` → ``normalized()`` 抛
    ``TypeError: '>' not supported between instances of 'float' and 'datetime.timedelta'``，
    被 :func:`atri_x.app.tool_runtime._build_resident_store` 吞成一条 WARN →
    **4 个 ``resident_memory*`` 工具在生产路径永远注册不上**（冒烟启动只注册 38 个而不是 42）。
    """
    from atri_x.memory.resident import NormalWritePolicy

    return NormalWritePolicy(
        min_interval=timedelta(
            seconds=_call_int(resident, "normal_write_min_interval_seconds_value")
        ),
        window=timedelta(
            seconds=_call_int(resident, "normal_write_window_seconds_value")
        ),
        max_writes=_call_int(resident, "normal_write_max_per_window_value"),
        max_lines=_call_int(resident, "normal_max_lines_value"),
        max_units_per_entry=_call_int(resident, "normal_max_units_per_entry_value"),
        block_instruction_patterns=call_bool(
            resident, "is_normal_block_instruction_patterns"
        ),
    )


def _is_enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))




# Go 命名别名。
defaultRuntimeFactory = DefaultRuntimeFactory
maxInt64 = max_int64
resolveFileDeliveryCredentials = resolve_file_delivery_credentials
buildCronService = build_cron_service
buildHookService = build_hook_service
buildAgent = build_agent
auditFunc = audit_func
registerTurnAlias = register_turn_alias
hookRuntimeLifecycle = HookRuntimeLifecycle
