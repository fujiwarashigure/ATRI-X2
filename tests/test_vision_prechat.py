"""两段式前置描述：``describe_images_before_chat`` 的行为级测试。

关注三件事：

1. ``vision_trigger`` 默认 ``"on_error"`` 时**完全不动**消息（既有兜底不变）；
2. ``"always"`` 时图片段被换成「画面属性」文本，失败一律降级成
   「图没取到（原因：…）」，绝不编造画面；
3. 隐私开关与以图搜图兜底的接入点（群聊关掉 → 未外发；问身份 + 低置信度 →
   追加外部参考，且 key 不出现在文本里）。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

from atri_x.agent.vision import describe_images_before_chat
from atri_x.errors import MediaTooLargeError, NotFoundError
from atri_x.llm.llm import LLMMessage, MessageSegment, Role, SegmentImage, SegmentText
from atri_x.vision import (
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_PARSE_FAILED,
    FAILURE_TIMEOUT,
    FAILURE_TOO_LARGE,
    IdentityConfig,
    Options,
    configure_privacy,
    reset_identity_state,
    reset_privacy,
)

_MEDIA_ID = "media:" + "a" * 64

_ATTRIBUTES_JSON = json.dumps(
    {
        "hair_color": "银色",
        "eye_color": "金色",
        "hairstyle": "长直发",
        "outfit_colors": "黑白配色",
        "trim": "金色滚边",
        "badges": "胸前盾形徽章",
        "accessories": "发带",
        "visible_text": "ATRI",
        "art_style": "赛璐璐",
        "ai_generated": False,
        "scene": "半身像，室内窗边",
        "character_count": 1,
        "identity_confidence": "low",
        "description": "银发金瞳的长发少女，黑白制服配金色滚边。",
    },
    ensure_ascii=False,
)


@dataclass
class _FakeMedia:
    data: bytes = b"fake-image-bytes"
    mime_type: str = "image/png"
    error: BaseException | None = None

    async def read_limited(self, ctx: Any, media_id: str, limit: int) -> tuple[bytes, Any]:
        del ctx, media_id, limit
        if self.error is not None:
            raise self.error
        return self.data, SimpleNamespace(mime_type=self.mime_type)


class _FakeService:
    """共享视觉服务的替身：``describe`` 回放固定文本，并记录失败类别。"""

    def __init__(
        self,
        text: str = _ATTRIBUTES_JSON,
        *,
        error: BaseException | None = None,
        delay: float = 0.0,
        opts: Any = None,
        identity_client: Any = None,
    ) -> None:
        self.text = text
        self.error = error
        self.delay = delay
        self.opts = opts if opts is not None else Options()
        self.identity_client = identity_client
        self.failures: list[str] = []
        self.requests: list[Any] = []

    async def describe(self, ctx: Any, req: Any) -> Any:
        self.requests.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)

    def record_failure(self, kind: str) -> None:
        self.failures.append(kind)


class _FakeDescriber:
    """对应 ``app.vision.VisionChatDescriber``：持有共享服务。"""

    def __init__(self, service: _FakeService, language: str = "zh") -> None:
        self.service = service
        self.language = language

    async def describe_image(self, ctx: Any, media_id: str, data: bytes, mime_type: str) -> str:
        del ctx, media_id, data, mime_type
        return self.service.text


@dataclass
class _Agent:
    media: Any = field(default_factory=_FakeMedia)
    vision: Any = None
    vision_trigger: str = "always"
    vision_probe_timeout_seconds: float = 10.0
    vision_allow_group_images: bool = True
    platform: Any = None
    scope_value: Any = None

    def scope(self, ctx: Any) -> Any:
        del ctx
        return self.scope_value or SimpleNamespace(platform_scope_id="private:1")


def _messages(text: str = "帮我看看这张图") -> list[LLMMessage]:
    return [
        LLMMessage(role=Role.SYSTEM, segments=[MessageSegment(type=SegmentText, text="sys")]),
        LLMMessage(
            role=Role.USER,
            segments=[
                MessageSegment(type=SegmentText, text=text),
                MessageSegment(
                    type=SegmentImage, media=_MEDIA_ID, mime_type="image/png", detail="original"
                ),
            ],
        ),
    ]


def _image_segments(messages: list[LLMMessage]) -> list[MessageSegment]:
    return [
        segment
        for message in messages
        for segment in message.segments
        if segment.type == SegmentImage
    ]


class TestDefaultTriggerKeepsBehaviour(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_privacy()
        reset_identity_state()

    def tearDown(self) -> None:
        reset_privacy()
        reset_identity_state()

    async def test_on_error_leaves_images_untouched(self) -> None:
        service = _FakeService()
        agent = _Agent(vision=_FakeDescriber(service), vision_trigger="on_error")
        messages = _messages()
        out = await describe_images_before_chat(agent, None, messages)
        self.assertEqual(len(_image_segments(out)), 1)
        self.assertEqual(service.requests, [])
        self.assertEqual(service.failures, [])

    async def test_missing_describer_keeps_images(self) -> None:
        agent = _Agent(vision=None)
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(len(_image_segments(out)), 1)

    async def test_no_image_returns_copy(self) -> None:
        service = _FakeService()
        agent = _Agent(vision=_FakeDescriber(service))
        messages = [LLMMessage(role=Role.USER, segments=[MessageSegment(type=SegmentText, text="hi")])]
        out = await describe_images_before_chat(agent, None, messages)
        self.assertEqual(out[0].segments[0].text, "hi")
        self.assertEqual(service.requests, [])


class TestAlwaysTrigger(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_privacy()
        reset_identity_state()

    def tearDown(self) -> None:
        reset_privacy()
        reset_identity_state()

    async def test_replaces_image_with_attributes_text(self) -> None:
        service = _FakeService()
        agent = _Agent(vision=_FakeDescriber(service))
        messages = _messages()
        out = await describe_images_before_chat(agent, None, messages)
        self.assertEqual(_image_segments(out), [])
        text = out[-1].segments[-1].text
        self.assertIn("画面属性", text)
        self.assertIn("发色=银色", text)
        self.assertIn("银发金瞳", text)
        self.assertIn("不可信的图片内容", text)
        # 原消息不受影响（返回的是深拷贝）
        self.assertEqual(len(_image_segments(messages)), 1)
        self.assertEqual(messages[-1].segments[-1].detail, "original")
        # 发的确实是属性模板
        prompt = service.requests[0].prompt
        self.assertEqual(prompt.version, "chat-image-attributes.v1")

    async def test_download_failure_degrades_without_invention(self) -> None:
        service = _FakeService()
        agent = _Agent(
            media=_FakeMedia(error=NotFoundError("expired")), vision=_FakeDescriber(service)
        )
        out = await describe_images_before_chat(agent, None, _messages())
        text = out[-1].segments[-1].text
        self.assertEqual(text, "图没取到（原因：下载失败）")
        self.assertIn(FAILURE_DOWNLOAD_FAILED, service.failures)
        self.assertEqual(service.requests, [])

    async def test_too_large_degrades(self) -> None:
        service = _FakeService()
        agent = _Agent(
            media=_FakeMedia(error=MediaTooLargeError("huge")), vision=_FakeDescriber(service)
        )
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "图没取到（原因：超限）")
        self.assertIn(FAILURE_TOO_LARGE, service.failures)

    async def test_unsupported_mime_degrades(self) -> None:
        service = _FakeService()
        agent = _Agent(media=_FakeMedia(mime_type="application/pdf"), vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "图没取到（原因：格式不支持）")

    async def test_provider_error_degrades(self) -> None:
        service = _FakeService(error=RuntimeError("upstream died"))
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "图没取到（原因：识别失败）")

    async def test_timeout_degrades(self) -> None:
        service = _FakeService(delay=5.0)
        agent = _Agent(vision=_FakeDescriber(service))
        with mock.patch("atri_x.agent.vision._probe_timeout", return_value=0.05):
            out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "图没取到（原因：超时）")
        self.assertIn(FAILURE_TIMEOUT, service.failures)

    async def test_unparseable_answer_falls_back_to_raw_text(self) -> None:
        service = _FakeService(text="这是一张插画，主角站在窗边。")
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages())
        text = out[-1].segments[-1].text
        self.assertIn("这是一张插画", text)
        self.assertIn(FAILURE_PARSE_FAILED, service.failures)

    async def test_empty_answer_degrades(self) -> None:
        service = _FakeService(text="")
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "图没取到（原因：识别失败）")

    async def test_privacy_switch_replaces_with_not_sent(self) -> None:
        configure_privacy(allow_external_images=False)
        service = _FakeService()
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(_image_segments(out), [])
        self.assertEqual(out[-1].segments[-1].text, "[图片未外发：外部识图已关闭]")
        self.assertEqual(service.requests, [])

    async def test_group_switch_replaces_with_not_sent(self) -> None:
        service = _FakeService()
        agent = _Agent(
            vision=_FakeDescriber(service),
            vision_allow_group_images=False,
            platform="telegram",
            scope_value=SimpleNamespace(platform_scope_id="group:42"),
        )
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertEqual(out[-1].segments[-1].text, "[图片未外发：外部识图已关闭]")
        self.assertEqual(service.requests, [])

    async def test_group_switch_allows_private_chat(self) -> None:
        service = _FakeService()
        agent = _Agent(
            vision=_FakeDescriber(service),
            vision_allow_group_images=False,
            platform="telegram",
            scope_value=SimpleNamespace(platform_scope_id="private:42"),
        )
        out = await describe_images_before_chat(agent, None, _messages())
        self.assertIn("画面属性", out[-1].segments[-1].text)


class _FakeIdentityClient:
    """只实现 ``search`` 的以图搜图替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    async def search(self, ctx: Any, data: bytes, mime_type: str) -> list[Any]:
        del ctx, data, mime_type
        self.calls += 1
        from atri_x.vision import IdentityCandidate

        return [IdentityCandidate(**self.payload)]


class TestIdentityWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_privacy()
        reset_identity_state()

    def tearDown(self) -> None:
        reset_privacy()
        reset_identity_state()

    def _service(self, client: Any, **opts: Any) -> _FakeService:
        options = Options(
            identity_enabled=True,
            identity_api_key="tok-secret",
            identity_daily_limit=5,
            **opts,
        )
        return _FakeService(opts=options, identity_client=client)

    async def test_reference_appended_when_user_asks_identity(self) -> None:
        client = _FakeIdentityClient(
            {"title": "某作品", "author": "某作者", "similarity": 0.95, "urls": ["https://e/x"]}
        )
        service = self._service(client)
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages("这是谁？"))
        text = out[-1].segments[-1].text
        self.assertIn("[以图搜图参考]", text)
        self.assertIn("某作品", text)
        self.assertIn("（外部服务，仅供参考）", text)
        self.assertNotIn("tok-secret", text)
        self.assertEqual(client.calls, 1)

    async def test_no_reference_when_not_asked(self) -> None:
        client = _FakeIdentityClient({"title": "某作品", "similarity": 0.95})
        service = self._service(client)
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages("帮我反推提示词"))
        self.assertNotIn("以图搜图参考", out[-1].segments[-1].text)
        self.assertEqual(client.calls, 0)

    async def test_no_reference_when_confidence_high(self) -> None:
        payload = json.loads(_ATTRIBUTES_JSON)
        payload["identity_confidence"] = "high"
        client = _FakeIdentityClient({"title": "某作品", "similarity": 0.95})
        service = self._service(client)
        agent = _Agent(vision=_FakeDescriber(service))
        service.text = json.dumps(payload, ensure_ascii=False)
        out = await describe_images_before_chat(agent, None, _messages("这是谁？"))
        self.assertNotIn("以图搜图参考", out[-1].segments[-1].text)
        self.assertEqual(client.calls, 0)

    async def test_identity_disabled_records_failure(self) -> None:
        service = _FakeService(opts=Options(identity_enabled=False))
        agent = _Agent(vision=_FakeDescriber(service))
        out = await describe_images_before_chat(agent, None, _messages("这是谁？"))
        self.assertNotIn("以图搜图参考", out[-1].segments[-1].text)
        self.assertIn("identity_disabled", service.failures)


class TestProbeTimeoutClamp(unittest.TestCase):
    def test_clamps_into_range(self) -> None:
        from atri_x.agent.vision import (
            DEFAULT_VISION_PROBE_TIMEOUT_SECONDS,
            _probe_timeout,
        )

        self.assertEqual(_probe_timeout(_Agent(vision_probe_timeout_seconds=0), None), 4.0)
        self.assertEqual(
            _probe_timeout(_Agent(vision_probe_timeout_seconds=0.01), None), 1.0
        )
        self.assertEqual(
            _probe_timeout(_Agent(vision_probe_timeout_seconds=999), None), 30.0
        )
        self.assertEqual(
            _probe_timeout(_Agent(vision_probe_timeout_seconds=7.5), None), 7.5
        )
        self.assertEqual(
            _probe_timeout(SimpleNamespace(), None), DEFAULT_VISION_PROBE_TIMEOUT_SECONDS
        )
        self.assertEqual(
            _probe_timeout(SimpleNamespace(), SimpleNamespace(probe_timeout_seconds=9.0)), 9.0
        )


class TestIdentityConfigFromOptions(unittest.TestCase):
    def test_reads_all_fields(self) -> None:
        config = IdentityConfig.from_options(
            Options(
                identity_enabled=True,
                identity_api_key="k",
                identity_api_key_env="MY_KEY",
                identity_daily_limit=7,
                identity_timeout_seconds=3.0,
                identity_min_similarity=0.8,
                identity_state_path="state.json",
            )
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.api_key, "k")
        self.assertEqual(config.api_key_env, "MY_KEY")
        self.assertEqual(config.daily_limit, 7)
        self.assertEqual(config.timeout_seconds, 3.0)
        self.assertEqual(config.min_similarity, 0.8)
        self.assertEqual(config.state_path, "state.json")

    def test_defaults_are_off(self) -> None:
        config = IdentityConfig.from_options(Options())
        self.assertFalse(config.enabled)
        self.assertEqual(config.resolve_api_key(environ={}), "")


if __name__ == "__main__":
    unittest.main()
