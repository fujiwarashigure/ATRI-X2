"""安全闸门的"归属"与"按会话模式绑定"（从 :mod:`atri_x.safety.guard` 抽出）。

拆出来的原因：``guard.py`` 已被行数棘轮冻结在 635 行（只降不升），1.1.8 的引擎要往
guard 里加东西，所以先把这两块与判定无关的样板挪出来。

内容：

* :class:`SafetyScope` —— 审计归属（平台 / scope / actor）；
* :func:`scope_from_host` / :func:`scope_from_context` / :func:`with_safety_scope`；
* :func:`mode_from_host` —— 推断当前会话模式（chat / work），逐级回落；
* :func:`with_safety_guard` / :func:`guard_from_context` —— 把 guard 挂进工具 ``ctx``；
* :class:`ModeBoundGuard` —— 绑定到某个会话模式的只读视图。

**不做什么**：不做任何判定。上下文键用模块级 ``object()`` 保证全进程唯一，
所以这些函数必须留在**同一个模块**里，不能各处复制。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from atri_x.safety.guard import SafetyDecision, SafetyGuard
    from atri_x.safety.policy import SafetyPolicy
    from atri_x.safety.rules import RuleSet

__all__ = [
    "SESSION_MODES",
    "ModeBoundGuard",
    "SafetyScope",
    "guard_from_context",
    "mode_from_host",
    "scope_from_context",
    "scope_from_host",
    "with_safety_guard",
    "with_safety_scope",
]

#: 把 guard 挂进 ctx 的键（工具执行路径用；与框架的 ``ctx_with_value`` 同构）。
_SAFETY_GUARD_KEY = object()

#: 把审计归属（平台 / scope / actor）挂进 ctx 的键（工具阶段用）。
_SAFETY_SCOPE_KEY = object()

#: 会话模式（与 ``atri_x.storage.models.SessionMode`` 一致；这里不 import 存储层，
#: 免得 ``atri_x.safety`` 反向依赖运行时）。
SESSION_MODES: tuple[str, ...] = ("chat", "work")


@dataclass(frozen=True)
class SafetyScope:
    """审计里的归属信息：平台 / scope / actor。"""

    platform: str = ""
    scope_id: str = ""
    actor: str = ""


def scope_from_host(host: Any, ctx: Any = None) -> SafetyScope:
    """从宿主（``agent``）与 ``ctx`` 尽力取平台 / scope / actor。

    任何一层缺失或抛错都降级为空串——审计归属信息不值得让一轮对话失败。
    """
    platform = ""
    scope_id = ""
    actor = ""
    if host is not None:
        with contextlib.suppress(Exception):
            helper = getattr(host, "scope", None)
            scope = helper(ctx) if callable(helper) else None
            platform = str(getattr(scope, "platform", "") or "")
            scope_id = str(
                getattr(scope, "platform_scope_id", "") or getattr(scope, "scope_id", "") or ""
            )
        with contextlib.suppress(Exception):
            helper = getattr(host, "actor", None)
            who = helper(ctx) if callable(helper) else None
            actor = str(getattr(who, "id", "") or "")
    return SafetyScope(platform=platform, scope_id=scope_id, actor=actor)


def mode_from_host(host: Any, ctx: Any = None) -> str:
    """尽力推断当前会话模式（``chat`` / ``work``），拿不到时回落 ``work``。

    推断顺序（每一层都容忍缺失 / 抛错）：

    1. ``host.sessions.current(host.scope(ctx)).mode`` —— 当前会话的真实模式；
    2. ``host.group_default_session_mode(ctx)`` —— 新建会话时会用的群级默认模式；
    3. ``host.sessions.default_mode()`` —— 服务默认模式；
    4. ``work`` —— 与 ``SessionRepository.create`` 的默认值一致。
    """
    scope = None
    if host is not None:
        with contextlib.suppress(Exception):
            helper = getattr(host, "scope", None)
            scope = helper(ctx) if callable(helper) else None
        with contextlib.suppress(Exception):
            sessions = getattr(host, "sessions", None)
            if sessions is not None and scope is not None:
                current = sessions.current(scope)
                mode = str(getattr(current, "mode", "") or "").strip().lower()
                if mode in SESSION_MODES:
                    return mode
        with contextlib.suppress(Exception):
            helper = getattr(host, "group_default_session_mode", None)
            if callable(helper):
                mode = str(helper(ctx) or "").strip().lower()
                if mode in SESSION_MODES:
                    return mode
        with contextlib.suppress(Exception):
            sessions = getattr(host, "sessions", None)
            default = getattr(sessions, "default_mode", None)
            if callable(default):
                mode = str(default() or "").strip().lower()
                if mode in SESSION_MODES:
                    return mode
    return SESSION_MODES[-1]


def with_safety_guard(ctx: Any, guard: "SafetyGuard | None") -> Any:
    """把 guard 挂到 ctx 上（工具执行路径用）。``guard`` 为 None 时原样返回。"""
    if guard is None:
        return ctx
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(_SAFETY_GUARD_KEY, guard)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[_SAFETY_GUARD_KEY] = guard
    return out


def guard_from_context(ctx: Any) -> "SafetyGuard | None":
    """从 ctx 取回 :func:`with_safety_guard` 挂上的 guard（没有则 None）。"""
    if ctx is None:
        return None
    getter = getattr(ctx, "value", None)
    if callable(getter):
        return getter(_SAFETY_GUARD_KEY, None)
    if isinstance(ctx, Mapping):
        return ctx.get(_SAFETY_GUARD_KEY)
    return None


def with_safety_scope(ctx: Any, scope: "SafetyScope | None") -> Any:
    """把审计归属挂到 ctx 上（工具阶段拿不到 agent，只能从这里取）。"""
    if scope is None:
        return ctx
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(_SAFETY_SCOPE_KEY, scope)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[_SAFETY_SCOPE_KEY] = scope
    return out


def scope_from_context(ctx: Any) -> "SafetyScope":
    """取回 :func:`with_safety_scope` 挂上的归属；没有则返回空 :class:`SafetyScope`。"""
    if ctx is None:
        return SafetyScope()
    getter = getattr(ctx, "value", None)
    raw = getter(_SAFETY_SCOPE_KEY, None) if callable(getter) else None
    if raw is None and isinstance(ctx, Mapping):
        raw = ctx.get(_SAFETY_SCOPE_KEY)
    return _coerce_scope(raw)


def _coerce_scope(scope: Any) -> SafetyScope:
    """``scope`` 允许 ``None`` / 字符串 / ``SafetyScope`` / ``Mapping`` / 普通对象。"""
    if scope is None:
        return SafetyScope()
    if isinstance(scope, SafetyScope):
        return scope
    if isinstance(scope, str):
        return SafetyScope(scope_id=scope)
    if isinstance(scope, Mapping):
        return SafetyScope(
            platform=str(scope.get("platform", "") or ""),
            scope_id=str(scope.get("scope_id", "") or scope.get("scope", "") or ""),
            actor=str(scope.get("actor", "") or ""),
        )
    return SafetyScope(
        platform=str(getattr(scope, "platform", "") or ""),
        scope_id=str(
            getattr(scope, "platform_scope_id", "") or getattr(scope, "scope_id", "") or ""
        ),
        actor=str(getattr(scope, "actor", "") or getattr(scope, "actor_id", "") or ""),
    )


class ModeBoundGuard:
    """把 :class:`SafetyGuard` 绑定到一个具体会话模式后的只读视图。

    工具执行路径（``image_generate`` / ``send_file`` / 工具结果）拿不到 ``agent``，
    ctx 里只能放 guard；放这个视图后，``guard.enforcing`` / ``filter_text(...)`` /
    ``allow_media_output(...)`` 自动使用该模式的覆盖策略，工具自己不用推导模式。
    """

    def __init__(self, guard: "SafetyGuard", mode: Any) -> None:
        self.guard = guard
        self.mode_name = _normalize_mode(mode)

    def for_mode(self, mode: Any) -> "ModeBoundGuard":
        """换一个模式重新绑定（已绑定视图上再推导模式时用）。"""
        return self.guard.for_mode(mode)

    @property
    def policy(self) -> "SafetyPolicy":
        return self.guard.policy_for(self.mode_name)

    @property
    def rules(self) -> "RuleSet":
        return self.guard.rules_for(self.mode_name)

    @property
    def mode(self) -> str:
        return self.guard.mode_for(self.mode_name)

    @property
    def active(self) -> bool:
        return self.guard.active_for(self.mode_name)

    @property
    def enforcing(self) -> bool:
        return self.guard.enforcing_for(self.mode_name)

    @property
    def block_reply(self) -> str:
        return self.guard.block_reply_for(self.mode_name)

    def should_strip_tool_images(self) -> bool:
        return self.guard.should_strip_tool_images(self.mode_name)

    def evaluate_text(self, text: str, **kwargs: Any) -> "SafetyDecision":
        return self.guard.evaluate_text(text, mode=self.mode_name, **kwargs)

    def filter_text(self, text: str, **kwargs: Any) -> tuple[str, "SafetyDecision"]:
        return self.guard.filter_text(text, mode=self.mode_name, **kwargs)

    def allow_media_output(self, kind: str, **kwargs: Any) -> "SafetyDecision":
        return self.guard.allow_media_output(kind, mode=self.mode_name, **kwargs)

    def stats(self) -> dict[str, int]:
        return self.guard.stats()

    def __getattr__(self, name: str) -> Any:
        # 其余属性（logger / audit / config_dir …）直接透传给底层 guard。
        return getattr(self.guard, name)


def _normalize_mode(mode: Any) -> str:
    """把任意 ``mode`` 归一成 ``""`` / ``"chat"`` / ``"work"``。

    与 :meth:`atri_x.safety.guard.SafetyGuard.normalize_mode` 等价；这里单独实现
    是为了让本模块不必 import :mod:`atri_x.safety.guard`（避免循环依赖）。
    """
    text = str(mode or "").strip().lower()
    return text if text in SESSION_MODES else ""


#: 兼容旧路径：``SafetyGuard.normalize_mode`` 是同一个算法的静态方法。
NORMALIZE_MODE: Callable[[Any], str] = _normalize_mode
