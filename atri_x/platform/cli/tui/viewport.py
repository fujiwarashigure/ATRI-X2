"""行缓冲视口：``bubbles/viewport`` 的最小等价物。

对应 Go：``tui.go`` / ``tui_copy.go`` 里 ``viewport.Model`` 的用法。

Go 的 viewport 持有已渲染的行，``View()`` 只截取 ``[YOffset, YOffset+Height)``。
本实现沿用同一模型（内容行 + 偏移），因此 copy mode 的
``ensureViewportLineVisible`` 与鼠标滚轮行为可以逐行对照。

与 Go 的差异：``SetContent`` 在 Go 里会保留 YOffset（除非 ``GotoBottom`` 被显式调用），
本实现同样**不**在 ``set_content`` 里改偏移，只在 :meth:`goto_bottom` 里改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 滚轮一格滚动的行数（Go 里由 bubbles 的 ``MouseWheelDelta`` 提供，缺省 3）。
DEFAULT_MOUSE_WHEEL_DELTA = 3

__all__ = ["DEFAULT_MOUSE_WHEEL_DELTA", "Viewport"]


@dataclass
class Viewport:
    """一段可滚动的行缓冲。对应 ``viewport.Model``。"""

    width: int = 0
    height: int = 0
    y_offset: int = 0
    mouse_wheel_delta: int = DEFAULT_MOUSE_WHEEL_DELTA
    _lines: list[str] = field(default_factory=list)

    # -- 内容 ---------------------------------------------------------------
    def set_content(self, content: str) -> None:
        """设置内容（按 ``\\n`` 拆行）。对应 ``SetContent``。"""
        self._lines = content.split("\n") if content != "" else []

    def content(self) -> str:
        """当前内容全文。对应 ``View`` 之外的读取口（Go 里没有直接对应物）。"""
        return "\n".join(self._lines)

    def lines(self) -> list[str]:
        """内容行快照。"""
        return list(self._lines)

    def line_count(self) -> int:
        """内容行数。"""
        return len(self._lines)

    def max_y_offset(self) -> int:
        """最大偏移（内容比视口矮时为 0）。"""
        return max(0, len(self._lines) - max(1, self.height))

    # -- 滚动 ---------------------------------------------------------------
    def scroll_up(self, amount: int) -> None:
        """向上滚动（``YOffset`` 减小）。对应 ``ScrollUp``。"""
        self.y_offset = max(0, self.y_offset - max(0, amount))

    def scroll_down(self, amount: int) -> None:
        """向下滚动（``YOffset`` 增大）。对应 ``ScrollDown``。"""
        self.y_offset = min(self.max_y_offset(), self.y_offset + max(0, amount))

    def goto_bottom(self) -> None:
        """跳到底部。对应 ``GotoBottom``。"""
        self.y_offset = self.max_y_offset()

    def set_y_offset(self, value: int) -> None:
        """直接设置偏移（copy mode 的 ``ensureViewportLineVisible`` 用）。"""
        self.y_offset = max(0, min(self.max_y_offset(), value))

    # -- 渲染 ---------------------------------------------------------------
    def view(self) -> str:
        """渲染可见区域。对应 ``View``。"""
        if not self._lines:
            return "\n".join([""] * max(1, self.height)) if self.height > 0 else ""
        height = max(1, self.height)
        visible = self._lines[self.y_offset : self.y_offset + height]
        return "\n".join(visible)
