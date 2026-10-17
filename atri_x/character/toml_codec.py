"""``character.toml`` 的读写映射。

对应 Go：``internal/character/store.go`` 的读取部分（``readCharacter`` /
``readDoc`` / ``characterMeta`` / ``fileFormat`` / ``Character.toFile``）与
``internal/character/write.go`` 的写入部分（``writeToml``）。按文件长度上限
从两个模块里拆出（Go 直接用 ``pelletier/go-toml/v2`` 的结构体标签做映射，
Python 的 ``tomllib`` 只读，因此两边都要手写映射）。

两条与 Go 对齐的约定：

- 读取时未知键忽略（go-toml 默认不严格），字段缺失取零值；
- 写入时按结构体顺序输出并实现 ``omitempty``：空串 / 空切片 / 0 不写进文件。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from atri_x.character.store import (
    DOC_ORDER,
    FILE_NAME,
    NOTES_DIR,
    Character,
    CharacterError,
    Image,
    ImageSettings,
    _NAME_PATTERN,
    _dir_entries,
    clean_strings,
    doc_file_name,
    normalize_visibility,
    safe_join,
    valid_id,
)

__all__ = [
    "read_character",
    "read_doc",
    "to_file",
    "write_toml",
]


@dataclass(slots=True)
class _CharacterMeta:
    """``character.toml`` 的 ``[character]`` 段。对应 Go ``characterMeta``。"""

    id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    owner_platform: str = ""
    owner_id: str = ""
    visibility: str = ""
    version: str = ""
    source: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class _FileFormat:
    """``character.toml`` 的整体结构。对应 Go ``fileFormat``。"""

    character: _CharacterMeta = field(default_factory=_CharacterMeta)
    image: ImageSettings = field(default_factory=ImageSettings)
    images: list[Image] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 读取（Go store.go）
# ---------------------------------------------------------------------------

def read_character(directory: str) -> Character:
    """读一个角色目录。对应 Go ``readCharacter``。"""
    path = os.path.join(directory, FILE_NAME)
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CharacterError(f"parse {FILE_NAME}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CharacterError(f"parse {FILE_NAME}: 顶层不是表")
    file = _file_from_toml(parsed)
    meta = file.character
    if not valid_id(meta.id):
        raise CharacterError(f'{FILE_NAME}: invalid id "{meta.id}"')
    item = Character(
        id=meta.id,
        name=str(meta.name or "").strip(),
        aliases=clean_strings(meta.aliases),
        description=str(meta.description or "").strip(),
        tags=clean_strings(meta.tags),
        owner_platform=str(meta.owner_platform or "").strip(),
        owner_id=str(meta.owner_id or "").strip(),
        visibility=normalize_visibility(meta.visibility),
        version=str(meta.version or "").strip(),
        source=str(meta.source or "").strip(),
        created_at=str(meta.created_at or "").strip(),
        updated_at=str(meta.updated_at or "").strip(),
        dir=directory,
        images=list(file.images),
        image=file.image,
        docs={},
    )
    if not item.name:
        item.name = item.id
    for kind in DOC_ORDER:
        name, ok = doc_file_name(kind)
        if not ok:
            continue
        try:
            content = read_doc(directory, name)
        except (OSError, CharacterError):
            continue
        if content.strip():
            item.docs[kind] = content
    notes_dir = os.path.join(directory, NOTES_DIR)
    try:
        notes = sorted(entry.name for entry in _dir_entries(notes_dir) if not entry.is_dir())
    except OSError:
        notes = []
    for name in notes:
        if not name.lower().endswith(".md"):
            continue
        base = os.path.splitext(name)[0]
        if not _NAME_PATTERN.match(base):
            continue
        try:
            content = read_doc(directory, NOTES_DIR + "/" + name)
        except (OSError, CharacterError):
            continue
        if content.strip():
            item.docs[NOTES_DIR + "/" + base] = content
    item.images.sort(key=lambda image: image.name)
    return item


def read_doc(directory: str, rel: str) -> str:
    """对应 Go ``readDoc``。"""
    path = safe_join(directory, rel)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _file_from_toml(parsed: Mapping[str, Any]) -> _FileFormat:
    """把 TOML 表映射成 :class:`_FileFormat`（未知键忽略，与 go-toml 一致）。"""
    character = parsed.get("character")
    character = character if isinstance(character, Mapping) else {}
    image = parsed.get("image")
    image = image if isinstance(image, Mapping) else {}
    raw_images = parsed.get("images")
    raw_images = raw_images if isinstance(raw_images, list) else []
    return _FileFormat(
        character=_CharacterMeta(
            id=_text(character.get("id")),
            name=_text(character.get("name")),
            aliases=_text_list(character.get("aliases")),
            description=_text(character.get("description")),
            tags=_text_list(character.get("tags")),
            owner_platform=_text(character.get("owner_platform")),
            owner_id=_text(character.get("owner_id")),
            visibility=_text(character.get("visibility")),
            version=_text(character.get("version")),
            source=_text(character.get("source")),
            created_at=_text(character.get("created_at")),
            updated_at=_text(character.get("updated_at")),
        ),
        image=ImageSettings(
            preset_prompt=_text(image.get("preset_prompt")),
            negative_prompt=_text(image.get("negative_prompt")),
            size=_text(image.get("size")),
            quality=_text(image.get("quality")),
            references=_text_list(image.get("references")),
        ),
        images=[
            Image(
                name=_text(item.get("name")),
                mime_type=_text(item.get("mime_type")),
                media_id=_text(item.get("media_id")),
                path=_text(item.get("path")),
                size=int(item.get("size") or 0),
                version=_text(item.get("version")),
                source=_text(item.get("source")),
                created_at=_text(item.get("created_at")),
            )
            for item in raw_images
            if isinstance(item, Mapping)
        ],
    )


def to_file(character: Character) -> _FileFormat:
    """把角色还原成待写入的文件结构。对应 Go ``Character.toFile``。"""
    return _FileFormat(
        character=_CharacterMeta(
            id=character.id,
            name=character.name,
            aliases=list(character.aliases),
            description=character.description,
            tags=list(character.tags),
            owner_platform=character.owner_platform,
            owner_id=character.owner_id,
            visibility=str(character.visibility),
            version=character.version,
            source=character.source,
            created_at=character.created_at,
            updated_at=character.updated_at,
        ),
        image=character.image,
        images=list(character.images),
    )


# ---------------------------------------------------------------------------
# 写入（Go write.go）
# ---------------------------------------------------------------------------

def write_toml(directory: str, file: _FileFormat) -> None:
    """写 ``character.toml``（原子替换）。对应 Go ``writeToml``。"""
    from atri_x.net.tomlw import dump  # 局部导入：只在写入路径上需要

    try:
        dump(_file_to_payload(file), os.path.join(directory, FILE_NAME), sort_keys=False)
    except (ValueError, OSError) as exc:
        raise CharacterError(f"encode {FILE_NAME}: {exc}") from exc


def _file_to_payload(file: _FileFormat) -> dict[str, Any]:
    """按 Go 结构体顺序与 ``omitempty`` 语义构造 TOML 映射。"""
    meta = file.character
    character: dict[str, Any] = {"id": meta.id, "name": meta.name}
    _put(character, "aliases", meta.aliases)
    _put(character, "description", meta.description)
    _put(character, "tags", meta.tags)
    _put(character, "owner_platform", meta.owner_platform)
    _put(character, "owner_id", meta.owner_id)
    _put(character, "visibility", meta.visibility)
    _put(character, "version", meta.version)
    _put(character, "source", meta.source)
    _put(character, "created_at", meta.created_at)
    _put(character, "updated_at", meta.updated_at)

    payload: dict[str, Any] = {"character": character}
    image: dict[str, Any] = {}
    _put(image, "preset_prompt", file.image.preset_prompt)
    _put(image, "negative_prompt", file.image.negative_prompt)
    _put(image, "size", file.image.size)
    _put(image, "quality", file.image.quality)
    _put(image, "references", file.image.references)
    if image:
        payload["image"] = image
    payload["images"] = [_image_payload(item) for item in file.images]
    return payload


def _image_payload(image: Image) -> dict[str, Any]:
    out: dict[str, Any] = {"name": image.name}
    _put(out, "mime_type", image.mime_type)
    _put(out, "media_id", image.media_id)
    _put(out, "path", image.path)
    if image.size:
        out["size"] = image.size
    _put(out, "version", image.version)
    _put(out, "source", image.source)
    _put(out, "created_at", image.created_at)
    return out


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    """Go ``omitempty``：零值不写进 TOML。"""
    if isinstance(value, str):
        if value:
            target[key] = value
        return
    if isinstance(value, (list, tuple)):
        if len(value):
            target[key] = list(value)
        return
    if value:
        target[key] = value


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []
