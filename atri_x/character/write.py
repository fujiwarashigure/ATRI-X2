"""角色写入：创建 / 更新 / 删除、图片索引与版本来源记录。

对应 Go：``internal/character/write.go``。

写入都是原子替换（临时文件 → 替换），并遵循「版本 + 来源」约定：

- ``version`` 显式给定时用它；已存在且未给定时递增（``next_version``）；新建时
  从 ``"1"`` 起步。
- ``source`` 显式给定时用它；否则保留原值，新建时回落到 ``viewer.platform``，
  再回落到 ``"local"``。
- 普通用户建的角色只属于自己且只能是 private；超管可以指定 owner 与可见性。

``character.toml`` 用 :mod:`atri_x.net.tomlw` 写（Python 的 ``tomllib`` 只读），
字段顺序与 ``omitempty`` 语义按 Go 结构体逐字段对齐。
"""

from __future__ import annotations

import os
from typing import Any

from atri_x.character.store import (
    FILE_NAME,
    IMAGES_DIR,
    MAX_DESCRIPTION_RUNES,
    MAX_DOCS_PER_CHARACTER,
    MAX_DOC_BYTES,
    MAX_IMAGES_PER_CHARACTER,
    MAX_LIST_ENTRIES,
    MAX_LIST_ENTRY_RUNES,
    MAX_NAME_RUNES,
    NOTES_DIR,
    Character,
    CharacterError,
    Image,
    ImageSettings,
    Store,
    Viewer,
    VisibilityPrivate,
    VisibilityPublic,
    WriteRequest,
    _CharacterMeta,
    _FileFormat,
    clean_strings,
    doc_file_name,
    first_non_empty,
    next_version,
    normalize_id,
    normalize_visibility,
    now_string,
    safe_image_name,
    to_file,
    valid_id,
    write_file_atomic,
)
from atri_x.errors import ErrForbidden, ErrInvalidID, ErrNotFound

__all__ = ["add_image", "add_image_with_meta", "delete", "remove_image", "write"]


def write(store: Store, ctx: Any, req: WriteRequest, viewer: Viewer | None = None) -> Character:
    """创建或更新一个角色。对应 Go ``Store.Write``。"""
    person = viewer or Viewer()
    req.id = normalize_id(req.id)
    if not valid_id(req.id):
        raise ErrInvalidID
    if not store.enabled():
        raise CharacterError("character library is not configured")
    _validate_write_request(req)
    with store._lock:
        store._scan_locked(ctx)
        existing = store._entries.get(req.id)
        if existing is not None and not existing.manageable_by(person):
            raise ErrForbidden

        meta = _CharacterMeta(id=req.id)
        images: list[Image] = []
        image_settings = ImageSettings()
        created_at = now_string()
        if existing is not None:
            file = to_file(existing)
            meta = file.character
            images = file.images
            image_settings = file.image
            created_at = meta.created_at
        else:
            meta.visibility = str(VisibilityPublic)
        name = str(req.name or "").strip()
        if name:
            meta.name = name
        if not meta.name:
            meta.name = req.id
        if req.aliases is not None:
            meta.aliases = clean_strings(req.aliases)
        if req.description is not None:
            meta.description = str(req.description).strip()
        if req.tags is not None:
            meta.tags = clean_strings(req.tags)
        if str(req.visibility or "").strip():
            meta.visibility = str(normalize_visibility(req.visibility))
        if req.image is not None:
            image_settings = req.image
        if req.version is not None:
            meta.version = str(req.version).strip()
        elif existing is not None:
            meta.version = next_version(meta.version)
        elif not str(meta.version or "").strip():
            meta.version = "1"
        if req.source is not None:
            meta.source = str(req.source).strip()
        elif not str(meta.source or "").strip():
            meta.source = first_non_empty(str(person.platform or "").strip(), "local")
        if not person.superadmin:
            # 普通用户拥有自己的角色，且只能保持 private。
            meta.owner_platform = person.platform
            meta.owner_id = person.actor_id
            meta.visibility = str(VisibilityPrivate)
        elif existing is None:
            meta.owner_platform = str(req.owner_platform or "").strip()
            meta.owner_id = str(req.owner_id or "").strip()
        meta.created_at = first_non_empty(created_at, now_string())
        meta.updated_at = now_string()

        directory = os.path.join(store.root, req.id)
        try:
            os.makedirs(os.path.join(directory, NOTES_DIR), mode=0o755, exist_ok=True)
        except OSError as exc:
            raise CharacterError(f"create character directory: {exc}") from exc
        try:
            os.makedirs(os.path.join(directory, IMAGES_DIR), mode=0o755, exist_ok=True)
        except OSError as exc:
            raise CharacterError(f"create character images directory: {exc}") from exc
        for kind in req.remove_docs or []:
            name, ok = doc_file_name(kind)
            if ok:
                _remove_quietly(os.path.join(directory, name.replace("/", os.sep)))
        for kind, value in (req.docs or {}).items():
            if value is None:
                continue
            name, ok = doc_file_name(kind)
            if not ok:
                raise CharacterError(f'invalid character document "{kind}"')
            path = os.path.join(directory, name.replace("/", os.sep))
            if not str(value).strip():
                _remove_quietly(path)
                continue
            write_file_atomic(path, (str(value).strip() + "\n").encode("utf-8"))
        write_toml(directory, _FileFormat(character=meta, image=image_settings, images=images))
        store._scan_locked(ctx)
        return store._entries[req.id]


def delete(store: Store, ctx: Any, id: str, viewer: Viewer | None = None) -> None:
    """永久删除一个角色目录。对应 Go ``Store.Delete``。"""
    person = viewer or Viewer()
    item = store.get(ctx, id)
    if not item.manageable_by(person):
        raise ErrForbidden
    with store._lock:
        path = os.path.join(store.root, item.id)
        try:
            _remove_tree(path)
        except OSError as exc:
            raise CharacterError(str(exc)) from exc
        store._scan_locked(ctx)


def add_image(
    store: Store,
    ctx: Any,
    id: str,
    name: str,
    mime_type: str,
    media_id: str,
    data: bytes,
    viewer: Viewer | None = None,
) -> Image:
    """存入原始字节与 Media Center 引用。对应 Go ``Store.AddImage``。"""
    return add_image_with_meta(
        store, ctx, id, name, mime_type, media_id, "", "", data, viewer
    )


def add_image_with_meta(
    store: Store,
    ctx: Any,
    id: str,
    name: str,
    mime_type: str,
    media_id: str,
    version: str,
    source: str,
    data: bytes,
    viewer: Viewer | None = None,
) -> Image:
    """``add_image`` 加上显式的资产版本与来源。对应 Go ``Store.AddImageWithMeta``。"""
    person = viewer or Viewer()
    safe = safe_image_name(name)
    if not safe:
        raise CharacterError("invalid image name")
    item = store.get(ctx, id)
    if not item.manageable_by(person):
        raise ErrForbidden
    if len(item.images) >= MAX_IMAGES_PER_CHARACTER:
        if not any(image.name == safe for image in item.images):
            raise CharacterError(f"每个角色最多 {MAX_IMAGES_PER_CHARACTER} 张图片")
    images_dir = os.path.join(item.dir, IMAGES_DIR)
    try:
        os.makedirs(images_dir, mode=0o755, exist_ok=True)
    except OSError as exc:
        raise CharacterError(str(exc)) from exc
    write_file_atomic(os.path.join(images_dir, safe), data)
    image = Image(
        name=safe,
        mime_type=str(mime_type or "").strip(),
        media_id=str(media_id or "").strip(),
        path=IMAGES_DIR + "/" + safe,
        size=len(data),
        version=first_non_empty(str(version or "").strip(), "1"),
        source=first_non_empty(str(source or "").strip(), str(person.platform or "").strip(), "local"),
        created_at=now_string(),
    )
    with store._lock:
        store._scan_locked(ctx)
        current = store._entries.get(item.id)
        if current is None:
            raise ErrNotFound
        file = to_file(current)
        replaced = False
        for index, existing in enumerate(file.images):
            if existing.name == safe:
                file.images[index] = image
                replaced = True
        if not replaced:
            file.images.append(image)
        file.images.sort(key=lambda entry: entry.name)
        file.character.updated_at = now_string()
        write_toml(item.dir, file)
        store._scan_locked(ctx)
        return image


def remove_image(store: Store, ctx: Any, id: str, name: str, viewer: Viewer | None = None) -> None:
    """删除一张图片文件与索引项。对应 Go ``Store.RemoveImage``。"""
    person = viewer or Viewer()
    item = store.get(ctx, id)
    if not item.manageable_by(person):
        raise ErrForbidden
    safe = safe_image_name(name)
    if not safe:
        raise CharacterError("invalid image name")
    with store._lock:
        store._scan_locked(ctx)
        current = store._entries.get(item.id)
        if current is None:
            raise ErrNotFound
        file = to_file(current)
        file.images = [image for image in file.images if image.name != safe]
        file.character.updated_at = now_string()
        write_toml(item.dir, file)
        _remove_quietly(os.path.join(item.dir, IMAGES_DIR, safe))
        store._scan_locked(ctx)


# ---------------------------------------------------------------------------
# TOML 序列化
# ---------------------------------------------------------------------------

def write_toml(directory: str, file: _FileFormat) -> None:
    """写 ``character.toml``（原子替换）。对应 Go ``writeToml``。"""
    from atri_x.net.tomlw import dump  # 局部导入：只在写入路径上需要

    payload = _file_to_payload(file)
    try:
        dump(payload, os.path.join(directory, FILE_NAME), sort_keys=False)
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
    image = _image_settings_payload(file.image)
    if image:
        payload["image"] = image
    payload["images"] = [_image_payload(item) for item in file.images]
    return payload


def _image_settings_payload(settings: ImageSettings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _put(out, "preset_prompt", settings.preset_prompt)
    _put(out, "negative_prompt", settings.negative_prompt)
    _put(out, "size", settings.size)
    _put(out, "quality", settings.quality)
    _put(out, "references", settings.references)
    return out


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


# ---------------------------------------------------------------------------
# 校验与工具
# ---------------------------------------------------------------------------

def _validate_write_request(req: WriteRequest) -> None:
    """对应 Go ``validateWriteRequest``。"""
    if len(str(req.name or "").strip()) > MAX_NAME_RUNES:
        raise CharacterError(f"角色名称最多 {MAX_NAME_RUNES} 字符")
    if req.description is not None:
        if len(str(req.description).strip()) > MAX_DESCRIPTION_RUNES:
            raise CharacterError(f"角色简介最多 {MAX_DESCRIPTION_RUNES} 字符")
    for values in (req.aliases, req.tags):
        if values is None:
            continue
        if len(values) > MAX_LIST_ENTRIES:
            raise CharacterError(f"别名 / tags 每项最多 {MAX_LIST_ENTRIES} 个")
        for value in values:
            if len(str(value or "").strip()) > MAX_LIST_ENTRY_RUNES:
                raise CharacterError(f"别名 / tag 单项最多 {MAX_LIST_ENTRY_RUNES} 字符")
    count = 0
    for value in (req.docs or {}).values():
        if value is None:
            continue
        count += 1
        if len(str(value).encode("utf-8")) > MAX_DOC_BYTES:
            raise CharacterError(f"单个文档最多 {MAX_DOC_BYTES} bytes")
    if count > MAX_DOCS_PER_CHARACTER:
        raise CharacterError(f"每个角色最多 {MAX_DOCS_PER_CHARACTER} 个文档")


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_tree(path: str) -> None:
    """``os.RemoveAll`` 的等价物（目录不存在也算成功）。"""
    import shutil

    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
        return
    _remove_quietly(path)
