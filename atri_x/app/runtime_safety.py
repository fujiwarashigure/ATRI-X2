"""内容安全与识图的宿主装配（从 :mod:`atri_x.app.runtime_agent` 抽出）。

拆出来的原因：``runtime_agent.py`` 已超 600 行并被 ``scripts/line_budget_baseline.json``
冻结（只降不升），而 1.1.8 的内容安全升级要往装配处加东西。这里先把**纯搬迁**做掉：
行为与 1.1.7 逐行一致，只是位置变了。

职责：

* :func:`wire_safety_and_vision` —— 把内容安全 guard 与识图开关挂到宿主上；
* :func:`build_safety_guard` —— 构造 :class:`atri_x.safety.SafetyGuard`（失败 fail-open）；
* :func:`configure_vision_privacy` —— 把 ``[vision]`` 的图片外发开关装成进程级隐私设置。

**不做什么**：不读配置文件（``cfg`` 是宿主传进来的鸭子类型对象）、不启动服务。
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "_build_safety_guard",
    "build_safety_guard",
    "configure_vision_privacy",
    "safety_guard_needed",
    "wire_safety_and_vision",
]


def _set_agent_attribute(target: Any, name: str, value: Any) -> None:
    """给宿主挂字段；只读/``__slots__`` 宿主静默跳过。"""
    if target is None:
        return
    try:
        setattr(target, name, value)
    except AttributeError:  # pragma: no cover - 只读宿主
        pass


def wire_safety_and_vision(agent: Any, cfg: Any, logger: Any) -> None:
    """把内容安全 guard 与识图开关挂到宿主上。

    不配置 ``[safety] enabled`` / ``[vision] trigger`` 时属性保持默认值，集成点因此
    走"什么都不做"的分支——**不配置就不改变既有行为**。
    """
    _set_agent_attribute(agent, "safety_guard", build_safety_guard(cfg, logger))
    section = getattr(cfg, "vision", None)
    configure_vision_privacy(section, logger)
    trigger = "on_error"
    method = getattr(section, "trigger_value", None)
    if callable(method):
        trigger = str(method())
    elif section is not None:
        trigger = str(getattr(section, "trigger", "") or "on_error").strip().lower()
    if trigger not in ("on_error", "always"):
        trigger = "on_error"
    timeout = 4.0
    timeout_method = getattr(section, "probe_timeout_value", None)
    if callable(timeout_method):
        timeout = float(timeout_method())
    else:
        try:
            timeout = float(getattr(section, "probe_timeout_seconds", 4.0) or 4.0)
        except (TypeError, ValueError):
            timeout = 4.0
    _set_agent_attribute(agent, "vision_trigger", trigger)
    _set_agent_attribute(agent, "vision_probe_timeout_seconds", timeout)
    _set_agent_attribute(
        agent,
        "vision_allow_group_images",
        bool(getattr(section, "allow_group_images", True)),
    )
    detail = str(getattr(section, "detail", "") or "original").strip().lower()
    _set_agent_attribute(
        agent,
        "vision_detail",
        detail if detail in ("original", "high", "auto", "low") else "original",
    )


def configure_vision_privacy(section: Any, logger: Any) -> None:
    """把 ``[vision]`` 的图片外发开关装成**进程级**隐私设置。

    工具路径（``view_image`` / ``image_to_prompt``）不经过 Agent，只认这份进程级
    开关；聊天路径另外还有 ``agent.vision_allow_group_images`` 的前置判定。
    未配置时保持"两个开关都开"（与改造前一致）。
    """
    try:
        from atri_x.vision.privacy import configure_privacy

        configure_privacy(
            allow_external_images=bool(getattr(section, "allow_external_images", True)),
            allow_group_images=bool(getattr(section, "allow_group_images", True)),
        )
    except Exception as exc:  # noqa: BLE001 - 隐私层缺件时退回"不额外限制"
        if logger is not None:
            logger.warn(
                "vision privacy switches unavailable; keeping defaults",
                event="vision_privacy_unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )


def safety_guard_needed(section: Any) -> bool:
    """是否需要装配 guard：全局开了，或任一会话模式的覆盖段把 enabled 打开了。

    默认（``[safety] enabled = false`` 且没有覆盖段）返回 ``False``，即"不配置就
    完全不改变既有行为"——集成点连 guard 都拿不到。
    """
    if section is None:
        return False
    if bool(getattr(section, "enabled", False)):
        return True
    for name in ("chat", "work"):
        override = getattr(section, name, None)
        if override is not None and bool(getattr(override, "enabled", False)):
            return True
    return False


def build_safety_guard(cfg: Any, logger: Any) -> Any:
    """构造内容安全 guard；未启用/依赖缺失时返回 ``None``（fail-open，不阻断启动）。"""
    section = getattr(cfg, "safety", None)
    if not safety_guard_needed(section):
        return None
    try:
        from atri_x.safety import SafetyGuard, policy_from_section

        policy = policy_from_section(section)
        config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
        return SafetyGuard(
            policy,
            logger=logger,
            config_dir=config_dir,
            chat=getattr(section, "chat", None),
            work=getattr(section, "work", None),
        )
    except Exception as exc:  # noqa: BLE001 - 安全层不可用不应让整个服务起不来
        if logger is not None:
            logger.warn(
                "safety guard unavailable; content filtering is disabled",
                event="safety_unavailable",
                error="%s: %s" % (type(exc).__name__, exc),
            )
        return None


#: 兼容旧名（1.1.7 及更早的测试与排障脚本按 ``_build_safety_guard`` 取）。
_build_safety_guard = build_safety_guard
