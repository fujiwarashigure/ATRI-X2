"""Agent 构造期的支撑件：默认配置、模型运行态、构造入口。

对应 Go：``internal/agent/core.go`` 里的 ``New`` / ``NewWithPrefixes`` /
``NewWithOptions`` 与 ``newModelRuntimeState`` / ``responseTimeout`` /
``durationFromSeconds`` 等自由函数，以及 ``soul`` 的文件实现。
本模块不 import :mod:`atri_x.agent.core` 的运行时状态，只借用其中的类型；
``core.py`` 在文件末尾把这些名字再导出，因此 ``from atri_x.agent.core import
new_options`` 依然成立。
"""

from __future__ import annotations

from typing import Any
from atri_x.completion import Service as CompletionService
from atri_x.agent.options import Options
from atri_x.session import Config as SessionConfig
from atri_x.storage import Store
from dataclasses import dataclass, field

from atri_x.agent.core import (
    Agent,
    SoulProvider,
)


class _ToolRuntime:
    """工具运行时的最小容器。对应 Go ``toolRuntimeState``。

    真实的注册表/管理器由同僚 ``atri_x/agent/tools.py`` 装配；这里只保证
    ``registry`` / ``manager`` / ``config`` 三个字段存在且可被覆盖。
    """

    __slots__ = ("registry", "manager", "config", "provider", "default_provider")

    def __init__(self) -> None:
        self.registry: Any = None
        self.manager: Any = None
        self.config: Any = _default_tools_config()
        self.provider: Any = None
        self.default_provider = True


def _default_tools_config() -> Any:
    from atri_x.config import ToolsConfig

    return ToolsConfig()


def _default_turn_directives() -> Any:
    from atri_x.config import TurnDirectivesConfig

    return TurnDirectivesConfig()


def _work_model(agent: Agent) -> str:
    selection = (agent.mode_models or {}).get("work")
    return getattr(selection, "model", "") if selection is not None else ""


def _work_provider(agent: Agent) -> str:
    selection = (agent.mode_models or {}).get("work")
    return getattr(selection, "provider", "") if selection is not None else ""


def _response_timeout(cfg: Any) -> float:
    if cfg is None:
        return 0.0
    seconds = int(getattr(cfg, "response_timeout_seconds", 0) or 0)
    return float(seconds) if seconds > 0 else 0.0


def _seconds(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if seconds > 0 else 0.0


def _file_soul_provider(path: str) -> SoulProvider:
    """从文件读灵魂文本。对应 Go ``FileSoulProvider``（懒读，读取失败回退固定文本）。"""
    return FileSoulProvider(path)


@dataclass
class FileSoulProvider:
    """文件灵魂文本来源。对应 Go ``FileSoulProvider``。"""

    path: str = ""

    def system_prompt(self, ctx: Any, scope_id: str) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return "You are a helpful assistant."


def _new_model_runtime(agent: Agent) -> Any:
    """模型运行时状态。优先用同僚 ``atri_x/agent/model.py`` 的实现。"""
    provider_name = _work_provider(agent)
    try:
        from atri_x.agent.model import ModelRuntimeState

        return ModelRuntimeState(
            client=agent.client_for_provider(provider_name),
            model=_work_model(agent),
            provider_name=provider_name,
            provider=agent.providers.get(provider_name),
            providers=agent.providers,
            mode_models=agent.mode_models,
            clients=agent.clients,
        )
    except Exception:  # noqa: BLE001 - 未落地/签名不同就退回最小实现
        return _FallbackModelRuntime(
            client=agent.clients.get(provider_name),
            model=_work_model(agent),
            provider=provider_name,
            provider_config=agent.providers.get(provider_name),
            providers=dict(agent.providers),
            mode_models=dict(agent.mode_models),
            clients=dict(agent.clients),
        )


@dataclass
class _FallbackModelRuntime:
    """``modelRuntimeState`` 的最小替身（字段名与 Go 一致）。"""

    client: Any = None
    model: str = ""
    provider: str = ""
    provider_config: Any = None
    providers: dict[str, Any] = field(default_factory=dict)
    mode_models: dict[str, Any] = field(default_factory=dict)
    clients: dict[str, Any] = field(default_factory=dict)


def _build_completion(agent: Agent) -> CompletionService | None:
    try:
        from atri_x.completion import new_service

        return new_service()
    except Exception:  # noqa: BLE001 - 补全源都是可选的
        return None


def _lazy_prompt_builder(opts: Options) -> Any:
    try:
        from atri_x.agent.prompt import new_prompt_builder

        return new_prompt_builder(opts)
    except Exception:  # noqa: BLE001 - 同僚模块未落地时退回最简 prompt
        return None


def new_options(opts: Options) -> Agent:
    """对应 Go ``NewWithOptions``。"""
    return Agent(opts)


def new(
    platform: Any,
    client: Any,
    model: str,
    provider: Any,
    store: Store,
) -> Agent:
    """对应 Go ``New``。"""
    from atri_x.config import ModelSelection
    from atri_x.storage import SessionModeChat, SessionModeWork

    mode_models = {
        SessionModeWork: ModelSelection(provider="default", model=model),
        SessionModeChat: ModelSelection(provider="default", model=model),
    }
    return new_with_prefixes(
        platform, client, mode_models, provider, store, ["/"]
    )


def new_with_prefixes(
    platform: Any,
    client: Any,
    mode_models: dict[str, Any],
    provider: Any,
    store: Store,
    prefixes: list[str],
) -> Agent:
    """对应 Go ``NewWithPrefixes``。"""
    from atri_x.config import default as default_config
    from atri_x.security import default_policy as default_security_policy

    defaults = default_config()
    return new_options(
        Options(
            platform=platform,
            clients={"default": client},
            mode_models=dict(mode_models or {}),
            providers={"default": provider},
            store=store,
            command_prefixes=prefixes,
            session_config=_session_config(defaults),
            llm_request_config=defaults.llm_request,
            ops=defaults.ops,
            security_policy=default_security_policy(),
            context_config=defaults.context,
            session_list_page_size=defaults.view.session_list_page_size,
            cleanup_retention_days=30,
            media_retention_days=defaults.maintenance.sandbox_cleanup.retention_days,
            session_idle_expiration=defaults.session.idle_expiration,
            sandbox_root=defaults.sandbox.root,
            tools_config=defaults.tools,
            angel_memory_config=defaults.angel_memory,
        )
    )


def _session_config(defaults: Any) -> SessionConfig:
    """对应 Go ``NewWithPrefixes`` 里的 ``session.Config`` 字面量。"""
    from atri_x.session import NamingConfig

    return SessionConfig(naming_config=NamingConfig(trigger_step=1), default_mode="work")
