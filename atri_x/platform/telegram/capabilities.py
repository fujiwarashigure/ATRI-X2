"""Telegram 的可选群目录能力（群信息 + 管理员列表）。

对应 Go：``internal/platform/telegram/capabilities.go``。

Bot API **不能**列出全部成员、也不能拉任意历史，所以：
群成员列表只有管理员，群历史回退本地 Chat History（调用方按可选能力处理）。

形式差异：Go 的 ``(*Adapter)`` 方法 → 模块级协程函数（首个参数是适配器），
由 :class:`atri_x.platform.telegram.adapter.Adapter` 的同名方法委托，
以免 ``adapter`` ↔ ``capabilities`` 循环导入。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.platform import GroupInfo, GroupMember


async def get_group_info(adapter: Any, ctx: Any, group_id: str) -> GroupInfo:
    """取群信息。对应 Go ``(*Adapter).GetGroupInfo``。"""
    chat_id = telegram_chat_id(group_id)
    raw = await adapter.call_platform_api(ctx, "getChat", {"chat_id": chat_id})
    try:
        info = json.loads(raw) if raw else None
    except ValueError as exc:
        raise AtriXError(f"decode telegram getChat response: {exc}") from exc
    if not isinstance(info, dict):
        raise AtriXError("decode telegram getChat response: result is not a JSON object")
    title = _text(info.get("title")).strip()
    if title == "":
        title = _text(info.get("username")).strip()
    return GroupInfo(
        id=str(_int(info.get("id"))),
        name=title,
        member_count=_int(info.get("member_count")),
    )


async def get_group_member_list(adapter: Any, ctx: Any, group_id: str) -> list[GroupMember]:
    """取管理员列表。对应 Go ``(*Adapter).GetGroupMemberList``。"""
    chat_id = telegram_chat_id(group_id)
    raw = await adapter.call_platform_api(ctx, "getChatAdministrators", {"chat_id": chat_id})
    try:
        members = json.loads(raw) if raw else None
    except ValueError as exc:
        raise AtriXError(f"decode telegram getChatAdministrators response: {exc}") from exc
    if not isinstance(members, list):
        raise AtriXError("decode telegram getChatAdministrators response: result is not a JSON array")
    out: list[GroupMember] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        user = member.get("user")
        if not isinstance(user, dict):
            continue
        user_id = _int(user.get("id"))
        if user_id == 0 or user.get("is_bot") is True:
            continue
        display = (_text(user.get("first_name")).strip() + " " + _text(user.get("last_name")).strip()).strip()
        if display == "":
            display = _text(user.get("username")).strip()
        role = _text(member.get("status")).strip()
        if role == "creator":
            role = "owner"
        elif role == "administrator":
            role = "admin"
        out.append(
            GroupMember(
                user_id=str(user_id),
                nickname=display,
                display_name=display,
                role=role,
            )
        )
    return out


def telegram_chat_id(group_id: str) -> str:
    """把 ``group:<id>`` / ``supergroup:<id>`` / 纯数字解析成数字 chat id。

    对应 Go ``telegramChatID``；无法解析时抛 :class:`atri_x.errors.InvalidError`。
    """
    group_id = (group_id or "").strip()
    if group_id == "":
        raise InvalidError("telegram group id is required")
    if _is_int(group_id):
        return group_id
    _, separator, value = group_id.partition(":")
    if separator and _is_int(value.strip()):
        return value.strip()
    raise InvalidError(f"telegram group id {group_id!r} is not numeric")


def _is_int(value: str) -> bool:
    """对应 Go ``strconv.ParseInt(s, 10, 64)`` 的成功判定。"""
    text = (value or "").strip()
    if text == "":
        return False
    body = text[1:] if text[0] in "+-" else text
    return body.isdigit() and body.isascii()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0


__all__ = [name for name in dir() if not name.startswith("_")]
