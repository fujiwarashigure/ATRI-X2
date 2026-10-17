"""运行态（``state.toml``）的启动恢复、热加载与回写。

对应 Go：``internal/agent/model.go`` 的 ``initialStateModTime`` /
``loadRuntimeStateAtStartup`` / ``refreshRuntimeState`` / ``applyRuntimeState`` /
``runtimeStateDigest`` / ``mergeExternalRuntimeState`` / ``saveRuntimeState``。

**与 Go 的落位差异**：Go 把这些方法挂在 ``*Agent`` 上、自己持有 ``statePath`` /
``stateModTime`` / ``stateMu`` / ``stateWriteMu``；本仓已经把这套机制收进
:class:`atri_x.config.RuntimeStateStore`（mtime 门限、分区合并、``[budget]`` 不被
热加载覆盖），所以这里只做「内存 ↔ StateConfig」的搬运与顺序保证：

1. 先合并外部编辑（``merge_external_runtime_state``），再改内存，最后
   ``save_runtime_state`` —— 顺序颠倒会把刚做的改动悄悄丢掉。
2. 回写前先合并一次外部修改（:meth:`RuntimeStateMixin.save_runtime_state` 内部），
   否则会覆盖操作员刚做的编辑。
3. ``[budget]`` 账本由运行中的进程独占：热加载**不**把磁盘副本搬回内存
   （用磁盘副本替换在途预留会丢预留）。

群策略 / 群知识 / 群服务 / 群运行态属于其它模块；这里通过
:meth:`RuntimeStateMixin.apply_extra_runtime_sections` 与
:meth:`RuntimeStateMixin.publish_extra_runtime_sections` 两个钩子交给宿主补。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from atri_x.config import (
    RUNTIME_STATE_SECTIONS,
    ModelSelection,
    RuntimeStateReload,
    RuntimeStateStore,
    StateConfig,
    changed_runtime_state_sections,
    runtime_state_digest,
)
from atri_x.errors import InvalidError
from atri_x.agent.model import audit_event, host_of, install_methods

__all__ = [
    "RUNTIME_STATE_METHODS",
    "RuntimeStateMixin",
    "ensure_state_store",
    "initial_state_mod_time",
    "install_runtime_state",
    "runtime_state_digest_of",
]


def initial_state_mod_time(path: str | os.PathLike[str] | None) -> datetime | None:
    """文件 mtime（UTC aware）；路径为空或读不到时返回 ``None``。对应 Go ``initialStateModTime``。"""
    if not path:
        return None
    try:
        stamp = os.stat(os.fspath(path)).st_mtime_ns
    except OSError:
        return None
    seconds, remainder = divmod(stamp, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder // 1000
    )


def runtime_state_digest_of(value: Any) -> str:
    """单个分区的指纹。对应 Go ``runtimeStateDigestOf``（json.Marshal）。"""
    from atri_x.config.state import canonical_json

    return canonical_json(value)


def ensure_state_store(host: Any) -> RuntimeStateStore | None:
    """取宿主的运行态存储；没有 ``state_path`` 时返回 ``None``。

    宿主可以自己注入 ``self.state_store``（``RuntimeStateStore``）；否则按
    ``self.state_path`` 现场打开一个（等价于 Go 的 ``statePath`` 为空即不落盘）。
    """
    agent = host_of(host)
    store = getattr(agent, "state_store", None)
    if isinstance(store, RuntimeStateStore):
        return store
    path = getattr(agent, "state_path", "") or ""
    if not path:
        return None
    store = RuntimeStateStore.open(path)
    agent.state_store = store
    return store


def _write_lock(host: Any) -> threading.RLock:
    """「合并 → 取快照 → 落盘」全过程用的锁（对应 Go ``stateWriteMu``）。"""
    agent = host_of(host)
    lock = getattr(agent, "state_write_mu", None)
    if lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"):
        return lock
    lock = threading.RLock()
    agent.state_write_mu = lock
    return lock


def install_runtime_state(agent: Any) -> list[str]:
    """把运行态方法装到 Agent 实例上，返回实际安装的名字。

    ``state_watch`` 取 ``agent.refresh_runtime_state``、``group_policy`` 与
    ``group_knowledge`` 取 ``agent.save_runtime_state``，都是鸭子类型调用；Agent 若没有
    继承本 mixin，就必须在装配阶段装上这几个方法。
    """
    return install_methods(agent, RuntimeStateMixin, RUNTIME_STATE_METHODS)


#: 需要装在 Agent 上的运行态方法。
RUNTIME_STATE_METHODS: tuple[str, ...] = (
    "load_runtime_state_at_startup",
    "refresh_runtime_state",
    "merge_external_runtime_state",
    "save_runtime_state",
    "apply_runtime_state",
    "publish_runtime_state",
    "runtime_state_digest",
    "mode_models_snapshot",
    "apply_extra_runtime_sections",
    "publish_extra_runtime_sections",
)


class RuntimeStateMixin:
    """运行态回写与热加载。宿主成员：``state_store``（或 ``state_path``）。

    另外按鸭子类型使用（缺失即跳过）：
    ``self.model_runtime`` / ``self.budget`` / ``self.model_snapshots_store`` /
    ``self.context_overflow_state`` / ``self.context_runtime`` / ``self.sessions`` /
    ``self.audit(event, **attrs)`` / ``self.logger``。
    """

    # -- 启动恢复 -----------------------------------------------------------
    def load_runtime_state_at_startup(self) -> None:
        """启动恢复运行态（含 ``[budget]`` 账本）。对应 Go ``loadRuntimeStateAtStartup``。

        群策略、模式模型、上下文溢出在 app 装配阶段已由 ``config.applyState`` 合并；
        这里补运行态自有分区（群运行态、账本、群知识/服务由宿主钩子补）。
        """
        store = ensure_state_store(self)
        if store is None:
            return
        state = store.state
        budget = getattr(self, "budget", None)
        loader = getattr(budget, "load_from_state", None)
        if callable(loader):
            loader(getattr(state, "budget", None))
        self.apply_runtime_state(state)

    # -- 刷新 ---------------------------------------------------------------
    def refresh_runtime_state(self, force: bool = False) -> RuntimeStateReload:
        """重读 ``state.toml`` 并合并运行态分区。对应 Go ``refreshRuntimeState``。"""
        store = ensure_state_store(self)
        if store is None:
            return RuntimeStateReload(path="")
        return self._refresh_with(store, force)

    def _refresh_with(self, store: RuntimeStateStore, force: bool) -> RuntimeStateReload:
        reload = store.refresh(force)
        if reload.applied:
            self.apply_runtime_state(store.state)
        return reload

    def merge_external_runtime_state(self, trigger: str = "write_back") -> None:
        """吸收外部对 ``state.toml`` 的编辑。对应 Go ``mergeExternalRuntimeState``。

        调用时机必须在**本地改动内存之前**：合并会用文件里的分片覆盖内存。
        """
        store = ensure_state_store(self)
        if store is None:
            return
        try:
            reload = self._refresh_with(store, False)
        except Exception as exc:  # noqa: BLE001 - 合并失败只告警，不能打断命令
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning(
                    "reload externally edited state config",
                    extra={"path": store.path, "error": str(exc)},
                )
            return
        if reload.applied and reload.changed:
            self._audit_runtime_state_reloaded(reload, trigger)

    def _audit_runtime_state_reloaded(
        self, reload: RuntimeStateReload, trigger: str
    ) -> None:
        audit_event(
            self,
            "runtime_state_reloaded",
            path=reload.path,
            sections=",".join(reload.changed),
            trigger=trigger,
            result="succeeded",
        )

    # -- 落盘 ---------------------------------------------------------------
    def save_runtime_state(self) -> None:
        """把运行态写回 ``state.toml``。对应 Go ``saveRuntimeState``。

        ``_state_write_lock`` 覆盖全过程：先合并外部修改、再取快照、最后落盘。
        同一个 ``RuntimeStateStore`` 的 ``save`` 内部还会再合并一次（mtime 未变时是
        空操作），因此这里的合并顺序与 Go 完全一致。
        """
        store = ensure_state_store(self)
        if store is None:
            return
        with _write_lock(self):
            reload = store.refresh(False)
            if reload.applied:
                # 文件更新：与 Go 一致，外部编辑优先（应用到内存后再取快照）。
                self.apply_runtime_state(store.state)
                if reload.changed:
                    self._audit_runtime_state_reloaded(reload, "write_back")
            self.publish_runtime_state(store.state)
            # store.save() 内部还会合并一次；mtime 门限让上一步的合并之后的这次是空操作。
            store.save()

    # -- 分区搬运 -----------------------------------------------------------
    def apply_runtime_state(self, state: StateConfig) -> None:
        """把 ``state`` 的运行态分区合并进内存。对应 Go ``applyRuntimeState``。

        ``[budget]`` 刻意不动（内存独占）。模型选择先整体校验 provider 是否存在，
        任何一个未知 provider 都整条拒绝，避免半个模型集合生效。
        """
        runtime = getattr(self, "model_runtime", None)
        providers = getattr(runtime, "providers", {}) or {}

        for selection in (state.mode_models or {}).values():
            self._require_provider(providers, selection)
        self._require_provider(providers, state.compact_model)
        self._require_provider(providers, state.naming_model)

        if runtime is not None:
            for mode, selection in (state.mode_models or {}).items():
                if not selection.provider or not selection.model:
                    continue
                self.apply_mode_model_selection(
                    mode, selection.provider, selection.model
                )
        if state.compact_model.provider and state.compact_model.model:
            from atri_x.agent.context_runtime import ensure_context_runtime

            ensure_context_runtime(self).set_compact_model(state.compact_model)
        if state.naming_model.provider and state.naming_model.model:
            self.set_naming_model(state.naming_model)
            title_gen = getattr(self, "title_gen", None)
            if title_gen is not None:
                title_gen.set_naming(
                    self.client_for_provider(state.naming_model.provider),
                    state.naming_model.provider,
                    state.naming_model.model,
                )
        from atri_x.agent.context_policy import ensure_context_overflow_state

        ensure_context_overflow_state(self).replace(state.context_overflow)
        from atri_x.agent.model_snapshot import ensure_model_snapshots

        ensure_model_snapshots(self).replace(state.model_snapshots)
        self.apply_extra_runtime_sections(state)

    def publish_runtime_state(self, state: StateConfig) -> None:
        """把内存里的运行态分区写进 ``state``。对应 Go ``saveRuntimeState`` 里的快照段。"""
        from atri_x.agent.context_policy import ensure_context_overflow_state
        from atri_x.agent.model_snapshot import ensure_model_snapshots

        runtime = getattr(self, "model_runtime", None)
        if runtime is not None:
            state.mode_models = runtime.mode_models_snapshot()
        from atri_x.agent.context_runtime import ensure_context_runtime

        state.compact_model = ensure_context_runtime(self).configured_compact_model()
        state.naming_model = self.configured_naming_model()
        state.model_snapshots = ensure_model_snapshots(self).snapshot()
        state.context_overflow = ensure_context_overflow_state(self).snapshot()

        budget = getattr(self, "budget", None)
        writer = getattr(budget, "write_into_state", None)
        if callable(writer):
            writer(state)
        self.publish_extra_runtime_sections(state)

    def runtime_state_digest(self) -> dict[str, str]:
        """当前内存运行态的分区指纹。对应 Go ``runtimeStateDigest``。"""
        store = ensure_state_store(self)
        if store is None:
            return {section: "" for section in RUNTIME_STATE_SECTIONS}
        self.publish_runtime_state(store.state)
        return runtime_state_digest(store.state)

    def mode_models_snapshot(self) -> dict[str, ModelSelection]:
        """模式选择的内存副本。对应 Go ``modeModelsSnapshot``。"""
        runtime = getattr(self, "model_runtime", None)
        if runtime is None:
            return {}
        return runtime.mode_models_snapshot()

    # -- 宿主扩展钩子 -------------------------------------------------------
    def apply_extra_runtime_sections(self, state: StateConfig) -> None:
        """把不属于本组的运行态分区（群策略/群知识/群服务/群运行态）合并进内存。

        默认空实现；宿主（``Agent``）可覆写。
        """

    def publish_extra_runtime_sections(self, state: StateConfig) -> None:
        """把不属于本组的运行态分区写进 ``state``。默认空实现；宿主可覆写。"""

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _require_provider(providers: Mapping[str, Any], selection: ModelSelection) -> None:
        if not selection.provider or not selection.model:
            return
        if selection.provider not in providers:
            raise InvalidError(f'provider "{selection.provider}" not found')


def changed_sections(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """对应 Go ``changedRuntimeStateSections``（薄封装，实现收在 config 层）。"""
    return changed_runtime_state_sections(before, after)
