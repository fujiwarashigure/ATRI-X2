"""视觉服务装配：把共享 ``vision.Service`` 接到 Agent 的聊天视觉兜底接口。

对应 Go：``internal/app/vision.go``。

与 Go 的差异：

* Go 的 ``vision.Metrics`` 是接口，Python 侧是 ``vision.Counters``（鸭子类型），
  因此 ``metrics`` 参数直接透传。
* Go 的 ``VisionDescriber.DescribeImage(ctx, mediaID, data, mimeType)``；
  Python 侧对应 ``describe_image(ctx, media_id, data, mime_type)``。
* ``vision.Service.describe`` 是协程，需 ``await``。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.dependencies import ModelClients
from atri_x.config import VisionSupport
from atri_x.errors import InvalidError
from atri_x.vision import Options as VisionOptions
from atri_x.vision import Request as VisionRequest
from atri_x.vision import chat_description, new as new_vision_service

__all__ = [
    "VisionChatDescriber",
    "visionChatDescriber",
    "visionCapabilityError",
    "vision_capability_error",
    "buildVisionDescriber",
    "build_vision_describer",
]


class VisionChatDescriber:
    """把共享视觉服务适配成 Agent 的聊天兜底接口。对应 Go ``visionChatDescriber``。

    提示词与语言都收在这里，Agent 不需要知道提示词与 provider 的存在。
    """

    def __init__(self, service: Any, language: str = "") -> None:
        self.service = service
        self.language = language

    async def describe_image(
        self, ctx: Any, media_id: str, data: bytes, mime_type: str
    ) -> str:
        """对应 Go ``DescribeImage``。"""
        result = await self.service.describe(
            ctx,
            VisionRequest(
                media_id=media_id,
                data=data or b"",
                mime_type=mime_type or "",
                prompt=chat_description(self.language),
            ),
        )
        return getattr(result, "text", "") or ""

    # Go 命名别名。
    DescribeImage = describe_image


def vision_capability_error(section: str, provider: str, model: str, cfg: Any) -> None:
    """把显式的 ``vision = false`` 声明变成启动错误。对应 Go ``visionCapabilityError``。

    未声明（``UNKNOWN``）保持原行为不变。
    """
    support_for = getattr(cfg, "vision_support_for", None)
    if not callable(support_for):
        return
    if support_for(provider, model) != VisionSupport.UNSUPPORTED:
        return
    raise InvalidError(
        f"[{section}] provider {provider!r} model {model!r} is declared vision = false "
        f"(in [providers.{provider}].vision or "
        f"[providers.{provider}.model_configs.{model!r}]); "
        "enable image input for that model or configure a vision-capable one"
    )


def build_vision_describer(
    base_ctx: Any, cfg: Any, models: ModelClients, metrics: Any = None
) -> Any:
    """对应 Go ``buildVisionDescriber``；未启用返回 ``None``。

    ``base_ctx`` 是进程生命周期上下文：关停时取消在途的共享视觉任务，而不是让它
    跑满共享超时。
    """
    if cfg is None or not _is_enabled(getattr(cfg, "vision", None)):
        return None
    section = getattr(cfg, "vision", None)
    provider = str(getattr(section, "provider", "") or "").strip()
    model = str(getattr(section, "model", "") or "").strip()
    if not provider or not model:
        raise InvalidError(
            f"[vision] is enabled but incomplete: provider={provider!r} model={model!r}; "
            "set both, or set enabled=false"
        )
    client = (models.by_provider or {}).get(provider)
    if client is None:
        raise InvalidError(
            f"[vision] references provider {provider!r}, but no "
            f"[providers.{provider}] client is configured"
        )
    vision_capability_error("vision", provider, model, cfg)
    providers = getattr(cfg, "providers", None) or {}
    provider_cfg = providers.get(provider)
    service = new_vision_service(
        VisionOptions(
            client=client,
            provider=provider,
            endpoint=str(getattr(provider_cfg, "base_url", "") or "").strip(),
            model=model,
            max_tokens=int(getattr(section, "max_tokens", 0) or 0),
            temperature=float(_temperature_value(section)),
            max_edge=int(getattr(section, "max_edge", 0) or 0),
            max_image_bytes=int(getattr(section, "max_image_bytes", 0) or 0),
            timeout=float(getattr(section, "timeout_seconds", 0) or 0),
            cache_ttl=float(getattr(section, "cache_ttl_seconds", 0) or 0),
            negative_ttl=float(
                getattr(section, "negative_cache_ttl_seconds", 0) or 0
            ),
            base_context=base_ctx,
            metrics=metrics,
        )
    )
    return VisionChatDescriber(service, _language_value(section))


def _is_enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


def _temperature_value(section: Any) -> float:
    method = getattr(section, "temperature_value", None)
    if callable(method):
        return float(method())
    return float(getattr(section, "temperature", 0.0) or 0.0)


def _language_value(section: Any) -> str:
    method = getattr(section, "language_value", None)
    if callable(method):
        return str(method())
    return str(getattr(section, "language", "") or "")


visionChatDescriber = VisionChatDescriber
visionCapabilityError = vision_capability_error
buildVisionDescriber = build_vision_describer
