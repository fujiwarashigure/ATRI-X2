"""结构化图像分析结果的解析与限幅。

对应 Go：``internal/vision/analysis.go``。

这一层把「像素级猜测」挡在画师流水线之外：模型只描述它真正看得见的东西，
是否与某个已知风格组合相似由**本地**画师串库检索决定。分析结果从不声称知道
是哪位画师画的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import ErrImageAnalysisEmpty
from atri_x.vision.prompt import clean_text

__all__ = [
    "ImageAnalysis",
    "MAX_ANALYSIS_PROMPT_RUNES",
    "MAX_ANALYSIS_TAGS",
    "MAX_ANALYSIS_TAG_RUNES",
    "MAX_ANALYSIS_WARNINGS",
    "MAX_ANALYSIS_WARNING_RUNES",
    "clean_analysis_list",
    "extract_json_object",
    "parse_image_analysis",
]

#: 模型答案的限幅：承诺给调用方的就是「一段提示词 + 一行画风 + 少量标签」。
#: Go 常量名：``maxAnalysisPromptRunes`` / ``maxAnalysisTagRunes`` /
#: ``maxAnalysisTags`` / ``maxAnalysisWarnings`` / ``maxAnalysisWarningRune``。
MAX_ANALYSIS_PROMPT_RUNES = 6000
MAX_ANALYSIS_TAG_RUNES = 40
MAX_ANALYSIS_TAGS = 12
MAX_ANALYSIS_WARNINGS = 8
MAX_ANALYSIS_WARNING_RUNES = 300

#: 缓存顺序敏感的字段顺序（Go struct 声明顺序）。
_FIELDS = (
    "positive_prompt",
    "negative_prompt",
    "style_description",
    "style_tags",
    "warnings",
)


@dataclass
class ImageAnalysis:
    """图像分析模板的结构化结果。对应 Go ``ImageAnalysis``。

    JSON 键名与 Go struct tag 逐字一致。
    """

    positive_prompt: str = ""
    negative_prompt: str = ""
    style_description: str = ""
    style_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "style_description": self.style_description,
            "style_tags": list(self.style_tags),
            "warnings": list(self.warnings),
        }


def parse_image_analysis(text: str) -> ImageAnalysis:
    """解析一次结构化分析结果。对应 Go ``ParseImageAnalysis``。

    模型经常把 JSON 包在散文或代码围栏里，因此取答案中**第一个平衡的 JSON
    对象**，四周文本一律忽略（绝不当数据解释）。取不到、不是对象、
    ``positive_prompt`` 为空或超长时抛 :data:`atri_x.errors.ErrImageAnalysisEmpty`；
    JSON 本身损坏时抛 ``json.JSONDecodeError``（对齐 Go 的 ``json.Unmarshal`` 错误）。
    """
    raw = extract_json_object(clean_text(text))
    if raw == "":
        raise ErrImageAnalysisEmpty
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ErrImageAnalysisEmpty
    out = ImageAnalysis(
        positive_prompt=_field_text(decoded, "positive_prompt"),
        negative_prompt=_field_text(decoded, "negative_prompt"),
        style_description=_field_text(decoded, "style_description"),
        style_tags=_field_list(decoded, "style_tags"),
        warnings=_field_list(decoded, "warnings"),
    )
    out.positive_prompt = out.positive_prompt.strip()
    out.negative_prompt = out.negative_prompt.strip()
    out.style_description = out.style_description.strip()
    if out.positive_prompt == "":
        raise ErrImageAnalysisEmpty
    if len(out.positive_prompt) > MAX_ANALYSIS_PROMPT_RUNES:
        raise ErrImageAnalysisEmpty
    out.style_tags = clean_analysis_list(
        out.style_tags, MAX_ANALYSIS_TAGS, MAX_ANALYSIS_TAG_RUNES
    )
    out.warnings = clean_analysis_list(
        out.warnings, MAX_ANALYSIS_WARNINGS, MAX_ANALYSIS_WARNING_RUNES
    )
    return out


def _field_text(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        # Go 的 json.Unmarshal 对类型不符返回 UnmarshalTypeError（不是空结果）。
        raise ValueError(f"vision: analysis field {name} must be a string")
    return value


def _field_list(raw: dict[str, Any], name: str) -> list[str]:
    value = raw.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"vision: {name} must be a string array")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"vision: {name} must be a string array")
        out.append(item)
    return out


def clean_analysis_list(values: list[str] | None, limit: int, rune_limit: int) -> list[str]:
    """折叠空白、按小写去重、按限幅截断。对应 Go ``cleanAnalysisList``。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = " ".join((value or "").split())
        if value == "" or len(value) > rune_limit:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def extract_json_object(text: str) -> str:
    """返回 ``text`` 里第一个顶层 JSON 对象，取不到返回空串。

    对应 Go ``extractJSONObject``。它跟踪字符串字面量与转义，所以提示词里的
    花括号不会提前结束对象。
    """
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if in_string and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
        elif char == "{":
            if not in_string:
                depth += 1
        elif char == "}":
            if not in_string:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return ""
