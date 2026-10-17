"""日志查询：反向分块读取 + 文本键值过滤。对应 Go：``internal/logging/reader.go``。

``/log``、``/audit``、``/atriwisp``、``/usage`` 与维护报告共用本模块。读者契约：

* 日志文件是 :mod:`atri_x.logging.logging` 写出的文本行
  （``time="..." level=INFO msg=... key=value``），本模块按同一套键值语法解析。
* **反向分块**：从文件尾部往前按块（默认 64 KiB）读，命中 ``limit`` 条后不再向更旧的
  文件走。比 Go 的实现（``bufio.Scanner`` 整文件读进内存再倒序过滤）内存占用有界；
  可观测行为保持一致——同一文件里的损坏行仍然全部计数（Go 也是整文件扫描完才倒序匹配）。
* **损坏行**：解析不出 ``time``/``level``/``msg`` 的行被跳过并计数，既不占用 ``limit``
  配额，也不会以零字段记录的形式混进结果；原始样本只在 ``--raw`` 查询里收集
  （每个文件最多 10 条、总计最多 10 条，且不参与任何筛选匹配）。
* 小数秒/时区偏移与 ``time.RFC3339Nano`` 无关：日志行的 ``time`` 是**本地墙钟**
  ``YYYY-MM-DD HH:MM:SS``，按本地时区回填（与 Go ``time.ParseInLocation(..., time.Local)`` 一致）。
"""

from __future__ import annotations

import os
from asyncio import CancelledError
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from atri_x.logging.logging import level_rank

#: Go ``logging.DefaultLogLimit``。
DEFAULT_LOG_LIMIT = 5

#: Go ``reader.normalizeLogQuery`` 的默认天数。
DEFAULT_LOG_DAYS = 1

#: Go ``reader.maxLogLineBytes``：单行上限，超过即报错（与 bufio.Scanner 的 ErrTooLong 对齐）。
MAX_LOG_LINE_BYTES = 16 * 1024 * 1024

#: 反向读取的块大小（Go 无此参数，因为它是整文件读）。
DEFAULT_BLOCK_SIZE = 64 * 1024

#: Go ``reader.damagedSampleLimit``：损坏行样本条数上限。
DAMAGED_SAMPLE_LIMIT = 10

#: ``--contains`` 会优先匹配的结构化字段（Go ``logEntryContains``）。
CONTAINS_FIELDS = (
    "text",
    "raw_text",
    "arguments",
    "result",
    "latest_message_json",
    "first_system_message_json",
)


@dataclass
class LogQuery:
    """一次日志查询。对应 Go ``LogQuery``。"""

    prefix: str = ""
    limit: int = 0
    days: int = 0
    min_level: str = ""
    since: datetime | None = None
    until: datetime | None = None
    fields: dict[str, str] = field(default_factory=dict)
    field_exists: list[str] = field(default_factory=list)
    field_contains: dict[str, list[str]] = field(default_factory=dict)
    contains: str = ""
    msg_contains: str = ""
    raw: bool = False


@dataclass
class LogEntry:
    """一条解析出来的日志。对应 Go ``LogEntry``。

    ``time`` 为 ``None`` 表示"没解析出时间"（Go 的 ``time.Time`` 零值）；
    时间缺失时不参与 ``since``/``until`` 过滤（与 Go 一致）。
    """

    time: datetime | None = None
    level: str = ""
    message: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""


@dataclass
class QueryResult:
    """一次日志扫描的结果。对应 Go ``QueryResult``。

    ``damaged_count`` 是本次扫描遇到的损坏行数；``damaged_lines`` 是损坏行原始内容样本
    （最多 :data:`DAMAGED_SAMPLE_LIMIT` 条），只在 ``--raw`` 查询里收集。
    """

    entries: list[LogEntry] = field(default_factory=list)
    damaged_count: int = 0
    damaged_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 文本键值解析（写入侧见 logging.py 的格式器，两者互为逆运算）
# ---------------------------------------------------------------------------

# Go ``strconv.Unquote`` 支持的简单转义；``\'`` 在 Go 里只对 rune 合法，这里宽松接受。
_GO_UNQUOTE_SIMPLE = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
}

_OCTAL_DIGITS = "01234567"


def unquote_go(quoted: str) -> str:
    """解析 Go ``strconv.Quote`` 风格的双引号字符串。

    非法转义抛 ``ValueError``（调用方按 Go ``parseTextValue`` 的约定回退到原文）。
    """
    if len(quoted) < 2 or not quoted.startswith('"') or not quoted.endswith('"'):
        raise ValueError("invalid quoted string")
    body = quoted[1:-1]
    out: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:
            raise ValueError("invalid trailing escape")
        escape = body[index + 1]
        index += 2
        simple = _GO_UNQUOTE_SIMPLE.get(escape)
        if simple is not None:
            out.append(simple)
            continue
        if escape in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[escape]
            digits = body[index : index + width]
            if len(digits) != width:
                raise ValueError("truncated escape")
            out.append(chr(int(digits, 16)))
            index += width
            continue
        if escape in _OCTAL_DIGITS:
            digits = escape
            while len(digits) < 3 and index < length and body[index] in _OCTAL_DIGITS:
                digits += body[index]
                index += 1
            out.append(chr(int(digits, 8)))
            continue
        raise ValueError(f"invalid escape \\{escape}")
    return "".join(out)


def parse_text_value(line: str, start: int) -> tuple[str, int]:
    """从 ``start`` 解析一个值，返回 (值, 下一个位置)。对应 Go ``parseTextValue``。"""
    if start >= len(line):
        return "", start
    if line[start] != '"':
        index = start
        while index < len(line) and line[index] != " ":
            index += 1
        return line[start:index], index
    index = start + 1
    escaped = False
    while index < len(line):
        char = line[index]
        if char == "\\" and not escaped:
            escaped = True
            index += 1
            continue
        if char == '"' and not escaped:
            quoted = line[start : index + 1]
            try:
                return unquote_go(quoted), index + 1
            except (ValueError, OverflowError):
                # Go 的 parseTextValue 在 strconv.Unquote 失败时退回引号内的原文。
                return line[start + 1 : index], index + 1
        escaped = False
        index += 1
    return line[start + 1 :], len(line)


def parse_text_attrs(line: str) -> dict[str, str]:
    """解析一行文本键值（``k=v k2="v 2"``）。对应 Go ``parseTextAttrs``。

    只看空格分词（与 Go 一致）：值不含空格时不必加引号，含空格/引号时用 Go 的转义规则。
    """
    out: dict[str, str] = {}
    index = 0
    length = len(line)
    while index < length:
        while index < length and line[index] == " ":
            index += 1
        start = index
        while index < length and line[index] != "=" and line[index] != " ":
            index += 1
        if index >= length or line[index] != "=" or start == index:
            while index < length and line[index] != " ":
                index += 1
            continue
        key = line[start:index]
        index += 1
        value, index = parse_text_value(line, index)
        out[key] = value
    return out


def parse_log_time(value: str) -> datetime | None:
    """解析日志行的 ``time`` 字段（本地墙钟），失败返回 ``None``。"""
    if not value:
        return None
    try:
        # astimezone() 对 naive 时间按系统本地时区解释，等价 Go 的 ParseInLocation(..., time.Local)。
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def parse_log_line(raw: str) -> tuple[LogEntry, bool]:
    """解析一行日志，返回 (记录, 是否损坏)。对应 Go ``parseLogLine``。

    损坏 = ``time`` / ``level`` / ``msg`` 三者都没有（被截断的半行、早期格式、
    非本系统的内容）。
    """
    fields = parse_text_attrs(raw)
    raw_time = fields.get("time", "")
    entry = LogEntry(
        time=parse_log_time(raw_time),
        level=fields.get("level", ""),
        message=fields.get("msg", ""),
        fields=fields,
        raw=raw,
    )
    if entry.level == "" and entry.message == "" and not raw_time.strip():
        return LogEntry(raw=raw, fields={}), True
    return entry, False


# ---------------------------------------------------------------------------
# 反向分块读取
# ---------------------------------------------------------------------------


def _decode_line(raw: bytes) -> str:
    # Go 的 string 保留原始字节；Python 用 replace 兜底，避免非 UTF-8 字节让查询直接失败。
    return raw.decode("utf-8", errors="replace")


def iter_lines_reversed(
    path: str,
    block_size: int = DEFAULT_BLOCK_SIZE,
    max_line_bytes: int = MAX_LOG_LINE_BYTES,
) -> Iterator[str]:
    """从文件尾部往前按块读出每一行（不含换行符）。

    这是本模块与 Go 的**实现差异**：Go 用 ``bufio.Scanner`` 顺序读完整个文件；
    这里内存里有界（最多一块 + 一条跨块行），可观测结果一致。
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        carry = b""
        at_eof = True
        while position > 0 or carry:
            if position > 0:
                size = min(block_size, position)
                position -= size
                handle.seek(position)
                data = handle.read(size) + carry
                parts = data.split(b"\n")
                # 块首那段可能被块边界截断，留到下一块前面再拼。
                carry = parts[0]
                ready = parts[1:]
            else:
                data = carry
                carry = b""
                ready = data.split(b"\n")
            for raw in reversed(ready):
                if at_eof:
                    at_eof = False
                    if raw == b"":
                        # 文件以换行结尾：EOF 之后的空片段不是一行（Go 的 Scanner 同样不产出）。
                        continue
                yield _decode_line(raw)
            if len(carry) > max_line_bytes:
                raise ValueError(f"read log file {path}: log line exceeds {max_line_bytes} bytes")


def log_paths(dir: str, prefix: str, days: int, today: datetime | None = None) -> list[str]:
    """按"今天、昨天、…"顺序列出候选日志文件路径。对应 Go ``logPaths``。"""
    now = today or datetime.now().astimezone()
    base = now.date()
    return [
        os.path.join(dir, f"{prefix}-{(base - timedelta(days=offset)).strftime('%Y-%m-%d')}.log")
        for offset in range(max(days, 0))
    ]


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------


def _as_aware(value: datetime | None) -> datetime | None:
    """naive 时间按本地时区补齐（日志行的 ``time`` 就是本地墙钟）。"""
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.astimezone()
    return value


def normalize_log_query(query: LogQuery) -> LogQuery:
    """补齐查询默认值。对应 Go ``normalizeLogQuery``。

    返回新对象（Go 的值语义同样不会改到调用方的 ``LogQuery``）。
    """
    return replace(
        query,
        prefix=(query.prefix or "").strip(),
        limit=query.limit if query.limit > 0 else DEFAULT_LOG_LIMIT,
        days=query.days if query.days > 0 else DEFAULT_LOG_DAYS,
        min_level=(query.min_level or "").strip().upper(),
        fields=dict(query.fields or {}),
        field_exists=list(query.field_exists or []),
        field_contains={key: list(values) for key, values in (query.field_contains or {}).items()},
        since=_as_aware(query.since),
        until=_as_aware(query.until),
    )


def contains_fold(value: str, needle: str) -> bool:
    """大小写不敏感的子串判断。对应 Go ``containsFold``。"""
    return (needle or "").lower() in (value or "").lower()


def level_at_least(got: str, minimum: str) -> bool:
    """级别下限判断。对应 Go ``levelAtLeast``。"""
    return level_rank(got) >= level_rank(minimum)


def log_entry_contains(entry: LogEntry, needle: str) -> bool:
    """``--contains`` 的匹配口径。对应 Go ``logEntryContains``。

    先看固定的结构化字段，再退回整行原文。
    """
    for key in CONTAINS_FIELDS:
        if contains_fold(entry.fields.get(key, ""), needle):
            return True
    return contains_fold(entry.raw, needle)


def match_log_entry(entry: LogEntry, query: LogQuery) -> bool:
    """判断一条日志是否命中查询。对应 Go ``matchLogEntry``。"""
    if query.since is not None and entry.time is not None and entry.time < query.since:
        return False
    if query.until is not None and entry.time is not None and entry.time > query.until:
        return False
    if query.min_level and not level_at_least(entry.level, query.min_level):
        return False
    for key, want in query.fields.items():
        if not (want or "").strip():
            continue
        # Go: strings.EqualFold(entry.Fields[key], want)（不 trim，只比较大小写不敏感）。
        if (entry.fields.get(key, "") or "").lower() != (want or "").lower():
            return False
    for key in query.field_exists:
        if not (entry.fields.get(key, "") or "").strip():
            return False
    for key, needles in query.field_contains.items():
        field_value = entry.fields.get(key, "")
        for needle in needles:
            if (needle or "").strip() and not contains_fold(field_value, needle):
                return False
    if query.contains and not log_entry_contains(entry, query.contains):
        return False
    if query.msg_contains and not contains_fold(entry.message, query.msg_contains):
        return False
    return True


def _is_cancelled(ctx: object | None) -> bool:
    """尽力识别取消信号（Go 的 ``ctx.Err()``）；识别不了就当没取消。"""
    if ctx is None:
        return False
    flag = getattr(ctx, "cancelled", None)
    if callable(flag):
        return bool(flag())
    if flag is not None:
        return bool(flag)
    flag = getattr(ctx, "is_cancelled", None)
    if callable(flag):
        return bool(flag())
    return False


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class Reader:
    """日志读取器。对应 Go ``logging.Reader``（Go 用结构体字面量 ``Reader{Dir: dir}``）。"""

    def __init__(
        self,
        dir: str = "",
        block_size: int = DEFAULT_BLOCK_SIZE,
        today: datetime | None = None,
    ) -> None:
        self.dir = dir
        #: 反向读取块大小（Go 无此参数）。
        self.block_size = block_size
        #: 测试注入的"今天"（None = 系统本地时间）。
        self.today = today

    def query(self, ctx: object | None = None, query: LogQuery | None = None) -> QueryResult:
        """扫描日志。对应 Go ``Reader.Query(ctx, query)``。

        调用便利：只传一个 :class:`LogQuery` 时按 ``query(log_query)`` 处理
        （``ctx`` 在同步路径上只是取消信号，通常没有）。
        """
        if isinstance(ctx, LogQuery) and query is None:
            query, ctx = ctx, None
        if query is None:
            raise ValueError("log query is not configured")
        if not (self.dir or "").strip():
            raise ValueError("log directory is not configured")
        normalized = normalize_log_query(query)
        result = QueryResult()
        for path in log_paths(self.dir, normalized.prefix, normalized.days, self.today):
            if _is_cancelled(ctx):
                raise CancelledError("log query canceled")
            self._scan_file(path, normalized, result)
            if len(result.entries) >= normalized.limit:
                return result
        return result

    def _scan_file(self, path: str, query: LogQuery, result: QueryResult) -> None:
        """反向扫一个文件，把命中的记录与损坏行统计写进 result。

        注意：命中 ``limit`` 后仍把**本文件**读完（只是不再收集记录），因为 Go 的
        整文件扫描会把该文件里的损坏行全部计入 ``damaged_count``；更旧的文件则不再打开。
        """
        samples: deque[str] = deque(maxlen=DAMAGED_SAMPLE_LIMIT)
        reached = False
        try:
            # iter_lines_reversed 是生成器：文件不存在时在第一次迭代抛 FileNotFoundError，
            # 与 Go 的 os.IsNotExist(err) → continue 对齐。
            for raw in iter_lines_reversed(path, self.block_size):
                if not raw.strip():
                    # 空白行不算损坏（Go 同样先跳过）。
                    continue
                entry, damaged = parse_log_line(raw)
                if damaged:
                    result.damaged_count += 1
                    if query.raw:
                        # appendleft 让样本最终保持"文件正序的前 N 条"（与 Go 的
                        # 顺序扫描收集口径一致）。
                        samples.appendleft(raw)
                    continue
                if reached:
                    continue
                if match_log_entry(entry, query):
                    result.entries.append(entry)
                    if len(result.entries) >= query.limit:
                        reached = True
        except FileNotFoundError:
            return
        if samples:
            result.damaged_lines.extend(samples)
            del result.damaged_lines[DAMAGED_SAMPLE_LIMIT:]


__all__ = [
    "CONTAINS_FIELDS",
    "DAMAGED_SAMPLE_LIMIT",
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_LOG_DAYS",
    "DEFAULT_LOG_LIMIT",
    "MAX_LOG_LINE_BYTES",
    "LogEntry",
    "LogQuery",
    "QueryResult",
    "Reader",
    "contains_fold",
    "iter_lines_reversed",
    "level_at_least",
    "log_entry_contains",
    "log_paths",
    "match_log_entry",
    "normalize_log_query",
    "parse_log_line",
    "parse_log_time",
    "parse_text_attrs",
    "parse_text_value",
    "unquote_go",
]
