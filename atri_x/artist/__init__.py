"""画师串领域包。

对应 Go：``internal/artist``。包内模块布局见 ``PORTING.md`` §4：

* ``types``：类型、枚举与上限（与 Go ``types.go`` 一一对应）。
* ``metadata`` / ``metadata_formats``：PNG/JPEG/WebP 容器解析（A1111 / NovelAI /
  ComfyUI / InvokeAI 方言）。
* ``parser``：确定性的提示词分段、权重解包与画师识别。
* ``render``：按方言把 preset 渲染成最终画师串。
* ``search``：检索与推荐排序（不判断权限——可见性只在 service 一处决定）。
* ``store``：单文件 JSON 串库 + 归一化校验 + revision 写序。
* ``metadata_store``：入库转码前留档的生成参数（``media.GenerationSink``）。
* ``service``：身份视图、可见性、保存/删除与编译。

导出约定：Go 的哨兵 error 在本仓收敛到 :mod:`atri_x.errors`，这里按 Go 的名字
（``ErrDisabled`` / ``ErrNotFound`` / ``ErrConflict`` / ``ErrForbidden`` /
``ErrLimitExceeded`` / ``ErrInvalid``）**再导出同一批单例**，让工具层可以用
``getattr(atri_x.artist, "ErrXxx")`` 做 ``is`` 判定。
"""

from __future__ import annotations

from atri_x.errors import (
    ErrConflict,
    ErrDisabled,
    ErrForbidden,
    ErrInvalid,
    ErrLimitExceeded,
    ErrNotFound,
)

from . import metadata, metadata_formats, metadata_store, parser, render, search, service, store, types
from .metadata import extract_metadata, parse_metadata, parse_metadata_with_origin
from .metadata_store import (
    GENERATION_CAPTURE_VERSION,
    GENERATION_SCHEMA,
    MAX_GENERATIONS,
    Generation,
    GenerationStore,
    new_generation_store,
)
from .parser import (
    Resolver,
    artist_name,
    classify,
    escape,
    extract_artists,
    extract_artists_with,
    extract_from_graph,
    has_top_level_comma,
    normalize_tag,
    outer_pair,
    split_segments,
    top_level_colon,
    unwrap_weight,
)
from .render import (
    Render,
    WeightedTag,
    describe_style,
    format_weight,
    render_preset,
    render_tag,
    render_tags,
)
from .search import (
    DEFAULT_SEARCH_LIMIT,
    MATCH_ARTIST,
    MATCH_PRESET,
    MAX_SEARCH_LIMIT,
    Match,
    rank,
    search,
)
from .service import (
    Compiled,
    Service,
    Viewer,
    compile_raw,
    group_scope_key,
    new_artist_id,
    new_preset_id,
    no_metadata_hint,
    slugify,
)
from .store import (
    LIBRARY_SCHEMA,
    MAX_ALIASES,
    MAX_ARTISTS,
    MAX_ID_READ_RUNES,
    MAX_ID_RUNES,
    MAX_NAME_RUNES,
    MAX_NOTE_RUNES,
    MAX_PRESET_ARTISTS,
    MAX_PRESETS,
    MAX_RAW_STRING_RUNES,
    MAX_STYLE_TAGS,
    MAX_TAG_RUNES,
    MAX_WEIGHT,
    MIN_WEIGHT,
    REVISION_ANY,
    REVISION_CREATE,
    ArtistConflictError,
    ReferencedError,
    Store,
    artist_invalid,
    clean_list,
    lookup_artist,
    normalize_artist,
    normalize_preset,
    valid_id,
    validate_artist,
    validate_preset,
)
from .types import (
    Artist,
    ArtistRef,
    Dialect,
    Extraction,
    ImageMetadata,
    Library,
    Metadata,
    MetadataOriginArchive,
    MetadataOriginEmbedded,
    Preset,
    PresetArtist,
    PresetSource,
    PresetStatus,
    Scope,
    SourceKind,
    Token,
    TokenKind,
    normalize_dialect,
    normalize_scope,
    normalize_source,
    normalize_status,
)

__all__ = [
    # 模块
    "metadata",
    "metadata_formats",
    "metadata_store",
    "parser",
    "render",
    "search",
    "service",
    "store",
    "types",
    # 哨兵（同 :mod:`atri_x.errors` 的模块级单例）
    "ErrConflict",
    "ErrDisabled",
    "ErrForbidden",
    "ErrInvalid",
    "ErrLimitExceeded",
    "ErrNotFound",
    # metadata
    "extract_metadata",
    "parse_metadata",
    "parse_metadata_with_origin",
    # types
    "Artist",
    "ArtistRef",
    "Dialect",
    "Extraction",
    "ImageMetadata",
    "Library",
    "Metadata",
    "MetadataOriginArchive",
    "MetadataOriginEmbedded",
    "Preset",
    "PresetArtist",
    "PresetSource",
    "PresetStatus",
    "Scope",
    "SourceKind",
    "Token",
    "TokenKind",
    "normalize_dialect",
    "normalize_scope",
    "normalize_source",
    "normalize_status",
    # parser
    "Resolver",
    "artist_name",
    "classify",
    "escape",
    "extract_artists",
    "extract_artists_with",
    "extract_from_graph",
    "has_top_level_comma",
    "normalize_tag",
    "outer_pair",
    "split_segments",
    "top_level_colon",
    "unwrap_weight",
    # render
    "Render",
    "WeightedTag",
    "describe_style",
    "format_weight",
    "render_preset",
    "render_tag",
    "render_tags",
    # search
    "DEFAULT_SEARCH_LIMIT",
    "MATCH_ARTIST",
    "MATCH_PRESET",
    "MAX_SEARCH_LIMIT",
    "Match",
    "rank",
    "search",
    # store
    "LIBRARY_SCHEMA",
    "MAX_ALIASES",
    "MAX_ARTISTS",
    "MAX_ID_READ_RUNES",
    "MAX_ID_RUNES",
    "MAX_NAME_RUNES",
    "MAX_NOTE_RUNES",
    "MAX_PRESET_ARTISTS",
    "MAX_PRESETS",
    "MAX_RAW_STRING_RUNES",
    "MAX_STYLE_TAGS",
    "MAX_TAG_RUNES",
    "MAX_WEIGHT",
    "MIN_WEIGHT",
    "REVISION_ANY",
    "REVISION_CREATE",
    "ArtistConflictError",
    "ReferencedError",
    "Store",
    "artist_invalid",
    "clean_list",
    "lookup_artist",
    "normalize_artist",
    "normalize_preset",
    "valid_id",
    "validate_artist",
    "validate_preset",
    # metadata_store
    "GENERATION_CAPTURE_VERSION",
    "GENERATION_SCHEMA",
    "MAX_GENERATIONS",
    "Generation",
    "GenerationStore",
    "new_generation_store",
    # service
    "Compiled",
    "Service",
    "Viewer",
    "compile_raw",
    "group_scope_key",
    "new_artist_id",
    "new_preset_id",
    "no_metadata_hint",
    "slugify",
]
