"""``character_*``：角色素材库的列表、读取、检索、管理与删除。

对应 Go：``internal/tool/builtin/character.go``。

五个工具在角色库已配置时才注册。列表 / 读取是纯读，管理 / 删除是 OwnerScoped
的写操作。

与 Go 的差异：

* Go 的 ``image_source`` 走 ``tool.ResolveWorkspacePath``（同步），本仓是 async，
  因此 :meth:`CharacterManageTool.add_image` 是 async；``media.Manager`` 的
  ``Metadata`` / ``Read`` / ``ImportURL`` / ``ImportReader`` 同样是 async。
* Go 的 ``character.ErrNotFound`` 等用 ``==`` 比较（哨兵错误）；本仓哨兵是
  :mod:`atri_x.errors` 的模块级单例，因此用 ``is`` 判定，语义一致。
* Go 的 ``String("description", ...)`` 用 ``*string`` 区分"没传"与"传空串"；
  Python 侧 ``decode_args`` 只在键出现时赋值，故 :class:`CharacterManageArgs`
  的可选字段默认 ``None``，与 Go 的 nil 指针一致。
* Go 里 ``characterPromptMaxRunes`` 是未被引用的常量；这里保留同名常量以便交叉
  检索，并注明它在本仓同样没有被使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.character import (
    Character,
    ErrForbidden,
    ErrInvalidID,
    ErrNotFound,
    Image,
    ImageSettings,
    SearchResult,
    Store,
    Viewer,
    WriteRequest,
)
from atri_x.character.activation import (
    activate_character,
    restore_soul,
    soul_status,
)
from atri_x.character.archive import import_text
from atri_x.delivery import is_http_media_source
from atri_x.errors import AtriXError, InvalidError
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText, ToolSchema
from atri_x.media import ID_PREFIX
from atri_x.media import Input as MediaInput
from atri_x.media import ValidID, read_all
from atri_x.memberprofile.persona import MemberPersona, MemberPersonaStore, PersonaDataError
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.tool.builder import Builder, enum, required
from atri_x.tool.builtin.common import decode_args, first_non_empty_string
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskLow
from atri_x.tool.workspace import PathResolveOptions, resolve_workspace_path

#: 角色工具名。对应 Go ``Character*Name``。
CHARACTER_LIST_NAME = "character_list"
CHARACTER_READ_NAME = "character_read"
CHARACTER_SEARCH_NAME = "character_search"
CHARACTER_MANAGE_NAME = "character_manage"
CHARACTER_DELETE_NAME = "character_delete"
#: Python 侧新增：一步导入粘贴文本、把人设卡设为全局 SOUL。
CHARACTER_IMPORT_NAME = "character_import"
CHARACTER_ACTIVATE_NAME = "character_activate"

#: 单个角色图片的大小上限。对应 Go ``characterMaxImageBytes``。
CHARACTER_MAX_IMAGE_BYTES = 10 << 20

#: Go ``characterPromptMaxRunes``：在 Go 与 Python 侧都未被引用，保留用于交叉检索。
CHARACTER_PROMPT_MAX_RUNES = 12000

#: ``character_list`` 的返回上限。对应 Go ``CharacterListTool.Call`` 的兜底。
CHARACTER_LIST_DEFAULT_LIMIT = 50
CHARACTER_LIST_MAX_LIMIT = 200


@dataclass
class CharacterTools:
    """把角色素材库接进工具注册表。对应 Go ``CharacterTools``。"""

    store: Store | None = None
    media: Any = None
    #: Python 侧新增：``SOUL.md`` 绝对路径（``character_activate`` 用）。
    soul_path: str = ""
    #: Python 侧新增：配置目录（``persona_control`` / 导入后按成员激活用）。
    config_dir: str = ""
    #: Python 侧新增：普通成员是否允许建卡（``[character_library].allow_user_create``）。
    allow_user_create: bool = True

    def enabled(self) -> bool:
        checker = getattr(self.store, "enabled", None)
        return bool(checker()) if callable(checker) else self.store is not None


def new_character_tools(
    store: Store | None,
    center: Any,
    *,
    soul_path: str = "",
    config_dir: str = "",
    allow_user_create: bool = True,
) -> list[Any]:
    """对应 Go ``NewCharacterTools``（外加 Python 侧新增的两个工具）。"""
    tools = CharacterTools(
        store=store,
        media=center,
        soul_path=soul_path,
        config_dir=config_dir,
        allow_user_create=allow_user_create,
    )
    return [
        CharacterListTool(tools),
        CharacterReadTool(tools),
        CharacterSearchTool(tools),
        CharacterManageTool(tools),
        CharacterDeleteTool(tools),
        CharacterImportTool(tools),
        CharacterActivateTool(tools),
    ]


def character_viewer(ctx: Any) -> Viewer:
    """本次调用的身份视图。对应 Go ``characterViewer``。"""
    actor, ok = actor_from_context(ctx)
    if not ok or actor is None:
        return Viewer()
    return Viewer(
        platform=getattr(actor, "platform", "") or "",
        actor_id=getattr(actor, "id", "") or "",
        superadmin=getattr(actor, "role", "") == RoleSuperadmin,
    )


def character_not_found_text(id_value: str) -> str:
    """对应 Go ``characterNotFoundText``。"""
    return f'没有找到角色 "{id_value}"，可先用 character_list 或 character_search 查询。'


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@dataclass
class CharacterListArgs:
    """对应 Go ``characterListArgs``。"""

    tag: str = ""
    query: str = ""
    limit: int = 0


class CharacterListTool:
    """列出当前可见的角色。对应 Go ``CharacterListTool``。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_LIST_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_LIST_NAME)
            .description(
                "列出角色素材库中当前可见的角色（公开角色 + 自己的私有角色）。"
                "返回 id、名称、别名、tags、可见性、拥有者、图片数和简介。"
                "用 @char:<id> 可在消息里临时启用某个角色。"
            )
            .risk(RiskLow)
            .tags("character")
            .depends_on(CHARACTER_READ_NAME, CHARACTER_SEARCH_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_LIST_NAME)
            .description("列出角色素材库中当前可见的角色。")
            .string("tag", "可选，按 tag 过滤。")
            .string("query", "可选，按 id/名称/别名/简介做简单过滤。")
            .integer("limit", "最多返回数量，默认 50，最大 200。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterListArgs, self.name())
        items = self.deps.store.list(ctx, character_viewer(ctx))
        query = (args.query or "").strip().lower()
        tag = (args.tag or "").strip().lower()
        filtered: list[Character] = []
        for item in items or []:
            if tag != "" and not character_has_tag(item, tag):
                continue
            if query != "" and query not in character_search_text(item):
                continue
            filtered.append(item)
        limit = _as_int(args.limit)
        if limit <= 0:
            limit = CHARACTER_LIST_DEFAULT_LIMIT
        if limit > CHARACTER_LIST_MAX_LIMIT:
            limit = CHARACTER_LIST_MAX_LIMIT
        lines = [f"角色：共 {len(filtered)} 个可见。"]
        for index, item in enumerate(filtered):
            if index >= limit:
                lines.append(f"...[已截断，还有 {len(filtered) - limit} 个]")
                break
            lines.append("- " + format_character_summary(item))
        lines.append("提示：发送 @char:<id> 可临时启用该角色；长期启用用 persona_control。")
        return Result(content="\n".join(lines))


def format_character_summary(item: Character) -> str:
    """对应 Go ``formatCharacterSummary``。"""
    parts = [item.id, item.name]
    if item.aliases:
        parts.append("aliases=" + ",".join(item.aliases))
    if item.tags:
        parts.append("tags=" + ",".join(item.tags))
    parts.append("visibility=" + str(item.visibility))
    owner = format_character_owner(item)
    if owner != "":
        parts.append("owner=" + owner)
    if item.images:
        parts.append(f"images={len(item.images)}")
    line = " | ".join(parts)
    if item.description != "":
        line += " | " + item.description
    return line


def format_character_owner(item: Character) -> str:
    """对应 Go ``formatCharacterOwner``。"""
    if item.owner_id == "":
        return "system"
    if item.owner_platform != "" and ":" not in item.owner_id:
        return item.owner_platform + ":" + item.owner_id
    return item.owner_id


def character_has_tag(item: Character, tag: str) -> bool:
    """对应 Go ``characterHasTag``（大小写不敏感）。"""
    for candidate in item.tags or []:
        if (candidate or "").strip().lower() == tag:
            return True
    return False


def character_search_text(item: Character) -> str:
    """对应 Go ``characterSearchText``。"""
    parts = [
        item.id,
        item.name,
        item.description,
        " ".join(item.aliases or []),
        " ".join(item.tags or []),
    ]
    for kind in sorted(item.docs or {}):
        parts.append(str(item.docs[kind] or ""))
    return "\n".join(parts).lower()


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
@dataclass
class CharacterReadArgs:
    """对应 Go ``characterReadArgs``。"""

    id: str = ""
    section: str = ""
    include_images: bool = False


class CharacterReadTool:
    """读取一个角色的设定文本与图片。对应 Go ``CharacterReadTool``。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_READ_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_READ_NAME)
            .description(
                "读取指定角色的设定文本（profile/profile、world、greeting、examples、"
                "notes/<name>），可选返回角色图片。没有明确要求时优先只读 profile。"
            )
            .risk(RiskLow)
            .tags("character")
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_READ_NAME)
            .description("读取指定角色的设定文本和图片。")
            .string("id", "角色 id。", required())
            .string(
                "section",
                "读取范围：all（默认）、profile、world、greeting、examples、"
                "notes 或 notes/<name>。",
            )
            .boolean("include_images", "为 true 时把角色图片作为图片段返回，默认 false。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterReadArgs, self.name())
        try:
            item = self.deps.store.get_visible(ctx, args.id or "", character_viewer(ctx))
        except AtriXError as exc:
            if exc is ErrNotFound:
                return Result(content=character_not_found_text(args.id or ""))
            if exc is ErrForbidden:
                return Result(content=f'角色 "{args.id}" 不可访问。')
            raise
        sections = character_sections(item, args.section or "")
        segments = [
            MessageSegment(
                type=SegmentText, text=render_character_sections(item, sections)
            )
        ]
        if args.include_images:
            if not item.images:
                segments.append(
                    MessageSegment(type=SegmentText, text="（该角色没有图片）")
                )
            for image in item.images:
                if (image.media_id or "").strip() == "":
                    segments.append(
                        MessageSegment(
                            type=SegmentText,
                            text=(
                                f"（图片 {image.name} 未关联 Media Center，"
                                f"仅保存在 {image.path}）"
                            ),
                        )
                    )
                    continue
                segments.append(
                    MessageSegment(
                        type=SegmentImage,
                        media=image.media_id,
                        name=image.name,
                        mime_type=image.mime_type,
                    )
                )
        return Result(segments=segments)


def character_sections(item: Character, section: str) -> list[str]:
    """决定要渲染哪些文档块。对应 Go ``characterSections``。"""
    section = (section or "").strip()
    docs = item.docs or {}
    if section == "" or section.lower() == "all":
        kinds = sorted(docs.keys(), key=character_doc_rank)
        return kinds
    if section.lower() != "notes" and not section.lower().startswith("notes/"):
        if section not in docs:
            raise InvalidError(
                f'角色 "{item.id}" 没有 "{section}" 文档；'
                "可用：all、profile、world、greeting、examples、notes/<name>"
            )
        return [section]
    if section.lower() == "notes":
        kinds = sorted(kind for kind in docs if kind.startswith("notes/"))
        if not kinds:
            raise InvalidError(f'角色 "{item.id}" 没有 notes')
        return kinds
    key = "notes/" + section[len("notes/") :]
    if key not in docs:
        raise InvalidError(f'角色 "{item.id}" 没有 "{key}"')
    return [key]


def character_doc_rank(kind: str) -> int:
    """对应 Go ``characterDocRank``。"""
    return {"profile": 0, "world": 1, "greeting": 2, "examples": 3}.get(kind, 10)


def render_character_sections(item: Character, sections: list[str]) -> str:
    """对应 Go ``renderCharacterSections``。"""
    head = f"角色 {item.name}（{item.id}）"
    if item.aliases:
        head += " 别名：" + "、".join(item.aliases)
    if item.description != "":
        head += " 简介：" + item.description
    if item.tags:
        head += " tags：" + "、".join(item.tags)
    lines = [head]
    for kind in sections:
        content = str((item.docs or {}).get(kind) or "").strip()
        lines.append("\n## " + kind + "\n" + content)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
@dataclass
class CharacterSearchArgs:
    """对应 Go ``characterSearchArgs``。"""

    query: str = ""
    match_mode: str = ""
    tag: str = ""
    limit: int = 0


class CharacterSearchTool:
    """按名称 / 别名 / tags / 正文检索角色。对应 Go ``CharacterSearchTool``。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_SEARCH_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_SEARCH_NAME)
            .description(
                "在角色素材库中按名称、别名、tags 和设定正文做全文检索，"
                "返回命中的角色和片段。"
            )
            .risk(RiskLow)
            .tags("character")
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_SEARCH_NAME)
            .description("检索角色素材库。")
            .string("query", "检索词，多个词用空格或逗号分隔。", required())
            .string("match_mode", "or（默认）或 and。")
            .string("tag", "可选，按 tag 过滤。")
            .integer("limit", "返回数量，默认 5，最大 20。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterSearchArgs, self.name())
        results: list[SearchResult] = self.deps.store.search(
            ctx,
            args.query or "",
            args.match_mode or "",
            args.tag or "",
            _as_int(args.limit),
            character_viewer(ctx),
        )
        if not results:
            return Result(content="没有找到匹配的角色。")
        lines = [f"命中 {len(results)} 个角色："]
        for result in results:
            line = f"- {result.id} | {result.name}"
            if result.tags:
                line += " | tags=" + ",".join(result.tags)
            line += f" | score={result.score}"
            if result.snippet != "":
                line += " | " + result.snippet
            lines.append(line)
        lines.append("用 character_read 读取完整设定，或发送 @char:<id> 临时启用。")
        return Result(content="\n".join(lines))


# ---------------------------------------------------------------------------
# manage
# ---------------------------------------------------------------------------
@dataclass
class CharacterImageArgs:
    """对应 Go ``characterImageArgs``。"""

    preset_prompt: str = ""
    negative_prompt: str = ""
    size: str = ""
    quality: str = ""
    references: list[str] = field(default_factory=list)


@dataclass
class CharacterManageArgs:
    """对应 Go ``characterManageArgs``。

    Go 的 ``description`` / ``version`` / ``source`` 是 ``*string``；这里用 ``None``
    表示"没传"，与 Go 的 nil 指针一致。``image_source_label`` 是 Go 里的
    ``image_source_name`` 字段（JSON 标签如此）。
    """

    operation: str = ""
    id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    visibility: str = ""
    version: str | None = None
    source: str | None = None
    docs: dict[str, str] = field(default_factory=dict)
    remove_docs: list[str] = field(default_factory=list)
    image_source: str = ""
    image_name: str = ""
    image_version: str = ""
    image_source_label: str = ""
    image: CharacterImageArgs | None = None


class CharacterManageTool:
    """创建或修改角色。对应 Go ``CharacterManageTool``。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_MANAGE_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_MANAGE_NAME)
            .description(
                "创建或修改自己的角色（公开角色只有超级管理员能改）。operation 为 "
                "create、update、add_image 或 remove_image；docs 支持 profile、world、"
                "greeting、examples、notes/<name>。普通用户创建的角色默认为私有。"
            )
            .risk(RiskLow)
            .owner_scoped()
            .tags("character")
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_MANAGE_NAME)
            .description("创建或修改角色。")
            .string(
                "operation",
                "create、update、add_image 或 remove_image。",
                required(),
                enum("create", "update", "add_image", "remove_image"),
            )
            .string("id", "角色 id：小写字母/数字/-/_/. ，2-64 位。", required())
            .string("name", "显示名称。")
            .string_array("aliases", "别名列表。")
            .string_array("tags", "分类 tag。")
            .string("description", "一句话简介。")
            .string("visibility", "public 或 private；普通用户只能创建/保留 private。")
            .object(
                "docs",
                "要写入的文档，key 为 profile、world、greeting、examples、image_prompt "
                "或 notes/<name>，value 为正文。传空字符串删除该文档。",
            )
            .string_array("remove_docs", "要删除的文档 key。")
            .object(
                "image",
                "可选，生图预设：preset_prompt、negative_prompt、size、quality、"
                "references（角色图片名列表）。",
            )
            .string("image_source", "add_image 用：工作区相对路径、http(s) URL 或 media:<sha256>。")
            .string("image_name", "add_image/remove_image 用的图片文件名，例如 avatar.png。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterManageArgs, self.name())
        viewer = character_viewer(ctx)
        operation = (args.operation or "").strip().lower()
        if operation == "add_image":
            try:
                image = await self.add_image(ctx, args, viewer)
            except BaseException as exc:  # noqa: BLE001 - Go 把失败翻成一句话
                return Result(content=str(exc))
            return Result(
                content=(
                    f"已写入角色 {args.id} 的图片 {image.name}"
                    f"（{image.size} bytes，media_id="
                    f"{first_non_empty_string([image.media_id, 'none'])}）。"
                )
            )
        if operation == "remove_image":
            if (args.image_name or "").strip() == "":
                return Result(content="remove_image 需要 image_name。")
            try:
                self.deps.store.remove_image(ctx, args.id, args.image_name, viewer)
            except AtriXError as exc:
                return Result(content=character_write_error(args.id, exc))
            return Result(content=f"已删除角色 {args.id} 的图片 {args.image_name}。")
        if operation in ("create", "update"):
            if (
                operation == "create"
                and not viewer.superadmin
                and not self.deps.allow_user_create
                and not _character_exists(self.deps.store, ctx, args.id)
            ):
                return Result(
                    content=(
                        "普通成员建卡功能已关闭（[character_library].allow_user_create=false），"
                        "请联系超级管理员创建角色卡。"
                    )
                )
            write_req = WriteRequest(
                id=args.id,
                name=args.name,
                aliases=list(args.aliases or []),
                description=args.description,
                tags=list(args.tags or []),
                visibility=args.visibility or "",
                version=args.version,
                source=args.source,
                remove_docs=list(args.remove_docs or []),
                docs={},
            )
            image_args = _image_args(args.image)
            if image_args is not None:
                write_req.image = ImageSettings(
                    preset_prompt=image_args.preset_prompt,
                    negative_prompt=image_args.negative_prompt,
                    size=image_args.size,
                    quality=image_args.quality,
                    references=list(image_args.references or []),
                )
            for kind, content in (args.docs or {}).items():
                # Go 里 docs 的值是 *string：出现即写入，空串表示删除。
                write_req.docs[kind] = content
            try:
                item = self.deps.store.write(ctx, write_req, viewer)
            except AtriXError as exc:
                return Result(content=character_write_error(args.id, exc))
            return Result(
                content=(
                    f"角色已保存：{item.name}（可见性 {item.visibility}，"
                    f"文档 {len(item.docs or {})} 个，图片 {len(item.images or [])} 张）。"
                    f"可用 @char:{item.id} 临时启用。"
                )
            )
        return Result(content="operation 必须是 create、update、add_image 或 remove_image。")

    async def add_image(
        self, ctx: Any, args: CharacterManageArgs, viewer: Viewer
    ) -> Image:
        """导入一张图片并写进角色。对应 Go ``CharacterManageTool.addImage``。"""
        if self.deps.media is None:
            raise InvalidError("Media Center 未配置，无法保存图片")
        source = (args.image_source or "").strip()
        if source == "":
            raise InvalidError("add_image 需要 image_source")
        name = (args.image_name or "").strip()
        mime_type = ""
        data = b""
        media_id = ""
        if source.startswith(ID_PREFIX):
            if not ValidID(source):
                raise InvalidError("media id 不合法")
            item = await self.deps.media.metadata(ctx, source)
            media_id = source
            mime_type = getattr(item, "mime_type", "") or ""
            if name == "":
                name = getattr(item, "name", "") or ""
            content, _meta = await self.deps.media.read(ctx, source)
            data = content
        elif is_http_media_source(source):
            imported = await self.deps.media.import_url(
                ctx, source, MediaInput(name=name)
            )
            media_id = imported.id
            mime_type = getattr(imported, "mime_type", "") or ""
            if name == "":
                name = getattr(imported, "name", "") or ""
            content, _meta = await self.deps.media.read(ctx, imported.id)
            data = content
        else:
            resolved = await resolve_workspace_path(ctx, source, PathResolveOptions())
            path = resolved.path
            if not os.path.exists(path):
                raise InvalidError(f"读取图片失败：找不到文件 {path}")
            if os.path.isdir(path):
                raise InvalidError("image_source 不能是目录")
            size = os.path.getsize(path)
            if size > CHARACTER_MAX_IMAGE_BYTES:
                raise InvalidError(f"图片超过 {CHARACTER_MAX_IMAGE_BYTES} bytes 上限")
            with open(path, "rb") as handle:
                data = read_all(handle)
            if name == "":
                name = os.path.basename(path)
            imported = await self.deps.media.import_reader(
                ctx, data, len(data), MediaInput(name=name)
            )
            media_id = imported.id
            mime_type = getattr(imported, "mime_type", "") or ""
        if len(data) == 0:
            raise InvalidError("图片内容为空")
        if len(data) > CHARACTER_MAX_IMAGE_BYTES:
            raise InvalidError(f"图片超过 {CHARACTER_MAX_IMAGE_BYTES} bytes 上限")
        if name == "":
            name = "image"
        return self.deps.store.add_image_with_meta(
            ctx,
            args.id,
            name,
            mime_type,
            media_id,
            args.image_version,
            args.image_source_label,
            data,
            viewer,
        )


def _image_args(value: Any) -> CharacterImageArgs | None:
    """把 ``image`` 参数收成 :class:`CharacterImageArgs`（dict 或已构造对象）。"""
    if value is None:
        return None
    if isinstance(value, CharacterImageArgs):
        return value
    if isinstance(value, dict):
        return CharacterImageArgs(
            preset_prompt=value.get("preset_prompt") or "",
            negative_prompt=value.get("negative_prompt") or "",
            size=value.get("size") or "",
            quality=value.get("quality") or "",
            references=list(value.get("references") or []),
        )
    return None


def character_write_error(id_value: str, exc: BaseException) -> str:
    """对应 Go ``characterWriteError``。"""
    if exc is ErrNotFound:
        return character_not_found_text(id_value)
    if exc is ErrForbidden:
        return f'角色 "{id_value}" 不属于你，只有拥有者或超级管理员可以修改。'
    if exc is ErrInvalidID:
        return "角色 id 不合法：只允许小写字母、数字、-、_、.，且以字母或数字开头。"
    return str(exc)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
@dataclass
class CharacterDeleteArgs:
    """对应 Go ``characterDeleteArgs``。"""

    id: str = ""


class CharacterDeleteTool:
    """永久删除角色。对应 Go ``CharacterDeleteTool``。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_DELETE_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_DELETE_NAME)
            .description(
                "永久删除自己的角色（含文本和图片）。公开角色或不属于自己的角色"
                "需要超级管理员。"
            )
            .risk(RiskHigh)
            .owner_scoped()
            .tags("character")
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_DELETE_NAME)
            .description("永久删除角色。")
            .string("id", "要删除的角色 id。", required())
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterDeleteArgs, self.name())
        try:
            self.deps.store.delete(ctx, args.id, character_viewer(ctx))
        except AtriXError as exc:
            return Result(content=character_write_error(args.id, exc))
        return Result(content=f"已删除角色 {args.id}。")


# ---------------------------------------------------------------------------
# import（Python 侧新增）：粘贴一段人设文本，一步建卡 / 启用
# ---------------------------------------------------------------------------
@dataclass
class CharacterImportArgs:
    """``character_import`` 参数（Python 侧新增，Go 无对应）。"""

    text: str = ""
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    visibility: str = ""
    #: "none" / "soul"（全局系统人设，需超管）/ "member"（绑定当前成员）。
    activate: str = ""
    docs: dict[str, str] = field(default_factory=dict)


class CharacterImportTool:
    """把用户粘贴的人设文本直接建卡，可顺带启用。

    1.1.3 的真实故障：用户贴长文说"加入人设卡并启用"，模型没有直接入口，改去翻
    ``app.toml`` / ``SOUL.md``，10 轮工具上限烧完也没有建卡。这个工具就是为了让
    这一类请求**一次调用**完成：建卡（``profile.md`` 写正文），再按 ``activate``
    决定是否设为全局 SOUL 或绑定当前成员。
    """

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_IMPORT_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_IMPORT_NAME)
            .description(
                "一步导入：把一段人设文本保存成角色卡（profile.md），可选同时启用。"
                "activate=none 只建卡；activate=member 绑定为当前成员的专属人设；"
                "activate=soul 设为全局系统人设 SOUL.md（仅超级管理员）。"
            )
            .risk(RiskLow)
            .owner_scoped()
            .tags("character")
            .depends_on(CHARACTER_MANAGE_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_IMPORT_NAME)
            .description("把粘贴的人设文本一步保存成角色卡，可选同时启用。")
            .string("text", "人设正文（会写进 profile.md）。", required())
            .string("id", "可选：角色 id（小写字母/数字/-/_/.）；留空按名称自动生成。")
            .string("name", "可选：角色名；留空取正文第一个标题。")
            .string("description", "可选：一句话简介。")
            .string_array("tags", "可选：分类 tag。")
            .string("visibility", "public 或 private；普通用户恒为 private。")
            .string(
                "activate",
                "可选：none（默认，只建卡）、member（绑定为当前成员的专属人设）、"
                "soul（设为全局系统人设，仅超级管理员）。",
                enum("none", "member", "soul"),
            )
            .object(
                "docs",
                "可选：额外文档，key 为 world / greeting / examples / notes/<name>。",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        args = decode_args(req.arguments, CharacterImportArgs, self.name())
        viewer = character_viewer(ctx)
        if not viewer.superadmin and not self.deps.allow_user_create:
            return Result(
                content=(
                    "普通成员建卡功能已关闭（[character_library].allow_user_create=false）。"
                    "可以先把正文发给超级管理员，或让他打开该开关。"
                )
            )
        try:
            result = import_text(
                self.deps.store,
                ctx,
                args.text,
                viewer,
                character_id=args.id,
                name=args.name,
                description=args.description,
                tags=list(args.tags or []),
                docs=dict(args.docs or {}),
                visibility=args.visibility,
            )
        except AtriXError as exc:
            return Result(content=f"导入失败：{exc}")
        item = result.character
        lines = [
            f"角色卡已{'创建' if result.created else '更新'}：{item.name}（id={item.id}，"
            f"可见性 {item.visibility}，文档 {len(item.docs or {})} 个）。"
        ]
        lines.extend(result.warnings or [])
        lines.extend(self._activate(ctx, viewer, item, args.activate or "none"))
        lines.append(
            "说明：@char:<id> 只对当前消息生效；长期启用请用 persona_control 或 "
            "character_activate。"
        )
        return Result(content="\n".join(lines))

    def _activate(
        self, ctx: Any, viewer: Viewer, item: Character, activate: str
    ) -> list[str]:
        mode = (activate or "none").strip().lower()
        if mode in ("", "none"):
            if not viewer.superadmin:
                return []
            return ["如需设为全局人设：character_activate(operation=\"activate\", id=...)。"]
        if mode == "soul":
            if not viewer.superadmin:
                return ["activate=soul 需要超级管理员权限，本次只建卡、未改 SOUL.md。"]
            if not (self.deps.soul_path or "").strip():
                return ["SOUL.md 路径未配置（[soul].path），未激活。"]
            try:
                result = activate_character(
                    self.deps.store, ctx, item.id, self.deps.soul_path, viewer
                )
            except (AtriXError, OSError, ValueError) as exc:
                return [f"设为全局人设失败：{exc}"]
            lines = [
                f"已把角色 {result.character.name}（{result.character.id}）设为全局系统人设"
                f"（SOUL.md），下一轮对话生效。"
            ]
            if result.backup_path:
                lines.append(f"旧 SOUL.md 备份：{result.backup_path}")
            return lines
        if mode == "member":
            platform, user_id = _current_member(ctx)
            if not platform or not user_id:
                return ["activate=member 失败：取不到当前用户身份。"]
            if not (self.deps.config_dir or "").strip():
                return ["activate=member 失败：配置目录未知。"]
            try:
                store = MemberPersonaStore(self.deps.config_dir)
                store.upsert(
                    MemberPersona(
                        platform=platform,
                        user_id=user_id,
                        character_id=item.id,
                        enabled=True,
                    )
                )
            except (PersonaDataError, OSError) as exc:
                return [f"绑定专属人设失败：{exc}"]
            return [
                f"已把 {item.name}（{item.id}）绑定为 {user_id} 的专属人设，"
                "之后与这个成员的对话都会按该角色进行；关闭用 "
                "persona_control(operation=\"disable\")。"
            ]
        return ["activate 只能是 none、member 或 soul，本次只建卡。"]


# ---------------------------------------------------------------------------
# activate（Python 侧新增）：把人设卡设为全局系统人设 SOUL.md
# ---------------------------------------------------------------------------
@dataclass
class CharacterActivateArgs:
    """``character_activate`` 参数（Python 侧新增，Go 无对应）。"""

    operation: str = ""
    id: str = ""


class CharacterActivateTool:
    """全局系统人设（SOUL.md）的激活 / 回滚 / 状态。仅超级管理员。"""

    def __init__(self, deps: CharacterTools | None = None) -> None:
        self.deps = deps if deps is not None else CharacterTools()

    def name(self) -> str:
        return CHARACTER_ACTIVATE_NAME

    def info_value(self) -> Info:
        return (
            Builder(CHARACTER_ACTIVATE_NAME)
            .description(
                "把某张人设卡设为**全局**系统人设（改写 SOUL.md，对所有没有专属人设的"
                "用户生效），或回滚到上一次备份。仅超级管理员可用；改之前会自动备份。"
            )
            .risk(RiskHigh)
            .tags("character")
            .depends_on(CHARACTER_MANAGE_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(CHARACTER_ACTIVATE_NAME)
            .description("管理全局系统人设 SOUL.md（激活 / 回滚 / 状态）。")
            .string(
                "operation",
                "activate（把角色卡写成 SOUL.md）、restore（回滚到上次备份）、status（查看状态）。",
                required(),
                enum("activate", "restore", "status"),
            )
            .string("id", "activate 用：角色卡 id。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return Result(content="角色素材库未配置。")
        viewer = character_viewer(ctx)
        if not viewer.superadmin:
            return Result(content="只有超级管理员可以修改全局系统人设（SOUL.md）。")
        soul_path = (self.deps.soul_path or "").strip()
        if not soul_path:
            return Result(content="SOUL.md 路径未配置（app.toml 的 [soul].path）。")
        args = decode_args(req.arguments, CharacterActivateArgs, self.name())
        operation = (args.operation or "").strip().lower()
        if operation == "activate":
            character_id = (args.id or "").strip()
            if not character_id:
                return Result(content="activate 需要 id：先用 character_list 查角色 id。")
            try:
                result = activate_character(
                    self.deps.store, ctx, character_id, soul_path, viewer
                )
            except AtriXError as exc:
                return Result(content=character_write_error(character_id, exc))
            except (OSError, ValueError) as exc:
                return Result(content=f"写入 SOUL.md 失败：{exc}")
            return Result(
                content=(
                    f"已把 {result.character.name}（{result.character.id}）设为全局系统人设"
                    f"（SOUL.md）。\n旧内容备份：{result.backup_path}\n"
                    "下一轮对话生效；回滚用 character_activate(operation=\"restore\")。"
                )
            )
        if operation == "restore":
            try:
                restored = restore_soul(soul_path)
            except OSError as exc:
                return Result(content=f"回滚失败：{exc}")
            if not restored:
                return Result(content="没有可回滚的 SOUL.md 备份（SOUL.md.bak）。")
            return Result(content="已把 SOUL.md 回滚到上一次备份，下一轮对话生效。")
        if operation == "status":
            status = soul_status(soul_path)
            backup = "有" if status.backup_exists else "无"
            return Result(
                content=(
                    f"SOUL.md：{status.soul_path}\n"
                    f"存在：{'是' if status.exists else '否'}，大小 {status.size} bytes\n"
                    f"备份：{backup}（{status.backup_size} bytes）"
                )
            )
        return Result(content="operation 必须是 activate、restore 或 status。")


def _current_member(ctx: Any) -> tuple[str, str]:
    """当前发言人 ``(平台, 平台用户 id)``；取不到返回 ``("", "")``。"""
    actor, ok = actor_from_context(ctx)
    if not ok or actor is None:
        return "", ""
    platform = (getattr(actor, "platform", "") or "").strip().lower()
    user_id = (getattr(actor, "platform_user_id", "") or "").strip()
    if not user_id:
        raw = str(getattr(actor, "id", "") or "").strip()
        if ":" in raw:
            prefix, rest = raw.split(":", 1)
            platform = platform or prefix
            user_id = rest
    return platform, user_id


def _character_exists(store: Store, ctx: Any, character_id: str) -> bool:
    """角色 id 是否已存在（用来区分"新建"与"改自己的卡"）。"""
    try:
        store.get(ctx, character_id)
    except AtriXError:
        return False
    return True


def _as_int(value: Any) -> int:
    """近似 Go 的 ``json.Unmarshal`` 到 ``int``。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


__all__ = [name for name in dir() if not name.startswith("_")]
