"""跨库历史 owner 对账。

对应 Go：``internal/media/history.go``。

Chat History 库与主库是**两个** SQLite 文件，历史删除与主库引用维护无法共享事务，
所以这里做一次保守对账：**只释放"历史 owner 已确认消失"的关联**。

* 历史消息查不到（``ErrNotFound``）—— 释放；
* 查到的行 ``id`` 与关联里记的 ``history_id`` 不一致（内部 ID 已被替换，例如平台
  消息 ID 被复用）—— 释放；
* **历史库不可用** —— 抛错。库连不上**不是**"消息被删了"的证据，宁可保守停止
  清理也不能误删引用（``Cleanup`` 因此整体失败）。
"""

from __future__ import annotations

from typing import Any

from atri_x.errors import NotFoundError
from atri_x.media.types import raise_if_cancelled

__all__ = ["PAGE_SIZE", "reconcile_history"]

#: 分页大小。对应 Go ``reconcileHistory`` 里的 ``pageSize``。
PAGE_SIZE = 100


async def reconcile_history(manager: Any, ctx: Any = None) -> None:
    """按主库已有关联分页核对历史 owner。对应 Go ``ReconcileHistory``。"""
    history = manager.history
    if history is None:
        return
    after = ""
    while True:
        raise_if_cancelled(ctx)
        items = manager.store.media().list_history(after, PAGE_SIZE)
        for item in items:
            try:
                row = history.get_by_platform_message(
                    item.platform, item.scope_id, item.message_id
                )
            except NotFoundError:
                row = None
            if row is None or row.id != item.history_id:
                manager.store.media().delete_history(item.owner_id)
        if len(items) < PAGE_SIZE:
            return
        after = items[-1].owner_id
