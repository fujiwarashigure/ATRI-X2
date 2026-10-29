"""把人设卡激活成全局系统人设（写 ``SOUL.md``），以及备份 / 回滚。

`SOUL.md` 是**唯一**的全局身份来源：所有没有绑定专属人设的对话都读它。本模块把
「某个角色卡 → SOUL.md」这条写路径收在一处，桌面端按钮与聊天里的
``character_activate`` 工具共用，避免两边各写一份（格式漂移、备份口径不一致）。

约定：

* 写之前把现有 ``SOUL.md`` 备份到 ``SOUL.md.bak``（只留最近一份；GUI 改写
  ``SOUL.md`` 时自己还会留 ``SOUL.md.gui.bak``）；
* 内容按 Markdown 渲染：标题 + ``profile`` + ``world`` / ``greeting`` / ``examples``
  小节，便于用户之后直接手改；
* 恢复 = 把 ``SOUL.md.bak`` 写回 ``SOUL.md``（同时把当前内容存成
  ``SOUL.md.restore.bak``，避免误操作后无法再回去）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from atri_x.character.store import Character, Store, Viewer, write_file_atomic
from atri_x.errors import ErrForbidden

__all__ = [
    "SOUL_BACKUP_SUFFIX",
    "SOUL_RESTORE_BACKUP_SUFFIX",
    "SoulActivationResult",
    "SoulStatus",
    "activate_character",
    "render_soul_from_character",
    "restore_soul",
    "soul_backup_path",
    "soul_status",
]

#: 激活前的备份后缀（与 GUI 的 ``.gui.bak`` 区分开）。
SOUL_BACKUP_SUFFIX = ".bak"

#: 恢复前把当前 SOUL 另存的后缀。
SOUL_RESTORE_BACKUP_SUFFIX = ".restore.bak"

#: 写进 SOUL.md 里的段落顺序（不包含 image_prompt / notes：它们是生图与备忘，不是身份）。
_SOUL_DOCS = ("profile", "world", "greeting", "examples")


@dataclass
class SoulActivationResult:
    """一次激活的结果。"""

    character: Character
    soul_path: str
    backup_path: str
    written: bool


@dataclass
class SoulStatus:
    """SOUL.md 与备份的当前状态（给界面 / 工具的只读展示）。"""

    soul_path: str
    exists: bool = False
    size: int = 0
    backup_path: str = ""
    backup_exists: bool = False
    backup_size: int = 0


def soul_backup_path(soul_path: str) -> str:
    """激活备份文件路径（``SOUL.md.bak``）。"""
    return str(soul_path or "") + SOUL_BACKUP_SUFFIX


def render_soul_from_character(item: Character) -> str:
    """把角色卡渲染成一份可读的 SOUL.md 文本。"""
    name = (item.name or item.id).strip()
    lines = [f"# 系统人设：{name}（{item.id}）", ""]
    body_parts: list[str] = []
    for kind in _SOUL_DOCS:
        content = str((item.docs or {}).get(kind) or "").strip()
        if not content:
            continue
        if kind == "profile":
            body_parts.append(content)
        else:
            body_parts.append("## " + kind + "\n\n" + content)
    if not body_parts:
        body_parts.append(item.prompt())
    lines.append("\n\n".join(body_parts))
    lines.append("")
    return "\n".join(lines)


def activate_character(
    store: Store,
    ctx: Any,
    character_id: str,
    soul_path: str,
    viewer: Viewer | None = None,
) -> SoulActivationResult:
    """把角色卡写进 ``SOUL.md``。仅超级管理员可调用（这里也做一次兜底）。"""
    person = viewer or Viewer()
    if not person.superadmin:
        raise ErrForbidden
    target = str(soul_path or "").strip()
    if not target:
        raise ValueError("SOUL.md 路径未配置")
    item = store.get_visible(ctx, character_id, person)
    text = render_soul_from_character(item)
    _make_parent(target)
    backup = soul_backup_path(target)
    written = False
    if os.path.exists(target):
        try:
            with open(target, "rb") as handle:
                current = handle.read()
        except OSError as exc:
            raise OSError(f"读取 SOUL.md 失败：{exc}") from exc
        write_file_atomic(backup, current)
    write_file_atomic(target, text.encode("utf-8"))
    written = True
    return SoulActivationResult(
        character=item, soul_path=target, backup_path=backup, written=written
    )


def restore_soul(soul_path: str) -> bool:
    """把 ``SOUL.md.bak`` 恢复回 ``SOUL.md``；没有备份时返回 ``False``。"""
    target = str(soul_path or "").strip()
    backup = soul_backup_path(target)
    if not target or not os.path.exists(backup):
        return False
    try:
        with open(backup, "rb") as handle:
            content = handle.read()
    except OSError as exc:
        raise OSError(f"读取 SOUL 备份失败：{exc}") from exc
    if os.path.exists(target):
        try:
            with open(target, "rb") as handle:
                current = handle.read()
            write_file_atomic(target + SOUL_RESTORE_BACKUP_SUFFIX, current)
        except OSError:
            pass
    write_file_atomic(target, content)
    return True


def soul_status(soul_path: str) -> SoulStatus:
    """读 SOUL.md 与备份的大小（不返回正文，避免把系统提示词带进日志）。"""
    target = str(soul_path or "").strip()
    status = SoulStatus(soul_path=target)
    status.size = _file_size(target)
    status.exists = status.size > 0 or os.path.exists(target)
    status.backup_path = soul_backup_path(target)
    status.backup_size = _file_size(status.backup_path)
    status.backup_exists = os.path.exists(status.backup_path)
    return status


def _file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def _make_parent(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, mode=0o755, exist_ok=True)
