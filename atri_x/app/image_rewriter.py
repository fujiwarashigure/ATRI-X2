"""``image_generate`` 的可选 LLM 改写通道。

对应 Go：``internal/app/image_rewriter.go``。

要点：

* ``OptimizeRewriteModel`` 决定用哪个模型槽（``naming`` / ``compact`` / ``chat`` /
  ``work``，默认 ``work``），槽位没配 provider/model 时回落到 work 模型。
* 目标方言决定系统提示词：自然语言模型（GPT Image / Flux）用自然语言教义，
  加权标签模型（SD / NovelAI / tags）用标签教义。
* 标签教义**明确禁止**模型输出 ``(tag:1.2)`` 这类权重表达式 —— artist 字符串由
  程序按用户原样编译，模型自造的括号会静默改变用户的要求。

Python 侧 ``atri_x.tool.builtin`` 尚未导出 ``ImagePromptRewriter``，因此请求类型
用 :class:`atri_x.app.service_types.ImagePromptRewriteRequest`（字段与 Go 一致），
实际调用按属性读取，接受任何同形对象。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.dependencies import ModelClients
from atri_x.app.service_types import ImagePromptRewriteRequest
from atri_x.errors import InvalidError
from atri_x.llm import ChatRequest, LLMMessage, Role, text_segments

__all__ = [
    "ImagePromptRewriter",
    "imagePromptRewriter",
    "buildImagePromptRewriter",
    "build_image_prompt_rewriter",
    "imageRewriteSelection",
    "image_rewrite_selection",
    "imageRewriteSystemFor",
    "image_rewrite_system_for",
    "imageRewriteTargetLine",
    "image_rewrite_target_line",
    "dialectLabel",
    "dialect_label",
    "IMAGE_REWRITE_SYSTEM_NATURAL",
    "IMAGE_REWRITE_SYSTEM_TAGS",
]

#: 自然语言目标（GPT Image、Flux）的教义。
IMAGE_REWRITE_SYSTEM_NATURAL = """你是生图提示词工程师。把用户给的画面描述改写成一段更适合自然语言生图模型（例如 GPT Image、Flux）的完整提示词。
要求：
- 只输出提示词本身，不要解释、不要 markdown 代码块、不要引号；
- 严格保留用户原意、主体和角色身份，不新增无关元素；
- 优先自然语言描述，可补充镜头、光线、构图、材质、画质；
- 如果给了角色设定，保持外貌、服装、风格一致；
- 如果给了参考对话，从中提取关键场景信息，不要照抄无关闲聊；
- 语言以英文为主，专有名词可以保留中文；
- 长度不超过给定上限。"""

#: 加权标签目标（Stable Diffusion、NovelAI）的教义。
IMAGE_REWRITE_SYSTEM_TAGS = """你是绘图标签工程师。目标模型使用以逗号分隔的标签提示词，不是自然语言长句。
要求：
- 只输出标签串本身，用逗号加空格分隔，不要解释、不要 markdown 代码块、不要引号；
- 每个标签是一个单词或短语，不要写成完整句子，不要写序号；
- 严格保留用户已经给出的标签原文以及它们的先后顺序，不要改写、不要翻译、不要重新排序；
- 可以补充与画面一致的质量、镜头、光线、构图、材质标签；
- 如果给了角色设定，把外貌、服装、风格转写成标签；
- 如果给了参考对话，只提取能变成标签的场景信息；
- 绝对不要输出 (tag:1.2) 这类权重表达式，也不要输出括号或方括号分组：权重由程序按用户原样单独处理，你输出括号会破坏它；
- 长度不超过给定上限。"""

#: 默认输出上限（rune）。
DEFAULT_MAX_RUNES = 4000

#: 与 Go 的 ``Temperature: 0.4`` / ``MaxTokens: 700`` 一致。
REWRITE_TEMPERATURE = 0.4
REWRITE_MAX_TOKENS = 700


class ImagePromptRewriter:
    """执行 ``image_generate`` 的可选 LLM 改写。对应 Go ``imagePromptRewriter``。"""

    def __init__(self, client: Any = None, model: str = "") -> None:
        self.client = client
        self.model = model

    async def rewrite_image_prompt(self, ctx: Any, req: Any) -> str:
        """对应 Go ``RewriteImagePrompt``。"""
        request = ImagePromptRewriteRequest.from_any(req)
        if self.client is None or not (self.model or "").strip():
            raise InvalidError("rewrite model is not configured")
        max_runes = request.max_runes or DEFAULT_MAX_RUNES
        prompt = build_rewrite_prompt(request, max_runes)
        stream = self.client.chat_stream(
            ctx,
            ChatRequest(
                model=self.model,
                messages=[
                    LLMMessage(
                        role=Role.SYSTEM,
                        segments=text_segments(
                            image_rewrite_system_for(
                                request.target_dialect, request.model_family
                            )
                        ),
                    ),
                    LLMMessage(role=Role.USER, segments=text_segments(prompt)),
                ],
                temperature=REWRITE_TEMPERATURE,
                max_tokens=REWRITE_MAX_TOKENS,
            ),
        )
        out: list[str] = []
        usage = None
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            if getattr(chunk, "error", None) is not None:
                raise chunk.error
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            out.append(getattr(chunk, "delta_content", "") or "")
        value = "".join(out).strip()
        if not value:
            raise InvalidError("rewrite returned an empty result")
        # 旁路用量：画师提示词改写也是一次真实调用。
        from atri_x.agent.call_reason import side_reason
        from atri_x.llm.usage_sink import record_side_usage

        record_side_usage(
            reason=side_reason("image_rewrite"), model=self.model, usage=usage
        )
        return value

    # Go 命名别名。
    RewriteImagePrompt = rewrite_image_prompt


def build_rewrite_prompt(req: ImagePromptRewriteRequest, max_runes: int) -> str:
    """拼用户消息。对应 Go ``RewriteImagePrompt`` 里构造 prompt 的那几段。"""
    parts: list[str] = []
    target = image_rewrite_target_line(req)
    if target:
        parts.append(target + "\n")
    parts.append("[场景描述]\n" + (req.scene or "").strip())
    if (req.character_name or "").strip() or (req.character_prompt or "").strip():
        block = "\n\n[角色]\n" + (req.character_name or "").strip()
        value = (req.character_prompt or "").strip()
        if value:
            block += "\n" + value
        parts.append(block)
    value = (req.context_text or "").strip()
    if value:
        parts.append("\n\n" + value)
    value = (req.library_hints or "").strip()
    if value:
        parts.append("\n\n[TAG 库参考]\n" + value)
    if req.protected_blocks:
        # 这些片段被点名，模型就不会试图复现它们，读请求的人也能看到已处理部分。
        parts.append(
            "\n\n[已单独处理的片段：不要输出它们，也不要改写]\n"
            + "、".join(req.protected_blocks)
        )
    parts.append(f"\n\n[输出]\n直接输出改写后的提示词，不超过 {max_runes} 个字符。")
    return "".join(parts)


def build_image_prompt_rewriter(cfg: Any, models: ModelClients) -> Any:
    """挑改写模型槽；没配就返回 ``None``，生图回落到规则优化器。

    对应 Go ``buildImagePromptRewriter``。
    """
    if cfg is None:
        return None
    selection = image_rewrite_selection(cfg)
    client = (models.by_provider or {}).get(selection.provider)
    if client is None or not (selection.model or "").strip():
        return None
    return ImagePromptRewriter(client, selection.model)


def image_rewrite_selection(cfg: Any) -> Any:
    """按 ``optimize_rewrite_model`` 解析模型槽。对应 Go ``imageRewriteSelection``。"""
    from atri_x.config import ModelSelection
    from atri_x.storage import SessionModeChat, SessionModeWork

    mode_models = getattr(cfg, "mode_models", None) or {}
    work = mode_models.get(SessionModeWork) or ModelSelection()

    def fallback(selection: Any) -> Any:
        if not (
            str(getattr(selection, "provider", "") or "").strip()
            and str(getattr(selection, "model", "") or "").strip()
        ):
            return work
        return selection

    image_generation = getattr(cfg, "image_generation", None)
    slot = str(
        getattr(image_generation, "optimize_rewrite_model", "") or ""
    ).strip().lower()
    if slot == "naming":
        return fallback(getattr(cfg, "naming_model", None))
    if slot == "compact":
        return fallback(getattr(cfg, "compact_model", None))
    if slot == "chat":
        return fallback(mode_models.get(SessionModeChat))
    return work


def image_rewrite_system_for(dialect: str, family: str) -> str:
    """按目标挑改写教义。对应 Go ``imageRewriteSystemFor``。

    未声明方言的 profile 保持自然语言默认值 —— 这是 OpenAI 兼容 images 端点的期望。
    """
    from atri_x.artist.types import Dialect, normalize_dialect

    normalized = normalize_dialect(dialect)
    if normalized == Dialect.AUTO:
        lowered = (family or "").strip().lower()
        if lowered in {"sdxl", "sd", "stable_diffusion", "novelai", "nai"}:
            normalized = Dialect.SD_WEIGHTED
        else:
            normalized = Dialect.NATURAL
    if normalized in (Dialect.SD_WEIGHTED, Dialect.NAI, Dialect.TAGS):
        return IMAGE_REWRITE_SYSTEM_TAGS
    return IMAGE_REWRITE_SYSTEM_NATURAL


#: 与 Go ``Dialect.Label`` 逐字一致。``atri_x/artist/types.py`` 尚未移植这个
#: 方法（它只提供 ``normalize_dialect``），因此 app 层先自带一份；Lead 对接时
#: 建议把它上提到 ``atri_x.artist.types`` 并删除这里的副本。
_DIALECT_LABELS = {
    "natural": "自然语言",
    "sd_weighted": "SD 加权标签",
    "nai": "NovelAI 加权标签",
    "tags": "纯标签",
}


def dialect_label(dialect: Any) -> str:
    """对应 Go ``Dialect.Label``。"""
    value = getattr(dialect, "value", dialect)
    return _DIALECT_LABELS.get(str(value), "跟随 profile")


def image_rewrite_target_line(req: ImagePromptRewriteRequest) -> str:
    """描述输出形态的那一行。对应 Go ``imageRewriteTargetLine``。"""
    from atri_x.artist.types import Dialect, normalize_dialect

    parts: list[str] = []
    model = (req.target_model or "").strip()
    if model:
        parts.append(model)
    dialect = normalize_dialect(req.target_dialect)
    if dialect != Dialect.AUTO:
        parts.append(dialect_label(dialect))
    if not parts:
        return ""
    return "目标模型：" + " / ".join(parts)


imagePromptRewriter = ImagePromptRewriter
buildImagePromptRewriter = build_image_prompt_rewriter
imageRewriteSelection = image_rewrite_selection
imageRewriteSystemFor = image_rewrite_system_for
imageRewriteTargetLine = image_rewrite_target_line
dialectLabel = dialect_label
