"""协议能力矩阵的唯一入口。

对应 Go：``internal/llm/protocol.go``。

刻意区分「协议理论上支持」与「fork 真的用上了」：Responses 适配器是协议
翻译层（固定 ``store=false``、每轮重放完整历史），因此它报告
``responses`` 但所有服务端能力都是 False。上层据此分派（例如压缩走客户端
还是服务端），换基时只需要改这里的返回值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol as _TypingProtocol, runtime_checkable

__all__ = [
    "Protocol",
    "ProtocolCapabilities",
    "ProtocolCapabilityReporter",
    "ProtocolChatCompletions",
    "ProtocolResponses",
    "protocol_capabilities_of",
]


class Protocol:
    """标识适配器实际使用的线上协议。对应 Go ``Protocol``。"""

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"

    ALL = (CHAT_COMPLETIONS, RESPONSES)


# Go 侧常量名逐一对齐。
ProtocolChatCompletions = Protocol.CHAT_COMPLETIONS
ProtocolResponses = Protocol.RESPONSES


@dataclass
class ProtocolCapabilities:
    """对应 Go ``ProtocolCapabilities``。"""

    protocol: str = ""
    #: 协议可以在服务端延续同一段对话（增量引用上一轮响应），而不是每轮重放完整历史。
    server_side_conversation: bool = False
    #: 压缩可以交给服务端完成，客户端不必自己生成摘要。
    native_compaction: bool = False
    #: 工具定义可以增量下发，而不必每轮重发完整定义。
    incremental_tools: bool = False
    #: 服务端会保存这次响应并允许回读。
    server_side_store: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "server_side_conversation": self.server_side_conversation,
            "native_compaction": self.native_compaction,
            "incremental_tools": self.incremental_tools,
            "server_side_store": self.server_side_store,
        }


@runtime_checkable
class ProtocolCapabilityReporter(_TypingProtocol):
    """对应 Go ``ProtocolCapabilityReporter``：适配器可选实现的查询入口。

    与 ``RetryNotifier`` / ``ModelMetadataProvider`` 同一套可选接口模式：
    不实现也能正常工作，由 :func:`protocol_capabilities_of` 给保守默认值。
    """

    def protocol_capabilities_for(self, model: str) -> ProtocolCapabilities: ...


def protocol_capabilities_of(client: Any, model: str) -> ProtocolCapabilities:
    """回答「这个客户端、这个模型实际支持哪些协议能力」。

    对应 Go ``ProtocolCapabilitiesOf``。Go 用接口断言，Python 用同名方法
    的存在性判定（等价，且对只挂在实例上的方法也成立）。没有实现查询接口的
    适配器按最弱的 Chat Completions 语义处理，调用方永远拿到保守答案。
    """
    reporter = getattr(client, "protocol_capabilities_for", None)
    if callable(reporter):
        caps = reporter(model)
        if caps is None:
            caps = ProtocolCapabilities()
        if not caps.protocol:
            # 空 protocol 会让分派点的默认分支失效，补上最弱协议。
            caps.protocol = ProtocolChatCompletions
        return caps
    return ProtocolCapabilities(protocol=ProtocolChatCompletions)
