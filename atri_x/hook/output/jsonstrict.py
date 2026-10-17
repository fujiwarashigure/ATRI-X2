"""严格 JSON 解码：拒绝未知字段、拒绝尾随值。

对应 Go：``internal/hook/output/output.go`` 里的 ``DecodeJSON`` 与
``encoding/json`` 的 ``Decoder.DisallowUnknownFields`` 语义。

Go 用反射把 JSON 解到结构体上，并在解码器上打开 ``DisallowUnknownFields``；
Python 的 ``json`` 没有这个能力，所以这里把"结构体"表示成
``{json 键: 取值函数}`` 的映射，由 :func:`strict_object` 负责逐字段严格取值：
出现映射之外的键一律报 :class:`JSONDecodeError`。

这些工具被 ``atri_x.hook.output``、``atri_x.hook.rules`` 与
``atri_x.hook.runtime`` 共用（Go 侧它们分别调用 ``hookoutput.DecodeJSON``）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, TypeVar

from atri_x.errors import AtriXError

T = TypeVar("T")

__all__ = [
    "JSONDecodeError",
    "loads_strict",
    "decode_json",
    "strict_object",
    "require_string",
    "require_bool",
    "require_int",
    "require_number",
    "require_object",
    "require_string_list",
    "require_list",
    "optional_string",
    "optional_bool",
    "optional_int",
    "optional_number",
]


class JSONDecodeError(AtriXError):
    """JSON 解码失败（非法 JSON、尾随值、类型不符、未知字段）。

    对应 Go 里 ``encoding/json`` 返回的错误值 —— 上游这些错误都是
    ``fmt.Errorf`` / ``json`` 包错误，不是哨兵。
    """


def _skip_ws(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] in " \t\n\r":
        index += 1
    return index


def loads_strict(raw: str | bytes | bytearray | None) -> Any:
    """解码一段 JSON，并要求整段输入**恰好**是一个 JSON 值。

    对应 Go ``json.Unmarshal`` / ``DecodeJSON``：空输入与尾随值都算错误。
    """
    if raw is None:
        raise JSONDecodeError("unexpected end of JSON input")
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JSONDecodeError(f"invalid UTF-8 in JSON input: {exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise JSONDecodeError(f"unsupported JSON input type {type(raw).__name__}")
    if not text.strip():
        raise JSONDecodeError("unexpected end of JSON input")
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text, _skip_ws(text, 0))
    except ValueError as exc:
        raise JSONDecodeError(f"invalid JSON: {exc}") from exc
    if text[end:].strip():
        raise JSONDecodeError("unexpected trailing JSON value")
    return value


def decode_json(raw: str | bytes | bytearray | None, decode: Callable[[Any], T]) -> T:
    """先 :func:`loads_strict`，再把结果交给 ``decode`` 做严格字段映射。

    ``decode`` 抛出的异常原样传播（与 Go 里 ``Decode`` 直接返回错误一致）。
    """
    return decode(loads_strict(raw))


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def strict_object(
    obj: Any,
    path: str,
    fields: Mapping[str, Callable[[Any, str], Any]],
) -> dict[str, Any]:
    """按 ``fields`` 严格映射一个 JSON object。

    未知键报 ``json: unknown field "x"``；``fields`` 里的键在输入中**缺失**时
    不会出现在返回值里（由调用方决定零值），出现时调用 ``converter(value, path)``。
    """
    if not isinstance(obj, dict):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(obj)} into object"
        )
    out: dict[str, Any] = {}
    for key in obj:
        if key not in fields:
            raise JSONDecodeError(f'{path}: json: unknown field "{key}"')
    for key, converter in fields.items():
        if key not in obj:
            continue
        out[key] = converter(obj[key], f"{path}.{key}")
    return out


def require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into string"
        )
    return value


def require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into bool"
        )
    return value


def require_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise JSONDecodeError(f"{path}: cannot unmarshal bool into int")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise JSONDecodeError(
        f"{path}: cannot unmarshal {_type_name(value)} into int"
    )


def require_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into number"
        )
    return float(value)


def require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into object"
        )
    return value


def require_string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into array"
        )
    return [require_string(item, f"{path}[{index}]") for index, item in enumerate(value)]


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise JSONDecodeError(
            f"{path}: cannot unmarshal {_type_name(value)} into array"
        )
    return list(value)


# -- 指针字段（Go 的 ``*T``：缺失与零值必须区分） ---------------------------
def optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return require_string(value, path)


def optional_bool(value: Any, path: str) -> bool | None:
    if value is None:
        return None
    return require_bool(value, path)


def optional_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return require_int(value, path)


def optional_number(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return require_number(value, path)
