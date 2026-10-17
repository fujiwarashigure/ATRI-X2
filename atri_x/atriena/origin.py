"""Atriena 事件来源。

对应 Go：``internal/atriena/origin.go``。

来源决定了鉴权口径与审计里的 ``origin`` 标签：HTTP 令牌走令牌鉴权，Hook 与进程内
调用走各自的信任边界。``label()`` 是审计/日志里出现的那串
``<kind>:<name>``，也是落库的 ``token_name`` 字段值。
"""

from __future__ import annotations

__all__ = [
    "ORIGIN_HOOK",
    "ORIGIN_HTTP_TOKEN",
    "ORIGIN_INTERNAL",
    "Origin",
    "OriginHook",
    "OriginHTTPToken",
    "OriginInternal",
    "OriginKind",
]


class OriginKind:
    """来源种类。对应 Go 的 ``OriginKind`` 字符串类型。"""

    HTTP_TOKEN = "http_token"
    HOOK = "hook"
    INTERNAL = "internal"


OriginHTTPToken = OriginKind.HTTP_TOKEN
OriginHook = OriginKind.HOOK
OriginInternal = OriginKind.INTERNAL

#: ``snake_case`` 别名，便于按 PORTING §1.4 的命名习惯引用。
ORIGIN_HTTP_TOKEN = OriginHTTPToken
ORIGIN_HOOK = OriginHook
ORIGIN_INTERNAL = OriginInternal

#: 合法的来源种类集合。对应 Go ``Origin.Validate`` 的 ``switch`` 白名单。
VALID_ORIGIN_KINDS = (OriginHTTPToken, OriginHook, OriginInternal)


class Origin:
    """事件从哪来。对应 Go ``Origin``。"""

    __slots__ = ("kind", "name")

    def __init__(self, kind: str = "", name: str = "") -> None:
        self.kind = kind
        self.name = name

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Origin):
            return NotImplemented
        return self.kind == other.kind and self.name == other.name

    def __hash__(self) -> int:
        return hash((self.kind, self.name))

    def __repr__(self) -> str:
        return f"Origin(kind={self.kind!r}, name={self.name!r})"

    def validate(self) -> None:
        """校验来源合法。对应 Go ``Origin.Validate``（返回 error）。

        非法时抛 :class:`atri_x.errors.InvalidError`。
        """
        from atri_x.errors import InvalidError

        if self.kind not in VALID_ORIGIN_KINDS:
            raise InvalidError(f"unsupported atriena origin kind {self.kind!r}")
        if not (self.name or "").strip():
            raise InvalidError("atriena origin name is required")

    def label(self) -> str:
        """审计标签。对应 Go ``Origin.Label``。"""
        name = (self.name or "").strip()
        if not name:
            return self.kind
        return f"{self.kind}:{name}"
