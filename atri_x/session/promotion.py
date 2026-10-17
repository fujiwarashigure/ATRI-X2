"""后台 Session 的前台接管判定与提升。对应 Go：``internal/session/promotion.go``。

后台任务（cron / atrinis）创建的 Session 被前台用户恢复时，需要**原地**变成当前
前台的普通 Session：记录接管前的后台来源、清掉后台身份、切到当前 scope 并回到
work 模式。在途的后台 turn 会在下一个安全点看到接管标记（
:data:`atri_x.errors.ErrForegroundSession`）并退出。

两条判定规则（与 Go 逐行一致）：

* :func:`is_background` —— metadata 有 ``background_kind``，或 ``platform_scope_id``
  以 ``cron:`` / ``atrinis:`` 开头；已被接管的 Session 不再是后台 Session。
* :func:`was_promoted` —— metadata 有 ``foreground_origin``，且其四个字段至少一个非空。

:func:`promote_to_foreground` **必须在** ``Sessions().Mutate`` 的事务内调用，否则
读-改-写会与并发写入互相覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from atri_x.session.shared import session_owner_id
from atri_x.session.types import Scope
from atri_x.storage.models import Session

__all__ = [
    "FOREGROUND_ORIGIN_KEY",
    "BACKGROUND_KIND_KEY",
    "ForegroundOrigin",
    "background_kind",
    "encode_metadata_fields",
    "is_background",
    "promote_to_foreground",
    "session_metadata_fields",
    "was_promoted",
]

#: metadata 里记录"接管前的后台来源"的键。对应 Go ``metadataKeyForegroundOrigin``。
FOREGROUND_ORIGIN_KEY = "foreground_origin"

#: metadata 里标记后台任务种类的键。对应 Go ``metadataKeyBackgroundKind``。
BACKGROUND_KIND_KEY = "background_kind"


@dataclass
class ForegroundOrigin:
    """Session 被前台接管前的后台来源。对应 Go ``session.ForegroundOrigin``。"""

    kind: str = ""
    owner_id: str = ""
    platform: str = ""
    scope_id: str = ""

    def to_json(self) -> str:
        """按 Go 的字段顺序与 ``omitempty`` 语义编码（空字段不输出）。"""
        payload: dict[str, str] = {}
        for key, value in (
            ("kind", self.kind),
            ("owner_id", self.owner_id),
            ("platform", self.platform),
            ("scope_id", self.scope_id),
        ):
            if value:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def was_promoted(row: Session | None) -> bool:
    """该 Session 是否已被前台接管（标记随 metadata 持久化）。对应 Go ``WasPromoted``。"""
    if row is None:
        return False
    raw = session_metadata_fields(row.metadata).get(FOREGROUND_ORIGIN_KEY)
    if not raw:
        return False
    try:
        origin = json.loads(raw)
    except ValueError:
        return False
    if origin is None:
        return False
    if not isinstance(origin, dict):
        return False
    values = []
    for key in ("kind", "owner_id", "platform", "scope_id"):
        value = origin.get(key, "")
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        values.append(value)
    return any(values)


def is_background(row: Session | None) -> bool:
    """该 Session 是否属于后台任务（cron / atrinis）。对应 Go ``IsBackground``。"""
    if row is None or was_promoted(row):
        return False
    if background_kind(row):
        return True
    scope_id = (row.platform_scope_id or "").strip()
    return scope_id.startswith("cron:") or scope_id.startswith("atrinis:")


def background_kind(row: Session | None) -> str:
    """metadata 里的后台种类（去空白）。对应 Go ``backgroundKind``。"""
    if row is None:
        return ""
    raw = session_metadata_fields(row.metadata).get(BACKGROUND_KIND_KEY)
    if not raw:
        return ""
    try:
        kind = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(kind, str):
        return ""
    return kind.strip()


def promote_to_foreground(row: Session, scope: Scope) -> None:
    """把后台 Session 原地变成当前前台的普通 Session。对应 Go ``promoteToForeground``。

    记录 ``foreground_origin``（接管前的 kind / owner / platform / scope）、删除
    ``background_kind``、切到当前 scope 并置 ``mode = work``。**必须在**
    ``Sessions().Mutate`` 的事务内调用。
    """
    fields = session_metadata_fields(row.metadata)
    origin = ForegroundOrigin(
        kind=background_kind(row),
        owner_id=row.owner_id,
        platform=row.platform,
        scope_id=row.platform_scope_id,
    )
    fields[FOREGROUND_ORIGIN_KEY] = origin.to_json()
    fields.pop(BACKGROUND_KIND_KEY, None)
    row.metadata = encode_metadata_fields(fields)
    row.owner_id = session_owner_id(scope)
    row.platform = scope.platform
    row.platform_scope_id = scope.platform_scope_id
    row.mode = "work"


def session_metadata_fields(raw: str) -> dict[str, str]:
    """把 metadata 解成"键 → 原始 JSON 文本"，未知键与值的原始写法都保留。

    对应 Go ``sessionMetadataFields``（``map[string]json.RawMessage``）。Go 用
    ``RawMessage`` 保留值的字节形态，Python 侧用 :class:`json.JSONDecoder` 的
    ``raw_decode`` 逐字段切出原文，因此 ``{"nested":1}`` 不会被重排成
    ``{"nested": 1}``。解析失败时返回空字典（与 Go 一致）。
    """
    text = (raw or "").strip()
    fields: dict[str, str] = {}
    if not text:
        return fields
    decoder = json.JSONDecoder()
    index = _skip_whitespace(text, 0)
    if index >= len(text) or text[index] != "{":
        return {}
    index += 1
    while True:
        index = _skip_whitespace(text, index)
        if index < len(text) and text[index] == "}":
            return fields
        try:
            key, index = decoder.raw_decode(text, index)
        except ValueError:
            return {}
        if not isinstance(key, str):
            return {}
        index = _skip_whitespace(text, index)
        if index >= len(text) or text[index] != ":":
            return {}
        index = _skip_whitespace(text, index + 1)
        try:
            _, end = decoder.raw_decode(text, index)
        except ValueError:
            return {}
        if end <= index:
            return {}
        fields[key] = text[index:end]
        index = _skip_whitespace(text, end)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return fields
        return {}


def encode_metadata_fields(fields: dict[str, str]) -> str:
    """把"键 → 原始 JSON 文本"重新拼成 JSON 对象（键排序，值原样输出）。

    对应 Go 的 ``json.Marshal(map[string]json.RawMessage)``：键按字典序、值按原始
    文本嵌入，不做二次转义。空字典写成 ``{}``。
    """
    if not fields:
        return "{}"
    parts = [
        json.dumps(key, ensure_ascii=False) + ":" + value
        for key, value in sorted(fields.items())
    ]
    return "{" + ",".join(parts) + "}"


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\n\r":
        index += 1
    return index
