"""缓存身份与请求指纹。

对应 Go：``internal/vision/fingerprint.go``。

两个哈希都刻意走「按 Go struct 序列化 → SHA-256」，而不是拼字符串，这样
增删字段或调整字段顺序都是一次显式改动，不会偶然产出有歧义或残缺的键。

**秘密（API key）绝不能出现在这里**：凭据/账号变更用 ``credential_epoch``
让旧结果失效，而不是把凭据本身放进键。

Python 侧没有 ``json.Marshal`` 的逐字节行为（字段顺序、无空格、HTML 转义、
``omitempty``），而缓存键必须稳定可比，所以这里自带一个最小复刻：
``_go_marshal_*`` 只覆盖本模块用到的类型（int / float / str），并在 docstring
里写明与 Go 的对齐点。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "CacheIdentity",
    "CacheSchemaVersion",
    "RequestFingerprint",
    "hash_text",
]

#: 缓存条目语义变化时递增。它属于每一个 key，所以升级永远不会命中旧格式结果。
CacheSchemaVersion = 1


def _go_quote(value: str) -> str:
    """Go ``encoding/json`` 的字符串转义（``escapeHTML=true``）。"""
    out: list[str] = ['"']
    for ch in value:
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
        elif ch == "<":
            out.append("\\u003c")
        elif ch == ">":
            out.append("\\u003e")
        elif ch == "&":
            out.append("\\u0026")
        elif ch in ("\u2028", "\u2029"):
            out.append("\\u%04x" % code)
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _go_float(value: float) -> str:
    """Go ``encoding/json`` 的 float64 编码。

    Go 用 ``strconv.AppendFloat(-1)``（最短可回读表示），当指数 < -6 或
    >= 21 时用 ``'e'`` 格式，否则用 ``'f'`` 格式；整数值不带小数点。
    Python 的 ``repr`` 也输出最短表示，只是切换指数格式的阈值不同，所以这里
    用 ``Decimal`` 做一次 ``'f'`` 归一化。
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("json: unsupported value: NaN or Inf")
    if number == 0:
        return "0"
    text = repr(number)
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f")
    if 1e-6 <= abs(number) < 1e21:
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    # 指数区间：Python 与 Go 的 'e' 输出一致（如 1e-07、1.5e+21）。
    return repr(number)


def _go_json_object(pairs: list[tuple[str, Any]]) -> str:
    """按给定键顺序编码一个扁平 JSON 对象（键名由调用方保证已按 Go tag 命名）。"""
    parts: list[str] = []
    for key, value in pairs:
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, int):
            encoded = str(value)
        elif isinstance(value, float):
            encoded = _go_float(value)
        else:
            encoded = _go_quote(str(value))
        parts.append(_go_quote(key) + ":" + encoded)
    return "{" + ",".join(parts) + "}"


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheIdentity:
    """一个可缓存结果的显式、带版本的身份。

    对应 Go ``CacheIdentity``。JSON 键名与 ``omitempty`` 逐字对齐 Go struct tag
    （``endpoint`` / ``credential_epoch`` 零值时不出现）。
    """

    schema_version: int = 0
    media_id: str = ""
    provider: str = ""
    endpoint: str = ""
    model: str = ""
    prompt_version: str = ""
    prompt_hash: str = ""
    preprocess_version: str = ""
    request_hash: str = ""
    credential_epoch: int = 0

    def to_json(self) -> str:
        """对应 Go 的 ``json.Marshal(id)``。"""
        pairs: list[tuple[str, Any]] = [
            ("schema_version", int(self.schema_version)),
            ("media_id", self.media_id),
            ("provider", self.provider),
        ]
        if self.endpoint:
            pairs.append(("endpoint", self.endpoint))
        pairs.append(("model", self.model))
        pairs.append(("prompt_version", self.prompt_version))
        pairs.append(("prompt_hash", self.prompt_hash))
        pairs.append(("preprocess_version", self.preprocess_version))
        pairs.append(("request_hash", self.request_hash))
        if self.credential_epoch:
            pairs.append(("credential_epoch", int(self.credential_epoch)))
        return _go_json_object(pairs)

    def key(self) -> str:
        """返回十六进制 SHA-256 缓存键。对应 Go ``CacheIdentity.Key``。"""
        return _sha256_hex(self.to_json())


def hash_text(value: str) -> str:
    """一段文本的稳定哈希（用于 prompt 内容）。对应 Go ``hashText``。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class RequestFingerprint:
    """请求级、会改变结果的参数指纹。对应 Go ``requestFingerprint``。

    只影响日志或路由的东西必须留在外面，否则改一次日志级别就会让缓存失效。
    """

    max_tokens: int = 0
    temperature: float = 0.0
    max_edge: int = 0
    max_image_bytes: int = 0
    output_mime_type: str = ""

    def to_json(self) -> str:
        return _go_json_object(
            [
                ("max_tokens", int(self.max_tokens)),
                ("temperature", float(self.temperature)),
                ("max_edge", int(self.max_edge)),
                ("max_image_bytes", int(self.max_image_bytes)),
                ("output_mime_type", self.output_mime_type),
            ]
        )

    def hash(self) -> str:
        """返回十六进制 SHA-256 指纹。对应 Go ``requestFingerprint.hash``。"""
        return _sha256_hex(self.to_json())
