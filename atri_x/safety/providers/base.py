"""远程审核供应商的公共契约：传输层、错误分类、签名工具、归一化入口。

设计要点：

* **零第三方依赖**：签名用标准库 ``hmac`` / ``hashlib`` 手写，不引云厂商 SDK；
* **传输层可注入**：默认 :class:`HttpTransport` 走 :mod:`atri_x.net.httpclient`，
  测试传假实现即可离线覆盖"签名 + 响应解析 + 错误分类"；
* **错误要能分类**：引擎按 ``retryable`` / ``auth`` 决定重试、切备、熔断。

**不做什么**：不做重试/熔断/双供应商——那是 :mod:`atri_x.safety.engine` 的事。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from atri_x.safety.types import (
    TIER_BLOCK,
    TIER_PASS,
    TIER_REVIEW,
    Verdict,
    normalize_labels,
)

__all__ = [
    "HttpTransport",
    "ModerationProvider",
    "ProviderConfig",
    "ProviderError",
    "Transport",
    "build_json_body",
    "decode_json",
    "hmac_sha256",
    "hmac_sha256_hex",
    "sha256_hex",
    "suggestion_from_risk",
]

#: 供应商返回的"风险等级/建议" → 档位。
_RISK_TO_TIER: dict[str, str] = {
    # 腾讯云：Pass / Review / Block
    "pass": TIER_PASS,
    "review": TIER_REVIEW,
    "block": TIER_BLOCK,
    # 阿里云 2.0：riskLevel = none / low / medium / high
    "none": TIER_PASS,
    "low": TIER_PASS,
    "medium": TIER_REVIEW,
    "high": TIER_BLOCK,
}


def suggestion_from_risk(value: str) -> str:
    """把供应商的风险等级/建议文本归一成 ``pass`` / ``review`` / ``block``。"""
    return _RISK_TO_TIER.get((value or "").strip().lower(), TIER_PASS)


class ProviderError(Exception):
    """一次远程审核调用失败。

    ``retryable`` 决定引擎是否重试；``auth`` 表示"配置错（签名/权限）"，
    引擎应当冷却该供应商而不是反复重试刷日志。
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: str = "",
        retryable: bool = True,
        auth: bool = False,
        status: int = 0,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.auth = auth
        self.status = status


@runtime_checkable
class Transport(Protocol):
    """最小传输契约：发一个 JSON POST，返回 ``(status, body)``。"""

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]: ...


class HttpTransport:
    """默认传输：复用 :class:`atri_x.net.httpclient.HTTPClient`（自带取消/重定向处理）。"""

    def __init__(self, *, user_agent: str = "ATRI-X-safety/1.1.8") -> None:
        from atri_x.net.httpclient import HTTPClient

        self._client = HTTPClient(timeout=0.8, headers={"User-Agent": user_agent})

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        response = await self._client.post(
            url, headers=dict(headers), body=body, timeout=timeout
        )
        return int(response.status), bytes(response.body or b"")


@runtime_checkable
class ModerationProvider(Protocol):
    """一个云厂商的审核适配器。"""

    #: 供应商标识（``tencent`` / ``aliyun``），审计里用。
    name: str

    async def moderate_text(self, text: str, *, biz_type: str = "") -> Verdict: ...

    async def moderate_image(
        self, data: bytes, *, mime: str = "", url: str = ""
    ) -> Verdict: ...


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def build_json_body(payload: Mapping[str, Any]) -> bytes:
    """统一的请求体序列化：紧凑 JSON + 不转义中文（与腾讯/阿里的示例一致）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_json(body: bytes, *, provider: str = "") -> dict[str, Any]:
    """把响应体解成 dict；非 JSON 抛 :class:`ProviderError`（不可重试）。"""
    try:
        value = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ProviderError(
            f"{provider}: 响应不是合法 JSON：{exc}", provider=provider, retryable=True
        ) from None
    if not isinstance(value, dict):
        raise ProviderError(
            f"{provider}: 响应顶层不是对象", provider=provider, retryable=True
        )
    return value


def b64(data: bytes) -> str:
    """标准 base64（云厂商的 ``Content`` / ``FileContent`` 字段都要它）。"""
    return base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class ProviderConfig:
    """适配器需要的运行时参数（从 :class:`atri_x.safety.settings.ProviderSettings` 来）。"""

    name: str
    region: str = ""
    endpoint: str = ""
    service: str = ""
    version: str = ""
    biz_type: str = ""
    secret_id: str = ""
    secret_key: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""

    @property
    def has_credentials(self) -> bool:
        if self.name == "tencent":
            return bool(self.secret_id and self.secret_key)
        if self.name == "aliyun":
            return bool(self.access_key_id and self.access_key_secret)
        return False


def verdict(
    *,
    provider: str,
    suggestion: str,
    labels: object = (),
    score: float | None = None,
    request_id: str = "",
    latency_ms: int = 0,
    raw_code: str = "",
) -> Verdict:
    """构造一条供应商无关的 :class:`Verdict`（把标签归一化收口在这里）。"""
    return Verdict(
        provider=provider,
        suggestion=suggestion,
        labels=normalize_labels(labels),
        score=score,
        request_id=request_id,
        latency_ms=latency_ms,
        raw_code=raw_code,
    )
