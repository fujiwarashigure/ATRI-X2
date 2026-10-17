"""``atri_x.utils.fileops`` 的单元测试。

对应 Go：``internal/utils/fileops/*_test.go``（match / line_mapper / revision /
encoding / rollback）。
"""

from __future__ import annotations

import os
import shutil
import stat
import unittest
import uuid

from atri_x.errors import (
    ErrRollbackConflict,
    ErrRollbackNotFound,
    FileChangedError,
)
from atri_x.utils.fileops import (
    BOM_UTF8,
    Edit,
    EditFileOptions,
    LineNumber,
    RollbackStore,
    apply_edits,
    atomic_write_file,
    content_revision,
    decode_bytes,
    detect_line_ending,
    edit_file,
    edit_file_with_options,
    encode_text,
    ensure_trailing_newline,
    find_content_matches,
    find_line_matches,
    normalize_context_lines,
    normalize_edit_text,
    normalize_grep_context_lines,
    normalize_max_matches,
    normalize_read_range,
    read_file,
    read_or_create_file,
    restore_line_endings,
    scan_edit_lines,
    select_matches,
    split_lines,
    unified_diff,
    write_text_file,
)


def _edit(**kwargs) -> Edit:
    return Edit(**kwargs)


#: DSH 文件沙箱只允许写工作区，因此临时目录建在仓库根下并在用例结束时清理。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_temp_dir(case: unittest.TestCase) -> str:
    """建一个用例级临时目录。

    注意：这里刻意用 ``os.makedirs(mode=0o777)`` 而不是 ``tempfile.mkdtemp``
    —— 后者按 POSIX 惯例建 0o700 目录，而本机 M: 卷（配合 DSH 沙箱）会因此
    拒绝后续写入，与 POSIX 语义不同。参考：probe 显示 0o700 目录不可写、
    0o755/0o777 可写。
    """
    while True:
        directory = os.path.join(_REPO_ROOT, ".fileops-test-" + uuid.uuid4().hex[:12])
        if not os.path.exists(directory):
            break
    os.makedirs(directory, mode=0o777)
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory


class TestTextHelpers(unittest.TestCase):
    def test_content_revision_matches_go(self) -> None:
        self.assertEqual(content_revision(b"alpha\n"), "b6a98d9ce9a2d914")
        self.assertEqual(len(content_revision(b"")), 16)

    def test_normalize_and_restore_line_endings(self) -> None:
        self.assertEqual(normalize_edit_text("a\r\nb\rc\n"), "a\nb\nc\n")
        self.assertEqual(restore_line_endings("a\nb", "\r\n"), "a\r\nb")
        self.assertEqual(restore_line_endings("a\nb", "\n"), "a\nb")
        self.assertEqual(detect_line_ending("a\r\nb"), "\r\n")
        self.assertEqual(detect_line_ending("a\rb"), "\r")
        self.assertEqual(detect_line_ending("a\nb"), "\n")

    def test_split_lines_and_trailing_newline(self) -> None:
        self.assertEqual(split_lines(""), [])
        self.assertEqual(split_lines("a"), ["a"])
        self.assertEqual(split_lines("a\n"), ["a"])
        self.assertEqual(split_lines("a\r\nb"), ["a", "b"])
        self.assertEqual(split_lines("\n"), [""])
        self.assertEqual(ensure_trailing_newline("a"), "a\n")
        self.assertEqual(ensure_trailing_newline("a\n"), "a\n")
        self.assertEqual(ensure_trailing_newline(""), "")

    def test_normalize_limits(self) -> None:
        self.assertEqual(normalize_grep_context_lines(0), 2)
        self.assertEqual(normalize_grep_context_lines(-1), 0)
        self.assertEqual(normalize_grep_context_lines(99), 20)
        self.assertEqual(normalize_max_matches(0), 20)
        self.assertEqual(normalize_max_matches(1000), 100)
        self.assertEqual(normalize_context_lines(0), 3)
        self.assertEqual(normalize_context_lines(-2), 0)
        self.assertEqual(normalize_context_lines(99), 20)

    def test_normalize_read_range(self) -> None:
        self.assertEqual(normalize_read_range(0, 1, LineNumber()), (1, 0, False))
        self.assertEqual(normalize_read_range(500, 1, LineNumber()), (1, 200, True))
        self.assertEqual(normalize_read_range(10, 3, LineNumber()), (3, 10, False))
        self.assertEqual(
            normalize_read_range(100, 1, LineNumber(value=0, end=True)), (1, 100, False)
        )
        with self.assertRaises(ValueError):
            normalize_read_range(5, 6, LineNumber())
        with self.assertRaises(ValueError):
            normalize_read_range(5, 3, LineNumber(value=2))

    def test_line_number_from_json(self) -> None:
        self.assertEqual(LineNumber.from_json("end"), LineNumber(value=0, end=True))
        self.assertEqual(LineNumber.from_json(" 12 "), LineNumber(value=12))
        self.assertEqual(LineNumber.from_json(7), LineNumber(value=7))
        self.assertEqual(LineNumber.from_json(""), LineNumber())
        self.assertEqual(LineNumber.from_json(None), LineNumber())
        with self.assertRaises(ValueError):
            LineNumber.from_json("abc")
        with self.assertRaises(ValueError):
            LineNumber.from_json([1])


class TestAtomicWrite(unittest.TestCase):
    def test_write_and_replace(self) -> None:
        directory = make_temp_dir(self)
        path = os.path.join(directory, "sample.txt")
        atomic_write_file(path, b"alpha\n", 0o600)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        atomic_write_file(path, b"beta\n", 0o600)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"beta\n")
        # 没有残留临时文件
        leftovers = [
            name for name in os.listdir(directory) if name.startswith(".sample.txt.tmp-")
        ]
        self.assertEqual(leftovers, [])

    def test_write_follows_symlink(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows 上创建符号链接需要额外权限")
        directory = make_temp_dir(self)
        target = os.path.join(directory, "target.txt")
        link = os.path.join(directory, "link.txt")
        with open(target, "wb") as handle:
            handle.write(b"old")
        os.symlink(target, link)
        atomic_write_file(link, b"new", 0o644)
        self.assertTrue(os.path.islink(link))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"new")


class TestApplyEdits(unittest.TestCase):
    def test_replace_text_unique_and_multiline(self) -> None:
        got = apply_edits(
            "alpha\nbeta\ngamma\n",
            [_edit(operation="replace_text", old_text="beta\ngamma", new_text="BETA\nGAMMA")],
        )
        self.assertEqual(got, "alpha\nBETA\nGAMMA\n")

    def test_replace_text_can_delete(self) -> None:
        got = apply_edits(
            "alpha\nbeta\ngamma\n",
            [_edit(operation="replace_text", old_text="beta\n", new_text="")],
        )
        self.assertEqual(got, "alpha\ngamma\n")

    def test_match_ambiguity_shows_candidates(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                "beta one\nbeta two\n",
                [_edit(operation="replace_text", old_text="beta", new_text="BETA")],
            )
        message = str(caught.exception)
        self.assertIn("matched 2 locations", message)
        self.assertIn("#1", message)
        self.assertIn("#2", message)

    def test_index_and_all_matches(self) -> None:
        got = apply_edits(
            "beta\nbeta\n",
            [_edit(operation="replace_text", old_text="beta", new_text="BETA", index=2)],
        )
        self.assertEqual(got, "beta\nBETA\n")
        got = apply_edits(
            "beta\nbeta\n",
            [_edit(operation="replace_text", old_text="beta", new_text="BETA", all_matches=True)],
        )
        self.assertEqual(got, "BETA\nBETA\n")
        with self.assertRaises(ValueError):
            apply_edits(
                "beta\n",
                [
                    _edit(
                        operation="replace_text",
                        old_text="beta",
                        new_text="BETA",
                        index=1,
                        all_matches=True,
                    )
                ],
            )
        with self.assertRaises(ValueError):
            apply_edits(
                "beta\n",
                [_edit(operation="replace_text", old_text="beta", new_text="BETA", index=3)],
            )

    def test_line_anchor_ignores_indentation(self) -> None:
        got = apply_edits(
            "func main() {\n\treturn\n}\n",
            [
                _edit(
                    operation="replace_line",
                    anchor="return",
                    new_text='\tfmt.Println("done")\n\treturn',
                )
            ],
        )
        self.assertEqual(got, 'func main() {\n\tfmt.Println("done")\n\treturn\n}\n')

    def test_anchor_insert_handles_line_boundaries(self) -> None:
        got = apply_edits(
            "alpha\ngamma\n",
            [
                _edit(operation="insert_before", anchor="gamma", new_text="  beta"),
                _edit(operation="insert_after", anchor="gamma", new_text="delta"),
            ],
        )
        self.assertEqual(got, "alpha\n  beta\ngamma\ndelta\n")

    def test_delete_line_by_anchor(self) -> None:
        got = apply_edits(
            "alpha\n  beta\ngamma", [_edit(operation="delete_line", anchor="beta")]
        )
        self.assertEqual(got, "alpha\ngamma")

    def test_anchor_must_be_single_line(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                "alpha\nbeta\n",
                [_edit(operation="replace_line", anchor="alpha\nbeta", new_text="X")],
            )
        self.assertIn("single-line prefix", str(caught.exception))

    def test_insert_by_position(self) -> None:
        cases = (
            ("", 1, "X"),
            ("A\nB\n", 1, "X\nA\nB\n"),
            ("A\nB\n", 2, "A\nX\nB\n"),
            ("A\nB\n", 3, "A\nB\nX\n"),
            ("A\nB", 3, "A\nB\nX"),
        )
        for text, line, want in cases:
            got = apply_edits(text, [_edit(operation="insert", line=line, new_text="X")])
            self.assertEqual(got, want, f"line={line}")

    def test_delete_line_range(self) -> None:
        cases = (
            ("A\nB\nC\nD\n", 2, 3, "A\nD\n"),
            ("A\nB\nC", 3, 0, "A\nB"),
            ("A\nB\nC\n", 3, 0, "A\nB\n"),
            ("A\nB", 1, 2, ""),
        )
        for text, line, end, want in cases:
            got = apply_edits(
                text, [_edit(operation="delete", line=line, end_line=end)]
            )
            self.assertEqual(got, want, f"line={line} end={end}")

    def test_replace_line_range_uses_raw_text(self) -> None:
        cases = (
            ("A\nB\nC\n", 2, 0, "X\n", "A\nX\nC\n"),
            ("A\nB\nC\n", 2, 0, "X", "A\nXC\n"),
            ("A\nB\nC\nD\n", 2, 3, "X\nY\n", "A\nX\nY\nD\n"),
            ("A\nB\nC\n", 2, 0, "", "A\nC\n"),
            ("A\nB", 2, 0, "\nX", "A\nX"),
            ("A\nB\n", 1, 2, "X", "X"),
            ("A\nB", 1, 2, "X\n", "X\n"),
        )
        for text, line, end, new_text, want in cases:
            got = apply_edits(
                text,
                [
                    _edit(
                        operation="replace",
                        line=line,
                        end_line=end,
                        new_text=new_text,
                    )
                ],
            )
            self.assertEqual(got, want, f"{text!r} line={line} end={end}")

    def test_replace_line_range_validation(self) -> None:
        cases = (
            ("A\n", _edit(operation="replace", line=1), "new_text is required"),
            (
                "A\nB\n",
                _edit(operation="replace", line=2, end_line=1, new_text="X"),
                "end_line must be >= line",
            ),
            (
                "A\n",
                _edit(operation="replace", line=2, new_text="X"),
                "exceeds total lines",
            ),
            (
                "",
                _edit(operation="replace", line=1, new_text="X"),
                "file has no lines",
            ),
        )
        for text, edit, want in cases:
            with self.assertRaises(ValueError) as caught:
                apply_edits(text, [edit])
            self.assertIn(want, str(caught.exception))

    def test_overwrite_is_raw_and_exclusive(self) -> None:
        got = apply_edits("old\n", [_edit(operation="overwrite", new_text="new\n\n")])
        self.assertEqual(got, "new\n\n")
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                "A\n",
                [
                    _edit(operation="overwrite", new_text="B\n"),
                    _edit(operation="insert", line=1, new_text="X"),
                ],
            )
        self.assertIn("overwrite must be the only edit", str(caught.exception))

    def test_missing_new_text_and_edits(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_edits("alpha\n", [_edit(operation="replace_text", old_text="alpha")])
        self.assertIn("new_text is required", str(caught.exception))
        with self.assertRaises(ValueError):
            apply_edits("alpha\n", [])
        with self.assertRaises(ValueError):
            apply_edits("alpha\n", [_edit(operation="nope")])

    def test_targets_original_content(self) -> None:
        got = apply_edits(
            "A\nB\nC\n",
            [
                _edit(operation="replace_text", old_text="B", new_text="BB"),
                _edit(operation="insert", line=3, new_text="X"),
            ],
        )
        self.assertEqual(got, "A\nBB\nX\nC\n")
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                "A\nB\n",
                [
                    _edit(operation="replace_text", old_text="A", new_text="X"),
                    _edit(operation="replace_text", old_text="X", new_text="Y"),
                ],
            )
        self.assertIn("old_text not found", str(caught.exception))

    def test_same_position_insertions_keep_request_order(self) -> None:
        got = apply_edits(
            "A\nB\n",
            [
                _edit(operation="insert", line=2, new_text="X"),
                _edit(operation="insert", line=2, new_text="Y"),
            ],
        )
        self.assertEqual(got, "A\nX\nY\nB\n")

    def test_rejects_overlaps_and_allows_boundary_insertion(self) -> None:
        with self.assertRaises(ValueError) as caught:
            apply_edits(
                "A\nB\nC\n",
                [
                    _edit(operation="replace_text", old_text="B\nC", new_text="BC"),
                    _edit(operation="replace", line=2, end_line=3, new_text="X\n"),
                ],
            )
        self.assertIn("overlap", str(caught.exception))
        got = apply_edits(
            "A\nB\n",
            [
                _edit(operation="replace_line", anchor="B", new_text="BB"),
                _edit(operation="insert_before", anchor="B", new_text="X"),
            ],
        )
        self.assertEqual(got, "A\nX\nBB\n")

    def test_scan_edit_lines(self) -> None:
        lines = scan_edit_lines("ab\ncd")
        self.assertEqual([(line.start, line.end, line.full_end) for line in lines],
                         [(0, 2, 3), (3, 5, 5)])
        self.assertEqual(scan_edit_lines(""), [])


class TestMatch(unittest.TestCase):
    def test_find_content_matches_reports_lines_and_preview(self) -> None:
        text = "alpha\nbeta one\nbeta two\n"
        matches = find_content_matches(text, "beta")
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].start_line, 2)
        self.assertEqual(matches[1].start_line, 3)
        # 预览从行首截到匹配结尾（与 Go 的 matchPreview 一致）。
        self.assertEqual(matches[0].preview, "beta")
        self.assertEqual(matches[1].preview, "beta")

    def test_find_line_matches_ignores_indentation(self) -> None:
        lines = scan_edit_lines("alpha\n\tbeta\n")
        matches = find_line_matches(lines, "beta")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].start_line, 2)
        self.assertEqual(matches[0].preview, "beta")

    def test_select_matches_errors(self) -> None:
        with self.assertRaises(ValueError) as caught:
            select_matches([], None, False, "old_text")
        self.assertIn("old_text not found", str(caught.exception))
        matches = find_content_matches("a\na\n", "a")
        self.assertEqual(len(select_matches(matches, None, True, "old_text")), 2)
        with self.assertRaises(ValueError):
            select_matches(matches, 5, False, "old_text")
        with self.assertRaises(ValueError):
            select_matches(matches, 1, True, "old_text")


class TestEncoding(unittest.TestCase):
    def test_utf16_with_bom_round_trip(self) -> None:
        for name, bom in (("utf-16le", b"\xff\xfe"), ("utf-16be", b"\xfe\xff")):
            encoded = encode_text("alpha\n中文\n", name, None)
            data = bom + encoded
            text, encoding_name, detected_bom = decode_bytes(data, "")
            self.assertEqual(text, "alpha\n中文\n")
            self.assertEqual(encoding_name, name)
            self.assertEqual(detected_bom, bom)

    def test_explicit_encoding_allows_nul(self) -> None:
        for name in ("utf-16le", "utf-16be", "gb18030", "big5", "shift_jis"):
            want = "alpha\x00beta"
            data = encode_text(want, name, None)
            text, _encoding, _bom = decode_bytes(data, name)
            self.assertEqual(text, want, name)

    def test_invalid_utf8_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_bytes(b"\x80\x81", "")
        with self.assertRaises(ValueError):
            decode_bytes(b"\xff", "utf-8")

    def test_utf8_bom(self) -> None:
        text, name, bom = decode_bytes(BOM_UTF8 + b"alpha\n", "")
        self.assertEqual((text, name, bom), ("alpha\n", "utf-8-bom", BOM_UTF8))
        self.assertEqual(encode_text("alpha\n", "utf-8-bom", BOM_UTF8), BOM_UTF8 + b"alpha\n")
        self.assertEqual(encode_text("alpha\n", "utf-8", BOM_UTF8), b"alpha\n")

    def test_unsupported_encoding(self) -> None:
        with self.assertRaises(ValueError):
            decode_bytes(b"x", "no-such-encoding")


class TestFileLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir(self)

    def path(self, name: str = "sample.txt") -> str:
        return os.path.join(self.dir, name)

    def test_read_file_keeps_encoding_and_binary_guard(self) -> None:
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"alpha\x00beta")
        with self.assertRaises(ValueError) as caught:
            read_file(path, "")
        self.assertIn("file appears to be binary", str(caught.exception))
        # 显式 utf-8 仍然按二进制拒绝（与 Go 的 isUTF8Encoding 判定一致）…
        with self.assertRaises(ValueError):
            read_file(path, "utf-8")
        # …换成非 UTF-8 编码时 NUL 是合法内容。
        with open(path, "wb") as handle:
            handle.write(encode_text("alpha\x00beta", "utf-16le", None))
        file = read_file(path, "utf-16le")
        self.assertEqual(file.text, "alpha\x00beta")

    def test_read_file_rejects_directory_and_oversize(self) -> None:
        from atri_x.utils.fileops import read_file_with_limit

        with self.assertRaises(ValueError) as caught:
            read_file(self.dir, "")
        self.assertIn("path is a directory", str(caught.exception))
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"x" * 16)
        with self.assertRaises(ValueError) as caught:
            read_file_with_limit(path, "", 4)
        self.assertIn("file too large", str(caught.exception))

    def test_read_or_create(self) -> None:
        path = self.path("new.txt")
        with self.assertRaises(FileNotFoundError):
            read_or_create_file(path, "", False, False)
        file, created = read_or_create_file(path, "", True, False)
        self.assertTrue(created)
        self.assertEqual(file.encoding, "utf-8")
        self.assertEqual(file.line_ending, "\n")
        file, created = read_or_create_file(path, "utf-8-bom", True, False)
        self.assertTrue(created)
        self.assertEqual(file.bom, BOM_UTF8)
        with self.assertRaises(FileNotFoundError):
            read_or_create_file(path, "", True, True)
        with self.assertRaises(ValueError):
            read_or_create_file(path, "no-such-encoding", True, False)

    def test_edit_file_revision_guard(self) -> None:
        path = self.path()
        original = b"alpha\n"
        with open(path, "wb") as handle:
            handle.write(original)
        revision = content_revision(original)
        result = edit_file(
            path, "", revision.upper(), False, False, 3,
            [Edit(operation="insert", line=2, new_text="beta")],
        )
        self.assertEqual(result.revision_before, revision)
        self.assertEqual(len(result.revision_after), 16)
        self.assertFalse(result.created)
        with self.assertRaises(FileChangedError) as caught:
            edit_file(
                path, "", revision, False, False, 3,
                [Edit(operation="insert", line=3, new_text="gamma")],
            )
        self.assertIn("file revision mismatch: current " + result.revision_after, str(caught.exception))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\nbeta\n")

    def test_edit_file_requires_revision_for_positional_edits(self) -> None:
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"alpha\n")
        for edit in (
            Edit(operation="replace", line=1, new_text="beta\n"),
            Edit(operation="insert", line=2, new_text="beta"),
            Edit(operation="delete", line=1),
            Edit(operation="overwrite", new_text="beta"),
        ):
            with self.assertRaises(ValueError) as caught:
                edit_file(path, "", "", False, False, 3, [edit])
            self.assertIn("expected_revision is required", str(caught.exception))

    def test_edit_inserts_into_empty_existing_file(self) -> None:
        path = self.path("empty.txt")
        with open(path, "wb"):
            pass
        result = edit_file(
            path, "", content_revision(b""), False, False, 3,
            [Edit(operation="insert", line=1, new_text="alpha")],
        )
        self.assertFalse(result.created)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha")

    def test_edit_creates_missing_file_without_revision(self) -> None:
        path = self.path("created.txt")
        result = edit_file(
            path, "", "", True, False, 3,
            [Edit(operation="insert", line=1, new_text="hello")],
        )
        self.assertTrue(result.created)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"hello")

    def test_edit_rejects_malformed_revision(self) -> None:
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"alpha\n")
        for revision in ("deadbeef", "zzzzzzzzzzzzzzzz"):
            with self.assertRaises(ValueError) as caught:
                edit_file(
                    path, "", revision, False, False, 3,
                    [Edit(operation="insert", line=2, new_text="beta")],
                )
            self.assertIn(
                "expected_revision must be 16 hexadecimal characters", str(caught.exception)
            )

    def test_edit_option_limits_and_omitted_diff(self) -> None:
        path = self.path()
        original = b"alpha\n"
        with open(path, "wb") as handle:
            handle.write(original)
        edits = [Edit(operation="replace_text", old_text="alpha", new_text="beta-long")]
        with self.assertRaises(ValueError) as caught:
            edit_file_with_options(
                path, "", "", False, False, 3, edits,
                EditFileOptions(max_input_bytes=3),
            )
        self.assertIn("file too large", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            edit_file_with_options(
                path, "", "", False, False, 3, edits,
                EditFileOptions(max_input_bytes=1024, max_output_bytes=5),
            )
        self.assertIn("edited file too large", str(caught.exception))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        result = edit_file_with_options(
            path, "", "", False, True, 3,
            [Edit(operation="replace_text", old_text="alpha", new_text="beta")],
            EditFileOptions(
                max_input_bytes=1024, max_output_bytes=1024, detailed_diff_max_bytes=5
            ),
        )
        self.assertIn("diff omitted: file exceeds detailed diff limit 5 bytes", result.diff)
        self.assertIn("old 6 bytes", result.diff)

    def test_edit_no_changes_rejected(self) -> None:
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"alpha\n")
        with self.assertRaises(ValueError) as caught:
            edit_file(
                path, "", content_revision(b"alpha\n"), False, False, 3,
                [Edit(operation="replace_text", old_text="alpha", new_text="alpha")],
            )
        self.assertIn("edit produced no changes", str(caught.exception))

    def test_edit_preserves_utf16_encoding_and_bom(self) -> None:
        path = self.path()
        bom = b"\xff\xfe"
        original = bom + encode_text("alpha\nbeta\n", "utf-16le", None)
        with open(path, "wb") as handle:
            handle.write(original)
        result = edit_file(
            path, "", content_revision(original), False, False, 0,
            [Edit(operation="replace_text", old_text="beta", new_text="gamma")],
        )
        self.assertEqual(result.encoding, "utf-16le")
        with open(path, "rb") as handle:
            got = handle.read()
        self.assertEqual(got, encode_text("alpha\ngamma\n", "utf-16le", bom))

    def test_edit_preserves_crlf_line_endings(self) -> None:
        path = self.path()
        original = b"alpha\r\nbeta\r\n"
        with open(path, "wb") as handle:
            handle.write(original)
        edit_file(
            path, "", content_revision(original), False, False, 3,
            [Edit(operation="replace_text", old_text="beta", new_text="gamma")],
        )
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\r\ngamma\r\n")

    def test_write_text_file_round_trip(self) -> None:
        path = self.path()
        with open(path, "wb") as handle:
            handle.write(b"alpha\n")
        file = read_file(path, "")
        written = write_text_file(path, file, "alpha\nbeta\n")
        self.assertEqual(written, b"alpha\nbeta\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\nbeta\n")


class TestDiff(unittest.TestCase):
    def test_unified_diff_renders_hunks(self) -> None:
        diff = unified_diff("sample.txt", ["alpha"], ["beta"], 3)
        self.assertEqual(
            diff,
            "--- sample.txt\n+++ sample.txt\n@@ -1,1 +1,1 @@\n-alpha\n+beta\n",
        )

    def test_unified_diff_with_context_and_equal_tail(self) -> None:
        diff = unified_diff(
            "sample.txt",
            ["a", "b", "c", "d", "e", "f", "g", "h"],
            ["a", "b", "c", "X", "e", "f", "g", "h"],
            1,
        )
        self.assertIn("@@ -3,3 +3,3 @@", diff)
        self.assertIn("-d", diff)
        self.assertIn("+X", diff)
        self.assertNotIn("-a", diff)

    def test_unified_diff_identical_is_header_only(self) -> None:
        diff = unified_diff("sample.txt", ["a"], ["a"], 3)
        self.assertEqual(diff, "--- sample.txt\n+++ sample.txt\n")

    def test_unified_diff_omits_oversized_output(self) -> None:
        old_line = "a" * 64 * 1024
        new_line = "b" * 64 * 1024
        diff = unified_diff("sample.txt", [old_line], [new_line], 3)
        self.assertIn("diff omitted: rendered diff exceeds", diff)
        self.assertNotIn(old_line, diff)

    def test_edit_diff_prefers_omission_when_too_large(self) -> None:
        from atri_x.utils.fileops import edit_diff

        diff = edit_diff("p.txt", "a", "b", 10, 10, 3, 5)
        self.assertIn("# diff omitted:", diff)


class TestRollback(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir(self)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_restore_last_edit_and_conflicts(self) -> None:
        path = self.path("a.txt")
        before = b"alpha\n"
        after = b"beta\n"
        with open(path, "wb") as handle:
            handle.write(after)
        store = RollbackStore()
        info, available = store.record(
            "s1", path, before, 0o600, True, content_revision(before), content_revision(after)
        )
        self.assertTrue(available)
        self.assertEqual(info.id, 1)
        self.assertFalse(info.created)
        self.assertEqual(info.path, path)
        self.assertEqual(info.bytes, len(before))
        self.assertEqual([item.id for item in store.list("s1")], [1])
        self.assertEqual(store.get("s1", 1)[0].path, path)

        with open(path, "wb") as handle:
            handle.write(b"external\n")
        with self.assertRaises(Exception) as caught:
            store.restore(None, "s1", 1)
        self.assertIs(caught.exception, ErrRollbackConflict)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"external\n")

        with open(path, "wb") as handle:
            handle.write(after)
        store.restore(None, "s1", 1)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        with self.assertRaises(Exception) as caught:
            store.restore(None, "s1", 1)
        self.assertIs(caught.exception, ErrRollbackNotFound)

    def test_restore_removes_created_file(self) -> None:
        path = self.path("new.txt")
        with open(path, "wb") as handle:
            handle.write(b"created\n")
        store = RollbackStore()
        info, _available = store.record(
            "s1", path, b"", 0, False, "", content_revision(b"created\n")
        )
        self.assertTrue(info.created)
        store.restore(None, "s1", info.id)
        self.assertFalse(os.path.exists(path))

    def test_created_file_changed_after_edit_is_kept(self) -> None:
        path = self.path("new.txt")
        after = b"created\n"
        with open(path, "wb") as handle:
            handle.write(after)
        store = RollbackStore()
        info, _available = store.record("s1", path, b"", 0, False, "", content_revision(after))
        with open(path, "wb") as handle:
            handle.write(b"owner edited\n")
        with self.assertRaises(Exception) as caught:
            store.restore(None, "s1", info.id)
        self.assertIs(caught.exception, ErrRollbackConflict)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"owner edited\n")
        with open(path, "wb") as handle:
            handle.write(after)
        store.restore(None, "s1", info.id)
        self.assertFalse(os.path.exists(path))

    def test_missing_created_file_reports_not_found(self) -> None:
        path = self.path("gone.txt")
        with open(path, "wb") as handle:
            handle.write(b"created\n")
        store = RollbackStore()
        info, _available = store.record(
            "s1", path, b"", 0, False, "", content_revision(b"created\n")
        )
        os.remove(path)
        with self.assertRaises(Exception) as caught:
            store.restore(None, "s1", info.id)
        self.assertIs(caught.exception, ErrRollbackNotFound)

    def test_scopes_sessions_and_replaces_older_records(self) -> None:
        first = self.path("a.txt")
        second = self.path("b.txt")
        store = RollbackStore()
        store.record("s1", first, b"v1\n", 0o644, True, "r1", "r2")
        store.record("s1", second, b"v2\n", 0o644, True, "r3", "r4")
        store.record("s2", first, b"v9\n", 0o644, True, "r5", "r6")
        store.record("s1", first, b"v3\n", 0o644, True, "r7", "r8")

        s1 = store.list("s1")
        self.assertEqual(len(s1), 2)
        self.assertEqual(s1[0].path, second)
        self.assertEqual(s1[1].path, first)
        self.assertFalse(store.get("s1", 1)[1])
        self.assertEqual(len(store.list("s2")), 1)

        store.forget("s1")
        self.assertEqual(store.list("s1"), [])
        self.assertEqual(len(store.list("s2")), 1)
        with self.assertRaises(Exception) as caught:
            store.restore(None, "s1", 3)
        self.assertIs(caught.exception, ErrRollbackNotFound)

    def test_rejects_blank_identity(self) -> None:
        store = RollbackStore()
        _info, available = store.record("  ", "/tmp/x", b"", 0, True, "a", "b")
        self.assertFalse(available)
        _info, available = store.record("s1", "  ", b"", 0, True, "a", "b")
        self.assertFalse(available)

    def test_evicts_oldest_records(self) -> None:
        store = RollbackStore(max_records=2, max_bytes=1024)
        for index in range(3):
            store.record(
                "s1",
                self.path(chr(ord("a") + index) + ".txt"),
                b"content\n",
                0o644,
                True,
                "a",
                "b",
            )
        listed = store.list("s1")
        self.assertEqual(len(listed), 2)
        self.assertFalse(store.get("s1", 1)[1])

    def test_evicts_on_byte_budget(self) -> None:
        store = RollbackStore(max_records=100, max_bytes=8)
        _info, available = store.record(
            "s1", self.path("big.txt"), b"1234567890", 0o644, True, "a", "b"
        )
        # 单条记录本身就超预算：它会被自己挤掉。
        self.assertFalse(available)
        self.assertEqual(store.list("s1"), [])

    def test_restore_honours_cancelled_token(self) -> None:
        from atri_x.safecontext import with_cancel

        store = RollbackStore()
        ctx = with_cancel(None)
        ctx.cancel("test")
        with self.assertRaises(Exception):
            store.restore(ctx, "s1", 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
