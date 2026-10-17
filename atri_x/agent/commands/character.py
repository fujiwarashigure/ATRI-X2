"""``/chars``：列出角色素材库，或重建角色索引。

对应 Go：``internal/agent/commands/character.go``（103 行）。
"""

from __future__ import annotations

from typing import Any

from atri_x.character import Character, Viewer
from atri_x.command import Info, Request, Result
from atri_x.security import RoleSuperadmin, RoleUser, actor_from_context
from atri_x.agent.commands.deps import Deps, Registrar, register_factories

__all__ = [
    "CharacterModule",
    "character_matches_query",
    "character_viewer_from_actor",
    "new_characters",
]

_MAX_LISTED_CHARACTERS = 50


def character_viewer_from_actor(ctx: Any) -> Viewer:
    """由 actor 构造角色可见性视图。对应 Go ``characterViewerFromActor``。"""
    actor, _ = actor_from_context(ctx)
    return Viewer(
        platform=actor.platform,
        actor_id=actor.id,
        superadmin=actor.role == RoleSuperadmin,
    )


def character_matches_query(item: Character, query: str) -> bool:
    """角色是否命中查询串（大小写不敏感子串）。对应 Go ``characterMatchesQuery``。"""
    fields = [
        item.id,
        item.name,
        item.description,
        " ".join(item.aliases or []),
        " ".join(item.tags or []),
    ]
    return any(query in (field or "").lower() for field in fields)


class CharactersCommand:
    """对应 Go ``charactersCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="chars",
            usage="/chars [query|reload]",
            description=(
                "List the character library. Use @char:<id> in a message to apply a "
                "character for one turn."
            ),
            min_role=RoleUser,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        store = self._deps.characters
        if store is None or not store.enabled():
            return Result(content="角色素材库未配置。")
        arg = (req.args or "").strip()
        if arg == "reload":
            actor, _ = actor_from_context(ctx)
            if actor.role != RoleSuperadmin:
                return Result(content="只有超级管理员可以重建角色索引。")
            store.refresh(ctx)
            return Result(content="角色索引已重建。")
        items = store.list(ctx, character_viewer_from_actor(ctx))
        query = arg.lower()
        filtered = [
            item
            for item in items
            if query == "" or character_matches_query(item, query)
        ]
        if not filtered:
            return Result(
                content=(
                    "没有可用角色。把角色目录放到 "
                    "data/config/atri-x/characters/<id>/ 后发送 "
                    + req.prefix
                    + "chars reload 刷新。"
                )
            )
        lines = [f"角色：共 {len(filtered)} 个可见。"]
        for index, item in enumerate(filtered):
            if index >= _MAX_LISTED_CHARACTERS:
                lines.append(f"...[已截断，还有 {len(filtered) - _MAX_LISTED_CHARACTERS} 个]")
                break
            line = "- " + item.id + " | " + item.name
            if item.aliases:
                line += " | aliases=" + ",".join(item.aliases)
            if item.tags:
                line += " | tags=" + ",".join(item.tags)
            line += " | " + str(item.visibility)
            if item.description != "":
                line += " | " + item.description
            lines.append(line)
        lines.append("发送 @char:<id> 可在本轮启用某个角色。")
        return Result(content="\n".join(lines))


def new_characters(deps: Deps) -> Any:
    """对应 Go ``NewCharacters``。"""
    return CharactersCommand(deps)


class CharacterModule:
    """对应 Go ``CharacterModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_characters)
