"""Hook 条件匹配、字段取值与模板值。

对应 Go：``internal/hook/match.go``。

上游的不对称之处（**刻意保留**，不要"顺手修好"）：
``field_value`` 支持 ``actor.nickname`` 与 ``actor.group_card``，但 ``KNOWN_FIELDS``
里**没有**它们，所以配置里写这两个字段会在 ``validate()`` 阶段被拒。
本移植照抄这一行为，免得两边对同一份 ``hooks.toml`` 给出不同结论。
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from atri_x.hook.event import Event
from atri_x.llm.segment import segments_content_text, segments_text_only


@dataclass
class RegexMatch:
    """一次正则条件的捕获结果。对应 Go ``RegexMatch``。

    注意：本类有一个名为 ``field`` 的属性（对齐 Go 的 ``Field``），它会在类体里
    遮蔽 ``dataclasses.field``，所以下面必须用 ``dataclasses.field`` 全名限定。
    """

    field: str = ""
    value: str = ""
    text: str = ""
    groups: list[str] = dataclasses.field(default_factory=list)
    named: dict[str, str] | None = None
    start: int = 0
    end: int = 0


@dataclass
class MatchContext:
    """对应 Go ``MatchContext``。"""

    regex: list[RegexMatch] = field(default_factory=list)


def event_match_context(event: Event) -> MatchContext:
    """从 ``event.metadata["match"]`` 取匹配上下文。对应 Go ``EventMatchContext``。"""
    if not event.metadata:
        return MatchContext()
    value = event.metadata.get("match")
    if isinstance(value, MatchContext):
        return value
    return MatchContext()


# -- 匹配操作符（对齐 Go 的 const 块） --------------------------------------
MATCH_ALWAYS = "always"
MATCH_EXISTS = "exists"
MATCH_CONTAINS = "contains"
MATCH_FULL = "fullmatch"
MATCH_PREFIX = "startswith"
MATCH_SUFFIX = "endswith"
MATCH_REGEX = "regex"

MatchAlways = MATCH_ALWAYS
MatchExists = MATCH_EXISTS
MatchContains = MATCH_CONTAINS
MatchFull = MATCH_FULL
MatchPrefix = MATCH_PREFIX
MatchSuffix = MATCH_SUFFIX
MatchRegex = MATCH_REGEX

KNOWN_OPS = (
    MATCH_ALWAYS,
    MATCH_EXISTS,
    MATCH_CONTAINS,
    MATCH_FULL,
    MATCH_PREFIX,
    MATCH_SUFFIX,
    MATCH_REGEX,
)

#: 需要 value 的操作符
OPS_NEEDING_VALUE = (MATCH_CONTAINS, MATCH_FULL, MATCH_PREFIX, MATCH_SUFFIX, MATCH_REGEX)

#: 可用字段。对应 Go ``KnownField``。
KNOWN_FIELDS = (
    "platform.name",
    "platform.scope_id",
    "platform.user_id",
    "platform.conversation_id",
    "platform.message_id",
    "platform.reply_to_message_id",
    "message.id",
    "message.text",
    "message.display_text",
    "message.platform_text",
    "message.intent_text",
    "message.role",
    "message.reply.message_id",
    "message.reply.sender_id",
    "message.reply.text",
    "message.reply.display_text",
    "llm.text",
    "llm.source_text",
    "llm.latest_user_text",
    "llm.latest_user_display_text",
    "llm.provider",
    "llm.model",
    "tool.name",
    "tool.arguments",
    "tool.result",
    "tool.risk",
    "actor.id",
    "actor.user_id",
    "actor.role",
    "actor.group_role",
    "actor.display_name",
    "session.id",
    "session.mode",
    "session.title",
    "session.status",
    "request.id",
    "request.kind",
    "request.session_id",
    "request.phase",
    "error.message",
)

TEMPLATE_FIELDS = (
    "platform.name",
    "platform.scope_id",
    "platform.user_id",
    "platform.conversation_id",
    "platform.message_id",
    "platform.reply_to_message_id",
    "actor.id",
    "actor.user_id",
    "actor.role",
    "actor.group_role",
    "actor.display_name",
    "session.id",
    "session.mode",
    "session.title",
    "session.status",
    "request.id",
    "request.kind",
    "request.session_id",
    "request.phase",
    "message.id",
    "message.text",
    "message.display_text",
    "message.platform_text",
    "message.intent_text",
    "message.role",
    "message.reply.message_id",
    "message.reply.sender_id",
    "message.reply.text",
    "message.reply.display_text",
    "llm.text",
    "llm.source_text",
    "llm.latest_user_text",
    "llm.latest_user_display_text",
    "llm.provider",
    "llm.model",
    "tool.name",
    "tool.arguments",
    "tool.result",
    "tool.risk",
    "error.message",
)


@dataclass
class Condition:
    """一条匹配条件。对应 Go ``Condition``（TOML 字段 ``field``/``op``/``value``）。"""

    field: str = ""
    op: str = ""
    value: str = ""


@dataclass
class Match:
    """条件的显式 **AND** 列表。对应 Go ``Match``。

    注意：Go 侧 ``Match`` 是**不是空**就代表有条件；``[]`` 空列表会被
    ``validate()`` 拒绝，而不是当成"总是匹配"——要用 ``always()``。
    """

    conditions: list[Condition] = field(default_factory=list)

    def validate(self) -> None:
        """对应 Go ``Match.Validate``；不合法抛 :class:`ValueError`。"""
        if not self.conditions:
            raise ValueError("hook match requires at least one condition")
        for i, cond in enumerate(self.conditions):
            cond.validate(index=i + 1)

    def matches(self, event: Event) -> bool:
        return self.match_event(event).ok

    def match_event(self, event: Event) -> "MatchResult":
        ctx = MatchContext()
        for cond in self.conditions:
            ok, capture = cond.match(event)
            if not ok:
                return MatchResult(ok=False, context=MatchContext())
            if capture is not None:
                ctx.regex.append(capture)
        return MatchResult(ok=True, context=ctx)


@dataclass
class MatchResult:
    """对应 Go ``MatchResult``。"""

    ok: bool = False
    context: MatchContext = field(default_factory=MatchContext)


# -- 构造器（对齐 Go 的构造函数） -------------------------------------------
def always() -> Match:
    return Match(conditions=[Condition(op=MATCH_ALWAYS)])


def contains(field_name: str, value: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_CONTAINS, value=value)])


def full_match(field_name: str, value: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_FULL, value=value)])


def starts_with(field_name: str, value: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_PREFIX, value=value)])


def ends_with(field_name: str, value: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_SUFFIX, value=value)])


def regex(field_name: str, value: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_REGEX, value=value)])


def exists(field_name: str) -> Match:
    return Match(conditions=[Condition(field=field_name, op=MATCH_EXISTS)])


def known_match_op(op: str) -> bool:
    """对应 Go ``knownMatchOp``。"""
    return (op or "").strip() in KNOWN_OPS


def needs_match_value(op: str) -> bool:
    """对应 Go ``needsMatchValue``。"""
    return (op or "").strip() in OPS_NEEDING_VALUE


def known_field(field_name: str) -> bool:
    """对应 Go ``KnownField``。"""
    return (field_name or "").strip() in KNOWN_FIELDS


# -- 条件判定 ----------------------------------------------------------------
def _validate_condition(cond: Condition, index: int = 0) -> None:
    op = (cond.op or "").strip()
    prefix = f"condition {index}: " if index else ""
    if not op:
        raise ValueError(prefix + "op is required")
    if not known_match_op(op):
        raise ValueError(prefix + f'unsupported op "{op}"')
    if op == MATCH_ALWAYS:
        if (cond.field or "").strip() or (cond.value or "").strip():
            raise ValueError(prefix + "always cannot set field or value")
        return
    if not known_field(cond.field):
        raise ValueError(prefix + f'unsupported field "{cond.field}"')
    if needs_match_value(op) and cond.value == "":
        raise ValueError(prefix + f"value is required for {op}")
    if op == MATCH_REGEX:
        try:
            _compile(cond.value)
        except re.error as exc:
            raise ValueError(prefix + f"invalid regex: {exc}") from exc


# 把方法挂到 dataclass 上（Go 是方法，这里保持同名以便交叉检索）
Condition.validate = _validate_condition  # type: ignore[attr-defined]


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    """编译并缓存正则。

    Go 侧每次 ``match`` 都重新 ``regexp.Compile``；这里加缓存只是省开销，
    语义等价（编译失败一律视为不匹配）。

    注意：Go 用 RE2，不支持后向引用与 lookbehind；Python 的 ``re`` 支持，
    因此**本实现接受的模式集是上游的超集**。``hooks.toml`` 里用到的模式
    两边都能编译，实际行为一致。
    """
    return re.compile(pattern)


def _condition_match(cond: Condition, event: Event) -> tuple[bool, RegexMatch | None]:
    field_value = field_value_of(event, cond.field)
    op = (cond.op or "").strip()
    if op == MATCH_ALWAYS:
        return True, None
    if op == MATCH_EXISTS:
        return field_value != "", None
    if op == MATCH_CONTAINS:
        return cond.value in field_value, None
    if op == MATCH_FULL:
        return field_value == cond.value, None
    if op == MATCH_PREFIX:
        return field_value.startswith(cond.value), None
    if op == MATCH_SUFFIX:
        return field_value.endswith(cond.value), None
    if op == MATCH_REGEX:
        capture = _regex_match_context(cond.field, cond.value, field_value)
        if capture is None:
            return False, None
        return True, capture
    return False, None


def _condition_matches(cond: Condition, event: Event) -> bool:
    ok, _ = _condition_match(cond, event)
    return ok


Condition.match = _condition_match  # type: ignore[attr-defined]
Condition.matches = _condition_matches  # type: ignore[attr-defined]


def _regex_match_context(field_name: str, pattern: str, value: str) -> RegexMatch | None:
    """对应 Go ``regexMatchContext``。"""
    try:
        compiled = _compile(pattern)
    except re.error:
        return None
    m = compiled.search(value)
    if m is None:
        return None

    groups: list[str] = []
    named: dict[str, str] = {}
    # groups[0] 是整段匹配，随后是各捕获组；未参与的组在 Go 里是空串。
    for i in range(compiled.groups + 1):
        try:
            g = m.group(i)
        except IndexError:
            g = None
        text = "" if g is None else g
        groups.append(text)
        if i == 0:
            continue

    index_to_name = {idx: name for name, idx in compiled.groupindex.items()}
    for i in range(1, len(groups)):
        name = index_to_name.get(i)
        if name and name.strip():
            named[name] = groups[i]

    return RegexMatch(
        field=field_name,
        value=pattern,
        text=groups[0],
        groups=groups,
        named=named or None,
        start=m.start(0),
        end=m.end(0),
    )


def field_value_of(event: Event, field_name: str) -> str:
    """取事件上某个字段的字符串值。对应 Go ``FieldValue``。"""
    name = (field_name or "").strip()
    p = event.platform
    msg = event.message
    if name == "platform.name":
        return p.name
    if name == "platform.scope_id":
        return p.scope_id
    if name == "platform.user_id":
        return p.user_id
    if name == "platform.conversation_id":
        return p.conversation_id
    if name == "platform.message_id":
        return p.message_id
    if name == "platform.reply_to_message_id":
        return p.reply_to_message_id
    if name == "message.id":
        return msg.id
    if name == "message.text":
        return segments_text_only(msg.segments)
    if name == "message.display_text":
        return segments_content_text(msg.segments)
    if name == "message.platform_text":
        return msg.platform_text
    if name == "message.intent_text":
        return event.message_intent_text()
    if name == "message.role":
        return msg.role
    if name == "message.reply.message_id":
        return msg.reply.message_id if msg.reply else ""
    if name == "message.reply.sender_id":
        return msg.reply.sender_id if msg.reply else ""
    if name == "message.reply.text":
        return msg.reply.text if msg.reply else ""
    if name == "message.reply.display_text":
        return msg.reply.display_text if msg.reply else ""
    if name == "llm.text":
        return event.llm.text
    if name == "llm.source_text":
        return event.llm.source_text
    if name == "llm.latest_user_text":
        return segments_text_only(msg.segments)
    if name == "llm.latest_user_display_text":
        return segments_content_text(msg.segments)
    if name == "llm.provider":
        return event.llm.provider
    if name == "llm.model":
        return event.llm.model
    if name == "tool.name":
        return event.tool.name
    if name == "tool.arguments":
        return event.tool.arguments
    if name == "tool.result":
        return event.tool.result
    if name == "tool.risk":
        return event.tool.risk
    if name == "actor.id":
        return event.actor.id
    if name == "actor.user_id":
        return event.actor.user_id
    if name == "actor.role":
        return event.actor.role
    if name == "actor.group_role":
        return event.actor.group_role
    if name == "actor.display_name":
        return event.actor.display_name
    # 上游不对称：这两个字段能取值，但不在 KnownField 里，条件校验会先拒绝。
    if name == "actor.nickname":
        return event.actor.nickname
    if name == "actor.group_card":
        return event.actor.group_card
    if name == "session.id":
        return event.session.id
    if name == "session.mode":
        return event.session.mode
    if name == "session.title":
        return event.session.title
    if name == "session.status":
        return event.session.status
    if name == "request.id":
        return event.request.id
    if name == "request.kind":
        return event.request.kind
    if name == "request.session_id":
        return event.request.session_id
    if name == "request.phase":
        return event.request.phase
    if name == "error.message":
        return event.error_message()
    return ""


# Go 的函数名是 FieldValue / TemplateValues，这里同名再导出一次。
FieldValue = field_value_of


def template_values(event: Event) -> dict[str, str]:
    """对应 Go ``TemplateValues``：产出 ``{{field}}`` → 值 的替换表。"""
    return {
        "{{" + name + "}}": field_value_of(event, name) for name in TEMPLATE_FIELDS
    }


TemplateValues = template_values


# -- BlockPolicy -------------------------------------------------------------
@dataclass
class BlockPolicy:
    """在一个插件贡献的全部注册被评估**之前**跳过它们。

    对应 Go ``BlockPolicy``。
    """

    platforms: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    users: set[str] = field(default_factory=set)

    def blocks(self, event: Event) -> bool:
        """对应 Go ``BlockPolicy.Blocks``。"""
        platform = (event.platform.name or "").strip()
        if not platform:
            return False
        if platform in self.platforms:
            return True
        group_id = _event_group_id(event.platform.scope_id)
        if group_id is not None and f"{platform}:{group_id}" in self.groups:
            return True
        user_id = (event.actor.user_id or "").strip()
        if not user_id:
            trimmed = (event.actor.id or "").strip()
            prefix = platform + ":"
            user_id = trimmed[len(prefix):] if trimmed.startswith(prefix) else trimmed
        return bool(user_id) and f"{platform}:{user_id}" in self.users


def new_block_policy(
    blocked_platforms: list[str] | None = None,
    blocked_groups: list[str] | None = None,
    blocked_users: list[str] | None = None,
) -> BlockPolicy:
    """对应 Go ``NewBlockPolicy``；不合法抛 :class:`ValueError`。"""
    policy = BlockPolicy()
    for value in blocked_platforms or []:
        value = (value or "").strip()
        if not value:
            raise ValueError("blocked_platform contains an empty platform")
        policy.platforms.add(value)
    for name, values, target in (
        ("blocked_group", blocked_groups or [], policy.groups),
        ("blocked_id", blocked_users or [], policy.users),
    ):
        for value in values:
            platform, ident, ok = _split_blocked_id(value)
            if not ok:
                raise ValueError(f'{name} entry "{value}" must use <platform>:<id>')
            target.add(f"{platform}:{ident}")
    return policy


def _split_blocked_id(value: str) -> tuple[str, str, bool]:
    """对应 Go ``splitBlockedID``（在第一个 ``:`` 处切开）。"""
    trimmed = (value or "").strip()
    platform, sep, ident = trimmed.partition(":")
    platform = platform.strip()
    ident = ident.strip()
    return platform, ident, bool(sep) and platform != "" and ident != ""


def _event_group_id(scope_id: str) -> str | None:
    """对应 Go ``eventGroupID``：``group:<id>`` / ``supergroup:<id>`` → ``<id>``。"""
    trimmed = (scope_id or "").strip()
    kind, sep, ident = trimmed.partition(":")
    ident = ident.strip()
    if not sep or not ident or kind not in ("group", "supergroup"):
        return None
    return ident


__all__ = [name for name in dir() if not name.startswith("_")]
