"""人物与参考图绑定（P0-3）。

核心约定：**人物身份是稳定的，显示名不是**。

* 群友的身份键是 ``member:<platform>:<sender_id>``，同名不同人的显示名不会串；
* 角色卡的身份键是 ``character:<角色 ID>``，走角色库的可见性校验；
* 参考图来源优先级：本次显式指定 > 角色卡图片 > 平台头像；用户明确要求
  "就用头像"（``mode=avatars``）时**不**偷偷换成角色卡。

缺图时：``mode=auto`` 记录 ``source=none`` + 告警（明确说"没有拿到参考图"）；
``mode=avatars`` 直接报错，不伪装成"已按头像绘制"；``mode=characters`` 里角色卡
没图片也只是告警，因为文字设定仍然可用。

本模块不触网：头像只解析出 **URL**（渲染阶段才经媒体中心下载）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from atri_x.comic.models import (
    MAX_COMIC_CHARACTERS,
    ComicCharacter,
    ComicCharacterError,
    ComicCharacterReference,
    ComicContext,
    ComicContextMessage,
)
from atri_x.media import ID_PREFIX, ValidID

__all__ = [
    "AVATAR_SIZE",
    "CharacterResolveRequest",
    "CharacterResolveResult",
    "ComicAvatarResolver",
    "NoAvatarResolver",
    "character_identity",
    "member_identity",
    "parse_reference_overrides",
    "resolve_comic_characters",
    "sender_identity_key",
]

#: 头像请求尺寸（与平台能力上限 640 对齐，够做参考图）。
AVATAR_SIZE = 320

_ID_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_ID_MAX_PART = 48


@runtime_checkable
class ComicAvatarResolver(Protocol):
    """能按平台 + 用户 ID 解析头像 URL（对应 ``platform.UserAvatarProvider``）。"""

    async def avatar_url(self, ctx: Any, platform: str, user_id: str) -> str: ...


class NoAvatarResolver:
    """没有平台头像能力时的空实现：永远返回空 URL（调用方据此告警/报错）。"""

    async def avatar_url(self, ctx: Any, platform: str, user_id: str) -> str:
        return ""


def _safe_part(value: Any) -> str:
    text = _ID_SAFE.sub("_", str(value or "").strip())
    return text[:_ID_MAX_PART] or "unknown"


def sender_identity_key(platform: str, sender_id: str, sender_name: str) -> str:
    """群友的稳定身份键。没有 sender_id 时退回显示名（仍保持可追溯）。"""
    platform_part = _safe_part(platform)
    if (sender_id or "").strip():
        return f"member:{platform_part}:{_safe_part(sender_id)}"
    return f"member:{platform_part}:name:{_safe_part(sender_name)}"


#: 交叉检索别名。
member_identity = sender_identity_key


def character_identity(character_id: str) -> str:
    """角色卡的稳定身份键。"""
    return "character:" + _safe_part(character_id)


@dataclass
class CharacterResolveRequest:
    """这次漫画要把哪些人画进去。"""

    #: ``auto``（有角色卡用卡、否则头像/无图）| ``characters``（只用角色卡）| ``avatars``。
    mode: str = "auto"
    #: 显式角色 ID（角色库 id）；给了就只画这些角色卡。
    character_ids: list[str] = field(default_factory=list)
    #: 本次显式指定的参考图：``<标识>=<参考>``。标识可以是角色 ID / 群友 ID /
    #: 显示名；参考可以是 ``media:<sha256>`` 或角色图片名。没有 ``=`` 的条目按
    #: 顺序补给还没有参考图的人物。
    reference_images: list[str] = field(default_factory=list)
    max_characters: int = MAX_COMIC_CHARACTERS


@dataclass
class CharacterResolveResult:
    characters: list[ComicCharacter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, ComicCharacter]:
        return {item.id: item for item in self.characters}


def _load_cards(store: Any, ctx: Any, viewer: Any) -> list[Any]:
    if store is None:
        return []
    enabled = getattr(store, "enabled", None)
    if callable(enabled) and not enabled():
        return []
    try:
        return list(store.list(ctx, viewer) or [])
    except Exception:  # noqa: BLE001 - 角色库异常不应阻止漫画（最多没有卡）
        return []


def _match_card(cards: list[Any], name: str, aliases_extra: tuple[str, ...] = ()) -> Any:
    """按显示名/别名匹配角色卡（大小写与首尾空白不敏感）。"""
    wanted: set[str] = set()
    for value in (name, *aliases_extra):
        text = (value or "").strip().casefold()
        if text:
            wanted.add(text)
    if not wanted:
        return None
    for card in cards:
        candidates = [str(getattr(card, "name", "") or "")]
        candidates.extend(str(item or "") for item in getattr(card, "aliases", []) or [])
        for candidate in candidates:
            if candidate.strip().casefold() in wanted:
                return card
    return None


def _card_reference(card: Any) -> ComicCharacterReference:
    """角色卡的首选参考图：``image.references[0]`` > 第一张图片名 > 无。"""
    settings = getattr(card, "image", None)
    references = getattr(settings, "references", None) or []
    for item in references:
        name = str(item or "").strip()
        if name:
            return ComicCharacterReference(kind="character_image", source=name)
    for image in getattr(card, "images", []) or []:
        name = str(getattr(image, "name", "") or "").strip()
        media_id = str(getattr(image, "media_id", "") or "").strip()
        if name:
            return ComicCharacterReference(kind="character_image", source=name)
        if media_id and ValidID(media_id):
            return ComicCharacterReference(kind="media", source=media_id)
    return ComicCharacterReference()


def parse_reference_overrides(
    values: list[str],
) -> tuple[dict[str, ComicCharacterReference], list[ComicCharacterReference]]:
    """解析 ``reference_images``：返回 (按标识的覆盖, 无标识按顺序补位)。"""
    scoped: dict[str, ComicCharacterReference] = {}
    bare: list[ComicCharacterReference] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        key = ""
        value = text
        if "=" in text:
            key, _, value = text.partition("=")
            key = key.strip()
            value = value.strip()
        if not value:
            continue
        if ValidID(value):
            reference = ComicCharacterReference(kind="media", source=value)
        elif value.startswith(ID_PREFIX):
            # 形如 media:<sha256> 但不是合法 ID：明确拒绝，别把坏 ID 当图片名。
            raise ComicCharacterError(f"参考图 media id 不合法：{value}")
        else:
            reference = ComicCharacterReference(kind="character_image", source=value)
        if key:
            scoped[key.casefold()] = reference
        else:
            bare.append(reference)
    return scoped, bare


def _identity_match_keys(character: ComicCharacter) -> tuple[str, ...]:
    keys = [character.id, character.character_id, character.member_id]
    return tuple(item for item in keys if (item or "").strip())


def _ordered_members(context: ComicContext) -> list[ComicContextMessage]:
    """按首次出现顺序去重的发言人列表。"""
    out: list[ComicContextMessage] = []
    seen: set[str] = set()
    for item in context.messages:
        key = sender_identity_key(context.platform, item.sender_id, item.sender_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def resolve_comic_characters(
    ctx: Any,
    *,
    context: ComicContext,
    request: CharacterResolveRequest,
    store: Any = None,
    viewer: Any = None,
    avatar_resolver: Any = None,
) -> CharacterResolveResult:
    """解析这次漫画的人物与参考图。"""
    mode = (request.mode or "auto").strip().lower() or "auto"
    if mode not in ("auto", "characters", "avatars"):
        raise ComicCharacterError(f"人物模式只能是 auto、characters 或 avatars，收到 {request.mode!r}")
    max_characters = int(request.max_characters or 0) or MAX_COMIC_CHARACTERS
    max_characters = max(1, min(max_characters, MAX_COMIC_CHARACTERS))
    result = CharacterResolveResult()
    cards = _load_cards(store, ctx, viewer)

    # 1) 显式角色 ID：优先于模式（用户点名要哪几张卡）。
    explicit_ids = [str(item or "").strip() for item in request.character_ids or []]
    explicit_ids = [item for item in explicit_ids if item]
    if explicit_ids and mode == "avatars":
        raise ComicCharacterError("character_ids 与 mode=avatars 冲突：要么指定角色卡，要么用头像。")
    if explicit_ids:
        if store is None:
            raise ComicCharacterError("角色库未启用，无法使用 character_ids；请改用 mode=avatars。")
        chosen: list[ComicCharacter] = []
        seen_ids: set[str] = set()
        for character_id in explicit_ids:
            if character_id in seen_ids:
                continue
            seen_ids.add(character_id)
            try:
                card = store.get_visible(ctx, character_id, viewer)
            except Exception as exc:  # noqa: BLE001
                raise ComicCharacterError(
                    f"找不到可访问的角色卡 {character_id}（{exc}）"
                ) from exc
            chosen.append(_character_from_card(card))
        result.characters = chosen
        _apply_reference_overrides(result, request.reference_images)
        _ensure_capacity(result.characters, max_characters)
        return result

    # 2) 按模式解析聊天里的发言人。
    members = _ordered_members(context)
    if mode == "characters":
        for member in members:
            card = _match_card(cards, member.sender_name)
            if card is None:
                result.warnings.append(
                    f"群友「{member.sender_name or member.sender_id}」没有同名角色卡，本次不画进去。"
                )
                continue
            result.characters.append(_character_from_card(card))
        if not result.characters:
            raise ComicCharacterError(
                "没有找到与聊天发言人对得上的角色卡；请改用 mode=avatars，"
                "或先用 character_import 导入人设。"
            )
    elif mode == "avatars":
        if avatar_resolver is None:
            raise ComicCharacterError(
                "当前平台没有接入头像能力，无法使用 mode=avatars；请指定角色卡，"
                "或用 reference_images 提供参考图。"
            )
        for member in members:
            url = await _avatar_url(ctx, avatar_resolver, context.platform, member)
            if not url:
                raise ComicCharacterError(
                    f"拿不到群友「{member.sender_name or member.sender_id}」的头像，"
                    "本次没有生成请求。请不要在缺图时假装已按头像绘制："
                    "确认平台头像能力或改用参考图。"
                )
            result.characters.append(
                ComicCharacter(
                    id=sender_identity_key(context.platform, member.sender_id, member.sender_name),
                    display_name=_member_name(member),
                    source="avatar",
                    member_platform=context.platform,
                    member_id=member.sender_id,
                    reference=ComicCharacterReference(kind="avatar", source=url),
                )
            )
    else:  # auto
        for member in members:
            card = _match_card(cards, member.sender_name)
            if card is not None:
                character = _character_from_card(card)
                if character.reference.kind == "none":
                    result.warnings.append(
                        f"角色卡「{character.display_name}」没有图片，本次只按文字设定绘制。"
                    )
                result.characters.append(character)
                continue
            url = ""
            if avatar_resolver is not None:
                url = await _avatar_url(ctx, avatar_resolver, context.platform, member)
            if url:
                result.characters.append(
                    ComicCharacter(
                        id=sender_identity_key(
                            context.platform, member.sender_id, member.sender_name
                        ),
                        display_name=_member_name(member),
                        source="avatar",
                        member_platform=context.platform,
                        member_id=member.sender_id,
                        reference=ComicCharacterReference(kind="avatar", source=url),
                        notes="没有角色卡，使用平台头像",
                    )
                )
            else:
                result.characters.append(
                    ComicCharacter(
                        id=sender_identity_key(
                            context.platform, member.sender_id, member.sender_name
                        ),
                        display_name=_member_name(member),
                        source="none",
                        member_platform=context.platform,
                        member_id=member.sender_id,
                        notes="既没有角色卡也没有头像，仅按显示名与台词绘制",
                    )
                )

    # 去重（同名角色卡可能被两个昵称各匹配一次）。
    deduped: list[ComicCharacter] = []
    seen_characters: set[str] = set()
    for character in result.characters:
        if character.id in seen_characters:
            continue
        seen_characters.add(character.id)
        deduped.append(character)
    result.characters = deduped
    _ensure_capacity(result.characters, max_characters)
    _apply_reference_overrides(result, request.reference_images)
    for character in result.characters:
        if character.source == "none":
            result.warnings.append(
                f"「{character.display_name}」没有参考图：不会声称按头像/角色图绘制。"
            )
    return result


def _member_name(member: ComicContextMessage) -> str:
    name = (member.sender_name or "").strip()
    return name or (member.sender_id or "").strip() or "群友"


def _character_from_card(card: Any) -> ComicCharacter:
    character_id = str(getattr(card, "id", "") or "").strip()
    name = str(getattr(card, "name", "") or "").strip() or character_id
    return ComicCharacter(
        id=character_identity(character_id),
        display_name=name,
        source="character",
        character_id=character_id,
        reference=_card_reference(card),
    )


async def _avatar_url(
    ctx: Any, resolver: Any, platform: str, member: ComicContextMessage
) -> str:
    getter = getattr(resolver, "avatar_url", None)
    if not callable(getter):
        return ""
    user_id = (member.sender_id or "").strip()
    if not user_id:
        return ""
    try:
        return str(await getter(ctx, platform, user_id) or "").strip()
    except Exception:  # noqa: BLE001 - 头像失败不拖垮规划，按"没有头像"处理
        return ""


def _ensure_capacity(characters: list[ComicCharacter], limit: int) -> None:
    if len(characters) <= limit:
        return
    names = "、".join(item.display_name for item in characters)
    raise ComicCharacterError(
        f"一页漫画最多 {limit} 个人物，这次识别到 {len(characters)} 个：{names}。"
        "请减少人物（character_ids / 缩小聊天范围）或分页画。"
    )


def _apply_reference_overrides(
    result: CharacterResolveResult, values: list[str]
) -> None:
    """把本次显式参考图套到人物上（优先级最高）。"""
    scoped, bare = parse_reference_overrides(values)
    if not scoped and not bare:
        return
    used_bare = 0
    for character in result.characters:
        keys = _identity_match_keys(character) + (character.display_name,)
        matched = None
        for key in keys:
            reference = scoped.get(str(key or "").strip().casefold())
            if reference is not None:
                matched = reference
                break
        if matched is None and used_bare < len(bare):
            matched = bare[used_bare]
            used_bare += 1
        if matched is None:
            continue
        character.reference = matched
        if character.source == "none":
            character.source = "media" if matched.kind == "media" else "character"
    leftover = len(bare) - used_bare
    if leftover > 0:
        result.warnings.append(f"有 {leftover} 张显式参考图没有对上任何人物，已忽略。")
