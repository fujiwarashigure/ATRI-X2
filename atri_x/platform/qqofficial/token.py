"""QQ 官方 access token 的获取与缓存。

对应 Go：``internal/platform/qqofficial/token.go``。

形式差异：

* ``sync.Mutex`` → :class:`asyncio.Lock`：Go 在 ``refresh`` 期间持锁，Python 同样
  用锁把并发刷新串行化。
* 到期判定用 ``time.monotonic()``（对应 Go 的 ``time.Until(s.expiresAt) > time.Minute``），
  不写 Wall clock，避免本机时间回拨把有效 token 判成过期。
* ``http.Client`` → :mod:`atri_x.net.httpclient` 的 ``HTTPClient``（鸭子类型：
  只要求 ``await http.request(method, url, json=..., headers=..., timeout=...)``）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.qqofficial.config import DEFAULT_TOKEN_URL, Config
from atri_x.platform.qqofficial.types import (
    AccessTokenRequest,
    AccessTokenResponse,
)

#: 提前刷新窗口（秒）。对应 Go 的 ``time.Minute``。
REFRESH_MARGIN_SECONDS = 60.0

#: ``expires_in`` 缺失/非正时的兜底（秒）。对应 Go 的 ``7200``。
DEFAULT_EXPIRES_SECONDS = 7200


def parse_expires_in(value: Any) -> int:
    """解释 ``expires_in``：数字、数字字符串、带引号的字符串都接受。

    对应 Go ``parseExpiresIn`` / ``parseExpiresText``；无法解释时抛
    :class:`atri_x.errors.InvalidError`（Go 返回 error）。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise InvalidError('decode qqofficial token expires_in: invalid syntax "true"')
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return parse_expires_text(value)
    raise InvalidError(f"decode qqofficial token expires_in: unsupported value {value!r}")


def parse_expires_text(text: str) -> int:
    """对应 Go ``parseExpiresText``。"""
    text = (text or "").strip()
    if text == "":
        return 0
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    body = text[1:] if text[:1] in ("+", "-") else text
    if not body or not body.isascii() or not body.isdigit():
        raise InvalidError(f"decode qqofficial token expires_in: invalid syntax {text!r}")
    return int(text, 10)


class TokenSource:
    """带缓存的 access token 来源。对应 Go ``tokenSource``。"""

    def __init__(
        self,
        cfg: Config,
        http: Any,
        token_url: str = DEFAULT_TOKEN_URL,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.token_url = token_url
        self.token = ""
        self.expires_at = 0.0
        self._lock = asyncio.Lock()

    def remaining(self) -> float:
        """距到期的秒数（负数表示已过期）。"""
        if self.token == "" or self.expires_at <= 0:
            return 0.0
        return self.expires_at - time.monotonic()

    @property
    def valid(self) -> bool:
        return self.token != "" and self.remaining() > REFRESH_MARGIN_SECONDS

    async def token_value(self, ctx: Any) -> str:
        """取当前有效 token，必要时刷新。对应 Go ``tokenValue``。"""
        async with self._lock:
            if self.valid:
                return self.token
        return await self.refresh(ctx)

    async def refresh(self, ctx: Any) -> str:
        """刷新 token。对应 Go ``refresh``（持锁，避免并发重复刷新）。"""
        async with self._lock:
            if self.valid:
                return self.token
            request = AccessTokenRequest(
                app_id=(self.cfg.app_id or "").strip(), client_secret=self.cfg.secret()
            )
            data = await self._post(request, ctx)
            out = AccessTokenResponse.from_json(data)
            if out.code != 0:
                raise AtriXError(
                    f"get qqofficial access token: code {out.code}: {out.message}"
                )
            if (out.access_token or "").strip() == "":
                raise AtriXError("get qqofficial access token: empty token")
            expires = parse_expires_in(out.expires_in)
            if expires <= 0:
                expires = DEFAULT_EXPIRES_SECONDS
            self.token = out.access_token.strip()
            self.expires_at = time.monotonic() + float(expires)
            return self.token

    async def force_refresh(self, ctx: Any) -> str:
        """强制刷新（401 重试用）。对应 Go ``forceRefresh``。"""
        async with self._lock:
            self.token = ""
            self.expires_at = 0.0
        return await self.refresh(ctx)

    async def _post(self, request: AccessTokenRequest, ctx: Any) -> Any:
        try:
            response = await self.http.request(
                "POST",
                self.token_url,
                json=request.to_json(),
                headers={"Content-Type": "application/json"},
                timeout=self.cfg.http_timeout(),
                cancel=ctx,
            )
        except Exception as exc:  # noqa: BLE001 - 传输层错误按 Go 返回 error
            raise AtriXError(f"get qqofficial access token: {exc}") from exc
        body = _body_text(response)
        if not 200 <= int(getattr(response, "status", 0)) < 300:
            raise AtriXError(
                f"get qqofficial access token: http {getattr(response, 'status', 0)}: {body.strip()}"
            )
        import json

        try:
            return json.loads(body) if body else None
        except ValueError as exc:
            raise AtriXError(f"decode qqofficial access token: {exc}") from exc


def _body_text(response: Any) -> str:
    """取响应体文本（兼容 :class:`atri_x.net.httpclient.Response` 与假响应）。"""
    text = getattr(response, "text", None)
    if callable(text):
        return text()
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


__all__ = [name for name in dir() if not name.startswith("_")]
