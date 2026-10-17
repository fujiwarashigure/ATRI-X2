"""文件生命周期、编码、原子编辑、匹配、diff 与按 Session 的回滚。

对应 Go：``internal/utils/fileops/``。

模块布局（与 Go 文件一一对应）：

- ``text.py`` —— ``text.go``：行尾/行号/revision
- ``encoding.py`` —— ``encoding.go``：编解码与 BOM
- ``atomic_write.py`` —— ``atomic_write*.go``：临时文件 + fsync + 原子替换
- ``match.py`` —— ``match.go``：目标匹配与候选展示
- ``edit.py`` —— ``edit.go``：操作解析与套用
- ``diff.py`` —— ``diff.go``：unified diff
- ``file.py`` —— ``file.go``：读写与 revision 守卫
- ``rollback.py`` —— ``rollback.go``：编辑前备份与回滚
"""

from __future__ import annotations

from atri_x.utils.fileops.atomic_write import atomic_write_file, replace_file_atomic
from atri_x.utils.fileops.diff import (
    DiffHunk,
    MAX_DIFF_CELLS,
    MAX_DIFF_OUTPUT_BYTES,
    build_diff_hunks,
    diff_lines,
    edit_diff,
    omitted_diff,
    unified_diff,
)
from atri_x.utils.fileops.edit import (
    EDIT_OPERATIONS,
    Edit,
    apply_edits,
    edit_operation_properties,
    edits_require_revision,
    scan_edit_lines,
)
from atri_x.utils.fileops.encoding import (
    BOM_UTF8,
    BOM_UTF16_BE,
    BOM_UTF16_LE,
    Codec,
    decode_bytes,
    encode_text,
    is_utf8_encoding,
    lookup_encoding,
)
from atri_x.utils.fileops.file import (
    MAX_FILE_SIZE,
    EditFileOptions,
    EditResult,
    File,
    edit_file,
    edit_file_with_options,
    read_file,
    read_file_with_limit,
    read_or_create_file,
    read_or_create_file_with_limit,
    write_text_file,
)
from atri_x.utils.fileops.match import (
    MatchLocation,
    SourceLine,
    find_content_matches,
    find_line_matches,
    format_line_range,
    format_match_locations,
    match_preview,
    offset_to_line,
    select_matches,
    truncate_line,
)
from atri_x.utils.fileops.rollback import (
    ROLLBACK_MAX_BYTES,
    ROLLBACK_MAX_RECORDS,
    RollbackInfo,
    RollbackStore,
    new_rollback_store,
)
from atri_x.utils.fileops.text import (
    CONTENT_REVISION_BYTES,
    DEFAULT_READ_LINE_LIMIT,
    LineNumber,
    content_revision,
    detect_line_ending,
    ensure_trailing_newline,
    looks_binary_utf8,
    normalize_context_lines,
    normalize_edit_text,
    normalize_grep_context_lines,
    normalize_max_matches,
    normalize_read_range,
    restore_line_endings,
    split_lines,
    validate_line_range,
)

__all__ = [name for name in dir() if not name.startswith("_")]
