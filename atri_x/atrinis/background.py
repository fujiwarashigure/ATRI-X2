"""Atrinis 的通用小工具：模型槽位/会话模式白名单、Actor 构造、平台与沙盒子目录。

对应 Go：``internal/atrinis/background.go``。

Go 这个文件名容易误导：它**不是**后台任务的实现（那在 ``llm.go`` 与
``internal/background``），而是一组包内纯函数。Python 侧保持同一落位以便交叉检索。

附带的 Python 侧新增件：

* :func:`maybe_await` —— 本仓的发送器/后台 runner 允许同步或异步实现
  （``atri_x.background.Runner`` 的文档明确写了这一点），所以调用点统一经它
  ``await``，避免每个调用点各写一遍 ``inspect.isawaitable``。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from atri_x.atriena import Event
from atri_x.security import Actor, RoleSuperadmin, actor_id
from atri_x.storage import SessionModeChat, SessionModeWork

__all__ = [
    "ATRINIS_MODEL_SLOTS",
    "ATRINIS_SESSION_MODES",
    "atrinis_actor",
    "atrinis_sandbox_subdir",
    "first_non_empty",
    "first_non_empty_str",
    "first_platform",
    "is_atrinis_model_slot",
    "is_atrinis_session_mode",
    "maybe_await",
    "unique_sorted",
]

#: Atrinis 允许的模型槽位。对应 Go ``isAtrinisModelSlot`` 的 case 列表。
ATRINIS_MODEL_SLOTS = ("atriwisp1", "atriwisp2", "atriwisp3")

#: 合法会话模式。对应 Go ``isAtrinisSessionMode`` 的两个 case。
ATRINIS_SESSION_MODES = (SessionModeWork, SessionModeChat)


def is_atrinis_model_slot(slot: str) -> bool:
    """是否是 Atrinis 专用模型槽位。对应 Go ``isAtrinisModelSlot``。"""
    return (slot or "").strip() in ATRINIS_MODEL_SLOTS


def is_atrinis_session_mode(mode: str) -> bool:
    """是否是合法的会话模式。对应 Go ``isAtrinisSessionMode``。

    Atrinis 只允许 ``work`` / ``chat``（后台任务不接受其它模式）。
    """
    return (mode or "").strip() in ATRINIS_SESSION_MODES



def atrinis_actor(event: Event) -> Actor:
    """Atrinis 事件的权限主体：超级管理员。对应 Go ``atrinisActor``。

    事件来自受控入口（HTTP 令牌 / Hook / 进程内），因此按 superadmin 处理；
    ``display_name`` 记 atriwisp 名，审计里能看出"哪个 atriwisp 触发的"。
    """
    label = event.origin.label() if event.origin is not None else ""
    return Actor(
        id=actor_id("atrinis", label),
        platform="atrinis",
        platform_user_id=label,
        display_name=event.request.atriwisp.name,
        role=RoleSuperadmin,
    )


def first_platform(raw_targets: str) -> str:
    """从落库的 resolved_targets JSON 里取第一个平台名。对应 Go ``firstPlatform``。

    解析失败或没有非空平台时返回空串。注意与 Go 一致：判定用 trim 后的值，
    返回值是**原样**的 ``platform``。
    """
    try:
        targets = json.loads(raw_targets or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(targets, list):
        return ""
    for item in targets:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform")
        if isinstance(platform, str) and platform.strip():
            return platform
    return ""


def first_non_empty(*values: str) -> str:
    """第一个 trim 后非空的值（返回 trim 后的）。对应 Go ``firstNonEmpty``。

    Go 侧同时存在 ``firstNonEmpty`` 与 ``firstNonEmptyStr`` 两个**行为完全相同**
    的函数；Python 只保留一个，并保留两个名字以防交叉检索落空。
    """
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


#: Go ``firstNonEmptyStr`` 的别名（与 ``firstNonEmpty`` 同义）。
first_non_empty_str = first_non_empty


def unique_sorted(values: list[str] | None) -> list[str]:
    """去空白、去重、升序。对应 Go ``uniqueSorted``。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        text = (value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    out.sort()
    return out


def atrinis_sandbox_subdir(atriwisp_name: str) -> str:
    """该 atriwisp 的沙盒子目录（正斜杠）。对应 Go ``atrinisSandboxSubdir``。"""
    return (Path("atrinis") / (atriwisp_name or "").strip()).as_posix()


async def maybe_await(value: Any) -> Any:
    """``await`` 一个可能是同步的值。Python 侧新增件。

    ``atri_x.background.Runner.run_background`` 与 Atrinis 的发送器都允许同步实现
    （测试夹具与 CLI 路径常常是同步的），调用点统一经这里处理。
    """
    if inspect.isawaitable(value):
        return await value
    return value
