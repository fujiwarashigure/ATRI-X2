"""``@tool:`` / ``@skill:`` 指令补全来源。

对应 Go：``internal/completion/tool.go``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.directive import tool as directive
from atri_x.tool import Actor, DefaultPolicy, Info, Policy, Registry, Tool, can_access_tool
from atri_x.tool.detail import DetailProvider

from atri_x.completion.completion import Item, Request

KIND_TOOL_DIRECTIVE = "tool_directive"
KIND_TOOL_TAG_DIRECTIVE = "tool_tag_directive"
KIND_SKILL_DIRECTIVE = "skill_directive"

KindToolDirective = KIND_TOOL_DIRECTIVE
KindToolTagDirective = KIND_TOOL_TAG_DIRECTIVE
KindSkillDirective = KIND_SKILL_DIRECTIVE

# Go 里的函数类型别名（Python 用 Callable 表达）。
ToolRegistryFunc = Callable[[], Registry | None]
ActorFunc = Callable[[Any], Actor]
PolicyFunc = Callable[[], Policy | None]
ToolTagsFunc = Callable[[Any, Registry, Actor, Policy | None], list[str]]
ToolNamesByTagFunc = Callable[[Any, Registry, str, Callable[[Tool], bool]], list[str]]


@dataclass
class ToolDirectiveSource:
    """补全 ``@tool:`` / ``@t:`` / ``@skill:`` / ``@s:``（含全角冒号）。

    对应 Go ``ToolDirectiveSource``。纯 Skill 类工具（实现了 ``DetailProvider``）
    只在 ``@skill:`` 路径出现，普通工具只在 ``@tool:`` 路径出现。
    """

    registry: ToolRegistryFunc | None = None
    actor: ActorFunc | None = None
    policy: PolicyFunc | None = None
    tags: ToolTagsFunc | None = None
    tool_names_by_tag: ToolNamesByTagFunc | None = None

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``ToolDirectiveSource.Complete``。"""
        registry = self._registry()
        if registry is None:
            return []
        cursor = req.cursor_or_end()
        actor = self.actor(ctx) if self.actor is not None else Actor()
        policy = self.policy() if self.policy is not None else None
        if policy is None:
            policy = DefaultPolicy()

        token = directive.parse_skill_completion_token(req.text, cursor)
        if token.ok:
            return self._complete_skills(registry, actor, policy, token, cursor)

        token = directive.parse_tool_completion_token(req.text, cursor)
        if not token.ok:
            return []
        if token.prefix_only:
            return [
                Item(
                    text=token.prefix,
                    label=token.prefix,
                    kind=KIND_TOOL_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            ]
        tags = self._matching_tags(ctx, registry, actor, policy, token.query)
        infos = self._matching_tools(registry, actor, policy, token.query)
        out: list[Item] = []
        seen_text: set[str] = set()
        for tag in tags:
            text = token.prefix + tag
            seen_text.add(text)
            out.append(
                Item(
                    text=text,
                    label=tag + " <tag>",
                    description=self._tag_description(ctx, registry, actor, policy, tag),
                    kind=KIND_TOOL_TAG_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            )
        for info in infos:
            text = token.prefix + info.name
            if text in seen_text:
                continue
            out.append(
                Item(
                    text=text,
                    label=info.name,
                    description=info.description,
                    kind=KIND_TOOL_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            )
        return out

    # -- 内部 ---------------------------------------------------------------
    def _registry(self) -> Registry | None:
        if self.registry is None:
            return None
        return self.registry()

    def _complete_skills(
        self,
        registry: Registry,
        actor: Actor,
        policy: Policy | None,
        token: directive.ToolCompletionToken,
        cursor: int,
    ) -> list[Item]:
        """对应 Go ``ToolDirectiveSource.completeSkills``。"""
        if token.prefix_only:
            return [
                Item(
                    text=token.prefix,
                    label=token.prefix,
                    kind=KIND_SKILL_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            ]
        infos = match_infos(self._allowed_skills(registry, actor, policy), token.query)
        return [
            Item(
                text=token.prefix + info.name,
                label=info.name,
                description=info.description,
                kind=KIND_SKILL_DIRECTIVE,
                replace_start=token.start,
                replace_end=cursor,
            )
            for info in infos
        ]

    def _matching_tags(
        self, ctx: Any, registry: Registry, actor: Actor, policy: Policy | None, query: str
    ) -> list[str]:
        return match_strings(self._allowed_tags(ctx, registry, actor, policy), query)

    def _matching_tools(
        self, registry: Registry, actor: Actor, policy: Policy | None, query: str
    ) -> list[Info]:
        return match_infos(self._allowed_plain_tools(registry, actor, policy), query)

    def _allowed_plain_tools(
        self, registry: Registry, actor: Actor, policy: Policy | None
    ) -> list[Info]:
        """对应 Go ``ToolDirectiveSource.allowedPlainTools``。"""
        out: list[Info] = []
        for info in registry.list():
            if info.name == "discover_tool" or info.hidden:
                continue
            candidate, ok = registry.get(info.name)
            if not ok or candidate is None:
                continue
            if not is_plain_allowed_tool(candidate, actor, policy):
                continue
            out.append(info)
        return out

    def _allowed_skills(
        self, registry: Registry, actor: Actor, policy: Policy | None
    ) -> list[Info]:
        """对应 Go ``ToolDirectiveSource.allowedSkills``。"""
        out: list[Info] = []
        for info in registry.list():
            candidate, ok = registry.get(info.name)
            if not ok or candidate is None:
                continue
            if not is_allowed_skill(candidate, actor, policy):
                continue
            out.append(info)
        return out

    def _allowed_tags(
        self, ctx: Any, registry: Registry, actor: Actor, policy: Policy | None
    ) -> list[str]:
        """对应 Go ``ToolDirectiveSource.allowedTags``。"""
        if self.tags is not None:
            return self.tags(ctx, registry, actor, policy)
        out: list[str] = []
        for tag in registry.tags():
            names = self._names_by_tag(
                ctx, registry, tag, lambda candidate: is_plain_allowed_tool(candidate, actor, policy)
            )
            if names:
                out.append(tag)
        return out

    def _tag_description(
        self, ctx: Any, registry: Registry, actor: Actor, policy: Policy | None, tag: str
    ) -> str:
        """对应 Go ``ToolDirectiveSource.tagDescription``。"""
        count = len(
            self._names_by_tag(
                ctx, registry, tag, lambda candidate: is_plain_allowed_tool(candidate, actor, policy)
            )
        )
        if count == 1:
            return "1 tool"
        return f"{count} tools"

    def _names_by_tag(
        self,
        ctx: Any,
        registry: Registry,
        tag: str,
        allowed: Callable[[Tool], bool],
    ) -> list[str]:
        if self.tool_names_by_tag is not None:
            return self.tool_names_by_tag(ctx, registry, tag, allowed)
        return registry.names_by_tag(tag, allowed)


def match_infos(candidates: Sequence[Info], query: str) -> list[Info]:
    """对应 Go ``matchInfos``：空前缀匹配优先，命中则不再返回模糊匹配。"""
    if query == "":
        return list(candidates)
    prefix: list[Info] = []
    fuzzy: list[Info] = []
    for info in candidates:
        if info.name.startswith(query):
            prefix.append(info)
            continue
        if fuzzy_match(info.name, query):
            fuzzy.append(info)
    return prefix if prefix else fuzzy


def match_strings(candidates: Sequence[str], query: str) -> list[str]:
    """对应 Go ``matchStrings``。"""
    if query == "":
        return list(candidates)
    prefix: list[str] = []
    fuzzy: list[str] = []
    for candidate in candidates:
        if candidate.startswith(query):
            prefix.append(candidate)
            continue
        if fuzzy_match(candidate, query):
            fuzzy.append(candidate)
    return prefix if prefix else fuzzy


def is_plain_allowed_tool(candidate: Tool, actor: Actor, policy: Policy | None) -> bool:
    """对应 Go ``isPlainAllowedTool``：非 Skill 类、可见且准入通过。"""
    info = candidate.info()
    if info.name == "discover_tool" or info.hidden:
        return False
    if not can_access_tool(actor, policy, info):
        return False
    return not isinstance(candidate, DetailProvider)


def is_allowed_skill(candidate: Tool, actor: Actor, policy: Policy | None) -> bool:
    """对应 Go ``isAllowedSkill``：实现 ``DetailProvider`` 的可见工具。"""
    info = candidate.info()
    if info.hidden or not can_access_tool(actor, policy, info):
        return False
    return isinstance(candidate, DetailProvider)


def fuzzy_match(value: str, query: str) -> bool:
    """对应 Go ``fuzzyMatch``：query 是 value 的子序列（大小写不敏感）。"""
    if query == "":
        return True
    value = value.lower()
    query = query.lower()
    j = 0
    for ch in value:
        if j < len(query) and ch == query[j]:
            j += 1
    return j == len(query)


__all__ = [name for name in dir() if not name.startswith("_")]
