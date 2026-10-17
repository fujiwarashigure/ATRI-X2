"""日志反向分块查询测试。

覆盖：文本键值解析（Go ``parseTextAttrs`` 口径）、反向分块读取（多块、跨块行、
无尾换行、超长行）、损坏行跳过与计数、``--raw`` 样本语义、级别/字段/时间过滤、
按天数选文件。
"""

from __future__ import annotations

import itertools
import os
import shutil
import unittest
from asyncio import CancelledError
from datetime import datetime
from unittest import mock

from atri_x.logging.reader import (
    DAMAGED_SAMPLE_LIMIT,
    DEFAULT_LOG_LIMIT,
    LogQuery,
    Reader,
    iter_lines_reversed,
    log_paths,
    match_log_entry,
    normalize_log_query,
    parse_log_line,
    parse_text_attrs,
    parse_text_value,
    unquote_go,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMP_ROOT = os.path.join(_REPO_ROOT, "tmp_atrix_test")
_COUNTER = itertools.count()

TODAY = datetime(2026, 6, 3, 15, 0, 0)


def make_temp_dir() -> str:
    """见 tests/test_logging.py 的同名 helper：避开沙箱里不可写的系统临时目录。"""
    path = os.path.join(_TEMP_ROOT, f"reader_{os.getpid()}_{next(_COUNTER)}")
    os.makedirs(path, exist_ok=True)
    return path


def remove_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(_TEMP_ROOT)
    except OSError:
        pass


def write_log(dir: str, prefix: str, day: str, content: str) -> str:
    path = os.path.join(dir, f"{prefix}-{day}.log")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


class ReaderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def reader(self, **kwargs: object) -> Reader:
        return Reader(dir=self.tmp, today=TODAY, **kwargs)  # type: ignore[arg-type]


class TestParseTextAttrs(unittest.TestCase):
    def test_parses_go_text_handler_line(self) -> None:
        line = (
            'time="2026-06-03 15:00:00" level=INFO msg="audit event"'
            " event=tool_call risk=high tool=shell error=\"bad thing\""
        )
        fields = parse_text_attrs(line)
        self.assertEqual("2026-06-03 15:00:00", fields["time"])
        self.assertEqual("INFO", fields["level"])
        self.assertEqual("audit event", fields["msg"])
        self.assertEqual("tool_call", fields["event"])
        self.assertEqual("bad thing", fields["error"])

    def test_unescapes_quoted_json(self) -> None:
        fields = parse_text_attrs(r'msg="tool call" arguments="{\"path\":\"a.txt\"}"')
        self.assertEqual('{"path":"a.txt"}', fields["arguments"])

    def test_unescapes_tags_array(self) -> None:
        fields = parse_text_attrs(r'msg=x tags="[\"windows\",\"onedrive\"]" raw_text=watchdog')
        self.assertEqual('["windows","onedrive"]', fields["tags"])
        self.assertEqual("watchdog", fields["raw_text"])

    def test_line_without_pairs_yields_nothing(self) -> None:
        # 注意：Go 的 parseTextAttrs 只要能找到 k=v 就会收下（哪怕这行整体是损坏行）；
        # "损坏"是 parseLogLine 按 time/level/msg 判定后**丢弃** fields，而不是这里解析不出。
        self.assertEqual({"key": "value"}, parse_text_attrs("this is a damaged line not key=value at all"))
        self.assertEqual({}, parse_text_attrs('{"time":"2026-06-03T15:00:00Z"}'))
        self.assertEqual({}, parse_text_attrs("=x"))

    def test_damaged_line_discards_parsed_fields(self) -> None:
        entry, damaged = parse_log_line("this is a damaged line not key=value at all")
        self.assertTrue(damaged)
        self.assertEqual({}, entry.fields)

    def test_value_without_quotes_stops_at_space(self) -> None:
        fields = parse_text_attrs("a=1 b=2")
        self.assertEqual({"a": "1", "b": "2"}, fields)

    def test_parse_text_value_handles_unterminated_quote(self) -> None:
        # 被截断的引号：Go 返回引号之后的余下内容，不抛异常。
        value, _ = parse_text_value('"unterminated', 0)
        self.assertEqual("unterminated", value)

    def test_invalid_escape_falls_back_to_raw_text(self) -> None:
        fields = parse_text_attrs(r'a="bad\qescape"')
        self.assertEqual(r"bad\qescape", fields["a"])

    def test_unquote_go_octal_and_unicode(self) -> None:
        self.assertEqual("A", unquote_go(r'"\101"'))
        self.assertEqual("中", unquote_go(r'"\u4e2d"'))
        self.assertEqual("中", unquote_go(r'"\U00004e2d"'))
        self.assertEqual("\x07", unquote_go(r'"\x07"'))
        with self.assertRaises(ValueError):
            unquote_go("not quoted")


class TestParseLogLine(unittest.TestCase):
    def test_valid_line(self) -> None:
        entry, damaged = parse_log_line('time="2026-06-03 15:00:00" level=INFO msg="audit event" tool=shell')
        self.assertFalse(damaged)
        self.assertEqual("INFO", entry.level)
        self.assertEqual("audit event", entry.message)
        self.assertIsNotNone(entry.time)
        time = entry.time
        assert time is not None  # 供类型检查；上一行已断言
        # 本地墙钟解析：日期时间分量与文件里写的一致。
        self.assertEqual(
            (2026, 6, 3, 15, 0, 0),
            (time.year, time.month, time.day, time.hour, time.minute, time.second),
        )
        self.assertEqual("shell", entry.fields["tool"])
        self.assertIn("tool=shell", entry.raw)

    def test_damaged_lines(self) -> None:
        for raw in (
            "this is a damaged line not key=value at all",
            '{"time":"2026-06-03T15:00:00Z"}',
            "truncated tail without any field",
        ):
            with self.subTest(raw=raw):
                entry, damaged = parse_log_line(raw)
                self.assertTrue(damaged)
                self.assertEqual({}, entry.fields)
                self.assertEqual(raw, entry.raw)

    def test_line_with_only_level_or_msg_is_not_damaged(self) -> None:
        self.assertFalse(parse_log_line("level=INFO only")[1])
        self.assertFalse(parse_log_line("msg=hello")[1])

    def test_unparseable_time_still_counts_as_record(self) -> None:
        # Go 只在 time/level/msg 三者都空时才判损坏；time 存在但格式不对不算损坏。
        entry, damaged = parse_log_line("time=not-a-time level=INFO msg=x")
        self.assertFalse(damaged)
        self.assertIsNone(entry.time)

    def test_blank_line_parses_as_damaged_but_query_skips_it(self) -> None:
        # parse_log_line 本身把空白行判成"解析不出 time/level/msg"；Reader 会先跳过空白行，
        # 所以空白行不计入 damaged_count（Go 的 readLogFile 同样先 TrimSpace 再判）。
        _, damaged = parse_log_line("   ")
        self.assertTrue(damaged)


class _RecordingFile:
    """记录 ``read`` 请求大小的文件代理，用来证明是分块读而不是整文件读。"""

    def __init__(self, handle: object, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def seek(self, *args: int) -> int:
        return self._handle.seek(*args)  # type: ignore[attr-defined]

    def tell(self) -> int:
        return self._handle.tell()  # type: ignore[attr-defined]

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        return self._handle.read(size)  # type: ignore[attr-defined]

    def __enter__(self) -> "_RecordingFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._handle.close()  # type: ignore[attr-defined]


class TestIterLinesReversed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()

    def tearDown(self) -> None:
        remove_temp_dir(self.tmp)

    def test_reads_lines_backwards_across_blocks(self) -> None:
        lines = [f"line-{index:03d}" for index in range(200)]
        path = write_log(self.tmp, "atri-x", "2026-06-03", "\n".join(lines) + "\n")
        for block_size in (1, 2, 7, 64, 4096):
            with self.subTest(block_size=block_size):
                self.assertEqual(list(reversed(lines)), list(iter_lines_reversed(path, block_size)))

    def test_handles_missing_trailing_newline(self) -> None:
        path = write_log(self.tmp, "atri-x", "2026-06-03", "first\nsecond")
        self.assertEqual(["second", "first"], list(iter_lines_reversed(path, 4)))

    def test_empty_file_yields_nothing(self) -> None:
        path = write_log(self.tmp, "atri-x", "2026-06-03", "")
        self.assertEqual([], list(iter_lines_reversed(path, 8)))

    def test_blank_lines_are_preserved_for_caller(self) -> None:
        path = write_log(self.tmp, "atri-x", "2026-06-03", "a\n\nb\n")
        self.assertEqual(["b", "", "a"], list(iter_lines_reversed(path, 2)))

    def test_uses_bounded_blocks(self) -> None:
        lines = [f"payload-{index:04d}" for index in range(100)]
        path = write_log(self.tmp, "atri-x", "2026-06-03", "\n".join(lines) + "\n")
        sizes: list[int] = []
        real_open = open

        def recording_open(*args: object, **kwargs: object) -> _RecordingFile:
            return _RecordingFile(real_open(*args, **kwargs), sizes)  # type: ignore[arg-type]

        with mock.patch("builtins.open", recording_open):
            read_back = list(iter_lines_reversed(path, 16))
        self.assertEqual(list(reversed(lines)), read_back)
        # 每一次 read 都不超过块大小：真的是从尾部往前分块读，而不是整文件读进内存。
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 16)
        self.assertGreater(len(sizes), 1)

    def test_over_long_line_raises(self) -> None:
        path = write_log(self.tmp, "atri-x", "2026-06-03", "x" * 200 + "\n")
        with self.assertRaises(ValueError):
            list(iter_lines_reversed(path, 16, max_line_bytes=32))


class TestLogPaths(unittest.TestCase):
    def test_lists_today_first(self) -> None:
        paths = log_paths("logs", "audit", 3, TODAY)
        self.assertEqual(
            [
                os.path.join("logs", "audit-2026-06-03.log"),
                os.path.join("logs", "audit-2026-06-02.log"),
                os.path.join("logs", "audit-2026-06-01.log"),
            ],
            paths,
        )


class TestNormalizeLogQuery(unittest.TestCase):
    def test_defaults(self) -> None:
        query = normalize_log_query(LogQuery(prefix=" audit ", limit=0, days=0, min_level=" info "))
        self.assertEqual("audit", query.prefix)
        self.assertEqual(DEFAULT_LOG_LIMIT, query.limit)
        self.assertEqual(1, query.days)
        self.assertEqual("INFO", query.min_level)
        self.assertEqual({}, query.fields)
        self.assertEqual({}, query.field_contains)

    def test_does_not_mutate_caller(self) -> None:
        original = LogQuery(prefix=" audit ", limit=0, min_level="info")
        normalize_log_query(original)
        self.assertEqual(" audit ", original.prefix)
        self.assertEqual(0, original.limit)
        self.assertEqual("info", original.min_level)

    def test_nullable_maps_become_empty(self) -> None:
        query = normalize_log_query(LogQuery(prefix="x", fields=None, field_contains=None, field_exists=None))  # type: ignore[arg-type]
        self.assertEqual({}, query.fields)
        self.assertEqual([], query.field_exists)


class TestQuery(ReaderTestCase):
    def test_filters_by_fields_and_min_level(self) -> None:
        write_log(
            self.tmp,
            "audit",
            "2026-06-03",
            'time="2026-06-03 15:00:00" level=INFO msg="audit event" event=tool_call risk=high tool=shell error="bad thing"\n'
            'time="2026-06-03 15:01:00" level=INFO msg="audit event" event=llm_usage model=foo total_tokens=42\n',
        )
        result = self.reader().query(LogQuery(prefix="audit", fields={"event": "tool_call", "risk": "high"}))
        self.assertEqual(1, len(result.entries))
        entry = result.entries[0]
        self.assertEqual("audit event", entry.message)
        self.assertEqual("shell", entry.fields["tool"])
        self.assertEqual("bad thing", entry.fields["error"])
        self.assertEqual(0, result.damaged_count)
        self.assertEqual([], result.damaged_lines)

    def test_returns_newest_first_and_applies_limit(self) -> None:
        write_log(
            self.tmp,
            "atri-x",
            "2026-06-03",
            'time="2026-06-03 15:00:00" level=DEBUG msg=debug\n'
            'time="2026-06-03 15:01:00" level=INFO msg=info\n'
            'time="2026-06-03 15:02:00" level=ERROR msg=error\n',
        )
        result = self.reader().query(LogQuery(prefix="atri-x", min_level="info", limit=1))
        self.assertEqual(1, len(result.entries))
        self.assertEqual("error", result.entries[0].message)
        result = self.reader().query(LogQuery(prefix="atri-x", min_level="debug", limit=2))
        self.assertEqual(["error", "info"], [entry.message for entry in result.entries])

    def test_contains_matches_structured_fields_and_raw(self) -> None:
        write_log(
            self.tmp,
            "atri-x",
            "2026-06-03",
            'time="2026-06-03 15:00:00" level=INFO msg="user input" event=user_message text="hello world"\n'
            'time="2026-06-03 15:01:00" level=INFO msg="tool call" event=tool_call arguments="{\\"path\\":\\"a.txt\\"}" result="file content"\n'
            "time=\"2026-06-03 15:02:00\" level=INFO msg=other custom_field=needle\n",
        )
        result = self.reader().query(LogQuery(prefix="atri-x", contains="file content"))
        self.assertEqual(1, len(result.entries))
        self.assertEqual("tool_call", result.entries[0].fields["event"])
        result = self.reader().query(LogQuery(prefix="atri-x", contains="needle"))
        self.assertEqual(1, len(result.entries))
        self.assertEqual("other", result.entries[0].message)

    def test_field_contains_and_exists(self) -> None:
        write_log(
            self.tmp,
            "atrinis",
            "2026-06-03",
            'time="2026-06-03 15:00:00" level=INFO msg="atrinis event accepted" tags="[\\"windows\\",\\"onedrive\\"]" raw_text=watchdog\n'
            'time="2026-06-03 15:01:00" level=INFO msg="atrinis event accepted" tags="[\\"watchdog\\"]" raw_text=onedrive\n',
        )
        result = self.reader().query(
            LogQuery(prefix="atrinis", field_contains={"tags": ['"onedrive"']})
        )
        self.assertEqual(1, len(result.entries))
        self.assertEqual('["windows","onedrive"]', result.entries[0].fields["tags"])
        result = self.reader().query(
            LogQuery(prefix="atrinis", field_contains={"tags": ['"windows"', '"onedrive"']})
        )
        self.assertEqual(1, len(result.entries))
        result = self.reader().query(LogQuery(prefix="atrinis", field_exists=["raw_text"]))
        self.assertEqual(2, len(result.entries))

    def test_msg_contains_and_time_window(self) -> None:
        write_log(
            self.tmp,
            "atri-x",
            "2026-06-03",
            'time="2026-06-03 15:00:00" level=INFO msg="alpha"\n'
            'time="2026-06-03 16:00:00" level=INFO msg="beta"\n',
        )
        result = self.reader().query(LogQuery(prefix="atri-x", msg_contains="BET"))
        self.assertEqual(["beta"], [entry.message for entry in result.entries])
        result = self.reader().query(
            LogQuery(
                prefix="atri-x",
                since=datetime(2026, 6, 3, 15, 30, 0),
                until=datetime(2026, 6, 3, 16, 30, 0),
            )
        )
        self.assertEqual(["beta"], [entry.message for entry in result.entries])

    def test_days_selects_older_files_and_order(self) -> None:
        write_log(self.tmp, "atri-x", "2026-06-03", 'time="2026-06-03 15:00:00" level=INFO msg=today\n')
        write_log(self.tmp, "atri-x", "2026-06-02", 'time="2026-06-02 15:00:00" level=INFO msg=yesterday\n')
        result = self.reader().query(LogQuery(prefix="atri-x", days=1))
        self.assertEqual(["today"], [entry.message for entry in result.entries])
        result = self.reader().query(LogQuery(prefix="atri-x", days=2))
        self.assertEqual(["today", "yesterday"], [entry.message for entry in result.entries])
        result = self.reader().query(LogQuery(prefix="atri-x", days=2, limit=1))
        self.assertEqual(["today"], [entry.message for entry in result.entries])

    def test_missing_file_is_not_an_error(self) -> None:
        result = self.reader().query(LogQuery(prefix="audit"))
        self.assertEqual([], result.entries)
        self.assertEqual(0, result.damaged_count)

    def test_missing_directory_is_an_error(self) -> None:
        reader = Reader(dir="", today=TODAY)
        with self.assertRaises(ValueError):
            reader.query(LogQuery(prefix="audit"))

    def test_accepts_single_query_argument(self) -> None:
        write_log(self.tmp, "audit", "2026-06-03", 'time="2026-06-03 15:00:00" level=INFO msg=x\n')
        result = self.reader().query(LogQuery(prefix="audit"))  # ctx 省略
        self.assertEqual(1, len(result.entries))

    def test_cancellation_raises(self) -> None:
        class CancelToken:
            cancelled = True

        write_log(self.tmp, "audit", "2026-06-03", 'time="2026-06-03 15:00:00" level=INFO msg=x\n')
        with self.assertRaises(CancelledError):
            self.reader().query(CancelToken(), LogQuery(prefix="audit"))

    def test_match_log_entry_directly(self) -> None:
        entry, _ = parse_log_line('time="2026-06-03 15:00:00" level=WARN msg="risk" result=rejected')
        self.assertTrue(match_log_entry(entry, LogQuery(prefix="x", fields={"result": "REJECTED"})))
        self.assertTrue(match_log_entry(entry, LogQuery(prefix="x", min_level="warn")))
        self.assertFalse(match_log_entry(entry, LogQuery(prefix="x", min_level="error")))
        self.assertFalse(match_log_entry(entry, LogQuery(prefix="x", fields={"result": "skipped"})))


class TestDamagedLines(ReaderTestCase):
    """损坏行：跳过 + 计数 + 不占 limit + 样本只在 --raw 里（对应 Go reader_test.go）。"""

    CONTENT = (
        "this is a damaged line not key=value at all\n"
        '{"time":"2026-06-03T15:00:00Z"}\n'
        "\n"
        'time="2026-06-03 15:00:00" level=INFO msg="audit event" event=tool_call tool=shell\n'
        'time="2026-06-03 15:01:00" level=INFO msg="audit event" event=tool_call tool=web_search\n'
        "truncated tail without any field\n"
    )

    def test_skips_and_counts_without_raw(self) -> None:
        write_log(self.tmp, "audit", "2026-06-03", self.CONTENT)
        result = self.reader().query(LogQuery(prefix="audit", limit=2, min_level="debug"))
        # 空白行不算损坏；同一文件里的损坏行全部计数（与 Go 的整文件扫描一致）。
        self.assertEqual(3, result.damaged_count)
        self.assertEqual(["web_search", "shell"], [entry.fields["tool"] for entry in result.entries])
        for entry in result.entries:
            self.assertNotEqual("", entry.level)
            self.assertNotEqual("", entry.message)
        self.assertEqual([], result.damaged_lines)

    def test_collects_samples_only_for_raw(self) -> None:
        write_log(self.tmp, "audit", "2026-06-03", self.CONTENT)
        result = self.reader().query(LogQuery(prefix="audit", raw=True, min_level="debug"))
        self.assertEqual(3, result.damaged_count)
        self.assertEqual(3, len(result.damaged_lines))
        # 样本按文件正序收集（与 Go 的顺序扫描口径一致）。
        self.assertIn("damaged line", result.damaged_lines[0])

    def test_caps_damaged_samples(self) -> None:
        content = "damaged line without fields\n" * (DAMAGED_SAMPLE_LIMIT + 5)
        content += 'time="2026-06-03 15:00:00" level=INFO msg="audit event"\n'
        write_log(self.tmp, "audit", "2026-06-03", content)
        result = self.reader().query(LogQuery(prefix="audit", raw=True))
        self.assertEqual(DAMAGED_SAMPLE_LIMIT + 5, result.damaged_count)
        self.assertEqual(DAMAGED_SAMPLE_LIMIT, len(result.damaged_lines))
        self.assertEqual(1, len(result.entries))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
