"""上下文基础能力：加载、窗口、用量、预算、压缩与摘要。

对应 Go：``internal/contextmgr``。

职责边界（与上游一致）：

- 这里只提供**基础能力**；"什么时候压缩、压完切到哪个 Session" 由
  ``atri_x.agent.context_*`` 编排，本包不反向依赖 agent。
- :class:`~atri_x.contextmgr.compressor.Compactor` 是后端接口；目前唯一实现是
  客户端摘要（:class:`~atri_x.contextmgr.compressor.Compressor`）。
- ``ctx`` 参数一律按鸭子类型传递；本包**不** import ``atri_x.agent``。
"""

from __future__ import annotations

from atri_x.contextmgr.budget import (
    DEFAULT_MAX_PROMPT_RATIO,
    DEFAULT_RESERVE_OUTPUT_TOKENS,
    DEFAULT_SINGLE_MESSAGE_MAX_RATIO,
    DEFAULT_USER_ORIGINAL_MAX_RUNES,
    FALLBACK_CONTEXT_WINDOW,
    PromptBudget,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    estimate_tools_tokens,
    new_prompt_budget,
    tool_schema_to_json,
    truncate_text_to_tokens,
    truncate_text_with_marker,
)
from atri_x.contextmgr.compact_prompt import (
    COMPACT_SYSTEM_PROMPT,
    TEXT_SUMMARIZER_SYSTEM_PROMPT,
    assemble_summary,
    compact_prompt,
)
from atri_x.contextmgr.compressor import (
    MAX_SUMMARIZE_CHUNKS,
    MAX_SUMMARIZE_DEPTH,
    USER_INPUT_TRUNCATION_MARKER,
    CompactMessage,
    CompactRequest,
    CompactResult,
    CompactToolCall,
    CompactionChoice,
    Compactor,
    Compressor,
    SummarizeRequest,
    SummarizeResult,
    cap_compact_request,
    split_summary_chunks,
)
from atri_x.contextmgr.loader import (
    MAX_FORK_DEPTH,
    STORE_REQUIRED,
    LoadedContext,
    Loader,
)
from atri_x.contextmgr.usage import FormatTokens, UsageState, format_tokens
from atri_x.contextmgr.window import (
    ClientProvider,
    WindowResolver,
    new_window_resolver,
)

__all__ = [
    "COMPACT_SYSTEM_PROMPT",
    "DEFAULT_MAX_PROMPT_RATIO",
    "DEFAULT_RESERVE_OUTPUT_TOKENS",
    "DEFAULT_SINGLE_MESSAGE_MAX_RATIO",
    "DEFAULT_USER_ORIGINAL_MAX_RUNES",
    "FALLBACK_CONTEXT_WINDOW",
    "MAX_FORK_DEPTH",
    "MAX_SUMMARIZE_CHUNKS",
    "MAX_SUMMARIZE_DEPTH",
    "STORE_REQUIRED",
    "TEXT_SUMMARIZER_SYSTEM_PROMPT",
    "USER_INPUT_TRUNCATION_MARKER",
    "ClientProvider",
    "CompactMessage",
    "CompactRequest",
    "CompactResult",
    "CompactToolCall",
    "CompactionChoice",
    "Compactor",
    "Compressor",
    "FormatTokens",
    "LoadedContext",
    "Loader",
    "PromptBudget",
    "SummarizeRequest",
    "SummarizeResult",
    "UsageState",
    "WindowResolver",
    "assemble_summary",
    "cap_compact_request",
    "compact_prompt",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "estimate_tools_tokens",
    "format_tokens",
    "new_prompt_budget",
    "new_window_resolver",
    "split_summary_chunks",
    "tool_schema_to_json",
    "truncate_text_to_tokens",
    "truncate_text_with_marker",
]
