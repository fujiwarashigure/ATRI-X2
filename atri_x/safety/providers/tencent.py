"""腾讯云内容安全适配器：文本（天御 TMS）+ 图片（IMS）。

签名：**TC3-HMAC-SHA256**（标准库 ``hmac``/``hashlib`` 手写，不引 SDK）。
请求：``POST https://<host>/``，``Content-Type: application/json; charset=utf-8``，
公共头 ``X-TC-Action`` / ``X-TC-Version`` / ``X-TC-Timestamp`` / ``X-TC-Region``。

> ⚠️ **未联调**：本适配器按官方文档的请求/响应形状实现，并用假传输覆盖了签名与解析；
> 真实账号联调（字段、错误码、配额）是 1.1.8 P2 的验收项，联调时用录制响应补
> ``tests/test_safety_providers_contract.py`` 的 fixture。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from atri_x.safety.providers.base import (
    ProviderConfig,
    ProviderError,
    Transport,
    b64,
    build_json_body,
    decode_json,
    hmac_sha256,
    hmac_sha256_hex,
    sha256_hex,
    suggestion_from_risk,
    verdict,
)
from atri_x.safety.types import Verdict, normalize_labels

__all__ = ["TencentProvider", "TENCENT_TEXT_HOST", "TENCENT_IMAGE_HOST"]

TENCENT_TEXT_HOST = "tms.tencentcloudapi.com"
TENCENT_IMAGE_HOST = "ims.tencentcloudapi.com"

#: 默认 API 版本（文本内容安全 / 图片内容安全）。
DEFAULT_VERSION = "2020-12-29"

TEXT_ACTION = "TextModeration"
IMAGE_ACTION = "ImageModeration"


class TencentProvider:
    """腾讯云天御文本 + 图片内容安全。"""

    name = "tencent"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: Transport,
        timeout_seconds: float = 0.8,
        clock: Any = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self._clock = clock or time.time
        self.version = config.version or DEFAULT_VERSION

    # -- 公共 ---------------------------------------------------------------
    async def moderate_text(self, text: str, *, biz_type: str = "") -> Verdict:
        """文本审核：``Content`` 是 **base64 编码后的原文**（天御要求）。"""
        content = (text or "").strip()
        if not content:
            return verdict(provider=self.name, suggestion="pass")
        payload: dict[str, Any] = {"Content": b64(content.encode("utf-8"))}
        type_name = (biz_type or self.config.biz_type or "").strip()
        if type_name:
            payload["BizType"] = type_name
        data = await self._call(TEXT_ACTION, TENCENT_TEXT_HOST, payload)
        return self._parse(data, default_label="text")

    async def moderate_image(
        self, data: bytes, *, mime: str = "", url: str = ""
    ) -> Verdict:
        """图片审核：有 URL 用 ``FileUrl``，否则 ``FileContent``（base64 图片字节）。"""
        if url:
            payload: dict[str, Any] = {"FileUrl": url}
        elif data:
            payload = {"FileContent": b64(data)}
        else:
            return verdict(provider=self.name, suggestion="pass")
        type_name = (self.config.biz_type or "").strip()
        if type_name:
            payload["BizType"] = type_name
        raw = await self._call(IMAGE_ACTION, TENCENT_IMAGE_HOST, payload)
        return self._parse(raw, default_label="image")

    # -- 内部 ---------------------------------------------------------------
    async def _call(
        self, action: str, host: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = build_json_body(payload)
        timestamp = int(self._clock())
        headers = self._signed_headers(host, action, body, timestamp)
        status, raw = await self.transport.post_json(
            f"https://{host}/", headers=headers, body=body, timeout=self.timeout_seconds
        )
        if status >= 400:
            raise ProviderError(
                f"tencent: HTTP {status}",
                provider=self.name,
                code=f"http_{status}",
                retryable=status >= 500 or status == 429,
                auth=status in (401, 403),
                status=status,
            )
        document = decode_json(raw, provider=self.name)
        response = document.get("Response")
        if not isinstance(response, dict):
            raise ProviderError(
                "tencent: 响应缺少 Response", provider=self.name, retryable=True
            )
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code", "") or "")
            message = str(error.get("Message", "") or "")
            raise ProviderError(
                f"tencent: {code} {message}".strip(),
                provider=self.name,
                code=code,
                retryable=_retryable_code(code),
                auth=_auth_code(code),
            )
        return response

    def _signed_headers(
        self, host: str, action: str, body: bytes, timestamp: int
    ) -> dict[str, str]:
        """构造 TC3-HMAC-SHA256 签名头。"""
        cfg = self.config
        service = (cfg.service or ("tms" if host == TENCENT_TEXT_HOST else "ims")).strip()
        content_type = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
        signed_headers = "content-type;host"
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                canonical_headers,
                signed_headers,
                sha256_hex(body),
            ]
        )
        date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                credential_scope,
                sha256_hex(canonical_request.encode("utf-8")),
            ]
        )
        secret_date = hmac_sha256(("TC3" + cfg.secret_key).encode("utf-8"), date.encode("utf-8"))
        secret_service = hmac_sha256(secret_date, service.encode("utf-8"))
        secret_signing = hmac_sha256(secret_service, b"tc3_request")
        signature = hmac_sha256_hex(secret_signing, string_to_sign.encode("utf-8"))
        authorization = (
            f"TC3-HMAC-SHA256 Credential={cfg.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Version": self.version,
            "X-TC-Timestamp": str(timestamp),
        }
        if cfg.region:
            headers["X-TC-Region"] = cfg.region
        return headers

    def _parse(self, response: Mapping[str, Any], *, default_label: str) -> Verdict:
        data = response.get("Data")
        request_id = str(response.get("RequestId", "") or "")
        if not isinstance(data, dict):
            raise ProviderError(
                "tencent: 响应缺少 Data", provider=self.name, retryable=True
            )
        suggestion = suggestion_from_risk(str(data.get("Suggestion", "") or ""))
        labels: list[str] = []
        primary = str(data.get("Label", "") or "")
        if primary:
            labels.append(primary)
        details = data.get("DetailResults")
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict):
                    label = str(item.get("Label", "") or "")
                    if label:
                        labels.append(label)
        if not labels:
            labels.append(default_label)
        try:
            score = float(data.get("Score"))
        except (TypeError, ValueError):
            score = None
        return verdict(
            provider=self.name,
            suggestion=suggestion,
            labels=normalize_labels(labels),
            score=score,
            request_id=request_id,
            raw_code=str(data.get("SubLabel", "") or ""),
        )


def _retryable_code(code: str) -> bool:
    """腾讯云错误码里可重试的那几类（限流 / 内部错误 / 超时）。"""
    text = (code or "").lower()
    if not text:
        return True
    return any(
        marker in text
        for marker in (
            "requestlimit",
            "internalerror",
            "serviceunavailable",
            "timeout",
            "resourceunavailable",
        )
    )


def _auth_code(code: str) -> bool:
    text = (code or "").lower()
    return any(marker in text for marker in ("authfailure", "unauthorized", "secretid"))


__all__ += ["DEFAULT_VERSION", "IMAGE_ACTION", "TEXT_ACTION"]


def _unused_import_guard() -> None:  # pragma: no cover - 防止未来误删 import
    _ = hmac_sha256_hex
