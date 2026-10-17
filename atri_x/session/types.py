"""会话领域类型与异步命名事件契约。对应 Go：``internal/session/types.go``。

本模块只放定义（与 Go 的 ``types.go`` 对齐）：:class:`Scope` 是全仓共享词汇表里
``atri_x/session/`` 的唯一出口（``PORTING.md`` §3），其余都是 Session 服务的请求/
结果类型与命名通知契约。

移植约定：

* Go 的 ``context.Context`` 参数不进入签名（与 :mod:`atri_x.storage.repositories`
  的约定一致）：本仓的 Session 服务是同步实现，取消由调用方在协程边界处理。
* Go 的 interface → :class:`typing.Protocol`；返回零值 + false 的双返回值不存在，
  因此 :class:`ActivateModeResult` 之类的 struct 保持原样返回。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from atri_x.storage.models import Message, Session

__all__ = [
    "ActivateModeRequest",
    "ActivateModeResult",
    "Config",
    "CreateRequest",
    "NamingCompletedEvent",
    "NamingConfig",
    "NamingFailedEvent",
    "NamingNotifier",
    "NamingScheduledEvent",
    "Scope",
    "Status",
    "TitleGenerator",
    "TitleResult",
]


@dataclass
class Scope:
    """会话作用域。对应 Go ``session.Scope``。

    ``shared`` 标记"群共享线程"作用域：同一平台作用域内的成员共用同一个 current
    Session；``actor_id`` 仍然标识当前操作者，用于鉴权与按用户限流，只有 Session
    的所有权与查找变成共享（见 :mod:`atri_x.session.shared`）。
    """

    actor_id: str = ""
    platform: str = ""
    platform_scope_id: str = ""
    is_cli: bool = False
    shared: bool = False


@dataclass
class CreateRequest:
    """创建会话请求。对应 Go ``session.CreateRequest``。"""

    title: str = ""
    mode: str = ""
    metadata: str = ""


@dataclass
class ActivateModeRequest:
    """模式激活请求。对应 Go ``session.ActivateModeRequest``。"""

    mode: str = ""
    new_session_title: str = ""


@dataclass
class ActivateModeResult:
    """模式激活结果。对应 Go ``session.ActivateModeResult``。"""

    session: Session | None = None
    already_active: bool = False


@dataclass
class Status:
    """当前会话状态摘要。对应 Go ``session.Status``。"""

    session: Session | None = None
    message_count: int = 0
    last_user_preview: str = ""
    last_answer_preview: str = ""


@dataclass
class NamingConfig:
    """命名配置。对应 Go ``session.NamingConfig``（零值 ``trigger_step`` 由服务归一化为 1）。"""

    trigger_step: int = 0


@dataclass
class Config(NamingConfig):
    """服务配置。对应 Go ``session.Config``（内嵌 ``NamingConfig``，故直接继承）。"""

    default_mode: str = ""


@dataclass
class TitleResult:
    """标题生成结果。对应 Go ``session.TitleResult``。"""

    raw_title: str = ""


@runtime_checkable
class TitleGenerator(Protocol):
    """标题生成器。对应 Go ``session.TitleGenerator``。"""

    def generate_title(self, messages: list[Message]) -> TitleResult:
        """按会话消息生成标题（Go 里还收一个 ctx，本仓同步调用不再传递）。"""
        ...


@dataclass
class NamingScheduledEvent:
    """命名已排期事件。对应 Go ``session.NamingScheduledEvent``。"""

    session_id: str = ""
    triggered_at: datetime | None = None
    message_count: int = 0
    trigger_step: int = 0


@dataclass
class NamingCompletedEvent:
    """命名完成事件。对应 Go ``session.NamingCompletedEvent``。"""

    session_id: str = ""
    title: str = ""
    triggered_at: datetime | None = None
    message_count: int = 0


@dataclass
class NamingFailedEvent:
    """命名失败事件。对应 Go ``session.NamingFailedEvent``。"""

    session_id: str = ""
    title: str = ""
    stage: str = ""
    llm_call: str = ""
    generated_title_raw: str = ""
    generated_title_normalized: str = ""
    invalid_reason: str = ""
    reason: str = ""
    err: BaseException | None = None
    triggered_at: datetime | None = None
    message_count: int = 0
    failure_count: int = 0
    max_failures: int = 0
    fallback_applied: bool = False
    fallback_title: str = ""


@runtime_checkable
class NamingNotifier(Protocol):
    """命名事件通知者。对应 Go ``session.NamingNotifier``。"""

    def notify_naming_scheduled(self, event: NamingScheduledEvent) -> None:
        ...

    def notify_naming_completed(self, event: NamingCompletedEvent) -> None:
        ...

    def notify_naming_failed(self, event: NamingFailedEvent) -> None:
        ...
