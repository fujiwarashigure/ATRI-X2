"""漫画服务的 app 侧装配（从 :mod:`atri_x.app.tool_runtime` 拆出）。

拆出来的原因：``tool_runtime.py`` 已经在 600 行上限附近，而这段装配有独立职责
——把 ``[comic]`` 段、生图 profile、历史库、角色库、媒体中心、规划模型、头像
解析器接成一个 :class:`atri_x.comic.ComicService`。

**装配顺序的硬约束**：漫画服务必须在 ``_build_image_profiles`` 之后建，因为它
复用那批 ``ImageProfile``（client + config）与同一个限流器；在 ``character_store``
之后建，因为它要读角色卡参考图。没有生图 profile 或功能未启用时返回 ``None``
（对应两个漫画工具整组不注册，而不是注册了永远报错）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_comic_service"]


def build_comic_service(
    opts: Any,
    image_profiles: dict[str, Any],
    default_image_profile: str,
    image_limiter: Any,
    character_store: Any,
    warnings: list[str],
) -> Any:
    """按配置建漫画服务；不可用时返回 ``None`` 并（可选）记 warning。"""
    try:
        from atri_x.comic import ComicService, ComicStore, comic_options_from, comic_root
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"comic service unavailable: {exc}")
        return None
    options = comic_options_from(getattr(opts, "comic", None))
    if not options.enabled:
        return None
    if not image_profiles:
        # 没有生图 profile 时漫画只能失败；不注册比"注册了永远报错"更清楚。
        return None
    try:
        store = ComicStore(comic_root(opts.config_dir), logger=getattr(opts, "logger", None))
        return ComicService(
            options,
            store=store,
            history=getattr(opts, "chat_history", None),
            characters=character_store,
            media=getattr(opts, "media", None),
            profiles=image_profiles,
            default_profile=default_image_profile,
            limiter=image_limiter,
            planner=getattr(opts, "comic_planner", None),
            avatar_resolver=getattr(opts, "comic_avatar_resolver", None),
            logger=getattr(opts, "logger", None),
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"create comic service: {exc}")
        return None
