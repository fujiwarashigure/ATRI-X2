"""聊天漫画业务域：把当前群聊改编成一页结构化漫画并复用现有生图能力。

对应上游 Go 仓库：**没有**这个域（Python 侧新增功能）。设计目标是"薄业务层 +
复用基础设施"：

* :mod:`atri_x.comic.models`     计划/记录值对象与校验（谁都不 import 谁）
* :mod:`atri_x.comic.context`    只从当前会话取材，保留消息 ID
* :mod:`atri_x.comic.characters` 群友身份 → 角色卡/头像 → 参考图绑定
* :mod:`atri_x.comic.planner`    规则规划 + 模型 JSON 的结构校验
* :mod:`atri_x.comic.prompt`     受保护提示词编译（台词逐字、超长报错）
* :mod:`atri_x.comic.store`      计划/记录的文件持久化（原子写 + 路径白名单）
* :mod:`atri_x.comic.service`    编排；生图客户端/限流/媒体/安全检查全部注入

对外入口是 :class:`~atri_x.comic.service.ComicService`；工具层见
``atri_x/tool/builtin/comic.py``（``comic_plan`` / ``comic_render``），装配层见
``atri_x/app/comic.py``（规划模型与平台头像适配）与
``atri_x/app/tool_runtime.py``（把服务接进运行时）。
"""

from __future__ import annotations

from atri_x.comic.characters import (
    CharacterResolveRequest,
    CharacterResolveResult,
    ComicAvatarResolver,
    NoAvatarResolver,
    parse_reference_overrides,
    resolve_comic_characters,
)
from atri_x.comic.context import (
    DEFAULT_CONTEXT_LIMIT,
    MAX_CONTEXT_MESSAGES,
    collect_comic_context,
    format_context_material,
)
from atri_x.comic.models import (
    CHARACTER_SOURCES,
    DIALOGUE_KINDS,
    LAYOUTS,
    LANGUAGES,
    MAX_COMIC_CHARACTERS,
    MAX_DIALOGUE_RUNES,
    MAX_PLAN_PANELS,
    MAX_SCENE_RUNES,
    MAX_TITLE_RUNES,
    PLAN_SCHEMA_VERSION,
    RECORD_SCHEMA_VERSION,
    REFERENCE_KINDS,
    ComicCharacter,
    ComicCharacterError,
    ComicCharacterReference,
    ComicContext,
    ComicContextError,
    ComicContextMessage,
    ComicDataError,
    ComicDialogue,
    ComicError,
    ComicPanel,
    ComicPlan,
    ComicPlanError,
    ComicPromptError,
    ComicRenderError,
    ComicSource,
    ComicStoreError,
    is_maybe_unknown_error,
    new_plan_id,
    new_task_id,
    validate_plan,
)
from atri_x.comic.planner import (
    ComicPlanRequest,
    ComicPlanner,
    build_plan_from_mapping,
    build_plan_prompt,
    parse_plan_json,
    parse_plan_response,
    rules_plan,
)
from atri_x.comic.prompt import (
    DEFAULT_COMIC_PROMPT_RUNES,
    MAX_COMIC_REFERENCES,
    ComicPrompt,
    build_comic_prompt,
    layout_description,
)
from atri_x.comic.records import ComicRecord, validate_record
from atri_x.comic.service import (
    DEFAULT_PANEL_COUNT,
    MAX_PANEL_COUNT,
    ComicOptions,
    ComicService,
    PlanOutcome,
    PlanRequest,
    RenderOutcome,
    RenderRequest,
    comic_options_from,
)
from atri_x.comic.store import PLANS_DIR, RECORDS_DIR, ComicStore, comic_root

__all__ = [
    "CHARACTER_SOURCES",
    "DEFAULT_COMIC_PROMPT_RUNES",
    "DEFAULT_CONTEXT_LIMIT",
    "DEFAULT_PANEL_COUNT",
    "DIALOGUE_KINDS",
    "LAYOUTS",
    "LANGUAGES",
    "MAX_COMIC_CHARACTERS",
    "MAX_COMIC_REFERENCES",
    "MAX_CONTEXT_MESSAGES",
    "MAX_DIALOGUE_RUNES",
    "MAX_PANEL_COUNT",
    "MAX_PLAN_PANELS",
    "MAX_SCENE_RUNES",
    "MAX_TITLE_RUNES",
    "PLANS_DIR",
    "PLAN_SCHEMA_VERSION",
    "RECORDS_DIR",
    "RECORD_SCHEMA_VERSION",
    "REFERENCE_KINDS",
    "CharacterResolveRequest",
    "CharacterResolveResult",
    "ComicAvatarResolver",
    "ComicCharacter",
    "ComicCharacterError",
    "ComicCharacterReference",
    "ComicContext",
    "ComicContextError",
    "ComicContextMessage",
    "ComicDataError",
    "ComicDialogue",
    "ComicError",
    "ComicOptions",
    "ComicPanel",
    "ComicPlan",
    "ComicPlanError",
    "ComicPlanRequest",
    "ComicPlanner",
    "ComicPrompt",
    "ComicPromptError",
    "ComicRecord",
    "ComicRenderError",
    "ComicService",
    "ComicSource",
    "ComicStore",
    "ComicStoreError",
    "NoAvatarResolver",
    "PlanOutcome",
    "PlanRequest",
    "RenderOutcome",
    "RenderRequest",
    "build_comic_prompt",
    "build_plan_from_mapping",
    "build_plan_prompt",
    "collect_comic_context",
    "comic_options_from",
    "comic_root",
    "format_context_material",
    "is_maybe_unknown_error",
    "layout_description",
    "new_plan_id",
    "new_task_id",
    "parse_plan_json",
    "parse_plan_response",
    "parse_reference_overrides",
    "resolve_comic_characters",
    "rules_plan",
    "validate_plan",
    "validate_record",
]
