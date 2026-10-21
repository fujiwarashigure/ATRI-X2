"""上游把工具调用写成正文时的补救与清洗。

对应 Go：无（Python 版新增的健壮性补丁，登记在 ``PORTING.md`` §4.1）。

真实事故（2026-10-21，ATRI-X 0.9.10 + ``opencode-go`` / ``deepseek-v4.1-flash``）：
网关没把模型的原生工具调用翻译成 OpenAI ``tool_calls``，而是把 DSML 文本协议原样
塞进 assistant 正文。结果是：工具一次都没执行、用户拿不到搜索结果，QQ 群里还收到了
一串 ``<｜｜DSML｜｜ …>`` 协议标记。

本模块只做"接线"：

* 判据、还原、清洗都在 :mod:`atri_x.llm.tooltext`（纯函数，可单独单测）；
* 这里把还原结果写回 :class:`~atri_x.agent.chat_llm.LLMCallResult`：能还原就补上
  ``tool_calls`` 继续走工具轮；还原不出来也必须把正文换成提示，**绝不把协议标记
  发给用户**；日志与审计记录在这里写。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.chat_llm import LLMCallResult
from atri_x.llm.tooltext import (
    TEXT_TOOL_CALL_NOTICE,
    looks_like_text_tool_call,
    recover_text_tool_calls,
)
from atri_x.storage import new_id

__all__ = ["apply_text_tool_recovery"]


def apply_text_tool_recovery(
    agent: Any,
    session_id: str,
    result: LLMCallResult,
    *,
    tools_enabled: bool,
) -> LLMCallResult:
    """把正文里泄漏的工具调用还原成结构化调用；否则清洗正文。

    * ``result`` 就地修改并返回（:class:`LLMCallResult` 是可变 dataclass）；
    * 已有结构化 ``tool_calls`` 时只清洗正文，避免同一批调用被执行两次；
    * ``tools_enabled=False``（chat 会话）不做还原，只清洗，避免 chat 会话绕过
      "不吃工具 schema"的约定；
    * 还原出的调用会补上 ``id``（原生 tool_calls 由上游给 id，文本形态没有）。
    """
    raw = str(result.raw_text or result.text or "")
    if not looks_like_text_tool_call(raw):
        return result
    calls, cleaned = recover_text_tool_calls(raw)
    names = [call.name for call in calls]
    already = list(result.tool_calls or [])
    if already:
        # 上游同时给了结构化调用与文本标记：以结构化调用为准，正文只留自然语言。
        result.text = cleaned
        result.raw_text = cleaned
        _record(agent, session_id, names, recovered=False, duplicated=True)
        return result
    if calls and tools_enabled:
        for call in calls:
            if not call.id:
                call.id = new_id()
        result.tool_calls = calls
        result.text = cleaned
        result.raw_text = cleaned
        _record(agent, session_id, names, recovered=True, duplicated=False)
        return result
    # 还原不出来（或者 chat 会话本来就不该有工具）：正文换成可读提示。
    fallback = cleaned or TEXT_TOOL_CALL_NOTICE
    result.text = fallback
    result.raw_text = fallback
    _record(agent, session_id, names, recovered=False, duplicated=False)
    return result


def _record(
    agent: Any,
    session_id: str,
    names: list[str],
    *,
    recovered: bool,
    duplicated: bool,
) -> None:
    """写一条 WARN 日志 + 审计记录（只记工具名与计数，绝不记正文）。

    生产装配里 ``AgentOptions.logs`` 只被存成 ``agent.logs``，``set_log_manager``
    并没有被调用 —— 直接读 ``agent.logger`` / ``agent.audit_logger`` 会全程是
    ``None``，日志与审计静默丢失。这里按需从 ``agent.logs`` 补挂一次通道：只在
    真出现这种异常时付代价，不改变正常路径的日志行为。
    """
    tools = ",".join(name for name in names if name)
    logger = _channel(agent, "logger", "runtime")
    audit = _audit(agent)
    if logger is not None:
        try:
            logger.warn(
                "llm returned tool calls as text",
                "session_id",
                session_id,
                "tools",
                tools,
                "recovered",
                recovered,
                "duplicated",
                duplicated,
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响本轮
            pass
    if audit is None:
        return
    try:
        audit(
            "llm_tool_calls_in_text",
            "session_id",
            session_id,
            "tools",
            tools,
            "recovered",
            recovered,
            "duplicated",
            duplicated,
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响本轮
        pass


def _channel(agent: Any, attr: str, channel: str) -> Any:
    """取 ``agent.<attr>``；没接线时用 ``agent.logs.<channel>()`` 补挂。"""
    current = getattr(agent, attr, None)
    if current is not None:
        return current
    logs = getattr(agent, "logs", None)
    getter = getattr(logs, channel, None) if logs is not None else None
    if not callable(getter):
        return None
    try:
        current = getter()
    except Exception:  # noqa: BLE001 - 日志通道不可用就保持无日志
        return None
    try:
        setattr(agent, attr, current)
    except Exception:  # noqa: BLE001 - 只读替身（测试桩）不强制挂载
        pass
    return current


def _audit(agent: Any) -> Any:
    """审计入口：优先 ``agent.audit_warn``（它读 ``audit_logger``）。

    ``audit_logger`` 没接线时先补挂审计通道，再取 ``audit_warn`` 绑定方法，
    这样审计记录的 ``event`` / ``module`` 契约仍由 :mod:`atri_x.agent.logging`
    统一维护，本模块不复制格式。
    """
    _channel(agent, "audit_logger", "audit")
    return getattr(agent, "audit_warn", None) or getattr(agent, "audit", None)

