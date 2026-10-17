"""OpenAI 兼容生图客户端与提示词组装（自包含子系统）。

对应 Go：``internal/imagegen``（``client.go`` / ``prompt.go`` / ``optimize.go`` /
``library.go`` / ``search.go`` / ``context.go`` + ``prompts/library.json``）。

本包**不** import ``atri_x.config``：上游 Go 的同一个包也不 import
``internal/config``，配置由调用方（``internal/tool/builtin`` →
``atri_x/tool/builtin``、``internal/app`` → ``atri_x/app``）映射成
:class:`Config` 后注入。

模块划分（与 Go 文件一一对应）：

- :mod:`atri_x.imagegen.client` — ``client.go``：端点、鉴权、参考图、回包解析。
- :mod:`atri_x.imagegen.prompt` — ``prompt.go``：``PromptBlock`` /
  ``compose_blocks``，按优先级整体丢弃。
- :mod:`atri_x.imagegen.optimize` — ``optimize.go``：规则优化器（短语 / tag 两种方言）。
- :mod:`atri_x.imagegen.library` — ``library.go``：内嵌库类型与加载。
- :mod:`atri_x.imagegen.search` — ``search.go``：库检索与改写器提示。
- :mod:`atri_x.imagegen.context` — ``context.go``：``@image:<name>`` profile 传递。
- :mod:`atri_x.imagegen._extract_library` — 从上游机械抽取 ``prompts/library.json``。
"""

from __future__ import annotations

from atri_x.imagegen.client import (
    DEFAULT_MAX_PROMPT,
    DEFAULT_MODEL,
    DEFAULT_NEGATIVE_PROMPT_FIELD,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_QUALITY,
    DEFAULT_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_IMAGE_BYTES,
    MAX_RESPONSE_BYTES,
    Client,
    Config,
    ImageGenError,
    NegativePromptMode,
    NegativePromptModeField,
    NegativePromptModeInline,
    NegativePromptModeOff,
    Reference,
    Request,
    Result,
    filter_reserved_headers,
    filter_reserved_payload,
    first_name,
    first_non_empty,
    normalize_negative_prompt_mode,
    read_error_body,
    reserved_payload_fields,
)
from atri_x.imagegen.context import profile_from, with_profile
from atri_x.imagegen.library import (
    NegativeGroup,
    PromptEntry,
    PromptLibrary,
    PromptTemplate,
    PromptTip,
    TermCount,
    UseCasePreset,
    library_path,
    prompt_lib,
)
from atri_x.imagegen.optimize import (
    EDIT_GUARD,
    OptimizeOptions,
    OptimizeResult,
    has_aspect_ratio,
    match_entry,
    match_preset,
    ratio_text,
    select_negatives,
    split_tag_words,
    valid_tag_word,
)
from atri_x.imagegen.optimize import optimize as optimize_prompt
from atri_x.imagegen.optimize import optimize_tags
from atri_x.imagegen.prompt import (
    ComposeResult,
    PromptBlock,
    PromptPriorityCore,
    PromptPriorityOptional,
    PromptPriorityProtected,
    PromptPriorityStyle,
    compose_blocks,
    join_blocks,
)
from atri_x.imagegen.search import LibraryMatch, LibraryScopes, library_hints
from atri_x.imagegen.search import search as search_library

#: Go 导出名的交叉检索别名（PascalCase）。
New = Client
ComposeBlocks = compose_blocks
JoinBlocks = join_blocks
PromptLib = prompt_lib
NormalizeNegativePromptMode = normalize_negative_prompt_mode
FilterReservedPayload = filter_reserved_payload
FilterReservedHeaders = filter_reserved_headers
FirstNonEmpty = first_non_empty
ReadErrorBody = read_error_body
WithProfile = with_profile
ProfileFrom = profile_from

#: 包级不导出裸名 ``optimize`` / ``search``：它们与同名子模块
#: (:mod:`atri_x.imagegen.optimize` / :mod:`atri_x.imagegen.search`) 冲突，
#: 会让 ``atri_x.imagegen.search`` 变成函数而不是模块。Go 的对应 API 是
#: ``PromptLibrary`` 上的方法，直接用 ``library.optimize(...)`` /
#: ``library.search(...)`` 即可；需要自由函数时用下面这两个别名。
optimize_prompt = optimize_prompt
search_library = search_library

__all__ = [
    "EDIT_GUARD",
    "Client",
    "ComposeBlocks",
    "ComposeResult",
    "Config",
    "DEFAULT_MAX_PROMPT",
    "DEFAULT_MODEL",
    "DEFAULT_NEGATIVE_PROMPT_FIELD",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_QUALITY",
    "DEFAULT_SIZE",
    "DEFAULT_TIMEOUT_SECONDS",
    "FilterReservedHeaders",
    "FilterReservedPayload",
    "FirstNonEmpty",
    "ImageGenError",
    "JoinBlocks",
    "LibraryMatch",
    "LibraryScopes",
    "MAX_IMAGE_BYTES",
    "MAX_RESPONSE_BYTES",
    "NegativeGroup",
    "NegativePromptMode",
    "NegativePromptModeField",
    "NegativePromptModeInline",
    "NegativePromptModeOff",
    "New",
    "NormalizeNegativePromptMode",
    "OptimizeOptions",
    "OptimizeResult",
    "PromptBlock",
    "PromptEntry",
    "PromptLib",
    "PromptLibrary",
    "PromptPriorityCore",
    "PromptPriorityOptional",
    "PromptPriorityProtected",
    "PromptPriorityStyle",
    "PromptTemplate",
    "PromptTip",
    "ProfileFrom",
    "ReadErrorBody",
    "Reference",
    "Request",
    "Result",
    "TermCount",
    "UseCasePreset",
    "WithProfile",
    "compose_blocks",
    "filter_reserved_headers",
    "filter_reserved_payload",
    "first_name",
    "first_non_empty",
    "has_aspect_ratio",
    "join_blocks",
    "library_hints",
    "library_path",
    "match_entry",
    "match_preset",
    "normalize_negative_prompt_mode",
    "optimize_prompt",
    "optimize_tags",
    "profile_from",
    "prompt_lib",
    "ratio_text",
    "read_error_body",
    "reserved_payload_fields",
    "search_library",
    "select_negatives",
    "split_tag_words",
    "valid_tag_word",
    "with_profile",
]
