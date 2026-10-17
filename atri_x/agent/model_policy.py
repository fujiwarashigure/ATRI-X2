"""执行前的模型授权（群模型目录）。

对应 Go：``internal/agent/model_policy.go``。

群策略在执行路径开始前就已解析，但 hook、turn override、cron override 与透明回退都
可能在之后改掉选择。本模块的裁决发生在**真正发起 provider 请求之前**，因此受目录
约束的是实际跑起来的模型，而不只是 ``/model`` 菜单。

宿主（``Agent``）按鸭子类型提供（均由 ``group_policy`` 侧实现）：

* ``self.group_runtime_blocked(ctx)``：当前群是否已暂停付费调用
* ``self.is_group_scope(ctx)`` / ``self.scope(ctx)``
* ``self.group_policy_for_scope(scope)``：``allowed_models`` / ``default_model``
* ``self.resolve_group_model_selection(name) -> (ModelSelection, bool)``
* ``self.group_model_selection_allowed(policy, selection, label) -> bool``
"""

from __future__ import annotations

from typing import Any, Callable

from atri_x.config import ModelSelection
from atri_x.errors import InvalidError

__all__ = ["ModelPolicyMixin", "authorize_execution_model_selection"]


class ModelPolicyMixin:
    """``internal/agent/model_policy.go`` 里挂在 ``*Agent`` 上的方法。"""

    def authorize_execution_model_selection(
        self, ctx: Any, selection: ModelSelection
    ) -> None:
        """最后一道模型授权。对应 Go ``authorizeExecutionModelSelection``。

        通过时返回 ``None``，拒绝时抛 :class:`atri_x.errors.InvalidError`
        （消息与 Go 逐字一致，会直接展示给用户）。
        """
        authorize_execution_model_selection(self, ctx, selection)


def authorize_execution_model_selection(
    host: Any, ctx: Any, selection: ModelSelection
) -> None:
    """:meth:`ModelPolicyMixin.authorize_execution_model_selection` 的独立实现。"""
    if host is None:
        return
    if _call(host, "group_runtime_blocked", ctx):
        raise InvalidError("当前群已暂停模型调用：机器人被禁言、已离群或状态不可用")
    if not _call(host, "is_group_scope", ctx):
        return
    policy = _call(host, "group_policy_for_scope", _call(host, "scope", ctx))
    catalog = list(getattr(policy, "allowed_models", None) or [])
    default_model = str(getattr(policy, "default_model", "") or "").strip()
    if not catalog and not default_model:
        # 没有显式群模型策略：完全沿用全局模型，未选择加入的群不受影响。
        return
    provider = str(getattr(selection, "provider", "") or "").strip()
    model = str(getattr(selection, "model", "") or "").strip()
    if not provider or not model:
        raise InvalidError("模型未配置或不可用")
    if default_model:
        resolved, ok = _resolve_default(host, default_model)
        if ok and resolved.provider == provider and resolved.model == model:
            return
    label = provider + "/" + model
    if catalog:
        allowed = getattr(host, "group_model_selection_allowed", None)
        if callable(allowed) and allowed(
            policy, ModelSelection(provider=provider, model=model), label
        ):
            return
    if not catalog:
        raise InvalidError(
            f"模型 {label} 不在本群默认模型内；"
            "请让超级管理员配置 /grouppolicy default-model 或 allowed-models"
        )
    raise InvalidError(
        f"模型 {label} 不在本群模型目录内；请让超级管理员先配置 /grouppolicy allowed-models"
    )


def _resolve_default(host: Any, name: str) -> tuple[ModelSelection, bool]:
    resolver: Callable[..., Any] | None = getattr(
        host, "resolve_group_model_selection", None
    )
    if not callable(resolver):
        return ModelSelection(), False
    result = resolver(name)
    if isinstance(result, tuple) and len(result) == 2:
        selection, ok = result
        return selection, bool(ok)
    if result is None:
        return ModelSelection(), False
    return result, True


def _call(host: Any, name: str, *args: Any) -> Any:
    fn = getattr(host, name, None)
    if not callable(fn):
        return None
    return fn(*args)
