"""以图搜图兜底（SauceNAO）：只在「画面属性确定不了身份」时补一条外部参考。

对应 Go：无（Go 版没有以图搜图）。两段式识图的第一段只允许视觉模型描述
**可观察属性**，并给出 ``identity_confidence``；当它明确说「仅凭画面定不了是谁」
且用户这一轮确实在问身份时，才把图发到 SauceNAO 查一次，把结果作为
「[以图搜图参考] …（外部服务，仅供参考）」追加到描述文本后面。

几条硬约束：

* **默认关闭**（``identity_enabled`` 默认 ``False``），且必须有 API key；
* **每日限额**按 UTC 日切，进程内计数 + 可选的 JSON 持久化；超限时**不发请求**，
  只记 ``identity_quota``；
* 失败只记原因（:data:`atri_x.vision.failures.FAILURE_KINDS`），**绝不编造**
  作品名/角色名；
* API key 只出现在 multipart 请求体里：不进日志、不进审计字段、不进异常文本。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_MAX_CANDIDATES",
    "DailyQuota",
    "IDENTITY_REFERENCE_PREFIX",
    "IDENTITY_REFERENCE_SUFFIX",
    "IdentityCandidate",
    "IdentityConfig",
    "SauceNaoClient",
    "identity_reference",
    "identity_requested",
    "parse_saucenao_response",
    "quota_for",
    "reset_identity_state",
]

#: SauceNAO 检索端点。
DEFAULT_ENDPOINT = "https://saucenao.com/search.php"
#: 一次检索最多回报几个候选（够主模型判断，又不至于把答案淹掉）。
DEFAULT_MAX_CANDIDATES = 3
#: 参考段落的前后缀：后缀里必须写明这是外部服务，避免被当成画面事实。
IDENTITY_REFERENCE_PREFIX = "[以图搜图参考] "
IDENTITY_REFERENCE_SUFFIX = "（外部服务，仅供参考）"

#: 「用户这一轮在问身份」的判定。只匹配明确的身份问句，宁可漏判也不乱发图。
_IDENTITY_QUESTION = re.compile(
    r"这是谁|什么角色|哪个角色|是谁|出自哪|什么作品|哪部作品|"
    r"who\s+is\s+(this|that|she|he|it)|which\s+character|what\s+anime|"
    r"source\s+of\s+this\s+image|identify\s+(this|the)\s+(character|image)",
    re.IGNORECASE,
)


@dataclass
class IdentityCandidate:
    """一条以图搜图候选。``urls`` 只保留外部链接，不含任何凭据。"""

    title: str = ""
    author: str = ""
    similarity: float = 0.0
    urls: list[str] = field(default_factory=list)

    def render(self) -> str:
        """渲染成一行可读参考（相似度按百分数给两位小数）。"""
        parts: list[str] = []
        if self.similarity > 0:
            parts.append("相似度 %.2f%%" % (self.similarity * 100))
        if self.title:
            parts.append(self.title)
        if self.author:
            parts.append("作者：" + self.author)
        if self.urls:
            parts.append("来源：" + self.urls[0])
        return "；".join(parts)


@dataclass
class IdentityConfig:
    """以图搜图配置。字段名与 :class:`atri_x.vision.service.Options` 对齐。"""

    enabled: bool = False
    api_key: str = ""
    api_key_env: str = "SAUCENAO_API_KEY"
    daily_limit: int = 100
    timeout_seconds: float = 8.0
    min_similarity: float = 0.7
    state_path: str = ""
    endpoint: str = DEFAULT_ENDPOINT

    @classmethod
    def from_options(cls, opts: Any) -> "IdentityConfig":
        """从视觉 ``Options``（或任何同名属性的对象）构造；缺字段用默认值。"""
        return cls(
            enabled=bool(getattr(opts, "identity_enabled", False)),
            api_key=str(getattr(opts, "identity_api_key", "") or ""),
            api_key_env=str(
                getattr(opts, "identity_api_key_env", "SAUCENAO_API_KEY")
                or "SAUCENAO_API_KEY"
            ),
            daily_limit=int(getattr(opts, "identity_daily_limit", 100) or 0),
            timeout_seconds=float(getattr(opts, "identity_timeout_seconds", 8.0) or 0),
            min_similarity=float(getattr(opts, "identity_min_similarity", 0.7) or 0),
            state_path=str(getattr(opts, "identity_state_path", "") or ""),
        )

    def resolve_api_key(self, environ: Any = None) -> str:
        """优先显式 key，否则按 :attr:`api_key_env` 读环境变量。"""
        key = (self.api_key or "").strip()
        if key:
            return key
        name = (self.api_key_env or "").strip()
        if not name:
            return ""
        source = environ if environ is not None else os.environ
        try:
            value = source.get(name, "")
        except Exception:  # noqa: BLE001 - 环境变量不可读时当作没有 key
            return ""
        return str(value or "").strip()

    def resolved_daily_limit(self) -> int:
        """``<= 0`` 视为不限额（显式关掉限额时才允许）。"""
        return max(0, int(self.daily_limit))


class DailyQuota:
    """按 UTC 日切的一次性配额。

    ``try_acquire`` 是**先扣后发**：并发下也不可能超过上限。进程内共享同一个
    对象（见 :func:`quota_for`），可选地把 ``{"day": ..., "count": ...}`` 落到
    磁盘，重启后不会白送一批额度。
    """

    def __init__(
        self,
        limit: int,
        state_path: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.limit = max(0, int(limit))
        self.state_path = (state_path or "").strip()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._day = ""
        self._count = 0
        self._load()

    # -- 公开 ---------------------------------------------------------------
    def try_acquire(self) -> bool:
        """占一次额度；超限返回 ``False``（调用方据此**不发请求**）。"""
        with self._lock:
            self._roll_locked()
            if self.limit > 0 and self._count >= self.limit:
                return False
            self._count += 1
            self._save_locked()
            return True

    def used(self) -> int:
        """当日的已用次数。"""
        with self._lock:
            self._roll_locked()
            return self._count

    def remaining(self) -> int:
        """当日剩余次数；不限额时返回 ``-1``。"""
        with self._lock:
            self._roll_locked()
            if self.limit <= 0:
                return -1
            return max(0, self.limit - self._count)

    # -- 内部 ---------------------------------------------------------------
    def _today(self) -> str:
        try:
            current = self._now()
        except Exception:  # noqa: BLE001 - 时钟不可用时退回真实时间
            current = datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _roll_locked(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._count = 0

    def _load(self) -> None:
        if not self.state_path:
            self._day = self._today()
            return
        try:
            raw = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._day = self._today()
            return
        if not isinstance(raw, dict):
            self._day = self._today()
            return
        day = str(raw.get("day") or "")
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        self._day = day if day else self._today()
        self._count = max(0, count) if day == self._today() else 0
        self._day = self._today()

    def _save_locked(self) -> None:
        if not self.state_path:
            return
        payload = json.dumps({"day": self._day, "count": self._count})
        path = Path(self.state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 同目录临时文件 + replace：宁可丢掉一次计数，也不要留下半截 JSON。
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(temp, path)
        except OSError:
            # 持久化失败不影响本进程的限额判定。
            return


_QUOTA_LOCK = threading.Lock()
_QUOTAS: dict[str, DailyQuota] = {}


def quota_for(
    config: IdentityConfig, now: Callable[[], datetime] | None = None
) -> DailyQuota:
    """取（或创建）该配置对应的进程级配额对象。"""
    key = config.state_path.strip() or "__memory__"
    limit = config.resolved_daily_limit()
    with _QUOTA_LOCK:
        quota = _QUOTAS.get(key)
        if quota is None or quota.limit != limit:
            quota = DailyQuota(limit, config.state_path, now)
            _QUOTAS[key] = quota
        return quota


def reset_identity_state() -> None:
    """清空进程级配额（测试用）。"""
    with _QUOTA_LOCK:
        _QUOTAS.clear()


def identity_requested(user_text: str) -> bool:
    """用户本轮文本是否明确在问「这是谁 / 出自哪」。"""
    return bool(_IDENTITY_QUESTION.search(str(user_text or "")))


class SauceNaoClient:
    """SauceNAO 检索的最小异步客户端（纯标准库，走共享 ``HTTPClient``）。

    ``http`` 可注入任意带 ``request_multipart`` 的对象，测试因此不需要联网。
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 8.0,
        min_similarity: float = 0.7,
        http: Any = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.timeout = timeout if timeout > 0 else 8.0
        self.min_similarity = min_similarity if min_similarity > 0 else 0.7
        self.max_candidates = max_candidates if max_candidates > 0 else 1
        self.http = http

    async def search(
        self, ctx: Any, data: bytes, mime_type: str = ""
    ) -> list[IdentityCandidate]:
        """检索一次，返回按相似度降序、已过滤的候选（失败抛异常）。

        请求体里带 ``output_type=2``（JSON）、``db=999``（全库）、``api_key`` 与
        ``file``；key 只存在于这个 multipart 的 fields 里。
        """
        if not (self.api_key or "").strip():
            raise ValueError("identity: api key is missing")
        if not data:
            raise ValueError("identity: empty image")
        filename = "image." + _extension_of(mime_type)
        response = await self._client().request_multipart(
            "POST",
            self.endpoint,
            fields={
                "output_type": "2",
                "db": "999",
                "api_key": self.api_key,
            },
            files=[("file", filename, data, mime_type or "application/octet-stream")],
            timeout=self.timeout,
            cancel=ctx,
        )
        return parse_saucenao_response(
            response, self.min_similarity, self.max_candidates
        )

    def _client(self) -> Any:
        if self.http is not None:
            return self.http
        from atri_x.net.httpclient import HTTPClient

        self.http = HTTPClient(timeout=self.timeout)
        return self.http


def parse_saucenao_response(
    response: Any, min_similarity: float, max_candidates: int
) -> list[IdentityCandidate]:
    """解析 SauceNAO 的 ``output_type=2`` 响应。

    HTTP 非 2xx、JSON 损坏、``header.status`` 非 0 都抛 ``ValueError``（调用方记
    ``identity_error``）；正常但对不上任何候选时返回空列表（不编造）。
    """
    status = int(getattr(response, "status", 0) or 0)
    if status < 200 or status >= 300:
        raise ValueError(f"identity: upstream status {status}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("identity: response is not JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("identity: response is not a JSON object")
    header = payload.get("header")
    if isinstance(header, dict):
        code = header.get("status")
        try:
            code_value = int(code)
        except (TypeError, ValueError):
            code_value = 0
        if code_value != 0:
            # 只带上游状态码，不带 message：它可能回显请求参数。
            raise ValueError(f"identity: upstream status {code_value}")
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out: list[IdentityCandidate] = []
    for item in results:
        candidate = _candidate_of(item)
        if candidate is None or candidate.similarity < min_similarity:
            continue
        out.append(candidate)
        if len(out) >= max_candidates:
            break
    return out


def _candidate_of(item: Any) -> IdentityCandidate | None:
    if not isinstance(item, dict):
        return None
    header = item.get("header") if isinstance(item.get("header"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    try:
        similarity = float(header.get("similarity") or 0.0)
    except (TypeError, ValueError):
        similarity = 0.0
    # SauceNAO 的 similarity 是百分数（"93.5" 表示 93.5%）；也接受已归一化的 0~1。
    if similarity > 1.0:
        similarity = similarity / 100.0
    similarity = max(0.0, min(1.0, similarity))
    urls: list[str] = []
    for value in _as_list(data.get("ext_urls")):
        text = str(value or "").strip()
        if text and text.startswith(("http://", "https://")):
            urls.append(text)
    title = _first_text(data, "title", "eng_name", "jp_name")
    author = _first_text(data, "author_name", "member_name", "creator")
    if not title and not author and not urls:
        return None
    return IdentityCandidate(
        title=title, author=author, similarity=similarity, urls=urls
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def _first_text(raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:200]
    return ""


def _extension_of(mime_type: str) -> str:
    folded = (mime_type or "").strip().lower()
    if "jpeg" in folded or "jpg" in folded:
        return "jpg"
    if "webp" in folded:
        return "webp"
    if "gif" in folded:
        return "gif"
    return "png"


async def identity_reference(
    ctx: Any,
    data: bytes,
    mime_type: str,
    *,
    user_text: str,
    identity_confidence: str,
    config: IdentityConfig | None = None,
    client: Any = None,
    logger: Any = None,
    now: Callable[[], datetime] | None = None,
    environ: Any = None,
) -> tuple[str, str]:
    """按触发条件决定要不要检索，返回 ``(参考文本, 失败类别)``。

    只有**全部**条件满足才会发请求：开启 + 有 key + 当日未超限 + 用户本轮明确
    问身份 + 第一段 ``identity_confidence == "low"``。不满足时返回 ``("", "")``
    （不是失败）；真正的失败返回 :data:`atri_x.vision.failures` 里的类别。
    """
    from atri_x.vision.failures import (
        FAILURE_IDENTITY_DISABLED,
        FAILURE_IDENTITY_ERROR,
        FAILURE_IDENTITY_QUOTA,
    )

    if not identity_requested(user_text):
        return "", ""
    if (identity_confidence or "").strip().lower() != "low":
        return "", ""
    cfg = config or IdentityConfig()
    if not cfg.enabled:
        return "", FAILURE_IDENTITY_DISABLED
    api_key = cfg.resolve_api_key(environ)
    if not api_key:
        return "", FAILURE_IDENTITY_DISABLED
    quota = quota_for(cfg, now)
    if not quota.try_acquire():
        _log_identity(logger, FAILURE_IDENTITY_QUOTA)
        return "", FAILURE_IDENTITY_QUOTA
    search_client = _search_client(client, api_key, cfg)
    try:
        candidates = await asyncio.wait_for(
            search_client.search(ctx, data, mime_type),
            timeout=cfg.timeout_seconds if cfg.timeout_seconds > 0 else None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 失败只记原因，绝不编造
        _log_identity(logger, FAILURE_IDENTITY_ERROR, type(exc).__name__)
        return "", FAILURE_IDENTITY_ERROR
    lines = [item.render() for item in candidates if item.render()]
    if not lines:
        return "", ""
    return IDENTITY_REFERENCE_PREFIX + "；".join(lines) + IDENTITY_REFERENCE_SUFFIX, ""


def _search_client(client: Any, api_key: str, cfg: IdentityConfig) -> Any:
    """把注入的 ``client`` 归一成「有 ``search`` 的对象」。

    装配/测试可以注入两种东西：已经实现的检索客户端（有 ``async search``），
    或只实现了 ``request_multipart`` 的共享 HTTP 客户端（会被包进
    :class:`SauceNaoClient`）。两者都不给时按配置自建。
    """
    if client is None:
        return SauceNaoClient(
            api_key,
            endpoint=cfg.endpoint,
            timeout=cfg.timeout_seconds,
            min_similarity=cfg.min_similarity,
        )
    if callable(getattr(client, "search", None)):
        return client
    return SauceNaoClient(
        api_key,
        endpoint=cfg.endpoint,
        timeout=cfg.timeout_seconds,
        min_similarity=cfg.min_similarity,
        http=client,
    )


def _log_identity(logger: Any, kind: str, error_name: str = "-") -> None:
    """只记类别与异常类名：**绝不**记 key、查询参数或图片。"""
    warn = getattr(logger, "warning", None)
    if not callable(warn):
        return
    warn("vision identity failure kind=%s error=%s", kind, error_name)
