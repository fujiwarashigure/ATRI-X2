"""隐私开关与失败可观测：两条防线、失败类别、以及「不建连接」的硬要求。

``allow_external_images=False`` 时服务层必须在**打开上游连接之前**拒绝：这里用
一个会记录调用次数的 fake 适配器证明它一次都没被碰过。
"""

from __future__ import annotations

import asyncio
import unittest
import unittest.mock

from atri_x.errors import MediaTooLargeError, NotFoundError
from atri_x.llm.errors import APIError
from atri_x.llm.llm import StreamChunk
from atri_x.media import UnsupportedImageError
from atri_x.safecontext import DeadlineExceededError
from atri_x.vision import (
    FAILURE_CANCELLED,
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_EMPTY_RESULT,
    FAILURE_PARSE_FAILED,
    FAILURE_PRIVACY_BLOCKED,
    FAILURE_PROVIDER_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_TOO_LARGE,
    FAILURE_UNSUPPORTED_FORMAT,
    Counters,
    NoopMetrics,
    Options,
    PrivacyBlockedError,
    Prompt,
    Request,
    Service,
    blocked_reason,
    configure_privacy,
    current_privacy,
    failure_kind,
    reset_privacy,
    scope_is_group,
)
from atri_x.vision.failures import FAILURE_KINDS
from tests._vision_fakes import FakeLLM, patch_prepare


def _request(media_id: str = "media:" + "b" * 64) -> Request:
    return Request(
        media_id=media_id,
        data=b"fake-image-bytes",
        mime_type="image/png",
        prompt=Prompt(version="chat-image-attributes.v1", system="sys", user="usr"),
    )


def _service(client: object, **overrides: object) -> Service:
    options = dict(
        client=client,
        provider="openai",
        model="qwen-vl-max",
        max_tokens=512,
        temperature=0.2,
        max_edge=1536,
        max_image_bytes=1024 * 1024,
        metrics=NoopMetrics(),
        timeout=5.0,
        shared_timeout=5.0,
    )
    options.update(overrides)
    return Service(Options(**options))


class TestPrivacySwitch(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_privacy()

    def tearDown(self) -> None:
        reset_privacy()

    async def test_options_switch_blocks_before_upstream(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="不该发生")])
        service = _service(client, allow_external_images=False)
        with patch_prepare() as prepare:
            with self.assertRaises(PrivacyBlockedError):
                await service.describe(None, _request())
        self.assertEqual(client.calls, 0, "隐私拒绝时绝不允许建立上游连接")
        self.assertEqual(prepare.call_count, 0, "隐私拒绝时不应该解码图片")
        self.assertEqual(service.stats().failures, {FAILURE_PRIVACY_BLOCKED: 1})

    async def test_process_switch_blocks(self) -> None:
        configure_privacy(allow_external_images=False)
        client = FakeLLM([StreamChunk(delta_content="不该发生")])
        service = _service(client)
        with self.assertRaises(PrivacyBlockedError):
            await service.describe(None, _request())
        self.assertEqual(client.calls, 0)

    async def test_group_switch_blocks_only_groups(self) -> None:
        blocked_client = FakeLLM([StreamChunk(delta_content="不该发生")])
        blocked = _service(
            blocked_client,
            allow_group_images=False,
            group_scope=lambda ctx: True,
        )
        with self.assertRaises(PrivacyBlockedError):
            await blocked.describe(None, _request())
        self.assertEqual(blocked_client.calls, 0)
        self.assertEqual(blocked.stats().failures, {FAILURE_PRIVACY_BLOCKED: 1})

        allowed_client = FakeLLM([StreamChunk(delta_content="ok")])
        allowed = _service(
            allowed_client,
            allow_group_images=False,
            group_scope=lambda ctx: False,
        )
        with patch_prepare():
            result = await allowed.describe(None, _request())
        self.assertEqual(result.text, "ok")
        self.assertEqual(allowed_client.calls, 1)

    async def test_group_switch_allows_groups_when_enabled(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client, allow_group_images=True, group_scope=lambda ctx: True)
        with patch_prepare():
            result = await service.describe(None, _request())
        self.assertEqual(result.text, "ok")

    async def test_default_allows_and_still_works(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client)
        with patch_prepare():
            result = await service.describe(None, _request())
        self.assertEqual(result.text, "ok")
        self.assertEqual(service.stats().failures, {})

    async def test_privacy_reason_texts(self) -> None:
        self.assertEqual(blocked_reason(), "")
        configure_privacy(allow_external_images=False)
        self.assertEqual(blocked_reason(), "外部识图已关闭")
        configure_privacy(allow_group_images=False)
        self.assertEqual(blocked_reason(is_group=True), "群聊外部识图已关闭")
        self.assertEqual(blocked_reason(is_group=False), "")
        self.assertTrue(current_privacy().allow_external_images)

    def test_scope_is_group(self) -> None:
        self.assertTrue(scope_is_group("group:123"))
        self.assertTrue(scope_is_group("supergroup:-100"))
        self.assertFalse(scope_is_group("private:1"))
        self.assertFalse(scope_is_group(""))


class TestFailureKinds(unittest.TestCase):
    def test_known_kinds_are_stable(self) -> None:
        self.assertEqual(
            set(FAILURE_KINDS),
            {
                "download_failed",
                "unsupported_format",
                "too_large",
                "timeout",
                "cancelled",
                "provider_error",
                "privacy_blocked",
                "empty_result",
                "parse_failed",
                "identity_disabled",
                "identity_quota",
                "identity_error",
            },
        )

    def test_failure_kind_mapping(self) -> None:
        self.assertEqual(failure_kind(None), "")
        self.assertEqual(failure_kind(TimeoutError("late")), FAILURE_TIMEOUT)
        self.assertEqual(failure_kind(asyncio.CancelledError()), FAILURE_CANCELLED)
        self.assertEqual(failure_kind(MediaTooLargeError("big")), FAILURE_TOO_LARGE)
        self.assertEqual(
            failure_kind(UnsupportedImageError("bad format")), FAILURE_UNSUPPORTED_FORMAT
        )
        self.assertEqual(failure_kind(NotFoundError("gone")), FAILURE_DOWNLOAD_FAILED)
        self.assertEqual(
            failure_kind(ValueError("vision: model returned an empty result")),
            FAILURE_EMPTY_RESULT,
        )
        self.assertEqual(failure_kind(APIError("nope", status_code=500)), FAILURE_PROVIDER_ERROR)
        self.assertEqual(failure_kind(PrivacyBlockedError("closed")), FAILURE_PRIVACY_BLOCKED)
        self.assertEqual(failure_kind(RuntimeError("boom")), FAILURE_PROVIDER_ERROR)
        self.assertNotEqual(FAILURE_PARSE_FAILED, FAILURE_PROVIDER_ERROR)


class TestServiceFailureCounters(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_privacy()

    def tearDown(self) -> None:
        reset_privacy()

    async def test_timeout_is_recorded(self) -> None:
        # 用真实的截止时间异常而不是 sleep 竞速：分类只看异常链，结果确定。
        client = FakeLLM(error=DeadlineExceededError("context deadline exceeded"))
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(BaseException):
                await service.describe(None, _request())
        self.assertEqual(service.stats().failures.get(FAILURE_TIMEOUT), 1)

    async def test_empty_result_is_recorded(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="   ", finish_reason="stop")])
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(ValueError):
                await service.describe(None, _request())
        self.assertEqual(service.stats().failures.get(FAILURE_EMPTY_RESULT), 1)

    async def test_provider_error_is_recorded(self) -> None:
        client = FakeLLM(error=APIError("boom", status_code=500))
        service = _service(client)
        with patch_prepare():
            with self.assertRaises(APIError):
                await service.describe(None, _request())
        self.assertEqual(service.stats().failures.get(FAILURE_PROVIDER_ERROR), 1)

    async def test_external_record_failure_is_counted(self) -> None:
        # 下载失败发生在服务之外，由调用方（agent 前置路径）分类记录。
        service = _service(FakeLLM([]))
        service.record_failure(FAILURE_DOWNLOAD_FAILED)
        service.record_failure(FAILURE_DOWNLOAD_FAILED)
        service.record_failure(FAILURE_TOO_LARGE)
        stats = service.stats()
        self.assertEqual(stats.failures[FAILURE_DOWNLOAD_FAILED], 2)
        self.assertEqual(stats.failures[FAILURE_TOO_LARGE], 1)
        self.assertEqual(stats.to_json()["failures"][FAILURE_TOO_LARGE], 1)

    async def test_failure_logger_line_has_no_secrets(self) -> None:
        logs: list[str] = []

        class _Logger:
            def warning(self, message: str, *args: object) -> None:
                logs.append(message % args if args else message)

        client = FakeLLM(error=APIError("Authorization: Bearer super-secret", status_code=401))
        service = _service(client, logger=_Logger(), allow_external_images=False)
        with self.assertRaises(PrivacyBlockedError):
            await service.describe(None, _request())
        joined = " ".join(logs)
        self.assertIn(FAILURE_PRIVACY_BLOCKED, joined)
        self.assertNotIn("super-secret", joined)
        self.assertNotIn("media:", joined)

    async def test_metrics_counters_still_work(self) -> None:
        counters = Counters()
        client = FakeLLM([StreamChunk(delta_content="ok", finish_reason="stop")])
        service = _service(client, metrics=counters)
        with patch_prepare():
            await service.describe(None, _request())
        snapshot = counters.snapshot()
        self.assertEqual(snapshot.jobs, 1)
        self.assertEqual(service.stats().failures, {})


class TestProbeTimeoutOption(unittest.TestCase):
    def test_default_and_clamp(self) -> None:
        self.assertEqual(_service(FakeLLM([])).probe_timeout_seconds, 4.0)
        self.assertEqual(_service(FakeLLM([]), probe_timeout_seconds=0).probe_timeout_seconds, 4.0)
        self.assertEqual(
            _service(FakeLLM([]), probe_timeout_seconds=0.1).probe_timeout_seconds, 1.0
        )
        self.assertEqual(
            _service(FakeLLM([]), probe_timeout_seconds=999).probe_timeout_seconds, 30.0
        )

    def test_detail_default_is_original(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client)
        self.assertEqual(service.detail, "original")


class TestServiceImageDetail(unittest.IsolatedAsyncioTestCase):
    async def test_sent_image_segment_carries_detail(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client)
        with patch_prepare():
            await service.describe(None, _request())
        image = client.requests[0].messages[1].segments[1]
        self.assertEqual(image.detail, "original")

    async def test_detail_can_be_turned_off(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client, detail="")
        with patch_prepare():
            await service.describe(None, _request())
        image = client.requests[0].messages[1].segments[1]
        self.assertEqual(image.detail, "")

    async def test_request_detail_overrides_options(self) -> None:
        client = FakeLLM([StreamChunk(delta_content="ok")])
        service = _service(client, detail="low")
        request = _request()
        request.detail = "original"
        with patch_prepare():
            await service.describe(None, request)
        image = client.requests[0].messages[1].segments[1]
        self.assertEqual(image.detail, "original")


class TestToolPathsRespectPrivacy(unittest.IsolatedAsyncioTestCase):
    """工具路径不经过服务层，也必须自己查开关并给出可读错误。"""

    def setUp(self) -> None:
        reset_privacy()

    def tearDown(self) -> None:
        reset_privacy()

    def test_view_image_segment_carries_original_detail(self) -> None:
        from types import SimpleNamespace

        from atri_x.tool.builtin.view_image import view_image_segment

        segment = view_image_segment(
            SimpleNamespace(id="media:" + "a" * 64, name="a.png", mime_type="image/png")
        )
        self.assertEqual(segment.detail, "original")
        self.assertEqual(segment.to_json()["detail"], "original")

    async def test_view_image_refuses_when_external_images_off(self) -> None:
        from atri_x.errors import InvalidError
        from atri_x.tool.builtin import view_image as module
        from atri_x.tool.tool import CallRequest

        tool = module.new_view_image_tool(None, object())
        configure_privacy(allow_external_images=False)
        with unittest.mock.patch.object(module, "vision_available", return_value=True):
            with self.assertRaises(InvalidError) as caught:
                await tool.call(
                    None,
                    CallRequest(
                        name="view_image",
                        arguments='{"source": "media:%s"}' % ("a" * 64),
                    ),
                )
        self.assertIn("外部识图已关闭", str(caught.exception))

    def test_image_to_prompt_returns_readable_privacy_error(self) -> None:
        from atri_x.tool.builtin.image_to_prompt import ImageToPromptTool

        result = ImageToPromptTool()._error_result(
            PrivacyBlockedError("vision: 外部识图已关闭"), "反推提示词失败："
        )
        self.assertIn("外部识图已关闭", result.content)
        self.assertNotIn("vision:", result.content)


class TestOnErrorFallbackStillObserved(unittest.IsolatedAsyncioTestCase):
    """``on_error`` 兜底路径的行为不变，但失败仍要进同一个计数器。"""

    async def test_download_failure_is_recorded_and_batch_degrades(self) -> None:
        from types import SimpleNamespace

        from atri_x.agent.vision import describe_vision_messages
        from atri_x.llm.llm import LLMMessage, MessageSegment, Role, SegmentImage, SegmentText

        class _Media:
            async def read_limited(self, ctx, media_id, limit):
                del ctx, media_id, limit
                raise NotFoundError("expired")

        class _Service:
            def __init__(self) -> None:
                self.failures: list[str] = []

            async def describe(self, ctx, req):  # pragma: no cover - 不会走到
                raise AssertionError("不该调用上游")

            def record_failure(self, kind: str) -> None:
                self.failures.append(kind)

        service = _Service()

        class _Describer:
            def __init__(self) -> None:
                self.service = service

            async def describe_image(self, ctx, media_id, data, mime_type):  # pragma: no cover
                raise AssertionError("不该调用上游")

        agent = SimpleNamespace(media=_Media(), vision=_Describer())
        messages = [
            LLMMessage(
                role=Role.USER,
                segments=[
                    MessageSegment(type=SegmentText, text="看图"),
                    MessageSegment(
                        type=SegmentImage, media="media:" + "a" * 64, mime_type="image/png"
                    ),
                ],
            )
        ]
        described, ok = await describe_vision_messages(agent, None, messages)
        self.assertIsNone(described)
        self.assertFalse(ok)
        self.assertEqual(service.failures, [FAILURE_DOWNLOAD_FAILED])


if __name__ == "__main__":
    unittest.main()
