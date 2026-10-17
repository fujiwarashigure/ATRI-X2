"""存储包。对应 Go：``internal/storage``。

对外出口与 Go 的 ``package storage`` 等价（领域模型 + repository 抽象 + ID/时间 helper）；
SQLite 实现见 :mod:`atri_x.storage.sqlite`（由独立模块提供，本包不 import 它，避免循环依赖）。
"""

from __future__ import annotations

from atri_x.storage.storage import *  # noqa: F401,F403
from atri_x.storage.storage import __all__ as _storage_all

# Go 侧 `ChatHistoryRangeRepository` 与 `OutboundMessageCounter` 都是 storage 包内的
# capability 接口；`OutboundMessageCounter` 已由 storage.py 导出，前者只在
# :mod:`atri_x.storage.repositories` 定义。此处显式再导出，让 `atri_x.historygate`
# 一类的调用方可以直接 `from atri_x.storage import ChatHistoryRangeRepository`。
from atri_x.storage.repositories import ChatHistoryRangeRepository  # noqa: F401

__all__ = list(_storage_all) + ["ChatHistoryRangeRepository"]
