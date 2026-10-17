"""内置工具的公共环境读取与共享小助手。

对应 Go：``internal/tool/builtin/env.go``（环境读取），并把 Go 散落在
``file_guard.go`` / ``file_manager.go`` / ``artist_tools.go`` / ``cron.go`` 里的
几个无状态小助手集中到这里（``absClean`` / ``samePath`` / ``firstFileGuard`` /
``firstString`` / ``safeFileName`` / ``firstNonEmptyString`` / ``appendWarnings`` /
``decodeArgs`` / ``jsonResult`` / ``textResult`` / ``actorFromContext`` /
``stringPtrIfNotEmpty``）。

ctx 在本仓是鸭子类型 ``Mapping``（``None`` 视为空上下文），键是模块级私有哨兵。
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Sequence

from atri_x.config import config_env
from atri_x.errors import InvalidError
from atri_x.security import Actor, RoleUser, actor_from_context
from atri_x.tool.capability import ctx_value, ctx_with_value

_CONFIG_ENV_DIR_KEY = object()


def with_config_env_dir(ctx: Any, dir: str) -> Any:
    """把配置目录写进 ctx。对应 Go ``WithConfigEnvDir``。"""
    return ctx_with_value(ctx, _CONFIG_ENV_DIR_KEY, dir)


def config_env_dir(ctx: Any) -> str:
    """从 ctx 取配置目录。对应 Go ``configEnvDir``。"""
    if ctx is None:
        return ""
    value = ctx_value(ctx, _CONFIG_ENV_DIR_KEY)
    return value if isinstance(value, str) else ""


def builtin_env(ctx: Any, key: str) -> str:
    """取必填环境变量，缺则抛异常。对应 Go ``builtinEnv``。"""
    value, ok = optional_builtin_env(ctx, key)
    if ok:
        return value
    raise InvalidError(f"{key} is required; set it in environment or config .env")


def optional_builtin_env(ctx: Any, key: str) -> tuple[str, bool]:
    """取可选环境变量。对应 Go ``optionalBuiltinEnv``。

    系统环境变量优先，其次配置目录 ``.env``；空串视为未设置。
    """
    value = config_env(key, config_env_dir(ctx))
    if value is None:
        return "", False
    value = value.strip()
    if value == "":
        return "", False
    return value, True


# ---------------------------------------------------------------------------
# 共享小助手
# ---------------------------------------------------------------------------
def cancel_token_from_ctx(ctx: Any) -> Any:
    """从工具 ctx 取可用于同步 I/O 的取消令牌。

    Go 里 ``ctx`` 本身就是 ``context.Context``；本仓的工具 ctx 既可能是
    :class:`atri_x.safecontext.CancelToken`（带 ``is_cancelled`` / ``error``），
    也可能只是不可变 Mapping。后一种情况返回 ``None``——给
    ``fileops`` 这类"ctx 可选为取消令牌"的同步 API 使用。
    """
    if ctx is None:
        return None
    return ctx if callable(getattr(ctx, "is_cancelled", None)) else None


def go_quote(text: str) -> str:
    """近似 Go ``fmt %q``／``strconv.Quote``：双引号包裹并转义。

    仅覆盖可打印 ASCII 与常见控制字符；其余按 ``\\xNN`` / ``\\uNNNN`` 输出，
    保证输出永远是 ASCII，避免 GBK 控制台渲染问题。
    """
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        elif code < 0x100:
            out.append(f"\\x{code:02x}")
        elif code < 0x10000:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    out.append('"')
    return "".join(out)


def abs_clean(path: str) -> str:
    """绝对化 + 规范化；空路径抛异常。对应 Go ``absClean``。"""
    path = (path or "").strip()
    if path == "":
        raise InvalidError("path is required")
    return os.path.normpath(os.path.abspath(path))


def same_path(a: str, b: str) -> bool:
    """判断两个路径是否指向同一处。对应 Go ``samePath``（大小写不敏感）。"""
    if a == b:
        return True
    return _slash_lower(a) == _slash_lower(b)


def _slash_lower(value: str) -> str:
    return value.replace("\\", "/").lower()


def first_file_guard(values: Sequence[Any]) -> Any:
    """对应 Go ``firstFileGuard``：取第一个 FileGuard，没有则 None。"""
    if not values:
        return None
    return values[0]


def first_string(values: Sequence[str]) -> str:
    """对应 Go ``firstString``：取第一个非空串（去空白后）。"""
    if not values:
        return ""
    return (values[0] or "").strip()


def first_non_empty_string(values: Iterable[str]) -> str:
    """对应 Go ``firstNonEmptyString``。"""
    for value in values:
        if (value or "").strip() != "":
            return value.strip()
    return ""


def safe_file_name(name: str) -> str:
    """对应 Go ``safeFileName``：取 basename，退化到 ``file``。"""
    base = os.path.basename((name or "").strip())
    if base in (".", os.sep, "..", ""):
        return "file"
    return base


def append_warnings(result: Any, *warnings: str) -> None:
    """把非空、去重后的 warning 追加到 Result。对应 Go ``appendWarnings``。"""
    existing = result.warnings
    for warning in warnings:
        warning = (warning or "").strip()
        if warning == "" or warning in existing:
            continue
        existing.append(warning)


# ---------------------------------------------------------------------------
# 工具包级小助手（Go 定义在 cron.go，被 character / view_image 等文件复用）
# ---------------------------------------------------------------------------
def decode_args(raw: str, kind: type, tool_name: str = "cron") -> Any:
    """解析工具入参：空入参返回零值。对应 Go ``decodeArgs``。

    Go 的 ``json.Unmarshal`` 忽略未知键、类型不符时报错；这里保持"未知键忽略"，
    类型交给各工具自己按需使用（与既有 ``chat_history._decode`` 口径一致）。
    """
    out = kind()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"parse {tool_name} arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidError(f"parse {tool_name} arguments: expected a JSON object")
    for key, value in data.items():
        if not hasattr(out, key):
            continue
        setattr(out, key, value)
    return out


def json_result(value: Any) -> Any:
    """把值序列化成紧凑 JSON 并包成 ``Result``。对应 Go ``jsonResult``。

    Go 的 ``json.Marshal`` 会转义 ``<`` / ``>`` / ``&`` 并按字典序排 map 键；
    这里逐条复刻，保证输出与 Go 逐字节一致。
    """
    from atri_x.tool.tool import Result

    return Result(content=go_json(value))


def go_json(value: Any) -> str:
    """近似 Go ``json.Marshal`` 的紧凑输出（含 HTML 转义与 map 键排序）。"""
    text = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )


def _jsonable(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def text_result(text: str) -> Any:
    """把文本去空白后包成 ``Result``。对应 Go ``textResult``。"""
    from atri_x.tool.tool import Result

    return Result(content=(text or "").strip())


def default_actor(ctx: Any) -> Actor:
    """取 ctx 里的 actor，缺失时回落普通用户。对应 Go ``actorFromContext``。"""
    actor, ok = actor_from_context(ctx)
    if not ok or actor is None:
        return Actor(role=RoleUser)
    return actor

def string_ptr_if_not_empty(value: str) -> str | None:
    """空串 → ``None``（Go 的 ``stringPtrIfNotEmpty`` 的 ``*string`` 语义）。"""
    if value == "":
        return None
    return value


__all__ = [name for name in dir() if not name.startswith("_")]
