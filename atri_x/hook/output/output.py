"""Hook 输出的线格式：``outputs`` 段的解码与构造。

对应 Go：``internal/hook/output/output.go``。

Go 侧用 ``encoding/json`` 的 ``DisallowUnknownFields`` 严格解码；Python 侧面
向 :mod:`atri_x.hook.output.jsonstrict` 的严格工具实现同样语义。
"""

from __future__ import annotations

import base64 as _base64
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence, TypeVar
from urllib.parse import urlparse

from atri_x.delivery import delivery
from atri_x.errors import AtriXError
from atri_x.hook.output.jsonstrict import (
    JSONDecodeError,
    decode_json,
    loads_strict,
    optional_bool,
    optional_int,
    optional_number,
    optional_string,
    require_bool,
    require_int,
    require_list,
    require_number,
    require_object,
    require_string,
    require_string_list,
    strict_object,
)
from atri_x.media.types import IDPrefix, valid_id

T = TypeVar("T")

__all__ = [
    "MAX_BASE64_BYTES",
    "MaxBase64Bytes",
    "OutputError",
    "JSONDecodeError",
    "Segment",
    "Target",
    "Group",
    "BuildOptions",
    "decode_json",
    "loads_strict",
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
    "build_group",
    "build_segment",
    "build_media_source",
    "trim_segment",
    "reject_fields",
]

#: 内联 base64 的**解码后**字节上限。对应 Go ``MaxBase64Bytes``。
MAX_BASE64_BYTES = 10 * 1024 * 1024
#: Go 导出名别名。
MaxBase64Bytes = MAX_BASE64_BYTES

_BASE64_TOO_LARGE = (
    "base64 output exceeds 10 MiB decoded limit; write large media to a file and "
    "return outputs[].path or outputs[].url instead"
)


class OutputError(AtriXError):
    """``outputs`` 段构造/解码失败。对应 Go 里这一层的 ``fmt.Errorf``。"""


@dataclass
class Segment:
    """一条 ``[[rules.actions.outputs]]`` 规格。对应 Go ``Segment``。

    TOML 与 JSON 键名逐字照抄 Go 的 struct tag。
    """

    kind: str = ""
    text: str = ""
    url: str = ""
    path: str = ""
    base64: str = ""
    name: str = ""
    mime_type: str = ""
    user_id: str = ""
    message_id: str = ""
    emoticon_id: str = ""

    # -- wire ---------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        """对齐 Go 的 json tag（``kind`` 必出，其余 ``omitempty``）。"""
        out: dict[str, Any] = {"kind": self.kind}
        for key, value in (
            ("text", self.text),
            ("url", self.url),
            ("path", self.path),
            ("base64", self.base64),
            ("name", self.name),
            ("mime_type", self.mime_type),
            ("user_id", self.user_id),
            ("message_id", self.message_id),
            ("emoticon_id", self.emoticon_id),
        ):
            if value:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, raw: Any, path: str = "segment") -> "Segment":
        """严格解码（未知字段报错）。"""
        return decode_json(raw, lambda obj: cls.from_object(obj, path))

    @classmethod
    def from_object(cls, obj: Any, path: str = "segment") -> "Segment":
        values = strict_object(
            obj,
            path,
            {
                "kind": require_string,
                "text": require_string,
                "url": require_string,
                "path": require_string,
                "base64": require_string,
                "name": require_string,
                "mime_type": require_string,
                "user_id": require_string,
                "message_id": require_string,
                "emoticon_id": require_string,
            },
        )
        return cls(**values)


@dataclass
class Target:
    """``outputs.target``：这条输出发到哪里。对应 Go ``Target``。"""

    platform: str = ""
    scope_id: str = ""
    private_user_id: str = ""
    group_id: str = ""
    superadmins: bool = False

    def empty(self) -> bool:
        """对应 Go ``Target.Empty``。"""
        return (
            not self.platform.strip()
            and not self.scope_id.strip()
            and not self.private_user_id.strip()
            and not self.group_id.strip()
            and not self.superadmins
        )

    def delivery(self) -> delivery.Target:
        """投影到交付层的 :class:`atri_x.delivery.delivery.Target`。"""
        return delivery.Target(
            platform=self.platform.strip(),
            scope_id=self.scope_id.strip(),
            private_user_id=self.private_user_id.strip(),
            group_id=self.group_id.strip(),
            superadmins=self.superadmins,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in (
            ("platform", self.platform),
            ("scope_id", self.scope_id),
            ("private_user_id", self.private_user_id),
            ("group_id", self.group_id),
        ):
            if value:
                out[key] = value
        if self.superadmins:
            out["superadmins"] = True
        return out

    @classmethod
    def from_json(cls, raw: Any, path: str = "target") -> "Target":
        return decode_json(raw, lambda obj: cls.from_object(obj, path))

    @classmethod
    def from_object(cls, obj: Any, path: str = "target") -> "Target":
        values = strict_object(
            obj,
            path,
            {
                "platform": require_string,
                "scope_id": require_string,
                "private_user_id": require_string,
                "group_id": require_string,
                "superadmins": require_bool,
            },
        )
        return cls(**values)


@dataclass
class Group:
    """一段 ``outputs`` 列表 + 目标 + 时机。对应 Go ``Group``。"""

    outputs: list[Segment] = field(default_factory=list)
    target: Target = field(default_factory=Target)
    timing: str = ""

    @classmethod
    def from_json(cls, raw: Any, path: str = "group") -> "Group":
        return decode_json(raw, lambda obj: cls.from_object(obj, path))

    @classmethod
    def from_object(cls, obj: Any, path: str = "group") -> "Group":
        values = strict_object(
            obj,
            path,
            {
                "outputs": lambda value, where: [
                    Segment.from_object(item, f"{where}[{index}]")
                    for index, item in enumerate(require_list(value, where))
                ],
                "target": Target.from_object,
                "timing": require_string,
            },
        )
        return cls(**values)


@dataclass
class BuildOptions:
    """构造一组输出时的缺省值。对应 Go ``BuildOptions``。"""

    base_dir: str = ""
    default_target: Target = field(default_factory=Target)
    default_timing: str = ""


def build_group(group: Group, opts: BuildOptions) -> list[delivery.Output]:
    """把一组规格构造成交付输出。对应 Go ``BuildGroup``；失败抛 :class:`AtriXError`。"""
    target = group.target
    if target.empty():
        target = opts.default_target
    timing = (group.timing or "").strip()
    if not timing:
        timing = (opts.default_timing or "").strip()
    delivery.validate_delivery_timing(timing)
    outputs: list[delivery.Output] = []
    for index, spec in enumerate(group.outputs):
        try:
            outputs.append(build_segment(spec, opts.base_dir, target.delivery(), timing))
        except AtriXError as exc:
            raise type(exc)(f"outputs[{index}]: {exc}") from exc
    return outputs


def build_segment(
    spec: Segment, base_dir: str, target: delivery.Target, timing: str
) -> delivery.Output:
    """构造单条输出。对应 Go ``buildSegment``。"""
    spec = trim_segment(spec)
    kind = spec.kind or delivery.Kind.TEXT
    if kind == delivery.Kind.TEXT:
        reject_fields(
            (
                ("url", spec.url),
                ("path", spec.path),
                ("base64", spec.base64),
                ("name", spec.name),
                ("mime_type", spec.mime_type),
                ("user_id", spec.user_id),
                ("message_id", spec.message_id),
                ("emoticon_id", spec.emoticon_id),
            )
        )
        out = delivery.text(spec.text)
    elif kind in (delivery.Kind.IMAGE, delivery.Kind.FILE, delivery.Kind.RECORD):
        reject_fields(
            (
                ("user_id", spec.user_id),
                ("message_id", spec.message_id),
                ("emoticon_id", spec.emoticon_id),
            )
        )
        source = build_media_source(spec, base_dir)
        out = delivery.Output(
            kind=kind, text=spec.text, name=spec.name, source=source
        )
    elif kind == delivery.Kind.EMOTICON:
        reject_fields(
            (
                ("url", spec.url),
                ("path", spec.path),
                ("base64", spec.base64),
                ("mime_type", spec.mime_type),
                ("user_id", spec.user_id),
                ("message_id", spec.message_id),
            )
        )
        if not spec.emoticon_id:
            raise OutputError("emoticon_id is required for emoticon output")
        out = delivery.emoticon(spec.emoticon_id, spec.name, spec.text)
    elif kind == delivery.Kind.AT:
        reject_fields(
            (
                ("text", spec.text),
                ("url", spec.url),
                ("path", spec.path),
                ("base64", spec.base64),
                ("name", spec.name),
                ("mime_type", spec.mime_type),
                ("message_id", spec.message_id),
                ("emoticon_id", spec.emoticon_id),
            )
        )
        if not spec.user_id:
            raise OutputError("user_id is required for at output")
        out = delivery.at(spec.user_id)
    elif kind == delivery.Kind.REPLY:
        reject_fields(
            (
                ("url", spec.url),
                ("path", spec.path),
                ("base64", spec.base64),
                ("name", spec.name),
                ("mime_type", spec.mime_type),
                ("user_id", spec.user_id),
                ("emoticon_id", spec.emoticon_id),
            )
        )
        if not spec.message_id:
            raise OutputError("message_id is required for reply output")
        out = delivery.reply(spec.message_id, spec.text)
    else:
        raise OutputError(f"unsupported output kind {spec.kind!r}")
    out.target = target
    return delivery.with_delivery_timing(out, timing)


def build_media_source(spec: Segment, base_dir: str) -> delivery.Source:
    """构造图片/文件/语音来源。对应 Go ``buildMediaSource``。"""
    provided = [value for value in (spec.path, spec.url, spec.base64) if value]
    if len(provided) != 1:
        raise OutputError(
            "image/file/record output must provide exactly one of path, url or base64"
        )
    source = delivery.Source(mime_type=spec.mime_type)
    for value in (spec.path, spec.url, spec.base64):
        if value.lower().startswith(IDPrefix):
            if not valid_id(value):
                raise OutputError("invalid media ID")
            source.media_id = value
            return source
    if spec.path:
        if "://" in spec.path:
            raise OutputError("path must be a filesystem path, not a URI")
        source.path = spec.path
        if not os.path.isabs(source.path) and base_dir.strip():
            source.path = os.path.join(base_dir, source.path)
        return source
    if spec.url:
        parsed = urlparse(spec.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise OutputError("url must be an absolute HTTP(S) URL")
        source.url = spec.url
        return source
    encoded = _strip_base64_whitespace(spec.base64)
    if _decoded_len(len(encoded)) > MAX_BASE64_BYTES:
        raise OutputError(_BASE64_TOO_LARGE)
    try:
        data = _base64.b64decode(encoded, validate=True)
    except (ValueError, _base64.binascii.Error) as exc:
        raise OutputError(f"decode base64 output: {exc}") from exc
    if len(data) > MAX_BASE64_BYTES:
        raise OutputError(_BASE64_TOO_LARGE)
    source.data = data
    return source


def trim_segment(spec: Segment) -> Segment:
    """去掉全部字符串字段的首尾空白。对应 Go ``trimSegment``。"""
    return Segment(
        kind=spec.kind.strip(),
        text=spec.text.strip(),
        url=spec.url.strip(),
        path=spec.path.strip(),
        base64=spec.base64.strip(),
        name=spec.name.strip(),
        mime_type=spec.mime_type.strip(),
        user_id=spec.user_id.strip(),
        message_id=spec.message_id.strip(),
        emoticon_id=spec.emoticon_id.strip(),
    )


def reject_fields(pairs: Sequence[tuple[str, str]]) -> None:
    """该输出类型不支持的字段一旦非空就报错。对应 Go ``rejectFields``。"""
    for name, value in pairs:
        if value:
            raise OutputError(f"field {name} is not supported for this output kind")


def _strip_base64_whitespace(value: str) -> str:
    """Go 的 base64 解码器忽略 ``\\r`` / ``\\n``，这里先剔除再严格解码。"""
    if "\n" in value or "\r" in value or "\t" in value:
        return value.replace("\n", "").replace("\r", "").replace("\t", "")
    return value


def _decoded_len(encoded_len: int) -> int:
    """对应 Go ``base64.StdEncoding.DecodedLen``：``n/4*3``。"""
    return encoded_len // 4 * 3


#: 供规则层构造匿名结构体式的严格解码器使用。
STRICT_MAPPING = Mapping  # 仅作文档用途；真实类型见 jsonstrict.strict_object
FIELD_CONVERTER = Callable[[Any, str], Any]
