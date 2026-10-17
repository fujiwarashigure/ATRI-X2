"""TOML 写入：对应 Go 的 ``pelletier/go-toml/v2``（写入侧）。

Python 标准库只有只读的 :mod:`tomllib`，本模块补上写入能力：
``dumps`` / ``dumps_table`` 生成 TOML 文本，``dump`` 原子落盘（临时文件 +
``os.replace``），供 state/config 与角色卡（``character/*.toml``）使用。

**``None`` 策略（唯一策略，调用方需知）**：TOML 没有 null。默认
``skip_none=False``，遇到 ``None`` 直接抛 :class:`TomlWriteError`（``ValueError``
的子类），异常信息里带出出错的键路径；显式传 ``skip_none=True`` 时改为**跳过**
该键（映射键整体略去，数组里的 ``None`` 元素略去）。

**确定性**：同一输入必得同一输出。``sort_keys=True``（默认）时表内按键名排序，
``sort_keys=False`` 时保持 dict 的插入顺序。表体输出顺序固定为：先标量/数组/可折叠
的点分键，再 ``[子表]`` 头与 ``[[数组表]]`` 块（TOML 要求同表标量不得出现在子表头之后）。

**键形态**：顶层子表用 ``[name]`` 头（惯用写法）；更深层且可折叠的子表用点分键
``a.b.c = 1``；含数组表或空子表时回退为 ``[a.b]`` 头。
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

__all__ = ["TomlWriteError", "dump", "dumps", "dumps_table"]

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_INLINE = 100
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class TomlWriteError(ValueError):
    """TOML 写入失败（类型不支持、出现 ``None``、键不是字符串等）。"""


# ---------------------------------------------------------------------------
# 标量
# ---------------------------------------------------------------------------
def _escape(text: str) -> str:
    out: list[str] = []
    for ch in text:
        mapped = _ESCAPES.get(ch)
        if mapped is not None:
            out.append(mapped)
        elif ch < " " or ch == "\x7f":
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _string(text: str, *, multiline_ok: bool = True) -> str:
    """基本字符串；含换行且无歧义时用多行形式 ``\"\"\"...\"\"\"``。"""
    if multiline_ok and "\n" in text:
        body = text.replace("\\", "\\\\").replace("\r", "\\r").replace('"""', '\\"\\"\\"')
        if not body.startswith('"') and not body.endswith('"') and not body.endswith("\\"):
            return '"""\n' + body + '"""'
    return '"' + _escape(text) + '"'


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _string(name, multiline_ok=False)


def _dotted(path: Sequence[str]) -> str:
    return ".".join(_key(part) for part in path)


def _number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    text = repr(float(value))
    return text if ("." in text or "e" in text or "E" in text) else text + ".0"


def _temporal(value: Any) -> str:
    if isinstance(value, _dt.datetime):
        text = value.strftime("%Y-%m-%dT%H:%M:%S")
        if value.microsecond:
            text += ".%06d" % value.microsecond
        offset = value.utcoffset()
        if offset is None:
            return text
        total = int(offset.total_seconds())
        if total == 0:
            return text + "Z"
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return "%s%s%02d:%02d" % (text, sign, total // 3600, (total % 3600) // 60)
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = value.strftime("%H:%M:%S")
    return text + (".%06d" % value.microsecond if value.microsecond else "")


def _scalar(value: Any, *, multiline_ok: bool = True) -> str | None:
    """返回标量的 TOML 文本；不是标量则返回 ``None``（``None`` 由调用方处理）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return _string(value, multiline_ok=multiline_ok)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return _temporal(value)
    return None


# ---------------------------------------------------------------------------
# 结构判定
# ---------------------------------------------------------------------------
def _is_array_of_tables(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, Mapping) for item in value)
    )


def _is_flattenable(value: Any) -> bool:
    """子表能否折叠成点分键：非空、无数组表、无空/不可折叠子表。"""
    if not isinstance(value, Mapping) or not value:
        return False
    for item in value.values():
        if _is_array_of_tables(item):
            return False
        if isinstance(item, Mapping) and not _is_flattenable(item):
            return False
    return True


def _iter_dotted(value: Mapping[str, Any], prefix: tuple[str, ...], sort_keys: bool):
    """把可折叠子表展开成 ``(点分键路径, 值)`` 序列。"""
    for key, item in _ordered(value.items(), sort_keys):
        path = prefix + (key,)
        if isinstance(item, Mapping):
            yield from _iter_dotted(item, path, sort_keys)
        else:
            yield path, item


def _ordered(items: Any, sort_keys: bool) -> list[tuple[str, Any]]:
    pairs = list(items)
    for key, _ in pairs:
        if not isinstance(key, str):
            raise TomlWriteError(f"TOML 键必须是字符串，得到 {type(key).__name__}: {key!r}")
    if sort_keys:
        pairs.sort(key=lambda pair: pair[0])
    return pairs


# ---------------------------------------------------------------------------
# 值渲染
# ---------------------------------------------------------------------------
class _Options:
    __slots__ = ("sort_keys", "skip_none")

    def __init__(self, sort_keys: bool, skip_none: bool) -> None:
        self.sort_keys = sort_keys
        self.skip_none = skip_none


def _render(value: Any, opts: _Options, *, inline: bool = False, depth: int = 0) -> str:
    if value is None:
        raise TomlWriteError("TOML 不支持 null：值为 None")
    scalar = _scalar(value, multiline_ok=not inline)
    if scalar is not None:
        return scalar
    if isinstance(value, Mapping):
        parts = [
            f"{_key(key)} = {_render(item, opts, inline=True, depth=depth + 1)}"
            for key, item in _ordered(value.items(), opts.sort_keys)
            if not (item is None and opts.skip_none)
        ]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return _array(value, opts, inline=inline, depth=depth)
    raise TomlWriteError(f"不支持的 TOML 值类型: {type(value).__name__}")


def _array(items: Sequence[Any], opts: _Options, *, inline: bool, depth: int) -> str:
    kept = [item for item in items if not (item is None and opts.skip_none)]
    if not kept:
        return "[]"
    parts = [_render(item, opts, inline=True, depth=depth + 1) for item in kept]
    one_line = "[" + ", ".join(parts) + "]"
    if inline or ("\n" not in one_line and len(one_line) <= _MAX_INLINE):
        return one_line
    pad = "  " * (depth + 1)
    body = "".join(f"{pad}{part},\n" for part in parts)
    return "[\n" + body + ("  " * depth) + "]"


# ---------------------------------------------------------------------------
# 表体
# ---------------------------------------------------------------------------
def _emit_table(
    out: list[str],
    table: Mapping[str, Any],
    path: tuple[str, ...],
    opts: _Options,
    *,
    header: bool,
    flatten: bool,
) -> None:
    """输出一张表：先本表标量，再子表头，最后数组表。"""
    if header:
        out.append(f"[{_dotted(path)}]\n")
    plain: list[tuple[tuple[str, ...], Any]] = []
    sub_tables: list[tuple[str, Mapping[str, Any]]] = []
    sub_arrays: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
    for key, value in _ordered(table.items(), opts.sort_keys):
        if value is None:
            if opts.skip_none:
                continue
            raise TomlWriteError(f"TOML 不支持 null：{'.'.join(path + (key,))} 为 None")
        if isinstance(value, Mapping):
            if flatten and _is_flattenable(value):
                plain.extend(_iter_dotted(value, (key,), opts.sort_keys))
            else:
                sub_tables.append((key, value))
        elif _is_array_of_tables(value):
            sub_arrays.append((key, value))
        else:
            plain.append(((key,), value))
    for key_path, value in plain:
        out.append(f"{_dotted(key_path)} = {_render(value, opts)}\n")
    for key, value in sub_tables:
        child = path + (key,)
        if value:
            _emit_table(out, value, child, opts, header=True, flatten=True)
        else:
            out.append(f"[{_dotted(child)}]\n")
    for key, value in sub_arrays:
        child = path + (key,)
        for element in value:
            out.append(f"[[{_dotted(child)}]]\n")
            _emit_table(out, element, child, opts, header=False, flatten=True)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def dumps(obj: Mapping[str, Any], *, sort_keys: bool = True, skip_none: bool = False) -> str:
    """把映射序列化成 TOML 文本。

    顶层子表输出为 ``[name]`` 头，更深层可折叠子表用点分键（``a.b.c = 1``）；
    ``None`` 默认抛 :class:`TomlWriteError`，``skip_none=True`` 时跳过。
    """
    return _dumps(obj, sort_keys=sort_keys, skip_none=skip_none, flatten_root=False, api="dumps")


def dumps_table(obj: Mapping[str, Any], *, sort_keys: bool = True, skip_none: bool = False) -> str:
    """输出**不带顶层表头**的表体，供调用方自行拼接 ``[header]``。

    与 :func:`dumps` 的唯一区别：顶层子表也优先折叠成点分键。
    """
    return _dumps(
        obj, sort_keys=sort_keys, skip_none=skip_none, flatten_root=True, api="dumps_table"
    )


def _dumps(
    obj: Mapping[str, Any], *, sort_keys: bool, skip_none: bool, flatten_root: bool, api: str
) -> str:
    if not isinstance(obj, Mapping):
        raise TomlWriteError(f"{api} 需要 Mapping，得到 {type(obj).__name__}")
    out: list[str] = []
    _emit_table(
        out, obj, (), _Options(sort_keys, skip_none), header=False, flatten=flatten_root
    )
    return "".join(out)


def dump(
    obj: Mapping[str, Any],
    path: str | os.PathLike[str],
    *,
    sort_keys: bool = True,
    skip_none: bool = False,
) -> None:
    """原子写入 TOML 文件（同目录临时文件 + ``os.replace``）。

    目标目录不存在时自动创建；内容为 UTF-8 无 BOM、``\\n`` 换行；失败时清理临时
    文件，已存在的目标文件要么保持旧内容、要么是完整的新内容。
    """
    text = dumps(obj, sort_keys=sort_keys, skip_none=skip_none)
    target = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".tomlw-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        temp_path = ""
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:  # pragma: no cover - 清理失败不掩盖原始异常
                pass
