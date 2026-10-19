"""后台任务里 shell 工具的风险裁决（Agent 侧）。对应 Go：``internal/agent/cron_tools.go``。

职责：``confirmBackgroundSandboxShell`` —— 后台 cron / Atrinis 的 shell 调用**不等待
用户确认**：critical 直接拒绝并把提醒写进工具结果，其余风险在**通过后台静态裁决**
（默认拒绝 + 只读白名单 + 结构约束，见 :mod:`atri_x.tool.builtin.shell_sandbox`）后
自动确认。

与 Go 的差异（形式，不是简化）：

* 纯决策部分已经在 :func:`atri_x.background.confirm_background_sandbox_shell`
  实现（共享契约，见 ``atri_x/background/background.py``）；本模块只负责 Go 里
  与 Agent 绑定的两件事：**写审计** 与 **拒绝时把提醒文本放进 tool 消息**。
* 决策不再只看评估阶段的 ``risk``：确认阶段从工具参数里取出 ``cmd`` 文本，重新做
  一遍静态裁决（fail-closed）；取不到命令文本同样按拒绝处理。
* Go 的 ``message *llm.LLMMessage`` 出参 → 直接返回 :class:`ShellRiskDecision`
  并把文本写进传入的 ``message.segments``（与 Go 相同：调用方持有同一个对象）。
* 审计字段逐字照抄 Go（``background_shell_rejected`` /
  ``background_shell_auto_confirmed``，含 ``sandbox_dir`` 与参数预览），并**新增**
  ``reason``（平台不支持 / 白名单未命中 / 参数违规 / 风险超限 / 无法静态解析）。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.background import (
    BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED,
    BACKGROUND_SHELL_AUDIT_REJECTED,
    BACKGROUND_SHELL_CRITICAL_MESSAGE,
    ShellRiskDecision,
    confirm_background_sandbox_shell as _decide,
)
from atri_x.llm import text_segments
from atri_x.tool.sandbox import sandbox_context_from_context

from atri_x.agent.tools import preview_arguments

__all__ = [
    "confirm_background_sandbox_shell",
    "background_shell_decision",
]


def confirm_background_sandbox_shell(
    agent: Any,
    ctx: Any,
    session_id: str,
    call: Any,
    risk: str,
    message: Any = None,
) -> ShellRiskDecision:
    """后台 shell 的风险裁决 + 审计。对应 Go ``confirmBackgroundSandboxShell``。

    返回值与 Go 的 ``(bool, bool)`` 一致：``decision.confirm`` / ``decision.handled``，
    也可以解包成元组（:class:`ShellRiskDecision` 实现了 ``__iter__``）。
    """
    sandbox, ok = sandbox_context_from_context(ctx)
    decision = _decide(
        sandbox if ok else None,
        str(getattr(call, "name", "") or ""),
        risk,
        cmd_text=shell_command_text(call),
        policy=background_shell_policy(agent),
    )
    if not decision.handled:
        return decision
    kind = str(getattr(sandbox, "background_kind", "") or "")
    fields = {
        "session_id": session_id,
        "kind": kind,
        "tool": getattr(call, "name", ""),
        "risk": risk,
        "sandbox_dir": getattr(sandbox, "dir", ""),
        "arguments": preview_arguments(getattr(call, "arguments", "") or ""),
        "reason": decision.reason,
    }
    if decision.confirm:
        _audit(agent, BACKGROUND_SHELL_AUDIT_AUTO_CONFIRMED, result="succeeded", **fields)
        return decision
    _audit(agent, BACKGROUND_SHELL_AUDIT_REJECTED, result="rejected", **fields)
    if message is not None:
        message.segments = text_segments(BACKGROUND_SHELL_CRITICAL_MESSAGE)
    return decision


def background_shell_decision(
    sandbox: Any,
    tool_name: str,
    risk: str,
    *,
    cmd_text: str = "",
    policy: Any = None,
) -> ShellRiskDecision:
    """纯决策入口（无审计、无 Agent）。对应共享包里的同名函数。"""
    return _decide(sandbox, tool_name, risk, cmd_text=cmd_text, policy=policy)


def shell_command_text(call: Any) -> str:
    """从工具调用参数里取 shell 的 ``cmd``；取不到返回空串（裁决侧 fail-closed 拒绝）。

    键名与 ``shell`` 工具的 schema 一致（``atri_x/tool/builtin/shell.py`` 的
    ``cmd``）。这里不 import 那个模块：``atri_x.tool.builtin`` 的包初始化会把整个
    内置工具面拉进 Agent 模块的 import 图；只认 ``cmd``，任何解析失败都返回空串
    （宁可拒绝，不要猜）。
    """
    raw = getattr(call, "arguments", "")
    if isinstance(raw, dict):
        data: Any = raw
    else:
        text = raw if isinstance(raw, str) else ""
        if text.strip() == "":
            return ""
        try:
            data = json.loads(text)
        except ValueError:
            return ""
    if not isinstance(data, dict):
        return ""
    cmd = data.get("cmd")
    return cmd.strip() if isinstance(cmd, str) else ""


def background_shell_policy(agent: Any) -> Any:
    """取后台 shell 策略：优先 Agent 上的同名属性，其次注册表里的 ``shell`` 工具。

    装配层把 ``[security].background_shell_allow[_all]`` 配到 ``ShellTool`` 实例上
    （``atri_x/app/runtime.py`` → ``atri_x/tool/builtin/register.py``）；确认阶段必须
    用同一份策略，否则 ``background_shell_allow_all`` 这类显式放行会在确认阶段被
    无声忽略。取不到就返回 ``None``，由裁决侧落到默认（默认拒绝 + 只读白名单）。
    """
    policy = _policy_like(getattr(agent, "background_shell_policy", None))
    if policy is not None:
        return policy
    registry = getattr(getattr(agent, "tool_runtime", None), "registry", None)
    get_tool = getattr(registry, "get", None)
    if not callable(get_tool):
        return None
    try:
        tool, ok = get_tool("shell")
    except Exception:  # noqa: BLE001 - 注册表实现各异，取不到就回落默认策略
        return None
    if not ok or tool is None:
        return None
    return _policy_like(getattr(tool, "background_shell_policy", None))


def _policy_like(candidate: Any) -> Any:
    """只接受形状正确的策略对象（``allow`` + ``allow_all``）；否则返回 ``None``。"""
    if candidate is None or not hasattr(candidate, "allow") or not hasattr(candidate, "allow_all"):
        return None
    return candidate


def _audit(agent: Any, event: str, **fields: Any) -> None:
    audit = getattr(agent, "audit", None)
    if audit is None:
        return
    args: list[Any] = [event]
    for key, value in fields.items():
        args.append(key)
        args.append(value)
    audit(*args)
