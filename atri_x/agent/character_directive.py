"""``@char:`` / ``@c:`` 单回合角色声明的解析与应用。

对应 Go：``internal/agent/character_directive.go``。

命中的角色提示块会被**前置**到当前用户消息里，只对本回合有效，永不写入会话状态。
角色库的唯一出处是 :mod:`atri_x.character`，指令匹配的唯一出处是
:mod:`atri_x.directive`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.directive import (
    CHARACTER_FULL_PREFIX,
    CHARACTER_PREFIX,
    CHARACTER_SHORT_FULL,
    CHARACTER_SHORT_PREFIX,
    character_matches,
    strip_tool_matches,
)
from atri_x.security import RoleSuperadmin

from atri_x.agent.session_metadata import sorted_unique

__all__ = [
    "CHARACTER_DIRECTIVE_METHODS",
    "CHARACTER_DIRECTIVE_PROMPT_RUNES",
    "CharacterDirectiveMixin",
    "CharacterDirectiveResult",
    "apply_character_directives",
    "character_viewer",
    "notify_character_directive_result",
]

#: 角色提示块的最大 rune 数。对应 Go ``characterDirectivePromptRunes``。
CHARACTER_DIRECTIVE_PROMPT_RUNES = 12000


@dataclass
class CharacterDirectiveResult:
    """一次角色声明的处理结果。对应 Go ``characterDirectiveResult``。"""

    text: str = ""
    applied: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


def apply_character_directives(agent: Any, ctx: Any, text: str) -> CharacterDirectiveResult:
    """把 ``@char:<id>`` 解析成单回合人设块。对应 Go ``Agent.applyCharacterDirectives``。"""
    result = CharacterDirectiveResult(text=text)
    characters = getattr(agent, "characters", None)
    if characters is None or not characters.enabled():
        return result
    needles = (
        CHARACTER_PREFIX,
        CHARACTER_FULL_PREFIX,
        CHARACTER_SHORT_PREFIX,
        CHARACTER_SHORT_FULL,
    )
    if not any(needle in text for needle in needles):
        return result
    matches = character_matches(text)
    if not matches:
        return result
    viewer = character_viewer(agent, ctx)
    remove = [False] * len(matches)
    blocks: list[str] = []
    for index, match in enumerate(matches):
        ident = (match.name or "").strip()
        try:
            item = characters.get_visible(ctx, ident, viewer)
        except Exception:  # noqa: BLE001 - 找不到/不可见都算 invalid
            result.invalid.append(ident)
            continue
        blocks.append(item.prompt(CHARACTER_DIRECTIVE_PROMPT_RUNES))
        result.applied.append(item.id)
        remove[index] = True
    if not result.applied:
        return result
    stripped = strip_tool_matches(text, matches, remove).strip()
    block = "\n\n".join(blocks)
    result.text = block if not stripped else block + "\n\n" + stripped
    return result


def character_viewer(agent: Any, ctx: Any) -> Any:
    """角色库的可见性视角。对应 Go ``Agent.characterViewer``。"""
    from atri_x.character import Viewer

    current = agent.actor(ctx)
    return Viewer(
        platform=current.platform,
        actor_id=current.id,
        superadmin=current.role == RoleSuperadmin,
    )


async def notify_character_directive_result(
    agent: Any, ctx: Any, result: CharacterDirectiveResult
) -> None:
    """对应 Go ``Agent.notifyCharacterDirectiveResult``。"""
    parts: list[str] = []
    if result.applied:
        parts.append("已启用角色：" + ", ".join(sorted_unique(result.applied)))
    if result.invalid:
        parts.append("未找到或不可用的角色：" + ", ".join(sorted_unique(result.invalid)))
    if not parts:
        return
    await agent.send_chat(ctx, "\n".join(parts))


# ---------------------------------------------------------------------------
# mixin
# ---------------------------------------------------------------------------
class CharacterDirectiveMixin:
    """``@char:`` 指令的 Agent 方法组。

    Go 里这三个方法都在 ``*Agent`` 上（``character_directive.go:22/61/66``），
    ``input.go:165-176`` 按**方法**调用；Python 侧必须挂到 :class:`Agent`
    上，否则每条聊天消息都会在 ``input.py:267`` 抛 ``AttributeError``。
    """

    def apply_character_directives(self, ctx: Any, text: str) -> CharacterDirectiveResult:
        """对应 Go ``Agent.applyCharacterDirectives``。"""
        return apply_character_directives(self, ctx, text)

    def character_viewer(self, ctx: Any) -> Any:
        """对应 Go ``Agent.characterViewer``。"""
        return character_viewer(self, ctx)

    async def notify_character_directive_result(
        self, ctx: Any, result: CharacterDirectiveResult
    ) -> None:
        """对应 Go ``Agent.notifyCharacterDirectiveResult``。"""
        await notify_character_directive_result(self, ctx, result)


#: 可以装到 Agent 实例上的方法（与 ``ToolDirectiveMixin`` 同形，供装配期核对）。
CHARACTER_DIRECTIVE_METHODS: tuple[str, ...] = (
    "apply_character_directives",
    "character_viewer",
    "notify_character_directive_result",
)
