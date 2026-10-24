"""表情图库与 Agent 接线（atri_x.agent.reactions / 提示词注入）的单元测试。

覆盖：

* ``[reactions]`` 配置 → :class:`ReactionSettings` 的转换；
* 群/私聊/CLI 作用域判定；
* 回合收尾的图片输出（``timing = after_assistant``）与"不发图时冷却计数"；
* 表情提示词只在启用时注入。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from atri_x.agent.reactions import (  # noqa: E402
    build_reaction_service,
    conversation_kind,
    finalize_turn_reaction,
    reaction_settings_from_config,
    reaction_scope,
)
from atri_x.agent.system_prompt_sources import (  # noqa: E402
    ReactionSystemPromptSource,
    reaction_prompt_text,
)
from atri_x.config.app_sections import ReactionsConfig  # noqa: E402
from atri_x.delivery import delivery_timing  # noqa: E402

TMP_DIR = os.path.join(REPO_ROOT, "tmp_agent_reactions_test")


class _Scope:
    def __init__(self, platform: str, scope_id: str) -> None:
        self.platform = platform
        self.platform_scope_id = scope_id
        self.is_cli = platform == "cli"


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def debug(self, message: str, **fields) -> None:
        self.records.append((message, fields))


class _Agent:
    """``finalize_turn_reaction`` 需要的宿主字段。"""

    def __init__(self, platform: str, scope_id: str, service) -> None:
        self._scope = _Scope(platform, scope_id)
        self.reaction_service = service
        self.logger = None

    def scope(self, _ctx) -> _Scope:
        return self._scope


def _write_catalog(gallery: str) -> None:
    os.makedirs(os.path.join(gallery, "assets"), exist_ok=True)
    for name in ("a.png", "b.png"):
        with open(os.path.join(gallery, "assets", name), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
    items = [
        {"id": "a", "emotion": "无语", "path": "assets/a.png"},
        {"id": "b", "emotion": "无语", "path": "assets/b.png"},
    ]
    with open(os.path.join(gallery, "catalog.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "items": items}, handle, ensure_ascii=False)


class ConfigTest(unittest.TestCase):
    def test_settings_from_config(self) -> None:
        config = ReactionsConfig(
            enabled=True,
            gallery_dir="/tmp/gallery",
            cooldown_text_replies=3,
            probability=0.5,
        )
        settings = reaction_settings_from_config(config)
        self.assertTrue(settings.enabled)
        self.assertEqual(3, settings.cooldown_text_replies)
        self.assertEqual(0.5, settings.probability)

    def test_settings_from_missing_config(self) -> None:
        settings = reaction_settings_from_config(None)
        self.assertFalse(settings.enabled)

    def test_service_requires_gallery(self) -> None:
        self.assertIsNone(build_reaction_service(None))
        self.assertIsNone(build_reaction_service(ReactionsConfig(gallery_dir="")))
        service = build_reaction_service(ReactionsConfig())
        self.assertIsNotNone(service)


class ScopeTest(unittest.TestCase):
    def test_conversation_kind(self) -> None:
        self.assertEqual("group", conversation_kind("group:123"))
        self.assertEqual("group", conversation_kind("supergroup:123"))
        self.assertEqual("channel", conversation_kind("channel:9"))
        self.assertEqual("private", conversation_kind("private:456"))
        self.assertEqual("unknown", conversation_kind("local"))
        self.assertEqual("unknown", conversation_kind(""))

    def test_reaction_scope_reads_agent_scope(self) -> None:
        agent = _Agent("qqonebot", "group:123", None)
        self.assertEqual(("qqonebot", "group:123"), reaction_scope(agent, None))


class FinalizeTurnTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        self.gallery = os.path.join(TMP_DIR, self._testMethodName)
        _write_catalog(self.gallery)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def _service(self, **kwargs):
        values = dict(
            enabled=True,
            gallery_dir=self.gallery,
            cooldown_text_replies=0,
            session_cooldown_seconds=60,
            group_cooldown_seconds=0,
            dedupe_recent=0,
            probability=1.0,
            max_text_chars=0,
            suppress_keywords=[],
        )
        values.update(kwargs)
        return build_reaction_service(ReactionsConfig(**values))

    def test_group_turn_emits_after_assistant_image(self) -> None:
        service = self._service()
        agent = _Agent("qqonebot", "group:123", service)
        outputs = finalize_turn_reaction(
            agent, None, None, text="这个需求已经重写整个项目了吧！", emotions=["无语"]
        )
        self.assertEqual(1, len(outputs))
        output = outputs[0]
        self.assertEqual("image", output.kind)
        self.assertTrue(output.source.path.endswith((".png",)))
        self.assertEqual("after_assistant", delivery_timing(output))
        self.assertEqual("无语表情", output.name)
        # 冷却已提交：同一会话立刻再发会被拦。
        again = finalize_turn_reaction(
            agent, None, None, text="再来一次", emotions=["无语"]
        )
        self.assertEqual([], again)

    def test_private_turn_allowed(self) -> None:
        service = self._service()
        agent = _Agent("qqonebot", "private:456", service)
        outputs = finalize_turn_reaction(agent, None, None, text="好耶", emotions=["无语"])
        self.assertEqual(1, len(outputs))

    def test_group_disabled_by_setting(self) -> None:
        service = self._service(show_in_group=False)
        agent = _Agent("qqonebot", "group:123", service)
        self.assertEqual(
            [], finalize_turn_reaction(agent, None, None, text="好耶", emotions=["无语"])
        )

    def test_unknown_scope_skipped(self) -> None:
        agent = _Agent("cli", "local", self._service())
        self.assertEqual(
            [], finalize_turn_reaction(agent, None, None, text="好耶", emotions=["无语"])
        )

    def test_no_emotion_counts_text_reply(self) -> None:
        service = self._service(cooldown_text_replies=1)
        agent = _Agent("qqonebot", "group:123", service)
        self.assertEqual(
            [], finalize_turn_reaction(agent, None, None, text="普通回复", emotions=[])
        )
        # 第一条普通回复把计数加满，第二条才允许发图。
        outputs = finalize_turn_reaction(agent, None, None, text="好耶", emotions=["无语"])
        self.assertEqual(1, len(outputs))

    def test_unknown_emotion_no_image(self) -> None:
        agent = _Agent("qqonebot", "group:123", self._service())
        self.assertEqual(
            [],
            finalize_turn_reaction(agent, None, None, text="好耶", emotions=["不存在"]),
        )

    def test_suppressed_text_no_image(self) -> None:
        service = self._service(suppress_keywords=["报错"], max_text_chars=5)
        agent = _Agent("qqonebot", "group:123", service)
        self.assertEqual(
            [],
            finalize_turn_reaction(agent, None, None, text="程序报错", emotions=["无语"]),
        )
        self.assertEqual(
            [],
            finalize_turn_reaction(
                agent, None, None, text="很长很长很长的正文", emotions=["无语"]
            ),
        )


class PromptSourceTest(unittest.TestCase):
    def _parts(self, config) -> list:
        source = ReactionSystemPromptSource(config)
        return asyncio.run(source.parts(None, None))

    def test_disabled_injects_nothing(self) -> None:
        self.assertEqual([], self._parts(ReactionsConfig(enabled=False)))
        self.assertEqual([], self._parts(None))

    def test_enabled_injects_emotion_list(self) -> None:
        parts = self._parts(ReactionsConfig(enabled=True, emotions=["无语", "得意"]))
        self.assertEqual(1, len(parts))
        self.assertEqual("reactions", parts[0].name)
        self.assertIn("[[emo:无语]]", parts[0].content)
        self.assertIn("无语、得意", parts[0].content)

    def test_prompt_text_is_stable(self) -> None:
        text = reaction_prompt_text(["无语"])
        self.assertIn("每轮最多一个标记", text)
        self.assertIn("不要向用户解释这个标记", text)


if __name__ == "__main__":
    unittest.main()
