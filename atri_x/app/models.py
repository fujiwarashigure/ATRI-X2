"""LLM 客户端装配：provider → 适配器（含协议路由、健康包装、熔断包装）。

对应 Go：``internal/app/models.go``。

与 Go 的差异：

* Go 的 ``openai.NewWithOptions`` / ``NewResponsesWithOptions`` 是构造函数；
  Python 侧对应类构造器 ``OpenAIClient(...)`` / ``OpenAIResponsesClient(...)``。
* Go 的 ``ProtocolCapabilitiesFor(model)`` 在 Python 侧是
  :func:`atri_x.llm.protocol.protocol_capabilities_of`，因此
  ``ProtocolRouter`` 提供同名方法并转发。
* ``setClientLogger`` 用 ``set_logger``（Python 侧没有 ``slog``）。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.breaker_llm import (
    wrap_breaker_llm,
    with_fallback_on_error,
    with_fallback_total_timeout,
)
from atri_x.app.dependencies import ModelClients, ModelRequest
from atri_x.app.health_llm import wrap_health_llm
from atri_x.config import APIProtocol, normalize_reasoning_effort
from atri_x.errors import InvalidError
from atri_x.llm import ChatRequest
from atri_x.llm.breaker import Breaker, Config as BreakerConfig
from atri_x.llm.openai import OpenAIResponsesClient, OpenAIClient, RequestOptions
from atri_x.llm.protocol import protocol_capabilities_of

__all__ = [
    "DefaultModelFactory",
    "ProtocolRouter",
    "protocolRouter",
    "appLLMRequestOptions",
    "app_llm_request_options",
    "modelExtraPayloads",
    "model_extra_payloads",
    "newProviderLLM",
    "new_provider_llm",
    "providerExtraPayload",
    "provider_extra_payload",
    "setClientLogger",
    "set_client_logger",
]


class DefaultModelFactory:
    """对应 Go ``defaultModelFactory``。

    ``build`` 是协程（Go 侧同步），以便与其它工厂统一由
    :func:`atri_x.app.runner.maybe_await` 调用。
    """

    async def build(self, req: ModelRequest) -> ModelClients:
        foundation = req.foundation
        if foundation is None:
            raise InvalidError("app: model factory requires foundation components")
        cfg = foundation.config
        logger = foundation.logger
        providers = dict(getattr(cfg, "providers", None) or {})
        llm_request = getattr(cfg, "llm_request", None)

        raw_clients: dict[str, Any] = {}
        for name, provider in providers.items():
            client = new_provider_llm(
                name,
                provider,
                app_llm_request_options(llm_request, getattr(provider, "proxy", "")),
            )
            if client is None:
                raise InvalidError(f'create provider "{name}" client: client is nil')
            set_client_logger(client, logger)
            raw_clients[name] = client

        health_clients = {
            name: wrap_health_llm(name, client, req.health)
            for name, client in raw_clients.items()
        }

        ops = getattr(cfg, "ops", None)
        clients: dict[str, Any] = {}
        for name, provider in providers.items():
            health_client = health_clients.get(name)
            if health_client is None:
                continue
            fallback = None
            fallback_name = getattr(provider, "fallback_provider", "") or ""
            if fallback_name and fallback_name != name:
                fallback = health_clients.get(fallback_name)
            breaker = Breaker(
                BreakerConfig(
                    failure_threshold=int(
                        getattr(ops, "circuit_breaker_failure_threshold", 0) or 0
                    ),
                    open_cooldown=float(
                        getattr(ops, "circuit_breaker_open_cooldown_seconds", 0) or 0
                    ),
                    half_open_max=int(
                        getattr(ops, "circuit_breaker_half_open_max", 0) or 0
                    ),
                )
            )
            clients[name] = wrap_breaker_llm(
                name,
                health_client,
                fallback,
                fallback_name,
                getattr(provider, "fallback_model", "") or "",
                breaker,
                req.health,
                with_fallback_on_error(_uses_fallback_on_error(provider)),
                with_fallback_total_timeout(
                    float(getattr(provider, "fallback_timeout_seconds", 0) or 0)
                ),
            )
        if req.profiler is not None:
            req.profiler.mark("llm adapters")
        return ModelClients(by_provider=clients)

    # Go 命名别名。
    Build = build


def _uses_fallback_on_error(provider: Any) -> bool:
    method = getattr(provider, "uses_fallback_on_error", None)
    if callable(method):
        return bool(method())
    return bool(getattr(provider, "fallback_on_error", False))


def app_llm_request_options(cfg: Any, proxy: str) -> RequestOptions:
    """对应 Go ``appLLMRequestOptions``。"""
    if cfg is None:
        return RequestOptions(proxy=proxy or "")
    return RequestOptions(
        first_chunk_timeout=float(getattr(cfg, "first_chunk_timeout_seconds", 0) or 0),
        stream_idle_timeout=float(getattr(cfg, "stream_idle_timeout_seconds", 0) or 0),
        max_retries=int(getattr(cfg, "max_retries", 0) or 0),
        retry_initial_delay=float(
            getattr(cfg, "retry_initial_delay_seconds", 0) or 0
        ),
        proxy=proxy or "",
    )


def model_extra_payloads(model_configs: Any) -> dict[str, dict[str, Any]]:
    """对应 Go ``modelExtraPayloads``：只保留显式配了额外字段的模型。

    Python 侧比 Go 多合并一项专用配置：``reasoning_effort`` 是桌面端「模型」页
    可编辑的一等字段，这里折进同一个 extra payload，由适配器按
    「Provider → 模型」顺序覆盖。同名键时专用字段胜出。
    """
    out: dict[str, dict[str, Any]] = {}
    for model, cfg in (model_configs or {}).items():
        extra = dict(getattr(cfg, "extra_payload", None) or {})
        effort, ok = normalize_reasoning_effort(
            getattr(cfg, "reasoning_effort", "") or ""
        )
        if ok and effort:
            extra["reasoning_effort"] = effort
        if extra:
            out[model] = extra
    return out


def provider_extra_payload(provider: Any) -> dict[str, Any] | None:
    """Provider 级 extra payload：``extra_payload`` + 专用 ``reasoning_effort``。

    两者同名时**专用字段胜出**——它就是为了让用户不必手写 ``extra_payload`` 而加的
    一等配置。返回 ``None`` 表示没有额外字段（与旧行为一致，不会多发空对象）。
    """
    payload = dict(getattr(provider, "extra_payload", None) or {})
    effort, ok = normalize_reasoning_effort(getattr(provider, "reasoning_effort", "") or "")
    if ok and effort:
        payload["reasoning_effort"] = effort
    return payload or None


def new_provider_llm(name: str, provider: Any, opts: RequestOptions) -> Any:
    """对应 Go ``newProviderLLM``。

    混协议的 provider 会拿到 :class:`ProtocolRouter`，让每个请求都落到与它模型
    匹配的端点上。
    """
    model_extras = model_extra_payloads(getattr(provider, "model_configs", None))
    base_url = getattr(provider, "base_url", "") or ""
    api_key = getattr(provider, "api_key", "") or ""
    extra_payload = provider_extra_payload(provider)
    extra_headers = getattr(provider, "extra_headers", None) or None
    chat = OpenAIClient(
        base_url, api_key, extra_payload, model_extras, opts, extra_headers=extra_headers
    )
    if not _uses_responses_api(provider):
        return chat
    responses = OpenAIResponsesClient(
        base_url, api_key, extra_payload, model_extras, opts, extra_headers=extra_headers
    )
    protocol_for = getattr(provider, "api_protocol_for", None)
    return ProtocolRouter(
        provider=name, chat=chat, responses=responses, protocol=protocol_for
    )


def _uses_responses_api(provider: Any) -> bool:
    method = getattr(provider, "uses_responses_api", None)
    if callable(method):
        return bool(method())
    return False


def set_client_logger(client: Any, logger: Any) -> None:
    """把 logger 挂到接受它的适配器上；不接受的静默跳过。对应 Go ``setClientLogger``。"""
    setter = getattr(client, "set_logger", None)
    if callable(setter):
        setter(logger)


class ProtocolRouter:
    """按模型把请求分派到 chat 或 responses 适配器。对应 Go ``protocolRouter``。"""

    def __init__(
        self,
        provider: str,
        chat: Any,
        responses: Any = None,
        protocol: Any = None,
    ) -> None:
        self.provider = provider
        self.chat = chat
        self.responses = responses
        self.protocol = protocol

    def pick(self, model: str) -> Any:
        """对应 Go ``pick``。"""
        if self.responses is not None and callable(self.protocol):
            try:
                if self.protocol(model) == APIProtocol.RESPONSES:
                    return self.responses
            except Exception:  # noqa: BLE001 - 协议解析失败按 chat 兜底
                pass
        return self.chat

    def chat_stream(self, ctx: Any, req: ChatRequest) -> Any:
        """对应 Go ``ChatStream``。"""
        return self.pick(getattr(req, "model", "")).chat_stream(ctx, req)

    def protocol_capabilities_for(self, model: str) -> Any:
        """按模型回答协议能力，与 :meth:`chat_stream` 同一套选择逻辑。"""
        return protocol_capabilities_of(self.pick(model), model)

    async def list_models(self, ctx: Any) -> list[str]:
        """``/models`` 与协议无关：chat 适配器回答，responses 只做兜底。"""
        try:
            result = self.chat.list_models(ctx)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001 - 需要试兜底
            if self.responses is not None:
                try:
                    fallback = self.responses.list_models(ctx)
                    if hasattr(fallback, "__await__"):
                        fallback = await fallback
                    return fallback
                except Exception:  # noqa: BLE001
                    pass
            raise exc

    async def list_model_metadata(self, ctx: Any) -> list[Any]:
        """对应 Go ``ListModelMetadata``：chat 优先，responses 兜底。"""
        try:
            return await _client_model_metadata(ctx, self.chat)
        except Exception:  # noqa: BLE001
            if self.responses is not None:
                try:
                    return await _client_model_metadata(ctx, self.responses)
                except Exception:  # noqa: BLE001
                    pass
            raise InvalidError(
                f'provider "{self.provider}" does not support model metadata'
            )

    def set_logger(self, logger: Any) -> None:
        """两个适配器都挂上，保证实际用到的那个仍有调试日志。"""
        set_client_logger(self.chat, logger)
        set_client_logger(self.responses, logger)

    def set_retry_notifier(self, fn: Any) -> None:
        """两个适配器都转发，混协议时重试通知不会丢。"""
        for client in (self.chat, self.responses):
            notifier = getattr(client, "set_retry_notifier", None)
            if callable(notifier):
                notifier(fn)

    # Go 命名别名。
    ChatStream = chat_stream
    ListModels = list_models
    ListModelMetadata = list_model_metadata
    ProtocolCapabilitiesFor = protocol_capabilities_for
    SetLogger = set_logger
    SetRetryNotifier = set_retry_notifier


async def _client_model_metadata(ctx: Any, client: Any) -> list[Any]:
    """取一个客户端的模型元数据；不支持时抛错。"""
    provider = getattr(client, "list_model_metadata", None)
    if not callable(provider):
        raise InvalidError("client does not support model metadata")
    result = provider(ctx)
    if hasattr(result, "__await__"):
        result = await result
    return result


protocolRouter = ProtocolRouter
appLLMRequestOptions = app_llm_request_options
modelExtraPayloads = model_extra_payloads
newProviderLLM = new_provider_llm
providerExtraPayload = provider_extra_payload
setClientLogger = set_client_logger
