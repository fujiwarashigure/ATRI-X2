"""guard 的异步三档接口：远程并入、review/hold、图片审核、审计 v2 留痕。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest

from atri_x.safety import SafetyGuard
from atri_x.safety.engine import SafetyEngine
from atri_x.safety.providers.base import ProviderError, verdict

TMP_ROOT = os.path.join(os.path.dirname(__file__), "_tmp_safety_async")

TOML = """
[local]
enabled = true
mode = "enforce"

[remote]
enabled = true
providers = ["tencent", "aliyun"]
quorum = "any_block"
fail_mode = "open"

[cost]
daily_budget_cny = 0.0
"""

BLOCK_TEXT = "你的消息包含违反内容安全策略的内容，已被拦截。"


class FakeProvider:
    def __init__(self, tier: str = "pass", labels: tuple[str, ...] = ()) -> None:
        self.name = "tencent"
        self.tier = tier
        self.labels = labels
        self.calls = 0

    async def moderate_text(self, text: str, *, biz_type: str = ""):
        self.calls += 1
        return verdict(
            provider=self.name,
            suggestion=self.tier,
            labels=self.labels,
            score=0.9,
            request_id="req-1",
        )

    async def moderate_image(self, data: bytes, *, mime: str = "", url: str = ""):
        self.calls += 1
        return verdict(
            provider=self.name,
            suggestion=self.tier,
            labels=self.labels,
            score=0.8,
            request_id="req-img",
        )


class AsyncCase(unittest.TestCase):
    toml_text = TOML

    def setUp(self) -> None:
        self.dir = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "safety.toml")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.toml_text)
        self.engine = SafetyEngine(config_dir=self.dir)
        self.provider = FakeProvider()
        self.inject(self.provider)
        self.guard = SafetyGuard(self.engine.policy, config_dir=self.dir, engine=self.engine)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def inject(self, provider: FakeProvider) -> None:
        from atri_x.safety.engine import _ProviderHealth

        self.engine._providers = {provider.name: provider}
        self.engine._health = {provider.name: _ProviderHealth()}

    def records(self) -> list[dict]:
        path = os.path.join(self.dir, "safety-audit.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    out.append(json.loads(text))
        return out

    def run_async(self, coro):  # noqa: ANN001, ANN201
        return asyncio.run(coro)


class TestRemoteText(AsyncCase):
    def test_remote_block_enforced(self) -> None:
        self.provider.tier = "block"
        self.provider.labels = ("porn",)
        text, decision = self.run_async(
            self.guard.afilter_text("任何内容", stage="send", block_text=BLOCK_TEXT)
        )
        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.tier, "block")
        self.assertEqual(text, BLOCK_TEXT)
        self.assertEqual(decision.provider, "tencent")
        self.assertEqual(decision.request_id, "req-1")
        record = self.records()[-1]
        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["action"], "block")
        self.assertEqual(record["provider"], "tencent")
        self.assertEqual(record["request_id"], "req-1")
        self.assertIn("porn", record["labels"])
        self.assertNotIn("任何内容", json.dumps(record, ensure_ascii=False))

    def test_remote_review_keeps_text(self) -> None:
        self.provider.tier = "review"
        text, decision = self.run_async(self.guard.afilter_text("待复核内容", stage="send"))
        self.assertEqual(decision.action, "review")
        self.assertEqual(decision.tier, "review")
        self.assertEqual(text, "待复核内容")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.hold)

    def test_remote_review_enqueues_review_item(self) -> None:
        self.provider.tier = "review"
        self.provider.labels = ("porn",)
        _text, decision = self.run_async(
            self.guard.afilter_text("电话 13812345678 的入队内容", stage="send")
        )
        self.assertEqual(decision.tier, "review")
        stats = self.engine.review.stats()
        self.assertGreaterEqual(stats.get("pending", 0), 1)
        items = self.engine.review.list(status="pending", limit=5)
        self.assertTrue(items)
        self.assertEqual(items[0]["provider"], "tencent")
        self.assertEqual(items[0]["stage"], "send")
        self.assertNotIn("13812345678", str(items[0].get("preview", "")))

    def test_audit_mode_never_changes_text(self) -> None:
        with open(os.path.join(self.dir, "safety.toml"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(TOML.replace('mode = "enforce"', 'mode = "audit"'))
        self.engine.refresh(force=True)
        self.inject(self.provider)
        self.provider.tier = "block"
        self.guard = SafetyGuard(self.engine.policy, config_dir=self.dir, engine=self.engine)
        text, decision = self.run_async(
            self.guard.afilter_text("审计模式内容", stage="send", block_text=BLOCK_TEXT)
        )
        self.assertEqual(text, "审计模式内容")
        self.assertEqual(decision.action, "block")
        self.assertEqual(record_field(self.records()[-1], "mode"), "audit")

    def test_local_hit_and_remote_pass(self) -> None:
        _text, decision = self.run_async(
            self.guard.afilter_text(
                "phone 13812345678", stage="send", block_text=BLOCK_TEXT
            )
        )
        # PII 默认 flag：不改文本、动作 flag
        self.assertEqual(decision.action, "flag")
        self.assertIn("phone", decision.labels)

    def test_sync_filter_text_stays_local_only(self) -> None:
        self.provider.tier = "block"
        text, decision = self.guard.filter_text("同步路径内容", stage="send")
        self.assertEqual(text, "同步路径内容")
        self.assertEqual(decision.action, "pass")
        self.assertEqual(self.provider.calls, 0)

    def test_remote_failure_is_fail_open(self) -> None:
        class FailingProvider(FakeProvider):
            async def moderate_text(self, text: str, *, biz_type: str = ""):
                raise ProviderError("boom", provider="tencent")

        failing = FailingProvider()
        self.inject(failing)
        self.engine.settings.remote.retries = 0
        text, decision = self.run_async(self.guard.afilter_text("失败放行", stage="send"))
        self.assertEqual(text, "失败放行")
        self.assertEqual(decision.action, "pass")
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fail_mode, "open")


class TestRemoteImage(AsyncCase):
    def test_image_block(self) -> None:
        self.provider.tier = "block"
        self.provider.labels = ("porn",)
        decision = self.run_async(
            self.guard.aallow_media_output("image", stage="send", data=b"\x89PNG")
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.tier, "block")
        record = self.records()[-1]
        self.assertEqual(record["provider"], "tencent")
        self.assertEqual(record["request_id"], "req-img")

    def test_image_pass(self) -> None:
        self.provider.tier = "pass"
        decision = self.run_async(
            self.guard.aallow_media_output("image", stage="send", data=b"\x89PNG")
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "pass")

    def test_trusted_actor_skips_remote_image(self) -> None:
        self.engine.settings.media.skip_actors = ["trusted-user"]
        self.provider.tier = "block"
        decision = self.run_async(
            self.guard.aallow_media_output(
                "image",
                stage="send",
                data=b"\x89PNG",
                scope={"platform": "qq", "scope_id": "group:g1", "actor": "trusted-user"},
            )
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(self.provider.calls, 0)

    def test_file_kind_not_sent_to_remote(self) -> None:
        self.provider.tier = "block"
        decision = self.run_async(
            self.guard.aallow_media_output("file", stage="send", data=b"data")
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(self.provider.calls, 0)


class TestHold(AsyncCase):
    def setUp(self) -> None:
        super().setUp()
        scopes_dir = os.path.join(self.dir, "safety", "scopes")
        os.makedirs(scopes_dir, exist_ok=True)
        with open(
            os.path.join(scopes_dir, "qq__group__g9.toml"),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(
                'platform = "qq"\nscope_id = "group:g9"\n\n[tiers]\noutbound_review = "hold"\n'
            )
        self.engine.refresh(force=True)
        self.inject(self.provider)

    def test_hold_flag_set_from_scope_file(self) -> None:
        self.provider.tier = "review"
        _text, decision = self.run_async(
            self.guard.afilter_text(
                "待人工", stage="send", scope={"platform": "qq", "scope_id": "group:g9"}
            )
        )
        self.assertEqual(decision.action, "review")
        self.assertTrue(decision.hold)


def record_field(record: dict, name: str):  # noqa: ANN001, ANN201
    return record.get(name, "")
