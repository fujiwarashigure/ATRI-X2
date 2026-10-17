"""Atrinis 鉴权：令牌、atriwisp 策略、内部工具白名单与外部工具声明校验。

对应 Go：``internal/atrinis/auth.go``。

四道闸门，全部是**拒绝即抛异常**（Go 返回 error）：

1. :meth:`AuthMixin.authenticate` —— HTTP 令牌 → 令牌名。空令牌永远不通过；
   只有非空且**逐字相等**的令牌才算命中（不由配置里的空值放行）。
2. :meth:`AuthMixin.authorize_atriwisp` —— atriwisp 级开关与令牌白名单。
   未配置的 atriwisp 一律放行（保持向后兼容）；``enabled`` 为 ``None`` 也放行，
   只有显式 ``false`` 才拒。令牌白名单只在 ``origin.kind == http_token`` 时生效，
   Hook / 进程内来源不受令牌限制。
3. :meth:`AuthMixin.authorize_internal_tools` —— 后台可用的内部工具白名单。
   ``discover_tool`` 无条件放行（它本身就是"看看还有什么工具"的入口）。
   工具清单**先 trim 去重**再判定，重复项不算错误。
4. :meth:`AuthMixin.authorize_external_tools` —— Atriwisp 声明的外部工具。
   名字必须非空、不重复、不含 ``.``、空格、``/``、``\\``；endpoint 必须是
   带 host 的 http/https；``timeout_seconds`` 必须在 ``[0, 60]``；schema 必须
   是非空对象且 ``type == "object"``。
"""

from __future__ import annotations

from urllib.parse import urlparse

from atri_x.atriena import Event, OriginHTTPToken
from atri_x.atrinis.store import background_tool_names, set_from_strings, trim_strings
from atri_x.errors import InvalidError
from atri_x.toolrun import AtriwispInjection, cached_tools_from_atriwisp

__all__ = ["AuthMixin", "MAX_EXTERNAL_TOOL_TIMEOUT_SECONDS"]

#: 外部工具超时上限（秒）。对应 Go 里的字面量 ``60``。
MAX_EXTERNAL_TOOL_TIMEOUT_SECONDS = 60

#: 外部工具名里不允许出现的字符（Go 的 ``strings.ContainsAny(name, ". /\\")``）。
_FORBIDDEN_NAME_CHARS = ". /\\"


class AuthMixin:
    """鉴权与授权。对应 Go ``auth.go`` 里的 ``Service`` 方法。

    混入方需提供 ``tokens`` 与 ``cfg``（见 :class:`atri_x.atrinis.service.Service`）。
    """

    def authenticate(self, token: str) -> tuple[str, bool]:
        """令牌 → 令牌名。对应 Go ``authenticate``。

        返回 ``(name, ok)``；``ok`` 为假表示未通过。遍历顺序与 Go 的 map 迭代
        一样不保证，但由于令牌值互不相同，结果与顺序无关。
        """
        text = (token or "").strip()
        if not text:
            return "", False
        for name, value in (self.tokens or {}).items():
            if value and text == value:
                return name, True
        return "", False

    def authorize_atriwisp(self, event: Event) -> None:
        """atriwisp 级授权。对应 Go ``authorizeAtriwisp``（拒绝时抛异常）。"""
        name = event.request.atriwisp.name
        policy = (self.cfg.atriwisps or {}).get(name)
        if policy is None:
            return
        if policy.enabled is not None and not policy.enabled:
            raise InvalidError(f"atriwisp {name!r} is disabled")
        allowed_tokens = trim_strings(policy.allowed_tokens)
        if not allowed_tokens:
            return
        origin = event.origin
        if origin is None or origin.kind != OriginHTTPToken:
            return
        if origin.name in allowed_tokens:
            return
        raise InvalidError(f"token {origin.name!r} is not allowed for atriwisp {name!r}")

    def authorize_internal_tools(self, event: Event) -> None:
        """内部工具白名单。对应 Go ``authorizeInternalTools``（拒绝时抛异常）。"""
        allowed = self.allowed_internal_tools(event.request.atriwisp.name)
        for name in background_tool_names(event.request.tool_list_names):
            if name == "discover_tool":
                continue
            if name not in allowed:
                raise InvalidError(
                    f"tool {name!r} is not allowed for atriwisp {event.request.atriwisp.name!r}"
                )

    def allowed_internal_tools(self, atriwisp_name: str) -> set[str]:
        """该 atriwisp 可用的内部工具集合。对应 Go ``allowedInternalTools``。

        atriwisp 自己声明了 ``allowed_tools`` 就覆盖全局白名单；否则用
        ``cfg.allowed_tools``。

        与 Go 的偏差：Go 判定 ``policy.AllowedTools != nil``（区分"未设置"与
        "显式空"）；Python 的 :class:`atri_x.config.AtrinisAtriwispConfig` 把该字段
        声明为 ``list[str] = field(default_factory=list)``，**无法**表达 nil，因此
        本仓按"空列表 = 未设置"处理。要表达"该 atriwisp 一个内部工具都不许用"，
        目前只能靠外部手段（例如把所有工具名都排除在全局白名单之外）。
        """
        allowed_tools = self.cfg.allowed_tools
        policy = (self.cfg.atriwisps or {}).get(atriwisp_name)
        if policy is not None and policy.allowed_tools:
            allowed_tools = policy.allowed_tools
        return set_from_strings(allowed_tools)

    def authorize_external_tools(self, event: Event) -> None:
        """外部工具声明校验。对应 Go ``authorizeExternalTools``（拒绝时抛异常）。"""
        policy = (self.cfg.atriwisps or {}).get(event.request.atriwisp.name)
        disabled = (
            set_from_strings(policy.disabled_external_tools) if policy is not None else set()
        )
        seen: set[str] = set()
        for declared in event.request.tools or []:
            name = (declared.name or "").strip()
            if not name:
                raise InvalidError("external tool name is required")
            if name in seen:
                raise InvalidError(f"external tool {name!r} is duplicated")
            seen.add(name)
            if name in disabled:
                raise InvalidError(
                    f"external tool {name!r} is disabled for atriwisp "
                    f"{event.request.atriwisp.name!r}"
                )
            if any(char in name for char in _FORBIDDEN_NAME_CHARS):
                raise InvalidError(f"external tool {name!r} has invalid name")
            if not (declared.endpoint or "").strip():
                raise InvalidError(f"external tool {name!r} endpoint is required")
            parsed = urlparse((declared.endpoint or "").strip())
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise InvalidError(
                    f"external tool {name!r} endpoint must be http or https URL"
                )
            timeout = declared.timeout_seconds
            if timeout < 0 or timeout > MAX_EXTERNAL_TOOL_TIMEOUT_SECONDS:
                raise InvalidError(
                    f"external tool {name!r} timeout_seconds must be between 0 and 60"
                )
            if not declared.schema:
                raise InvalidError(f"external tool {name!r} schema is required")
            if declared.schema.get("type") != "object":
                raise InvalidError(f"external tool {name!r} schema.type must be object")

    def atriwisp_cached_tools(self, event: Event) -> list:
        """把声明的外部工具转成后台可用的缓存工具。对应 Go ``atriwispCachedTools``。"""
        return cached_tools_from_atriwisp(
            AtriwispInjection(
                atriwisp_name=event.request.atriwisp.name,
                event_key=event.event_key,
                tools=list(event.request.tools or []),
            )
        )
