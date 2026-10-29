"""角色卡导入 / 导出（JSON / ZIP / Markdown / TOML / 目录）。

给两条路径共用：

* 桌面端「人设卡」页的「导入 / 导出」按钮（本地文件，走 GUI 的超级管理员视角）；
* 聊天里的 ``character_import`` 工具（用户直接粘贴一段人设文本，一步建卡）。

一次导入最终都收敛到 :class:`~atri_x.character.store.WriteRequest` +
``Store.write``，所以版本号自增、owner / visibility 规则、文档落盘与聊天工具链路
完全一致，不存在"第二条写卡路径"。

支持的来源：

+----------------+--------------------------------------------------------------+
| 扩展名         | 说明                                                         |
+================+==============================================================+
| ``.json``      | 本模块导出的 JSON（``format = "atri-x-character"``）；不含图片字节 |
| ``.zip``       | 本模块导出的 ZIP（含 ``character.toml`` / 文档 / ``images/``）  |
| ``.toml``      | 单个 ``character.toml``（同目录的 ``*.md`` / ``notes/`` 会一起读）|
| ``.md``/``.txt`` | 整段人设文本：作为 ``profile.md`` 建卡                       |
| 目录            | 结构同 ``characters/<id>/`` 的目录                            |
+----------------+--------------------------------------------------------------+

安全边界：ZIP 只解压到 ``characters/.imports/<uuid>/`` 临时目录（目录名以 ``.`` 开头，
角色库扫描会跳过），拒绝 ``..`` / 绝对路径 / 超量条目，导入结束后删除临时目录。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from atri_x.character.store import (
    DOC_ORDER,
    FILE_NAME,
    IMAGES_DIR,
    MAX_DOCS_PER_CHARACTER,
    NOTES_DIR,
    Character,
    CharacterError,
    ImageSettings,
    Store,
    Viewer,
    WriteRequest,
    normalize_id,
    now_string,
    safe_join,
    valid_id,
    write_file_atomic,
)
from atri_x.errors import AtriXError

__all__ = [
    "ARCHIVE_FORMAT",
    "ARCHIVE_VERSION",
    "CharacterPayload",
    "ImportResult",
    "export_directory",
    "export_json",
    "export_markdown",
    "export_zip",
    "guess_mime_type",
    "import_file",
    "import_payload",
    "import_text",
    "payload_from_directory",
    "payload_from_json",
    "payload_from_zip",
    "slug_id",
]

#: JSON / 清单里标记本格式的字段值。
ARCHIVE_FORMAT = "atri-x-character"
ARCHIVE_VERSION = 1

#: 单个图片上限（与 ``character_manage`` 的 add_image 一致）。
MAX_IMAGE_BYTES = 10 << 20

#: ZIP 解压后的总量 / 条目数上限（防压缩炸弹）。
MAX_ARCHIVE_BYTES = 64 << 20
MAX_ARCHIVE_ENTRIES = 512

#: 临时解压目录名（以 ``.`` 开头，角色库扫描按 id 规则跳过它）。
IMPORT_SCRATCH_DIR = ".imports"

#: 扩展名 → MIME（只用于展示与 media 元数据，不参与解码）。
_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


@dataclass
class CharacterPayload:
    """一份待写进角色库的中间形态（所有导入来源都收敛到这里）。"""

    id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    visibility: str = ""
    source: str = "import"
    docs: dict[str, str] = field(default_factory=dict)
    image: ImageSettings | None = None
    #: ``(文件名, MIME, 字节)``：只有 ZIP / 目录导入才有。
    images: list[tuple[str, str, bytes]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """导入结果（``warnings`` 非空时界面仍算成功，只是有信息要告诉用户）。"""

    character: Character
    created: bool
    warnings: list[str] = field(default_factory=list)


def guess_mime_type(name: str) -> str:
    """按扩展名猜图片 MIME；未知返回空串。"""
    return _MIME_TYPES.get(os.path.splitext(str(name or ""))[1].lower(), "")


def slug_id(text: str, *, fallback: str = "") -> str:
    """把用户给的名字收敛成合法角色 id（ASCII 化；失败时用 ``fallback``）。"""
    raw = str(text or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-._")
    raw = re.sub(r"-{2,}", "-", raw)
    if valid_id(raw):
        return raw
    base = str(fallback or "").strip().lower()
    if valid_id(base):
        return base
    return ""


def _generated_id() -> str:
    """自动 id：``char-<UTC 时间戳>``（调用方只在用户没给合法 id 时用到）。"""
    stamp = now_string().replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
    return "char-" + stamp


# ---------------------------------------------------------------------------
# 文本 / JSON / 目录 / ZIP → CharacterPayload
# ---------------------------------------------------------------------------
def payload_from_json(text: str) -> CharacterPayload:
    """解析本模块导出的 JSON；也容忍"只有一个 character 表"的简化 JSON。"""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CharacterError(f"不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise CharacterError("JSON 顶层必须是对象")
    fmt = str(data.get("format") or "").strip()
    if fmt and fmt != ARCHIVE_FORMAT:
        raise CharacterError(f'不认识的导入格式 "{fmt}"（期望 {ARCHIVE_FORMAT}）')
    character = data.get("character")
    character = character if isinstance(character, dict) else {}
    docs: dict[str, str] = {}
    raw_docs = data.get("docs")
    if isinstance(raw_docs, dict):
        for kind, content in raw_docs.items():
            key = str(kind or "").strip()
            if key and isinstance(content, str) and content.strip():
                docs[key] = content
    payload = CharacterPayload(
        id=normalize_id(str(character.get("id") or "")),
        name=str(character.get("name") or "").strip(),
        aliases=_string_list(character.get("aliases")),
        description=str(character.get("description") or "").strip(),
        tags=_string_list(character.get("tags")),
        visibility=str(character.get("visibility") or "").strip(),
        source=str(character.get("source") or "").strip() or "import",
        docs=docs,
        warnings=[],
    )
    image = data.get("image")
    if isinstance(image, dict):
        payload.image = ImageSettings(
            preset_prompt=str(image.get("preset_prompt") or ""),
            negative_prompt=str(image.get("negative_prompt") or ""),
            size=str(image.get("size") or ""),
            quality=str(image.get("quality") or ""),
            references=_string_list(image.get("references")),
        )
    images = data.get("images")
    if isinstance(images, list) and images:
        payload.warnings.append(
            f"JSON 只带图片元数据（{len(images)} 张），不含图片字节；"
            "需要连图片一起迁移请用 ZIP 导入/导出。"
        )
    if not payload.docs and not payload.name:
        raise CharacterError("JSON 里没有角色文档和名称，无法导入")
    return payload


def payload_from_directory(directory: str) -> CharacterPayload:
    """读一个 ``characters/<id>/`` 结构的目录（也用于 ZIP 解压后的目录）。"""
    root = os.path.abspath(directory)
    toml_path = os.path.join(root, FILE_NAME)
    try:
        with open(toml_path, "rb") as handle:
            parsed = tomllib.loads(handle.read().decode("utf-8"))
    except FileNotFoundError as exc:
        raise CharacterError(f"目录里找不到 {FILE_NAME}：{root}") from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CharacterError(f"读取 {FILE_NAME} 失败：{exc}") from exc
    character = parsed.get("character")
    character = character if isinstance(character, dict) else {}
    payload = CharacterPayload(
        id=normalize_id(str(character.get("id") or "") or os.path.basename(root)),
        name=str(character.get("name") or "").strip(),
        aliases=_string_list(character.get("aliases")),
        description=str(character.get("description") or "").strip(),
        tags=_string_list(character.get("tags")),
        visibility=str(character.get("visibility") or "").strip(),
        source=str(character.get("source") or "").strip() or "import",
        warnings=[],
    )
    for kind in DOC_ORDER:
        content = _read_text(os.path.join(root, kind + ".md"))
        if content.strip():
            payload.docs[kind] = content
    notes_dir = os.path.join(root, NOTES_DIR)
    try:
        note_names = sorted(name for name in os.listdir(notes_dir) if name.lower().endswith(".md"))
    except OSError:
        note_names = []
    for name in note_names:
        content = _read_text(os.path.join(notes_dir, name))
        if content.strip():
            payload.docs[NOTES_DIR + "/" + name[: -len(".md")]] = content
    image = parsed.get("image")
    if isinstance(image, dict):
        payload.image = ImageSettings(
            preset_prompt=str(image.get("preset_prompt") or ""),
            negative_prompt=str(image.get("negative_prompt") or ""),
            size=str(image.get("size") or ""),
            quality=str(image.get("quality") or ""),
            references=_string_list(image.get("references")),
        )
    for entry in parsed.get("images") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        rel = str(entry.get("path") or "").strip() or IMAGES_DIR + "/" + name
        try:
            path = safe_join(root, rel)
            with open(path, "rb") as handle:
                data = handle.read()
        except (OSError, CharacterError):
            payload.warnings.append(f"图片 {name} 在目录里缺失，已跳过。")
            continue
        if len(data) > MAX_IMAGE_BYTES:
            payload.warnings.append(f"图片 {name} 超过 {MAX_IMAGE_BYTES} bytes，已跳过。")
            continue
        mime = str(entry.get("mime_type") or "").strip() or guess_mime_type(name)
        payload.images.append((name, mime, data))
    # 兜底：images/ 里有、但 character.toml 没登记的文件也一起导入（手工整理的目录常见）。
    seen = {name for name, _mime, _data in payload.images}
    images_dir = os.path.join(root, IMAGES_DIR)
    try:
        file_names = sorted(os.listdir(images_dir))
    except OSError:
        file_names = []
    for name in file_names:
        path = os.path.join(images_dir, name)
        if name in seen or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            payload.warnings.append(f"图片 {name} 超过 {MAX_IMAGE_BYTES} bytes，已跳过。")
            continue
        payload.images.append((name, guess_mime_type(name), data))
    if not payload.docs:
        payload.warnings.append("这个角色目录里没有任何文档（profile/world/...），建出来会是空卡。")
    return payload


def payload_from_zip(store: Store, path: str) -> CharacterPayload:
    """解压 ZIP 到角色库内的临时目录，读成 payload，然后删临时目录。"""
    scratch_root = os.path.join(store.root, IMPORT_SCRATCH_DIR)
    scratch = os.path.join(scratch_root, uuid.uuid4().hex)
    os.makedirs(scratch, mode=0o755, exist_ok=True)
    try:
        try:
            with zipfile.ZipFile(path) as archive:
                _extract_zip(archive, scratch)
        except (OSError, zipfile.BadZipFile) as exc:
            raise CharacterError(f"读取 ZIP 失败：{exc}") from exc
        base = _find_character_directory(scratch)
        if base is None:
            markdown = _find_first_markdown(scratch)
            if markdown is None:
                raise CharacterError("ZIP 里既没有 character.toml，也没有可当人设的 .md 文件")
            text = _read_text(markdown)
            return CharacterPayload(
                id="",
                name=os.path.splitext(os.path.basename(markdown))[0],
                docs={"profile": text},
                source="import",
            )
        return payload_from_directory(base)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _extract_zip(archive: zipfile.ZipFile, destination: str) -> None:
    """安全解压：拒绝绝对路径 / 越界，限制总大小与条目数。"""
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise CharacterError(f"ZIP 条目过多（{len(entries)} > {MAX_ARCHIVE_ENTRIES}）")
    total = 0
    for info in entries:
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts) or name.startswith("/"):
            raise CharacterError(f"ZIP 里有不安全的路径：{info.filename}")
        if re.match(r"^[A-Za-z]:", parts[0]):
            raise CharacterError(f"ZIP 里有绝对路径：{info.filename}")
        total += int(info.file_size)
        if total > MAX_ARCHIVE_BYTES:
            raise CharacterError(f"ZIP 解压后超过 {MAX_ARCHIVE_BYTES} bytes 上限")
        target = os.path.join(destination, *parts)
        os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
        with archive.open(info) as source:
            write_file_atomic(target, source.read())


def _find_character_directory(root: str) -> str | None:
    """找最浅的 ``character.toml`` 所在目录（导出的 ZIP 通常就在根上）。"""
    candidates: list[tuple[int, str]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in (IMAGES_DIR, NOTES_DIR)]
        if FILE_NAME in files:
            candidates.append((current.count(os.sep), current))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _find_first_markdown(root: str) -> str | None:
    for current, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith((".md", ".txt")):
                return os.path.join(current, name)
    return None


# ---------------------------------------------------------------------------
# 导入（payload → 角色库）
# ---------------------------------------------------------------------------
def import_payload(
    store: Store,
    ctx: Any,
    payload: CharacterPayload,
    viewer: Viewer | None = None,
    *,
    id_override: str = "",
    name_override: str = "",
) -> ImportResult:
    """把 payload 写进角色库（存在同 id 且可管理时为更新）。"""
    person = viewer or Viewer()
    character_id = normalize_id(id_override or payload.id)
    if not character_id:
        character_id = slug_id(name_override or payload.name, fallback=_generated_id())
    if not valid_id(character_id):
        raise CharacterError(
            "角色 id 不合法：只允许小写字母、数字、-、_、.，且以字母或数字开头"
        )
    name = (name_override or payload.name or "").strip() or character_id
    docs = {kind: content for kind, content in payload.docs.items() if str(content or "").strip()}
    if len(docs) > MAX_DOCS_PER_CHARACTER:
        raise CharacterError(f"每个角色最多 {MAX_DOCS_PER_CHARACTER} 个文档")
    request = WriteRequest(
        id=character_id,
        name=name,
        aliases=list(payload.aliases or []),
        description=payload.description,
        tags=list(payload.tags or []),
        visibility=(payload.visibility or "").strip(),
        source=payload.source or "import",
        docs=docs,
    )
    if payload.image is not None:
        request.image = payload.image
    created = _character_missing(store, ctx, character_id)
    character = store.write(ctx, request, person)
    warnings = list(payload.warnings)
    for image_name, mime_type, data in payload.images:
        try:
            store.add_image_with_meta(
                ctx, character_id, image_name, mime_type, "", "", payload.source or "import", data, person
            )
        except (AtriXError, OSError) as exc:
            warnings.append(f"图片 {image_name} 导入失败：{exc}")
    current = store.get(ctx, character_id)
    return ImportResult(character=current, created=created, warnings=warnings)


def import_text(
    store: Store,
    ctx: Any,
    text: str,
    viewer: Viewer | None = None,
    *,
    character_id: str = "",
    name: str = "",
    description: str = "",
    tags: list[str] | None = None,
    docs: dict[str, str] | None = None,
    visibility: str = "",
    source: str = "import",
) -> ImportResult:
    """把一段粘贴的人设文本建卡（聊天里"一步导入"的核心）。

    ``text`` 会写进 ``profile.md``；``docs`` 里额外的 key 一并写入（world /
    greeting / examples / notes/<name>）。``name`` 为空时取正文第一个标题。
    """
    body = str(text or "").strip()
    extra = {key: value for key, value in (docs or {}).items() if str(value or "").strip()}
    if not body and not extra:
        raise CharacterError("没有可导入的人设正文")
    if body and "profile" not in extra:
        extra["profile"] = body
    payload = CharacterPayload(
        id=normalize_id(character_id),
        name=(name or "").strip() or _first_heading(body),
        description=description,
        tags=list(tags or []),
        visibility=visibility,
        source=source,
        docs=extra,
    )
    return import_payload(store, ctx, payload, viewer)


def import_file(
    store: Store,
    ctx: Any,
    path: str,
    viewer: Viewer | None = None,
    *,
    character_id: str = "",
    name: str = "",
) -> ImportResult:
    """按扩展名 / 目录分派导入。"""
    target = os.path.abspath(str(path or ""))
    if not os.path.exists(target):
        raise CharacterError(f"找不到要导入的文件：{target}")
    if os.path.isdir(target):
        payload = payload_from_directory(target)
        return import_payload(store, ctx, payload, viewer, id_override=character_id, name_override=name)
    lower = target.lower()
    if lower.endswith(".zip"):
        payload = payload_from_zip(store, target)
        return import_payload(store, ctx, payload, viewer, id_override=character_id, name_override=name)
    if lower.endswith(".json"):
        payload = payload_from_json(_read_text(target))
        return import_payload(store, ctx, payload, viewer, id_override=character_id, name_override=name)
    if lower.endswith(".toml"):
        payload = payload_from_directory(os.path.dirname(target))
        return import_payload(store, ctx, payload, viewer, id_override=character_id, name_override=name)
    if lower.endswith((".md", ".txt")):
        return import_text(
            store,
            ctx,
            _read_text(target),
            viewer,
            character_id=character_id,
            name=name or os.path.splitext(os.path.basename(target))[0],
        )
    raise CharacterError("只支持导入 .zip / .json / .toml / .md / .txt 或角色目录")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_json(
    store: Store, ctx: Any, character_id: str, output_path: str, viewer: Viewer | None = None
) -> Character:
    """导出为单个 JSON 文件（文档全文 + 图片元数据，不含图片字节）。"""
    item = store.get_visible(ctx, character_id, viewer)
    payload: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_VERSION,
        "exported_at": now_string(),
        "character": {
            "id": item.id,
            "name": item.name,
            "aliases": list(item.aliases),
            "description": item.description,
            "tags": list(item.tags),
            "visibility": str(item.visibility),
            "source": item.source,
            "version": item.version,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        },
        "docs": dict(sorted(item.docs.items())),
        "image": {
            "preset_prompt": item.image.preset_prompt,
            "negative_prompt": item.image.negative_prompt,
            "size": item.image.size,
            "quality": item.image.quality,
            "references": list(item.image.references),
        },
        "images": [
            {
                "name": image.name,
                "path": image.path,
                "mime_type": image.mime_type,
                "size": image.size,
            }
            for image in item.images
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    write_file_atomic(output_path, text.encode("utf-8"))
    return item


def export_zip(
    store: Store, ctx: Any, character_id: str, output_path: str, viewer: Viewer | None = None
) -> Character:
    """导出为 ZIP（整份角色目录，含图片字节，可在另一台机器原样导入）。"""
    item = store.get_visible(ctx, character_id, viewer)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for current, _dirs, files in os.walk(item.dir):
            for name in sorted(files):
                absolute = os.path.join(current, name)
                relative = os.path.relpath(absolute, item.dir).replace("\\", "/")
                archive.write(absolute, relative)
    return item


def export_directory(
    store: Store, ctx: Any, character_id: str, output_dir: str, viewer: Viewer | None = None
) -> Character:
    """导出一个角色目录（复制成 ``<output_dir>/<id>/``）。"""
    item = store.get_visible(ctx, character_id, viewer)
    target = os.path.join(os.path.abspath(output_dir), item.id)
    shutil.copytree(item.dir, target, dirs_exist_ok=True)
    return item


def export_markdown(
    store: Store, ctx: Any, character_id: str, output_path: str, viewer: Viewer | None = None
) -> Character:
    """只导出 ``profile.md`` 正文（适合分享"人设文本"）。"""
    item = store.get_visible(ctx, character_id, viewer)
    body = str(item.docs.get("profile") or "").strip()
    text = body or item.prompt()
    write_file_atomic(output_path, (text.rstrip() + "\n").encode("utf-8"))
    return item


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _character_missing(store: Store, ctx: Any, character_id: str) -> bool:
    try:
        store.get(ctx, character_id)
    except AtriXError:
        return True
    return False


def _first_heading(text: str) -> str:
    """正文第一个 Markdown 标题（``# xxx``）当角色名；没有就返回空串。"""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:128]
    return ""


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise CharacterError(f"读取 {path} 失败：{exc}") from exc


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
