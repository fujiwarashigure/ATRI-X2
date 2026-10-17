"""常驻记忆（``memories.toml``）读写。对应 Go 包：``internal/memory/resident``（``store.go`` / ``atomic.go``）。

======================  ==================================================
模块                     对应 Go / 职责
======================  ==================================================
``entries.py``           ``store.go`` 的内容部分：:class:`Memory`、条目规范化、
                        单位计数、注入渲染（``<resident_memory>`` 边界 + 尖括号转义）
``store.py``             ``store.go`` 的存储部分：:class:`Store` 与上限/频率策略
``atomic.py``            ``atomic.go``：原子落盘（复用
                        :mod:`atri_x.utils.fileops.atomic_write`）
======================  ==================================================

``resident.ErrNotFound`` 在 Go 里是本包私有哨兵；本仓按 ``PORTING.md`` §1.5/§3 的
"哨兵只在 :mod:`atri_x.errors` 定义"约定，直接再导出
:data:`atri_x.errors.ErrNotFound`（与 ``storage.ErrNotFound`` 是同一个对象，便于
跨模块 ``except`` 与 ``is`` 判定）。
"""

from __future__ import annotations

from atri_x.errors import ErrNotFound  # noqa: F401  (对应 Go resident.ErrNotFound)
from atri_x.memory.resident.atomic import write_file_atomic  # noqa: F401
from atri_x.memory.resident.entries import (  # noqa: F401
    NORMAL_INSTRUCTION_PATTERNS,
    RESIDENT_MEMORY_PROMPT_HEADER,
    Memory,
    count_units,
    escape_memory_boundary,
    is_cjk,
    normalize_normal_entries,
    split_normal_entries,
    strip_normal_entry_prefix,
    validate_normal_content,
    wrap_resident_memory_prompt,
)
from atri_x.memory.resident.store import (  # noqa: F401
    DEFAULT_CORE_MAX_UNITS,
    DEFAULT_NORMAL_MAX_UNITS,
    MAX_UPDATE_ATTEMPTS,
    Limits,
    NormalWritePolicy,
    NormalWriteState,
    ResidentMemory,
    Store,
    TomlFile,
    actor_scope,
    find_memory,
    new_store,
    new_store_with_limits,
    new_store_with_options,
    normalize_limits,
    validate_core,
    validate_normal,
    validate_scope,
)

__all__ = [name for name in dir() if not name.startswith("_")]
