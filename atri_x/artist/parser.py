"""画师串的确定性解析：分段、权重解包、画师识别与还原。

对应 Go：``internal/artist/parser.go``。

这一层**不做模型调用**：它只按绘画提示词的书写约定（逗号分段、``()``/``{}``/``[]``
权重、``artist:`` 前缀、``by ``）把一段文本拆成 token，并把被标记为画师的片段
还原成 :class:`~atri_x.artist.types.ArtistRef`。

三条不可动摇的规则（与 Go 注释里的意图一致）：

* 解析器**拒绝**解释的表达式（括号不配对、括号组里含逗号、冒号是 prompt editing
  语法）一律标成 ``unparsed`` 并逐字保留：既不改写、也不重排、更不切开。
* 未知转义原样保留两个字符，不静默丢字符。
* ``ExtractArtistsWith`` 的结果永远带 ``SourceText``；只有
  :func:`extract_from_graph` 才把来源标成 ``metadata``。

与 Go 的差异：

* Go 的 ``Resolver`` 是 ``func(string) (string, bool)``，Python 用同签名的可调用对象。
* Go 按字节切分（``topLevelColon`` 返回字节下标）；Python 的字符串下标是码位，
  对 ASCII 冒号（``:``/``：``）二者等价，因此这里直接用码位下标。
"""

from __future__ import annotations

from typing import Any, Callable

from .types import (
    ArtistRef,
    Dialect,
    Extraction,
    SourceKind,
    Token,
    TokenKind,
)

#: 名字 → 目录 id 的解析函数（Go ``Resolver``）。取不到就返回 ``("", False)``。
Resolver = Callable[[str], "tuple[str, bool]"]

#: 隐式权重的社区约定（不是 ATRI-X 配置推导出来的）。对应 Go ``parenWeight`` 等。
PAREN_WEIGHT = 1.1
BRACE_WEIGHT = 1.05
BLOCK_WEIGHT = 0.9

#: 显式权重允许范围（对应 Go ``MinWeight`` / ``MaxWeight``）。
MIN_WEIGHT = 0.01
MAX_WEIGHT = 10.0

#: 显式声明"这是画师"的前缀。对应 Go ``artistKeyPrefixes``。
ARTIST_KEY_PREFIXES = ("artist", "artist_name", "artists", "画师", "艺术家")

_OPENERS = "([{"
_CLOSERS = ")]}"
_CLOSER_OF = {"(": ")", "[": "]", "{": "}"}
_SEPARATORS = (",", "\n", "\r")


def split_segments(text: str) -> list[str]:
    """把提示词按"深度 0 的逗号/换行"切成顶层片段。对应 Go ``SplitSegments``。

    括号深度被夹在 0（多余的 ``)`` 不能把后面整段吞掉），被转义的分隔符不切分。
    """
    segments: list[str] = []
    current: list[str] = []
    depth = 0
    escaped = False
    for ch in text or "":
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            if depth > 0:
                depth -= 1
        elif ch in _SEPARATORS and depth == 0:
            segments.append("".join(current))
            current = []
            continue
        current.append(ch)
    segments.append("".join(current))
    return segments


def outer_pair(s: str) -> tuple[str, bool]:
    """判断首字符的括号是否恰好在最后一个字符闭合。对应 Go ``OuterPair``。

    返回 ``(开括号, 是否整段成对)``；不成对时开括号为 ``""``。
    """
    if len(s) < 2:
        return "", False
    opener = s[0]
    if opener not in _CLOSER_OF:
        return "", False
    if s[-1] != _CLOSER_OF[opener]:
        return "", False
    depth = 0
    escaped = False
    for index, ch in enumerate(s):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
            if depth == 0:
                return opener, index == len(s) - 1
    return "", False


def top_level_colon(s: str) -> int:
    """最后一个深度 0 的冒号下标，没有则 -1。对应 Go ``topLevelColon``。"""
    depth = 0
    escaped = False
    found = -1
    for index, ch in enumerate(s):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            if depth > 0:
                depth -= 1
        elif ch == ":" and depth == 0:
            found = index
    return found


def has_top_level_comma(s: str) -> bool:
    """片段自带分隔符 → 它是"标签组"而不是单个加权标签。对应 Go ``HasTopLevelComma``。"""
    depth = 0
    escaped = False
    for ch in s or "":
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            if depth > 0:
                depth -= 1
        elif ch in _SEPARATORS and depth == 0:
            return True
    return False


def unwrap_weight(segment: str) -> tuple[str, float, bool, bool]:
    """把一个片段解释成"可能带权重的表达式"。对应 Go ``UnwrapWeight``。

    返回 ``(inner, weight, weighted, ok)``；``ok=False`` 表示解析器拒绝解释，
    调用方必须逐字保留原文（既不改写也不切开）。
    """
    value = (segment or "").strip()
    if value == "":
        return "", 1.0, False, False
    weight = 1.0
    weighted = False
    while True:
        opener, is_pair = outer_pair(value)
        if not is_pair:
            break
        inner = value[1:-1].strip()
        if has_top_level_comma(inner):
            # "(a, b)" 是标签组：切开就改变了用户写的标签顺序。
            return "", 1.0, False, False
        if opener == "(":
            body, explicit, has_weight = split_explicit_weight(inner)
            if has_weight:
                weight *= explicit
                value = body
                weighted = True
                continue
            if top_level_colon(inner) >= 0:
                # 不是权重的冒号 = prompt editing 语法，拒绝解释。
                return "", 1.0, False, False
            weight *= PAREN_WEIGHT
        elif opener == "{":
            if top_level_colon(inner) >= 0:
                return "", 1.0, False, False
            weight *= BRACE_WEIGHT
        elif opener == "[":
            if top_level_colon(inner) >= 0:
                return "", 1.0, False, False
            weight *= BLOCK_WEIGHT
        else:
            return "", 1.0, False, False
        weighted = True
        value = inner
    if value == "":
        return "", 1.0, False, False
    return unescape(value), weight, weighted, True


def split_explicit_weight(inner: str) -> tuple[str, float, bool]:
    """``name:1.2`` → ``("name", 1.2, True)``。对应 Go ``splitExplicitWeight``。"""
    index = top_level_colon(inner)
    if index < 0:
        return "", 0.0, False
    raw = inner[index + 1 :].strip()
    try:
        parsed = float(raw)
    except ValueError:
        return "", 0.0, False
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return "", 0.0, False
    if parsed < MIN_WEIGHT or parsed > MAX_WEIGHT:
        return "", 0.0, False
    body = inner[:index].strip()
    if body == "" or top_level_colon(body) >= 0 or has_top_level_comma(body):
        return "", 0.0, False
    return body, parsed, True


def unescape(s: str) -> str:
    """还原绘画语法定义的转义；未知转义保留两个字符。对应 Go ``unescape``。"""
    if "\\" not in s:
        return s
    out: list[str] = []
    index = 0
    length = len(s)
    while index < length:
        ch = s[index]
        if ch != "\\" or index + 1 >= length:
            out.append(ch)
            index += 1
            continue
        nxt = s[index + 1]
        if nxt in "\\()[]{},:;":
            out.append(nxt)
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def escape(s: str) -> str:
    """转义会被读成语法字符的字符。对应 Go ``Escape``。"""
    out: list[str] = []
    for ch in s or "":
        if ch in "\\()[]{},:":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def strip_quotes(s: str) -> str:
    """剥掉成对的引号（ASCII 与全角）。对应 Go ``stripQuotes``。"""
    if len(s) < 2:
        return s
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if s.startswith(opener) and s.endswith(closer):
            return s[len(opener) : len(s) - len(closer)].strip()
    return s


def artist_name(value: str) -> tuple[str, bool]:
    """判断标签是否自称"画师标签"，并返回标记后的名字。对应 Go ``ArtistName``。"""
    trimmed = strip_quotes((value or "").strip())
    if trimmed == "":
        return "", False
    for prefix in ARTIST_KEY_PREFIXES:
        if len(trimmed) < len(prefix) or trimmed[: len(prefix)].lower() != prefix:
            continue
        rest = trimmed[len(prefix) :]
        if rest == "" or rest[0] not in (":", "："):
            continue
        name = rest[1:].strip()
        if name != "":
            return name, True
        return "", False
    if len(trimmed) > 3 and trimmed[:3].lower() == "by ":
        name = trimmed[3:].strip()
        if name != "":
            return name, True
    return "", False


def classify(text: str, resolve: Resolver | None = None) -> list[Token]:
    """切分并给每个顶层片段分类。对应 Go ``classify``。"""
    tokens: list[Token] = []
    for segment in split_segments(text):
        raw = segment.strip()
        if raw == "":
            continue
        value, weight, weighted, ok = unwrap_weight(raw)
        if not ok:
            tokens.append(
                Token(
                    index=len(tokens),
                    raw=raw,
                    value=raw,
                    weight=1.0,
                    kind=TokenKind.UNPARSED,
                )
            )
            continue
        token = Token(
            index=len(tokens),
            raw=raw,
            value=value,
            weight=weight,
            weighted=weighted,
            kind=TokenKind.PLAIN,
        )
        name, is_artist = artist_name(value)
        if is_artist:
            token.kind = TokenKind.ARTIST
            token.artist = name
        elif resolve is not None:
            _id, found = resolve(value)
            if found:
                token.kind = TokenKind.ARTIST
                token.artist = value
        tokens.append(token)
    return tokens


def extract_artists(text: str) -> Extraction:
    """只认显式标记的画师标签。对应 Go ``ExtractArtists``。"""
    return extract_artists_with(text, None)


def extract_artists_with(text: str, resolve: Resolver | None = None) -> Extraction:
    """完整抽取画师串。对应 Go ``ExtractArtistsWith``。"""
    tokens = classify(text, resolve)
    out = Extraction(
        source=SourceKind.TEXT, dialect=Dialect.AUTO, tokens=tokens
    )
    remaining: list[str] = []
    raw_parts: list[str] = []
    names: dict[str, int] = {}
    for token in tokens:
        if token.kind != TokenKind.ARTIST:
            remaining.append(token.raw)
            continue
        ref = ArtistRef(
            name=token.artist,
            raw=token.raw,
            weight=token.weight,
            index=len(out.artists),
        )
        if resolve is not None:
            resolved_id, ok = resolve(token.artist)
            if ok:
                ref.id = resolved_id
        key = (ref.name or "").lower()
        if key in names:
            out.warning(
                "画师 "
                + ref.name
                + " 在画师串里出现了多次（第 "
                + str(names[key] + 1)
                + " 和第 "
                + str(ref.index + 1)
                + " 个），已按原顺序全部保留。"
            )
        else:
            names[key] = ref.index
        out.artists.append(ref)
        raw_parts.append(token.raw)
    out.remaining = ", ".join(remaining)
    if out.artists:
        out.original = ", ".join(raw_parts)
        out.has_original = True
    return out


def extract_from_graph(text: str, resolve: Resolver | None = None) -> Extraction:
    """从图片元数据里的 prompt 抽取画师串。对应 Go ``ExtractFromGraph``。"""
    out = extract_artists_with(text, resolve)
    out.source = SourceKind.METADATA
    return out


def normalize_tag(tag: str) -> str:
    """折叠成查表形式：去空白、压成一行、小写。对应 Go ``NormalizeTag``。"""
    return " ".join((tag or "").split()).lower()


__all__ = [name for name in dir() if not name.startswith("_")]
