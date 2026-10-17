"""Actor / Policy 在取消令牌上的传播。

对应 Go：``internal/security/context.go``。

Go 用 ``context.WithValue``；Python 侧等价物是
:meth:`atri_x.safecontext.CancelToken.with_value` / ``value``。这里做**鸭子类型**
访问，所以 ctx 也可以是普通 ``Mapping``（工具层与测试都用这种简化表示）。

键名是跨模块契约：``atri_x.tool.capability`` 直接 import :data:`ACTOR_CONTEXT_KEY`
与 :data:`POLICY_CONTEXT_KEY`，**不再各留一份私有常量** —— 早期两边各有一套私有键
导致"同一件事有两种不互认的表示"，见模块底部的历史注记。
"""

from __future__ import annotations

from typing import Any, Mapping

from atri_x.security.security import Actor, Policy
from atri_x.safecontext import CancelToken

__all__ = [
    "ACTOR_CONTEXT_KEY",
    "POLICY_CONTEXT_KEY",
    "actor_from_context",
    "policy_from_context",
    "with_actor",
    "with_policy",
]

#: 对应 Go 的 ``actorKey``。**公共契约**：其他模块存放/读取 Actor 必须用它。
ACTOR_CONTEXT_KEY = "security_actor"

#: 对应 Go 的 ``policyKey``。**公共契约**。
POLICY_CONTEXT_KEY = "security_policy"


def _context(ctx: Any) -> Any:
    return ctx if ctx is not None else CancelToken()


def _with_value(ctx: Any, key: Any, value: Any) -> Any:
    """对应 Go ``context.WithValue``（对象令牌或不可变 Mapping 副本都支持）。"""
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(key, value)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[key] = value
    return out


def _value(ctx: Any, key: Any, default: Any = None) -> Any:
    """对应 Go ``ctx.Value``。"""
    if ctx is None:
        return default
    getter = getattr(ctx, "value", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:  # 只接受一个参数的自定义实现
            found = getter(key)
            return default if found is None else found
    if isinstance(ctx, Mapping):
        return ctx.get(key, default)
    return default


def with_actor(ctx: Any, actor: Actor) -> Any:
    """把 Actor 挂到令牌上（返回携带该值的子令牌）。对应 Go ``WithActor``。"""
    return _with_value(_context(ctx), ACTOR_CONTEXT_KEY, actor)


def actor_from_context(ctx: Any) -> tuple[Actor, bool]:
    """从令牌上取 Actor。对应 Go ``ActorFromContext``。

    与 Go 一致：命中不到时返回**零值** ``Actor()`` 与 ``False``（不是 ``None``）。
    """
    value = _value(ctx, ACTOR_CONTEXT_KEY)
    if isinstance(value, Actor):
        return value, True
    return Actor(), False


def with_policy(ctx: Any, policy: Policy | None) -> Any:
    """把 Policy 挂到令牌上。对应 Go ``WithPolicy``。"""
    return _with_value(_context(ctx), POLICY_CONTEXT_KEY, policy)


def policy_from_context(ctx: Any) -> tuple[Policy | None, bool]:
    """从令牌上取 Policy。对应 Go ``PolicyFromContext``。

    Go 的 ``ctx.Value(policyKey).(*Policy)`` 在存入 ``nil`` 时会返回
    ``(nil, false)`` —— 指针存进去了但类型断言失败？不：存入 ``(*Policy)(nil)``
    时断言成功、返回 ``(nil, true)``。因此这里用"键是否存在"来区分
    "显式存了 None" 与 "没存"。
    """
    value = _value(ctx, POLICY_CONTEXT_KEY, _MISSING)
    if value is _MISSING:
        return None, False
    if value is None:
        return None, True
    if isinstance(value, Policy):
        return value, True
    return None, False


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return "<missing>"


_MISSING = _Missing()


# ---------------------------------------------------------------------------
# 历史注记（不要重犯）
#
# ``atri_x/tool/capability.py`` 早期在自己的 ``try/except ImportError`` 兜底分支里
# 复制了一份 ``Role/GroupRole/RiskLevel/Actor/Policy`` 与 ``with_actor`` 等函数，
# 并且用的是一套**私有键**。由于该 ``try`` 里请求的若干 PascalCase 名字当时并不存在，
# import 必然失败 → 静默走兜底 → 同一个概念出现两份互不相认的类
# （``isinstance`` 与 ``==`` 全部失效）。
#
# 现在：类型与纯函数只有 ``atri_x.security`` 一份；键名由本模块导出；工具层
# 无条件 import。跨模块比较 ``Actor`` 时不会再出现真假两个类。
# ---------------------------------------------------------------------------
