"""ASR（语音转写）服务装配。

对应 Go：``internal/app/asr.go``。

与 Go 的差异：

* Go 的 ``asr.Service.Transcribe(ctx, req)`` 在 Python 侧是
  ``await service.transcribe(ctx, req)``；结果字段名一致（``text`` / ``language`` /
  ``provider`` / ``model``）。
* Agent 侧的接口在 Python 里是 ``await transcribe(ctx, req)``，请求对象字段与 Go
  ``agent.AudioTranscriptionRequest`` 一致（``media_id`` / ``data`` / ``name`` /
  ``mime_type`` / ``language``）；因为不能改 ``atri_x/agent``，这里用
  :func:`getattr` 鸭子取值。
* ASR 显式声明 ``audio = false`` 时启动即失败（``Unknown`` 保持放行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.dependencies import ModelClients
from atri_x.asr import New as new_asr_service
from atri_x.asr import Options as AsrOptions
from atri_x.asr import Request as AsrRequest
from atri_x.config import AudioSupport
from atri_x.errors import InvalidError

__all__ = [
    "AsrTranscriberAdapter",
    "asrTranscriberAdapter",
    "audioCapabilityError",
    "audio_capability_error",
    "buildAudioTranscriber",
    "build_audio_transcriber",
]


class AsrTranscriberAdapter:
    """把共享 asr 服务适配成 Agent 需要的请求/结果形状。

    对应 Go ``asrTranscriberAdapter``：provider 与上传策略只在这里出现一次。
    """

    def __init__(self, service: Any = None) -> None:
        self.service = service

    async def transcribe(self, ctx: Any, req: Any) -> Any:
        """对应 Go ``Transcribe``。

        返回 :class:`atri_x.app.service_types.AudioTranscriptionResult`；调用方
        （Agent）按鸭子类型读 ``text`` / ``language`` / ``provider`` / ``model``。
        """
        from atri_x.app.service_types import AudioTranscriptionResult

        if self.service is None:
            raise InvalidError("asr service is not configured")
        result = await self.service.transcribe(
            ctx,
            AsrRequest(
                media_id=_field(req, "media_id"),
                data=_field(req, "data") or b"",
                name=_field(req, "name"),
                mime_type=_field(req, "mime_type"),
                language=_field(req, "language"),
            ),
        )
        return AudioTranscriptionResult(
            text=getattr(result, "text", "") or "",
            language=getattr(result, "language", "") or "",
            provider=getattr(result, "provider", "") or "",
            model=getattr(result, "model", "") or "",
        )

    # Go 命名别名。
    Transcribe = transcribe


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return ""
    return getattr(obj, name, "")


def audio_capability_error(provider: str, model: str, cfg: Any) -> None:
    """把显式的 ``audio = false`` 声明变成启动错误。对应 Go ``audioCapabilityError``。"""
    support_for = getattr(cfg, "audio_support_for", None)
    if not callable(support_for):
        return
    if support_for(provider, model) != AudioSupport.UNSUPPORTED:
        return
    raise InvalidError(
        f"[asr] provider {provider!r} model {model!r} is declared audio = false "
        f"(in [providers.{provider}].audio or "
        f"[providers.{provider}.model_configs.{model!r}]); "
        "enable transcription for that model or configure a capable one"
    )


def build_audio_transcriber(
    base_ctx: Any, cfg: Any, models: ModelClients | None = None
) -> Any:
    """对应 Go ``buildAudioTranscriber``；ASR 未启用返回 ``None``。

    显式启用但配置不完整/指向未知 provider 时抛错，让拼写错误在启动期大声失败，
    而不是静默关掉转写。
    """
    if cfg is None or not _is_enabled(getattr(cfg, "asr", None)):
        return None
    asr_cfg = _normalized(getattr(cfg, "asr", None))
    provider = str(getattr(asr_cfg, "provider", "") or "").strip()
    model = str(getattr(asr_cfg, "model", "") or "").strip()
    if not provider or not model:
        raise InvalidError(
            f"[asr] is enabled but incomplete: provider={provider!r} model={model!r}; "
            "set both, or set enabled=false"
        )
    providers = getattr(cfg, "providers", None) or {}
    provider_cfg = providers.get(provider)
    if provider_cfg is None:
        raise InvalidError(
            f"[asr] references provider {provider!r}, but no "
            f"[providers.{provider}] client is configured"
        )
    if not str(getattr(provider_cfg, "base_url", "") or "").strip():
        raise InvalidError(
            f"[asr] references provider {provider!r}, but "
            f"[providers.{provider}].base_url is empty"
        )
    audio_capability_error(provider, model, cfg)
    service = new_asr_service(
        AsrOptions(
            base_url=str(getattr(provider_cfg, "base_url", "") or ""),
            api_key=str(getattr(provider_cfg, "api_key", "") or ""),
            model=model,
            provider=provider,
            language=str(getattr(asr_cfg, "language", "") or ""),
            prompt=str(getattr(asr_cfg, "prompt", "") or ""),
            timeout=float(getattr(asr_cfg, "timeout_seconds", 0) or 0),
            max_audio_bytes=int(getattr(asr_cfg, "max_audio_bytes", 0) or 0),
            max_concurrent_jobs=int(getattr(asr_cfg, "max_concurrent", 0) or 0),
            max_job_queue=int(getattr(asr_cfg, "queue_size", 0) or 0),
            cache_ttl=float(getattr(asr_cfg, "cache_ttl_seconds", 0) or 0),
            cache_max_entries=int(getattr(asr_cfg, "cache_max_entries", 0) or 0),
            negative_ttl=float(
                getattr(asr_cfg, "negative_cache_ttl_seconds", 0) or 0
            ),
            negative_max=int(getattr(asr_cfg, "negative_cache_max_entries", 0) or 0),
            max_retries=int(getattr(asr_cfg, "max_retries", 0) or 0),
            retry_initial_delay=float(
                getattr(asr_cfg, "retry_initial_delay_seconds", 0) or 0
            ),
            proxy=str(getattr(provider_cfg, "proxy", "") or "").strip(),
            base_context=base_ctx,
        )
    )
    return AsrTranscriberAdapter(service)


def _is_enabled(section: Any) -> bool:
    method = getattr(section, "is_enabled", None)
    if callable(method):
        return bool(method())
    return bool(getattr(section, "enabled", False))


def _normalized(section: Any) -> Any:
    method = getattr(section, "normalized", None)
    if callable(method):
        return method()
    return section


asrTranscriberAdapter = AsrTranscriberAdapter
audioCapabilityError = audio_capability_error
buildAudioTranscriber = build_audio_transcriber
