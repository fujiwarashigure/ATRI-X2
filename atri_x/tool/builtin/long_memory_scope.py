"""Long Memory 的会话作用域：归属记录、检索隔离与跨会话操作授权。

本模块是「长期记忆按 scope 隔离」的**唯一判定入口**（评估文档 §A2 / 决策 4），
被 :mod:`atri_x.tool.builtin.long_memory_store` 与三个 long memory 工具共用：

* **归属**：新写入的记忆带 ``source_scope``（``<platform>:<scope_id>``，例如
  ``qq_onebot:group:123456``）与 ``source_actor``。旧文件没有这两个字段，按
  「全局（未归属会话）」处理，仍然到处可见 —— 升级不能让老记忆失忆。
* **隔离**：检索、分类目录、更新、删除默认只看「本会话 + 全局旧数据」。
* **跨会话**：仅超级管理员，且必须在参数里显式写 ``scope``（``"all"`` 或一个
  具体作用域键）。非超管即使绕过工具可见性也会在这里被拒绝（纵深防御）。
* **无会话上下文**（CLI / 后台任务 / 测试传 ``ctx=None``）：视为不受限，行为与
  本批改动之前一致，避免破坏既有脚本与调用点。

作用域键由平台层给出（``group:`` / ``private:`` / ``c2c:`` / ``channel:`` 前缀
保留），本模块只做拼接与比较，不解析平台语义，因此新增平台不需要改这里。
"""

from __future__ import annotations

from typing import Any

from atri_x.errors import InvalidError
from atri_x.platform import message_context_from
from atri_x.security import RoleSuperadmin, actor_from_context

#: 检索参数 ``scope`` 的取值：显式要求跨会话（全库）。
SCOPE_ALL = "all"

#: 检索参数 ``scope`` 的取值：当前会话（默认值，可用空串代替）。
SCOPE_CURRENT = "current"

#: 没有来源会话的记录（旧文件 / 无会话上下文写入）用的作用域键。
GLOBAL_SCOPE = ""


def scope_key(platform: str, scope_id: str) -> str:
    """把平台与会话 id 拼成作用域键；会话 id 为空时返回 :data:`GLOBAL_SCOPE`。"""
    platform = (platform or "").strip()
    scope_id = (scope_id or "").strip()
    if scope_id == "":
        return GLOBAL_SCOPE
    if platform == "":
        return scope_id
    return platform + ":" + scope_id


def current_scope_key(ctx: Any) -> str:
    """当前调用的会话作用域键；拿不到会话上下文时返回空串（= 不受限）。"""
    msg, ok = message_context_from(ctx)
    if not ok:
        return GLOBAL_SCOPE
    return scope_key(msg.platform, msg.scope_id)


def current_actor_id(ctx: Any) -> str:
    """当前调用的主体 id（``platform:user_id``）；拿不到时返回空串。"""
    actor, ok = actor_from_context(ctx)
    if not ok:
        return ""
    return str(getattr(actor, "id", "") or "")


def is_superadmin(ctx: Any) -> bool:
    """当前调用主体是否超级管理员。"""
    actor, ok = actor_from_context(ctx)
    if not ok:
        return False
    return getattr(actor, "role", "") == RoleSuperadmin


def resolve_scope_filter(ctx: Any, requested: str = "") -> tuple[str, bool]:
    """解析检索参数 ``scope``，返回 ``(目标作用域键, 是否跨会话)``。

    * 空串 / ``"current"`` → 当前会话，不跨会话；
    * ``"all"`` → 全库（跨会话），要求超管；
    * 其他值 → 指定作用域键（跨会话），要求超管。
    """
    requested = (requested or "").strip()
    current = current_scope_key(ctx)
    if requested == "" or requested == SCOPE_CURRENT:
        return current, False
    if not is_superadmin(ctx):
        raise InvalidError(
            "只有超级管理员可以跨会话检索长期记忆；scope 留空或传 current 时只检索当前会话。"
        )
    if requested == SCOPE_ALL:
        return GLOBAL_SCOPE, True
    return requested, True


def scope_allows(record_scope: str, target_scope: str, cross_scope: bool = False) -> bool:
    """一条记录是否落在本次检索范围内。

    目标作用域为空（无会话上下文，或超管传 ``scope=all``）→ 全部可见；
    未归属会话的旧数据 → 到处可见；其余按作用域键相等判定。
    ``cross_scope`` 只用于表达调用方意图（是否跨会话），不影响判定 ——
    是否"跨会话"最终只体现在 ``target_scope`` 上。
    """
    record_scope = (record_scope or "").strip()
    target_scope = (target_scope or "").strip()
    if target_scope == GLOBAL_SCOPE:
        return True
    if record_scope == GLOBAL_SCOPE:
        return True
    return record_scope == target_scope


def scope_label(scope_value: str) -> str:
    """作用域键 → 便于阅读的来源标签。"""
    scope_value = (scope_value or "").strip()
    if scope_value == GLOBAL_SCOPE:
        return "全局（旧数据）"
    platform, sep, scope_id = scope_value.partition(":")
    if sep == "" or scope_id == "":
        return scope_value
    return f"{scope_id}（{platform}）"


def scope_origin_line(record_scope: str, cross_scope: bool) -> str:
    """检索结果里的「来源会话」行；只在跨会话检索时输出，避免默认输出变化。"""
    if not cross_scope:
        return ""
    return f"来源会话：{scope_label(record_scope)}\n"


def check_record_access(ctx: Any, record_scope: str, requested: str = "") -> None:
    """更新/删除前的归属校验（不通过时抛 :class:`InvalidError`）。

    规则与检索一致：本会话、未归属会话的旧数据、以及无会话上下文都可直接操作；
    其他会话的记录必须由超管显式传 ``scope=<来源作用域>`` 才放行。
    """
    record_scope = (record_scope or "").strip()
    if record_scope == GLOBAL_SCOPE:
        return
    current = current_scope_key(ctx)
    if current == GLOBAL_SCOPE or current == record_scope:
        return
    requested = (requested or "").strip()
    if requested == record_scope and is_superadmin(ctx):
        return
    raise InvalidError(
        f"该记忆属于会话 {scope_label(record_scope)}，不在当前会话范围内。"
        f"如确需跨会话操作，超级管理员可显式传 scope={record_scope}。"
    )


__all__ = [name for name in dir() if not name.startswith("_")]
