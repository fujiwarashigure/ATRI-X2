"""高风险工具确认的内联键盘。

对应 Go：``internal/platform/telegram/keyboard.go``。

按钮的 ``callback_data`` 就是普通 slash 命令文本，点击后由
``handleCallbackQuery`` 转成一条入站消息，复用既有确认命令，不新增平台专属协议。
"""

from __future__ import annotations

from atri_x.platform.telegram.types import InlineKeyboardButton, ReplyMarkup

#: 风险确认提示的特征串。对应 Go 的字面量。
RISK_CONFIRM_MARKER = "高风险工具调用等待确认"


def should_attach_risk_keyboard(text: str, prefix: str) -> bool:
    """该文本是否要附带确认键盘。对应 Go ``shouldAttachRiskKeyboard``。"""
    text = text or ""
    return RISK_CONFIRM_MARKER in text and (prefix + "confirm") in text


def risk_keyboard(prefix: str) -> ReplyMarkup:
    """对应 Go ``riskKeyboard``。"""
    return ReplyMarkup(
        inline_keyboard=[
            [
                command_button("详情", prefix + "detail"),
                command_button("确认", prefix + "confirm"),
            ],
            [
                command_button("确认此工具", prefix + "confirmtool"),
                command_button("全部确认", prefix + "confirmall"),
            ],
            [
                command_button("拒绝", prefix + "reject"),
                command_button("停止", prefix + "stop"),
            ],
        ]
    )


def command_button(label: str, command: str) -> InlineKeyboardButton:
    """对应 Go ``commandButton``。"""
    return InlineKeyboardButton(text=label, callback_data=command)


__all__ = [name for name in dir() if not name.startswith("_")]
