"""OneBot 的可选平台能力：群历史、群资料、群成员、头像 URL。

对应 Go：``internal/platform/qq-onebot/capabilities.go``。

这些方法实现群分析与学习用到的**可选**能力，是对 OneBot 动作面的尽力而为封装：
没有暴露这些动作的部署只会拿到一个错误，调用方回退到本地 Chat History。

形式差异：

* ``(T, error)`` → 抛异常（:class:`atri_x.errors.AtriXError` 子类）。
* ``time.Time`` → **aware UTC** ``datetime``；Go 的零值 → ``None``
  （``since`` / ``until`` 为 ``None`` 表示不设界）。
* Go 的 ``(*Adapter)`` 方法 → 模块级协程函数（首参是适配器），由
  :class:`atri_x.platform.qq_onebot.adapter.Adapter` 的同名方法委托。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.platform import GroupInfo, GroupMember, HistoryMessage
from atri_x.platform.qq_onebot.message import display_name, normalize_message
from atri_x.platform.qq_onebot.types import Sender, json_str, parse_int64
from atri_x.storage.times import now as storage_now

#: ``get_group_msg_history`` 的条数上限。对应 Go 里的字面量 5000。
GROUP_HISTORY_MAX_LIMIT = 5000

#: 头像尺寸默认值与上限。对应 Go ``GetUserAvatarURL`` 里的字面量。
DEFAULT_AVATAR_SIZE = 100
MAX_AVATAR_SIZE = 640


async def fetch_group_history(
    adapter: Any,
    ctx: Any,
    group_id: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[HistoryMessage]:
    """拉取群历史。对应 Go ``(*Adapter).FetchGroupHistory``。"""
    if adapter is None or adapter.transport is None:
        raise AtriXError("qqonebot transport is not configured")
    gid = parse_int64((group_id or "").strip())
    if gid is None:
        raise InvalidError(
            f"parse qqonebot group id: invalid syntax {(group_id or '').strip()!r}"
        )
    if limit <= 0 or limit > GROUP_HISTORY_MAX_LIMIT:
        limit = GROUP_HISTORY_MAX_LIMIT
    resp = await adapter.transport.call(
        ctx, "get_group_msg_history", {"group_id": gid, "count": limit}
    )
    messages = _history_messages(resp.data)
    out: list[HistoryMessage] = []
    for item in messages:
        created_at = _unix(item.get("time"))
        if since is not None and created_at < since:
            continue
        if until is not None and created_at > until:
            continue
        normalized = normalize_message(item.get("message"), json_str(item.get("raw_message")), 0)
        sender = item.get("sender")
        sender_obj = Sender.from_json(sender)
        sender_id = raw_id_string(item.get("user_id"))
        if sender_id == "":
            sender_id = str(sender_obj.user_id)
        out.append(
            HistoryMessage(
                platform_message_id=raw_id_string(item.get("message_id")),
                sender_id=sender_id,
                sender_name=display_name(sender_obj, sender_obj.user_id),
                text=normalized.text,
                segments=list(normalized.segments),
                created_at=created_at,
            )
        )
    return out


def _history_messages(data: Any) -> list[dict[str, Any]]:
    """``{"messages": [...]}`` 或直接数组。对应 Go 的两次 Unmarshal 尝试。"""
    if isinstance(data, Mapping):
        items = data.get("messages")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    raise InvalidError("decode get_group_msg_history response: unexpected shape")


def _unix(value: Any) -> datetime:
    """``time.Unix(n, 0)`` → aware UTC；非法/缺失退到当前时间。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return storage_now()
    if value <= 0:
        return storage_now()
    return datetime.fromtimestamp(value, timezone.utc)


async def get_group_info(adapter: Any, ctx: Any, group_id: str) -> GroupInfo:
    """取群资料。对应 Go ``(*Adapter).GetGroupInfo``。"""
    if adapter is None or adapter.transport is None:
        raise AtriXError("qqonebot transport is not configured")
    gid = parse_int64((group_id or "").strip())
    if gid is None:
        raise InvalidError(
            f"parse qqonebot group id: invalid syntax {(group_id or '').strip()!r}"
        )
    resp = await adapter.transport.call(ctx, "get_group_info", {"group_id": gid})
    data = resp.data
    if not isinstance(data, Mapping):
        raise InvalidError("decode get_group_info response: not a JSON object")
    resolved_id = raw_id_string(data.get("group_id"))
    if resolved_id == "":
        resolved_id = str(gid)
    member_count = data.get("member_count")
    if isinstance(member_count, bool) or not isinstance(member_count, int):
        member_count = 0
    return GroupInfo(
        id=resolved_id,
        name=json_str(data.get("group_name")).strip(),
        member_count=member_count,
    )


async def get_group_member_list(adapter: Any, ctx: Any, group_id: str) -> list[GroupMember]:
    """取群成员列表。对应 Go ``(*Adapter).GetGroupMemberList``。"""
    if adapter is None or adapter.transport is None:
        raise AtriXError("qqonebot transport is not configured")
    gid = parse_int64((group_id or "").strip())
    if gid is None:
        raise InvalidError(
            f"parse qqonebot group id: invalid syntax {(group_id or '').strip()!r}"
        )
    resp = await adapter.transport.call(ctx, "get_group_member_list", {"group_id": gid})
    data = resp.data
    if not isinstance(data, list):
        raise InvalidError("decode get_group_member_list response: not a JSON array")
    out: list[GroupMember] = []
    for member in data:
        if not isinstance(member, Mapping):
            continue
        user_id = raw_id_string(member.get("user_id"))
        if user_id == "":
            continue
        card = json_str(member.get("card")).strip()
        nickname = json_str(member.get("nickname")).strip()
        display = card if card != "" else nickname
        out.append(
            GroupMember(
                user_id=user_id,
                nickname=nickname,
                group_card=card,
                display_name=display,
                role=json_str(member.get("role")).strip(),
            )
        )
    return out


def get_user_avatar_url(user_id: str, size: int) -> str:
    """拼 QQ 头像 URL。对应 Go ``(*Adapter).GetUserAvatarURL``。

    Go 侧不读 ctx、也不用适配器状态，因此这里是纯函数；适配器方法只做一层委托。
    """
    user_id = (user_id or "").strip()
    if user_id == "":
        raise InvalidError("qqonebot user id is required")
    if size <= 0 or size > MAX_AVATAR_SIZE:
        size = DEFAULT_AVATAR_SIZE
    query = urlencode({"b": "qq", "nk": user_id, "s": str(size)})
    return "https://q1.qlogo.cn/g?" + query


def raw_id_string(raw: Any) -> str:
    """把 ``json.RawMessage`` 形态的 ID 转成字符串。对应 Go ``rawIDString``。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw).strip()
    if isinstance(raw, float):
        return (str(int(raw)) if raw == int(raw) else repr(raw)).strip()
    return str(raw).strip().strip('"')


__all__ = [name for name in dir() if not name.startswith("_")]
