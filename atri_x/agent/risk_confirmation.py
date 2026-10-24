"""高风险工具确认的文案与指令识别。对应 Go：``internal/agent/risk_confirmation.go``。

职责：``riskConfirmationCommands`` / ``riskConfirmationPromptText`` /
``riskConfirmationWaitingText`` / ``riskConfirmationDetailText`` /
``formatRiskConfirmationArguments``（YAML 风格的参数渲染）/
``isRiskConfirmationCommand``。

与 Go 的差异（形式，不是简化）：

* Go 的 ``json.Decoder.UseNumber`` → :class:`json.JSONDecoder` ``parse_float``
  保留整数原样；``json.Number`` 的语义（不丢精度、不改成科学计数法）因此保留。
* ``command.Router`` 未落地时 ``is_risk_confirmation_command`` 接受鸭子类型的
  ``router.parse(text)``，成功且名字命中即返回 True（与 Go 的 ``parsed.OK`` 同义）。
* 缩进/键渲染逐字符对齐 Go：``riskIndent`` 两级空格，含换行的字符串走块标量 ``|``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "REDACTED_ARGUMENTS_TEXT",
    "RiskConfirmationCommand",
    "confirmation_allows_confirm_all",
    "confirmation_conversation_kind",
    "confirmation_hides_arguments",
    "format_risk_confirmation_arguments",
    "is_risk_confirmation_command",
    "is_risk_confirmation_command_name",
    "risk_confirmation_command_help",
    "risk_confirmation_command_names",
    "risk_confirmation_commands",
    "risk_confirmation_detail_text",
    "risk_confirmation_prompt_text",
    "risk_confirmation_waiting_text",
]

#: 群聊 / 频道里详情文案对参数的替代说法。
REDACTED_ARGUMENTS_TEXT = "参数：已隐藏（群聊中不展示，请结合工具名与风险等级判断）"


@dataclass
class RiskConfirmationCommand:
    """一条确认期指令。对应 Go ``riskConfirmationCommand``。"""

    name: str = ""
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    description: str = ""


def risk_confirmation_commands(
    prefix: str, include_confirm_all: bool = True
) -> list[RiskConfirmationCommand]:
    """确认期可用指令表。对应 Go ``riskConfirmationCommands``。

    ``include_confirm_all=False`` 时去掉 ``confirmall`` / ``ca``：群聊与频道里
    不提供「一次放行本会话全部工具」，避免一次误确认把后续所有高风险调用都放行。
    """
    prefix = prefix or ""
    commands = [
        RiskConfirmationCommand(
            name="detail",
            aliases=["details"],
            usage=prefix + "detail",
            description="查看完整参数",
        ),
        RiskConfirmationCommand(
            name="confirm",
            aliases=["c"],
            usage=prefix + "confirm",
            description="确认本次",
        ),
        RiskConfirmationCommand(
            name="confirmtool",
            aliases=["ct"],
            usage=prefix + "confirmtool",
            description="确认本次并自动确认后续同工具调用",
        ),
        RiskConfirmationCommand(
            name="confirmall",
            aliases=["ca"],
            usage=prefix + "confirmall",
            description="确认本次并自动确认当前 Session 后续所有工具调用",
        ),
        RiskConfirmationCommand(
            name="reject",
            usage=prefix + "reject <原因>",
            description="拒绝",
        ),
        RiskConfirmationCommand(
            name="stop",
            usage=prefix + "stop",
            description="停止当前请求",
        ),
    ]
    if include_confirm_all:
        return commands
    return [cmd for cmd in commands if cmd.name != "confirmall"]


def risk_confirmation_command_help(prefix: str, include_confirm_all: bool = True) -> str:
    """指令帮助行。对应 Go ``riskConfirmationCommandHelp``。"""
    return "；".join(
        f"{cmd.usage} {cmd.description}"
        for cmd in risk_confirmation_commands(prefix, include_confirm_all)
    )


def risk_confirmation_prompt_text(
    prefix: str, timeout: float, include_confirm_all: bool = True
) -> str:
    """等待确认时发给用户的提示。对应 Go ``riskConfirmationPromptText``。

    ``timeout`` 是秒数（Go 的 ``time.Duration``）；``<= 0`` 表示不设超时。
    """
    text = "可用指令：" + risk_confirmation_command_help(prefix, include_confirm_all)
    if timeout > 0:
        text += "；超过 %s 没有执行有效操作将自动停止，%sdetail 可重新计时" % (
            confirmation_wait_duration_text(timeout),
            prefix,
        )
    return text


def risk_confirmation_waiting_text(
    prefix: str, include_confirm_all: bool = True
) -> str:
    """"正在等待确认、只接受这些指令"的前缀。对应 Go ``riskConfirmationWaitingText``。"""
    return (
        "正在等待高风险工具调用确认，只接受："
        + risk_confirmation_command_help(prefix, include_confirm_all)
        + "。\n"
    )


def risk_confirmation_detail_text(confirmation: Any) -> str:
    """详情文案。对应 Go ``riskConfirmationDetailText``。

    ``confirmation.redacted`` 为真时只给工具名 / 风险等级 / 摘要：群聊里参数
    属于泄露面，宁可确认方看不到细节，也不把它发进群。
    """
    if getattr(confirmation, "redacted", False):
        text = "高风险工具调用详情\n工具：%s\n风险：%s\n%s\n" % (
            getattr(confirmation, "tool_name", ""),
            getattr(confirmation, "risk", ""),
            REDACTED_ARGUMENTS_TEXT,
        )
        summary = str(getattr(confirmation, "summary", "") or "").strip()
        if summary:
            text += "摘要：%s\n" % summary
        return text
    detail = str(getattr(confirmation, "detail", "") or "").strip()
    if not detail:
        detail = format_risk_confirmation_arguments(
            getattr(confirmation, "arguments", "") or ""
        )
    return "高风险工具调用详情\n工具：%s\n风险：%s\n参数：\n%s\n" % (
        getattr(confirmation, "tool_name", ""),
        getattr(confirmation, "risk", ""),
        detail,
    )


# ---------------------------------------------------------------------------
# 确认交互的会话可见性（本仓扩展）
# ---------------------------------------------------------------------------
def confirmation_conversation_kind(agent: Any, ctx: Any) -> str:
    """这次确认交互所在会话的种类：``group`` / ``private`` / ``channel`` / ``""``。

    只看平台作用域 id 前缀，与 ``thread_mode`` 无关 —— 群里的确认提示始终发到
    群里（所有群成员可见），所以参数是否展示不能看会话是否共享线程。
    """
    scope = _scope_of(agent, ctx)
    scope_id = str(getattr(scope, "platform_scope_id", "") or "")
    from atri_x.agent.identity import conversation_kind_from_scope

    return conversation_kind_from_scope(scope_id)


def confirmation_hides_arguments(agent: Any, ctx: Any) -> bool:
    """群聊 / 频道里不展示高风险工具的参数（提示与 ``/detail`` 都不展示）。"""
    return confirmation_conversation_kind(agent, ctx) in ("group", "channel")


def confirmation_allows_confirm_all(agent: Any, ctx: Any) -> bool:
    """``/confirmall`` 只在单聊与 CLI 可用。

    「本会话后续所有高风险工具一次放行」在群里等价于一次误确认就把整个会话
    交给模型，默认关闭；确实需要等价能力时用 ``/confirmtool``（只放行同工具）。
    """
    return confirmation_conversation_kind(agent, ctx) not in ("group", "channel")


def _scope_of(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None) if agent is not None else None
    if helper is None:
        return None
    return helper(ctx)



def confirmation_wait_duration_text(timeout: float) -> str:
    """等待时长文案。对应 Go ``confirmationWaitDurationText``（input.go）。

    Go 用 ``time.Duration.Round(time.Second).String()``；这里输出同样的
    ``1m30s`` / ``45s`` 形态。
    """
    seconds = int(round(float(timeout or 0)))
    if seconds <= 0:
        return "0s"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    out = ""
    if hours:
        out += "%dh" % hours
    if minutes:
        out += "%dm" % minutes
    if secs or not out:
        out += "%ds" % secs
    return out


# ---------------------------------------------------------------------------
# 参数渲染（Go 用 YAML 风格手写渲染，不引入 YAML 库）
# ---------------------------------------------------------------------------
def format_risk_confirmation_arguments(args: str) -> str:
    """把 JSON 参数渲染成可读的多行文本。对应 Go ``formatRiskConfirmationArguments``。"""
    text = str(args or "").strip()
    if not text:
        return "{}"
    decoder = json.JSONDecoder(parse_float=_keep_number, parse_int=_keep_number)
    try:
        value, end = decoder.raw_decode(text)
    except ValueError:
        return text
    if text[end:].strip():
        # Go 用第二次 Decode 必须返回 io.EOF 来拒绝尾部垃圾。
        return text
    return render_risk_value(value, 0).rstrip("\n")


class _RawNumber(str):
    """保留原始写法的数字（对应 Go ``json.Number``）。"""


def _keep_number(raw: str) -> _RawNumber:
    return _RawNumber(raw)


def render_risk_value(value: Any, indent: int) -> str:
    """渲染一个值。对应 Go ``renderRiskValue``。"""
    if isinstance(value, dict):
        if not value:
            return risk_indent(indent) + "{}\n"
        out = []
        for key in sorted(value.keys()):
            out.append(_write_risk_entry(risk_indent(indent) + _format_risk_key(key) + ":", value[key], indent))
        return "".join(out)
    if isinstance(value, list):
        if not value:
            return risk_indent(indent) + "[]\n"
        return "".join(
            _write_risk_entry(risk_indent(indent) + "-", item, indent) for item in value
        )
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            return risk_indent(indent) + "|\n" + _risk_block_string(value, indent + 1)
        return risk_indent(indent) + _format_risk_inline_string(value) + "\n"
    return risk_indent(indent) + _format_risk_scalar(value) + "\n"


def _write_risk_entry(prefix: str, value: Any, indent: int) -> str:
    if isinstance(value, (dict, list)):
        return prefix + "\n" + render_risk_value(value, indent + 1)
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            return prefix + " |\n" + _risk_block_string(value, indent + 1)
        return prefix + " " + _format_risk_inline_string(value) + "\n"
    return prefix + " " + _format_risk_scalar(value) + "\n"


def _risk_block_string(value: str, indent: int) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    prefix = risk_indent(indent)
    return "".join(prefix + line + "\n" for line in text.split("\n"))


def _format_risk_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _RawNumber):
        return str(value)
    if isinstance(value, float):
        return _format_risk_float(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _format_risk_inline_string(value)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _format_risk_float(value: float) -> str:
    """对应 Go ``strconv.FormatFloat(v, 'f', -1, 64)``：最短且**不带指数**。"""
    text = repr(value)
    if "e" not in text and "E" not in text:
        return text
    from decimal import Decimal

    return format(Decimal(text), "f")


def _format_risk_inline_string(value: str) -> str:
    if value == "" or value.strip() != value:
        return json.dumps(value, ensure_ascii=False)
    return value


def _format_risk_key(key: str) -> str:
    if not key:
        return json.dumps(key, ensure_ascii=False)
    for char in key:
        if char.isascii() and (char.isalnum() or char in "_-."):
            continue
        return json.dumps(key, ensure_ascii=False)
    return key


def risk_indent(level: int) -> str:
    """对应 Go ``riskIndent``。"""
    return "  " * max(0, level)


# ---------------------------------------------------------------------------
# 指令识别
# ---------------------------------------------------------------------------
def risk_confirmation_command_names() -> list[str]:
    """全部确认期指令名与别名。对应 Go ``riskConfirmationCommandNames``。"""
    names: list[str] = []
    for command in risk_confirmation_commands("/"):
        names.append(command.name)
        names.extend(command.aliases)
    return names


def is_risk_confirmation_command_name(name: str) -> bool:
    """某个名字是否是确认期指令。对应 Go ``isRiskConfirmationCommandName``。"""
    text = str(name or "").strip()
    return text in risk_confirmation_command_names()


def is_risk_confirmation_command(text: str, router: Any) -> bool:
    """文本是否是确认期指令。对应 Go ``isRiskConfirmationCommand``。"""
    if router is None:
        return False
    parsed = router.parse(text)
    if not getattr(parsed, "ok", False):
        return False
    return is_risk_confirmation_command_name(getattr(parsed, "name", ""))
