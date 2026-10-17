"""群聊天统计与可选摘要。对应 Go：``internal/groupanalysis/``。

* :mod:`atri_x.groupanalysis.service` —— Go ``service.go``：确定性统计与文本报告。
* :mod:`atri_x.groupanalysis.summarizer` —— Go ``summarizer.go``：可选 ``Summarizer``
  与 ``LLMSummarizer``。

本模块只做再导出。
"""

from __future__ import annotations

from atri_x.groupanalysis.service import (
    DEFAULT_RANGE_LIMIT,
    MAX_RANGE_LIMIT,
    MAX_SENDER_NAME_RUNES,
    MAX_TOP_SENDERS,
    NewService,
    Report,
    Request,
    SenderStat,
    Service,
    format_time,
    new_service,
    peak_hour,
    report_inline,
    top_senders,
)
from atri_x.groupanalysis.summarizer import (
    DEFAULT_SUMMARY_INPUT_RUNES,
    DEFAULT_SUMMARY_MAX_RUNES,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TIMEOUT,
    LLMSummarizer,
    Summarizer,
    SummaryPrompt,
)

__all__ = [
    "DEFAULT_RANGE_LIMIT",
    "DEFAULT_SUMMARY_INPUT_RUNES",
    "DEFAULT_SUMMARY_MAX_RUNES",
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "DEFAULT_SUMMARY_TIMEOUT",
    "LLMSummarizer",
    "MAX_RANGE_LIMIT",
    "MAX_SENDER_NAME_RUNES",
    "MAX_TOP_SENDERS",
    "NewService",
    "Report",
    "Request",
    "SenderStat",
    "Service",
    "Summarizer",
    "SummaryPrompt",
    "format_time",
    "new_service",
    "peak_hour",
    "report_inline",
    "top_senders",
]
