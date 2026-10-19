"""``atri_x.agent.dsh`` 的协议类型、常量与解析函数。

本模块只放数据形状：JSON-RPC 错误、ACP 的会话／事件／用量对象，以及把
``session/update`` 与 ``session/prompt`` 响应解析成这些对象的纯函数。
子进程与帧收发在 :mod:`atri_x.agent.dsh.transport`，会话语义在
:mod:`atri_x.agent.dsh.client`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "DEFAULT_COMMAND",
    "MAX_FRAME_BYTES",
    "TEXT_LIMIT",
    "AcpError",
    "AcpEvent",
    "AcpProcessError",
    "AcpPromptResult",
    "AcpProtocolError",
    "AcpServerInfo",
    "AcpSession",
    "AcpTimeoutError",
    "AcpUsage",
    "as_code",
    "as_int",
    "as_text",
    "client_version",
    "event_from_update",
    "parse_config_options",
    "reject_permission",
]

#: ACP 协议版本。DSH ``dsh-acp`` 公布 1；更高版本视为不兼容。
ACP_PROTOCOL_VERSION = 1
#: 单帧上限；DSH ``--json`` 事件流的单行上限是 32 KiB，这里为协议帧留足余量。
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: 默认启动命令；实际部署用 ``DSH_ACP_COMMAND`` 或显式覆盖。
DEFAULT_COMMAND: tuple[str, ...] = ("dsh", "--profile", "acp")
#: 单轮收集的文本上限，防止服务端异常刷屏撑爆内存。
TEXT_LIMIT = 4 * 1024 * 1024


def as_text(value: Any) -> str:
    """把协议字段收敛成字符串；``None`` 给空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def as_int(value: Any) -> int:
    """把协议里的计数收敛成非负整数；非法值给 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float) and value > 0:
        return int(value)
    return 0


def as_code(value: Any) -> int:
    """把 JSON-RPC 错误码收敛成整数（错误码是负数，不能套 :func:`as_int`）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def client_version() -> str:
    """客户端版本号；``atri_x`` 不可导入时退回 ``dev``（不硬依赖包版本）。"""
    try:
        from atri_x import __version__ as version

        return as_text(version) or "dev"
    except Exception:  # noqa: BLE001 - 版本号不是关键路径
        return "dev"


class AcpError(Exception):
    """ACP 调用失败的基类；``code`` 为 JSON-RPC 错误码（本端错误为 0）。"""

    def __init__(self, message: str, *, code: int = 0, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    def __str__(self) -> str:
        text = super().__str__()
        return f"[{self.code}] {text}" if self.code else text


class AcpProtocolError(AcpError):
    """协议层错误：帧过大、版本不兼容、字段缺失。"""


class AcpProcessError(AcpError):
    """子进程错误：起不来、中途退出、管道破裂。"""


class AcpTimeoutError(AcpError):
    """请求或整轮超时。"""


@dataclass(frozen=True)
class AcpUsage:
    """一轮（或整个会话）的 token 用量；字段缺失按 0 计。"""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0

    @classmethod
    def from_mapping(cls, raw: Any) -> "AcpUsage":
        """从 ``session/prompt`` 响应的 ``usage`` 对象构造。"""
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            total_tokens=as_int(raw.get("totalTokens")),
            input_tokens=as_int(raw.get("inputTokens")),
            output_tokens=as_int(raw.get("outputTokens")),
            thought_tokens=as_int(raw.get("thoughtTokens")),
            cached_read_tokens=as_int(raw.get("cachedReadTokens")),
            cached_write_tokens=as_int(raw.get("cachedWriteTokens")),
        )

    def to_dict(self) -> dict[str, int]:
        """序列化（写账本／冒烟输出／测试断言用）。"""
        return {
            "totalTokens": self.total_tokens,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "thoughtTokens": self.thought_tokens,
            "cachedReadTokens": self.cached_read_tokens,
            "cachedWriteTokens": self.cached_write_tokens,
        }


@dataclass(frozen=True)
class AcpServerInfo:
    """``initialize`` 的结果：协议版本、agent 标识与认证方式。"""

    protocol_version: int
    agent_name: str = ""
    agent_version: str = ""
    auth_methods: tuple[str, ...] = ()
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "protocolVersion": self.protocol_version,
            "agentName": self.agent_name,
            "agentVersion": self.agent_version,
            "authMethods": list(self.auth_methods),
            "capabilities": dict(self.capabilities),
        }


@dataclass(frozen=True)
class AcpSession:
    """一个 ACP 会话及其公布的可配置项（``model`` / ``reasoning_effort``）。"""

    session_id: str
    cwd: str = ""
    config_options: tuple[Mapping[str, Any], ...] = ()

    def option(self, config_id: str) -> Mapping[str, Any] | None:
        """按 id 找公布的可配置项。"""
        for item in self.config_options:
            if as_text(item.get("id")) == config_id:
                return item
        return None

    def current_value(self, config_id: str) -> str:
        """读某项当前值；不存在给空串。"""
        item = self.option(config_id)
        return as_text(item.get("currentValue")) if item is not None else ""


@dataclass(frozen=True)
class AcpEvent:
    """一条 ``session/update`` 通知的收敛形态（``raw`` 保留原始对象）。"""

    kind: str
    session_id: str = ""
    text: str = ""
    tool_call_id: str = ""
    tool_title: str = ""
    tool_status: str = ""
    used_tokens: int = 0
    context_size: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化（``raw`` 不展开，避免日志被工具结果撑爆）。"""
        return {
            "kind": self.kind,
            "sessionId": self.session_id,
            "text": self.text,
            "toolCallId": self.tool_call_id,
            "toolTitle": self.tool_title,
            "toolStatus": self.tool_status,
            "usedTokens": self.used_tokens,
            "contextSize": self.context_size,
        }


@dataclass(frozen=True)
class AcpPromptResult:
    """一轮 prompt 的结果：停止原因、token 用量、正文与思考文本。

    ``usage_reported`` 说明**响应里到底有没有** ``usage`` 字段：DSH 内核的
    ``session/prompt`` 目前只回 ``stopReason``，这时 ``usage`` 是全 0 的占位值
    （不能当作"真实用量是 0"）。记账方必须用这个布尔区分"没用量"和"用量为零"。
    """

    session_id: str
    stop_reason: str
    usage: AcpUsage
    text: str = ""
    thought: str = ""
    used_tokens: int = 0
    context_size: int = 0
    events: tuple[AcpEvent, ...] = ()
    elapsed_seconds: float = 0.0
    usage_reported: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化（事件默认不展开）。"""
        return {
            "sessionId": self.session_id,
            "stopReason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "usageReported": self.usage_reported,
            "text": self.text,
            "thought": self.thought,
            "usedTokens": self.used_tokens,
            "contextSize": self.context_size,
            "eventCount": len(self.events),
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


def parse_config_options(result: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """从会话响应里取 ``configOptions``；缺省或非法时给空元组。"""
    raw = result.get("configOptions")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def event_from_update(session_id: str, update: Mapping[str, Any]) -> AcpEvent:
    """把 ``session/update`` 里的 ``update`` 对象映射成 :class:`AcpEvent`。"""
    kind = as_text(update.get("sessionUpdate"))
    text = ""
    content = update.get("content")
    if isinstance(content, Mapping) and content.get("type") == "text":
        text = as_text(content.get("text"))
    return AcpEvent(
        kind=kind,
        session_id=session_id,
        text=text,
        tool_call_id=as_text(update.get("toolCallId")),
        tool_title=as_text(update.get("title")),
        tool_status=as_text(update.get("status")),
        used_tokens=as_int(update.get("used")),
        context_size=as_int(update.get("size")),
        raw=update,
    )


def reject_permission(params: Mapping[str, Any]) -> dict[str, Any]:
    """权限请求的默认应答：优先选 reject 选项，否则整体取消（安全优先）。"""
    options = params.get("options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            if as_text(option.get("kind")).startswith("reject"):
                option_id = as_text(option.get("optionId"))
                if option_id:
                    return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return {"outcome": {"outcome": "cancelled"}}
