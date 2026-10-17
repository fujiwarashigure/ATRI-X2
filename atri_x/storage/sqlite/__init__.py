"""SQLite 存储实现包。对应 Go：``internal/storage/sqlite``。

Go 的 ``package sqlite`` 有 12 个生产文件；本包按同样的职责切分：

============================ =========================================================
Go 文件                       Python 模块
============================ =========================================================
``store.go``                 :mod:`atri_x.storage.sqlite.store`
``migrations.go``            :mod:`atri_x.storage.sqlite.migrations`
``session_repository.go``    :mod:`atri_x.storage.sqlite.session_repository`
``message_repository.go``    :mod:`atri_x.storage.sqlite.message_repository`
``chat_history_repository.go`` :mod:`atri_x.storage.sqlite.chat_history_repository`
``context_summary_repository.go`` :mod:`atri_x.storage.sqlite.context_summary_repository`
``cron_job_repository.go``   :mod:`atri_x.storage.sqlite.cron_job_repository`
``atrinis_event_repository.go`` :mod:`atri_x.storage.sqlite.atrinis_event_repository`
``tool_call_repository.go``  :mod:`atri_x.storage.sqlite.tool_call_repository`
``media_repository.go``      :mod:`atri_x.storage.sqlite.media_repository`
``media_history.go``         :mod:`atri_x.storage.sqlite.media_history`
``media_lifecycle.go``       :mod:`atri_x.storage.sqlite.media_lifecycle`
============================ =========================================================

对外只有两个门面：主库用 :func:`new`（Go ``sqlite.New``），聊天历史库用
:func:`new_chat_history`（Go ``sqlite.NewChatHistory``）。
"""

from __future__ import annotations

from atri_x.storage.sqlite.atrinis_event_repository import AtrinisEventRepository
from atri_x.storage.sqlite.chat_history_repository import (
    ChatHistoryRepository,
    ChatHistoryStore,
    OutboundMessageRepository,
    migrate_chat_history,
    new_chat_history,
)
from atri_x.storage.sqlite.context_summary_repository import ContextSummaryRepository
from atri_x.storage.sqlite.cron_job_repository import CronJobRepository
from atri_x.storage.sqlite.media_history import MediaHistoryMixin
from atri_x.storage.sqlite.media_lifecycle import MediaLifecycleMixin
from atri_x.storage.sqlite.media_repository import MediaReferenceRepository, MediaRepository
from atri_x.storage.sqlite.message_repository import MessageRepository
from atri_x.storage.sqlite.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    applied_versions,
    apply_migration,
    migrate,
    pending_versions,
)
from atri_x.storage.sqlite.session_repository import SessionRepository
from atri_x.storage.sqlite.store import (
    SQLiteDatabase,
    SQLiteError,
    SQLiteTransaction,
    Store,
    new,
    open_database,
)
from atri_x.storage.sqlite.tool_call_repository import ToolCallRepository

__all__ = [
    "MIGRATIONS",
    "AtrinisEventRepository",
    "ChatHistoryRepository",
    "ChatHistoryStore",
    "ContextSummaryRepository",
    "CronJobRepository",
    "MediaHistoryMixin",
    "MediaLifecycleMixin",
    "MediaReferenceRepository",
    "MediaRepository",
    "MessageRepository",
    "Migration",
    "MigrationError",
    "OutboundMessageRepository",
    "SQLiteDatabase",
    "SQLiteError",
    "SQLiteTransaction",
    "SessionRepository",
    "Store",
    "ToolCallRepository",
    "applied_versions",
    "apply_migration",
    "migrate",
    "migrate_chat_history",
    "new",
    "new_chat_history",
    "open_database",
    "pending_versions",
]
