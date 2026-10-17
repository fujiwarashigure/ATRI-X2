"""单回合指令：``@model:`` / ``@image:`` / ``@use:`` 的解析、裁决与剥离。

对应 Go：``internal/agent/turn_override.go``。

这些声明**只对本回合有效**，会从用户消息里剥掉且永不落库。前缀与关键字来自
``config.TurnDirectivesConfig``（``.normalized()``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.delivery import ctx_value, ctx_with_value
from atri_x.llm import ToolSchema
from atri_x.security import RoleSuperadmin
from atri_x.tool import can_access_tool, info_available_in_context

from atri_x.agent.session_metadata import sorted_unique

__all__ = [
    "TURN_OVERRIDE_METHODS",
    "TurnDirectiveMatch",
    "TurnDirectiveResult",
    "TurnModelOverride",
    "TurnOverrideMixin",
    "TurnToolProfile",
    "apply_turn_overrides",
    "is_turn_name_char",
    "notify_turn_override_result",
    "parse_turn_directives",
    "read_turn_name",
    "resolve_turn_alias",
    "strip_turn_directives",
    "turn_model_override",
    "turn_separator_len",
    "turn_tool_profile",
    "turn_tool_schemas",
    "with_turn_model_override",
    "with_turn_tool_profile",
]

#: Go 的 ``turnModelOverrideKey{}`` / ``turnToolProfileKey{}``。
TurnModelOverride = object()
TurnToolProfile = object()


def with_turn_model_override(ctx: Any, selection: Any) -> Any:
    """对应 Go ``withTurnModelOverride``。"""
    return ctx_with_value(ctx, TurnModelOverride, selection)


def turn_model_override(ctx: Any) -> tuple[Any, bool]:
    """对应 Go ``turnModelOverride``。"""
    value = ctx_value(ctx, TurnModelOverride)
    if value is None:
        return None, False
    return value, True


def with_turn_tool_profile(ctx: Any, name: str) -> Any:
    """对应 Go ``withTurnToolProfile``。"""
    return ctx_with_value(ctx, TurnToolProfile, name)


def turn_tool_profile(ctx: Any) -> str:
    """对应 Go ``turnToolProfile``。"""
    value = ctx_value(ctx, TurnToolProfile)
    return value.strip() if isinstance(value, str) else ""


@dataclass
class TurnDirectiveResult:
    """一次单回合声明的处理结果。对应 Go ``turnDirectiveResult``。"""

    text: str = ""
    applied: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


@dataclass
class TurnDirectiveMatch:
    """一条命中的单回合声明。对应 Go ``turnDirectiveMatch``。

    ``start`` / ``end`` 是 **rune 下标**（Python 字符串下标天然按码点）。
    """

    kind: str = ""
    name: str = ""
    start: int = 0
    end: int = 0


def apply_turn_overrides(agent: Any, ctx: Any, text: str) -> tuple[Any, TurnDirectiveResult]:
    """解析并应用本回合的模型/生图/工具声明。对应 Go ``Agent.applyTurnOverrides``。"""
    result = TurnDirectiveResult(text=text)
    if agent is None or not text:
        return ctx, result
    config = agent.turn_directives
    normalized = config.normalized() if hasattr(config, "normalized") else config
    matches = parse_turn_directives(text, normalized)
    if not matches:
        return ctx, result
    current = agent.actor(ctx)
    if current.role != RoleSuperadmin:
        for match in matches:
            result.denied.append(match.kind + ":" + match.name)
        result.text = strip_turn_directives(text, matches, None)
        return ctx, result

    remove = [False] * len(matches)
    for index, match in enumerate(matches):
        if match.kind == "model":
            name = resolve_turn_alias(agent.model_aliases, match.name)
            selection = agent.model_profiles.get(name)
            clients = getattr(getattr(agent, "model_runtime", None), "clients", {}) or {}
            if selection is None or clients.get(selection.provider) is None:
                result.invalid.append("model:" + match.name)
                continue
            ctx = with_turn_model_override(ctx, selection)
            result.applied.append("model:" + name)
            remove[index] = True
        elif match.kind == "image":
            name = resolve_turn_alias(agent.image_aliases, match.name)
            if not agent.image_profiles.get(name):
                result.invalid.append("image:" + match.name)
                continue
            try:
                from atri_x.imagegen import with_profile as image_with_profile
            except ImportError:  # pragma: no cover - 生图模块尚未落地
                result.invalid.append("image:" + match.name)
                continue
            ctx = image_with_profile(ctx, name)
            result.applied.append("image:" + name)
            remove[index] = True
        elif match.kind == "tool":
            name = resolve_turn_alias(agent.tool_aliases, match.name)
            if not agent.tool_profiles.get(name):
                result.invalid.append("use:" + match.name)
                continue
            ctx = with_turn_tool_profile(ctx, name)
            result.applied.append("use:" + name)
            remove[index] = True
    result.text = strip_turn_directives(text, matches, remove)
    return ctx, result


async def notify_turn_override_result(agent: Any, ctx: Any, result: TurnDirectiveResult) -> None:
    """把单回合声明的处理结果告诉用户。对应 Go ``Agent.notifyTurnOverrideResult``。"""
    parts: list[str] = []
    if result.applied:
        parts.append(
            "本轮已启用："
            + ", ".join(sorted_unique(result.applied))
            + "（仅本轮有效，下条消息需重新声明）"
        )
    if result.invalid:
        parts.append("未配置的 profile：" + ", ".join(sorted_unique(result.invalid)))
    if result.denied:
        parts.append("仅超级管理员可以声明：" + ", ".join(sorted_unique(result.denied)))
    if not parts:
        return
    await agent.send_chat(ctx, "\n".join(parts))


def turn_tool_schemas(agent: Any, ctx: Any, name: str) -> list[ToolSchema]:
    """把 ``@use:`` profile 解析成本回合的工具 schema。

    对应 Go ``Agent.turnToolSchemas``。
    """
    names = agent.tool_profiles.get(name)
    registry = getattr(getattr(agent, "tool_runtime", None), "registry", None)
    if not names or registry is None:
        return []
    current = agent.actor(ctx)
    policy = agent.security_policy
    out: list[ToolSchema] = []
    for tool_name in names:
        entry, ok = registry.get(tool_name)
        if not ok:
            continue
        info = entry.info()
        if not info_available_in_context(ctx, info) or not can_access_tool(
            current, policy, info
        ):
            continue
        out.append(entry.schema())
    return out


@dataclass
class _TurnKeyword:
    text: str = ""
    kind: str = ""


def parse_turn_directives(text: str, config: Any) -> list[TurnDirectiveMatch]:
    """扫描 ``<prefix><keyword><separator><name>``。对应 Go ``parseTurnDirectives``。"""
    prefixes = [p.strip() for p in (config.prefixes or []) if p and p.strip()]
    if not prefixes:
        return []
    prefixes.sort(key=len, reverse=True)

    keywords: list[_TurnKeyword] = []
    for keyword in config.model_keywords or []:
        keywords.append(_TurnKeyword(text=(keyword or "").strip(), kind="model"))
    for keyword in config.image_keywords or []:
        keywords.append(_TurnKeyword(text=(keyword or "").strip(), kind="image"))
    for keyword in config.tool_keywords or []:
        keywords.append(_TurnKeyword(text=(keyword or "").strip(), kind="tool"))
    keywords = [keyword for keyword in keywords if keyword.text]
    keywords.sort(key=lambda item: len(item.text), reverse=True)

    out: list[TurnDirectiveMatch] = []
    index = 0
    while index < len(text):
        matched = False
        for prefix in prefixes:
            if not text.startswith(prefix, index):
                continue
            rest = text[index + len(prefix) :]
            lower_rest = rest.lower()
            for keyword in keywords:
                if not lower_rest.startswith(keyword.text.lower()):
                    continue
                after = rest[len(keyword.text) :]
                separator = turn_separator_len(after)
                if separator == 0:
                    continue
                name, name_len = read_turn_name(after[separator:])
                if name_len == 0:
                    continue
                end = index + len(prefix) + len(keyword.text) + separator + name_len
                out.append(
                    TurnDirectiveMatch(kind=keyword.kind, name=name, start=index, end=end)
                )
                index = end
                matched = True
                break
            if matched:
                break
        if matched:
            continue
        index += 1
    return out


def turn_separator_len(text: str) -> int:
    """声明名前的分隔符长度（``:`` 或 ``：``）。对应 Go ``turnSeparatorLen``。"""
    if not text:
        return 0
    return 1 if text[0] in (":", "：") else 0


def read_turn_name(text: str) -> tuple[str, int]:
    """读声明名。对应 Go ``readTurnName``。"""
    end = 0
    while end < len(text) and is_turn_name_char(text[end]):
        end += 1
    if end == 0:
        return "", 0
    return text[:end], end


def is_turn_name_char(ch: str) -> bool:
    """声明名允许的字符。对应 Go ``isTurnNameRune``（字母 / 数字 / ``_`` / ``-`` / ``.``）。"""
    if ch in ("_", "-", "."):
        return True
    return ch.isalpha() or ch.isdecimal()


def strip_turn_directives(
    text: str, matches: list[TurnDirectiveMatch], remove: list[bool] | None
) -> str:
    """剥掉命中的声明并折叠空白。对应 Go ``stripTurnDirectives``。

    Go 的 rune 下标在 Python 里就是字符串下标，因此这里可以直接切片。
    """
    out: list[str] = []
    last = 0
    for index, match in enumerate(matches):
        drop = True
        if remove is not None:
            if index >= len(remove):
                break
            drop = remove[index]
        if not drop:
            continue
        out.append(text[last : match.start])
        last = match.end
    out.append(text[last:])
    return " ".join("".join(out).split())


def resolve_turn_alias(aliases: dict[str, str] | None, raw: str) -> str:
    """声明名 → profile 名（先查别名表）。对应 Go ``resolveTurnAlias``。"""
    trimmed = (raw or "").strip()
    if not trimmed:
        return ""
    mapping = aliases or {}
    return mapping.get(trimmed.lower(), trimmed)


# ---------------------------------------------------------------------------
# mixin
# ---------------------------------------------------------------------------
class TurnOverrideMixin:
    """``@model:`` / ``@image:`` / ``@use:`` 的 Agent 方法组。

    Go 里这三个方法都在 ``*Agent`` 上（``turn_override.go:54/109/127``），
    ``input.go:174/176`` 按**方法**调用；Python 侧必须挂到 :class:`Agent`
    上，否则每条聊天消息都会在 ``input.py:277`` 抛 ``AttributeError``。
    """

    def apply_turn_overrides(
        self, ctx: Any, text: str
    ) -> tuple[Any, TurnDirectiveResult]:
        """对应 Go ``Agent.applyTurnOverrides``（返回新的 ctx 与结果）。"""
        return apply_turn_overrides(self, ctx, text)

    def turn_tool_schemas(self, ctx: Any, name: str) -> list[ToolSchema]:
        """对应 Go ``Agent.turnToolSchemas``。"""
        return turn_tool_schemas(self, ctx, name)

    async def notify_turn_override_result(
        self, ctx: Any, result: TurnDirectiveResult
    ) -> None:
        """对应 Go ``Agent.notifyTurnOverrideResult``。"""
        await notify_turn_override_result(self, ctx, result)


#: 可以装到 Agent 实例上的方法（与 ``ToolDirectiveMixin`` 同形，供装配期核对）。
TURN_OVERRIDE_METHODS: tuple[str, ...] = (
    "apply_turn_overrides",
    "notify_turn_override_result",
    "turn_tool_schemas",
)
