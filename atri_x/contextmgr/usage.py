"""用量状态与厂商 usage 的展示格式。

对应 Go：``internal/contextmgr/usage.go``。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.llm.llm import Usage


@dataclass
class UsageState:
    """最近一次调用的用量与压缩阈值状态。对应 Go ``UsageState``。"""

    usage: Usage | None = None
    context_window: int = 0
    trigger_ratio: float = 0.0
    pending_compact: bool = False

    def tokens_known(self) -> bool:
        """对应 Go ``TokensKnown``。"""
        return self.usage is not None and self.usage.total_tokens > 0

    def usage_ratio(self) -> float:
        """已用窗口占比；未知或窗口非法时为 ``0``。对应 Go ``UsageRatio``。"""
        if not self.tokens_known() or self.context_window <= 0:
            return 0.0
        return float(self.usage.total_tokens) / float(self.context_window)  # type: ignore[union-attr]

    def reached_threshold(self) -> bool:
        """对应 Go ``ReachedThreshold``。"""
        return (
            self.tokens_known()
            and self.trigger_ratio > 0
            and self.usage_ratio() >= self.trigger_ratio
        )


def format_tokens(usage: Usage | None) -> str:
    """把 usage 渲染成展示文本。对应 Go ``FormatTokens``。

    主体文本与 Go 逐字一致（含全角冒号），因为会有测试与用户可见输出断言它。
    Python 扩展：上游响应里没有缓存命中字段时显示「未上报」而不是 0 —— "没上报"
    不等于"确实没命中"，否则用户看到的永远是误导性的 ``命中：0``（见
    :attr:`atri_x.llm.llm.Usage.cache_hit_reported`）。
    """
    if usage is None or usage.total_tokens <= 0:
        return "tokens：unknown（命中：unknown）"
    if not getattr(usage, "cache_hit_reported", True):
        return f"tokens：{usage.total_tokens}（命中：未上报）"
    return f"tokens：{usage.total_tokens}（命中：{usage.cache_hit_tokens}）"


# Go 的导出名
FormatTokens = format_tokens

__all__ = ["FormatTokens", "UsageState", "format_tokens"]
