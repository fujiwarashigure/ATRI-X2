"""DSH 的目录解析与子进程环境收敛（从 runtime 拆出，见 600 行约定）。

两件事都属于"进程参数"而不是"轮次调度"：

- :func:`resolve_paths`：``[agent.dsh].workspace`` / ``home`` 相对路径按配置目录展开；
- :func:`child_environment`：子进程**不继承**整份 ``os.environ``，先用
  :meth:`atri_x.processenv.Environment.without_sensitive_keys` 过滤 ``*_KEY`` /
  ``*_TOKEN`` / ``*SECRET*`` 之类的名字，再叠回调用方明确要注入的凭据；
- :func:`allow_permission`：``allow_permissions=true`` 时对权限请求选第一个 allow
  选项（默认拒绝在 :mod:`atri_x.agent.dsh.client`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.agent.dsh.router import DshRouteConfig
from atri_x.agent.dsh.types import as_text
from atri_x.processenv import new as new_process_env

__all__ = [
    "WorkspacePaths",
    "allow_permission",
    "child_environment",
    "config_dir_of",
    "resolve_paths",
]


@dataclass(frozen=True)
class WorkspacePaths:
    """DSH 需要的两个目录：会话工作区与 ``DSH_HOME``。"""

    workspace: str
    home: str


def config_dir_of(agent: Any) -> str:
    """本轮配置目录（``state.toml`` 所在目录；拿不到时退回当前目录）。"""
    state_path = str(getattr(agent, "state_path", "") or "").strip()
    if state_path:
        return os.path.dirname(os.path.abspath(state_path))
    return os.getcwd()


def resolve_paths(agent: Any, config: DshRouteConfig) -> WorkspacePaths:
    """解析工作区与 ``DSH_HOME``；相对路径按配置目录展开。"""
    base = config_dir_of(agent)

    def resolve(value: str, fallback: str) -> str:
        text = (value or "").strip()
        path = text if text else fallback
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        return os.path.abspath(path)

    return WorkspacePaths(
        workspace=resolve(config.workspace, os.path.join("dsh", "workspace")),
        home=resolve(config.home, os.path.join("dsh", "home")),
    )


def allow_permission(params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``[agent.dsh].allow_permissions=true`` 时的权限应答：选第一个 allow 选项。"""
    options = params.get("options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            if as_text(option.get("kind")).startswith("allow"):
                option_id = as_text(option.get("optionId"))
                if option_id:
                    return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return None


def child_environment(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """给 DSH 子进程构造环境：先收敛掉继承来的凭据，再放回显式注入项。

    子进程不该无脑继承整份 ``os.environ``（内核日志里就会出现宿主的密钥变量），
    因此复用 :mod:`atri_x.processenv` 的 :meth:`Environment.without_sensitive_keys`
    过滤 ``*_KEY`` / ``*_TOKEN`` / ``*SECRET*`` 之类的名字，再叠回调用方
    **明确要注入**的变量（例如 ATRI-X 自己从 ``.env`` 解析出来的 provider 密钥）。
    ``extra_env`` 的值覆盖继承值，空值不进环境。
    """
    entries = [f"{name}={value}" for name, value in os.environ.items()]
    filtered = new_process_env(entries).without_sensitive_keys()
    overlay = {
        str(name): str(value)
        for name, value in (extra_env or {}).items()
        if str(name).strip() and str(value)
    }
    if overlay:
        filtered = filtered.overlay(overlay)
    out: dict[str, str] = {}
    for entry in filtered.environ():
        name, sep, value = entry.partition("=")
        if sep and name:
            out[name] = value
    return out
