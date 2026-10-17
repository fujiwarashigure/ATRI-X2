"""客户端摘要压缩后端。

对应 Go：``internal/contextmgr/compressor.go``（类型与 ``Compact``）与
``internal/contextmgr/summarize.go``（``SummarizeText``）。

两个上游设计意图必须保留：

1. **``Compactor`` 是接口而不是流程的一部分。** 目前唯一实现是客户端摘要
   （``name() == "client_summary"``）；将来某个协议真能用服务端原生压缩时，
   新增一个实现并在**分派点**按 :class:`atri_x.llm.protocol.ProtocolCapabilities`
   选择，而不是把协议判断写进压缩流程。
2. **回退必须显式记录。** :class:`CompactionChoice` 记录这次压缩实际走了哪个
   后端、以及是否发生"协议更强但实现未跟上"的回退 —— 静默回退会让
   "服务端压缩没生效"变成没人发现的行为差异。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from atri_x.contextmgr.budget import (
    DEFAULT_USER_ORIGINAL_MAX_RUNES,
    estimate_text_tokens,
    truncate_text_to_tokens,
    truncate_text_with_marker,
)
from atri_x.contextmgr.compact_prompt import (
    COMPACT_SYSTEM_PROMPT,
    TEXT_SUMMARIZER_SYSTEM_PROMPT,
    assemble_summary,
    compact_prompt,
)
from atri_x.contextmgr.window import ClientProvider
from atri_x.llm.llm import ChatRequest, LLMMessage, Role, Usage
from atri_x.llm.segment import text_segments

#: 摘要分片上限。对应 Go ``maxSummarizeChunks``。
MAX_SUMMARIZE_CHUNKS = 64

#: 递归摘要的最大深度。对应 Go ``maxSummarizeDepth``。
MAX_SUMMARIZE_DEPTH = 4

#: 用户原话被截断时追加的标记。对应 Go 的字面量。
USER_INPUT_TRUNCATION_MARKER = "\n...[用户原话过长，压缩时已截断]"


@dataclass
class CompactToolCall:
    """对应 Go ``CompactToolCall``。"""

    name: str = ""
    arguments: str = ""


@dataclass
class CompactMessage:
    """对应 Go ``CompactMessage``。"""

    role: str = ""
    content: str = ""
    tool_calls: list[CompactToolCall] = field(default_factory=list)


@dataclass
class CompactRequest:
    """对应 Go ``CompactRequest``。"""

    provider: str = ""
    model: str = ""
    messages: list[CompactMessage] = field(default_factory=list)
    user_inputs: list[str] = field(default_factory=list)
    user_input_max_runes: int = 0


@dataclass
class CompactResult:
    """对应 Go ``CompactResult``。"""

    summary: str = ""
    assembled_summary: str = ""
    usage: Usage | None = None


@dataclass
class SummarizeRequest:
    """对应 Go ``SummarizeRequest``。"""

    provider: str = ""
    model: str = ""
    text: str = ""
    max_input_tokens: int = 0
    chunk_input_tokens: int = 0
    max_output_tokens: int = 0


@dataclass
class SummarizeResult:
    """对应 Go ``SummarizeResult``。"""

    summary: str = ""
    usage: Usage | None = None


@dataclass
class CompactionChoice:
    """记录一次压缩实际走了哪个后端。对应 Go ``CompactionChoice``。"""

    backend: str = ""
    fallback_reason: str = ""


@runtime_checkable
class Compactor(Protocol):
    """一次上下文压缩的执行后端。对应 Go ``Compactor``。"""

    def name(self) -> str: ...

    async def compact(self, ctx: Any, req: CompactRequest) -> CompactResult: ...

    async def summarize_text(
        self, ctx: Any, req: SummarizeRequest
    ) -> SummarizeResult: ...


class Compressor:
    """客户端摘要压缩后端。对应 Go ``Compressor``。"""

    def __init__(self, client_for: ClientProvider | None = None) -> None:
        self.client_for = client_for

    def name(self) -> str:
        """对应 Go ``Compressor.Name``。审计日志会记这个值。"""
        return "client_summary"

    # -- 压缩 ---------------------------------------------------------------
    async def compact(self, ctx: Any, req: CompactRequest) -> CompactResult:
        """把历史交给压缩模型生成摘要。对应 Go ``Compressor.Compact``。"""
        if self.client_for is None:
            raise ValueError("compressor is not configured")
        if not req.messages:
            raise ValueError("没有可压缩的历史消息")
        if not req.provider or not req.model:
            raise ValueError("压缩模型未配置")

        messages, user_inputs = cap_compact_request(req)
        prompt = compact_prompt(messages, user_inputs)
        summary = await self._collect_stream(
            ctx,
            req.provider,
            ChatRequest(
                model=req.model,
                messages=[
                    LLMMessage(
                        role=Role.SYSTEM, segments=text_segments(COMPACT_SYSTEM_PROMPT)
                    ),
                    LLMMessage(role=Role.USER, segments=text_segments(prompt)),
                ],
            ),
            call_error="调用压缩模型",
            read_error="读取压缩结果",
        )
        if summary.text == "":
            raise ValueError("压缩模型返回空摘要")
        return CompactResult(
            summary=summary.text,
            assembled_summary=assemble_summary(summary.text, user_inputs),
            usage=summary.usage,
        )

    # -- 摘要 ---------------------------------------------------------------
    async def summarize_text(self, ctx: Any, req: SummarizeRequest) -> SummarizeResult:
        """把长用户消息压成能塞进 ``max_input_tokens`` 的摘要。

        对应 Go ``Compressor.SummarizeText``：分片 -> 逐片摘要 -> 必要时**递归**
        对摘要再摘要（深度上限 4），最后按 token 截断。
        """
        if self.client_for is None:
            raise ValueError("summarizer is not configured")
        if not req.provider or not req.model:
            raise ValueError("摘要模型未配置")
        text = (req.text or "").strip()
        if text == "":
            return SummarizeResult()
        req = _normalize_summarize_request(req)
        summary, usage = await self._summarize_text(ctx, req, text, 0)
        return SummarizeResult(summary=summary, usage=usage)

    async def _summarize_text(
        self, ctx: Any, req: SummarizeRequest, text: str, depth: int
    ) -> tuple[str, Usage | None]:
        text = (text or "").strip()
        if text == "":
            return "", None
        chunks = split_summary_chunks(text, req.chunk_input_tokens)
        if len(chunks) > MAX_SUMMARIZE_CHUNKS:
            raise ValueError(
                f"消息过长（{len(chunks)} 个摘要分片），超过摘要模式上限 {MAX_SUMMARIZE_CHUNKS}"
            )

        summaries: list[str] = []
        usage: Usage | None = None
        for chunk in chunks:
            piece, chunk_usage = await self._summarize_chunk(ctx, req, chunk)
            if piece:
                summaries.append(piece)
            if chunk_usage is not None:
                usage = chunk_usage

        combined = "\n\n".join(summaries).strip()
        if combined == "":
            raise ValueError("摘要模型返回空摘要")
        # 分片多于 1 且合并后仍嫌大 -> 递归再摘要一层
        if (
            len(chunks) > 1
            and estimate_text_tokens(combined) > req.max_input_tokens * 4 // 5
            and depth < MAX_SUMMARIZE_DEPTH
        ):
            return await self._summarize_text(ctx, req, combined, depth + 1)
        if estimate_text_tokens(combined) > req.max_input_tokens:
            combined = truncate_text_to_tokens(combined, req.max_input_tokens)
        return combined, usage

    async def _summarize_chunk(
        self, ctx: Any, req: SummarizeRequest, chunk: str
    ) -> tuple[str, Usage | None]:
        client = self.client_for(req.provider) if self.client_for else None
        if client is None:
            raise ValueError(f'摘要 provider "{req.provider}" 未配置')
        result = await self._collect_stream(
            ctx,
            req.provider,
            ChatRequest(
                model=req.model,
                messages=[
                    LLMMessage(
                        role=Role.SYSTEM,
                        segments=text_segments(TEXT_SUMMARIZER_SYSTEM_PROMPT),
                    ),
                    LLMMessage(
                        role=Role.USER,
                        segments=text_segments("请压缩以下消息：\n\n" + chunk),
                    ),
                ],
                max_tokens=req.max_output_tokens,
            ),
            call_error="调用摘要模型",
            read_error="读取摘要结果",
        )
        return result.text, result.usage

    # -- 流式收集 -----------------------------------------------------------
    async def _collect_stream(
        self,
        ctx: Any,
        provider: str,
        request: ChatRequest,
        *,
        call_error: str,
        read_error: str,
    ) -> "_Collected":
        client = self.client_for(provider) if self.client_for else None
        if client is None:
            raise ValueError(f'provider "{provider}" 未配置')
        try:
            stream = client.chat_stream(ctx, request)
            if hasattr(stream, "__await__"):
                stream = await stream
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{call_error}: {exc}") from exc

        parts: list[str] = []
        usage: Usage | None = None
        async for chunk in _aiter(stream):
            if chunk.error is not None:
                raise ValueError(f"{read_error}: {chunk.error}") from chunk.error
            parts.append(chunk.delta_content or "")
            if chunk.usage is not None:
                usage = chunk.usage
        return _Collected(text="".join(parts).strip(), usage=usage)


@dataclass
class _Collected:
    text: str = ""
    usage: Usage | None = None


async def _aiter(stream: Any) -> Any:
    """同时支持异步迭代器与（同步/异步）返回可迭代对象两种客户端。"""
    if hasattr(stream, "__aiter__"):
        async for item in stream:
            yield item
        return
    for item in stream:
        yield item


def cap_compact_request(req: CompactRequest) -> tuple[list[CompactMessage], list[str]]:
    """按 rune 上限截断用户原话。对应 Go ``capCompactRequest``。

    只截 ``role == "user"`` 的消息与 ``user_inputs``；其他角色原样保留。
    """
    max_runes = req.user_input_max_runes
    if max_runes <= 0:
        max_runes = DEFAULT_USER_ORIGINAL_MAX_RUNES
    messages: list[CompactMessage] = []
    for message in req.messages:
        if (message.role or "").strip().lower() == "user":
            messages.append(
                dataclasses.replace(
                    message,
                    content=truncate_text_with_marker(
                        message.content, max_runes, USER_INPUT_TRUNCATION_MARKER
                    ),
                )
            )
        else:
            messages.append(message)
    user_inputs = [
        truncate_text_with_marker(value, max_runes, USER_INPUT_TRUNCATION_MARKER)
        for value in req.user_inputs
    ]
    return messages, user_inputs


def _normalize_summarize_request(req: SummarizeRequest) -> SummarizeRequest:
    """补默认值。对应 Go ``SummarizeText`` 开头的三段 if。"""
    max_input = req.max_input_tokens if req.max_input_tokens > 0 else 8192
    chunk = req.chunk_input_tokens if req.chunk_input_tokens > 0 else max_input
    output = req.max_output_tokens if req.max_output_tokens > 0 else 1024
    return dataclasses.replace(
        req, max_input_tokens=max_input, chunk_input_tokens=chunk, max_output_tokens=output
    )


def split_summary_chunks(text: str, max_input_tokens: int) -> list[str]:
    """按 rune 切分摘要分片。对应 Go ``splitSummaryChunks``。

    分片长度 = ``max_input_tokens * 3 / 4`` 个 rune（下界 256）；
    注意这里**不按 token** 切，token 只用来定 rune 数（与 Go 一致）。
    """
    text = (text or "").strip()
    if text == "":
        return []
    if max_input_tokens <= 0:
        max_input_tokens = 8192
    chunk_runes = max_input_tokens * 3 // 4
    if chunk_runes < 256:
        chunk_runes = 256
    runes = list(text)
    if len(runes) <= chunk_runes:
        return [text]
    out: list[str] = []
    for start in range(0, len(runes), chunk_runes):
        out.append("".join(runes[start:start + chunk_runes]))
    return out


# Go 的导出名
CapCompactRequest = cap_compact_request
SplitSummaryChunks = split_summary_chunks

__all__ = [
    "MAX_SUMMARIZE_CHUNKS",
    "MAX_SUMMARIZE_DEPTH",
    "USER_INPUT_TRUNCATION_MARKER",
    "CapCompactRequest",
    "CompactMessage",
    "CompactRequest",
    "CompactResult",
    "CompactToolCall",
    "CompactionChoice",
    "Compactor",
    "Compressor",
    "SplitSummaryChunks",
    "SummarizeRequest",
    "SummarizeResult",
    "cap_compact_request",
    "split_summary_chunks",
]
