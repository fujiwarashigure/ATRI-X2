"""合并转发消息的展开渲染，以及 ``message.go`` 里的段/文本自由函数。

对应 Go：``internal/platform/qq-onebot/message.go`` 的转发部分
（``forwardParseState`` 起至文件末尾）与同文件末尾的自由函数
（``segmentDataString`` / ``segmentDataInt64`` / ``firstNonEmpty`` / ``cleanText`` /
``atText`` —— 放在本模块是为了让 :mod:`atri_x.platform.qq_onebot.message` 单向依赖
本模块，避免循环导入；Go 侧同属一个包，无此问题）。

转发展开的硬边界（上游语义，逐条保留）：

* 节点数 / rune 数 / 深度 / 拉取次数 / 单次响应体积 / 非文本节点数各自有上限，
  超限只截断并打标记，不抛异常。
* ``nodeKey`` 命中 ``seen`` 说明循环引用，输出 ``[检测到循环引用，已省略]`` 后返回。
* 转发正文一律以 ``FORWARD_TRUST_MARKER`` 起头，且**不进入**唤醒/命令匹配视图
  （由 ``NormalizedMessage.match_text`` 保证）。

形式差异：Go 的 ``json.RawMessage`` → 已解析的 JSON 值；``fmt.Sprint`` 兜底
字符串化只在异常载荷上生效，见 :func:`raw_any_string`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from atri_x.platform.qq_onebot.types import (
    FORWARD_TRUNCATED_MARKER,
    FORWARD_TRUST_MARKER,
    ForwardLimits,
    Segment,
    default_forward_limits,
)

_NULL_TEXT = "null"


# ---------------------------------------------------------------------------
# 自由函数（Go message.go 末尾）
# ---------------------------------------------------------------------------

def first_non_empty(*values: str) -> str:
    """第一个去空白后非空的值（**已去空白**）。对应 Go ``firstNonEmpty``。"""
    for value in values:
        value = (value or "").strip()
        if value != "":
            return value
    return ""


def clean_text(text: str) -> str:
    """保留原始换行与缩进，只统一换行符并去掉首尾空白。对应 Go ``cleanText``。

    调用方若需要紧凑形式做唤醒/匹配，必须另行归一化：模型输入与 Chat History
    应当看到代码缩进和粘贴的日志。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def at_text(qq: str, name: str) -> str:
    """``[at qq:123]`` / ``[at 昵称 qq:123]``。对应 Go ``atText``。"""
    qq = (qq or "").strip()
    name = (name or "").strip()
    if name == "":
        return "[at qq:" + qq + "]"
    return "[at " + name + " qq:" + qq + "]"


def segment_data_string(data: Mapping[str, Any] | None, key: str) -> str:
    """取段数据里的字符串。对应 Go ``segmentDataString``。

    Go 对 ``float64`` 走 ``fmt.Sprintf("%f")`` 再各去掉**一次**后缀 ``0`` 与 ``.``，
    因此整数也会留下 ``123.00000`` 这样的形态；本函数逐字复刻（含这个怪癖），
    因为 :func:`segment_data_int64` 的失败判定依赖它。
    """
    if not data:
        return ""
    value = data.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _go_float_suffix(float(value))
    if isinstance(value, float):
        return _go_float_suffix(value)
    return _go_sprint(value)


def _go_float_suffix(value: float) -> str:
    """``TrimSuffix(TrimSuffix(Sprintf("%f", v), "0"), ".")``。"""
    text = f"{value:f}"
    if text.endswith("0"):
        text = text[:-1]
    if text.endswith("."):
        text = text[:-1]
    return text


def segment_data_int64(data: Mapping[str, Any] | None, key: str) -> int:
    """对应 Go ``segmentDataInt64``：解析失败返回 0。"""
    value = segment_data_string(data, key)
    if value == "":
        return 0
    try:
        return int(value, 10)
    except ValueError:
        return 0


def raw_any_string(raw: Any) -> str:
    """把任意 JSON 值转成去空白的字符串。对应 Go ``rawAnyString``。

    ``fmt.Sprint`` 分支（对象/数组）在实际载荷里不会出现，这里用
    :func:`_go_sprint` 做近似（Go map 的键序是随机的，本实现按排序输出）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw).strip()
    if isinstance(raw, float):
        return _number_text(raw).strip()
    return _go_sprint(raw).strip()


def _number_text(value: float) -> str:
    """``json.Number`` 的近似：整数浮点不写 ``.0``。"""
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _go_sprint(value: Any) -> str:
    """``fmt.Sprint`` 的近似（容器按排序键展开）。"""
    if isinstance(value, Mapping):
        items = " ".join(f"{key}:{_go_sprint(value[key])}" for key in sorted(value, key=str))
        return "map[" + items + "]"
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(_go_sprint(item) for item in value) + "]"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "<nil>"
    return str(value)


def raw_object_field(object_value: Mapping[str, Any], key: str) -> tuple[Any, bool]:
    """对应 Go ``rawObjectField``（键存在即为 True）。"""
    if key in object_value:
        return object_value[key], True
    return None, False


def raw_object_string(object_value: Mapping[str, Any], key: str) -> str:
    """对应 Go ``rawObjectString``。"""
    if key not in object_value:
        return ""
    return raw_any_string(object_value[key])


def raw_string_equals(raw: Any, want: str) -> bool:
    """对应 Go ``rawStringEquals``（大小写不敏感）。"""
    return raw_any_string(raw).lower() == want.lower()


def first_raw_object_field(
    object_value: Mapping[str, Any], *keys: str
) -> tuple[Any, bool]:
    """取第一个非空非 ``null`` 的字段。对应 Go ``firstRawObjectField``。"""
    for key in keys:
        raw, ok = raw_object_field(object_value, key)
        if not ok:
            continue
        if raw is None:
            continue
        if isinstance(raw, str) and raw.strip() == "":
            continue
        if isinstance(raw, str) and raw.strip() == _NULL_TEXT:
            continue
        return raw, True
    return None, False


# ---------------------------------------------------------------------------
# 转发树渲染
# ---------------------------------------------------------------------------

@dataclass
class ForwardParseState:
    """一次转发展开的累计状态与上限。对应 Go ``forwardParseState``。"""

    limits: ForwardLimits = field(default_factory=default_forward_limits)
    nodes: int = 0
    depth: int = 0
    runes: int = 0
    fetches: int = 0
    non_text: int = 0
    truncated: bool = False
    seen: dict[str, bool] = field(default_factory=dict)


def new_forward_parse_state(limits: ForwardLimits | None = None) -> ForwardParseState:
    """对应 Go ``newForwardParseState``。"""
    effective = (limits or ForwardLimits()).normalized()
    return ForwardParseState(limits=effective, seen={})


def clip_forward_text(state: ForwardParseState, text: str) -> str:
    """按共享 rune 预算裁剪。对应 Go ``(*forwardParseState).clip``。"""
    if not text:
        return ""
    if state.runes + len(text) <= state.limits.max_runes:
        state.runes += len(text)
        return text
    remaining = state.limits.max_runes - state.runes
    if remaining < 0:
        remaining = 0
    state.runes = state.limits.max_runes
    state.truncated = True
    return text[:remaining]


def looks_like_forward_node(raw: Any) -> bool:
    """对应 Go ``looksLikeForwardNode``。"""
    if not isinstance(raw, Mapping):
        return False
    for key in ("content", "message", "messages", "nodes"):
        if key in raw:
            return True
    if raw_object_string(raw, "user_id") != "":
        return True
    if raw_object_string(raw, "nickname") != "":
        return True
    return raw_object_string(raw, "card") != ""


def forward_node_sender(object_value: Mapping[str, Any]) -> tuple[str, str]:
    """对应 Go ``forwardNodeSender``：``(sender_id, sender_name)``。"""
    sender_id = raw_object_string(object_value, "user_id")
    sender_name = first_non_empty(
        raw_object_string(object_value, "card"), raw_object_string(object_value, "nickname")
    )
    raw_sender, ok = raw_object_field(object_value, "sender")
    if ok and isinstance(raw_sender, Mapping):
        sid = raw_object_string(raw_sender, "user_id")
        if sid != "":
            sender_id = sid
        name = first_non_empty(
            raw_object_string(raw_sender, "card"), raw_object_string(raw_sender, "nickname")
        )
        if name != "":
            sender_name = name
    return sender_id, sender_name


def forward_node_time_text(object_value: Mapping[str, Any]) -> str:
    """节点时间戳 → 本地 ``YYYY-MM-DD HH:MM:SS``。对应 Go ``forwardNodeTimeText``。"""
    raw, ok = raw_object_field(object_value, "time")
    if not ok:
        return ""
    value = raw_any_string(raw).strip()
    if value == "":
        return ""
    try:
        number = int(value, 10)
    except ValueError:
        return ""
    if number > 1_000_000_000_000:
        number //= 1000
    try:
        moment = datetime.fromtimestamp(number).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def indent_forward_content(text: str, prefix: str) -> str:
    """逐行加前缀。对应 Go ``indentForwardContent``。"""
    text = (text or "").rstrip("\n")
    if text == "":
        return text
    return "\n".join(prefix + line for line in text.split("\n"))


def forward_non_text(state: ForwardParseState | None, marker: str) -> str:
    """非文本节点计数与省略标记。对应 Go ``forwardNonText``。"""
    if state is None:
        return marker
    state.non_text += 1
    if state.non_text > state.limits.max_non_text:
        state.truncated = True
        return "[更多非文本节点已省略]"
    return marker


def render_forward_segment_text(segment: Segment, state: ForwardParseState) -> str:
    """把一个转发内部的消息段投影成文本。对应 Go ``renderForwardSegmentText``。"""
    kind = segment.type
    if kind == "text":
        return segment_data_string(segment.data, "text")
    if kind == "at":
        return at_text(segment_data_string(segment.data, "qq"), "")
    if kind == "image":
        return forward_non_text(state, "[图片]")
    if kind in ("record", "voice"):
        return forward_non_text(state, "[语音]")
    if kind == "video":
        return forward_non_text(state, "[视频]")
    if kind == "file":
        return forward_non_text(state, "[文件]")
    if kind == "face":
        return "[表情]"
    if kind in ("forward", "node"):
        text, _id, _resolved = normalize_forward_segment_with_state(segment.data, state)
        if text != "":
            return text
        return "[转发消息]"
    if kind != "":
        return "[" + kind + "]"
    return ""


def render_forward_content(items: Sequence[Any], state: ForwardParseState) -> str:
    """渲染一个转发内容数组。对应 Go ``renderForwardContent``。"""
    parts: list[str] = []
    for item in items:
        text, _id, ok = render_forward_data(item, state)
        if not ok or text.strip() == "":
            continue
        parts.append(clip_forward_text(state, text.rstrip("\n")))
        if state.truncated:
            break
    return "\n".join(parts)


def render_forward_raw_content(raw: Any, state: ForwardParseState) -> str:
    """渲染 ``content`` / ``message`` / ``messages`` / ``nodes`` 的原始值。

    对应 Go ``renderForwardRawContent``。
    """
    if raw is None:
        return ""
    if isinstance(raw, list):
        return render_forward_content(raw, state)
    if isinstance(raw, Mapping):
        text, _id, ok = render_forward_data(raw, state)
        return text if ok else ""
    if isinstance(raw, str):
        return clip_forward_text(state, raw)
    return ""


def render_forward_node(raw: Any, state: ForwardParseState) -> tuple[str, str, bool]:
    """渲染一个转发节点。对应 Go ``renderForwardNode``。"""
    if state.nodes >= state.limits.max_nodes:
        state.truncated = True
        return "", "", True
    if not isinstance(raw, Mapping):
        return "", "", False
    object_value: Mapping[str, Any] = raw
    inner = raw.get("data")
    if inner is not None and not isinstance(inner, Mapping):
        return "", "", False
    if isinstance(inner, Mapping):
        object_value = inner
    node_key = raw_object_string(object_value, "message_id")
    if node_key == "":
        node_key = raw_object_string(object_value, "id")
    tracked = node_key != ""
    if tracked:
        if state.seen.get(node_key):
            return "\n[检测到循环引用，已省略]", "", True
        state.seen[node_key] = True
    try:
        state.nodes += 1
        state.depth += 1
        try:
            sender_id, sender_name = forward_node_sender(object_value)
            message_id = raw_object_string(object_value, "message_id")
            if message_id == "":
                message_id = raw_object_string(object_value, "id")
            header = "[转发节点]"
            if sender_name != "" or sender_id != "":
                header += " 发送者："
                header += sender_name if sender_name != "" else "未知"
                if sender_id != "":
                    header += "（" + sender_id + "）"
            timestamp = forward_node_time_text(object_value)
            if timestamp != "":
                header += " 时间：" + timestamp
            if message_id != "":
                header += " 消息ID：" + message_id
            content_raw, has_content = first_raw_object_field(
                object_value, "content", "message", "messages", "nodes"
            )
            if not has_content:
                return header, "", True
            content = render_forward_raw_content(content_raw, state)
            if content.strip() == "":
                content = "[空消息]"
            content = indent_forward_content(content, "  ")
            content = clip_forward_text(state, content)
            return header + "\n" + content, "", True
        finally:
            state.depth -= 1
    finally:
        if tracked:
            state.seen.pop(node_key, None)


def render_forward_data(raw: Any, state: ForwardParseState) -> tuple[str, str, bool]:
    """渲染任意转发载荷。对应 Go ``renderForwardData``。

    返回 ``(text, forward_id, ok)``。``forward_id`` 非空且第二个返回值为 False
    表示这条转发还没被展开（调用方需要按 id 再拉一次）。
    """
    if raw is None:
        return "", "", False
    if state.depth >= state.limits.max_depth:
        state.truncated = True
        return "\n[转发层级超过上限，已截断]", "", True
    if isinstance(raw, Mapping):
        inner = raw.get("data")
        if inner is not None and (
            raw_string_equals(raw.get("type"), "node") or looks_like_forward_node(inner)
        ):
            return render_forward_node(inner, state)
        if looks_like_forward_node(raw):
            return render_forward_node(raw, state)
        # 转发内部的一条消息段：渲染它的文本投影。
        segment_type = raw.get("type")
        if isinstance(segment_type, str) and segment_type != "":
            data = raw.get("data")
            segment = Segment(type=segment_type, data=dict(data) if isinstance(data, Mapping) else {})
            return render_forward_segment_text(segment, state), "", True
        # 没有显式 type 但带 content/messages 等键的对象也可能是节点。
        for key in ("content", "message", "messages", "nodes"):
            if key in raw:
                return render_forward_node(raw, state)
        forward_id = raw_object_string(raw, "id")
        if forward_id != "":
            return "", forward_id, True
        return "", "", False
    if isinstance(raw, list):
        return render_forward_content(raw, state), "", True
    if isinstance(raw, str):
        return clip_forward_text(state, raw), "", True
    return "", "", False


def normalize_forward_segment_with_state(
    data: Mapping[str, Any] | None, state: ForwardParseState | None
) -> tuple[str, str, bool]:
    """把 ``forward`` / ``node`` 段的 ``data`` 渲染成带信任标记的正文。

    对应 Go ``normalizeForwardSegmentWithState``：返回 ``(text, forward_id, resolved)``。
    """
    if state is None:
        state = new_forward_parse_state()
    body, forward_id, _resolved = render_forward_data(dict(data or {}), state)
    if body == "" and forward_id == "":
        return "", "", False
    resolved = body != ""
    parts = [FORWARD_TRUST_MARKER]
    if body != "":
        parts.append("\n" + body)
    if forward_id != "":
        if body != "":
            parts.append("\n")
        parts.append("[转发消息 id:" + forward_id + "]")
    if state.truncated:
        parts.append(FORWARD_TRUNCATED_MARKER)
    return "".join(parts), forward_id, resolved


def normalize_forward_segment(
    data: Mapping[str, Any] | None, limits: ForwardLimits | None = None
) -> tuple[str, str, bool]:
    """对应 Go ``normalizeForwardSegment``（每次调用新建解析状态）。"""
    return normalize_forward_segment_with_state(data, new_forward_parse_state(limits))


__all__ = [name for name in dir() if not name.startswith("_")]
