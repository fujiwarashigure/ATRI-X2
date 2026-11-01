"""阿里云内容安全 2.0 适配器：文本（TextModeration）+ 图片（ImageModeration）。

签名：**ACS3-HMAC-SHA256**（标准库手写）。请求：``POST https://<endpoint>/``，
业务参数放在 ``Service`` + ``ServiceParameters``（后者是 JSON **字符串**）里；
响应里的 ``Data`` 也是 JSON **字符串**，要二次解析。

> ⚠️ **未联调**：字段形状按官方文档实现，假传输覆盖签名与解析；真实账号联调是
> 1.1.8 P2 的验收项。图片审核需要可访问的 URL（``ImageModeration`` 走 ``imageUrl``），
> 本地字节只有在配置了对象存储时才送得出去——否则抛不可重试错误，由引擎跳过该供应商。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping

from atri_x.safety.providers.base import (
    ProviderConfig,
    ProviderError,
    Transport,
    build_json_body,
    decode_json,
    hmac_sha256_hex,
    sha256_hex,
    suggestion_from_risk,
    verdict,
)
from atri_x.safety.types import Verdict, normalize_labels

__all__ = ["AliyunProvider", "DEFAULT_ENDPOINT", "DEFAULT_VERSION"]

DEFAULT_ENDPOINT = "green-cip.cn-shanghai.aliyuncs.com"
DEFAULT_VERSION = "2022-03-02"

TEXT_ACTION = "TextModeration"
IMAGE_ACTION = "ImageModeration"


class AliyunProvider:
    """阿里云内容安全 2.0（文本 + 图片）。"""

    name = "aliyun"

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
        self.endpoint = (config.endpoint or DEFAULT_ENDPOINT).strip()
        self.version = config.version or DEFAULT_VERSION
        #: 文本审核默认 service（阿里云 2.0 的"评论检测"场景，可用 ``[remote.aliyun].service`` 覆盖）。
        self.service = (config.service or "comment_detection").strip()

    # -- 公共 ---------------------------------------------------------------
    async def moderate_text(self, text: str, *, biz_type: str = "") -> Verdict:
        content = text or ""
        if not content.strip():
            return verdict(provider=self.name, suggestion="pass")
        parameters = {"content": content, "dataId": uuid.uuid4().hex}
        if biz_type or self.config.biz_type:
            parameters["businessType"] = biz_type or self.config.biz_type
        payload = {
            "Service": self.service,
            "ServiceParameters": json.dumps(parameters, ensure_ascii=False),
        }
        response = await self._call(TEXT_ACTION, payload)
        return self._parse(response, default_label="text")

    async def moderate_image(
        self, data: bytes, *, mime: str = "", url: str = ""
    ) -> Verdict:
        if not url:
            raise ProviderError(
                "aliyun: 图片审核需要 URL（对象存储未配置时跳过该供应商）",
                provider=self.name,
                code="image_requires_url",
                retryable=False,
            )
        parameters = {"imageUrl": url, "dataId": uuid.uuid4().hex}
        payload = {
            "Service": "baselineCheck",
            "ServiceParameters": json.dumps(parameters, ensure_ascii=False),
        }
        response = await self._call(IMAGE_ACTION, payload)
        return self._parse(response, default_label="image")

    # -- 内部 ---------------------------------------------------------------
    async def _call(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = build_json_body(payload)
        timestamp = self._clock()
        headers = self._signed_headers(action, body, timestamp)
        status, raw = await self.transport.post_json(
            f"https://{self.endpoint}/",
            headers=headers,
            body=body,
            timeout=self.timeout_seconds,
        )
        if status >= 400:
            raise ProviderError(
                f"aliyun: HTTP {status}",
                provider=self.name,
                code=f"http_{status}",
                retryable=status >= 500 or status == 429,
                auth=status in (401, 403),
                status=status,
            )
        document = decode_json(raw, provider=self.name)
        code = document.get("Code")
        # 阿里云 2.0 用 ``Code``（字符串 "200" / "Success" / 错误码），兼容 HTTP 200 里带错误码。
        if code not in (None, "", 200, "200", "Success", "success", "OK"):
            message = str(document.get("Message", "") or "")
            code_text = str(code)
            raise ProviderError(
                f"aliyun: {code_text} {message}".strip(),
                provider=self.name,
                code=code_text,
                retryable=_retryable_code(code_text),
                auth=_auth_code(code_text),
            )
        return document

    def _signed_headers(self, action: str, body: bytes, timestamp: float) -> dict[str, str]:
        cfg = self.config
        nonce = uuid.uuid4().hex
        date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        content_type = "application/json"
        payload_hash = sha256_hex(body)
        signed: dict[str, str] = {
            "content-type": content_type,
            "host": self.endpoint,
            "x-acs-action": action,
            "x-acs-content-sha256": payload_hash,
            "x-acs-date": date,
            "x-acs-signature-nonce": nonce,
            "x-acs-version": self.version,
        }
        names = sorted(signed)
        canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
        signed_headers = ";".join(names)
        canonical_request = (
            "POST\n"
            "/\n"
            "\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )
        string_to_sign = (
            "ACS3-HMAC-SHA256\n" + sha256_hex(canonical_request.encode("utf-8"))
        )
        signature = hmac_sha256_hex(
            cfg.access_key_secret.encode("utf-8"), string_to_sign.encode("utf-8")
        )
        authorization = (
            f"ACS3-HMAC-SHA256 Credential={cfg.access_key_id},"
            f"SignedHeaders={signed_headers},Signature={signature}"
        )
        headers = {name: value for name, value in signed.items()}
        headers["Authorization"] = authorization
        return headers

    def _parse(self, response: Mapping[str, Any], *, default_label: str) -> Verdict:
        request_id = str(response.get("RequestId", "") or "")
        data = response.get("Data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        risk = str(data.get("riskLevel", "") or data.get("RiskLevel", "") or "")
        suggestion = suggestion_from_risk(risk)
        labels: list[str] = []
        raw_labels = data.get("labels", data.get("Labels", ()))
        if isinstance(raw_labels, str):
            labels.extend(part for part in raw_labels.replace(";", ",").split(",") if part)
        elif isinstance(raw_labels, (list, tuple)):
            labels.extend(str(part) for part in raw_labels)
        # 增强版（TextModerationPlus）把结果放在 Result[] 里。
        results = data.get("Result") or data.get("result")
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("Label", "") or item.get("label", "") or "")
                if label:
                    labels.append(label)
                if not suggestion or suggestion == "pass":
                    suggestion = suggestion_from_risk(
                        str(item.get("RiskLevel", "") or item.get("riskLevel", "") or "")
                    )
        if not labels:
            labels.append(default_label)
        score = None
        confidence = data.get("confidence", data.get("Confidence"))
        try:
            score = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            score = None
        return verdict(
            provider=self.name,
            suggestion=suggestion,
            labels=normalize_labels(labels),
            score=score,
            request_id=request_id,
            raw_code=risk,
        )


def _retryable_code(code: str) -> bool:
    text = (code or "").lower()
    if not text:
        return True
    return any(
        marker in text
        for marker in ("throttling", "serviceunavailable", "internalerror", "timeout", "500")
    )


def _auth_code(code: str) -> bool:
    text = (code or "").lower()
    return any(
        marker in text
        for marker in ("signature", "forbidden", "unauthorized", "invalidaccesskey")
    )
