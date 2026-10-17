"""``atri_x.net.tomlw`` 的单元测试：写入结果必须能被 ``tomllib`` 无损读回。"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import math
import os
import shutil
import tomllib
import unittest
from collections.abc import Iterator

from atri_x.net.tomlw import TomlWriteError, dump, dumps, dumps_table

# 本机 zoneinfo 没有 tzdata（``ZoneInfo("UTC")`` 直接抛 ZoneInfoNotFoundError），
# 因此测试只用固定偏移时区。
CST = dt.timezone(dt.timedelta(hours=8))
# 临时目录必须落在工作区内（%TEMP% 在沙箱外不可写）。
_TMP_BASE = os.path.dirname(os.path.abspath(__file__))
_TMP_SEQ = itertools.count()


@contextlib.contextmanager
def _tempdir() -> Iterator[str]:
    """工作区内的临时目录。

    刻意不用 ``tempfile.mkdtemp`` / ``TemporaryDirectory``：它们以 0o700 建目录，
    在本机沙箱下该 ACL 会拒绝后续任何写入；``os.makedirs`` 的默认权限正常。
    """
    path = os.path.join(_TMP_BASE, f"tmp-tomlw-{os.getpid()}-{next(_TMP_SEQ)}")
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _roundtrip(obj: object) -> object:
    return tomllib.loads(dumps(obj))  # type: ignore[arg-type]


class TestScalars(unittest.TestCase):
    def test_strings(self) -> None:
        cases = ["", "plain", "带中文与 emoji 🎉", 'quote " inside', "back\\slash", "tab\there"]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(_roundtrip({"k": value}), {"k": value})

    def test_multiline_string(self) -> None:
        value = "line1\nline2\n"
        text = dumps({"k": value})
        self.assertIn('"""', text)
        self.assertEqual(_roundtrip({"k": value}), {"k": value})

    def test_multiline_string_edge_cases(self) -> None:
        for value in ['"""tricky', 'ends with quote"', 'ends with backslash\\', '\nleading']:
            with self.subTest(value=value):
                self.assertEqual(_roundtrip({"k": value}), {"k": value})

    def test_control_characters(self) -> None:
        value = "bell\x07 null\u0000 del\x7f"
        self.assertEqual(_roundtrip({"k": value}), {"k": value})

    def test_integers(self) -> None:
        obj = {"zero": 0, "neg": -17, "big": 2**40, "huge": 2**70}
        self.assertEqual(_roundtrip(obj), obj)
        self.assertNotIn("_", dumps(obj))

    def test_floats(self) -> None:
        obj = {"a": 1.0, "b": -0.5, "c": 1e300, "d": 1e-5, "e": 3.141592653589793}
        result = _roundtrip(obj)
        self.assertEqual(result, obj)
        self.assertIsInstance(result["a"], float)  # type: ignore[index]

    def test_float_specials(self) -> None:
        text = dumps({"inf": float("inf"), "ninf": float("-inf"), "nan": float("nan")})
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["inf"], float("inf"))
        self.assertEqual(parsed["ninf"], float("-inf"))
        self.assertTrue(math.isnan(parsed["nan"]))

    def test_bools_not_confused_with_ints(self) -> None:
        self.assertEqual(dumps({"t": True, "f": False}), "f = false\nt = true\n")
        self.assertTrue(all(isinstance(v, bool) for v in _roundtrip({"t": True, "f": False}).values()))

    def test_datetime_aware_and_naive(self) -> None:
        obj = {
            "utc": dt.datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt.timezone.utc),
            "offset": dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=CST),
            "naive": dt.datetime(2026, 5, 6, 7, 8, 9),
            "date": dt.date(2024, 2, 29),
            "time": dt.time(23, 59, 59, 500000),
        }
        result = _roundtrip(obj)
        self.assertEqual(result, obj)
        self.assertIsNone(result["naive"].tzinfo)  # type: ignore[union-attr]
        self.assertEqual(result["utc"].utcoffset(), dt.timedelta(0))  # type: ignore[union-attr]


class TestStructures(unittest.TestCase):
    def test_nested_roundtrip(self) -> None:
        obj = {
            "agent": {"name": "ATRI", "retries": 3, "limits": {"max_tokens": 8192}},
            "flags": {"enabled": True, "disabled": False},
            "empty_dict": {},
            "empty_list": [],
            "mixed": [1, "two", 3.0, True],
            "nested": [[1, 2], [3, [4, 5]]],
            "inline": [{"a": 1}, "scalar", {"b": {"c": 2}}],
            "platforms": [
                {"name": "qq", "enabled": True},
                {"name": "telegram", "enabled": False},
            ],
            "when": dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc),
        }
        self.assertEqual(_roundtrip(obj), obj)

    def test_array_of_tables_uses_double_brackets(self) -> None:
        obj = {"server": [{"host": "a"}, {"host": "b"}]}
        text = dumps(obj)
        self.assertEqual(text.count("[[server]]"), 2)
        self.assertEqual(_roundtrip(obj), obj)

    def test_nested_array_of_tables(self) -> None:
        obj = {"a": {"b": {"rows": [{"id": 1}, {"id": 2}]}}}
        text = dumps(obj)
        self.assertIn("[[a.b.rows]]", text)
        self.assertEqual(_roundtrip(obj), obj)

    def test_dotted_keys_for_deep_tables(self) -> None:
        obj = {"a": {"b": {"c": 1}}}
        text = dumps(obj)
        self.assertIn("b.c = 1", text)
        self.assertEqual(_roundtrip(obj), obj)

    def test_dotted_keys_from_dumps_table(self) -> None:
        text = dumps_table({"a": {"b": {"c": 1}}, "d": 2})
        self.assertIn("a.b.c = 1", text)
        self.assertEqual(tomllib.loads(text), {"a": {"b": {"c": 1}}, "d": 2})

    def test_table_header_with_inline_subtable(self) -> None:
        obj = {"a": {"b": {"rows": [{"x": 1}]}, "c": 2}}
        text = dumps(obj)
        self.assertIn("[a]", text)
        self.assertIn("c = 2", text)
        self.assertIn("[[a.b.rows]]", text)
        self.assertEqual(_roundtrip(obj), obj)

    def test_scalars_precede_subtable_headers(self) -> None:
        obj = {"a": {"zzz": 1, "sub": {"rows": [{"x": 1}]}}}
        text = dumps(obj)
        self.assertLess(text.index("zzz = 1"), text.index("[a.sub]"))

    def test_quoted_keys(self) -> None:
        obj = {"a b": 1, "多字节": 2, "with.dot": 3, 'quo"te': 4, "normal-key_1": 5}
        self.assertEqual(_roundtrip(obj), obj)

    def test_quoted_dotted_key(self) -> None:
        obj = {"a b": {"c d": {"e": 1}}}
        self.assertEqual(_roundtrip(obj), obj)

    def test_heterogeneous_and_long_arrays(self) -> None:
        obj = {
            "mixed": [1, "a", 2.5, True, dt.date(2026, 1, 1)],
            "long": list(range(80)),
            "nested_long": [list(range(20)), list(range(20))],
        }
        self.assertEqual(_roundtrip(obj), obj)

    def test_empty_root(self) -> None:
        self.assertEqual(tomllib.loads(dumps({})), {})


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output(self) -> None:
        obj = {"b": 1, "a": {"z": 1, "y": [1, 2]}, "c": [{"k": 1}]}
        first = dumps(obj)
        self.assertEqual(first, dumps(obj))
        self.assertEqual(first, dumps(dict(reversed(list(obj.items())))))

    def test_insertion_order_preserved_when_unsorted(self) -> None:
        text = dumps({"b": 1, "a": 2}, sort_keys=False)
        self.assertEqual(text, "b = 1\na = 2\n")
        self.assertEqual(dumps({"a": 2, "b": 1}, sort_keys=False), "a = 2\nb = 1\n")

    def test_reparse_of_dumped_text_is_stable(self) -> None:
        obj = {"a": {"b": {"c": [1, 2, {"d": 3}]}}, "e": [{"f": 1}]}
        text = dumps(obj)
        self.assertEqual(dumps(tomllib.loads(text)), text)


class TestNonePolicy(unittest.TestCase):
    def test_none_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            dumps({"a": {"b": None}})
        with self.assertRaises(TomlWriteError) as ctx:
            dumps({"a": {"b": None}})
        self.assertIn("a.b", str(ctx.exception))

    def test_none_in_array_raises(self) -> None:
        with self.assertRaises(TomlWriteError):
            dumps({"a": [1, None]})

    def test_skip_none_policy(self) -> None:
        self.assertEqual(dumps({"a": None, "b": 1}, skip_none=True), "b = 1\n")
        self.assertEqual(tomllib.loads(dumps({"a": [1, None, 2]}, skip_none=True)), {"a": [1, 2]})
        self.assertEqual(tomllib.loads(dumps({"t": {"a": None, "b": 1}}, skip_none=True)),
                         {"t": {"b": 1}})

    def test_unsupported_type(self) -> None:
        with self.assertRaises(TomlWriteError):
            dumps({"a": object()})
        with self.assertRaises(TomlWriteError):
            dumps({"a": {1: "x"}})
        with self.assertRaises(TomlWriteError):
            dumps([1, 2])  # type: ignore[arg-type]


class TestAtomicDump(unittest.TestCase):
    def test_dump_creates_directories_and_file(self) -> None:
        with _tempdir() as base:
            target = os.path.join(base, "nested", "state.toml")
            dump({"a": {"b": 1}}, target)
            with open(target, "rb") as handle:
                raw = handle.read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", raw)
            self.assertEqual(tomllib.loads(raw.decode("utf-8")), {"a": {"b": 1}})

    def test_dump_is_atomic_and_leaves_no_temp_files(self) -> None:
        with _tempdir() as base:
            target = os.path.join(base, "state.toml")
            dump({"n": 1}, target)
            dump({"n": 2}, target)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(tomllib.loads(handle.read()), {"n": 2})
            self.assertEqual(os.listdir(base), ["state.toml"])

    def test_dump_failure_keeps_previous_content(self) -> None:
        with _tempdir() as base:
            target = os.path.join(base, "state.toml")
            dump({"ok": 1}, target)
            with self.assertRaises(TomlWriteError):
                dump({"ok": None}, target)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(tomllib.loads(handle.read()), {"ok": 1})
            self.assertEqual(os.listdir(base), ["state.toml"])

    def test_dump_accepts_pathlike(self) -> None:
        import pathlib

        with _tempdir() as base:
            target = pathlib.Path(base) / "p.toml"
            dump({"a": 1}, target)
            self.assertEqual(tomllib.loads(target.read_text(encoding="utf-8")), {"a": 1})


class TestAtriXStateShape(unittest.TestCase):
    """真实形态：state/config 结构（嵌套 dict/list/标量）必须能读回等值。"""

    FIXTURE = {
        "version": 1,
        "updated_at": dt.datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt.timezone.utc),
        "agent": {
            "name": "ATRI",
            "system_prompt": "你是 ATRI。\n保持简洁。",
            "limits": {"max_tokens": 8192, "temperature": 0.7, "stream": True},
            "tools": {"enabled": ["web_search", "read_file"], "blocked": []},
        },
        "platform": [
            {"name": "qq-onebot", "enabled": True, "ws_url": "ws://127.0.0.1:3001", "at_only": False},
            {"name": "telegram", "enabled": False, "token": "x", "admins": [1, 2, 3]},
        ],
        "cron": [{"name": "daily", "expr": "0 3 * * *", "meta": {"title": "日报"}}],
        "tags": ["a", "b", "c"],
        "empty": {},
        "single": [{"only": 1}],
    }

    def test_roundtrip(self) -> None:
        self.assertEqual(_roundtrip(self.FIXTURE), self.FIXTURE)

    def test_text_is_parseable_and_stable(self) -> None:
        text = dumps(self.FIXTURE)
        self.assertEqual(dumps(tomllib.loads(text)), text)
        self.assertIn("[[platform]]", text)
        self.assertIn("[agent]", text)
        self.assertIn("limits.max_tokens = 8192", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
