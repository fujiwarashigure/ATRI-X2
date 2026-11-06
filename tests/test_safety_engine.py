"""内容安全引擎：双供应商仲裁、失败策略、缓存、熔断、预算、按 scope 覆盖、复核队列。"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest

from atri_x.safety.engine import SafetyEngine, build_engine
from atri_x.safety.providers.base import ProviderError, verdict

TMP_ROOT = os.path.join(os.path.dirname(__file__), "_tmp_safety_engine")

BASE_TIME = 1000.0

REMOTE_TOML = """
[local]
enabled = true
mode = "enforce"

[remote]
enabled = true
providers = ["tencent", "aliyun"]
quorum = "any_block"
fail_mode = "open"
cache_ttl_seconds = 600

[cost]
daily_budget_cny = 0.0
"""


class FakeClock:
    def __init__(self, value: float = BASE_TIME) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeProvider:
    """可编程的假供应商（不联网）。"""

    def __init__(
        self,
        name: str,
        *,
        tier: str = "pass",
        labels: tuple[str, ...] = (),
        score: float | None = None,
        error: Exception | None = None,
        fail_times: int = 0,
    ) -> None:
        self.name = name
        self.tier = tier
        self.labels = labels
        self.score = score
        self.error = error
        self.fail_times = fail_times
        self.calls = 0
        self.texts: list[str] = []
        self.images = 0

    async def moderate_text(self, text: str, *, biz_type: str = ""):
        self.calls += 1
        self.texts.append(text)
        if self.error is not None and (self.fail_times <= 0 or self.calls <= self.fail_times):
            raise self.error
        return verdict(
            provider=self.name,
            suggestion=self.tier,
            labels=self.labels,
            score=self.score,
            request_id=f"{self.name}-{self.calls}",
        )

    async def moderate_image(self, data: bytes, *, mime: str = "", url: str = ""):
        self.images += 1
        return verdict(
            provider=self.name,
            suggestion=self.tier,
            labels=self.labels,
            score=self.score,
            request_id=f"{self.name}-img-{self.images}",
        )


class EngineCase(unittest.TestCase):
    toml_text = REMOTE_TOML

    def setUp(self) -> None:
        self.dir = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.write("safety.toml", self.toml_text)
        self.clock = FakeClock()
        self.engine = SafetyEngine(config_dir=self.dir, clock=self.clock)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def inject(self, *providers: FakeProvider) -> None:
        from atri_x.safety.engine import _ProviderHealth

        self.engine._providers = {item.name: item for item in providers}
        self.engine._health = {item.name: _ProviderHealth() for item in providers}

    def moderate(self, text: str, **kwargs):
        return asyncio.run(self.engine.moderate_text(text, **kwargs))


class TestCacheSafetyBoundaries(EngineCase):
    def test_degraded_outcome_never_cached(self) -> None:
        from atri_x.safety.engine_remote import RemoteOutcome
        self.engine._cache_put("synthetic", RemoteOutcome(tier="pass", degraded=True, fail_mode="open"))
        self.assertIsNone(self.engine._cache_get("synthetic"))

    def test_text_and_image_do_not_share_content_cache(self) -> None:
        import hashlib
        provider = FakeProvider("tencent")
        self.inject(provider)
        content = hashlib.sha256(b"synthetic image").hexdigest()
        self.moderate(content, stage="send")
        asyncio.run(self.engine.moderate_image(b"synthetic image"))
        self.assertEqual(provider.images, 1)


class TestRouting(EngineCase):
    def test_remote_disabled_returns_none(self) -> None:
        self.engine.settings.remote.enabled = False
        self.inject(FakeProvider("tencent", tier="block"))
        self.assertIsNone(self.moderate("任意文本", stage="send"))

    def test_local_only_stage_returns_none(self) -> None:
        provider = FakeProvider("tencent", tier="block")
        self.inject(provider)
        self.assertIsNone(self.moderate("任意文本", stage="tool", tool="web_search"))
        self.assertEqual(provider.calls, 0)

    def test_sync_stage_calls_provider(self) -> None:
        provider = FakeProvider("tencent", tier="block", labels=("porn",))
        self.inject(provider)
        outcome = self.moderate("内容", stage="send")
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.tier, "block")
        self.assertEqual(provider.calls, 1)

    def test_cache_only_stage_misses_then_hits_cache(self) -> None:
        provider = FakeProvider("tencent", tier="review")
        self.inject(provider)
        # 未命中缓存 → 不调远程
        self.assertIsNone(self.moderate("缓存内容", stage="tool", tool="demo_tool"))
        # 先由 send 阶段填充缓存
        first = self.moderate("缓存内容", stage="send")
        assert first is not None
        # 同一文本在 cache_only 工具阶段应命中缓存、不再调用
        second = self.moderate("缓存内容", stage="tool", tool="demo_tool")
        assert second is not None
        self.assertEqual(second.tier, "review")
        self.assertEqual(provider.calls, 1)
        self.assertGreaterEqual(self.engine.stats()["cache_hits"], 1)


class TestQuorum(EngineCase):
    def test_any_block_wins(self) -> None:
        self.inject(
            FakeProvider("tencent", tier="pass"),
            FakeProvider("aliyun", tier="block", labels=("abuse",)),
        )
        outcome = self.moderate("内容", stage="send")
        assert outcome is not None
        self.assertEqual(outcome.tier, "block")
        self.assertIn("abuse", outcome.labels)

    def test_both_pass_requires_all_pass(self) -> None:
        self.engine.settings.remote.quorum = "both_pass"
        self.inject(
            FakeProvider("tencent", tier="pass"),
            FakeProvider("aliyun", tier="review"),
        )
        outcome = self.moderate("内容", stage="send")
        assert outcome is not None
        self.assertEqual(outcome.tier, "review")

    def test_primary_only_uses_first_provider(self) -> None:
        self.engine.settings.remote.quorum = "primary_only"
        primary = FakeProvider("tencent", tier="pass")
        backup = FakeProvider("aliyun", tier="block")
        self.inject(primary, backup)
        outcome = self.moderate("内容", stage="send")
        assert outcome is not None
        self.assertEqual(outcome.tier, "pass")
        self.assertEqual(backup.calls, 0)

    def test_primary_failure_falls_back_to_backup(self) -> None:
        self.engine.settings.remote.quorum = "primary_only"
        primary = FakeProvider("tencent", error=ProviderError("boom", provider="tencent"))
        backup = FakeProvider("aliyun", tier="review")
        self.inject(primary, backup)
        outcome = self.moderate("内容", stage="send")
        assert outcome is not None
        self.assertEqual(outcome.tier, "review")
        self.assertTrue(outcome.degraded)
        self.assertEqual(backup.calls, 1)


class TestFailPolicy(EngineCase):
    def test_all_fail_open_degrades_to_pass(self) -> None:
        self.inject(
            FakeProvider("tencent", error=ProviderError("x", provider="tencent")),
            FakeProvider("aliyun", error=ProviderError("y", provider="aliyun")),
        )
        outcome = self.moderate("内容", stage="send")
        assert outcome is not None
        self.assertEqual(outcome.tier, "pass")
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.fail_mode, "open")
        self.assertEqual(self.engine.stats()["fail_open"], 1)

    def test_fail_close_scope_blocks(self) -> None:
        self.engine.settings.remote.fail_close_scopes = ["qq:group:high"]
        self.inject(FakeProvider("tencent", error=ProviderError("x", provider="tencent")))
        outcome = self.moderate("内容", stage="send", platform="qq", scope_id="group:high")
        assert outcome is not None
        self.assertEqual(outcome.tier, "block")
        self.assertEqual(outcome.fail_mode, "closed")
        self.assertEqual(self.engine.stats()["fail_closed"], 1)

    def test_non_retryable_error_does_not_retry(self) -> None:
        self.engine.settings.remote.retries = 3
        provider = FakeProvider(
            "tencent",
            error=ProviderError("fatal", provider="tencent", retryable=False),
        )
        self.inject(provider)
        self.moderate("内容", stage="send")
        self.assertEqual(provider.calls, 1)

    def test_auth_error_opens_breaker(self) -> None:
        auth = ProviderError("bad key", provider="tencent", auth=True)
        self.inject(FakeProvider("tencent", error=auth))
        self.moderate("内容", stage="send")
        self.assertTrue(self.engine._breaker_open("tencent"))


class TestCircuitBreaker(EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine.settings.remote.retries = 0

    def test_breaker_opens_after_threshold_and_skips_provider(self) -> None:
        provider = FakeProvider("tencent", error=ProviderError("x", provider="tencent"))
        self.inject(provider)
        for index in range(5):
            self.moderate("内容%d" % index, stage="send")
        self.assertEqual(provider.calls, 5)
        self.moderate("熔断后", stage="send")
        self.assertEqual(provider.calls, 5)  # 熔断后不再调用

    def test_breaker_recovers_after_cooldown(self) -> None:
        provider = FakeProvider("tencent", error=ProviderError("x", provider="tencent"))
        self.inject(provider)
        for index in range(5):
            self.moderate("内容%d" % index, stage="send")
        self.clock.advance(61)
        provider.error = None
        provider.tier = "pass"
        self.moderate("恢复后", stage="send")
        self.assertEqual(provider.calls, 6)


class TestCostAndRedaction(EngineCase):
    toml_text = REMOTE_TOML.replace(
        "daily_budget_cny = 0.0", "daily_budget_cny = 0.000001\ntext_price_cny_per_10k = 22.0"
    )

    def test_budget_exceeded_skips_remote(self) -> None:
        provider = FakeProvider("tencent", tier="block")
        self.inject(provider)
        self.moderate("第一条会花钱的内容", stage="send")
        outcome = self.moderate("第二条超预算的内容", stage="send")
        self.assertIsNone(outcome)
        self.assertEqual(provider.calls, 1)
        self.assertGreaterEqual(self.engine.stats()["budget_skips"], 1)

    def test_redact_before_submit(self) -> None:
        provider = FakeProvider("tencent", tier="pass")
        self.inject(provider)
        self.moderate("这是 key sk-abcdefghijklmnopqrstuvwx 的说明", stage="send")
        self.assertTrue(provider.texts)
        self.assertIn("[已脱敏", provider.texts[0])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", provider.texts[0])


class TestTiersAndHold(EngineCase):
    def test_scope_file_overrides_tier_mapping(self) -> None:
        self.write(
            "safety/scopes/qq__group__g1.toml",
            'platform = "qq"\nscope_id = "g1"\n\n[tiers]\nremote_block = "review"\n',
        )
        self.engine.refresh(force=True)
        self.inject(FakeProvider("tencent", tier="block"))
        scope = self.engine.scope_for("qq", "g1")
        self.assertEqual(self.engine.resolve_tier("block", scope=scope), "review")

    def test_should_hold_for_high_risk_scope(self) -> None:
        self.write(
            "safety/scopes/qq__group__g2.toml",
            'platform = "qq"\nscope_id = "g2"\n\n[tiers]\noutbound_review = "hold"\n',
        )
        self.engine.refresh(force=True)
        scope = self.engine.scope_for("qq", "g2")
        self.assertTrue(self.engine.should_hold("review", scope=scope))
        self.assertFalse(self.engine.should_hold("block", scope=scope))

    def test_images_mode_from_scope(self) -> None:
        self.write(
            "safety/scopes/qq__group__g3.toml",
            'platform = "qq"\nscope_id = "g3"\nimages = "off"\n',
        )
        self.engine.refresh(force=True)
        scope = self.engine.scope_for("qq", "g3")
        self.assertEqual(self.engine.images_mode(scope), "off")
        self.assertIsNone(
            asyncio.run(
                self.engine.moderate_image(
                    b"bytes", stage="image", platform="qq", scope_id="g3"
                )
            )
        )


class TestImageModeration(EngineCase):
    def test_image_block(self) -> None:
        provider = FakeProvider("tencent", tier="block", labels=("porn",))
        self.inject(provider)
        outcome = asyncio.run(self.engine.moderate_image(b"\x89PNG", stage="image"))
        assert outcome is not None
        self.assertEqual(outcome.tier, "block")
        self.assertEqual(provider.images, 1)


class TestImageSkipActors(EngineCase):
    def test_trusted_actor_skips_remote_image(self) -> None:
        self.engine.settings.media.skip_actors = ["trusted-user"]
        self.inject(FakeProvider("tencent", tier="block"))
        self.assertIsNone(
            asyncio.run(
                self.engine.moderate_image(b"\x89PNG", stage="image", actor="trusted-user")
            )
        )
        outcome = asyncio.run(
            self.engine.moderate_image(b"\x89PNG", stage="image", actor="other-user")
        )
        assert outcome is not None
        self.assertEqual(outcome.tier, "block")


class TestReviewQueue(EngineCase):
    def test_enqueue_and_resolve(self) -> None:
        item_id = self.engine.enqueue_review(
            stage="send",
            tier="review",
            action_taken="sent",
            provider="tencent",
            request_id="req-9",
            labels=["porn"],
            score=0.8,
            sha256="abc",
            preview="预览",
        )
        self.assertTrue(item_id)
        item = self.engine.review.get(item_id)
        assert item is not None
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["provider"], "tencent")
        self.assertTrue(self.engine.review.resolve(item_id, status="approved", reviewer="admin"))
        resolved = self.engine.review.get(item_id)
        assert resolved is not None
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(self.engine.review.stats()["approved"], 1)

    def test_expire_stale_items(self) -> None:
        item_id = self.engine.enqueue_review(stage="send", tier="review", action_taken="sent")
        self.assertTrue(item_id)
        from datetime import datetime, timedelta, timezone

        self.engine.review._now = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
        count = self.engine.review.expire(older_than_seconds=60)
        self.assertGreaterEqual(count, 1)


class TestBuild(EngineCase):
    def test_build_engine_requires_file(self) -> None:
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty, exist_ok=True)
        self.assertIsNone(build_engine(config_dir=empty))
        self.assertIsNotNone(build_engine(config_dir=self.dir))

    def test_stats_shape(self) -> None:
        stats = self.engine.stats()
        for key in ("remote_calls", "cache_hits", "cost_today_cny", "review", "providers"):
            self.assertIn(key, stats)
