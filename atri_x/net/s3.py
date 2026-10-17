"""最小 S3 客户端（SigV4 签名 + REST）：对应 Go 的 ``aws-sdk-go-v2/service/s3``。

Python 标准库没有 S3 客户端，本模块只实现 ATRI-X 媒体外置所需的那部分：
``put_object`` / ``get_object`` / ``delete_object`` / ``head_object`` /
``list_objects``（自动翻页）/ ``presign_get``（query 签名）。

设计要点：

* 签名与请求构造是**纯函数**（:func:`canonical_request` / :func:`string_to_sign` /
  :func:`signing_key` / :func:`sign_request` / :func:`presign_url`），不联网即可单测，
  测试用 AWS 官方文档与 SigV4 测试套件的已知向量断言；
* 传输走 ``http.client``（在 ``asyncio.to_thread`` 里执行），与
  :mod:`atri_x.net.httpclient` 同样不引入第三方依赖；
* 同时支持 path-style（MinIO、自建网关）与 virtual-hosted-style（AWS 默认）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import http.client
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "EMPTY_SHA256",
    "S3Client",
    "S3Config",
    "S3Error",
    "S3NoSuchKeyError",
    "canonical_request",
    "parse_error_body",
    "parse_list_objects",
    "presign_url",
    "sign_request",
    "signing_key",
    "string_to_sign",
    "uri_encode",
]

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class S3Error(Exception):
    """S3 请求失败。``code`` 来自错误响应 XML（如 ``AccessDenied``）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status: int = 0,
        request_id: str = "",
        body: bytes = b"",
        operation: str = "",
        key: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        self.body = body
        self.operation = operation
        self.key = key

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status:
            parts.append(f"status={self.status}")
        if self.operation:
            parts.append(f"op={self.operation}")
        if self.key:
            parts.append(f"key={self.key}")
        return " ".join(parts)


class S3NoSuchKeyError(S3Error):
    """对象不存在（``NoSuchKey`` / 404）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class S3Config:
    """S3 连接配置。

    ``path_style=True``（默认）时 URL 形如 ``{endpoint}/{bucket}/{key}``，
    适合 MinIO 与自建网关；``False`` 时用 virtual-hosted
    ``https://{bucket}.{endpoint-host}/{key}``（AWS 默认）。
    ``endpoint`` 允许带基础路径（如反代到 ``/s3``）。
    """

    endpoint: str = ""
    region: str = "us-east-1"
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    session_token: str = ""
    path_style: bool = True
    verify_tls: bool = True
    prefix: str = ""
    timeout: float = 30.0
    service: str = "s3"

    def object_key(self, key: str) -> str:
        """补上 ``prefix`` 的完整对象键。"""
        prefix = self.prefix
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix + key.lstrip("/")


# ---------------------------------------------------------------------------
# URI / 规范化（SigV4 纯函数部分）
# ---------------------------------------------------------------------------
def uri_encode(text: str, *, encode_slash: bool = True) -> str:
    """AWS 口径的百分号编码：只保留 ``A-Za-z0-9-_.~``，空格编码为 ``%20``。"""
    out: list[str] = []
    for byte in text.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED or (char == "/" and not encode_slash):
            out.append(char)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _canonical_query(raw_query: str, extra: Mapping[str, Any] | None = None) -> str:
    pairs: list[tuple[str, str]] = []
    if raw_query:
        for key, value in urllib.parse.parse_qsl(raw_query, keep_blank_values=True):
            pairs.append((key, value))
    for key, value in (extra or {}).items():
        if value is None:
            continue
        pairs.append((str(key), str(value)))
    encoded = sorted((uri_encode(key), uri_encode(value)) for key, value in pairs)
    return "&".join(f"{key}={value}" for key, value in encoded)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    trimmed = sorted(
        (str(name).lower(), " ".join(str(value).split())) for name, value in headers.items()
    )
    block = "".join(f"{name}:{value}\n" for name, value in trimmed)
    return block, ";".join(name for name, _ in trimmed)


def canonical_request(
    method: str,
    canonical_uri: str,
    canonical_query: str,
    headers: Mapping[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """构造 SigV4 canonical request，返回 ``(文本, signed_headers)``。"""
    block, signed = _canonical_headers(headers)
    text = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{block}\n{signed}\n{payload_hash}"
    )
    return text, signed


def string_to_sign(amz_date: str, scope: str, canonical_request_text: str) -> str:
    """拼接 SigV4 string-to-sign。"""
    digest = hashlib.sha256(canonical_request_text.encode("utf-8")).hexdigest()
    return f"{ALGORITHM}\n{amz_date}\n{scope}\n{digest}"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """派生签名密钥：date → region → service → aws4_request 的 HMAC 链。"""
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _split_url(url: str) -> tuple[urllib.parse.SplitResult, str, str]:
    parts = urllib.parse.urlsplit(url)
    canonical_uri = uri_encode(urllib.parse.unquote(parts.path or "/"), encode_slash=False)
    return parts, canonical_uri, parts.query


def _host_header(parts: urllib.parse.SplitResult) -> str:
    return parts.netloc


def sign_request(
    *,
    method: str,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    service: str = "s3",
    session_token: str = "",
    payload: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
    payload_hash: str | None = None,
    include_content_sha256: bool = True,
) -> dict[str, str]:
    """为一次请求生成 SigV4 头。

    返回**需要发送的全部头**（含 ``Host``、``x-amz-date``、
    ``x-amz-content-sha256``、``x-amz-security-token`` 与 ``Authorization``）。
    ``payload`` 为 ``None`` 时按空体计算哈希。``include_content_sha256=False``
    用于非 S3 服务（S3 强制要求该头，其它服务通常不发）。
    """
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    moment = moment.astimezone(_dt.timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")
    parts, canonical_uri, raw_query = _split_url(url)

    digest = payload_hash
    if digest is None:
        digest = hashlib.sha256(payload if payload is not None else b"").hexdigest()

    signed: dict[str, str] = {"host": _host_header(parts)}
    for name, value in (headers or {}).items():
        signed[str(name).lower()] = str(value)
    signed["x-amz-date"] = amz_date
    if include_content_sha256:
        signed["x-amz-content-sha256"] = digest
    if session_token:
        signed["x-amz-security-token"] = session_token

    c_query = _canonical_query(raw_query, query)
    text, signed_headers = canonical_request(method, canonical_uri, c_query, signed, digest)
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        string_to_sign(amz_date, scope, text).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    request_headers = {name: value for name, value in signed.items()}
    request_headers["Host"] = signed["host"]
    request_headers["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return request_headers


def presign_url(
    *,
    method: str,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    service: str = "s3",
    session_token: str = "",
    expires: int = 900,
    now: _dt.datetime | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """生成 query 签名 URL（``X-Amz-Signature`` 形式），不产生网络请求。"""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    moment = moment.astimezone(_dt.timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")
    parts, canonical_uri, _ = _split_url(url)
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    params: dict[str, Any] = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    if session_token:
        params["X-Amz-Security-Token"] = session_token
    canonical_query = _canonical_query("", params)

    signed = {"host": _host_header(parts)}
    for name, value in (headers or {}).items():
        signed[str(name).lower()] = str(value)
    text, _ = canonical_request(method, canonical_uri, canonical_query, signed, UNSIGNED_PAYLOAD)
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        string_to_sign(amz_date, scope, text).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    base = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", "", "")
    )
    return f"{base}?{canonical_query}&X-Amz-Signature={signature}"


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str:
    for child in root.iter():
        if _localname(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_error_body(body: bytes) -> tuple[str, str, str]:
    """从 S3 错误响应解析 ``(code, message, request_id)``；非 XML 时降级。"""
    if not body:
        return "", "", ""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", body[:400].decode("utf-8", errors="replace"), ""
    return _find_text(root, "Code"), _find_text(root, "Message"), _find_text(root, "RequestId")


def parse_list_objects(body: bytes) -> tuple[list[str], bool, str]:
    """解析 ListObjectsV2 响应，返回 ``(keys, is_truncated, next_token)``。"""
    root = ET.fromstring(body)
    keys = [
        child.text.strip()
        for child in root.iter()
        if _localname(child.tag) == "Key" and child.text
    ]
    truncated = _find_text(root, "IsTruncated").lower() == "true"
    return keys, truncated, _find_text(root, "NextContinuationToken")


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
@dataclass
class _Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class S3Client:
    """最小 S3 客户端（async 接口，内部走阻塞 ``http.client``）。"""

    def __init__(self, config: S3Config) -> None:
        self.config = config

    # -- URL ---------------------------------------------------------------
    def _base(self) -> tuple[str, str, str]:
        parts = urllib.parse.urlsplit(self.config.endpoint)
        if not parts.scheme or not parts.netloc:
            raise S3Error(f"endpoint 非法：{self.config.endpoint!r}", code="InvalidEndpoint")
        return parts.scheme, parts.netloc, parts.path.rstrip("/")

    def _url_for(self, key: str = "", query: Mapping[str, Any] | None = None) -> str:
        scheme, netloc, base_path = self._base()
        bucket = self.config.bucket
        if not bucket:
            raise S3Error("bucket 未配置", code="InvalidBucket")
        encoded = uri_encode(key, encode_slash=False) if key else ""
        if self.config.path_style:
            path = f"{base_path}/{bucket}"
            if encoded:
                path += f"/{encoded}"
        else:
            host = netloc.split("@")[-1]
            port = ""
            if ":" in host:
                host, port = host.rsplit(":", 1)
                port = ":" + port
            netloc = f"{bucket}.{host}{port}"
            path = base_path + (f"/{encoded}" if encoded else "")
        # 查询串直接用 AWS 口径编码并排序：发送的 URL 与签名的规范查询串逐字节一致，
        # 避免 "+" 与 "%20" 的二次解释差异。
        params = _canonical_query("", query)
        url = urllib.parse.urlunsplit((scheme, netloc, path or "/", params, ""))
        return url

    def _raise_for_status(
        self, response: _Response, operation: str, key: str
    ) -> None:
        if 200 <= response.status < 300:
            return
        code, message, request_id = parse_error_body(response.body)
        if not code:
            code = {
                403: "AccessDenied",
                404: "NoSuchKey",
                409: "Conflict",
                429: "SlowDown",
            }.get(response.status, f"HTTP{response.status}")
        if not message:
            message = f"{operation} 失败：HTTP {response.status}"
        error_type = S3Error
        if response.status == 404 or code in ("NoSuchKey", "NotFound"):
            error_type = S3NoSuchKeyError
        raise error_type(
            message,
            code=code,
            status=response.status,
            request_id=request_id,
            body=response.body,
            operation=operation,
            key=key,
        )

    # -- 传输 ---------------------------------------------------------------
    def _send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> _Response:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme
        if scheme not in ("http", "https"):
            raise S3Error(f"不支持的 scheme：{scheme}", code="InvalidEndpoint")
        target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        if scheme == "https":
            context = (
                ssl.create_default_context()
                if self.config.verify_tls
                else ssl._create_unverified_context()  # noqa: SLF001 - 配置项显式关闭
            )
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname or "", parts.port or 443, timeout=self.config.timeout, context=context
            )
        else:
            connection = http.client.HTTPConnection(
                parts.hostname or "", parts.port or 80, timeout=self.config.timeout
            )
        try:
            connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            if body:
                connection.send(body)
            raw = connection.getresponse()
            payload = raw.read()
            return _Response(
                status=raw.status,
                headers={k.lower(): v for k, v in raw.getheaders()},
                body=payload,
            )
        except (OSError, http.client.HTTPException) as exc:
            raise S3Error(f"{method} {url} 传输失败：{exc}", code="TransportError") from exc
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def _signed(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        payload_hash: str | None = None,
    ) -> dict[str, str]:
        return sign_request(
            method=method,
            url=url,
            region=self.config.region,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            service=self.config.service,
            session_token=self.config.session_token,
            payload=body,
            headers=headers,
            payload_hash=payload_hash,
        )

    async def _run(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> _Response:
        return await asyncio.to_thread(self._send, method, url, headers, body)

    # -- 操作 ---------------------------------------------------------------
    async def put_object(
        self,
        key: str,
        data: bytes | str,
        content_type: str = "application/octet-stream",
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """上传对象，返回补全 ``prefix`` 后的键。"""
        full_key = self.config.object_key(key)
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        extra: dict[str, str] = {"content-type": content_type}
        for name, value in (metadata or {}).items():
            extra[f"x-amz-meta-{name.lower()}"] = str(value)
        url = self._url_for(full_key)
        headers = self._signed("PUT", url, body=payload, headers=extra)
        response = await self._run("PUT", url, headers, payload)
        self._raise_for_status(response, "put_object", full_key)
        return full_key

    async def get_object(self, key: str) -> bytes:
        """下载对象内容。"""
        full_key = self.config.object_key(key)
        url = self._url_for(full_key)
        headers = self._signed("GET", url)
        response = await self._run("GET", url, headers, None)
        self._raise_for_status(response, "get_object", full_key)
        return response.body

    async def delete_object(self, key: str) -> None:
        """删除对象（S3 对不存在的键也返回 204）。"""
        full_key = self.config.object_key(key)
        url = self._url_for(full_key)
        headers = self._signed("DELETE", url)
        response = await self._run("DELETE", url, headers, None)
        self._raise_for_status(response, "delete_object", full_key)

    async def head_object(self, key: str) -> dict[str, str]:
        """取对象元数据（小写响应头）；不存在时抛 :class:`S3NoSuchKeyError`。"""
        full_key = self.config.object_key(key)
        url = self._url_for(full_key)
        headers = self._signed("HEAD", url)
        response = await self._run("HEAD", url, headers, None)
        if response.status == 404:
            raise S3NoSuchKeyError(
                f"对象不存在：{full_key}",
                code="NoSuchKey",
                status=404,
                operation="head_object",
                key=full_key,
            )
        self._raise_for_status(response, "head_object", full_key)
        return response.headers

    async def list_objects(
        self, prefix: str = "", *, delimiter: str = "", max_keys: int = 1000
    ) -> list[str]:
        """列出对象键，内部按 ``NextContinuationToken`` 自动翻页。"""
        full_prefix = self.config.object_key(prefix)
        keys: list[str] = []
        token = ""
        while True:
            query: dict[str, Any] = {"list-type": "2", "max-keys": max_keys}
            if full_prefix:
                query["prefix"] = full_prefix
            if delimiter:
                query["delimiter"] = delimiter
            if token:
                query["continuation-token"] = token
            # 查询串在签名时必须与发送的完全一致：直接使用签名时构造好的 URL。
            url = self._url_for("", query)
            headers = self._signed("GET", url)
            response = await self._run("GET", url, headers, None)
            self._raise_for_status(response, "list_objects", full_prefix)
            page, truncated, token = parse_list_objects(response.body)
            keys.extend(page)
            if not truncated or not token:
                return keys

    def presign_get(self, key: str, expires: int = 900) -> str:
        """生成预签名 GET URL（默认 900 秒有效）。"""
        full_key = self.config.object_key(key)
        return presign_url(
            method="GET",
            url=self._url_for(full_key),
            region=self.config.region,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            service=self.config.service,
            session_token=self.config.session_token,
            expires=expires,
        )
