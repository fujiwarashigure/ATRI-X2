"""结构化上游 API 失败与确定性失败分类。

对应 Go：``internal/llm/errors.go``（``APIError`` / ``ErrorCategory*`` /
``Deterministic`` / ``IsModelNotFoundCode`` / ``IsInvalidParameterCode``），
外加 ``internal/llm/openai/openai.go`` 里紧贴响应解析的 ``classifyAPIError``：
分类表的唯一事实来源是那两个 Go 文件，本模块不允许发明新类别。

两个消费点务必保持一致：

* ``internal/app/vision.go`` + ``agent.chat_llm.shouldFallbackVision`` 只读
  ``APIError.Category == ErrorCategory.VISION_UNSUPPORTED`` 决定视觉兜底，
  所以 :func:`classify` 的视觉判定必须与 Go 逐条对齐（含 param 否决规则）；
* ``Deterministic`` 决定负缓存，取消/超时必须压过状态码。
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = [
    "APIError",
    "ErrorCategory",
    "ErrorCategoryAuth",
    "ErrorCategoryInvalidRequest",
    "ErrorCategoryModelNotFound",
    "ErrorCategoryRateLimit",
    "ErrorCategoryServer",
    "ErrorCategoryTimeout",
    "ErrorCategoryVisionUnsupported",
    "VISION_UNSUPPORTED_IMAGE_PARAMS",
    "VISION_UNSUPPORTED_NEEDLES",
    "as_api_error",
    "classify",
    "contains_any_needle",
    "is_deterministic_code",
    "is_invalid_parameter_code",
    "is_model_not_found_code",
    "is_vision_unsupported",
    "stringify_error_field",
]


class ErrorCategory:
    """适配器控制的失败类别。对应 Go 的 ``ErrorCategory*`` 常量。

    Go 侧权威集合就是这 7 个；``""``（空串）表示「未分类」，例如裸 400：
    OpenAI-compatible 网关也用它表示瞬时上游故障，因此不能当成确定性失败。
    上游**没有** ``context_length`` / ``network`` / ``bad_request`` /
    ``content_filter`` / ``overloaded`` / ``unknown`` 这些类别，不要凭想象补。
    """

    #: 上游拒绝了图片内容块，即所选模型不接受图片。
    VISION_UNSUPPORTED = "vision_unsupported"
    #: 具名模型/端点不存在。
    MODEL_NOT_FOUND = "model_not_found"
    #: 上游把失败归因到某个具体请求字段。
    INVALID_REQUEST = "invalid_request"
    #: 凭据被拒。
    AUTH = "auth"
    #: 账号/端点被限流。
    RATE_LIMIT = "rate_limit"
    #: 上游/网关把请求判为超时。
    TIMEOUT = "timeout"
    #: 上游服务端失败。
    SERVER = "server_error"

    ALL = (
        VISION_UNSUPPORTED,
        MODEL_NOT_FOUND,
        INVALID_REQUEST,
        AUTH,
        RATE_LIMIT,
        TIMEOUT,
        SERVER,
    )


# Go 侧变量名逐一对齐（ErrorCategoryVisionUnsupported 等），便于交叉检索。
ErrorCategoryVisionUnsupported = ErrorCategory.VISION_UNSUPPORTED
ErrorCategoryModelNotFound = ErrorCategory.MODEL_NOT_FOUND
ErrorCategoryInvalidRequest = ErrorCategory.INVALID_REQUEST
ErrorCategoryAuth = ErrorCategory.AUTH
ErrorCategoryRateLimit = ErrorCategory.RATE_LIMIT
ErrorCategoryTimeout = ErrorCategory.TIMEOUT
ErrorCategoryServer = ErrorCategory.SERVER


class APIError(Exception):
    """结构化的上游 API 失败。对应 Go ``APIError``。

    ``message`` 永远是安全摘要（可能出现在用户可见位置），不得包含凭据、
    请求体或原始上游载荷。``category`` 由适配器根据状态码 + 机器可读的
    code/type/param 推导，调用方据此分支，而不是去匹配自由文本。
    """

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int = 0,
        code: str = "",
        type: str = "",
        param: str = "",
        category: str = "",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.type = type
        self.param = param
        self.message = message
        self.category = category
        self.cause = cause

    # -- Go 的 Error() / Unwrap() -------------------------------------------
    def __str__(self) -> str:
        message = (self.message or "").strip() or "request failed"
        if self.status_code > 0:
            return f"HTTP {self.status_code}: {message}"
        return message

    def error(self) -> str:
        """Go 的 ``Error()`` 方法名，便于交叉检索。"""
        return str(self)

    def unwrap(self) -> BaseException | None:
        """Go 的 ``Unwrap()``：暴露底层 cause 给 ``except ... from`` 链。"""
        return self.cause

    def deterministic(self) -> bool:
        """对应 Go ``APIError.Deterministic``。

        只有被明确指认的模型/参数失败才算确定性失败（可以进短期负缓存）。
        取消/超时永远压过状态码：放弃的请求没有产生稳定的服务端结论。
        """
        if _is_cancellation(self):
            return False
        if self.status_code not in (400, 404, 422):
            return False
        if is_deterministic_code(self.code):
            return True
        # 上游点名了出错字段的 invalid_request_error 足够明确，即使没有 code。
        return (self.type or "").strip().lower() == "invalid_request_error" and bool(
            (self.param or "").strip()
        )

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化的结构化快照（字段名对齐 Go struct）。"""
        return {
            "status_code": self.status_code,
            "code": self.code,
            "type": self.type,
            "param": self.param,
            "message": self.message,
            "category": self.category,
        }

    to_json = to_dict

    def _state(self) -> dict[str, Any]:
        state = self.to_dict()
        state["cause"] = self.cause
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.status_code = int(state.get("status_code") or 0)
        self.code = str(state.get("code") or "")
        self.type = str(state.get("type") or "")
        self.param = str(state.get("param") or "")
        self.message = str(state.get("message") or "")
        self.category = str(state.get("category") or "")
        self.cause = state.get("cause")

    def __reduce__(self) -> Any:
        return (_rebuild_api_error, (self.message,), self._state())


def _rebuild_api_error(message: str) -> APIError:
    """pickle 重建入口。"""
    return APIError(message)


def as_api_error(err: BaseException | None) -> APIError | None:
    """对应 Go ``AsAPIError``：从异常链里取出 ``APIError``，取不到返回 None。"""
    seen = 0
    current: Any = err
    while current is not None and seen < 32:
        if isinstance(current, APIError):
            return current
        current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
        seen += 1
    return None


def is_model_not_found_code(code: str) -> bool:
    """对应 Go ``IsModelNotFoundCode``。"""
    return (code or "").strip().lower() in (
        "model_not_found",
        "model_not_available",
        "unknown_model",
        "no_such_model",
        "model_does_not_exist",
    )


def is_invalid_parameter_code(code: str) -> bool:
    """对应 Go ``IsInvalidParameterCode``。"""
    return (code or "").strip().lower() in (
        "invalid_parameter",
        "invalid_value",
        "unknown_parameter",
        "unsupported_value",
        "missing_required_parameter",
        "invalid_type",
    )


def is_deterministic_code(code: str) -> bool:
    """对应 Go ``isDeterministicCode``。"""
    return is_model_not_found_code(code) or is_invalid_parameter_code(code)


# ---------------------------------------------------------------------------
# 分类表（逐条对齐 internal/llm/openai/openai.go 的 classifyAPIError）
# ---------------------------------------------------------------------------

#: 被测试过的、表示「上游拒绝了图片内容块」的措辞。刻意收窄：裸的
#: "unsupported" 不能触发去图重试，网关也用它表示无关的参数问题。
VISION_UNSUPPORTED_NEEDLES = (
    "image",
    "vision",
    "multimodal",
    "content part",
    "content type",
    "unexpected item type in content",
)

#: 上游点名「被拒的是图片相关字段」时用的字段名。无关参数（temperature /
#: tools / max_tokens）会否决视觉判定，因此正文里的泛化提示不能让
#: temperature 报错去剥图片。
VISION_UNSUPPORTED_IMAGE_PARAMS = (
    "image",
    "images",
    "messages",
    "content",
    "vision",
    "multimodal",
    "media",
    "input",
)


def classify(
    status_code: int,
    code: str = "",
    type: str = "",
    param: str = "",
    message: str = "",
) -> str:
    """对应 Go ``classifyAPIError``：由状态码 + 机器可读字段推出类别。

    ``message`` 只在一处被参考：视觉/模态拒绝——若干 OpenAI-compatible 网关
    只用散文描述它。这份方言知识就放在这里，紧邻响应解析，别的层不必再去
    匹配上游文本。其余情况一律不读 ``message``。
    """
    code = (code or "").strip().lower()
    type_name = (type or "").strip().lower()
    param = (param or "").strip().lower()
    message = (message or "").lower()

    if is_vision_unsupported(status_code, code, type_name, param, message):
        return ErrorCategory.VISION_UNSUPPORTED
    if status_code == 404 or is_model_not_found_code(code):
        return ErrorCategory.MODEL_NOT_FOUND
    if status_code in (401, 403):
        return ErrorCategory.AUTH
    if status_code == 429:
        return ErrorCategory.RATE_LIMIT
    if status_code in (408, 504):
        return ErrorCategory.TIMEOUT
    if status_code >= 500:
        return ErrorCategory.SERVER
    if status_code in (400, 422):
        # 只有明确的归因信号才把 400/422 判成请求字段失败；裸 400 保持未分类。
        if (type_name == "invalid_request_error" and param != "") or is_invalid_parameter_code(code):
            return ErrorCategory.INVALID_REQUEST
    return ""


def is_vision_unsupported(
    status_code: int, code: str, type_name: str, param: str, message: str
) -> bool:
    """对应 Go ``isVisionUnsupported``。入参需已 lower/trim（见 :func:`classify`）。"""
    if status_code not in (400, 422, 404):
        return False
    if param and not contains_any_needle(param, VISION_UNSUPPORTED_IMAGE_PARAMS):
        # 上游点名的是别的字段，那图片就不是问题，散文说什么都不算。
        return False
    structured = contains_any_needle(
        " ".join((code or "", type_name or "", param or "")), VISION_UNSUPPORTED_NEEDLES
    )
    if status_code == 404:
        # 404 通常是「模型/端点不存在」，而模型名里就可能带 "image"，
        # 因此这里只认结构化证据。
        return structured
    return structured or contains_any_needle(message or "", VISION_UNSUPPORTED_NEEDLES)


def contains_any_needle(text: str, needles: Any) -> bool:
    """对应 Go ``containsAnyNeedle``。"""
    return any(needle in text for needle in needles)


def stringify_error_field(value: Any) -> str:
    """对应 Go ``stringifyErrorField``：把无类型的上游字段规整成字符串。

    网关会送字符串、数字、布尔或 null；Go 用 ``fmt.Sprint``/``strconv``
    的语义，这里逐一对齐（布尔是 ``"true"``/``"false"``，整数不带 ``.0``）。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            # Go 的 json 解码器不会产生 NaN/Inf，但 Python 的会；防御性返回文本。
            return repr(value)
        if value == int(value):
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _is_cancellation(err: BaseException | None) -> bool:
    """对应 Go 的 ``errors.Is(err, context.Canceled|DeadlineExceeded)``。"""
    seen = 0
    current: Any = err
    while current is not None and seen < 32:
        if isinstance(current, (asyncio.CancelledError, TimeoutError)):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
        seen += 1
    return False
