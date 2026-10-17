"""风险确认内联键盘。

对应 Go：``internal/platform/qqofficial/keyboard.go``。

只有正文里同时出现「高风险工具调用等待确认」与 ``<prefix>confirm`` 时才挂键盘，
避免把普通回复也变成按钮消息。
"""

from __future__ import annotations

from atri_x.platform.qqofficial.types import (
    InlineKeyboardContent,
    KeyboardButton,
    KeyboardButtonAction,
    KeyboardPermission,
    KeyboardRenderData,
    KeyboardRow,
    MessageKeyboard,
)


def should_attach_risk_keyboard(text: str, prefix: str) -> bool:
    """对应 Go ``shouldAttachRiskKeyboard``。"""
    return "高风险工具调用等待确认" in (text or "") and (prefix + "confirm") in (text or "")


def command_button(
    identifier: str, label: str, visited: str, command: str, style: int
) -> KeyboardButton:
    """构造一个"发命令"按钮。对应 Go ``commandButton``。"""
    return KeyboardButton(
        id=identifier,
        render_data=KeyboardRenderData(label=label, visited_label=visited, style=style),
        action=KeyboardButtonAction(
            type=2,
            permission=KeyboardPermission(type=2),
            click_limit=10,
            unsupport_tips=command,
            data=command,
            enter=True,
        ),
    )


def risk_keyboard(app_id: str, prefix: str) -> MessageKeyboard:
    """风险确认键盘。对应 Go ``riskKeyboard``。"""
    return MessageKeyboard(
        content=InlineKeyboardContent(
            bot_appid=app_id,
            rows=[
                KeyboardRow(
                    buttons=[
                        command_button("detail", "详情", "已查看", prefix + "detail", 0),
                        command_button("confirm", "确认", "已确认", prefix + "confirm", 1),
                    ]
                ),
                KeyboardRow(
                    buttons=[
                        command_button(
                            "confirmtool", "确认此工具", "已确认此工具", prefix + "confirmtool", 1
                        ),
                        command_button(
                            "confirmall", "全部确认", "已全部确认", prefix + "confirmall", 1
                        ),
                    ]
                ),
                KeyboardRow(
                    buttons=[
                        command_button("reject", "拒绝", "已拒绝", prefix + "reject", 0),
                        command_button("stop", "停止", "已停止", prefix + "stop", 0),
                    ]
                ),
            ],
        )
    )


__all__ = [name for name in dir() if not name.startswith("_")]
