"""基于目录的角色库（自包含子系统）。

对应 Go：``internal/character``（``store.go`` / ``write.go`` / ``search.go``）。

本包**不** import ``atri_x.config``：上游同一个包也不 import ``internal/config``。
调用方（``internal/agent`` → ``atri_x/agent``、``internal/tool/builtin`` →
``atri_x/tool/builtin``）自己把配置里的角色根目录交给 :class:`Store`，并用
:class:`Viewer` 表达调用者身份。

模块划分（与 Go 文件一一对应）：

- :mod:`atri_x.character.store` — ``store.go``：目录扫描、可见性、清单类型。
- :mod:`atri_x.character.write` — ``write.go``：写入 / 删除 / 图片索引 / TOML。
- :mod:`atri_x.character.search` — ``search.go``：内存检索。
- :mod:`atri_x.character.manifest` — ``store.go`` 的清单构建与 JSON 渲染
  （按文件长度上限拆出；类型仍在 ``store`` 里）。

``Store`` 上的方法全部就位（``write`` / ``delete`` / ``add_image`` /
``add_image_with_meta`` / ``remove_image`` / ``search`` / ``manifest`` /
``write_manifest`` / ``read_image``）。写入类自由函数另以
``write_character`` / ``add_image`` / ``add_image_with_meta`` / ``remove_image``
/ ``delete`` 导出；清单构建用 ``Store.manifest`` 或
``atri_x.character.manifest.manifest``。
"""

from __future__ import annotations

from atri_x.character.activation import (
    SOUL_BACKUP_SUFFIX,
    SoulActivationResult,
    SoulStatus,
    activate_character,
    render_soul_from_character,
    restore_soul,
    soul_backup_path,
    soul_status,
)
from atri_x.character.archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_VERSION,
    CharacterPayload,
    ImportResult,
    export_directory,
    export_json,
    export_markdown,
    export_zip,
    import_file,
    import_payload,
    import_text,
    payload_from_directory,
    payload_from_json,
    payload_from_zip,
)
from atri_x.character.manifest import go_json_indent
from atri_x.character.search import (
    MAX_SEARCH_LIMIT,
    SNIPPET_RUNES,
    has_tag,
    make_snippet,
    match_terms,
    split_query_terms,
)
from atri_x.character.search import search as search_results
from atri_x.character.store import (
    DEFAULT_SEARCH_LIMIT,
    DOC_ORDER,
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
    SCAN_COOLDOWN,
    AssetManifest,
    AssetManifestEntry,
    Character,
    CharacterError,
    CharacterManifest,
    Image,
    ImageSettings,
    SearchResult,
    Store,
    Viewer,
    Visibility,
    VisibilityPrivate,
    VisibilityPublic,
    WriteRequest,
    active_ids,
    clean_strings,
    doc_file_name,
    next_version,
    normalize_id,
    normalize_visibility,
    now_string,
    safe_image_name,
    safe_join,
    valid_id,
    with_active,
)
from atri_x.character.write import (
    add_image,
    add_image_with_meta,
    delete,
    remove_image,
    write_toml,
)
from atri_x.character.write import write as write_character
from atri_x.errors import ErrConflict, ErrForbidden, ErrInvalidID, ErrNotFound

#: Go 导出名的交叉检索别名（PascalCase）。
NewStore = Store
WithActive = with_active
ActiveIDs = active_ids
SafeImageName = safe_image_name
DocFileName = doc_file_name
Search = search_results

#: 注意：包级**不**导出裸名 ``write``，否则会盖掉同名子模块
#: :mod:`atri_x.character.write`（``atri_x.character.write.write(...)`` 才是
#: 文件模块里的自由函数；``Store.write(...)`` 才是 Go 的 ``Store.Write``）。
Write = write_character

__all__ = [
    "ARCHIVE_FORMAT",
    "ARCHIVE_VERSION",
    "ActiveIDs",
    "AssetManifest",
    "AssetManifestEntry",
    "Character",
    "CharacterError",
    "CharacterManifest",
    "CharacterPayload",
    "DEFAULT_SEARCH_LIMIT",
    "DOC_ORDER",
    "DocFileName",
    "ErrConflict",
    "ErrForbidden",
    "ErrInvalidID",
    "ErrNotFound",
    "FILE_NAME",
    "IMAGES_DIR",
    "Image",
    "ImageSettings",
    "ImportResult",
    "MAX_DESCRIPTION_RUNES",
    "MAX_DOCS_PER_CHARACTER",
    "MAX_DOC_BYTES",
    "MAX_IMAGES_PER_CHARACTER",
    "MAX_LIST_ENTRIES",
    "MAX_LIST_ENTRY_RUNES",
    "MAX_NAME_RUNES",
    "MAX_SEARCH_LIMIT",
    "NOTES_DIR",
    "NewStore",
    "SCAN_COOLDOWN",
    "SNIPPET_RUNES",
    "SOUL_BACKUP_SUFFIX",
    "SafeImageName",
    "Search",
    "SearchResult",
    "SoulActivationResult",
    "SoulStatus",
    "Store",
    "Viewer",
    "Visibility",
    "VisibilityPrivate",
    "VisibilityPublic",
    "WithActive",
    "Write",
    "WriteRequest",
    "activate_character",
    "active_ids",
    "add_image",
    "add_image_with_meta",
    "clean_strings",
    "delete",
    "doc_file_name",
    "export_directory",
    "export_json",
    "export_markdown",
    "export_zip",
    "go_json_indent",
    "has_tag",
    "import_file",
    "import_payload",
    "import_text",
    "make_snippet",
    "match_terms",
    "next_version",
    "normalize_id",
    "normalize_visibility",
    "now_string",
    "payload_from_directory",
    "payload_from_json",
    "payload_from_zip",
    "remove_image",
    "render_soul_from_character",
    "restore_soul",
    "safe_image_name",
    "safe_join",
    "search_results",
    "soul_backup_path",
    "soul_status",
    "split_query_terms",
    "valid_id",
    "with_active",
    "write_character",
    "write_toml",
]
