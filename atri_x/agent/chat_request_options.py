"""LLM 请求的临时参数（``LLMRequestOptions``）与其合并/解析。

对应 Go：``internal/agent/llm_request_options.go``——Go 上游本来就是单独一个文件；
Python 版最初为了自洽把它并进了 :mod:`atri_x.agent.chat_llm`，现在拆回来（行数上限，
也便于 hook / 测试单独引用）。``chat_llm`` 在文件末尾重新导出这些名字，导入路径不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LLMRequestOptions",
    "llm_request_options_from_payload",
    "merge_llm_request_options",
]


@dataclass
class LLMRequestOptions:
    """Hook 返回的临时请求参数（不落库）。对应 Go ``llmRequestOptions``。"""

    system_append: list[str] = field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


def llm_request_options_from_payload(payload: Any) -> LLMRequestOptions:
    """从 hook 负载取临时请求参数。对应 Go ``llmRequestOptionsFromPayload``。"""
    if payload is None:
        return LLMRequestOptions()
    options = LLMRequestOptions(
        temperature=getattr(payload, "temperature", None),
        max_tokens=getattr(payload, "max_tokens", None),
        extra_body=_clone_any_map(getattr(payload, "extra_body", None)),
    )
    for part in getattr(payload, "system_append", None) or []:
        text = str(part or "").strip()
        if text:
            options.system_append.append(text)
    return options


def merge_llm_request_options(
    base: LLMRequestOptions | None, overlay: LLMRequestOptions | None
) -> LLMRequestOptions:
    """叠加上层参数。对应 Go ``mergeLLMRequestOptions``。

    Go 的 ``merged := base`` 是值拷贝，因此这里也必须复制：返回值与 ``base``
    不共享可变容器，调用方不会因为后续合并而看到自己的参数被改。
    """
    source = base or LLMRequestOptions()
    merged = LLMRequestOptions(
        system_append=list(source.system_append),
        temperature=source.temperature,
        max_tokens=source.max_tokens,
        extra_body=dict(source.extra_body),
    )
    if overlay is None:
        return merged
    if overlay.system_append:
        merged.system_append = list(merged.system_append) + list(overlay.system_append)
    if overlay.temperature is not None:
        merged.temperature = overlay.temperature
    if overlay.max_tokens is not None:
        merged.max_tokens = overlay.max_tokens
    if overlay.extra_body:
        combined = dict(merged.extra_body)
        combined.update(overlay.extra_body)
        merged.extra_body = combined
    return merged


def _clone_any_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    return {str(key): value for key, value in raw.items()}
