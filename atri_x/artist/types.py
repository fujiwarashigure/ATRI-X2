"""画师串领域类型、方言/来源枚举与各类上限。

对应 Go：``internal/artist/types.go``，并收纳 ``internal/artist/metadata.go`` 里
对外可见的常量与结果类型 ``Metadata``（它是「字节解析路径」与「入库留档路径」
共享的契约，所以放在类型层而不是解析器里）。

分工说明：

* **metadata 路径**（本文件 + ``metadata.py`` + ``metadata_formats.py``）用到的
  类型：``Metadata``、``MetadataText``（Go 未导出类型 ``metadataText``，两个解析模块
  共用的文本块收集器）、``MetadataContainer*``/``MetadataSource*``、
  ``MetadataOriginArchive``/``MetadataOriginEmbedded``、``ImageMetadata``、
  两个字节上限。
* **串库/组合领域**（``Artist``、``Preset``、``PresetArtist``、``PresetSource``、
  ``Library``、``Extraction``、``Token``、``ArtistRef`` 及其枚举）由
  ``atri_x/artist/parser.py`` / ``store.py`` / ``render.py`` / ``service.py``
  负责实现业务逻辑；此处**只保留与 Go 一致的字段定义**，不做归一化与校验
  （Go 的 ``Artist.Normalize/Validate``、``Preset.Normalize/Validate`` 归 store.py）。
* Go 的哨兵错误 ``ErrDisabled``/``ErrNotFound``/``ErrForbidden``/``ErrConflict``/
  ``ErrInvalid``/``ErrLimitExceeded`` 按 PORTING.md §3 统一落在 ``atri_x/errors.py``，
  本文件不重复定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# --- 来源与方言（Go types.go 的 SourceKind / Dialect / TokenKind） ---

class SourceKind(StrEnum):
    """画师串的来源。对应 Go ``SourceKind``。"""

    NONE = "none"
    TEXT = "text"
    METADATA = "metadata"
    VISUAL = "visual_recommendation"

    # Go 常量名别名，便于交叉检索（不产生新成员）。
    SourceNone = NONE
    SourceText = TEXT
    SourceMetadata = METADATA
    SourceVisual = VISUAL

    def valid(self) -> bool:
        """对应 Go ``SourceKind.Valid``。"""
        return self in _SOURCE_KINDS

    def label(self) -> str:
        """对应 Go ``SourceKind.Label``，用户可见中文名。"""
        return {
            SourceKind.TEXT: "文本解析",
            SourceKind.METADATA: "图片元数据",
            SourceKind.VISUAL: "风格推荐",
        }.get(self, "无来源")

_SOURCE_KINDS = (SourceKind.NONE, SourceKind.TEXT, SourceKind.METADATA, SourceKind.VISUAL)

def normalize_source(value: str) -> SourceKind:
    """对应 Go ``NormalizeSource``：未知值折叠为 ``none``（不抛异常）。"""
    folded = value.strip().lower()
    if folded == SourceKind.TEXT:
        return SourceKind.TEXT
    if folded == SourceKind.METADATA:
        return SourceKind.METADATA
    if folded == SourceKind.VISUAL:
        return SourceKind.VISUAL
    return SourceKind.NONE

class Dialect(StrEnum):
    """目标模型族理解的提示词语法。对应 Go ``Dialect``。"""

    AUTO = "auto"
    NATURAL = "natural"
    SD_WEIGHTED = "sd_weighted"
    NAI = "nai"
    TAGS = "tags"

    DialectAuto = AUTO
    DialectNatural = NATURAL
    DialectSDWeighted = SD_WEIGHTED
    DialectNAI = NAI
    DialectTags = TAGS

    def valid(self) -> bool:
        """对应 Go ``Dialect.Valid``。"""
        return self in _DIALECTS

    def label(self) -> str:
        """对应 Go ``Dialect.Label``。"""
        return {
            Dialect.NATURAL: "自然语言",
            Dialect.SD_WEIGHTED: "SD 加权标签",
            Dialect.NAI: "NovelAI 加权标签",
            Dialect.TAGS: "纯标签",
        }.get(self, "跟随 profile")

_DIALECTS = (Dialect.AUTO, Dialect.NATURAL, Dialect.SD_WEIGHTED, Dialect.NAI, Dialect.TAGS)

def normalize_dialect(value: str) -> Dialect:
    """对应 Go ``NormalizeDialect``：未识别的一律折叠为 ``auto``。"""
    folded = value.strip().lower()
    if folded == Dialect.NATURAL:
        return Dialect.NATURAL
    if folded in (Dialect.SD_WEIGHTED, "sd", "a1111", "webui"):
        return Dialect.SD_WEIGHTED
    if folded in (Dialect.NAI, "novelai"):
        return Dialect.NAI
    if folded in (Dialect.TAGS, "tag"):
        return Dialect.TAGS
    return Dialect.AUTO

class TokenKind(StrEnum):
    """提示词里一个顶层片段的分类。对应 Go ``TokenKind``。"""

    PLAIN = "plain"
    ARTIST = "artist"
    UNPARSED = "unparsed"

    TokenPlain = PLAIN
    TokenArtist = ARTIST
    # Go ``TokenUnparsed``：本解析器拒绝解释的表达式，必须原样透传。
    TokenUnparsed = UNPARSED

# --- 提示词片段（Go types.go 的 Token / ArtistRef） ---

@dataclass
class Token:
    """提示词的一个顶层片段。对应 Go ``Token``。

    ``raw`` 是精确的原始子串，未改动的 token 可以逐字节重新渲染；
    ``value`` 是解除转义后的无权重内文，仅供查找。
    """

    index: int = 0
    raw: str = ""
    value: str = ""
    weight: float = 0.0
    weighted: bool = False
    kind: TokenKind = TokenKind.PLAIN
    artist: str = ""

@dataclass
class ArtistRef:
    """画师串里的一个画师标签，按源顺序。对应 Go ``ArtistRef``。"""

    id: str = ""
    name: str = ""
    raw: str = ""
    weight: float = 0.0
    index: int = 0

# --- 生成参数解析结果（Go metadata.go 的 Metadata 与相关常量） ---

# Go Metadata.Container 的取值。
MetadataContainerPNG = "png"
MetadataContainerJPEG = "jpeg"
MetadataContainerWebP = "webp"

# Go Metadata.Source 的取值（方言标识）。
MetadataSourceA1111 = "a1111"
MetadataSourceNovelAI = "novelai"
MetadataSourceComfyUI = "comfyui"
MetadataSourceInvokeAI = "invokeai"
MetadataSourceUnknown = "unknown"

# 生成参数来自哪里（Go types.go）。图片被转码后只剩留档能回答。
MetadataOriginArchive = "archive"
MetadataOriginEmbedded = "embedded"

# 单个文本块与单张图片全部文本的硬上限（Go metadata.go）。
MaxMetadataTextBytes = 1 << 20
MaxMetadataTotalBytes = 4 << 20

class MetadataText:
    """Go 未导出类型 ``metadataText``：一张图片的全部文本块 + 字节预算 + 降级说明。

    放在这里是因为 ``metadata.py``（容器解析）与 ``metadata_formats.py``（方言解析）
    都要用它，拆包后需要一个共同底座。``add`` 先按单块上限拦截，再按总量上限熔断，
    保证恶意文件既不能撑爆内存，也不能把告警列表撑长。
    """

    __slots__ = ("values", "order", "total", "seen", "exhausted", "warnings", "_warned")

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.order: list[str] = []
        self.total: int = 0
        self.seen: bool = False
        self.exhausted: bool = False
        self.warnings: list[str] = []
        self._warned: set[str] = set()

    def warn(self, message: str) -> None:
        """对应 Go ``metadataText.warn``：相同告警只记一次。"""
        if message not in self._warned:
            self._warned.add(message)
            self.warnings.append(message)

    def add(self, keyword: str, value: str) -> None:
        """对应 Go ``metadataText.add``：超限的块被丢弃并给出告警。"""
        if not keyword:
            return
        self.seen = True
        size = len(value.encode("utf-8", "surrogatepass"))
        if size > MaxMetadataTextBytes:
            self.warn("单个文本块超过 1 MiB 上限，已丢弃")
            return
        if self.exhausted:
            return
        if self.total + size > MaxMetadataTotalBytes:
            self.exhausted = True
            self.warn("元数据文本总量超过 4 MiB 上限，已忽略后续文本块")
            return
        self.total += size
        if keyword not in self.values:
            self.order.append(keyword)
        self.values[keyword] = value

    def get(self, keyword: str) -> tuple[str, bool]:
        """对应 Go ``metadataText.get``：大小写不敏感，先插入者优先。"""
        if keyword in self.values:
            return self.values[keyword], True
        folded = keyword.casefold()
        for existing in self.order:
            if existing.casefold() == folded:
                return self.values[existing], True
        return "", False

    def has(self, keyword: str) -> bool:
        """对应 Go ``metadataText.has``。"""
        return self.get(keyword)[1]

    def mark(self) -> None:
        """对应 Go ``metadataText.mark``：容器带了文本块（哪怕解不出来）。"""
        self.seen = True

@dataclass
class Metadata:
    """从图片容器里恢复出来的生成参数。对应 Go ``Metadata``。

    字段顺序与 Go 结构体一致，便于按位置构造。``parameters`` / ``text`` 在已知
    容器里总是可用（非 None），调用方不需要额外判空。
    """

    container: str = ""  # "png" | "jpeg" | "webp" | "" 未知容器
    source: str = ""  # "a1111" | "novelai" | "comfyui" | "invokeai" | "unknown" | ""
    positive: str = ""  # 正向提示词原文，未做任何改写
    negative: str = ""  # 负向提示词原文
    parameters: dict[str, str] = field(default_factory=dict)  # steps / sampler / cfg / seed ...
    text: dict[str, str] = field(default_factory=dict)  # 原始文本块：关键字 -> 值
    prompts: list[str] = field(default_factory=list)  # 无法归属正负向的其它提示词
    workflow: str = ""  # ComfyUI 工作流 JSON 原文（有则填）
    warnings: list[str] = field(default_factory=list)  # 降级说明，用户可见，中文

# 任务书里把生成参数结果类型叫 GenerationMetadata；Go 侧类型名是 Metadata，
# 这里用别名让两种叫法都可用（同一个类，isinstance 判定不受影响）。
GenerationMetadata = Metadata

@dataclass
class Extraction:
    """从提示词里读出画师串的结果。对应 Go ``Extraction``。

    **由 artist/parser.py 负责**：切分、权重解析与 ``TokenUnparsed`` 判定都在那边；
    此处仅保留类型定义与 Go 的两个方法（``Names`` / ``Warning``）以对齐 Go。
    """

    source: SourceKind = SourceKind.NONE
    dialect: Dialect = Dialect.AUTO
    original: str = ""
    has_original: bool = False
    artists: list[ArtistRef] = field(default_factory=list)
    remaining: str = ""
    tokens: list[Token] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        """对应 Go ``Extraction.Names``：按源顺序返回画师名。"""
        return [item.name for item in self.artists]

    def warning(self, text: str) -> None:
        """对应 Go ``Extraction.Warning``：非空告警只追加一次。"""
        trimmed = text.strip()
        if not trimmed or trimmed in self.warnings:
            return
        self.warnings.append(trimmed)

@dataclass
class ImageMetadata:
    """读一张图片生成参数的最终结果。对应 Go ``ImageMetadata``。

    ``origin`` 为 ``MetadataOriginArchive`` / ``MetadataOriginEmbedded``；
    什么都没找到时为空字符串。
    """

    extraction: Extraction = field(default_factory=Extraction)
    metadata: Metadata = field(default_factory=Metadata)
    found: bool = False
    origin: str = ""

# --- 串库/组合领域类型（字段与 Go 一致，业务逻辑归 parser/store/render/service） ---

class Scope(StrEnum):
    """预设或目录条目的可见范围。对应 Go ``Scope``。"""

    USER = "user"
    GROUP = "group"
    GLOBAL = "global"

    ScopeUser = USER
    ScopeGroup = GROUP
    ScopeGlobal = GLOBAL

    def valid(self) -> bool:
        """对应 Go ``Scope.Valid``。"""
        return self in (Scope.USER, Scope.GROUP, Scope.GLOBAL)

    def label(self) -> str:
        """对应 Go ``Scope.Label``。"""
        return {Scope.USER: "个人", Scope.GROUP: "本群", Scope.GLOBAL: "全局"}.get(self, "未知")

class PresetStatus(StrEnum):
    """预设的审核状态。对应 Go ``PresetStatus``。"""

    APPROVED = "approved"
    PENDING = "pending"

    StatusApproved = APPROVED
    StatusPending = PENDING

    def valid(self) -> bool:
        """对应 Go ``PresetStatus.Valid``。"""
        return self in (PresetStatus.APPROVED, PresetStatus.PENDING)

@dataclass
class PresetSource:
    """预设的来源记录。对应 Go ``PresetSource``（由 artist/store.py 负责写入）。"""

    type: SourceKind = SourceKind.NONE
    media_id: str = ""
    operator: str = ""
    note: str = ""

@dataclass
class Artist:
    """目录条目：规范画师标签 + 用户/模型实际写出的名字。对应 Go ``Artist``。"""

    id: str = ""
    canonical_tag: str = ""
    aliases: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    note: str = ""
    created_at: datetime | None = None  # Go 零值 time.Time 在 Python 里表示为 None
    updated_at: datetime | None = None

@dataclass
class PresetArtist:
    """预设里的一个画师槽位；切片顺序有意义。对应 Go ``PresetArtist``。"""

    artist_id: str = ""
    weight: float = 0.0

@dataclass
class Preset:
    """一个可复用的画师串组合。对应 Go ``Preset``（业务逻辑归 artist/store.py）。"""

    id: str = ""
    name: str = ""
    scope: Scope = Scope.USER
    scope_id: str = ""
    revision: int = 0
    dialect: Dialect = Dialect.AUTO
    compatible_models: list[str] = field(default_factory=list)
    artists: list[PresetArtist] = field(default_factory=list)
    raw_string: str = ""
    positive_tags: list[str] = field(default_factory=list)
    negative_tags: list[str] = field(default_factory=list)
    style_description: str = ""
    source: PresetSource = field(default_factory=PresetSource)
    status: PresetStatus = PresetStatus.APPROVED
    created_at: datetime | None = None
    updated_at: datetime | None = None

@dataclass
class Library:
    """整份磁盘串库：一个文件承载全部 scope。对应 Go ``Library``。"""

    schema: int = 0
    revision: int = 0
    updated_at: datetime | None = None
    artists: list[Artist] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)

def normalize_scope(value: str) -> Scope:
    """对应 Go ``NormalizeScope``：畸形输入收紧到最窄的 ``user``。"""
    folded = value.strip().lower()
    if folded == Scope.GROUP:
        return Scope.GROUP
    if folded == Scope.GLOBAL:
        return Scope.GLOBAL
    return Scope.USER

def normalize_status(value: str) -> PresetStatus:
    """对应 Go ``NormalizeStatus``：只认 ``pending``，其余一律 ``approved``。"""
    if value.strip().lower() == PresetStatus.PENDING:
        return PresetStatus.PENDING
    return PresetStatus.APPROVED

# 串库容量与长度上限（Go types.go）。
MaxArtists = 2000
MaxPresets = 2000
MaxPresetArtists = 32
MaxAliases = 32
MaxStyleTags = 32
MaxTagRunes = 200
MaxNameRunes = 80
MaxNoteRunes = 2000
MaxRawStringRunes = 4000
MaxWeight = 10.0
MinWeight = 0.01

# 条目 id 的形状约束（Go types.go）。
MaxIDRunes = 64
MaxIDReadRunes = 128

__all__ = [
    "Artist",
    "ArtistRef",
    "Dialect",
    "Extraction",
    "GenerationMetadata",
    "ImageMetadata",
    "Library",
    "MaxAliases",
    "MaxArtists",
    "MaxIDReadRunes",
    "MaxIDRunes",
    "MaxMetadataTextBytes",
    "MaxMetadataTotalBytes",
    "MaxNameRunes",
    "MaxNoteRunes",
    "MaxPresetArtists",
    "MaxPresets",
    "MaxRawStringRunes",
    "MaxStyleTags",
    "MaxTagRunes",
    "MaxWeight",
    "Metadata",
    "MetadataText",
    "MetadataContainerJPEG",
    "MetadataContainerPNG",
    "MetadataContainerWebP",
    "MetadataOriginArchive",
    "MetadataOriginEmbedded",
    "MetadataSourceA1111",
    "MetadataSourceComfyUI",
    "MetadataSourceInvokeAI",
    "MetadataSourceNovelAI",
    "MetadataSourceUnknown",
    "MinWeight",
    "Preset",
    "PresetArtist",
    "PresetSource",
    "PresetStatus",
    "Scope",
    "SourceKind",
    "Token",
    "TokenKind",
    "normalize_dialect",
    "normalize_scope",
    "normalize_source",
    "normalize_status",
]
