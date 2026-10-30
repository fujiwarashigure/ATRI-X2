"""视觉 prompt 模板与文本清洗。

对应 Go：``internal/vision/prompt.go``。

模板文本是**契约**：四段 system 提示逐字照抄 Go 源码（含全角标点与代码围栏
说明），本模块不允许改写、翻译或重新断行。四个 ``*Version`` 常量参与缓存
指纹，改文本就必须同步改版本号。

1.1.6 起 system 模板可从桌面端「提示词」页覆盖（条目 id 见各构造函数）；
覆盖只改运行期文本，内置默认仍是这里的逐字原文，且缓存键包含 prompt 内容
哈希，所以覆盖后旧缓存条目自动 miss。

Python 侧新增第五套模板 :func:`image_attributes_prompt`（「画面属性描述」）：
它是两段式识图的第一段，视觉模型只输出**可观察属性**的紧凑 JSON，不下任何
身份/作品结论，结论留给拿到描述的主模型结合人设去下。它有自己的版本常量
:data:`AttributeDescriptionTemplateVersion`，因此旧的聊天描述缓存条目不会被
误当成属性描述复用。
"""

from __future__ import annotations

from typing import Any

from atri_x.promptlib import active as active_prompt_library
from atri_x.promptlib.defaults import (
    PROMPT_VISION_ATTRIBUTES,
    PROMPT_VISION_CHAT_DESCRIPTION,
    PROMPT_VISION_IMAGE_ANALYSIS,
    PROMPT_VISION_IMAGE_PROMPT,
)
from atri_x.vision.service_types import Prompt

__all__ = [
    "AttributeDescriptionTemplateVersion",
    "ChatDescriptionTemplateVersion",
    "ImageAnalysisTemplateVersion",
    "ImagePromptTemplateVersion",
    "PreprocessVersion",
    "chat_description",
    "clean_text",
    "image_analysis_prompt",
    "image_attributes_prompt",
    "image_prompt",
    "image_prompt_instruction",
    "normalize_image_prompt_language",
    "normalize_image_prompt_target",
]

#: 图像预处理算法版本。resize/重编码行为变化时必须递增，否则会复用由不同像素
#: 产出的旧描述。
PreprocessVersion = "vision-image-v1"

#: 绘图提示词模板版本。
ImagePromptTemplateVersion = "image-to-prompt.v1"

#: 内置 system 模板。用户可在桌面端「提示词」页覆盖（id 见下方函数）；
#: 缓存键含 prompt 内容哈希，覆盖后旧条目自动 miss（见 ``vision/service.py``）。
_IMAGE_PROMPT_SYSTEM = PROMPT_VISION_IMAGE_PROMPT

#: 结构化分析模板版本。它与绘图提示词模板**刻意分开**：缓存的分析结果与缓存
#: 的绘图提示词是两种结果，共用版本号会让其中一个被当成另一个返回。
ImageAnalysisTemplateVersion = "image-to-prompt.structured.v1"

_IMAGE_ANALYSIS_SYSTEM = PROMPT_VISION_IMAGE_ANALYSIS

#: 聊天兜底模板版本。同样与绘图提示词模板分开：聊天描述与绘图提示词是两种
#: 结果，不能共用缓存条目。
ChatDescriptionTemplateVersion = "chat-image-description.v1"

_CHAT_DESCRIPTION_SYSTEM = PROMPT_VISION_CHAT_DESCRIPTION


def image_prompt(
    target: str, language: str, *, library: Any = None
) -> Prompt:
    """构造绘图提示词模板。对应 Go ``ImagePrompt``。"""
    prompts = library if library is not None else active_prompt_library()
    return Prompt(
        version=ImagePromptTemplateVersion,
        system=prompts.text("vision.image_prompt", _IMAGE_PROMPT_SYSTEM),
        user=image_prompt_instruction(target, language),
    )


def image_prompt_instruction(target: str, language: str) -> str:
    """告诉视觉模型要的输出形态（不重复图片描述规则）。对应 Go ``imagePromptInstruction``。"""
    target_hint = "目标绘图模型 general：通用的自然语言描述，信息完整、可直接使用。"
    normalized = normalize_image_prompt_target(target)
    if normalized == "sdxl":
        target_hint = "目标绘图模型 SDXL：以逗号分隔的关键词/短语为主，重要元素靠前，信息密度高，避免长句。"
    elif normalized == "flux":
        target_hint = "目标绘图模型 Flux：以连贯的自然语言长句为主，明确主体、场景、构图、光线和风格之间的关系。"
    language_hint = "用中文输出提示词。"
    if normalize_image_prompt_language(language) == "en":
        language_hint = "Output the prompt in English."
    return "参考图片见下，请反推绘图提示词。\n" + target_hint + "\n" + language_hint


def normalize_image_prompt_target(value: str) -> str:
    """把任意取值折叠到 target 枚举。对应 Go ``NormalizeImagePromptTarget``。"""
    folded = (value or "").strip().lower()
    if folded == "sdxl":
        return "sdxl"
    if folded == "flux":
        return "flux"
    return "general"


def normalize_image_prompt_language(value: str) -> str:
    """把任意取值折叠到 language 枚举。对应 Go ``NormalizeImagePromptLanguage``。"""
    if (value or "").strip().lower() == "en":
        return "en"
    return "zh"


def image_analysis_prompt(
    target: str, language: str, *, library: Any = None
) -> "Prompt":
    """构造结构化分析模板。对应 Go ``ImageAnalysisPrompt``。

    模型只被要求说出它看得见的东西，并被明确禁止点名画师。哪个已知风格组合
    最接近是**事后**由本地画师串库回答的问题，所以一个看起来合理的画师名
    永远不会作为证据流进流水线。
    """
    prompts = library if library is not None else active_prompt_library()
    return Prompt(
        version=ImageAnalysisTemplateVersion,
        system=prompts.text("vision.image_analysis", _IMAGE_ANALYSIS_SYSTEM),
        user=_image_analysis_instruction(target, language),
    )


def _image_analysis_instruction(target: str, language: str) -> str:
    target_hint = "目标绘图模型 general：positive_prompt 用通用的自然语言描述，信息完整、可直接使用。"
    normalized = normalize_image_prompt_target(target)
    if normalized == "sdxl":
        target_hint = "目标绘图模型 SDXL：positive_prompt 以逗号分隔的关键词/短语为主，重要元素靠前，信息密度高，避免长句。"
    elif normalized == "flux":
        target_hint = "目标绘图模型 Flux：positive_prompt 以连贯的自然语言长句为主，明确主体、场景、构图、光线和风格之间的关系。"
    language_hint = "positive_prompt 与 style_description 用中文输出。"
    if normalize_image_prompt_language(language) == "en":
        language_hint = "Output positive_prompt and style_description in English."
    return "参考图片见下，请按 JSON 格式输出分析结果。\n" + target_hint + "\n" + language_hint


def chat_description(language: str, *, library: Any = None) -> Prompt:
    """文本模型拒图、调用方需要文字替身时用的模板。对应 Go ``ChatDescription``。"""
    instruction = "请把这张图片转写成客观、完整的文字描述。"
    if normalize_image_prompt_language(language) == "en":
        instruction = "Describe this image objectively and completely in English."
    prompts = library if library is not None else active_prompt_library()
    return Prompt(
        version=ChatDescriptionTemplateVersion,
        system=prompts.text("vision.chat_description", _CHAT_DESCRIPTION_SYSTEM),
        user=instruction,
    )


#: 「画面属性描述」模板版本（两段式识图的第一段）。它是 Python 侧新增的模板，
#: 与聊天描述模板**刻意分开**：属性描述要求紧凑 JSON，聊天描述要求一段自然语言，
#: 两者共用版本号会让一种结果被当成另一种返回。
AttributeDescriptionTemplateVersion = "chat-image-attributes.v1"

_ATTRIBUTE_DESCRIPTION_SYSTEM = PROMPT_VISION_ATTRIBUTES


def image_attributes_prompt(language: str, *, library: Any = None) -> Prompt:
    """两段式识图第一段用的「画面属性描述」模板。

    与 :func:`chat_description` 的区别是输出形态与约束：这里要求**紧凑 JSON**
    且只描述可观察属性（发色、瞳色、服装配色、纹样、徽章、配件、可见文字、
    画风、是否疑似 AI 生成、构图、人物数量、身份确定度），明令禁止点名角色
    与作品。是否认识图里这个人，留给拿到这段描述的**主模型**结合人设回答。
    """
    instruction = "请按 JSON 格式记录这张图片的画面属性。"
    if normalize_image_prompt_language(language) == "en":
        instruction = (
            "Record the observable visual attributes of this image as JSON "
            "(same fields, English values where the field is free text)."
        )
    prompts = library if library is not None else active_prompt_library()
    return Prompt(
        version=AttributeDescriptionTemplateVersion,
        system=prompts.text("vision.attributes", _ATTRIBUTE_DESCRIPTION_SYSTEM),
        user=instruction,
    )


def clean_text(value: str) -> str:
    """去掉部分模型会加的 markdown 代码围栏。对应 Go ``CleanText``。"""
    value = (value or "").strip()
    if not value.startswith("```"):
        return value
    value = value[3:]
    newline = value.find("\n")
    if newline >= 0:
        value = value[newline + 1 :]
    else:
        value = value.strip()
    value = value.strip()
    if value.endswith("```"):
        value = value[: -3]
    return value.strip()
