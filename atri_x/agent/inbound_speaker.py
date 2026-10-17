"""发言人身份：回合输入的快照、显示名与群共享线程标记。

对应 Go：``internal/agent/inbound_speaker.go``。

职责：``turnInputForMessage`` / ``speakerDisplayName`` / ``uniqueInputSpeakers`` /
``attachSpeakerMarker`` / ``inputSpeakerMetadata``。

与 Go 的差异（形式，不是简化）：

* Go 的 map 去重键 ``actorID\\x00userID\\x00name`` 原样保留（同一个 ``\\x00`` 分隔）。
* ``inputSpeakerMetadata`` 的 JSON 键名逐字照抄 Go 的匿名 struct tag：
  ``speakers[].actor_id`` / ``user_id`` / ``name`` / ``role``，全部 ``omitempty``。
* ``turn.Input`` / ``turn.Speaker`` 是共享契约（:mod:`atri_x.turn`），本模块只读字段。
"""

from __future__ import annotations

import json
import re
from typing import Any

from atri_x.llm import MessageSegment, prepend_segment_text
from atri_x.turn import Input, Speaker

from atri_x.agent.tools import first_non_empty, service_host

__all__ = [
    "InboundSpeakerMixin",
    "attach_speaker_marker",
    "input_speaker_metadata",
    "speaker_display_name",
    "turn_input_for_message",
    "unique_input_speakers",
]

_WHITESPACE = re.compile(r"\s+")


def turn_input_for_message(agent: Any, ctx: Any, text: str) -> Input:
    """把当前发言人身份快照到回合输入上。对应 Go ``turnInputForMessage``。

    收件箱用 ``speaker.actor_id`` 判断"同一发言人可合并"，也防止把一个人的权限
    套用到另一个人的文本上。
    """
    host = service_host(agent)
    from atri_x.agent.segments import inbound_turn_input

    input_value = inbound_turn_input(ctx, text)
    actor = _actor(host, ctx)
    actor_id = str(getattr(actor, "id", "") or "").strip() or str(
        getattr(actor, "platform_user_id", "") or ""
    ).strip()
    input_value.speaker = Speaker(
        actor_id=actor_id,
        user_id=str(getattr(actor, "platform_user_id", "") or "").strip(),
        name=first_non_empty(
            getattr(actor, "group_card", ""),
            getattr(actor, "nickname", ""),
            getattr(actor, "display_name", ""),
        ),
        role=str(getattr(actor, "group_role", "") or ""),
    )
    return input_value


def speaker_display_name(speaker: Any) -> str:
    """发言人展示名（含 ``(id:...)``）。对应 Go ``speakerDisplayName``。"""
    name = str(getattr(speaker, "name", "") or "").strip()
    name = _WHITESPACE.sub(" ", name)
    name = name.replace("]", "）").replace("[", "（")
    if not name:
        name = "未知成员"
    user_id = str(getattr(speaker, "user_id", "") or "").strip()
    if not user_id:
        return name
    return name + "(id:" + user_id + ")"


def unique_input_speakers(input_value: Any) -> list[Any]:
    """回合输入里出现过的发言人（去重，保持顺序）。对应 Go ``uniqueInputSpeakers``。"""
    seen: set[str] = set()
    out: list[Any] = []

    def _add(speaker: Any) -> None:
        if (
            not getattr(speaker, "actor_id", "")
            and not getattr(speaker, "user_id", "")
            and not getattr(speaker, "name", "")
        ):
            return
        key = "\x00".join(
            [
                str(getattr(speaker, "actor_id", "") or ""),
                str(getattr(speaker, "user_id", "") or ""),
                str(getattr(speaker, "name", "") or ""),
            ]
        )
        if key in seen:
            return
        seen.add(key)
        out.append(speaker)

    for part in getattr(input_value, "parts", None) or ():
        _add(getattr(part, "speaker", None) or Speaker())
    _add(getattr(input_value, "speaker", None) or Speaker())
    return out


def attach_speaker_marker(agent: Any, ctx: Any, input_value: Any, segments: list[Any]) -> list[Any]:
    """群共享线程下给正文加一行"发言成员"。对应 Go ``attachSpeakerMarker``。

    该行由服务端生成；用户文本永远不会被这个助手当成指令。
    """
    host = service_host(agent)
    if host is None or not _scope(host, ctx).shared:
        return list(segments)
    speakers = unique_input_speakers(input_value)
    if not speakers:
        return list(segments)
    names = "、".join(speaker_display_name(speaker) for speaker in speakers)
    marker = "[发言成员：" + names + "]"
    return prepend_segment_text(list(segments), marker)


def input_speaker_metadata(input_value: Any) -> str:
    """回合输入的发言人 metadata。对应 Go ``inputSpeakerMetadata``。

    json tag：``speakers[].actor_id|user_id|name|role``（全部 ``omitempty``）。
    """
    speakers = unique_input_speakers(input_value)
    if not speakers:
        return ""
    records: list[dict[str, str]] = []
    for speaker in speakers:
        record: dict[str, str] = {}
        for key, attr in (
            ("actor_id", "actor_id"),
            ("user_id", "user_id"),
            ("name", "name"),
            ("role", "role"),
        ):
            value = str(getattr(speaker, attr, "") or "")
            if value:
                record[key] = value
        records.append(record)
    return json.dumps({"speakers": records}, ensure_ascii=False, separators=(",", ":"))


class InboundSpeakerMixin:
    """Go ``*Agent`` 的发言人方法组。"""

    def turn_input_for_message(self, ctx: Any, text: str) -> Input:
        return turn_input_for_message(self, ctx, text)

    def attach_speaker_marker(
        self, ctx: Any, input_value: Any, segments: list[MessageSegment]
    ) -> list[MessageSegment]:
        return attach_speaker_marker(self, ctx, input_value, segments)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _actor(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "actor", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import actor

    return actor(agent, ctx)


def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)
