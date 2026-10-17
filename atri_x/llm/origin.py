"""「这段已有历史能不能交给目标模型」的唯一入口。

对应 Go：``internal/llm/origin.go``。

``Origin`` 是「能不能换模型」的比较单位：会话 metadata 里记录的
``llm_origin``、以及目标 provider/model 解析出的归属，都归约成这个形状，
因此切换裁决只有这一个入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.errors import AtriXError
from atri_x.llm.protocol import ProtocolChatCompletions

__all__ = ["Origin", "OriginError", "can_switch"]


@dataclass
class Origin:
    """对应 Go ``Origin``。

    ``provider`` 为空表示来源没有记录厂商（迁移前的旧数据），按「未知」处理。
    """

    protocol: str = ""
    provider: str = ""

    def __bool__(self) -> bool:
        return bool(self.protocol or self.provider)

    def to_dict(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "provider": self.provider}

    @classmethod
    def from_dict(cls, raw: Any) -> "Origin":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()
        return cls(
            protocol=str(raw.get("protocol") or ""),
            provider=str(raw.get("provider") or ""),
        )


class OriginError(AtriXError):
    """来源与目标不兼容。对应 Go ``CanSwitch`` 返回的 error。"""


def can_switch(source: Origin, target: Origin) -> None:
    """回答「这段已有材料能不能交给目标模型继续」。

    对应 Go ``CanSwitch``。只做比较：不解析模型、不改选择、不看会话状态，
    也不读写任何存储。规则与上游 Elflare/elbot 的 ``modelmgr.CanSwitch`` 一致，
    判定顺序：

    1. 目标缺协议或缺厂商：无法判断，直接拒绝（配置错误，不该静默放行）；
    2. 来源没记协议：放行（旧会话没有这个事实，宁可让它试一次）；
    3. 同协议且（是 chat_completions 或同厂商）：放行——chat 材料在厂商之间
       可移植；responses 材料的续链依赖厂商侧状态，跨厂商不可移植；
    4. 其余：拒绝。

    拒绝时抛 :class:`OriginError`（对应 Go 返回非 nil error）。
    """
    if not target.protocol or not target.provider:
        raise OriginError("target model is missing protocol or provider")
    if not source.protocol:
        return
    if source.protocol == target.protocol and (
        source.protocol == ProtocolChatCompletions or source.provider == target.provider
    ):
        return
    source_provider = source.provider or "unknown"
    raise OriginError(
        f"session context {source_provider}/{source.protocol} is not compatible with "
        f"target model {target.provider}/{target.protocol}; start a new session or "
        "switch back to a compatible model"
    )
