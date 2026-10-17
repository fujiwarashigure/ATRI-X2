"""平台配置解码与命令前缀/唤醒词处理。

对应 Go：``internal/platform/config.go``。

Go 用 ``toml.Marshal`` + ``toml.Unmarshal`` 把 ``[platform.<name>]`` 小节灌进
适配器自己的 config struct。Python 没有 struct tag 解码，因此
:func:`decode_config` 直接按字段名赋值：key 先归一为 snake_case，与目标对象的
属性名大小写不敏感地匹配，未知键按 Go 默认行为忽略。

``atri_x/net/tomlw.py`` 未落地时不依赖它：本模块只接受已经解析好的
``dict``（app 层从 app.toml 读出来的那一层）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from atri_x.errors import InvalidError


def decode_config(raw: Mapping[str, Any] | None, out: Any) -> None:
    """把 ``[platform.<name>]`` 原始小节解码进适配器自己的配置对象。

    对应 Go ``DecodeConfig``；``out`` 为 None 或 ``raw`` 为空时按 Go 语义直接返回。
    """
    if out is None:
        raise InvalidError("platform config output is nil")
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise InvalidError("decode platform config: section must be a table")
    fields: dict[str, str] = {}
    for name in dir(out):
        if name.startswith("_"):
            continue
        fields.setdefault(name.lower(), name)
    for key, value in raw.items():
        attr = fields.get(str(key).lower())
        if attr is None:
            continue
        try:
            setattr(out, attr, value)
        except AttributeError as exc:
            raise InvalidError(f"decode platform config: {exc}") from exc


def primary_command_prefix(prefixes: Sequence[str] | None) -> str:
    """返回平台适配器应使用的主命令前缀（配置里第一个非空项）。

    对应 Go ``PrimaryCommandPrefix``。
    """
    for prefix in prefixes or []:
        prefix = (prefix or "").strip()
        if prefix:
            return prefix
    return "/"


def strip_trigger_keyword(text: str, keywords: Sequence[str] | None) -> tuple[str, bool]:
    """只剥掉命中的唤醒词前缀。对应 Go ``StripTriggerKeyword``。"""
    text = (text or "").strip()
    for keyword in keywords or []:
        keyword = (keyword or "").strip()
        if keyword and text.startswith(keyword):
            return text[len(keyword):].strip(), True
    return text, False


def has_command_prefix(text: str, prefixes: Sequence[str] | None) -> bool:
    """对应 Go ``HasCommandPrefix``。"""
    return command_name(text, prefixes)[1]


def command_name(text: str, prefixes: Sequence[str] | None) -> tuple[str, bool]:
    """取命令名。对应 Go ``CommandName``。

    返回 ``(name, ok)``：``ok`` 为 False 表示不是命令；``name`` 可能为空
    （只写了前缀、还没写名字）。
    """
    text = (text or "").strip()
    for prefix in prefixes or []:
        prefix = (prefix or "").strip()
        if prefix == "" or not text.startswith(prefix):
            continue
        rest = text[len(prefix):].strip()
        if rest == "":
            return "", True
        name = rest.split(" ", 1)[0]
        return name.strip(), True
    return "", False


__all__ = [name for name in dir() if not name.startswith("_")]
