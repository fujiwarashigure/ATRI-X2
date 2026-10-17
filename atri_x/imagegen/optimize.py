"""提示词库规则优化器（不调用模型）。

对应 Go：``internal/imagegen/optimize.go``。

本模块只做确定性改写：识别用途 / 分类，追加锚点与画幅，选出少量相关负面词，
并按 ``term_mode`` 决定输出「短语锚点」还是「逗号分隔的单词 tag 串」。

低成本的**模型**改写不在这里：上游把它放在 ``internal/app/image_rewriter.go``
（``ImagePromptRewriteRequest`` + ``imageRewriteSystemFor`` 两套系统提示词，
经 ``llm.ChatStream`` 调用），属 ``atri_x/app/*``。本包为它提供的接口是
``Config.optimize_rewrite`` / ``optimize_rewrite_model`` / ``optimize_rewrite_min_runes``
三个开关与 :meth:`PromptLibrary.library_hints` 生成的库提示文本。

实现体写成以库为首参的自由函数（``optimize(library, scene, opts)``），由
:class:`atri_x.imagegen.library.PromptLibrary` 上的同名方法转发，以便按 Go 的
文件边界拆分模块。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注，避免与 library 循环导入
    from atri_x.imagegen.library import PromptEntry, PromptLibrary, UseCasePreset

__all__ = [
    "EDIT_GUARD",
    "OptimizeOptions",
    "OptimizeResult",
    "clean_ratio",
    "contains_any",
    "contains_fold",
    "dedupe_fold",
    "edit_trigger",
    "has_aspect_ratio",
    "match_entry",
    "match_key_score",
    "match_preset",
    "matchable_terms",
    "matchable_word",
    "optimize",
    "optimize_tags",
    "people_trigger",
    "photo_trigger",
    "product_trigger",
    "ratio_text",
    "select_negatives",
    "split_tag_words",
    "squash_commas",
    "text_trigger",
    "useful_anchor",
    "valid_tag_word",
]

#: 改图请求追加的护栏句，防止模型重画整张图。
EDIT_GUARD = (
    "Keep the subject, pose, composition, lighting, and background unchanged; "
    "do not redraw the whole image"
)

#: ``\b`` 用 ASCII 语义，与 Go RE2 的 ``\b``（ASCII 单词边界）一致：CJK 紧邻
#: 数字时 Python 默认的 Unicode ``\b`` 会不匹配，Go 会匹配。
_RATIO_PATTERN = re.compile(r"\b\d{1,2}\s*[:：]\s*\d{1,2}\b", re.ASCII)

_RATIO_WORDS = ("widescreen", "vertical", "square", "landscape", "portrait aspect", "aspect ratio")

#: 每个负面词组最多取几条。
_NEGATIVE_CAPS = {
    "quality": 3,
    "exposure": 2,
    "safety": 3,
    "anatomy": 2,
    "face": 2,
    "duplication": 1,
    "text": 3,
    "composition": 2,
    "perspective": 1,
    "style": 2,
}
_NEGATIVE_DEFAULT_CAP = 2

_TAG_STOPLIST = frozenset(
    {
        "photo",
        "image",
        "picture",
        "style",
        "generate",
        "create",
        "using",
        "keep",
        "dpi",
        "safe",
        "ready",
    }
)

_ANCHOR_WORD_STOPLIST = frozenset(
    {
        "detailed", "realistic", "quality", "background", "natural", "soft", "light",
        "lighting", "color", "colors", "style", "photo", "image", "high", "modern",
        "sharp", "focus", "texture", "clean", "composition", "premium", "beautiful",
        "elegant", "summer", "winter", "spring", "autumn", "season", "seasons", "four",
        "panel", "subject", "scene", "action", "palette", "aspect", "ratio", "shape",
        "form", "object", "item", "element", "part", "area", "shot", "depth", "field",
        "wide", "angle", "view", "medium", "full", "size", "type", "kind",
    }
)

_PEOPLE_TRIGGERS = (
    "人物", "人像", "头像", "写真", "肖像", "角色", "少女", "女孩", "男孩", "男人", "女人",
    "情侣", "全家", "模特", "肖像照", "catgirl", "portrait", "girl", "boy", "woman", "man",
    "character", "person", "elf", "cyborg", "warrior", "knight", "mage",
)
_TEXT_TRIGGERS = (
    "文字", "标题", "字体", "海报", "封面", "logo", "ui", "banner", "typography",
    "text", "title", "caption", "label", "headline",
)
_PRODUCT_TRIGGERS = (
    "产品", "商品", "电商", "主图", "详情页", "广告", "product", "e-commerce", "packshot",
    "advertisement", "brand", "logo",
)
_PHOTO_TRIGGERS = (
    "摄影", "写实", "照片", "人像", "photograph", "photo", "realistic", "photorealistic",
    "editorial",
)
_EDIT_TRIGGERS = (
    "换背景", "背景换", "替换成", "换成", "换衣", "换发色", "改表情", "局部", "重绘",
    "扩图", "放大", "修复", "去物体", "去掉", "去除", "移除", "加雪景", "白天变夜",
    "edit this image", "inpaint", "outpaint", "remove ", "replace ", "change the ",
    "only change",
)


@dataclass(slots=True)
class OptimizeOptions:
    """规则优化器允许追加多少内容。对应 Go ``OptimizeOptions``。"""

    max_anchors: int = 0
    max_negatives: int = 0
    max_added_runes: int = 0
    max_tags: int = 0
    #: ``"phrase"``（默认）或 ``"tag"``。tag 模式追加紧凑的单字 tag 串。
    term_mode: str = ""


@dataclass(slots=True)
class OptimizeResult:
    """规则优化器输出。对应 Go ``OptimizeResult``。"""

    prompt: str = ""
    negative: str = ""
    matched: str = ""
    anchors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# rune 类别助手（Go 的 unicode.IsLetter / unicode.IsDigit）
# ---------------------------------------------------------------------------

def _is_letter(ch: str) -> bool:
    """对应 Go ``unicode.IsLetter``（Unicode 类别 L*）。"""
    return unicodedata.category(ch).startswith("L")


def _is_digit(ch: str) -> bool:
    """对应 Go ``unicode.IsDigit``（Unicode 类别 Nd，不含上标等 No）。"""
    return unicodedata.category(ch) == "Nd"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def optimize(
    library: "PromptLibrary | None", scene: str, opts: OptimizeOptions | None = None
) -> OptimizeResult:
    """用内嵌提示词库改写一段用户场景描述。对应 Go ``PromptLibrary.Optimize``。"""
    options = opts or OptimizeOptions()
    original = str(scene or "").strip()
    result = OptimizeResult(prompt=original)
    if library is None or not original:
        return result
    max_anchors = options.max_anchors if options.max_anchors > 0 else 4
    max_negatives = options.max_negatives if options.max_negatives > 0 else 10

    folded = original.lower()
    if str(options.term_mode or "").strip().lower() == "tag":
        return optimize_tags(library, original, folded, options, max_negatives)

    added: list[str] = []
    preset, preset_ok = match_preset(library, folded)
    if preset_ok and preset is not None:
        result.matched = "use=" + preset.use
        if not has_aspect_ratio(original):
            ratio = ratio_text(preset.ratio)
            if ratio:
                added.append(ratio)
        supplement = str(preset.supplement or "").strip()
        if supplement and not contains_fold(folded, supplement):
            added.append(supplement)

    anchors: list[str] = []
    entry, entry_ok, allow_anchors = match_entry(library, folded)
    if entry_ok and entry is not None:
        if result.matched:
            result.matched += "; "
        result.matched += "category=" + entry.category + "/" + entry.scene
        for anchor in (entry.anchors if allow_anchors else []):
            if len(anchors) >= max_anchors:
                break
            if not useful_anchor(anchor) or contains_fold(folded, anchor) or contains_any(added, anchor):
                continue
            anchors.append(anchor)
    if entry_ok and allow_anchors and len(anchors) < max_anchors and entry is not None:
        extra = 0
        for term in matchable_terms(entry):
            if len(anchors) >= max_anchors or extra >= 2:
                break
            if contains_fold(folded, term) or contains_any(added, term) or contains_any(anchors, term):
                continue
            anchors.append(term)
            extra += 1
    if not anchors:
        for keyword in library.keyword_group("画质"):
            if contains_fold(folded, keyword):
                continue
            anchors.append(keyword)
            break
    if anchors:
        added.extend(anchors)
        result.anchors = list(anchors)
    added = dedupe_fold(added)
    if edit_trigger(folded):
        added.append(EDIT_GUARD)
    result.negative = ", ".join(select_negatives(library, folded, max_negatives))

    cleaned = [str(item).strip().rstrip(".") for item in added]
    added_text = squash_commas(", ".join(cleaned).strip())
    max_added = options.max_added_runes
    if max_added > 0 and len(added_text) > max_added:
        added_text = added_text[:max_added].strip()
    if added_text:
        result.prompt = (original + " " + added_text).strip()
    return result


def optimize_tags(
    library: "PromptLibrary",
    original: str,
    folded: str,
    opts: OptimizeOptions,
    max_negatives: int,
) -> OptimizeResult:
    """构造紧凑的单字 tag 串。对应 Go ``optimizeTags``。"""
    result = OptimizeResult(prompt=original)
    max_tags = opts.max_tags if opts.max_tags > 0 else 12
    ratio = ""
    supplement = ""
    preset, preset_ok = match_preset(library, folded)
    if preset_ok and preset is not None:
        result.matched = "use=" + preset.use
        if not has_aspect_ratio(original):
            ratio = clean_ratio(preset.ratio)
        supplement = str(preset.supplement or "")
    entry, entry_ok, allow_anchors = match_entry(library, folded)
    if entry_ok and entry is not None:
        if result.matched:
            result.matched += "; "
        result.matched += "category=" + entry.category + "/" + entry.scene

    weights: dict[str, int] = {}
    counts: dict[str, int] = {}

    def record(word: str, weight: int) -> None:
        if weight > weights.get(word, 0):
            weights[word] = weight
        if counts.get(word, 0) == 0:
            counts[word] = library.vocabulary_count(word)

    # 条目里的 terms 已经是单词。
    if entry_ok and allow_anchors and entry is not None:
        added = 0
        for term in entry.terms:
            if added >= 6:
                break
            if not valid_tag_word(term) or contains_fold(folded, term):
                continue
            record(term, 30)
            added += 1
    # 用途补充：只保留有意义的词（丢掉数字与短噪声）。
    if supplement:
        added = 0
        for word in split_tag_words(supplement):
            if added >= 4:
                break
            if len(word) < 4 or contains_fold(folded, word):
                continue
            record(word, 25)
            added += 1
    matched = entry_ok or bool(str(result.matched or "").strip())
    if matched or not edit_trigger(folded):
        # 画质组是中性的；其它关键词组故意不取，因为盲选出的 "photorealistic"
        # 或 "backlight" 会和命中的场景冲突。
        added = 0
        for item in library.keyword_group("画质"):
            if added >= 2:
                break
            if " " in item or not valid_tag_word(item) or contains_fold(folded, item.lower()):
                continue
            record(str(item).strip().lower(), 15)
            added += 1

    candidates = sorted(
        ((word, weight, counts.get(word, 0)) for word, weight in weights.items()),
        key=lambda item: (-item[1], -item[2], item[0]),
    )

    tags: list[str] = []
    if ratio:
        tags.append(ratio)
    for word, _weight, _count in candidates:
        if len(tags) >= max_tags:
            break
        tags.append(word)
    result.tags = tags

    added_text = ", ".join(tags)
    if edit_trigger(folded):
        added_text = EDIT_GUARD if not added_text else added_text + ", " + EDIT_GUARD
    max_added = opts.max_added_runes
    if max_added > 0 and len(added_text) > max_added:
        added_text = added_text[:max_added].strip()
    if added_text:
        result.prompt = (original + " " + added_text).strip()
    result.negative = ", ".join(select_negatives(library, folded, max_negatives))
    return result


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def clean_ratio(ratio: str) -> str:
    """取画幅串的第一种写法（去掉「或 / | ／」后面的备选）。对应 Go ``cleanRatio``。"""
    text = str(ratio or "").strip()
    if not text:
        return ""
    for sep in ("或", "|", "／", "/"):
        index = text.find(sep)
        if index > 0:
            text = text[:index].strip()
    return text


def valid_tag_word(word: str) -> bool:
    """是否是可单独成 tag 的单词。对应 Go ``validTagWord``。"""
    text = str(word or "").strip().lower()
    if len(text) < 3 or text in _TAG_STOPLIST:
        return False
    has_letter = False
    for ch in text:
        if _is_letter(ch):
            has_letter = True
            continue
        if not _is_digit(ch) and ch != "-":
            return False
    return has_letter


def split_tag_words(text: str) -> list[str]:
    """切出单词级 token（小写、去重、过滤停用词）。对应 Go ``splitTagWords``。"""
    out: list[str] = []
    seen: set[str] = set()
    current: list[str] = []

    def flush() -> None:
        word = "".join(current).strip("'-").lower()
        current.clear()
        if len(word) < 3 or word in _TAG_STOPLIST or word in seen or not valid_tag_word(word):
            return
        seen.add(word)
        out.append(word)

    for ch in str(text or ""):
        if _is_letter(ch) or _is_digit(ch) or ch == "-" or ch == "'":
            current.append(ch)
            continue
        flush()
    flush()
    return out


def match_preset(library: "PromptLibrary", folded: str) -> tuple["UseCasePreset | None", bool]:
    """选出得分最高的用途预设。对应 Go ``matchPreset``。"""
    best = None
    best_score = 0
    for preset in library.presets:
        for key in preset.keys:
            score = match_key_score(folded, key, 4)
            if score > best_score:
                best, best_score = preset, score
        for key in preset.scene_keys:
            score = match_key_score(folded, key, 0)
            if score > best_score:
                best, best_score = preset, score
    return best, best_score > 0


def match_key_score(folded: str, key: str, bonus: int) -> int:
    """键命中得分（键长 + 奖励）。对应 Go ``matchKeyScore``。"""
    text = str(key or "").strip().lower()
    if not text or text not in folded:
        return 0
    return len(text) + bonus


def match_entry(library: "PromptLibrary", folded: str) -> tuple["PromptEntry | None", bool, bool]:
    """选出得分最高的提示词条目，并给出是否允许追加锚点。对应 Go ``matchEntry``。"""
    best = None
    best_score = 0
    best_allow = False
    for entry in library.entries:
        score = 0
        allow = False
        for key in entry.keys:
            text = str(key or "").strip().lower()
            if not text or text not in folded:
                continue
            score += len(text)
            allow = True
        if not allow:
            # 泛化键（头像/海报/主图…）只在没有更强的命中时才计分。
            for key in entry.generic_keys:
                text = str(key or "").strip().lower()
                if text and text in folded:
                    score += 1
                    break
        matched_terms = 0
        for word in matchable_terms(entry):
            if matched_terms >= 3:
                break
            if word not in folded:
                continue
            score += len(word) + 2
            matched_terms += 1
            allow = True
        if allow and entry.category != "通用" and score > 0:
            score += 1
        if score > best_score:
            best = entry
            best_score = score
            best_allow = allow
    return best, best_score > 0, best_allow


def matchable_terms(entry: "PromptEntry") -> list[str]:
    """条目的单词 terms 加上其短语锚点里的单词（去重、过滤）。对应 Go ``matchableTerms``。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        text = str(word or "").strip().strip(",.;:!?()[]").lower()
        if not matchable_word(text) or text in seen:
            return
        seen.add(text)
        out.append(text)

    for term in entry.terms:
        add(term)
    for anchor in entry.anchors:
        for word in str(anchor or "").split():
            add(word)
    return out


def matchable_word(word: str) -> bool:
    """单词是否够长且不在锚点停用表里。对应 Go ``matchableWord``。"""
    return len(word) >= 6 and word not in _ANCHOR_WORD_STOPLIST


def select_negatives(library: "PromptLibrary", folded: str, max_items: int) -> list[str]:
    """按触发词挑出相关负面词。对应 Go ``selectNegatives``。"""
    keys = ["quality"]
    if people_trigger(folded):
        keys.extend(["anatomy", "face", "duplication"])
    if text_trigger(folded):
        keys.append("text")
    if product_trigger(folded):
        keys.append("composition")
    if photo_trigger(folded):
        keys.extend(["style", "exposure"])
    keys.append("safety")

    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        cap = _NEGATIVE_CAPS.get(key, _NEGATIVE_DEFAULT_CAP)
        if cap <= 0:
            cap = _NEGATIVE_DEFAULT_CAP
        count = 0
        for item in library.negative_items(key):
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
            count += 1
            if count >= cap:
                break
    if max_items > 0 and len(out) > max_items:
        out = out[:max_items]
    return out


# ---------------------------------------------------------------------------
# 触发词与文本助手
# ---------------------------------------------------------------------------

def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if keyword.lower() in text:
            return True
    return False


def edit_trigger(folded: str) -> bool:
    """是否是改图请求。对应 Go ``editTrigger``。"""
    return _contains_any_keyword(folded, _EDIT_TRIGGERS)


def people_trigger(folded: str) -> bool:
    return _contains_any_keyword(folded, _PEOPLE_TRIGGERS)


def text_trigger(folded: str) -> bool:
    return _contains_any_keyword(folded, _TEXT_TRIGGERS)


def product_trigger(folded: str) -> bool:
    return _contains_any_keyword(folded, _PRODUCT_TRIGGERS)


def photo_trigger(folded: str) -> bool:
    return _contains_any_keyword(folded, _PHOTO_TRIGGERS)


def contains_fold(text: str, sub: str) -> bool:
    """``sub`` 的小写是否出现在 ``text``（已 fold）里。对应 Go ``containsFold``。"""
    needle = str(sub or "").strip().lower()
    return bool(needle) and needle in text


def contains_any(values: list[str], sub: str) -> bool:
    """任一值里是否包含 ``sub``。对应 Go ``containsAny``。"""
    needle = str(sub or "").strip().lower()
    if not needle:
        return False
    return any(needle in str(value).lower() for value in values)


def has_aspect_ratio(text: str) -> bool:
    """文本里是否已经有画幅描述。对应 Go ``hasAspectRatio``。"""
    value = str(text or "")
    if _RATIO_PATTERN.search(value):
        return True
    folded = value.lower()
    return any(word in folded for word in _RATIO_WORDS)


def ratio_text(ratio: str) -> str:
    """把画幅串展开成短语。对应 Go ``ratioText``。"""
    text = str(ratio or "").strip()
    if text == "1:1":
        return "1:1 square composition"
    if text in ("3:4", "4:5"):
        return text + " vertical portrait composition"
    if text == "2:3":
        return "2:3 vertical composition"
    if text == "9:16":
        return "9:16 vertical composition"
    if text == "16:9":
        return "16:9 widescreen composition"
    if text in ("21:9", "2.39:1"):
        return text + " cinematic widescreen composition"
    if text in ("3:2", "5:4"):
        return text + " composition"
    if not text:
        return ""
    return text + " composition"


def useful_anchor(anchor: str) -> bool:
    """锚点是否值得追加（太短或纯短语噪声就跳过）。对应 Go ``usefulAnchor``。"""
    text = str(anchor or "").strip()
    if len(text) < 6:
        return False
    if len(text.lower().split()) >= 2:
        return True
    return len(text) >= 8


def dedupe_fold(values: list[str]) -> list[str]:
    """按小写去重并丢掉空值。对应 Go ``dedupeFold``。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def squash_commas(text: str) -> str:
    """压掉多余逗号与空白。对应 Go ``squashCommas``。"""
    value = str(text or "")
    while ", ," in value:
        value = value.replace(", ,", ",")
    value = value.replace(" ,", ",")
    value = " ".join(value.split())
    return value.strip(" ,")
