"""``atri_x.llm.errors`` 测试。

对应 Go：``internal/llm/errors_test.go``（``APIError`` 的 ``Error()`` /
``Deterministic`` / code helper）与 ``internal/llm/openai/errors_test.go`` 里的
``classifyAPIError`` 判定表（分类表本体也在这个模块里）。
"""

from __future__ import annotations

import asyncio
import pickle
import unittest

from atri_x.llm.errors import (
    APIError,
    ErrorCategory,
    ErrorCategoryAuth,
    ErrorCategoryInvalidRequest,
    ErrorCategoryModelNotFound,
    ErrorCategoryRateLimit,
    ErrorCategoryServer,
    ErrorCategoryTimeout,
    ErrorCategoryVisionUnsupported,
    VISION_UNSUPPORTED_IMAGE_PARAMS,
    VISION_UNSUPPORTED_NEEDLES,
    as_api_error,
    classify,
    is_invalid_parameter_code,
    is_model_not_found_code,
    is_vision_unsupported,
    stringify_error_field,
)


class TestErrorCategory(unittest.TestCase):
    def test_matches_go_set_exactly(self) -> None:
        # Go 的 internal/llm/errors.go 只有这 7 个类别；多一个少一个都会让
        # 「按 Category 分支」的调用方（视觉兜底、负缓存、重试）与 Go 不一致。
        self.assertEqual(
            ErrorCategory.ALL,
            (
                "vision_unsupported",
                "model_not_found",
                "invalid_request",
                "auth",
                "rate_limit",
                "timeout",
                "server_error",
            ),
        )
        self.assertEqual(ErrorCategoryVisionUnsupported, ErrorCategory.VISION_UNSUPPORTED)
        self.assertEqual(ErrorCategoryModelNotFound, ErrorCategory.MODEL_NOT_FOUND)
        self.assertEqual(ErrorCategoryInvalidRequest, ErrorCategory.INVALID_REQUEST)
        self.assertEqual(ErrorCategoryAuth, ErrorCategory.AUTH)
        self.assertEqual(ErrorCategoryRateLimit, ErrorCategory.RATE_LIMIT)
        self.assertEqual(ErrorCategoryTimeout, ErrorCategory.TIMEOUT)
        self.assertEqual(ErrorCategoryServer, ErrorCategory.SERVER)

    def test_no_invented_categories(self) -> None:
        # 上游没有 context_length / network / bad_request / content_filter /
        # overloaded / unknown：不允许凭想象补。
        for invented in (
            "context_length",
            "network",
            "bad_request",
            "content_filter",
            "overloaded",
            "unknown",
        ):
            self.assertNotIn(invented, ErrorCategory.ALL)


class TestAPIErrorErrorFormat(unittest.TestCase):
    def test_error_format_matches_go(self) -> None:
        cases = [
            (APIError("bad request", status_code=400), "HTTP 400: bad request"),
            (APIError(status_code=500), "HTTP 500: request failed"),
            (APIError("boom"), "boom"),
            (APIError("  spaced  ", status_code=404), "HTTP 404: spaced"),
        ]
        for error, want in cases:
            with self.subTest(want=want):
                self.assertEqual(str(error), want)
                self.assertEqual(error.error(), want)

    def test_is_raisable_and_carries_fields(self) -> None:
        with self.assertRaises(APIError) as caught:
            raise APIError(
                "bad field",
                status_code=400,
                code="invalid_parameter",
                type="invalid_request_error",
                param="temperature",
                category=ErrorCategory.INVALID_REQUEST,
            )
        error = caught.exception
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.code, "invalid_parameter")
        self.assertEqual(error.type, "invalid_request_error")
        self.assertEqual(error.param, "temperature")
        self.assertEqual(error.category, ErrorCategory.INVALID_REQUEST)


class TestAsAPIError(unittest.TestCase):
    def test_unwraps_wrapped_error(self) -> None:
        api_error = APIError("missing", status_code=400, code="model_not_found")
        wrapper = RuntimeError("chat: missing")
        wrapper.__cause__ = api_error
        self.assertIs(as_api_error(wrapper), api_error)
        self.assertIs(as_api_error(api_error), api_error)
        self.assertIsNone(as_api_error(ValueError("plain")))
        self.assertIsNone(as_api_error(None))

    def test_walks_explicit_cause_attribute(self) -> None:
        api_error = APIError("boom", status_code=500)
        outer = RuntimeError("outer")
        outer.cause = api_error  # type: ignore[attr-defined]
        self.assertIs(as_api_error(outer), api_error)

    def test_keeps_cause_for_identity_checks(self) -> None:
        cause = RuntimeError("boom")
        error = APIError("read failed", status_code=400, cause=cause)
        self.assertIs(error.unwrap(), cause)
        self.assertIs(error.cause, cause)


class TestAPIErrorDeterministic(unittest.TestCase):
    def test_deterministic_matrix(self) -> None:
        cases = [
            ("explicit model_not_found", APIError(status_code=404, code="model_not_found"), True),
            ("explicit invalid parameter code", APIError(status_code=400, code="invalid_parameter"), True),
            (
                "param attributed invalid request",
                APIError(status_code=400, type="invalid_request_error", param="temperature"),
                True,
            ),
            ("bare 400 without code", APIError("bad request", status_code=400), False),
            ("400 with unknown code", APIError(status_code=400, code="something_new"), False),
            ("429 is transient", APIError(status_code=429, code="model_not_found"), False),
            ("500 is transient", APIError(status_code=500, code="invalid_parameter"), False),
            ("auth failure is not cached", APIError(status_code=401, code="invalid_parameter"), False),
            (
                "canceled request wins over status",
                APIError(status_code=404, code="model_not_found", cause=asyncio.CancelledError()),
                False,
            ),
            (
                "timed-out request wins over status",
                APIError(status_code=400, code="invalid_parameter", cause=TimeoutError()),
                False,
            ),
            (
                "wrapped cancellation cause",
                APIError(status_code=422, code="invalid_value", cause=_wrapped(asyncio.CancelledError())),
                False,
            ),
            (
                "unrelated cause stays deterministic",
                APIError(status_code=404, code="model_not_found", cause=RuntimeError("boom")),
                True,
            ),
        ]
        for name, error, want in cases:
            with self.subTest(name=name):
                self.assertEqual(error.deterministic(), want)

    def test_invalid_request_type_needs_param(self) -> None:
        self.assertFalse(APIError(status_code=400, type="invalid_request_error").deterministic())
        self.assertFalse(APIError(status_code=400, type="INVALID_REQUEST_ERROR ").deterministic())
        self.assertTrue(
            APIError(status_code=422, type=" Invalid_Request_Error ", param="input").deterministic()
        )


class TestExportedErrorCodeHelpers(unittest.TestCase):
    def test_model_not_found_codes(self) -> None:
        self.assertTrue(is_model_not_found_code(" model_does_not_exist "))
        self.assertTrue(is_model_not_found_code("MODEL_NOT_FOUND"))
        self.assertTrue(is_model_not_found_code("model_not_available"))
        self.assertTrue(is_model_not_found_code("unknown_model"))
        self.assertTrue(is_model_not_found_code("no_such_model"))
        self.assertFalse(is_model_not_found_code("invalid_parameter"))
        self.assertFalse(is_model_not_found_code(""))

    def test_invalid_parameter_codes(self) -> None:
        self.assertTrue(is_invalid_parameter_code("invalid_type"))
        self.assertTrue(is_invalid_parameter_code("MISSING_REQUIRED_PARAMETER"))
        self.assertTrue(is_invalid_parameter_code("unknown_parameter"))
        self.assertTrue(is_invalid_parameter_code("unsupported_value"))
        self.assertFalse(is_invalid_parameter_code("model_not_found"))
        self.assertFalse(is_invalid_parameter_code(""))


class TestClassify(unittest.TestCase):
    def test_status_table(self) -> None:
        cases = [
            (404, "", "", "", "", ErrorCategory.MODEL_NOT_FOUND),
            (400, "model_not_found", "", "", "nope", ErrorCategory.MODEL_NOT_FOUND),
            (401, "", "", "", "bad key", ErrorCategory.AUTH),
            (403, "", "", "", "forbidden", ErrorCategory.AUTH),
            (429, "", "", "", "slow down", ErrorCategory.RATE_LIMIT),
            (408, "", "", "", "", ErrorCategory.TIMEOUT),
            (504, "", "", "", "", ErrorCategory.TIMEOUT),
            (500, "", "", "", "boom", ErrorCategory.SERVER),
            (503, "", "", "", "", ErrorCategory.SERVER),
            (400, "", "invalid_request_error", "max_tokens", "bad field", ErrorCategory.INVALID_REQUEST),
            (422, "invalid_value", "", "", "", ErrorCategory.INVALID_REQUEST),
            (400, "", "", "", "something went wrong", ""),
            (400, "", "invalid_request_error", "", "no param", ""),
            (429, "", "", "", "rate limited while sending image", ErrorCategory.RATE_LIMIT),
        ]
        for status, code, type_name, param, message, want in cases:
            with self.subTest(status=status, code=code, param=param):
                self.assertEqual(classify(status, code, type_name, param, message), want)

    def test_vision_unsupported_from_structured_fields(self) -> None:
        # 结构化证据必须真的命中 needle 表。
        self.assertEqual(classify(400, "", "", "image_url", ""), ErrorCategory.VISION_UNSUPPORTED)
        self.assertEqual(
            classify(422, "unsupported_image_content", "", "", ""),
            ErrorCategory.VISION_UNSUPPORTED,
        )
        # invalid_parameter 本身不含视觉措辞、正文也没提，按 Go 只是请求字段失败。
        self.assertEqual(
            classify(400, "invalid_parameter", "invalid_parameter_error", "", "whatever"),
            ErrorCategory.INVALID_REQUEST,
        )

    def test_vision_unsupported_from_prose(self) -> None:
        self.assertEqual(
            classify(
                400,
                "",
                "",
                "",
                "the error info is [Unexpected item type in content.]",
            ),
            ErrorCategory.VISION_UNSUPPORTED,
        )
        self.assertEqual(
            classify(422, "", "", "", "multimodal input not supported"),
            ErrorCategory.VISION_UNSUPPORTED,
        )

    def test_vision_vetoed_by_unrelated_param(self) -> None:
        self.assertEqual(
            classify(
                400,
                "invalid_parameter",
                "invalid_request_error",
                "temperature",
                "provider image-lab: temperature out of range",
            ),
            ErrorCategory.INVALID_REQUEST,
        )

    def test_404_needs_structured_evidence(self) -> None:
        # 404 通常是模型不存在，而模型名里可能带 "image"。
        self.assertEqual(
            classify(404, "", "invalid_request_error", "", "model image-chat-v2 not found"),
            ErrorCategory.MODEL_NOT_FOUND,
        )
        self.assertEqual(
            classify(404, "", "", "image", "content part rejected"),
            ErrorCategory.VISION_UNSUPPORTED,
        )

    def test_vision_requires_capability_statuses(self) -> None:
        for status in (200, 401, 408, 429, 500, 503):
            with self.subTest(status=status):
                self.assertFalse(
                    is_vision_unsupported(status, "image", "vision", "content", "image")
                )
        for status in (400, 404, 422):
            with self.subTest(status=status):
                self.assertTrue(is_vision_unsupported(status, "image", "", "", ""))

    def test_needle_tables_are_narrow(self) -> None:
        # 裸 "unsupported" 不能触发去图重试：网关也用它表示无关参数问题。
        self.assertNotIn("unsupported", VISION_UNSUPPORTED_NEEDLES)
        self.assertIn("unexpected item type in content", VISION_UNSUPPORTED_NEEDLES)
        self.assertFalse(is_vision_unsupported(400, "", "", "", "unsupported parameter"))
        self.assertEqual(
            is_vision_unsupported(400, "", "", "temperature", "image"), False
        )
        self.assertIn("messages", VISION_UNSUPPORTED_IMAGE_PARAMS)


class TestStringifyErrorField(unittest.TestCase):
    def test_matches_go_semantics(self) -> None:
        self.assertEqual(stringify_error_field(None), "")
        self.assertEqual(stringify_error_field("  x  "), "x")
        self.assertEqual(stringify_error_field(123), "123")
        self.assertEqual(stringify_error_field(123.0), "123")
        self.assertEqual(stringify_error_field(1.5), "1.5")
        self.assertEqual(stringify_error_field(True), "true")
        self.assertEqual(stringify_error_field(False), "false")
        self.assertEqual(stringify_error_field({"a": 1}), "{'a': 1}")


class TestSerialization(unittest.TestCase):
    def test_to_dict_and_pickle_round_trip(self) -> None:
        error = APIError(
            "bad field",
            status_code=422,
            code="invalid_value",
            type="invalid_request_error",
            param="messages",
            category=ErrorCategory.INVALID_REQUEST,
        )
        self.assertEqual(
            error.to_dict(),
            {
                "status_code": 422,
                "code": "invalid_value",
                "type": "invalid_request_error",
                "param": "messages",
                "message": "bad field",
                "category": "invalid_request",
            },
        )
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual(restored.to_dict(), error.to_dict())
        self.assertEqual(str(restored), str(error))
        self.assertTrue(restored.deterministic())


def _wrapped(inner: BaseException) -> BaseException:
    outer = RuntimeError("read")
    outer.__cause__ = inner
    return outer


if __name__ == "__main__":
    unittest.main()
