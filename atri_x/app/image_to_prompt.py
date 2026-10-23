"""``image_to_prompt`` 工具共用的视觉引擎装配。

对应 Go：``internal/app/image_to_prompt.go``。

工具本身不再自带缓存/单飞：它和聊天视觉兜底共用同一个 ``vision.Service``，
于是缓存、合并、输出上限、负缓存与超时只存在一处。

与 Go 的差异：``vision.Service`` 在 Python 侧由 :func:`atri_x.vision.new` 构造，
字段名与 Go 一一对应（``cache_ttl`` / ``negative_ttl`` / ``base_context`` 等）。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.dependencies import ModelClients
from atri_x.app.vision import vision_capability_error
from atri_x.errors import InvalidError
from atri_x.vision import Options as VisionOptions
from atri_x.vision import new as new_vision_service

__all__ = ["buildImagePromptService", "build_image_prompt_service"]


def build_image_prompt_service(
    base_ctx: Any,
    cfg: Any,
    models: ModelClients,
    metrics: Any = None,
    logger: Any = None,
) -> Any:
    """对应 Go ``buildImagePromptService``；未启用返回 ``None``。

    显式启用但配置不完整/未知时抛错，让 services.toml 里的拼写错误在启动期暴露，
    而不是看起来像"模型从来不调用这个工具"。

    ``base_ctx`` 是进程生命周期上下文：关停会取消在途描述任务。``logger`` 只用于
    失败原因的一行摘要（不含图片内容与凭据）。
    """
    section = getattr(cfg, "image_to_prompt", None)
    if cfg is None or section is None or not _enabled(section):
        return None
    provider = str(getattr(section, "provider", "") or "").strip()
    model = str(getattr(section, "model", "") or "").strip()
    if not provider or not model:
        raise InvalidError(
            f"[image_to_prompt] is enabled but incomplete: provider={provider!r} "
            f"model={model!r}; set both, or remove the section / set enabled=false"
        )
    client = (models.by_provider or {}).get(provider)
    if client is None:
        raise InvalidError(
            f"[image_to_prompt] references provider {provider!r}, but no "
            f"[providers.{provider}] client is configured"
        )
    vision_capability_error("image_to_prompt", provider, model, cfg)
    providers = getattr(cfg, "providers", None) or {}
    provider_cfg = providers.get(provider)
    return new_vision_service(
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
            # 1.0.3：图片精度（默认 original）与隐私总开关。
            detail=_detail_value(section),
            allow_external_images=bool(
                getattr(section, "allow_external_images", True)
            ),
            logger=logger,
            base_context=base_ctx,
            metrics=metrics,
        )
    )


def _enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


def _temperature_value(section: Any) -> float:
    method = getattr(section, "temperature_value", None)
    if callable(method):
        return float(method())
    return float(getattr(section, "temperature", 0.0) or 0.0)


def _detail_value(section: Any) -> str:
    """图片精度：默认 ``original``（低精度先把认人细节糊掉）。"""
    value = str(getattr(section, "detail", "") or "").strip().lower()
    return value if value in ("original", "high", "auto", "low") else "original"


buildImagePromptService = build_image_prompt_service
