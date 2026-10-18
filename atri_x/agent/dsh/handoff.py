"""上下文交接：把 ATRI-X 的对话拼成 DSH 的一次性 prompt，并把 ACP 用量折算成账本口径。

对应文档：``devdocs/dsh-acp.md`` §6.2（"``session/resume`` 不回放历史，人设/常驻记忆/
最近轮摘要要由 ATRI-X 拼进 ``session/prompt``"）。

做法是**无状态交接**：每次路由都新开一个 DSH 会话、发一条一次性 prompt、拿到结果后
关掉。这样：

- 不需要假设 DSH 侧保存了（或没保存）某一轮历史，路由与非路由轮次混排也不会串；
- 交接内容完全由这里决定：系统设定 + 最近若干轮 + 当前用户消息，长度受字符预算约束；
- 失败重试、换模型、换内核都不影响正确性（没有跨轮状态要迁移）。

代价是每轮都要重新带上最近历史（多花一点输入 token），因此 ``max_history_messages``
与 ``prompt_char_budget`` 默认取保守值。
"""

from __future__ import annotations

from typing import Sequence

from atri_x.agent.dsh.types import AcpUsage
from atri_x.llm import (
    LLMMessage,
    RoleAssistant,
    RoleSystem,
    RoleTool,
    RoleUser,
    Usage,
    segments_content_text,
)

__all__ = [
    "HANDOFF_HEADER",
    "HANDOFF_INSTRUCTION",
    "build_handoff_prompt",
    "latest_user_text",
    "truncate_text",
    "usage_from_acp",
]

#: 交接 prompt 的标题行。
HANDOFF_HEADER = "[ATRI-X 转交任务]"
#: 交接 prompt 的行为说明（不要让它复述、要它保持人格）。
HANDOFF_INSTRUCTION = (
    "以上是 ATRI-X 会话的上下文。请直接以助手的身份回答「当前用户消息」，"
    "不要复述本说明、不要输出标题或角色标记。保持系统设定里的人设与语言；"
    "如果信息不足，说明缺什么再给出最接近的答案。"
)

#: 单条历史消息最多保留多少字符（避免一条巨长工具输出吃掉全部预算）。
_MESSAGE_CHAR_LIMIT = 4000


def truncate_text(text: str, limit: int) -> str:
    """按字符截断并标记省略；``limit<=0`` 表示不截断。"""
    body = text or ""
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit] + "\n…（已截断）"


def _role_label(role: str) -> str:
    if role == RoleAssistant:
        return "助手"
    if role == RoleTool:
        return "工具"
    return "用户"


def _message_text(message: LLMMessage) -> str:
    """一条消息的可见文本（图片/文件走分段里的占位说明）。"""
    return segments_content_text(list(message.segments or []))


def latest_user_text(messages: Sequence[LLMMessage]) -> str:
    """最近一条用户消息的文本（没有时为空串）——路由打分与 prompt 末段都用它。"""
    for message in reversed(list(messages or [])):
        if message.role == RoleUser:
            return _message_text(message).strip()
    return ""


def build_handoff_prompt(
    messages: Sequence[LLMMessage],
    *,
    max_history_messages: int = 8,
    char_budget: int = 12000,
) -> str:
    """把 LLM 消息序列拼成交接 prompt。

    - 系统消息（``system``）合并成「系统设定」块；
    - 其余消息只保留最近 ``max_history_messages`` 条，且**必定**包含最后一条用户消息；
    - 总长度超过 ``char_budget`` 时从最旧的历史开始丢，并在 prompt 里注明；
    - 当前用户消息始终放在最后（不截断，除非它自己就超过预算）。
    """
    system_parts: list[str] = []
    dialog: list[tuple[str, str]] = []
    for message in list(messages or []):
        text = _message_text(message).strip()
        if message.role == RoleSystem:
            if text:
                system_parts.append(text)
            continue
        if not text:
            continue
        dialog.append((_role_label(message.role), truncate_text(text, _MESSAGE_CHAR_LIMIT)))

    current = ""
    history = dialog
    if dialog and dialog[-1][0] == "用户":
        current = dialog[-1][1]
        history = dialog[:-1]
    if max_history_messages >= 0:
        history = history[-max_history_messages:] if max_history_messages else []

    budget = max(1000, int(char_budget or 0))
    system_text = "\n\n".join(system_parts)
    header = HANDOFF_HEADER
    instruction = HANDOFF_INSTRUCTION
    # 逐步丢最旧历史，直到放进预算。
    while True:
        lines: list[str] = [header, ""]
        if system_text:
            lines.extend(["[系统设定]", system_text, ""])
        if history:
            lines.extend(["[最近对话]"])
            for role, text in history:
                lines.append(f"{role}：{text}")
            lines.append("")
        lines.extend(["[当前用户消息]", current or "（空）", "", instruction])
        prompt = "\n".join(lines)
        if len(prompt) <= budget or not history:
            break
        history = history[1:]
    return prompt


def usage_from_acp(usage: AcpUsage | None) -> Usage:
    """ACP ``usage`` → 账本口径的 :class:`atri_x.llm.Usage`。

    - ``inputTokens`` → ``prompt_tokens``；``outputTokens`` → ``completion_tokens``；
    - ``cachedReadTokens`` → ``cache_hit_tokens``（账本按缓存命中价折算）；
    - ``totalTokens`` 缺失时用输入+输出兜底（账本对 ``total<=0`` 也会兜底）。
    """
    if usage is None:
        return Usage()
    prompt_tokens = max(0, int(usage.input_tokens or 0))
    completion_tokens = max(0, int(usage.output_tokens or 0))
    total = max(0, int(usage.total_tokens or 0))
    if total <= 0:
        total = prompt_tokens + completion_tokens
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cache_hit_tokens=max(0, int(usage.cached_read_tokens or 0)),
    )


def messages_preview(messages: Sequence[LLMMessage], limit: int = 400) -> str:
    """调试用：把交接 prompt 截一小段写进审计（不落全文，避免日志爆量）。"""
    prompt = build_handoff_prompt(messages, max_history_messages=4, char_budget=limit)
    return truncate_text(prompt, limit)
