"""统一错误类型与错误码。

工具层把 :class:`ResearchError` 渲染成 ``{"ok": false, "error": {...}}``；
其它异常一律包成 ``INTERNAL``，避免把 Python 栈直接丢给模型。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ResearchError",
    "ENV_INVALID",
    "RUN_NOT_FOUND",
    "RUN_EXISTS",
    "STAGE_INVALID",
    "ARG_INVALID",
    "SOURCE_UNAVAILABLE",
    "DEPENDENCY_MISSING",
    "ARTIFACT_MISSING",
    "GATE_FAILED",
    "INTERNAL",
]

# 错误码：前四类是调用方问题（可修正后重试），SOURCE_* 是外部问题，
# GATE_* 表示工具执行成功但质量闸门未通过（结果里仍带数据）。
ENV_INVALID = "ENV_INVALID"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
RUN_EXISTS = "RUN_EXISTS"
STAGE_INVALID = "STAGE_INVALID"
ARG_INVALID = "ARG_INVALID"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
ARTIFACT_MISSING = "ARTIFACT_MISSING"
GATE_FAILED = "GATE_FAILED"
INTERNAL = "INTERNAL"


class ResearchError(Exception):
    """带机器可读错误码的异常。

    ``hint`` 是给模型的下一步建议（例如"先跑 research_search"），
    会被原样放进错误信封，因此必须是一句可直接执行的话。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload
