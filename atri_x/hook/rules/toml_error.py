"""``hooks.toml`` 的读取与详细错误信息。

对应 Go：``internal/hook/rules/toml_error.go``。

上游用 ``pelletier/go-toml/v2`` 的 ``DisallowUnknownFields``；Python 标准库只有
只读的 ``tomllib``，所以严格字段校验由 :func:`strict_table` 这一层自己做。

本模块还负责把 tomllib 的错误翻成**带行号、列号、字段名与所属小节**的中文
可读信息 —— 与 Go 的 ``detailedTOMLDecodeError`` 对齐，便于用户直接定位
``hooks.toml`` 里写错的那一行。
"""

from __future__ import annotations

import os
import re
import threading
import tomllib
from typing import Any, Callable, Mapping, TypeVar

from atri_x.errors import AtriXError

T = TypeVar("T")

#: 当前正在解码的源文本（严格错误要给出源码行号，而嵌套结构体的转换函数只拿到
#: 值本身，所以把源文本放在线程局部里，由 strict_table 兜底读取）。
_SOURCE = threading.local()

__all__ = [
    "ConfigParseError",
    "StrictDecodeError",
    "decode_toml_file",
    "decode_toml_text",
    "detailed_toml_decode_error",
    "detailed_generic_toml_parse_error",
    "conflicting_toml_key",
    "find_toml_array_table_conflict",
    "source_line_snippet",
    "toml_context_at_line",
    "indent_lines",
    "parse_toml_assignment_key",
    "is_toml_array_table_for_key",
    "parse_toml_string_assignment",
    "locate_key_line",
    "strict_table",
    "strict_array",
    "strict_string",
    "strict_bool",
    "strict_int",
    "strict_string_list",
]

#: tomllib 报"重复声明"时的键名提取（不同版本措辞不同，做两种尝试）。
_KEY_PATTERNS = (
    re.compile(r"already exists as a\s+([A-Za-z0-9_-]+)"),
    re.compile(r"Cannot declare \('[^']*',\s*\d+,\s*'([^']+)'\) twice"),
    re.compile(r"Cannot declare \('([^']+)'"),
    re.compile(r"duplicate key:?\s*'?([A-Za-z0-9_-]+)'?"),
    re.compile(r"key '([^']+)'"),
)


class ConfigParseError(AtriXError):
    """``hooks.toml`` 解析/严格解码失败。对应 Go ``configParseError``。"""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self._cause = cause

    @property
    def cause(self) -> BaseException | None:
        return self._cause

    def __str__(self) -> str:
        return self.message


class StrictDecodeError(AtriXError):
    """字段级严格解码错误（未知字段、类型不符）。"""

    def __init__(
        self,
        message: str,
        *,
        key: str = "",
        row: int = 0,
        column: int = 0,
        context: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.key = key
        self.row = row
        self.column = column
        self.context = context


def decode_toml_file(path: str, decode: Callable[[dict[str, Any]], T]) -> T:
    """读文件并严格解码。对应 Go ``decodeTOMLFile``。

    文件不存在时抛 :class:`FileNotFoundError`（对应 Go 的 ``os.ErrNotExist``，
    调用方需要按它判定"没有配置"）。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f'read hook rule config "{path}": {exc}'
        ) from exc
    except OSError as exc:
        raise ConfigParseError(f'read hook rule config "{path}": {exc}', exc) from exc
    data = raw.decode("utf-8")
    return decode_toml_text(path, data, decode)


def decode_toml_text(path: str, data: str, decode: Callable[[dict[str, Any]], T]) -> T:
    """同 :func:`decode_toml_file`，但输入已经是文本。"""
    try:
        parsed = tomllib.loads(data)
    except tomllib.TOMLDecodeError as exc:
        raise detailed_toml_decode_error(path, data, exc) from exc
    except ValueError as exc:  # pragma: no cover - 其它解析错误
        raise detailed_toml_decode_error(path, data, exc) from exc
    _SOURCE.text = data
    try:
        return decode(parsed)
    except StrictDecodeError as exc:
        raise _wrap_strict_error(path, [exc]) from exc
    finally:
        _SOURCE.text = ""


def _current_source() -> str:
    return getattr(_SOURCE, "text", "") or ""


def _wrap_strict_error(path: str, errors: list[StrictDecodeError]) -> ConfigParseError:
    detail = [f'parse hook rule config "{path}"']
    for err in errors:
        detail.append(_format_strict_detail(err))
    return ConfigParseError("\n".join(detail))


def _format_strict_detail(err: StrictDecodeError) -> str:
    parts: list[str] = []
    if err.row > 0:
        parts.append(f"line {err.row}, column {err.column or 1}")
    if err.key:
        parts.append(f'field "{err.key}"')
    if err.context:
        parts.append(err.context)
    prefix = ("- " + ", ".join(parts) + ": ") if parts else "- "
    return prefix + err.message


def detailed_toml_decode_error(
    path: str, data: str, err: BaseException
) -> AtriXError:
    """把 tomllib 的错误翻成带位置的详细错误。对应 Go ``detailedTOMLDecodeError``。"""
    row, column = _error_position(err)
    key = conflicting_toml_key(str(err))
    if key and row <= 0:
        found_row, found_column, previous_row, context = find_toml_array_table_conflict(
            data, key
        )
        if found_row > 0:
            row, column = found_row, found_column
    context = toml_context_at_line(data, row) if row > 0 else ""
    pieces = [f'parse hook rule config "{path}"']
    detail: list[str] = []
    if row > 0:
        detail.append(f"line {row}, column {column or 1}")
    if key:
        detail.append(f'field "{key}"')
    if context:
        detail.append(context)
    prefix = ("- " + ", ".join(detail) + ": ") if detail else "- "
    pieces.append(prefix + str(err).strip())
    snippet = source_line_snippet(data, row, column) if row > 0 else ""
    if snippet:
        pieces.append(indent_lines(snippet, "  "))
    if key and row > 0:
        conflict = find_toml_array_table_conflict(data, key)
        if conflict[2] > 0:
            pieces.append(f'  （"{key}" 已在第 {conflict[2]} 行出现过）')
    return ConfigParseError("\n".join(pieces), err)


def detailed_generic_toml_parse_error(
    path: str, data: str, err: BaseException
) -> AtriXError | None:
    """键与 ``[[数组表]]`` 冲突时的兜底详情。对应 Go ``detailedGenericTOMLParseError``。"""
    message = str(err)
    if "already exists as a" not in message and "twice" not in message:
        return None
    key = conflicting_toml_key(message)
    if not key:
        return None
    row, column, previous_row, context = find_toml_array_table_conflict(data, key)
    if row == 0:
        return None
    pieces = [f'parse hook rule config "{path}"']
    detail = f'- line {row}, column {column}, field "{key}"'
    if context:
        detail += f", {context}"
    detail += f": {message}"
    if previous_row > 0:
        detail += f'；"{key}" 已在第 {previous_row} 行设置过'
    pieces.append(detail)
    snippet = source_line_snippet(data, row, column)
    if snippet:
        pieces.append(indent_lines(snippet, "  "))
    return ConfigParseError("\n".join(pieces), err)


def conflicting_toml_key(message: str) -> str:
    """从 tomllib 的错误文本里提取冲突键名。对应 Go ``conflictingTOMLKey``。"""
    for pattern in _KEY_PATTERNS:
        match = pattern.search(message or "")
        if match and match.group(1):
            return match.group(1)
    return ""


def find_toml_array_table_conflict(
    data: str, key: str
) -> tuple[int, int, int, str]:
    """定位"标量键 ↔ 数组表"冲突的行与上下文。

    对应 Go ``findTOMLArrayTableConflict``，返回
    ``(row, column, previous_row, context)``。
    """
    lines = data.split("\n")
    rule_name = ""
    seen_key_line = 0
    seen_array_table_line = 0
    for index, line in enumerate(lines):
        line_no = index + 1
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if trimmed == "[[rules]]":
            rule_name = ""
            seen_key_line = 0
            seen_array_table_line = 0
            continue
        name, value, ok = parse_toml_string_assignment(trimmed)
        if ok and name == "name":
            rule_name = value
        if parse_toml_assignment_key(trimmed) == key:
            if seen_array_table_line > 0:
                return (
                    line_no,
                    line_column(line, key),
                    seen_array_table_line,
                    rule_context(rule_name),
                )
            seen_key_line = line_no
            continue
        if is_toml_array_table_for_key(trimmed, key):
            if seen_key_line > 0:
                return (
                    line_no,
                    line_column(line, "[["),
                    seen_key_line,
                    rule_context(rule_name),
                )
            seen_array_table_line = line_no
    return 0, 0, 0, ""


def parse_toml_assignment_key(line: str) -> str:
    """取 ``key = value`` 里的键名。对应 Go ``parseTomlAssignmentKey``。"""
    key, sep, _value = line.partition("=")
    if not sep:
        return ""
    key = key.strip()
    if not key or key.startswith("["):
        return ""
    return key


def is_toml_array_table_for_key(line: str, key: str) -> bool:
    """该行是否是 ``[[key]]``（含 ``[[rules.key]]``）。对应 Go 同名函数。"""
    return (
        line == "[[" + key + "]]"
        or line.endswith("." + key + "]]")
        or line.endswith("." + key + "]] ")
    )


def rule_context(name: str) -> str:
    """对应 Go ``ruleContext``。"""
    if not (name or "").strip():
        return "rule"
    return f'rule "{name}"'


def line_column(line: str, needle: str) -> int:
    """needle 在该行的 1 基列号。对应 Go ``lineColumn``。"""
    index = line.find(needle)
    return index + 1 if index >= 0 else 1


def source_line_snippet(data: str, row: int, column: int) -> str:
    """源码片段 + 插入符。对应 Go ``sourceLineSnippet``。"""
    lines = data.split("\n")
    if row <= 0 or row > len(lines):
        return ""
    line = lines[row - 1]
    if column <= 0:
        column = 1
    return f"{row}| {line}\n | {' ' * (column - 1)}^"


def toml_context_at_line(data: str, row: int) -> str:
    """该行所属的小节/规则名。对应 Go ``tomlContextAtLine``。"""
    if row <= 0:
        return ""
    lines = data.split("\n")
    if row > len(lines):
        row = len(lines)
    section = ""
    rule_name = ""
    plugin_name = ""
    for index in range(row):
        line = lines[index].strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[rules]]":
            section = "rule"
            rule_name = ""
        elif line == "[[plugins]]":
            section = "plugin_ref"
            plugin_name = ""
        elif line == "[plugin]":
            section = "plugin_info"
        elif line.startswith("[rules.") or line.startswith("[[rules."):
            section = "rule"
        elif line.startswith("["):
            section = line
        key, value, ok = parse_toml_string_assignment(line)
        if not ok or key != "name":
            continue
        if section == "rule":
            rule_name = value
        elif section == "plugin_ref":
            plugin_name = value
    if section == "rule":
        return f'rule "{rule_name}"' if rule_name else "rule"
    if section == "plugin_ref":
        return f'plugin ref "{plugin_name}"' if plugin_name else "plugin ref"
    if section == "plugin_info":
        return "plugin metadata"
    if section.startswith("["):
        return "section " + section
    return ""


def parse_toml_string_assignment(line: str) -> tuple[str, str, bool]:
    """解析 ``key = "value"``（含行尾注释）。对应 Go ``parseTomlStringAssignment``。"""
    key, sep, value = line.partition("=")
    if not sep:
        return "", "", False
    key = key.strip()
    value = value.strip()
    comment = value.find(" #")
    if comment >= 0:
        value = value[:comment].strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return key, "", False
    return key, value.strip('"'), True


def indent_lines(text: str, prefix: str) -> str:
    """给非空行加前缀。对应 Go ``indentLines``。"""
    return "\n".join(
        (prefix + line) if line else line for line in text.split("\n")
    )


def locate_key_line(data: str, key: str, section: str = "") -> tuple[int, int]:
    """在源码里找 ``key =`` 的行列；找不到返回 ``(0, 0)``。"""
    if not data:
        return 0, 0
    current = ""
    for index, line in enumerate(data.split("\n")):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if trimmed.startswith("["):
            current = trimmed
            continue
        if section and section not in current:
            continue
        if parse_toml_assignment_key(trimmed) == key:
            return index + 1, line_column(line, key)
    return 0, 0


# ---------------------------------------------------------------------------
# 严格字段映射（替代 Go 的反射 + DisallowUnknownFields）
# ---------------------------------------------------------------------------
def strict_table(
    raw: Any,
    path: str,
    fields: Mapping[str, Callable[[Any, str], Any]],
    data: str = "",
) -> dict[str, Any]:
    """按 ``fields`` 严格映射一个 TOML 表。

    ``fields`` 之外的键一律抛 :class:`StrictDecodeError`（附源码行号）。
    缺失的键不会出现在返回值里，由调用方决定默认值。
    """
    if not isinstance(raw, Mapping):
        raise StrictDecodeError(f"{path}: 期望一个表（table）", row=0)
    source = data or _current_source()
    out: dict[str, Any] = {}
    for key in raw:
        if key not in fields:
            row, column = locate_key_line(source, str(key))
            context = toml_context_at_line(source, row) if row else ""
            raise StrictDecodeError(
                "unknown field",
                key=str(key),
                row=row,
                column=column,
                context=context,
            )
    for key, converter in fields.items():
        if key not in raw:
            continue
        try:
            out[key] = converter(raw[key], f"{path}.{key}")
        except StrictDecodeError as exc:
            if exc.row == 0 and not exc.key:
                row, column = locate_key_line(source, key)
                exc.row, exc.column = row, column
                exc.context = toml_context_at_line(source, row) if row else ""
                exc.key = key
            raise
    return out


def strict_array(raw: Any, path: str, item: Callable[[Any, str], T]) -> list[T]:
    """严格映射一个数组。"""
    if not isinstance(raw, list):
        raise StrictDecodeError(f"{path}: 期望一个数组（array）")
    return [item(value, f"{path}[{index}]") for index, value in enumerate(raw)]


def strict_string(value: Any, path: str) -> str:
    """严格取字符串。"""
    if not isinstance(value, str):
        raise StrictDecodeError(f"{path}: 期望字符串，得到 {_toml_type(value)}")
    return value


def strict_bool(value: Any, path: str) -> bool:
    """严格取布尔。"""
    if not isinstance(value, bool):
        raise StrictDecodeError(f"{path}: 期望布尔值，得到 {_toml_type(value)}")
    return value


def strict_int(value: Any, path: str) -> int:
    """严格取整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrictDecodeError(f"{path}: 期望整数，得到 {_toml_type(value)}")
    return value


def strict_string_list(value: Any, path: str) -> list[str]:
    """严格取字符串数组。"""
    return strict_array(value, path, strict_string)


def _toml_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "浮点"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, Mapping):
        return "表"
    return type(value).__name__


def _error_position(err: BaseException) -> tuple[int, int]:
    """尽量取到 tomllib 错误的行列（不同小版本属性不同）。"""
    row = getattr(err, "lineno", 0) or 0
    column = getattr(err, "colno", 0) or 0
    if row:
        return int(row), int(column or 1)
    match = re.search(r"\(at line (\d+), column (\d+)\)", str(err))
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"\(at end of document\)", str(err))
    if match:
        return 0, 0
    return 0, 0


# 交叉检索：Go 里 ``decodeTOMLFile`` 用 os.ReadFile。
_OS = os
