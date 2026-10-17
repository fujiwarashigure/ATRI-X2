"""``ModelMixin``：``internal/agent/model.go`` 里挂在 ``*Agent`` 上的模型方法。

状态与共享助手在 :mod:`atri_x.agent.model`（``ModelRuntimeState`` / ``host_of`` /
``install_methods``），模型列表与快照分别在 :mod:`atri_x.agent.model_list` /
:mod:`atri_x.agent.model_snapshot`，服务门面与装配在 :mod:`atri_x.agent.model_service`。

**调用关系**：本模块 import ``model``（单向）；``model`` 不 import 本模块，因此没有导入环。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Any, Iterable, Mapping

from atri_x.agent.model import (
    AgentService,  # noqa: F401 - 便于同组模块从这里再导出
    MAX_USER_ERROR_RUNES,
    ModelListOptions,
    ModelOption,
    ModelRuntimeState,
    _copy_selection,
    _shared,
    clone_mode_models,
    ensure_model_runtime,
    format_delay,
    host_of,
    join_model_matches,
    model_selection_key,
)
from atri_x.config import ModelSelection, ProviderConfig
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import RetryEvent
from atri_x.llm.origin import Origin, can_switch
from atri_x.llm.protocol import protocol_capabilities_of
from atri_x.storage.models import SessionModeChat, SessionModeWork

__all__ = ["ModelMixin", "ensure_context_runtime"]

#: 通知投递不出去时的兜底 logger（对应 Go 的 ``slog`` 默认 logger）。
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent 方法
# ---------------------------------------------------------------------------
class ModelMixin:
    """``internal/agent/model.go`` 里挂在 ``*Agent`` 上的方法。"""

    # -- 当前选择 -----------------------------------------------------------
    def current_model(self) -> str:
        """对应 Go ``CurrentModel``。"""
        return self.current_mode_model().model

    def current_provider(self) -> str:
        """对应 Go ``CurrentProvider``。"""
        return self.current_mode_model().provider

    def current_mode_model(self) -> ModelOption:
        """当前会话模式正在用的模型。对应 Go ``CurrentModeModel``。"""
        selected = self.current_model_for_mode(self.current_mode(None))
        selected.current = True
        return selected

    def current_model_for_mode(self, mode: str) -> ModelOption:
        """对应 Go ``CurrentModelForMode``。"""
        selected = self.model_for_mode(mode)
        return ModelOption(provider=selected.provider, model=selected.model)

    def current_compact_model(self) -> ModelOption:
        """当前会话的压缩模型。对应 Go ``CurrentCompactModel``。"""
        current = self._current_session()
        from atri_x.agent.context_runtime import compact_selection_for_session

        selected = compact_selection_for_session(self, current)
        return ModelOption(
            provider=selected.provider, model=selected.model, compact=True
        )

    def current_naming_model(self) -> ModelOption:
        """命名模型（未配置时返回空 option）。对应 Go ``CurrentNamingModel``。"""
        selected = self.configured_naming_model()
        if not selected.provider or not selected.model:
            return ModelOption()
        return ModelOption(
            provider=selected.provider, model=selected.model, naming=True
        )

    def current_mode(self, ctx: Any) -> str:
        """当前会话模式（无会话时用服务默认模式）。对应 Go ``currentMode``。"""
        sessions = getattr(self, "sessions", None)
        if sessions is None:
            return SessionModeWork
        try:
            current = sessions.current(self.scope(ctx))
        except Exception:  # noqa: BLE001 - Go 侧对任何错误都退回默认模式
            current = None
        mode = getattr(current, "mode", "") or ""
        if not mode:
            return sessions.default_mode()
        return mode

    def _current_session(self) -> Any:
        sessions = getattr(self, "sessions", None)
        if sessions is None:
            return None
        try:
            return sessions.current(self.scope(None))
        except Exception:  # noqa: BLE001 - 没有当前会话不是错误
            return None

    # -- 切换 ---------------------------------------------------------------
    async def select_compact_model(self, arg: str) -> ModelOption:
        """设置压缩模型。对应 Go ``SelectCompactModel``。"""
        selected = await self.select_model_option(arg)
        self.merge_external_runtime_state()
        ensure_context_runtime(self).set_compact_model(
            ModelSelection(provider=selected.provider, model=selected.model)
        )
        selected.compact = True
        self.save_runtime_state()
        return selected

    async def select_naming_model(self, arg: str) -> ModelOption:
        """设置命名模型。对应 Go ``SelectNamingModel``。"""
        selected = await self.select_model_option(arg)
        self.merge_external_runtime_state()
        self.set_naming_model(
            ModelSelection(provider=selected.provider, model=selected.model)
        )
        selected.naming = True
        title_gen = getattr(self, "title_gen", None)
        if title_gen is not None:
            title_gen.set_naming(
                self.client_for_provider(selected.provider),
                selected.provider,
                selected.model,
            )
        self.save_runtime_state()
        return selected

    async def select_model(self, ctx: Any, arg: str) -> ModelOption:
        """切换当前会话模式的模型。对应 Go ``SelectModel``。"""
        return await self.select_model_for_mode(ctx, self.current_mode(ctx), arg)

    async def select_model_for_mode(
        self, ctx: Any, mode: str, arg: str
    ) -> ModelOption:
        """切换某个模式槽位的模型。对应 Go ``SelectModelForMode``。

        顺序与 Go 一致：先吸收外部编辑 → 兼容性裁决（拒绝时**不改**内存）→ 落状态 → 落盘。
        """
        selected = await self.select_model_option(arg)
        self.merge_external_runtime_state()
        self.check_model_switch(ctx, mode, selected.provider, selected.model)
        self.apply_mode_model_selection(mode, selected.provider, selected.model)
        selected.current = True
        if mode == SessionModeChat:
            selected.chat_current = True
        if mode == SessionModeWork:
            selected.work_current = True
        self.save_runtime_state()
        return selected

    async def select_model_option(self, arg: str) -> ModelOption:
        """把 ``<名字或序号>`` 解析成一个模型选择。对应 Go ``selectModelOption``。"""
        name = (arg or "").strip()
        if not name:
            raise InvalidError(f"usage: {self.command_prefix()}model <name or number>")

        models = (await self.model_options("", ModelListOptions())).options
        if not models:
            raise InvalidError("no models available")

        index = _parse_index(name)
        if index is not None:
            if index < 1 or index > len(models):
                raise InvalidError(
                    f"model index {index} out of range [1-{len(models)}]"
                )
            return models[index - 1]

        matches = [
            m
            for m in models
            if m.model.lower() == name.lower()
            or f"{m.provider}/{m.model}".lower() == name.lower()
        ]
        if not matches:
            query = name.lower()
            matches = [
                m
                for m in models
                if query in m.model.lower() or query in m.provider.lower()
            ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InvalidError(
                f'ambiguous model "{name}", matches: {join_model_matches(matches)}'
            )
        raise InvalidError(f'model "{name}" not found')

    def apply_mode_model_selection(
        self, mode: str, provider_name: str, model: str
    ) -> ProviderConfig:
        """切模式槽位并同步主选择。对应 Go ``applyModeModelSelection``。"""
        provider = ensure_model_runtime(self).apply_mode_model_selection(
            mode, provider_name, model
        )
        title_gen = getattr(self, "title_gen", None)
        if title_gen is not None and mode == SessionModeWork:
            title_gen.set_primary(self.client_for_provider(provider_name), model)
        return provider

    def model_for_mode(self, mode: str) -> ModelSelection:
        """对应 Go ``modelForMode``。"""
        return ensure_model_runtime(self).model_for_mode(mode)

    def model_mode_marks(self, modes: Iterable[str]) -> dict[str, list[str]]:
        """``provider\\x00model`` → 使用它的模式名列表。对应 Go ``modelModeMarks``。"""
        marks: dict[str, list[str]] = {}
        seen: set[str] = set()
        for mode in modes:
            selected = self.model_for_mode(mode)
            if not selected.provider or not selected.model:
                continue
            key = model_selection_key(selected.provider, selected.model)
            mark_key = key + "\x00" + mode
            if mark_key in seen:
                continue
            seen.add(mark_key)
            marks.setdefault(key, []).append(mode)
        return marks

    def client_for_provider(self, provider_name: str) -> Any:
        """对应 Go ``clientForProvider``。"""
        return ensure_model_runtime(self).client_for_provider(provider_name)

    # -- 命名模型 -----------------------------------------------------------
    def configured_naming_model(self) -> ModelSelection:
        """对应 Go ``configuredNamingModel``。"""
        selection = getattr(self, "naming_model", None)
        if selection is None:
            return ModelSelection()
        return _copy_selection(selection)

    def set_naming_model(self, selection: ModelSelection) -> None:
        """对应 Go ``setNamingModel``。"""
        self.naming_model = _copy_selection(selection)

    # -- 协议归属与兼容性 ---------------------------------------------------
    def llm_origin_for(self, provider: str, model: str) -> Origin:
        """``provider/model`` 实际使用的协议归属。对应 Go ``llmOriginFor``。"""
        client = self.client_for_provider(provider)
        return Origin(
            protocol=protocol_capabilities_of(client, model).protocol,
            provider=provider,
        )

    def check_model_switch(self, ctx: Any, mode: str, provider: str, model: str) -> None:
        """切换前的兼容性裁决。对应 Go ``checkModelSwitch``。

        只拦「当前会话正在用的模式」；没有当前会话、会话没记过 ``llm_origin``、
        或切换别的模式槽位时放行，剩下的规则交给 :func:`atri_x.llm.origin.can_switch`。
        """
        if not provider or not model:
            return
        sessions = getattr(self, "sessions", None)
        if sessions is None:
            return
        try:
            current = sessions.current(self.scope(ctx))
        except Exception:  # noqa: BLE001 - 没有当前会话时放行
            return
        if current is None or (getattr(current, "mode", "") or "") != mode:
            return
        saved = _llm_origin_state(getattr(current, "metadata", "") or "")
        can_switch(
            Origin(protocol=saved[0], provider=saved[1]),
            self.llm_origin_for(provider, model),
        )

    # -- 重试通知 -----------------------------------------------------------
    def attach_llm_retry_notifier(self, client: Any, provider_name: str) -> None:
        """给适配器挂重试通知。对应 Go ``attachLLMRetryNotifier``。"""
        setter = getattr(client, "set_retry_notifier", None)
        if not callable(setter):
            return

        def notifier(ctx: Any, event: RetryEvent) -> None:
            self._on_llm_retry(ctx, provider_name, event)

        setter(notifier)

    def _on_llm_retry(self, ctx: Any, provider_name: str, event: RetryEvent) -> None:
        err = getattr(event, "err", None)
        if err is None:
            return
        if _context_cancelled(ctx):
            return
        record = getattr(self, "record_provider_retry", None)
        if callable(record):
            record(ctx, provider_name)
        from atri_x.redact import summarize

        safe = summarize(str(err), MAX_USER_ERROR_RUNES)
        # 即使通知投递不出去（服务模式、目标被挡、通知窗口过期），重试也要留痕。
        logger = getattr(self, "logger", None) or _logger
        try:
            logger.warning(
                "model call retry",
                extra={
                    "event": "model_retry",
                    "provider": provider_name,
                    "retry_attempt": event.attempt,
                    "max_retries": event.max_retries,
                    "delay_ms": int(round((event.delay or 0.0) * 1000)),
                    "error": safe,
                },
            )
        except Exception:  # noqa: BLE001 - 日志不能影响主流程
            pass
        if provider_name:
            text = (
                f"LLM 请求失败，正在重试 {event.attempt}/{event.max_retries}"
                f"（provider={provider_name}，{format_delay(event.delay)} 后）：{safe}"
            )
        else:
            text = (
                f"LLM 请求失败，正在重试 {event.attempt}/{event.max_retries}"
                f"（{format_delay(event.delay)} 后）：{safe}"
            )
        _send_warn_notice(self, ctx, text)


def _parse_index(name: str) -> int | None:
    """Go ``strconv.Atoi``：只接受带可选符号的十进制整数。"""
    text = name.strip()
    if not text:
        return None
    body = text[1:] if text[0] in "+-" else text
    if not body or not body.isascii() or not body.isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # pragma: no cover - 上面已保证可解析
        return None


def _llm_origin_state(metadata: str) -> tuple[str, str]:
    """从会话 metadata 里读 ``(protocol, provider)``。对应 Go ``decodeSessionMetadata(...).LLMOrigin``。

    ``session_metadata`` 归 agent-core；这里延迟导入并按鸭子类型读字段，缺失时按
    「没有记录」处理（与 Go 对空 metadata 的零值行为一致）。
    """
    if not metadata:
        return "", ""
    decode = _shared("atri_x.agent.session_metadata", "decode_session_metadata")
    state: Any = None
    if decode is not None:
        try:
            state = getattr(decode(metadata), "llm_origin", None)
        except Exception:  # noqa: BLE001 - 解析失败按未记录处理
            return "", ""
    if state is None:
        return "", ""
    return (
        str(getattr(state, "protocol", "") or ""),
        str(getattr(state, "provider", "") or ""),
    )


def _context_cancelled(ctx: Any) -> bool:
    """``ctx`` 已取消/超时。对应 Go 的 ``errors.Is(ctx.Err(), Canceled/DeadlineExceeded)``。"""
    if ctx is None:
        return False
    token = getattr(ctx, "cancelled", None)
    if callable(token):
        try:
            return bool(token())
        except Exception:  # noqa: BLE001
            return False
    if isinstance(ctx, Mapping):
        value = ctx.get("cancelled")
        return bool(value() if callable(value) else value)
    return False


def _send_warn_notice(host: Any, ctx: Any, text: str) -> None:
    """发一条 warn 级通知；宿主没实现 ``send_notice`` 时静默跳过。"""
    send = getattr(host, "send_notice", None)
    if not callable(send):
        return
    try:
        from atri_x.delivery import Notice, Output, text as text_output

        notice = Notice(outputs=[text_output(text)], level="warning")
    except Exception:  # noqa: BLE001 - delivery 契约缺失时不影响主流程
        return
    try:
        result = send(ctx, notice)
    except Exception:  # noqa: BLE001 - 通知失败不能影响重试
        return
    if inspect.isawaitable(result):
        _schedule(result)


def _schedule(awaitable: Any) -> None:
    """把协程挂到当前事件循环；没有运行中的循环时丢弃（避免 "never awaited" 警告）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            awaitable.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return
    loop.create_task(awaitable)


def ensure_context_runtime(host: Any) -> Any:
    """取宿主的上下文运行态（延迟导入避免与 context_runtime 形成导入环）。"""
    from atri_x.agent.context_runtime import ensure_context_runtime as ensure

    return ensure(host)
