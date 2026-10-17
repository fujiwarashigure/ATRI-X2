"""群共享线程的 Session 所有者、Scope.Shared key 与元数据标记。对应 Go：``internal/session/shared.go``。

一个平台作用域（群）里的所有成员共用同一个 current Session：实现方式是用一个
**合成的稳定 owner**（``group-thread:<platform>:<scope>``）落库，这样既不改动上游
存储 schema 与 owner 索引，又能让同群成员解析到同一个 Session。与之配套的是
metadata 里的 ``thread_mode = "group"`` 标记，访问判定（
:meth:`atri_x.session.service.Service.can_access`）用它区分共享线程 Session 与
普通按用户隔离的 Session。
"""

from __future__ import annotations

import json

from atri_x.session.types import Scope
from atri_x.storage.models import Session

__all__ = [
    "SHARED_THREAD_OWNER_PREFIX",
    "is_shared_thread_session",
    "metadata_object",
    "session_owner_id",
    "shared_thread_metadata_for_scope",
    "shared_thread_owner_id",
]

#: 合成 owner 的前缀。对应 Go ``sharedThreadOwnerPrefix``。
SHARED_THREAD_OWNER_PREFIX = "group-thread:"


def shared_thread_owner_id(scope: Scope) -> str:
    """群共享线程的合成 owner ID。对应 Go ``sharedThreadOwnerID``。"""
    platform = (scope.platform or "").strip()
    scope_id = (scope.platform_scope_id or "").strip()
    return SHARED_THREAD_OWNER_PREFIX + platform + ":" + scope_id


def session_owner_id(scope: Scope) -> str:
    """会话落库时的 owner。对应 Go ``sessionOwnerID``。"""
    if scope.shared:
        return shared_thread_owner_id(scope)
    return scope.actor_id


def shared_thread_metadata_for_scope(scope: Scope, raw: str) -> str:
    """按作用域补齐共享线程标记后的 metadata 文本。

    对应 Go ``sharedThreadMetadataForScope``：以 ``map[string]any`` 解析（未知键
    会保留但重新序列化），共享作用域补 ``thread_mode = "group"``；非共享作用域
    原样重排（Go 的 ``json.Marshal`` 对 map 按 key 排序，这里用
    ``sort_keys=True`` 对齐），解析失败或为空时返回空串。
    """
    metadata = metadata_object(raw)
    if scope.shared:
        metadata["thread_mode"] = "group"
    if not metadata:
        return ""
    try:
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return raw


def is_shared_thread_session(session: Session | None) -> bool:
    """该会话是否是群共享线程 Session。对应 Go ``isSharedThreadSession``。"""
    if session is None or not (session.metadata or "").strip():
        return False
    mode = metadata_object(session.metadata).get("thread_mode")
    if not isinstance(mode, str):
        return False
    return mode.strip().casefold() == "group"


def metadata_object(raw: str) -> dict[str, object]:
    """把 metadata 文本解析成普通字典；非法 JSON / 非对象一律返回空字典。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
