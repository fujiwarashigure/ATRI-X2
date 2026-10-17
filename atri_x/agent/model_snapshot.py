"""命名模型快照（``/model --save`` / ``--apply`` / ``--delete``）。

对应 Go：``internal/agent/model_snapshot.go``。

快照与 ``model_profiles`` 的分工：profile 只固定**一个**模型，快照记录的是
「按模式/用途的一组模型」。两者同名时同一个名字会有两种含义，所以
:meth:`ModelSnapshotMixin.save_model_snapshot` 会拒绝与静态 profile 重名，
:meth:`ModelSnapshotMixin.apply_model_snapshot` 也会给出可操作的提示。

**顺序语义**（Go 注释里反复强调，不能改）：

* 应用快照时先**整体校验** provider 与协议兼容性，再动手 —— 半个模型集合比不改更糟。
* 写入前先 ``merge_external_runtime_state()``，否则会用内存里已过期的快照改模型，
  紧接着的落盘又把内存覆盖回文件内容。
* 落盘失败要**回滚内存**，避免「命令报失败、重启后又消失」或「命令报失败、本次进程
  里却已生效」这种两头不一致。

成员改名：Go 的未导出字段 ``modelSnapshots`` 在 Python 里叫
``model_snapshots_store``（导出方法 ``model_snapshots()`` 占用了这个名字）。
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from atri_x.agent.model import ModelSnapshot, ModelSnapshotSlot, host_of
from atri_x.agent.model_list import model_profile_name_exists
from atri_x.config import ModelSelection, StateModelSnapshot
from atri_x.errors import ErrModelSnapshotNameEmpty, InvalidError
from atri_x.storage.models import SessionModeChat, SessionModeWork

#: 快照名长度上限（按 rune 计）。对应 Go ``modelSnapshotNameLimit``。
MODEL_SNAPSHOT_NAME_LIMIT = 32

#: 应用快照时的固定顺序。对应 Go ``modelSnapshotModeOrder``。
MODEL_SNAPSHOT_MODE_ORDER: tuple[str, ...] = (
    SessionModeChat,
    "atriwisp1",
    "atriwisp2",
    "atriwisp3",
    SessionModeWork,
)

#: 可以装到 Agent 实例上的方法。
SNAPSHOT_METHODS: tuple[str, ...] = (
    "model_snapshots",
    "save_model_snapshot",
    "apply_model_snapshot",
    "delete_model_snapshot",
    "current_compact_selection",
    "set_compact_selection",
)

__all__ = [
    "MODEL_SNAPSHOT_MODE_ORDER",
    "MODEL_SNAPSHOT_NAME_LIMIT",
    "SNAPSHOT_METHODS",
    "ModelSnapshotMixin",
    "ModelSnapshotStore",
    "clone_model_snapshot",
    "ensure_model_snapshots",
    "install_snapshot_services",
    "model_snapshot_view",
    "normalize_model_snapshot_name",
    "ordered_model_snapshot_modes",
]


def normalize_model_snapshot_name(name: str) -> str:
    """校验并归一化快照名。对应 Go ``normalizeModelSnapshotName``。

    字符集刻意收窄到「字母/数字/下划线/连字符」（含中文等 Unicode 字母）：名字要作为
    TOML 键落盘、在命令回复里显示，收窄比事后处理转义便宜。
    """
    name = (name or "").strip()
    if not name:
        raise ErrModelSnapshotNameEmpty
    runes = list(name)
    if len(runes) > MODEL_SNAPSHOT_NAME_LIMIT:
        raise InvalidError(
            f"快照名最长 {MODEL_SNAPSHOT_NAME_LIMIT} 个字符，"
            f'"{name}" 有 {len(runes)} 个'
        )
    for char in runes:
        if char.isalpha() or char.isdecimal() or char in ("_", "-"):
            continue
        raise InvalidError(
            f'快照名 "{name}" 含有不允许的字符 "{char}"；'
            "只能用字母、数字、下划线、连字符"
        )
    return name


def ordered_model_snapshot_modes(
    mode_models: Mapping[str, ModelSelection] | None,
) -> list[str]:
    """固定顺序的模式列表（未列出的按名字排序跟在后面）。对应 Go ``orderedModelSnapshotModes``。"""
    mode_models = mode_models or {}
    modes: list[str] = []
    seen: set[str] = set()
    for mode in MODEL_SNAPSHOT_MODE_ORDER:
        if mode not in mode_models:
            continue
        modes.append(mode)
        seen.add(mode)
    rest = sorted(mode for mode in mode_models if mode not in seen)
    return modes + rest


def model_snapshot_view(name: str, snapshot: StateModelSnapshot) -> ModelSnapshot:
    """把落盘结构转成命令层显示的形状。对应 Go ``modelSnapshotView``。"""
    view = ModelSnapshot(name=name)
    for mode in ordered_model_snapshot_modes(snapshot.mode_models):
        selection = snapshot.mode_models[mode]
        if not selection.provider or not selection.model:
            continue
        view.slots.append(
            ModelSnapshotSlot(
                label=mode, provider=selection.provider, model=selection.model
            )
        )
    for label, selection in (
        ("compact", snapshot.compact_model),
        ("naming", snapshot.naming_model),
    ):
        if not selection.provider or not selection.model:
            continue
        view.slots.append(
            ModelSnapshotSlot(
                label=label, provider=selection.provider, model=selection.model
            )
        )
    return view


def clone_model_snapshot(snapshot: StateModelSnapshot) -> StateModelSnapshot:
    """深拷贝一份快照（模式表也复制）。对应 Go ``cloneModelSnapshot``。"""
    cloned = StateModelSnapshot(
        compact_model=snapshot.compact_model.copy()
        if hasattr(snapshot.compact_model, "copy")
        else ModelSelection(),
        naming_model=snapshot.naming_model.copy()
        if hasattr(snapshot.naming_model, "copy")
        else ModelSelection(),
    )
    for mode, selection in (snapshot.mode_models or {}).items():
        cloned.mode_models[mode] = (
            selection.copy() if hasattr(selection, "copy") else selection
        )
    return cloned


class ModelSnapshotStore:
    """命名快照的内存表。对应 Go ``Agent.modelSnapshots`` + ``modelSnapshotsMu``。"""

    def __init__(self, snapshots: Mapping[str, StateModelSnapshot] | None = None) -> None:
        self.mu = threading.RLock()
        self.items: dict[str, StateModelSnapshot] = {
            name: clone_model_snapshot(snapshot)
            for name, snapshot in (snapshots or {}).items()
        }

    def get(self, name: str) -> StateModelSnapshot | None:
        with self.mu:
            snapshot = self.items.get(name)
            return None if snapshot is None else clone_model_snapshot(snapshot)

    def names(self) -> list[str]:
        with self.mu:
            return sorted(self.items.keys())

    def put(self, name: str, snapshot: StateModelSnapshot) -> None:
        with self.mu:
            self.items[name] = clone_model_snapshot(snapshot)

    def delete(self, name: str) -> bool:
        """删除并返回它此前是否存在。"""
        with self.mu:
            return self.items.pop(name, None) is not None

    def snapshot(self) -> dict[str, StateModelSnapshot]:
        """内存副本，供落盘。对应 Go ``modelSnapshotsSnapshot``（空表返回 ``None``）。"""
        with self.mu:
            if not self.items:
                return {}
            return {
                name: clone_model_snapshot(value) for name, value in self.items.items()
            }

    def replace(self, snapshots: Mapping[str, StateModelSnapshot] | None) -> None:
        """用文件里的内容替换内存副本。对应 Go ``setModelSnapshotsSnapshot``。"""
        with self.mu:
            self.items = {
                name: clone_model_snapshot(snapshot)
                for name, snapshot in (snapshots or {}).items()
            }


def ensure_model_snapshots(host: Any) -> ModelSnapshotStore:
    """取宿主的快照表；缺失时补建（补建在真正的宿主上，门面不持有第二份）。"""
    agent = host_of(host)
    store = getattr(agent, "model_snapshots_store", None)
    if isinstance(store, ModelSnapshotStore):
        return store
    store = ModelSnapshotStore()
    agent.model_snapshots_store = store
    return store


def install_snapshot_services(agent: Any) -> list[str]:
    """把快照方法装到 Agent 实例上。"""
    from atri_x.agent.model import install_methods

    return install_methods(agent, ModelSnapshotMixin, SNAPSHOT_METHODS)


class ModelSnapshotMixin:
    """``internal/agent/model_snapshot.go`` 里挂在 ``*Agent`` 上的方法。"""

    def model_snapshots(self) -> list[ModelSnapshot]:
        """列出已保存的命名快照（按名字排序）。对应 Go ``ModelSnapshots``。"""
        store = ensure_model_snapshots(self)
        return [
            model_snapshot_view(name, store.get(name))
            for name in store.names()
        ]

    def save_model_snapshot(self, name: str) -> None:
        """把当前的模型选择（各模式 + 压缩 + 命名）保存为命名快照。

        对应 Go ``SaveModelSnapshot``。
        """
        key = normalize_model_snapshot_name(name)
        if model_profile_name_exists(self, key):
            raise InvalidError(
                f'名字 "{key}" 已经是 services.toml 里的模型 profile/别名；'
                "快照名要与 @model:<名字> 用的名字区分开"
            )
        snapshot = StateModelSnapshot(
            mode_models=self.mode_models_snapshot(),
            compact_model=self.current_compact_selection(),
            naming_model=self.configured_naming_model(),
        )
        if not snapshot.mode_models:
            raise InvalidError("当前没有可保存的模型选择")
        # 先吸收外部编辑再改内存：反过来会把刚保存的快照丢掉。
        self.merge_external_runtime_state()
        store = ensure_model_snapshots(self)
        previous = store.get(key)
        existed = previous is not None
        store.put(key, snapshot)
        try:
            self.save_runtime_state()
        except Exception:
            # 内存里已经写进去了，但文件里没有：回滚内存。
            if existed and previous is not None:
                store.put(key, previous)
            else:
                store.delete(key)
            raise

    def apply_model_snapshot(self, ctx: Any, name: str) -> ModelSnapshot:
        """把命名快照里的模型选择整体切回去。对应 Go ``ApplyModelSnapshot``。"""
        key = normalize_model_snapshot_name(name)
        # 先合并外部编辑：否则用过期的快照改模型，落盘的会是"没应用"的状态。
        self.merge_external_runtime_state()
        store = ensure_model_snapshots(self)
        snapshot = store.get(key)
        if snapshot is None:
            if model_profile_name_exists(self, key):
                raise InvalidError(
                    f'"{key}" 是静态模型 profile，不是快照：它只固定一个模型，'
                    f"用 `/model --profiles` 查看，或先用 `/model --save {key}` "
                    "把当前选择存成快照"
                )
            raise InvalidError(
                f'没有名为 "{key}" 的模型快照；用 `/model --snapshots` 查看已保存的快照'
            )

        mods = ordered_model_snapshot_modes(snapshot.mode_models)
        providers = getattr(getattr(self, "model_runtime", None), "providers", {}) or {}
        # 先整体校验 provider，再动手：半个模型集合比不改更糟。
        for mode in mods:
            selection = snapshot.mode_models[mode]
            if selection.provider not in providers:
                raise InvalidError(
                    f'快照 "{key}" 的 {mode} 指向未知 provider "{selection.provider}"'
                )
            # 与 /model 同一个裁决：当前会话正在用的模式不能被快照悄悄切到不兼容的协议。
            self.check_model_switch(ctx, mode, selection.provider, selection.model)
        for label, selection in (
            ("compact", snapshot.compact_model),
            ("naming", snapshot.naming_model),
        ):
            if selection.provider and selection.provider not in providers:
                raise InvalidError(
                    f'快照 "{key}" 的 {label} 指向未知 provider "{selection.provider}"'
                )

        for mode in mods:
            selection = snapshot.mode_models[mode]
            self.apply_mode_model_selection(mode, selection.provider, selection.model)
        if snapshot.compact_model.provider and snapshot.compact_model.model:
            self.set_compact_selection(snapshot.compact_model)
        if snapshot.naming_model.provider and snapshot.naming_model.model:
            self.set_naming_model(snapshot.naming_model)
            title_gen = getattr(self, "title_gen", None)
            if title_gen is not None:
                title_gen.set_naming(
                    self.client_for_provider(snapshot.naming_model.provider),
                    snapshot.naming_model.provider,
                    snapshot.naming_model.model,
                )
        self.save_runtime_state()
        return model_snapshot_view(key, snapshot)

    def delete_model_snapshot(self, name: str) -> None:
        """删除命名快照。对应 Go ``DeleteModelSnapshot``（不存在时报错）。"""
        key = normalize_model_snapshot_name(name)
        self.merge_external_runtime_state()
        store = ensure_model_snapshots(self)
        existed = store.delete(key)
        if not existed:
            raise InvalidError(f'没有名为 "{key}" 的模型快照')
        self.save_runtime_state()

    # -- 与 context_runtime 的两个薄桥（避免在本模块重复实现选择解析） --------
    def current_compact_selection(self) -> ModelSelection:
        from atri_x.agent.context_runtime import ensure_context_runtime

        return ensure_context_runtime(self).configured_compact_model()

    def set_compact_selection(self, selection: ModelSelection) -> None:
        from atri_x.agent.context_runtime import ensure_context_runtime

        ensure_context_runtime(self).set_compact_model(selection)
