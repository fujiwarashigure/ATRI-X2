"""QQ 官方 HTTP API 客户端：网关地址、发消息、上传媒体。

对应 Go：``internal/platform/qqofficial/client.go``。

形式差异：

* ``net/http`` → :mod:`atri_x.net.httpclient`：``ApiClient.http`` 只要求鸭子类型的
  ``await request(method, url, json=..., headers=..., timeout=..., cancel=...)``，
  测试可以注入假客户端（Go 测试是替换 ``http.Client``）。
* ``json.Marshal`` + ``Unmarshal`` → 请求体传 :meth:`to_json` 出来的 ``dict``，
  响应体解析成 ``dict`` 后由调用方构造 dataclass。
* ``os.ReadFile`` 失败 → :class:`atri_x.errors.AtriXError`（文案与 Go 逐字一致）。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from atri_x.errors import AtriXError, InvalidError
from atri_x.net.httpclient import HTTPClient
from atri_x.platform.qqofficial.config import DEFAULT_API_BASE_URL, Config
from atri_x.platform.qqofficial.target import TARGET_C2C, TARGET_GROUP, SendTarget
from atri_x.platform.qqofficial.token import TokenSource
from atri_x.platform.qqofficial.types import (
    ApiErrorResponse,
    GatewayResponse,
    MessageResponse,
    MessageToCreate,
    UploadFileRequest,
    UploadFileResponse,
)


@dataclass
class PreparedSource:
    """上传前的媒体来源。对应 Go ``preparedSource``。"""

    url: str = ""
    data: bytes = b""
    name: str = ""


def prepare_source(url_value: str, path: str, data: bytes) -> PreparedSource:
    """整理媒体来源（URL 优先，其次内联数据，最后本地文件）。对应 Go ``prepareSource``。"""
    value = (url_value or "").strip()
    if value != "":
        return PreparedSource(url=value, name=_path_base(value))
    if data:
        return PreparedSource(data=bytes(data))
    path = (path or "").strip()
    if path == "":
        raise InvalidError("source path is empty")
    try:
        with open(path, "rb") as handle:
            file_data = handle.read()
    except OSError as exc:
        raise AtriXError(f"read source {path!r}: {exc}") from exc
    return PreparedSource(data=file_data, name=_path_base(path))


def _path_base(value: str) -> str:
    """对应 Go ``filepath.Base``（Windows 与 POSIX 分隔符都认）。"""
    value = (value or "").replace("\\", "/").rstrip("/")
    if "/" in value:
        value = value[value.rindex("/") + 1 :]
    return value or "/"


def target_api_path(target: SendTarget, resource: str) -> str:
    """落点 + 资源名 → API 路径。对应 Go ``targetAPIPath``。"""
    openid = (target.openid or "").strip()
    if openid == "":
        raise InvalidError("qqofficial target openid is empty")
    if target.kind == TARGET_C2C:
        collection = "users"
    elif target.kind == TARGET_GROUP:
        collection = "groups"
    else:
        raise InvalidError(f"qqofficial unsupported target kind {target.kind!r}")
    return "/v2/" + collection + "/" + quote(openid, safe="") + "/" + (resource or "").strip()


class ApiClient:
    """QQ 官方 API 客户端。对应 Go ``apiClient``。"""

    def __init__(
        self,
        cfg: Config,
        http: Any = None,
        tokens: TokenSource | None = None,
        base_url: str = DEFAULT_API_BASE_URL,
    ) -> None:
        self.cfg = cfg
        self.base_url = base_url
        self.http = (
            http
            if http is not None
            else HTTPClient(base_url=base_url, timeout=cfg.http_timeout())
        )
        self.tokens = tokens if tokens is not None else TokenSource(cfg, self.http)

    async def gateway(self, ctx: Any) -> str:
        """取网关地址。对应 Go ``gateway``。"""
        data = await self.do_json(ctx, "GET", "/gateway", None)
        out = GatewayResponse.from_json(data)
        if (out.url or "").strip() == "":
            raise AtriXError("qqofficial gateway url is empty")
        return out.url.strip()

    async def send_message(
        self, ctx: Any, target: SendTarget, msg: MessageToCreate
    ) -> MessageResponse:
        """发一条消息。对应 Go ``sendMessage``。"""
        path = target_api_path(target, "messages")
        data = await self.do_json(ctx, "POST", path, msg.to_json())
        return MessageResponse.from_json(data)

    async def upload_file(
        self, ctx: Any, target: SendTarget, file_type: int, source: PreparedSource
    ) -> UploadFileResponse:
        """上传媒体并取回 ``file_info``。对应 Go ``uploadFile``。"""
        body = UploadFileRequest(file_type=file_type, srv_send_msg=False)
        if (source.url or "").strip() != "":
            body.url = source.url
        elif source.data:
            body.file_data = base64.b64encode(source.data).decode("ascii")
        else:
            raise InvalidError("qqofficial media source is empty")
        path = target_api_path(target, "files")
        data = await self.do_json(ctx, "POST", path, body.to_json())
        out = UploadFileResponse.from_json(data)
        if (out.file_info or "").strip() == "":
            raise AtriXError("qqofficial upload returned empty file_info")
        return out

    async def do_json(
        self, ctx: Any, method: str, path: str, body: dict[str, Any] | None
    ) -> Any:
        """发一次 JSON 请求并解析响应。对应 Go ``doJSON``。"""
        return await self.do_json_with_retry(ctx, method, path, body, False)

    async def do_json_with_retry(
        self,
        ctx: Any,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        retried: bool,
    ) -> Any:
        """带一次 401 重试的请求。对应 Go ``doJSONWithRetry``。"""
        base_url = (self.base_url or "").rstrip("/")
        if base_url == "":
            base_url = DEFAULT_API_BASE_URL.rstrip("/")
        url = base_url + path
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        token = await self.tokens.token_value(ctx)
        headers["Authorization"] = "QQBot " + token
        try:
            response = await self.http.request(
                method,
                url,
                json=body,
                headers=headers,
                timeout=self.cfg.http_timeout(),
                cancel=ctx,
            )
        except Exception as exc:  # noqa: BLE001 - 传输层错误按 Go 返回 error
            raise AtriXError(f"qqofficial api {method} {path}: {exc}") from exc
        status = int(getattr(response, "status", 0))
        text = _body_text(response)
        if status == 401 and not retried:
            await self.tokens.force_refresh(ctx)
            return await self.do_json_with_retry(ctx, method, path, body, True)
        if status < 200 or status >= 300:
            api_error = _api_error(text)
            if api_error is not None and (api_error.code != 0 or api_error.message != ""):
                raise AtriXError(
                    f"qqofficial api {method} {path}: code {api_error.code}: {api_error.message}"
                )
            raise AtriXError(
                f"qqofficial api {method} {path}: http {status}: {text.strip()}"
            )
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AtriXError(f"decode qqofficial api {method} {path}: {exc}") from exc


def _api_error(text: str) -> ApiErrorResponse | None:
    """尝试把错误响应体解析成 ``apiErrorResponse``。"""
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return ApiErrorResponse.from_json(raw)


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
