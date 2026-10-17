"""长期记忆的记录模型、front matter 读写与文本处理助手。

对应 Go：``internal/tool/builtin/long_memory.go`` 的类型声明、
``parseLongMemoryText`` / ``marshalLongMemoryFile`` / ``validateLongMemoryRecord``
以及文件末尾的文本助手（``tokenizeLongMemory`` / ``splitLongMemoryKeywords`` /
``slugLongMemoryTitle`` / ``normalizeLongMemory*``）。

拆分来源：``long_memory.go``（1181 行）按职责拆成三个模块——
本模块（模型 + 文本）、:mod:`atri_x.tool.builtin.long_memory_store`（SQLite 索引与
文件回填）、:mod:`atri_x.tool.builtin.long_memory`（三个工具）。

与 Go 的差异：

* ``\\p{Han}`` 正则（Go ``regexp`` 支持 Unicode 类）在 Python ``re`` 里不存在，
  用等价的 CJK 码位区间替代；``unicode.IsLetter(r) && r <= MaxASCII`` 用
  ``ch.isascii() and ch.isalpha()`` 复刻。
* TOML 用标准库 ``tomllib`` 读、:func:`atri_x.net.tomlw.dumps` 写（Go 用
  ``pelletier/go-toml/v2``）。
* ``content_edits`` 的元素类型是 :class:`atri_x.utils.fileops.Edit`（Go 的
  ``fileops.Edit``）。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError
from atri_x.net import tomlw
from atri_x.utils.fileops import Edit

#: 结果条数默认值与上限。对应 Go ``longMemoryDefaultLimit`` / ``longMemoryMaxLimit``。
LONG_MEMORY_DEFAULT_LIMIT = 3
LONG_MEMORY_MAX_LIMIT = 10

#: 同步冷却，避免每次检索都全量扫目录。对应 Go ``longMemorySyncCooldown``。
LONG_MEMORY_SYNC_COOLDOWN_SECONDS = 30.0

#: ``slugLongMemoryTitle`` 用的非法字符正则。
#:
#: Go 是 ``[^a-zA-Z0-9\\p{Han}]+``；Python ``re`` 不支持 ``\\p{...}``，这里用
#: CJK 统一表意文字（含扩展 A/B–F 与兼容区）的码位区间替代。
_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)
_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: slug 的最大字符数（Go 用 ``[]rune`` 截断，Python 按码位即可）。
SLUG_MAX_RUNES = 40

_KEYWORD_SEPARATORS = {",", "，", ";", "；", "\n", "\r", "\t"}

#: ``longMemoryWriteArgs.content_edits`` 的元素类型。
EditItems = list[Edit]


@dataclass
class LongMemoryRecord:
    """一条长期记忆。对应 Go ``longMemoryRecord``。"""

    id: int = 0
    file_path: str = ""
    category: str = ""
    title: str = ""
    summary: str = ""
    keywords: str = ""
    content: str = ""
    created_at: str = ""
    updated_at: str = ""
    file_mtime_ns: int = 0
    file_size: int = 0
    invalid: bool = False


@dataclass
class LongMemoryInvalidFile:
    """一条坏文件的索引状态。对应 Go ``longMemoryInvalidFile``。"""

    file_path: str = ""
    error: str = ""
    detected_at: str = ""
    file_mtime_ns: int = 0
    file_size: int = 0


@dataclass
class LongMemoryFrontMatter:
    """``+++`` 之间的 TOML 元数据。对应 Go ``longMemoryFrontMatter``。

    字段名与 Go 的 ``toml`` tag 逐字一致（``json``/``toml`` 都是 snake_case）。
    """

    id: int = 0
    category: str = ""
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ParsedLongMemoryFile:
    """解析结果。对应 Go ``parsedLongMemoryFile``。"""

    meta: LongMemoryFrontMatter = field(default_factory=LongMemoryFrontMatter)
    content: str = ""


@dataclass
class LongMemorySaveArgs:
    """对应 Go ``longMemorySaveArgs``（JSON 键名照抄 struct tag）。"""

    category: str = ""
    title: str = ""
    summary: str = ""
    keywords: str = ""
    content: str = ""


@dataclass
class LongMemorySearchArgs:
    """对应 Go ``longMemorySearchArgs``。``brief_only`` 为 ``None`` 表示未传。"""

    keywords: str = ""
    category: str = ""
    match_mode: str = ""
    brief_only: bool | None = None
    limit: int = 0


@dataclass
class LongMemoryUpdateArgs:
    """对应 Go ``longMemoryUpdateArgs``（字段是 ``*string``：``None`` 表示不改）。"""

    id: int = 0
    category: str | None = None
    title: str | None = None
    summary: str | None = None
    keywords: str | None = None
    content: str | None = None
    content_edits: EditItems = field(default_factory=list)

    def has_changes(self) -> bool:
        """对应 Go ``longMemoryUpdateArgs.hasChanges``。"""
        return (
            self.category is not None
            or self.title is not None
            or self.summary is not None
            or self.keywords is not None
            or self.content is not None
            or len(self.content_edits) > 0
        )


@dataclass
class LongMemoryWriteArgs:
    """对应 Go ``longMemoryWriteArgs``。"""

    operation: str = ""
    id: int = 0
    category: str = ""
    title: str = ""
    summary: str = ""
    keywords: str = ""
    content: str = ""
    content_edits: EditItems = field(default_factory=list)


@dataclass
class LongMemoryUpdatePreview:
    """一次 update 的预检结果。对应 Go ``longMemoryUpdatePreview``。"""

    before: LongMemoryRecord = field(default_factory=LongMemoryRecord)
    after: LongMemoryRecord = field(default_factory=LongMemoryRecord)
    file: Any = None
    full_content: str = ""
    diff: str = ""
    changes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 参数解码
# ---------------------------------------------------------------------------
def decode_long_memory_args(raw: str, kind: type, name: str) -> Any:
    """对应 Go ``lmDecodeArgs``：空入参返回零值，否则解成数据类。

    Go 的 ``json.Unmarshal`` 会忽略未知字段；``content_edits`` 里的对象先转成
    :class:`atri_x.utils.fileops.Edit`。
    """
    out = kind()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse {name} arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"parse {name} arguments: expected a JSON object")
    for key, value in data.items():
        if key == "content_edits":
            out.content_edits = _decode_edits(value, name)
            continue
        if not hasattr(out, key):
            continue
        setattr(out, key, value)
    return out


def _decode_edits(value: Any, name: str) -> EditItems:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidError(f"parse {name} arguments: content_edits must be an array")
    edits: EditItems = []
    for item in value:
        if isinstance(item, Edit):
            edits.append(item)
            continue
        if not isinstance(item, dict):
            raise InvalidError(
                f"parse {name} arguments: content_edits items must be objects"
            )
        kwargs: dict[str, Any] = {}
        for key, raw_value in item.items():
            if hasattr(Edit, key):
                kwargs[key] = raw_value
        edits.append(Edit(**kwargs))
    return edits


def update_args_from_write(args: LongMemoryWriteArgs) -> LongMemoryUpdateArgs:
    """对应 Go ``longMemoryUpdateArgsFromWrite``：空串表示该字段不改。"""
    update = LongMemoryUpdateArgs(id=args.id, content_edits=list(args.content_edits))
    if args.category != "":
        update.category = args.category
    if args.title != "":
        update.title = args.title
    if args.summary != "":
        update.summary = args.summary
    if args.keywords != "":
        update.keywords = args.keywords
    if args.content != "":
        update.content = args.content
    return update


# ---------------------------------------------------------------------------
# front matter 解析与序列化
# ---------------------------------------------------------------------------
def parse_long_memory_text(text: str) -> ParsedLongMemoryFile:
    """解析 ``+++`` 包裹的 TOML front matter + 正文。对应 Go ``parseLongMemoryText``。"""
    if not text.startswith("+++"):
        raise InvalidError("缺少开头 TOML front matter 分隔符 +++")
    remaining = text[3:]
    if remaining.startswith("\r\n"):
        remaining = remaining[2:]
    elif remaining.startswith("\n"):
        remaining = remaining[1:]

    index = remaining.find("\n+++")
    sep_len = 4
    if index < 0:
        index = remaining.find("\r\n+++")
        sep_len = 5
    if index < 0:
        raise InvalidError("缺少结束 TOML front matter 分隔符 +++，无法可靠区分元数据和正文")

    front = remaining[:index]
    after = remaining[index + sep_len :]
    if after.startswith("\r\n"):
        after = after[2:]
    elif after.startswith("\n"):
        after = after[1:]

    try:
        raw = tomllib.loads(front)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidError(f"TOML front matter 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidError("TOML front matter 解析失败：顶层不是表")
    keywords = raw.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    meta = LongMemoryFrontMatter(
        id=_as_int(raw.get("id")),
        category=_as_str(raw.get("category")),
        title=_as_str(raw.get("title")),
        summary=_as_str(raw.get("summary")),
        keywords=[_as_str(item) for item in keywords],
        created_at=_as_str(raw.get("created_at")),
        updated_at=_as_str(raw.get("updated_at")),
    )
    return ParsedLongMemoryFile(meta=meta, content=after.strip())


def parse_long_memory_file(path: str) -> ParsedLongMemoryFile:
    """读文件再解析。对应 Go ``parseLongMemoryFile``。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise InvalidError(f"read file: {exc}") from exc
    return parse_long_memory_text(data.decode("utf-8", errors="replace"))


def marshal_long_memory_file(record: LongMemoryRecord) -> bytes:
    """序列化成 ``+++`` + TOML + 正文。对应 Go ``marshalLongMemoryFile``。"""
    meta = {
        "id": record.id,
        "category": record.category,
        "title": record.title,
        "summary": record.summary,
        "keywords": split_long_memory_keywords(record.keywords),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    try:
        front = tomlw.dumps(meta, sort_keys=False)
    except Exception as exc:  # noqa: BLE001 - tomlw 只有一种写失败
        raise InvalidError(f"marshal long memory front matter: {exc}") from exc
    body = "+++\n" + front.strip() + "\n+++\n\n" + record.content.strip() + "\n"
    return body.encode("utf-8")


def record_from_parsed_file(
    path: str, parsed: ParsedLongMemoryFile, mtime_ns: int, size: int
) -> LongMemoryRecord:
    """对应 Go ``recordFromParsedFile``。"""
    return LongMemoryRecord(
        id=_as_int(parsed.meta.id),
        file_path=path,
        category=clean_long_memory_text(parsed.meta.category),
        title=clean_long_memory_text(parsed.meta.title),
        summary=clean_long_memory_text(parsed.meta.summary),
        keywords=" ".join(trim_string_list(parsed.meta.keywords)),
        content=parsed.content.strip(),
        created_at=parsed.meta.created_at.strip(),
        updated_at=parsed.meta.updated_at.strip(),
        file_mtime_ns=mtime_ns,
        file_size=size,
    )


def validate_long_memory_record(record: LongMemoryRecord, require_content: bool) -> None:
    """校验必填字段。对应 Go ``validateLongMemoryRecord``（Go 返回 error）。"""
    if record.id <= 0:
        raise InvalidError("id 必须是正整数")
    if record.category == "":
        raise InvalidError("category is required")
    if record.title == "":
        raise InvalidError("title is required")
    if record.summary == "":
        raise InvalidError("summary is required")
    if require_content and record.content.strip() == "":
        raise InvalidError("content is required")
    if record.created_at.strip() == "" or record.updated_at.strip() == "":
        raise InvalidError("created_at 和 updated_at 是必填字段")


# ---------------------------------------------------------------------------
# 文本助手
# ---------------------------------------------------------------------------
def normalize_long_memory_limit(limit: int) -> int:
    """对应 Go ``normalizeLongMemoryLimit``。"""
    if limit <= 0:
        return LONG_MEMORY_DEFAULT_LIMIT
    if limit > LONG_MEMORY_MAX_LIMIT:
        return LONG_MEMORY_MAX_LIMIT
    return limit


def normalize_long_memory_match_mode(mode: str) -> str:
    """对应 Go ``normalizeLongMemoryMatchMode``。"""
    if (mode or "").strip().upper() == "AND":
        return "AND"
    return "OR"


def long_memory_search_tokens(text: str) -> list[str]:
    """对应 Go ``longMemorySearchTokens``。"""
    return tokenize_long_memory(text).split()


def long_memory_fts_query(tokens: list[str], match_mode: str) -> str:
    """对应 Go ``longMemoryFTSQuery``：双引号包裹并转义。"""
    parts = ['"' + token.replace('"', '""') + '"' for token in tokens]
    operator = " AND " if match_mode == "AND" else " OR "
    return operator.join(parts)


def long_memory_text_matches(text: str, tokens: list[str], match_mode: str) -> bool:
    """对应 Go ``longMemoryTextMatches``。"""
    lowered = text.lower()
    if match_mode == "AND":
        return all(token.lower() in lowered for token in tokens)
    if any(token.lower() in lowered for token in tokens):
        return True
    return len(tokens) == 0


def tokenize_long_memory(text: str) -> str:
    """小写化 + ASCII 词 / CJK 单字与二元切分。对应 Go ``tokenizeLongMemory``。"""
    words: list[str] = []
    runes = list((text or "").lower())
    i = 0
    while i < len(runes):
        ch = runes[i]
        if _is_alpha_num(ch):
            start = i
            while i < len(runes) and _is_alpha_num(runes[i]):
                i += 1
            words.append("".join(runes[start:i]))
            continue
        if _is_cjk(ch):
            start = i
            while i < len(runes) and _is_cjk(runes[i]):
                i += 1
            segment = runes[start:i]
            for j in range(len(segment)):
                words.append(segment[j])
                if j + 1 < len(segment):
                    words.append("".join(segment[j : j + 2]))
            continue
        i += 1
    return " ".join(words)


def _is_alpha_num(ch: str) -> bool:
    """对应 Go ``isLongMemoryAlphaNum``（只认 ASCII 字母/数字与下划线）。"""
    if ch == "_":
        return True
    return ch.isascii() and (ch.isalpha() or ch.isdigit())


def _is_cjk(ch: str) -> bool:
    """对应 Go ``unicode.Is(unicode.Han, r)``（用码位区间近似）。"""
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def split_long_memory_keywords(text: str) -> list[str]:
    """按逗号/分号/空白切关键词并去重。对应 Go ``splitLongMemoryKeywords``。"""
    normalized = []
    for ch in text or "":
        normalized.append(" " if ch in _KEYWORD_SEPARATORS else ch)
    return trim_string_list("".join(normalized).split())


def clean_long_memory_text(text: str) -> str:
    """折叠空白。对应 Go ``cleanLongMemoryText``。"""
    return " ".join((text or "").strip().split())


def trim_string_list(values: Any) -> list[str]:
    """去空白 + 去重 + 保序。对应 Go ``lmTrimStringList``。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = _as_str(value).strip()
        if value == "" or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def slug_long_memory_title(title: str) -> str:
    """标题 → 文件名 slug。对应 Go ``slugLongMemoryTitle``。"""
    slug = _SLUG_INVALID.sub("-", title or "").strip("-")
    if slug == "":
        slug = "memory"
    if len(slug) > SLUG_MAX_RUNES:
        slug = slug[:SLUG_MAX_RUNES]
    return slug


def format_memory_id(id: int) -> str:
    """``%06d`` 形式，用于文件名。对应 Go 的 ``fmt.Sprintf("%06d-%s.md", ...)``。"""
    return f"{id:06d}"


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


__all__ = [name for name in dir() if not name.startswith("_")]
