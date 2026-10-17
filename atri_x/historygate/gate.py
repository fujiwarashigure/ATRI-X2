"""群聊历史写入闸门。

对应 Go：``internal/historygate/gate.go``。

**为什么要有这个包**（Go 的包注释，逐条译出）：把"这一行聊天历史到底能不能落库"
的判定收在一处。平台适配器只拿到一个仓储对象，而那个仓储在应用启动时被包装了
**一次**——于是将来新加的适配器不可能"忘记"先问当前群策略再写入。

两条不可动的语义：

* :class:`Policy` 未安装回调（``fn is None``）时**一律放行**，保持测试与未配置
  群策略的部署行为不变；
* 读操作**永远委托**。关掉历史不会删除或隐藏已有记录——只有新写入会被丢掉，
  而且丢得**静默**（``append`` 返回 ``None`` 而不是抛错）。

**与 Go 的实现差异（Python 必需）**：

* Go 的 ``Policy`` 用 ``sync.RWMutex`` 保护回调字段。Python 标准库没有读写锁，
  这里用 :class:`threading.Lock`；本场景是一次属性读写，读多写少的收益可以忽略。
* Go 里 ``p == nil`` 时 ``p.Allowed(...)`` / ``p.Set(...)`` 是合法的 nil-receiver
  调用。Python 在 ``None`` 上调用方法会 ``AttributeError``，因此额外提供
  :func:`policy_allowed` / :func:`policy_set` 两个模块级助手来承担这份语义；
  实例方法保留，供非 nil 场景直接使用。
* Go 用**类型断言**（``r.repo.(storage.ChatHistoryRangeRepository)``）探测可选能力；
  Python 侧对应 ``atri_x.storage`` 里已经 ``@runtime_checkable`` 的 Protocol，
  因此用 :func:`isinstance` 探测（运行时检查方法名集合）。
* Go 的包内私有哨兵 ``errRangeUnsupported`` 按 PORTING §1.5 收进
  :mod:`atri_x.errors`（:class:`~atri_x.errors.RangeUnsupportedError`），本模块再导出。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from atri_x.errors import RangeUnsupportedError
from atri_x.storage import (
    ChatHistoryRangeRequest,
    ChatHistoryRepository,
    OutboundMessageCounter,
    OutboundMessageRangeRequest,
    OutboundMessageRepository,
)

# ``ChatHistoryRangeRepository`` 定义在 ``atri_x.storage.repositories``，但当前
# ``atri_x.storage`` 的 ``__init__`` 没有把它再导出（``OutboundMessageCounter`` 有）。
# 定义处就是唯一出处，因此直接从定义模块 import；集成期若 storage 补上再导出，
# 这里可以改回 ``from atri_x.storage import ...``。
from atri_x.storage.repositories import ChatHistoryRangeRepository

__all__ = [
    "ChatRepository",
    "OutboundRepository",
    "Policy",
    "RangeUnsupportedError",
    "new_policy",
    "policy_allowed",
    "policy_set",
    "wrap_chat",
    "wrap_outbound",
]


class Policy:
    """历史写入策略。对应 Go ``Policy``。

    ``fn(platform, scope_id) -> bool``；未安装时（``None``）允许一切。
    """

    __slots__ = ("_fn", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fn: Callable[[str, str], bool] | None = None

    def set(self, fn: Callable[[str, str], bool] | None) -> None:
        """安装（或清除）策略回调。对应 Go ``Set``。"""
        with self._lock:
            self._fn = fn

    def allowed(self, platform: str, scope_id: str) -> bool:
        """该 ``platform + scope_id`` 是否允许写入新历史。对应 Go ``Allowed``。"""
        with self._lock:
            fn = self._fn
        if fn is None:
            return True
        return bool(fn((platform or "").strip(), (scope_id or "").strip()))

    def callback(self) -> Callable[[str, str], bool] | None:
        """当前策略回调（``None`` 表示允许一切）。"""
        with self._lock:
            return self._fn


def new_policy() -> Policy:
    """空策略：在 ``set`` 之前允许一切。对应 Go ``NewPolicy``。"""
    return Policy()


def policy_set(policy: Policy | None, fn: Callable[[str, str], bool] | None) -> None:
    """安装策略回调（``policy`` 为 ``None`` 时是 no-op）。对应 Go 的 nil-receiver ``Set``。"""
    if policy is None:
        return None
    policy.set(fn)
    return None


def policy_allowed(policy: Policy | None, platform: str, scope_id: str) -> bool:
    """查询是否允许写入（``policy`` 为 ``None`` 时允许一切）。

    对应 Go 的 nil-receiver ``Allowed``。两边都会先 trim 再交给回调。
    """
    if policy is None:
        return True
    return policy.allowed(platform, scope_id)


class ChatRepository:
    """按策略丢弃入站历史写入的仓储包装。对应 Go ``ChatRepository``。

    读操作全部委托；``append`` 在策略拒绝时**静默丢弃**。
    """

    __slots__ = ("_policy", "_repo")

    def __init__(self, repo: ChatHistoryRepository, policy: Policy) -> None:
        self._repo = repo
        self._policy = policy

    def append(self, message: Any) -> None:
        """写入一条历史（策略拒绝时丢弃）。对应 Go ``ChatRepository.Append``。"""
        if self._repo is None or message is None:
            return None
        if not policy_allowed(
            self._policy,
            getattr(message, "platform", ""),
            getattr(message, "platform_scope_id", ""),
        ):
            return None
        self._repo.append(message)
        return None

    def get_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> Any:
        """按平台消息 ID 取（永远委托）。对应 Go ``GetByPlatformMessage``。"""
        return self._repo.get_by_platform_message(platform, scope_id, platform_message_id)

    def search(self, req: Any) -> list[Any]:
        """检索（永远委托）。对应 Go ``Search``。"""
        return self._repo.search(req)

    def around(self, req: Any) -> list[Any]:
        """取上下文窗口（永远委托）。对应 Go ``Around``。"""
        return self._repo.around(req)

    def delete_before(self, cutoff: datetime) -> int:
        """按时间删除（永远委托）。对应 Go ``DeleteBefore``。"""
        return self._repo.delete_before(cutoff)

    def list_range(self, req: ChatHistoryRangeRequest) -> list[Any]:
        """按时间窗批量读取；被包装仓储不支持时抛 :class:`RangeUnsupportedError`。

        对应 Go ``ChatRepository.ListRange``：这个方法**总是存在**（Go 的注释
        解释了原因——调用方可以探测一次再决定是否降级，不必面对
        ``AttributeError``）。
        """
        if not isinstance(self._repo, ChatHistoryRangeRepository):
            raise RangeUnsupportedError(
                "history range access is not supported by the wrapped repository"
            )
        return self._repo.list_range(req)


class OutboundRepository:
    """按同一策略过滤已发送助手消息的仓储包装。对应 Go ``OutboundRepository``。

    出站行是"真的发出去过的助手消息"，同属持久化聊天历史面，因此共用一份策略。
    """

    __slots__ = ("_policy", "_repo")

    def __init__(self, repo: OutboundMessageRepository, policy: Policy) -> None:
        self._repo = repo
        self._policy = policy

    def append(self, message: Any) -> None:
        """写入一条已发送消息（策略拒绝时丢弃）。对应 Go ``OutboundRepository.Append``。"""
        if self._repo is None or message is None:
            return None
        if not policy_allowed(
            self._policy,
            getattr(message, "platform", ""),
            getattr(message, "platform_scope_id", ""),
        ):
            return None
        self._repo.append(message)
        return None

    def list_range(self, req: OutboundMessageRangeRequest) -> list[Any]:
        """按时间窗批量读取（永远委托）。对应 Go ``OutboundRepository.ListRange``。"""
        return self._repo.list_range(req)

    def delete_before(self, cutoff: datetime) -> int:
        """按时间删除（永远委托）。对应 Go ``OutboundRepository.DeleteBefore``。"""
        return self._repo.delete_before(cutoff)

    def count_range(self, req: OutboundMessageRangeRequest) -> int:
        """统计时间窗内条数；不支持时抛 :class:`RangeUnsupportedError`。

        对应 Go ``OutboundRepository.CountRange``。
        """
        if not isinstance(self._repo, OutboundMessageCounter):
            raise RangeUnsupportedError(
                "history range access is not supported by the wrapped repository"
            )
        return self._repo.count_range(req)


def wrap_chat(
    repo: ChatHistoryRepository | None, policy: Policy | None
) -> ChatHistoryRepository | None:
    """按策略包装入站历史仓储。对应 Go ``WrapChat``（``None`` 原样返回）。"""
    if repo is None:
        return None
    return ChatRepository(repo, policy if policy is not None else new_policy())


def wrap_outbound(
    repo: OutboundMessageRepository | None, policy: Policy | None
) -> OutboundMessageRepository | None:
    """按策略包装出站消息仓储。对应 Go ``WrapOutbound``（``None`` 原样返回）。"""
    if repo is None:
        return None
    return OutboundRepository(repo, policy if policy is not None else new_policy())
