"""字节与文本之间的编码转换。

对应 Go：``internal/utils/fileops/encoding.go``。

Go 用 ``golang.org/x/text/encoding/htmlindex`` 查任意编码；Python 标准库自带
大量编解码器，因此 :func:`lookup_encoding` 直接映射到 ``codecs``。别名规则与
Go 一致：``gbk``/``gb2312`` → ``gb18030``，``shift_jis``/``sjis`` →
``shift-jis``。

与 Go 的 x/text 一样，非 UTF-8 编码解码时对无法映射的字节做替换（U+FFFD）
而不报错；UTF-8 路径仍然严格校验（Go 用 ``utf8.Valid`` 显式拒绝非法字节）。
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

__all__ = [
    "Codec",
    "decode_bytes",
    "decode_with_encoding",
    "encode_text",
    "is_utf8_encoding",
    "lookup_encoding",
]

BOM_UTF8 = b"\xef\xbb\xbf"
BOM_UTF16_LE = b"\xff\xfe"
BOM_UTF16_BE = b"\xfe\xff"

#: Python ``codecs`` 与 Go htmlindex 名称的差异映射。
_ALIASES = {
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "shift-jis": "shift_jis",
    "utf-16le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "utf16le": "utf-16-le",
    "utf16be": "utf-16-be",
    "utf8": "utf-8",
    "utf-8-bom": "utf-8",
    "latin1": "latin-1",
    "iso-8859-1": "latin-1",
    "euc-kr": "euc_kr",
    "big5": "big5",
}


@dataclass
class Codec:
    """一个具名编解码器。对应 Go ``golang.org/x/text/encoding.Encoding``。"""

    name: str
    python_name: str

    def decode(self, data: bytes) -> str:
        return data.decode(self.python_name, errors="replace")

    def encode(self, text: str) -> bytes:
        return text.encode(self.python_name, errors="replace")


def is_utf8_encoding(name: str) -> bool:
    """是否是 UTF-8 家族（含带 BOM 的写法）。"""
    name = (name or "").strip().lower()
    return name in ("utf-8", "utf-8-bom")


def lookup_encoding(name: str) -> Codec:
    """按名查找编解码器；不支持时抛 ``ValueError``。"""
    name = (name or "").strip().lower()
    python_name = _ALIASES.get(name, name)
    try:
        codecs.lookup(python_name)
    except LookupError as exc:
        raise ValueError(f'unsupported encoding "{name}"') from exc
    return Codec(name=name, python_name=python_name)


def decode_bytes(data: bytes, requested: str) -> tuple[str, str, bytes | None]:
    """返回 ``(文本, 编码名, BOM)``。

    ``requested`` 为 ``""`` / ``"auto"`` 时按 BOM 自动判定；否则按名字解码。
    """
    name = (requested or "").strip().lower()
    if name in ("", "auto"):
        if data.startswith(BOM_UTF8):
            return _decode_utf8(data[3:], "utf-8-bom", BOM_UTF8)
        if data.startswith(BOM_UTF16_LE):
            return decode_with_encoding(data[2:], "utf-16le", BOM_UTF16_LE)
        if data.startswith(BOM_UTF16_BE):
            return decode_with_encoding(data[2:], "utf-16be", BOM_UTF16_BE)
        if not _valid_utf8(data):
            raise ValueError("file is not valid UTF-8; pass encoding explicitly")
        return data.decode("utf-8"), "utf-8", None
    if name == "utf8":
        name = "utf-8"
    if name == "utf-8-bom":
        if data.startswith(BOM_UTF8):
            data = data[3:]
        if not _valid_utf8(data):
            raise ValueError("file is not valid UTF-8")
        return data.decode("utf-8"), "utf-8-bom", BOM_UTF8
    if name == "utf-8":
        if data.startswith(BOM_UTF8):
            data = data[3:]
        if not _valid_utf8(data):
            raise ValueError("file is not valid UTF-8")
        return data.decode("utf-8"), "utf-8", None
    return decode_with_encoding(data, name, None)


def decode_with_encoding(
    data: bytes, name: str, bom: bytes | None
) -> tuple[str, str, bytes | None]:
    """用指定编码解码。"""
    codec = lookup_encoding(name)
    try:
        decoded = codec.decode(data)
    except UnicodeDecodeError as exc:  # pragma: no cover - errors="replace" 已兜底
        raise ValueError(f"decode {name}: {exc}") from exc
    return decoded, name, bom


def encode_text(text: str, name: str, bom: bytes | None = None) -> bytes:
    """把文本编码回字节（``utf-8`` 不写 BOM，``utf-8-bom`` 写 BOM）。"""
    if name == "utf-8":
        return text.encode("utf-8")
    if name == "utf-8-bom":
        return (bom or BOM_UTF8) + text.encode("utf-8")
    codec = lookup_encoding(name)
    encoded = codec.encode(text)
    if bom:
        return bytes(bom) + encoded
    return encoded


def _decode_utf8(data: bytes, name: str, bom: bytes | None) -> tuple[str, str, bytes | None]:
    if not _valid_utf8(data):
        raise ValueError("file is not valid UTF-8")
    return data.decode("utf-8"), name, bom


def _valid_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
