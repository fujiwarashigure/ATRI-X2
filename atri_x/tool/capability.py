"""当前请求所用模型的能力声明，并充当本包共享的零依赖底座。

对应 Go：``internal/tool/capability.go``（能力声明部分）。

本模块同时承载两样"必须有唯一出处、又不能造成循环 import"的东西：

1. ``internal/security/security.go`` 的词汇（``Role`` / ``GroupRole`` /
   ``RiskLevel`` / ``Actor`` / ``Policy`` / ``ParseRisk`` / ``CompareRisk`` /
   ``DefaultPolicy`` 及 ctx 存取）。PORTING.md §3 把 ``RiskLevel`` 归到
   ``atri_x/tool/tool.py``，但该文件受"单文件 ≤ 600 行"约束放不下整份词汇表，
   而 ``atri_x/security/`` 是并行任务负责、本包不能创建的模块。
2. ctx 读写助手。Go 用 ``context.WithValue`` / ``ctx.Value``；Python 侧 ctx 是
   鸭子类型：优先用 ctx 自带的 ``with_value`` / ``value`` / ``error``
   （:class:`atri_x.safecontext.CancelToken` 就是这套接口），否则退回不可变
   ``Mapping``（``dict``）表示。键一律是模块级 ``object()`` 哨兵，避免不同来源
   的 ctx 互相覆盖（对应 Go 用未导出结构体类型作 key 的做法）。

:mod:`atri_x.tool.tool` 从本模块导入并原样再导出风险词汇，因此
``from atri_x.tool.tool import RiskLevel`` 与 ``from atri_x.tool import RiskLevel``
都成立；``atri_x.security`` 落地后下面的 try 分支会自动接管，本模块的兜底定义
随之失效（调用点无需改动）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.errors import AtriXError

# 安全词汇只有 atri_x.security 一份（Actor / Policy / Role / GroupRole / RiskLevel
# 与全部纯函数）。**不要再在本文件里复制定义、也不要 try/except 静默兜底**：
# 那样会出现两份互不相认的 Actor，`isinstance` 与 `==` 会在跨模块时失效。
# （这段历史注记见 atri_x/security/context.py 末尾。）
from atri_x.security import (  # noqa: F401  - 大量名字是给调用方再导出的
    ACTOR_CONTEXT_KEY,
    POLICY_CONTEXT_KEY,
    Actor,
    ActorFromContext,
    ActorID,
    CompareRisk,
    DefaultPolicy,
    GroupRole,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
    ParseGroupRole,
    ParseRisk,
    Policy,
    PolicyFromContext,
    RiskCritical,
    RiskHigh,
    RiskLevel,
    RiskLow,
    RiskMedium,
    RiskSafe,
    Role,
    RoleSuperadmin,
    RoleUser,
    WithActor,
    WithPolicy,
    actor_from_context,
    actor_id,
    compare_risk,
    default_policy,
    parse_group_role,
    parse_risk,
    policy_from_context,
    with_actor,
    with_policy,
)


# ---------------------------------------------------------------------------
# 共享的 ctx 读写（Go 的 context.WithValue / ctx.Value / ctx.Err）
# ---------------------------------------------------------------------------

def ctx_with_value(ctx: Any, key: Any, value: Any) -> Any:
    """对应 Go ``context.WithValue``。

    ctx 自带 ``with_value``（:class:`atri_x.safecontext.CancelToken`）时用它；
    否则退回"不可变 Mapping 副本"这一鸭子类型表示。
    """
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(key, value)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[key] = value
    return out


def ctx_value(ctx: Any, key: Any, default: Any = None) -> Any:
    """对应 Go ``ctx.Value``。"""
    if ctx is None:
        return default
    getter = getattr(ctx, "value", None)
    if callable(getter):
        return getter(key, default)
    if isinstance(ctx, Mapping):
        return ctx.get(key, default)
    return default


def ctx_error(ctx: Any) -> BaseException | None:
    """对应 Go ``ctx.Err()``：ctx 未取消时返回 ``None``。"""
    if ctx is None:
        return None
    for name in ("error", "err"):
        getter = getattr(ctx, name, None)
        if callable(getter):
            err = getter()
            if err is not None:
                return err
            break
    if getattr(ctx, "cancelled", False) is True:
        return AtriXError("context cancelled")
    return None


def ctx_raise_if_cancelled(ctx: Any) -> None:
    """对应 Go ``if err := ctx.Err(); err != nil { return err }``。"""
    err = ctx_error(ctx)
    if err is not None:
        raise err


# ---------------------------------------------------------------------------
# internal/tool/capability.go
# ---------------------------------------------------------------------------

_CAPABILITIES_KEY = object()


@dataclass
class Capabilities:
    """模型能接受什么输入。对应 Go ``Capabilities``。

    ``vision`` 为真表示当前模型接受图片输入。
    """

    vision: bool = False


def with_capabilities(ctx: Any, capabilities: Capabilities) -> Any:
    """对应 Go ``WithCapabilities``。返回新的 ctx。"""
    return ctx_with_value(ctx, _CAPABILITIES_KEY, capabilities)


def capabilities_from_context(ctx: Any) -> tuple[Capabilities, bool]:
    """对应 Go ``CapabilitiesFromContext``。"""
    capabilities = ctx_value(ctx, _CAPABILITIES_KEY)
    if isinstance(capabilities, Capabilities):
        return capabilities, True
    return Capabilities(), False


def vision_available(ctx: Any) -> bool:
    """当前模型是否接受图片。对应 Go ``VisionAvailable``。

    没有能力声明的上下文保持历史行为（True），因此从不设置能力的调用方
    （后台任务、测试里的直接调用）不受影响。
    """
    capabilities, ok = capabilities_from_context(ctx)
    if not ok:
        return True
    return bool(capabilities.vision)


__all__ = [name for name in dir() if not name.startswith("_")]
