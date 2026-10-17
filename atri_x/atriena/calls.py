"""Atriena 平台调用（capability 与 raw 两种形态）。

对应 Go：``internal/atriena/calls.go``。

调用方可以写"我想要的能力"（``capability``：撤回消息 / 禁言成员 / 退群），由本模块
翻译成平台原生 API（OneBot 的 ``delete_msg``、Telegram 的 ``deleteMessage`` 等）；
也可以直接写平台原生 API（``raw``）。能力名与平台原生名的映射是 **纯函数**，
可以直接单测，不需要平台适配器。

Python 侧的差异（都是等价物，不是简化）：

* ``PlatformAPICaller`` / ``PlatformCallerResolver`` 在 Go 里是 interface，本仓用
  :class:`typing.Protocol` + 鸭子类型表达；平台适配器（如 telegram）已经实现了
  ``async def call_platform_api(ctx, api, params) -> str``。
* ``PlatformCallerResolver.platform_caller`` 返回 ``(caller, ok)`` 二元组，
  对应 Go 的 ``(PlatformAPICaller, bool)`` 双返回（Python 不能返回 nil 接口值，
  所以用 ``ok`` 表达"平台没有这个能力"）。
* Go ``ExecuteCalls`` 返回"部分结果 + error"；本仓抛
  :class:`CallExecutionError` 并把它携带的 ``results`` 一路带出来（与
  ``atri_x.delivery.DeliveryError`` 携带 ``receipt`` 是同一套惯例）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from atri_x.atriena.types import Call, CallKind, CallKindCapability, CallKindRaw, CallResult, Target
from atri_x.errors import AtriXError, InvalidError

__all__ = [
    "CAPABILITY_CHAT_LEAVE",
    "CAPABILITY_MEMBER_MUTE",
    "CAPABILITY_MESSAGE_RECALL",
    "CallExecutionError",
    "CapabilityChatLeave",
    "CapabilityMemberMute",
    "CapabilityMessageRecall",
    "PlatformAPICaller",
    "PlatformCallerResolver",
    "RawCall",
    "call_platform",
    "clone_params",
    "execute_call",
    "execute_calls",
    "map_capability",
    "map_chat_leave",
    "map_member_mute",
    "map_message_recall",
    "raw_call",
    "required_int_param",
    "resolve_raw_call",
    "target_or_param_id",
    "to_int64",
]

#: 跨平台能力名。对应 Go ``CapabilityMessageRecall`` / ``CapabilityMemberMute`` /
#: ``CapabilityChatLeave``。
CapabilityMessageRecall = "message.recall"
CapabilityMemberMute = "member.mute"
CapabilityChatLeave = "chat.leave"

#: ``snake_case`` 别名（常量也允许 snake_case 形态，见 PORTING §1.4）。
CAPABILITY_MESSAGE_RECALL = CapabilityMessageRecall
CAPABILITY_MEMBER_MUTE = CapabilityMemberMute
CAPABILITY_CHAT_LEAVE = CapabilityChatLeave

#: OneBot 平台的两个别名（Go 的 ``case "qq-onebot", "qqonebot"``）。
_ONEBOT_PLATFORMS = ("qq-onebot", "qqonebot")


@runtime_checkable
class PlatformAPICaller(Protocol):
    """一个平台的原生 API 调用能力。对应 Go ``PlatformAPICaller``。

    ``params`` 是平台原生参数；返回值是平台原始响应（Go 是 ``json.RawMessage``，
    本仓平台适配器返回 ``str``，故这里按鸭子类型声明为 ``Any``）。
    """

    async def call_platform_api(self, ctx: Any, api: str, params: dict[str, Any] | None) -> Any:
        ...


@runtime_checkable
class PlatformCallerResolver(Protocol):
    """按平台名取原生 API 调用器。对应 Go ``PlatformCallerResolver``。

    返回 ``(caller, ok)``：``ok`` 为假表示该平台不支持原生 API 调用。
    """

    def platform_caller(self, platform: str) -> tuple[Any, bool]:
        ...


class CallExecutionError(AtriXError):
    """一次批量平台调用失败；``results`` 保留已经完成（含失败那条）的结果。

    对应 Go ``ExecuteCalls`` 的 ``([]CallResult, error)`` 双返回：失败项也已写进
    结果列表，调用方据此知道"哪些调用真的发出去了"。
    """

    def __init__(self, message: str, results: list[CallResult] | None = None) -> None:
        super().__init__(message)
        self.results: list[CallResult] = list(results or [])


@dataclass
class RawCall:
    """翻译后的平台原生调用。对应 Go ``RawCall``。"""

    platform: str = ""
    api: str = ""
    params: dict[str, Any] = field(default_factory=dict)


async def execute_calls(
    ctx: Any, resolver: Any, calls: list[Call] | None
) -> list[CallResult]:
    """按顺序执行一批调用，返回结果列表。对应 Go ``ExecuteCalls``。

    任一条失败即停：抛 :class:`CallExecutionError`（其 ``results`` 含失败项与之前
    的成功项）。全部成功时返回与 ``calls`` 等长的结果列表。
    """
    results: list[CallResult] = []
    for call in calls or []:
        result = CallResult(call=call)
        try:
            response = await execute_call(ctx, resolver, call)
        except BaseException as exc:
            result.error = str(exc)
            results.append(result)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise CallExecutionError(str(exc), results) from exc
        result.response = _response_text(response)
        results.append(result)
    return results


async def execute_call(ctx: Any, resolver: Any, call: Call) -> Any:
    """执行单条调用。对应 Go ``ExecuteCall``。"""
    if resolver is None:
        raise InvalidError("platform api caller resolver is not configured")
    raw = resolve_raw_call(call)
    platform = (raw.platform or "").strip()
    if not platform:
        raise InvalidError("call platform is required")
    caller, ok = resolver.platform_caller(platform)
    if not ok or caller is None:
        raise InvalidError(f"platform {platform!r} does not support api calls")
    return await caller.call_platform_api(ctx, raw.api, raw.params)


def _response_text(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, (bytes, bytearray)):
        return bytes(response).decode("utf-8", "replace")
    return str(response)


def resolve_raw_call(call: Call) -> RawCall:
    """把一条 :class:`Call` 解析成平台原生调用。对应 Go ``ResolveRawCall``。

    ``kind`` 为空时按 Go 的推断规则：有 ``name`` → capability，否则有 ``api`` →
    raw；两者都空则落到 ``unsupported call kind ""``。
    """
    kind = call.kind
    if kind == "":
        if (call.name or "").strip():
            kind = CallKindCapability
        elif (call.api or "").strip():
            kind = CallKindRaw
    if kind == CallKindRaw:
        return raw_call(call)
    if kind == CallKindCapability:
        return map_capability(call)
    raise InvalidError(f"unsupported call kind {kind!r}")


def raw_call(call: Call) -> RawCall:
    """``kind=raw`` 的直通分支。对应 Go ``rawCall``。"""
    api = (call.api or "").strip()
    if not api:
        raise InvalidError("raw call api is required")
    return RawCall(platform=call_platform(call), api=api, params=clone_params(call.params))


def map_capability(call: Call) -> RawCall:
    """能力名 → 平台原生调用。对应 Go ``mapCapability``。"""
    name = (call.name or "").strip()
    if name == CapabilityMessageRecall:
        return map_message_recall(call)
    if name == CapabilityMemberMute:
        return map_member_mute(call)
    if name == CapabilityChatLeave:
        return map_chat_leave(call)
    raise InvalidError(f"unsupported capability {name!r}")


def map_message_recall(call: Call) -> RawCall:
    """``message.recall`` 的平台映射。对应 Go ``mapMessageRecall``。"""
    platform = call_platform(call)
    params = clone_params(call.params)
    message_id = required_int_param(params, "message_id")
    if platform in _ONEBOT_PLATFORMS:
        return RawCall(platform=platform, api="delete_msg", params={"message_id": message_id})
    if platform == "telegram":
        chat_id = target_or_param_id(call.target, params, "chat_id")
        return RawCall(
            platform=platform,
            api="deleteMessage",
            params={"chat_id": chat_id, "message_id": message_id},
        )
    raise InvalidError(
        f"capability {CapabilityMessageRecall} is not mapped for platform {platform!r}"
    )


def map_member_mute(call: Call) -> RawCall:
    """``member.mute`` 的平台映射。对应 Go ``mapMemberMute``。"""
    platform = call_platform(call)
    params = clone_params(call.params)
    user_id = required_int_param(params, "user_id")
    duration = required_int_param(params, "duration_seconds")
    if duration <= 0:
        raise InvalidError("duration_seconds must be positive")
    if platform in _ONEBOT_PLATFORMS:
        group_id = target_or_param_id(call.target, params, "group_id")
        return RawCall(
            platform=platform,
            api="set_group_ban",
            params={"group_id": group_id, "user_id": user_id, "duration": duration},
        )
    if platform == "telegram":
        chat_id = target_or_param_id(call.target, params, "chat_id")
        until = int(time.time()) + duration
        permissions = {
            "can_send_messages": False,
            "can_send_audios": False,
            "can_send_documents": False,
            "can_send_photos": False,
            "can_send_videos": False,
            "can_send_video_notes": False,
            "can_send_voice_notes": False,
            "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
        }
        return RawCall(
            platform=platform,
            api="restrictChatMember",
            params={
                "chat_id": chat_id,
                "user_id": user_id,
                "until_date": until,
                "permissions": permissions,
            },
        )
    raise InvalidError(
        f"capability {CapabilityMemberMute} is not mapped for platform {platform!r}"
    )


def map_chat_leave(call: Call) -> RawCall:
    """``chat.leave`` 的平台映射。对应 Go ``mapChatLeave``。"""
    platform = call_platform(call)
    params = clone_params(call.params)
    if platform in _ONEBOT_PLATFORMS:
        group_id = target_or_param_id(call.target, params, "group_id")
        out: dict[str, Any] = {"group_id": group_id}
        if "is_dismiss" in params:
            out["is_dismiss"] = params["is_dismiss"]
        return RawCall(platform=platform, api="set_group_leave", params=out)
    if platform == "telegram":
        chat_id = target_or_param_id(call.target, params, "chat_id")
        return RawCall(platform=platform, api="leaveChat", params={"chat_id": chat_id})
    raise InvalidError(
        f"capability {CapabilityChatLeave} is not mapped for platform {platform!r}"
    )


def call_platform(call: Call) -> str:
    """取调用所属平台：``platform`` 优先，否则回落到 ``target.platform``。"""
    platform = (call.platform or "").strip()
    if platform:
        return platform
    target = call.target
    return ((target.platform if target is not None else "") or "").strip()


def target_or_param_id(target: Target | None, params: dict[str, Any], key: str) -> int:
    """目标 ID 优先，其次参数。对应 Go ``targetOrParamID``。

    ``target.id`` 非空但解析失败时直接抛错（不静默回落到参数）。
    """
    target_id = (target.id if target is not None else "") or ""
    if target_id:
        return _parse_int(target_id, key="target.id")
    return required_int_param(params, key)


def required_int_param(params: dict[str, Any], key: str) -> int:
    """取必需整数参数。对应 Go ``requiredIntParam``（缺参数/类型错分别报错）。"""
    if key not in params:
        raise InvalidError(f"{key} is required")
    value = to_int64(params[key])
    if value is None:
        raise InvalidError(f"{key} must be integer")
    return value


def to_int64(value: Any) -> int | None:
    """宽容取整数。对应 Go ``toInt64``（不能转换返回 ``None`` 即 Go 的 ``false``）。

    与 Go 一致：``bool`` **不算**整数（Go 的 ``any`` 断言不会命中 ``int``）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        as_int = int(value)
        return as_int if value == float(as_int) else None
    if isinstance(value, str):
        return _try_parse_int(value)
    return None


def clone_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """浅拷贝参数表。对应 Go ``cloneParams``（``nil`` → 空表）。"""
    return dict(params) if params else {}


def _try_parse_int(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _parse_int(value: str, key: str = "value") -> int:
    parsed = _try_parse_int(value)
    if parsed is None:
        raise InvalidError(f"{key} must be integer, got {value!r}")
    return parsed
