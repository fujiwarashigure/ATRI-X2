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
from atri_x.delivery import is_http_media_source
from atri_x.errors import AtriXError, InvalidError
from atri_x.llm.llm import MessageSegment, SegmentImage, SegmentText, ToolSchema
from atri_x.media import ID_PREFIX
from atri_x.media import Input as MediaInput
from atri_x.media import ValidID, read_all
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

    def enabled(self) -> bool:
        checker = getattr(self.store, "enabled", None)
        return bool(checker()) if callable(checker) else self.store is not None


def new_character_tools(store: Store | None, center: Any) -> list[Any]:
    """对应 Go ``NewCharacterTools``。"""
    tools = CharacterTools(store=store, media=center)
    return [
        CharacterListTool(tools),
        CharacterReadTool(tools),
        CharacterSearchTool(tools),
        CharacterManageTool(tools),
        CharacterDeleteTool(tools),
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
        lines.append("提示：发送 @char:<id> 可临时启用该角色。")
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
