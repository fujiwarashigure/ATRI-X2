"""``artist_library_*``：画师串组合的检索、读取、保存与删除。

对应 Go：``internal/tool/builtin/artist_library.go``。

四个工具只在画师串库已配置时才注册：没有库的时候它们每个都只能失败，注册出去
只会白占模型的工具位。

与 Go 的差异：

* Go 的 ``artistMatchJSON`` / ``artistSlotJSON`` / ``artistPresetJSON`` 用
  ``encoding/json`` 的结构体标签序列化；Python 侧用同名 dataclass + ``to_json()``
  （逐字段复刻 ``omitempty`` 语义），统一交给
  :func:`atri_x.tool.builtin.common.go_json` 输出紧凑 JSON。
* Go 的 ``ArtistTools.artistTagMap`` 是方法；这里改成模块函数
  :func:`artist_tag_map`，避免为了一个只在本文件用到的助手去改共享依赖类型。
* Go 的 ``%s`` 打 ``Scope``/``PresetStatus``/``SourceKind`` 得到原始字符串值，
  Python 侧这些是 ``StrEnum``；``str()`` 给出的仍是同样的值，故文案不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from atri_x.artist import (
    MAX_ID_READ_RUNES,
    MAX_SEARCH_LIMIT,
    Artist,
    Dialect,
    Match,
    Preset,
    PresetArtist,
    PresetSource,
    PresetStatus,
    Render,
    Scope,
    SourceKind,
    format_weight,
    new_artist_id,
    normalize_dialect,
    normalize_scope,
    normalize_source,
    normalize_status,
)
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import enum, required
from atri_x.tool.builtin.artist_tools import (
    ARTIST_LIBRARY_DELETE_NAME,
    ARTIST_LIBRARY_READ_NAME,
    ARTIST_LIBRARY_SAVE_NAME,
    ARTIST_LIBRARY_SEARCH_NAME,
    ArtistTools,
    artist_builder,
    artist_error_message,
    artist_library_disabled_result,
    artist_viewer,
)
from atri_x.tool.builtin.common import append_warnings, decode_args, go_json
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskLow


def new_artist_library_tools(deps: ArtistTools) -> list[Any]:
    """对应 Go ``NewArtistLibraryTools``：四个读写串库的工具。"""
    return [
        ArtistLibrarySearchTool(deps),
        ArtistLibraryReadTool(deps),
        ArtistLibrarySaveTool(deps),
        ArtistLibraryDeleteTool(deps),
    ]


# ---------------------------------------------------------------------------
# JSON 形状
# ---------------------------------------------------------------------------
@dataclass
class ArtistMatchJSON:
    """一条检索命中。对应 Go ``artistMatchJSON``。"""

    kind: str = ""
    id: str = ""
    title: str = ""
    reason: str = ""
    scope: str = ""
    status: str = ""
    score: int = 0
    artists: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "reason": self.reason,
            "scope": self.scope,
            "status": self.status,
            "score": self.score,
        }
        # Go 的 omitempty：nil 与空切片都不出现。
        if self.artists:
            out["artists"] = list(self.artists)
        if self.style_tags:
            out["style_tags"] = list(self.style_tags)
        return out


def artist_matches_json(matches: Sequence[Match]) -> list[ArtistMatchJSON]:
    """对应 Go ``artistMatchesJSON``。"""
    return [
        ArtistMatchJSON(
            kind=match.kind,
            id=match.id,
            title=match.title,
            reason=match.detail,
            scope=str(match.scope),
            status=str(match.status),
            score=match.score,
            artists=list(match.artists),
            style_tags=list(match.style_tags),
        )
        for match in matches
    ]


@dataclass
class ArtistSlotJSON:
    """组合里的一个画师槽位。对应 Go ``artistSlotJSON``。"""

    artist_id: str = ""
    tag: str = ""
    weight: float = 0.0

    def to_json(self) -> dict[str, Any]:
        # 三个字段都没有 omitempty。
        return {"artist_id": self.artist_id, "tag": self.tag, "weight": self.weight}


@dataclass
class ArtistPresetJSON:
    """组合的完整存储形状。对应 Go ``artistPresetJSON``。"""

    id: str = ""
    name: str = ""
    scope: str = ""
    dialect: str = ""
    status: str = ""
    revision: int = 0
    compatible_models: list[str] = field(default_factory=list)
    artists: list[ArtistSlotJSON] = field(default_factory=list)
    raw_string: str = ""
    positive_tags: list[str] = field(default_factory=list)
    negative_tags: list[str] = field(default_factory=list)
    style_description: str = ""
    source_type: str = ""
    source_media: str = ""
    source_note: str = ""
    rendered_positive: str = ""
    rendered_negative: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "scope": self.scope,
            "dialect": self.dialect,
            "status": self.status,
            "revision": self.revision,
            "source_type": self.source_type,
        }
        for key, value in (
            ("compatible_models", self.compatible_models),
            ("artists", [slot.to_json() for slot in self.artists]),
            ("raw_string", self.raw_string),
            ("positive_tags", self.positive_tags),
            ("negative_tags", self.negative_tags),
            ("style_description", self.style_description),
            ("source_media", self.source_media),
            ("source_note", self.source_note),
            ("rendered_positive", self.rendered_positive),
            ("rendered_negative", self.rendered_negative),
            ("warnings", self.warnings),
        ):
            if value:
                out[key] = value
        return out


def artist_preset_json_of(
    preset: Preset, tags: dict[str, str], render: Render
) -> ArtistPresetJSON:
    """渲染一个已存组合，并把画师 id 解析成标签。对应 Go ``artistPresetJSONOf``。"""
    out = ArtistPresetJSON(
        id=preset.id,
        name=preset.name,
        scope=str(preset.scope),
        dialect=str(preset.dialect),
        status=str(preset.status),
        revision=preset.revision,
        compatible_models=list(preset.compatible_models),
        raw_string=preset.raw_string,
        positive_tags=list(preset.positive_tags),
        negative_tags=list(preset.negative_tags),
        style_description=preset.style_description,
        source_type=str(preset.source.type),
        source_media=preset.source.media_id,
        source_note=preset.source.note,
        rendered_positive=render.positive,
        rendered_negative=render.negative,
        warnings=list(render.warnings),
    )
    for slot in preset.artists:
        out.artists.append(
            ArtistSlotJSON(
                artist_id=slot.artist_id,
                tag=tags.get(slot.artist_id, ""),
                weight=slot.weight,
            )
        )
    if render.artist_raw:
        out.raw_string = render.artist_raw
    return out


def artist_tag_map(tools: ArtistTools) -> dict[str, str]:
    """把画师目录按 id 索引成 ``id -> canonical_tag``。对应 Go ``artistTagMap``。"""
    tags: dict[str, str] = {}
    if not tools.enabled():
        return tags
    service = tools.service
    try:
        items = service.visible_artists(_empty_viewer())
    except Exception:  # noqa: BLE001 - Go 里同样只吞掉错误并返回空表
        return tags
    for item in items or []:
        tags[item.id] = item.canonical_tag
    return tags


def _empty_viewer() -> Any:
    """Go 的 ``artist.Viewer{}`` 零值视图。"""
    from atri_x.artist import Viewer

    return Viewer()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
@dataclass
class ArtistLibrarySearchArgs:
    """对应 Go ``artistLibrarySearchArgs``。"""

    query: str = ""
    kind: str = ""
    limit: int = 0


class ArtistLibrarySearchTool:
    """搜索当前可见的画师标签与组合。对应 Go ``ArtistLibrarySearchTool``。"""

    def __init__(self, deps: ArtistTools | None = None) -> None:
        self.deps = deps if deps is not None else ArtistTools()

    def name(self) -> str:
        return ARTIST_LIBRARY_SEARCH_NAME

    def info_value(self) -> Info:
        return (
            artist_builder(
                ARTIST_LIBRARY_SEARCH_NAME,
                "搜索当前可见的画师标签与画师串组合，按名称、别名、风格标签和适用模型匹配，"
                "返回 id、名称、作用域、状态和匹配理由。拿到 id 后用 artist_library_read "
                "读取完整内容，或在 image_generate 里用 artist_preset_id 直接引用。",
            )
            .risk(RiskLow)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            artist_builder(ARTIST_LIBRARY_SEARCH_NAME, "搜索画师串库。")
            .string(
                "query",
                "检索词：画师名、别名、画风关键词或适用模型；留空则列出全部可见条目。",
            )
            .string(
                "kind",
                "只搜一类：artist（画师标签）或 preset（画师串组合）；不填两类都搜。",
                enum("artist", "preset"),
            )
            .integer("limit", "返回条数，默认 10，最多 50。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return artist_library_disabled_result()
        args = decode_args(req.arguments, ArtistLibrarySearchArgs, self.name())
        kind = (args.kind or "").strip().lower()
        limit = _as_int(args.limit)
        if kind != "":
            # 过滤发生在这里，所以先要最宽的一页再裁剪；否则带上 kind 时
            # 会静默地比请求的条数更少。
            limit = MAX_SEARCH_LIMIT
        try:
            matches = self.deps.service.search(artist_viewer(ctx), args.query or "", limit)
        except Exception as exc:  # noqa: BLE001 - Go 把 err 翻成文案后正常返回
            return Result(content=artist_error_message(exc))
        filtered = [match for match in matches if kind == "" or match.kind == kind]
        if _as_int(args.limit) > 0 and len(filtered) > _as_int(args.limit):
            filtered = filtered[: _as_int(args.limit)]
        result = Result(data=go_json({"matches": artist_matches_json(filtered)}))
        if not filtered:
            if (args.query or "").strip() == "":
                result.content = (
                    "画师串库里还没有可见的条目。可以用 artist_library_save 新建一个画师串组合。"
                )
            else:
                result.content = (
                    "没有匹配的画师或组合。可以换关键词，或用 artist_library_save "
                    "把这次的结果存成新组合。"
                )
            return result
        lines = [f"找到 {len(filtered)} 条："]
        for match in filtered:
            lines.append(f"\n- [{match.kind}] {match.id}（{match.scope.label()} / {match.status}）")
            if (match.detail or "").strip() != "":
                lines.append(f"\n  标题：{match.title}\n  理由：{match.detail}")
            else:
                lines.append(f"\n  标题：{match.title}")
            if match.artists:
                lines.append("\n  画师：" + "、".join(match.artists))
            if match.style_tags:
                lines.append("\n  风格：" + "、".join(match.style_tags))
        result.content = "".join(lines)
        return result


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
@dataclass
class ArtistLibraryReadArgs:
    """对应 Go ``artistLibraryReadArgs``。"""

    id: str = ""
    dialect: str = ""


class ArtistLibraryReadTool:
    """按方言渲染并读取一个组合。对应 Go ``ArtistLibraryReadTool``。"""

    def __init__(self, deps: ArtistTools | None = None) -> None:
        self.deps = deps if deps is not None else ArtistTools()

    def name(self) -> str:
        return ARTIST_LIBRARY_READ_NAME

    def info_value(self) -> Info:
        return (
            artist_builder(
                ARTIST_LIBRARY_READ_NAME,
                "按 id 读取一个画师串组合的完整内容（画师、权重、原文、正负标签、适用模型、"
                "revision），并按指定方言给出渲染后的正向/负向提示词。改组合前先读一次，"
                "保存时要带上这里的 revision。",
            )
            .risk(RiskLow)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            artist_builder(ARTIST_LIBRARY_READ_NAME, "读取一个画师串组合。")
            .string("id", "组合 id，来自 artist_library_search 的结果。", required())
            .string(
                "dialect",
                "渲染方言：natural（自然语言）、sd_weighted（SD 加权标签）、nai（NovelAI）、"
                "tags（纯标签）；不填按组合自己的方言。",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return artist_library_disabled_result()
        args = decode_args(req.arguments, ArtistLibraryReadArgs, self.name())
        id_value = (args.id or "").strip()
        if id_value == "":
            return Result(content="id 不能为空。")
        if len(id_value) > MAX_ID_READ_RUNES:
            return Result(content="id 过长。")
        viewer = artist_viewer(ctx)
        try:
            preset = self.deps.service.preset(viewer, id_value)
        except Exception as exc:  # noqa: BLE001
            return Result(content=artist_error_message(exc))
        dialect = normalize_dialect(args.dialect or "")
        if dialect == Dialect.AUTO:
            dialect = preset.dialect
            if dialect == Dialect.AUTO:
                dialect = Dialect.SD_WEIGHTED
        try:
            compiled = self.deps.service.compile(viewer, preset.id, "", dialect)
        except Exception as exc:  # noqa: BLE001
            return Result(content=artist_error_message(exc))
        render = Render(
            dialect=dialect,
            positive=compiled.positive,
            negative=compiled.negative,
            warnings=list(compiled.warnings),
        )
        payload = artist_preset_json_of(preset, artist_tag_map(self.deps), render)
        result = Result(data=go_json(payload))
        lines = [
            f"组合：{preset.name}（{preset.id}）\n作用域：{preset.scope.label()}\n"
            f"状态：{preset.status}\nrevision：{preset.revision}\n方言：{dialect.label()}"
        ]
        if preset.raw_string != "":
            lines.append(f"\n画师串原文：{preset.raw_string}")
        for slot in payload.artists:
            tag = slot.tag or slot.artist_id
            lines.append(
                f"\n画师：{tag}（{slot.artist_id}）权重 {format_weight(slot.weight)}"
            )
        if compiled.positive != "":
            lines.append(f"\n渲染正向：{compiled.positive}")
        if compiled.negative != "":
            lines.append(f"\n渲染负向：{compiled.negative}")
        if preset.positive_tags:
            lines.append("\n正向标签：" + ", ".join(preset.positive_tags))
        if preset.negative_tags:
            lines.append("\n负向标签：" + ", ".join(preset.negative_tags))
        if preset.style_description != "":
            lines.append(f"\n风格描述：{preset.style_description}")
        if preset.compatible_models:
            lines.append("\n适用模型：" + ", ".join(preset.compatible_models))
        result.content = "".join(lines)
        append_warnings(result, *compiled.warnings)
        return result


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------
@dataclass
class ArtistSlotArgs:
    """对应 Go ``artistSlotArgs``。"""

    artist_id: str = ""
    tag: str = ""
    aliases: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    weight: float = 0.0


@dataclass
class ArtistLibrarySaveArgs:
    """对应 Go ``artistLibrarySaveArgs``。"""

    id: str = ""
    name: str = ""
    scope: str = ""
    expected_revision: int = 0
    dialect: str = ""
    status: str = ""
    raw_string: str = ""
    artists: list[Any] = field(default_factory=list)
    positive_tags: list[str] = field(default_factory=list)
    negative_tags: list[str] = field(default_factory=list)
    style_description: str = ""
    compatible_models: list[str] = field(default_factory=list)
    source_type: str = ""
    source_media: str = ""
    note: str = ""


class ArtistLibrarySaveTool:
    """新建或更新一个组合。对应 Go ``ArtistLibrarySaveTool``。"""

    def __init__(self, deps: ArtistTools | None = None) -> None:
        self.deps = deps if deps is not None else ArtistTools()

    def name(self) -> str:
        return ARTIST_LIBRARY_SAVE_NAME

    def info_value(self) -> Info:
        return (
            artist_builder(
                ARTIST_LIBRARY_SAVE_NAME,
                "保存或更新一个画师串组合，保留画师顺序和权重。scope=user 只写自己的个人库，"
                "scope=group 写入当前群，scope=global 仅超级管理员可用；作用域身份由服务端决定，"
                "不接受模型传入的用户或群 id。更新必须带上读取时拿到的 expected_revision，"
                "否则会拒绝而不是覆盖别人的修改。",
            )
            .risk(RiskHigh)
            .owner_scoped()
            .depends_on(ARTIST_LIBRARY_SEARCH_NAME, ARTIST_LIBRARY_READ_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            artist_builder(ARTIST_LIBRARY_SAVE_NAME, "保存或更新画师串组合。")
            .string("name", "组合显示名，例如「柔和粉彩组合」。", required())
            .string(
                "scope",
                "作用域：user（个人，默认）、group（本群）、global（全局，仅超级管理员）。",
                enum("user", "group", "global"),
            )
            .string("id", "已有组合的 id；不填时自动生成。传入已有 id 就是更新。")
            .integer(
                "expected_revision",
                "更新必填：artist_library_read 返回的 revision。"
                "不填（0）时只能新建，不能覆盖已有组合。",
            )
            .string(
                "dialect",
                "组合的默认方言：natural、sd_weighted、nai、tags。",
                enum("natural", "sd_weighted", "nai", "tags"),
            )
            .string(
                "status",
                "approved（默认，可直接用于生图）或 pending（推荐结果，等待确认）。",
                enum("approved", "pending"),
            )
            .string("raw_string", "画师串原文，按用户给的样子保存；顺序和权重都会原样保留。")
            .object_array(
                "artists",
                "有序的画师列表；顺序即最终画师串的顺序，不会重新排序。",
                {
                    "artist_id": {
                        "type": "string",
                        "description": "画师标签 id；填了 tag 时可以省略，会自动生成。",
                    },
                    "tag": {
                        "type": "string",
                        "description": "画师标签本体（如 artist_a）。填了就同时写入画师目录。",
                    },
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "别名，写入画师目录。",
                    },
                    "weight": {
                        "type": "number",
                        "description": "权重，默认 1；范围 0.01 到 10。",
                    },
                },
            )
            .string_array("positive_tags", "随画师串一起写入的正向标签。")
            .string_array(
                "negative_tags",
                "负向标签；会作为独立负向提示词传递，不再拼进正向提示词。",
            )
            .string("style_description", "一句话风格描述，用于检索和展示。")
            .string_array(
                "compatible_models",
                "声明适用的模型名；留空表示不限制，不等于已验证可用。",
            )
            .string(
                "source_type",
                "来源：text（默认，用户给的）、metadata（图片元数据）、"
                "visual_recommendation（风格推荐）。",
                enum("text", "metadata", "visual_recommendation"),
            )
            .string("source_media", "组合来源图片的 media:<sha256>（可选）。")
            .string("note", "备注，例如匹配理由或使用注意事项（可选）。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return artist_library_disabled_result()
        args = decode_args(req.arguments, ArtistLibrarySaveArgs, self.name())
        if (args.name or "").strip() == "":
            return Result(content="name 不能为空。")
        id_value = (args.id or "").strip()
        if len(id_value) > MAX_ID_READ_RUNES:
            return Result(content="id 过长。")
        viewer = artist_viewer(ctx)
        source = normalize_source(args.source_type or "")
        if source == SourceKind.NONE:
            source = SourceKind.TEXT
        slots: list[PresetArtist] = []
        for raw_slot in args.artists or []:
            slot = _slot_args(raw_slot)
            artist_id = (slot.artist_id or "").strip()
            tag = (slot.tag or "").strip()
            if tag != "":
                if artist_id == "":
                    artist_id = new_artist_id(tag)
                try:
                    self.deps.service.store().put_artist(
                        Artist(
                            id=artist_id,
                            canonical_tag=tag,
                            aliases=list(slot.aliases or []),
                            style_tags=list(slot.style_tags or []),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    return Result(content="写入画师目录失败：" + artist_error_message(exc))
            if artist_id == "":
                return Result(content="artists 里每一项都需要 artist_id 或 tag（至少一个）。")
            weight = _as_float(slot.weight)
            if weight <= 0:
                weight = 1
            slots.append(PresetArtist(artist_id=artist_id, weight=weight))
        preset = Preset(
            id=id_value,
            name=args.name,
            scope=normalize_scope(args.scope or ""),
            dialect=normalize_dialect(args.dialect or ""),
            status=normalize_status(args.status or ""),
            raw_string=args.raw_string or "",
            artists=slots,
            positive_tags=list(args.positive_tags or []),
            negative_tags=list(args.negative_tags or []),
            style_description=args.style_description or "",
            compatible_models=list(args.compatible_models or []),
            source=PresetSource(
                type=source,
                media_id=(args.source_media or "").strip(),
                operator=viewer.actor_id,
                note=args.note or "",
            ),
        )
        if preset.dialect == Dialect.AUTO:
            preset.dialect = Dialect.SD_WEIGHTED
        try:
            saved = self.deps.service.save(viewer, preset, _as_int(args.expected_revision))
        except Exception as exc:  # noqa: BLE001
            return Result(content=artist_error_message(exc))
        result = Result()
        compiled = None
        compile_err: BaseException | None = None
        try:
            compiled = self.deps.service.compile(viewer, saved.id, "", saved.dialect)
        except Exception as exc:  # noqa: BLE001 - Go 里编译失败只影响 Data 与渲染行
            compile_err = exc
        if compile_err is None and compiled is not None:
            render = Render(
                dialect=saved.dialect,
                positive=compiled.positive,
                negative=compiled.negative,
                warnings=list(compiled.warnings),
            )
            payload = artist_preset_json_of(saved, artist_tag_map(self.deps), render)
            result.data = go_json(payload)
            append_warnings(result, *compiled.warnings)
        lines = [
            f"已保存画师串组合：{saved.name}（{saved.id}）\n作用域：{saved.scope.label()}\n"
            f"revision：{saved.revision}\n画师数：{len(saved.artists)}"
        ]
        if saved.raw_string != "":
            lines.append(f"\n画师串原文：{saved.raw_string}")
        if compile_err is None and compiled is not None and compiled.positive != "":
            lines.append(f"\n渲染正向：{compiled.positive}")
        if compile_err is None and compiled is not None and compiled.negative != "":
            lines.append(f"\n渲染负向：{compiled.negative}")
        if saved.status == PresetStatus.PENDING:
            lines.append(
                "\n状态是 pending：这条只是候选，不会自动用于生图；"
                "确认后再用 status=approved 保存一次。"
            )
        lines.append(
            f"\n下次生图可以传 artist_preset_id={saved.id}，"
            "或先 artist_library_read 看渲染结果。"
        )
        result.content = "".join(lines)
        return result


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
@dataclass
class ArtistLibraryDeleteArgs:
    """对应 Go ``artistLibraryDeleteArgs``。"""

    id: str = ""
    expected_revision: int = 0
    confirm: bool = False


class ArtistLibraryDeleteTool:
    """确认后删除一个组合。对应 Go ``ArtistLibraryDeleteTool``。"""

    def __init__(self, deps: ArtistTools | None = None) -> None:
        self.deps = deps if deps is not None else ArtistTools()

    def name(self) -> str:
        return ARTIST_LIBRARY_DELETE_NAME

    def info_value(self) -> Info:
        return (
            artist_builder(
                ARTIST_LIBRARY_DELETE_NAME,
                "删除一个画师串组合。首次调用只返回将被删除的内容并说明影响，"
                "必须再次调用并传 confirm=true 才会真正删除；删除自己的个人组合、"
                "本群组合或（超级管理员）全局组合需要相应权限。",
            )
            .risk(RiskHigh)
            .owner_scoped()
            .depends_on(ARTIST_LIBRARY_SEARCH_NAME, ARTIST_LIBRARY_READ_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            artist_builder(ARTIST_LIBRARY_DELETE_NAME, "删除一个画师串组合。")
            .string("id", "要删除的组合 id。", required())
            .integer("expected_revision", "可选：读取时拿到的 revision；不填时按当前版本删除。")
            .boolean("confirm", "确认删除；第一次调用不要传，看到影响说明后再传 true。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        if not self.deps.enabled():
            return artist_library_disabled_result()
        args = decode_args(req.arguments, ArtistLibraryDeleteArgs, self.name())
        id_value = (args.id or "").strip()
        if id_value == "":
            return Result(content="id 不能为空。")
        viewer = artist_viewer(ctx)
        try:
            preset = self.deps.service.preset(viewer, id_value)
        except Exception as exc:  # noqa: BLE001
            return Result(content=artist_error_message(exc))
        if not args.confirm:
            lines = [
                f"将要删除画师串组合：{preset.name}（{preset.id}）\n"
                f"作用域：{preset.scope.label()}\n状态：{preset.status}\n"
                f"revision：{preset.revision}\n画师数：{len(preset.artists)}"
            ]
            if preset.raw_string != "":
                lines.append(f"\n画师串原文：{preset.raw_string}")
            lines.append(
                "\n\n删除后该组合立即不可用，引用它的调用会失败；"
                "画师目录里的标签不会被删除。确认删除请再次调用并传 confirm=true"
            )
            if _as_int(args.expected_revision) > 0:
                lines.append(f"、expected_revision={_as_int(args.expected_revision)}")
            else:
                lines.append(f"、expected_revision={preset.revision}")
            lines.append("。")
            return Result(content="".join(lines))
        revision = _as_int(args.expected_revision)
        if revision <= 0:
            revision = preset.revision
        try:
            removed = self.deps.service.delete(viewer, preset.id, revision)
        except Exception as exc:  # noqa: BLE001
            return Result(content=artist_error_message(exc))
        return Result(
            content=f"已删除画师串组合：{removed.name}（{removed.id}，{removed.scope.label()}）。",
            data=go_json(
                {
                    "deleted": removed.id,
                    "name": removed.name,
                    "scope": str(removed.scope),
                }
            ),
        )


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------
def _as_int(value: Any) -> int:
    """近似 Go 的 ``json.Unmarshal`` 到 ``int``。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _slot_args(raw: Any) -> ArtistSlotArgs:
    """把 ``artists`` 数组元素收成 :class:`ArtistSlotArgs`（dict 或已构造对象）。"""
    if isinstance(raw, ArtistSlotArgs):
        return raw
    if isinstance(raw, dict):
        return ArtistSlotArgs(
            artist_id=raw.get("artist_id") or "",
            tag=raw.get("tag") or "",
            aliases=list(raw.get("aliases") or []),
            style_tags=list(raw.get("style_tags") or []),
            weight=raw.get("weight") or 0.0,
        )
    return ArtistSlotArgs()


__all__ = [name for name in dir() if not name.startswith("_")]
