"""``runtime.go`` 对应的入口名与配置换算。

对应 Go：``internal/tool/builtin/runtime.go``。

**为什么这里没有 ``Runtime`` / ``RuntimeOptions`` / ``NewRuntime`` 的装配实现**：
Go 的 ``NewRuntime`` 要同时拉起 angelmemory / selflearning / character /
imagegen / groupanalysis / artist 六个服务，并需要 config / storage 句柄——
只有 app 层同时拿得到这些。本仓已把这个装配**有意**落在
:mod:`atri_x.app.tool_runtime`（``ToolRuntime`` / ``ToolRuntimeOptions`` /
``build_tool_runtime``，该模块 docstring 里写明了这个决定）。本模块因此不重复
装配，只提供 Go 名字到既有实现的转发：

============================  ==========================================
Go 名字                       本仓实现
============================  ==========================================
``Runtime``                   ``atri_x.app.tool_runtime.ToolRuntime``
``RuntimeOptions``            ``atri_x.app.tool_runtime.ToolRuntimeOptions``
``NewRuntime``                ``atri_x.app.tool_runtime.build_tool_runtime``
``ImagePromptRewriter``       ``atri_x.tool.builtin.image_generate_context``
``ImagePromptRewriteRequest`` 同上
``ImagePromptService``        ``atri_x.tool.builtin.image_to_prompt``
``imageConfigFrom``           :func:`image_config_from`
============================  ==========================================

转发一律**惰性**解析（模块级 ``__getattr__``）：既避免 ``atri_x.app`` ↔
``atri_x.tool.builtin`` 的循环导入，也让 ``Internal/llm`` 侧的改写器实现只在使用到
那几个名字时才被导入。

与 Go 的差异：Go 的 ``ImagePromptRewriter`` / ``ImagePromptService`` 是接口声明；
本仓用 ``Protocol`` + 鸭子类型（见
:mod:`atri_x.tool.builtin.image_generate_context` 与
:mod:`atri_x.tool.builtin.image_to_prompt`），名字在这里转发是为了按 Go 名交叉检索。
"""

from __future__ import annotations

from typing import Any

from atri_x.imagegen import Config as ImageGenConfig
from atri_x.imagegen import NormalizeNegativePromptMode as normalize_negative_prompt_mode

__all__ = [
    "ImageGenConfig",
    "ImagePromptRewriteRequest",
    "ImagePromptRewriter",
    "ImagePromptService",
    "Runtime",
    "RuntimeOptions",
    "image_config_from",
    "new_runtime",
]

_APP_FORWARD = {
    "Runtime": "ToolRuntime",
    "RuntimeOptions": "ToolRuntimeOptions",
    "new_runtime": "build_tool_runtime",
}

#: Go 里定义在别的 .go 文件里的名字 → 负责它的本仓模块。
_MODULE_FORWARD = {
    "ImagePromptRewriter": "atri_x.tool.builtin.image_generate_context",
    "ImagePromptRewriteRequest": "atri_x.tool.builtin.image_generate_context",
    "ImagePromptService": "atri_x.tool.builtin.image_to_prompt",
}


def _flag(value: Any) -> bool:
    """取开关值：既接受布尔，也接受 ``is_xxx()`` 形式的取反标记（Go 的 ``IsXxx``）。"""
    return bool(value)


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def image_config_from(section: Any) -> ImageGenConfig:
    """``config.ImageGenerationConfig`` → :class:`atri_x.imagegen.Config`。

    对应 Go ``imageConfigFrom``：``Enabled`` 强制为 true（调用方已经判断过启用），
    ``AutoCharacter`` / ``AutoContext`` / ``SuperadminOnly`` / ``SaveToCharacter``
    走配置的 ``IsXxx()`` 取值，``NegativePromptMode`` 走归一化。
    """
    get = lambda name, default=None: getattr(section, name, default)  # noqa: E731

    def flag(name: str, method: str) -> bool:
        getter = getattr(section, method, None)
        if callable(getter):
            return bool(getter())
        return bool(get(name, False))

    return ImageGenConfig(
        enabled=True,
        base_url=_text(get("base_url", "")),
        endpoint=_text(get("endpoint", "")),
        api_key=_text(get("api_key", "")),
        api_key_env=_text(get("api_key_env", "")),
        model=_text(get("model", "")),
        size=_text(get("size", "")),
        quality=_text(get("quality", "")),
        output_format=_text(get("output_format", "")),
        response_format=_text(get("response_format", "")),
        timeout_seconds=int(get("timeout_seconds", 0) or 0),
        preset_prompt=_text(get("preset_prompt", "")),
        negative_prompt=_text(get("negative_prompt", "")),
        max_prompt_runes=int(get("max_prompt_runes", 0) or 0),
        optimize=_text(get("optimize", "")),
        optimize_term_mode=_text(get("optimize_term_mode", "")),
        optimize_max_anchors=int(get("optimize_max_anchors", 0) or 0),
        optimize_max_negatives=int(get("optimize_max_negatives", 0) or 0),
        optimize_max_added_runes=int(get("optimize_max_added_runes", 0) or 0),
        optimize_max_tags=int(get("optimize_max_tags", 0) or 0),
        optimize_rewrite=_text(get("optimize_rewrite", "")),
        optimize_rewrite_model=_text(get("optimize_rewrite_model", "")),
        optimize_rewrite_min_runes=int(get("optimize_rewrite_min_runes", 0) or 0),
        auto_character=flag("auto_character", "is_auto_character"),
        auto_context=flag("auto_context", "is_auto_context"),
        context_default_limit=int(get("context_default_limit", 0) or 0),
        superadmin_only=flag("superadmin_only", "is_superadmin_only"),
        save_to_character=flag("save_to_character", "is_save_to_character"),
        send_by_default=bool(get("send_by_default", False)),
        supports_reference=bool(get("supports_reference", False)),
        reference_field=_text(get("reference_field", "")),
        model_family=_text(get("model_family", "")),
        prompt_dialect=_text(get("prompt_dialect", "")),
        negative_prompt_field=_text(get("negative_prompt_field", "")),
        negative_prompt_mode=normalize_negative_prompt_mode(
            get("negative_prompt_mode", "")
        ),
        artist_default_preset=_text(get("artist_default_preset", "")),
        extra_payload=dict(get("extra_payload", None) or {}),
        extra_headers=dict(get("extra_headers", None) or {}),
        proxy=_text(get("proxy", "")),
    )


def __getattr__(name: str) -> Any:
    """惰性转发（见模块 docstring）。"""
    if name in _APP_FORWARD:
        from atri_x.app import tool_runtime

        return getattr(tool_runtime, _APP_FORWARD[name])
    if name in _MODULE_FORWARD:
        module_name = _MODULE_FORWARD[name]
        try:
            module = __import__(module_name, fromlist=[name])
        except ImportError as exc:  # pragma: no cover - 依赖并行落地进度
            raise AttributeError(
                f"{name} 需要 {module_name}（尚未落地）：{exc}"
            ) from exc
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
