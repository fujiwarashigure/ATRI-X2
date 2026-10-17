"""模型服务的共享数据类型与形状。

对应 Go：``internal/agent/commands/register.go`` 里的 ``ModelOption`` /
``ModelProviderError`` / ``ModelListOptions`` / ``ModelListResult`` /
``ModelProfile`` / ``ModelSnapshotSlot`` / ``ModelSnapshot`` / ``ModelService``。

从 ``register.go`` 拆出来是因为这些类型几乎只被 ``/model`` 与 ``/checkmodel``
消费，混在注册机制里会让 :mod:`atri_x.agent.commands.deps` 难以阅读。
:mod:`atri_x.agent.commands.deps` 会再导出它们，按 Go 文件名检索时不会丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ModelListOptions",
    "ModelListResult",
    "ModelOption",
    "ModelProfile",
    "ModelProviderError",
    "ModelService",
    "ModelSnapshot",
    "ModelSnapshotSlot",
]


@dataclass
class ModelOption:
    """一条可选模型。对应 Go ``ModelOption``。"""

    index: int = 0
    provider: str = ""
    model: str = ""
    current: bool = False
    chat_current: bool = False
    work_current: bool = False
    mode_marks: list[str] = field(default_factory=list)
    compact: bool = False
    naming: bool = False


@dataclass
class ModelProviderError:
    """某个 provider 拉取模型列表失败。对应 Go ``ModelProviderError``。"""

    provider: str = ""
    err: Exception | None = None


@dataclass
class ModelListOptions:
    """模型列表查询选项。对应 Go ``ModelListOptions``。"""

    fresh: bool = False


@dataclass
class ModelListResult:
    """模型列表查询结果。对应 Go ``ModelListResult``。"""

    options: list[ModelOption] = field(default_factory=list)
    errors: list[ModelProviderError] = field(default_factory=list)


@dataclass
class ModelProfile:
    """一个命名模型选择（``services.toml`` 的 model_profiles / model_aliases）。

    对应 Go ``ModelProfile``。``available`` 表示该 profile 指向的 provider 在
    当前进程里真的有客户端；不可用的 profile 在 ``@model:<name>`` 与群策略里
    都会被拒绝，所以列表要把两种情况分开显示。
    """

    name: str = ""
    provider: str = ""
    model: str = ""
    available: bool = False


@dataclass
class ModelSnapshotSlot:
    """命名快照里的一个槽位：模式名（chat / work / atriwisp1…）或用途（compact / naming）。

    对应 Go ``ModelSnapshotSlot``。
    """

    label: str = ""
    provider: str = ""
    model: str = ""


@dataclass
class ModelSnapshot:
    """一组命名保存的模型选择（``/model --save``）。

    对应 Go ``ModelSnapshot``：与 model_profiles 不同，profile 只固定一个模型，
    快照记录的是"按模式/用途的一组模型"。
    """

    name: str = ""
    slots: list[ModelSnapshotSlot] = field(default_factory=list)


@runtime_checkable
class ModelService(Protocol):
    """模型选择/快照服务。对应 Go ``ModelService``。

    只用鸭子类型消费；这里声明形状是为了让 ``/model`` 的调用点可以被静态检索到。
    """

    def current_model(self) -> str: ...

    def current_provider(self) -> str: ...

    def current_mode_model(self) -> ModelOption: ...

    def current_model_for_mode(self, mode: str) -> ModelOption: ...

    def current_compact_model(self) -> ModelOption: ...

    def current_naming_model(self) -> ModelOption: ...

    async def select_model(self, ctx: Any, arg: str) -> ModelOption: ...

    async def select_model_for_mode(
        self, ctx: Any, mode: str, arg: str
    ) -> ModelOption: ...

    def select_compact_model(self, arg: str) -> ModelOption: ...

    def select_naming_model(self, arg: str) -> ModelOption: ...

    def models(self, query: str) -> list[ModelOption]: ...

    def model_list(self, query: str, opts: ModelListOptions) -> ModelListResult: ...

    def model_profiles(self) -> list[ModelProfile]: ...

    def model_snapshots(self) -> list[ModelSnapshot]: ...

    def save_model_snapshot(self, name: str) -> None: ...

    async def apply_model_snapshot(self, ctx: Any, name: str) -> ModelSnapshot: ...

    def delete_model_snapshot(self, name: str) -> None: ...
