"""漫画业务策略与工具入参/出参值对象。

与 :mod:`atri_x.comic.service` 分开的原因：这些是**稳定的契约**（配置映射、
工具参数、结果），而 service 是编排流程，两者变化频率不同；放在一个文件里
会同时超过 600 行、也会让配置映射埋在两百行业务逻辑中间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.comic.prompt import MAX_COMIC_REFERENCES
from atri_x.comic.records import ComicRecord

__all__ = [
    "DEFAULT_PANEL_COUNT",
    "MAX_PANEL_COUNT",
    "ComicOptions",
    "PlanOutcome",
    "PlanRequest",
    "RenderOutcome",
    "RenderRequest",
    "comic_options_from",
    "dedupe_warnings",
]

DEFAULT_PANEL_COUNT = 6
MAX_PANEL_COUNT = 12
_MAX_WARNINGS = 12


def _cfg(source: Any, name: str, default: Any) -> Any:
    value = getattr(source, name, None)
    return default if value is None else value


@dataclass
class ComicOptions:
    """漫画业务策略（来自 ``[comic]`` 段，缺段时用这里的默认值）。"""

    enabled: bool = True
    #: ``auto``（有规划模型就用，否则规则）| ``rules`` | ``llm``。
    planner: str = "auto"
    language: str = "zh"
    layout: str = "grid"
    default_panel_count: int = DEFAULT_PANEL_COUNT
    max_panels: int = MAX_PANEL_COUNT
    max_characters: int = 4
    context_limit: int = 12
    max_scene_runes: int = 240
    max_dialogue_runes: int = 120
    max_prompt_runes: int = 6000
    send_by_default: bool = True


def comic_options_from(section: Any) -> ComicOptions:
    """把配置段（``[comic]``）映射成 :class:`ComicOptions`；``None`` 用默认值。"""
    options = ComicOptions()
    if section is None:
        return options
    options.enabled = bool(_cfg(section, "enabled", options.enabled))
    options.planner = str(_cfg(section, "planner", options.planner) or options.planner).strip()
    if options.planner not in ("auto", "rules", "llm"):
        options.planner = "auto"
    options.language = str(_cfg(section, "language", options.language) or options.language)
    if options.language not in ("zh", "en"):
        options.language = "zh"
    options.layout = str(_cfg(section, "layout", options.layout) or options.layout)
    if options.layout not in ("grid", "strip"):
        options.layout = "grid"
    options.default_panel_count = int(
        _cfg(section, "default_panel_count", options.default_panel_count) or 0
    ) or DEFAULT_PANEL_COUNT
    options.max_panels = int(_cfg(section, "max_panels", options.max_panels) or 0) or MAX_PANEL_COUNT
    options.max_panels = max(1, min(options.max_panels, MAX_PANEL_COUNT))
    options.max_characters = int(_cfg(section, "max_characters", options.max_characters) or 0) or 4
    options.max_characters = max(1, min(options.max_characters, MAX_COMIC_REFERENCES))
    options.context_limit = int(_cfg(section, "context_limit", options.context_limit) or 0) or 12
    options.max_scene_runes = int(
        _cfg(section, "max_scene_runes", options.max_scene_runes) or 0
    ) or 240
    options.max_dialogue_runes = int(
        _cfg(section, "max_dialogue_runes", options.max_dialogue_runes) or 0
    ) or 120
    options.max_prompt_runes = int(
        _cfg(section, "max_prompt_runes", options.max_prompt_runes) or 0
    ) or 6000
    options.send_by_default = bool(_cfg(section, "send_by_default", options.send_by_default))
    return options


@dataclass
class PlanRequest:
    """``comic_plan`` 的入参（已由工具层解析）。"""

    panel_count: int = 0
    language: str = ""
    layout: str = ""
    title_hint: str = ""
    instructions: str = ""
    character_mode: str = "auto"
    character_ids: list[str] = field(default_factory=list)
    reference_images: list[str] = field(default_factory=list)
    anchor_message_id: str = ""
    context_query: str = ""
    context_limit: int = 0


@dataclass
class PlanOutcome:
    plan: Any = None
    plan_path: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenderRequest:
    """``comic_render`` 的入参（已由工具层解析）。"""

    plan_id: str = ""
    revision: int = 0
    #: 非空表示"重发这个任务的已有图片"，不会重新生成。
    task_id: str = ""
    profile: str = ""
    size: str = ""
    quality: str = ""
    send: bool | None = None
    dry_run: bool = False


@dataclass
class RenderOutcome:
    plan: Any = None
    record: ComicRecord | None = None
    media_id: str = ""
    media_name: str = ""
    mime_type: str = ""
    prompt: str = ""
    negative: str = ""
    revised_prompt: str = ""
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    reused: bool = False


def dedupe_warnings(values: list[str]) -> list[str]:
    """去空、去重、限长（告警太多会淹没工具结果）。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out[:_MAX_WARNINGS]
