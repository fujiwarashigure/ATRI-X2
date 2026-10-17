"""角色库的 sha256 备份清单。

对应 Go：``internal/character/store.go`` 的 ``Store.Manifest`` /
``Store.WriteManifest`` / ``AssetManifest*``（按职责从 ``store.go`` 拆到本模块，
类型本身仍定义在 :mod:`atri_x.character.store`）。

清单被上游 ``deploy/backup.sh`` / ``deploy/restore-verify.sh`` 那条链路用于
备份与恢复校验，因此 JSON 必须与 Go **逐字段一致**：

- 字段名与顺序：``generated_at`` → ``characters[]`` →
  ``id``/``name``/``version``/``source``/``updated_at``/``assets[]`` →
  ``kind``/``name``/``path``/``size``/``version``/``source``/``sha256``；
- ``version`` / ``source`` / ``updated_at`` 带 ``omitempty``（空值省略）；
- ``assets`` **没有** ``omitempty``：没有资产时 Go 输出 ``null``，这里同样输出
  ``null``（不是 ``[]``）；
- 缩进两格、UTF-8 无 BOM、结尾一个换行；``<`` ``>`` ``&`` 与 U+2028/U+2029
  按 Go 默认的 ``SetEscapeHTML(true)`` 转义成 ``\\uXXXX``；
- 读不到文件的条目被**跳过**（与 Go 一致），不是让整份清单失败。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from atri_x.character.store import (
    NOTES_DIR,
    AssetManifest,
    AssetManifestEntry,
    Character,
    CharacterError,
    CharacterManifest,
    Viewer,
    doc_file_name,
    now_string,
    safe_join,
    write_file_atomic,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from atri_x.character.store import Store

__all__ = ["go_json_indent", "manifest", "write_manifest"]


def manifest(store: "Store", ctx: Any = None, viewer: Viewer | None = None) -> AssetManifest:
    """构造所有可见角色的版本 / 来源 / 校验和清单。对应 Go ``Store.Manifest``。"""
    person = viewer or Viewer()
    store._ensure(ctx)
    with store._lock:
        items = [
            store._entries[item_id]
            for item_id in store._order
            if store._entries[item_id].visible_to(person)
        ]

    result = AssetManifest(generated_at=now_string(), characters=[])
    for item in items:
        entry = CharacterManifest(
            id=item.id,
            name=item.name,
            version=item.version,
            source=item.source,
            updated_at=item.updated_at,
            assets=[],
        )
        # Go 用 map 收集再按键排序，因此资产顺序是确定的。
        assets: dict[str, AssetManifestEntry] = {}
        for kind, rel, version, source in _doc_assets(item):
            _add(assets, "doc", kind, rel, version, source, item.dir)
        for image in item.images:
            _add(assets, "image", image.name, image.path, image.version, image.source, item.dir)
        for key in sorted(assets):
            entry.assets.append(assets[key])
        result.characters.append(entry)
    return result


def write_manifest(
    store: "Store", ctx: Any, path: str, viewer: Viewer | None = None
) -> None:
    """原子写一份 JSON 备份清单。对应 Go ``Store.WriteManifest``。"""
    data = (go_json_indent(manifest(store, ctx, viewer)) + "\n").encode("utf-8")
    write_file_atomic(path, data)


def go_json_indent(manifest_value: AssetManifest) -> str:
    """按 Go ``json.MarshalIndent(v, "", "  ")`` 渲染清单（逐字段一致）。"""
    payload: dict[str, Any] = {
        "generated_at": manifest_value.generated_at,
        "characters": [_character_json(item) for item in manifest_value.characters],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": "))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return text


def _doc_assets(character: Character) -> list[tuple[str, str, str, str]]:
    """把 ``docs`` 的键映射成 ``(kind, 相对路径, version, source)``。

    对应 Go ``Store.Manifest`` 里那段 ``for kind := range item.Docs``：正常文档
    用 ``docFileName``；``notes/...`` 的键再回落到 ``notes/<name>.md``。
    """
    out: list[tuple[str, str, str, str]] = []
    for kind in character.docs:
        name, ok = doc_file_name(kind)
        if ok:
            out.append((kind, name, character.version, character.source))
            continue
        prefix = NOTES_DIR + "/"
        if kind.startswith(prefix):
            note = kind[len(prefix) :]
            out.append(
                (kind, NOTES_DIR + "/" + note + ".md", character.version, character.source)
            )
    return out


def _add(
    assets: dict[str, AssetManifestEntry],
    kind: str,
    name: str,
    rel: str,
    version: str,
    source: str,
    directory: str,
) -> None:
    """读文件并登记一项；读不到就跳过（与 Go 一致）。"""
    try:
        path = safe_join(directory, rel)
        with open(path, "rb") as handle:
            data = handle.read()
    except (OSError, CharacterError):
        return
    assets[kind + ":" + name] = AssetManifestEntry(
        kind=kind,
        name=name,
        path=rel.replace("\\", "/"),
        size=len(data),
        version=version,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _character_json(item: CharacterManifest) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": item.id, "name": item.name}
    if item.version:
        payload["version"] = item.version
    if item.source:
        payload["source"] = item.source
    if item.updated_at:
        payload["updated_at"] = item.updated_at
    payload["assets"] = [_asset_json(asset) for asset in item.assets] or None
    return payload


def _asset_json(asset: AssetManifestEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": asset.kind,
        "name": asset.name,
        "path": asset.path,
        "size": asset.size,
    }
    if asset.version:
        payload["version"] = asset.version
    if asset.source:
        payload["source"] = asset.source
    payload["sha256"] = asset.sha256
    return payload
