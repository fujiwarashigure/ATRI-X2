"""常驻记忆存储：``memories.toml`` 的读写、上限与写入频率策略。对应 Go：``internal/memory/resident/store.go``（**拆分说明**：内容规范化、单位计数、
注入渲染与 :class:`Memory` 结果类型在 :mod:`atri_x.memory.resident.entries`；原子落盘
在 :mod:`atri_x.memory.resident.atomic`）。

关键语义（与 Go 逐行一致）：

* ``normal`` 以**换行分隔的独立条目**保存，写入前统一过
  :func:`atri_x.memory.resident.entries.normalize_normal_entries`。
* 落盘走"临时文件 + fsync + rename"（见 :mod:`.atomic`），并在保存前用
  "存在性 + 大小 + mtime"检测外部改动：外部编辑过就整体重载并重试，而不是覆盖它。
* 记忆按 ``(platform, actor_id)`` 隔离；两个字段都为空时拒绝读写。
* 写入频率策略（最小间隔 / 窗口内最大次数）只拦 normal 写入，core 写入不受影响。

异常映射：内容/入参问题抛 :class:`atri_x.errors.InvalidError`；容量与频率限制抛
:class:`atri_x.errors.LimitExceededError`；查不到记忆抛 :data:`atri_x.errors.ErrNotFound`
（对应 Go ``resident.ErrNotFound``，本仓与 ``storage.ErrNotFound`` 是同一个哨兵）；
保存时发现外部改动抛 :data:`atri_x.errors.ErrFileChanged`（对应 Go ``errFileChanged``，
由重试循环吞掉），重试耗尽抛 :class:`atri_x.errors.ConflictError`。

Go 的 ``ctx context.Context`` 只用于入口的 ``ctx.Err()`` 检查，本仓同步路径不进入
签名（与 :mod:`atri_x.storage.repositories` 的约定一致）。
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from atri_x.errors import (
    ConflictError,
    ErrFileChanged,
    ErrNotFound,
    FileChangedError,
    InvalidError,
    LimitExceededError,
)
from atri_x.memory.resident.atomic import write_file_atomic
from atri_x.memory.resident.entries import (
    Memory,
    count_units,
    normalize_normal_entries,
    validate_normal_content,
)
from atri_x.net.tomlw import dumps as toml_dumps
from atri_x.security import Actor
from atri_x.session.types import Scope
from atri_x.storage.times import format_time, now

__all__ = [
    "DEFAULT_CORE_MAX_UNITS",
    "DEFAULT_NORMAL_MAX_UNITS",
    "MAX_UPDATE_ATTEMPTS",
    "Limits",
    "NormalWritePolicy",
    "NormalWriteState",
    "ResidentMemory",
    "Store",
    "TomlFile",
    "actor_scope",
    "find_memory",
    "new_store",
    "new_store_with_limits",
    "new_store_with_options",
    "normalize_limits",
    "validate_core",
    "validate_normal",
    "validate_scope",
]

#: core 上限默认值（字数或单词数）。对应 Go ``DefaultCoreMaxUnits``。
DEFAULT_CORE_MAX_UNITS = 200

#: normal 上限默认值。对应 Go ``DefaultNormalMaxUnits``。
DEFAULT_NORMAL_MAX_UNITS = 300

#: 乐观并发重试上限。对应 Go ``maxUpdateAttempts``。
MAX_UPDATE_ATTEMPTS = 3

#: 保存时的文件权限。对应 Go ``writeFileAtomic(path, data, 0o644)``。
_FILE_PERM = 0o644


@dataclass
class Limits:
    """记忆容量上限（单位数）。对应 Go ``resident.Limits``。"""

    core: int = 0
    normal: int = 0


@dataclass
class NormalWritePolicy:
    """normal 写入策略（零值表示对应护栏关闭）。对应 Go ``NormalWritePolicy``。

    ``normal`` 按"一行一件事"存储，因此 ``max_lines`` 同时约束行数与条目数。
    """

    min_interval: timedelta = timedelta(0)
    window: timedelta = timedelta(0)
    max_writes: int = 0
    max_lines: int = 0
    max_units_per_entry: int = 0
    block_instruction_patterns: bool = False

    def normalized(self) -> "NormalWritePolicy":
        """剔除非法/半成品配置。对应 Go ``NormalWritePolicy.Normalized``。"""
        min_interval = self.min_interval if self.min_interval > timedelta(0) else timedelta(0)
        window = self.window
        max_writes = self.max_writes
        if window <= timedelta(0) or max_writes <= 0:
            window = timedelta(0)
            max_writes = 0
        return NormalWritePolicy(
            min_interval=min_interval,
            window=window,
            max_writes=max_writes,
            max_lines=max(0, self.max_lines),
            max_units_per_entry=max(0, self.max_units_per_entry),
            block_instruction_patterns=bool(self.block_instruction_patterns),
        )


@dataclass
class ResidentMemory:
    """``memories.toml`` 里的一行。对应 Go ``resident.ResidentMemory``（TOML 数组表）。"""

    platform: str = ""
    actor_id: str = ""
    core: str = ""
    normal: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TomlFile:
    """``memories.toml`` 的内容。对应 Go ``tomlFile``。"""

    resident_memories: list[ResidentMemory] = field(default_factory=list)


@dataclass
class NormalWriteState:
    """某个作用域的 normal 写入历史。对应 Go ``normalWriteState``。"""

    last: datetime | None = None
    recent: list[datetime] = field(default_factory=list)


@dataclass
class FileState:
    """文件指纹（存在性 + 大小 + mtime）。对应 Go ``fileState``。"""

    exists: bool = False
    size: int = 0
    mtime_ns: int = 0


@dataclass
class StoreCache:
    """文件内容缓存。对应 Go ``storeCache``。"""

    loaded: bool = False
    file: TomlFile = field(default_factory=TomlFile)
    state: FileState = field(default_factory=FileState)


class Store:
    """常驻记忆存储。对应 Go ``resident.Store``。"""

    def __init__(
        self,
        path: str,
        limits: Limits | None = None,
        policy: NormalWritePolicy | None = None,
    ) -> None:
        self.path = path
        self.limits = normalize_limits(limits or Limits())
        self.normal_policy = (policy or NormalWritePolicy()).normalized()
        self.mu = threading.RLock()
        self.cache = StoreCache()
        self.normal_writes: dict[str, NormalWriteState] = {}

    # -- 读 -----------------------------------------------------------------

    def read(self, scope: Scope) -> Memory:
        """读一个作用域的记忆；不存在时抛 :data:`atri_x.errors.ErrNotFound`。

        对应 Go ``Store.Read``。
        """
        with self.mu:
            file = self.load_locked()
            index = find_memory(file.resident_memories, scope)
            if index < 0:
                raise ErrNotFound
            memory = file.resident_memories[index]
            return Memory(
                core=(memory.core or "").strip(), normal=(memory.normal or "").strip()
            )

    # -- 写 -----------------------------------------------------------------

    def write_core(self, scope: Scope, content: str) -> None:
        """整体覆盖 core。对应 Go ``Store.WriteCore``。"""

        def update(memory: ResidentMemory) -> None:
            memory.core = (content or "").strip()

        self.update(scope, False, update)

    def write_normal(self, scope: Scope, content: str) -> None:
        """整体覆盖 normal（写空串即清空）。对应 Go ``Store.WriteNormal``。"""

        def update(memory: ResidentMemory) -> None:
            memory.normal = (content or "").strip()

        self.update(scope, True, update)

    def append_normal(self, scope: Scope, content: str) -> None:
        """追加一条 normal 条目。对应 Go ``Store.AppendNormal``。"""
        content = (content or "").strip()
        if not content:
            raise InvalidError("resident memory normal content is required")

        def update(memory: ResidentMemory) -> None:
            existing = (memory.normal or "").strip()
            memory.normal = content if not existing else existing + "\n" + content

        self.update(scope, True, update)

    def delete_normal(self, scope: Scope) -> None:
        """清空 normal（等价于 ``write_normal(scope, "")``）。对应 Go ``DeleteNormal``。"""
        return self.write_normal(scope, "")

    def limits_or_default(self) -> Limits:
        """归一化后的上限（未配置时 200/300）。对应 Go ``LimitsOrDefault``。"""
        return normalize_limits(self.limits)

    # -- 事务化更新 ---------------------------------------------------------

    def update(self, scope: Scope, normal: bool, update) -> None:
        """读-改-写一条记忆并原子落盘。对应 Go ``Store.update``。"""
        validate_scope(scope)
        with self.mu:
            for _ in range(MAX_UPDATE_ATTEMPTS):
                file = self.load_locked()
                loaded_state = self.cache.state
                moment = now()
                stamp = format_time(moment)
                index = find_memory(file.resident_memories, scope)
                if index < 0:
                    # 刻意与 Go 不同的一处：Go 用未 trim 的 scope 字段建行，但
                    # findMemory 用 trim 后的值匹配，带空白的 scope 会写出重复行。
                    # 这里落库前统一 trim（记录见汇报里的"有意偏离"）。
                    file.resident_memories.append(
                        ResidentMemory(
                            platform=(scope.platform or "").strip(),
                            actor_id=(scope.actor_id or "").strip(),
                            created_at=stamp,
                        )
                    )
                    index = len(file.resident_memories) - 1
                memory = file.resident_memories[index]
                update(memory)
                memory.core = (memory.core or "").strip()
                if normal:
                    memory.normal = normalize_normal_entries(memory.normal)
                else:
                    memory.normal = (memory.normal or "").strip()
                if normal:
                    validate_normal(memory, self.limits_or_default(), self.normal_policy)
                    self.check_normal_write_policy(scope, moment)
                else:
                    validate_core(memory, self.limits_or_default())
                if not memory.core and not memory.normal:
                    del file.resident_memories[index]
                else:
                    memory.updated_at = stamp
                    if not memory.created_at:
                        memory.created_at = stamp
                    file.resident_memories[index] = memory
                try:
                    self.save_locked(file, loaded_state)
                except FileChangedError:
                    self.cache = StoreCache()
                    continue
                if normal:
                    self.record_normal_write(scope, moment)
                return
        raise ConflictError(
            "resident memory update conflicted with concurrent file changes, please retry"
        )

    # -- 写入频率 -----------------------------------------------------------

    def check_normal_write_policy(self, scope: Scope, moment: datetime) -> None:
        """检查 normal 写入是否被最小间隔/窗口上限拦住。对应 Go ``checkNormalWritePolicy``。"""
        policy = self.normal_policy
        if policy.min_interval <= timedelta(0) and (
            policy.window <= timedelta(0) or policy.max_writes <= 0
        ):
            return
        state = self.normal_writes.get(scope_key(scope))
        if state is None:
            state = NormalWriteState()
        if policy.min_interval > timedelta(0) and state.last is not None:
            elapsed = moment - state.last
            if elapsed < policy.min_interval:
                remaining = policy.min_interval - elapsed
                raise LimitExceededError(
                    "resident memory normal write too frequent: wait "
                    f"{_round_seconds(remaining)}"
                )
        if policy.window > timedelta(0) and policy.max_writes > 0:
            cutoff = moment - policy.window
            count = sum(1 for written_at in state.recent if written_at > cutoff)
            if count >= policy.max_writes:
                raise LimitExceededError(
                    "resident memory normal write limit reached: max "
                    f"{policy.max_writes} per {_duration_text(policy.window)}"
                )

    def record_normal_write(self, scope: Scope, moment: datetime) -> None:
        """记录一次 normal 写入。对应 Go ``recordNormalWrite``。"""
        policy = self.normal_policy
        if policy.min_interval <= timedelta(0) and (
            policy.window <= timedelta(0) or policy.max_writes <= 0
        ):
            return
        key = scope_key(scope)
        state = self.normal_writes.get(key) or NormalWriteState()
        state.last = moment
        if policy.window > timedelta(0) and policy.max_writes > 0:
            cutoff = moment - policy.window
            state.recent = [item for item in state.recent if item > cutoff]
            state.recent.append(moment)
        self.normal_writes[key] = state

    # -- 文件 I/O -----------------------------------------------------------

    def load_locked(self) -> TomlFile:
        """加载文件（必要时重载）并返回可安全修改的副本。对应 Go ``loadLocked``。"""
        path = (self.path or "").strip()
        if not path:
            raise InvalidError("resident memory path is required")
        state = current_file_state(path)
        if self.cache.loaded and same_file_state(self.cache.state, state):
            return clone_file(self.cache.file)
        if not state.exists:
            file = TomlFile()
            self.cache = StoreCache(loaded=True, file=clone_file(file), state=state)
            return file
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise OSError(f"read resident memory {path!r}: {exc}") from exc
        file = parse_toml_file(data, path)
        self.cache = StoreCache(loaded=True, file=clone_file(file), state=state)
        return file

    def save_locked(self, file: TomlFile, expected: FileState) -> None:
        """把内容原子落盘；发现外部改动时抛 :data:`atri_x.errors.ErrFileChanged`。

        对应 Go ``saveLocked``。
        """
        path = (self.path or "").strip()
        if not path:
            raise InvalidError("resident memory path is required")
        current = current_file_state(path)
        if not same_file_state(expected, current):
            raise ErrFileChanged
        payload = {
            "resident_memories": [
                {
                    "platform": memory.platform,
                    "actor_id": memory.actor_id,
                    "core": memory.core,
                    "normal": memory.normal,
                    "created_at": memory.created_at,
                    "updated_at": memory.updated_at,
                }
                for memory in file.resident_memories
            ]
        }
        try:
            data = toml_dumps(payload, sort_keys=False).encode("utf-8")
        except ValueError as exc:
            raise InvalidError(f"marshal resident memory: {exc}") from exc
        try:
            write_file_atomic(path, data, _FILE_PERM)
        except OSError as exc:
            raise OSError(f"write resident memory {path!r}: {exc}") from exc
        self.cache = StoreCache(
            loaded=True, file=clone_file(file), state=current_file_state(path)
        )


# -- 构造 helper（对应 Go 的三个构造函数） ----------------------------------


def new_store(path: str) -> Store:
    """默认存储。对应 Go ``NewStore``。"""
    return Store(path, Limits(), NormalWritePolicy())


def new_store_with_limits(path: str, limits: Limits) -> Store:
    """带上限的存储。对应 Go ``NewStoreWithLimits``。"""
    return Store(path, limits, NormalWritePolicy())


def new_store_with_options(
    path: str, limits: Limits, policy: NormalWritePolicy
) -> Store:
    """完整选项的存储。对应 Go ``NewStoreWithOptions``。"""
    return Store(path, limits, policy)


def actor_scope(actor: Actor) -> Scope:
    """由 Actor 得到记忆作用域（平台 + actor）。对应 Go ``resident.ActorScope``。"""
    return Scope(actor_id=actor.id, platform=actor.platform)


# -- 纯函数 helper ----------------------------------------------------------


def normalize_limits(limits: Limits) -> Limits:
    """上限归一化（<=0 时用默认值）。对应 Go ``normalizeLimits``。"""
    core = limits.core if limits.core > 0 else DEFAULT_CORE_MAX_UNITS
    normal = limits.normal if limits.normal > 0 else DEFAULT_NORMAL_MAX_UNITS
    return Limits(core=core, normal=normal)


def validate_core(memory: ResidentMemory, limits: Limits) -> None:
    """校验 core 长度。对应 Go ``validateCore``。"""
    units = count_units(memory.core)
    if units > limits.core:
        raise LimitExceededError(
            f"resident memory core is too long: {units}/{limits.core} units"
        )


def validate_normal(memory: ResidentMemory, limits: Limits, policy: NormalWritePolicy) -> None:
    """校验 normal 长度与内容。对应 Go ``validateNormal``。"""
    units = count_units(memory.normal)
    if units > limits.normal:
        raise LimitExceededError(
            f"resident memory normal is too long: {units}/{limits.normal} units"
        )
    validate_normal_content(memory.normal, policy)


def validate_scope(scope: Scope) -> None:
    """校验作用域（平台与 actor 都不能为空）。对应 Go ``validateScope``。"""
    if not (scope.platform or "").strip():
        raise InvalidError("resident memory platform scope is required")
    if not (scope.actor_id or "").strip():
        raise InvalidError("resident memory actor scope is required")


def find_memory(memories: list[ResidentMemory], scope: Scope) -> int:
    """按 ``(platform, actor_id)`` 定位记忆行；没有返回 -1。对应 Go ``findMemory``。"""
    platform = (scope.platform or "").strip()
    actor_id = (scope.actor_id or "").strip()
    for index, memory in enumerate(memories):
        if memory.platform == platform and memory.actor_id == actor_id:
            return index
    return -1


def scope_key(scope: Scope) -> str:
    """写入频率状态的内层键。对应 Go ``scopeKey``。"""
    return (scope.platform or "").strip() + "\x00" + (scope.actor_id or "").strip()


def current_file_state(path: str) -> FileState:
    """当前文件指纹。对应 Go ``currentFileState``。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return FileState()
    except OSError as exc:
        raise OSError(f"stat resident memory {path!r}: {exc}") from exc
    return FileState(exists=True, size=info.st_size, mtime_ns=info.st_mtime_ns)


def same_file_state(left: FileState, right: FileState) -> bool:
    """两个文件指纹是否相同。对应 Go ``sameFileState``。"""
    return (
        left.exists == right.exists
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
    )


def clone_file(file: TomlFile) -> TomlFile:
    """复制文件内容，避免调用方改到缓存。对应 Go ``cloneFile``。"""
    return TomlFile(resident_memories=[replace(memory) for memory in file.resident_memories])


def parse_toml_file(data: bytes, path: str) -> TomlFile:
    """解析 ``memories.toml``。对应 Go ``toml.Unmarshal(data, &file)``。"""
    text = data.decode("utf-8")
    if not text.strip():
        return TomlFile()
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidError(f"parse resident memory {path!r}: {exc}") from exc
    entries = parsed.get("resident_memories")
    if entries is None:
        return TomlFile()
    if not isinstance(entries, list):
        raise InvalidError(f"parse resident memory {path!r}: resident_memories must be a list")
    memories: list[ResidentMemory] = []
    for item in entries:
        if not isinstance(item, dict):
            raise InvalidError(
                f"parse resident memory {path!r}: resident_memories entries must be tables"
            )
        memories.append(
            ResidentMemory(
                platform=_string_field(item, "platform", path),
                actor_id=_string_field(item, "actor_id", path),
                core=_string_field(item, "core", path),
                normal=_string_field(item, "normal", path),
                created_at=_string_field(item, "created_at", path),
                updated_at=_string_field(item, "updated_at", path),
            )
        )
    return TomlFile(resident_memories=memories)


def _string_field(item: dict, key: str, path: str) -> str:
    value = item.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidError(
            f"parse resident memory {path!r}: field {key} must be a string"
        )
    return value


def _round_seconds(value: timedelta) -> str:
    """把剩余时间四舍五入到秒（Go 的 ``Duration.Round(time.Second)``）。"""
    return f"{round(value.total_seconds())}s"


def _duration_text(value: timedelta) -> str:
    """时长文本（Go 的 ``Duration.String()`` 的近似：只用于错误消息）。"""
    seconds = int(value.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h0m0s"
    if seconds % 60 == 0:
        return f"{seconds // 60}m0s"
    return f"{seconds}s"
