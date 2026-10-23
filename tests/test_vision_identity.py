"""以图搜图兜底（SauceNAO）：触发条件、每日限额、失败不编造、key 不外泄。

全部离线：``HTTPClient`` 用一个只记录调用并回放固定响应的 fake 替代，因此这些
用例既不需要 key，也不会发真实请求。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atri_x.vision import (
    FAILURE_IDENTITY_DISABLED,
    FAILURE_IDENTITY_ERROR,
    FAILURE_IDENTITY_QUOTA,
    DailyQuota,
    IdentityConfig,
    identity_reference,
    identity_requested,
    parse_saucenao_response,
    quota_for,
    reset_identity_state,
)

_TMP_DIR = Path(__file__).resolve().parent / "_tmp_vision_identity"


class _FakeResponse:
    def __init__(self, status: int = 200, payload: object = None) -> None:
        self.status = status
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _FakeHTTP:
    """只记录调用并回放 ``response``（或抛出 ``error``）的 multipart 假客户端。"""

    def __init__(
        self,
        response: object = None,
        error: BaseException | None = None,
        payload: object = None,
        status: int = 200,
    ) -> None:
        self.response = response if response is not None else _FakeResponse(status, payload)
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def request_multipart(self, method: str, path: str, **kwargs: object):
        self.calls.append({"method": method, "path": path, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def _saucenao_payload() -> dict[str, object]:
    return {
        "header": {"status": 0, "results_returned": 2},
        "results": [
            {
                "header": {"similarity": "93.5", "index_id": 5},
                "data": {
                    "title": "某作品 第 1 话",
                    "author_name": "某作者",
                    "ext_urls": ["https://example.com/a"],
                },
            },
            {
                "header": {"similarity": "41.0", "index_id": 5},
                "data": {"title": "不相关", "author_name": "x"},
            },
        ],
    }


class TestIdentityQuestion(unittest.TestCase):
    def test_matches_chinese_questions(self) -> None:
        for text in (
            "这是谁？",
            "这个什么角色",
            "哪个角色啊",
            "她是谁",
            "出自哪部动画",
            "这是什么作品",
        ):
            self.assertTrue(identity_requested(text), text)

    def test_matches_english_questions(self) -> None:
        for text in (
            "Who is this?",
            "which character is that",
            "what anime is this from",
            "source of this image please",
        ):
            self.assertTrue(identity_requested(text), text)

    def test_ignores_non_identity_talk(self) -> None:
        for text in ("画得真好看", "帮我反推提示词", "这张图的配色不错", ""):
            self.assertFalse(identity_requested(text), text)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class TestDailyQuota(unittest.TestCase):
    def setUp(self) -> None:
        reset_identity_state()
        _TMP_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        reset_identity_state()
        for path in _TMP_DIR.glob("*"):
            try:
                path.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响结论
                pass
        try:
            _TMP_DIR.rmdir()
        except OSError:  # pragma: no cover
            pass

    def test_limit_stops_further_requests(self) -> None:
        quota = DailyQuota(2)
        self.assertTrue(quota.try_acquire())
        self.assertTrue(quota.try_acquire())
        self.assertFalse(quota.try_acquire())
        self.assertEqual(quota.used(), 2)
        self.assertEqual(quota.remaining(), 0)

    def test_zero_limit_means_unlimited(self) -> None:
        quota = DailyQuota(0)
        for _ in range(5):
            self.assertTrue(quota.try_acquire())
        self.assertEqual(quota.remaining(), -1)

    def test_rolls_over_at_utc_midnight(self) -> None:
        clock = _Clock(datetime(2026, 2, 13, 23, 59, 30, tzinfo=timezone.utc))
        quota = DailyQuota(1, now=clock)
        self.assertTrue(quota.try_acquire())
        self.assertFalse(quota.try_acquire())
        clock.advance(60)  # 跨过 UTC 日切
        self.assertEqual(quota.used(), 0)
        self.assertTrue(quota.try_acquire())

    def test_persists_count_across_instances(self) -> None:
        state = _TMP_DIR / "quota.json"
        first = DailyQuota(5, str(state))
        self.assertTrue(first.try_acquire())
        second = DailyQuota(5, str(state))
        self.assertEqual(second.used(), 1)
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["count"], 1)

    def test_corrupt_state_falls_back_to_zero(self) -> None:
        state = _TMP_DIR / "broken.json"
        state.write_text("{not json", encoding="utf-8")
        quota = DailyQuota(5, str(state))
        self.assertEqual(quota.used(), 0)
        self.assertTrue(quota.try_acquire())

    def test_quota_for_is_shared_per_state_path(self) -> None:
        config = IdentityConfig(enabled=True, daily_limit=1)
        first = quota_for(config)
        second = quota_for(config)
        self.assertIs(first, second)
        self.assertTrue(first.try_acquire())
        self.assertFalse(second.try_acquire())


class TestSaucenaoParsing(unittest.TestCase):
    def test_filters_by_similarity_and_keeps_order(self) -> None:
        response = _FakeResponse(payload=_saucenao_payload())
        candidates = parse_saucenao_response(response, 0.7, 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "某作品 第 1 话")
        self.assertEqual(candidates[0].author, "某作者")
        self.assertAlmostEqual(candidates[0].similarity, 0.935)
        self.assertEqual(candidates[0].urls, ["https://example.com/a"])

    def test_non_2xx_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_saucenao_response(_FakeResponse(status=500, payload={}), 0.7, 3)

    def test_upstream_header_status_raises(self) -> None:
        payload = {"header": {"status": -1}, "results": []}
        with self.assertRaises(ValueError) as caught:
            parse_saucenao_response(_FakeResponse(payload=payload), 0.7, 3)
        self.assertIn("-1", str(caught.exception))

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_saucenao_response(_FakeResponse(payload=ValueError("bad")), 0.7, 3)

    def test_no_results_returns_empty(self) -> None:
        payload = {"header": {"status": 0}, "results": []}
        self.assertEqual(parse_saucenao_response(_FakeResponse(payload=payload), 0.7, 3), [])

    def test_max_candidates(self) -> None:
        payload = _saucenao_payload()
        payload["results"] = payload["results"] * 3
        candidates = parse_saucenao_response(_FakeResponse(payload=payload), 0.5, 2)
        self.assertEqual(len(candidates), 2)


class TestIdentityReferenceGate(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_identity_state()

    def tearDown(self) -> None:
        reset_identity_state()

    async def test_disabled_returns_disabled_without_request(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁？",
            identity_confidence="low",
            config=IdentityConfig(enabled=False),
            client=http,
        )
        self.assertEqual(text, "")
        self.assertEqual(failure, FAILURE_IDENTITY_DISABLED)
        self.assertEqual(http.calls, [])

    async def test_missing_key_is_disabled(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁？",
            identity_confidence="low",
            config=IdentityConfig(enabled=True, api_key="", api_key_env="NOT_SET_HERE"),
            client=http,
            environ={},
        )
        self.assertEqual(text, "")
        self.assertEqual(failure, FAILURE_IDENTITY_DISABLED)
        self.assertEqual(http.calls, [])

    async def test_key_from_env(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        config = IdentityConfig(enabled=True, api_key="", api_key_env="ATRI_TEST_SAUCENAO")
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁？",
            identity_confidence="low",
            config=config,
            client=http,
            environ={"ATRI_TEST_SAUCENAO": "env-secret"},
        )
        self.assertEqual(failure, "")
        self.assertIn("某作品", text)
        self.assertNotIn("env-secret", text)

    async def test_disabled_confidence_is_not_a_failure(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁？",
            identity_confidence="high",
            config=IdentityConfig(enabled=True, api_key="k"),
            client=http,
        )
        self.assertEqual((text, failure), ("", ""))
        self.assertEqual(http.calls, [])

    async def test_not_asked_is_not_a_failure(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="画得真好看",
            identity_confidence="low",
            config=IdentityConfig(enabled=True, api_key="k"),
            client=http,
        )
        self.assertEqual((text, failure), ("", ""))
        self.assertEqual(http.calls, [])

    async def test_success_returns_reference_and_hides_key(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=IdentityConfig(enabled=True, api_key="tok-123", daily_limit=5),
            client=http,
        )
        self.assertEqual(failure, "")
        self.assertTrue(text.startswith("[以图搜图参考] "))
        self.assertTrue(text.endswith("（外部服务，仅供参考）"))
        self.assertIn("某作品 第 1 话", text)
        self.assertIn("相似度 93.50%", text)
        self.assertNotIn("tok-123", text)
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("saucenao.com", call["path"])
        fields = call["kwargs"]["fields"]
        self.assertEqual(fields["output_type"], "2")
        self.assertEqual(fields["db"], "999")
        self.assertEqual(fields["api_key"], "tok-123")
        files = call["kwargs"]["files"]
        self.assertEqual(files[0][0], "file")

    async def test_quota_exhausted_does_not_send(self) -> None:
        http = _FakeHTTP(payload=_saucenao_payload())
        config = IdentityConfig(enabled=True, api_key="k", daily_limit=1)
        first = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=config,
            client=http,
        )
        self.assertEqual(first[1], "")
        second = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=config,
            client=http,
        )
        self.assertEqual(second[0], "")
        self.assertEqual(second[1], FAILURE_IDENTITY_QUOTA)
        self.assertEqual(len(http.calls), 1)

    async def test_upstream_error_is_reported_not_faked(self) -> None:
        http = _FakeHTTP(error=RuntimeError("boom tok-123"))
        logs: list[str] = []

        class _Logger:
            def warning(self, message: str, *args: object) -> None:
                logs.append(message % args if args else message)

        text, failure = await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=IdentityConfig(enabled=True, api_key="tok-123", daily_limit=5),
            client=http,
            logger=_Logger(),
        )
        self.assertEqual(text, "")
        self.assertEqual(failure, FAILURE_IDENTITY_ERROR)
        self.assertTrue(logs)
        joined = " ".join(logs)
        self.assertIn(FAILURE_IDENTITY_ERROR, joined)
        self.assertNotIn("tok-123", joined)

    async def test_logger_never_sees_the_key_on_quota(self) -> None:
        logs: list[str] = []

        class _Logger:
            def warning(self, message: str, *args: object) -> None:
                logs.append(message % args if args else message)

        config = IdentityConfig(enabled=True, api_key="tok-999", daily_limit=1)
        http = _FakeHTTP(payload=_saucenao_payload())
        await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=config,
            client=http,
            logger=_Logger(),
        )
        await identity_reference(
            None,
            b"img",
            "image/png",
            user_text="这是谁",
            identity_confidence="low",
            config=config,
            client=http,
            logger=_Logger(),
        )
        self.assertIn(FAILURE_IDENTITY_QUOTA, " ".join(logs))
        self.assertNotIn("tok-999", " ".join(logs))


if __name__ == "__main__":
    unittest.main()
