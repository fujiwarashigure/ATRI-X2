"""参考图加载：把计划里的参考图描述变成真正的字节。

三种来源，全部走**现有基础设施**，不另写下载器：

* ``media`` —— 已入库媒体，走 ``Media Center.read``；
* ``character_image`` —— 角色卡图片，走 ``character.Store.read_image``（带可见性校验）；
* ``avatar`` —— 平台头像 URL，先 ``Media Center.import_url``（沿用其超时/大小限制），
  再 ``read`` 出字节。

错误策略按"用户是否点名了这张图"区分：

* 显式参考图（media / avatar）失败 → **报错**，绝不悄悄换成别的来源；
* 角色卡图片失败 → 告警并跳过（角色还有文字设定，提示词里会写明"没有参考图"）；
* ``download=False``（dry_run）→ 只校验数量与能力，不读图、不下载、不产生副作用。
"""

from __future__ import annotations

from typing import Any

from atri_x.comic.characters import AVATAR_SIZE
from atri_x.comic.models import ComicCharacter, ComicPromptError, ComicRenderError
from atri_x.comic.prompt import MAX_COMIC_REFERENCES
from atri_x.imagegen import Reference
from atri_x.media import Input as MediaInput

__all__ = [
    "character_presets",
    "load_references",
    "reference_bytes",
    "validate_reference_capacity",
]


def validate_reference_capacity(plan: Any, cfg: Any) -> list[ComicCharacter]:
    """校验参考图数量与 profile 能力，返回需要参考图的人物（按编号顺序）。"""
    wanted = list(plan.reference_order())
    if len(wanted) > MAX_COMIC_REFERENCES:
        raise ComicPromptError(
            f"参考图最多 {MAX_COMIC_REFERENCES} 张，这次需要 {len(wanted)} 张；请减少人物或分页。"
        )
    if wanted and not bool(getattr(cfg, "supports_reference", False)):
        raise ComicRenderError(
            "当前生图服务未开启参考图支持（image_generation.supports_reference=false），"
            f"而本次漫画需要 {len(wanted)} 张参考图；"
            "请换用支持参考图的 profile，或去掉人物参考图。"
        )
    return wanted


async def load_references(
    ctx: Any,
    plan: Any,
    cfg: Any,
    viewer: Any,
    warnings: list[str],
    *,
    media: Any,
    characters: Any,
    download: bool = True,
) -> tuple[list[ComicCharacter], list[Any]]:
    """加载参考图；返回 (实际带上参考图的人物, Reference 列表)。"""
    wanted = validate_reference_capacity(plan, cfg)
    if not download:
        # dry_run：只校验与编号，不读图、不下载头像（不产生任何网络/媒体副作用）。
        return wanted, []
    loaded: list[ComicCharacter] = []
    references: list[Any] = []
    for character in wanted:
        try:
            data, mime_type = await reference_bytes(
                ctx, character, viewer, media=media, characters=characters
            )
        except ComicRenderError:
            if character.reference.kind == "character_image":
                warnings.append(
                    f"角色卡图片「{character.reference.source}」读取失败，"
                    f"「{character.display_name}」这次只按文字设定绘制。"
                )
                continue
            raise
        if not data:
            warnings.append(
                f"「{character.display_name}」的参考图为空，这次只按文字设定绘制。"
            )
            continue
        loaded.append(character)
        references.append(Reference(data=data, mime_type=mime_type or "image/png"))
    return loaded, references


async def reference_bytes(
    ctx: Any,
    character: ComicCharacter,
    viewer: Any,
    *,
    media: Any,
    characters: Any,
) -> tuple[bytes, str]:
    """读出一张参考图的字节。"""
    reference = character.reference
    if reference.kind == "media":
        if media is None:
            raise ComicRenderError("Media Center 未配置，读不到参考图。")
        try:
            data, stored = await media.read(ctx, reference.source)
        except Exception as exc:  # noqa: BLE001
            raise ComicRenderError(f"读取参考图失败：{exc}") from exc
        return data, str(getattr(stored, "mime_type", "") or "")
    if reference.kind == "character_image":
        if characters is None or not (character.character_id or "").strip():
            raise ComicRenderError("角色素材库未配置，读不到角色卡图片。")
        try:
            data, mime_type = characters.read_image(
                ctx, character.character_id, reference.source, viewer
            )
        except Exception as exc:  # noqa: BLE001
            raise ComicRenderError(f"读取角色卡图片失败：{exc}") from exc
        return data, str(mime_type or "")
    if reference.kind == "avatar":
        if media is None:
            raise ComicRenderError("Media Center 未配置，下载不了平台头像。")
        url = reference.source.strip()
        if not url.startswith(("http://", "https://")):
            raise ComicRenderError("平台头像地址不是 http(s) URL。")
        try:
            stored = await media.import_url(
                ctx,
                url,
                MediaInput(name=f"comic-avatar-{AVATAR_SIZE}.png", mime_type="image/png"),
            )
            data, item = await media.read(ctx, stored.id)
        except Exception as exc:  # noqa: BLE001
            raise ComicRenderError(
                f"下载「{character.display_name}」的平台头像失败：{exc}"
            ) from exc
        return data, str(getattr(item, "mime_type", "") or "")
    return b"", ""


def character_presets(
    ctx: Any,
    plan: Any,
    viewer: Any,
    warnings: list[str],
    *,
    characters: Any,
) -> dict[str, str]:
    """按人物收集角色卡里的图片设定（作为可丢的风格片段）。"""
    out: dict[str, str] = {}
    if characters is None:
        return out
    for character in plan.characters:
        if not character.character_id:
            continue
        try:
            card = characters.get_visible(ctx, character.character_id, viewer)
            preset = str(card.image_prompt() or "").strip()
        except Exception:  # noqa: BLE001 - 角色卡读不到不影响出图
            warnings.append(
                f"读取角色卡「{character.display_name}」的图片预设失败，已跳过。"
            )
            continue
        if preset:
            out[character.id] = preset
    return out
