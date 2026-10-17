"""``@char:`` / ``@c:`` 角色指令补全来源。

对应 Go：``internal/completion/character.go``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atri_x.directive import tool as directive

from atri_x.completion.completion import Item, Request

KIND_CHARACTER_DIRECTIVE = "character_directive"
KindCharacterDirective = KIND_CHARACTER_DIRECTIVE


@dataclass
class CharacterOption:
    """``@char:`` 补全里的一个可选角色。对应 Go ``CharacterOption``。"""

    id: str = ""
    name: str = ""
    description: str = ""


# CharacterLister 列出可选角色（Go ``func(ctx) []CharacterOption``）。
CharacterLister = Callable[[Any], list[CharacterOption]]


@dataclass
class CharacterDirectiveSource:
    """补全 ``@char:<id>`` / ``@c:<id>``。对应 Go ``CharacterDirectiveSource``。"""

    characters: CharacterLister | None = None

    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``CharacterDirectiveSource.Complete``。"""
        if self.characters is None:
            return []
        cursor = req.cursor_or_end()
        token = directive.parse_character_completion_token(req.text, cursor)
        if not token.ok:
            return []
        if token.prefix_only:
            return [
                Item(
                    text=token.prefix,
                    label=token.prefix,
                    kind=KIND_CHARACTER_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            ]
        query = token.query.strip().lower()
        out: list[Item] = []
        for option in self.characters(ctx):
            if query != "" and query not in option.id.lower() and query not in option.name.lower():
                continue
            description = option.name
            if option.description:
                description = option.name + " · " + option.description
            out.append(
                Item(
                    text=token.prefix + option.id,
                    label=option.id,
                    description=description,
                    kind=KIND_CHARACTER_DIRECTIVE,
                    replace_start=token.start,
                    replace_end=cursor,
                )
            )
        return out


__all__ = [name for name in dir() if not name.startswith("_")]
