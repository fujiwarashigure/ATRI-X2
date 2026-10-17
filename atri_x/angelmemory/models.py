"""长期记忆的领域模型（scope-local）。

对应 Go：``internal/angelmemory/store.go`` 的 ``Memory`` / ``SourceFilter`` /
``RecallQuery`` / ``LegacySourceBackfill``，以及 ``service.go`` 的 ``StoreLimits``。

按 ``PORTING.md`` §2 的「单文件 ≤ 600 行」约束，Go 的 ``store.go`` 在这里拆成
:mod:`atri_x.angelmemory.models`（模型）、:mod:`atri_x.angelmemory.schema`
（建表/迁移/文本 helper）与 :mod:`atri_x.angelmemory.store`（``Store`` 本体）。

**来源字段**：``source_kind`` / ``source_actor_id`` / ``source_message_id`` /
``source_session_id`` 是结构化的可追溯链，不是自由文本标签；``source`` 只保留给
"只有人类可读来源"的旧调用方（生产写入方固定为 ``"tool"``）。旧行没有结构化来源，
因此**永远不匹配**按来源的删除与解析（见 :meth:`Store.delete_by_source`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "LegacySourceBackfill",
    "Memory",
    "RecallQuery",
    "SourceFilter",
    "StoreLimits",
]


@dataclass
class Memory:
    """一条可复核、scope 限定的长期记忆。对应 Go ``Memory``。

    ``created_at`` / ``updated_at`` / ``last_accessed_at`` 为 ``None`` 表示 Go 的
    零值时间（由 :meth:`Store.remember` 填充）。
    """

    id: str = ""
    platform: str = ""
    scope_id: str = ""
    content: str = ""
    tags: str = ""
    strength: int = 0
    source: str = ""
    source_kind: str = ""
    source_actor_id: str = ""
    source_message_id: str = ""
    source_session_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None


@dataclass
class SourceFilter:
    """按结构化来源筛选记忆行。对应 Go ``SourceFilter``。

    非空字段之间是 **AND**；全空的过滤器匹配整个 scope。

    这也是 `/forget` 的**库层可见性边界**：普通成员只暴露"来源为自己"的群记忆，
    命令层据此构造 ``SourceFilter(actor_id=自己)``；群主 / 管理员 / 超管用空过滤器
    （整个 scope）。可见性由调用方选择过滤器决定，库只负责**严格按记录到的来源匹配**
    —— 没有来源的旧行不会因为过滤器为空而被"顺带"匹配到，也不会因为过滤器非空而
    被猜出来。
    """

    actor_id: str = ""
    message_id: str = ""
    session_id: str = ""


@dataclass
class RecallQuery:
    """一次召回的参数。对应 Go ``RecallQuery``。"""

    platform: str = ""
    scope_id: str = ""
    text: str = ""
    limit: int = 0
    #: 关键词召回为空时，允许回落到"高强度记忆"兜底。对应 Go ``AllowFallback``。
    allow_fallback: bool = False


@dataclass
class LegacySourceBackfill:
    """旧版来源回填的覆盖面报告。对应 Go ``LegacySourceBackfill``。

    没有结构化来源列的旧行保持原状：它们的成员、消息与 Session 从来没有被记录过，
    所以必须继续**不匹配**按来源的删除，而不是从文本里猜。
    """

    #: 库里记忆行总数。
    total: int = 0
    #: 已经带结构化来源 id 的行数。
    linked: int = 0
    #: ``source_kind`` 可确定的旧行数。
    backfillable: int = 0


@dataclass
class StoreLimits:
    """由 SQLite store 直接执行的写入上限。对应 Go ``StoreLimits``。"""

    max_content_runes: int = 0
    max_per_scope: int = 0
