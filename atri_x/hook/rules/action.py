"""规则动作的执行：改文本、发消息、跑工具、调插件。

对应 Go：``internal/hook/rules/action.go``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from atri_x.delivery.delivery import Output
from atri_x.hook.event import Point
from atri_x.hook.match import event_match_context
from atri_x.hook.output import (
    BuildOptions,
    Group,
    Segment,
    Target,
    build_group,
)
from atri_x.hook.rules.types import Action, Rule
from atri_x.llm import llm
from atri_x.llm.llm import ToolCallRequest
from atri_x.logging.contract import ModuleHook, ResultFailed, ResultSucceeded
from atri_x.tool.tool import CallRequest

__all__ = [
    "State",
    "ActionResult",
    "run_action",
    "apply_text_action",
    "make_outputs",
    "render",
    "rendered_action_target",
    "first_non_empty",
    "byte_size",
    "set_text_field",
    "allow_field",
]


@dataclass
class ActionResult:
    """一个动作的结果。对应 Go ``actionResult``。"""

    result: str = ""
    error: str = ""
    matched: bool | None = None
    pass_through: bool | None = None


@dataclass
class State:
    """规则内共享的动作结果。对应 Go ``state``。"""

    actions: dict[str, ActionResult] = field(default_factory=dict)


async def run_action(
    module: Any, ctx: Any, event: Any, action: Action, state: State
) -> tuple[Any, ActionResult]:
    """执行一个动作。对应 Go ``Module.runAction``。"""
    kind = (action.type or "").strip()
    if kind in ("prepend", "append", "replace", "delete"):
        return apply_text_action(event, action, state), ActionResult()
    if kind == "send":
        outputs = make_outputs(action, event, state)
        event.outputs.extend(outputs)
        return event, ActionResult()
    if kind == "exec":
        event, result = await module.run_exec(ctx, event, action, state)
        key = first_non_empty((action.action_name or "").strip(), "exec")
        if key:
            state.actions[key] = result
        return event, result
    if kind == "tool":
        result = await call_tool(module, ctx, event, action, state)
        key = first_non_empty(
            (action.action_name or "").strip(), (action.tool or "").strip()
        )
        if key:
            state.actions[key] = result
        return event, result
    raise ValueError(f"unsupported action type {action.type!r}")


def apply_text_action(event: Any, action: Action, state: State) -> Any:
    """改文本类动作。对应 Go ``applyTextAction``。"""
    field_name = (action.field or "").strip()
    allow_field(event, field_name)
    kind = (action.type or "").strip()
    if kind == "prepend":
        return prepend_text_field(event, field_name, render(action.text, event, state))
    if kind == "append":
        return append_text_field(event, field_name, render(action.text, event, state))
    if kind == "replace":
        pattern = _compile(render(action.match, event, state))
        return replace_text_field(
            event,
            field_name,
            pattern,
            render(action.replace, event, state),
            action.all,
        )
    if kind == "delete":
        pattern = _compile(render(first_non_empty(action.match, action.text), event, state))
        return replace_text_field(event, field_name, pattern, "", True)
    raise ValueError(f"unsupported text action {action.type!r}")


def prepend_text_field(event: Any, field_name: str, value: str) -> Any:
    """对应 Go ``prependTextField``。"""
    if field_name in ("message.text", "llm.latest_user_text"):
        event.message.segments = llm.prepend_segment_text(event.message.segments, value)
    elif field_name == "llm.text":
        event.llm.text = value + event.llm.text
    elif field_name == "tool.arguments":
        event.tool.arguments = value + event.tool.arguments
    elif field_name == "tool.result":
        event.message.segments = llm.prepend_segment_text(
            tool_result_segments(event), value
        )
        event.tool.result = llm.segments_text_only(event.message.segments)
    else:
        raise ValueError(f"unsupported prepend field {field_name!r}")
    return event


def append_text_field(event: Any, field_name: str, value: str) -> Any:
    """对应 Go ``appendTextField``。"""
    if field_name in ("message.text", "llm.latest_user_text"):
        event.message.segments = llm.append_segment_text(event.message.segments, value)
    elif field_name == "llm.text":
        event.llm.text += value
    elif field_name == "tool.arguments":
        event.tool.arguments += value
    elif field_name == "tool.result":
        event.message.segments = llm.append_segment_text(
            tool_result_segments(event), value
        )
        event.tool.result = llm.segments_text_only(event.message.segments)
    else:
        raise ValueError(f"unsupported append field {field_name!r}")
    return event


def replace_text_field(
    event: Any, field_name: str, pattern: Any, replacement: str, all_: bool
) -> Any:
    """对应 Go ``replaceTextField``。"""
    if field_name in ("message.text", "llm.latest_user_text"):
        event.message.segments = llm.replace_segment_text(
            event.message.segments, pattern, replacement, all_
        )
    elif field_name == "llm.text":
        event.llm.text = replace_string(event.llm.text, pattern, replacement, all_)
    elif field_name == "tool.arguments":
        event.tool.arguments = replace_string(
            event.tool.arguments, pattern, replacement, all_
        )
    elif field_name == "tool.result":
        event.message.segments = llm.replace_segment_text(
            tool_result_segments(event), pattern, replacement, all_
        )
        event.tool.result = llm.segments_text_only(event.message.segments)
    else:
        raise ValueError(f"unsupported replace field {field_name!r}")
    return event


def replace_string(text: str, pattern: Any, replacement: str, all_: bool) -> str:
    """对应 Go ``replaceString``（非 ``all`` 时只替换第一处）。"""
    if all_:
        return pattern.sub(replacement, text)
    match = pattern.search(text)
    if match is None:
        return text
    return (
        text[: match.start()]
        + pattern.sub(replacement, text[match.start() : match.end()])
        + text[match.end() :]
    )


def tool_result_segments(event: Any) -> list[Any]:
    """``tool.result`` 动作作用的段。对应 Go ``toolResultSegments``。"""
    if event.message.segments:
        return event.message.segments
    return llm.text_segments(event.tool.result)


def allow_field(event: Any, field_name: str) -> None:
    """该 Hook 点是否允许编辑这个字段。对应 Go ``allowField``。"""
    point = event.point
    if point in (Point.PLATFORM_MESSAGE_RECEIVED, Point.AGENT_INPUT_PREPARED):
        if field_name == "message.text":
            return
    elif point in (Point.LLM_TURN_PREPARED, Point.LLM_REQUEST_PREPARED):
        if field_name == "llm.latest_user_text":
            return
    elif point == Point.LLM_RESPONSE_RECEIVED:
        if field_name == "llm.text":
            return
    elif point == Point.TOOL_CALL_PREPARED:
        if field_name == "tool.arguments":
            return
    elif point == Point.TOOL_CALL_COMPLETED:
        if field_name == "tool.result":
            return
    elif point in (
        Point.AGENT_OUTPUT_PREPARED,
        Point.AGENT_TURN_OUTPUT_PREPARED,
        Point.PLATFORM_MESSAGE_SENT,
    ):
        if field_name == "message.text" and event.message.role == llm.Role.ASSISTANT:
            return
    raise ValueError(
        f"field {field_name!r} cannot be edited at hook point {event.point!r}"
    )


def make_outputs(action: Action, event: Any, state: State) -> list[Output]:
    """构造 ``send`` 动作的输出。对应 Go ``makeOutputs``。"""
    target = Target(
        platform=render(action.target.platform, event, state),
        scope_id=render(action.target.scope_id, event, state),
        private_user_id=render(action.target.private_user_id, event, state),
        group_id=render(action.target.group_id, event, state),
        superadmins=action.target.superadmins,
    )
    if not target.platform.strip() and event.point == Point.PLATFORM_CONNECTED:
        target.platform = event.platform.name
    quick = Segment(
        kind=action.kind,
        text=action.text,
        url=action.url,
        path=action.path,
        base64=action.base64,
        name=action.name,
        mime_type=action.mime_type,
        user_id=action.user_id,
        message_id=action.message_id,
        emoticon_id=action.emoticon_id,
    )
    if action.outputs and segment_spec_set(quick):
        raise ValueError("send action cannot combine outputs with quick output fields")
    specs = list(action.outputs) if action.outputs else [quick]
    rendered = [
        Segment(
            kind=render(seg.kind, event, state),
            text=render(seg.text, event, state),
            url=render(seg.url, event, state),
            path=render(seg.path, event, state),
            base64=render(seg.base64, event, state),
            name=render(seg.name, event, state),
            mime_type=render(seg.mime_type, event, state),
            user_id=render(seg.user_id, event, state),
            message_id=render(seg.message_id, event, state),
            emoticon_id=render(seg.emoticon_id, event, state),
        )
        for seg in specs
    ]
    return build_group(
        Group(
            outputs=rendered,
            target=target,
            timing=render(action.timing, event, state),
        ),
        BuildOptions(base_dir=source_base_dir(action)),
    )


def segment_spec_set(spec: Segment) -> bool:
    """对应 Go ``segmentSpecSet``。"""
    return bool(
        spec.kind
        or spec.text
        or spec.url
        or spec.path
        or spec.base64
        or spec.name
        or spec.mime_type
        or spec.user_id
        or spec.message_id
        or spec.emoticon_id
    )


def rendered_action_target(action: Action, event: Any, state: State) -> Target:
    """渲染动作的 target（模板替换）。对应 Go ``renderedActionTarget``。"""
    return Target(
        platform=render(action.target.platform, event, state),
        scope_id=render(action.target.scope_id, event, state),
        private_user_id=render(action.target.private_user_id, event, state),
        group_id=render(action.target.group_id, event, state),
        superadmins=action.target.superadmins,
    )


async def call_tool(
    module: Any, ctx: Any, event: Any, action: Action, state: State
) -> ActionResult:
    """调用一个已注册工具（不经过 Agent 的风险/确认门）。

    对应 Go ``Module.callTool``：Hook 作者自己为 Hook 触发的调用负责授权与风险。
    """
    if module.opts.tools is None:
        raise ValueError("tool registry is not configured")
    name = (action.tool or "").strip()
    if not name:
        raise ValueError("tool is required")
    arguments = render(action.arguments, event, state)
    registered, ok = module.opts.tools.get(name)
    if not ok or registered is None:
        raise ValueError(f"tool {name!r} not found")
    try:
        tool_result = await registered.call(
            ctx, CallRequest(id="hook:" + name, name=name, arguments=arguments)
        )
    except Exception as exc:  # noqa: BLE001
        module.audit(
            "hook_tool_error",
            "module",
            ModuleHook,
            "tool",
            name,
            "error",
            str(exc),
            "result",
            ResultFailed,
        )
        raise
    content = ""
    if tool_result is not None:
        content = llm.segments_content_text(tool_result.llm_segments())
    module.audit(
        "hook_tool_call",
        "module",
        ModuleHook,
        "tool",
        name,
        "result",
        ResultSucceeded,
    )
    return ActionResult(result=content)


def render(text: str, event: Any, state: State) -> str:
    """模板替换。对应 Go ``render``。"""
    from atri_x.hook.match import template_values

    replacements = template_values(event)
    for index, match in enumerate(event_match_context(event).regex):
        prefix = f"{{{{match.regex.{index}"
        replacements[prefix + ".text}}"] = match.text
        replacements[prefix + ".field}}"] = match.field
        replacements[prefix + ".pattern}}"] = match.value
        for group_index, group in enumerate(match.groups):
            replacements[
                f"{{{{match.regex.{index}.group.{group_index}}}}}"
            ] = group
        for name, group in (match.named or {}).items():
            replacements[f"{{{{match.regex.{index}.{name}}}}}"] = group
    for name, result in state.actions.items():
        replacements["{{actions." + name + ".result}}"] = result.result
        replacements["{{actions." + name + ".error}}"] = result.error
    for old, new_text in replacements.items():
        text = text.replace(old, new_text)
    return text


def first_non_empty(*values: str) -> str:
    """对应 Go ``firstNonEmpty``。"""
    for value in values:
        if value:
            return value
    return ""


def byte_size(size: int) -> str:
    """人类可读的字节数。对应 Go ``byteSize``。"""
    if size % (1024 * 1024) == 0:
        return f"{size // (1024 * 1024)} MiB"
    if size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size} bytes"


def set_text_field(event: Any, field_name: str, value: str) -> Any:
    """整段覆盖某个文本字段。对应 Go ``setTextField``。"""
    field_name = (field_name or "").strip()
    allow_field(event, field_name)
    if field_name in ("message.text", "llm.latest_user_text"):
        event.message.segments = llm.set_segment_text(event.message.segments, value)
    elif field_name == "llm.text":
        event.llm.text = value
    elif field_name == "tool.arguments":
        event.tool.arguments = value
    elif field_name == "tool.result":
        event.tool.result = value
        event.message.segments = llm.set_segment_text(
            tool_result_segments(event), value
        )
    else:
        raise ValueError(f"unsupported set field {field_name!r}")
    return event


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def source_base_dir(action: Action) -> str:
    """对应 Go ``Action.sourceBaseDir``。"""
    return (action.source.base_dir or "").strip()


def source_strict_dir(action: Action) -> str:
    """对应 Go ``Action.sourceStrictDir``。"""
    return (action.source.strict_dir or "").strip()


def has_strict_dir(action: Action) -> bool:
    """对应 Go ``Action.hasStrictDir``。"""
    return bool((action.source.strict_dir or "").strip())


def _compile(pattern: str) -> Any:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc


# 交叉检索：Go 里 action.go 还用到这些类型。
_TOOL_CALL_REQUEST = ToolCallRequest
_RULE = Rule
_JSON = json
