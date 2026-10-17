"""候选挖掘：观察扫描 → token 统计 → 候选 upsert。

对应 Go：``internal/selflearning/store.go`` 的 ``Store.Mine`` 与
``upsertCandidate``（含 ``upsertResult`` 常量）。

挖掘语义（与 Go 逐条一致）：

* CJK n-gram 2–4 只在连续字母/数字段内部跑，ASCII 单词（长度 ≥ 3）每条消息抽一次；
* **每条消息对同一个 token 最多贡献 1 次计数**（否则复读会刷爆计数）；
* 候选必须被至少 ``min_users`` 个**不同用户**支持，且计数 ≥ ``min_count``；
* 扫描受行数（1000）、总字符预算（``max_mine_chars``）与调用方截止时间约束；
* 已经是 ``pending`` 且计数没变的候选择跳过，已裁决（approved/rejected）的候选
  永不因再次挖掘而回退。
"""

from __future__ import annotations

import time
from typing import Any

from atri_x.errors import InvalidError
from atri_x.selflearning.db import transaction
from atri_x.selflearning.models import (
    KindExpression,
    KindJargon,
    MineStats,
    StatusPending,
    TokenStat,
    UpsertCreated,
    UpsertResult,
    UpsertSkipped,
    UpsertUpdated,
)
from atri_x.selflearning.patterns import extract_candidates, is_ascii_word, is_stop_candidate
from atri_x.storage.ids import new_id
from atri_x.storage.times import format_time, now as storage_now

__all__ = ["DEFAULT_MIN_COUNT", "DEFAULT_MIN_USERS", "MAX_MINE_ROWS", "mine", "upsert_candidate"]

#: 候选最小出现次数。对应 Go ``defaultMinCount``。
DEFAULT_MIN_COUNT = 3
#: 候选最小支持用户数。对应 Go ``defaultMinUsers``。
DEFAULT_MIN_USERS = 2
#: 一次挖掘最多读取的观察行数（Go 里是写死的 ``LIMIT 1000``）。
MAX_MINE_ROWS = 1000


def _check_deadline(deadline: float | None) -> None:
    """协作式截止时间检查（Go 的 ``context.WithTimeout`` 在这里对应的位置）。"""
    if deadline is not None and time.monotonic() > deadline:
        from atri_x.safecontext import DeadlineExceededError

        raise DeadlineExceededError("context deadline exceeded")


def mine(
    store: Any,
    platform: str,
    scope_id: str,
    min_count: int,
    min_users: int,
    limit: int,
    deadline: float | None = None,
) -> MineStats:
    """扫描近期观察并创建可复核的高频模式候选。对应 Go ``Store.Mine``。

    ``deadline`` 是 ``time.monotonic()`` 基准的绝对时刻（``None`` 表示不限时）。
    超时抛 :class:`atri_x.safecontext.DeadlineExceededError`：Go 返回
    ``(stats, context.DeadlineExceeded)`` 且调用方在 ``err != nil`` 时丢弃 stats，
    因此抛异常与 Go 的可观测行为一致。
    """
    stats = MineStats()
    connection = store.connection
    if connection is None:
        raise InvalidError("self learning store is not open")
    if min_count <= 0:
        min_count = DEFAULT_MIN_COUNT
    if min_users <= 0:
        min_users = DEFAULT_MIN_USERS
    if limit <= 0 or limit > 500:
        limit = 200
    platform = (platform or "").strip()
    scope_id = (scope_id or "").strip()
    rows = connection.execute(
        """
SELECT text, user_id FROM observations
WHERE platform = ? AND scope_id = ?
ORDER BY created_at DESC
LIMIT ?""",
        (platform, scope_id, MAX_MINE_ROWS),
    ).fetchall()

    counts: dict[str, TokenStat] = {}
    budget = store.limits.max_mine_chars
    for row in rows:
        _check_deadline(deadline)
        text = (row[0] or "").strip()
        size = len(text)
        if budget > 0 and stats.total_chars + size > budget and stats.scanned > 0:
            stats.truncated = True
            break
        stats.total_chars += size
        stats.scanned += 1
        user_id = (row[1] or "").strip()
        seen: set[str] = set()
        for token in extract_candidates(text):
            if token in seen:
                continue
            seen.add(token)
            entry = counts.get(token)
            if entry is None:
                entry = TokenStat()
                counts[token] = entry
            entry.count += 1
            if user_id != "":
                entry.users.add(user_id)

    ordered: list[tuple[str, int, int]] = []
    for token, entry in counts.items():
        if entry.count < min_count or len(entry.users) < min_users:
            continue
        if is_stop_candidate(token):
            continue
        ordered.append((token, entry.count, len(entry.users)))
    # 计数降序 → 用户数降序 → token 升序（Go 的 sort.Slice 比较器）。
    ordered.sort(key=lambda item: (-item[1], -item[2], item[0]))
    if len(ordered) > limit:
        ordered = ordered[:limit]

    for token, count, users in ordered:
        _check_deadline(deadline)
        kind = KindJargon if is_ascii_word(token) else KindExpression
        result = upsert_candidate(store, kind, platform, scope_id, token, count, users)
        if result == UpsertCreated:
            stats.created += 1
        elif result == UpsertUpdated:
            stats.updated += 1
        else:
            stats.skipped += 1
    return stats


def upsert_candidate(
    store: Any,
    kind: str,
    platform: str,
    scope_id: str,
    pattern: str,
    count: int,
    user_count: int,
) -> UpsertResult:
    """插入或刷新一条候选。对应 Go ``Store.upsertCandidate``。

    SELECT 与 INSERT/UPDATE 在同一个事务里，因此两个并发挖掘过程不会同时判定
    "候选不存在"再各自插入（唯一键会撞车）。
    """
    connection = store.connection
    platform = (platform or "").strip()
    scope_id = (scope_id or "").strip()
    with transaction(connection):
        row = connection.execute(
            """
SELECT id, status, count, user_count FROM candidates
WHERE kind = ? AND platform = ? AND scope_id = ? AND pattern = ?""",
            (kind, platform, scope_id, pattern),
        ).fetchone()
        if row is None:
            stamp = format_time(storage_now())
            connection.execute(
                """
INSERT INTO candidates (id, kind, platform, scope_id, pattern, meaning, count, user_count, status, created_at, updated_at, reviewed_at, reviewed_by)
VALUES (?, ?, ?, ?, ?, '', ?, ?, 'pending', ?, ?, '', '')""",
                (new_id(), kind, platform, scope_id, pattern, count, user_count, stamp, stamp),
            )
            return UpsertCreated
        existing_id, existing_status = row[0], row[1]
        existing_count, existing_user_count = int(row[2] or 0), int(row[3] or 0)
        if existing_status != StatusPending:
            return UpsertSkipped
        if count == existing_count and user_count == existing_user_count:
            return UpsertSkipped
        connection.execute(
            "UPDATE candidates SET count = ?, user_count = ?, updated_at = ? WHERE id = ?",
            (count, user_count, format_time(storage_now()), existing_id),
        )
        return UpsertUpdated
