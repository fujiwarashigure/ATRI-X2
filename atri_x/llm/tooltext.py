"""把「以正文形式泄漏的工具调用」还原成结构化调用。

上游网关（真实观测到的是 opencode-go 上的 ``deepseek-v4.1-flash``）偶尔不把模型的
原生工具调用翻译成 OpenAI ``tool_calls``，而是把文本协议原样塞进 assistant 正文。
这类回合在模型侧看是「调了工具」，在客户端看却只是普通文本：工具一次都不会执行，
用户还会看到一串协议标记。本模块负责把这种正文救回来。

两种形态都在真实会话里出现过：

* **DSML**（DeepSeek Harness 文本协议）::

      <｜｜DSML｜｜ calls>
      <｜｜DSML｜｜ invoke name="web_search">
      <｜｜DSML｜｜ parameter name="query" string="true">最新游戏显卡 2026 型号</｜｜DSML｜｜ parameter>
      </｜｜DSML｜｜ invoke>
      </｜｜DSML｜｜ calls>

  流式传输会把特殊 token 打碎：``</invoke>`` 变成 ``<｜place▁holder▁no▁73｜>invoke>``、外层
  ``calls`` 标签丢失、``parameter name=`` 被吞掉，所以解析必须容忍破损：
  只要能认出 ``invoke name="…"``，参数就按 ``string="true|false">值<`` 逐段提取。

* **JSON 片段**：``<tool-call>{"name": "web_search", "arguments": {...}}</tool-call>``。

三条使用约定（调用方见 :mod:`atri_x.agent.chat_turn`）：

1. :func:`looks_like_text_tool_call` 判定正文里有没有协议标记；
2. :func:`recover_text_tool_calls` 还原调用并给出**清洗后的正文**；
3. 还原不出来时也必须用清洗后的正文，绝不能把协议标记发给用户。

本模块是纯函数集合：不读配置、不写日志、不发网络请求。
"""

from __future__ import annotations

import json
import re
from typing import Any

from atri_x.llm.llm import ToolCallRequest

__all__ = [
    "TEXT_TOOL_CALL_NOTICE",
    "looks_like_text_tool_call",
    "parse_text_tool_calls",
    "recover_text_tool_calls",
    "strip_text_tool_calls",
]

#: 还原失败时替换正文的提示。要短、要能指导用户下一步。
TEXT_TOOL_CALL_NOTICE = "（模型这次把工具调用写成了正文，已忽略；请重试，或改用其它模型。）"

#: DSML 的分隔符指纹。两个全角竖线之间的这段 token 是协议自己的标记，
#: 正常自然语言里不会出现，因此可以用它判定「这是泄漏的工具调用」。
_DSML_MARK = "｜DSML｜"

#: ``<tool-call>`` / ``<tool_call>`` / ``<function_call>`` 一族标签。
_TOOL_JSON_RE = re.compile(r"<tool[-_]?calls?>(.*?)(?:</tool[-_]?calls?>|$)", re.S | re.I)
_TOOL_JSON_TAG_RE = re.compile(r"</?tool[-_]?calls?[^>]*>", re.I)
_FUNCTION_TAG_RE = re.compile(r"</?function[-_]?calls?[^>]*>", re.I)
#: ``<function_calls>`` 整块（含内部 ``invoke`` / ``parameter``）：一起头就整段截掉。
_FUNCTION_BLOCK_RE = re.compile(r"<function[-_]?calls?\b.*?(?:</function[-_]?calls?>|$)", re.S | re.I)

#: 工具名允许的形状（与 OpenAI ``function.name`` 一致）。
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,80}$")

#: ``invoke name="tool_name"``（DSML 与 function-calls 两种包装共有的锚点）。
_INVOKE_NAME_RE = re.compile(r'invoke\s+name\s*=\s*"([^"]{1,80})"')

#: 完整参数：``parameter name="query" string="true">值<``。
_PARAM_RE = re.compile(
    r'parameter\s+name\s*=\s*"([A-Za-z_][A-Za-z0-9_.\-]{0,80})"'
    r'\s+string\s*=\s*"(true|false)"\s*>'
)

#: 破损参数：``parameter name=`` 被流式分片吃掉，只剩 ``query" string="true">值<``。
_BARE_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_.\-]{0,80})"\s+string\s*=\s*"(true|false)"\s*>')

#: 正文里第一处 DSML 标记（``<｜｜DSML｜｜`` 或被拆碎的 ``<｜place▁holder▁no▁73｜>``）。
_DSML_START_RE = re.compile(r"<[^<>\n]{0,20}" + _DSML_MARK + r"|\b<｜place▁holder▁no▁73｜>[A-Za-z_]")

#: 清洗时兜底删掉的残余标签（含被拆碎的 ``<｜place▁holder▁no▁73｜>invoke>`` 形态）。
_RESIDUAL_TAG_RE = re.compile(
    r"</?[^<>\n]{0,32}" + _DSML_MARK + r"[^<>\n]{0,32}>|\b<｜place▁holder▁no▁73｜>[A-Za-z_]{0,32}>?"
)

#: ``<tool-call>`` 里允许的 JSON 对象键（宽松：兼容 name/arguments、tool/args 等写法）。
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARGS_KEYS = ("arguments", "args", "parameters", "input")


def looks_like_text_tool_call(text: str) -> bool:
    """正文里是否出现工具调用协议标记（DSML / ``<tool-call>``）。

    只做"像不像"的粗判：真判据是 :func:`_DSML_START_RE` 或 ``<tool-call>`` 标签，
    自然语言里不会出现 ``｜DSML｜`` 这一串协议分隔符与标签组合。
    """
    body = str(text or "")
    if not body:
        return False
    if _TOOL_JSON_RE.search(body) or _TOOL_JSON_TAG_RE.search(body):
        return True
    if _DSML_START_RE.search(body) and (
        _INVOKE_NAME_RE.search(body) or "parameter" in body or "calls>" in body
    ):
        return True
    return bool(_FUNCTION_TAG_RE.search(body) and _INVOKE_NAME_RE.search(body))


def recover_text_tool_calls(text: str) -> tuple[list[ToolCallRequest], str]:
    """还原正文里的工具调用，并返回 ``(调用列表, 清洗后的正文)``。

    * 能认出来的调用按出现顺序返回（JSON 形态优先，其次 DSML/function-calls）；
    * ``清洗后的正文`` 一定不含协议标记：还原成功的回合通常为空串（和原生
      ``tool_calls`` 回合的 ``content`` 一样），失败时可能保留协议标记之前的自然语言；
    * 调用 ``arguments`` 统一是 JSON 对象字符串，等价于 OpenAI ``tool_calls`` 的
      ``function.arguments``，可以直接交给工具执行层。
    """
    body = str(text or "")
    if not looks_like_text_tool_call(body):
        return [], body
    calls = _parse_json_calls(body)
    if not calls:
        calls = _parse_markup_calls(body)
    return calls, strip_text_tool_calls(body)


def parse_text_tool_calls(text: str) -> list[ToolCallRequest]:
    """只还原调用、不清洗正文（:func:`recover_text_tool_calls` 的便捷入口）。"""
    return recover_text_tool_calls(text)[0]


def strip_text_tool_calls(text: str) -> str:
    """删掉正文里的工具调用协议标记，返回可以安全发给用户的文本。"""
    body = str(text or "")
    if not looks_like_text_tool_call(body):
        return body
    cleaned = _TOOL_JSON_RE.sub("", body)
    cleaned = _TOOL_JSON_TAG_RE.sub("", cleaned)
    cleaned = _FUNCTION_BLOCK_RE.sub("", cleaned)
    cleaned = _FUNCTION_TAG_RE.sub("", cleaned)
    start = _DSML_START_RE.search(cleaned)
    if start is not None:
        # 协议标记一起头，后面就全是工具调用载荷：整段截掉，避免留下半截参数。
        cleaned = cleaned[: start.start()]
    cleaned = _RESIDUAL_TAG_RE.sub("", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# JSON 形态
# ---------------------------------------------------------------------------


def _parse_json_calls(text: str) -> list[ToolCallRequest]:
    """解析 ``<tool-call>`` 里的 JSON（单个对象、数组或逐行 JSON 都接受）。"""
    calls: list[ToolCallRequest] = []
    for match in _TOOL_JSON_RE.finditer(text):
        for item in _json_payloads(match.group(1)):
            calls.extend(_calls_from_payload(item))
    return calls


def _calls_from_payload(item: Any) -> list[ToolCallRequest]:
    """把一个 JSON 值展开成调用列表（数组递归展开，映射直接转换）。"""
    if isinstance(item, list):
        calls: list[ToolCallRequest] = []
        for entry in item:
            calls.extend(_calls_from_payload(entry))
        return calls
    call = _call_from_mapping(item)
    return [call] if call is not None else []


def _json_payloads(blob: str) -> list[Any]:
    """把一段文本解析成 JSON 值列表：整体解析优先，失败时按行解析。"""
    body = str(blob or "").strip()
    if not body:
        return []
    try:
        return [json.loads(body)]
    except (TypeError, ValueError):
        pass
    out: list[Any] = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (TypeError, ValueError):
            continue
    return out


def _call_from_mapping(item: Any) -> ToolCallRequest | None:
    """把 ``{"name": …, "arguments": …}`` 一类映射转成 :class:`ToolCallRequest`。"""
    if not isinstance(item, dict):
        return None
    nested = item.get("function")
    if isinstance(nested, dict):
        item = {**item, **nested}
    name = _clean_tool_name(_first_text(item, _NAME_KEYS))
    if not name:
        return None
    arguments = _first_present(item, _ARGS_KEYS)
    return ToolCallRequest(id="", name=name, arguments=_arguments_text(arguments))


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _arguments_text(value: Any) -> str:
    """参数统一成 JSON 对象字符串（原生 tool_calls 的形状）。"""
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return json.dumps({"value": value}, ensure_ascii=False)
        return json.dumps(decoded, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _clean_tool_name(value: str) -> str:
    name = str(value or "").strip()
    return name if _TOOL_NAME_RE.match(name) else ""


# ---------------------------------------------------------------------------
# DSML / function-calls 形态
# ---------------------------------------------------------------------------


def _parse_markup_calls(text: str) -> list[ToolCallRequest]:
    """按 ``invoke name="…"`` 切段，再从段内抽 ``parameter name=… string=…>值<``。

    破损容忍点：``parameter name=`` 被吃掉时按 ``名字" string="…">值<`` 兜底；
    ``</invoke>`` 被打碎成 ``<｜place▁holder▁no▁73｜>invoke>`` 也不影响，因为切段只看 ``invoke name=``。
    """
    invocations = [
        (match.start(), match.group(1))
        for match in _INVOKE_NAME_RE.finditer(text)
        if _clean_tool_name(match.group(1))
    ]
    if not invocations:
        return []
    parsed = _collect_parameters(text)
    buckets: list[dict[str, Any]] = [{} for _ in invocations]
    for position, name, is_string, raw_value in parsed:
        index = _invocation_index(invocations, position)
        if index is None:
            continue
        buckets[index][name] = _decode_parameter(raw_value, is_string)
    calls: list[ToolCallRequest] = []
    for index, (_position, name) in enumerate(invocations):
        arguments = json.dumps(buckets[index], ensure_ascii=False) if buckets[index] else "{}"
        calls.append(ToolCallRequest(id="", name=name, arguments=arguments))
    return calls


def _collect_parameters(text: str) -> list[tuple[int, str, str, str]]:
    """抽取全部参数，返回 ``(位置, 名字, string 标记, 原始值)``，按位置排序。"""
    found: list[tuple[int, str, str, str]] = []
    covered: list[tuple[int, int]] = []
    for match in _PARAM_RE.finditer(text):
        found.append(
            (
                match.start(),
                match.group(1),
                match.group(2),
                _parameter_value(text, match.end()),
            )
        )
        covered.append((match.start(), match.end()))
    for match in _BARE_PARAM_RE.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        found.append(
            (
                match.start(),
                match.group(1),
                match.group(2),
                _parameter_value(text, match.end()),
            )
        )
    found.sort(key=lambda item: item[0])
    return found


def _parameter_value(text: str, position: int) -> str:
    """参数值到下一个 ``<`` 为止（协议里值不会带尖括号）。"""
    end = text.find("<", position)
    if end < 0:
        end = len(text)
    return text[position:end].strip()


def _invocation_index(invocations: list[tuple[int, str]], position: int) -> int | None:
    """参数属于它前面最近的那个 ``invoke``。"""
    index: int | None = None
    for candidate, (start, _name) in enumerate(invocations):
        if start <= position:
            index = candidate
        else:
            break
    return index


def _decode_parameter(raw_value: str, is_string: str) -> Any:
    """``string="false"`` 的值按 JSON 解（数组/对象/数字），失败就保留原文。"""
    if is_string == "true":
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        return raw_value
