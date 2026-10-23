"""结构化图像分析结果的解析与限幅。

对应 Go：``internal/vision/analysis.go``。

这一层把「像素级猜测」挡在画师流水线之外：模型只描述它真正看得见的东西，
是否与某个已知风格组合相似由**本地**画师串库检索决定。分析结果从不声称知道
是哪位画师画的。

Python 侧新增 :class:`ImageAttributes`（两段式识图第一段的结果）：它比
:class:`ImageAnalysis` 更窄——只有可观察属性与「身份确定度」，没有作品名、
角色名和画师名，所以它的输出可以安全地交给主模型去结合人设回答。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import ErrImageAnalysisEmpty, ImageAnalysisEmptyError
from atri_x.vision.prompt import clean_text

__all__ = [
    "ImageAnalysis",
    "ImageAttributes",
    "IDENTITY_CONFIDENCE_HIGH",
    "IDENTITY_CONFIDENCE_LOW",
    "IDENTITY_CONFIDENCE_MEDIUM",
    "MAX_ANALYSIS_PROMPT_RUNES",
    "MAX_ANALYSIS_TAGS",
    "MAX_ANALYSIS_TAG_RUNES",
    "MAX_ANALYSIS_WARNINGS",
    "MAX_ANALYSIS_WARNING_RUNES",
    "MAX_ATTRIBUTE_DESCRIPTION_RUNES",
    "MAX_ATTRIBUTE_FIELD_RUNES",
    "MAX_ATTRIBUTE_TEXT_RUNES",
    "analyze_attributes_text",
    "attributes_summary",
    "clean_analysis_list",
    "extract_json_object",
    "normalize_identity_confidence",
    "parse_image_analysis",
    "parse_image_attributes",
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


#: 「画面属性描述」各文本字段的限幅。属性字段只该是一两个词，给足余量但不许
#: 变成第二段自由发挥；``description`` 单独放宽。
MAX_ATTRIBUTE_FIELD_RUNES = 120
MAX_ATTRIBUTE_DESCRIPTION_RUNES = 1200
#: 整段属性描述（渲染成给主模型的文本后）的上限。
MAX_ATTRIBUTE_TEXT_RUNES = 2000

IDENTITY_CONFIDENCE_LOW = "low"
IDENTITY_CONFIDENCE_MEDIUM = "medium"
IDENTITY_CONFIDENCE_HIGH = "high"

#: 两段式第一段结果里参与渲染的属性字段（顺序即渲染顺序）。
_ATTRIBUTE_FIELDS = (
    ("hair_color", "发色"),
    ("eye_color", "瞳色"),
    ("hairstyle", "发型"),
    ("outfit_colors", "服装配色"),
    ("trim", "滚边纹样"),
    ("badges", "徽章标志"),
    ("accessories", "配件"),
    ("visible_text", "可见文字"),
    ("art_style", "画风"),
    ("scene", "构图背景"),
)


@dataclass
class ImageAttributes:
    """两段式识图第一段的结果：只记录可观察属性，不含任何身份结论。

    每个字段都只是「画面里能直接看到的东西」；看不懂的字段留空字符串，
    绝不用猜测填满。``identity_confidence`` 表达的是「能否仅凭画面确定具体
    角色」的程度，:data:`IDENTITY_CONFIDENCE_LOW` 才会触发以图搜图兜底。
    """

    hair_color: str = ""
    eye_color: str = ""
    hairstyle: str = ""
    outfit_colors: str = ""
    trim: str = ""
    badges: str = ""
    accessories: str = ""
    visible_text: str = ""
    art_style: str = ""
    ai_generated: bool = False
    scene: str = ""
    character_count: int = 0
    identity_confidence: str = IDENTITY_CONFIDENCE_LOW
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        """按模板字段顺序输出（``ai_generated`` 是布尔、数量是整数）。"""
        out: dict[str, Any] = {name: getattr(self, name) for name, _ in _ATTRIBUTE_FIELDS}
        out["ai_generated"] = bool(self.ai_generated)
        out["character_count"] = int(self.character_count)
        out["identity_confidence"] = self.identity_confidence
        out["description"] = self.description
        return out


def parse_image_attributes(text: str) -> ImageAttributes:
    """解析一段「画面属性描述」。模型答案里的散文/代码围栏一律忽略。

    取不到 JSON 对象、不是对象、或**所有属性字段都为空**时抛
    :data:`atri_x.errors.ErrImageAnalysisEmpty`；JSON 本身损坏时抛
    ``json.JSONDecodeError``。类型不符的字段按空值处理而不是整体失败——属性
    描述是尽力而为的观测，缺一两个字段不该让整张图变成「识别失败」。
    """
    raw = extract_json_object(clean_text(text))
    if raw == "":
        raise ErrImageAnalysisEmpty
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ErrImageAnalysisEmpty
    out = ImageAttributes(
        hair_color=_text_field(decoded, "hair_color", MAX_ATTRIBUTE_FIELD_RUNES),
        eye_color=_text_field(decoded, "eye_color", MAX_ATTRIBUTE_FIELD_RUNES),
        hairstyle=_text_field(decoded, "hairstyle", MAX_ATTRIBUTE_FIELD_RUNES),
        outfit_colors=_text_field(decoded, "outfit_colors", MAX_ATTRIBUTE_FIELD_RUNES),
        trim=_text_field(decoded, "trim", MAX_ATTRIBUTE_FIELD_RUNES),
        badges=_text_field(decoded, "badges", MAX_ATTRIBUTE_FIELD_RUNES),
        accessories=_text_field(decoded, "accessories", MAX_ATTRIBUTE_FIELD_RUNES),
        visible_text=_text_field(decoded, "visible_text", MAX_ATTRIBUTE_FIELD_RUNES),
        art_style=_text_field(decoded, "art_style", MAX_ATTRIBUTE_FIELD_RUNES),
        ai_generated=_bool_field(decoded, "ai_generated"),
        scene=_text_field(decoded, "scene", MAX_ATTRIBUTE_FIELD_RUNES),
        character_count=_int_field(decoded, "character_count"),
        identity_confidence=normalize_identity_confidence(
            _text_field(decoded, "identity_confidence", MAX_ATTRIBUTE_FIELD_RUNES)
        ),
        description=_text_field(
            decoded, "description", MAX_ATTRIBUTE_DESCRIPTION_RUNES
        ),
    )
    if not any(
        (out.hair_color, out.eye_color, out.hairstyle, out.outfit_colors, out.trim,
         out.badges, out.accessories, out.visible_text, out.art_style, out.scene,
         out.description)
    ):
        raise ErrImageAnalysisEmpty
    return out


def normalize_identity_confidence(value: str) -> str:
    """把任意取值折叠到 ``low`` / ``medium`` / ``high``；未知按 ``low``。"""
    folded = (value or "").strip().lower()
    if folded in (IDENTITY_CONFIDENCE_MEDIUM, IDENTITY_CONFIDENCE_HIGH):
        return folded
    return IDENTITY_CONFIDENCE_LOW


def analyze_attributes_text(text: str) -> tuple[ImageAttributes | None, bool]:
    """宽容地解析一段属性描述，返回 ``(属性, 是否解析失败)``。

    与 :func:`parse_image_attributes` 的区别是**不抛异常**：聊天前置路径拿它做
    「解析不出来就把原文当描述」的降级，因此这里只回报是否失败，由调用方决定
    记不记 ``parse_failed``。
    """
    try:
        return parse_image_attributes(text), False
    except (ImageAnalysisEmptyError, ValueError):
        return None, True


def attributes_summary(attributes: ImageAttributes | None) -> str:
    """把属性渲染成一行「发色=黑；瞳色=…」，供主模型读。

    空字段直接跳过（不写「未知」占位，避免主模型把占位当成观测结果）。
    """
    if attributes is None:
        return ""
    parts: list[str] = []
    for name, label in _ATTRIBUTE_FIELDS:
        value = str(getattr(attributes, name, "") or "").strip()
        if value:
            parts.append(f"{label}={value}")
    if attributes.character_count > 0:
        parts.append(f"人物数量={int(attributes.character_count)}")
    if attributes.ai_generated:
        parts.append("疑似 AI 生成")
    return "；".join(parts)


def _text_field(raw: dict[str, Any], name: str, rune_limit: int) -> str:
    """取一个文本属性字段：折叠空白、限幅；类型不符按空值处理。"""
    value = raw.get(name)
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    if len(value) > rune_limit:
        value = value[:rune_limit]
    return value


def _bool_field(raw: dict[str, Any], name: str) -> bool:
    """宽容地读布尔字段：接受 ``true/false`` 与 ``"true"/"yes"/"1"``。"""
    value = raw.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "是", "有")
    return False


def _int_field(raw: dict[str, Any], name: str) -> int:
    """读整数计数：负数与非数字按 0 处理。"""
    value = raw.get(name)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


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
