"""生成参数的方言解析：A1111 / NovelAI / ComfyUI / InvokeAI。

对应 Go：``internal/artist/metadata.go`` 的**方言解析层**——``metadataText`` 收集器、
``metadataParseA1111`` 家族、``metadataApplyNovelAI``、``metadataApplyComfyUI`` 家族、
``metadataApplyInvokeAI`` 以及配套的 JSON 助手。

容器解析层（PNG ``tEXt``/``zTXt``/``iTXt``/``eXIf``、JPEG EXIF/XMP、WebP
``EXIF``/``XMP `` chunk、TIFF/IFD 与 XMP 读取、统一入口 ``extract_metadata``）在同包的
``atri_x/artist/metadata.py``；两者合起来等于 Go 的单文件 ``metadata.go``。

这里只做定界内的字符串/JSON 处理：读到的文本不执行、不当链接、不当命令。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any

from .types import (
    MaxMetadataTextBytes,
    Metadata,
    MetadataSourceA1111,
    MetadataSourceComfyUI,
    MetadataSourceInvokeAI,
    MetadataSourceNovelAI,
    MetadataText,
)

# --- JSON 助手（对应 Go metadataDecodeJSONObject 及其配套） ---

class JSONNumber(str):
    """JSON 数字的字面量文本。对应 Go ``json.Number``（``Decoder.UseNumber()``）。

    用 ``str`` 子类承载，既让 ``6.0`` 与 ``6`` 保持原样，又不会把真正的字符串误判成数字
    ——判定顺序永远是先 ``JSONNumber`` 后 ``str``。"""

    __slots__ = ()

_DECODER = json.JSONDecoder(parse_int=JSONNumber, parse_float=JSONNumber)

def decode_json_object(raw: str) -> dict[str, Any] | None:
    """对应 Go ``metadataDecodeJSONObject``：解析 JSON 对象，失败返回 None。

    只接受对象（数组/标量返回 None），与 Go 反序列化到 ``map[string]any`` 一致；与 Go 的
    ``Decoder.Decode`` 一样忽略首个 JSON 值之后的多余内容。"""
    if not raw or len(raw) > MaxMetadataTextBytes:
        return None
    text = raw.lstrip()
    if not text.startswith("{"):
        return None
    try:
        value, _ = _DECODER.raw_decode(text)
    except (ValueError, RecursionError):
        return None  # 畸形 JSON 与嵌套炸弹（RecursionError）都降级为「不是对象」
    return value if isinstance(value, dict) else None

def has_any_key(object: dict[str, Any], *keys: str) -> bool:
    """对应 Go ``metadataHasAnyKey``。"""
    return any(key in object for key in keys)

def format_float(value: float) -> str:
    """对应 Go ``strconv.FormatFloat(value, 'f', -1, 64)``：不用指数、不留 ``.0``。"""
    text = repr(float(value))
    if "e" not in text and "E" not in text:
        return text[:-2] if text.endswith(".0") else text
    mantissa, _, exponent = text.partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = whole + frac
    point = len(whole) + int(exponent)
    if point <= 0:
        return "0." + "0" * (-point) + digits
    if point >= len(digits):
        return digits + "0" * (point - len(digits))
    return digits[:point] + "." + digits[point:]

def json_scalar(value: Any) -> tuple[str, bool]:
    """对应 Go ``metadataJSONScalar``：把 JSON 标量渲染成 Parameters 用的字符串。"""
    if isinstance(value, JSONNumber):
        return str(value), True
    if isinstance(value, str):
        return (value, True) if value else ("", False)
    if isinstance(value, bool):
        return ("true" if value else "false"), True
    if isinstance(value, float):
        return format_float(value), True
    return "", False

_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_DIGITS = frozenset("0123456789")

def parse_int64(text: str) -> int | None:
    """对应 Go ``strconv.ParseInt(text, 10, 64)``：不合法或越界返回 None。

    只认 ASCII 数字（``str.isdigit`` 会接受全角等 Unicode 数字），并先按位数挡掉超长输入，
    避免超大整数字符串把 ``int()`` 拖垮。"""
    if not text:
        return None
    body = text[1:] if text[0] in "+-" else text
    if not body or len(body) > 19 or not set(body) <= _DIGITS:
        return None
    value = int(body)
    if text[0] == "-":
        value = -value
    return value if _INT64_MIN <= value <= _INT64_MAX else None

def node_less(left: str, right: str) -> bool:
    """对应 Go ``metadataNodeLess``：两边都是整数则按数值，否则按字符串比较——这样
    ``"10"`` 排在 ``"9"`` 之后而不是之前，节点顺序才与生成器一致。"""
    left_number = parse_int64(left)
    right_number = parse_int64(right)
    if left_number is not None and right_number is not None:
        return left_number < right_number
    return left < right

def _node_compare(left: str, right: str) -> int:
    if node_less(left, right):
        return -1
    return 1 if node_less(right, left) else 0

_NODE_KEY = cmp_to_key(_node_compare)

def sorted_keys(values: dict[str, Any]) -> list[str]:
    """对应 Go ``metadataSortedKeys``：确定性地排序 map 的键。"""
    return sorted(values, key=_NODE_KEY)

# --- A1111 / SD WebUI（对应 Go metadataParseA1111 家族） ---

# 负向提示词的分隔标记（Go metadataNegativeMarker）。
NEGATIVE_MARKER = "\nNegative prompt: "

def apply_a1111(meta: Metadata, text: MetadataText) -> bool:
    """对应 Go ``metadataApplyA1111``：识别 A1111 / SD WebUI 参数块。"""
    raw, ok = text.get("parameters")
    if not ok:
        return False
    positive, negative, params, ok = parse_a1111(raw)
    if not ok:
        return False
    meta.source = MetadataSourceA1111
    meta.positive = positive
    meta.negative = negative
    for name, value in params.items():
        meta.parameters[name] = value
    return True

def parse_a1111(raw: str) -> tuple[str, str, dict[str, str], bool]:
    """对应 Go ``metadataParseA1111``：拆出提示词两段与尾部字段表。

    参数行是最后一行以 ``Steps:`` 开头的行，缺它则 ``ok=False``；返回
    ``(positive, negative, params, ok)``。"""
    lines = raw.replace("\r\n", "\n").split("\n")
    index = -1
    for position, line in enumerate(lines):
        if line.startswith("Steps:"):
            index = position
    if index < 0:
        return "", "", {}, False
    params = parse_a1111_fields("\n".join(lines[index:]))
    if not any(value != "" for value in params.values()):
        return "", "", {}, False
    positive, negative = split_a1111_negative("\n".join(lines[:index]))
    return positive, negative, params, True

def split_a1111_negative(head: str) -> tuple[str, str]:
    """对应 Go ``metadataSplitA1111Negative``：在最后一个负向标记处切开。"""
    index = head.rfind(NEGATIVE_MARKER)
    if index >= 0:
        return head[:index], head[index + len(NEGATIVE_MARKER):]
    prefix = "Negative prompt: "
    if head.startswith(prefix):
        return "", head[len(prefix):]
    return head, ""

def parse_a1111_fields(raw: str) -> dict[str, str]:
    """对应 Go ``metadataParseA1111Fields``：解析 ``K: V, K: V`` 尾部字段表。

    值本身可能含逗号，所以只有「看起来像新的 K: V 对」的片段才开新字段，其余片段并入前
    一个字段。"""
    params: dict[str, str] = {}
    name = ""
    parts: list[str] = []

    def flush() -> None:
        if name:
            params[parameter_key(name)] = "".join(parts).strip()

    for piece in raw.split(","):
        trimmed = piece.strip()
        if not trimmed:
            continue
        field_name, field_value, is_field = split_pair(trimmed)
        if not is_field:
            if name:
                parts.append(", ")
                parts.append(trimmed)
            continue
        flush()
        name, parts = field_name, [field_value]
    flush()
    return params

def split_pair(piece: str) -> tuple[str, str, bool]:
    """对应 Go ``metadataSplitPair``：切开一个 ``K: V`` 片段。"""
    index = piece.find(":")
    if index <= 0:
        return "", "", False
    name = piece[:index].strip()
    if not name:
        return "", "", False
    return name, piece[index + 1:].strip(), True

def parameter_key(name: str) -> str:
    """对应 Go ``metadataParameterKey``：字段名折成 Parameters 的规范小写键。"""
    slug = name.strip().lower().replace(" ", "_").replace("\t", "_").replace("-", "_")
    return "cfg" if slug in ("cfg_scale", "cfg") else slug

# --- NovelAI（对应 Go metadataApplyNovelAI） ---

# NovelAI 写进 Comment 的 JSON 里会被收进 Parameters 的键（Go 的键列表）。
_NOVELAI_KEYS = (
    "steps", "sampler", "noise_schedule", "seed", "scale",
    "width", "height", "model_name", "model_hash", "software",
)

def apply_novelai(meta: Metadata, text: MetadataText) -> bool:
    """对应 Go ``metadataApplyNovelAI``：识别 NovelAI 图片。

    方言由三路信号并集判定：负载自身的键集合、``Software``、``Source``。单个 ``Software``
    不足以定案（第三方客户端会改写它），三者都只是线索而非出处证明。"""
    comment, has_comment = text.get("Comment")
    description, has_description = text.get("Description")
    named = named_novelai(text)
    payload = decode_json_object(comment)
    if payload is None:
        if not named:
            return False
        if has_comment:
            text.warn("Comment 文本块不是合法的 JSON，已跳过 NovelAI 参数解析")
        meta.source = MetadataSourceNovelAI
        meta.positive = description
        return True
    prompt = payload.get("prompt")
    negative = payload.get("uc")
    if not named and not has_any_key(payload, "uc", "steps", "sampler", "scale", "seed"):
        return False
    meta.source = MetadataSourceNovelAI
    meta.positive = prompt if isinstance(prompt, str) else ""
    if meta.positive == "" and has_description:
        meta.positive = description
    meta.negative = negative if isinstance(negative, str) else ""
    # model_name 折到 model——其它方言用的也是这个键，调用方读模型名的方式统一。
    for key in _NOVELAI_KEYS:
        value, ok = json_scalar(payload.get(key))
        if ok:
            meta.parameters["model" if key == "model_name" else key] = value
    width = meta.parameters.get("width")
    height = meta.parameters.get("height")
    if width is not None and height is not None:
        meta.parameters["size"] = width + "x" + height
    return True

def named_novelai(text: MetadataText) -> bool:
    """对应 Go ``metadataNamedNovelAI``：文本块自身是否点名 NovelAI。

    ``Software`` 写作者名、``Source`` 写跑的模型（如 ``NovelAI Diffusion V5 DB276663``）；
    两者都是任何工具都能改写的纯文本，所以只是方言线索。"""
    for keyword in ("Software", "Source"):
        value, ok = text.get(keyword)
        if ok and "novelai" in value.lower():
            return True
    return False

# --- ComfyUI（对应 Go metadataApplyComfyUI 家族） ---

@dataclass
class ComfyNode:
    """ComfyUI API 图里的一个节点。对应 Go ``metadataComfyNode``。"""

    id: str = ""
    class_type: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

def apply_comfyui(meta: Metadata, text: MetadataText) -> bool:
    """对应 Go ``metadataApplyComfyUI``：API 图在 ``prompt`` 块，编辑器图在 ``workflow`` 块。"""
    prompt_raw, has_prompt = text.get("prompt")
    workflow_raw, has_workflow = text.get("workflow")
    graph = decode_json_object(prompt_raw)
    if graph is not None and not looks_like_comfy_graph(graph):
        graph = None
    editor = decode_json_object(workflow_raw)
    if editor is not None and not looks_like_comfy_workflow(editor):
        editor = None
    if graph is None and editor is None:
        return False
    meta.source = MetadataSourceComfyUI
    if editor is not None and has_workflow:
        meta.workflow = workflow_raw
    elif graph is not None and has_prompt:
        meta.workflow = prompt_raw
    if graph is not None:
        apply_comfy_graph(meta, text, graph)
        return True
    # 只有编辑器图：它不带 conditioning 连线，文本只能说「有」，分不了正负向。
    texts = comfy_editor_texts(editor)
    if texts:
        meta.prompts = texts
        text.warn("无法确定 ComfyUI 工作流中提示词的正负向归属，已全部放入 Prompts")
    return True

def apply_comfy_workflow_fallback(meta: Metadata, text: MetadataText) -> None:
    """对应 Go ``metadataApplyComfyWorkflowFallback``：被判成别的方言时也留图文本。"""
    if meta.workflow:
        return
    for keyword in ("workflow", "prompt"):
        raw, ok = text.get(keyword)
        if not ok:
            continue
        graph = decode_json_object(raw)
        if graph is None:
            continue
        if keyword == "workflow" and looks_like_comfy_workflow(graph):
            meta.workflow = raw
            return
        if keyword == "prompt" and looks_like_comfy_graph(graph):
            meta.workflow = raw
            return

def looks_like_comfy_graph(object: dict[str, Any]) -> bool:
    """对应 Go ``metadataLooksLikeComfyGraph``：节点 id 到带 class_type 的节点。"""
    return any(isinstance(value, dict) and isinstance(value.get("class_type"), str)
               for value in object.values())

def looks_like_comfy_workflow(object: dict[str, Any]) -> bool:
    """对应 Go ``metadataLooksLikeComfyWorkflow``：带非空 ``nodes`` 的编辑器图。"""
    nodes = object.get("nodes")
    if isinstance(nodes, list) and nodes:
        return True
    return looks_like_comfy_graph(object)

def comfy_nodes(graph: dict[str, Any]) -> list[ComfyNode]:
    """对应 Go ``metadataComfyNodes``：把 API 图规范成按 id 排序的节点表——排序让后续
    每个判断都与 id 的书写顺序无关（``"10"`` 在 ``"9"`` 之后）。"""
    nodes: list[ComfyNode] = []
    for node_id, value in graph.items():
        if not isinstance(value, dict):
            continue
        class_type = value.get("class_type")
        inputs = value.get("inputs")
        nodes.append(ComfyNode(
            id=node_id,
            class_type=class_type if isinstance(class_type, str) else "",
            inputs=inputs if isinstance(inputs, dict) else {},
        ))
    nodes.sort(key=cmp_to_key(lambda left, right: _node_compare(left.id, right.id)))
    return nodes

def is_comfy_text_node(class_type: str) -> bool:
    """对应 Go ``metadataIsComfyTextNode``。"""
    return "cliptextencode" in class_type.lower()

def apply_comfy_graph(meta: Metadata, text: MetadataText, graph: dict[str, Any]) -> None:
    """对应 Go ``metadataApplyComfyGraph``：``CLIPTextEncode`` 持文本，采样器分正负向。"""
    nodes = comfy_nodes(graph)
    by_id = {node.id: node for node in nodes}
    texts: dict[str, str] = {}
    for node in nodes:
        value = node.inputs.get("text")
        if is_comfy_text_node(node.class_type) and isinstance(value, str) and value:
            texts[node.id] = value
    if not texts:
        return
    sampler = find_comfy_sampler(nodes)
    if sampler is not None:
        positive, positive_id = comfy_resolve(sampler.inputs.get("positive"), by_id, texts)
        negative, negative_id = comfy_resolve(sampler.inputs.get("negative"), by_id, texts)
        if positive or negative:
            meta.positive = positive
            meta.negative = negative
            meta.prompts = comfy_texts(texts, {item for item in (positive_id, negative_id) if item})
            apply_comfy_parameters(meta, nodes, sampler)
            return
    meta.prompts = comfy_texts(texts, None)
    text.warn("无法确定 ComfyUI 工作流中提示词的正负向归属，已全部放入 Prompts")

def find_comfy_sampler(nodes: list[ComfyNode]) -> ComfyNode | None:
    """对应 Go ``metadataFindComfySampler``：优先 KSampler 家族，其次任何连两侧的节点。"""
    for node in nodes:
        if "ksampler" in node.class_type.lower() and comfy_links_both(node):
            return node
    for node in nodes:
        if comfy_links_both(node):
            return node
    return None

def comfy_links_both(node: ComfyNode) -> bool:
    """对应 Go ``metadataComfyLinksBoth``：通过连线数组同时引用正负 conditioning。"""
    if comfy_link_id(node.inputs.get("positive"))[1]:
        return comfy_link_id(node.inputs.get("negative"))[1]
    return False

def comfy_link_id(link: Any) -> tuple[str, bool]:
    """对应 Go ``metadataComfyLinkID``：连线数组是 ``[nodeID, outputSlot]``。"""
    if not isinstance(link, list) or not link:
        return "", False
    value = link[0]
    if isinstance(value, JSONNumber):
        return str(value), True
    if isinstance(value, str):
        return value, True
    if isinstance(value, float):
        return format_float(value), True
    return "", False

def comfy_resolve(link: Any, by_id: dict[str, ComfyNode], texts: dict[str, str]) -> tuple[str, str]:
    """对应 Go ``metadataComfyResolve``：沿 conditioning 连线找到文本节点。

    真实图会在采样器与文本编码器之间插 combine / reroute 节点，所以这里是有界的广度优先
    搜索（深度上限 32），而不是跳一跳。"""
    start, ok = comfy_link_id(link)
    if not ok:
        return "", ""
    seen: set[str] = set()
    queue = [start]
    depth = 0
    while queue and depth < 32:
        following: list[str] = []
        for node_id in queue:
            if node_id in seen:
                continue
            seen.add(node_id)
            if node_id in texts:
                return texts[node_id], node_id
            node = by_id.get(node_id)
            if node is None:
                continue
            for key in sorted_keys(node.inputs):
                child, child_ok = comfy_link_id(node.inputs[key])
                if child_ok:
                    following.append(child)
        queue = following
        depth += 1
    return "", ""

def comfy_texts(texts: dict[str, str], skip: set[str] | None) -> list[str]:
    """对应 Go ``metadataComfyTexts``：按节点 id 顺序返回文本，跳过 ``skip`` 里的 id。"""
    return [texts[node_id] for node_id in sorted_keys(texts) if not skip or node_id not in skip]

def comfy_editor_texts(editor: dict[str, Any]) -> list[str]:
    """对应 Go ``metadataComfyEditorTexts``：编辑器图把文本存成 widget 值。"""
    nodes = editor.get("nodes")
    if not isinstance(nodes, list):
        return []
    texts: list[str] = []
    for value in nodes:
        if not isinstance(value, dict):
            continue
        class_type = value.get("type")
        if not isinstance(class_type, str) or not is_comfy_text_node(class_type):
            continue
        widgets = value.get("widgets_values")
        if isinstance(widgets, list):
            texts.extend(item for item in widgets if isinstance(item, str) and item)
        elif isinstance(widgets, dict):
            text = widgets.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts

def apply_comfy_parameters(meta: Metadata, nodes: list[ComfyNode], sampler: ComfyNode) -> None:
    """对应 Go ``metadataApplyComfyParameters``：采样器设置 + 描述图片的少数图设置。"""
    for key in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
        value, ok = json_scalar(sampler.inputs.get(key))
        if not ok:
            continue
        if key == "sampler_name":
            meta.parameters["sampler"] = value
        elif key == "denoise":
            meta.parameters["denoising_strength"] = value
        else:
            meta.parameters[key] = value
    for node in nodes:
        class_type = node.class_type.lower()
        if "model" not in meta.parameters and "checkpointloader" in class_type:
            value = node.inputs.get("ckpt_name")
            if isinstance(value, str) and value:
                meta.parameters["model"] = value
        if "size" not in meta.parameters and "latentimage" in class_type:
            width, has_width = json_scalar(node.inputs.get("width"))
            height, has_height = json_scalar(node.inputs.get("height"))
            if has_width and has_height:
                meta.parameters["size"] = width + "x" + height

# --- InvokeAI（对应 Go metadataApplyInvokeAI 家族） ---

# InvokeAI 历史上用过的信封键（Go metadataInvokePayload）。
_INVOKE_ENVELOPES = ("sd-metadata", "invokeai_metadata", "image", "metadata")

def apply_invokeai(meta: Metadata, text: MetadataText) -> bool:
    """对应 Go ``metadataApplyInvokeAI``：参数在 ``sd-metadata`` / ``invokeai_metadata`` 块。"""
    for keyword in ("sd-metadata", "invokeai_metadata", "Comment"):
        raw, ok = text.get(keyword)
        if not ok:
            continue
        object = decode_json_object(raw)
        if object is None:
            continue
        payload = invoke_payload(object, 0)
        if payload is None:
            continue
        positive, negative, params = parse_invoke_payload(payload)
        if positive == "" and negative == "" and not params:
            continue
        meta.source = MetadataSourceInvokeAI
        meta.positive = positive
        meta.negative = negative
        for name, value in params.items():
            meta.parameters[name] = value
        return True
    return False

def invoke_payload(object: dict[str, Any], depth: int) -> dict[str, Any] | None:
    """对应 Go ``metadataInvokePayload``：拆信封并把内层键覆盖到外层之上；``depth`` 有
    上限，构造出来的文档不能驱动无限递归。"""
    if depth > 8:
        return object
    for key in _INVOKE_ENVELOPES:
        inner = object.get(key)
        if not isinstance(inner, dict):
            continue
        # 被拆掉的信封键不能再次进入，否则会一直找到同一个键而不是下面的负载。
        merged = {name: value for name, value in object.items() if name != key}
        merged.update(inner)
        return invoke_payload(merged, depth + 1)
    return object

def parse_invoke_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    """对应 Go ``metadataParseInvokePayload``：读一条 InvokeAI 负载的提示词与设置。"""
    positive = invoke_prompt(payload.get("prompt"))
    if positive == "":
        value = payload.get("positive_prompt")
        if isinstance(value, str):
            positive = value
    negative = ""
    for key in ("negative_prompt", "negative"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            negative = value
            break
    params: dict[str, str] = {}
    for key in ("steps", "sampler", "sampler_name", "cfg_scale", "seed",
                "width", "height", "model"):
        value, ok = json_scalar(payload.get(key))
        if not ok:
            continue
        if key == "cfg_scale":
            params["cfg"] = value
        elif key == "sampler_name":
            params["sampler"] = value
        else:
            params[key] = value
    width = params.get("width")
    height = params.get("height")
    if width is not None and height is not None:
        params["size"] = width + "x" + height
    return positive, negative, params

def invoke_prompt(value: Any) -> str:
    """对应 Go ``metadataInvokePrompt``：把 InvokeAI 的加权片段列表拍平成一行。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                if entry:
                    parts.append(entry)
            elif isinstance(entry, dict):
                prompt = entry.get("prompt")
                if isinstance(prompt, str) and prompt:
                    parts.append(prompt)
        return ", ".join(parts)
    return ""

__all__ = [
    "ComfyNode", "JSONNumber", "MetadataText", "NEGATIVE_MARKER", "apply_a1111",
    "apply_comfy_graph", "apply_comfy_parameters", "apply_comfy_workflow_fallback",
    "apply_comfyui", "apply_invokeai", "apply_novelai", "comfy_editor_texts",
    "comfy_link_id", "comfy_links_both", "comfy_nodes", "comfy_resolve", "comfy_texts",
    "decode_json_object", "find_comfy_sampler", "format_float", "has_any_key",
    "invoke_payload", "invoke_prompt", "is_comfy_text_node", "json_scalar",
    "looks_like_comfy_graph", "looks_like_comfy_workflow", "named_novelai", "node_less",
    "parameter_key", "parse_a1111", "parse_a1111_fields", "parse_int64",
    "parse_invoke_payload", "sorted_keys", "split_a1111_negative", "split_pair",
]
