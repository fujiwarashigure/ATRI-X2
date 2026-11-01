"""腾讯云 / 阿里云适配器：签名形状、响应归一化、错误分类（假传输，离线）。"""

from __future__ import annotations

import base64
import json
import os
import unittest

from atri_x.safety.providers import build_providers
from atri_x.safety.providers.aliyun import AliyunProvider
from atri_x.safety.providers.base import ProviderConfig, ProviderError
from atri_x.safety.providers.tencent import TencentProvider
from atri_x.safety.settings import load_safety_settings

FIXED_TIME = 1700000000


class FakeTransport:
    """记录请求、回放固定响应的假传输。"""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict[str, object]] = []

    async def post_json(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.requests.append(
            {"url": url, "headers": dict(headers), "body": bytes(body), "timeout": timeout}
        )
        return self.status, self.body


def tencent_config(**kwargs) -> ProviderConfig:
    base = {
        "name": "tencent",
        "region": "ap-guangzhou",
        "secret_id": "AKIDEXAMPLE",
        "secret_key": "SECRETEXAMPLE",
        "service": "tms",
    }
    base.update(kwargs)
    return ProviderConfig(**base)


def aliyun_config(**kwargs) -> ProviderConfig:
    base = {
        "name": "aliyun",
        "endpoint": "green-cip.cn-shanghai.aliyuncs.com",
        "access_key_id": "LTAIEXAMPLE",
        "access_key_secret": "SECRETEXAMPLE",
        "service": "comment_detection",
    }
    base.update(kwargs)
    return ProviderConfig(**base)


def tencent_body(data: dict, request_id: str = "req-1") -> bytes:
    return json.dumps({"Response": {"RequestId": request_id, "Data": data}}).encode("utf-8")


class TestTencent(unittest.TestCase):
    def make(self, transport: FakeTransport) -> TencentProvider:
        return TencentProvider(tencent_config(), transport=transport, clock=lambda: FIXED_TIME)

    def test_text_request_is_signed_and_base64_encoded(self) -> None:
        transport = FakeTransport(
            body=tencent_body(
                {"Label": "Porn", "Suggestion": "Block", "Score": 92, "SubLabel": "porn_hot"}
            )
        )
        provider = self.make(transport)
        verdict = asyncio_run(provider.moderate_text("你好世界", biz_type="biz-1"))
        self.assertEqual(verdict.suggestion, "block")
        self.assertIn("porn", verdict.labels)
        self.assertEqual(verdict.request_id, "req-1")
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://tms.tencentcloudapi.com/")
        headers = request["headers"]
        self.assertTrue(str(headers["Authorization"]).startswith("TC3-HMAC-SHA256 "))
        self.assertIn("Credential=AKIDEXAMPLE/", str(headers["Authorization"]))
        self.assertEqual(headers["X-TC-Action"], "TextModeration")
        self.assertEqual(headers["X-TC-Region"], "ap-guangzhou")
        payload = json.loads(bytes(request["body"]).decode("utf-8"))
        self.assertEqual(base64.b64decode(payload["Content"]).decode("utf-8"), "你好世界")
        self.assertEqual(payload["BizType"], "biz-1")

    def test_signature_is_deterministic(self) -> None:
        transport = FakeTransport(body=tencent_body({"Suggestion": "Pass"}))
        provider = self.make(transport)
        asyncio_run(provider.moderate_text("同一条文本"))
        asyncio_run(provider.moderate_text("同一条文本"))
        first = transport.requests[0]["headers"]["Authorization"]
        second = transport.requests[1]["headers"]["Authorization"]
        self.assertEqual(first, second)

    def test_review_suggestion_and_detail_labels(self) -> None:
        transport = FakeTransport(
            body=tencent_body(
                {
                    "Label": "Ad",
                    "Suggestion": "Review",
                    "DetailResults": [{"Label": "Sexy"}, {"Label": "Ad"}],
                }
            )
        )
        provider = self.make(transport)
        verdict = asyncio_run(provider.moderate_text("广告"))
        self.assertEqual(verdict.suggestion, "review")
        self.assertIn("ad", verdict.labels)
        self.assertIn("porn", verdict.labels)

    def test_error_code_is_classified(self) -> None:
        body = json.dumps(
            {"Response": {"Error": {"Code": "RequestLimitExceeded", "Message": "too many"}}}
        ).encode("utf-8")
        provider = self.make(FakeTransport(body=body))
        with self.assertRaises(ProviderError) as ctx:
            asyncio_run(provider.moderate_text("文本"))
        self.assertTrue(ctx.exception.retryable)
        self.assertFalse(ctx.exception.auth)

    def test_auth_error_is_not_retryable(self) -> None:
        body = json.dumps(
            {"Response": {"Error": {"Code": "AuthFailure.SecretIdNotFound", "Message": "bad"}}}
        ).encode("utf-8")
        provider = self.make(FakeTransport(body=body))
        with self.assertRaises(ProviderError) as ctx:
            asyncio_run(provider.moderate_text("文本"))
        self.assertTrue(ctx.exception.auth)

    def test_image_uses_file_url_when_given(self) -> None:
        transport = FakeTransport(body=tencent_body({"Suggestion": "Pass"}))
        provider = self.make(transport)
        asyncio_run(provider.moderate_image(b"", url="https://example.com/a.jpg"))
        payload = json.loads(bytes(transport.requests[0]["body"]).decode("utf-8"))
        self.assertEqual(payload["FileUrl"], "https://example.com/a.jpg")
        self.assertEqual(transport.requests[0]["headers"]["X-TC-Action"], "ImageModeration")


class TestAliyun(unittest.TestCase):
    def make(self, transport: FakeTransport) -> AliyunProvider:
        return AliyunProvider(aliyun_config(), transport=transport, clock=lambda: FIXED_TIME)

    def test_text_request_shape_and_parse(self) -> None:
        data = {"riskLevel": "high", "labels": "porn,abuse", "confidence": 0.91}
        body = json.dumps(
            {"RequestId": "ali-1", "Code": "200", "Data": json.dumps(data)}
        ).encode("utf-8")
        transport = FakeTransport(body=body)
        provider = self.make(transport)
        verdict = asyncio_run(provider.moderate_text("内容"))
        self.assertEqual(verdict.suggestion, "block")
        self.assertIn("porn", verdict.labels)
        self.assertIn("abuse", verdict.labels)
        self.assertEqual(verdict.request_id, "ali-1")
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://green-cip.cn-shanghai.aliyuncs.com/")
        headers = request["headers"]
        self.assertTrue(str(headers["Authorization"]).startswith("ACS3-HMAC-SHA256 "))
        self.assertIn("Credential=LTAIEXAMPLE", str(headers["Authorization"]))
        payload = json.loads(bytes(request["body"]).decode("utf-8"))
        self.assertEqual(payload["Service"], "comment_detection")
        params = json.loads(payload["ServiceParameters"])
        self.assertEqual(params["content"], "内容")

    def test_medium_risk_maps_to_review(self) -> None:
        data = {"riskLevel": "medium", "labels": "ad"}
        body = json.dumps({"RequestId": "ali-2", "Code": "200", "Data": json.dumps(data)}).encode()
        provider = self.make(FakeTransport(body=body))
        self.assertEqual(asyncio_run(provider.moderate_text("x")).suggestion, "review")

    def test_image_without_url_is_not_retryable(self) -> None:
        provider = self.make(FakeTransport())
        with self.assertRaises(ProviderError) as ctx:
            asyncio_run(provider.moderate_image(b"\x89PNG"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "image_requires_url")

    def test_error_code_in_http_200_is_raised(self) -> None:
        body = json.dumps({"RequestId": "x", "Code": "Throttling", "Message": "slow"}).encode()
        provider = self.make(FakeTransport(body=body))
        with self.assertRaises(ProviderError) as ctx:
            asyncio_run(provider.moderate_text("x"))
        self.assertTrue(ctx.exception.retryable)


class TestBuildProviders(unittest.TestCase):
    def test_missing_credentials_skips_provider(self) -> None:
        settings, _ = load_safety_settings(None)
        settings.remote.enabled = True
        providers = build_providers(settings, config_dir="")
        self.assertEqual(providers, {})

    def test_env_credentials_are_picked_up(self) -> None:
        settings, _ = load_safety_settings(None)
        settings.remote.enabled = True
        settings.remote.tencent.secret_id_env = "TEST_TENCENT_ID"
        settings.remote.tencent.secret_key_env = "TEST_TENCENT_KEY"
        os.environ["TEST_TENCENT_ID"] = "AKIDTEST"
        os.environ["TEST_TENCENT_KEY"] = "KEYTEST"
        try:
            providers = build_providers(settings, config_dir="")
        finally:
            os.environ.pop("TEST_TENCENT_ID", None)
            os.environ.pop("TEST_TENCENT_KEY", None)
        self.assertIn("tencent", providers)
        self.assertNotIn("aliyun", providers)


def asyncio_run(coro):  # noqa: ANN001, ANN201 - 与仓库其它测试一致的小助手
    import asyncio

    return asyncio.run(coro)
