"""后台任务 JSON 结果的解析与格式重试提示。对应 Go：``internal/background/result.go``。

LLM 被要求"最终回复必须是严格 JSON"，但实际输出经常带 Markdown 代码块或前后缀文字。
:func:`ParseJSONResult` 因此先从整段文本里**取出第一个 ``{`` 到最后一个 ``}``** 的子串
再解析（与 Go 的 ``strings.Index`` / ``strings.LastIndex`` 口径一致），并在解析失败时
抛 ``ValueError``（Go 返回 ``error``）；调用方据此决定是否发起同 Session 的格式重试。
"""

from __future__ import annotations

import json

from atri_x.background.background import JSONResult
from atri_x.background.report import PathInstruction

__all__ = ["DefaultJSONRetryPrompt", "ParseJSONResult", "default_json_retry_prompt", "parse_json_result"]


def ParseJSONResult(text: str) -> JSONResult:  # noqa: N802 - 对齐 Go 导出名
    """从模型回复里解析 :class:`JSONResult`。对应 Go ``ParseJSONResult``。

    与 Go 逐字对齐的处理顺序：去首尾空白 → 去掉 ``` ```json`` 前缀 → 去掉 ``` ``` `` 前缀
    → 去掉尾部 ``` ``` `` → 再去首尾空白 → 取第一个 ``{`` 到最后一个 ``}``。
    找不到 JSON 对象或解析失败都抛 ``ValueError``。
    """
    value = (text or "").strip()
    value = _trim_prefix(value, "```json")
    value = _trim_prefix(value, "```")
    value = _trim_suffix(value, "```")
    value = value.strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("json object not found")
    payload = value[start : end + 1]
    try:
        raw = json.loads(payload)
    except ValueError as exc:
        raise ValueError(f"{exc}") from exc
    return JSONResult.from_json(raw)


parse_json_result = ParseJSONResult


def DefaultJSONRetryPrompt() -> str:  # noqa: N802 - 对齐 Go 导出名
    """同 Session 的 JSON 格式重试提示词。对应 Go ``DefaultJSONRetryPrompt``。"""
    return (
        "你返回的格式有误，请严格使用 JSON 格式。不能包含 Markdown 代码块，不能包含 JSON 外的任何文字。"
        '格式：{"completed":true,"need_report":false,"report":"","report_segments":[]}。'
        "need_report 表示是否需要向目标平台汇报；成功、失败或阻塞都可以请求汇报。"
        "report_segments 可选，url 使用工作目录内的相对路径或直接使用 HTTP(S) URL；远程 URL 不会下载。"
        + PathInstruction()
    )


default_json_retry_prompt = DefaultJSONRetryPrompt


def _trim_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _trim_suffix(value: str, suffix: str) -> str:
    return value[: -len(suffix)] if value.endswith(suffix) else value
