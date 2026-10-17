"""文件读取、按 revision 守卫的编辑与写回。

对应 Go：``internal/utils/fileops/file.go``。

要点：

- 读取时同时得到文本、编码、BOM 与行尾；写回时按原样还原（UTF-16/GBK 等
  不会因为一次编辑被改成 UTF-8）。
- UTF-8 文件在前 4096 字节内出现 NUL 时判定为二进制并拒绝。
- 位置类操作（replace / insert / delete / overwrite）在**已存在**的文件上
  必须带 ``expected_revision``；revision 不匹配时拒绝覆盖。
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass

from atri_x.errors import FileChangedError
from atri_x.utils.fileops.atomic_write import atomic_write_file
from atri_x.utils.fileops.diff import edit_diff
from atri_x.utils.fileops.edit import Edit, apply_edits, edits_require_revision
from atri_x.utils.fileops.encoding import (
    BOM_UTF8,
    decode_bytes,
    encode_text,
    is_utf8_encoding,
    lookup_encoding,
)
from atri_x.utils.fileops.text import (
    content_revision,
    detect_line_ending,
    looks_binary_utf8,
    normalize_context_lines,
    normalize_edit_text,
    restore_line_endings,
)

__all__ = [
    "EditFileOptions",
    "EditResult",
    "File",
    "MAX_FILE_SIZE",
    "edit_file",
    "edit_file_with_options",
    "read_file",
    "read_file_with_limit",
    "read_or_create_file",
    "read_or_create_file_with_limit",
    "write_text_file",
]

MAX_FILE_SIZE = 2 * 1024 * 1024


@dataclass
class EditFileOptions:
    """编辑的输入/输出/详细 diff 上限。对应 Go ``fileops.EditFileOptions``。"""

    max_input_bytes: int = 0
    max_output_bytes: int = 0
    detailed_diff_max_bytes: int = 0


@dataclass
class File:
    """一个已解码的文件。对应 Go ``fileops.File``。"""

    path: str = ""
    bytes: bytes = b""
    text: str = ""
    encoding: str = ""
    bom: bytes | None = None
    line_ending: str = "\n"
    ends_newline: bool = False


@dataclass
class EditResult:
    """一次编辑的结果。对应 Go ``fileops.EditResult``。"""

    dry_run: bool = False
    path: str = ""
    created: bool = False
    encoding: str = ""
    revision_before: str = ""
    revision_after: str = ""
    diff: str = ""
    new_bytes: bytes = b""


def read_file(path: str, requested_encoding: str = "") -> File:
    """按上限 :data:`MAX_FILE_SIZE` 读取文件。"""
    return read_file_with_limit(path, requested_encoding, MAX_FILE_SIZE)


def read_file_with_limit(
    path: str, requested_encoding: str = "", max_bytes: int = MAX_FILE_SIZE
) -> File:
    """读取并解码文件；目录、超限与二进制文件都会抛错。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OSError(f"stat file: {exc}") from exc
    if stat_module.S_ISDIR(info.st_mode):
        raise ValueError("path is a directory")
    if max_bytes > 0 and info.st_size > max_bytes:
        raise ValueError(f"file too large: {info.st_size} bytes exceeds {max_bytes}")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise OSError(f"read file: {exc}") from exc
    text, encoding_name, bom = decode_bytes(data, requested_encoding)
    if is_utf8_encoding(encoding_name) and looks_binary_utf8(text):
        raise ValueError("file appears to be binary")
    return File(
        path=path,
        bytes=data,
        text=text,
        encoding=encoding_name,
        bom=bom,
        line_ending=detect_line_ending(text),
        ends_newline=text.endswith("\n"),
    )


def read_or_create_file(
    path: str,
    requested_encoding: str,
    create: bool,
    has_expected_revision: bool,
) -> tuple[File, bool]:
    """读取文件；不存在且允许新建时返回一个空的新文件描述。"""
    return read_or_create_file_with_limit(
        path, requested_encoding, create, has_expected_revision, MAX_FILE_SIZE
    )


def read_or_create_file_with_limit(
    path: str,
    requested_encoding: str,
    create: bool,
    has_expected_revision: bool,
    max_bytes: int = MAX_FILE_SIZE,
) -> tuple[File, bool]:
    try:
        return read_file_with_limit(path, requested_encoding, max_bytes), False
    except FileNotFoundError:
        if not create or has_expected_revision:
            raise
    encoding_name = (requested_encoding or "").strip().lower()
    if encoding_name in ("", "auto", "utf8"):
        encoding_name = "utf-8"
    if encoding_name == "utf-8-bom":
        return (
            File(path=path, encoding="utf-8-bom", bom=BOM_UTF8, line_ending="\n"),
            True,
        )
    if encoding_name != "utf-8":
        lookup_encoding(encoding_name)
    return File(path=path, encoding=encoding_name, line_ending="\n"), True


def edit_file(
    path: str,
    requested_encoding: str,
    expected_revision: str,
    create: bool,
    dry_run: bool,
    context_lines: int,
    edits: list[Edit],
) -> EditResult:
    """编辑文件（输入上限 :data:`MAX_FILE_SIZE`）。"""
    return edit_file_with_options(
        path,
        requested_encoding,
        expected_revision,
        create,
        dry_run,
        context_lines,
        edits,
        EditFileOptions(max_input_bytes=MAX_FILE_SIZE),
    )


def edit_file_with_options(
    path: str,
    requested_encoding: str,
    expected_revision: str,
    create: bool,
    dry_run: bool,
    context_lines: int,
    edits: list[Edit],
    options: EditFileOptions,
) -> EditResult:
    """按选项编辑文件；任何失败都不会改动磁盘内容。"""
    expected_revision = (expected_revision or "").strip()
    if expected_revision:
        if len(expected_revision) != 16:
            raise ValueError("expected_revision must be 16 hexadecimal characters")
        try:
            bytes.fromhex(expected_revision)
        except ValueError as exc:
            raise ValueError(
                "expected_revision must be 16 hexadecimal characters"
            ) from exc
    file, created = read_or_create_file_with_limit(
        path, requested_encoding, create, expected_revision != "", options.max_input_bytes
    )
    old_revision = content_revision(file.bytes)
    if expected_revision and expected_revision.lower() != old_revision:
        raise FileChangedError(f"file revision mismatch: current {old_revision}")
    if not created and not expected_revision and edits_require_revision(edits):
        raise ValueError(
            "expected_revision is required for replace, insert, delete, "
            "and overwrite on existing files"
        )
    old_text = normalize_edit_text(file.text)
    new_text = apply_edits(old_text, edits)
    if new_text == old_text:
        raise ValueError("edit produced no changes")
    output_text = restore_line_endings(new_text, file.line_ending)
    new_bytes = encode_text(output_text, file.encoding, file.bom)
    if options.max_output_bytes > 0 and len(new_bytes) > options.max_output_bytes:
        raise ValueError(
            f"edited file too large: {len(new_bytes)} bytes exceeds "
            f"{options.max_output_bytes}"
        )
    context_lines = normalize_context_lines(context_lines)
    result = EditResult(
        dry_run=dry_run,
        path=file.path,
        created=created,
        encoding=file.encoding,
        revision_before=old_revision,
        revision_after=content_revision(new_bytes),
        diff=edit_diff(
            file.path,
            old_text,
            new_text,
            len(file.bytes),
            len(new_bytes),
            context_lines,
            options.detailed_diff_max_bytes,
        ),
        new_bytes=new_bytes,
    )
    if dry_run:
        return result
    if created:
        directory = os.path.dirname(path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                raise OSError(f"create parent directory: {exc}") from exc
    mode = 0o644
    try:
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        if not created:
            raise
    except OSError as exc:
        raise OSError(f"stat file: {exc}") from exc
    try:
        atomic_write_file(path, new_bytes, mode)
    except OSError as exc:
        raise OSError(f"write file: {exc}") from exc
    return result


def write_text_file(path: str, base: File, text: str) -> bytes:
    """按 ``base`` 的编码/行尾把 ``text`` 写回 ``path``。"""
    output_text = restore_line_endings(text, base.line_ending)
    new_bytes = encode_text(output_text, base.encoding, base.bom)
    try:
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        raise OSError(f"stat file: {exc}") from exc
    try:
        atomic_write_file(path, new_bytes, mode)
    except OSError as exc:
        raise OSError(f"write file: {exc}") from exc
    return new_bytes
